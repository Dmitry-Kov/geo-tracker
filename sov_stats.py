# -*- coding: utf-8 -*-
"""
Audit-grade share of voice: a distribution, not a point guess.

A single "17% mention share" is false precision: AI answers are
non-deterministic, and repeated samples of the same query are correlated.
This module reports the share as a proportion with a 95% confidence interval
whose unit of evidence is the **query**, not the single answer — so
within-query correlation is never mistaken for independent evidence.

Two estimators, chosen by how many queries stand behind a cell:

- few queries (nq < MIN_BOOTSTRAP_QUERIES) or a share of exactly 0% / 100%:
  a **Wilson score interval with n = nq**. Each query counts as one
  observation, i.e. its samples are treated as fully correlated — the
  conservative reading. A resampling interval is unreliable on a handful of
  clusters (it comes out too narrow) and degenerates to [p, p] when every
  query agrees.
- many queries: a **percentile cluster bootstrap over queries** — whole
  queries are resampled with all their samples, so the interval also reflects
  how much the queries actually differ.

Pure functions, no I/O and no dependency on the local queries.py config:
callers pass their own domains/niches. The bootstrap is seeded and Wilson is
closed-form, so a reported interval is reproducible from the same rows — part
of the evidence contract.
"""
from __future__ import annotations

import random
from statistics import NormalDist

# Methodology parameters, quoted by the reports' methodology notes: a reported
# interval is reproducible from the same rows with these defaults.
DEFAULT_B = 2000
DEFAULT_SEED = 0
MIN_BOOTSTRAP_QUERIES = 30


def hit(row: dict, domain: str) -> bool:
    """A domain is visible in an answer if it is a source (src) or named in text (txt)."""
    return row.get(f"src_{domain}") == "1" or row.get(f"txt_{domain}") == "1"


def is_grounded(row: dict) -> bool:
    """True if the engine actually ran a web search (the answer has sources)."""
    try:
        return int(row.get("n_sources") or 0) > 0
    except (ValueError, TypeError):
        return False


def cluster_counts(rows: list, niche: str, domain: str) -> dict:
    """{query: (hits, n_samples)} over ok rows of one niche — one cluster per query."""
    clusters: dict = {}
    for r in rows:
        if r.get("niche") != niche or r.get("status") != "ok":
            continue
        q = r["query"]
        h, n = clusters.get(q, (0, 0))
        clusters[q] = (h + int(hit(r, domain)), n + 1)
    return clusters


def wilson_interval(p: float, n: int, alpha: float = 0.05) -> tuple:
    """Wilson score interval, in percent, for a proportion p (0..1) over n observations.

    p need not be k/n for an integer k: with n = number of queries, p is the
    pooled share across their samples.
    """
    z = NormalDist().inv_cdf(1 - alpha / 2)
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5) / denom
    lo = 0.0 if p <= 0 else 100.0 * (centre - half)
    hi = 100.0 if p >= 1 else 100.0 * (centre + half)
    return lo, hi


def share_ci(clusters: dict, *, B: int = DEFAULT_B, seed: int = DEFAULT_SEED,
             alpha: float = 0.05, min_bootstrap: int = MIN_BOOTSTRAP_QUERIES) -> dict | None:
    """Point share + 95% CI, in percent. Returns None when there are no samples.

    The result's "method" says how the interval was obtained:

    - "wilson" — fewer than min_bootstrap queries, or a share of exactly 0% /
      100%: Wilson score interval with one observation per query (n =
      n_queries, not n_samples — samples of one query are correlated).
    - "bootstrap" — percentile cluster bootstrap over queries.
    """
    items = list(clusters.values())
    n_queries = len(items)
    n_samples = sum(n for _, n in items)
    if n_samples == 0:
        return None
    hits = sum(h for h, _ in items)
    share = hits / n_samples
    if n_queries < min_bootstrap or hits in (0, n_samples):
        lo, hi = wilson_interval(share, n_queries, alpha)
        method = "wilson"
    else:
        rng = random.Random(seed)
        props = []
        for _ in range(B):
            h_sum = n_sum = 0
            for _ in range(n_queries):
                h, n = rng.choice(items)
                h_sum += h
                n_sum += n
            props.append(100.0 * h_sum / n_sum if n_sum else 0.0)
        props.sort()
        lo = props[max(0, int((alpha / 2) * B))]
        hi = props[min(B - 1, int((1 - alpha / 2) * B))]
        method = "bootstrap"
    return {"point": round(100.0 * share, 1), "lo": round(lo, 1), "hi": round(hi, 1),
            "n_queries": n_queries, "n_samples": n_samples, "hits": hits,
            "method": method}


