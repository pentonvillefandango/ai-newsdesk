# syntax=docker/dockerfile:1
# ai-newsdesk app image (V1-S6). Two stages: uv builds the venv, the final stage runs as uid 10001.
FROM python:3.13.16-slim-trixie AS builder
COPY --from=ghcr.io/astral-sh/uv:0.12.23 /uv /uvx /bin/
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_NO_DEV=1 UV_PYTHON_DOWNLOADS=never
WORKDIR /app
# Dependencies first (cached layer), then the project itself, installed non-editable.
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --locked --no-install-project
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-editable

FROM python:3.13.16-slim-trixie
ARG GIT_SHA=unknown
RUN groupadd --gid 10001 app && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin app \
    && mkdir -p /data && chown 10001:10001 /data
WORKDIR /app
COPY --from=builder /app/.venv /app/.venv
COPY config ./config
ENV PATH=/app/.venv/bin:$PATH PYTHONUNBUFFERED=1 DB_PATH=/data/ai-newsdesk.sqlite GIT_SHA=${GIT_SHA}
USER 10001
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4).status == 200 else 1)"]
# --no-proxy-headers: any container on the shared proxy network could forge X-Forwarded-*, so the
# app never trusts them (the login limiter is global; redirects are relative; cookies are Secure).
CMD ["uvicorn", "ai_newsdesk.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", \
     "--no-proxy-headers", "--timeout-graceful-shutdown", "5"]
