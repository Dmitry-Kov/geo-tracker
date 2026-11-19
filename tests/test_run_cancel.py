# -*- coding: utf-8 -*-
"""Run estimate, >100-call confirmation and between-call cancellation.

Engines are mocked; the cancel-between-calls test drives execute_run in a
thread with an engine that blocks until the cancel flag lands in the DB.
"""
import io
import threading
import time

import pytest
from fastapi.testclient import TestClient

import geo_tracker as gt
import app.runner as runner
import app.scheduler as scheduler
from app.db import Answer, Brand, Run
from app.main import create_app
from app.runner import execute_run, run_estimate, start_run

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


# ---------------------------------------------------------------------------
# Estimate
# ---------------------------------------------------------------------------

def test_estimate_counts_queries_engines_samples(client):
    brand_id = create_brand(client, queries="q1\nq2\nq3", samples="2")
    Session = client.app.state.Session
    with Session() as s:
        est = run_estimate(s.get(Brand, brand_id))
    assert est["calls"] == 3 * 3 * 2  # queries × engines × samples
    assert est["minutes"] == 5        # ceil(18 × 15s / 60)
    page = client.get(f"/brands/{brand_id}").text
    assert "18 calls" in page and "~5 min" in page


def test_estimate_skips_engines_without_keys(client, env):
    env.delenv("PERPLEXITY_API_KEY")
    env.delenv("OPENAI_API_KEY")
    brand_id = create_brand(client)  # 2 queries, 3 engines picked, 1 sample
    Session = client.app.state.Session
    with Session() as s:
        est = run_estimate(s.get(Brand, brand_id))
    assert est["engines"] == ["gemini"]
    assert est["calls"] == 2
    assert "2 calls" in client.get(f"/brands/{brand_id}").text


# ---------------------------------------------------------------------------
# >100 calls: confirmation with the number, server-side gate
# ---------------------------------------------------------------------------

