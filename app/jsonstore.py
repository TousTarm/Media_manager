"""Atomické čtení/zápis JSON souborů sdílených se `sorter.py`.

`sorter_db.json` i `manual_overrides.json` může současně číst cron. Každý zápis
proto jde přes tmp soubor + `os.replace()`, aby čtenář nikdy neviděl půlku
souboru. Zápisy z aplikace navíc serializuje procesní zámek a vždy se dělá
read-modify-write, aby se nepřepsalo, co mezitím zapsal sorter.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger(__name__)

_locks: dict[Path, threading.Lock] = {}
_locks_guard = threading.Lock()


def _lock_for(path: Path) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault(path, threading.Lock())


def read_json(path: Path, default: Any = None) -> Any:
    if default is None:
        default = {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return default
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("Nelze načíst %s (%s), používám prázdný obsah", path, exc)
        return default
    return data if isinstance(data, type(default)) else default


def write_json(path: Path, data: Any) -> None:
    tmp = path.with_name(f".{path.name}.tmp.{os.getpid()}")
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2, sort_keys=True)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def mutate_json(path: Path, mutator: Callable[[dict], Any]) -> Any:
    """Načte, nechá `mutator` upravit dict na místě, atomicky uloží.

    Návratová hodnota `mutator` se propaguje ven. Pokud vrátí `False`, zápis
    se přeskočí (nic se nezměnilo).
    """
    with _lock_for(path):
        data = read_json(path, {})
        result = mutator(data)
        if result is not False:
            write_json(path, data)
        return result
