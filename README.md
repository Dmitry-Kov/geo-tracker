# GEO Tracker

Tracks how often your domains are cited in AI answers (Generative Engine
Optimization monitoring). Runs a fixed matrix of queries against AI engines
with web search — Gemini, Perplexity, OpenAI — and records whether your
domains show up in the answer sources or text, so you can measure AI
visibility per niche over time instead of guessing.

Official APIs only, no browser automation: nothing to break, no bot
detection to fight.

## Features

- Three engines with web search via official APIs: Gemini (Google Search
  grounding), Perplexity Sonar, OpenAI Responses (`web_search` tool)
- Engines enable themselves based on which API keys are present in `.env`
- Manual checklist mode (CSV export/import) for engines without a citation
  API: ChatGPT, Yandex Neuro
- Two signals per domain per answer: cited as a **source** (`src`) and
  **mentioned in the answer text** (`txt`)
- Exact-host and word-boundary matching — `pc.uz` matches `www.pc.uz` but
  not `1pc.uz`
- Optional brand aliases: counts a domain as mentioned when the engine names
  the brand in prose without linking it (`txt` only, never `src`)
- Cross-citation tracking: every answer is checked against **all** tracked
  domains, not just the niche's primary one
- Grounded vs ungrounded split: answers that came with no sources at all
  (the engine skipped its search tool and replied from parametric memory)
  are reported as a separate channel instead of diluting share of voice
- Audit-grade provenance: every CSV row and raw receipt records the
  **model**, the **requested geo** (and whether the engine actually applied
  it), and the **sample number** — model drift and locality are visible
- Repeated sampling (`run --samples N`): N answers per cell, so visibility
  can be reported as a distribution instead of one non-deterministic answer
- Share of voice with a **95% confidence interval** (console, HTML dashboard,
  DOCX) that counts evidence per query, not per answer, because repeated
  samples of one query are correlated: a Wilson interval over queries for
  small query sets, a seeded cluster bootstrap for large ones. Reproducible
  from the same rows
- Geo axis: `run --geo CC` is applied by OpenAI and Perplexity (country-level
  `user_location`); the Gemini adapter sends no locality yet, so for Gemini
  the geo is recorded as requested, not applied — a documented limitation of
  this tracker
- Every source domain is recorded, tracked or not (`all_source_domains`), so
  you can see who the engines cite *instead* of you
- Raw answers archived to `answers.jsonl`, keyed to their CSV row — re-read
  what an engine actually said without burning quota on a re-query that
  would return something different anyway
- Optional article tracking (`ARTICLES`): checks whether specific pages —
  placed articles, landing pages — are among an answer's sources, not just
  their domain. A big portal being cited doesn't mean your page was
- Gemini's `src` is matched against `web.title` (its citation URIs are
  opaque Google redirects), deliberately NOT against resolved links, so
  historical runs stay comparable. Since matching rules v2, titles get
  the same subdomain semantics as URLs (`uz.wikipedia.org` counts toward
  `wikipedia.org`); under v1 Gemini undercounted subdomain-heavy domains.
  Every service run snapshots the rules version in `Run.config`. The
  article check is not limited this way:
  with `ARTICLES` set, Gemini's redirects are resolved during the run
- Append-only CSV history — interrupted runs lose nothing, every run is a
  new data point on the trend. `run` and `manual-import` refuse to append to
  a `results.csv` whose columns no longer match `DOMAINS`/`ARTICLES`, instead
  of silently shifting values under the wrong headers
- Quota-aware: parses Google's 429 `QuotaFailure` details, stops the engine
  on daily-quota exhaustion instead of burning retries, `--resume` finishes
  the run later without duplicates
- Reports: console summary, self-contained HTML dashboard (Chart.js),
  DOCX export

## Quick Start

