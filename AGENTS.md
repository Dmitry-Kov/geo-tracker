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

`.env`, `queries.py`, `results*.csv`, `answers.jsonl`, `geo_tracker.db` and
its `-wal` / `-shm` sidecar files, `backups/`, `data/` (the Docker volume),
`dashboard*.html`, `.qwen/`, `.opencode/`, `opencode.json`. They hold API keys
or client data and are gitignored; `.dockerignore` keeps the same files out
of the image. The repository is public: do not add client names or domains
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
- **The active slot is global.** One brand's pending or running run disables
  Run now on every brand, and the page says another run is active.
  (`test_run_now_disabled_when_another_brand_holds_the_slot`)
- **A brand with an active or queued run cannot be deleted** — the run thread
  still writes to its rows. And a run whose row has vanished stops instead of
  crashing. (`test_delete_refused_while_run_active_or_queued`,
  `test_execute_run_survives_a_deleted_run_row`)
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
- **What is reachable without login is an explicit list:** `/login`,
  `/logout`, `/static`, `/healthz` and the routes under `/share/`. Every
  other route depends on `auth_guard`, and FastAPI's `/docs`, `/redoc` and
  `/openapi.json` are switched off — they are public by default and list
  every route of the admin service. A new public route is added to the
  list in the test on purpose, never by leaving the guard off.
  (`test_only_listed_routes_are_open_without_login`,
  `test_api_docs_are_not_served`)
- **`/healthz` returns "ok" after a database query and nothing else.** The
  host guard lets GET/HEAD `/healthz` through on a loopback name even when
  `APP_ALLOWED_HOSTS` lists only the public domain (a container healthcheck
  has no other way in); every other path on loopback is still refused then.
  Do not widen that exception. (`test_healthz_with_password_and_allowed_hosts`)

### Client share links (service)

`/share/{token}` is the one part of the service open to people without a
login. Treat every change under it as a change to a public site.

- **The token is the access, and it opens exactly one brand.** Runs, answers
  and exports of any other brand read as "not found", as does an unknown or
  revoked token. Rotating replaces the token; the old link dies at once.
  (`test_cross_brand_isolation`, `test_share_lifecycle_enable_rotate_revoke`)
- **Read-only.** No route under `/share/` changes state; management
  (`/brands/{id}/share`, `.../share/revoke`) is behind login.
  (`test_shared_pages_are_public_read_only_and_headed`,
  `test_admin_pages_still_require_login`)
- **Every `/share/` response carries `SHARE_HEADERS`** — `Referrer-Policy:
  no-referrer`, so the token in the URL does not leak when a client clicks a
  source link, and `X-Robots-Tag: noindex`. Error pages included.
  (`test_unknown_token_is_404_with_share_headers`)
- **Nothing internal reaches the client.** No API usage, token counts or
  cost, no admin links, no cancel button, no polling of admin endpoints.
  Templates branch on `share_mode`; a new block on a shared template must
  decide which side it is on.
  (`test_internal_usage_hidden_on_share_but_shown_to_admin`)
- **Shared pages and exports are built by the same code as the admin ones**
  (`run_context()`, `html_export()`, `docx_export()`), so they read the
  run's snapshot and carry `incomplete_notice()`.
  (`test_shared_cancelled_run_says_incomplete_everywhere`)

### Database (service)

- **Schema changes go through Alembic.** Edit the models in `app/db.py`, then
  `alembic revision --autogenerate`, review the result by hand, and test the
  upgrade on a database with data in it. Never `create_all` on a live path.
  (`test_baseline_matches_create_all_schema`)
- **A copy is made before any migration** (`backup_database()`, into
  `backups/` next to the database). A start with nothing to migrate writes
  nothing. (`test_old_main_db_adopted_with_data_and_backup`,
  `test_repeat_startup_changes_nothing`)
- **Every connection sets `busy_timeout`, `foreign_keys=ON` and WAL**
  (`make_engine()`); SQLite pragmas are per connection, and without
  `foreign_keys` the cascades are not enforced.
  (`test_sqlite_pragmas_on_every_connection`,
  `test_delete_finished_brand_with_all_children_leaves_no_orphans`)
