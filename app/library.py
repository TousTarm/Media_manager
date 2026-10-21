"""Jádro: procházení knihovny, stav hardlinků, jejich vytváření a mazání.

Zdroj pravdy je vždy filesystem. `sorter_db.json` je jen cache výsledků
ffprobe — o tom, kde položka *skutečně* je, rozhoduje shoda inode mezi
souborem ve zdroji a souborem v `sorted/**`.
"""

from __future__ import annotations

import hashlib
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

from .config import CATEGORIES, LANGS, VIDEO_EXTS, Category, get_settings

log = logging.getLogger(__name__)


class PathError(ValueError):
    """Cesta mimo povolený strom nebo neexistující položka."""


# --------------------------------------------------------------------------
# Rozlišení a validace cest
# --------------------------------------------------------------------------

def get_category(key: str) -> Category:
    cat = CATEGORIES.get((key or "").lower())
    if cat is None:
        raise PathError(f"Neznámá kategorie: {key!r}")
    return cat


def source_root(cat: Category) -> Path:
    return get_settings().media_root / cat.dirname


def sorted_root(cat: Category, lang: str) -> Path:
    return get_settings().sorted_root / lang / cat.dirname


def _contained(child: Path, parent: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except (ValueError, OSError):
        return False


def _safe_join(root: Path, rel: str | None) -> Path:
    rel = (rel or "").strip().strip("/")
    if not rel:
        return root
    candidate = root / rel
    if ".." in Path(rel).parts or not _contained(candidate, root):
        raise PathError("Cesta míří mimo povolený strom")
    return candidate


def resolve_in_source(cat: Category, rel: str | None) -> Path:
    return _safe_join(source_root(cat), rel)


def nearest_listing_rel(cat: Category, rel: str | None) -> str:
    """Nejbližší existující složka na cestě od `rel` nahoru ke kořeni kategorie.

    Po smazání poslední položky složka zmizí (`prune_empty_dirs`), ale prohlížeč
    na ni dál ukazuje. Výpis se tehdy má vrátit o úroveň výš, ne skončit chybou.
    Cesta mimo strom zůstává chybou — na tu se potichu nepadá.
    """
    root = source_root(cat)
    current = resolve_in_source(cat, rel)
    while current != root and not current.is_dir():
        current = current.parent
    return "" if current == root else str(current.relative_to(root))


def locate(abs_path: str | Path) -> tuple[Category, Path]:
    """Najde kategorii, do jejíhož zdrojového kořene daná absolutní cesta patří."""
    path = Path(abs_path)
    for cat in CATEGORIES.values():
        if _contained(path, source_root(cat)):
            return cat, path
    raise PathError(f"Cesta není uvnitř Movies/Series/Anime: {abs_path}")


def show_root_of(cat: Category, path: Path) -> Path:
    """Kořenová složka show = první úroveň bezprostředně pod kořenem kategorie."""
    root = source_root(cat)
    rel = path.resolve().relative_to(root.resolve())
    if not rel.parts:
        raise PathError("Kořen kategorie není show")
    return root / rel.parts[0]


def subject_of(cat: Category, path: Path) -> Path:
    """Na co se vztahuje klasifikace pro danou cestu.

    U seriálů a anime je to vždy kořenová složka show. U filmů samotný soubor —
    a když uživatel klikne na složku filmu, tak jediný video soubor uvnitř,
    protože sorter klíčuje filmy přesnou cestou k souboru.
    """
    if cat.kind == "show":
        return show_root_of(cat, path)
    if path.is_dir():
        videos = video_files_under(path)
        if len(videos) == 1:
            return videos[0]
        raise PathError(
            "Složka neobsahuje právě jeden film — otevři konkrétní soubor"
            if videos else "Ve složce není žádný video soubor"
        )
    return path


def override_key_for(cat: Category, path: Path) -> Path:
    """Klíč do `manual_overrides.json` — soubor u filmů, kořen show u seriálů."""
    return subject_of(cat, path)


def is_video(path: Path) -> bool:
    return path.suffix.lower() in VIDEO_EXTS


def video_files_under(path: Path) -> list[Path]:
    if path.is_file():
        return [path] if is_video(path) else []
    found: list[Path] = []
    for dirpath, _dirnames, filenames in os.walk(path):
        for name in sorted(filenames):
            candidate = Path(dirpath) / name
            if is_video(candidate):
                found.append(candidate)
    return sorted(found)


def all_files_under(path: Path) -> list[Path]:
    if path.is_file():
        return [path]
    found: list[Path] = []
    for dirpath, _dirnames, filenames in os.walk(path):
        for name in sorted(filenames):
            found.append(Path(dirpath) / name)
    return sorted(found)


# --------------------------------------------------------------------------
# Stav: kam položka reálně ukazuje
# --------------------------------------------------------------------------

def mirror_path(cat: Category, lang: str, source_path: Path) -> Path:
    """Odpovídající cesta v `sorted/{lang}/{Kategorie}/…` (stejná relativní cesta)."""
    rel = source_path.resolve().relative_to(source_root(cat).resolve())
    return sorted_root(cat, lang) / rel


def _same_inode(a: Path, b: Path) -> bool:
    try:
        sa, sb = a.stat(), b.stat()
    except OSError:
        return False
    return (sa.st_dev, sa.st_ino) == (sb.st_dev, sb.st_ino)


def linked_langs(cat: Category, source_path: Path) -> list[str]:
    """Do kterých `sorted/*` větví položka skutečně ukazuje.

    U souboru se porovná inode. U složky (show) stačí, že zrcadlová složka
    existuje a obsahuje aspoň jeden soubor — kontrolovat inode každé epizody
    zvlášť by z výpisu udělalo tisíce `stat()` volání.
    """
    out: list[str] = []
    for lang in LANGS:
        try:
            mirror = mirror_path(cat, lang, source_path)
        except (ValueError, OSError):
            continue
        if source_path.is_dir():
            if mirror.is_dir() and any(mirror.iterdir()):
                out.append(lang)
        elif _same_inode(source_path, mirror):
            out.append(lang)
    return out


# --------------------------------------------------------------------------
# Výpis
# --------------------------------------------------------------------------

@dataclass
class Item:
    """Jedna dlaždice v mřížce nebo řádek v seznamu."""
    name: str                # co se zobrazí
    rel: str                 # relativní cesta vůči kořeni kategorie
    abs_path: str            # cesta, na kterou se kliká (subject u dlaždic)
    is_dir: bool
    action: str              # "detail" | "enter" | "none"
    langs: list[str] = field(default_factory=list)
    has_override: bool = False
    override_targets: list[str] = field(default_factory=list)
    size: int = 0
    mtime: float = 0.0
    category: str = ""
    episodes: int = 0
    probe_path: str = ""              # soubor, kterým se měří jazyk (vzorek u show)
    probe_langs: list[str] = field(default_factory=list)
    auto_langs: list[str] = field(default_factory=list)

    @property
    def unsorted(self) -> bool:
        return not self.langs


@dataclass
class Listing:
    category: Category
    rel: str
    abs_path: str
    items: list[Item]
    breadcrumbs: list[tuple[str, str]]
    parent_rel: str | None
    inside_show: bool


def _size_of(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _mtime_of(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _entry_stat(entry: os.DirEntry) -> tuple[int, float]:
    try:
        st = entry.stat()
        return st.st_size, st.st_mtime
    except OSError:
        return 0, 0.0


def _tile_for(cat: Category, path: Path, overrides: dict[str, dict]) -> Item | None:
    """Postaví dlaždici pro položku na nejvyšší úrovni kategorie.

    U filmů se složka s jediným videem tváří jako ten film (klasifikace i tak
    míří na soubor); složka s víc videi zůstane složkou k prolistování.
    """
    display = path.name
    if cat.kind == "show":
        subject = path
        videos = video_files_under(path)
        episodes = len(videos)
        action = "detail"
    else:
        videos = video_files_under(path) if path.is_dir() else [path]
        episodes = len(videos)
        if len(videos) == 1:
            subject, action = videos[0], "detail"
        elif videos:
            subject, action = path, "enter"
        else:
            return None

    item = Item(
        name=display,
        rel=str(path.relative_to(source_root(cat))),
        abs_path=str(subject),
        is_dir=path.is_dir(),
        action=action,
        size=_size_of(subject) if subject.is_file() else sum(
            _size_of(v) for v in video_files_under(path)
        ),
        mtime=_mtime_of(path),
        category=cat.key,
        episodes=episodes,
        probe_path=str(videos[0]) if videos else "",
    )
    if action == "detail":
        item.langs = linked_langs(cat, subject)
        ov = overrides.get(str(subject))
        if ov is not None:
            item.has_override = True
            item.override_targets = list(ov.get("targets") or [])
    return item


def browse(cat: Category, rel: str | None, overrides: dict[str, dict]) -> Listing:
    base = resolve_in_source(cat, rel)
    if not base.exists():
        raise PathError(f"Neexistující cesta: {base}")
    if base.is_file():
        raise PathError("Cesta je soubor, ne složka")

    root = source_root(cat)
    rel_clean = (rel or "").strip().strip("/")
    depth = len(Path(rel_clean).parts) if rel_clean else 0
    inside_show = cat.kind == "show" and depth > 0

    try:
        entries = sorted(os.scandir(base), key=lambda e: e.name.casefold())
    except OSError as exc:
        raise PathError(f"Nelze číst složku: {exc}") from exc

    items: list[Item] = []
    for entry in entries:
        if entry.name.startswith("."):
            continue
        path = Path(entry.path)
        is_dir = entry.is_dir(follow_symlinks=False)
        if not is_dir and not is_video(path):
            continue

        if depth == 0:
            tile = _tile_for(cat, path, overrides)
            if tile is not None:
                items.append(tile)
            continue

        # Hlouběji ve stromu už jde jen o náhled struktury.
        size, mtime = _entry_stat(entry)
        items.append(Item(
            name=entry.name,
            rel=str(path.relative_to(root)),
            abs_path=str(path),
            is_dir=is_dir,
            action="enter" if is_dir else ("none" if inside_show else "detail"),
            size=0 if is_dir else size,
            mtime=mtime,
            category=cat.key,
        ))

    if depth > 0 and not inside_show:
        for item in items:
            if item.action == "detail":
                item.langs = linked_langs(cat, Path(item.abs_path))
                ov = overrides.get(item.abs_path)
                if ov is not None:
                    item.has_override = True
                    item.override_targets = list(ov.get("targets") or [])

    crumbs: list[tuple[str, str]] = [(cat.label, "")]
    walked = Path()
    for part in Path(rel_clean).parts if rel_clean else ():
        walked = walked / part
        crumbs.append((part, str(walked)))

    parent = str(Path(rel_clean).parent) if rel_clean else None
    if parent in (".", ""):
        parent = "" if rel_clean else None

    return Listing(
        category=cat, rel=rel_clean, abs_path=str(base), items=items,
        breadcrumbs=crumbs, parent_rel=parent, inside_show=inside_show,
    )


def review_queue(overrides: dict[str, dict]) -> list[Item]:
    """Vše, co ještě nemá ruční rozhodnutí — tedy pod kontrolou sorteru.

    Řadí se od nejnovějšího, protože čerstvě přibylé položky jsou ty, u kterých
    se automatika mohla splést a ještě si toho nikdo nevšiml.
    """
    out: list[Item] = []
    for cat in CATEGORIES.values():
        root = source_root(cat)
        if not root.is_dir():
            continue
        try:
            entries = sorted(os.scandir(root), key=lambda e: e.name.casefold())
        except OSError as exc:
            log.warning("Nelze číst %s: %s", root, exc)
            continue
        for entry in entries:
            if entry.name.startswith("."):
                continue
            path = Path(entry.path)
            if not entry.is_dir(follow_symlinks=False) and not is_video(path):
                continue
            tile = _tile_for(cat, path, overrides)
            if tile is not None and tile.action == "detail" and not tile.has_override:
                out.append(tile)
    out.sort(key=lambda i: i.mtime, reverse=True)
    return out


# --------------------------------------------------------------------------
# Hardlinky: hledání
# --------------------------------------------------------------------------

def find_all_hardlinks(source_path: str, search_roots: list[str]) -> list[str]:
    """Najde všechny cesty se stejným inode jako `source_path` (dle spec 5.1)."""
    st = os.stat(source_path)
    target_dev, target_ino = st.st_dev, st.st_ino
    matches: list[str] = []
    for root_dir in search_roots:
        for dirpath, _, filenames in os.walk(root_dir):
            for fname in filenames:
                full = os.path.join(dirpath, fname)
                try:
                    s = os.stat(full)
                except OSError:
                    continue
                if s.st_dev == target_dev and s.st_ino == target_ino:
                    matches.append(full)
    return matches


def find_hardlinks_bulk(sources: list[Path]) -> dict[Path, list[Path]]:
    """Najde hardlinky pro víc souborů naráz.

    Nejdřív se zkusí předpokládaná zrcadlová umístění v `sorted/**`. Pokud u
    každého souboru sedí počet nalezených jmen s `st_nlink`, jsou všechna jména
    známá a celý walk knihovnou se přeskočí — což je běžný případ. Jinak se
    dohledá zbytek jedním průchodem přes `search_roots` (ne jedním na soubor).
    """
    settings = get_settings()
    result: dict[Path, list[Path]] = {}
    wanted: dict[tuple[int, int], list[Path]] = {}
    incomplete = False

    for src in sources:
        try:
            st = src.stat()
        except OSError:
            continue
        names = [src]
        try:
            cat, _ = locate(src)
            for lang in LANGS:
                mirror = mirror_path(cat, lang, src)
                if _same_inode(src, mirror):
                    names.append(mirror)
        except (PathError, ValueError, OSError):
            incomplete = True
        result[src] = names
        if len(names) < st.st_nlink:
            incomplete = True
        # Dva zdroje můžou sdílet inode; klíč proto drží seznam, ne poslední zápis.
        wanted.setdefault((st.st_dev, st.st_ino), []).append(src)

    if not incomplete or not wanted:
        return result

    log.info("Dohledávám hardlinky celým walkem (%d souborů)", len(wanted))
    found: dict[tuple[int, int], set[Path]] = {key: set() for key in wanted}
    for root_dir in settings.search_roots:
        for dirpath, _dirnames, filenames in os.walk(root_dir):
            for fname in filenames:
                full = os.path.join(dirpath, fname)
                try:
                    s = os.stat(full)
                except OSError:
                    continue
                key = (s.st_dev, s.st_ino)
                if key in found:
                    found[key].add(Path(full))

    for key, srcs in wanted.items():
        for src in srcs:
            result[src] = sorted(set(result.get(src, [])) | found[key])
    return result


# --------------------------------------------------------------------------
# Hardlinky: vytváření, rušení, úklid
# --------------------------------------------------------------------------

def _stop_dirs() -> set[Path]:
    settings = get_settings()
    stops = {settings.media_root.resolve(), settings.sorted_root.resolve()}
    for cat in CATEGORIES.values():
        stops.add(source_root(cat).resolve())
        for lang in LANGS:
            stops.add((settings.sorted_root / lang).resolve())
            stops.add(sorted_root(cat, lang).resolve())
    return stops


def prune_empty_dirs(start: Path) -> list[Path]:
    """Odmaže prázdné rodičovské složky směrem nahoru, kořeny nechá být."""
    stops = _stop_dirs()
    removed: list[Path] = []
    current = start
    while True:
        try:
            resolved = current.resolve()
        except OSError:
            break
        if resolved in stops or resolved == resolved.parent:
            break
        if not _contained(current, get_settings().media_root):
            break
        try:
            os.rmdir(current)
        except OSError:
            break
        removed.append(current)
        current = current.parent
    return removed


def _link_one(source: Path, target: Path) -> bool:
    if _same_inode(source, target):
        return False
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() or target.is_symlink():
        os.remove(target)          # zastaralý link na starý inode
    os.link(source, target)
    return True


def _unlink_mirror(cat: Category, lang: str, source_path: Path) -> list[Path]:
    """Zruší zrcadlo položky v jedné jazykové větvi.

    `sorted/**` je celý generovaný, takže u složky se odstraní všechny soubory
    uvnitř — ne jen ty, kterým se povedlo dohledat zdrojový inode (osiřelé
    linky po smazaných zdrojích by jinak zůstaly viset).
    """
    try:
        mirror = mirror_path(cat, lang, source_path)
    except (ValueError, OSError):
        return []
    branch = sorted_root(cat, lang)
    if not _contained(mirror, branch) and mirror != branch:
        return []

    removed: list[Path] = []
    if mirror.is_dir():
        for path in reversed(all_files_under(mirror)):
            try:
                os.remove(path)
                removed.append(path)
            except OSError as exc:
                log.warning("Nelze smazat %s: %s", path, exc)
        for dirpath, dirnames, _ in os.walk(mirror, topdown=False):
            for name in dirnames:
                try:
                    os.rmdir(os.path.join(dirpath, name))
                except OSError:
                    pass
        try:
            os.rmdir(mirror)
        except OSError:
            pass
        prune_empty_dirs(mirror.parent)
    elif mirror.exists():
        try:
            os.remove(mirror)
            removed.append(mirror)
            prune_empty_dirs(mirror.parent)
        except OSError as exc:
            log.warning("Nelze smazat %s: %s", mirror, exc)
    return removed


def apply_targets(cat: Category, source_path: Path, targets: list[str]) -> dict:
    """Srovná skutečné hardlinky se seznamem cílů (spec 5.3).

    Originál ve zdroji se nikdy nedotýká — mění se jen `sorted/**`.
    """
    if not source_path.exists():
        raise PathError(f"Zdroj neexistuje: {source_path}")

    created: list[str] = []
    removed: list[str] = []
    current = linked_langs(cat, source_path)

    for lang in LANGS:
        want = lang in targets
        have = lang in current
        if want:
            sources = video_files_under(source_path)
            for src in sources:
                target = mirror_path(cat, lang, src)
                try:
                    if _link_one(src, target):
                        created.append(str(target))
                except OSError as exc:
                    log.error("Nelze vytvořit hardlink %s -> %s: %s", src, target, exc)
                    raise PathError(f"Nelze vytvořit hardlink: {exc}") from exc
        elif have:
            removed.extend(str(p) for p in _unlink_mirror(cat, lang, source_path))

    return {"created": created, "removed": removed, "langs": linked_langs(cat, source_path)}


# --------------------------------------------------------------------------
# Mazání
# --------------------------------------------------------------------------

def plan_delete(cat: Category, source_path: Path) -> dict:
    """Dry-run: co všechno by smazání odstranilo (spec 5.1 / 5.2 krok 1–2)."""
    if not source_path.exists():
        raise PathError(f"Zdroj neexistuje: {source_path}")

    source_files = all_files_under(source_path)
    if source_path.is_file():
        # Film bývá sám ve vlastní složce spolu s titulky a .nfo. Když uvnitř
        # není žádné jiné video, patří k němu celý ten obal — jinak po smazání
        # zůstane složka s osiřelými titulky, kterou pak nejde uklidit.
        folder = source_path.parent
        if folder != source_root(cat) and _contained(folder, source_root(cat)):
            siblings = video_files_under(folder)
            if siblings == [source_path]:
                source_files = all_files_under(folder)
    videos = [p for p in source_files if is_video(p)]
    links = find_hardlinks_bulk(videos)

    paths: set[Path] = set(source_files)
    for names in links.values():
        paths.update(names)

    if source_path.is_dir():
        for lang in LANGS:
            try:
                mirror = mirror_path(cat, lang, source_path)
            except (ValueError, OSError):
                continue
            if mirror.is_dir() and _contained(mirror, get_settings().sorted_root):
                paths.update(all_files_under(mirror))

    settings = get_settings()
    paths = {p for p in paths if _contained(p, settings.media_root)}
    ordered = sorted(paths)
    in_source = [p for p in ordered if _contained(p, source_root(cat))]
    in_sorted = [p for p in ordered if _contained(p, settings.sorted_root)]

    paths_str = [str(p) for p in ordered]
    return {
        "paths": paths_str,
        "source_paths": [str(p) for p in in_source],
        "link_paths": [str(p) for p in in_sorted],
        "total_bytes": sum(_size_of(p) for p in in_source),
        "count": len(ordered),
        "token": plan_token(paths_str),
    }


def plan_token(paths: list[str]) -> str:
    """Otisk seznamu cest, kterým se confirm váže na to, co uživatel viděl."""
    digest = hashlib.sha256("\n".join(sorted(paths)).encode("utf-8"))
    return digest.hexdigest()[:16]


def execute_delete(cat: Category, source_path: Path, token: str | None = None) -> dict:
    """Skutečné smazání (spec 5.2). Vrací seznam smazaných a chybných cest.

    `token` je otisk seznamu z dry-runu. Plán se přepočítá znovu (cestám z
    prohlížeče se věřit nedá) a když mezitím na disku něco přibylo nebo ubylo,
    smazání se odmítne — dělení na dva requesty má smysl jen tehdy, když se
    maže přesně to, co uživatel odsouhlasil.
    """
    plan = plan_delete(cat, source_path)
    if token and token != plan["token"]:
        raise PathError(
            "Knihovna se od zobrazení seznamu změnila. Otevři mazání znovu "
            "a zkontroluj, co se smaže."
        )
    result = _remove_paths(plan["paths"])

    roots = [source_path] + [mirror_safe(cat, lang, source_path) for lang in LANGS]
    _prune_trees([r for r in roots if r is not None], result["parents"])
    result["source_paths"] = plan["source_paths"]
    return result


def _remove_paths(paths: list[str]) -> dict:
    deleted: list[str] = []
    failed: list[dict] = []
    parents: set[Path] = set()
    for raw in paths:
        p = Path(raw)
        parents.add(p.parent)
        try:
            os.remove(p)
            deleted.append(raw)
        except FileNotFoundError:
            deleted.append(raw)
        except OSError as exc:
            log.error("Nelze smazat %s: %s", p, exc)
            failed.append({"path": raw, "error": str(exc)})
    return {"deleted": deleted, "failed": failed, "count": len(deleted), "parents": parents}


def _prune_trees(roots: list[Path], parents: set[Path]) -> None:
    for root in roots:
        if root.is_dir():
            for dirpath, dirnames, _ in os.walk(root, topdown=False):
                for name in dirnames:
                    try:
                        os.rmdir(os.path.join(dirpath, name))
                    except OSError:
                        pass
            try:
                os.rmdir(root)
            except OSError:
                pass
            parents.add(root.parent)
    for parent in sorted(parents, key=lambda p: len(p.parts), reverse=True):
        prune_empty_dirs(parent)


def mirror_safe(cat: Category, lang: str, source_path: Path) -> Path | None:
    try:
        mirror = mirror_path(cat, lang, source_path)
    except (ValueError, OSError):
        return None
    return mirror if _contained(mirror, get_settings().sorted_root) else None
