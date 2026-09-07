"""Manual override store pro `sorter.py` — nakopíruj vedle sorter.py.

Záměrně bez závislostí (stdlib only) a bez importu z `app/`, aby cron skript
nepotřeboval venv aplikace. Formát souboru je společný s Media Managerem, viz
`app/overrides.py`.

Použití v `sorter.py` — celý potřebný diff:

    from sorter_overrides import load_overrides, override_key, lookup

    # v process(), jednou na začátku běhu:
    overrides = load_overrides(MEDIA_ROOT)

    # v iteraci WATCH_LIST, TĚSNĚ PŘED voláním ffprobe:
    key = override_key(item, path)          # path = cesta k souboru (movie)
                                            #        nebo k sample souboru (folder scan)
    targets = lookup(overrides, key)
    if targets is not None:
        final_cz, final_en = "cz" in targets, "en" in targets
        log(f"Override aktivní: {os.path.basename(key)} -> {targets or ['nikam']}")
    else:
        ... původní ffprobe větev ...
        final_cz, final_en = has_cz, has_en
"""

from __future__ import annotations

import json
import os

OVERRIDES_FILENAME = "manual_overrides.json"

# Jazykové větve v sorted/. "jp" přibylo pro anime s anglickým dabingem —
# sorter sám ho nikdy nenastaví, ale musí ho respektovat, když ho nastaví appka.
TARGETS = ("cz", "en", "jp")


def load_overrides(media_root: str) -> dict:
    """Načte override store. Chybějící nebo rozbitý soubor = prázdný dict."""
    path = os.path.join(media_root, OVERRIDES_FILENAME)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        print(f"VAROVANI: {path} nelze precist ({exc}), overrides se ignoruji")
        return {}
    return data if isinstance(data, dict) else {}


def show_root(source_root: str, path: str) -> str:
    """Kořen show = první úroveň bezprostředně pod `source_root`.

    `path` může být cesta k sample souboru hluboko v sezóně; klíč do override
    store je vždy složka celého seriálu, ne sezóna a ne sample soubor.
    """
    source_root = os.path.abspath(source_root).rstrip(os.sep)
    rel = os.path.relpath(os.path.abspath(path), source_root)
    first = rel.split(os.sep)[0]
    if first in ("", os.curdir, os.pardir):
        return os.path.abspath(path)
    return os.path.join(source_root, first)


def override_key(item: dict, path: str) -> str:
    """Klíč pro danou položku WATCH_LISTu.

    `item` je prvek WATCH_LIST se `source` (kořen Movies/Series/Anime) a `type`
    ("movie" vs. cokoliv jiného = show). U filmu je klíčem přesná cesta k
    souboru, u seriálu/anime kořenová složka show.
    """
    if item.get("type") == "movie":
        return os.path.abspath(path)
    return show_root(item["source"], path)


def lookup(overrides: dict, key: str) -> list | None:
    """Vrátí seznam cílů, nebo None když override neexistuje.

    Pozor: prázdný seznam `[]` je platné rozhodnutí "nikam nedávat", ne
    absence override — proto `is not None`, ne pravdivostní test.
    """
    entry = overrides.get(key)
    if not isinstance(entry, dict):
        return None
    targets = entry.get("targets")
    if not isinstance(targets, list):
        return None
    return [str(t).lower() for t in targets if str(t).lower() in TARGETS]