```bash
pip install requests                # optional, for DOCX export: pip install python-docx
cp .env.example .env                # add your API keys
cp queries.example.py queries.py    # add your domains, niches and queries

# Smoke test: one engine, 2 queries per niche
python geo_tracker.py run --engines gemini --limit 2

# Full run across all engines that have keys
python geo_tracker.py run

# Console summary + dashboard.html
python geo_tracker.py report
```

The fastest free start is a single Gemini key from
[aistudio.google.com](https://aistudio.google.com) (no credit card; free
tier includes a Google Search grounding quota).

`queries.py` and `.env` are local configuration and are gitignored.

## What exactly is measured

The tracker queries the **official APIs with web search enabled**
(Gemini `generateContent` + `google_search`, Perplexity Sonar, OpenAI
Responses + `web_search`). That is not the same product a human sees in
the ChatGPT app, the Gemini app, or Google's AI Overviews: those carry
their own retrieval pipelines, ranking, personalization and UI. The
numbers here describe **API behavior for a fixed model, geo and date** —
a reproducible measurement contract, not a screenshot of what any
particular user was shown. Model aliases resolve to dated versions
(recorded per answer), engines decide themselves whether to search at
all (ungrounded answers are reported as a separate channel), and
repeated samples of one query are correlated — hence the query-clustered
confidence intervals.

## How it works

`run` iterates the query matrix (niche × query × engine) and appends one row
per answer to `results.csv` immediately, so an interrupted run keeps
everything collected so far.

**Engines.** Each engine adapter returns the same shape — answer text, source
URLs, source titles:

- **Gemini** — `generateContent` with the `google_search` tool. Source URIs
  in `groundingChunks` are Google redirect links that hide the real host, so
  domains are matched against `web.title` (which usually carries the source
  host) and the answer text. The redirects expire within weeks, so when
  `ARTICLES` is set they are resolved right after each answer and stored as
  `resolved_urls` in `answers.jsonl`; only the article check uses them.
- **Perplexity** — `chat/completions` with the `sonar` model; sources come
  from `citations` and `search_results`.
- **OpenAI** — Responses API with the `web_search` tool; sources come from
  `url_citation` annotations.

**Matching.** For every tracked domain in every answer:

- `src` — the domain appears among the answer's sources/citations (the
  strong signal: the AI relied on you);
- `txt` — the domain is mentioned in the answer text (the AI recommends
  you).

URL matching is exact-host (subdomains count, lookalikes don't). Text and
source-title matching (rules v2) uses the same host semantics:
`shop.brand.io` counts as `brand.io`; `1pc.uz` and `my-brand.io` don't;
`brand.io.evil.com` doesn't (a dot followed by more name is a longer
host), while a sentence-final dot ("Go to brand.io.") does count;
`https://brand.io/pricing` inside text counts. Brand aliases match on
word boundaries, case-insensitive ("Apex" hits "try Apex today", not
"apexcharts").

**Matching rules version.** `MATCH_RULES_VERSION` (currently **2**) is
snapshotted into every service run's `Run.config`. v1 → v2 (2026-11-17):
subdomains now count in text and source titles, a dot followed by a name
continuation no longer counts, and aliases moved from substring to
word-boundary matching. The rules change the numbers, so runs measured
under different versions are not directly comparable; service snapshots
taken before v2 lack the key and read as version 1.

For every page in `ARTICLES`, `art_<key>` is 1 when that page is among the
answer's sources. Pages are compared by host and path, ignoring the scheme,
`www.`, query strings (engines append `?utm_source=…`), fragments and the
trailing slash. Keys become column names, so, as with `DOMAINS`, editing
the list means starting a new `results.csv`.

**Share of voice** — the percentage of a niche's queries where at least one
of the two signals fired. The dashboard shows the per-niche visibility
matrix, the trend across run dates, a per-engine breakdown and the queries
that actually produced hits.

AI answers are non-deterministic: the same query gives different answers on
different days. Watch the trend across several runs, not a single
measurement.

