# -*- coding: utf-8 -*-
"""Task 4 UI: history and comparison exclude non-done runs; gates via the
route; the comparison page renders verdicts, grounded fractions and method.
"""
import json
import re

import pytest
from fastapi.testclient import TestClient

import geo_tracker as gt
import app.runner as runner
from app.db import Answer, Brand, Hit, Run
from app.main import create_app
from app.runner import brand_config

LOCAL = "http://localhost"


def miss(query, geo=""):
    return {"text": "about other.org", "urls": ["https://other.org/p"],
            "titles": ["other.org"], "model": "m", "usage": {}, "searches": 1}


def hit(query, geo=""):
    return {"text": "brand.io rocks", "urls": ["https://brand.io/p"],
            "titles": ["brand.io"], "model": "m", "usage": {}, "searches": 1}


@pytest.fixture
def env(monkeypatch):
    for e in gt.ENGINES:
        monkeypatch.setenv(gt.ENGINES[e]["env"], "test-key")
        monkeypatch.setitem(gt.ENGINES[e], "fn", hit)
    for var in ("APP_PASSWORD", "APP_ALLOWED_HOSTS", "APP_SECRET", "APP_HTTPS_ONLY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(gt, "load_env", lambda: None)
    monkeypatch.setattr(runner, "SLEEP_SEC", 0)
    return monkeypatch


@pytest.fixture
def client(tmp_path, env):
    app = create_app(db_path=tmp_path / "test.db")
    with TestClient(app, base_url=LOCAL) as c:
        yield c


def make_brand(client, queries, domains="brand.io", engines=("gemini",), name="DynBrand"):
    form = {"name": name, "domains": domains,
            "niche_key": "main", "niche_title": "Main", "primary_domain": domains.split("\n")[0],
            "queries": queries, "geo": "UZ", "engines": list(engines),
            "samples": "1", "schedule": "", "articles": "", "aliases": "",
            "model_gemini": "", "model_perplexity": "", "model_openai": ""}
    r = client.post("/brands", data=form, follow_redirects=False)
    assert r.status_code == 303, r.text
    return int(r.headers["location"].rsplit("/", 1)[-1])


def run_once(client, brand_id):
    r = client.post(f"/brands/{brand_id}/run", follow_redirects=False)
    return int(r.headers["location"].rsplit("/", 1)[-1])


def history_run_ids(page):
    m = re.search(r"const HISTORY = (\{.*?\});\n", page, re.S)
    if not m:
        return set()
    data = json.loads(m.group(1))
    ids = set()
    for ndata in data.values():
        for s in ndata["domains"].values():
            ids.update(s["run_ids"])
    return ids


def test_history_and_selectors_exclude_non_done(client):
    queries = "\n".join(f"q{i}" for i in range(3))
    bid = make_brand(client, queries)
    r1, r2 = run_once(client, bid), run_once(client, bid)  # done
    Session = client.app.state.Session
    with Session() as s:
        b = s.get(Brand, bid)
        for status in ("cancelled", "error", "queued"):
            run = Run(brand_id=bid, status=status, trigger="manual",
                      config=brand_config(b), total_calls=3, done_calls=1)
            s.add(run)
            s.commit()
            s.add(Answer(id=f"{status[:4]}row0001", run_id=run.id, engine="gemini",
                         niche="main", query="q0", status="ok", model="m",
                         n_sources=1, answer_chars=5, all_source_domains=""))
            s.commit()
            s.add(Hit(answer_id=f"{status[:4]}row0001", domain="brand.io", src=1, txt=0))
            s.commit()
    page = client.get(f"/brands/{bid}").text
    assert history_run_ids(page) == {r1, r2}  # non-done contribute no history
    # compare selectors list only done runs
    options = re.findall(r'<select name="a">(.*?)</select>', page, re.S)[0]
    opt_ids = {int(x) for x in re.findall(r'<option value="(\d+)"', options)}
    assert opt_ids == {r1, r2}


def test_compare_rejects_non_done(client):
    bid = make_brand(client, "q0\nq1")
    r1 = run_once(client, bid)
    Session = client.app.state.Session
    with Session() as s:
        b = s.get(Brand, bid)
        run = Run(brand_id=bid, status="cancelled", trigger="manual",
                  config=brand_config(b))
        s.add(run)
        s.commit()
        cid = run.id
    page = client.get(f"/brands/{bid}/compare?a={cid}&b={r1}").text
    assert "only done runs can be compared" in page


def test_compare_rejects_same_and_foreign(client):
    bid = make_brand(client, "q0\nq1")
    r1 = run_once(client, bid)
    assert "pick two different runs" in client.get(f"/brands/{bid}/compare?a={r1}&b={r1}").text
    other = make_brand(client, "q0\nq1", name="OtherBrand")
    ro = run_once(client, other)
    assert "run not found in this brand" in client.get(f"/brands/{bid}/compare?a={ro}&b={r1}").text


def test_compare_page_renders_significant_and_method(client, env):
    queries = "\n".join(f"q{i}" for i in range(30))
    bid = make_brand(client, queries)
    env.setitem(gt.ENGINES["gemini"], "fn", miss)
    ra = run_once(client, bid)               # all miss → share 0
    env.setitem(gt.ENGINES["gemini"], "fn", hit)
    rb = run_once(client, bid)               # all hit → share 100
    page = client.get(f"/brands/{bid}/compare?a={ra}&b={rb}").text
    assert "significant (up)" in page
    assert "&lt;0.001" in page and ">0.000<" not in page   # a tiny p is not shown as zero
    assert "Holm correction" in page
    assert "Grounded fraction" in page
    assert "Sensitivity" in page
    assert "mean over common queries" in page
    # family = niche row + overall row
    assert "family of 2 pairs" in page


def test_compare_page_insufficient_below_floor(client):
    bid = make_brand(client, "q0\nq1")  # 2 common queries < 30
    ra, rb = run_once(client, bid), run_once(client, bid)
    page = client.get(f"/brands/{bid}/compare?a={ra}&b={rb}").text
    assert "insufficient queries" in page


# ---------------------------------------------------------------------------
# History: breaks in comparability are marked, figures of done runs are cached
# ---------------------------------------------------------------------------

def history_data(page):
    m = re.search(r"const HISTORY = (\{.*?\});\n", page, re.S)
    return json.loads(m.group(1))


def edit_brand(client, brand_id, **changes):
    form = {"name": "DynBrand", "domains": "brand.io", "niche_key": "main",
            "niche_title": "Main", "primary_domain": "brand.io", "queries": "q1\nq2",
            "geo": "UZ", "engines": ["gemini"], "samples": "1", "schedule": "",
            "articles": "", "aliases": "", "model_gemini": "", "model_perplexity": "",
            "model_openai": "", **changes}
    r = client.post(f"/brands/{brand_id}", data=form, follow_redirects=False)
    assert r.status_code == 303, r.text


def test_history_marks_runs_not_comparable_with_the_previous_one(client):
    brand_id = make_brand(client, "q1\nq2")
    run_once(client, brand_id)                                   # run 1
    run_once(client, brand_id)                                   # run 2: same basis
    edit_brand(client, brand_id, aliases='{"brand.io": ["Brand*"]}')
    run_once(client, brand_id)                                   # run 3: aliases changed
    edit_brand(client, brand_id, aliases='{"brand.io": ["Brand*"]}', geo="")
    run_once(client, brand_id)                                   # run 4: geo changed

    page = client.get(f"/brands/{brand_id}").text
    series = history_data(page)["main"]["domains"]["brand.io"]
    assert series["run_ids"] == [1, 2, 3, 4]
    assert series["breaks"] == ["", "", "aliases changed", "geo UZ → —"]
    assert page.count("not comparable</span> aliases changed") == 1
    assert "not comparable</span> geo UZ → —" in page
    assert "breaks[c.p1DataIndex]" in page                       # the chart dashes that segment


def test_history_figures_of_done_runs_are_computed_once(client, env):
    from app import dynamics
    brand_id = make_brand(client, "q1\nq2")
    run_once(client, brand_id)
    run_once(client, brand_id)
    calls = []
    real = dynamics.share_ci_rows
    env.setattr(dynamics, "share_ci_rows", lambda *a, **k: calls.append(1) or real(*a, **k))

    first = client.get(f"/brands/{brand_id}").text
    assert len(calls) == 2                                       # one per done run
    again = client.get(f"/brands/{brand_id}").text
    assert len(calls) == 2                                       # served from the cache
    assert history_data(first) == history_data(again)

    run_once(client, brand_id)                                   # a new run is computed, once
    client.get(f"/brands/{brand_id}")
    assert len(calls) == 3


def test_queued_run_blocks_run_now(client):
    brand_id = make_brand(client, "q1\nq2")
    assert "disabled" not in re.search(r"<button[^>]*>Run now</button>", client.get(f"/brands/{brand_id}").text).group(0)
    with client.app.state.Session() as s:
        s.add(Run(brand_id=brand_id, status="queued", trigger="scheduled"))
        s.commit()
    assert "disabled" in re.search(r"<button[^>]*>Run now</button>", client.get(f"/brands/{brand_id}").text).group(0)
