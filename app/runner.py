# -*- coding: utf-8 -*-
"""Execute brand runs: the measurement core's adapters → DB.

One run at a time (single-user slice). The guarantee lives in the database:
Run.active is UNIQUE, so a second pending/running run cannot be inserted no
matter how the triggers interleave. That is also what makes the per-brand
model overrides (process-wide env vars) safe.
"""
import os
import time
from datetime import datetime

from sqlalchemy import or_
from sqlalchemy.exc import IntegrityError

import geo_tracker as gt

from .db import Answer, ArticleHit, Brand, Hit, Run, utcnow

SLEEP_SEC = 2.0
ACTIVE = ("pending", "running")
MODEL_ENV = {"gemini": "GEMINI_MODEL", "perplexity": "PERPLEXITY_MODEL",
             "openai": "OPENAI_MODEL"}
INTERRUPTED = "interrupted: the service stopped before this run finished"


def brand_config(b: Brand) -> dict:
    """The part of a brand that decides what a run measures — snapshotted per run."""
    return {"domains": list(b.domains), "aliases": dict(b.aliases),
            "niches": dict(b.niches), "articles": dict(b.articles),
            "geo": b.geo, "engines": list(b.engines),
            "models": dict(b.models), "samples": b.samples,
            "match_rules_version": gt.MATCH_RULES_VERSION}


def run_config(run: Run) -> dict:
    """The configuration a run was measured under (its snapshot).
    Snapshots taken before match_rules_version existed lack the key —
    consumers must read it as .get("match_rules_version", 1)."""
    return run.config or brand_config(run.brand)


def start_run(Session, brand_id: int, trigger: str = "manual"):
    """Create a pending run; None if another run is active or brand missing."""
    with Session() as s:
        b = s.get(Brand, brand_id)
        if b is None:
            return None
        run = Run(brand_id=brand_id, status="pending", trigger=trigger,
                  active=1, config=brand_config(b))
        s.add(run)
        try:
            s.commit()
        except IntegrityError:  # another run holds the active slot
            s.rollback()
            return None
        return run.id


def recover_interrupted(Session) -> int:
    """Close runs a previous process left pending/running.

    Called once at startup. A run cannot survive its process, so anything
    still marked active is dead; left alone it would hold the active slot and
    block every future run.
    """
    with Session() as s:
        stale = s.query(Run).filter(or_(Run.active == 1,
                                        Run.status.in_(ACTIVE))).all()
        for run in stale:
            run.status = "error"
            run.error = INTERRUPTED
            run.active = None
            run.finished_at = utcnow()
        s.commit()
        return len(stale)


def record_skipped(Session, brand_id: int, reason: str) -> None:
    """Leave a visible trace of a scheduled run that never started."""
    with Session() as s:
        if s.get(Brand, brand_id) is None:
            return
        s.add(Run(brand_id=brand_id, status="error", trigger="scheduled",
                  error=reason, finished_at=utcnow()))
        s.commit()


def to_csv_row(a: Answer, domains: list, articles: dict, run_date: str = None) -> dict:
    """DB answer → the CSV-shaped row dict the stats/report modules consume
    (string flags, src_/txt_/art_ columns).

    run_date: the report modules treat the latest run_date as "the latest
    run". Rows of one service run must share one date, or a run that crosses
    UTC midnight is reported from its post-midnight answers only.
    """
    row = {
        "row_id": a.id,
        "run_date": run_date or a.run_ts.strftime("%Y-%m-%d"),
        "run_ts": a.run_ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "engine": a.engine, "niche": a.niche, "query": a.query,
        "model": a.model, "geo": a.geo, "geo_applied": str(a.geo_applied),
        "sample": str(a.sample), "status": a.status,
        "answer_chars": str(a.answer_chars), "n_sources": str(a.n_sources),
        "all_source_domains": a.all_source_domains,
    }
    hits = {h.domain: h for h in a.hits}
    for d in domains:
        h = hits.get(d)
        row[f"src_{d}"] = str(h.src) if h else "0"
        row[f"txt_{d}"] = str(h.txt) if h else "0"
    cited = {ah.article_key for ah in a.article_hits}
    for k in articles:
        row[f"art_{k}"] = "1" if k in cited else "0"
    return row