def test_big_run_requires_confirmation(client):
    queries = "\n".join(f"q{i}" for i in range(60))
    brand_id = create_brand(client, queries=queries,
                            engines=["gemini", "perplexity"])  # 60×2 = 120
    page = client.get(f"/brands/{brand_id}").text
    assert "120 calls" in page and "confirm(" in page

    r = client.post(f"/brands/{brand_id}/run", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == f"/brands/{brand_id}"  # bounced, not started
    Session = client.app.state.Session
    with Session() as s:
        assert s.query(Run).count() == 0

    r = client.post(f"/brands/{brand_id}/run", data={"big_confirmed": "1"},
                    follow_redirects=False)
    assert r.headers["location"].startswith("/runs/")
    with Session() as s:
        assert s.get(Run, 1).status == "done"  # mocked engines are instant


def test_small_run_needs_no_confirmation(client):
    brand_id = create_brand(client)  # 2×3 = 6 calls
    assert "This run will make" not in client.get(f"/brands/{brand_id}").text
    r = client.post(f"/brands/{brand_id}/run", follow_redirects=False)
    assert r.headers["location"].startswith("/runs/")


# ---------------------------------------------------------------------------
# Cancellation
# ---------------------------------------------------------------------------

def test_cancel_between_calls_keeps_answers_and_frees_slot(tmp_path, env):
    app = create_app(db_path=tmp_path / "cancel.db")
    Session = app.state.Session
    with TestClient(app, base_url=LOCAL) as c:
        brand_id = create_brand(c, queries="q1\nq2\nq3", engines=["gemini"])
        run_id = start_run(Session, brand_id)

        reached = threading.Event()

        def blocking_ask(query, geo=""):
            if query == "q2":
                reached.set()
                for _ in range(500):  # block until the cancel flag lands
                    with Session() as s:
                        if s.get(Run, run_id).cancel_requested:
                            break
                    time.sleep(0.02)
            return fake_answer(query, geo)

        env.setitem(gt.ENGINES["gemini"], "fn", blocking_ask)
        t = threading.Thread(target=execute_run, args=(Session, run_id),
                             daemon=True)
        t.start()
        assert reached.wait(10)  # q1 done, q2 in flight

        r = c.post(f"/runs/{run_id}/cancel", follow_redirects=False)
        assert r.status_code == 303
        t.join(15)
        assert not t.is_alive()

        with Session() as s:
            run = s.get(Run, run_id)
            assert run.status == "cancelled"
            assert run.active is None                 # slot freed
            assert run.finished_at is not None
            assert run.done_calls == 2                # q1 + the in-flight q2
            answers = s.query(Answer).filter(Answer.run_id == run_id).all()
            assert {a.query for a in answers} == {"q1", "q2"}  # kept, not lost
        assert start_run(Session, brand_id) is not None  # next run can start


def test_cancel_pending_queued_run_makes_no_calls(client):
    brand_id = create_brand(client)
    Session = client.app.state.Session
    run_id = start_run(Session, brand_id, trigger="scheduled")
    r = client.post(f"/runs/{run_id}/cancel", follow_redirects=False)
    assert r.status_code == 303
    execute_run(Session, run_id)  # what the queue worker calls when it dequeues
    with Session() as s:
        run = s.get(Run, run_id)
        assert run.status == "cancelled"
        assert run.active is None
        assert run.done_calls == 0 and run.total_calls == 0
        assert s.query(Answer).count() == 0
    assert start_run(Session, brand_id) is not None


def test_cancel_finished_run_is_noop(client):
    brand_id = create_brand(client)
    client.post(f"/brands/{brand_id}/run")  # completes instantly (mocked)
    r = client.post("/runs/1/cancel", follow_redirects=False)
    assert r.status_code == 303
    Session = client.app.state.Session
    with Session() as s:
        run = s.get(Run, 1)
        assert run.status == "done" and run.cancel_requested == 0


def test_cancel_unknown_run_is_404(client):
    assert client.post("/runs/999/cancel").status_code == 404


def test_run_page_shows_cancel_only_while_active(client):
    brand_id = create_brand(client)
    Session = client.app.state.Session
    run_id = start_run(Session, brand_id)  # pending, not executed
    page = client.get(f"/runs/{run_id}").text
    assert f"/runs/{run_id}/cancel" in page
    execute_run(Session, run_id)
    page = client.get(f"/runs/{run_id}").text
    assert f"/runs/{run_id}/cancel" not in page


# ---------------------------------------------------------------------------
# After the click: the page says the cancel was received
# ---------------------------------------------------------------------------

def test_page_shows_cancelling_until_the_call_in_flight_ends(client):
    brand_id = create_brand(client)
    Session = client.app.state.Session
    with Session() as s:
        s.add(Run(brand_id=brand_id, status="running", active=1, total_calls=6))
        s.commit()
    assert 'action="/runs/1/cancel"' in client.get("/runs/1").text

    client.post("/runs/1/cancel")                       # the call in flight is still running
    page = client.get("/runs/1").text
    assert "cancelling" in page and "Cancel requested" in page
    assert 'action="/runs/1/cancel"' not in page        # nothing left to click
    assert "cancelling" in client.get("/runs/1/progress").text
    assert 'hx-get="/runs/1/progress"' in page          # and it keeps polling


# ---------------------------------------------------------------------------
# A run that is not done never reads like a full report
# ---------------------------------------------------------------------------

def cancelled_run(client):
    """A run stopped after 2 of its 6 calls, as cancellation leaves it."""
    brand_id = create_brand(client)
    Session = client.app.state.Session
    run_id = start_run(Session, brand_id)
    calls = []

    def ask_then_cancel(query, geo=""):
        calls.append(query)
        if len(calls) == 2:
            with Session() as s:
                s.get(Run, run_id).cancel_requested = 1
                s.commit()
        return fake_answer(query, geo)

    for e in gt.ENGINES:
        gt.ENGINES[e]["fn"] = ask_then_cancel           # restored by the env fixture
    execute_run(Session, run_id)
    return run_id


def test_exports_of_a_cancelled_run_say_so(client):
    run_id = cancelled_run(client)
    with client.app.state.Session() as s:
        run = s.get(Run, run_id)
        assert (run.status, run.done_calls, run.total_calls) == ("cancelled", 2, 6)

    page = client.get(f"/runs/{run_id}").text
    assert "Incomplete run — cancelled: 2 of 6 planned calls" in page

    dash = client.get(f"/runs/{run_id}/report.html")
    assert "Incomplete run \u2014 cancelled: 2 of 6 planned calls" in dash.text \
        or "Incomplete run — cancelled: 2 of 6 planned calls" in dash.text
    assert f"_{run_id}_cancelled.html" in dash.headers["content-disposition"]

    docx = pytest.importorskip("docx")
    for lang, text in (("en", "Incomplete run — cancelled: 2 of 6 planned calls"),
                       ("ru", "Неполный прогон — отменён: выполнено 2 из 6")):
        r = client.get(f"/runs/{run_id}/report.docx?lang={lang}")
        assert r.status_code == 200
        assert "_cancelled.docx" in r.headers["content-disposition"]
        paragraphs = [p.text for p in docx.Document(io.BytesIO(r.content)).paragraphs]
        assert any(text in p for p in paragraphs), lang
        assert next(i for i, p in enumerate(paragraphs) if text in p) <= 3  # before any figure


def test_exports_of_a_completed_run_carry_no_notice(client):
    brand_id = create_brand(client)
    client.post(f"/brands/{brand_id}/run")
    assert "Incomplete run" not in client.get("/runs/1").text
    dash = client.get("/runs/1/report.html")
    assert '"notice": ""' in dash.text
    assert "geo_dashboard_TestBrand_1.html" in dash.headers["content-disposition"]
    docx = pytest.importorskip("docx")
    r = client.get("/runs/1/report.docx?lang=en")
    text = "\n".join(p.text for p in docx.Document(io.BytesIO(r.content)).paragraphs)
    assert "Incomplete run" not in text


def test_interrupted_run_is_marked_too(client):
    brand_id = create_brand(client)
    with client.app.state.Session() as s:
        s.add(Run(brand_id=brand_id, status="error", total_calls=6, done_calls=1,
                  error=runner.INTERRUPTED))
        s.commit()
    assert "Incomplete run — error: 1 of 6 planned calls" in client.get("/runs/1").text


# ---------------------------------------------------------------------------
# A scheduled tick that has to wait is a visible run, and can be stopped
# ---------------------------------------------------------------------------

def waiting_tick(client, env):
    """A scheduled brand whose tick is waiting behind an active manual run."""
    env.setattr(scheduler, "POLL_SEC", 0.01)
    brand_id = create_brand(client, schedule="0 9 * * 1")
    Session = client.app.state.Session
    with Session() as s:
        s.add(Run(brand_id=brand_id, status="running", active=1))   # run 1: the manual one
        s.commit()
    calls = []
    for e in gt.ENGINES:
        env.setitem(gt.ENGINES[e], "fn", lambda q, geo="": calls.append(q) or fake_answer(q, geo))
    job = client.app.state.scheduler.get_job(f"brand-{brand_id}")
    t = threading.Thread(target=job.func, args=(brand_id,), daemon=True)
    t.start()
    for _ in range(200):                                            # until the queued row exists
        with Session() as s:
            if s.get(Run, 2) is not None:
                break
        time.sleep(0.01)
    return brand_id, t, calls


def test_waiting_scheduled_tick_is_listed_and_cancellable(client, env):
    brand_id, t, calls = waiting_tick(client, env)
    assert "queued" in client.get(f"/brands/{brand_id}").text       # visible on the brand
    page = client.get("/runs/2").text
    assert "queued" in page and 'action="/runs/2/cancel"' in page

    assert client.post("/runs/2/cancel", follow_redirects=False).status_code == 303
    t.join(10)
    assert not t.is_alive()
    with client.app.state.Session() as s:
        run = s.get(Run, 2)
        assert run.status == "cancelled" and run.active is None and run.finished_at
        assert s.get(Run, 1).status == "running"                    # the manual run is untouched
    assert calls == []                                              # it never started


def test_removing_the_schedule_stops_a_waiting_tick(client, env):
    brand_id, t, calls = waiting_tick(client, env)
    r = client.post(f"/brands/{brand_id}", data={**BRAND_FORM, "schedule": ""},
                    follow_redirects=False)
    assert r.status_code == 303
    t.join(10)
    assert not t.is_alive()
    with client.app.state.Session() as s:
        run = s.get(Run, 2)
        assert run.status == "cancelled" and "schedule was removed" in run.error
    assert calls == []


def test_tick_waiting_behind_another_scheduled_run_is_visible(client, env):
    env.setattr(scheduler, "POLL_SEC", 0.01)
    ids = [create_brand(client, name=n, schedule="0 9 * * 1") for n in ("A", "B")]
    Session = client.app.state.Session
    release = threading.Event()

    def held(query, geo=""):
        release.wait(10)                                            # A's run holds the queue
        return fake_answer(query, geo)

    for e in gt.ENGINES:
        env.setitem(gt.ENGINES[e], "fn", held)
    sched = client.app.state.scheduler
    ta = threading.Thread(target=sched.get_job(f"brand-{ids[0]}").func, args=(ids[0],), daemon=True)
    ta.start()
    for _ in range(200):
        with Session() as s:
            if s.query(Run).filter(Run.status == "running").count():
                break
        time.sleep(0.01)
    tb = threading.Thread(target=sched.get_job(f"brand-{ids[1]}").func, args=(ids[1],), daemon=True)
    tb.start()
    for _ in range(200):
        with Session() as s:
            queued = s.query(Run).filter(Run.brand_id == ids[1]).all()
            if queued:
                break
        time.sleep(0.01)
    assert [r.status for r in queued] == ["queued"]                 # B waits in plain sight
    release.set()
    ta.join(20)
    tb.join(20)
    with Session() as s:
        assert [r.status for r in s.query(Run).order_by(Run.id)] == ["done", "done"]


def test_restart_closes_a_queued_run(tmp_path, env):
    db = tmp_path / "queued.db"
    with TestClient(create_app(db_path=db), base_url=LOCAL) as c:
        brand_id = create_brand(c)
        with c.app.state.Session() as s:
            s.add(Run(brand_id=brand_id, status="queued", trigger="scheduled"))
            s.commit()
    with TestClient(create_app(db_path=db), base_url=LOCAL) as c:   # restart
        with c.app.state.Session() as s:
            run = s.get(Run, 1)
            assert run.status == "error" and "interrupted" in run.error