def sov_stats(rows: list, domains: list, niches, *, run_date: str = None,
              engine: str = None, grounded: bool = None, B: int = DEFAULT_B,
              seed: int = DEFAULT_SEED) -> dict:
    """{niche: {domain: {point, lo, hi, n_queries, n_samples, hits, method}}} over ok rows.

    Filters mirror share_of_voice (run_date/engine/grounded) so the audit table
    and the legacy point matrix describe the same channel.
    """
    subset = [r for r in rows
              if r.get("status") == "ok"
              and (run_date is None or r.get("run_date") == run_date)
              and (engine is None or r.get("engine") == engine)
              and (grounded is None or is_grounded(r) == grounded)]
    out: dict = {}
    for niche in niches:
        out[niche] = {}
        for d in domains:
            st = share_ci(cluster_counts(subset, niche, d), B=B, seed=seed)
            if st is not None:
                out[niche][d] = st
    return out


def format_ci(st: dict) -> str:
    """'17.0% [9.0–28.0] (nq=10, n=30)' — the defensible one-line form."""
    return (f"{st['point']:.1f}% [{st['lo']:.1f}–{st['hi']:.1f}] "
            f"(nq={st['n_queries']}, n={st['n_samples']})")


def _host_matches_tracked(host: str, tracked: list) -> bool:
    """True if host IS a tracked domain or a subdomain of one.

    Exact host + subdomains only, after lowercasing and stripping a leading
    "www." on both sides: lookalikes (1brand.io, my-brand.io, brand.io.evil.com)
    are third-party sources, not the brand.
    """
    h = host.strip().lower()
    h = h[4:] if h.startswith("www.") else h
    for dom in tracked:
        d = dom.strip().lower()
        d = d[4:] if d.startswith("www.") else d
        if d and (h == d or h.endswith("." + d)):
            return True
    return False


def unowned_cited_sources(rows: list, domains: list, niches, *, limit: int = 10) -> dict:
    """{niche: {total_grounded, total_queries, sources: [...]}} — who the engines
    cite instead of the tracked domains.

    Grounded ok rows only (the engine ran a web search). Per niche, every
    third-party host in all_source_domains (the raw record of what was cited,
    tracked or not) is counted in answers and in distinct queries, overall and
    restricted to answers where NO tracked domain appeared (src or txt) — the
    placement-gap view. The unit of evidence stays the query: repeated samples
    of one query add answers, never queries.

    Sources are sorted by (-gap_queries, -queries, -answers, domain) and cut to
    limit (a non-positive limit keeps all). Niches without a single third-party
    source are omitted. Pure aggregation of stored fields: matching rules are
    not touched, so MATCH_RULES_VERSION does not change.
    """
    grounded = [r for r in rows if r.get("status") == "ok" and is_grounded(r)]
    out: dict = {}
    for niche in niches:
        sub = [r for r in grounded if r.get("niche") == niche]
        if not sub:
            continue
        total_grounded = len(sub)
        total_queries = len({r["query"] for r in sub})
        agg: dict = {}
        for r in sub:
            brand_missed = not any(hit(r, d) for d in domains)
            seen = set()
            for host in (r.get("all_source_domains") or "").split(";"):
                h = host.strip().lower()
                h = h[4:] if h.startswith("www.") else h
                if not h or h in seen:
                    continue
                seen.add(h)  # one host counts once per answer, however repeated
                if _host_matches_tracked(h, domains):
                    continue
                a = agg.setdefault(h, {"answers": 0, "queries": set(),
                                       "gap_answers": 0, "gap_queries": set(),
                                       "engines": set()})
                a["answers"] += 1
                a["queries"].add(r["query"])
                if brand_missed:
                    a["gap_answers"] += 1
                    a["gap_queries"].add(r["query"])
                a["engines"].add(r["engine"])
        items = [{"domain": h, "answers": a["answers"], "queries": len(a["queries"]),
                  "gap_answers": a["gap_answers"], "gap_queries": len(a["gap_queries"]),
                  "engines": sorted(a["engines"])}
                 for h, a in agg.items()]
        items.sort(key=lambda x: (-x["gap_queries"], -x["queries"],
                                  -x["answers"], x["domain"]))
        if limit > 0:
            items = items[:limit]
        if items:
            out[niche] = {"total_grounded": total_grounded,
                          "total_queries": total_queries, "sources": items}
    return out
