# -*- coding: utf-8 -*-
"""Run-to-run dynamics: paired comparison of two done runs (task 4).

Method: docs/comparison-method.md (approved with corrections A–E). Pure
functions over the CSV-shaped rows from runner.run_rows and the config
snapshots from runner.run_config — no I/O, no queries.py, no new deps.

Unit of evidence is the query, matched at the (query × engine) level: a
query is common only if some engine has ok grounded answers for it in BOTH
runs, and its per-run rate pools exactly those common engines. The tested
quantity is the mean over common queries of the per-query rate difference;
the displayed change IS that mean (correction B), not the difference of the
pooled subset shares (shown only as reference). Significance is a seeded
paired sign-flip permutation test, with Holm correction across the whole
family of niche rows + per-domain overall rows (corrections A, D).
"""
from __future__ import annotations

import random

from sov_stats import (hit, is_grounded, share_ci, sov_stats,
                       DEFAULT_B, DEFAULT_SEED, MIN_BOOTSTRAP_QUERIES)

ALPHA = 0.05
PERM_B = 10000
PERM_SEED = 0
MIN_COMMON = MIN_BOOTSTRAP_QUERIES  # 30, same floor as the CI


def grounded_fraction(rows: list, engine: str = None) -> dict | None:
    """Share of ok answers that have sources (the engine actually searched).

    Shown for both runs next to the comparison so a drop in searching is
    visible (decision 1). engine=None pools all engines."""
    ok = [r for r in rows if r.get("status") == "ok"
          and (engine is None or r.get("engine") == engine)]
    if not ok:
        return None
    g = sum(1 for r in ok if is_grounded(r))
    return {"grounded": g, "ok": len(ok), "fraction": g / len(ok)}


def _rates_by_obs(rows: list, domain: str, niche: str = None) -> dict:
    """{(niche, query): {engine: (hits, n_samples)}} over ok grounded rows.

    niche=None collects across all niches (the per-domain overall row, D)."""
    out: dict = {}
    for r in rows:
        if r.get("status") != "ok" or not is_grounded(r):
            continue
        if niche is not None and r.get("niche") != niche:
            continue
        obs = (r["niche"], r["query"])
        eng = r["engine"]
        d = out.setdefault(obs, {})
        h, n = d.get(eng, (0, 0))
        d[eng] = (h + int(hit(r, domain)), n + 1)
    return out


def pair_diffs(rates_a: dict, rates_b: dict) -> dict:
    """Paired per-query rate differences over common (query × engine).

    Returns diffs (one per common query), the common-query count, counts of
    queries unique to each run, and the pooled subset shares for reference
    (NOT the tested change — correction B)."""
    diffs = []
    only_a = only_b = 0
    ah = an = bh = bn = 0
    for obs, eng_a in rates_a.items():
        eng_b = rates_b.get(obs)
        if not eng_b:
            only_a += 1
            continue
        common = set(eng_a) & set(eng_b)
        if not common:
            # present in both runs but no common engine for this query:
            # not a common query, and not "only in A" either — just excluded
            continue
        ha = sum(eng_a[e][0] for e in common)
        na = sum(eng_a[e][1] for e in common)
        hb = sum(eng_b[e][0] for e in common)
        nb = sum(eng_b[e][1] for e in common)
        diffs.append(hb / nb - ha / na)
        ah += ha; an += na; bh += hb; bn += nb
    only_b = sum(1 for obs in rates_b if obs not in rates_a)
    return {
        "diffs": diffs,
        "n_common": len(diffs),
        "only_a": only_a,
        "only_b": only_b,
        "pooled_a": (ah / an) if an else None,
        "pooled_b": (bh / bn) if bn else None,
    }


def paired_pvalue(diffs: list, B: int = PERM_B, seed: int = PERM_SEED) -> float:
    """Two-sided paired sign-flip permutation p-value on the mean difference.

    Statistic |sum(d)| (equivalent to |mean(d)| for fixed n). Under the null
    each difference's sign is exchangeable; B seeded sign-flips give the
    reference distribution. Add-one correction. All-zero → p=1."""
    n = len(diffs)
    if n == 0:
        return 1.0
    obs = abs(sum(diffs))
    if obs == 0:
        return 1.0
    rng = random.Random(seed)
    ge = 0
    for _ in range(B):
        bits = rng.getrandbits(n)
        s = 0
        for q in range(n):
            s += diffs[q] if (bits >> q) & 1 else -diffs[q]
        if abs(s) >= obs:
            ge += 1
    return (1 + ge) / (1 + B)


def holm(pvals: list) -> list:
    """Holm–Bonferroni adjusted p-values (step-down), same order as input."""
    m = len(pvals)
    if m == 0:
        return []
    order = sorted(range(m), key=lambda i: pvals[i])
    adj = [0.0] * m
    running = 0.0
    for rank, i in enumerate(order):
        running = max(running, (m - rank) * pvals[i])
        adj[i] = min(1.0, running)
    return adj


def _models_by_engine(rows: list) -> dict:
    out: dict = {}
    for r in rows:
        if r.get("status") != "ok":
            continue
        m = r.get("model") or ""
        if m:
            out.setdefault(r["engine"], set()).add(m)
    return {e: sorted(ms) for e, ms in out.items()}


