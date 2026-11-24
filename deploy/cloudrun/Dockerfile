# Hosted Zapproof: the score API and the remote MCP endpoint in one container.
#
# Unlike the repository-root Dockerfile, this image is self-contained: it carries the
# compact hosted database (data/hosted.db, built by scripts/build_hosted_db.py)
# instead of mounting the ingest database. That is what a scale-to-zero host needs.
#
# Build from the repository root:
#   gcloud builds submit --config deploy/cloudrun/cloudbuild.yaml .
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    UV_LINK_MODE=copy

RUN pip install --no-cache-dir "uv>=0.5"

WORKDIR /app

# Dependencies first, so editing a source file does not re-resolve the environment.
# README.md and LICENSE are inputs to the project build (hatchling), not docs here.
COPY pyproject.toml uv.lock README.md LICENSE ./
RUN uv sync --frozen --no-dev --no-install-project

COPY src ./src
RUN uv sync --frozen --no-dev

# The compact database is read-only at runtime; the API opens it with SQLite mode=ro.
COPY data/hosted.db ./data/hosted.db

# Telemetry is JSON lines on stdout; the container filesystem is never written.
RUN useradd --create-home --uid 1000 app \
    && chown -R app:app /app
USER app

ENV REPINDEX_DB=/app/data/hosted.db \
    ZAPPROOF_TRUST_PROXY=1 \
    ZAPPROOF_MCP_STATELESS=1 \
    ZAPPROOF_MCP_JSON_RESPONSE=1 \
    USAGE_METRICS=1 \
    USAGE_SINK=stdout

EXPOSE 8080

# No access log: it would write caller addresses to disk, which the privacy rule
# forbids. Cloud Run's own request log is excluded at the project level by deploy.sh.
CMD ["sh", "-c", "exec /app/.venv/bin/uvicorn repindex.hosted:app --host 0.0.0.0 --port \"${PORT:-8080}\" --no-access-log"]
