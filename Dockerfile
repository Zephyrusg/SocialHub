FROM mwader/static-ffmpeg:9.0.2-1@sha256:f5d100184884eb3e82b0eb14bb5ebf98da99826ac824989720297fb9ef21c822 AS media-source

FROM python:3.13-slim@sha256:9d2e5553305c7c7b0097999bb17187c69b921ccd6bc9d40e4bb5ebe652c00285 AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PATH="/srv/socialhub/.venv/bin:$PATH"
COPY --from=media-source /ffprobe /usr/local/bin/ffprobe
COPY docker/ffprobe-source.txt /usr/share/doc/socialhub/ffprobe-source.txt
RUN useradd --uid 10001 --create-home socialhub && mkdir -p /data/media && chown -R socialhub:socialhub /data
WORKDIR /srv/socialhub
EXPOSE 8000
HEALTHCHECK --interval=20s --timeout=5s --start-period=20s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/readyz',timeout=3)"

FROM base AS app-build
COPY --from=ghcr.io/astral-sh/uv:0.13.5@sha256:32a7bb39b496babbd997b0a39a4d66c126bdaf5661b302bf6f06abda8deca6ff /uv /usr/local/bin/uv
ENV UV_PYTHON_DOWNLOADS=0 UV_LINK_MODE=copy UV_NO_CACHE=1
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project
COPY app ./app
RUN uv sync --locked --no-dev --no-editable
COPY migrations ./migrations
COPY alembic.ini ./
COPY scripts ./scripts

FROM app-build AS test
RUN uv sync --locked --extra test --no-editable
COPY tests ./tests
COPY normalizer ./normalizer
USER socialhub
CMD ["python", "-m", "pytest", "-q", "-p", "no:cacheprovider"]

FROM base AS runtime
COPY --from=app-build /srv/socialhub /srv/socialhub
USER socialhub
CMD ["python", "-m", "app.main"]
