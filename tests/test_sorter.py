"""Samotest sorteru a přihlášení na dočasné knihovně: `venv/bin/python -m tests.test_sorter`."""

import os
import tempfile
import time
from pathlib import Path

tmp = Path(tempfile.mkdtemp())
os.environ.update(MEDIA_ROOT=str(tmp), APP_PASSWORD="heslo123", APP_USER="admin")

from app import auth, overrides, probe, sorter  # noqa: E402
from app.config import CATEGORIES  # noqa: E402

AUDIO = {}  # název souboru -> jazyky, místo skutečného ffprobe
probe.run_ffprobe = lambda p: AUDIO[p.name]


def video(rel, langs, age=3600):
    p = tmp / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"x")
    os.utime(p, (time.time() - age,) * 2)
    AUDIO[p.name] = langs
    return p


def linked(rel):
    return sorted(str(p.relative_to(tmp / "sorted")) for p in (tmp / "sorted").rglob("*")
                  if p.is_file() and p.name == Path(rel).name)


movies, anime = CATEGORIES["movies"], CATEGORIES["anime"]
assert sorter.auto_targets(movies, ["cze", "eng"]) == ["cz", "en"]
assert sorter.auto_targets(movies, ["und"]) == ["en"]
assert sorter.auto_targets(movies, ["eng"], in_cz=True) == ["cz"]
assert sorter.auto_targets(anime, ["jpn", "eng"]) == ["en", "jp"]
assert sorter.auto_targets(anime, ["jpn"]) == ["jp"]
assert sorter.auto_targets(anime, []) == ["en"]

video("Movies/A/a.mkv", ["cze", "eng"])
video("Movies/b.mkv", ["und"])
video("Movies/c.mkv", ["eng"], age=10)                 # ještě se stahuje
video("Series/Show/S1/e1.mkv", ["eng"])
video("Series/Show/S1/e2.mkv", ["eng"])
video("Anime/Ani/x.mkv", ["jpn"])
(tmp / "sorted/en/Movies").mkdir(parents=True)
(tmp / "sorted/en/Movies/gone.mkv").write_bytes(b"orphan")

stats = sorter.run_once()
assert linked("a.mkv") == ["cz/Movies/A/a.mkv", "en/Movies/A/a.mkv"], linked("a.mkv")
assert linked("b.mkv") == ["en/Movies/b.mkv"]
assert linked("c.mkv") == [], "stahovaný soubor se nemá zařadit"
assert linked("e2.mkv") == ["en/Series/Show/S1/e2.mkv"]
assert linked("x.mkv") == ["jp/Anime/Ani/x.mkv"]
assert not (tmp / "sorted/en/Movies/gone.mkv").exists(), "osiřelý link má zmizet"
assert stats["settling"] == 1 and stats["removed"] == 1, stats

overrides.set_override(tmp / "Series/Show", [])          # ruční „nikam“
sorter.run_once()
assert linked("e1.mkv") == []
assert sorter.run_once()["created"] == 0                   # druhý běh nic nemění

cookie = auth.make_session()
assert auth.session_valid(cookie)
assert not auth.session_valid(cookie[:-1] + ("0" if cookie[-1] != "0" else "1"))
assert not auth.session_valid("1." + cookie.split(".")[1])
assert auth.check_credentials("admin", "heslo123") and not auth.check_credentials("admin", "x")
print("OK")
