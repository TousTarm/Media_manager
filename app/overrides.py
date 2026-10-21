"""Manual override store — `/media/jellyfin/manual_overrides.json`.

Klíč je absolutní cesta:
  * film        -> přesná cesta k video souboru
  * series/anime-> cesta ke kořenové složce show

Hodnota: {"targets": ["cz"|"en", ...], "set_at": ISO8601 UTC}
Prázdné `targets` = "nikam nedávat", což je platné rozhodnutí, ne absence
override. Odstranění override se dělá přes `clear_override()`.

Čte ho i automatický sorter (`app/sorter.py`).
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from .config import LANGS, get_settings
from .jsonstore import mutate_json, read_json


def _path() -> Path:
    return get_settings().overrides_file


def normalize_targets(targets: list[str] | tuple[str, ...] | None) -> list[str]:
    """Očistí vstup od uživatele na kanonické pořadí a povolené hodnoty."""
    if not targets:
        return []
    lowered = {str(t).strip().lower() for t in targets}
    unknown = lowered - set(LANGS)
    if unknown:
        raise ValueError(f"Neznámý cíl: {', '.join(sorted(unknown))}")
    return [lang for lang in LANGS if lang in lowered]


def load_all() -> dict[str, dict]:
    raw = read_json(_path(), {})
    out: dict[str, dict] = {}
    for key, value in raw.items():
        if isinstance(value, dict) and isinstance(value.get("targets"), list):
            out[key] = value
    return out


def get_override(key: str | Path) -> dict | None:
    return load_all().get(str(key))


def set_override(key: str | Path, targets: list[str]) -> dict:
    entry = {
        "targets": normalize_targets(targets),
        "set_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }

    def _apply(data: dict):
        data[str(key)] = entry

    mutate_json(_path(), _apply)
    return entry


def clear_override(key: str | Path) -> bool:
    """Smaže override pro klíč. Vrací True, pokud tam nějaký byl."""

    def _apply(data: dict):
        return data.pop(str(key), None) is not None

    return bool(mutate_json(_path(), _apply))


def move_prefix(old: str | Path, new: str | Path) -> int:
    """Po přesunu složky přejmenuje klíče pod ní (stejně jako probe.move_prefix)."""
    old, new = str(old), str(new)

    def _apply(data: dict):
        hit = [k for k in data if k == old or k.startswith(old.rstrip("/") + "/")]
        for k in hit:
            data[new + k[len(old):]] = data.pop(k)
        return len(hit) or False

    return int(mutate_json(_path(), _apply) or 0)


def clear_overrides_under(prefix: str | Path) -> int:
    """Smaže override pro cestu i všechny cesty pod ní (mazání celé show)."""
    prefix_str = str(prefix)

    def _apply(data: dict):
        doomed = [
            k for k in data
            if k == prefix_str or k.startswith(prefix_str.rstrip("/") + "/")
        ]
        for k in doomed:
            del data[k]
        return len(doomed) or False

    return int(mutate_json(_path(), _apply) or 0)
