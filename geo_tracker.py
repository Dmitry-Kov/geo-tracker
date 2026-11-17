#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
GEO tracker — monitors how often domains are cited in AI answers.

Architecture: official APIs only, no browser automation.
Engines enable themselves based on keys in .env / environment variables:

  GEMINI_API_KEY      — Gemini + Google Search grounding (free tier, aistudio.google.com)
  PERPLEXITY_API_KEY  — Perplexity Sonar (Pro subscription credits or pay-as-you-go)
  OPENAI_API_KEY      — OpenAI Responses API + web_search (paid, cents on mini models)

Commands:
  run            — run the query matrix, append results to results.csv
  report         — console summary + HTML dashboard (dashboard.html)
  manual-export  — export a CSV checklist for manual runs (ChatGPT, Yandex Neuro)
  manual-import  — merge a filled checklist into the shared history

Examples:
  python geo_tracker.py run
  python geo_tracker.py run --engines gemini --niches tech,medical --limit 3
  python geo_tracker.py run --samples 3 --geo UZ   # repeated samples + provenance
  python geo_tracker.py report
  python geo_tracker.py manual-export --engine chatgpt
  python geo_tracker.py manual-import manual_chatgpt_2026-06-11.csv
"""

import argparse
import csv
import json
import os
import re
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote, urlparse

import requests

try:
    from queries import DOMAINS, NICHES
except ImportError:
    # queries.py is local, gitignored config. The CLI needs it (main() says
    # so); the service passes per-brand config explicitly and runs without it.
    DOMAINS, NICHES = [], {}

try:
    # Optional {domain: [brand name, ...]} map — engines often name a brand in
    # the answer text without ever linking its domain. Lives in queries.py
    # (local config) so client brand names stay out of the repo.
    from queries import BRAND_ALIASES
except ImportError:
    BRAND_ALIASES = {}
try:
    # Optional {key: {"title": ..., "url": ...}} — specific pages (placed
    # articles, landing pages) checked at URL level on top of the domain
    # signals: a big portal being cited doesn't mean *our* page was.
    from queries import ARTICLES
except ImportError:
    ARTICLES = {}
from report_html import build_dashboard
from sov_stats import (sov_stats, format_ci, DEFAULT_B, DEFAULT_SEED,
                       MIN_BOOTSTRAP_QUERIES)

BASE_DIR = Path(__file__).resolve().parent
RESULTS_CSV = BASE_DIR / "results.csv"
DASHBOARD_HTML = BASE_DIR / "dashboard.html"
ANSWERS_JSONL = BASE_DIR / "answers.jsonl"

TIMEOUT = 90
RETRIES = 4
BACKOFF = (3, 8, 20)  # seconds between attempts on network/other errors
THROTTLE_BACKOFF = (10, 30, 60)  # waits on 429 (rate limit) and 503 (high demand)

# ---------------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------------

def load_env() -> None:
    """Load .env next to the script without overriding already-set variables."""
    env_path = BASE_DIR / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


# ---------------------------------------------------------------------------
# Domain matching
# ---------------------------------------------------------------------------

# Bumped whenever the matching rules change, so runs stay comparable:
# Run.config snapshots it, and cross-run comparisons refuse to mix versions.
# v1: text mentions required a bare domain (no subdomains), a trailing dot
#     could continue into a longer host, aliases matched as substrings.
# v2: subdomains count in text/titles like in URLs; domain + "." + name
#     continuation is rejected; aliases match on word boundaries.
MATCH_RULES_VERSION = 2


def domain_in_url(domain: str, url: str) -> bool:
    """Exact host match: pc.uz matches pc.uz and www.pc.uz, but not 1pc.uz."""
    try:
        netloc = urlparse(url if "://" in url else "https://" + url).netloc.lower()
    except ValueError:
        return False
    netloc = netloc.split(":")[0]
    return netloc == domain or netloc.endswith("." + domain)


def domain_in_text(domain: str, text: str) -> bool:
    """Domain mention in text or source titles, same host semantics as URLs:
    shop.brand.io counts as brand.io; 1pc.uz and my-brand.io do not;
    brand.io.evil.com does not (dot + name continuation), while a
    sentence-final dot ("Go to brand.io.") does count."""
    pattern = (r"(?<![\w-])" + re.escape(domain) + r"(?!\.\w)(?![\w-])")
    return re.search(pattern, text, flags=re.IGNORECASE) is not None


def alias_in_text(alias: str, text: str) -> bool:
    """Brand alias on word boundaries, case-insensitive: 'Apex' matches
    'try Apex today' but not 'apexcharts'."""
    pattern = r"(?<!\w)" + re.escape(alias) + r"(?!\w)"
    return re.search(pattern, text, flags=re.IGNORECASE) is not None


def match_domains(answer_text: str, source_urls: list, source_titles: list, *,
                  domains: list = None, aliases: dict = None) -> dict:
    """For each domain: src (among sources) and txt (in the answer text).

    domains/aliases default to the queries.py config (CLI); the service
    passes per-brand config explicitly.
    """
    domains = DOMAINS if domains is None else domains
    aliases = BRAND_ALIASES if aliases is None else aliases
    hits = {}
    titles_blob = " ".join(source_titles)
    for d in domains:
        src = any(domain_in_url(d, u) for u in source_urls) or domain_in_text(d, titles_blob)
        txt = domain_in_text(d, answer_text) or any(
            alias_in_text(alias, answer_text)
            for alias in aliases.get(d, []))
        hits[d] = {"src": int(src), "txt": int(txt)}
    return hits


def normalize_host(value: str) -> str:
    """Bare hostname from a URL or an already-bare domain string (Gemini's web.title)."""
    v = (value or "").strip()
    if not v:
        return ""
    try:
        netloc = urlparse(v if "://" in v else "https://" + v).netloc.lower()
    except ValueError:
        return ""
    netloc = netloc.split(":")[0]
    return netloc[4:] if netloc.startswith("www.") else netloc


def extract_source_domains(engine: str, urls: list, titles: list) -> list:
    """All source hostnames in an answer, tracked or not (for all_source_domains)."""
    values = titles if engine == "gemini" else urls
    seen, out = set(), []
    for v in values:
        host = normalize_host(v)
        if host and host not in seen:
            seen.add(host)
            out.append(host)
    return out


# ---------------------------------------------------------------------------
# Article (page-level) matching
# ---------------------------------------------------------------------------

def normalize_page(url: str) -> str:
    """host + path, ignoring scheme, www, query (?utm_source=…), fragment and trailing slash."""
    host = normalize_host(url)
    if not host:
        return ""
    path = urlparse(url if "://" in url else "https://" + url).path
    return host + unquote(path).rstrip("/").lower()


def match_articles(source_urls: list, *, articles: dict = None) -> dict:
    """For each tracked article: 1 if its page is among the answer's sources."""
    articles = ARTICLES if articles is None else articles
    pages = {normalize_page(u) for u in source_urls}
    return {key: int(normalize_page(cfg["url"]) in pages) for key, cfg in articles.items()}


def article_key(value: str) -> str:
    """Article key from either the key itself or any URL form of its page ("" if unknown)."""
    if value in ARTICLES:
        return value
    page = normalize_page(value)
    return next((key for key, cfg in ARTICLES.items()
                 if page and normalize_page(cfg["url"]) == page), "")


def resolve_redirect(url: str) -> str:
    """Target of a redirect link, one hop, without fetching the page ("" if not a redirect)."""
    try:
        with requests.get(url, allow_redirects=False, timeout=15, stream=True) as resp:
            return resp.headers.get("Location", "") if resp.is_redirect else ""
    except requests.RequestException:
        return ""


def resolve_gemini_sources(answer: dict) -> int:
    """Add resolved_urls (the real source pages) to a Gemini answer; returns how many failed.

    Gemini's groundingChunks uris are Google redirects that expire (404 within
    weeks), so they can only be resolved during the run. The resolved URLs feed
    the article check only: domain src under Gemini stays on web.title so
    historical runs remain comparable.
    """
    answer["resolved_urls"] = [resolve_redirect(u) for u in answer["urls"]]
    return answer["resolved_urls"].count("")


# ---------------------------------------------------------------------------
# Engines
# ---------------------------------------------------------------------------

class QuotaExhausted(RuntimeError):
    """The engine's daily quota is exhausted — retrying is pointless."""


def _parse_429(body: str) -> tuple:
    """From a Google 429 body: (quota names from QuotaFailure, RetryInfo delay in sec)."""
    quotas, delay = [], None
    try:
        details = json.loads(body).get("error", {}).get("details", [])
    except ValueError:
        return quotas, delay
    for d in details:
        t = d.get("@type", "")
        if t.endswith("QuotaFailure"):
            for v in d.get("violations", []):
                q = v.get("quotaId") or v.get("quotaMetric") or "?"
                lim = v.get("quotaValue")
                quotas.append(f"{q} (limit {lim})" if lim else q)
        elif t.endswith("RetryInfo"):
            m = re.match(r"([\d.]+)s", d.get("retryDelay", ""))
            if m:
                delay = float(m.group(1))
    return quotas, delay


def _post_with_retries(url: str, *, headers: dict, payload: dict) -> dict:
    last_err = None
    for attempt in range(RETRIES):
        try:
            resp = requests.post(url, headers=headers, json=payload, timeout=TIMEOUT)
            if resp.status_code == 429:
                body = resp.text.strip()
                quotas, delay = _parse_429(body)
                qname = "; ".join(quotas) or "quota not specified in details"
                daily = (any("PerDay" in q for q in quotas)
                         or ("RESOURCE_EXHAUSTED" in body and not quotas and delay is None))
                # Give a daily quota one chance to sit out a short RetryInfo
                # (the window is sometimes sliding); a repeat 429 stops retries.
                if daily and (attempt >= 1 or delay is None or delay > 120):
                    raise QuotaExhausted(f"daily quota: {qname}")
                wait = (int(delay) + 2 if delay and delay <= 120
                        else THROTTLE_BACKOFF[min(attempt, len(THROTTLE_BACKOFF) - 1)])
                print(f"    429 [{qname}], waiting {wait}s…")
                time.sleep(wait)
                last_err = RuntimeError(f"429: {qname}")
                continue
            if resp.status_code == 503:
                wait = THROTTLE_BACKOFF[min(attempt, len(THROTTLE_BACKOFF) - 1)]
                print(f"    503 high demand, waiting {wait}s…")
                time.sleep(wait)
                last_err = RuntimeError(f"503: {resp.text.strip()[:300]}")
                continue
            resp.raise_for_status()
            return resp.json()
        except (requests.RequestException, ValueError) as e:
            last_err = e
            if attempt < RETRIES - 1:
                time.sleep(BACKOFF[attempt])
    raise RuntimeError(f"request failed after {RETRIES} attempts: {last_err}")


def ask_gemini(query: str, geo: str = "") -> dict:
    """Gemini + Google Search grounding. Sources come from groundingChunks.

    Note: groundingChunks uris are Google redirect links that hide the real
    host, so domains are matched against web.title (usually the source host)
    and the answer text. This adapter sends no locality to Gemini (not
    implemented: google_search has no country-level field), so a requested
    geo is recorded but NOT applied.
    """
    model = os.environ.get("GEMINI_MODEL", "gemini-flash-latest")
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    payload = {
        "contents": [{"parts": [{"text": query}]}],
        "tools": [{"google_search": {}}],
    }
    data = _post_with_retries(
        url,
        headers={"x-goog-api-key": os.environ["GEMINI_API_KEY"],
                 "Content-Type": "application/json"},
        payload=payload,
    )
    candidate = (data.get("candidates") or [{}])[0]
    parts = candidate.get("content", {}).get("parts", [])
    text = " ".join(p.get("text", "") for p in parts)

    urls, titles = [], []
    gm = candidate.get("groundingMetadata", {}) or {}
    for chunk in gm.get("groundingChunks", []) or []:
        web = chunk.get("web", {}) or {}
        if web.get("uri"):
            urls.append(web["uri"])
        if web.get("title"):
            titles.append(web["title"])
    return {"text": text, "urls": urls, "titles": titles,
            "model": data.get("modelVersion") or model, "model_requested": model,
            "usage": data.get("usageMetadata") or {},
            "searches": len(gm.get("webSearchQueries") or [])}


def ask_perplexity(query: str, geo: str = "") -> dict:
    """Perplexity Sonar. Sources: citations + search_results.

    A requested geo IS applied here: Sonar takes
    web_search_options.user_location.country (ISO alpha-2, country-level).
    Usage is archived as reported — Sonar includes the actual cost
    (usage.cost.total_cost) but no search count, so searches stays None.
    """
    model = os.environ.get("PERPLEXITY_MODEL", "sonar")
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": query}],
    }
    if geo:
        payload["web_search_options"] = {"user_location": {"country": geo}}
    data = _post_with_retries(
        "https://api.perplexity.ai/chat/completions",
        headers={"Authorization": f"Bearer {os.environ['PERPLEXITY_API_KEY']}",
                 "Content-Type": "application/json"},
        payload=payload,
    )
    text = (data.get("choices") or [{}])[0].get("message", {}).get("content", "")
    urls = list(data.get("citations") or [])
    titles = []
    for sr in data.get("search_results") or []:
        if sr.get("url"):
            urls.append(sr["url"])
        if sr.get("title"):
            titles.append(sr["title"])
    return {"text": text, "urls": urls, "titles": titles,
            "model": data.get("model") or model, "model_requested": model,
            "usage": data.get("usage") or {}, "searches": None}


