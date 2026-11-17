# -*- coding: utf-8 -*-
"""APScheduler wiring: one cron job per brand with a schedule (cron in UTC).

Runs are strictly one at a time, so scheduled runs queue instead of racing:
brands that share a cron tick execute one after another, and a tick that
lands on a manual run waits for it to finish. A tick that has to wait is a
run with status "queued" — visible on the brand and cancellable; removing
the brand's schedule cancels it too. One that still cannot start after
MAX_WAIT_SEC is closed as skipped — never dropped silently.
"""
import re
import threading
import time

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from .db import Brand
from .runner import (claim_queued, close_queued, execute_run, queue_run,
                     start_run)

POLL_SEC = 5.0
MAX_WAIT_SEC = 6 * 3600
CRON_WEEKDAYS = ["sun", "mon", "tue", "wed", "thu", "fri", "sat"]  # cron: 0 = Sunday


def _weekday_names(field: str) -> str:
    """A crontab weekday field (0 or 7 = Sunday, 1 = Monday…) as day names.

    APScheduler numbers weekdays from Monday = 0, so passing crontab numbers
    through unchanged shifts every schedule by a day. Names mean the same in
    both, so numeric fields are expanded to names; fields already written
    with names are left as they are.
    """
    if field == "*" or re.search(r"[A-Za-z]", field):
        return field
    days = set()
    for part in field.split(","):
        span, _, step = part.partition("/")
        step = int(step) if step else 1
        if span == "*":
            first, last = 0, 6
        elif "-" in span:
            first, last = (int(x) for x in span.split("-", 1))
        else:
            first = last = int(span)
        if not (0 <= first <= last <= 7) or step < 1:
            raise ValueError(f"invalid weekday field: {field!r} (use 0-7, 0 and 7 = Sunday)")
        days.update(d % 7 for d in range(first, last + 1, step))
    if len(days) == 7:
        return "*"
    return ",".join(CRON_WEEKDAYS[d] for d in sorted(days))


def cron_trigger(schedule: str) -> CronTrigger:
    """A standard 5-field crontab line as a trigger, evaluated in UTC.

    Not CronTrigger.from_crontab: that reads the weekday numbers in
    APScheduler's own numbering and, without an explicit timezone, uses the
    server's local one — "0 9 * * 1" would fire on Tuesdays at 09:00 local.
    """
    fields = schedule.split()
    if len(fields) != 5:
        raise ValueError("expected 5 fields: minute hour day month weekday")
    minute, hour, day, month, weekday = fields
    return CronTrigger(minute=minute, hour=hour, day=day, month=month,
                       day_of_week=_weekday_names(weekday), timezone="UTC")


def make_scheduler(Session):
    """(scheduler, sync, load_all, stop). Call stop() on shutdown so a queued
    scheduled run stops waiting instead of holding the process open."""
    sched = BackgroundScheduler(timezone="UTC")
    queue = threading.Lock()   # same-tick brands: one after another
    stopping = threading.Event()

    def scheduled_run(brand_id: int) -> None:
        deadline = time.monotonic() + MAX_WAIT_SEC
        run_id = None  # the queued run, once this tick has to wait
        locked = queue.acquire(blocking=False)
        try:
            while not stopping.is_set():
                if not locked:
                    # Behind another scheduled run: wait for the queue, but as
                    # a visible queued run so the wait can be cancelled.
                    if run_id is None:
                        run_id = queue_run(Session, brand_id)
                        if run_id is None:
                            return  # brand deleted
                    locked = queue.acquire(timeout=POLL_SEC)
                elif run_id is None:
                    started = start_run(Session, brand_id, trigger="scheduled")
                    if started is not None:
                        execute_run(Session, started)
                        return
                    run_id = queue_run(Session, brand_id)  # a manual run holds the slot
                    if run_id is None:
                        return  # brand deleted
                outcome = claim_queued(Session, run_id, take_slot=locked)
                if outcome == "claimed":
                    execute_run(Session, run_id)
                    return
                if outcome == "closed":
                    return  # cancelled, schedule removed, or brand deleted
                if time.monotonic() >= deadline:
                    close_queued(
                        Session, run_id,
                        f"skipped: another run was still active after "
                        f"{MAX_WAIT_SEC // 3600}h of waiting")
                    return
                if locked:
                    stopping.wait(POLL_SEC)
        finally:
            if locked:
                queue.release()

    def sync(brand_id: int, schedule: str) -> None:
        job_id = f"brand-{brand_id}"
        if sched.get_job(job_id):
            sched.remove_job(job_id)
        if schedule:
            # misfire_grace_time=None: a tick delayed behind other brands in
            # the worker pool still runs instead of being discarded as late.
            sched.add_job(scheduled_run, cron_trigger(schedule),
                          args=[brand_id], id=job_id, replace_existing=True,
                          misfire_grace_time=None, coalesce=True, max_instances=1)

    def load_all() -> None:
        with Session() as s:
            for b in s.query(Brand).all():
                if b.schedule:
                    sync(b.id, b.schedule)

    return sched, sync, load_all, stopping.set
