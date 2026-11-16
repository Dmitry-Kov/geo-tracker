# -*- coding: utf-8 -*-
"""Provenance, schema guard, geo handling, sampling and report end-to-end.

The `queries` config is faked in conftest.py; RESULTS_CSV / ANSWERS_JSONL /
DASHBOARD_HTML are monkeypatched to tmp_path, so nothing touches real data.
Engine HTTP calls are mocked at _post_with_retries / ENGINES["fn"] — no keys,
no network, no quota spent.
"""
import argparse
import csv
import json

import pytest

import geo_tracker as gt

ANSWER = {"text": "example.com is great", "urls": ["https://example.com/a"],
          "titles": ["example.com"], "model": "gem-test"}


@pytest.fixture
def paths(tmp_path, monkeypatch):
    monkeypatch.setattr(gt, "RESULTS_CSV", tmp_path / "results.csv")
    monkeypatch.setattr(gt, "ANSWERS_JSONL", tmp_path / "answers.jsonl")
    monkeypatch.setattr(gt, "DASHBOARD_HTML", tmp_path / "dashboard.html")
    return tmp_path


def run_args(**overrides):
    args = dict(engines="gemini", niches=None, limit=None, sleep=0,
                resume=False, samples=1, geo="")
    args.update(overrides)
    return argparse.Namespace(**args)