def run_rows(run: Run) -> tuple:
    """(config snapshot, CSV-shaped rows) of one run, all under one run date."""
    cfg = run_config(run)
    answers = list(run.answers)
    first = run.started_at or (answers[0].run_ts if answers else utcnow())
    day = first.strftime("%Y-%m-%d")
    return cfg, [to_csv_row(a, cfg["domains"], cfg["articles"], run_date=day)
                 for a in answers]


def execute_run(Session, run_id: int) -> None:
    with Session() as s:
        run = s.get(Run, run_id)
        if run is None or run.status != "pending":
            return
        cfg = run_config(run)
        run.status = "running"
        run.started_at = utcnow()
        s.commit()

    engines = [e for e in cfg["engines"]
               if e in gt.ENGINES and os.environ.get(gt.ENGINES[e]["env"])]
    old_models = {}
    for e, m in cfg["models"].items():
        if e in MODEL_ENV and m:
            old_models[MODEL_ENV[e]] = os.environ.get(MODEL_ENV[e])
            os.environ[MODEL_ENV[e]] = m

    dead, error = set(), None
    try:
        total = (sum(len(ncfg["queries"]) for ncfg in cfg["niches"].values())
                 * len(engines) * cfg["samples"])
        with Session() as s:
            s.get(Run, run_id).total_calls = total
            s.commit()

        for niche, ncfg in cfg["niches"].items():
            for query in ncfg["queries"]:
                for engine in engines:
                    if engine in dead:
                        continue
                    geo_applied = int(bool(cfg["geo"]) and gt.GEO_SUPPORTED[engine])
                    for sample in range(1, cfg["samples"] + 1):
                        answer, status = None, "ok"
                        try:
                            answer = gt.ENGINES[engine]["fn"](query, cfg["geo"])
                            if cfg["articles"] and engine == "gemini":
                                gt.resolve_gemini_sources(answer)
                        except gt.QuotaExhausted as e:
                            dead.add(engine)
                            status = f"error: {e}"
                        except Exception as e:  # noqa: BLE001 — record and move on
                            status = f"error: {e}"
                        row = gt.make_row(engine, niche, query, status=status,
                                          answer=answer, geo=cfg["geo"],
                                          geo_applied=geo_applied, sample=sample,
                                          domains=cfg["domains"],
                                          articles=cfg["articles"],
                                          aliases=cfg["aliases"],
                                          save_receipt=False)
                        receipt = (gt.build_receipt(answer, geo=cfg["geo"],
                                                    geo_applied=geo_applied,
                                                    sample=sample)
                                   if answer else None)
                        with Session() as s:
                            a = Answer(
                                id=row["row_id"], run_id=run_id, engine=engine,
                                niche=niche, query=query, model=row["model"],
                                model_requested=(answer or {}).get("model_requested", ""),
                                geo=cfg["geo"], geo_applied=geo_applied,
                                sample=sample, status=status,
                                answer_chars=row["answer_chars"],
                                n_sources=row["n_sources"],
                                searches=(answer or {}).get("searches"),
                                usage=(answer or {}).get("usage"),
                                receipt=receipt,
                                all_source_domains=row["all_source_domains"],
                                run_ts=datetime.strptime(row["run_ts"],
                                                         "%Y-%m-%dT%H:%M:%SZ"))
                            s.add(a)
                            for d in cfg["domains"]:
                                s.add(Hit(answer_id=a.id, domain=d,
                                          src=row[f"src_{d}"], txt=row[f"txt_{d}"]))
                            for k in cfg["articles"]:
                                if row[f"art_{k}"]:
                                    s.add(ArticleHit(answer_id=a.id, article_key=k))
                            s.get(Run, run_id).done_calls += 1
                            s.commit()
                        if engine in dead:
                            break
                        time.sleep(SLEEP_SEC)
    except Exception as e:  # noqa: BLE001 — surface on the run, don't crash the app
        error = str(e)
    finally:
        for key, val in old_models.items():
            if val is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = val
        with Session() as s:
            run = s.get(Run, run_id)
            run.status = "error" if error else "done"
            run.error = error or ""
            run.active = None  # free the slot for the next run
            run.finished_at = utcnow()
            s.commit()
