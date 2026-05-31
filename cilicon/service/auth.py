"""cilicon dashboard auth — GitHub OAuth + a signed, stateless session cookie.

We log users in with the App's own OAuth (one identity provider, and it lines
up exactly with the repos the App can see). The session is a signed cookie
(itsdangerous) carrying the user's id, login, and the GitHub org logins they
belong to — that org-login set is how we scope which projects they may view,
without a DB round-trip on every request.
"""
from __future__ import annotations

import secrets
from typing import Optional

from itsdangerous import BadSignature, URLSafeSerializer

from .settings import settings

COOKIE = "cilicon_session"
STATE_COOKIE = "cilicon_oauth_state"


def _serializer() -> URLSafeSerializer:
    return URLSafeSerializer(settings().session_secret, salt="cilicon-session")


def new_state() -> str:
    return secrets.token_urlsafe(24)


def encode_session(data: dict) -> str:
    return _serializer().dumps(data)


def decode_session(raw: Optional[str]) -> Optional[dict]:
    if not raw:
        return None
    try:
        return _serializer().loads(raw)
    except BadSignature:
        return None


def current_user(request) -> Optional[dict]:
    """The logged-in user dict, or None. Read off the signed cookie."""
    return decode_session(request.cookies.get(COOKIE))


def can_see_org_login(user: Optional[dict], org_login: str) -> bool:
    if not user:
        return False
    return org_login in (user.get("org_logins") or [])