def _rules_version(cfg: dict) -> int:
    return cfg.get("match_rules_version", 1)


def compare(cfg_a: dict, rows_a: list, cfg_b: dict, rows_b: list, *,
            status_a: str = "done", status_b: str = "done",
            alpha: float = ALPHA, B: int = PERM_B, seed: int = PERM_SEED) -> dict:
    """Compare two runs. Hard gates first (status, rules version, geo);
    then per-domain gates (presence, aliases); then the paired test with
    Holm across the family of niche rows + overall rows."""
    reasons = []
    if status_a != "done" or status_b != "done":
        reasons.append("not_done")
    if _rules_version(cfg_a) != _rules_version(cfg_b):
        reasons.append("rules_version")
    if (cfg_a.get("geo") or "") != (cfg_b.get("geo") or ""):
        reasons.append("geo")

    ma, mb = _models_by_engine(rows_a), _models_by_engine(rows_b)
    models_warning = []
    for e in sorted(set(ma) | set(mb)):
        was, became = ma.get(e, []), mb.get(e, [])
        if was != became:
            models_warning.append({"engine": e, "was": was, "became": became})

    gf = {
        "a": {"overall": grounded_fraction(rows_a),
              "by_engine": {e: grounded_fraction(rows_a, e)
                            for e in sorted({r["engine"] for r in rows_a
                                             if r.get("status") == "ok"})}},
        "b": {"overall": grounded_fraction(rows_b),
              "by_engine": {e: grounded_fraction(rows_b, e)
                            for e in sorted({r["engine"] for r in rows_b
                                             if r.get("status") == "ok"})}},
    }

    base = {"comparable": not reasons, "reasons": reasons,
            "rules_a": _rules_version(cfg_a), "rules_b": _rules_version(cfg_b),
            "geo_a": cfg_a.get("geo") or "", "geo_b": cfg_b.get("geo") or "",
            "models_warning": models_warning, "grounded": gf,
            "alpha": alpha, "B": B, "seed": seed, "min_common": MIN_COMMON,
            "pairs": [], "family_size": 0}
    if reasons:
        return base

    domains = sorted(set(cfg_a["domains"]) | set(cfg_b["domains"]))
    niches = sorted(set(cfg_a["niches"]) & set(cfg_b["niches"]))
    aliases_a, aliases_b = cfg_a.get("aliases", {}), cfg_b.get("aliases", {})

    pairs = []
    for d in domains:
        if d not in cfg_a["domains"] or d not in cfg_b["domains"]:
            pairs.append({"domain": d, "niche": None, "overall": False,
                          "verdict": "absent", "n_common": 0})
            continue
        if sorted(aliases_a.get(d, [])) != sorted(aliases_b.get(d, [])):
            pairs.append({"domain": d, "niche": None, "overall": False,
                          "verdict": "aliases_differ", "n_common": 0,
                          "aliases_a": sorted(aliases_a.get(d, [])),
                          "aliases_b": sorted(aliases_b.get(d, []))})
            continue
        # niche rows + one overall row across all niches (D), one family (A)
        for niche in niches + [None]:
            ra = _rates_by_obs(rows_a, d, niche)
            rb = _rates_by_obs(rows_b, d, niche)
            pd = pair_diffs(ra, rb)
            n = pd["n_common"]
            row = {"domain": d, "niche": niche, "overall": niche is None,
                   "n_common": n, "only_a": pd["only_a"], "only_b": pd["only_b"],
                   "pooled_a": pd["pooled_a"], "pooled_b": pd["pooled_b"]}
            if n < MIN_COMMON:
                row["verdict"] = "insufficient"
                row["delta_pp"] = None
                row["raw_p"] = None
                row["adj_p"] = None
            else:
                mean_d = sum(pd["diffs"]) / n
                row["delta_pp"] = round(100.0 * mean_d, 1)
                row["direction"] = "up" if mean_d > 0 else ("down" if mean_d < 0 else "flat")
                row["discordant"] = sum(1 for x in pd["diffs"] if x != 0)
                row["raw_p"] = paired_pvalue(pd["diffs"], B=B, seed=seed)
                row["verdict"] = "pending_holm"
            pairs.append(row)

    family = [p for p in pairs if p.get("raw_p") is not None]
    adj = holm([p["raw_p"] for p in family])
    for p, a in zip(family, adj):
        p["adj_p"] = a
        p["verdict"] = "significant" if a < alpha else "noise"
    base["pairs"] = pairs
    base["family_size"] = len(family)
    return base


def run_series(cfg: dict, rows: list) -> dict:
    """{(niche, domain): share_ci} over this run's own ok grounded rows —
    the descriptive history point (its own engines, its own queries)."""
    stats = share_ci_rows(rows, cfg["domains"], cfg["niches"])
    return stats


def share_ci_rows(rows: list, domains: list, niches) -> dict:
    """Grounded-channel share_ci per niche × domain (audit channel), via the
    guaranteed sov_stats estimator."""
    return sov_stats(rows, domains, niches, grounded=True)
