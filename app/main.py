"""FastAPI aplikace — HTML (Jinja2 + HTMX) i JSON API ze sekce 9 specifikace."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import quote

from fastapi import FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from . import auth, diagnostics, library, overrides, service, sorter
from .auth import Auth, LoginRequired
from .config import LANG_LABELS, LANGS, TABS, Category, get_settings
from .jellyfin import trigger_refresh
from .library import PathError

logging.basicConfig(
    level=getattr(logging, get_settings().log_level, logging.INFO),
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)
log = logging.getLogger("media-manager")

BASE_DIR = Path(__file__).resolve().parent

app = FastAPI(title="Media Manager", docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")

templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
templates.env.filters["human_size"] = service.human_size
templates.env.globals.update(LANGS=LANGS, LANG_LABELS=LANG_LABELS, TABS=TABS)


@app.on_event("startup")
async def _startup() -> None:
    settings = get_settings()
    log.info(
        "Media Manager startuje — MEDIA_ROOT=%s, port=%s, .env=%s",
        settings.media_root, settings.app_port,
        "načteno" if settings.env_file_found else "CHYBÍ",
    )
    # Síťovou kontrolu Jellyfinu při startu vynecháme, ať se běh nezdrží,
    # když je zrovna dole; na /debug se dělá kompletní.
    diagnostics.log_report(diagnostics.run_checks(include_network=False, include_writes=True))
    if settings.sorter_interval > 0:
        app.state.sorter_task = asyncio.create_task(_sorter_loop(settings.sorter_interval))
    else:
        log.warning("SORTER_INTERVAL=0 — automatické třídění je vypnuté")


async def _run_sorter() -> dict:
    stats = await asyncio.to_thread(sorter.run_once)
    if stats.get("created") or stats.get("removed"):
        trigger_refresh()
    return stats


async def _sorter_loop(interval: int) -> None:
    while True:
        try:
            await _run_sorter()
        except Exception:  # noqa: BLE001 — smyčka nesmí umřít kvůli jednomu běhu
            log.exception("Běh sorteru spadl")
        await asyncio.sleep(interval)


# --------------------------------------------------------------------------
# Chyby
# --------------------------------------------------------------------------

@app.exception_handler(LoginRequired)
async def _login_required(request: Request, exc: LoginRequired):
    if request.url.path.startswith("/api/"):
        return JSONResponse({"detail": "Nepřihlášen"}, status_code=401)
    if request.headers.get("HX-Request"):
        # HTMX by redirect jen tiše vložil do stránky; tohle přesměruje celé okno.
        return Response(status_code=200, headers={"HX-Redirect": "/login"})
    target = request.url.path + (f"?{request.url.query}" if request.url.query else "")
    return RedirectResponse(f"/login?next={quote(target)}", status_code=303)

@app.exception_handler(PathError)
async def _path_error(request: Request, exc: PathError):
    message = str(exc)
    log.warning("PathError na %s: %s", request.url.path, message)
    if request.headers.get("HX-Request"):
        # HX-Reswap: none — odpověď nese jen out-of-band toast, cíl requestu
        # (panel, modal) se nesmí přepsat prázdnem.
        return templates.TemplateResponse(
            request, "partials/toast_oob.html",
            {"toast": {"kind": "error", "message": message}},
            status_code=200, headers={"HX-Reswap": "none"},
        )
    return JSONResponse({"detail": message}, status_code=400)


@app.exception_handler(ValueError)
async def _value_error(request: Request, exc: ValueError):
    return await _path_error(request, PathError(str(exc)))


def _page(request: Request, template: str, ctx: dict, partial: str,
          headers: dict[str, str] | None = None):
    """HTMX dostane jen vnitřek, přímé načtení celou stránku."""
    ctx = {**ctx, "health": service.library_health()}
    if request.headers.get("HX-Request"):
        return templates.TemplateResponse(request, partial, ctx, headers=headers)
    return templates.TemplateResponse(request, template, ctx, headers=headers)


def _browse_url(cat: Category, rel: str) -> str:
    return f"/b/{cat.key}" + (f"?path={quote(rel, safe='/')}" if rel else "")


# --------------------------------------------------------------------------
# HTML
# --------------------------------------------------------------------------

@app.get("/", include_in_schema=False)
async def root(_: Auth):
    return RedirectResponse("/b/movies", status_code=302)


@app.get("/healthz", include_in_schema=False)
async def healthz():
    return {"status": "ok"}


def _safe_next(target: str) -> str:
    # Jen cesta v rámci appky — žádný open redirect na cizí doménu.
    if not target.startswith("/") or target.startswith(("//", "/\\")):
        return "/"
    return target


@app.get("/login", response_class=HTMLResponse, include_in_schema=False)
async def login_page(request: Request, next: str = "/"):
    if auth.session_valid(request.cookies.get(auth.COOKIE)):
        return RedirectResponse(_safe_next(next), status_code=303)
    return templates.TemplateResponse(request, "login.html", {"next": _safe_next(next)})


@app.post("/login", include_in_schema=False)
async def login_submit(
    request: Request,
    username: Annotated[str, Form()] = "",
    password: Annotated[str, Form()] = "",
    next: Annotated[str, Form()] = "/",
):
    if not auth.check_credentials(username, password):
        log.warning("Neúspěšné přihlášení (%s) z %s", username,
                    request.client.host if request.client else "?")
        await asyncio.sleep(1)   # brzda proti hádání hesla
        return templates.TemplateResponse(
            request, "login.html",
            {"next": _safe_next(next), "error": "Špatné jméno nebo heslo.", "username": username},
            status_code=401,
        )
    resp = RedirectResponse(_safe_next(next), status_code=303)
    resp.set_cookie(
        auth.COOKIE, auth.make_session(), max_age=auth.MAX_AGE, httponly=True,
        samesite="lax", secure=request.url.scheme == "https",
    )
    return resp


@app.post("/logout", include_in_schema=False)
async def logout():
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(auth.COOKIE)
    return resp


@app.get("/b/{category}", response_class=HTMLResponse, include_in_schema=False)
async def page_browse(request: Request, _: Auth, category: str, path: str = ""):
    cat = library.get_category(category)
    # Po smazání poslední položky složka zmizí, ale URL v prohlížeči (a hx-get
    # na #view, který se po `library-changed` obnovuje) na ni dál ukazuje.
    # Místo PathError se výpis vrátí do nejbližší existující nadřazené složky
    # a adresa se opraví — u HTMX hlavičkou, u přímého načtení redirectem.
    wanted = path.strip().strip("/")
    rel = library.nearest_listing_rel(cat, wanted)
    headers: dict[str, str] = {}
    if rel != wanted:
        url = _browse_url(cat, rel)
        if not request.headers.get("HX-Request"):
            return RedirectResponse(url, status_code=302)
        headers["HX-Replace-Url"] = url

    listing = library.browse(cat, rel, overrides.load_all())
    return _page(request, "index.html",
                 {"listing": listing, "active": cat.key},
                 "partials/view_browse.html", headers=headers)


@app.get("/auto", response_class=HTMLResponse, include_in_schema=False)
async def page_auto(request: Request, _: Auth):
    return _page(request, "index.html",
                 {"items": service.review_items(), "active": "auto"},
                 "partials/view_auto.html")


@app.get("/debug", response_class=HTMLResponse, include_in_schema=False)
async def page_debug(request: Request, _: Auth):
    diagnostics.invalidate_summary()
    report = diagnostics.run_checks()
    return _page(request, "index.html",
                 {"report": report, "active": "debug"},
                 "partials/view_debug.html")


@app.get("/sorter", response_class=HTMLResponse, include_in_schema=False)
async def page_sorter(request: Request, _: Auth):
    return _page(request, "index.html", {"active": "sorter", **_sorter_ctx()},
                 "partials/view_sorter.html")


@app.post("/sorter/run", response_class=HTMLResponse, include_in_schema=False)
async def sorter_run(request: Request, _: Auth):
    stats = await _run_sorter()
    toast = ({"kind": "error", "message": "Sorter už právě běží."} if stats.get("busy")
             else {"kind": "ok", "message": f"Hotovo: +{stats['created']} / -{stats['removed']} linků"})
    return templates.TemplateResponse(
        request, "partials/view_sorter.html", {**_sorter_ctx(), "toast": toast},
        headers={"HX-Trigger": "library-changed"} if not stats.get("busy") else None,
    )


def _sorter_ctx() -> dict:
    return {"last": sorter.last_run, "events": list(sorter.events),
            "interval": get_settings().sorter_interval}


@app.get("/partial/detail", response_class=HTMLResponse, include_in_schema=False)
async def partial_detail(request: Request, _: Auth, path: str):
    return templates.TemplateResponse(
        request, "partials/detail.html", {"d": service.get_detail(path)}
    )


@app.post("/partial/detail/save", response_class=HTMLResponse, include_in_schema=False)
async def partial_save(
    request: Request,
    _: Auth,
    path: Annotated[str, Form()],
    targets: Annotated[list[str], Form()] = [],
):
    result = service.set_targets(path, targets)
    added, gone = len(result["created"]), len(result["removed"])
    where = "+".join(t.upper() for t in result["targets"]) or "nikam"
    message = (f"{result['name']} → {where}"
               + (f" ({added} linků přidáno, {gone} odebráno)" if added or gone else ""))
    return templates.TemplateResponse(
        request, "partials/detail.html",
        {"d": service.get_detail(path), "toast": {"kind": "ok", "message": message}},
        headers={"HX-Trigger": "library-changed"},
    )


@app.post("/partial/detail/reanalyze", response_class=HTMLResponse, include_in_schema=False)
async def partial_reanalyze(request: Request, _: Auth, path: Annotated[str, Form()]):
    try:
        detail = service.reanalyze(path)
        found = ", ".join(detail.probe_langs) or "žádná audio stopa"
        toast = {"kind": "ok", "message": f"ffprobe: {found}"}
    except RuntimeError as exc:
        detail, toast = service.get_detail(path), {"kind": "error", "message": str(exc)}
    return templates.TemplateResponse(
        request, "partials/detail.html", {"d": detail, "toast": toast}
    )


@app.post("/partial/detail/clear", response_class=HTMLResponse, include_in_schema=False)
async def partial_clear(request: Request, _: Auth, path: Annotated[str, Form()]):
    return templates.TemplateResponse(
        request, "partials/detail.html",
        {"d": service.clear_target_override(path),
         "toast": {"kind": "ok", "message": "Zpět pod automatiku."}},
        headers={"HX-Trigger": "library-changed"},
    )


@app.post("/partial/auto/set", response_class=HTMLResponse, include_in_schema=False)
async def partial_auto_set(
    request: Request,
    _: Auth,
    path: Annotated[str, Form()],
    targets: Annotated[list[str], Form()] = [],
):
    """Rychlá oprava přímo v Auto seznamu — položka tím ze seznamu zmizí."""
    result = service.set_targets(path, targets)
    where = "+".join(t.upper() for t in result["targets"]) or "nikam"
    return templates.TemplateResponse(
        request, "partials/row_done.html",
        {"toast": {"kind": "ok", "message": f"{result['name']} → {where}"}},
    )


@app.get("/partial/delete", response_class=HTMLResponse, include_in_schema=False)
async def partial_delete_modal(request: Request, _: Auth, path: str):
    return templates.TemplateResponse(
        request, "partials/delete_modal.html", {"plan": service.delete_plan(path)}
    )


@app.post("/partial/delete", response_class=HTMLResponse, include_in_schema=False)
async def partial_delete_confirm(
    request: Request, _: Auth,
    path: Annotated[str, Form()],
    token: Annotated[str, Form()] = "",
):
    result = service.delete_confirm(path, token or None)
    if result["failed"]:
        toast = {"kind": "error",
                 "message": f"Smazáno {result['count']}, {len(result['failed'])} selhalo — viz log."}
    else:
        toast = {"kind": "ok",
                 "message": f"Smazáno: {result['name']} ({result['count']} souborů)"}
    return templates.TemplateResponse(
        request, "partials/deleted.html", {"toast": toast},
        headers={"HX-Trigger": "library-changed"},
    )


# --------------------------------------------------------------------------
# JSON API (sekce 9)
# --------------------------------------------------------------------------

class TargetsBody(BaseModel):
    path: str
    targets: list[str] = Field(default_factory=list)


class PathBody(BaseModel):
    path: str


class DeleteBody(BaseModel):
    path: str
    token: str | None = None


def _item_json(i: library.Item) -> dict:
    return {
        "name": i.name, "rel": i.rel, "path": i.abs_path, "is_dir": i.is_dir,
        "action": i.action, "langs": i.langs, "has_override": i.has_override,
        "override_targets": i.override_targets, "size": i.size, "mtime": i.mtime,
        "category": i.category, "episodes": i.episodes,
        "probe_langs": i.probe_langs, "auto_langs": i.auto_langs,
    }


@app.get("/api/browse")
async def api_browse(
    _: Auth,
    type: Literal["movies", "series", "anime"] = Query(...),
    path: str = "",
):
    listing = library.browse(library.get_category(type), path, overrides.load_all())
    return {
        "category": listing.category.key,
        "path": listing.rel,
        "abs_path": listing.abs_path,
        "parent": listing.parent_rel,
        "inside_show": listing.inside_show,
        "items": [_item_json(i) for i in listing.items],
    }


@app.get("/api/auto")
async def api_auto(_: Auth):
    items = service.review_items()
    return {"count": len(items), "items": [_item_json(i) for i in items]}


@app.get("/api/item/detail")
async def api_detail(_: Auth, path: str):
    d = service.get_detail(path)
    return {
        "name": d.name, "path": d.abs_path, "category": d.category.key, "kind": d.kind,
        "override_key": d.override_key,
        "linked_langs": d.langs,
        "probe_langs": d.probe_langs,
        "probe_stale": d.probe_stale,
        "probe_source": d.probe_source,
        "auto_langs": d.auto_langs,
        "override": (None if not d.has_override
                     else {"targets": d.override_targets, "set_at": d.override_set_at}),
        "file_count": d.file_count,
        "total_bytes": d.total_bytes,
    }


@app.post("/api/item/reanalyze")
async def api_reanalyze(_: Auth, body: PathBody):
    try:
        d = service.reanalyze(body.path)
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {"path": d.abs_path, "probe_source": d.probe_source, "langs": d.probe_langs}


@app.post("/api/item/set-targets")
async def api_set_targets(_: Auth, body: TargetsBody):
    return {"path": body.path, **service.set_targets(body.path, body.targets)}


@app.post("/api/item/confirm")
async def api_confirm(_: Auth, body: PathBody):
    return service.confirm_auto(body.path)


@app.post("/api/item/delete")
async def api_delete_dryrun(_: Auth, body: PathBody):
    return service.delete_plan(body.path)


@app.post("/api/item/delete/confirm")
async def api_delete_confirm(_: Auth, body: DeleteBody):
    return service.delete_confirm(body.path, body.token)


@app.get("/api/debug")
async def api_debug(_: Auth):
    report = diagnostics.run_checks()
    return {
        "status": report.status,
        "checks": [
            {"name": c.name, "status": c.status, "detail": c.detail, "hint": c.hint}
            for c in report.checks
        ],
    }


@app.get("/api/overrides")
async def api_overrides(_: Auth):
    return overrides.load_all()


@app.get("/api/sorter")
async def api_sorter(_: Auth):
    return {"interval": get_settings().sorter_interval, "last_run": sorter.last_run,
            "events": [{"time": t, "level": lvl, "message": m} for t, lvl, m in sorter.events]}
