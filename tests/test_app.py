# -*- coding: utf-8 -*-
"""Service end-to-end on a temp SQLite DB with mocked engines.

Proves the per-brand config path: the fake brand tracks brand.io, which is
NOT in the faked queries.py globals — any leaked global config would show
up as missing columns / zero hits.
"""
import io
import json
import os
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import geo_tracker as gt
import app.runner as runner
import app.scheduler as scheduler
from app.db import Answer, Brand, Hit, Run
from app.main import create_app, request_guard_error

ROOT = Path(__file__).resolve().parents[1]
# Local mode (no APP_PASSWORD) answers on loopback names only, so the test
# client talks to http://localhost rather than Starlette's default "testserver".
LOCAL = "http://localhost"

BRAND_FORM = {
    "name": "TestBrand", "domains": "brand.io\nother.org",
    "niche_key": "main", "niche_title": "Main niche", "primary_domain": "brand.io",
    "queries": "q one\nq two", "geo": "UZ",
    "engines": ["gemini", "perplexity", "openai"],
    "samples": "1", "schedule": "", "articles": "", "aliases": "",
    "model_gemini": "", "model_perplexity": "", "model_openai": "",
}


def fake_answer(query, geo=""):
    return {"text": f"about {query} — brand.io rocks",
            "urls": ["https://brand.io/page"], "titles": ["brand.io"],
            "model": "fake-model", "model_requested": "fake-req",
            "usage": {"total_tokens": 10}, "searches": 1}


