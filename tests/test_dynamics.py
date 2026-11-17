# -*- coding: utf-8 -*-
"""Task 4 dynamics: paired comparison, Holm, gates, displayed=tested.

Mandatory tests (approved method §9): FWER ≤5% on an 18-pair family under
no change (fixed-seed simulation); the sign-divergence example B; all
comparability gates C; non-done runs excluded. Plus Holm correctness,
query×engine matching, the overall row, and a power-trend sanity check.
"""
import random

import pytest

from app import dynamics as dy


def ans(niche, query, engine, domain, hits, n, model="m", grounded=True):
    """n ok answers for one (niche, query, engine); the first `hits` hit."""
    rows = []
    for i in range(n):
        rows.append({
            "niche": niche, "query": query, "engine": engine, "status": "ok",
            "n_sources": "1" if grounded else "0", "model": model,
            "run_date": "2026-11-17", "sample": str(i + 1),
            f"src_{domain}": "1" if i < hits else "0", f"txt_{domain}": "0",
        })
    return rows


def cfg(domains, niches, geo="UZ", version=2, aliases=None, engines=("gemini",)):
    return {"domains": list(domains),
            "niches": {n: {"title": n, "primary_domain": domains[0], "queries": []}
                       for n in niches},
            "aliases": aliases or {}, "geo": geo, "engines": list(engines),
            "models": {}, "samples": 1, "match_rules_version": version}


def find(pairs, domain, niche):
    return next(p for p in pairs if p["domain"] == domain and p["niche"] == niche)


# ---------------------------------------------------------------------------
# Correction B: the displayed change is the tested mean, not the pooled diff
# ---------------------------------------------------------------------------

def test_example_B_sign_divergence_at_pair_level():
    ra = {("n", "q1"): {"gemini": (0, 1)}, ("n", "q2"): {"gemini": (10, 10)}}
    rb = {("n", "q1"): {"gemini": (1, 1)}, ("n", "q2"): {"gemini": (5, 10)}}
    pd = dy.pair_diffs(ra, rb)
    assert pd["diffs"] == [1.0, -0.5]
    assert sum(pd["diffs"]) / 2 == 0.25                 # tested mean → +25 pp
    assert pd["pooled_a"] == pytest.approx(10 / 11)     # reference 90.9%
    assert pd["pooled_b"] == pytest.approx(6 / 11)      # reference 54.5%
    assert pd["pooled_b"] - pd["pooled_a"] < 0          # pooled diff is negative


def test_compare_shows_tested_mean_not_pooled_diff():
    d = "example.com"
    # q1, q2 from example B; q3..q30 neutral (1/1 both) so n=30 ≥ floor
    rows_a = ans("n", "q1", "gemini", d, 0, 1) + ans("n", "q2", "gemini", d, 10, 10)
    rows_b = ans("n", "q1", "gemini", d, 1, 1) + ans("n", "q2", "gemini", d, 5, 10)
    for i in range(3, 31):
        rows_a += ans("n", f"q{i}", "gemini", d, 1, 1)
        rows_b += ans("n", f"q{i}", "gemini", d, 1, 1)
    res = dy.compare(cfg([d], ["n"]), rows_a, cfg([d], ["n"]), rows_b)
    row = find(res["pairs"], d, "n")
    assert row["n_common"] == 30
    assert row["delta_pp"] == round(100 * 0.5 / 30, 1)  # +1.7 pp = mean of diffs
    assert row["direction"] == "up"
    assert row["pooled_b"] - row["pooled_a"] < 0         # reference differs in sign


# ---------------------------------------------------------------------------
# Correction A: Holm controls the family-wise error rate
# ---------------------------------------------------------------------------

def test_holm_adjustment_known_values():
    adj = dy.holm([0.01, 0.04, 0.03])
    assert adj[0] == pytest.approx(0.03)
    assert adj[1] == pytest.approx(0.06)
    assert adj[2] == pytest.approx(0.06)
    assert dy.holm([]) == []


def _null_diffs(rng, n, samples):
    d = []
    for _ in range(n):
        p = rng.uniform(0.1, 0.9)
        ha = sum(rng.random() < p for _ in range(samples))
        hb = sum(rng.random() < p for _ in range(samples))
        d.append(hb / samples - ha / samples)
    return d


