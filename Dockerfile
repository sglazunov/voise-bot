# Voice Transcriber — Linux, with the Telemost recorder bot.
# Recognition + protocol + Weeek + cloud, PLUS a headed Chromium bot that joins
# a Telemost call inside a virtual display (Xvfb) and records screen + audio
# (x11grab + PulseAudio). CPU-only; no GPU needed.
# Pin to Debian bookworm: the default slim tag moved to trixie, whose loader
# rejects ctranslate2 4.4.0's executable-stack flag ("cannot enable executable
# stack as shared object requires").

# ---- Stage 1: build the React SPA (Vite) → /build/dist -----------------------
FROM node:20-slim AS frontend
WORKDIR /build
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

# ---- Stage 2: the Python app + recorder bot ---------------------------------
FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONUTF8=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

# System deps:
#   ffmpeg                       — decode for transcription + x11grab/pulse capture
#   tesseract-ocr(+rus)          — on-screen text OCR
#   xvfb, x11-utils              — virtual display for the headed bot browser
#   pulseaudio                   — audio server + null-sink (meeting loopback)
#   dbus-x11, fonts, procps      — Chromium runtime niceties / debugging
#   gosu                         — drop root to the app user at start
#   tzdata, ca-certificates, curl
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        tesseract-ocr tesseract-ocr-rus \
        xvfb x11-utils \
        pulseaudio \
        dbus-x11 \
        fonts-liberation fonts-noto-color-emoji \
        procps gosu \
        tzdata ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Python deps first (better layer caching).
COPY requirements.txt ./
RUN pip install --upgrade pip wheel && pip install -r requirements.txt

# Д7: optional diarization layer (pyannote + CPU torch, ~2.5 GB) — for speaker
# separation of UPLOADED audio files (bot videos use the tile-based speaker_id
# instead). Build with:  DIARIZATION=1 docker compose up -d --build app
# Needs RAM >= 8 GB at runtime; leave 0 on small hosts.
ARG DIARIZATION=0
# Метка сборки: короткий хэш коммита, из которого собран образ. Показывается в
# /healthz и в шапке интерфейса — иначе не отличить, стоит ли на бою новый код
# или контейнер всё ещё из старого образа (02.09: git был свежий, образ — нет).
ARG VTX_BUILD=dev
ENV VTX_BUILD=${VTX_BUILD}
RUN if [ "$DIARIZATION" = "1" ]; then \
      pip install --no-cache-dir "torch>=2.2,<3" "torchaudio>=2.2,<3" --index-url https://download.pytorch.org/whl/cpu \
      && pip install --no-cache-dir "pyannote.audio>=3.3,<4"; \
    fi

# Chromium for the bot, installed to a world-readable path so the non-root app
# user can use it. playwright (the pip pkg) is already in requirements.txt;
# --with-deps pulls the browser's own OS libraries.
RUN playwright install --with-deps chromium \
    && chmod -R a+rX /ms-playwright

# App code + entrypoint.
COPY app/ ./app/
COPY scripts/ ./scripts/
# Тесты едут в образ: сервер — единственное место, где их запускают
# (`docker compose exec app python -m pytest`). Каталог маленький и в рантайме
# не используется.
COPY tests/ ./tests/
# Built SPA from stage 1 — FastAPI serves the whole UI at the site root.
COPY --from=frontend /build/dist ./frontend/dist
COPY Modelfile ./Modelfile
COPY docker/ ./docker/
RUN chmod +x docker/*.sh

# Non-root user to run Xvfb/PulseAudio/Chromium/uvicorn (Pulse dislikes root).
RUN useradd -m -u 1000 app && mkdir -p /data && chown -R app:app /data

ENV VTX_DATA_DIR=/data \
    VTX_MODEL=large-v3-turbo \
    VTX_PRELOAD_MODELS=1 \
    HF_HOME=/data/hf \
    VTX_COMPUTE_TYPE=int8 \
    VTX_CPU_THREADS=4 \
    VTX_BEAM_SIZE=5 \
    VTX_DIARIZATION=0 \
    VTX_RECORDER_ENABLED=0 \
    VTX_MAX_CONCURRENT_RECORDINGS=4 \
    VTX_DISPLAY_BASE=99 \
    VTX_SCREEN_RES=1920x1080x24 \
    VTX_PULSE_MONITOR=meet0.monitor \
    DISPLAY=:99 \
    XDG_RUNTIME_DIR=/tmp/xdg \
    PULSE_RUNTIME_PATH=/tmp/pulse \
    OLLAMA_URL=http://host.docker.internal:11434 \
    TZ=Europe/Moscow

VOLUME ["/data"]
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD curl -fsS http://localhost:8000/healthz || exit 1

# Entry starts as root (fixes /data ownership) then drops to `app` and brings up
# Xvfb + PulseAudio (only if VTX_RECORDER_ENABLED=1) before launching uvicorn.
ENTRYPOINT ["/app/docker/entrypoint.sh"]
