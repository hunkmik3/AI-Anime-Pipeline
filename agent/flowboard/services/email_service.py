"""Outbound email over SMTP (Google Workspace).

Stdlib ``smtplib`` only — no extra dependency. Config comes from the env:

    FLOWBOARD_SMTP_HOST      smtp.gmail.com
    FLOWBOARD_SMTP_PORT      587 (STARTTLS) or 465 (implicit TLS)
    FLOWBOARD_SMTP_USER      you@sleepygiant.studio
    FLOWBOARD_SMTP_PASSWORD  a Google **App Password** (not the login password)
    FLOWBOARD_SMTP_FROM      "Giant Studio <you@sleepygiant.studio>"  (optional)
    FLOWBOARD_APP_URL        https://giantstudio.reelmind.co  (link in the mail)

Design rule: email is best-effort and must NEVER swallow the action that
triggered it. ``send()`` returns False (and logs) instead of raising, so an
approval still succeeds when the mail bounces — the caller surfaces the
credentials to the admin to relay by hand.
"""
from __future__ import annotations

import logging
import os
import smtplib
import ssl
from email.message import EmailMessage
from typing import Optional

logger = logging.getLogger(__name__)


def _env(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()


def app_url() -> str:
    return _env("FLOWBOARD_APP_URL", "http://localhost:8101").rstrip("/")


def is_configured() -> bool:
    """True when enough SMTP config exists to attempt a send."""
    return bool(_env("FLOWBOARD_SMTP_HOST") and _env("FLOWBOARD_SMTP_USER")
                and _env("FLOWBOARD_SMTP_PASSWORD"))


def _from_addr() -> str:
    return _env("FLOWBOARD_SMTP_FROM") or _env("FLOWBOARD_SMTP_USER")


def send(to: str, subject: str, body: str) -> bool:
    """Send a plain-text mail. Returns True on success, False on any failure
    (never raises — see the module docstring)."""
    if not is_configured():
        logger.warning("email not sent to %s: SMTP not configured", to)
        return False

    host = _env("FLOWBOARD_SMTP_HOST")
    port = int(_env("FLOWBOARD_SMTP_PORT", "587") or "587")
    user = _env("FLOWBOARD_SMTP_USER")
    password = _env("FLOWBOARD_SMTP_PASSWORD")

    msg = EmailMessage()
    msg["From"] = _from_addr()
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)

    try:
        ctx = ssl.create_default_context()
        if port == 465:  # implicit TLS
            with smtplib.SMTP_SSL(host, port, context=ctx, timeout=20) as s:
                s.login(user, password)
                s.send_message(msg)
        else:  # STARTTLS (587)
            with smtplib.SMTP(host, port, timeout=20) as s:
                s.ehlo()
                s.starttls(context=ctx)
                s.login(user, password)
                s.send_message(msg)
        logger.info("email sent to %s (%s)", to, subject)
        return True
    except Exception as exc:  # noqa: BLE001 — best-effort by design
        logger.error("email to %s failed: %s", to, exc)
        return False


# ── templates ────────────────────────────────────────────────────────────────


def send_credentials(to: str, username: str, password: str,
                     display_name: Optional[str] = None) -> bool:
    """Approved-signup mail: the account + its temporary password."""
    who = (display_name or "").strip() or username
    body = f"""Hi {who},

Your Giant Studio account has been approved.

    Sign in:   {app_url()}
    Username:  {username}
    Password:  {password}

For security you'll be asked to set a new password the first time you sign in.
This temporary password only works until you change it.

If you didn't request this account, please ignore this email.

— Giant Studio
"""
    return send(to, "Your Giant Studio account is ready", body)


def send_rejected(to: str, display_name: Optional[str] = None) -> bool:
    """Optional courtesy mail when an admin declines a signup."""
    who = (display_name or "").strip() or "there"
    body = f"""Hi {who},

Thanks for your interest in Giant Studio. Your access request wasn't approved
at this time.

If you think this is a mistake, please contact your studio administrator.

— Giant Studio
"""
    return send(to, "About your Giant Studio access request", body)
