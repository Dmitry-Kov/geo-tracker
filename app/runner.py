# -*- coding: utf-8 -*-
"""Execute brand runs: the measurement core's adapters → DB.

One run at a time (single-user slice). The guarantee lives in the database:
Run.active is UNIQUE, so a second pending/running run cannot be inserted no
matter how the triggers interleave. That is also what makes the per-brand
model overrides (process-wide env vars) safe.
"""
import math
import os
import time
from datetime import datetime

from sqlalchemy import or_
from sqlalchemy.exc import IntegrityError

import geo_tracker as gt

from .db import Answer, ArticleHit, Brand, Hit, Run, utcnow

SLEEP_SEC = 2.0
ACTIVE = ("pending", "running")
# A scheduled tick that has to wait for the active slot is a run too: a row
# with this status, so the wait is visible on the brand and can be cancelled.
QUEUED = "queued"
WAITING = ACTIVE + (QUEUED,)   # everything that is not over yet
# Rough per-call wall time observed in live runs (latency + the 2s pause).
# An estimate shown before launching, not a promise.
EST_SEC_PER_CALL = 15
BIG_RUN_CONFIRM = 100  # more calls than this → explicit confirmation required
MODEL_ENV = {"gemini": "GEMINI_MODEL", "perplexity": "PERPLEXITY_MODEL",
             "openai": "OPENAI_MODEL"}
INTERRUPTED = "interrupted: the service stopped before this run finished"


class _Cancelled(Exception):
    """Internal signal: the run's cancel_requested flag was seen between calls."""


def run_estimate(b: Brand) -> dict:
    """Calls a run of this brand would make right now: queries × engines
    with keys × samples, plus a rough duration. Engines without a key in
    the environment are skipped by execute_run, so they are skipped here."""
    engines = [e for e in b.engines
               if e in gt.ENGINES and os.environ.get(gt.ENGINES[e]["env"])]
    calls = (sum(len(ncfg["queries"]) for ncfg in b.niches.values())
             * len(engines) * b.samples)
    return {"calls": calls, "engines": engines,
            "minutes": math.ceil(calls * EST_SEC_PER_CALL / 60) if calls else 0}


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
                                        Run.status.in_(WAITING))).all()
        for run in stale:
            run.status = "error"
            run.error = INTERRUPTED
            run.active = None
            run.finished_at = utcnow()
        s.commit()
        return len(stale)


def queue_run(Session, brand_id: int, trigger: str = "scheduled"):
    """Record a run that is waiting for the active slot; None if the brand is gone.

    The row holds no slot (active stays NULL) and no config yet — the snapshot
    is taken when the run actually starts, so it measures the brand as it is
    then, not as it was when the wait began.
    """
    with Session() as s:
        if s.get(Brand, brand_id) is None:
            return None
        run = Run(brand_id=brand_id, status=QUEUED, trigger=trigger)
        s.add(run)
        s.commit()
        return run.id


def claim_queued(Session, run_id: int, take_slot: bool = True) -> str:
    """Move a queued run forward. Returns:

    "claimed" — it now holds the active slot as a pending run (execute it);
    "busy"    — still waiting (the slot is taken, or take_slot is False);
    "closed"  — it will not run: cancelled by the user, its brand's schedule
                was removed while it waited, or the run/brand is gone.
    """
    with Session() as s:
        run = s.get(Run, run_id)
        if run is None or run.status != QUEUED:
            return "closed"
        brand = run.brand
        reason = None
        if run.cancel_requested:
            reason = ""
        elif run.trigger == "scheduled" and not brand.schedule:
            reason = "the brand's schedule was removed while this run was waiting"
        if reason is not None:
            run.status = "cancelled"
            run.error = reason
            run.finished_at = utcnow()
            s.commit()
            return "closed"
        if not take_slot:
            return "busy"
        run.active = 1
        run.status = "pending"
        run.config = brand_config(brand)
        try:
            s.commit()
        except IntegrityError:  # another run still holds the slot
            s.rollback()
            return "busy"
        return "claimed"


def close_queued(Session, run_id: int, error: str) -> None:
    """Give up on a queued run, leaving the reason on it."""
    with Session() as s:
        run = s.get(Run, run_id)
        if run is None or run.status != QUEUED:
            return
        run.status = "error"
        run.error = error
        run.finished_at = utcnow()
        s.commit()


STATUS_RU = {"cancelled": "отменён", "error": "прерван с ошибкой"}


def incomplete_notice(run: Run, lang: str = "en"):
    """Text that must accompany any figures taken from a run that is not done;
    None for a completed run. A cancelled or interrupted run has measured only
    part of its matrix: without this line its report reads like a full one."""
    if run.status == "done":
        return None
    if lang == "ru":
        status = STATUS_RU.get(run.status, "ещё не завершён")
        return (f"Неполный прогон — {status}: выполнено {run.done_calls} из "
                f"{run.total_calls} запланированных вызовов. Цифры ниже относятся "
                f"только к собранным ответам и не являются полным измерением.")
    status = run.status if run.status in STATUS_RU else "not finished"
    return (f"Incomplete run — {status}: {run.done_calls} of {run.total_calls} "
            f"planned calls were made. The figures below cover only the answers "
            f"collected and are not a full measurement.")


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
        if run.cancel_requested:
            # Cancelled while queued (e.g. a scheduled tick waiting for the
            # active slot): stop before any call, free the slot.
            run.status = "cancelled"
            run.active = None
            run.finished_at = utcnow()
            s.commit()
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

    dead, error, cancelled = set(), None, False
    try:
        total = (sum(len(ncfg["queries"]) for ncfg in cfg["niches"].values())
                 * len(engines) * cfg["samples"])
        with Session() as s:
            run_row = s.get(Run, run_id)
            if run_row is not None:
                run_row.total_calls = total
                s.commit()

        for niche, ncfg in cfg["niches"].items():
            for query in ncfg["queries"]:
                for engine in engines:
                    if engine in dead:
                        continue
                    geo_applied = int(bool(cfg["geo"]) and gt.GEO_SUPPORTED[engine])
                    for sample in range(1, cfg["samples"] + 1):
                        # Cancellation is honored between calls: the answer
                        # in flight finishes, everything collected is kept.
                        # A run row that is gone (deleted underneath us) is
                        # treated like a cancel: stop instead of crashing.
                        with Session() as s:
                            run_row = s.get(Run, run_id)
                            if run_row is None or run_row.cancel_requested:
                                raise _Cancelled()
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
                            run_row = s.get(Run, run_id)
                            if run_row is not None:
                                run_row.done_calls += 1
                            s.commit()
                        if engine in dead:
                            break
                        time.sleep(SLEEP_SEC)
    except _Cancelled:
        cancelled = True
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
            if run is not None:  # the row may have been deleted mid-run
                run.status = "cancelled" if cancelled else ("error" if error else "done")
                run.error = error or ""
                run.active = None  # free the slot for the next run
                run.finished_at = utcnow()
                s.commit()