@pytest.fixture
def env(monkeypatch):
    """Fake engines and a clean service environment; the real .env is never read."""
    for e in gt.ENGINES:
        monkeypatch.setenv(gt.ENGINES[e]["env"], "test-key")
        monkeypatch.setitem(gt.ENGINES[e], "fn", fake_answer)
    for var in ("APP_PASSWORD", "APP_ALLOWED_HOSTS", "APP_SECRET", "APP_HTTPS_ONLY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(gt, "load_env", lambda: None)
    monkeypatch.setattr(runner, "SLEEP_SEC", 0)
    return monkeypatch


@pytest.fixture
def client(tmp_path, env):
    application = create_app(db_path=tmp_path / "test.db")
    with TestClient(application, base_url=LOCAL) as c:
        yield c


def create_brand(client, **overrides):
    form = {**BRAND_FORM, **overrides}
    r = client.post("/brands", data=form, follow_redirects=False)
    assert r.status_code == 303, r.text
    return int(r.headers["location"].rsplit("/", 1)[-1])


def test_full_flow_brand_run_reports(client):
    brand_id = create_brand(client)
    assert "TestBrand" in client.get("/").text

    r = client.post(f"/brands/{brand_id}/run", follow_redirects=False)
    assert r.status_code == 303
    run_id = int(r.headers["location"].rsplit("/", 1)[-1])

    page = client.get(f"/runs/{run_id}").text
    assert "done" in page
    assert "brand.io" in page                      # per-brand domain, not globals
    assert "example.com" not in page               # fake queries.py must not leak
    assert "Measurement uncertainty" in page
    assert "nq=2" in page                          # 2 queries behind the CI
    assert "100.0%" in page                        # every answer cites brand.io
    assert "fake-model" in page
    assert "6/6" in page                           # 2 queries × 3 engines

    Session = client.app.state.Session
    with Session() as s:
        answers = s.query(Answer).all()
        assert len(answers) == 6
        assert all(a.geo == "UZ" for a in answers)
        applied = {a.engine: a.geo_applied for a in answers}
        assert applied == {"gemini": 0, "perplexity": 1, "openai": 1}
        hits = s.query(Hit).filter(Hit.domain == "brand.io").all()
        assert len(hits) == 6 and all(h.src == 1 and h.txt == 1 for h in hits)
        assert s.query(Hit).filter(Hit.domain == "example.com").count() == 0
        first = answers[0]
        assert first.receipt["text"].startswith("about q")
        assert first.receipt["usage"] == {"total_tokens": 10}
        assert first.model_requested == "fake-req"
        receipt_page = client.get(f"/answers/{first.id}").text
        assert "brand.io rocks" in receipt_page
        assert "fake-req" in receipt_page

    html = client.get(f"/runs/{run_id}/report.html")
    assert html.status_code == 200 and "brand.io" in html.text

    docx = pytest.importorskip("docx")
    r = client.get(f"/runs/{run_id}/report.docx?lang=ru")
    assert r.status_code == 200
    text = "\n".join(p.text for p in docx.Document(io.BytesIO(r.content)).paragraphs)
    assert "brand.io" in text and "Неопределённость измерения" in text
    assert "example.com" not in text


def test_progress_endpoint(client):
    brand_id = create_brand(client)
    client.post(f"/brands/{brand_id}/run")
    r = client.get("/runs/1/progress")
    assert r.status_code == 200
    assert "done" in r.text and "6/6" in r.text


def test_run_page_follows_an_active_run_and_stops_when_it_ends(client):
    brand_id = create_brand(client)
    Session = client.app.state.Session
    with Session() as s:
        s.add(Run(brand_id=brand_id, status="running", active=1, total_calls=6))
        s.commit()
    poll = 'hx-get="/runs/1/progress"'
    refresh = 'hx-get="/runs/1"'
    assert poll in client.get("/runs/1").text            # the page starts polling itself
    assert poll in client.get("/runs/1/progress").text   # and keeps polling while active

    with Session() as s:
        run = s.get(Run, 1)
        run.status, run.active = "done", None
        s.commit()
    fragment = client.get("/runs/1/progress").text
    assert poll not in fragment and refresh in fragment  # run over → load the results once
    page = client.get("/runs/1").text
    assert poll not in page and refresh not in page      # finished page: nothing to reload


def test_one_run_at_a_time(client):
    brand_id = create_brand(client)
    Session = client.app.state.Session
    with Session() as s:
        s.add(Run(brand_id=brand_id, status="running", active=1))
        s.commit()
    r = client.post(f"/brands/{brand_id}/run", follow_redirects=False)
    assert r.headers["location"] == f"/brands/{brand_id}"  # refused, back to brand
    with Session() as s:
        assert s.query(Run).count() == 1


def test_brand_validation(client):
    for overrides, msg in (({"domains": ""}, "domain"),
                           ({"queries": ""}, "query"),
                           ({"geo": "Tashkent"}, "alpha-2"),
                           ({"samples": "0"}, "samples"),
                           ({"schedule": "not a cron"}, "cron"),
                           ({"primary_domain": "nope.io"}, "primary"),
                           ({"articles": "{bad json"}, "JSON")):
        r = client.post("/brands", data={**BRAND_FORM, **overrides})
        assert r.status_code == 400, overrides
        assert msg in r.text
    r = client.post("/brands", data={**BRAND_FORM, "name": "TestBrand"},
                    follow_redirects=False)
    assert r.status_code == 303
    r = client.post("/brands", data=BRAND_FORM)
    assert r.status_code == 400 and "already exists" in r.text


def test_schedule_job_lifecycle(client):
    sched = client.app.state.scheduler
    brand_id = create_brand(client, schedule="0 9 * * 1")
    assert sched.get_job(f"brand-{brand_id}") is not None
    r = client.post(f"/brands/{brand_id}",
                    data={**BRAND_FORM, "schedule": ""}, follow_redirects=False)
    assert r.status_code == 303
    assert sched.get_job(f"brand-{brand_id}") is None


def test_cron_means_what_crontab_means_in_utc():
    tuesday = datetime(2026, 11, 17, 7, 0, tzinfo=timezone.utc)

    def next_fire(expr):
        return scheduler.cron_trigger(expr).get_next_fire_time(None, tuesday)

    monday = next_fire("0 9 * * 1")                      # crontab: 1 = Monday
    assert monday.strftime("%A %H:%M") == "Monday 09:00"
    assert monday.utcoffset() == timedelta(0)            # UTC, not the server's zone
    assert next_fire("0 9 * * 0").strftime("%A") == "Sunday"
    assert next_fire("0 9 * * 7").strftime("%A") == "Sunday"
    assert next_fire("0 9 * * 6").strftime("%A") == "Saturday"
    assert next_fire("0 9 * * mon").strftime("%A") == "Monday"   # names pass through
    assert next_fire("30 4 * * *").strftime("%A %H:%M") == "Wednesday 04:30"

    def weekdays(expr, n):
        t, when, seen = scheduler.cron_trigger(expr), tuesday, []
        for _ in range(n):
            when = t.get_next_fire_time(when, when + timedelta(seconds=1))
            seen.append(when.strftime("%a"))
        return seen

    assert weekdays("0 9 * * 1-5", 5) == ["Tue", "Wed", "Thu", "Fri", "Mon"]  # no weekend
    assert weekdays("0 9 * * 0,3", 2) == ["Wed", "Sun"]
    assert weekdays("0 9 * * 0-6", 3) == ["Tue", "Wed", "Thu"]   # every day

    for bad in ("0 9 * * 8", "0 9 * * 5-1", "0 9 * *", "not a cron"):
        with pytest.raises(ValueError):
            scheduler.cron_trigger(bad)


def test_scheduled_job_is_registered_in_utc_on_the_right_weekday(client):
    brand_id = create_brand(client, schedule="0 9 * * 1")
    job = client.app.state.scheduler.get_job(f"brand-{brand_id}")
    assert job.next_run_time.strftime("%A %H:%M") == "Monday 09:00"
    assert job.next_run_time.utcoffset() == timedelta(0)


def test_delete_brand_cascades(client):
    brand_id = create_brand(client)
    client.post(f"/brands/{brand_id}/run")
    r = client.post(f"/brands/{brand_id}/delete", follow_redirects=False)
    assert r.status_code == 303
    assert client.get(f"/brands/{brand_id}").status_code == 404
    Session = client.app.state.Session
    with Session() as s:
        assert s.query(Brand).count() == 0
        assert s.query(Run).count() == 0
        assert s.query(Answer).count() == 0


def test_auth_password_flow(tmp_path, env):
    env.setenv("APP_PASSWORD", "secret")
    application = create_app(db_path=tmp_path / "auth.db")
    with TestClient(application) as c:  # with a password any host name is served
        r = c.get("/", follow_redirects=False)
        assert r.status_code == 307 and r.headers["location"] == "/login"
        assert c.get("/login").status_code == 200
        assert c.post("/login", data={"password": "wrong"}).status_code == 401
        r = c.post("/login", data={"password": "secret"}, follow_redirects=False)
        assert r.status_code == 303
        assert c.get("/").status_code == 200
        c.post("/logout")
        assert c.get("/", follow_redirects=False).status_code == 307


def test_login_rate_limit_refuses_after_five_failures(tmp_path, env):
    env.setenv("APP_PASSWORD", "secret")
    application = create_app(db_path=tmp_path / "rl.db")
    with TestClient(application) as c:
        for _ in range(5):
            assert c.post("/login", data={"password": "wrong"}).status_code == 401
        # even the CORRECT password is refused while the address is blocked
        r = c.post("/login", data={"password": "secret"})
        assert r.status_code == 429
        assert "Too many failed attempts" in r.text
        assert int(r.headers["retry-after"]) > 0
        assert c.get("/", follow_redirects=False).status_code == 307


def test_login_rate_limit_window_expires(tmp_path, env):
    env.setenv("APP_PASSWORD", "secret")
    application = create_app(db_path=tmp_path / "rl-window.db")
    with TestClient(application) as c:
        now = [0.0]
        application.state.login_limiter.clock = lambda: now[0]
        for _ in range(5):
            c.post("/login", data={"password": "wrong"})
        assert c.post("/login", data={"password": "secret"}).status_code == 429
        now[0] = 901.0  # 15 minutes + 1s: the failures left the window
        r = c.post("/login", data={"password": "secret"}, follow_redirects=False)
        assert r.status_code == 303


def test_login_success_resets_failure_count(tmp_path, env):
    env.setenv("APP_PASSWORD", "secret")
    application = create_app(db_path=tmp_path / "rl-reset.db")
    with TestClient(application) as c:
        for _ in range(4):
            c.post("/login", data={"password": "wrong"})
        assert c.post("/login", data={"password": "secret"},
                      follow_redirects=False).status_code == 303
        c.post("/logout")
        for _ in range(4):
            assert c.post("/login", data={"password": "wrong"}).status_code == 401
        # without the reset these would be failures 5..8 → blocked
        assert c.post("/login", data={"password": "secret"},
                      follow_redirects=False).status_code == 303


def test_session_cookie_secure_when_https_only(tmp_path, env):
    env.setenv("APP_PASSWORD", "secret")
    env.setenv("APP_HTTPS_ONLY", "1")
    application = create_app(db_path=tmp_path / "https.db")
    with TestClient(application) as c:
        r = c.post("/login", data={"password": "secret"}, follow_redirects=False)
        assert r.status_code == 303
        assert "secure" in r.headers["set-cookie"].lower()


def test_session_cookie_not_secure_by_default(tmp_path, env):
    env.setenv("APP_PASSWORD", "secret")
    application = create_app(db_path=tmp_path / "http.db")
    with TestClient(application) as c:
        r = c.post("/login", data={"password": "secret"}, follow_redirects=False)
        assert r.status_code == 303
        assert "secure" not in r.headers["set-cookie"].lower()


def test_settings_shows_key_status_not_values(client):
    page = client.get("/settings").text
    assert "set" in page and "gemini" in page
    assert "test-key" not in page


# ---------------------------------------------------------------------------
# Runs survive restarts, never overlap, and queue when scheduled together
# ---------------------------------------------------------------------------

def test_restart_closes_interrupted_run_and_frees_the_slot(tmp_path, env):
    db = tmp_path / "restart.db"
    with TestClient(create_app(db_path=db), base_url=LOCAL) as c:
        brand_id = create_brand(c)
        with c.app.state.Session() as s:  # the process dies mid-run
            s.add(Run(brand_id=brand_id, status="running", active=1))
            s.commit()
    with TestClient(create_app(db_path=db), base_url=LOCAL) as c:  # restart
        with c.app.state.Session() as s:
            dead = s.get(Run, 1)
            assert dead.status == "error" and dead.active is None
            assert "interrupted" in dead.error and dead.finished_at is not None
        r = c.post(f"/brands/{brand_id}/run", follow_redirects=False)
        assert r.headers["location"] == "/runs/2"  # not blocked by the dead run


def test_two_simultaneous_triggers_start_exactly_one_run(client):
    brand_id = create_brand(client)
    Session = client.app.state.Session
    for _ in range(20):
        with Session() as s:
            s.query(Run).delete()
            s.commit()
        barrier, started = threading.Barrier(2), []

        def trigger():
            barrier.wait()
            started.append(runner.start_run(Session, brand_id))

        threads = [threading.Thread(target=trigger) for _ in range(2)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        assert sum(1 for run_id in started if run_id is not None) == 1


def test_brands_on_the_same_cron_tick_run_one_after_another(client, env):
    gauge = {"now": 0, "max": 0}
    lock = threading.Lock()

    def slow_answer(query, geo=""):
        with lock:
            gauge["now"] += 1
            gauge["max"] = max(gauge["max"], gauge["now"])
        time.sleep(0.01)
        with lock:
            gauge["now"] -= 1
        return fake_answer(query, geo)

    for e in gt.ENGINES:
        env.setitem(gt.ENGINES[e], "fn", slow_answer)
    env.setattr(scheduler, "POLL_SEC", 0.01)
    ids = [create_brand(client, name=name, schedule="0 9 * * 1") for name in ("A", "B")]
    sched = client.app.state.scheduler
    threads = [threading.Thread(target=sched.get_job(f"brand-{i}").func, args=(i,))
               for i in ids]
    [t.start() for t in threads]
    [t.join(timeout=20) for t in threads]

    with client.app.state.Session() as s:
        runs = s.query(Run).order_by(Run.id).all()
        assert sorted(r.brand_id for r in runs) == ids      # neither tick was dropped
        assert all(r.status == "done" and r.active is None for r in runs)
        assert s.query(Answer).count() == 12
    assert gauge["max"] == 1                                 # never two runs at once


def test_scheduled_tick_waits_for_a_manual_run(client, env):
    env.setattr(scheduler, "POLL_SEC", 0.01)
    brand_id = create_brand(client, schedule="0 9 * * 1")
    Session = client.app.state.Session
    with Session() as s:
        s.add(Run(brand_id=brand_id, status="running", active=1))
        s.commit()
    job = client.app.state.scheduler.get_job(f"brand-{brand_id}")
    t = threading.Thread(target=job.func, args=(brand_id,))
    t.start()
    time.sleep(0.1)
    with Session() as s:
        assert s.query(Run).count() == 1                    # still waiting, not skipped
        manual = s.get(Run, 1)
        manual.status, manual.active = "done", None         # the manual run finishes
        s.commit()
    t.join(timeout=20)
    with Session() as s:
        scheduled = s.get(Run, 2)
        assert scheduled.trigger == "scheduled" and scheduled.status == "done"


def test_scheduled_tick_that_cannot_start_leaves_a_trace(client, env):
    env.setattr(scheduler, "MAX_WAIT_SEC", 0)
    brand_id = create_brand(client, schedule="0 9 * * 1")
    Session = client.app.state.Session
    with Session() as s:
        s.add(Run(brand_id=brand_id, status="running", active=1))
        s.commit()
    client.app.state.scheduler.get_job(f"brand-{brand_id}").func(brand_id)
    with Session() as s:
        skipped = s.get(Run, 2)
        assert skipped.status == "error" and skipped.trigger == "scheduled"
        assert skipped.error.startswith("skipped:") and skipped.active is None


# ---------------------------------------------------------------------------
# A finished run is read against its own config snapshot
# ---------------------------------------------------------------------------

def test_editing_the_brand_does_not_rewrite_a_finished_run(client):
    brand_id = create_brand(client)
    client.post(f"/brands/{brand_id}/run")
    before = client.get("/runs/1").text
    assert "100.0%" in before and "other.org" in before

    r = client.post(f"/brands/{brand_id}",
                    data={**BRAND_FORM, "domains": "brand.io\nnewrival.com",
                          "niche_key": "renamed", "niche_title": "Renamed"},
                    follow_redirects=False)
    assert r.status_code == 303

    after = client.get("/runs/1").text
    assert "100.0%" in after and "other.org" in after   # the run as it was measured
    assert "newrival.com" not in after                  # never measured → not shown as 0%
    assert "Main niche" in after and "Renamed" not in after
    assert client.get("/runs/1/report.html").status_code == 200
    pytest.importorskip("docx")
    assert client.get("/runs/1/report.docx?lang=en").status_code == 200

    client.post(f"/brands/{brand_id}/run")              # a new run uses the new config
    fresh = client.get("/runs/2").text
    assert "newrival.com" in fresh and "Renamed" in fresh


def test_run_crossing_utc_midnight_is_reported_whole(client):
    from report_html import _prepare
    brand_id = create_brand(client, engines=["gemini"])
    client.post(f"/brands/{brand_id}/run")
    Session = client.app.state.Session
    with Session() as s:
        late = s.query(Answer).filter(Answer.query == "q two").all()
        for a in late:
            a.run_ts = a.run_ts + timedelta(days=1)     # answered after 00:00 UTC
        s.commit()
        cfg, rows = runner.run_rows(s.get(Run, 1))
    assert len({r["run_date"] for r in rows}) == 1
    data = _prepare(rows, cfg["domains"], cfg["niches"])
    assert data["audit"]["main"]["brand.io"]["n_queries"] == 2  # both queries, not one


# ---------------------------------------------------------------------------
# Request guard: cross-site requests and foreign host names
# ---------------------------------------------------------------------------

def test_cross_site_post_is_refused(client):
    brand_id = create_brand(client)
    Session = client.app.state.Session
    for headers in ({"Origin": "https://evil.example"},
                    {"Referer": "https://evil.example/page"},
                    {"Origin": "null"},
                    {"Origin": "http://localhost:9999"}):     # other port = other origin
        for path in (f"/brands/{brand_id}/run", f"/brands/{brand_id}/delete"):
            assert client.post(path, headers=headers).status_code == 403, (headers, path)
    with Session() as s:
        assert s.query(Run).count() == 0 and s.query(Brand).count() == 1

    same_site = client.post(f"/brands/{brand_id}/run", headers={"Origin": LOCAL},
                            follow_redirects=False)
    assert same_site.status_code == 303 and same_site.headers["location"] == "/runs/1"


def test_local_mode_answers_on_loopback_names_only(tmp_path, env):
    application = create_app(db_path=tmp_path / "hosts.db")
    # IPv6 loopback through the pure check: the test client cannot address [::1]
    assert request_guard_error("GET", {"host": "[::1]:8000"}) is None
    for base, expected in (("http://localhost", 200), ("http://127.0.0.1:8000", 200),
                           ("http://rebound.evil.example:8000", 400),
                           ("http://192.168.1.20:8000", 400)):
        with TestClient(application, base_url=base) as c:
            assert c.get("/").status_code == expected, base

    env.setenv("APP_ALLOWED_HOSTS", "geo.example.com")
    with TestClient(application, base_url="https://geo.example.com") as c:
        assert c.get("/").status_code == 200
    with TestClient(application, base_url=LOCAL) as c:
        assert c.get("/").status_code == 400            # an explicit list is exact


# ---------------------------------------------------------------------------
# queries.py is CLI config: the service starts without it, the CLI says so
# ---------------------------------------------------------------------------

NO_QUERIES = (
    "import sys\n"
    "class Block:\n"
    "    def find_spec(self, name, path=None, target=None):\n"
    "        if name == 'queries':\n"
    "            raise ModuleNotFoundError(\"No module named 'queries'\")\n"
    "sys.meta_path.insert(0, Block())\n"
    f"sys.path.insert(0, {str(ROOT)!r})\n")


def test_service_starts_without_queries_py(tmp_path):
    p = subprocess.run([sys.executable, "-c", NO_QUERIES + "import app.main\n"],
                       capture_output=True, text=True, cwd=tmp_path)
    assert p.returncode == 0, p.stderr


def test_cli_without_queries_py_explains_what_to_do(tmp_path):
    code = (NO_QUERIES + "import geo_tracker\n"
            "sys.argv = ['geo_tracker.py', 'report']\n"
            "geo_tracker.main()\n")
    p = subprocess.run([sys.executable, "-c", code], capture_output=True,
                       text=True, cwd=tmp_path)
    assert p.returncode != 0
    assert "queries.example.py" in p.stderr and "Traceback" not in p.stderr


# ---------------------------------------------------------------------------
# Settings written in .env reach the app (the documented place for them)
# ---------------------------------------------------------------------------

def test_settings_from_dotenv_are_applied(tmp_path):
    """`uvicorn app.main:app` with every setting in .env and none exported.

    Runs in a subprocess: the settings are read while the module-level app is
    built, and this process has load_env switched off (see conftest).
    """
    (tmp_path / ".env").write_text(
        "APP_PASSWORD=pw-from-dotenv\n"
        "APP_SECRET=secret-from-dotenv\n"
        "APP_HTTPS_ONLY=1\n"
        f"GEO_DB_PATH={tmp_path / 'from_dotenv.db'}\n", encoding="utf-8")
    code = (
        "import sys, json\n"
        "from pathlib import Path\n"
        f"sys.path.insert(0, {str(ROOT)!r})\n"
        "import geo_tracker as gt\n"
        f"gt.BASE_DIR = Path({str(tmp_path)!r})\n"      # where .env is looked up
        "import app.main as m\n"
        "from fastapi.testclient import TestClient\n"
        "from starlette.middleware.sessions import SessionMiddleware\n"
        "mw = [x for x in m.app.user_middleware if x.cls is SessionMiddleware][0]\n"
        "with TestClient(m.app) as c:\n"
        "    r = c.post('/login', data={'password': 'pw-from-dotenv'}, follow_redirects=False)\n"
        "print(json.dumps({'login': r.status_code,\n"
        "                  'cookie': r.headers.get('set-cookie', ''),\n"
        "                  'secret': mw.kwargs.get('secret_key')}))\n")
    clean = {k: v for k, v in os.environ.items()
             if not k.startswith(("APP_", "GEO_DB_PATH"))}
    p = subprocess.run([sys.executable, "-c", code], capture_output=True,
                       text=True, cwd=tmp_path, env=clean)
    assert p.returncode == 0, p.stderr
    out = json.loads(p.stdout.strip().splitlines()[-1])
    assert out["login"] == 303                           # APP_PASSWORD
    assert "secure" in out["cookie"].lower()             # APP_HTTPS_ONLY
    assert out["secret"] == "secret-from-dotenv"         # APP_SECRET
    assert (tmp_path / "from_dotenv.db").exists()        # GEO_DB_PATH
