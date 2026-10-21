"""ffprobe wrapper + čtení/zápis cache `sorter_db.json`.

Plní ji sorter (`app/sorter.py`) a tlačítko „Přeanalyzovat“; stránky z ní jen
čtou, aby se ffprobe nepouštěl při každém načtení. Formát `{mtime, langs, name}`.
"""

from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path

from .config import LANG_CODES, LANGS, get_settings
from .jsonstore import mutate_json, read_json

log = logging.getLogger(__name__)


def load_db() -> dict[str, dict]:
    return read_json(get_settings().sorter_db, {})


def db_entry(db: dict, path: str | Path) -> dict | None:
    entry = db.get(str(path))
    return entry if isinstance(entry, dict) else None


def entry_langs(entry: dict | None) -> list[str]:
    """Vytáhne pole jazyků z db záznamu, tolerantně k formátu (list i string)."""
    if not entry:
        return []
    langs = entry.get("langs")
    if isinstance(langs, str):
        langs = [langs]
    if not isinstance(langs, list):
        return []
    return [str(x).lower() for x in langs if x]


def entry_is_fresh(entry: dict | None, path: Path) -> bool:
    """True, pokud cache odpovídá aktuálnímu mtime souboru."""
    if not entry or "mtime" not in entry:
        return False
    try:
        return abs(float(entry["mtime"]) - path.stat().st_mtime) < 1.0
    except (OSError, TypeError, ValueError):
        return False


def classify(langs: list[str]) -> list[str]:
    """Které z našich cílů (cz/en/jp) audio stopy pokrývají."""
    lowered = {str(x).lower().strip() for x in langs}
    return [lang for lang in LANGS if lowered & LANG_CODES[lang]]


def run_ffprobe(path: Path) -> list[str]:
    """Vrátí kódy jazyků všech audio stop. Neznámé stopy hlásí jako `und`."""
    settings = get_settings()
    binary = settings.ffprobe_binary()
    if binary is None:
        raise RuntimeError(
            f"ffprobe nenalezen ({settings.ffprobe_path!r}). Nainstaluj ffmpeg "
            "nebo nastav FFPROBE_PATH v .env."
        )
    cmd = [
        binary, "-v", "error",
        "-select_streams", "a",
        "-show_entries", "stream_tags=language",
        "-of", "json",
        str(path),
    ]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True,
            timeout=settings.ffprobe_timeout, check=False
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
        raise RuntimeError(f"ffprobe selhal: {exc}") from exc
    if proc.returncode != 0:
        raise RuntimeError(f"ffprobe selhal: {proc.stderr.strip()[:300]}")

    try:
        streams = json.loads(proc.stdout or "{}").get("streams", [])
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"ffprobe vrátil nečitelný výstup: {exc}") from exc

    langs: list[str] = []
    for stream in streams:
        lang = (stream.get("tags") or {}).get("language") or "und"
        langs.append(str(lang).lower())
    return langs


def store_result(path: Path, langs: list[str]) -> None:
    """Zapíše výsledek do `sorter_db.json` ve formátu, který používá sorter."""
    try:
        mtime = path.stat().st_mtime
    except OSError:
        mtime = 0.0

    def _apply(db: dict):
        # Merge, ne replace: kdyby si sorter do záznamu ukládal ještě něco
        # dalšího, přepsáním celého dictu bychom mu to potichu smazali.
        entry = db.get(str(path))
        entry = dict(entry) if isinstance(entry, dict) else {}
        entry.update({"mtime": mtime, "langs": langs, "name": path.name})
        db[str(path)] = entry

    mutate_json(get_settings().sorter_db, _apply)


def forget(paths: list[Path]) -> int:
    """Odstraní záznamy smazaných souborů z cache."""
    wanted = {str(p) for p in paths}

    def _apply(db: dict):
        hit = [k for k in db if k in wanted]
        for k in hit:
            del db[k]
        return len(hit) or False

    return int(mutate_json(get_settings().sorter_db, _apply) or 0)


def move_prefix(old: Path, new: Path) -> int:
    """Přejmenuje záznamy po přesunu složky, ať se nemusí měřit znovu."""
    prefix = str(old).rstrip("/") + "/"

    def _apply(db: dict):
        hit = [k for k in db if k == str(old) or k.startswith(prefix)]
        for k in hit:
            db[str(new) + k[len(str(old)):]] = db.pop(k)
        return len(hit) or False

    return int(mutate_json(get_settings().sorter_db, _apply) or 0)


def analyze(path: Path) -> list[str]:
    """Spustí ffprobe a rovnou uloží do cache."""
    langs = run_ffprobe(path)
    store_result(path, langs)
    return langs