def test_fwer_no_change_18_pairs_at_most_5_percent():
    rng = random.Random(7)
    families, false_fam = 200, 0
    for _ in range(families):
        ps = [dy.paired_pvalue(_null_diffs(rng, 30, 3), B=2000, seed=0)
              for _ in range(18)]
        if any(a < 0.05 for a in dy.holm(ps)):
            false_fam += 1
    assert false_fam / families <= 0.05


def test_power_trend_increases_with_queries():
    def power(n, reps=120):
        rng = random.Random(1000 + n)
        sig = 0
        for _ in range(reps):
            d = []
            for _ in range(n):
                ha = int(rng.random() < 0.2)
                hb = int(rng.random() < 0.4)
                d.append(hb - ha)
            if dy.paired_pvalue(d, B=1000, seed=0) < 0.05:
                sig += 1
        return sig / reps
    p30, p60, p120 = power(30), power(60), power(120)
    assert p30 < p60 < p120
    assert p30 < 0.5 and p120 > 0.85


# ---------------------------------------------------------------------------
# Permutation test edges
# ---------------------------------------------------------------------------

def test_paired_pvalue_edges_and_reproducibility():
    assert dy.paired_pvalue([]) == 1.0
    assert dy.paired_pvalue([0.0, 0.0, 0.0]) == 1.0
    d = [0.5] * 40
    p1 = dy.paired_pvalue(d, B=2000, seed=0)
    p2 = dy.paired_pvalue(d, B=2000, seed=0)
    assert p1 == p2 and p1 < 0.05
    assert dy.paired_pvalue([-0.5] * 40, B=2000, seed=0) == p1  # two-sided


# ---------------------------------------------------------------------------
# Correction C: hard gates + models warning
# ---------------------------------------------------------------------------

def _two_runs(d="example.com", n=30, **kw):
    rows_a, rows_b = [], []
    for i in range(n):
        rows_a += ans("n", f"q{i}", "gemini", d, 1, 2, **kw.get("a", {}))
        rows_b += ans("n", f"q{i}", "gemini", d, 1, 2, **kw.get("b", {}))
    return rows_a, rows_b


def test_gate_rules_version():
    ra, rb = _two_runs()
    res = dy.compare(cfg(["example.com"], ["n"], version=1), ra,
                     cfg(["example.com"], ["n"], version=2), rb)
    assert not res["comparable"] and "rules_version" in res["reasons"]
    assert res["pairs"] == []


def test_gate_geo():
    ra, rb = _two_runs()
    res = dy.compare(cfg(["example.com"], ["n"], geo="UZ"), ra,
                     cfg(["example.com"], ["n"], geo="US"), rb)
    assert not res["comparable"] and "geo" in res["reasons"]


def test_gate_not_done():
    ra, rb = _two_runs()
    res = dy.compare(cfg(["example.com"], ["n"]), ra,
                     cfg(["example.com"], ["n"]), rb, status_a="cancelled")
    assert not res["comparable"] and "not_done" in res["reasons"]


def test_gate_aliases_per_domain():
    d1, d2 = "example.com", "other.org"
    ra = ans("n", "q", "gemini", d1, 1, 1) + ans("n", "q", "gemini", d2, 1, 1)
    rb = list(ra)
    ca = cfg([d1, d2], ["n"], aliases={d1: ["A"]})
    cb = cfg([d1, d2], ["n"], aliases={d1: ["B"]})
    res = dy.compare(ca, ra, cb, rb)
    assert res["comparable"]
    p1 = find(res["pairs"], d1, None)  # domain-level gate → one row, niche=None
    assert p1["verdict"] == "aliases_differ"
    assert p1["aliases_a"] == ["A"] and p1["aliases_b"] == ["B"]
    assert find(res["pairs"], d2, "n")["verdict"] != "aliases_differ"


def test_domain_absent_in_one_run():
    ra = ans("n", "q", "gemini", "example.com", 1, 1)
    rb = ans("n", "q", "gemini", "other.org", 1, 1)
    res = dy.compare(cfg(["example.com"], ["n"]), ra, cfg(["other.org"], ["n"]), rb)
    assert find(res["pairs"], "example.com", None)["verdict"] == "absent"
    assert find(res["pairs"], "other.org", None)["verdict"] == "absent"


