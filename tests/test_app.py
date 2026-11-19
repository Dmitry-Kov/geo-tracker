# -*- coding: utf-8 -*-
"""Service end-to-end on a temp SQLite DB with mocked engines.

Proves the per-brand config path: the fake brand tracks brand.io, which is
NOT in the faked queries.py globals — any leaked global config would show
up as missing columns / zero hits.
"""
import io
import json
import os
import re
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError

import geo_tracker as gt
import app.runner as runner
import app.scheduler as scheduler
from app.db import Answer, ArticleHit, Brand, Hit, Run, make_engine
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
    for var in ("APP_PASSWORD", "APP_ALLOWED_HOSTS", "APP_SECRET", "APP_HTTPS_ONLY",
                "REPORT_BRAND", "REPORT_ACCENT"):
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


# ---------------------------------------------------------------------------
# White-label exports: the audited brand as subject, publisher branding from
# REPORT_BRAND / REPORT_ACCENT, no hardcoded publisher anywhere
# ---------------------------------------------------------------------------

def dashboard_data(html):
    start = html.index("const D = ") + len("const D = ")
    return json.loads(html[start:html.index(";\n", start)])


def test_exports_carry_brand_subject_and_env_branding(client, env):
    docx = pytest.importorskip("docx")
    brand_id = create_brand(client)
    r = client.post(f"/brands/{brand_id}/run", follow_redirects=False)
    run_id = int(r.headers["location"].rsplit("/", 1)[-1])

    html = client.get(f"/runs/{run_id}/report.html").text
    assert "Optimize.uz" not in html
    assert "Measured via official search APIs, not consumer chat apps." in html
    assert '<section id="trendSec" hidden>' in html          # single run: no trend
    data = dashboard_data(html)
    assert data["subject"] == "TestBrand"                    # the audited brand
    assert data["brand"] == "" and data["accent"] == "#FF5C1F"

    for lang, footer in (
            ("en", "GEO Tracker · Official search APIs only (not consumer chat apps)"),
            ("ru", "GEO-трекер · Только официальные поисковые API "
                   "(не потребительские чат-приложения)")):
        r = client.get(f"/runs/{run_id}/report.docx?lang={lang}")
        assert r.status_code == 200
        doc = docx.Document(io.BytesIO(r.content))
        text = "\n".join(p.text for p in doc.paragraphs)
        assert "Optimize.uz" not in text
        assert "TestBrand · " in text                        # subject prefixes subtitle
        assert footer in text

    # publisher branding comes from the environment at export time
    env.setenv("REPORT_BRAND", "agency.io")
    env.setenv("REPORT_ACCENT", "#2B6CB0")
    html = client.get(f"/runs/{run_id}/report.html").text
    data = dashboard_data(html)
    assert data["brand"] == "agency.io" and data["subject"] == "TestBrand"
    assert data["accent"] == "#2B6CB0" and data["accent_rgb"] == [43, 108, 176]
    assert data["colors"]["brand.io"] == "#2B6CB0"           # first domain color only
    assert "agency.io" not in html.split("const D = ")[0]    # never raw markup

    r = client.get(f"/runs/{run_id}/report.docx?lang=en")
    doc = docx.Document(io.BytesIO(r.content))
    text = "\n".join(p.text for p in doc.paragraphs)
    assert doc.paragraphs[0].text == "agency.io  GEO Monitor"
    assert "TestBrand · " in text
    assert "agency.io · Official search APIs only (not consumer chat apps)" in text
    assert "2B6CB0" in doc.element.xml and "FF5C1F" not in doc.element.xml


def test_report_branding_env_does_not_leak_between_tests(client):
    # the previous test set REPORT_BRAND / REPORT_ACCENT; defaults must be back
    brand_id = create_brand(client)
    client.post(f"/brands/{brand_id}/run")
    data = dashboard_data(client.get("/runs/1/report.html").text)
    assert data["brand"] == "" and data["accent"] == "#FF5C1F"
    assert data["subject"] == "TestBrand"


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
        waiting = s.get(Run, 2)                             # the wait is a visible run
        assert waiting.status == "queued" and waiting.trigger == "scheduled"
        assert waiting.active is None and waiting.config is None
        manual = s.get(Run, 1)
        manual.status, manual.active = "done", None         # the manual run finishes
        s.commit()
    t.join(timeout=20)
    with Session() as s:
        assert s.query(Run).count() == 2                    # the same row, not a new one
        scheduled = s.get(Run, 2)
        assert scheduled.trigger == "scheduled" and scheduled.status == "done"
        assert scheduled.config["domains"] == ["brand.io", "other.org"]  # snapshot at start


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


