"""Runtime configuration, all overridable via environment variables.

Defaults are tuned for THIS machine:
    AMD Ryzen 5 5500U — 6 cores / 12 threads, 14 GB RAM, no CUDA GPU,
    Windows 11. With this much CPU/RAM we can run the "medium" model with
    beam search for noticeably better Russian quality than the old low-end
    "small"/greedy defaults.
"""
import os
from pathlib import Path

# Where uploads and results live. Override with VTX_DATA_DIR.
DATA_DIR = Path(os.getenv("VTX_DATA_DIR", Path(__file__).resolve().parent.parent / "data"))
UPLOAD_DIR = DATA_DIR / "uploads"
RESULT_DIR = DATA_DIR / "results"
JOBS_FILE = DATA_DIR / "jobs.json"

# Whisper model. "medium" gives clearly better Russian than "small" and fits
# comfortably in 14 GB at int8 (~1.5 GB). Drop to "small" if you want it faster,
# or try "large-v3" if you don't mind it running ~2x slower on this CPU.
MODEL = os.getenv("VTX_MODEL", "medium")
DEVICE = os.getenv("VTX_DEVICE", "cpu")          # no CUDA GPU on the 5500U
COMPUTE_TYPE = os.getenv("VTX_COMPUTE_TYPE", "int8")
# CTranslate2 scales best with PHYSICAL cores. The 5500U has 6 — using 6 keeps
# a couple of logical threads free for the web server + OS responsiveness.
CPU_THREADS = int(os.getenv("VTX_CPU_THREADS", "6"))
# beam_size=5 for quality — the 6-core CPU has the headroom for it. Set to 1
# (greedy) if you'd rather have faster, lower-quality transcripts.
BEAM_SIZE = int(os.getenv("VTX_BEAM_SIZE", "5"))
DEFAULT_LANGUAGE = os.getenv("VTX_LANGUAGE", "ru")
VAD_FILTER = os.getenv("VTX_VAD", "1") == "1"

# Max upload size in MB. 2 GB by default so 1 GB videos go through comfortably.
MAX_UPLOAD_MB = int(os.getenv("VTX_MAX_UPLOAD_MB", "2048"))

# How long finished results (and their uploads) are kept before auto-cleanup.
# Within this window a result stays downloadable even after a page reload.
RESULT_RETENTION_HOURS = int(os.getenv("VTX_RETENTION_HOURS", "24"))

# Diarization ("who spoke") is opt-in and OFF by default. With 14 GB it now
# fits, but it still needs a one-off `pip install pyannote.audio torch` plus a
# free HF_TOKEN. Enable with VTX_DIARIZATION=1 once those are in place.
DIARIZATION_ENABLED = os.getenv("VTX_DIARIZATION", "0") == "1"
HF_TOKEN = os.getenv("HF_TOKEN", "")

# ---- AI analysis (protocol / meeting-summary generation) -------------------
# The protocol can be built by any of several LLM providers. Each transcript
# job picks one. Providers fall into two buckets:
#   FREE  — Ollama (fully local/offline) and Groq (free cloud tier)
#   PAID  — Anthropic Claude (billed per token, highest quality)

# "auto" = use whichever is available, preferring free providers so you never
# get billed unexpectedly. Override per-job from the UI or globally here.
LLM_PROVIDER = os.getenv("VTX_LLM_PROVIDER", "auto")

# --- Paid: Anthropic Claude (per-token billing) ---
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
ANALYSIS_MODEL = os.getenv("VTX_ANALYSIS_MODEL", "claude-sonnet-4-6")

# --- Free: Groq cloud (free tier; get a key at https://console.groq.com) ---
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
GROQ_MODEL = os.getenv("VTX_GROQ_MODEL", "llama-3.3-70b-versatile")

# --- Google Gemini (free tier; key at https://aistudio.google.com/apikey) ---
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL = os.getenv("VTX_GEMINI_MODEL", "gemini-2.0-flash")

# --- YandexGPT (Yandex Cloud: API key + folder id) ---
YANDEX_API_KEY = os.getenv("YANDEX_API_KEY", "")
YANDEX_FOLDER_ID = os.getenv("YANDEX_FOLDER_ID", "")
YANDEX_MODEL = os.getenv("VTX_YANDEX_MODEL", "yandexgpt/latest")

