# -*- coding: utf-8 -*-
"""FastAPI + HTMX service over the geo-tracker core (M1 skeleton).

Single user, many brands. One run at a time. Auth: one password from
APP_PASSWORD (.env); unset = local mode without login, reachable through
loopback host names only. Run as ONE process (the scheduler and the run
queue live in it):
    uvicorn app.main:app
"""
import json
import os
import re
import secrets
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Request
from fastapi.responses import (FileResponse, HTMLResponse, PlainTextResponse,
                               RedirectResponse)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import text
from starlette.background import BackgroundTask
from starlette.middleware.sessions import SessionMiddleware

import geo_tracker as gt
from report_html import build_dashboard, resolve_branding
from report_docx import build_docx
from sov_stats import (sov_stats, unowned_cited_sources, DEFAULT_B, DEFAULT_SEED,
                       MIN_BOOTSTRAP_QUERIES)

from .db import Answer, Brand, Run, make_engine, make_session_factory, migrate_database
from . import dynamics
from .login_guard import LoginLimiter
from .runner import (BIG_RUN_CONFIRM, WAITING, execute_run, incomplete_notice,
                     recover_interrupted, run_estimate, run_rows, start_run)
from .scheduler import cron_trigger, make_scheduler

APP_DIR = Path(__file__).resolve().parent
TEMPLATES = Jinja2Templates(directory=str(APP_DIR / "templates"))


LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}

# On EVERY /share/... response: the token in the URL must not leak through
# the Referer header when a client clicks an external source link, and
# search engines must not index client links.
SHARE_HEADERS = {"Referrer-Policy": "no-referrer",
                 "X-Robots-Tag": "noindex, nofollow"}


def env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


def allowed_hosts():
    """Host names the service answers to; None = any.

    APP_ALLOWED_HOSTS (comma-separated) wins. Without it, local mode (no
    APP_PASSWORD) answers on loopback names only: with no login there is
    nothing else between a web page in the same browser and this service,
    and a rebound DNS name must not be able to reach it.
    """
    raw = os.environ.get("APP_ALLOWED_HOSTS", "")
    names = {h.strip().lower() for h in raw.split(",") if h.strip()}
    if names:
        return names
    return None if os.environ.get("APP_PASSWORD") else LOOPBACK_HOSTS


def request_guard_error(method: str, headers, path: str = "") -> tuple:
    """(status, message) if the request must be refused, else None.

    1. Host must be an allowed name (see allowed_hosts). One narrow exception:
       GET/HEAD /healthz on a loopback name. A container healthcheck has no
       session cookie and addresses 127.0.0.1 even when APP_ALLOWED_HOSTS
       lists only the public domain. The exception is read-only, loopback
       only and /healthz only — GET / on loopback is still refused then.
    2. A state-changing request that a browser sent from another site is
       refused: its Origin (or Referer) names a different host than the one
       it is addressed to. Requests without either header are not browser
       cross-site requests (curl, scripts) and pass.
    """
    host = (headers.get("host") or "").strip().lower()
    hostname = urlsplit("//" + host).hostname or ""
    allowed = allowed_hosts()
    if allowed is not None and hostname not in allowed:
        healthz = (path == "/healthz" and method.upper() in ("GET", "HEAD")
                   and hostname in LOOPBACK_HOSTS)
        if not healthz:
            return 400, ("host not allowed — set APP_ALLOWED_HOSTS (and APP_PASSWORD) "
                         "to serve this name")
    if method.upper() not in SAFE_METHODS:
        source = headers.get("origin") or headers.get("referer")
        if source is not None and urlsplit(source).netloc.lower() != host:
            return 403, "cross-site request refused"
    return None


def _hit(row, domain):
    return row.get(f"src_{domain}") == "1" or row.get(f"txt_{domain}") == "1"


