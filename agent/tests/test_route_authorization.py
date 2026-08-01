"""Every /api route must authorize the object it names.

This is the regression guard for the audit that found 42 ungated endpoints. The
app grew from a single-user tool, so its older routes looked a row up by id and
acted on it; with sequential integer ids on Node, Edge and Request, that let any
authenticated employee walk 1..N through another team's canvas — and queue
billable generation against their credit budget.

Fixing those 42 by hand does not stop the 43rd. So this test enumerates the live
route table and fails when a handler neither authorizes nor is on the explicit
public list below. A new route is guilty until it says otherwise, which means the
decision has to be made deliberately rather than forgotten.

If this fails on a route you just added, the fix is one of:
  * call a ``resource_guard.authorize_*`` helper (the usual answer), or
  * call ``permissions.require`` / ``require_scene`` directly, or
  * add it to ``PUBLIC_ROUTES`` with a comment saying why it is safe.
"""
from __future__ import annotations

import inspect
import re

import pytest

from flowboard.main import app

#: Symbols that constitute "this handler authorized the caller". Any one of them
#: appearing in the handler's source counts. Bare names are listed alongside the
#: qualified ones because either import style is fine — matching only
#: ``resource_guard.x`` would fail a handler that did ``from ... import x``.
_AUTHZ_MARKERS = (
    "authorize_",
    "require_unscoped",
    # The weakest real gate: a shared workspace, open to every account but not to
    # the public. Only Flow Studio (/giantflow) uses it — see the docstring on
    # resource_guard.require_signed_in for what it does and does not protect.
    "require_signed_in",
    # A list endpoint authorizes by NARROWING its query rather than refusing —
    # 403-ing a whole feed because one row is out of reach would be useless.
    "is_unscoped",
    "visible_scope",
    "_visible_scene_ids",
    "permissions.require",
    "require_admin",
    "require_structure_admin",
    "owner_scope",
    "user_can_access_project",
    "_gate_project",
    "_gate_scene",
    "_gate_shot",
    "_gate",
)

#: Routes that are public on purpose. Each needs a reason — if you cannot write
#: one, the route probably is not safe.
PUBLIC_ROUTES: dict[str, str] = {
    # Health + auth handshake: must work before anyone has a session.
    "GET /api/health": "liveness probe, exposes no user data",
    "POST /api/account/login": "issues the session in the first place",
    "POST /api/account/register": "self-service signup → lands in the approval queue",
    "GET /api/account/sso/google/start": "OAuth handshake, pre-session",
    "GET /api/account/sso/google/callback": "OAuth handshake, pre-session",
    # The extension callback authenticates with its own shared secret, checked
    # inside the handler rather than via a user session.
    "POST /api/ext/callback": "authenticated by X-Callback-Secret, not a user token",
}

#: Routes that act ONLY on the caller's own account or their own assigned work.
#: They need authentication (the global middleware provides it) but there is no
#: other object to authorize against — the caller IS the scope.
SELF_SCOPED_ROUTES: dict[str, str] = {
    "GET /api/account/me": "returns the caller's own account",
    "GET /api/account/notifications": "the caller's own notifications",
    "POST /api/account/change-password": "changes the caller's own password",
    "GET /api/auth/me": "returns the caller's own account",
    "POST /api/auth/logout": "revokes the caller's own session",
    "GET /api/my/episodes": "filters to episodes assigned to the caller",
    "GET /api/review/queue": "filters to submissions awaiting the caller",
}

#: Routes whose authorization lives in the service layer rather than the handler.
#: Listed explicitly so the exemption is a decision, not an oversight — each was
#: read and confirmed.
SERVICE_AUTHORIZED_ROUTES: dict[str, str] = {
    "POST /api/scenes/{scene_id}/submissions":
        "submission_service.submit enforces assignee-only (nobody else may deliver)",
    "POST /api/submissions/{submission_id}/approve":
        "submission_service resolves the approver chain and forbids self-review",
    "POST /api/submissions/{submission_id}/reject":
        "same approver chain, plus a mandatory reason",
}

#: Routes addressed by an OPAQUE media id (a content hash), not a guessable
#: number, and which act only on the caller's own upload/download. Authentication
#: is enforced by the global middleware; there is no project to authorize
#: against because the asset is not yet attached to one.
#:
#: This reasoning depends on ids staying unguessable — which is why
#: GET /api/media/_debug/assets, the one route that listed them all, is
#: admin-only. If a listing route is ever added back, revisit every line here.
OPAQUE_ID_ROUTES: dict[str, str] = {
    "GET /api/media/{media_id}/status": "cache-warm check on an opaque id",
    "POST /api/media/{media_id}/downloaded": "records the caller's own download",
    "POST /api/media/{media_id}/extract-frame": "derives a still into the caller's own assets",
    "POST /api/upload": "creates a new asset with no project attachment yet",
    "POST /api/upload-audio": "as above",
    "POST /api/upload-video": "as above",
    "POST /api/upload-url": "as above; fetches a URL server-side",
    "POST /api/vision/describe": "describes an opaque media id the caller supplies",
    "POST /api/auth/scan": "extension connection diagnostic, no studio data",
}

#: Read-only capability lists — what the installation can do, not anyone's data.
CATALOGUE_ROUTES: dict[str, str] = {
    "GET /api/video/models": "static list of configured video models",
    "GET /api/audio/seed/available": "whether an audio provider is configured",
    "GET /api/llm/providers": "which LLM providers exist (no secrets returned)",
    "GET /api/llm/config": "which provider each feature uses (no secrets)",
}