def ask_openai(query: str, geo: str = "") -> dict:
    """OpenAI Responses API + web_search. Sources: url_citation annotations.

    A requested geo IS applied here: web_search takes user_location
    (approximate, country-level).
    """
    model = os.environ.get("OPENAI_MODEL", "gpt-5-mini")
    tool = {"type": "web_search"}
    if geo:
        tool["user_location"] = {"type": "approximate", "country": geo}
    payload = {
        "model": model,
        "input": query,
        "tools": [tool],
        "tool_choice": "required",
    }
    data = _post_with_retries(
        "https://api.openai.com/v1/responses",
        headers={"Authorization": f"Bearer {os.environ['OPENAI_API_KEY']}",
                 "Content-Type": "application/json"},
        payload=payload,
    )
    text_parts, urls, titles = [], [], []
    for item in data.get("output") or []:
        if item.get("type") != "message":
            continue
        for content in item.get("content") or []:
            if content.get("type") == "output_text":
                text_parts.append(content.get("text", ""))
                for ann in content.get("annotations") or []:
                    if ann.get("type") == "url_citation":
                        if ann.get("url"):
                            urls.append(ann["url"])
                        if ann.get("title"):
                            titles.append(ann["title"])
    # open_page / find_in_page actions are browsing, not searches (verified live)
    searches = sum(1 for item in data.get("output") or []
                   if item.get("type") == "web_search_call"
                   and (item.get("action") or {}).get("type") == "search")
    return {"text": " ".join(text_parts), "urls": urls, "titles": titles,
            "model": data.get("model") or model, "model_requested": model,
            "usage": data.get("usage") or {}, "searches": searches}