# --- GigaChat / Sber (Authorization key = base64 client_id:secret) ---
GIGACHAT_AUTH_KEY = os.getenv("GIGACHAT_AUTH_KEY", "")
GIGACHAT_SCOPE = os.getenv("GIGACHAT_SCOPE", "GIGACHAT_API_PERS")
GIGACHAT_MODEL = os.getenv("VTX_GIGACHAT_MODEL", "GigaChat")

# --- Free: Ollama (fully local, no key; runs on this machine) ---
# qwen2.5:7b is the pick for this box: strong Russian summarization, ~4.7 GB,
# fits in 14 GB alongside the Whisper "medium" model. Enabled by default since
# Ollama is installed locally. Set VTX_OLLAMA=0 to turn it off.
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434")
OLLAMA_MODEL = os.getenv("VTX_OLLAMA_MODEL", "qwen2.5:7b")
OLLAMA_ENABLED = os.getenv("VTX_OLLAMA", "1") == "1"

# Human-friendly labels shown in the UI provider picker.
PROVIDER_LABELS = {
    "ollama": "Локально · Ollama (бесплатно, оффлайн)",
    "groq": "Groq · Llama (бесплатно, облако)",
    "gemini": "Google Gemini (по ключу)",
    "yandex": "YandexGPT (ключ + folder id)",
    "gigachat": "GigaChat / Sber (по ключу)",
    "anthropic": "Claude (платно по токенам, точнее)",
}
# Order = preference for "auto" (free/local first, paid last).
PROVIDER_ORDER = ["ollama", "groq", "gemini", "yandex", "gigachat", "anthropic"]

# Providers configurable from the UI by an API key (+ optional extra field).
KEY_PROVIDERS = {"anthropic", "groq", "gemini", "yandex", "gigachat"}


def available_providers() -> list[str]:
    """Providers that are configured and therefore selectable right now."""
    out = []
    if OLLAMA_ENABLED:
        out.append("ollama")
    if GROQ_API_KEY:
        out.append("groq")
    if GEMINI_API_KEY:
        out.append("gemini")
    if YANDEX_API_KEY and YANDEX_FOLDER_ID:
        out.append("yandex")
    if GIGACHAT_AUTH_KEY:
        out.append("gigachat")
    if ANTHROPIC_API_KEY:
        out.append("anthropic")
    return [p for p in PROVIDER_ORDER if p in out]


def set_hf_token(token: str) -> None:
    """Set the HuggingFace token at runtime (for diarization). In-memory only."""
    global HF_TOKEN
    HF_TOKEN = token.strip()


def set_provider_key(provider: str, key: str, extra: str = "") -> None:
    """Set an API key at runtime (from the UI). Kept in memory only — not
    written to disk, so it's gone on restart. Add it to run.bat to persist.

    `extra` carries the provider's second credential where needed:
    YandexGPT → folder id; GigaChat → scope (optional)."""
    global ANTHROPIC_API_KEY, GROQ_API_KEY, GEMINI_API_KEY
    global YANDEX_API_KEY, YANDEX_FOLDER_ID, GIGACHAT_AUTH_KEY, GIGACHAT_SCOPE
    if provider == "anthropic":
        ANTHROPIC_API_KEY = key
    elif provider == "groq":
        GROQ_API_KEY = key
    elif provider == "gemini":
        GEMINI_API_KEY = key
    elif provider == "yandex":
        YANDEX_API_KEY = key
        if extra:
            YANDEX_FOLDER_ID = extra
    elif provider == "gigachat":
        GIGACHAT_AUTH_KEY = key
        if extra:
            GIGACHAT_SCOPE = extra
    else:
        raise RuntimeError(f"Ключ для провайдера '{provider}' не поддерживается.")


def resolve_provider(name: str | None) -> str:
    """Turn a requested provider (or 'auto'/None) into a concrete one."""
    avail = available_providers()
    if not avail:
        raise RuntimeError(
            "Ни один LLM-провайдер не настроен. Включите Ollama (VTX_OLLAMA=1) "
            "или задайте GROQ_API_KEY / ANTHROPIC_API_KEY."
        )
    if name and name != "auto":
        if name not in avail:
            raise RuntimeError(f"Провайдер '{name}' не настроен.")
        return name
    return avail[0]  # PROVIDER_ORDER puts free providers first


ANALYSIS_ENABLED = bool(available_providers())

for _d in (DATA_DIR, UPLOAD_DIR, RESULT_DIR):
    _d.mkdir(parents=True, exist_ok=True)