**Provenance and sampling.** Every row carries `model`, `geo`, `geo_applied`
and `sample` columns (and the raw receipt carries them too): which model
answered, which country was requested, whether the engine applied it, and
which repeat sample this is. `run --samples N` collects N answers per cell
(engine × niche × query); `--geo CC` requests a country (ISO alpha-2). OpenAI
(`web_search` → `user_location`) and Perplexity (`web_search_options` →
`user_location`) receive it as a country-level hint, `geo_applied=1`. The
Gemini adapter sends no locality — not implemented here — so Gemini rows
record the geo with `geo_applied=0`. `model` is the model the API reports
having served the answer (Gemini `modelVersion`, OpenAI/Perplexity `model`),
falling back to the requested name; the receipt also keeps `model_requested`.

**Resource usage (COGS evidence).** Each receipt archives the API's own usage
report as-is: tokens (Gemini `usageMetadata`, OpenAI/Perplexity `usage`), the
number of executed searches (Gemini `webSearchQueries`; OpenAI
`web_search_call` actions of type `search` — `open_page`/`find_in_page` are
browsing, not searches; Sonar reports no count → `null`), and — for
Perplexity — the actual billed cost (`usage.cost.total_cost`). `report`
prints a per-engine summary for the latest run. No price tables or guesses:
only what the APIs themselves reported; older receipts without usage show
`—`, not fabricated zeros. The summary is a **lower bound**: only successful
answers are counted — failed calls and HTTP retries never reach a receipt
and are excluded.

**Measurement uncertainty.** A single "17% mention share" is false precision.
Alongside the point matrix, the reports show the share with a **95%
confidence interval whose unit of evidence is the query, not the answer** —
repeated samples of one query are correlated and must not be counted as
independent. Every interval carries `nq`/`n` (queries/samples). The estimator
depends on how many queries stand behind the cell:

- **fewer than 30 queries, or a share of exactly 0% / 100%** — a Wilson score
  interval with n = `nq`: each query is one observation, the conservative
  reading. 0 hits across 10 queries reads `0.0% [0.0–27.8]`, one hit in 30
  samples over the same 10 queries `3.3% [0.2–32.3]`. A bootstrap is not
  used here: on a handful of clusters it comes out too narrow, and when every
  query agrees it collapses to a falsely certain `[0.0–0.0]`.
- **30 queries or more** — a percentile cluster bootstrap over queries
  (B=2000 resamples, seed=0): whole queries are resampled with all their
  samples, so the interval also reflects how much the queries differ.

Both are reproducible from the same rows. Practical consequence: with a
typical 5–15 queries per niche the interval is wide and **narrows with more
queries, not with more samples** — `--samples` steadies the point share.
The audit section covers all
grounded samples of the latest run; the legacy snapshot matrix (freshest
answer per pair) keeps its semantics.

**Provenance columns and old histories.** `model`, `geo`, `geo_applied`,
`sample` were inserted after `query`, so the schema guard refuses to append
to a pre-audit `results.csv`. To keep an old history, migrate it once — old
rows get empty provenance, which is the truth about them:

```bash
python3 - <<'EOF'
import csv
path = "results.csv"
with open(path, newline="", encoding="utf-8-sig") as f:
    reader = csv.DictReader(f)
    rows, old = list(reader), reader.fieldnames
i = old.index("query") + 1
new = old[:i] + ["model", "geo", "geo_applied", "sample"] + old[i:]
with open(path, "w", newline="", encoding="utf-8-sig") as f:
    writer = csv.DictWriter(f, fieldnames=new)
    writer.writeheader()
    writer.writerows(rows)
EOF
```

## Commands

### `run` — query the engines

```bash
python geo_tracker.py run [--engines LIST] [--niches LIST] [--limit N] [--sleep SEC] [--samples N] [--geo CC] [--resume]
```

