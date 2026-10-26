# Media Manager

A small web app that sorts a Jellyfin library by audio language and lets me fix the result from my phone. New media arrive in the library automatically; this app replaces logging in over VPN and SSH to correct the language sorting by hand every time something lands in the wrong place.

The UI is in Czech and mobile-first (bottom navigation bar, bottom sheets, long-press to delete). On desktop the same layout spreads into top tabs and a side panel.

## What it does

- **Automatic sorter** runs inside the service every `SORTER_INTERVAL` seconds. It reads audio tracks with `ffprobe` and creates hardlinks in `sorted/{cz,en,jp}/<Category>`:
  - Movies/series: CZ track → CZ, EN track → EN (both → both), nothing → EN.
  - Anime: JP track → JP, EN track or nothing → EN.
  - Each folder of a series is decided separately by its largest video file.
  - Files modified less than 2 minutes ago are treated as still downloading and skipped.
  - Links in `sorted/` that no longer belong anywhere are removed (unless the source folder is missing, e.g. an unmounted drive).
- **Manual overrides** that the sorter never overwrites. Any combination of CZ/EN/JP is allowed, including none ("nowhere" is a valid decision). *Back to auto* removes the override.
- **Tabs:** *Auto* (queue of items without a manual decision; *Confirm* locks the current state), *Movies*, *Series*, *Anime* (grids; series and anime can be set per show or per season), *Sorter* (last run, changes, *Run now*).
- **Global search** across all categories. Names are shown shortened (no release group, quality or codec); the full name is in the tooltip and detail.
- **Attach a season to another show:** when a later season was downloaded separately and Jellyfin sees it as a new show, *Patří k jiné show (další sezóna)…* moves it into the target show as `Season N`. Hardlinks, overrides and the ffprobe cache move with it.
- **Two-step delete:** a dry run lists every path that will be removed (original and all hardlinks) and returns a token; the confirm request recomputes the plan and refuses if the library changed in between.
- **Jellyfin refresh** (`POST /Library/Refresh`) is sent fire-and-forget after changes; if Jellyfin is down, the operation still succeeds.
- **`/debug` page** and startup checks: write access to the library, `sorted/` on the same filesystem as the sources (hardlinks), leftover cron `sorter.py`, ffprobe and Jellyfin availability.
- **Login form** with a signed session cookie valid for 30 days; changing the password logs out all devices.
- JSON API alongside the HTML views (see below).

## Requirements

- Python 3 with `venv`
- `ffprobe` (from ffmpeg)
- A media root containing `Movies/`, `Series/`, `Anime/` and `sorted/`, with `sorted/` on the same filesystem as the sources
- Optional: Jellyfin and an API key for library refresh

Python dependencies are pinned in `requirements.txt` (FastAPI, Uvicorn, Jinja2, pydantic-settings, httpx, python-multipart). There is no frontend build step; templates are server-rendered with htmx.

## Configuration

Everything is read from `.env` in the project root; environment variables take precedence. Copy `.env.example` to `.env`.

| Key | Default | Purpose |
| --- | --- | --- |
| `MEDIA_ROOT` | `/media/jellyfin` | library root |
| `JELLYFIN_URL` | `http://127.0.0.1:8096` | base URL for library refresh |
| `JELLYFIN_API_KEY` | empty | empty = refresh is skipped, everything else works |
| `JELLYFIN_TIMEOUT` | `10` | seconds |
| `APP_USER` / `APP_PASSWORD` | `admin` / empty | login; **without a password every request is rejected** |
| `APP_HOST` / `APP_PORT` | `0.0.0.0` / `8181` | listen address |
| `LOG_LEVEL` | `INFO` | `CRITICAL`, `ERROR`, `WARNING`, `INFO`, `DEBUG` |
| `FFPROBE_PATH` / `FFPROBE_TIMEOUT` | `ffprobe` / `90` | |
| `SORTER_INTERVAL` | `60` | seconds between sorter runs, `0` = off |

The sorter's state lives next to the media: `sorter_db.json` (ffprobe cache) and `manual_overrides.json`.

## Running locally

