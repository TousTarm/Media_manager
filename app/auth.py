"""Přihlášení přes formulář + podepsaná session cookie (jedno sdílené heslo z `.env`).

Cookie nese jen čas vypršení a HMAC podpis. Klíčem podpisu je samo heslo,
takže změna APP_PASSWORD rovnou odhlásí všechna zařízení.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status

from .config import get_settings

COOKIE = "mm_session"
MAX_AGE = 30 * 24 * 3600


class LoginRequired(Exception):
    """Chybí nebo neplatí session — main.py z toho udělá redirect na /login."""


def _key() -> bytes:
    s = get_settings()
    return f"{s.app_user}\0{s.app_password}".encode("utf-8")


def _sign(expires: int) -> str:
    return hmac.new(_key(), str(expires).encode(), hashlib.sha256).hexdigest()


def make_session() -> str:
    expires = int(time.time()) + MAX_AGE
    return f"{expires}.{_sign(expires)}"


def session_valid(value: str | None) -> bool:
    if not value or not get_settings().app_password:
        return False
    expires, _, sig = value.partition(".")
    if not expires.isdigit() or int(expires) < time.time():
        return False
    return hmac.compare_digest(sig, _sign(int(expires)))


def check_credentials(username: str, password: str) -> bool:
    settings = get_settings()
    if not settings.app_password:
        return False
    # Obě porovnání se provedou vždy, aby doba odpovědi neprozradila, co nesedělo.
    user_ok = secrets.compare_digest(username.encode("utf-8"), settings.app_user.encode("utf-8"))
    pass_ok = secrets.compare_digest(password.encode("utf-8"), settings.app_password.encode("utf-8"))
    return user_ok and pass_ok


def require_auth(request: Request) -> str:
    if not get_settings().app_password:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="APP_PASSWORD není nastaveno — aplikace odmítá běžet bez hesla.",
        )
    if not session_valid(request.cookies.get(COOKIE)):
        raise LoginRequired()
    return get_settings().app_user


Auth = Annotated[str, Depends(require_auth)]