def test_run_config_snapshots_the_matching_rules_version(client):
    brand_id = create_brand(client)
    client.post(f"/brands/{brand_id}/run")
    Session = client.app.state.Session
    with Session() as s:
        cfg = s.get(Run, 1).config
        assert cfg["match_rules_version"] == gt.MATCH_RULES_VERSION == 2


# ---------------------------------------------------------------------------
# Top cited third-party sources: run-page counts, snapshot, incomplete notice
# ---------------------------------------------------------------------------

def rival_answer(query, geo=""):
    # "q one": the brand is cited alongside a third party; "q two": it is not.
    hosts = ["news.example.net"]
    hosts += ["brand.io"] if query == "q one" else ["review.example.org"]
    return {"text": f"about {query}",
            "urls": [f"https://{h}/page" for h in hosts],
            "titles": hosts, "model": "fake-model",
            "usage": {"total_tokens": 10}, "searches": 1}


def gap_section(page):
    return page.split("Top cited third-party sources")[1].split("Resource usage")[0]


def test_run_page_shows_third_party_sources_gap(client, env):
    for e in gt.ENGINES:
        env.setitem(gt.ENGINES[e], "fn", rival_answer)
    brand_id = create_brand(client, engines=["gemini"])
    client.post(f"/brands/{brand_id}/run")
    page = client.get("/runs/1").text
    assert "Top cited third-party sources — grounded" in page
    assert "2 grounded queries · 2 answers" in page
    # news.example.net: both queries, brand absent on one of them;
    # review.example.org: only the brand-absent query
    assert "2/2 (2 ans.)" in page
    assert "1/2 (1 ans.)" in page
    assert page.count("1 queries (1 ans.)") == 2
    section = gap_section(page)
    assert section.index("news.example.net") < section.index("review.example.org")
    assert "gemini" in section

    data = dashboard_data(client.get("/runs/1/report.html").text)
    assert data["sources_gap"]["main"] == {
        "total_grounded": 2, "total_queries": 2,
        "sources": [
            {"domain": "news.example.net", "answers": 2, "queries": 2,
             "gap_answers": 1, "gap_queries": 1, "engines": ["gemini"]},
            {"domain": "review.example.org", "answers": 1, "queries": 1,
             "gap_answers": 1, "gap_queries": 1, "engines": ["gemini"]}]}
    docx = pytest.importorskip("docx")
    r = client.get("/runs/1/report.docx?lang=en")
    text = "\n".join(p.text for p in docx.Document(io.BytesIO(r.content)).paragraphs)
    assert "Top cited third-party sources" in text


def test_sources_gap_respects_the_run_snapshot(client, env):
    for e in gt.ENGINES:
        env.setitem(gt.ENGINES[e], "fn", rival_answer)
    brand_id = create_brand(client, engines=["gemini"])
    client.post(f"/brands/{brand_id}/run")
    assert "news.example.net" in gap_section(client.get("/runs/1").text)

    # the brand later starts tracking the rival and renames the niche: the
    # finished run must still be read against the config it was measured with
    r = client.post(f"/brands/{brand_id}",
                    data={**BRAND_FORM, "engines": ["gemini"],
                          "domains": "brand.io\nother.org\nnews.example.net",
                          "niche_title": "Renamed niche"},
                    follow_redirects=False)
    assert r.status_code == 303
    after = client.get("/runs/1").text
    section = gap_section(after)
    assert "news.example.net" in section                    # untracked THEN → shown
    assert "review.example.org" in section
    assert "2/2 (2 ans.)" in section and "1 queries (1 ans.)" in section
    assert "Main niche" in section and "Renamed niche" not in section

    # a NEW run under the new config no longer lists the now-tracked rival
    client.post(f"/brands/{brand_id}/run")
    fresh = gap_section(client.get("/runs/2").text)
    assert "news.example.net" not in fresh
    assert "review.example.org" in fresh


