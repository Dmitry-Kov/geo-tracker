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
- Cross-citation tracking: every answer is checked against **all** tracked
  domains, not just the niche's primary one
- Append-only CSV history — interrupted runs lose nothing, every run is a
  new data point on the trend
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

## How it works

`run` iterates the query matrix (niche × query × engine) and appends one row
per answer to `results.csv` immediately, so an interrupted run keeps
everything collected so far.

**Engines.** Each engine adapter returns the same shape — answer text, source
URLs, source titles:

- **Gemini** — `generateContent` with the `google_search` tool. Source URIs
  in `groundingChunks` are Google redirect links that hide the real host, so
  domains are matched against `web.title` (which usually carries the source
  host) and the answer text.
- **Perplexity** — `chat/completions` with the `sonar` model; sources come
  from `citations` and `search_results`.
- **OpenAI** — Responses API with the `web_search` tool; sources come from
  `url_citation` annotations.

**Matching.** For every tracked domain in every answer:

- `src` — the domain appears among the answer's sources/citations (the
  strong signal: the AI relied on you);
- `txt` — the domain is mentioned in the answer text (the AI recommends
  you).

URL matching is exact-host (subdomains count, lookalikes don't); text
matching uses word boundaries.

**Share of voice** — the percentage of a niche's queries where at least one
of the two signals fired. The dashboard shows the per-niche visibility
matrix, the trend across run dates, a per-engine breakdown and the queries
that actually produced hits.

AI answers are non-deterministic: the same query gives different answers on
different days. Watch the trend across several runs, not a single
measurement.

## Commands

### `run` — query the engines

```bash
python geo_tracker.py run [--engines LIST] [--niches LIST] [--limit N] [--sleep SEC] [--resume]
```

| Flag | Meaning |
|---|---|
| `--engines` | comma-separated subset: `gemini,perplexity,openai` (default: all with keys) |
| `--niches` | comma-separated subset of niche keys from `queries.py` |
| `--limit N` | max queries per niche — cheap smoke tests |
| `--sleep SEC` | pause between API calls, default 2 |
| `--resume` | skip (engine, niche, query) triples that already have an ok row in **any** prior run — fills an unfinished matrix without re-spending quota on duplicates |

Errors don't abort the run: the row is recorded with an `error:` status and
the run continues. On a daily-quota 429 (`RESOURCE_EXHAUSTED` with a
`PerDay` quota id) the engine is dropped from the run after one
server-suggested retry; finish the remainder later with `--resume`.

### `report` — summary and dashboard

```bash
python geo_tracker.py report [--out PATH]
```

Prints the share-of-voice matrix and domain leaderboard for the latest run
and writes `dashboard.html` — a single self-contained file (Chart.js loads
from a CDN, so opening it needs internet).

### `manual-export` / `manual-import` — engines without an API

```bash
python geo_tracker.py manual-export --engine chatgpt [--limit N]
# → manual_chatgpt_DATE.csv: run the queries by hand in a regular browser,
#   fill domains_in_sources / domains_in_text with comma-separated domains
python geo_tracker.py manual-import manual_chatgpt_DATE.csv
```

Imported rows join the shared history and show up in all reports alongside
API engines.

### DOCX export

```bash
python report_docx.py [--out PATH]   # requires python-docx
```

Builds a formatted DOCX report from the latest run: KPI row, visibility
matrix, engine comparison, auto-derived findings.

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
`OPENAI_MODEL` in `.env`.

## Roadmap

Ideas, not promises:

- more engines as citation APIs appear (Grok, Claude web search)
- per-engine trend charts on the dashboard
- query auto-suggestion from niche keywords

## License

[MIT](LICENSE)