| Flag | Meaning |
|---|---|
| `--engines` | comma-separated subset: `gemini,perplexity,openai` (default: all with keys) |
| `--niches` | comma-separated subset of niche keys from `queries.py` |
| `--limit N` | max queries per niche — cheap smoke tests |
| `--sleep SEC` | pause between API calls, default 2 |
| `--samples N` | answers per cell (engine × niche × query), default 1. Each sample is a separate row + receipt and spends quota; repeated samples steady the point share (the CI narrows with more queries, not more samples) |
| `--geo CC` | requested country, ISO alpha-2 (e.g. `UZ`). Applied by OpenAI and Perplexity (`user_location`); for Gemini it is only recorded — this tracker sends Gemini no locality |
| `--resume` | count ok rows per (engine, niche, query) triple **with the same `--geo`** across all prior runs and fetch only what is missing up to `--samples` — fills an unfinished matrix (including a cell cut off mid-samples) without re-spending quota on duplicates |

Errors don't abort the run: the row is recorded with an `error:` status and
the run continues. On a daily-quota 429 (`RESOURCE_EXHAUSTED` with a
`PerDay` quota id) the engine is dropped from the run after one
server-suggested retry; finish the remainder later with `--resume`.

### `report` — summary and dashboard

```bash
python geo_tracker.py report [--out PATH]
```

Prints the share-of-voice matrix and domain leaderboard for the latest run,
plus a **measurement-uncertainty** table (95% CI per
niche × domain, with `nq`/`n`, methodology and provenance: models used,
requested geo and which engines applied it) and a **resource-usage** summary
per engine (calls, tokens, executed searches, API-reported cost — from the
raw receipts), and writes `dashboard.html` —
a single self-contained file (Chart.js loads from a CDN, so opening it needs
internet) with the same audit section.

### `manual-export` / `manual-import` — engines without an API

```bash
python geo_tracker.py manual-export --engine chatgpt [--limit N]
# → manual_chatgpt_DATE.csv: run the queries by hand in a regular browser,
#   fill domains_in_sources / domains_in_text with comma-separated domains
python geo_tracker.py manual-import manual_chatgpt_DATE.csv
```

Imported rows join the shared history and show up in all reports alongside
API engines. With `ARTICLES` set, the checklist gets an `articles_in_sources`
column: list the article keys, or just paste the URLs you saw among the
sources.

### DOCX export

```bash
python report_docx.py [--out PATH]   # requires python-docx
```

Builds a formatted DOCX report from the latest run: KPI row, visibility
matrix, measurement uncertainty (95% CI + methodology + provenance), article
citations (when `ARTICLES` is set), engine comparison, auto-derived findings.

### Scheduled runs

```bash
# weekly run on Mondays at 06:00 + fresh dashboard
crontab -e
0 6 * * 1 cd /opt/geo-tracker && python3 geo_tracker.py run && python3 geo_tracker.py report
```

The dashboard is one file — serve it with nginx or just `scp` it.

## API limits and cost

| Engine | Limits / cost (for a ~55-query run) |
|---|---|
| Gemini | Free tier: **~20 grounded requests/day per model** (quota `GenerateRequestsPerDayPerProjectPerModel-FreeTier`) — a full run takes ~3 days with `--resume`, or enable billing |
| Perplexity | Prepaid credits; Pro subscription includes $5/mo which covers weekly runs, otherwise ~$0.5–1 per run on `sonar` |
| OpenAI | Pay-as-you-go, cents per run on `gpt-5-mini`; requires a card |
| ChatGPT / Yandex Neuro | free by hand via manual mode, ~15 min/week |

Models are overridable via `GEMINI_MODEL`, `PERPLEXITY_MODEL`,
`OPENAI_MODEL` in `.env`. Each row records the model the API reports having
served the answer, so drift behind a moving alias (e.g. `gemini-flash-latest`)
is visible in the history instead of silently changing the numbers. `--samples N` multiplies API spend by N — smoke-test with `--limit`
first.

## Tests

```bash
python3 -m venv --system-site-packages .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest tests/ -q
```