- **Copy the database only through SQLite's backup API** — `backup_database()`
  or `python -m app.backup`. In WAL mode the newest rows live in the `-wal`
  file, so copying `geo_tracker.db` alone gives a stale or broken copy.
  (`test_online_backup_of_live_wal_db_captures_wal_rows`)
- **Migrations run with `foreign_keys` off on their own connection** and turn
  it back on: batch mode recreates tables, and dropping a referenced table
  fails otherwise. Test every migration on a database with data.
  (`test_upgrade_0002_with_data_to_0003`)
- **Scheduled backups verify before they prune.** A copy that fails
  `integrity_check` is deleted and the older copies are kept; pre-migration
  copies in `backups/` are never pruned.
  (`test_failed_verification_deletes_bad_copy_without_pruning`,
  `test_scheduled_backups_stay_separate_from_pre_migration_snapshots`)
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
- **Runs are compared only on equal footing.** `dynamics.compare()` refuses
  runs that are not `done` or differ in matching-rules version or geo, and a
  domain whose aliases differ; verdicts use the Holm-adjusted p across all
  pairs of one comparison; the change shown is the tested quantity. The
  history marks the same breaks (`basis_change()`) instead of drawing them
  as a trend. (`test_fwer_no_change_18_pairs_at_most_5_percent`,
  `test_history_marks_runs_not_comparable_with_the_previous_one`)
- **Permutation counts are exact.** `paired_pvalue()` sums integers on a
  common denominator. Floats lose ties and make p too small.
  (`test_pvalue_counts_ties_exactly`)
- **Reports are white-label.** No publisher or agency name in the source:
  the name and accent color come from `REPORT_BRAND` / `REPORT_ACCENT`
  through `resolve_branding()`, which accepts only `#RRGGBB` for the color.
  (`test_report_sources_and_env_example_have_no_publisher_branding`,
  `test_invalid_accent_never_reaches_css_or_docx`)
- **Everything written into report markup is escaped.** Queries, titles,
  domains and the brand name are untrusted: in the dashboard they pass
  through `esc()`, and the embedded data cannot close its `<script>`.
  (`test_dashboard_escapes_hostile_queries_and_titles`,
  `test_dashboard_hostile_brand_and_subject_stay_escaped`)
- **The third-party sources table is an aggregation of stored fields**
  (`unowned_cited_sources()`), read through the run's snapshot. It does not
  change what counts as a hit, so it does not bump the rules version.
  (`test_sources_gap_respects_the_run_snapshot`)
- **The CSV schema guard stays.** `check_results_schema()` refuses to append
  to a `results.csv` with other columns. (`test_schema_guard_rejects_old_header`)

### Deployment (Docker)

- **One container, one process, port 8000 not published.** Only Caddy on the
  compose network reaches the app. That is the sole reason uvicorn may run
  with `--forwarded-allow-ips=*`; publish the port and client addresses
  (and with them the login limiter) can be forged.
- **Caddy serves TCP only** (`protocols h1 h2`): the server it shares may
  have another service on 443/udp.
- **The app runs as UID 10001**; its database and backups live in `/data`,
  the only mounted volume.
- Run a deploy between runs: a restart ends the active run.

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
| p-values too small because float sums lost ties | comparing with exact arithmetic |
| History drew an added alias as growth from 0% to 100% | seeding runs with a config change |
| Geo was documented as unsupported by an API that supports it | reading the API docs |
| `/docs` and `/openapi.json` served the route map without login | listing routes that lack the login guard |
| The database's `-wal` file was not gitignored after WAL was turned on | `git check-ignore` on the new files |

## Known gaps

Not bugs to fix in passing — each needs a decision or its own task.

- Whether a requested geo changes the answers has not been measured.
- A legacy database older than the Alembic baseline is stamped without a
  check.
- Backups stay on the same disk as the database; nothing copies them off the
  server.
- No limit on what a run may spend; scheduled runs are not gated by the
  >100-call confirmation either.
- A failing call is retried 4 times even when the error cannot go away (an
  invalid API key), about 33 s per call, and a cancel waits for it.
- Share tokens appear in the server's access log, and shared pages do not
  send `Cache-Control: no-store`.
- CI does not build the Docker image.