def test_sources_gap_of_a_cancelled_run_still_says_incomplete(client, env):
    brand_id = create_brand(client, engines=["gemini"])
    Session = client.app.state.Session
    run_id = runner.start_run(Session, brand_id)
    calls = []

    def ask_then_cancel(query, geo=""):
        calls.append(query)
        if len(calls) == 1:
            with Session() as s:
                s.get(Run, run_id).cancel_requested = 1
                s.commit()
        return rival_answer(query, geo)

    env.setitem(gt.ENGINES["gemini"], "fn", ask_then_cancel)
    runner.execute_run(Session, run_id)
    page = client.get(f"/runs/{run_id}").text
    assert "Incomplete run — cancelled: 1 of 2 planned calls" in page
    assert "Top cited third-party sources — grounded" in page
    assert "news.example.net" in gap_section(page)          # collected answer kept

    dash = client.get(f"/runs/{run_id}/report.html").text
    assert "Incomplete run" in dash
    assert dashboard_data(dash)["sources_gap"]["main"]["total_grounded"] == 1
    docx = pytest.importorskip("docx")
    r = client.get(f"/runs/{run_id}/report.docx?lang=en")
    text = "\n".join(p.text for p in docx.Document(io.BytesIO(r.content)).paragraphs)
    assert "Incomplete run — cancelled" in text
    assert "Top cited third-party sources" in text


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
# /healthz: liveness + DB check without a session, loopback even behind
# APP_ALLOWED_HOSTS (Docker's HEALTHCHECK addresses 127.0.0.1)
# ---------------------------------------------------------------------------

def test_healthz_answers_ok_in_local_mode(tmp_path, env):
    application = create_app(db_path=tmp_path / "healthz_local.db")
    for base in (LOCAL, "http://127.0.0.1:8000"):
        with TestClient(application, base_url=base) as c:
            r = c.get("/healthz")
            assert r.status_code == 200, base
            assert r.text == "ok"
            assert r.headers["content-type"].startswith("text/plain")


def test_healthz_with_password_and_allowed_hosts(tmp_path, env):
    env.setenv("APP_PASSWORD", "test-password")
    env.setenv("APP_ALLOWED_HOSTS", "geo.example.com")
    application = create_app(db_path=tmp_path / "healthz_hosts.db")

    # Public name, no session cookie: healthz answers, the pages still login.
    with TestClient(application, base_url="https://geo.example.com") as c:
        r = c.get("/healthz")
        assert r.status_code == 200 and r.text == "ok"
        assert c.get("/", follow_redirects=False).status_code == 307

    # Docker healthcheck: loopback host although only the public name is
    # allowed — while GET / on loopback stays refused (the list is exact).
    with TestClient(application, base_url="http://127.0.0.1:8000") as c:
        r = c.get("/healthz")
        assert r.status_code == 200 and r.text == "ok"
        assert c.head("/healthz").status_code == 200
        assert c.get("/").status_code == 400
        # The exception is read-only: POST /healthz is not exempt.
        assert c.post("/healthz").status_code == 400

    # Foreign hosts get no exception, not even on /healthz.
    with TestClient(application, base_url="http://rebound.evil.example:8000") as c:
        assert c.get("/healthz").status_code == 400

    # The pure check behind the middleware: loopback GET/HEAD only.
    assert request_guard_error("GET", {"host": "127.0.0.1:8000"}, "/healthz") is None
    assert request_guard_error("HEAD", {"host": "[::1]:8000"}, "/healthz") is None
    assert request_guard_error("POST", {"host": "127.0.0.1:8000"}, "/healthz") is not None
    assert request_guard_error("GET", {"host": "127.0.0.1:8000"}, "/") is not None
    assert request_guard_error("GET", {"host": "evil.example"}, "/healthz") is not None


