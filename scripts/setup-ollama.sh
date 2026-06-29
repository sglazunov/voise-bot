#!/usr/bin/env bash
# One-time: pull the protocol model into the Ollama service and build the
# tuned "vtx-protocol" model from the Modelfile. Run AFTER:
#   docker compose --profile local-ai up -d
set -euo pipefail

SVC=linux-voise-ollama
MODEL="${VTX_OLLAMA_BASE_MODEL:-qwen2.5:7b}"

echo "==> Pulling base model $MODEL (one-time, ~4.7 GB)…"
docker exec "$SVC" ollama pull "$MODEL"

echo "==> Building tuned model 'vtx-protocol' from Modelfile…"
docker cp ./Modelfile "$SVC:/tmp/Modelfile"
docker exec "$SVC" ollama create vtx-protocol -f /tmp/Modelfile

echo "==> Done. Models available:"
docker exec "$SVC" ollama list