ENGINES = {
    "gemini": {"env": "GEMINI_API_KEY", "fn": ask_gemini},
    "perplexity": {"env": "PERPLEXITY_API_KEY", "fn": ask_perplexity},
    "openai": {"env": "OPENAI_API_KEY", "fn": ask_openai},
}

# Which adapters actually send a requested geo to the engine. OpenAI and
# Perplexity take a country-level user_location; the Gemini adapter sends no
# locality (not implemented), so for Gemini the geo is recorded as requested,
# not applied — a limitation of this tracker, stated rather than assumed away.
GEO_SUPPORTED = {"gemini": False, "perplexity": True, "openai": True}


def available_engines() -> list:
    return [name for name, cfg in ENGINES.items() if os.environ.get(cfg["env"])]


# ---------------------------------------------------------------------------
# CSV: results history
# ---------------------------------------------------------------------------

def csv_fieldnames() -> list:
    fields = ["row_id", "run_date", "run_ts", "engine", "niche", "query",
              "model", "geo", "geo_applied", "sample",
              "status", "answer_chars", "n_sources"]
    for d in DOMAINS:
        fields.append(f"src_{d}")
        fields.append(f"txt_{d}")
    for key in ARTICLES:
        fields.append(f"art_{key}")
    fields.append("all_source_domains")
    return fields