def parse_brand_form(form) -> tuple:
    """(config dict, error string) from the brand form. Niches arrive as
    aligned repeated fields (niche_key / niche_title / primary_domain /
    queries — one set per niche row); articles/aliases are optional JSON."""
    name = form.get("name", "").strip()
    domains = [d.strip().lower() for d in form.get("domains", "").splitlines() if d.strip()]
    geo = form.get("geo", "").strip().upper()
    engines = [e for e in form.getlist("engines") if e in gt.ENGINES]
    schedule = form.get("schedule", "").strip()
    try:
        samples = int(form.get("samples") or 1)
    except ValueError:
        samples = 0
    models = {e: form.get(f"model_{e}", "").strip() for e in gt.ENGINES
              if form.get(f"model_{e}", "").strip()}
    try:
        articles = json.loads(form.get("articles") or "{}")
        aliases = json.loads(form.get("aliases") or "{}")
    except ValueError:
        return {}, "articles/aliases must be valid JSON objects"
    if not isinstance(articles, dict) or not isinstance(aliases, dict):
        return {}, "articles/aliases must be JSON objects"

    if not name:
        return {}, "name is required"
    if not domains:
        return {}, "at least one domain is required"

    keys = form.getlist("niche_key")
    titles = form.getlist("niche_title")
    primaries = form.getlist("primary_domain")
    query_blocks = form.getlist("queries")
    niches = {}
    for i, raw_key in enumerate(keys):
        key = re.sub(r"\W+", "_", raw_key.strip()) or f"niche_{i + 1}"
        block = query_blocks[i] if i < len(query_blocks) else ""
        queries = [q.strip() for q in block.splitlines() if q.strip()]
        title = (titles[i].strip() if i < len(titles) else "") or name
        primary = primaries[i].strip().lower() if i < len(primaries) else ""
        if not queries:
            return {}, f"niche '{key}': at least one query is required"
        if key in niches:
            return {}, f"duplicate niche key: '{key}'"
        if primary and primary not in domains:
            return {}, f"niche '{key}': primary domain must be one of the domains"
        niches[key] = {"title": title, "primary_domain": primary or domains[0],
                       "queries": queries}
    if not niches:
        return {}, "at least one niche is required"

    if geo and not re.fullmatch(r"[A-Z]{2}", geo):
        return {}, "geo must be an ISO alpha-2 country code (e.g. UZ)"
    if not engines:
        return {}, "pick at least one engine"
    if samples < 1:
        return {}, "samples must be >= 1"
    if schedule:
        try:
            cron_trigger(schedule)
        except ValueError as e:
            return {}, f"invalid cron schedule: {e}"
    cfg = {
        "name": name, "domains": domains, "aliases": aliases,
        "niches": niches,
        "articles": articles, "geo": geo, "engines": engines,
        "models": models, "samples": samples, "schedule": schedule,
    }
    return cfg, ""


def form_values(brand=None, form=None) -> dict:
    """Template-ready form values from a Brand or a submitted form.
    values.niches is a list of row dicts, one per niche."""
    if form is not None:
        v = {k: form.get(k, "") for k in
             ("name", "geo", "samples", "schedule", "articles", "aliases")}
        v["domains"] = form.get("domains", "")
        v["engines"] = form.getlist("engines")
        keys = form.getlist("niche_key")
        titles = form.getlist("niche_title")
        primaries = form.getlist("primary_domain")
        blocks = form.getlist("queries")
        v["niches"] = [
            {"key": keys[i] if i < len(keys) else "",
             "title": titles[i] if i < len(titles) else "",
             "primary_domain": primaries[i] if i < len(primaries) else "",
             "queries": blocks[i] if i < len(blocks) else ""}
            for i in range(max(len(keys), len(blocks), 1))]
        for e in gt.ENGINES:
            v[f"model_{e}"] = form.get(f"model_{e}", "")
        return v
    niches = [{"key": k, "title": cfg.get("title", ""),
               "primary_domain": cfg.get("primary_domain", ""),
               "queries": "\n".join(cfg.get("queries", []))}
              for k, cfg in (brand.niches.items() if brand else [])]
    return {
        "name": brand.name if brand else "",
        "domains": "\n".join(brand.domains) if brand else "",
        "niches": niches or [{"key": "main", "title": "",
                              "primary_domain": "", "queries": ""}],
        "geo": brand.geo if brand else "",
        "engines": brand.engines if brand else list(gt.ENGINES),
        "samples": brand.samples if brand else 1,
        "schedule": brand.schedule if brand else "",
        "articles": json.dumps(brand.articles, ensure_ascii=False, indent=2) if brand and brand.articles else "",
        "aliases": json.dumps(brand.aliases, ensure_ascii=False, indent=2) if brand and brand.aliases else "",
        **{f"model_{e}": (brand.models.get(e, "") if brand else "") for e in gt.ENGINES},
    }


def _run_label(run) -> str:
    when = run.started_at.strftime("%Y-%m-%d %H:%M") if run.started_at else "—"
    return f"#{run.id} · {when}"


