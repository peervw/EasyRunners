# syntax=docker/dockerfile:1.7
FROM ghcr.io/astral-sh/uv:0.12.3 AS uv

FROM python:3.12-slim-bookworm AS manager
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PATH=/app/.venv/bin:$PATH
WORKDIR /app
RUN --mount=type=cache,id=easyrunners-manager-apt-cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,id=easyrunners-manager-apt-lists,target=/var/lib/apt,sharing=locked \
    apt-get update && apt-get install -y --no-install-recommends ca-certificates curl
COPY --from=uv /uv /uvx /bin/
COPY pyproject.toml uv.lock README.md ./
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev
RUN mkdir -p /data && chmod 700 /data
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=3)"
CMD ["easyrunners", "serve"]
