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
from urllib.parse import urlparse

import requests

from queries import DOMAINS, NICHES

try:
    # Optional {domain: [brand name, ...]} map — engines often name a brand in
    # the answer text without ever linking its domain. Lives in queries.py
    # (local config) so client brand names stay out of the repo.
    from queries import BRAND_ALIASES
except ImportError:
    BRAND_ALIASES = {}
from report_html import build_dashboard

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

def domain_in_url(domain: str, url: str) -> bool:
    """Exact host match: pc.uz matches pc.uz and www.pc.uz, but not 1pc.uz."""
    try:
        netloc = urlparse(url if "://" in url else "https://" + url).netloc.lower()
    except ValueError:
        return False
    netloc = netloc.split(":")[0]
    return netloc == domain or netloc.endswith("." + domain)


def domain_in_text(domain: str, text: str) -> bool:
    """Domain mention in text with word boundaries so pc.uz won't catch 1pc.uz."""
    pattern = r"(?<![\w.-])" + re.escape(domain) + r"(?![\w-])"
    return re.search(pattern, text, flags=re.IGNORECASE) is not None


def match_domains(answer_text: str, source_urls: list, source_titles: list) -> dict:
    """For each domain: src (among sources) and txt (in the answer text)."""
    hits = {}
    titles_blob = " ".join(source_titles)
    for d in DOMAINS:
        src = any(domain_in_url(d, u) for u in source_urls) or domain_in_text(d, titles_blob)
        txt = domain_in_text(d, answer_text) or any(
            alias.lower() in answer_text.lower()
            for alias in BRAND_ALIASES.get(d, []))
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