def _run_history_point(run, cache: dict) -> tuple:
    """(stats, day, cfg) of one done run for the history block.

    A done run never changes — its answers and its config snapshot are fixed
    — so its figures are computed once. Recomputing them for every run on
    every view of the brand page costs seconds per run on large query sets.
    Keyed by finished_at as well: SQLite may reuse a deleted run's id."""
    key = (run.id, run.finished_at)
    if cache is not None and key in cache:
        return cache[key]
    cfg, rows = run_rows(run)
    stats = dynamics.share_ci_rows(rows, cfg["domains"], cfg["niches"])
    day = rows[0]["run_date"] if rows else (
        run.started_at.strftime("%Y-%m-%d") if run.started_at else f"run {run.id}")
    point = (stats, day, cfg)
    if cache is not None:
        cache[key] = point
    return point


def _brand_history(brand, runs, cache: dict = None) -> tuple:
    """Per niche × domain history over DONE runs only, each read through its
    own snapshot (run_rows). Non-done runs never contribute figures. Returns
    (history_json for tables+charts, done_list for the compare selectors).

    Each point carries "break": why it is not comparable with the previous
    point of its series (rules version, geo or the domain's aliases changed),
    or "". The comparison refuses such pairs; the history must not draw them
    as a trend either."""
    done = sorted((r for r in runs if r.status == "done"), key=lambda r: r.id)
    hist: dict = {}
    last_basis: dict = {}
    # A niche removed from the brand lives on in its done runs' snapshots;
    # keep the latest snapshot title so its history stays human-readable.
    snap_titles: dict = {}
    for r in done:
        stats, day, cfg = _run_history_point(r, cache)
        for niche, ncfg in (cfg.get("niches") or {}).items():
            title = (ncfg or {}).get("title")
            if title:
                snap_titles[niche] = title  # done runs ascend by id: latest wins
        for niche, by_dom in stats.items():
            for d, st in by_dom.items():
                cur = dynamics.basis(cfg, d)
                why = dynamics.basis_change(last_basis.get((niche, d)), cur)
                last_basis[(niche, d)] = cur
                hist.setdefault(niche, {}).setdefault(d, []).append(
                    {"run_id": r.id, "label": f"#{r.id} · {day}", "break": why, **st})
    history_json = {}
    for niche, by_dom in hist.items():
        title = ((brand.niches.get(niche) or {}).get("title")
                 or snap_titles.get(niche) or niche)
        history_json[niche] = {"title": title, "domains": {
            d: {"labels": [p["label"] for p in pts],
                "point": [p["point"] for p in pts],
                "lo": [p["lo"] for p in pts],
                "hi": [p["hi"] for p in pts],
                "nq": [p["n_queries"] for p in pts],
                "n": [p["n_samples"] for p in pts],
                "method": [p["method"] for p in pts],
                "breaks": [p["break"] for p in pts],
                "run_ids": [p["run_id"] for p in pts]}
            for d, pts in by_dom.items()}}
    done_list = [{"id": r.id, "label": _run_label(r)} for r in done]
    return history_json, done_list