Tests run on synthetic rows and mocked engine calls — no API keys, no quota,
no network. The `queries` config is faked and all file paths are redirected
to a temp dir, so your real `results.csv` / `answers.jsonl` are never touched.
Coverage: interval known-answer/edge cases and reproducibility, provenance
columns and receipts, the schema guard (old/reordered headers), geo payload
handling per engine, sampling/quota/resume in `run`, the console/HTML/DOCX
reports end-to-end, and the service (brand CRUD, runs, auth, scheduler,
exports) on a temp SQLite DB.

## Service (M1 skeleton)

A single-user web service over the same measurement core: many brands,
scheduled runs, receipts and reports in the browser. The CLI keeps working
unchanged alongside it (its history stays in `results.csv`; the service has
its own SQLite history).

```bash
.venv/bin/pip install -r requirements.txt
.venv/bin/uvicorn app.main:app
# open http://127.0.0.1:8000
```

Run it as **one process** — no `--workers`, and no `--reload` while a run is
in flight: the scheduler and the run queue live in the process, and a restart
ends whatever run was active (it is closed as `interrupted` on the next
start, so it never blocks later runs). `queries.py` is CLI configuration;
the service does not need it.

- **Brands**: domains, one or more niches with queries (add/remove niche
  rows right in the form), optional articles
  and aliases (JSON), geo, engines, per-engine model overrides, samples,
  cron schedule. Domains are data, not columns — editing them needs no
  migration.
- **Runs**: one at a time, enforced by the database; live progress;
  per-answer receipts (raw answer, served vs requested model, usage/cost)
  drill down in the browser. Each run stores a snapshot of the brand's
  configuration: results and exports of a finished run never change when the
  brand is edited later.
- **Results**: visibility matrix, the same audit-grade 95% CI (Wilson /
  cluster bootstrap), resource-usage summary; export the branded HTML
  dashboard or DOCX (en/ru) per run.
- **Auth**: set `APP_PASSWORD` in `.env` to require login (session cookie);
  unset = local mode without auth, which answers on `localhost` /
  `127.0.0.1` only. To serve another host name set `APP_ALLOWED_HOSTS`
  (comma-separated) and a password. State-changing requests sent by a browser
  from another site are refused (Origin/Referer must match the Host — behind
  a reverse proxy, forward the original `Host` header). `APP_SECRET` pins the
  session key across restarts. `GEO_DB_PATH` moves the SQLite file (default
  `geo_tracker.db`, gitignored — it contains client domains).
- **Login hardening**: 5 failed logins within 15 minutes from one address
  block further attempts (HTTP 429 + `Retry-After`) until the oldest failure
  leaves the window; a successful login resets the count. The limit is
  in-memory per process. Behind a reverse proxy run uvicorn with
  `--proxy-headers --forwarded-allow-ips=<proxy address>` so the limiter sees
  real client addresses (raw `X-Forwarded-For` is never trusted). Set
  `APP_HTTPS_ONLY=1` when TLS terminates in front of the service: the session
  cookie gets the `Secure` flag and browsers will not send it over plain HTTP.
- **Scheduler**: APScheduler cron per brand, **in UTC** (`0 9 * * 1` =
  Mondays 09:00 UTC). Scheduled runs queue: brands on the same tick run one
  after another, and a tick that lands on a manual run waits for it. A tick
  that still cannot start after 6 hours is recorded on the brand as a skipped
  run. Schedules are not persisted across downtime — a tick missed while the
  service was stopped is not replayed.

Slice-1 limits, stated honestly: one run
at a time, single user, single process (the login limiter, the run queue and
the scheduler live in it). Trends across runs, users and
billing are the next slices.

## Roadmap

Ideas, not promises:

- more engines as citation APIs appear (Grok, Claude web search)
- per-engine trend charts on the dashboard
- query auto-suggestion from niche keywords

## License

[MIT](LICENSE)
