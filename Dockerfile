# One image for both roles: the web Deployment runs uvicorn, the worker
# Deployment runs `python main.py worker ...`.
FROM python:3.11-slim

COPY --from=ghcr.io/astral-sh/uv:0.9.5 /uv /usr/local/bin/uv

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH="/opt/venv/bin:$PATH"

# git: dev-team/reviewer clone and push project repos.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Dependencies first so code edits don't invalidate this layer.
# uv.lock is optional (it is gitignored): use it when present, else resolve.
COPY pyproject.toml uv.lock* ./
RUN if [ -f uv.lock ]; then uv sync --frozen --no-dev --no-install-project; \
    else uv sync --no-dev --no-install-project; fi

COPY *.py dashboard.html ./

RUN useradd --uid 10001 --create-home relay \
    && mkdir -p /data \
    && chown relay:relay /data
USER 10001

ENV RELAY_DATABASE_URL=sqlite:////data/agent-relay.db
EXPOSE 8000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
