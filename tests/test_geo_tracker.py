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
          "titles": ["example.com"], "model": "gem-test",
          "usage": {"totalTokenCount": 100}, "searches": 1}


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
# Matching rules v2: subdomains in text/titles, no dot-continuation,
# word-boundary aliases (each case verified against the old behavior)
# ---------------------------------------------------------------------------

def test_text_subdomain_counts_like_url():
    assert gt.domain_in_text("brand.io", "see shop.brand.io")
    assert gt.domain_in_text("brand.io", "a.b.brand.io")
    assert gt.domain_in_url("brand.io", "https://shop.brand.io/x")  # parity


def test_title_subdomain_counts_as_src():
    # Gemini: the title IS the source host
    hits = gt.match_domains("no domains here", [], ["shop.brand.io"],
                            domains=["brand.io"], aliases={}, titles_are_hosts=True)
    assert hits["brand.io"]["src"] == 1


def test_page_title_of_another_site_is_not_a_citation(paths):
    # Perplexity / OpenAI return real URLs plus the cited page's headline.
    # A review of brand.io on techsite.com is techsite.com being cited.
    answer = {"text": "see the review", "model": "m",
              "urls": ["https://techsite.com/review"],
              "titles": ["Brand.io review: worth it?"]}
    for engine in ("perplexity", "openai"):
        row = gt.make_row(engine, "n1", "q1", answer=dict(answer),
                          domains=["brand.io", "techsite.com"], articles={})
        assert row["src_brand.io"] == 0, engine
        assert row["src_techsite.com"] == 1, engine
    # their own URL still counts, whatever the title says
    own = {**answer, "urls": ["https://shop.brand.io/pricing"], "titles": ["Pricing"]}
    row = gt.make_row("openai", "n1", "q1", answer=own,
                      domains=["brand.io"], articles={})
    assert row["src_brand.io"] == 1


def test_gemini_host_title_still_decides_src(paths):
    answer = {"text": "t", "model": "m", "urls": ["https://redirect.example/abc"],
              "titles": ["shop.brand.io"]}
    row = gt.make_row("gemini", "n1", "q1", answer=answer,
                      domains=["brand.io"], articles={})
    assert row["src_brand.io"] == 1
    assert set(gt.HOST_TITLE_ENGINES) <= set(gt.ENGINES)


def test_titles_do_not_count_unless_the_caller_says_they_are_hosts():
    hits = gt.match_domains("", [], ["brand.io"], domains=["brand.io"], aliases={})
    assert hits["brand.io"]["src"] == 0   # safe default


def test_text_dot_continuation_rejected():
    assert not gt.domain_in_text("brand.io", "visit brand.io.evil.com")
    assert not gt.domain_in_text("brand.io", "brand.io.shop")
    hits = gt.match_domains("visit brand.io.evil.com", [], [],
                            domains=["brand.io"], aliases={})
    assert hits["brand.io"]["txt"] == 0


def test_text_sentence_final_dot_counts():
    assert gt.domain_in_text("brand.io", "Go to brand.io.")
    assert gt.domain_in_text("brand.io", "Go to brand.io. Next sentence")
    assert gt.domain_in_text("brand.io", "brand.io, brand.io; (brand.io)")


def test_text_url_and_old_guarantees():
    assert gt.domain_in_text("brand.io", "https://brand.io/pricing")
    assert gt.domain_in_text("brand.io", "BRAND.IO")
    assert not gt.domain_in_text("pc.uz", "1pc.uz")
    assert not gt.domain_in_text("brand.io", "my-brand.io")
    assert not gt.domain_in_text("brand.io", "brand.ioverse")


def test_aliases_match_on_word_boundaries():
    aliases = {"brand.io": ["Apex"]}
    hits = gt.match_domains("apexcharts is a chart library", [], [],
                            domains=["brand.io"], aliases=aliases)
    assert hits["brand.io"]["txt"] == 0
    for text in ("try Apex today", "APEX rocks", "the apex tool"):
        hits = gt.match_domains(text, [], [], domains=["brand.io"], aliases=aliases)
        assert hits["brand.io"]["txt"] == 1, text


