# -*- coding: utf-8 -*-
"""Read-only client share links (/share/{token}).

The token is the only key: no login, strictly one brand, internal API
usage/cost hidden, no-referrer + noindex on every shared response. Admin
pages keep their login; the admin share management keeps auth + CSRF guard.
"""
import io
import json
import re

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError

import geo_tracker as gt
import app.runner as runner
from app.db import Answer, Brand, Hit, Run
from app.main import create_app
from app.runner import brand_config

LOCAL = "http://localhost"

BRAND_FORM = {
    "name": "ShareBrand", "domains": "brand.io\nother.org",
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
    for var in ("APP_ALLOWED_HOSTS", "APP_SECRET", "APP_HTTPS_ONLY",
                "REPORT_BRAND", "REPORT_ACCENT"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("APP_PASSWORD", "secret")  # share links must work WITH login on
    monkeypatch.setattr(gt, "load_env", lambda: None)
    monkeypatch.setattr(runner, "SLEEP_SEC", 0)
    return monkeypatch


@pytest.fixture
def admin(tmp_path, env):
    application = create_app(db_path=tmp_path / "share.db")
    with TestClient(application, base_url=LOCAL) as c:
        r = c.post("/login", data={"password": "secret"}, follow_redirects=False)
        assert r.status_code == 303
        yield c


@pytest.fixture
def anon(admin):
    # A second client on the SAME app, without the session cookie. No context
    # manager: the lifespan (scheduler start, migrations) already ran once.
    return TestClient(admin.app, base_url=LOCAL)


def create_brand(client, **overrides):
    r = client.post("/brands", data={**BRAND_FORM, **overrides},
                    follow_redirects=False)
    assert r.status_code == 303, r.text
    return int(r.headers["location"].rsplit("/", 1)[-1])


def run_once(client, brand_id):
    r = client.post(f"/brands/{brand_id}/run", follow_redirects=False)
    assert r.status_code == 303, r.text
    return int(r.headers["location"].rsplit("/", 1)[-1])


def enable_share(client, brand_id):
    r = client.post(f"/brands/{brand_id}/share", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == f"/brands/{brand_id}"
    with client.app.state.Session() as s:
        token = s.get(Brand, brand_id).share_token
    assert token
    return token


def assert_share_headers(r):
    assert r.headers["referrer-policy"] == "no-referrer"
    assert r.headers["x-robots-tag"] == "noindex, nofollow"


def assert_no_admin_chrome(page):
    assert "/settings" not in page
    assert "/brands/" not in page
    assert 'href="/"' not in page
    assert "Logout" not in page
    assert "Run now" not in page


def history_data(page):
    m = re.search(r"const HISTORY = (\{.*?\});\n", page, re.S)
    return json.loads(m.group(1))


# ---------------------------------------------------------------------------
# Admin management: enable, rotate, revoke
# ---------------------------------------------------------------------------

def test_share_lifecycle_enable_rotate_revoke(admin, anon):
    brand_id = create_brand(admin)
    page = admin.get(f"/brands/{brand_id}").text
    assert "Client share link" in page and "Enable share link" in page

    token = enable_share(admin, brand_id)
    assert len(token) == 32 and re.fullmatch(r"[A-Za-z0-9_-]+", token)  # 192 bits
    page = admin.get(f"/brands/{brand_id}").text
    assert f"{LOCAL}/share/{token}" in page          # full public URL, readonly
    assert "readonly" in page
    assert f'href="/share/{token}"' in page          # Open
    assert "Rotate" in page and "Revoke" in page
    assert anon.get(f"/share/{token}").status_code == 200

    rotated = enable_share(admin, brand_id)          # rotate
    assert rotated != token
    assert anon.get(f"/share/{token}").status_code == 404   # old link dead
    assert anon.get(f"/share/{rotated}").status_code == 200

    r = admin.post(f"/brands/{brand_id}/share/revoke", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == f"/brands/{brand_id}"
    with admin.app.state.Session() as s:
        assert s.get(Brand, brand_id).share_token is None
    assert anon.get(f"/share/{rotated}").status_code == 404
    assert "Enable share link" in admin.get(f"/brands/{brand_id}").text


def test_share_management_of_missing_brand_is_404(admin):
    assert admin.post("/brands/999/share").status_code == 404
    assert admin.post("/brands/999/share/revoke").status_code == 404


def test_unknown_token_is_404_with_share_headers(anon):
    r = anon.get("/share/no-such-token")
    assert r.status_code == 404
    assert_share_headers(r)


def test_share_token_is_unique_in_db(admin):
    a = create_brand(admin, name="Alpha")
    b = create_brand(admin, name="Beta")
    token = enable_share(admin, a)
    with admin.app.state.Session() as s:
        s.get(Brand, b).share_token = token
        with pytest.raises(IntegrityError):
            s.commit()


# ---------------------------------------------------------------------------
# Public read-only access: every shared page, with the security headers
# ---------------------------------------------------------------------------

def test_shared_pages_are_public_read_only_and_headed(admin, anon):
    pytest.importorskip("docx")
    brand_id = create_brand(admin)
    r1, r2 = run_once(admin, brand_id), run_once(admin, brand_id)
    token = enable_share(admin, brand_id)
    with admin.app.state.Session() as s:
        aid = s.query(Answer).filter(Answer.run_id == r1).first().id

    page = anon.get(f"/share/{token}")
    assert page.status_code == 200
    assert_share_headers(page)
    assert "ShareBrand" in page.text and "brand.io" in page.text
    assert "geo=UZ" in page.text
    assert f'href="/share/{token}/runs/{r1}"' in page.text
    assert f'action="/share/{token}/compare"' in page.text   # >= 2 done runs
    assert "const HISTORY" in page.text
    assert_no_admin_chrome(page.text)
    assert 'name="schedule"' not in page.text                # no edit form
    assert "Delete" not in page.text

    run_page = anon.get(f"/share/{token}/runs/{r1}")
    assert run_page.status_code == 200
    assert_share_headers(run_page)
    assert "Measurement uncertainty" in run_page.text
    assert "brand.io rocks" not in run_page.text             # receipt is drill-down
    assert f'href="/share/{token}"' in run_page.text         # brand link
    assert f'href="/share/{token}/runs/{r1}/report.html"' in run_page.text
    assert f'href="/share/{token}/runs/{r1}/report.docx?lang=ru"' in run_page.text
    assert f'hx-get="/share/{token}/answers/{aid}"' in run_page.text
    assert_no_admin_chrome(run_page.text)
    assert "Cancel" not in run_page.text
    assert 'hx-get="/runs/' not in run_page.text             # no admin polling

    receipt = anon.get(f"/share/{token}/answers/{aid}")
    assert receipt.status_code == 200
    assert_share_headers(receipt)
    assert "brand.io rocks" in receipt.text                  # answer text kept
    assert "fake-model" in receipt.text                      # provenance kept
    assert "https://brand.io/page" in receipt.text           # sources kept

    compare = anon.get(f"/share/{token}/compare?a={r1}&b={r2}")
    assert compare.status_code == 200
    assert_share_headers(compare)
    assert f'action="/share/{token}/compare"' in compare.text
    assert f'href="/share/{token}"' in compare.text          # Back link
    assert "Holm correction" in compare.text
    assert_no_admin_chrome(compare.text)

    html = anon.get(f"/share/{token}/runs/{r1}/report.html")
    assert html.status_code == 200
    assert_share_headers(html)
    assert "brand.io" in html.text and "ShareBrand" in html.text

    docx = pytest.importorskip("docx")
    r = anon.get(f"/share/{token}/runs/{r1}/report.docx?lang=ru")
    assert r.status_code == 200
    assert_share_headers(r)
    text = "\n".join(p.text for p in docx.Document(io.BytesIO(r.content)).paragraphs)
    assert "brand.io" in text and "Неопределённость измерения" in text


def test_shared_history_and_compare_exclude_non_done(admin, anon):
    brand_id = create_brand(admin, engines=["gemini"])
    r1, r2 = run_once(admin, brand_id), run_once(admin, brand_id)
    with admin.app.state.Session() as s:
        b = s.get(Brand, brand_id)
        run = Run(brand_id=brand_id, status="cancelled", trigger="manual",
                  config=brand_config(b), total_calls=2, done_calls=1)
        s.add(run)
        s.commit()
        cid = run.id
    token = enable_share(admin, brand_id)
    page = anon.get(f"/share/{token}").text
    assert f'href="/share/{token}/runs/{cid}"' not in page   # client list: done only
    data = history_data(page)
    ids = {i for nd in data.values() for s in nd["domains"].values()
           for i in s["run_ids"]}
    assert ids == {r1, r2}
    options = re.findall(r'<select name="a">(.*?)</select>', page, re.S)[0]
    assert {int(x) for x in re.findall(r'<option value="(\d+)"', options)} == {r1, r2}


# ---------------------------------------------------------------------------
# The rest of the service stays behind the login
# ---------------------------------------------------------------------------

def test_admin_pages_still_require_login(admin, anon):
    brand_id = create_brand(admin)
    run_id = run_once(admin, brand_id)
    with admin.app.state.Session() as s:
        aid = s.query(Answer).first().id
    for path in ("/", f"/brands/{brand_id}", "/settings", f"/runs/{run_id}",
                 f"/answers/{aid}", f"/runs/{run_id}/report.html",
                 f"/runs/{run_id}/report.docx?lang=en",
                 f"/brands/{brand_id}/compare"):
        r = anon.get(path, follow_redirects=False)
        assert r.status_code == 307 and r.headers["location"] == "/login", path
    for path in (f"/brands/{brand_id}/run", f"/brands/{brand_id}/share",
                 f"/brands/{brand_id}/share/revoke", f"/runs/{run_id}/cancel",
                 f"/brands/{brand_id}/delete"):
        r = anon.post(path, follow_redirects=False)
        assert r.status_code == 307 and r.headers["location"] == "/login", path
    with admin.app.state.Session() as s:
        assert s.get(Brand, brand_id).share_token is None    # nothing was enabled


# ---------------------------------------------------------------------------
# Cross-brand isolation: a token opens exactly one brand
# ---------------------------------------------------------------------------

def test_cross_brand_isolation(admin, anon):
    pytest.importorskip("docx")
    a = create_brand(admin, name="Alpha")
    b = create_brand(admin, name="Beta")
    ra = run_once(admin, a)
    rb1, rb2 = run_once(admin, b), run_once(admin, b)
    token_a = enable_share(admin, a)
    with admin.app.state.Session() as s:
        aid_b = s.query(Answer).filter(Answer.run_id == rb1).first().id

    assert anon.get(f"/share/{token_a}/runs/{rb1}").status_code == 404
    assert anon.get(f"/share/{token_a}/answers/{aid_b}").status_code == 404
    assert anon.get(f"/share/{token_a}/runs/{rb1}/report.html").status_code == 404
    assert anon.get(f"/share/{token_a}/runs/{rb1}/report.docx?lang=ru").status_code == 404
    page = anon.get(f"/share/{token_a}/compare?a={rb1}&b={rb2}")
    assert page.status_code == 200
    assert "run not found in this brand" in page.text
    assert "Beta" not in page.text                           # not even the name
    # own brand still works
    assert anon.get(f"/share/{token_a}/runs/{ra}").status_code == 200


# ---------------------------------------------------------------------------
# Internal COGS: hidden on share pages, still shown to the admin
# ---------------------------------------------------------------------------

def test_internal_usage_hidden_on_share_but_shown_to_admin(admin, anon):
    brand_id = create_brand(admin)
    run_id = run_once(admin, brand_id)
    token = enable_share(admin, brand_id)
    with admin.app.state.Session() as s:
        aid = s.query(Answer).first().id

    admin_run = admin.get(f"/runs/{run_id}").text
    assert "Resource usage" in admin_run and "Cost (API-reported)" in admin_run
    admin_receipt = admin.get(f"/answers/{aid}").text
    assert "Usage (raw API report)" in admin_receipt

    shared_run = anon.get(f"/share/{token}/runs/{run_id}").text
    assert "Resource usage" not in shared_run
    assert "Cost (API-reported)" not in shared_run
    assert "Usage (raw API report)" not in shared_run
    shared_receipt = anon.get(f"/share/{token}/answers/{aid}").text
    assert "Usage (raw API report)" not in shared_receipt
    assert "total_tokens" not in shared_receipt
    assert "brand.io rocks" in shared_receipt                # the evidence stays


# ---------------------------------------------------------------------------
# Incomplete-run guarantee holds on the shared pages and exports too
# ---------------------------------------------------------------------------

def test_shared_cancelled_run_says_incomplete_everywhere(admin, anon, env):
    docx = pytest.importorskip("docx")
    brand_id = create_brand(admin, engines=["gemini"])
    Session = admin.app.state.Session
    run_id = runner.start_run(Session, brand_id)
    calls = []

    def ask_then_cancel(query, geo=""):
        calls.append(query)
        if len(calls) == 1:
            with Session() as s:
                s.get(Run, run_id).cancel_requested = 1
                s.commit()
        return fake_answer(query, geo)

    env.setitem(gt.ENGINES["gemini"], "fn", ask_then_cancel)
    runner.execute_run(Session, run_id)
    token = enable_share(admin, brand_id)

    page = anon.get(f"/share/{token}/runs/{run_id}")
    assert page.status_code == 200
    assert "Incomplete run — cancelled: 1 of 2 planned calls" in page.text

    html = anon.get(f"/share/{token}/runs/{run_id}/report.html")
    assert html.status_code == 200
    assert "Incomplete run" in html.text
    assert "_cancelled" in html.headers["content-disposition"]

    r = anon.get(f"/share/{token}/runs/{run_id}/report.docx?lang=en")
    assert r.status_code == 200
    assert "_cancelled" in r.headers["content-disposition"]
    text = "\n".join(p.text for p in docx.Document(io.BytesIO(r.content)).paragraphs)
    assert "Incomplete run — cancelled" in text

    # the cancelled run is not listed as a completed one on the share home
    home = anon.get(f"/share/{token}").text
    assert f'href="/share/{token}/runs/{run_id}"' not in home


# ---------------------------------------------------------------------------
# White-label header on the shared views
# ---------------------------------------------------------------------------

def test_shared_header_uses_report_brand(admin, anon, env):
    brand_id = create_brand(admin)
    run_id = run_once(admin, brand_id)
    token = enable_share(admin, brand_id)

    page = anon.get(f"/share/{token}").text                  # default logo
    assert 'geo<span>.</span>tracker' in page
    assert f'<a class="logo" href="/share/{token}">' in page

    env.setenv("REPORT_BRAND", "agency.io")
    for path in (f"/share/{token}", f"/share/{token}/runs/{run_id}",
                 f"/share/{token}/compare"):
        page = anon.get(path).text
        assert "agency.io" in page
        assert f'<a class="logo" href="/share/{token}">agency.io</a>' in page
        assert_no_admin_chrome(page)