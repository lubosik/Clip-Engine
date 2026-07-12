FROM python:3.11-slim

# All-in-one image: web API + producer/scheduler/analytics crons in one
# container. Railway volumes mount to a single service only, so the clip
# storage volume lives here and every component shares it via STORAGE_DIR.

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        ffmpeg \
        libpq5 \
        libpq-dev \
        gcc \
        # fonts
        fonts-dejavu-core \
        fontconfig \
        # OpenCV headless system libs
        libglib2.0-0 \
        libsm6 \
        libxext6 \
        libxrender1 \
        libgl1 \
        curl \
    && rm -rf /var/lib/apt/lists/*

# supercronic: container-friendly cron runner (skips overlapping runs).
ARG SUPERCRONIC_VERSION=v0.2.33
RUN curl -fsSL -o /usr/local/bin/supercronic \
        "https://github.com/aptible/supercronic/releases/download/${SUPERCRONIC_VERSION}/supercronic-linux-amd64" \
    && chmod +x /usr/local/bin/supercronic

RUN pip install --no-cache-dir yt-dlp

WORKDIR /app

COPY pyproject.toml alembic.ini ./
COPY core/ ./core/
COPY producer/ ./producer/
COPY scheduler/ ./scheduler/
COPY meme/ ./meme/
COPY web/ ./web/
COPY campaigns/ ./campaigns/
COPY assets/ ./assets/
COPY migrations/ ./migrations/
COPY deploy/crontab deploy/start.sh ./deploy/
RUN chmod +x ./deploy/start.sh

# torch is required by `punctuators` (punctuation restoration for clip
# boundaries). Install the CPU-ONLY wheel FIRST so the punctuators install
# below finds torch already satisfied — the default PyPI wheel drags in ~2GB
# of CUDA libraries the Railway container can never use; the cpu wheel is
# ~200MB. Decision documented 2026-07-12 (reviewer issue #2): torch is
# intentionally present in this image.
RUN pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu

RUN pip install --no-cache-dir \
    fastapi \
    "uvicorn[standard]" \
    sqlalchemy>=2 \
    alembic \
    psycopg2-binary \
    pydantic>=2 \
    pydantic-settings \
    pyyaml \
    apify-client \
    httpx \
    anthropic \
    faster-whisper \
    opencv-python-headless \
    mediapipe \
    python-multipart \
    jinja2 \
    boto3 \
    modal \
    pillow \
    punctuators \
    && pip install --no-cache-dir --no-deps -e .

RUN mkdir -p /data/clips/raw /data/clips/clips /data/clips/thumbs /data/clips/logs

ENV PORT=8000
ENV PYTHONUNBUFFERED=1
ENV STORAGE_DIR=/data/clips

HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:${PORT}/healthz')" || exit 1

EXPOSE $PORT

CMD ["./deploy/start.sh"]
