# -*- coding: utf-8 -*-
"""APScheduler wiring: one cron job per brand with a schedule (cron in UTC).

Runs are strictly one at a time, so scheduled runs queue instead of racing:
brands that share a cron tick execute one after another, and a tick that
lands on a manual run waits for it to finish. A scheduled run that still
cannot start after MAX_WAIT_SEC is recorded on the brand as a skipped run —
never dropped silently.
"""
import threading
import time

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from .db import Brand
from .runner import execute_run, record_skipped, start_run

POLL_SEC = 5.0
MAX_WAIT_SEC = 6 * 3600


def make_scheduler(Session):
    """(scheduler, sync, load_all, stop). Call stop() on shutdown so a queued
    scheduled run stops waiting instead of holding the process open."""
    sched = BackgroundScheduler(timezone="UTC")
    queue = threading.Lock()   # same-tick brands: one after another
    stopping = threading.Event()

    def scheduled_run(brand_id: int) -> None:
        with queue:
            deadline = time.monotonic() + MAX_WAIT_SEC
            while not stopping.is_set():
                with Session() as s:
                    if s.get(Brand, brand_id) is None:
                        return  # brand deleted while the tick was queued
                run_id = start_run(Session, brand_id, trigger="scheduled")
                if run_id is not None:
                    execute_run(Session, run_id)
                    return
                if time.monotonic() >= deadline:
                    record_skipped(
                        Session, brand_id,
                        f"skipped: another run was still active after "
                        f"{MAX_WAIT_SEC // 3600}h of waiting")
                    return
                stopping.wait(POLL_SEC)

    def sync(brand_id: int, schedule: str) -> None:
        job_id = f"brand-{brand_id}"
        if sched.get_job(job_id):
            sched.remove_job(job_id)
        if schedule:
            # misfire_grace_time=None: a tick delayed behind other brands in
            # the worker pool still runs instead of being discarded as late.
            sched.add_job(scheduled_run, CronTrigger.from_crontab(schedule),
                          args=[brand_id], id=job_id, replace_existing=True,
                          misfire_grace_time=None, coalesce=True, max_instances=1)

    def load_all() -> None:
        with Session() as s:
            for b in s.query(Brand).all():
                if b.schedule:
                    sync(b.id, b.schedule)

    return sched, sync, load_all, stopping.set
