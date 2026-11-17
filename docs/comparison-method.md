# Comparing two runs: method (APPROVED with corrections A–E)

Task 4: the dynamics of share of voice between runs — a per niche × domain
history (table + chart) on the brand page, and a comparison of two selected
runs that says, for each pair, whether the change is **significant** or
**within noise**. Approved with the decisions and corrections below; this
document is the contract the implementation follows.

Built against the `AGENTS.md` guarantees: the unit of evidence is the
**query**; a finished run is read only through its snapshot
(`run_rows(run)` / `run_config(run)`); a non-`done` run never reads like a
full report (`incomplete_notice()`); matching rules are versioned and runs
under different versions are not comparable.

## Decisions (approved)

1. **Channel: grounded-only** (`sov_stats.is_grounded`: the answer has
   sources) — the audit-grade web-visibility channel. Additionally, for
   **both** runs show the **fraction of ok answers that have sources**
   (overall and per engine): if an engine started searching less often,
   that must be visible next to the comparison.
2. **Common engines, matched at (query × engine).** `rate_A(q)` and
   `rate_B(q)` pool only the engines that have ok grounded answers for
   that query in **both** runs. A query is **common** iff at least one such
   engine exists.
3. **α / B / seed = 0.05 / 10000 / 0.**
4. **Test: paired sign-flip permutation** on the per-query rate differences.

## 1. Which runs may be compared (hard gates, correction C)

Read everything from the snapshots (`run_config` / `run_rows`).

- **Only `done` runs.** `cancelled` / `error` / `queued` / `pending` /
  `running` are excluded from history numbers and from the comparison
  selectors. If listed, they carry a status badge and **no figures**. Any
  export of a non-`done` run uses `incomplete_notice()`; the comparison
  refuses non-`done` selections outright.
- **Different `match_rules_version` → the whole pair is incomparable**,
  shown explicitly («несравнимы: разные версии правил vA ≠ vB»). A snapshot
  without the key reads as version 1.
- **Different `geo` → the whole pair is incomparable** («разное geo:
  A=… B=…»), shown explicitly. (Correction C.)
- **Different aliases for a domain → that domain is incomparable**, with
  an explanation listing the two alias sets; other domains still compare.
  (Correction C.)
- **Different served models in the answers → a warning in the comparison
  header**, per engine, as a «было → стало» list (not a gate). (Correction C.)

## 2. Pairing and the per-query rates

For a niche × domain (and for the per-domain overall row, section 5):

- Build `{(niche, query): {engine: (hits, n_samples)}}` over ok grounded
  rows of each run.
- A query is **common** iff some engine appears for it in both runs. Its
  per-run rate pools exactly the common engines:
  `rate_R(q) = Σ hits_R(q,e) / Σ n_R(q,e)` over common engines `e`.
- Paired difference `d_q = rate_B(q) − rate_A(q)`. One observation per
  common query; each query weighs **equally** regardless of sample counts
  (more samples must not buy significance — the sov_stats guarantee).
- Queries in only one run, or with no common engine, are excluded from the
  test and reported as counts (`only in A`, `only in B`).

## 3. The displayed change IS the tested quantity (correction B)

- **Δ shown on screen = mean over common queries of `d_q`**, in percentage
  points (`100 · mean(d_q)`), rounded to 1 decimal. The **direction**
  (рост/падение) comes from the same mean.
- The pooled subset shares `share_A = Σhits_A/Σn_A`, `share_B` (over the
  common query × engine subset) may be shown **as reference only**; their
  difference is **not** called "the change" — it can differ in sign from
  the tested mean when queries have unequal sample counts.
- Worked case (mandatory test): `q1: A 0/1, B 1/1; q2: A 10/10, B 5/10`.
  `d = [+1, −0.5]`, mean `+0.25` → **Δ = +25.0 pp (рост)**. Pooled reference:
  A `10/11 = 90.9%`, B `6/11 = 54.5%`, pooled difference `−36.4 pp` — the
  opposite sign. The screen shows +25.0 pp as the change; 90.9%→54.5% is
  labeled reference, not the change.

## 4. Significance: permutation test + Holm (corrections A, E)

**Gate: common queries < 30 → «недостаточно запросов (n общих из X/Y)», no
number** (mirrors `MIN_BOOTSTRAP_QUERIES = 30`). No Δ/p/verdict below it.

For n ≥ 30, a **seeded paired sign-flip permutation test** on the `d_q`:

- Null: the run label carries no information; each `d_q`'s sign is
  exchangeable. Statistic `T = |Σ d_q|` (≡ `|mean(d_q)|` for fixed n).
- `B = 10000` independent sign-flips, `seed = 0` (reproducible from the same
  rows — the same evidence contract as the seeded bootstrap). Zero
  differences flip to zero (ties handled).
- Two-sided p with add-one correction: `p = (1 + #{T_perm ≥ T_obs})/(1 + B)`.
- **The count is exact.** Per-query rates are ratios of small counts, so many
  re-signed sums are *exactly* equal to the observed one, and those ties
  belong in `#{T_perm ≥ T_obs}`. Summed as floats they differ in the last
  bit and drop out, which can only lower p: on data where a query has a
  different number of grounded answers in the two runs, p came out too low
  in 127 of 150 trials, by up to 0.18. The differences are kept as exact
  rationals and summed as integers on a common denominator.
  All `d_q = 0` → `p = 1`.

