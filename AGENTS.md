# Working on geo-tracker

Rules for anyone changing this repository — a person or a coding agent.
Read this before the first edit. `QWEN.md` and `CLAUDE.md` only point here.

geo-tracker measures how often a brand's domains are cited by AI engines with
web search (Gemini, Perplexity, OpenAI). Two front ends share one measurement
core: a CLI (`geo_tracker.py`, history in `results.csv`) and a single-user web
service (`app/`, history in SQLite). The product's promise is that every
number can be defended, so correctness of the measurement beats features.

## How to work

- Start from a fresh `main`: `git checkout main && git pull`. One branch and
  one pull request per task.
- Tests must pass before you say a task is done, on both Python versions CI
  runs (3.9 and 3.12): `.venv/bin/python -m pytest tests/ -q`.
- A change to the UI is checked in a real browser, and a change to startup or
  logging on a real `uvicorn` process. The test client did not see the bugs
  listed under "What tests missed".
- Every bug fix comes with a test that fails without the fix.
- The pull request description has a "Not verified" section. List what you
  did not check; do not leave it out because the list is short.
- Do not claim an external API "cannot" do something without reading its
  current documentation. Write "not implemented here" when that is the truth.

## Commands

```bash
.venv/bin/pip install -r requirements-dev.txt   # everything, incl. tests
.venv/bin/python -m pytest tests/ -q            # no API keys, no network
.venv/bin/uvicorn app.main:app                  # service on 127.0.0.1:8000
.venv/bin/python geo_tracker.py run --limit 1   # CLI; spends API quota
```

## Never commit

`.env`, `queries.py`, `results*.csv`, `answers.jsonl`, `geo_tracker.db`,
`backups/`, `dashboard*.html`, `.qwen/`. They hold API keys or client data and
are gitignored. The repository is public: do not add client names or domains
to code, docs, tests, commit messages or screenshots.

## Guarantees — do not break these

Each one exists because the opposite once shipped or nearly did. The named
tests fail if the guarantee is broken; do not weaken them to make a change
pass.

### Runs (service)

- **One run at a time, enforced by the database.** `Run.active` is `UNIQUE`:
  1 while pending or running, `NULL` afterwards. `start_run()` inserts and
  treats an integrity error as "busy". Never go back to check-then-insert —
  two triggers both passed that check 40 times out of 40.
  (`test_two_simultaneous_triggers_start_exactly_one_run`)
- **A run cannot outlive its process.** `recover_interrupted()` runs at
  startup, before anything can start a run, and closes whatever was left
  active. (`test_restart_closes_interrupted_run_and_frees_the_slot`)
- **A finished run is read against its own snapshot.** `Run.config` stores the
  brand's configuration at start. Pages and exports go through `run_rows(run)`;
  never read `brand.domains` / `brand.niches` for a run's results.
  (`test_editing_the_brand_does_not_rewrite_a_finished_run`)
- **All rows of a run share one run date**, so a run crossing 00:00 UTC is
  reported whole. (`test_run_crossing_utc_midnight_is_reported_whole`)
- **Scheduled runs queue, they do not race or vanish.** Same-tick brands run
  one after another; a tick that meets a manual run waits; after
  `MAX_WAIT_SEC` it is closed as a skipped run.
  (`test_brands_on_the_same_cron_tick_run_one_after_another`,
  `test_scheduled_tick_that_cannot_start_leaves_a_trace`)
- **A waiting tick is a visible `queued` run.** It can be cancelled, and
  removing the brand's schedule cancels it. Never wait without a row: an
  invisible wait cannot be stopped.
  (`test_waiting_scheduled_tick_is_listed_and_cancellable`,
  `test_removing_the_schedule_stops_a_waiting_tick`)
- **Cancel is honored between calls** and leaves status `cancelled` with the
  answers collected so far; the page shows `cancelling` meanwhile.
  (`test_cancel_between_calls_keeps_answers_and_frees_slot`)
- **A run that is not `done` never reads like a full report.** Its page and
  every export carry `incomplete_notice()`. Any new export or summary must
  use it. (`test_exports_of_a_cancelled_run_say_so`)
- **Schedules are crontab lines in UTC.** Build triggers with
  `cron_trigger()`, never `CronTrigger.from_crontab()`: that one numbers
  weekdays from Monday and uses the server's local timezone.
  (`test_cron_means_what_crontab_means_in_utc`)
- **One process.** The scheduler, the run queue and the login limiter live in
  it. No `--workers`; no `--reload` while a run is in flight.

### Requests and settings (service)