def test_multiword_alias_boundaries():
    aliases = {"brand.io": ["Example Brand"]}
    hits = gt.match_domains("example brand new world", [], [],
                            domains=["brand.io"], aliases=aliases)
    assert hits["brand.io"]["txt"] == 1
    hits = gt.match_domains("example brandnew world", [], [],
                            domains=["brand.io"], aliases=aliases)
    assert hits["brand.io"]["txt"] == 0


def test_alias_with_star_matches_inflected_forms():
    aliases = {"brand.io": ["Бренд*"]}
    for text in ("по данным Бренд", "по данным Бренда", "в Бренде есть", "БРЕНДОМ назвали"):
        hits = gt.match_domains(text, [], [], domains=["brand.io"], aliases=aliases)
        assert hits["brand.io"]["txt"] == 1, text
    # still anchored at the start of a word
    hits = gt.match_domains("это суббренд компании", [], [],
                            domains=["brand.io"], aliases=aliases)
    assert hits["brand.io"]["txt"] == 0


def test_alias_without_star_is_the_exact_word():
    aliases = {"brand.io": ["Бренд"]}
    assert gt.match_domains("по данным Бренд", [], [], domains=["brand.io"],
                            aliases=aliases)["brand.io"]["txt"] == 1
    assert gt.match_domains("по данным Бренда", [], [], domains=["brand.io"],
                            aliases=aliases)["brand.io"]["txt"] == 0


def test_empty_alias_never_matches():
    for alias in ("", "  ", "*", " * "):
        for text in ("anything, at all.", "", "word"):
            assert not gt.alias_in_text(alias, text), (alias, text)


def test_match_rules_version_is_v2():
    assert gt.MATCH_RULES_VERSION == 2


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
    assert receipt["usage"] == {"totalTokenCount": 100}  # COGS evidence archived
    assert receipt["searches"] == 1


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


def test_perplexity_deduplicates_overlapping_sources(paths, monkeypatch):
    # citations and search_results repeat the same pages: each source counts
    # once (first-seen order), or n_sources and receipts inflate.
    def fake_post(url, *, headers, payload):
        return {"model": "sonar-served",
                "choices": [{"message": {"content": "t"}}],
                "citations": ["https://example.com/a", "", "https://test.org/b",
                              "https://example.com/a"],
                "search_results": [
                    {"url": "https://example.com/a", "title": "Example A"},
                    {"url": "https://other.net/c", "title": "Other C"},
                    {"url": "https://test.org/b", "title": "Test B"},
                    {"url": "", "title": "No URL"},
                ]}

    monkeypatch.setattr(gt, "_post_with_retries", fake_post)
    monkeypatch.setenv("PERPLEXITY_API_KEY", "test-key")

    answer = gt.ask_perplexity("q")
    assert answer["urls"] == ["https://example.com/a", "https://test.org/b",
                              "https://other.net/c"]  # no repeats, no empties
    assert answer["titles"] == ["Example A", "Other C", "Test B", "No URL"]

    row = gt.make_row("perplexity", "n1", "q1", answer=answer,
                      domains=["example.com", "test.org"], articles={})
    assert row["n_sources"] == 3
    assert row["src_example.com"] == 1 and row["src_test.org"] == 1
    receipt = json.loads(gt.ANSWERS_JSONL.read_text(encoding="utf-8").strip())
    assert receipt["urls"] == answer["urls"]  # no duplicate links archived


# ---------------------------------------------------------------------------
# Usage archival: raw API counters (and Perplexity's reported cost) in receipts
# ---------------------------------------------------------------------------

def test_gemini_archives_usage_and_search_count(monkeypatch):
    def fake_post(url, *, headers, payload):
        return {"candidates": [{"content": {"parts": [{"text": "t"}]},
                                "groundingMetadata": {
                                    "groundingChunks": [],
                                    "webSearchQueries": ["q one", "q two"]}}],
                "usageMetadata": {"promptTokenCount": 4, "totalTokenCount": 100}}

    monkeypatch.setattr(gt, "_post_with_retries", fake_post)
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    answer = gt.ask_gemini("q")
    assert answer["usage"]["totalTokenCount"] == 100
    assert answer["searches"] == 2  # executed searches = billing unit on Gemini 3+


