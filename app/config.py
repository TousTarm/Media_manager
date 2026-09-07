"""Konfigurace aplikace + definice kategorií knihovny.

Všechno nastavitelné se čte z `.env` vedle aplikace (nebo z proměnných
prostředí, které mají přednost). V kódu nejsou žádné natvrdo zadrátované
cesty ani hesla — `.env` se na cílovém stroji vytváří ručně při nasazení
a `.env.example` je jeho vzor.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Kořen repozitáře (…/media-manager), ne balíček app/.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = PROJECT_ROOT / ".env"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=ENV_FILE,
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- knihovna
    media_root: Path = Path("/media/jellyfin")

    # --- Jellyfin
    jellyfin_url: str = "http://127.0.0.1:8096"
    jellyfin_api_key: str = ""
    jellyfin_timeout: float = 10.0

    # --- přihlášení
    app_user: str = "admin"
    app_password: str = ""

    # --- server
    app_host: str = "0.0.0.0"
    app_port: int = 8181
    log_level: str = "INFO"

    # --- nástroje
    ffprobe_path: str = "ffprobe"
    ffprobe_timeout: int = 90

    @field_validator("media_root", mode="before")
    @classmethod
    def _expand(cls, value):
        return Path(str(value)).expanduser()

    @field_validator("jellyfin_url")
    @classmethod
    def _strip_slash(cls, value: str) -> str:
        return value.rstrip("/")

    @field_validator("log_level")
    @classmethod
    def _upper(cls, value: str) -> str:
        level = value.strip().upper()
        allowed = {"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"}
        if level not in allowed:
            raise ValueError(f"LOG_LEVEL musí být jedno z {sorted(allowed)}, ne {value!r}")
        return level

    @field_validator("app_port")
    @classmethod
    def _port_range(cls, value: int) -> int:
        if not 1 <= value <= 65535:
            raise ValueError(f"APP_PORT mimo rozsah: {value}")
        return value

    # --- odvozené cesty
    @property
    def sorted_root(self) -> Path:
        return self.media_root / "sorted"

    @property
    def sorter_db(self) -> Path:
        return self.media_root / "sorter_db.json"

    @property
    def overrides_file(self) -> Path:
        return self.media_root / "manual_overrides.json"

    @property
    def sorter_script(self) -> Path:
        return self.media_root / "sorter.py"

    @property
    def search_roots(self) -> list[Path]:
        return [self.media_root / c.dirname for c in CATEGORIES.values()] + [
            self.sorted_root
        ]

    @property
    def env_file_found(self) -> bool:
        return ENV_FILE.is_file()

    def ffprobe_binary(self) -> str | None:
        """Rozvine `ffprobe` na absolutní cestu, nebo None když chybí."""
        return shutil.which(self.ffprobe_path)


@dataclass(frozen=True)
class Category:
    key: str          # url identifikátor
    label: str        # co vidí uživatel
    dirname: str      # název složky pod media_root
    kind: str         # "movie" = granularita souboru, "show" = granularita složky


CATEGORIES: dict[str, Category] = {
    "movies": Category("movies", "Movies", "Movies", "movie"),
    "series": Category("series", "Series", "Series", "show"),
    "anime": Category("anime", "Anime", "Anime", "show"),
}


@dataclass(frozen=True)
class Tab:
    key: str
    label: str
    href: str


# Fronta na kontrolu je vlevo — je to jediný tab, který má „došlou poštu“.
TABS: list[Tab] = [
    Tab("auto", "Auto", "/auto"),
    Tab("movies", "Movies", "/b/movies"),
    Tab("series", "Series", "/b/series"),
    Tab("anime", "Anime", "/b/anime"),
]

# Jazykové větve v `sorted/`. Pořadí určuje pořadí přepínačů v UI.
LANGS = ("cz", "en", "jp")

LANG_LABELS = {"cz": "Čeština", "en": "Angličtina", "jp": "Japonština"}

VIDEO_EXTS = {
    ".mkv", ".mp4", ".avi", ".m4v", ".mov", ".ts", ".m2ts",
    ".wmv", ".flv", ".webm", ".mpg", ".mpeg", ".ogv", ".divx",
}

# Normalizace ISO kódů audio stop na naše tři cíle.
LANG_CODES: dict[str, set[str]] = {
    "cz": {"cz", "cs", "cze", "ces", "czech", "sk", "slk", "slo", "svk", "slovak"},
    "en": {"en", "eng", "english"},
    "jp": {"jp", "ja", "jpn", "japanese"},
}


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
