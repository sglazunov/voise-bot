# Voice Transcriber — Linux core (recognition + protocol + Weeek + cloud).
# CPU-only; no GPU needed. The meeting-recorder bot is NOT in this image.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONUTF8=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# System deps:
#   ffmpeg            — audio/video decoding for transcription
#   tesseract-ocr(+rus) — on-screen text OCR
#   tzdata            — correct local meeting times
#   curl, ca-certificates — health checks / outbound HTTPS to LLM APIs
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        tesseract-ocr \
        tesseract-ocr-rus \
        tzdata \
        ca-certificates \
        curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python deps first for better layer caching.
COPY requirements.txt ./
RUN pip install --upgrade pip wheel && pip install -r requirements.txt

# App code.
COPY app/ ./app/
COPY Modelfile ./Modelfile

# Defaults tuned for a small CPU VM; override in docker-compose / .env.
ENV VTX_DATA_DIR=/data \
    VTX_MODEL=small \
    VTX_COMPUTE_TYPE=int8 \
    VTX_CPU_THREADS=4 \
    VTX_BEAM_SIZE=5 \
    VTX_DIARIZATION=0 \
    VTX_RECORDER_ENABLED=0 \
    OLLAMA_URL=http://ollama:11434 \
    TZ=Europe/Moscow

VOLUME ["/data"]
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD curl -fsS http://localhost:8000/healthz || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