def test_healthz_fails_when_the_database_is_unreachable(tmp_path, env):
    """The endpoint must execute a query, not just answer "ok": with the DB
    broken it has to fail, or a healthcheck would pass on a dead database."""
    from sqlalchemy import event
    application = create_app(db_path=tmp_path / "healthz_db.db")
    with TestClient(application, base_url=LOCAL,
                    raise_server_exceptions=False) as c:
        assert c.get("/healthz").status_code == 200
        engine = application.state.Session.kw["bind"]

        @event.listens_for(engine, "before_cursor_execute")
        def db_down(conn, cursor, statement, parameters, context, executemany):
            raise RuntimeError("database is down")

        try:
            assert c.get("/healthz").status_code == 500
        finally:
            event.remove(engine, "before_cursor_execute", db_down)


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
# Multiple niches per brand
# ---------------------------------------------------------------------------

MULTI_FORM = {**BRAND_FORM,
              "niche_key": ["crm", "hosting"],
              "niche_title": ["CRM", "Hosting"],
              "primary_domain": ["brand.io", "other.org"],
              "queries": ["best crm\ncheap crm", "best hosting"]}


def test_multi_niche_brand_run_and_reports(client):
    r = client.post("/brands", data=MULTI_FORM, follow_redirects=False)
    assert r.status_code == 303, r.text
    brand_id = int(r.headers["location"].rsplit("/", 1)[-1])
    client.post(f"/brands/{brand_id}/run")
    page = client.get("/runs/1").text
    assert "CRM" in page and "Hosting" in page
    Session = client.app.state.Session
    with Session() as s:
        answers = s.query(Answer).all()
        assert {a.niche for a in answers} == {"crm", "hosting"}
        assert len(answers) == 9  # 3 queries × 3 engines
        b = s.get(Brand, brand_id)
        assert set(b.niches) == {"crm", "hosting"}
        assert b.niches["hosting"]["primary_domain"] == "other.org"
    docx = pytest.importorskip("docx")
    r = client.get("/runs/1/report.docx?lang=en")
    doc = docx.Document(io.BytesIO(r.content))
    text = "\n".join(p.text for p in doc.paragraphs)
    tables = "\n".join(c.text for t in doc.tables for row in t.rows for c in row.cells)
    assert "CRM" in text + tables and "Hosting" in text + tables


def test_niche_row_partial(client):
    r = client.get("/brands/niche-row")
    assert r.status_code == 200
    assert 'name="niche_key"' in r.text and 'name="queries"' in r.text


def test_multi_niche_validation(client):
    dup = {**MULTI_FORM, "niche_key": ["crm", "crm"]}
    r = client.post("/brands", data=dup)
    assert r.status_code == 400 and "duplicate" in r.text
    empty = {**MULTI_FORM, "queries": ["best crm", ""]}
    r = client.post("/brands", data=empty)
    assert r.status_code == 400 and "query" in r.text
    bad_primary = {**MULTI_FORM, "primary_domain": ["brand.io", "nope.io"]}
    r = client.post("/brands", data=bad_primary)
    assert r.status_code == 400 and "primary" in r.text


def test_edit_brand_round_trips_multiple_niches(client):
    r = client.post("/brands", data=MULTI_FORM, follow_redirects=False)
    brand_id = int(r.headers["location"].rsplit("/", 1)[-1])
    page = client.get(f"/brands/{brand_id}").text
    assert page.count('name="niche_key"') == 2
    assert 'value="crm"' in page and 'value="hosting"' in page
    three = {**MULTI_FORM, "niche_key": ["crm", "hosting", "vpn"],
             "niche_title": ["CRM", "Hosting", "VPN"],
             "primary_domain": ["brand.io", "other.org", "brand.io"],
             "queries": ["best crm", "best hosting", "best vpn"]}
    r = client.post(f"/brands/{brand_id}", data=three, follow_redirects=False)
    assert r.status_code == 303
    Session = client.app.state.Session
    with Session() as s:
        assert set(s.get(Brand, brand_id).niches) == {"crm", "hosting", "vpn"}


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


# ---------------------------------------------------------------------------
# Pre-deploy hardening: SQLite pragmas, safe deletes, global active slot,
# full error pages, snapshot niche titles
# ---------------------------------------------------------------------------

