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

# --- zkrácené názvy, hledání
from app import library, service  # noqa: E402
assert library.clean_name("[Erai-raws] Cyberpunk - Edgerunners - 01 ~ 10 [1080p][Multiple Subtitle]") \
    == "Cyberpunk - Edgerunners"
assert library.clean_name("Alien.Covenant.2017.1080p.BluRay.x264-[YTS.AG].mp4") == "Alien Covenant 2017"
assert library.show_base_name("Cyberpunk - Edgerunners - Season 2") == "Cyberpunk - Edgerunners"
assert library.guess_season("Cyberpunk - Edgerunners - Season 2") == 2
assert library.guess_season("JoJo (Seasons 1-4 + OVAs)") is None
found = library.search("show", {})
assert [i.name for i in found["series"]] == ["Show"] and "movies" not in found

# --- spojení dvou sezón a pak přidání třetí do už spojené show
video("Anime/[Grp] Cyber - 01 ~ 10 [1080p]/c1.mkv", ["jpn", "eng"])
video("Anime/Cyber - Season 2/c2.mkv", ["jpn"])
video("Anime/Cyber S03/c3.mkv", ["eng"])
sorter.run_once()
assert linked("c1.mkv") == ["en/Anime/[Grp] Cyber - 01 ~ 10 [1080p]/c1.mkv",
                            "jp/Anime/[Grp] Cyber - 01 ~ 10 [1080p]/c1.mkv"]
service.trigger_refresh = lambda: None
a = str(tmp / "Anime/[Grp] Cyber - 01 ~ 10 [1080p]")
b = str(tmp / "Anime/Cyber - Season 2")
service.merge_shows([a, b], "Cyber", {a: 1, b: 2})
assert (tmp / "Anime/Cyber/Season 1/c1.mkv").is_file() and (tmp / "Anime/Cyber/Season 2/c2.mkv").is_file()
assert not os.path.exists(a) and not os.path.exists(b)
assert linked("c1.mkv") == ["en/Anime/Cyber/Season 1/c1.mkv", "jp/Anime/Cyber/Season 1/c1.mkv"]
assert linked("c2.mkv") == ["jp/Anime/Cyber/Season 2/c2.mkv"]
assert str(tmp / "Anime/Cyber/Season 1/c1.mkv") in probe.load_db(), "cache se má přenést"
c = str(tmp / "Anime/Cyber S03")
try:
    service.merge_shows([str(tmp / "Anime/Cyber"), c], "Cyber", {c: 2})
    raise AssertionError("kolize sezón se má odmítnout")
except library.PathError:
    pass
assert (tmp / "Anime/Cyber S03/c3.mkv").is_file(), "po odmítnutí se nic nesmí pohnout"
service.merge_shows([str(tmp / "Anime/Cyber"), c], "Cyber", {c: 3})
assert (tmp / "Anime/Cyber/Season 3/c3.mkv").is_file() and (tmp / "Anime/Cyber/Season 1/c1.mkv").is_file()
assert sorter.run_once()["removed"] == 0, "sorter nemá po spojení nic uklízet"
assert linked("c3.mkv") == ["en/Anime/Cyber/Season 3/c3.mkv"]

# --- „patří k“: S02 do show, která sezóny ještě nemá; pak další bez čísla v názvu
video("Series/[Grp] Edge - 01 ~ 10/e1.mkv", ["eng"])
video("Series/Edge - Season 2/e2.mkv", ["eng"])
video("Series/Edge extra/e9.mkv", ["eng"])
sorter.run_once()
edge = tmp / "Series/[Grp] Edge - 01 ~ 10"
r = service.attach_season(str(tmp / "Series/Edge - Season 2"), str(edge))
assert r["season"] == 2 and (edge / "Season 1/e1.mkv").is_file() and (edge / "Season 2/e2.mkv").is_file()
assert service.attach_season(str(tmp / "Series/Edge extra"), str(edge / "Season 1"))["season"] == 3
assert linked("e9.mkv") == ["en/Series/[Grp] Edge - 01 ~ 10/Season 3/e9.mkv"]
assert sorter.run_once()["removed"] == 0

cookie = auth.make_session()
assert auth.session_valid(cookie)
assert not auth.session_valid(cookie[:-1] + ("0" if cookie[-1] != "0" else "1"))
assert not auth.session_valid("1." + cookie.split(".")[1])
assert auth.check_credentials("admin", "heslo123") and not auth.check_credentials("admin", "x")
print("OK")