def create_app(db_path=None) -> FastAPI:
    # .env first. Everything below takes its settings from the environment
    # while the app is being built — the database path, the session key, the
    # cookie flags. Loaded any later (it used to happen at startup), those
    # settings silently fell back to their defaults when they came from .env.
    gt.load_env()
    engine = make_engine(db_path)
    Session = make_session_factory(engine)
    sched, sync_schedule, load_schedules, stop_queue = make_scheduler(Session)
    login_limiter = LoginLimiter()
    history_cache: dict = {}   # figures of done runs, see _run_history_point

    @asynccontextmanager
    async def lifespan(app):
        migrate_database(engine)
        # Before anything can start a run: a run left active by the previous
        # process is dead and would block every new one.
        recover_interrupted(Session)
        load_schedules()
        sched.start()
        yield
        stop_queue()
        sched.shutdown(wait=False)

    app = FastAPI(title="geo-tracker", lifespan=lifespan)

    @app.middleware("http")
    async def request_guard(request: Request, call_next):
        refused = request_guard_error(request.method, request.headers,
                                      request.url.path)
        if refused:
            return PlainTextResponse(refused[1], status_code=refused[0])
        response = await call_next(request)
        if request.url.path.startswith("/share/"):
            # every shared response, error pages included
            for k, v in SHARE_HEADERS.items():
                response.headers[k] = v
        return response

    app.add_middleware(
        SessionMiddleware,
        secret_key=os.environ.get("APP_SECRET") or secrets.token_hex(32),
        # APP_HTTPS_ONLY: mark the session cookie Secure when the service
        # sits behind HTTPS. Browsers then refuse to send it over plain
        # HTTP — enable it only when TLS terminates in front of the app.
        https_only=env_flag("APP_HTTPS_ONLY"))
    app.mount("/static", StaticFiles(directory=str(APP_DIR / "static")), name="static")
    app.state.Session = Session
    app.state.scheduler = sched
    app.state.login_limiter = login_limiter

    def auth_guard(request: Request):
        if not os.environ.get("APP_PASSWORD"):
            return  # local mode: no password configured
        if request.session.get("authed"):
            return
        raise HTTPException(status_code=307, headers={"Location": "/login"})

    def render(request, template, ctx=None, status=200, headers=None):
        base = {"request": request, "authed": bool(request.session.get("authed")),
                "auth_enabled": bool(os.environ.get("APP_PASSWORD"))}
        base.update(ctx or {})
        if base.get("share_mode"):
            # white-label header: the publisher from REPORT_BRAND, else the
            # neutral logo — never the admin chrome
            base.setdefault("report_brand", resolve_branding()[0])
        return TEMPLATES.TemplateResponse(request, template, base,
                                          status_code=status, headers=headers)

    def brand_form_context(s, b, values, error=""):
        """The complete brand_form.html context for an existing brand — runs,
        history, compare selectors and estimate included, so an error page is
        the same page with a banner, not a stripped-down form.

        The active slot is GLOBAL (Run.active is unique across all brands):
        any brand's pending/running run disables Run now on every brand."""
        runs = list(b.runs)
        active_run = s.query(Run).filter(Run.active == 1).first()
        busy = any(r.status in WAITING for r in runs) or active_run is not None
        history_json, done_list = _brand_history(b, runs, history_cache)
        return {"brand": b, "values": values, "error": error,
                "engines_all": list(gt.ENGINES), "runs": runs, "busy": busy,
                "other_busy": active_run is not None and active_run.brand_id != b.id,
                "estimate": run_estimate(b), "big_confirm": BIG_RUN_CONFIRM,
                "history_json": history_json, "done_list": done_list}

    # -- auth ---------------------------------------------------------------
    @app.get("/login", response_class=HTMLResponse)
    def login_page(request: Request):
        if not os.environ.get("APP_PASSWORD"):
            return RedirectResponse("/", status_code=303)
        return render(request, "login.html", {"error": ""})

    @app.post("/login", response_class=HTMLResponse)
    async def login_submit(request: Request):
        ip = request.client.host if request.client else "unknown"
        if login_limiter.blocked(ip):
            return render(request, "login.html",
                          {"error": "Too many failed attempts — try again later"},
                          status=429,
                          headers={"Retry-After": str(login_limiter.retry_after(ip))})
        form = await request.form()
        given = str(form.get("password") or "").encode()
        expected = (os.environ.get("APP_PASSWORD") or "").encode()
        if expected and secrets.compare_digest(given, expected):
            login_limiter.reset(ip)
            request.session["authed"] = True
            return RedirectResponse("/", status_code=303)
        login_limiter.record_failure(ip)
        return render(request, "login.html",
                      {"error": "Wrong password"}, status=401)

    @app.post("/logout")
    def logout(request: Request):
        request.session.clear()
        return RedirectResponse("/login", status_code=303)

    # -- health ---------------------------------------------------------------
    # GET and HEAD: FastAPI (unlike plain Starlette) does not add HEAD to a
    # GET route on its own, and the host guard exempts both for healthchecks.
    @app.api_route("/healthz", methods=["GET", "HEAD"],
                   response_class=PlainTextResponse)
    def healthz():
        """Liveness + database check for container healthchecks. Deliberately
        without auth_guard: a healthcheck has no session cookie. Answers only
        when the process serves AND the database executes a query; exposes no
        data. The host guard lets loopback GET/HEAD reach it even when
        APP_ALLOWED_HOSTS lists only the public domain (see request_guard_error)."""
        with Session() as s:
            s.execute(text("SELECT 1"))
        return "ok"

    # -- brands -------------------------------------------------------------
    @app.get("/", response_class=HTMLResponse, dependencies=[Depends(auth_guard)])
    def index(request: Request):
        with Session() as s:
            brands = s.query(Brand).order_by(Brand.name).all()
            latest = {b.id: (b.runs[0] if b.runs else None) for b in brands}
        return render(request, "index.html", {"brands": brands, "latest": latest})

    @app.get("/brands/new", response_class=HTMLResponse, dependencies=[Depends(auth_guard)])
    def brand_new(request: Request):
        return render(request, "brand_form.html",
                      {"brand": None, "values": form_values(), "error": "",
                       "engines_all": list(gt.ENGINES)})

    @app.post("/brands", response_class=HTMLResponse, dependencies=[Depends(auth_guard)])
    async def brand_create(request: Request):
        form = await request.form()
        cfg, error = parse_brand_form(form)
        if error:
            return render(request, "brand_form.html",
                          {"brand": None, "values": form_values(form=form),
                           "error": error, "engines_all": list(gt.ENGINES)}, status=400)
        with Session() as s:
            if s.query(Brand).filter(Brand.name == cfg["name"]).first():
                return render(request, "brand_form.html",
                              {"brand": None, "values": form_values(form=form),
                               "error": "a brand with this name already exists",
                               "engines_all": list(gt.ENGINES)}, status=400)
            b = Brand(**cfg)
            s.add(b)
            s.commit()
            sync_schedule(b.id, b.schedule)
            return RedirectResponse(f"/brands/{b.id}", status_code=303)

    @app.get("/brands/niche-row", response_class=HTMLResponse, dependencies=[Depends(auth_guard)])
    def niche_row(request: Request):
        """One empty niche fieldset, appended to the form by HTMX. Declared
        before /brands/{brand_id} so 'niche-row' is not parsed as an id."""
        return TEMPLATES.TemplateResponse(
            request, "_niche_row.html",
            {"n": {"key": "", "title": "", "primary_domain": "", "queries": ""}})

    @app.get("/brands/{brand_id}", response_class=HTMLResponse, dependencies=[Depends(auth_guard)])
    def brand_edit(request: Request, brand_id: int):
        with Session() as s:
            b = s.get(Brand, brand_id)
            if b is None:
                raise HTTPException(404, "brand not found")
            return render(request, "brand_form.html",
                          brand_form_context(s, b, form_values(brand=b)))

    def compare_context(s, brand, a, b):
        """The comparison page context, scoped to one brand: runs of any
        other brand read as "not found in this brand". Shared by the admin
        and the client-share comparison pages."""
        done = sorted((r for r in brand.runs if r.status == "done"),
                      key=lambda r: r.id)
        done_list = [{"id": r.id, "label": _run_label(r)} for r in done]
        ctx = {"brand": brand, "done_list": done_list, "a": a, "b": b,
               "result": None, "error": ""}
        if a is not None and b is not None:
            ra, rb = s.get(Run, a), s.get(Run, b)
            if (ra is None or rb is None or ra.brand_id != brand.id
                    or rb.brand_id != brand.id):
                ctx["error"] = "run not found in this brand"
            elif ra.status != "done" or rb.status != "done":
                ctx["error"] = "only done runs can be compared"
            elif a == b:
                ctx["error"] = "pick two different runs"
            else:
                cfg_a, rows_a = run_rows(ra)
                cfg_b, rows_b = run_rows(rb)
                ctx["result"] = dynamics.compare(
                    cfg_a, rows_a, cfg_b, rows_b,
                    status_a=ra.status, status_b=rb.status)
                ctx["label_a"], ctx["label_b"] = _run_label(ra), _run_label(rb)
        return ctx

    @app.get("/brands/{brand_id}/compare", response_class=HTMLResponse, dependencies=[Depends(auth_guard)])
    def compare_page(request: Request, brand_id: int, a: int = None, b: int = None):
        with Session() as s:
            brand = s.get(Brand, brand_id)
            if brand is None:
                raise HTTPException(404, "brand not found")
            return render(request, "comparison.html", compare_context(s, brand, a, b))

    @app.post("/brands/{brand_id}", response_class=HTMLResponse, dependencies=[Depends(auth_guard)])
    async def brand_update(request: Request, brand_id: int):
        form = await request.form()
        cfg, error = parse_brand_form(form)
        with Session() as s:
            b = s.get(Brand, brand_id)
            if b is None:
                raise HTTPException(404, "brand not found")
            # names are UNIQUE: refuse a rename onto another brand's name
            # with a form error instead of an IntegrityError crash.
            if not error and s.query(Brand).filter(
                    Brand.name == cfg["name"], Brand.id != brand_id).first():
                error = "a brand with this name already exists"
            if error:
                return render(request, "brand_form.html",
                              brand_form_context(s, b, form_values(form=form), error),
                              status=400)
            for k, v in cfg.items():
                setattr(b, k, v)
            s.commit()
            sync_schedule(b.id, b.schedule)
        return RedirectResponse(f"/brands/{brand_id}", status_code=303)

    @app.post("/brands/{brand_id}/delete", dependencies=[Depends(auth_guard)])
    def brand_delete(brand_id: int, request: Request):
        with Session() as s:
            b = s.get(Brand, brand_id)
            if b is None:
                raise HTTPException(404, "brand not found")
            runs = list(b.runs)
            if any(r.status in WAITING or r.active == 1 for r in runs):
                # A live execute_run thread (or a queued tick about to start)
                # still writes to these rows — deleting the brand underneath
                # it would orphan its answers or crash it. Refuse until the
                # run is cancelled or finishes.
                return render(request, "brand_form.html",
                              brand_form_context(
                                  s, b, form_values(brand=b),
                                  "cannot delete a brand while its run is active "
                                  "or queued — cancel the run first"),
                              status=409)
            s.delete(b)
            s.commit()
        sync_schedule(brand_id, "")
        return RedirectResponse("/", status_code=303)

    # -- client share links (admin side) -------------------------------------
    @app.post("/brands/{brand_id}/share", dependencies=[Depends(auth_guard)])
    def share_enable(brand_id: int):
        """Create or rotate the brand's read-only share token. The old link
        stops working the moment a new token is committed."""
        with Session() as s:
            b = s.get(Brand, brand_id)
            if b is None:
                raise HTTPException(404, "brand not found")
            b.share_token = secrets.token_urlsafe(24)  # 192 bits, URL-safe
            s.commit()
        return RedirectResponse(f"/brands/{brand_id}", status_code=303)

    @app.post("/brands/{brand_id}/share/revoke", dependencies=[Depends(auth_guard)])
    def share_revoke(brand_id: int):
        with Session() as s:
            b = s.get(Brand, brand_id)
            if b is None:
                raise HTTPException(404, "brand not found")
            b.share_token = None
            s.commit()
        return RedirectResponse(f"/brands/{brand_id}", status_code=303)

    # -- runs ---------------------------------------------------------------
    @app.post("/brands/{brand_id}/run", dependencies=[Depends(auth_guard)])
    async def run_trigger(brand_id: int, background: BackgroundTasks, request: Request):
        form = await request.form()
        with Session() as s:
            b = s.get(Brand, brand_id)
            if b is None:
                raise HTTPException(404, "brand not found")
            est = run_estimate(b)
        # Big runs need an explicit confirmation carrying the call count —
        # the browser dialog sets big_confirmed; a bare POST does not start it.
        if est["calls"] > BIG_RUN_CONFIRM and form.get("big_confirmed") != "1":
            return RedirectResponse(f"/brands/{brand_id}", status_code=303)
        run_id = start_run(Session, brand_id, trigger="manual")
        if run_id is None:
            return RedirectResponse(f"/brands/{brand_id}", status_code=303)
        background.add_task(execute_run, Session, run_id)
        return RedirectResponse(f"/runs/{run_id}", status_code=303)

    @app.post("/runs/{run_id}/cancel", dependencies=[Depends(auth_guard)])
    def run_cancel(run_id: int):
        with Session() as s:
            run = s.get(Run, run_id)
            if run is None:
                raise HTTPException(404, "run not found")
            if run.status in WAITING:  # running, pending, or queued behind another run
                run.cancel_requested = 1
                s.commit()
        return RedirectResponse(f"/runs/{run_id}", status_code=303)

    def run_context(s, run):
        # Everything below is read against the run's own config snapshot:
        # editing the brand later must not rewrite a finished run.
        b = run.brand
        cfg, rows = run_rows(run)
        ok_rows = [r for r in rows if r["status"] == "ok"]
        matrix = {}
        for niche in cfg["niches"]:
            sub = [r for r in ok_rows if r["niche"] == niche]
            if sub:
                matrix[niche] = {d: round(100 * sum(_hit(r, d) for r in sub) / len(sub), 1)
                                 for d in cfg["domains"]}
        audit = sov_stats(ok_rows, cfg["domains"], cfg["niches"], grounded=True)
        # Third-party citation gap: strictly from the snapshot's domains/niches,
        # so adding a rival to the brand later never rewrites a finished run.
        sources_gap = unowned_cited_sources(ok_rows, cfg["domains"], cfg["niches"],
                                            limit=10)
        usage = {}
        for a in run.answers:
            u = a.usage or {}
            agg = usage.setdefault(a.engine, {"calls": 0, "with_usage": 0, "tokens": 0,
                                              "searches": 0, "searches_known": False,
                                              "cost": 0.0, "cost_known": False})
            agg["calls"] += 1
            if u:
                agg["with_usage"] += 1
            t = u.get("totalTokenCount", u.get("total_tokens"))
            if isinstance(t, (int, float)):
                agg["tokens"] += t
            if isinstance(a.searches, int):
                agg["searches"] += a.searches
                agg["searches_known"] = True
            c = (u.get("cost") or {}).get("total_cost")
            if isinstance(c, (int, float)):
                agg["cost"] += c
                agg["cost_known"] = True
        return {"run": run, "brand": b, "cfg": cfg, "matrix": matrix, "audit": audit,
                "sources_gap": sources_gap,
                "usage": usage, "answers": list(run.answers),
                "active": run.status in WAITING,
                # shown once the run is over without being done
                "notice": None if run.status in WAITING else incomplete_notice(run),
                "B": DEFAULT_B, "seed": DEFAULT_SEED, "N": MIN_BOOTSTRAP_QUERIES}

    @app.get("/runs/{run_id}", response_class=HTMLResponse, dependencies=[Depends(auth_guard)])
    def run_page(request: Request, run_id: int):
        with Session() as s:
            run = s.get(Run, run_id)
            if run is None:
                raise HTTPException(404, "run not found")
            ctx = run_context(s, run)
        return render(request, "run.html", ctx)

    @app.get("/runs/{run_id}/progress", response_class=HTMLResponse, dependencies=[Depends(auth_guard)])
    def run_progress(request: Request, run_id: int):
        with Session() as s:
            run = s.get(Run, run_id)
            if run is None:
                raise HTTPException(404, "run not found")
        return TEMPLATES.TemplateResponse(request, "_progress.html",
                                          {"run": run, "poll": True, "refresh": True})

    @app.get("/answers/{answer_id}", response_class=HTMLResponse, dependencies=[Depends(auth_guard)])
    def answer_receipt(request: Request, answer_id: str):
        with Session() as s:
            a = s.get(Answer, answer_id)
            if a is None:
                raise HTTPException(404, "answer not found")
            return TEMPLATES.TemplateResponse(request, "_receipt.html", {"a": a})

    # -- exports ------------------------------------------------------------
    # One builder per format for the admin AND the client-share exports:
    # same config snapshot, same incomplete_notice, same white-label subject.
    def html_export(run):
        b = run.brand
        cfg, rows = run_rows(run)
        fd, tmp = tempfile.mkstemp(suffix=".html")
        os.close(fd)
        # subject = the audited brand; publisher branding (REPORT_BRAND /
        # REPORT_ACCENT) is resolved from the environment by the builder.
        build_dashboard(rows, cfg["domains"], cfg["niches"], Path(tmp),
                        articles=cfg["articles"], notice=incomplete_notice(run),
                        report_subject=b.name)
        tag = "" if run.status == "done" else f"_{run.status}"
        return tmp, f"geo_dashboard_{b.name}_{run.id}{tag}.html"

    def docx_export(run, lang):
        b = run.brand
        cfg, rows = run_rows(run)
        fd, tmp = tempfile.mkstemp(suffix=".docx")
        os.close(fd)
        build_docx(Path(tmp), lang=lang, rows=rows, domains=cfg["domains"],
                   niches=cfg["niches"], articles=cfg["articles"],
                   notice=incomplete_notice(run, lang), report_subject=b.name)
        tag = "" if run.status == "done" else f"_{run.status}"
        return tmp, f"geo_report_{lang}_{b.name}_{run.id}{tag}.docx"

    @app.get("/runs/{run_id}/report.html", dependencies=[Depends(auth_guard)])
    def export_html(run_id: int):
        with Session() as s:
            run = s.get(Run, run_id)
            if run is None:
                raise HTTPException(404, "run not found")
            tmp, name = html_export(run)
        return FileResponse(tmp, filename=name,
                            background=BackgroundTask(os.unlink, tmp))

    @app.get("/runs/{run_id}/report.docx", dependencies=[Depends(auth_guard)])
    def export_docx(run_id: int, lang: str = "en"):
        if lang not in ("en", "ru"):
            raise HTTPException(400, "lang must be en or ru")
        with Session() as s:
            run = s.get(Run, run_id)
            if run is None:
                raise HTTPException(404, "run not found")
            tmp, name = docx_export(run, lang)
        return FileResponse(tmp, filename=name,
                            background=BackgroundTask(os.unlink, tmp))

    # -- client share links (public, read-only) ------------------------------
    # No auth_guard: possessing the brand's exact share_token IS the access.
    # Every route is strictly scoped to that one brand; anything else reads
    # as "not found". SHARE_HEADERS are attached by the request_guard
    # middleware to every /share/... response.
    def _shared_brand(s, token: str) -> Brand:
        if not token:
            raise HTTPException(404, "not found")
        b = s.query(Brand).filter(Brand.share_token == token).first()
        if b is None:
            raise HTTPException(404, "not found")
        return b

    def _share_ctx(token: str, brand) -> dict:
        return {"share_mode": True, "share_token": token,
                "share_brand_name": brand.name, "share_home": f"/share/{token}"}

    def _shared_run(s, brand, run_id: int) -> Run:
        run = s.get(Run, run_id)
        if run is None or run.brand_id != brand.id:
            raise HTTPException(404, "not found")
        return run

    @app.get("/share/{token}", response_class=HTMLResponse, name="share_brand")
    def share_brand(request: Request, token: str):
        with Session() as s:
            b = _shared_brand(s, token)
            runs = list(b.runs)  # newest first (Run.id.desc)
            done_runs = [r for r in runs if r.status == "done"]
            history_json, done_list = _brand_history(b, runs, history_cache)
            ctx = {"brand": b, "token": token, "done_runs": done_runs,
                   "history_json": history_json, "done_list": done_list,
                   **_share_ctx(token, b)}
        return render(request, "share_brand.html", ctx, headers=SHARE_HEADERS)

    @app.get("/share/{token}/compare", response_class=HTMLResponse)
    def share_compare(request: Request, token: str, a: int = None, b: int = None):
        with Session() as s:
            brand = _shared_brand(s, token)
            ctx = compare_context(s, brand, a, b)
            ctx.update(_share_ctx(token, brand))
        return render(request, "comparison.html", ctx, headers=SHARE_HEADERS)

    @app.get("/share/{token}/runs/{run_id}", response_class=HTMLResponse)
    def share_run(request: Request, token: str, run_id: int):
        with Session() as s:
            b = _shared_brand(s, token)
            run = _shared_run(s, b, run_id)
            ctx = run_context(s, run)
            ctx.update(_share_ctx(token, b))
        return render(request, "run.html", ctx, headers=SHARE_HEADERS)

    @app.get("/share/{token}/answers/{answer_id}", response_class=HTMLResponse)
    def share_answer(request: Request, token: str, answer_id: str):
        with Session() as s:
            b = _shared_brand(s, token)
            a = s.get(Answer, answer_id)
            if a is None or a.run.brand_id != b.id:
                raise HTTPException(404, "not found")
            return TEMPLATES.TemplateResponse(
                request, "_receipt.html", {"a": a, "share_mode": True},
                headers=SHARE_HEADERS)

    @app.get("/share/{token}/runs/{run_id}/report.html")
    def share_export_html(token: str, run_id: int):
        with Session() as s:
            b = _shared_brand(s, token)
            run = _shared_run(s, b, run_id)
            tmp, name = html_export(run)
        return FileResponse(tmp, filename=name, headers=SHARE_HEADERS,
                            background=BackgroundTask(os.unlink, tmp))

    @app.get("/share/{token}/runs/{run_id}/report.docx")
    def share_export_docx(token: str, run_id: int, lang: str = "en"):
        if lang not in ("en", "ru"):
            raise HTTPException(400, "lang must be en or ru")
        with Session() as s:
            b = _shared_brand(s, token)
            run = _shared_run(s, b, run_id)
            tmp, name = docx_export(run, lang)
        return FileResponse(tmp, filename=name, headers=SHARE_HEADERS,
                            background=BackgroundTask(os.unlink, tmp))

    # -- settings -----------------------------------------------------------
    @app.get("/settings", response_class=HTMLResponse, dependencies=[Depends(auth_guard)])
    def settings(request: Request):
        keys = {e: bool(os.environ.get(cfg["env"])) for e, cfg in gt.ENGINES.items()}
        jobs = [{"id": j.id, "next": str(j.next_run_time)} for j in sched.get_jobs()]
        return render(request, "settings.html",
                      {"keys": keys, "jobs": jobs,
                       "auth_enabled": bool(os.environ.get("APP_PASSWORD")),
                       "db_path": str(engine.url)})

    return app


app = create_app()
