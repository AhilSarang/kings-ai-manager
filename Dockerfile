# ---------- base: API and shared code ----------
FROM python:3.12-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    UV_PROJECT_ENVIRONMENT=/opt/venv PATH="/opt/venv/bin:$PATH"
COPY --from=ghcr.io/astral-sh/uv:0.12 /uv /usr/local/bin/uv
RUN useradd --create-home --uid 1000 app
WORKDIR /srv
COPY pyproject.toml uv.lock* ./
RUN uv sync --no-dev --no-install-project $( [ -f uv.lock ] && echo --frozen )
COPY . .
RUN mkdir -p /data/files && chown -R app:app /data /srv
USER app

# ---------- worker ----------
FROM base AS worker
USER root
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates postgresql-client \
    && apt-get clean && rm -rf /var/lib/apt/lists/*
USER app