#: Path prefixes that are public: raw bytes loaded by <img>/<video> `src`, which
#: cannot carry an Authorization header. Gating these would break every
#: thumbnail and video in the app. They serve opaque ids and no listing.
PUBLIC_PREFIXES: tuple[str, ...] = (
    "/media/",
    # Thumbnails render in <img> tags, which cannot send an Authorization
    # header. main.py's auth gate exempts this path for the same reason; gating
    # it here would break every cover image in the app.
    "/api/media/{media_id}/thumb",
)


_EXEMPTION_TABLES = ()  # populated below, after every table is defined


def _router_level_deps(r) -> str:
    """Names of dependencies attached to the route itself (or inherited from its
    router). ``/api/admin`` gates every route with one ``Depends(require_admin)``
    on the APIRouter, which is stronger than a per-handler check — this test must
    see it, or it would demand redundant guards.
    """
    names = []
    for dep in getattr(r, "dependencies", None) or []:
        call = getattr(dep, "dependency", None)
        if call is not None:
            names.append(getattr(call, "__name__", ""))
    tree = getattr(r, "dependant", None)
    for sub in getattr(tree, "dependencies", None) or []:
        call = getattr(sub, "call", None)
        if call is not None:
            names.append(getattr(call, "__name__", ""))
    return " ".join(names)


def _api_routes():
    """(method, path, endpoint_fn, deps) for every /api route, minus the SPA catch-all."""
    out = []
    for r in app.routes:
        path = getattr(r, "path", "")
        methods = getattr(r, "methods", None) or set()
        fn = getattr(r, "endpoint", None)
        if fn is None or not path.startswith(("/api", "/media")):
            continue
        deps = _router_level_deps(r)
        for m in sorted(methods - {"HEAD", "OPTIONS"}):
            out.append((m, path, fn, deps))
    return sorted(out, key=lambda t: (t[1], t[0]))


def _authorizes(fn) -> bool:
    try:
        src = inspect.getsource(fn)
    except (OSError, TypeError):  # pragma: no cover - C-implemented handler
        return False
    return any(marker in src for marker in _AUTHZ_MARKERS)


def _is_public(method: str, path: str) -> bool:
    key = f"{method} {path}"
    for table in (
        PUBLIC_ROUTES,
        SELF_SCOPED_ROUTES,
        CATALOGUE_ROUTES,
        SERVICE_AUTHORIZED_ROUTES,
        OPAQUE_ID_ROUTES,
    ):
        if key in table:
            return True
    return any(path.startswith(p) for p in PUBLIC_PREFIXES)


_EXEMPTION_TABLES = (
    PUBLIC_ROUTES,
    SELF_SCOPED_ROUTES,
    CATALOGUE_ROUTES,
    SERVICE_AUTHORIZED_ROUTES,
    OPAQUE_ID_ROUTES,
)


def test_the_route_table_is_not_empty():
    """Guard the guard: a broken import would make every assertion below vacuous."""
    assert len(_api_routes()) > 50


def test_admin_router_is_gated_as_a_whole():
    """The admin console is protected by one dependency on its router. If that
    ever comes off, dozens of routes silently open at once — so pin it here
    rather than relying on each handler."""
    admin = [r for r in _api_routes() if r[1].startswith("/api/admin")]
    assert admin, "no admin routes found — did the router move?"
    for method, path, _fn, deps in admin:
        assert "require_admin" in deps, f"{method} {path} lost its admin gate"


@pytest.mark.parametrize(
    "method,path,fn,deps",
    _api_routes(),
    ids=[f"{m} {p}" for m, p, _, _ in _api_routes()],
)
def test_route_authorizes_or_is_explicitly_public(method, path, fn, deps):
    if _is_public(method, path):
        return
    # A router-level require_admin covers every route under it.
    if any(marker in deps for marker in ("require_admin", "require_structure_admin")):
        return
    assert _authorizes(fn), (
        f"{method} {path} ({fn.__module__}.{fn.__name__}) performs no authorization.\n"
        "Call a resource_guard.authorize_* helper, or add it to PUBLIC_ROUTES "
        "with a reason if it is genuinely safe to expose."
    )


def test_every_exemption_carries_a_reason():
    """An entry with no reason is how a temporary exemption becomes permanent."""
    for table in _EXEMPTION_TABLES:
        for route, reason in table.items():
            assert reason.strip(), f"{route} is exempted with no justification"


def test_exemptions_are_not_stale():
    """An exemption for a route that no longer exists hides the fact that the
    list was never revisited — and would silently cover a future route that
    happens to reuse the path."""
    live = {f"{m} {p}" for m, p, _, _ in _api_routes()}
    for table in _EXEMPTION_TABLES:
        for route in table:
            assert route in live, f"{route} is exempted but no longer exists"


def test_no_handler_leaks_existence_with_403(monkeypatch):
    """Object-scoped denials must be 404.

    A 403 confirms the id exists, which is precisely what an enumeration attack
    is looking for. ``resource_guard`` raises 404 throughout; this pins that so a
    later "clearer error message" change doesn't quietly undo it.
    """
    from flowboard.services import resource_guard

    exc = resource_guard._deny("node")
    assert exc.status_code == 404


def test_guard_covers_every_object_type_with_an_id_route():
    """Each addressable object type needs a guard, or its routes have nothing to
    call and the gap comes back."""
    from flowboard.services import resource_guard

    for obj in (
        "project", "scene", "series", "shot", "node", "edge", "request",
        "submission", "reference",
    ):
        assert hasattr(resource_guard, f"authorize_{obj}"), f"no guard for {obj}"
