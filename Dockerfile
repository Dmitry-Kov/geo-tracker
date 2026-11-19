# geo-tracker service: one process, non-root, behind Caddy (docker-compose.yml).
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    GEO_DB_PATH=/data/geo_tracker.db

# Non-root user; /data is the bind-mounted volume holding the SQLite DB
# (WAL) and backups/ — it must be writable by this UID (chown 10001:10001
# on the host directory when creating it as root).
RUN groupadd --gid 10001 app \
    && useradd --uid 10001 --gid app --no-create-home --shell /usr/sbin/nologin app \
    && mkdir -p /app /data \
    && chown 10001:10001 /app /data

WORKDIR /app

COPY requirements-lock.txt ./
RUN pip install --no-cache-dir -r requirements-lock.txt

# Runtime only: the measurement core, the service, templates/static and the
# Alembic migrations. No tests, no queries.py, no data (see .dockerignore).
COPY geo_tracker.py sov_stats.py report_html.py report_docx.py alembic.ini ./
COPY migrations/ migrations/
COPY app/ app/

USER 10001:10001

EXPOSE 8000

# Liveness + DB check without a session cookie; loopback is exempt from the
# host guard for GET/HEAD /healthz (app.main.request_guard_error).
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=3).read()"]

# ONE process — no --workers: the scheduler, the run queue and the login
# limiter live in it (AGENTS.md). --proxy-headers with a wildcard is safe
# ONLY because docker-compose.yml keeps port 8000 internal to the compose
# network: only Caddy can reach it, so X-Forwarded-For cannot be spoofed.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips=*"]