def test_sqlite_pragmas_on_every_connection(tmp_path):
    """busy_timeout, foreign_keys and WAL are set on each new DBAPI
    connection (pragmas are connection-local), and the FK pragma is
    enforced, not merely reported."""
    eng = make_engine(tmp_path / "pragmas.db")
    try:
        with eng.connect() as c1, eng.connect() as c2:  # two pool connections
            for conn in (c1, c2):
                assert conn.exec_driver_sql("PRAGMA busy_timeout").scalar() == 5000
                assert conn.exec_driver_sql("PRAGMA foreign_keys").scalar() == 1
                assert conn.exec_driver_sql("PRAGMA journal_mode").scalar() == "wal"
        with eng.begin() as conn:
            conn.exec_driver_sql("CREATE TABLE parent (id INTEGER PRIMARY KEY)")
            conn.exec_driver_sql(
                "CREATE TABLE child (id INTEGER PRIMARY KEY,"
                " parent_id INTEGER REFERENCES parent(id))")
        with pytest.raises(IntegrityError):             # orphan insert refused
            with eng.begin() as conn:
                conn.exec_driver_sql("INSERT INTO child (parent_id) VALUES (999)")
    finally:
        eng.dispose()


def test_delete_finished_brand_with_all_children_leaves_no_orphans(client):
    # non-Gemini engines: the article check must not resolve redirects over
    # the network in tests; page-level matching works the same for them.
    articles = json.dumps({"page": {"title": "A page", "url": "https://brand.io/page"}})
    brand_id = create_brand(client, articles=articles,
                            engines=["perplexity", "openai"])
    client.post(f"/brands/{brand_id}/run")
    Session = client.app.state.Session
    with Session() as s:
        engine = s.get_bind()
    with engine.connect() as conn:                      # the service's own engine
        assert conn.exec_driver_sql("PRAGMA foreign_keys").scalar() == 1
    with Session() as s:
        assert s.query(Run).count() == 1
        assert s.query(Answer).count() == 4             # 2 queries × 2 engines
        assert s.query(Hit).count() == 8                # 2 domains per answer
        assert s.query(ArticleHit).count() == 4         # every answer cites the page
    r = client.post(f"/brands/{brand_id}/delete", follow_redirects=False)
    assert r.status_code == 303
    with Session() as s:
        for model in (Brand, Run, Answer, Hit, ArticleHit):
            assert s.query(model).count() == 0, model.__tablename__


def test_brand_update_rejects_duplicate_name(client):
    create_brand(client, name="Alpha")
    beta = create_brand(client, name="Beta")
    r = client.post(f"/brands/{beta}", data={**BRAND_FORM, "name": "Alpha"})
    assert r.status_code == 400                         # not an IntegrityError crash
    assert "already exists" in r.text
    with client.app.state.Session() as s:
        assert s.get(Brand, beta).name == "Beta"        # nothing was written
    r = client.post(f"/brands/{beta}", data={**BRAND_FORM, "name": "Gamma"},
                    follow_redirects=False)
    assert r.status_code == 303                         # a free name still works


def test_brand_update_error_keeps_history_and_shows_busy(client):
    alpha = create_brand(client, name="Alpha")
    beta = create_brand(client, name="Beta")
    client.post(f"/brands/{beta}/run")
    client.post(f"/brands/{beta}/run")                  # two done runs → history + compare
    with client.app.state.Session() as s:
        s.add(Run(brand_id=alpha, status="running", active=1))
        s.commit()
    r = client.post(f"/brands/{beta}",
                    data={**BRAND_FORM, "name": "Beta", "samples": "0"})
    assert r.status_code == 400 and "samples" in r.text
    assert "const HISTORY" in r.text                    # history block survived
    assert '<select name="a">' in r.text and '<select name="b">' in r.text
    button = re.search(r"<button[^>]*>Run now</button>", r.text).group(0)
    assert "disabled" in button                         # the slot is global
    assert "(another run active)" in r.text


def test_delete_refused_while_run_active_or_queued(client):
    brand_id = create_brand(client)
    Session = client.app.state.Session
    for status, active in (("running", 1), ("pending", 1), ("queued", None)):
        with Session() as s:
            run = Run(brand_id=brand_id, status=status, active=active)
            s.add(run)
            s.commit()
            run_id = run.id
        r = client.post(f"/brands/{brand_id}/delete")
        assert r.status_code == 409, (status, r.status_code)
        assert "cannot delete a brand while its run is active or queued" in r.text
        assert "cancel the run first" in r.text
        assert "6 calls" in r.text                      # full context: estimate
        assert f'<td class="mono">{run_id}</td>' in r.text  # full context: runs
        with Session() as s:
            assert s.get(Brand, brand_id) is not None   # nothing was deleted
            assert s.get(Run, run_id) is not None
            s.delete(s.get(Run, run_id))
            s.commit()
    r = client.post(f"/brands/{brand_id}/delete", follow_redirects=False)
    assert r.status_code == 303                         # nothing waiting → delete works


