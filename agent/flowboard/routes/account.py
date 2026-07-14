"""Account/session routes — app login (Phase 9 multi-user).

Distinct from the Flow-identity routes under /api/auth (that's the Chrome
extension's Google profile). These are the app's own admin-provisioned logins.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel

from flowboard.db.models import User
from flowboard.routes.deps import get_current_user
from flowboard.services import audit_service, auth, budget_service, sso, user_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/account", tags=["account"])


class LoginBody(BaseModel):
    username: str
    password: str


class ChangePasswordBody(BaseModel):
    current_password: str
    new_password: str


@router.post("/login")
def login(body: LoginBody, request: Request) -> dict:
    ip = audit_service.client_ip(request)
    username = body.username.strip()
    user = user_service.get_by_username(username)
    # Same generic error for unknown user / wrong password / suspended, so we
    # don't leak which usernames exist. Lockout is the one distinct signal.
    if user is None:
        audit_service.record("login.failed", target_label=username, ip=ip, detail="unknown user")
        raise HTTPException(status_code=401, detail="invalid credentials")
    if user_service.is_locked(user):
        audit_service.record("login.locked", target=user, ip=ip)
        raise HTTPException(
            status_code=429, detail="too many failed attempts — try again later"
        )
    if user.status != "active" or not auth.verify_password(body.password, user.password_hash):
        user_service.register_failed_login(user.id)
        audit_service.record("login.failed", target=user, ip=ip)
        raise HTTPException(status_code=401, detail="invalid credentials")
    # Success: clear lockout, stamp last_login, upgrade the hash if it's below
    # the current cost, then issue a token stamped with the account's tv.
    user_service.register_successful_login(user.id, rehash_password=body.password)
    fresh = user_service.get_by_id(user.id) or user
    audit_service.record("login.success", actor=fresh, ip=ip)
    return {
        "token": auth.make_token(str(fresh.id), fresh.token_version),
        "user": user_service.public_dict(fresh),
    }


@router.post("/change-password")
def change_password(
    body: ChangePasswordBody, request: Request, user: User = Depends(get_current_user)
) -> dict:
    """Self-service password change. Verifies the current password, enforces the
    strength policy, clears the force-change flag, and returns a FRESH token so
    the current session stays signed in (other sessions are revoked)."""
    try:
        user_service.change_own_password(user.id, body.current_password, body.new_password)
    except user_service.UserError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    audit_service.record("password.change", actor=user, ip=audit_service.client_ip(request))
    fresh = user_service.get_by_id(user.id) or user
    return {
        "token": auth.make_token(str(fresh.id), fresh.token_version),
        "user": user_service.public_dict(fresh),
    }


def _sso_redirect(fragment: str) -> RedirectResponse:
    """Send the browser back to the SPA login page with the result in the URL
    fragment (fragments aren't sent to servers / logged). The SPA reads it."""
    base = sso.frontend_url() or ""
    return RedirectResponse(f"{base}/login#{fragment}", status_code=302)


@router.get("/sso/google/start")
def sso_google_start() -> RedirectResponse:
    if not sso.is_configured():
        raise HTTPException(status_code=404, detail="SSO not configured")
    return RedirectResponse(sso.authorization_url(sso.sign_state()), status_code=302)


@router.get("/sso/google/callback")
async def sso_google_callback(
    request: Request, code: str = "", state: str = "", error: str = ""
) -> RedirectResponse:
    if not sso.is_configured():
        raise HTTPException(status_code=404, detail="SSO not configured")
    ip = audit_service.client_ip(request)
    if error or not code:
        return _sso_redirect("sso_error=google_denied")
    if not sso.verify_state(state):
        return _sso_redirect("sso_error=bad_state")
    try:
        claims = await sso.exchange_code(code)
    except sso.SSOError as exc:
        logger.warning("SSO exchange failed: %s", exc)
        return _sso_redirect("sso_error=exchange_failed")
    email = (claims.get("email") or "").lower()
    if not sso.email_allowed(email, email_verified=bool(claims.get("email_verified"))):
        audit_service.record("sso.denied", target_label=email, ip=ip, detail="domain not allowed")
        return _sso_redirect("sso_error=domain_not_allowed")
    try:
        user = user_service.get_or_create_sso_user(email, claims.get("name"))
    except user_service.UserError:
        audit_service.record("sso.denied", target_label=email, ip=ip, detail="account disabled")
        return _sso_redirect("sso_error=account_disabled")
    audit_service.record("sso.login", actor=user, ip=ip)
    token = auth.make_token(str(user.id), user.token_version)
    return _sso_redirect(f"sso_token={token}")


@router.get("/me")
def me(user: User = Depends(get_current_user)) -> dict:
    d = user_service.public_dict(user)
    summ = budget_service.summary(user.id)
    if summ:
        d["available_usd"] = summ["available_usd"]
        d["reserved_usd"] = summ["reserved_usd"]
    return d