def test_gemini_zero_searches_is_known_zero(monkeypatch):
    def fake_post(url, *, headers, payload):
        return {"candidates": [{"content": {"parts": [{"text": "t"}]}}],
                "usageMetadata": {"totalTokenCount": 50}}

    monkeypatch.setattr(gt, "_post_with_retries", fake_post)
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    answer = gt.ask_gemini("q")
    assert answer["searches"] == 0  # model skipped search — observed zero, not unknown
    assert answer["usage"]["totalTokenCount"] == 50


def test_openai_archives_usage_and_search_count(monkeypatch):
    def fake_post(url, *, headers, payload):
        return {"output": [{"type": "web_search_call",
                            "action": {"type": "search", "query": "a"}},
                           {"type": "reasoning"},
                           {"type": "web_search_call",
                            "action": {"type": "search", "query": "b"}},
                           {"type": "message", "content": [
                               {"type": "output_text", "text": "hi",
                                "annotations": []}]}],
                "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}}

    monkeypatch.setattr(gt, "_post_with_retries", fake_post)
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    answer = gt.ask_openai("q")
    assert answer["searches"] == 2
    assert answer["usage"]["total_tokens"] == 15


def test_openai_counts_only_search_actions(monkeypatch):
    # Live probe 2026-11-17: web_search_call items carry action.type
    # search / open_page / find_in_page — only search is a search.
    def fake_post(url, *, headers, payload):
        return {"output": [
            {"type": "web_search_call", "action": {"type": "search", "query": "a"}},
            {"type": "web_search_call", "action": {"type": "open_page",
                                                   "url": "https://example.com"}},
            {"type": "web_search_call", "action": {"type": "find_in_page",
                                                   "url": "https://example.com"}},
            {"type": "web_search_call"},  # no action → not counted as a search
            {"type": "message", "content": [
                {"type": "output_text", "text": "hi", "annotations": []}]}]}

    monkeypatch.setattr(gt, "_post_with_retries", fake_post)
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    answer = gt.ask_openai("q")
    assert answer["searches"] == 1


def test_perplexity_archives_usage_with_reported_cost(monkeypatch):
    def fake_post(url, *, headers, payload):
        return {"choices": [{"message": {"content": "t"}}],
                "usage": {"prompt_tokens": 12, "completion_tokens": 563,
                          "total_tokens": 575,
                          "cost": {"total_cost": 0.00526}}}

    monkeypatch.setattr(gt, "_post_with_retries", fake_post)
    monkeypatch.setenv("PERPLEXITY_API_KEY", "test-key")
    answer = gt.ask_perplexity("q")
    assert answer["usage"]["cost"]["total_cost"] == 0.00526  # API-reported, not guessed
    assert answer["searches"] is None  # Sonar reports no search count


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
    assert "Resource usage (latest run, from receipts)" in out
    assert "calls=5" in out and "tokens=500" in out  # 5 receipts × 100 tokens
    assert "usage=5/5" in out
    dashboard = gt.DASHBOARD_HTML.read_text(encoding="utf-8")
    assert "Measurement uncertainty" in dashboard
    assert '"audit"' in dashboard and '"audit_meta"' in dashboard


def test_resource_usage_unknown_for_old_receipts(paths, capsys):
    old = {k: v for k, v in ANSWER.items() if k not in ("usage", "searches")}
    gt.append_row(gt.make_row("gemini", "n1", "q1", answer=old))
    gt.cmd_report(argparse.Namespace(out=None))
    out = capsys.readouterr().out
    assert "Resource usage" in out
    # pre-usage receipts: unknown, not fabricated zeros
    assert "tokens=—" in out and "searches=—" in out and "cost=—" in out
    assert "usage=0/1" in out


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


def test_dashboard_escapes_hostile_queries_and_titles(paths):
    from report_html import build_dashboard
    query = "best <script>alert(1)</script></script> tool"
    answer = {"text": "t", "urls": ["https://example.com/a"],
              "titles": ["example.com"], "model": "m"}
    gt.append_row(gt.make_row("gemini", "n1", query, answer=answer))
    articles = {"a1": {"title": "<img onerror=alert(1)>",
                       "url": "https://example.com/page"}}
    out = paths / "dashboard.html"
    build_dashboard(gt.load_results(), gt.DOMAINS, gt.NICHES, out, articles=articles)
    html = out.read_text(encoding="utf-8")

    # The inline JSON must not close the <script> tag early: "</" is written
    # "<\/", which the JSON/JS parser still reads as "/".
    start = html.index("const D = ") + len("const D = ")
    end = html.index(";\n", start)
    blob = html[start:end]
    assert "</" not in blob
    assert "<\\/script>" in blob
    data = json.loads(blob)  # the escape round-trips to the original data
    assert data["current_hits"][0]["query"] == query
    assert data["articles"][0]["title"] == "<img onerror=alert(1)>"

    # Outside the JSON blob no raw payload may appear as markup, and only the
    # two real <script> tags (CDN + inline) may be closed.
    markup = html[:start] + html[end:]
    assert "<script>alert(1)" not in markup
    assert "<img onerror" not in markup
    assert html.count("</script>") == 2

    # Data strings reach innerHTML only through the esc() helper.
    for frag in ("esc(v)", "esc(l)", "esc(d)", "esc(info.title)", "esc(e)",
                 "esc(a.title)", "esc(a.url)", "esc(q.engine)", "esc(q.query)",
                 "esc(h.engine)", "esc(h.query)"):
        assert frag in html


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


# ---------------------------------------------------------------------------
# Per-brand config parameters (service path) — defaults stay the CLI globals
# ---------------------------------------------------------------------------

def test_match_domains_explicit_config():
    hits = gt.match_domains("Brandio is nice", [], [],
                            domains=["brand.io"], aliases={"brand.io": ["Brandio"]})
    assert hits == {"brand.io": {"src": 0, "txt": 1}}  # globals not consulted


def test_match_articles_explicit_config():
    articles = {"a1": {"title": "T", "url": "https://brand.io/page"}}
    assert gt.match_articles(["https://brand.io/page?utm=x"], articles=articles) == {"a1": 1}
    assert gt.match_articles(["https://brand.io/page"]) == {}  # global ARTICLES is empty


def test_make_row_brand_config_without_receipt(paths):
    answer = {"text": "brand.io is great", "urls": ["https://brand.io/a"],
              "titles": ["brand.io"], "model": "m"}
    row = gt.make_row("gemini", "n1", "q1", answer=answer,
                      domains=["brand.io"], articles={}, aliases={},
                      save_receipt=False)
    assert "src_brand.io" in row and row["src_brand.io"] == 1
    assert "src_example.com" not in row
    assert not gt.ANSWERS_JSONL.exists()  # service keeps receipts in its own DB


def test_build_receipt_is_pure():
    before = dict(ANSWER)
    receipt = gt.build_receipt(ANSWER, geo="UZ", geo_applied=1, sample=2)
    assert receipt["geo"] == "UZ" and receipt["sample"] == 2
    assert receipt["model"] == ANSWER["model"]
    assert ANSWER == before  # input untouched


def test_build_docx_explicit_config(paths):
    docx = pytest.importorskip("docx")
    import report_docx
    row = {"row_id": "x1", "run_date": "2026-11-17",
           "run_ts": "2026-11-17T10:00:00Z", "engine": "gemini", "niche": "n1",
           "query": "q", "status": "ok", "n_sources": "1", "model": "m",
           "geo": "", "geo_applied": "0", "sample": "1",
           "src_brand.io": "1", "txt_brand.io": "0"}
    out = paths / "brand.docx"
    report_docx.build_docx(out, rows=[row], domains=["brand.io"],
                           niches={"n1": {"title": "T", "primary_domain": "brand.io",
                                          "queries": ["q"]}},
                           articles={})
    text = "\n".join(p.text for p in docx.Document(out).paragraphs)
    tables = "\n".join(c.text for t in docx.Document(out).tables for r in t.rows for c in r.cells)
    assert "brand.io" in text + tables
    assert "example.com" not in text + tables  # no leaked global config