def test_run_now_disabled_when_another_brand_holds_the_slot(client):
    alpha = create_brand(client, name="Alpha")
    beta = create_brand(client, name="Beta")
    Session = client.app.state.Session
    with Session() as s:
        s.add(Run(brand_id=alpha, status="running", active=1))
        s.commit()

    def run_now_button(page):
        return re.search(r"<button[^>]*>Run now</button>", page).group(0)

    page_b = client.get(f"/brands/{beta}").text
    assert "disabled" in run_now_button(page_b)         # B has no runs of its own
    assert "(another run active)" in page_b
    page_a = client.get(f"/brands/{alpha}").text
    assert "disabled" in run_now_button(page_a)         # own run → busy as before
    assert "(another run active)" not in page_a         # the slot is its own
    with Session() as s:
        run = s.query(Run).filter(Run.active == 1).first()
        run.status, run.active = "done", None
        s.commit()
    page_b = client.get(f"/brands/{beta}").text
    assert "disabled" not in run_now_button(page_b)     # slot free → enabled again
    assert "(another run active)" not in page_b


def test_history_keeps_removed_niche_title_from_snapshot(client):
    brand_id = create_brand(client)                     # niche "main", "Main niche"
    client.post(f"/brands/{brand_id}/run")              # run 1 snapshots that title
    r = client.post(f"/brands/{brand_id}",
                    data={**BRAND_FORM, "niche_title": "Renamed title"},
                    follow_redirects=False)
    assert r.status_code == 303
    client.post(f"/brands/{brand_id}/run")              # run 2 snapshots the new title
    r = client.post(f"/brands/{brand_id}",              # now remove niche "main"
                    data={**BRAND_FORM, "niche_key": "fresh",
                          "niche_title": "Fresh", "queries": "q fresh"},
                    follow_redirects=False)
    assert r.status_code == 303
    page = client.get(f"/brands/{brand_id}").text
    m = re.search(r"const HISTORY = (\{.*?\});\n", page, re.S)
    data = json.loads(m.group(1))
    assert data["main"]["title"] == "Renamed title"     # latest snapshot, not the key
    assert data["main"]["domains"]["brand.io"]["run_ids"] == [1, 2]


def test_execute_run_survives_a_deleted_run_row(tmp_path, env):
    """The run row vanishing mid-run (what an unguarded delete used to do)
    must stop the run cleanly, not crash the worker thread on None."""
    application = create_app(db_path=tmp_path / "gone.db")
    Session = application.state.Session
    with TestClient(application, base_url=LOCAL) as c:
        brand_id = create_brand(c, queries="q1\nq2\nq3", engines=["gemini"])
        run_id = runner.start_run(Session, brand_id)
        reached, proceed = threading.Event(), threading.Event()

        def blocking_ask(query, geo=""):
            if query == "q2":
                reached.set()
                proceed.wait(10)
            return fake_answer(query, geo)

        env.setitem(gt.ENGINES["gemini"], "fn", blocking_ask)
        failures = []

        def target():
            try:
                runner.execute_run(Session, run_id)
            except Exception as e:  # noqa: BLE001 — the point of the test
                failures.append(e)

        t = threading.Thread(target=target, daemon=True)
        t.start()
        assert reached.wait(10)                         # q1 saved, q2 in flight
        with Session() as s:
            s.delete(s.get(Run, run_id))                # brand stays; the run row goes
            s.commit()
        proceed.set()
        t.join(15)
        assert not t.is_alive()
        assert failures == []                           # no AttributeError from None
        with Session() as s:
            assert s.get(Brand, brand_id) is not None
            assert s.query(Run).count() == 0
            assert s.query(Answer).count() == 0         # nothing orphaned
