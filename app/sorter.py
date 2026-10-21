"""Automatické třídění do `sorted/{cz,en,jp}` — náhrada za cron `sorter.py`.

Běží uvnitř služby na pozadí (každých SORTER_INTERVAL s). Pravidla:

  * ruční override (`manual_overrides.json`) vždy vyhrává, ffprobe se nespouští;
  * film/seriál: CZ audio -> CZ, EN audio -> EN, ani jedno -> EN (fallback);
    položka, kterou někdo dal do `sorted/cz` ručně a CZ stopu nemá, zůstane jen v CZ;
  * anime: JP audio -> JP, EN audio nebo nic -> EN;
  * soubor změněný před méně než SETTLE_SECONDS se ještě stahuje — jeho linky
    zůstanou, jak jsou, a ffprobe počká;
  * cokoliv v `sorted/{lang}/{Kategorie}`, co nikam nepatří, se smaže.

Měření ffprobem běží mimo zámek (může trvat), samotné linkování a úklid pod
`lock`, který sdílí i ruční zásahy z UI — jinak by úklid mohl smazat link,
který UI právě vytvořilo.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path

from . import library, overrides, probe
from .config import CATEGORIES, LANGS, Category

log = logging.getLogger(__name__)

SETTLE_SECONDS = 120

lock = threading.Lock()          # sdílený s ručními zásahy v service.py
_run_lock = threading.Lock()     # dva běhy naráz nikdy
events: deque[tuple[str, str, str]] = deque(maxlen=300)   # (čas, úroveň, zpráva)
last_run: dict = {}


def _event(level: str, message: str) -> None:
    events.appendleft((datetime.now().strftime("%d.%m. %H:%M:%S"), level, message))
    log.log(logging.WARNING if level == "warn" else logging.INFO, message)


def auto_targets(cat: Category, langs: list[str], in_cz: bool = False) -> list[str]:
    """Kam položku pošle automatika podle jazyků audio stop."""
    has = set(probe.classify(langs))
    if cat.key == "anime":
        out = {"jp"} if "jp" in has else set()
        if "en" in has or "jp" not in has:
            out.add("en")
    elif "cz" in has:
        out = {"cz"} | ({"en"} if "en" in has else set())
    elif in_cz:
        out = {"cz"}             # ručně vložené do CZ bez CZ stopy (štítek UND apod.)
    else:
        out = {"en"}
    return [lang for lang in LANGS if lang in out]


def _groups(cat: Category):
    """(vzorek, soubory) — film je skupina sám za sebe, u show každá složka s videi."""
    root = library.source_root(cat)
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        videos = sorted(Path(dirpath) / f for f in filenames
                        if not f.startswith(".") and library.is_video(Path(f)))
        if not videos:
            continue
        if cat.kind == "movie":
            for v in videos:
                yield v, [v]
        else:
            # Největší soubor = epizoda, ne znělka/extra s jinými stopami.
            yield max(videos, key=lambda v: (library._size_of(v), v)), videos


def _override_for(cat: Category, sample: Path, ovr: dict) -> list[str] | None:
    key = sample if cat.kind == "movie" else library.show_root_of(cat, sample)
    entry = ovr.get(str(key))
    return None if entry is None else list(entry.get("targets") or [])


def _settling(files: list[Path], now: float) -> bool:
    return any(now - library._mtime_of(f) < SETTLE_SECONDS for f in files)


def _langs(sample: Path, db: dict, label: str, stats: dict) -> list[str]:
    entry = probe.db_entry(db, sample)
    if probe.entry_is_fresh(entry, sample):
        return probe.entry_langs(entry)
    try:
        langs = probe.run_ffprobe(sample)
    except RuntimeError as exc:
        _event("warn", f"ffprobe selhal u {label}: {exc}")
        stats["errors"] += 1
        langs = []
    # Ukládá se i neúspěch — jinak by se rozbitý soubor měřil každý běh znovu.
    probe.store_result(sample, langs)
    db[str(sample)] = {"mtime": library._mtime_of(sample), "langs": langs}
    stats["probed"] += 1
    _event("info", f"Změřeno: {label} -> {', '.join(langs).upper() or 'UND'}")
    return langs


def _label(cat: Category, sample: Path) -> str:
    rel = sample.relative_to(library.source_root(cat))
    return str(rel) if cat.kind == "movie" else str(rel.parent if rel.parent.parts else rel)


def run_once() -> dict:
    """Jeden běh. Vrací statistiku; když už jiný běh jede, vrátí {'busy': True}."""
    if not _run_lock.acquire(blocking=False):
        return {"busy": True}
    try:
        return _run()
    finally:
        _run_lock.release()


def _run() -> dict:
    started = time.monotonic()
    stats = {"created": 0, "removed": 0, "probed": 0, "errors": 0, "settling": 0}
    cats = [c for c in CATEGORIES.values() if library.source_root(c).is_dir()]
    missing = [c.dirname for c in CATEGORIES.values() if c not in cats]
    if missing:
        # Chybějící zdroj (odpojený mount) nesmí vést k úklidu celé jeho větve.
        _event("warn", f"Chybí {', '.join(missing)} — tyto větve sorted/ se neuklízí")

    # 1) ffprobe mimo zámek — nové soubory, které nemají override a už se nestahují.
    db = probe.load_db()
    ovr = overrides.load_all()
    now = time.time()
    for cat in cats:
        for sample, files in _groups(cat):
            if _override_for(cat, sample, ovr) is None and not _settling(files, now):
                _langs(sample, db, _label(cat, sample), stats)

    # 2) linkování a úklid pod zámkem, s čerstvě načteným override store.
    with lock:
        ovr = overrides.load_all()
        valid: set[Path] = set()
        for cat in cats:
            for sample, files in _groups(cat):
                _place(cat, sample, files, ovr, db, valid, stats, now)
        for cat in cats:
            for lang in LANGS:
                stats["removed"] += _cleanup(library.sorted_root(cat, lang), valid)

    stats["seconds"] = round(time.monotonic() - started, 1)
    stats["finished"] = datetime.now().strftime("%d.%m. %H:%M:%S")
    if stats["created"] or stats["removed"]:
        _event("info", f"Hotovo: +{stats['created']} linků, -{stats['removed']} linků "
                       f"({stats['seconds']} s)")
    last_run.clear()
    last_run.update(stats)
    return stats


def _place(cat, sample, files, ovr, db, valid, stats, now) -> None:
    label = _label(cat, sample)
    targets = _override_for(cat, sample, ovr)
    if targets is None:
        if _settling(files, now):
            # Ještě se stahuje: nech existující linky být, rozhodne se později.
            stats["settling"] += 1
            for f in files:
                for lang in LANGS:
                    m = library.mirror_path(cat, lang, f)
                    if m.exists():
                        valid.add(m)
            return
        in_cz = library.mirror_path(cat, "cz", sample).exists()
        targets = auto_targets(cat, _langs(sample, db, label, stats), in_cz)
        if cat.key != "anime":
            # Ručně vložené epizody v CZ zůstávají i mimo vzorek (jako ve starém sorteru).
            valid.update(m for f in files if (m := library.mirror_path(cat, "cz", f)).exists())

    created = 0
    for f in files:
        for lang in targets:
            target = library.mirror_path(cat, lang, f)
            valid.add(target)
            try:
                created += library._link_one(f, target)
            except OSError as exc:
                stats["errors"] += 1
                _event("warn", f"Nelze vytvořit hardlink {target}: {exc}")
    if created:
        stats["created"] += created
        _event("info", f"Zařazeno: {label} -> {'+'.join(targets).upper()} (+{created})")


def _cleanup(branch: Path, valid: set[Path]) -> int:
    if not branch.is_dir():
        return 0
    removed = 0
    for dirpath, _dirnames, filenames in os.walk(branch, topdown=False):
        for name in filenames:
            path = Path(dirpath) / name
            if path not in valid:
                try:
                    os.remove(path)
                    removed += 1
                    _event("info", f"Odstraněn link: {path.relative_to(branch.parent.parent)}")
                except OSError as exc:
                    _event("warn", f"Nelze smazat {path}: {exc}")
        if Path(dirpath) != branch:
            try:
                os.rmdir(dirpath)
            except OSError:
                pass
    return removed