def test_models_warning_was_became():
    ra, rb = _two_runs(a={"model": "m-old"}, b={"model": "m-new"})
    res = dy.compare(cfg(["example.com"], ["n"]), ra, cfg(["example.com"], ["n"]), rb)
    assert res["comparable"]
    w = res["models_warning"]
    assert {"engine": "gemini", "was": ["m-old"], "became": ["m-new"]} in w


def test_grounded_fraction_shown_for_both_runs():
    ra = ans("n", "q", "gemini", "example.com", 1, 2, grounded=True)
    rb = ans("n", "q", "gemini", "example.com", 1, 2, grounded=False)
    res = dy.compare(cfg(["example.com"], ["n"]), ra, cfg(["example.com"], ["n"]), rb)
    assert res["grounded"]["a"]["overall"]["fraction"] == 1.0
    assert res["grounded"]["b"]["overall"]["fraction"] == 0.0


# ---------------------------------------------------------------------------
# Query × engine matching (decision 2)
# ---------------------------------------------------------------------------

def test_query_without_common_engine_is_not_common():
    d = "example.com"
    ra = ans("n", "q", "gemini", d, 1, 1)
    rb = ans("n", "q", "openai", d, 1, 1)  # same query, disjoint engines
    res = dy.compare(cfg([d], ["n"], engines=("gemini", "openai")), ra,
                     cfg([d], ["n"], engines=("gemini", "openai")), rb)
    row = find(res["pairs"], d, "n")
    assert row["n_common"] == 0 and row["verdict"] == "insufficient"


def test_rate_pools_only_common_engines():
    d = "example.com"
    # q has gemini in both, openai only in A → rate uses gemini only
    ra = ans("n", "q", "gemini", d, 0, 1) + ans("n", "q", "openai", d, 1, 1)
    rb = ans("n", "q", "gemini", d, 1, 1)
    rates_a = dy._rates_by_obs(ra, d, "n")
    rates_b = dy._rates_by_obs(rb, d, "n")
    pd = dy.pair_diffs(rates_a, rates_b)
    assert pd["diffs"] == [1.0]  # gemini 0→1; openai excluded (not in B)


# ---------------------------------------------------------------------------
# Correction D: overall row across niches, one Holm family, floor 30
# ---------------------------------------------------------------------------

def test_overall_row_reaches_floor_when_niches_do_not():
    d = "example.com"
    ra, rb = [], []
    for niche in ("n1", "n2"):
        for i in range(20):  # 20 common queries per niche (<30), 40 overall
            ra += ans(niche, f"{niche}-q{i}", "gemini", d, 1, 1)
            rb += ans(niche, f"{niche}-q{i}", "gemini", d, 1, 1)
    res = dy.compare(cfg([d], ["n1", "n2"]), ra, cfg([d], ["n1", "n2"]), rb)
    assert find(res["pairs"], d, "n1")["verdict"] == "insufficient"
    assert find(res["pairs"], d, "n2")["verdict"] == "insufficient"
    overall = find(res["pairs"], d, None)
    assert overall["overall"] and overall["n_common"] == 40
    assert overall["verdict"] in ("noise", "significant")
    # family = the overall row only (niche rows gated out as insufficient)
    assert res["family_size"] == 1


def test_family_includes_niche_and_overall_rows():
    d = "example.com"
    ra, rb = [], []
    for niche in ("n1", "n2"):
        for i in range(30):
            ra += ans(niche, f"{niche}-q{i}", "gemini", d, 1, 2)
            rb += ans(niche, f"{niche}-q{i}", "gemini", d, 1, 2)
    res = dy.compare(cfg([d], ["n1", "n2"]), ra, cfg([d], ["n1", "n2"]), rb)
    # 2 niche rows + 1 overall row, all ≥30 → family of 3
    assert res["family_size"] == 3
    for p in res["pairs"]:
        if p["verdict"] in ("noise", "significant"):
            assert "raw_p" in p and "adj_p" in p


def test_insufficient_shows_no_number():
    d = "example.com"
    ra = ans("n", "q", "gemini", d, 1, 1)
    rb = ans("n", "q", "gemini", d, 1, 1)
    res = dy.compare(cfg([d], ["n"]), ra, cfg([d], ["n"]), rb)
    row = find(res["pairs"], d, "n")
    assert row["verdict"] == "insufficient"
    assert row["delta_pp"] is None and row["raw_p"] is None and row["adj_p"] is None
