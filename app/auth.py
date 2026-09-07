"""HTTP Basic Auth — jedno sdílené heslo z `.env`."""

from __future__ import annotations

import secrets
from typing import Annotated

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from .config import get_settings

_security = HTTPBasic(realm="Media Manager")


def require_auth(
    credentials: Annotated[HTTPBasicCredentials, Depends(_security)]
) -> str:
    settings = get_settings()
    if not settings.app_password:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="APP_PASSWORD není nastaveno — aplikace odmítá běžet bez hesla.",
        )

    # Obě porovnání se provedou vždy, aby doba odpovědi neprozradila,
    # která polovina neseděla.
    user_ok = secrets.compare_digest(
        credentials.username.encode("utf-8"), settings.app_user.encode("utf-8")
    )
    pass_ok = secrets.compare_digest(
        credentials.password.encode("utf-8"), settings.app_password.encode("utf-8")
    )
    if not (user_ok and pass_ok):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Nesprávné přihlašovací údaje",
            headers={"WWW-Authenticate": 'Basic realm="Media Manager"'},
        )
    return credentials.username


Auth = Annotated[str, Depends(require_auth)]
