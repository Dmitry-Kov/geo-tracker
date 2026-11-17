# -*- coding: utf-8 -*-
"""Run estimate, >100-call confirmation and between-call cancellation.

Engines are mocked; the cancel-between-calls test drives execute_run in a
thread with an engine that blocks until the cancel flag lands in the DB.
"""
import threading
import time

import pytest
from fastapi.testclient import TestClient

import geo_tracker as gt
import app.runner as runner
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
