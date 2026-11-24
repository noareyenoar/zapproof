# Zapproof score API, read-only.
#
# The database is mounted, never baked into the image: it is 1.6 GB and changes on a
# rebuild, while the image should not. `docs/deploy_local.md` makes the plain-uvicorn
# path primary; this file exists so a container is possible without the owner needing
# to know Docker.
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_PROJECT_ENVIRONMENT=/app/.venv

RUN pip install --no-cache-dir "uv>=0.5"

WORKDIR /app

# Dependencies first, so a source edit does not re-resolve the environment.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY src ./src
RUN uv sync --frozen --no-dev

# The API computes its repo root from the file location, so the mount point matters:
# data/repindex.db inside the container is the mounted host database.
VOLUME ["/app/data"]

EXPOSE 8000

# Read-only: the API opens SQLite with mode=ro and exposes no write routes. Calling
# the venv's uvicorn directly means the runtime needs no package manager and no
# network access.
CMD ["/app/.venv/bin/uvicorn", "repindex.api.app:app", "--host", "0.0.0.0", "--port", "8000"]
