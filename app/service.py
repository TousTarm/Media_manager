"""Aplikační vrstva nad `library` — to, co volají HTML i JSON routy."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from . import diagnostics, library, overrides, probe, sorter
from .config import Category, get_settings
from .jellyfin import trigger_refresh

log = logging.getLogger(__name__)

# Kolik souborů se u seriálu reálně měří ffprobem při "Přeanalyzovat".
# Sorter sám pracuje se sample souborem, nemá smysl projíždět 200 epizod.
SHOW_PROBE_SAMPLES = 1


@dataclass
class Detail:
    category: Category
    name: str
    abs_path: str
    rel: str
    is_dir: bool
    kind: str                       # "movie" | "show"
    override_key: str
    langs: list[str] = field(default_factory=list)          # skutečné hardlinky
    probe_langs: list[str] = field(default_factory=list)    # z ffprobe cache
    probe_stale: bool = True
    probe_source: str | None = None
    auto_langs: list[str] = field(default_factory=list)     # co by řekla automatika
    override_targets: list[str] | None = None
    override_set_at: str | None = None
    file_count: int = 0
    total_bytes: int = 0

    @property
    def has_override(self) -> bool:
        return self.override_targets is not None

    @property
    def effective_targets(self) -> list[str]:
        """Co má být v UI předvyplněné: override, jinak skutečný stav."""
        return self.override_targets if self.has_override else self.langs


def get_detail(abs_path: str) -> Detail:
    cat, path = library.locate(abs_path)
    if not path.exists():
        raise library.PathError(f"Neexistující položka: {abs_path}")

    subject = library.subject_of(cat, path)
    key = subject
    ov = overrides.get_override(key)
    videos = library.video_files_under(subject)

    detail = Detail(
        category=cat,
        name=subject.parent.name if (cat.kind == "movie" and subject.parent != library.source_root(cat)) else subject.name,
        abs_path=str(subject),
        rel=str(subject.resolve().relative_to(library.source_root(cat).resolve())),
        is_dir=subject.is_dir(),
        kind=cat.kind,
        override_key=str(key),
        langs=library.linked_langs(cat, subject),
        file_count=len(videos),
        total_bytes=sum(library._size_of(p) for p in videos),
    )
    if ov is not None:
        detail.override_targets = list(ov.get("targets") or [])
        detail.override_set_at = ov.get("set_at")

    db = probe.load_db()
    probe_target = videos[0] if videos else (subject if subject.is_file() else None)
    if probe_target is not None:
        entry = probe.db_entry(db, probe_target)
        detail.probe_langs = probe.entry_langs(entry)
        detail.probe_stale = not probe.entry_is_fresh(entry, probe_target)
        detail.probe_source = str(probe_target)
        if entry:
            in_cz = library.mirror_path(cat, "cz", probe_target).exists()
            detail.auto_langs = sorter.auto_targets(cat, detail.probe_langs, in_cz)

    return detail


def reanalyze(abs_path: str) -> Detail:
    cat, path = library.locate(abs_path)
    subject = library.subject_of(cat, path)
    videos = library.video_files_under(subject)
    if not videos:
        raise library.PathError("Uvnitř položky není žádný video soubor")

    for video in videos[:SHOW_PROBE_SAMPLES] if subject.is_dir() else videos[:1]:
        langs = probe.analyze(video)
        log.info("Přeanalyzováno %s -> %s", video, langs or ["(žádná audio stopa)"])
    return get_detail(str(subject))


def set_targets(abs_path: str, targets: list[str]) -> dict:
    cat, path = library.locate(abs_path)
    subject = library.subject_of(cat, path)
    clean = overrides.normalize_targets(targets)

    with sorter.lock:
        result = library.apply_targets(cat, subject, clean)
        overrides.set_override(subject, clean)
    log.info(
        "Override uložen: %s -> %s (+%d linků, -%d linků)",
        subject, clean or ["(nikam)"], len(result["created"]), len(result["removed"]),
    )
    trigger_refresh()
    result["targets"] = clean
    result["name"] = subject.name
    return result


def confirm_auto(abs_path: str) -> dict:
    """„Automatika to trefila“ — uzamkne aktuální stav jako ruční rozhodnutí.

    Hardlinky se nemění, jen se stávající umístění zapíše do override store,
    takže ho příští běh sorteru nemůže přehodnotit.
    """
    cat, path = library.locate(abs_path)
    subject = library.subject_of(cat, path)
    with sorter.lock:
        current = library.linked_langs(cat, subject)
        overrides.set_override(subject, current)
    log.info("Potvrzeno automatické zařazení: %s -> %s", subject, current or ["(nikam)"])
    return {"path": str(subject), "targets": current, "name": subject.name}


def clear_target_override(abs_path: str) -> Detail:
    """Zruší ruční rozhodnutí — položka se vrátí pod kontrolu sorteru."""
    cat, path = library.locate(abs_path)
    subject = library.subject_of(cat, path)
    with sorter.lock:
        cleared = overrides.clear_override(subject)
    if cleared:
        log.info("Override zrušen: %s", subject)
    return get_detail(str(subject))


def delete_plan(abs_path: str) -> dict:
    cat, path = library.locate(abs_path)
    subject = library.subject_of(cat, path)
    plan = library.plan_delete(cat, subject)
    plan.update(name=subject.name, abs_path=str(subject), category=cat.key)
    return plan


def delete_confirm(abs_path: str, token: str | None = None) -> dict:
    cat, path = library.locate(abs_path)
    subject = library.subject_of(cat, path)
    name = subject.name

    with sorter.lock:
        result = library.execute_delete(cat, subject, token)
        probe.forget([Path(p) for p in result["source_paths"]])
        overrides.clear_overrides_under(subject)
    log.info("Smazáno %s (%d souborů)", name, result["count"])
    trigger_refresh()

    result.update(name=name, category=cat.key)
    return result


def review_items() -> list[library.Item]:
    """Fronta na kontrolu + doplněné jazyky z ffprobe cache.

    Cache se načte jednou pro celý seznam; bez ní by u každé položky nebylo
    podle čeho poznat, jestli se automatika trefila.
    """
    items = library.review_queue(overrides.load_all())
    db = probe.load_db()
    for item in items:
        if not item.probe_path:
            continue
        entry = probe.db_entry(db, item.probe_path)
        if not entry:
            continue
        item.probe_langs = probe.entry_langs(entry)
        cat = library.get_category(item.category)
        in_cz = library.mirror_path(cat, "cz", Path(item.probe_path)).exists()
        item.auto_langs = sorter.auto_targets(cat, item.probe_langs, in_cz)
    return items


def human_size(num_bytes: int) -> str:
    if num_bytes <= 0:
        return "—"
    value = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.0f} {unit}" if unit in ("B", "KB") else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TB"


def library_health() -> dict:
    """Lehká verze pro hlavičku — bez síťových volání, běží na každý request."""
    settings = get_settings()
    report = diagnostics.cached_summary()
    return {
        "media_root": str(settings.media_root),
        "media_root_ok": settings.media_root.is_dir(),
        "jellyfin_configured": bool(settings.jellyfin_api_key),
        "problems": len(report.problems),
        "failures": len(report.failures),
    }
