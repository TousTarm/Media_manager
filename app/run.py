"""Spouštěč — `python -m app.run`.

Existuje proto, aby APP_HOST, APP_PORT a LOG_LEVEL z `.env` opravdu něco
dělaly. Kdyby se uvicorn spouštěl z příkazové řádky s natvrdo zadaným portem
(jak to má systemd unit ve specifikaci), byly by ty tři klíče v `.env` jen
dekorace a jejich změna by neměla žádný efekt.
"""

from __future__ import annotations

import sys

import uvicorn

from .config import get_settings


def main() -> int:
    settings = get_settings()
    if not settings.env_file_found:
        print(
            "VAROVÁNÍ: .env nenalezen, běžím na výchozích hodnotách. "
            "Zkopíruj .env.example na .env.",
            file=sys.stderr,
        )
    uvicorn.run(
        "app.main:app",
        host=settings.app_host,
        port=settings.app_port,
        log_level=settings.log_level.lower(),
        proxy_headers=True,
        forwarded_allow_ips="*",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