def check_results_schema() -> None:
    """Stop before appending to a results.csv written under a different schema.

    Columns come from the provenance set plus DOMAINS and ARTICLES, and rows
    are appended by position: after a schema change (e.g. the model/geo/sample
    provenance columns) or an edit to either list, new rows would land under
    the wrong headers and silently corrupt the history.
    """
    if not RESULTS_CSV.exists() or RESULTS_CSV.stat().st_size == 0:
        return
    with RESULTS_CSV.open(newline="", encoding="utf-8-sig") as f:
        header = next(csv.reader(f), [])
    fields = csv_fieldnames()
    if header == fields:
        return
    missing = [c for c in fields if c not in header]
    extra = [c for c in header if c not in fields]
    details = (f"missing: {', '.join(missing) or '-'}; extra: {', '.join(extra) or '-'}"
               if missing or extra else "same columns in a different order")
    sys.exit(f"{RESULTS_CSV.name} columns don't match the current schema ({details}).\n"
             f"Appending would shift values under the wrong headers. Start a new history "
             f"(rename {RESULTS_CSV.name}) or migrate it to the current columns first "
             f"(see README: provenance columns).")


def append_row(row: dict) -> None:
    """Append rows one by one — a crash mid-run loses nothing."""
    fields = csv_fieldnames()
    is_new = not RESULTS_CSV.exists() or RESULTS_CSV.stat().st_size == 0
    with RESULTS_CSV.open("a", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        if is_new:
            writer.writeheader()
        writer.writerow(row)


def load_results() -> list:
    if not RESULTS_CSV.exists():
        return []
    with RESULTS_CSV.open(newline="", encoding="utf-8-sig") as f:
        return [row for row in csv.DictReader(f)]


def save_answer(row_id: str, answer: dict) -> None:
    """Persist the full raw answer (text + urls + titles) keyed by row_id.

    results.csv only keeps derived signals; the raw answer used to be
    discarded entirely, which meant re-querying (burning quota, getting a
    different non-deterministic answer) was the only way to read it later.
    """
    with ANSWERS_JSONL.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"row_id": row_id, **answer}, ensure_ascii=False) + "\n")


def build_receipt(answer: dict, *, geo: str = "", geo_applied: int = 0,
                  sample: int = 1) -> dict:
    """The raw answer enriched with run provenance — the archival record."""
    return {**answer, "geo": geo, "geo_applied": geo_applied, "sample": sample}


def make_row(engine: str, niche: str, query: str, *, status: str = "ok",
             answer: dict = None, geo: str = "", geo_applied: int = 0,
             sample: int = 1, domains: list = None, articles: dict = None,
             aliases: dict = None, save_receipt: bool = True) -> dict:
    domains = DOMAINS if domains is None else domains
    articles = ARTICLES if articles is None else articles
    now = datetime.now(timezone.utc)
    row = {
        "row_id": uuid.uuid4().hex[:12],
        "run_date": now.strftime("%Y-%m-%d"),
        "run_ts": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "engine": engine,
        "niche": niche,
        "query": query,
        "model": (answer or {}).get("model", ""),
        "geo": geo,
        "geo_applied": geo_applied,
        "sample": sample,
        "status": status,
        "answer_chars": 0,
        "n_sources": 0,
        "all_source_domains": "",
    }
    for d in domains:
        row[f"src_{d}"] = 0
        row[f"txt_{d}"] = 0
    for key in articles:
        row[f"art_{key}"] = 0
    if answer:
        row["answer_chars"] = len(answer["text"])
        row["n_sources"] = len(answer["urls"])
        hits = match_domains(answer["text"], answer["urls"], answer["titles"],
                             domains=domains, aliases=aliases)
        for d, h in hits.items():
            row[f"src_{d}"] = h["src"]
            row[f"txt_{d}"] = h["txt"]
        pages = answer["urls"] + [u for u in answer.get("resolved_urls", []) if u]
        for key, hit in match_articles(pages, articles=articles).items():
            row[f"art_{key}"] = hit
        row["all_source_domains"] = ";".join(
            extract_source_domains(engine, answer["urls"], answer["titles"]))
        if save_receipt:
            save_answer(row["row_id"], build_receipt(answer, geo=geo,
                                                     geo_applied=geo_applied, sample=sample))
    return row


