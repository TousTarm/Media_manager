"""Jellyfin refresh — fire-and-forget.

Uživatelská odpověď na Jellyfin nikdy nečeká a jeho výpadek nesmí shodit
mazání ani uložení override. Chyba se jen zaloguje.
"""

from __future__ import annotations

import asyncio
import logging

import httpx

from .config import get_settings

log = logging.getLogger(__name__)

_background: set[asyncio.Task] = set()


async def _refresh() -> None:
    settings = get_settings()
    if not settings.jellyfin_api_key:
        log.info("JELLYFIN_API_KEY není nastaven, refresh přeskočen")
        return
    url = settings.jellyfin_url.rstrip("/") + "/Library/Refresh"
    try:
        async with httpx.AsyncClient(timeout=settings.jellyfin_timeout) as client:
            resp = await client.post(
                url, headers={"X-Emby-Token": settings.jellyfin_api_key}
            )
        if resp.status_code >= 400:
            log.warning("Jellyfin refresh vrátil HTTP %s", resp.status_code)
        else:
            log.info("Jellyfin refresh odpálen (HTTP %s)", resp.status_code)
    except Exception as exc:  # noqa: BLE001 — nikdy nesmí probublat k uživateli
        log.warning("Jellyfin refresh selhal: %s", exc)


def trigger_refresh() -> None:
    """Odpálí refresh na pozadí a okamžitě se vrátí."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        # Volání mimo event loop by refresh tiše zahodilo — to se musí ozvat.
        log.warning("Jellyfin refresh přeskočen: volání mimo event loop")
        return
    task = loop.create_task(_refresh())
    _background.add(task)
    task.add_done_callback(_background.discard)
