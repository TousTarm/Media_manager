"""Kontrola prostředí — co appka potřebuje a jestli to opravdu má.

Smyslem není hezký report, ale chytit tiché chyby dřív, než si jich uživatel
všimne podle toho, že „to nic neudělalo“: chybějící právo zápisu do knihovny,
`sorted/` na jiném filesystému než `Movies/` (pak hardlinky skončí na EXDEV),
starý sorter.py v cronu, chybějící ffprobe, mrtvý Jellyfin.

Kontroly jsou **jen pro čtení a dočasné soubory**, které po sobě uklidí.
Nikdy nesahají na skutečná média.
"""

from __future__ import annotations

import logging
import os
import pwd
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from .config import CATEGORIES, ENV_FILE, LANGS, get_settings
from .jsonstore import read_json

log = logging.getLogger(__name__)

OK, WARN, FAIL = "ok", "warn", "fail"


@dataclass
class Check:
    name: str
    status: str                 # ok | warn | fail
    detail: str = ""
    hint: str = ""              # co s tím dělat, když to není ok

    @property
    def bad(self) -> bool:
        return self.status in (WARN, FAIL)


@dataclass
class Report:
    checks: list[Check] = field(default_factory=list)

    def add(self, name: str, status: str, detail: str = "", hint: str = "") -> Check:
        check = Check(name, status, detail, hint)
        self.checks.append(check)
        return check

    @property
    def failures(self) -> list[Check]:
        return [c for c in self.checks if c.status == FAIL]

    @property
    def warnings(self) -> list[Check]:
        return [c for c in self.checks if c.status == WARN]

    @property
    def problems(self) -> list[Check]:
        return [c for c in self.checks if c.bad]

    @property
    def status(self) -> str:
        if self.failures:
            return FAIL
        return WARN if self.warnings else OK


# --------------------------------------------------------------------------
# Dílčí kontroly
# --------------------------------------------------------------------------

def _writable(path: Path) -> bool:
    """Ověří zápis skutečným zápisem, ne `os.access`.

    `os.access()` u roota vrací True i tam, kde je filesystem read-only nebo
    kde brání ACL — což je přesně případ bind mountu ze ZFS.
    """
    probe = path / ".media-manager-write-test"
    try:
        with open(probe, "w") as fh:
            fh.write("x")
        probe.unlink()
        return True
    except OSError:
        try:
            probe.unlink()
        except OSError:
            pass
        return False


def _check_env(report: Report, writes: bool = True) -> None:
    settings = get_settings()
    if ENV_FILE.is_file():
        report.add(".env", OK, f"načteno z {ENV_FILE}")
    else:
        report.add(
            ".env", WARN, f"{ENV_FILE} neexistuje",
            "Zkopíruj .env.example na .env a vyplň. Bez něj platí výchozí "
            "hodnoty a APP_PASSWORD zůstane prázdné.",
        )

    if not settings.app_password:
        report.add(
            "APP_PASSWORD", FAIL, "prázdné",
            "Appka odmítne každý request, dokud heslo nenastavíš v .env.",
        )
    elif len(settings.app_password) < 8:
        report.add(
            "APP_PASSWORD", WARN, f"jen {len(settings.app_password)} znaků",
            "Nástroj je sice interní, ale osm znaků a víc stojí za to.",
        )
    else:
        report.add("APP_PASSWORD", OK, f"nastaveno ({len(settings.app_password)} znaků)")

    who = f"uid={os.getuid()} gid={os.getgid()}"
    try:
        who = f"{pwd.getpwuid(os.getuid()).pw_name} ({who})"
    except KeyError:
        pass
    report.add("Běžící uživatel", OK, who)


def _check_library(report: Report, writes: bool = True) -> None:
    settings = get_settings()
    root = settings.media_root

    if not root.exists():
        report.add(
            "MEDIA_ROOT", FAIL, f"{root} neexistuje",
            "Zkontroluj cestu v .env a že je bind mount připojený.",
        )
        return
    if not root.is_dir():
        report.add("MEDIA_ROOT", FAIL, f"{root} není složka")
        return
    report.add("MEDIA_ROOT", OK, str(root))

    for cat in CATEGORIES.values():
        path = root / cat.dirname
        if not path.is_dir():
            report.add(
                f"{cat.dirname}/", FAIL, "chybí",
                f"Kategorie {cat.label} se nezobrazí. Vytvoř {path} nebo uprav MEDIA_ROOT.",
            )
        elif not os.access(path, os.R_OK | os.X_OK):
            report.add(f"{cat.dirname}/", FAIL, "nelze číst", f"Chybí právo čtení na {path}.")
        else:
            count = sum(1 for _ in path.iterdir())
            report.add(f"{cat.dirname}/", OK, f"{count} položek")

    sorted_root = settings.sorted_root
    if not sorted_root.is_dir():
        report.add(
            "sorted/", FAIL, "chybí",
            f"Bez {sorted_root} nejde nic zařadit. Vytvoří ji sorter, "
            "nebo ji založ ručně.",
        )
        return
    if writes:
        if not _writable(sorted_root):
            report.add(
                "sorted/ zápis", FAIL, "nelze zapisovat",
                f"Uživatel, pod kterým appka běží, nemá právo zápisu do {sorted_root}. "
                "Ukládání jazyků i mazání selže.",
            )
        else:
            report.add("sorted/ zápis", OK, "lze zapisovat")

    missing = [
        f"sorted/{lang}/{cat.dirname}"
        for lang in LANGS
        for cat in CATEGORIES.values()
        if not (sorted_root / lang / cat.dirname).is_dir()
    ]
    if missing:
        report.add(
            "Jazykové větve", WARN, f"chybí {len(missing)}: {', '.join(missing[:6])}"
            + (" …" if len(missing) > 6 else ""),
            "Nevadí — vytvoří se samy při prvním zařazení.",
        )
    else:
        report.add("Jazykové větve", OK, f"{len(LANGS) * len(CATEGORIES)} složek")


