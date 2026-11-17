# Comparing two runs: method (for approval — no implementation yet)

Task 4 asks for the dynamics of share of voice between runs: a per
niche × domain history (table + chart) on the brand page, and a comparison
of two selected runs that says, for each pair, whether the change is
**significant** or **within noise**. This document is the method only; code
follows after approval, per the task rule.

It is written against the current guarantees in `AGENTS.md`: the unit of
evidence is the **query**; a finished run is read only through its config
snapshot (`run_rows(run)` / `run_config(run)`); a run that is not `done`
never reads like a full report (`incomplete_notice()`); matching rules are
versioned and runs under different versions are not comparable.

## 1. Which runs may be compared

- **Only `done` runs.** `cancelled`, `error`, `queued`, `pending`,
  `running` are excluded from history numbers and from the comparison
  selectors. If such a run appears in a list, it carries its status badge
  and **no figures**. Any export or summary that ever shows a non-`done`
  run's numbers must use `incomplete_notice()` (the comparison refuses
  non-`done` selections outright, so it never needs the notice itself).
- **Same matching-rules version.** Read `match_rules_version` from each
  run's snapshot via `run_config(run)` (a snapshot without the key reads as
  version 1). If the two runs differ, the pair is **not compared**: the UI
  shows «несравнимы: разные версии правил сопоставления (vA ≠ vB)» and no
  numbers. This is a hard gate, not a warning.
- **Everything through the snapshot.** Domains, niches, queries, engines,
  samples and the rules version come from `run_rows(run)` /
  `run_config(run)`, never from the brand as it is edited today.

## 2. What "share" means here

Consistent with `sov_stats.py`, the share for a niche × domain is
**hit samples / all ok samples** in a channel, where a hit is `src` or
`txt` (`sov_stats.hit`). The proposed channel for the dynamics is
**grounded only** (`sov_stats.is_grounded`: the answer has sources) — the
same channel as the audit-grade CI section, i.e. web visibility, not
parametric recall.

> Decision point for the reviewer: grounded-only is the defensible
> web-visibility number and matches the audit section, but a
> Gemini-heavy brand that answers parametrically will have few grounded
> queries and often hit the "insufficient queries" floor. The alternative
> is all ok answers (the headline visibility matrix, which mixes
> grounded + ungrounded). The method below is identical either way; only
> the row filter changes. **Proposed: grounded-only.**

## 3. Pairing: common queries, common engines

The comparison is **paired by query** — the same query measured in both
runs is one paired observation. Pairing removes the query-mix variance
(different queries are differently hard), which is exactly the variance
that makes two independent shares look "different" when nothing changed.

For a niche × domain pair:

- **Common queries** = queries with ≥ 1 ok grounded answer in **both**
  runs (within that niche). Queries present in only one run are excluded
  from the test and reported as counts (`only in A: kA`, `only in B: kB`).
- **Common engines** = engines present in both runs' snapshots. The
  paired comparison is restricted to common engines, so a change in the
  engine mix cannot masquerade as a visibility change. Engines unique to
  one run are reported (`engines only in A/B`). If there are **no common
  engines**, the pair shows «нет общих движков» and no numbers.
  The descriptive per-run history share (section 8) stays pooled over
  each run's own engines, clearly labeled.
- **Different sample counts are fine.** Per query we use a *rate*
  (hits / samples), not a count, so 1 sample in A vs 5 in B is comparable.
  Each query weighs **equally** in the test regardless of its sample
  counts — more samples must not buy more significance (the same
  principle as the CI: samples of one query are correlated).

## 4. The change (descriptive)

Over the **common queries × common engines × grounded** subset:

- `share_A = hits_A / samples_A`, `share_B = hits_B / samples_B` (pooled).
- `Δ = share_B − share_A`, in **percentage points**, rounded to 1 decimal.

Both shares and Δ describe the *same* query set, so the delta is
apples-to-apples. (Each run's headline share over its own full query set
is shown separately in the history table; the comparison delta is always
over the common subset, and the UI says so.)

## 5. Significant or within noise — the test

Per common query `q`, compute the paired difference of its rates:

```
d_q = rate_B(q) − rate_A(q),   rate_R(q) = hits_R(q) / samples_R(q)
```

There are `n` such differences (n = number of common queries).

**Gate: n < 30 → «недостаточно запросов (n общих из X/Y)», no number.**
This mirrors `MIN_BOOTSTRAP_QUERIES = 30` in `sov_stats.py`: below 30
clusters the product already refuses a resampling interval. The same
floor applies to the paired test. No Δ, no p, no verdict is shown — only
the counts and the reason.

**For n ≥ 30: a seeded paired sign-flip permutation test** on the `d_q`.

- Null hypothesis: the run label carries no information — for each query
  the sign of `d_q` is exchangeable (equally likely to be `+d_q` or
  `−d_q`). This is the standard paired permutation null; it needs no
  normality and respects that the query, not the answer, is the unit.
- Statistic: `T = |mean(d_q)|` (the absolute mean paired difference).
- Reference distribution: flip the sign of each `d_q` independently at
  random, recompute `T`, `B = 10000` times, **seeded (`seed = 0`)** so the
  p-value is reproducible from the same rows — the same evidence contract
  as the seeded bootstrap in `sov_stats.py`. Zero differences flip to
  zero and contribute nothing, so ties are handled naturally.
- Two-sided p-value with the add-one correction:
  `p = (1 + #{T_perm ≥ T_obs}) / (1 + B)`.
