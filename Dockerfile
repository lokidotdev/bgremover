# CPU image for a regular VPS (no GPU). The model (~1 GB) is downloaded on first start into
# /app/models, so mount persistent volumes on /app/models and /app/data.

FROM python:3.12-slim-bookworm AS build
COPY --from=ghcr.io/astral-sh/uv:0.13.0 /uv /bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --no-default-groups --group cpu
COPY bgremover ./bgremover

FROM python:3.12-slim-bookworm
RUN useradd --create-home --uid 1000 app
WORKDIR /app
COPY --from=build --chown=app:app /app /app
RUN mkdir -p models data && chown app:app models data
USER app

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    BGR_HOST=0.0.0.0 \
    BGR_PORT=8000 \
    BGR_WORKERS=2 \
    BGR_MAX_MEGAPIXELS=16

VOLUME ["/app/models", "/app/data"]
EXPOSE 8000

# start-period covers the first-boot model download and warm-up.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10m --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if b'\"ok\":true' in urllib.request.urlopen('http://127.0.0.1:8000/health').read() else 1)"

CMD ["python", "-m", "bgremover.main"]
