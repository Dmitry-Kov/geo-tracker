#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
GEO-трекер Optimize.uz — мониторинг цитируемости доменов в ответах ИИ.

Архитектура: только официальные API, никакой браузерной автоматизации.
Движки включаются автоматически по наличию ключей в .env / переменных окружения:

  GEMINI_API_KEY      — Gemini + Google Search grounding (бесплатный тир, aistudio.google.com)
  PERPLEXITY_API_KEY  — Perplexity Sonar (кредиты Pro-подписки или pay-as-you-go)
  OPENAI_API_KEY      — OpenAI Responses API + web_search (платно, копейки на mini-модели)

Команды:
  run            — прогнать матрицу запросов, дописать результаты в results.csv
  report         — консольная сводка + HTML-дашборд (dashboard.html)
  manual-export  — выгрузить чеклист CSV для ручного прогона (ChatGPT, Яндекс Нейро)
  manual-import  — влить заполненный чеклист в общую историю

Примеры:
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
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import requests

from queries import DOMAINS, NICHES
from report_html import build_dashboard

BASE_DIR = Path(__file__).resolve().parent
RESULTS_CSV = BASE_DIR / "results.csv"
DASHBOARD_HTML = BASE_DIR / "dashboard.html"

TIMEOUT = 90
RETRIES = 4
BACKOFF = (3, 8, 20)  # секунды между попытками при сетевых/прочих ошибках
THROTTLE_BACKOFF = (10, 30, 60)  # паузы при 429 (rate limit) и 503 (high demand)

# ---------------------------------------------------------------------------
# Окружение
# ---------------------------------------------------------------------------

def load_env() -> None:
    """Подхватываем .env рядом со скриптом, не перетирая уже заданные переменные."""
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
# Матчинг доменов
# ---------------------------------------------------------------------------

def domain_in_url(domain: str, url: str) -> bool:
    """Точное совпадение хоста: pc.uz матчит pc.uz и www.pc.uz, но не 1pc.uz."""
    try:
        netloc = urlparse(url if "://" in url else "https://" + url).netloc.lower()
    except ValueError:
        return False
    netloc = netloc.split(":")[0]
    return netloc == domain or netloc.endswith("." + domain)


def domain_in_text(domain: str, text: str) -> bool:
    """Упоминание домена в тексте с границами, чтобы pc.uz не ловил 1pc.uz."""
    pattern = r"(?<![\w.-])" + re.escape(domain) + r"(?![\w-])"
    return re.search(pattern, text, flags=re.IGNORECASE) is not None


def match_domains(answer_text: str, source_urls: list, source_titles: list) -> dict:
    """Для каждого домена: src (в источниках) и txt (в тексте ответа)."""
    hits = {}
    titles_blob = " ".join(source_titles)
    for d in DOMAINS:
        src = any(domain_in_url(d, u) for u in source_urls) or domain_in_text(d, titles_blob)
        txt = domain_in_text(d, answer_text)
        hits[d] = {"src": int(src), "txt": int(txt)}
    return hits


# ---------------------------------------------------------------------------
# Движки
# ---------------------------------------------------------------------------

class QuotaExhausted(RuntimeError):
    """Суточная квота движка исчерпана — ретраить бессмысленно."""


def _parse_429(body: str) -> tuple:
    """Из тела 429 Google: (имена квот из QuotaFailure, retryDelay из RetryInfo в сек)."""
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
                qname = "; ".join(quotas) or "квота не указана в details"
                daily = (any("PerDay" in q for q in quotas)
                         or ("RESOURCE_EXHAUSTED" in body and not quotas and delay is None))
                # Суточной квоте даём один шанс пересидеть короткий RetryInfo
                # (окно иногда скользящее), повторный 429 — стоп без ретраев.
                if daily and (attempt >= 1 or delay is None or delay > 120):
                    raise QuotaExhausted(f"суточная квота: {qname}")
                wait = (int(delay) + 2 if delay and delay <= 120
                        else THROTTLE_BACKOFF[min(attempt, len(THROTTLE_BACKOFF) - 1)])
                print(f"    429 [{qname}], жду {wait}с…")
                time.sleep(wait)
                last_err = RuntimeError(f"429: {qname}")
                continue
            if resp.status_code == 503:
                wait = THROTTLE_BACKOFF[min(attempt, len(THROTTLE_BACKOFF) - 1)]
                print(f"    503 high demand, жду {wait}с…")
                time.sleep(wait)
                last_err = RuntimeError(f"503: {resp.text.strip()[:300]}")
                continue
            resp.raise_for_status()
            return resp.json()
        except (requests.RequestException, ValueError) as e:
            last_err = e
            if attempt < RETRIES - 1:
                time.sleep(BACKOFF[attempt])
    raise RuntimeError(f"запрос не удался после {RETRIES} попыток: {last_err}")