# ---------------------------------------------------------------------------
# run command
# ---------------------------------------------------------------------------

def cmd_run(args) -> None:
    load_env()
    engines = available_engines()
    if args.engines:
        requested = [e.strip() for e in args.engines.split(",") if e.strip()]
        unknown = [e for e in requested if e not in ENGINES]
        if unknown:
            sys.exit(f"Unknown engines: {', '.join(unknown)}. Available: {', '.join(ENGINES)}")
        missing = [e for e in requested if e not in engines]
        if missing:
            sys.exit(f"No keys for: {', '.join(missing)}. Add them to .env and try again.")
        engines = requested
    if not engines:
        sys.exit("No API key found. Create .env from the .env.example template.\n"
                 "Fastest free start: GEMINI_API_KEY from aistudio.google.com")

    niches = list(NICHES)
    if args.niches:
        requested = [n.strip() for n in args.niches.split(",") if n.strip()]
        unknown = [n for n in requested if n not in NICHES]
        if unknown:
            sys.exit(f"Unknown niches: {', '.join(unknown)}. Available: {', '.join(NICHES)}")
        niches = requested

    geo = (args.geo or "").strip().upper()
    if geo and not re.fullmatch(r"[A-Z]{2}", geo):
        sys.exit(f"--geo expects an ISO 3166-1 alpha-2 country code (e.g. UZ, US, DE), "
                 f"got: {args.geo!r}")
    samples = args.samples
    if samples < 1:
        sys.exit("--samples must be >= 1")

    check_results_schema()
    # (engine, niche, query) -> ok answers already in history for THIS geo:
    # an answer fetched without a locality is not a sample of the UZ matrix.
    ok_counts = {}
    if args.resume:
        for r in load_results():
            if r["status"] == "ok" and (r.get("geo") or "") == geo:
                key = (r["engine"], r["niche"], r["query"])
                ok_counts[key] = ok_counts.get(key, 0) + 1
        if ok_counts:
            print(f"--resume: {sum(ok_counts.values())} ok answers already in history "
                  f"across {len(ok_counts)} pairs (geo: {geo or 'none'}) — topping each "
                  f"pair up to {samples} sample(s)")

    total = 0
    for n in niches:
        qs = NICHES[n]["queries"][:args.limit] if args.limit else NICHES[n]["queries"]
        total += sum(max(0, samples - ok_counts.get((e, n, q), 0))
                     for q in qs for e in engines)
    settings = f"Engines: {', '.join(engines)} | Niches: {', '.join(niches)}"
    if samples > 1:
        settings += f" | Samples per cell: {samples}"
    if geo:
        applied = [e for e in engines if GEO_SUPPORTED[e]]
        settings += f" | Geo: {geo} (applied by: {', '.join(applied) or 'none — recorded only'})"
    print(settings + f" | API calls: {total}\n")

    done, hits_total = 0, 0
    dead = set()  # engines dropped from the run due to exhausted quota
    for niche in niches:
        queries = NICHES[niche]["queries"]
        if args.limit:
            queries = queries[:args.limit]
        for query in queries:
            for engine in engines:
                have = ok_counts.get((engine, niche, query), 0)
                if engine in dead or have >= samples:
                    continue
                geo_applied = int(bool(geo) and GEO_SUPPORTED[engine])
                for sample in range(have + 1, samples + 1):
                    done += 1
                    label = f"[{done}/{total}] {engine:<10} {niche:<12} {query[:50]}"
                    if samples > 1:
                        label += f" s{sample}/{samples}"
                    try:
                        answer = ENGINES[engine]["fn"](query, geo)
                        unresolved = (resolve_gemini_sources(answer)
                                      if ARTICLES and engine == "gemini" else 0)
                        row = make_row(engine, niche, query, answer=answer,
                                       geo=geo, geo_applied=geo_applied, sample=sample)
                        hit_domains = [d for d in DOMAINS
                                       if row[f"src_{d}"] or row[f"txt_{d}"]]
                        hit_articles = [k for k in ARTICLES if row[f"art_{k}"]]
                        hits_total += len(hit_domains)
                        marker = " ✓ " + ", ".join(hit_domains) if hit_domains else ""
                        if hit_articles:
                            marker += " | articles: " + ", ".join(hit_articles)
                        print(label + marker)
                        if unresolved:
                            print(f"    ! {unresolved} of {len(answer['urls'])} Gemini source links "
                                  f"did not resolve — the article check may miss them")
                    except QuotaExhausted as e:
                        dead.add(engine)
                        row = make_row(engine, niche, query, status=f"error: {e}",
                                       geo=geo, geo_applied=geo_applied, sample=sample)
                        print(f"{label} ✗ {e}")
                        print(f"    !! {engine}: quota exhausted, engine dropped from the run. "
                              f"Finish after the quota resets: python geo_tracker.py run --resume")
                        append_row(row)
                        break
                    except Exception as e:  # noqa: BLE001 — record the error and move on
                        row = make_row(engine, niche, query, status=f"error: {e}",
                                       geo=geo, geo_applied=geo_applied, sample=sample)
                        print(f"{label} ✗ {e}")
                    append_row(row)
                    time.sleep(args.sleep)
            if dead >= set(engines):
                break
        if dead >= set(engines):
            print("\nAll engines dropped from the run due to quotas. "
                  "Finish the rest: python geo_tracker.py run --resume")
            break

    print(f"\nDone. Domain hits: {hits_total}. History: {RESULTS_CSV.name}")
    print("Summary and dashboard: python geo_tracker.py report")