**Multiple comparisons (correction A).** With many pairs, an uncorrected
α=0.05 produces false «значимо» often (verified: with 18 pairs and no
change, at least one false positive in ~26% of comparisons at 30 queries,
~37% at 60). Therefore:

- The **family** = all niche rows **plus** the per-domain overall rows of
  one comparison (correction D). Let `m` = number of pairs that produced a
  raw p (gated-out / insufficient pairs are not in the family).
- **Holm–Bonferroni** step-down adjustment across the family: sort raw p
  ascending, `adj_(k) = max_{j≤k} min(1, (m−j+1)·p_(j))`.
- Verdict «изменение значимо» **only if adjusted p < 0.05**; otherwise
  «в пределах шума».
- Show **raw p, adjusted p, and the family size m** for every tested pair.

**Sensitivity caveat (correction E).** «в пределах шума» is always shown
with the number of common queries and this caveat: the test has limited
power at small n. Documented sensitivity for a share rise **20% → 40%**
(approved reference; actual power depends on samples per query and the
per-query hit-rate distribution):

| common queries | change detected |
| --- | --- |
| 30 | 34% |
| 60 | 71% |
| 120 | 96% |

So «в пределах шума» at n=30 means "not detected", not "no change".

## 5. Per-domain overall row (correction D)

For each domain, an **overall row across all niches**: the same rules, with
common queries collected across all niches (observations are `(niche,
query)` pairs, matched at query × engine). The **30-common-query floor
applies to the overall row too**. Niche rows and overall rows belong to the
**same Holm family**.

## 6. What each pair shows

| Situation | Shown |
| --- | --- |
| non-`done` selection | refused; listed with status badge, no figures |
| different rules version | «несравнимы: разные версии правил (vA ≠ vB)», whole pair, no numbers |
| different geo | «несравнимы: разное geo (A=… B=…)», whole pair, no numbers |
| domain absent in one snapshot | «домен отсутствует в прогоне X», no numbers |
| domain aliases differ | «домен несравним: разные псевдонимы», both sets listed, no numbers |
| no common queries / no grounded data | «нет общих запросов» / «нет grounded-данных», no numbers |
| common < 30 | «недостаточно запросов (n общих из X/Y)», no Δ/p/verdict |
| n ≥ 30, adj p < 0.05 | «изменение значимо (рост/падение)»: Δ pp (tested mean), raw p, adj p, m, n, discordant, reference shares |
| n ≥ 30, adj p ≥ 0.05 | «в пределах шума»: Δ pp, raw p, adj p, m, n + sensitivity caveat |

Header also shows: both runs' grounded fractions (overall + per engine),
the models «было → стало» warning if any, the common-engine restriction,
and that Δ is the mean paired difference over the common subset.

## 7. History view

On the brand page, per niche × domain:

- **Table** of `done` runs (oldest → newest): run date, share, 95% CI and
  method via `sov_stats.share_ci` over that run's own ok grounded rows
  (`run_rows`), `nq`, `n`. Non-`done` runs are not in this table.
- **Chart**: share over run date (the CI is in the table). **Chart.js is
  vendored in `app/static/`** (like htmx), not loaded from a CDN.
- **Breaks in comparability are marked.** A run whose matching-rules
  version, geo or aliases for that domain differ from the previous run of
  the series is flagged «not comparable» in the table with the reason, and
  the chart draws the segment leading to it dashed and grey. These are the
  conditions the comparison gates on (C); without the mark the chart would
  draw a change of measurement as a trend — adding one alias moved a domain
  from 0% to 100% in the test data.
- The figures of a done run are computed once and cached: the run and its
  snapshot never change.
- **Comparison** control: pick run A and run B (both `done`) → section 6.

## 8. Reproducibility, dependencies, scope

- Parameters quoted in the UI methodology note: α=0.05, B=10000, seed=0,
  floor 30, Holm across the family. A verdict is reproducible from the same
  two snapshots.
- Pure functions over `run_rows` rows + `run_config` snapshots, in
  `app/dynamics.py` beside `sov_stats.py`. **No new dependency** (no scipy);
  the lock stays as pinned. Chart.js vendored, no network at runtime.
- No causal claim: «значимо» = the measured share moved beyond sampling
  noise between these two runs, not a claim about the world (model drift,
  geo, seasonality are recorded confounders, not controlled).
- No comparison across rules versions or geo, ever.

## 9. Mandatory tests

- No change → false «значимо» rate over a family of 18 pairs ≤ 5%
  (simulation, fixed seed).
- The section 3 sign-divergence example (tested Δ +25.0 pp vs pooled
  −36.4 pp).
- All section 1 gates (status, rules version, geo, aliases) + models warning.
- Non-`done` runs appear in neither history nor comparison.
- Power trend sanity (increases with n; modest at 30, high at 120) — the
  exact 34/71/96% table is the approved reference, assumption-dependent.