def _check_hardlinks(report: Report, writes: bool = True) -> None:
    """Hardlink přes hranici filesystému nejde — a je to tichá, matoucí chyba."""
    settings = get_settings()
    movies = settings.media_root / "Movies"
    sorted_root = settings.sorted_root
    if not (movies.is_dir() and sorted_root.is_dir()):
        return

    try:
        if movies.stat().st_dev != sorted_root.stat().st_dev:
            report.add(
                "Hardlinky", FAIL, "Movies/ a sorted/ jsou na různých filesystémech",
                "os.link() skončí na EXDEV. Musí to být jeden mount — "
                "v LXC jeden bind mount celého /media/jellyfin.",
            )
            return
    except OSError as exc:
        report.add("Hardlinky", WARN, f"nelze ověřit: {exc}")
        return

    if not writes:
        report.add("Hardlinky", OK, "stejný filesystem")
        return

    # Skutečný test: vytvoř soubor, zalinkuj ho přes hranici a ukliď.
    src = settings.media_root / ".media-manager-link-src"
    dst = sorted_root / ".media-manager-link-dst"
    try:
        src.write_text("x")
        os.link(src, dst)
        report.add("Hardlinky", OK, "ověřeno skutečným os.link()")
    except OSError as exc:
        report.add(
            "Hardlinky", FAIL, f"os.link() selhal: {exc}",
            "Bez hardlinků neudělá appka vůbec nic užitečného.",
        )
    finally:
        for path in (dst, src):
            try:
                path.unlink()
            except OSError:
                pass


def _check_state_files(report: Report, writes: bool = True) -> None:
    settings = get_settings()

    db = settings.sorter_db
    if not db.is_file():
        report.add(
            "sorter_db.json", WARN, "neexistuje",
            "Jazyky z ffprobe se nebudou předvyplňovat, dokud sorter "
            "poprvé neproběhne. Tlačítko Přeanalyzovat funguje i tak.",
        )
    else:
        data = read_json(db, {})
        if not isinstance(data, dict):
            report.add("sorter_db.json", WARN, "nečitelný obsah",
                       "Soubor existuje, ale není to JSON objekt.")
        else:
            report.add("sorter_db.json", OK, f"{len(data)} záznamů")

    ov = settings.overrides_file
    if ov.exists():
        data = read_json(ov, {})
        report.add("manual_overrides.json", OK, f"{len(data)} ručních rozhodnutí")
    else:
        report.add("manual_overrides.json", OK, "zatím neexistuje, vytvoří se při prvním uložení")

    if writes:
        if not _writable(settings.media_root):
            report.add(
                "Zápis do MEDIA_ROOT", FAIL, "nelze zapisovat",
                f"manual_overrides.json ani sorter_db.json nepůjde uložit "
                f"({settings.media_root}).",
            )
        else:
            report.add("Zápis do MEDIA_ROOT", OK, "lze zapisovat")


CRONTAB = Path("/var/spool/cron/crontabs/root")


def _check_sorter(report: Report) -> None:
    """Sorter běží v appce; starý cron skript by si s ním přetahoval linky."""
    try:
        in_cron = "sorter.py" in CRONTAB.read_text(encoding="utf-8", errors="replace")
    except OSError:
        in_cron = False
    interval = get_settings().sorter_interval
    if in_cron:
        report.add(
            "Sorter", FAIL, "starý sorter.py je pořád v cronu",
            "Dva sortery by si přetahovaly linky. Smaž ten řádek přes crontab -e.",
        )
    elif interval <= 0:
        report.add("Sorter", WARN, "automatický běh vypnutý (SORTER_INTERVAL=0)")
    else:
        report.add("Sorter", OK, f"běží v appce každých {interval} s")