- **Cross-site requests are refused.** `request_guard_error()` rejects a
  state-changing request whose Origin/Referer names another host, and
  `allowed_hosts()` limits the host names served. Without `APP_PASSWORD` the
  service answers on loopback names only. (`test_cross_site_post_is_refused`,
  `test_local_mode_answers_on_loopback_names_only`)
- **`.env` is loaded first thing in `create_app()`.** Settings read while the
  app is built (`APP_SECRET`, `APP_HTTPS_ONLY`, `GEO_DB_PATH`) were silently
  ignored when it was loaded later. (`test_settings_from_dotenv_are_applied`)
- **Tests never read the real `.env`.** `tests/conftest.py` switches
  `load_env` off for the whole test process; keep it that way.
- **`queries.py` is CLI configuration only.** The service must start without
  it. (`test_service_starts_without_queries_py`)

### Database (service)

- **Schema changes go through Alembic.** Edit the models in `app/db.py`, then
  `alembic revision --autogenerate`, review the result by hand, and test the
  upgrade on a database with data in it. Never `create_all` on a live path.
  (`test_baseline_matches_create_all_schema`)
- **A copy is made before any migration** (`backup_database()`, into
  `backups/` next to the database). A start with nothing to migrate writes
  nothing. (`test_old_main_db_adopted_with_data_and_backup`,
  `test_repeat_startup_changes_nothing`)
- **No database path goes through a text URL.** `make_engine()` uses
  `URL.create()`, and `migrate_database()` hands its own connection to
  Alembic. SQLAlchemy 2.1 percent-decodes URLs parsed from text; 2.0 does not.
  (`test_database_path_with_a_percent_sign`)
- **`migrations/env.py` calls `fileConfig(..., disable_existing_loggers=False)`.**
  The default silences uvicorn after a migrating start.
  (`test_migrating_does_not_silence_the_servers_loggers`)

### Measurement (core, both front ends)

- **Matching rules are versioned.** Any change to what counts as a hit changes
  the numbers: bump `MATCH_RULES_VERSION`, describe the change in the README,
  and keep it in the run snapshot. Runs under different versions are not
  comparable. (`test_run_config_snapshots_the_matching_rules_version`)
- **Source titles decide `src` only for `HOST_TITLE_ENGINES` (Gemini).** For
  other engines the title is the cited page's own headline; their real URLs
  decide. (`test_page_title_of_another_site_is_not_a_citation`)
- **Gemini's `src` uses the title, not the resolved redirect**, on purpose, so
  history stays comparable. Resolved URLs feed the article check only.
- **Aliases match whole words; a trailing `*` matches word starts.**
  (`test_alias_with_star_matches_inflected_forms`,
  `test_empty_alias_never_matches`)
- **Record what happened, not what was asked.** `model` is the model the API
  says served the answer; `geo_applied` is 1 only when the geo was actually
  sent to that engine (`GEO_SUPPORTED`); usage and cost are the API's own
  numbers, never a price table. Unknown is shown as "—", never as 0.
  (`test_gemini_records_served_model_behind_alias`)
- **The unit of evidence is the query, not the answer.** `sov_stats.py`:
  Wilson interval over queries when fewer than `MIN_BOOTSTRAP_QUERIES` stand
  behind a cell or the share is exactly 0% / 100%, seeded cluster bootstrap
  otherwise. More samples per query must not narrow the interval.
  (`test_all_misses_is_not_reported_as_certainty`,
  `test_more_samples_per_query_do_not_narrow_the_interval`)
- **The CSV schema guard stays.** `check_results_schema()` refuses to append
  to a `results.csv` with other columns. (`test_schema_guard_rejects_old_header`)

## What tests missed

Found only by running the real thing. Check these classes of change by hand.

| Bug | How it was found |
| --- | --- |
| Run page stayed on "running 0/N" after the run finished | real browser |
| `0 9 * * 1` fired on Tuesdays at 09:00 local time | settings page in a browser |
| Server printed no logs after a migrating start | real `uvicorn` process |
| Settings from `.env` ignored | starting the app with only a `.env` |
| A database path with `%` broke on Python 3.12 only | CI, different SQLAlchemy |
| A third-party page title counted as the brand being cited | hand-made examples |
| Export of a cancelled run looked like a full report | exporting one by hand |
| A scheduled tick waiting for its turn could not be seen or stopped | driving the scheduler by hand |
| Geo was documented as unsupported by an API that supports it | reading the API docs |

## Known gaps

Not bugs to fix in passing — each needs a decision or its own task.

- API answers are not what a person sees in the consumer apps; the README
  says so, reports should too.
- Whether a requested geo changes the answers has not been measured.
- A legacy database older than the Alembic baseline is stamped without a
  check.
- No regular backups (only before migrations), no deployment recipe, no
  limit on what a run may spend.