- **Verdict at α = 0.05 (two-sided):**
  - `p < 0.05` → «изменение значимо», direction = sign of Δ (рост/падение).
  - `p ≥ 0.05` → «в пределах шума».
  - all `d_q = 0` → `T_obs = 0`, `p = 1` → «в пределах шума» (no change).
- Reported alongside the verdict: `n` common queries, the number of
  **discordant** queries (`d_q ≠ 0`), Δ (pp), `p` (3 decimals), and the
  method note («парный перестановочный тест по запросам, B=10000, seed=0»).

### Why this test

- **Not "do the two CIs overlap".** Overlap of two independent intervals
  is not a test of the difference, and it ignores the pairing; it is both
  invalid and under-powered. The paired test uses the query pairing
  directly.
- **Permutation over a paired t-test.** Query-level rates are bounded in
  [−1, 1], often zero-heavy and skewed; a t-test leans on normality of
  the mean. The sign-flip permutation test assumes only exchangeability
  under the null, is exact up to Monte Carlo error, and its seeded
  resampling matches the product's existing bootstrap philosophy. (A
  Wilcoxon signed-rank test is a reasonable alternative; it needs an
  exact rank distribution for the many-ties case and more code. The
  permutation test is simpler and dependency-free — `random` +
  `statistics`, no scipy, keeping the lock unchanged.)
- **Equal weight per query** keeps the guarantee that more samples per
  query do not manufacture significance.

## 6. What each pair shows (verdict table)

| Situation | Shown |
| --- | --- |
| different `match_rules_version` | «несравнимы: разные версии правил (vA ≠ vB)», no numbers |
| a selected run is not `done` | selection refused; run listed with status badge, no numbers |
| domain/niche absent in one run's snapshot | «домен/ниша отсутствует в прогоне X», no numbers |
| no common engines | «нет общих движков», no numbers |
| no common queries (or no grounded data) | «нет общих запросов» / «нет grounded-данных», no numbers |
| common queries < 30 | «недостаточно запросов (n общих из X/Y)», no Δ/p/verdict |
| n ≥ 30, p < 0.05 | «изменение значимо (рост/падение)», Δ pp, p, n, discordant, shares |
| n ≥ 30, p ≥ 0.05 | «в пределах шума», Δ pp, p, n, discordant, shares |

Every numeric cell also carries `nq`/`n` and the channel label, and the
comparison header states the common-engine restriction and the
common-subset basis of Δ.

## 7. Worked example (synthetic, no client data)

Niche `n`, domain `example.com`, both runs `done`, rules v2, engines
{gemini, openai} in both, grounded channel. 32 common queries. In run A
20 queries hit (rate 1) and 12 miss (rate 0); in run B the same 20 hit,
plus 6 of the 12 now hit. Then `d_q = +1` for 6 queries, `0` for 26.
`share_A = 20/32 = 62.5%`, `share_B = 26/32 = 81.25%`, `Δ = +18.8 pp`
(assuming one sample per query for simplicity). `T_obs = 6/32 = 0.1875`.
Sign-flip permutations rarely reach a mean that large with only 6
non-zero differences out of 32 → small p → «изменение значимо (рост)».
If instead only 2 of the 12 flipped, `T_obs = 2/32`, permutations reach
that easily → large p → «в пределах шума», even though Δ is positive.
With 20 common queries the same data would show «недостаточно запросов»,
no number.

## 8. History view (after approval)

On the brand page, per niche × domain:

- **Table** of `done` runs (oldest → newest): run date, share, 95% CI and
  method from `sov_stats.share_ci` over that run's own ok grounded rows
  (via `run_rows`), `nq`, `n`. Non-`done` runs are not in this table; they
  remain in the runs list with a status badge and no figures.
- **Chart**: share over run date with the CI as a band/error bars
  (Chart.js, the same CDN pattern the dashboard already uses; offline
  caveat noted). One line per domain within a niche, or a selector.
- A **comparison** control: pick run A and run B (both `done`, same rules
  version) → the section 6 table per niche × domain.

## 9. Reproducibility and parameters

- `α = 0.05` two-sided; `B = 10000` permutations; `seed = 0`; common-query
  floor `30` (= `MIN_BOOTSTRAP_QUERIES`). All quoted in the UI methodology
  note and in this doc, so a reported verdict is reproducible from the
  same two snapshots.
- Pure functions over the CSV-shaped rows from `run_rows` (no I/O, no
  `queries.py`), living alongside `sov_stats.py` — testable on synthetic
  rows, no keys, no network.

## 10. Deliberately not done

- No causal claim: a significant change is «the measured share moved
  beyond sampling noise between these two runs», not «the brand became
  more visible in the world» (model drift, geo, seasonality are
  confounders the product already records but does not control).
- No comparison across rules versions, ever.
- No pooling of grounded and ungrounded into the test channel (section 2).
- No new dependency (no scipy); the lock stays as pinned.

## Open decisions for the reviewer

1. **Channel**: grounded-only (proposed) vs all ok answers.
2. **Common-engine restriction** for the paired test (proposed) vs pooled
   engines with a warning.
3. **α / B / seed**: 0.05 / 10000 / 0 (proposed, matching sov_stats seed).
4. **Test**: seeded sign-flip permutation (proposed) vs Wilcoxon vs
   paired t.

Implementation (history table + chart, comparison UI, tests on both
Python versions, real-browser check) starts only after these are agreed.