def _check_ffprobe(report: Report) -> None:
    settings = get_settings()
    binary = settings.ffprobe_binary()
    if not binary:
        report.add(
            "ffprobe", FAIL, f"{settings.ffprobe_path!r} nenalezen v PATH",
            "Nainstaluj: apt install ffmpeg. Bez něj nefunguje Přeanalyzovat.",
        )
        return
    try:
        proc = subprocess.run([binary, "-version"], capture_output=True, text=True, timeout=10)
        version = proc.stdout.splitlines()[0] if proc.stdout else "neznámá verze"
        report.add("ffprobe", OK, f"{binary} — {version[:60]}")
    except (OSError, subprocess.SubprocessError) as exc:
        report.add("ffprobe", WARN, f"{binary} nelze spustit: {exc}")


def _check_jellyfin(report: Report) -> None:
    settings = get_settings()
    if not settings.jellyfin_api_key:
        report.add(
            "Jellyfin", WARN, "JELLYFIN_API_KEY není nastaven",
            "Appka funguje, jen po každé změně nepošle refresh — knihovna se "
            "v Jellyfinu aktualizuje až sama od sebe.",
        )
        return
    url = f"{settings.jellyfin_url}/System/Info"
    try:
        resp = httpx.get(url, headers={"X-Emby-Token": settings.jellyfin_api_key}, timeout=5.0)
    except Exception as exc:  # noqa: BLE001 — diagnostika nesmí shodit stránku
        report.add(
            "Jellyfin", WARN, f"{settings.jellyfin_url} neodpovídá: {type(exc).__name__}",
            "Zkontroluj JELLYFIN_URL a že Jellyfin běží. Mazání i ukládání "
            "funguje dál, jen bez refreshe.",
        )
        return
    if resp.status_code == 401:
        report.add(
            "Jellyfin", FAIL, "API klíč odmítnut (HTTP 401)",
            "Vygeneruj nový v Dashboard → API Keys a přepiš JELLYFIN_API_KEY.",
        )
    elif resp.status_code >= 400:
        report.add("Jellyfin", WARN, f"HTTP {resp.status_code} z {url}")
    else:
        try:
            info = resp.json()
            name = info.get("ServerName") or "Jellyfin"
            version = info.get("Version") or "?"
            report.add("Jellyfin", OK, f"{name} {version} na {settings.jellyfin_url}")
        except ValueError:
            report.add("Jellyfin", OK, f"odpovídá na {settings.jellyfin_url}")


def _check_disk(report: Report) -> None:
    settings = get_settings()
    if not settings.media_root.is_dir():
        return
    try:
        usage = shutil.disk_usage(settings.media_root)
    except OSError:
        return
    free_gb = usage.free / 1024 ** 3
    pct = usage.free / usage.total * 100 if usage.total else 0
    detail = f"{free_gb:.1f} GB volných ({pct:.0f} %)"
    if pct < 3:
        report.add("Místo na disku", WARN, detail, "Hardlinky místo neberou, ale stahování ano.")
    else:
        report.add("Místo na disku", OK, detail)


# --------------------------------------------------------------------------
# Veřejné API
# --------------------------------------------------------------------------

def run_checks(include_network: bool = True, include_writes: bool = True) -> Report:
    """Projde prostředí.

    `include_writes` zapíná testy, které opravdu něco zapíšou (a hned uklidí) —
    práva na zápis a hardlink přes hranici `sorted/`. Hlavička stránky je volá
    s False, aby se na každý request nesahalo na disk; /debug a start s True.
    """
    report = Report()
    _check_env(report, include_writes)
    _check_library(report, include_writes)
    _check_hardlinks(report, include_writes)
    _check_state_files(report, include_writes)
    _check_sorter(report)
    _check_ffprobe(report)
    if include_network:
        _check_jellyfin(report)
    _check_disk(report)
    return report


_summary_lock = threading.Lock()
_summary: tuple[float, Report] | None = None
SUMMARY_TTL = 30.0


def cached_summary() -> Report:
    """Levná verze pro indikátor v hlavičce, přepočítaná nejvýš jednou za TTL."""
    global _summary
    with _summary_lock:
        now = time.monotonic()
        if _summary is None or now - _summary[0] > SUMMARY_TTL:
            _summary = (now, run_checks(include_network=False, include_writes=False))
        return _summary[1]


def invalidate_summary() -> None:
    global _summary
    with _summary_lock:
        _summary = None


def log_report(report: Report) -> None:
    """Vypíše výsledek do logu — chyby hlasitě, zbytek jednou řádkou."""
    for check in report.failures:
        log.error("KONTROLA SELHALA — %s: %s%s",
                  check.name, check.detail, f" → {check.hint}" if check.hint else "")
    for check in report.warnings:
        log.warning("Varování — %s: %s%s",
                    check.name, check.detail, f" → {check.hint}" if check.hint else "")
    log.info(
        "Kontrola prostředí: %d ok, %d varování, %d chyb (detail na /debug)",
        len(report.checks) - len(report.problems), len(report.warnings), len(report.failures),
    )