@pytest.fixture
def gemini_env(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.delenv("PERPLEXITY_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)


# ---------------------------------------------------------------------------
# Matching is untouched by the audit-grade work
# ---------------------------------------------------------------------------

def test_domain_in_url_exact_host():
    assert gt.domain_in_url("example.com", "https://example.com/x")
    assert gt.domain_in_url("example.com", "https://www.example.com/x")
    assert gt.domain_in_url("example.com", "https://sub.example.com/x")
    assert gt.domain_in_url("example.com", "https://example.com:8080/x")
    assert gt.domain_in_url("example.com", "example.com/x")
    assert not gt.domain_in_url("example.com", "https://1example.com")
    assert not gt.domain_in_url("example.com", "https://example.com.evil.ru")


def test_domain_in_text_word_boundaries():
    assert gt.domain_in_text("example.com", "see example.com for details")
    assert gt.domain_in_text("example.com", "EXAMPLE.COM")
    assert not gt.domain_in_text("example.com", "1example.com")
    assert not gt.domain_in_text("example.com", "example.commerce")


def test_match_domains_src_txt_and_aliases(monkeypatch):
    monkeypatch.setattr(gt, "DOMAINS", ["example.com", "test.org"])
    monkeypatch.setattr(gt, "BRAND_ALIASES", {"example.com": ["Example Brand"]})
    hits = gt.match_domains("I recommend Example Brand", ["https://test.org/a"],
                            ["test.org"])
    assert hits["example.com"] == {"src": 0, "txt": 1}  # alias → txt only
    assert hits["test.org"] == {"src": 1, "txt": 0}


# ---------------------------------------------------------------------------
# Provenance: schema, make_row, receipts, roundtrip
# ---------------------------------------------------------------------------

def test_csv_fieldnames_provenance_columns():
    fields = gt.csv_fieldnames()
    i = fields.index("query")
    assert fields[i + 1:i + 5] == ["model", "geo", "geo_applied", "sample"]
    assert fields[i + 5] == "status"


def test_schema_guard_rejects_old_header(paths):
    fields = [c for c in gt.csv_fieldnames()
              if c not in ("model", "geo", "geo_applied", "sample")]
    gt.RESULTS_CSV.write_text(",".join(fields) + "\n", encoding="utf-8-sig")
    with pytest.raises(SystemExit, match="missing"):
        gt.check_results_schema()


def test_schema_guard_rejects_reordered_columns(paths):
    fields = gt.csv_fieldnames()
    fields[0], fields[1] = fields[1], fields[0]
    gt.RESULTS_CSV.write_text(",".join(fields) + "\n", encoding="utf-8-sig")
    with pytest.raises(SystemExit, match="different order"):
        gt.check_results_schema()


def test_schema_guard_accepts_current_and_empty(paths):
    gt.check_results_schema()  # no file yet
    gt.RESULTS_CSV.write_text(",".join(gt.csv_fieldnames()) + "\n",
                              encoding="utf-8-sig")
    gt.check_results_schema()  # current header


def test_make_row_records_provenance_and_enriches_receipt(paths):
    row = gt.make_row("gemini", "n1", "q1", answer=ANSWER,
                      geo="UZ", geo_applied=0, sample=2)
    assert row["model"] == "gem-test"
    assert row["geo"] == "UZ" and row["geo_applied"] == 0 and row["sample"] == 2
    assert row["src_example.com"] == 1
    receipt = json.loads(gt.ANSWERS_JSONL.read_text(encoding="utf-8").strip())
    assert receipt["row_id"] == row["row_id"]
    assert receipt["model"] == "gem-test"
    assert receipt["geo"] == "UZ" and receipt["sample"] == 2
    assert receipt["text"] == ANSWER["text"]  # raw answer preserved


def test_make_row_defaults_without_answer(paths):
    row = gt.make_row("chatgpt", "n1", "q1")
    assert row["model"] == "" and row["geo"] == ""
    assert row["geo_applied"] == 0 and row["sample"] == 1
    assert row["status"] == "ok"
    assert not gt.ANSWERS_JSONL.exists()  # no answer → no receipt


def test_append_load_roundtrip(paths):
    gt.append_row(gt.make_row("gemini", "n1", "q1", answer=ANSWER, sample=2))
    rows = gt.load_results()
    assert len(rows) == 1
    assert rows[0]["model"] == "gem-test"
    assert rows[0]["sample"] == "2"
    assert rows[0]["src_example.com"] == "1"
    with gt.RESULTS_CSV.open(newline="", encoding="utf-8-sig") as f:
        assert next(csv.reader(f)) == gt.csv_fieldnames()


# ---------------------------------------------------------------------------
# Adapters: model in every receipt, geo applied only where the API allows
# ---------------------------------------------------------------------------

def test_geo_supported_covers_all_engines():
    assert set(gt.GEO_SUPPORTED) == set(gt.ENGINES)
    assert gt.GEO_SUPPORTED == {"gemini": False, "perplexity": True, "openai": True}


def test_openai_applies_geo_via_user_location(monkeypatch):
    captured = {}

    def fake_post(url, *, headers, payload):
        captured["payload"] = payload
        return {"output": [{"type": "message", "content": [
            {"type": "output_text", "text": "hi", "annotations": []}]}]}

    monkeypatch.setattr(gt, "_post_with_retries", fake_post)
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("OPENAI_MODEL", "gpt-test")

    answer = gt.ask_openai("q", geo="UZ")
    tool = captured["payload"]["tools"][0]
    assert tool["user_location"] == {"type": "approximate", "country": "UZ"}
    assert answer["model"] == "gpt-test" and answer["text"] == "hi"

    gt.ask_openai("q")
    assert "user_location" not in captured["payload"]["tools"][0]


def test_openai_records_served_model(monkeypatch):
    def fake_post(url, *, headers, payload):
        return {"model": "gpt-test-2026-01-01", "output": []}

    monkeypatch.setattr(gt, "_post_with_retries", fake_post)
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("OPENAI_MODEL", "gpt-test")

    answer = gt.ask_openai("q")
    assert answer["model"] == "gpt-test-2026-01-01"   # what the API served
    assert answer["model_requested"] == "gpt-test"    # what we asked for


def test_gemini_records_geo_without_payload_change(monkeypatch):
    captured = {}

    def fake_post(url, *, headers, payload):
        captured["payload"] = payload
        return {"candidates": [{"content": {"parts": [{"text": "t"}]},
                                "groundingMetadata": {"groundingChunks": [
                                    {"web": {"uri": "https://redirect",
                                             "title": "example.com"}}]}}]}

    monkeypatch.setattr(gt, "_post_with_retries", fake_post)
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setenv("GEMINI_MODEL", "gem-test")

    answer = gt.ask_gemini("q", geo="UZ")
    assert captured["payload"]["tools"] == [{"google_search": {}}]
    assert answer["model"] == "gem-test"  # no modelVersion → requested name
    assert answer["urls"] == ["https://redirect"]
    assert answer["titles"] == ["example.com"]


def test_gemini_records_served_model_behind_alias(monkeypatch):
    def fake_post(url, *, headers, payload):
        return {"modelVersion": "gemini-9-flash-001", "candidates": []}

    monkeypatch.setattr(gt, "_post_with_retries", fake_post)
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setenv("GEMINI_MODEL", "gemini-flash-latest")

    answer = gt.ask_gemini("q")
    assert answer["model"] == "gemini-9-flash-001"
    assert answer["model_requested"] == "gemini-flash-latest"


def test_perplexity_applies_geo_via_user_location(monkeypatch):
    captured = {}

    def fake_post(url, *, headers, payload):
        captured["payload"] = payload
        return {"model": "sonar-served",
                "choices": [{"message": {"content": "t"}}],
                "citations": ["https://example.com"],
                "search_results": [{"url": "https://test.org", "title": "Test"}]}

    monkeypatch.setattr(gt, "_post_with_retries", fake_post)
    monkeypatch.setenv("PERPLEXITY_API_KEY", "test-key")
    monkeypatch.setenv("PERPLEXITY_MODEL", "sonar-test")

    answer = gt.ask_perplexity("q", geo="UZ")
    assert captured["payload"]["web_search_options"] == {
        "user_location": {"country": "UZ"}}
    assert answer["model"] == "sonar-served"
    assert answer["model_requested"] == "sonar-test"
    assert answer["urls"] == ["https://example.com", "https://test.org"]

    gt.ask_perplexity("q")
    assert set(captured["payload"]) == {"model", "messages"}  # no geo → untouched


# ---------------------------------------------------------------------------
# cmd_run: sampling, geo propagation, quota, resume
# ---------------------------------------------------------------------------

def test_cmd_run_samples_and_geo(paths, gemini_env, monkeypatch):
    calls = []

    def fake_ask(query, geo=""):
        calls.append((query, geo))
        return dict(ANSWER)

    monkeypatch.setitem(gt.ENGINES["gemini"], "fn", fake_ask)
    gt.cmd_run(run_args(samples=2, geo="uz"))

    rows = gt.load_results()
    assert len(rows) == 4  # 2 queries × 2 samples
    assert [r["sample"] for r in rows] == ["1", "2", "1", "2"]
    assert all(r["geo"] == "UZ" for r in rows)          # normalized to upper
    assert all(r["geo_applied"] == "0" for r in rows)   # gemini: recorded only
    assert all(r["model"] == "gem-test" for r in rows)
    assert all(g == "UZ" for _, g in calls)
    assert len(gt.ANSWERS_JSONL.read_text(encoding="utf-8").strip().splitlines()) == 4


def test_cmd_run_quota_drops_engine_mid_samples(paths, gemini_env, monkeypatch):
    def fake_ask(query, geo=""):
        raise gt.QuotaExhausted("daily quota: test")

    monkeypatch.setitem(gt.ENGINES["gemini"], "fn", fake_ask)
    gt.cmd_run(run_args(samples=3))

    rows = gt.load_results()
    assert len(rows) == 1  # engine dropped after the first failure
    assert rows[0]["status"].startswith("error:")
    assert rows[0]["sample"] == "1"


def test_resume_tops_up_missing_samples(paths, gemini_env, monkeypatch):
    # q1 was cut off after its first sample; an error row must not count.
    gt.append_row(gt.make_row("gemini", "n1", "q1", answer=ANSWER))
    gt.append_row(gt.make_row("gemini", "n1", "q1", status="error: boom", sample=2))
    calls = []

    def fake_ask(query, geo=""):
        calls.append(query)
        return dict(ANSWER)

    monkeypatch.setitem(gt.ENGINES["gemini"], "fn", fake_ask)
    gt.cmd_run(run_args(samples=2, resume=True))

    assert calls == ["q1", "q2", "q2"]  # q1 gets only its missing sample
    ok = [r for r in gt.load_results() if r["status"] == "ok"]
    assert sorted((r["query"], r["sample"]) for r in ok) == [
        ("q1", "1"), ("q1", "2"), ("q2", "1"), ("q2", "2")]

    calls.clear()
    gt.cmd_run(run_args(samples=2, resume=True))
    assert calls == []  # matrix complete → nothing re-fetched


def test_resume_counts_only_answers_with_the_same_geo(paths, gemini_env, monkeypatch):
    gt.append_row(gt.make_row("gemini", "n1", "q1", answer=ANSWER))            # no geo
    gt.append_row(gt.make_row("gemini", "n1", "q2", answer=ANSWER, geo="UZ"))  # UZ
    calls = []

    def fake_ask(query, geo=""):
        calls.append((query, geo))
        return dict(ANSWER)

    monkeypatch.setitem(gt.ENGINES["gemini"], "fn", fake_ask)
    gt.cmd_run(run_args(resume=True, geo="UZ"))
    assert calls == [("q1", "UZ")]  # the no-geo answer is not a UZ sample

    calls.clear()
    gt.cmd_run(run_args(resume=True))
    assert calls == [("q2", "")]    # and the UZ answer is not a no-geo sample


def test_resume_single_sample_skips_done_pairs(paths, gemini_env, monkeypatch):
    gt.append_row(gt.make_row("gemini", "n1", "q1", answer=ANSWER))
    calls = []

    def fake_ask(query, geo=""):
        calls.append(query)
        return dict(ANSWER)

    monkeypatch.setitem(gt.ENGINES["gemini"], "fn", fake_ask)
    gt.cmd_run(run_args(resume=True))
    assert calls == ["q2"]  # default --samples 1: the pre-samples behaviour


def test_geo_validation(paths, gemini_env):
    with pytest.raises(SystemExit, match="alpha-2"):
        gt.cmd_run(run_args(geo="Tashkent"))
    with pytest.raises(SystemExit, match=">= 1"):
        gt.cmd_run(run_args(samples=0))


# ---------------------------------------------------------------------------
# Reports end-to-end on synthetic history (no API calls)
# ---------------------------------------------------------------------------

def seed_history(paths):
    for q in ("q1", "q2"):
        for s in (1, 2):
            gt.append_row(gt.make_row("gemini", "n1", q, answer=ANSWER,
                                      geo="UZ", geo_applied=0, sample=s))
    ungrounded = {**ANSWER, "urls": [], "titles": []}
    gt.append_row(gt.make_row("gemini", "n1", "q1", answer=ungrounded, sample=3))


def test_cmd_report_audit_section(paths, capsys):
    seed_history(paths)
    gt.cmd_report(argparse.Namespace(out=None))
    out = capsys.readouterr().out
    assert "Measurement uncertainty" in out
    assert "nq=2, n=4" in out                      # grounded: 2 queries × 2 samples
    assert "nq=1, n=1" in out                      # ungrounded: single cluster
    assert f"B={gt.DEFAULT_B}, seed={gt.DEFAULT_SEED}" in out
    assert "Provenance (latest run): models — gem-test" in out
    assert "requested geo — UZ (recorded only" in out
    dashboard = gt.DASHBOARD_HTML.read_text(encoding="utf-8")
    assert "Measurement uncertainty" in dashboard
    assert '"audit"' in dashboard and '"audit_meta"' in dashboard


def test_dashboard_snapshot_semantics_unchanged(paths):
    seed_history(paths)
    from report_html import _prepare
    data = _prepare(gt.load_results(), gt.DOMAINS, gt.NICHES)
    # snapshot still picks ONE freshest row per pair, not all samples
    assert data["n_pairs"] == 2
    # audit section aggregates ALL grounded samples of the latest run
    assert data["audit"]["n1"]["example.com"]["n_samples"] == 4
    assert data["audit_meta"]["models"] == ["gem-test"]
    assert data["audit_meta"]["geo"] == ["UZ"]
    assert data["audit_meta"]["geo_applied_engines"] == []


def test_build_docx_both_langs(paths, monkeypatch):
    docx = pytest.importorskip("docx")
    import report_docx
    monkeypatch.setattr(report_docx, "RESULTS_CSV", gt.RESULTS_CSV)
    seed_history(paths)
    for lang, heading in (("en", "Measurement uncertainty"),
                          ("ru", "Неопределённость измерения")):
        out = paths / f"report_{lang}.docx"
        report_docx.build_docx(out, lang=lang)
        text = "\n".join(p.text for p in docx.Document(out).paragraphs)
        assert heading in text
        assert f"seed={gt.DEFAULT_SEED}" in text
        assert "gem-test" in text


def test_manual_import_with_new_schema(paths):
    checklist = paths / "manual.csv"
    with checklist.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=gt.MANUAL_FIELDS)
        writer.writeheader()
        writer.writerow({"engine": "chatgpt", "niche": "n1", "query": "q1",
                         "domains_in_sources": "example.com",
                         "domains_in_text": "test.org"})
    gt.cmd_manual_import(argparse.Namespace(file=str(checklist)))
    rows = gt.load_results()
    assert rows[0]["src_example.com"] == "1"
    assert rows[0]["txt_test.org"] == "1"
    assert rows[0]["sample"] == "1" and rows[0]["model"] == ""