# ---------------------------------------------------------------------------
# report command
# ---------------------------------------------------------------------------

def is_grounded(row: dict) -> bool:
    """True if the engine actually ran a web search for this answer (has sources).

    Some engines (OpenAI's Responses API without a forced tool_choice) may
    skip the search tool and answer from parametric knowledge — a different
    channel that shouldn't be mixed into the same share-of-voice numbers.
    """
    try:
        return int(row.get("n_sources") or 0) > 0
    except ValueError:
        return False


def share_of_voice(rows: list, *, run_date: str = None, engine: str = None,
                    grounded: bool = None) -> dict:
    """{niche: {domain: % of the niche's queries with a hit (src or txt)}}"""
    sov = {}
    for niche in NICHES:
        subset = [r for r in rows
                  if r["niche"] == niche and r["status"] == "ok"
                  and (run_date is None or r["run_date"] == run_date)
                  and (engine is None or r["engine"] == engine)
                  and (grounded is None or is_grounded(r) == grounded)]
        if not subset:
            continue
        sov[niche] = {}
        for d in DOMAINS:
            hits = sum(1 for r in subset
                       if r.get(f"src_{d}") == "1" or r.get(f"txt_{d}") == "1")
            sov[niche][d] = round(100 * hits / len(subset), 1)
    return sov