def ask_gemini(query: str) -> dict:
    """Gemini + Google Search grounding. Источники берём из groundingChunks.

    Важно: uri в groundingChunks — это redirect-ссылки Google, домен в них не виден.
    Поэтому домен ищем в web.title (там обычно хост источника) и в тексте ответа.
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
    """Perplexity Sonar. Источники: citations + search_results."""
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
    """OpenAI Responses API + web_search. Источники: url_citation-аннотации."""
    model = os.environ.get("OPENAI_MODEL", "gpt-5-mini")
    payload = {
        "model": model,
        "input": query,
        "tools": [{"type": "web_search"}],
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
# CSV: история результатов
# ---------------------------------------------------------------------------

def csv_fieldnames() -> list:
    fields = ["run_date", "run_ts", "engine", "niche", "query",
              "status", "answer_chars", "n_sources"]
    for d in DOMAINS:
        fields.append(f"src_{d}")
        fields.append(f"txt_{d}")
    return fields


def append_row(row: dict) -> None:
    """Дописываем построчно — при падении посреди прогона данные не теряются."""
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


def make_row(engine: str, niche: str, query: str, *, status: str = "ok",
             answer: dict = None) -> dict:
    now = datetime.now(timezone.utc)
    row = {
        "run_date": now.strftime("%Y-%m-%d"),
        "run_ts": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "engine": engine,
        "niche": niche,
        "query": query,
        "status": status,
        "answer_chars": 0,
        "n_sources": 0,
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
    return row


# ---------------------------------------------------------------------------
# Команда run
# ---------------------------------------------------------------------------

def cmd_run(args) -> None:
    load_env()
    engines = available_engines()
    if args.engines:
        requested = [e.strip() for e in args.engines.split(",") if e.strip()]
        unknown = [e for e in requested if e not in ENGINES]
        if unknown:
            sys.exit(f"Неизвестные движки: {', '.join(unknown)}. Доступны: {', '.join(ENGINES)}")
        missing = [e for e in requested if e not in engines]
        if missing:
            sys.exit(f"Нет ключей для: {', '.join(missing)}. Добавь в .env и попробуй снова.")
        engines = requested
    if not engines:
        sys.exit("Не найден ни один API-ключ. Создай .env по образцу .env.example.\n"
                 "Самый быстрый бесплатный старт: GEMINI_API_KEY с aistudio.google.com")

    niches = list(NICHES)
    if args.niches:
        requested = [n.strip() for n in args.niches.split(",") if n.strip()]
        unknown = [n for n in requested if n not in NICHES]
        if unknown:
            sys.exit(f"Неизвестные ниши: {', '.join(unknown)}. Доступны: {', '.join(NICHES)}")
        niches = requested

    done_today = set()
    if args.resume:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        done_today = {(r["engine"], r["niche"], r["query"]) for r in load_results()
                      if r["run_date"] == today and r["status"] == "ok"}
        if done_today:
            print(f"--resume: за {today} уже есть {len(done_today)} ok-ответов, пропускаю их")

    total = 0
    for n in niches:
        qs = NICHES[n]["queries"][:args.limit] if args.limit else NICHES[n]["queries"]
        total += sum(1 for q in qs for e in engines if (e, n, q) not in done_today)
    print(f"Движки: {', '.join(engines)} | Ниши: {', '.join(niches)} | Запросов к API: {total}\n")

    done, hits_total = 0, 0
    dead = set()  # движки, снятые с прогона из-за исчерпанной квоты
    for niche in niches:
        queries = NICHES[niche]["queries"]
        if args.limit:
            queries = queries[:args.limit]
        for query in queries:
            for engine in engines:
                if engine in dead or (engine, niche, query) in done_today:
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
                    print(f"    !! {engine}: квота исчерпана, движок снят с прогона. "
                          f"Дожать после сброса квоты: python geo_tracker.py run --resume")
                    append_row(row)
                    continue
                except Exception as e:  # noqa: BLE001 — пишем ошибку в историю и едем дальше
                    row = make_row(engine, niche, query, status=f"error: {e}")
                    print(f"{label} ✗ {e}")
                append_row(row)
                time.sleep(args.sleep)
            if dead >= set(engines):
                break
        if dead >= set(engines):
            print("\nВсе движки сняты с прогона из-за квот. "
                  "Дожать остаток: python geo_tracker.py run --resume")
            break

    print(f"\nГотово. Попаданий доменов: {hits_total}. История: {RESULTS_CSV.name}")
    print("Сводка и дашборд: python geo_tracker.py report")


# ---------------------------------------------------------------------------
# Команда report
# ---------------------------------------------------------------------------

def share_of_voice(rows: list, *, run_date: str = None, engine: str = None) -> dict:
    """{niche: {domain: процент запросов с попаданием (src или txt)}}"""
    sov = {}
    for niche in NICHES:
        subset = [r for r in rows
                  if r["niche"] == niche and r["status"] == "ok"
                  and (run_date is None or r["run_date"] == run_date)
                  and (engine is None or r["engine"] == engine)]
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
        sys.exit("results.csv пуст — сначала запусти: python geo_tracker.py run")

    dates = sorted({r["run_date"] for r in rows})
    engines = sorted({r["engine"] for r in rows})
    latest = dates[-1]
    ok_rows = [r for r in rows if r["status"] == "ok"]
    err_rows = [r for r in rows if r["status"] != "ok"]

    print(f"История: {len(dates)} дат ({dates[0]} … {latest}), "
          f"{len(ok_rows)} ответов, {len(err_rows)} ошибок, движки: {', '.join(engines)}\n")

    print(f"=== Share of voice, последний прогон {latest} (src или txt, любой движок) ===")
    sov = share_of_voice(rows, run_date=latest)
    header = "ниша".ljust(14) + "".join(d.split(".")[0][:12].rjust(13) for d in DOMAINS)
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
    print("(* — целевой домен ниши)\n")

    print("=== Общий зачёт по доменам (последний прогон, % от всех запросов) ===")
    subset = [r for r in ok_rows if r["run_date"] == latest]
    leaderboard = []
    for d in DOMAINS:
        hits = sum(1 for r in subset if r.get(f"src_{d}") == "1" or r.get(f"txt_{d}") == "1")
        leaderboard.append((d, round(100 * hits / len(subset), 1) if subset else 0.0))
    for d, pct in sorted(leaderboard, key=lambda x: -x[1]):
        print(f"  {d:<22} {pct:5.1f}%")

    out = Path(args.out) if args.out else DASHBOARD_HTML
    build_dashboard(rows, DOMAINS, NICHES, out)
    print(f"\nДашборд: {out}")


# ---------------------------------------------------------------------------
# Ручной режим: чеклист для ChatGPT / Яндекс Нейро
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
    print(f"Чеклист: {out.name}")
    print("Прогони запросы руками, впиши домены через запятую (например: pc.uz, sprav.uz),\n"
          "затем: python geo_tracker.py manual-import " + out.name)


def cmd_manual_import(args) -> None:
    path = Path(args.file)
    if not path.exists():
        path = BASE_DIR / args.file
    if not path.exists():
        sys.exit(f"Файл не найден: {args.file}")

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
    print(f"Импортировано строк: {imported} → {RESULTS_CSV.name}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="GEO-трекер Optimize.uz")
    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser("run", help="прогнать матрицу запросов через API")
    p_run.add_argument("--engines", help="через запятую: gemini,perplexity,openai")
    p_run.add_argument("--niches", help="через запятую: tech,medical,…")
    p_run.add_argument("--limit", type=int, help="максимум запросов на нишу (для теста)")
    p_run.add_argument("--sleep", type=float, default=2.0,
                       help="пауза между запросами, сек (default: 2)")
    p_run.add_argument("--resume", action="store_true",
                       help="пропускать запросы, по которым за сегодня уже есть ok-строка движка")
    p_run.set_defaults(fn=cmd_run)

    p_rep = sub.add_parser("report", help="сводка + HTML-дашборд")
    p_rep.add_argument("--out", help="путь для dashboard.html")
    p_rep.set_defaults(fn=cmd_report)

    p_me = sub.add_parser("manual-export", help="чеклист CSV для ручного прогона")
    p_me.add_argument("--engine", help="имя движка в отчёте: chatgpt, yandex…")
    p_me.add_argument("--limit", type=int, help="максимум запросов на нишу")
    p_me.set_defaults(fn=cmd_manual_export)

    p_mi = sub.add_parser("manual-import", help="влить заполненный чеклист")
    p_mi.add_argument("file", help="путь к заполненному чеклисту")
    p_mi.set_defaults(fn=cmd_manual_import)

    args = parser.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
