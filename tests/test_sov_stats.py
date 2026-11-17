# -*- coding: utf-8 -*-
"""Known-answer and edge-case tests for the audit-grade statistics module.

The module is pure (rows in, numbers out), so everything here runs on
synthetic CSV-shaped rows — no config, no files, no network.
"""
from sov_stats import (DEFAULT_B, DEFAULT_SEED, MIN_BOOTSTRAP_QUERIES,
                       cluster_counts, format_ci, hit, is_grounded, share_ci,
                       sov_stats, wilson_interval)


def make_row(niche="n1", query="q1", domain="example.com", src=0, txt=0,
             status="ok", n_sources=1, run_date="2026-11-16", engine="gemini"):
    return {"niche": niche, "query": query, "status": status,
            "n_sources": str(n_sources), "run_date": run_date, "engine": engine,
            f"src_{domain}": str(src), f"txt_{domain}": str(txt)}


# ---------------------------------------------------------------------------
# Signal helpers
# ---------------------------------------------------------------------------

def test_hit_src_or_txt():
    assert hit(make_row(src=1), "example.com")
    assert hit(make_row(txt=1), "example.com")
    assert hit(make_row(src=1, txt=1), "example.com")
    assert not hit(make_row(), "example.com")
    assert not hit({}, "example.com")  # missing columns (old history) → no hit


def test_is_grounded():
    assert is_grounded(make_row(n_sources=3))
    assert not is_grounded(make_row(n_sources=0))
    assert not is_grounded({"n_sources": ""})
    assert not is_grounded({"n_sources": None})
    assert not is_grounded({"n_sources": "abc"})
    assert not is_grounded({})


# ---------------------------------------------------------------------------
# Clustering
# ---------------------------------------------------------------------------

def test_cluster_counts_groups_samples_per_query():
    rows = [make_row(query="q1", src=1), make_row(query="q1", txt=1),
            make_row(query="q2")]
    assert cluster_counts(rows, "n1", "example.com") == {"q1": (2, 2), "q2": (0, 1)}


def test_cluster_counts_filters_niche_and_status():
    rows = [make_row(niche="other"), make_row(status="error: quota"),
            make_row(query="q1", src=1)]
    assert cluster_counts(rows, "n1", "example.com") == {"q1": (1, 1)}


# ---------------------------------------------------------------------------
# Intervals: known answers and honest edges
# ---------------------------------------------------------------------------

def test_empty_clusters_return_none():
    assert share_ci({}) is None


def test_wilson_interval_known_values():
    lo, hi = wilson_interval(0.0, 10)
    assert lo == 0.0 and round(hi, 1) == 27.8      # z²/(n+z²) = 3.84/13.84
    lo, hi = wilson_interval(1.0, 10)
    assert round(lo, 1) == 72.2 and hi == 100.0    # mirror image
    lo, hi = wilson_interval(0.5, 10)
    assert round(lo, 1) == 23.7 and round(hi, 1) == 76.3


def test_all_misses_is_not_reported_as_certainty():
    # 0 hits across 10 queries × 3 samples: a percentile bootstrap would say
    # [0.0–0.0]. The honest upper bound comes from the 10 queries, not 30 rows.
    st = share_ci({f"q{i}": (0, 3) for i in range(10)})
    assert (st["point"], st["lo"], st["hi"]) == (0.0, 0.0, 27.8)
    assert st["method"] == "wilson" and st["n_queries"] == 10


def test_all_hits_is_not_reported_as_certainty():
    st = share_ci({"q1": (2, 2), "q2": (1, 1)})
    assert (st["point"], st["lo"], st["hi"]) == (100.0, 34.2, 100.0)
    assert st["method"] == "wilson"


def test_few_queries_use_wilson_over_queries_not_samples():
    # 1 hit in 30 samples over 10 queries: n is the 10 queries.
    clusters = {"q0": (1, 3), **{f"q{i}": (0, 3) for i in range(1, 10)}}
    st = share_ci(clusters)
    assert (st["point"], st["lo"], st["hi"]) == (3.3, 0.2, 32.3)
    assert st["method"] == "wilson"
    assert st["n_queries"] == 10 and st["n_samples"] == 30 and st["hits"] == 1


def test_a_hit_never_narrows_the_upper_bound():
    # The regression this guards: [0.0–27.8] at 0 hits but [0.0–10.0] at 1 hit.
    prev_hi = -1.0
    for hits in range(0, 11):
        clusters = {f"q{i}": (3 if i < hits else 0, 3) for i in range(10)}
        st = share_ci(clusters)
        assert st["hi"] >= prev_hi
        prev_hi = st["hi"]


def test_more_samples_per_query_do_not_narrow_the_interval():
    few = share_ci({f"q{i}": (i % 2, 1) for i in range(10)})
    many = share_ci({f"q{i}": (20 * (i % 2), 20) for i in range(10)})
    assert (few["lo"], few["hi"]) == (many["lo"], many["hi"])