def cmd_report(args) -> None:
    rows = load_results()
    if not rows:
        sys.exit("results.csv is empty — run first: python geo_tracker.py run")

    dates = sorted({r["run_date"] for r in rows})
    engines = sorted({r["engine"] for r in rows})
    latest = dates[-1]
    ok_rows = [r for r in rows if r["status"] == "ok"]
    err_rows = [r for r in rows if r["status"] != "ok"]

    latest_ok = [r for r in ok_rows if r["run_date"] == latest]
    grounded_n = sum(1 for r in latest_ok if is_grounded(r))
    ungrounded_n = len(latest_ok) - grounded_n
    print(f"History: {len(dates)} dates ({dates[0]} … {latest}), "
          f"{len(ok_rows)} answers, {len(err_rows)} errors, engines: {', '.join(engines)}")
    print(f"Latest run {latest}: {grounded_n} grounded (had sources), "
          f"{ungrounded_n} ungrounded (answered without a web search) — reported separately below.\n")

    def print_sov_table(title: str, grounded_filter: bool) -> None:
        print(f"=== Share of voice, latest run {latest}, {title} (src or txt, any engine) ===")
        sov = share_of_voice(rows, run_date=latest, grounded=grounded_filter)
        if not sov:
            print("  (no rows in this channel)\n")
            return
        header = "niche".ljust(14) + "".join(d.split(".")[0][:12].rjust(13) for d in DOMAINS)
        print(header)
        for niche, by_domain in sov.items():
            primary = NICHES[niche]["primary_domain"]
            cells = ""
            for d in DOMAINS:
                val = f"{by_domain[d]:.0f}%"
                if d == primary:
                    val = "*" + val
                cells += val.rjust(13)
            print(niche.ljust(14) + cells)
        print("(* — niche's primary domain)\n")

    print_sov_table("GROUNDED", True)
    print_sov_table("UNGROUNDED — answered from parametric knowledge, no web search", False)

    def print_audit_table(title: str, grounded_filter: bool) -> None:
        print(f"=== Measurement uncertainty, latest run {latest}, {title} (95% CI) ===")
        stats = sov_stats(rows, DOMAINS, NICHES, run_date=latest, grounded=grounded_filter)
        if not any(stats.values()):
            print("  (no rows in this channel)\n")
            return
        header = "niche".ljust(14) + "".join(d.split(".")[0][:12].rjust(38) for d in DOMAINS)
        print(header)
        for niche, by_domain in stats.items():
            if not by_domain:
                continue
            cells = "".join((format_ci(by_domain[d]) if d in by_domain else "—").rjust(38)
                            for d in DOMAINS)
            print(niche.ljust(14) + cells)
        print()

    print_audit_table("GROUNDED", True)
    print_audit_table("UNGROUNDED — parametric knowledge", False)
    print("Method: share = hit samples / all samples of the channel. The 95% CI counts "
          "evidence per query, not per answer: repeated answers to one query are "
          "correlated.")
    print(f"Fewer than {MIN_BOOTSTRAP_QUERIES} queries behind a cell, or a share of exactly "
          f"0% / 100%: Wilson score interval with n = nq (each query is one observation — "
          f"the conservative reading).")
    print(f"{MIN_BOOTSTRAP_QUERIES}+ queries: cluster bootstrap over queries, whole queries "
          f"resampled with all their samples (B={DEFAULT_B}, seed={DEFAULT_SEED}). "
          f"Reproducible from the same rows.")
    print("More queries narrow the interval; more samples per query only steady the "
          "point share.\n")

    models = sorted({r.get("model") or "" for r in latest_ok} - {""})
    geos = sorted({r.get("geo") or "" for r in latest_ok} - {""})
    if models:
        print(f"Provenance (latest run): models — {', '.join(models)}")
    if geos:
        applied = sorted({r["engine"] for r in latest_ok if r.get("geo_applied") == "1"})
        note = (f"applied by {', '.join(applied)}" if applied
                else "recorded only — not sent to any engine in this run")
        print(f"Provenance (latest run): requested geo — {', '.join(geos)} ({note})")
    if models or geos:
        print()

    engine_by_id = {r["row_id"]: r["engine"] for r in latest_ok}
    usage_agg = {}
    if ANSWERS_JSONL.exists():
        with ANSWERS_JSONL.open(encoding="utf-8") as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                eng = engine_by_id.get(rec.get("row_id"))
                if not eng:
                    continue
                a = usage_agg.setdefault(eng, {"calls": 0, "usage_receipts": 0,
                                               "tokens": 0, "tokens_known": False,
                                               "searches": 0, "searches_known": False,
                                               "cost": 0.0, "cost_known": False})
                a["calls"] += 1
                u = rec.get("usage") or {}
                if u:
                    a["usage_receipts"] += 1
                t = u.get("totalTokenCount", u.get("total_tokens"))
                if isinstance(t, (int, float)):
                    a["tokens"] += t
                    a["tokens_known"] = True
                s = rec.get("searches")
                if isinstance(s, int):
                    a["searches"] += s
                    a["searches_known"] = True
                c = (u.get("cost") or {}).get("total_cost")
                if isinstance(c, (int, float)):
                    a["cost"] += c
                    a["cost_known"] = True
    if usage_agg:
        print("=== Resource usage (latest run, from receipts) ===")
        for eng in sorted(usage_agg):
            a = usage_agg[eng]
            tokens = str(a["tokens"]) if a["tokens_known"] else "—"
            searches = str(a["searches"]) if a["searches_known"] else "—"
            cost = f"${a['cost']:.4f}" if a["cost_known"] else "—"
            print(f"  {eng:<11} calls={a['calls']:<3} usage={a['usage_receipts']}/{a['calls']:<3} "
                  f"tokens={tokens:<8} searches={searches:<4} cost={cost}")
        print("  Tokens/searches/cost summed over receipts WITH usage only (usage=X/Y); "
              "raw API counters, cost only where the API reported it (Perplexity). "
              "Lower bound: successful answers only — failed calls and retries excluded.\n")

    print("=== Domain leaderboard (latest run, GROUNDED only, % of grounded queries) ===")
    subset = [r for r in latest_ok if is_grounded(r)]
    leaderboard = []
    for d in DOMAINS:
        hits = sum(1 for r in subset if r.get(f"src_{d}") == "1" or r.get(f"txt_{d}") == "1")
        leaderboard.append((d, round(100 * hits / len(subset), 1) if subset else 0.0))
    for d, pct in sorted(leaderboard, key=lambda x: -x[1]):
        print(f"  {d:<22} {pct:5.1f}%")

    print(f"\n=== All source domains (raw, incl. untracked), latest run {latest}, GROUNDED, by niche ===")
    for niche in NICHES:
        freq = {}
        for r in subset:
            if r["niche"] != niche:
                continue
            for host in (r.get("all_source_domains") or "").split(";"):
                if host:
                    freq[host] = freq.get(host, 0) + 1
        if not freq:
            continue
        top = ", ".join(f"{d} ({c})" for d, c in sorted(freq.items(), key=lambda x: -x[1]))
        print(f"  {niche}: {top}")

    if ARTICLES:
        print(f"\n=== Article citations, latest run {latest} (page among the answer's sources) ===")
        for key, cfg in ARTICLES.items():
            cited = [r for r in latest_ok if r.get(f"art_{key}") == "1"]
            engines_hit = sorted({r["engine"] for r in cited})
            print(f"  {key} — {cfg['title']}\n    {cfg['url']}")
            print(f"    cited in {len(cited)} of {len(latest_ok)} answers"
                  + (f" ({', '.join(engines_hit)})" if cited else ""))
            for r in cited:
                print(f"      {r['engine']:<10} {r['niche']:<14} {r['query']}")

    out = Path(args.out) if args.out else DASHBOARD_HTML
    build_dashboard(rows, DOMAINS, NICHES, out, articles=ARTICLES)
    print(f"\nDashboard: {out}")