def ask_gemini(query: str) -> dict:
    """Gemini + Google Search grounding. Sources come from groundingChunks.

    Note: groundingChunks uris are Google redirect links that hide the real
    host, so domains are matched against web.title (usually the source host)
    and the answer text.
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
    return {"text": text, "urls": urls, "titles": titles}


def ask_perplexity(query: str) -> dict:
    """Perplexity Sonar. Sources: citations + search_results."""
    model = os.environ.get("PERPLEXITY_MODEL", "sonar")
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": query}],
    }
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
    return {"text": text, "urls": urls, "titles": titles}


def ask_openai(query: str) -> dict:
    """OpenAI Responses API + web_search. Sources: url_citation annotations."""
    model = os.environ.get("OPENAI_MODEL", "gpt-5-mini")
    payload = {
        "model": model,
        "input": query,
        "tools": [{"type": "web_search"}],
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
    return {"text": " ".join(text_parts), "urls": urls, "titles": titles}


ENGINES = {
    "gemini": {"env": "GEMINI_API_KEY", "fn": ask_gemini},
    "perplexity": {"env": "PERPLEXITY_API_KEY", "fn": ask_perplexity},
    "openai": {"env": "OPENAI_API_KEY", "fn": ask_openai},
}


def available_engines() -> list:
    return [name for name, cfg in ENGINES.items() if os.environ.get(cfg["env"])]


# ---------------------------------------------------------------------------
# CSV: results history
# ---------------------------------------------------------------------------

def csv_fieldnames() -> list:
    fields = ["row_id", "run_date", "run_ts", "engine", "niche", "query",
              "status", "answer_chars", "n_sources"]
    for d in DOMAINS:
        fields.append(f"src_{d}")
        fields.append(f"txt_{d}")
    fields.append("all_source_domains")
    return fields


def append_row(row: dict) -> None:
    """Append rows one by one — a crash mid-run loses nothing."""
    fields = csv_fieldnames()
    is_new = not RESULTS_CSV.exists()
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


def make_row(engine: str, niche: str, query: str, *, status: str = "ok",
             answer: dict = None) -> dict:
    now = datetime.now(timezone.utc)
    row = {
        "row_id": uuid.uuid4().hex[:12],
        "run_date": now.strftime("%Y-%m-%d"),
        "run_ts": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "engine": engine,
        "niche": niche,
        "query": query,
        "status": status,
        "answer_chars": 0,
        "n_sources": 0,
        "all_source_domains": "",
    }
    for d in DOMAINS:
        row[f"src_{d}"] = 0
        row[f"txt_{d}"] = 0
    if answer:
        row["answer_chars"] = len(answer["text"])
        row["n_sources"] = len(answer["urls"])
        hits = match_domains(answer["text"], answer["urls"], answer["titles"])
        for d, h in hits.items():
            row[f"src_{d}"] = h["src"]
            row[f"txt_{d}"] = h["txt"]
        row["all_source_domains"] = ";".join(
            extract_source_domains(engine, answer["urls"], answer["titles"]))
        save_answer(row["row_id"], answer)
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

    done_already = set()
    if args.resume:
        done_already = {(r["engine"], r["niche"], r["query"]) for r in load_results()
                        if r["status"] == "ok"}
        if done_already:
            print(f"--resume: {len(done_already)} ok answers already in history, "
                  f"skipping them — filling the unfinished pairs")

    total = 0
    for n in niches:
        qs = NICHES[n]["queries"][:args.limit] if args.limit else NICHES[n]["queries"]
        total += sum(1 for q in qs for e in engines if (e, n, q) not in done_already)
    print(f"Engines: {', '.join(engines)} | Niches: {', '.join(niches)} | API calls: {total}\n")

    done, hits_total = 0, 0
    dead = set()  # engines dropped from the run due to exhausted quota
    for niche in niches:
        queries = NICHES[niche]["queries"]
        if args.limit:
            queries = queries[:args.limit]
        for query in queries:
            for engine in engines:
                if engine in dead or (engine, niche, query) in done_already:
                    continue
                done += 1
                label = f"[{done}/{total}] {engine:<10} {niche:<12} {query[:50]}"
                try:
                    answer = ENGINES[engine]["fn"](query)
                    row = make_row(engine, niche, query, answer=answer)
                    hit_domains = [d for d in DOMAINS
                                   if row[f"src_{d}"] or row[f"txt_{d}"]]
                    hits_total += len(hit_domains)
                    marker = " ✓ " + ", ".join(hit_domains) if hit_domains else ""
                    print(label + marker)
                except QuotaExhausted as e:
                    dead.add(engine)
                    row = make_row(engine, niche, query, status=f"error: {e}")
                    print(f"{label} ✗ {e}")
                    print(f"    !! {engine}: quota exhausted, engine dropped from the run. "
                          f"Finish after the quota resets: python geo_tracker.py run --resume")
                    append_row(row)
                    continue
                except Exception as e:  # noqa: BLE001 — record the error and move on
                    row = make_row(engine, niche, query, status=f"error: {e}")
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

    out = Path(args.out) if args.out else DASHBOARD_HTML
    build_dashboard(rows, DOMAINS, NICHES, out)
    print(f"\nDashboard: {out}")


# ---------------------------------------------------------------------------
# Manual mode: checklist for ChatGPT / Yandex Neuro
# ---------------------------------------------------------------------------

MANUAL_FIELDS = ["engine", "niche", "query", "domains_in_sources", "domains_in_text"]

def cmd_manual_export(args) -> None:
    engine = args.engine or "manual"
    out = BASE_DIR / f"manual_{engine}_{datetime.now().strftime('%Y-%m-%d')}.csv"
    with out.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=MANUAL_FIELDS)
        writer.writeheader()
        for niche, cfg in NICHES.items():
            queries = cfg["queries"][:args.limit] if args.limit else cfg["queries"]
            for q in queries:
                writer.writerow({"engine": engine, "niche": niche, "query": q,
                                 "domains_in_sources": "", "domains_in_text": ""})
    print(f"Checklist: {out.name}")
    print("Run the queries by hand, fill in domains comma-separated (e.g. pc.uz, sprav.uz),\n"
          "then: python geo_tracker.py manual-import " + out.name)


def cmd_manual_import(args) -> None:
    path = Path(args.file)
    if not path.exists():
        path = BASE_DIR / args.file
    if not path.exists():
        sys.exit(f"File not found: {args.file}")

    imported = 0
    with path.open(newline="", encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            row = make_row(r.get("engine") or "manual", r["niche"], r["query"])
            for field, prefix in (("domains_in_sources", "src_"), ("domains_in_text", "txt_")):
                for d in re.split(r"[,;\s]+", (r.get(field) or "").strip().lower()):
                    d = d.removeprefix("www.")
                    if d in DOMAINS:
                        row[prefix + d] = 1
            append_row(row)
            imported += 1
    print(f"Imported rows: {imported} → {RESULTS_CSV.name}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="GEO tracker — AI answer citation monitoring")
    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser("run", help="run the query matrix through the APIs")
    p_run.add_argument("--engines", help="comma-separated: gemini,perplexity,openai")
    p_run.add_argument("--niches", help="comma-separated: tech,medical,…")
    p_run.add_argument("--limit", type=int, help="max queries per niche (for testing)")
    p_run.add_argument("--sleep", type=float, default=2.0,
                       help="pause between calls, sec (default: 2)")
    p_run.add_argument("--resume", action="store_true",
                       help="skip pairs that already have an ok answer in any prior run "
                            "(fills an unfinished matrix without re-spending quota on duplicates)")
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
    args.fn(args)


if __name__ == "__main__":
    main()
