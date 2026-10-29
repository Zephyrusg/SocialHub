FROM mwader/static-ffmpeg:9.0.2-1@sha256:f5d100184884eb3e82b0eb14bb5ebf98da99826ac824989720297fb9ef21c822 AS media-source
FROM python:3.13-slim@sha256:9d2e5553305c7c7b0097999bb17187c69b921ccd6bc9d40e4bb5ebe652c00285
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
COPY --from=media-source /ffmpeg /usr/local/bin/ffmpeg
RUN useradd --uid 10001 --create-home worker && mkdir /work && chown worker:worker /work
WORKDIR /worker
COPY app/services/normalization_protocol.py ./app/services/normalization_protocol.py
COPY normalizer/server.py ./server.py
USER worker
HEALTHCHECK --interval=30s --timeout=3s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8090/healthz',timeout=2)"
CMD ["python", "server.py"]