# ---------------------------------------------------------------------------
# Manual mode: checklist for ChatGPT / Yandex Neuro
# ---------------------------------------------------------------------------

MANUAL_FIELDS = ["engine", "niche", "query", "domains_in_sources", "domains_in_text"]

def cmd_manual_export(args) -> None:
    engine = args.engine or "manual"
    out = BASE_DIR / f"manual_{engine}_{datetime.now().strftime('%Y-%m-%d')}.csv"
    fields = MANUAL_FIELDS + (["articles_in_sources"] if ARTICLES else [])
    with out.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for niche, cfg in NICHES.items():
            queries = cfg["queries"][:args.limit] if args.limit else cfg["queries"]
            for q in queries:
                writer.writerow({"engine": engine, "niche": niche, "query": q,
                                 "domains_in_sources": "", "domains_in_text": ""})
    print(f"Checklist: {out.name}")
    print("Run the queries by hand, fill in domains comma-separated (e.g. pc.uz, sprav.uz),\n"
          "then: python geo_tracker.py manual-import " + out.name)
    if ARTICLES:
        print("articles_in_sources: article keys or the URLs you saw among the sources "
              f"({', '.join(ARTICLES)})")


def cmd_manual_import(args) -> None:
    path = Path(args.file)
    if not path.exists():
        path = BASE_DIR / args.file
    if not path.exists():
        sys.exit(f"File not found: {args.file}")

    check_results_schema()
    imported = 0
    with path.open(newline="", encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            row = make_row(r.get("engine") or "manual", r["niche"], r["query"])
            for field, prefix in (("domains_in_sources", "src_"), ("domains_in_text", "txt_")):
                for d in re.split(r"[,;\s]+", (r.get(field) or "").strip().lower()):
                    d = d.removeprefix("www.")
                    if d in DOMAINS:
                        row[prefix + d] = 1
            for value in re.split(r"[,;\s]+", (r.get("articles_in_sources") or "").strip()):
                key = article_key(value)
                if key:
                    row[f"art_{key}"] = 1
            append_row(row)
            imported += 1
    print(f"Imported rows: {imported} → {RESULTS_CSV.name}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def require_config() -> None:
    """The CLI measures what queries.py lists; without it there is nothing to run."""
    if not DOMAINS or not NICHES:
        sys.exit("queries.py is missing or defines no DOMAINS / NICHES.\n"
                 "Create it from the template: cp queries.example.py queries.py")


def main() -> None:
    parser = argparse.ArgumentParser(description="GEO tracker — AI answer citation monitoring")
    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser("run", help="run the query matrix through the APIs")
    p_run.add_argument("--engines", help="comma-separated: gemini,perplexity,openai")
    p_run.add_argument("--niches", help="comma-separated: tech,medical,…")
    p_run.add_argument("--limit", type=int, help="max queries per niche (for testing)")
    p_run.add_argument("--sleep", type=float, default=2.0,
                       help="pause between calls, sec (default: 2)")
    p_run.add_argument("--samples", type=int, default=1, metavar="N",
                       help="answers per cell (engine × niche × query), default 1. "
                            "Repeated samples steady the point share (the CI narrows "
                            "with more queries, not more samples); each sample is a "
                            "separate row + receipt and spends quota")
    p_run.add_argument("--geo", metavar="CC",
                       help="requested country (ISO alpha-2, e.g. UZ). Applied by OpenAI "
                            "and Perplexity (user_location); for Gemini it is only "
                            "recorded — this tracker sends Gemini no locality")
    p_run.add_argument("--resume", action="store_true",
                       help="count ok answers per pair (same --geo) across all prior "
                            "runs and only fetch what is missing up to --samples (fills "
                            "an unfinished matrix without re-spending quota on duplicates)")
    p_run.set_defaults(fn=cmd_run)

    p_rep = sub.add_parser("report", help="summary + HTML dashboard")
    p_rep.add_argument("--out", help="path for dashboard.html")
    p_rep.set_defaults(fn=cmd_report)

    p_me = sub.add_parser("manual-export", help="CSV checklist for a manual run")
    p_me.add_argument("--engine", help="engine name in the report: chatgpt, yandex…")
    p_me.add_argument("--limit", type=int, help="max queries per niche")
    p_me.set_defaults(fn=cmd_manual_export)

    p_mi = sub.add_parser("manual-import", help="merge a filled checklist")
    p_mi.add_argument("file", help="path to the filled checklist")
    p_mi.set_defaults(fn=cmd_manual_import)

    args = parser.parse_args()
    require_config()
    args.fn(args)


if __name__ == "__main__":
    main()