```bash
python3 -m venv venv
venv/bin/pip install -r requirements.txt
cp .env.example .env          # set APP_PASSWORD and MEDIA_ROOT
venv/bin/python -m app.run
```

Then open `http://localhost:8181`. Use `python -m app.run` rather than calling `uvicorn` directly, so `APP_HOST`, `APP_PORT` and `LOG_LEVEL` from `.env` take effect.

## Deployment (systemd)

It runs natively, without Docker, in an LXC container next to Jellyfin.

```bash
apt install python3-venv ffmpeg
cd /opt && git clone <repo-url> media-manager && cd media-manager
python3 -m venv venv
venv/bin/pip install -r requirements.txt
cp .env.example .env && $EDITOR .env

cp media-manager.service /etc/systemd/system/
systemctl daemon-reload && systemctl enable --now media-manager
```

The unit expects the app in `/opt/media-manager` and runs as root. Logs: `journalctl -u media-manager`.

Updating a Proxmox LXC from a checkout (`<proxmox-host>` and `<ctid>` are yours):

```bash
git archive HEAD | ssh root@<proxmox-host> "pct exec <ctid> -- tar -x -C /opt/media-manager" \
  && ssh root@<proxmox-host> "pct exec <ctid> -- systemctl restart media-manager"
```

`.env` and `venv/` are not in the repo, so they are not overwritten. Run `pip install -r requirements.txt` again when requirements change.

## Tests

```bash
venv/bin/python -m tests.test_sorter
```

A self-test of the sorter and login on a temporary library (ffprobe is mocked). Prints `OK` on success.

## API

Same session cookie as the UI; without it the API returns 401.

| Endpoint | Purpose |
| --- | --- |
| `GET /api/browse?type=movies\|series\|anime&path=` | folder listing with language state and overrides |
| `GET /api/auto` | review queue (items without an override) |
| `GET /api/item/detail?path=` | ffprobe languages, actual link placement, override |
| `POST /api/item/reanalyze` | re-run ffprobe, update `sorter_db.json` |
| `POST /api/item/set-targets` | save an override, relink, refresh Jellyfin |
| `POST /api/item/confirm` | lock the current state as a manual decision |
| `POST /api/item/delete` | dry run, returns the paths to delete and a token |
| `POST /api/item/delete/confirm` | the actual delete (takes the token) |
| `GET /api/overrides` | the whole override store |
| `GET /api/debug` | environment check results |
| `GET /api/sorter` | last sorter run and events |
| `GET /healthz` | the only route without auth, returns `{"status":"ok"}` |

## How it works

- **The filesystem is the source of truth.** The CZ/EN/JP badge is computed by comparing inodes of the source file and its counterparts in `sorted/**`, not from the ffprobe cache.
- **Override keys:** a file path for movies; for series and anime the show root or a season folder. The sorter uses the nearest setting from the episode folder up to the show root. Saving a language for the whole show clears the season settings.
- **Delete finds hardlinks by guessing first:** it checks the mirror locations in `sorted/**` against `st_nlink`; only if the count does not match does it walk the library, once for all files.
- Ffprobe runs outside the lock; linking and cleanup run under a lock shared with manual changes from the UI.

## Structure

```
app/
  main.py         FastAPI routes (HTML partials and JSON API)
  service.py      application layer (detail, reanalyze, set-targets, delete)
  library.py      filesystem: browse, inode matching, hardlinks, delete
  sorter.py       automatic sorting (background task in the service)
  overrides.py    manual_overrides.json
  probe.py        ffprobe + sorter_db.json
  jellyfin.py     fire-and-forget refresh
  jsonstore.py    atomic JSON read/write shared with the sorter
  auth.py         login form and signed session cookie
  diagnostics.py  environment checks for /debug and startup
  run.py          entry point that applies APP_HOST/APP_PORT/LOG_LEVEL
  config.py       .env -> Settings, categories, languages
  templates/, static/
tests/test_sorter.py   sorter and login self-test
media-manager.service  systemd unit
media-manager-spec.md  original specification (Czech)
```

Built with the help of AI coding tools.