def test_single_query_is_one_observation():
    st = share_ci({"q1": (3, 5)})
    assert st["point"] == 60.0 and st["method"] == "wilson"
    assert st["n_queries"] == 1 and st["n_samples"] == 5 and st["hits"] == 3
    assert st["lo"] < 20.0 and st["hi"] > 90.0   # one query says very little
    st = share_ci({"q1": (0, 3)})
    assert (st["point"], st["lo"], st["hi"]) == (0.0, 0.0, 79.3)


def many_clusters():
    # 32 queries: enough for the bootstrap branch. 16 always hit, 16 never.
    return {f"q{i}": (2 * (i % 2), 2) for i in range(32)}


def test_many_queries_use_the_cluster_bootstrap():
    st = share_ci(many_clusters())
    assert st["method"] == "bootstrap" and st["point"] == 50.0
    assert st["n_queries"] == 32 and st["n_samples"] == 64 and st["hits"] == 32
    assert 25.0 < st["lo"] < 45.0 and 55.0 < st["hi"] < 75.0


def test_bootstrap_threshold_is_the_documented_one():
    below = {f"q{i}": (i % 2, 1) for i in range(MIN_BOOTSTRAP_QUERIES - 1)}
    at = {f"q{i}": (i % 2, 1) for i in range(MIN_BOOTSTRAP_QUERIES)}
    assert share_ci(below)["method"] == "wilson"
    assert share_ci(at)["method"] == "bootstrap"


def test_seeded_bootstrap_is_reproducible():
    a = share_ci(many_clusters(), seed=DEFAULT_SEED)
    b = share_ci(many_clusters(), seed=DEFAULT_SEED)
    assert a == b and a["method"] == "bootstrap"
    assert a["lo"] <= a["point"] <= a["hi"]
    assert 0.0 <= a["lo"] <= a["hi"] <= 100.0


def test_unequal_cluster_sizes_reported_honestly():
    st = share_ci({"q1": (3, 3), "q2": (0, 1)})
    assert st["point"] == 75.0
    assert st["n_queries"] == 2 and st["n_samples"] == 4 and st["hits"] == 3


# ---------------------------------------------------------------------------
# sov_stats: filters and the legacy-point cross-check
# ---------------------------------------------------------------------------

def mixed_rows():
    # q1: 2/2 hits, q2: 1/2, q3: 0/2, q4: 2/2 → 5/8 = 62.5%
    rows = []
    for query, hits in (("q1", 2), ("q2", 1), ("q3", 0), ("q4", 2)):
        for i in range(2):
            rows.append(make_row(query=query, src=int(i < hits)))
    return rows


def test_sov_stats_point_matches_sample_proportion():
    stats = sov_stats(mixed_rows(), ["example.com"], ["n1"])
    st = stats["n1"]["example.com"]
    assert st["point"] == 62.5
    assert st["n_queries"] == 4 and st["n_samples"] == 8 and st["hits"] == 5
    assert st["lo"] <= 62.5 <= st["hi"]


def test_sov_stats_filters():
    rows = mixed_rows()
    rows.append(make_row(query="q9", src=1, run_date="2026-11-17"))
    rows.append(make_row(query="q9", src=1, engine="openai"))
    rows.append(make_row(query="q9", src=1, n_sources=0))  # ungrounded

    base = sov_stats(rows, ["example.com"], ["n1"])["n1"]["example.com"]
    assert base["n_samples"] == 11

    by_date = sov_stats(rows, ["example.com"], ["n1"], run_date="2026-11-17")
    assert by_date["n1"]["example.com"]["n_samples"] == 1

    by_engine = sov_stats(rows, ["example.com"], ["n1"], engine="openai")
    assert by_engine["n1"]["example.com"]["n_samples"] == 1

    grounded = sov_stats(rows, ["example.com"], ["n1"], grounded=True)
    assert grounded["n1"]["example.com"]["n_samples"] == 10
    ungrounded = sov_stats(rows, ["example.com"], ["n1"], grounded=False)
    assert ungrounded["n1"]["example.com"]["n_samples"] == 1


def test_sov_stats_empty_niche_and_domain_args():
    stats = sov_stats([], ["example.com"], ["n1"])
    assert stats == {"n1": {}}
    # domains/niches come from the caller, not from any global config
    stats = sov_stats(mixed_rows(), ["test.org"], ["n1", "absent"])
    assert stats["n1"]["test.org"]["point"] == 0.0
    assert stats["absent"] == {}


def test_format_ci():
    st = {"point": 17.0, "lo": 9.0, "hi": 28.0, "n_queries": 10, "n_samples": 30}
    assert format_ci(st) == "17.0% [9.0\u201328.0] (nq=10, n=30)"


def test_defaults_are_the_documented_methodology():
    assert DEFAULT_B == 2000
    assert DEFAULT_SEED == 0
    assert MIN_BOOTSTRAP_QUERIES == 30
