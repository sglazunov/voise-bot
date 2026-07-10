"""Runtime configuration, all overridable via environment variables.

Defaults are tuned for THIS machine:
    AMD Ryzen 5 5500U — 6 cores / 12 threads, 14 GB RAM, no CUDA GPU,
    Linux/Docker. With this much CPU/RAM we can run bigger models with
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

# Whisper model. Default "large-v3-turbo": near large-v3 quality but much faster,
# ~2 GB at int8 — fits comfortably in 14 GB. All offered models are pre-downloaded
# in the background at startup (see whisper_setup.preload_all), so nothing is
# fetched manually. Override per job in the UI or globally with VTX_MODEL.
MODEL = os.getenv("VTX_MODEL", "large-v3-turbo")
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
# Feed the previous window's text back as context. OFF by default: it makes one
# recognition error snowball into "guessed" phrases over a long meeting; with it
# off every window is decoded strictly from its own audio.
CONDITION_PREV_TEXT = os.getenv("VTX_CONDITION_PREV", "0") == "1"

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

# Selectable model tiers per cloud provider — "how powerful the API model is".
# Chosen from the UI as "<provider>:<model>"; the first entry is the default.
# Weaker/cheaper tiers are faster; stronger tiers give better protocols.
PROVIDER_MODELS = {
    "groq": [
        {"value": "llama-3.3-70b-versatile", "label": "Llama 3.3 70B · мощная (по умолчанию)"},
        {"value": "llama-3.1-8b-instant", "label": "Llama 3.1 8B · быстрая/лёгкая"},
    ],
    "gigachat": [
        {"value": "GigaChat", "label": "GigaChat Lite · базовая (быстро)"},
        {"value": "GigaChat-Pro", "label": "GigaChat Pro · сильнее"},
        {"value": "GigaChat-Max", "label": "GigaChat Max · максимум"},
    ],
    "yandex": [
        {"value": "yandexgpt/latest", "label": "YandexGPT · полная"},
        {"value": "yandexgpt-lite/latest", "label": "YandexGPT Lite · лёгкая/быстрая"},
    ],
    "gemini": [
        {"value": "gemini-2.0-flash", "label": "Gemini 2.0 Flash · быстрая"},
        {"value": "gemini-1.5-pro", "label": "Gemini 1.5 Pro · мощная"},
    ],
    "anthropic": [
        {"value": "claude-sonnet-4-6", "label": "Claude Sonnet · баланс"},
        {"value": "claude-opus-4-8", "label": "Claude Opus · максимум"},
        {"value": "claude-haiku-4-5-20251001", "label": "Claude Haiku · быстрая/дешёвая"},
    ],
}

# Providers configurable from the UI by an API key (+ optional extra field).
KEY_PROVIDERS = {"anthropic", "groq", "gemini", "yandex", "gigachat"}

# Per-minute token budget (TPM) of a provider's free tier, counted per REQUEST as
# input + the REQUESTED max_tokens. Asking for a big answer can therefore fail on
# its own (HTTP 413 "Request too large"), no matter how many keys you have — every
# account of the same tier has the same cap. We size each request to fit.
# Extra keys still help: they multiply the per-MINUTE throughput (see llm.py).
PROVIDER_TPM = {"groq": int(os.getenv("VTX_GROQ_TPM", "12000"))}


def provider_creds(provider: str, user_keys: dict | None = None) -> list[tuple[str, str]]:
    """List of (api_key, extra) for a provider — ALL the user's keys (for
    rate-limit rotation), else the server env fallback. `user_keys` is
    {provider: [{'key','extra'}, ...]} from user_creds.load()."""
    entries = (user_keys or {}).get(provider) or []
    creds = [(e.get("key", ""), e.get("extra", "")) for e in entries if e.get("key")]
    if creds:
        return creds
    env = {
        "anthropic": (ANTHROPIC_API_KEY, ""),
        "groq": (GROQ_API_KEY, ""),
        "gemini": (GEMINI_API_KEY, ""),
        "yandex": (YANDEX_API_KEY, YANDEX_FOLDER_ID),
        "gigachat": (GIGACHAT_AUTH_KEY, GIGACHAT_SCOPE),
    }.get(provider, ("", ""))
    return [env] if env[0] else []


def _has_provider_key(p: str, user_keys: dict | None) -> bool:
    creds = provider_creds(p, user_keys)
    if not creds:
        return False
    if p == "yandex":
        return any(key and extra for key, extra in creds)
    return True


def available_providers(user_keys: dict | None = None) -> list[str]:
    """Providers configured (and thus selectable) for this user — their own key
    first, then the server env fallback."""
    out = []
    if OLLAMA_ENABLED:
        out.append("ollama")
    for p in ("groq", "gemini", "yandex", "gigachat", "anthropic"):
        if _has_provider_key(p, user_keys):
            out.append(p)
    return [p for p in PROVIDER_ORDER if p in out]


def set_hf_token(token: str) -> None:
    """Set the HuggingFace token at runtime (for diarization). In-memory only."""
    global HF_TOKEN
    HF_TOKEN = token.strip()


def set_provider_key(provider: str, key: str, extra: str = "") -> None:
    """Set an API key at runtime (from the UI). Kept in memory only — not
    written to disk, so it's gone on restart. Put it in .env to persist.

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


def resolve_provider(name: str | None, user_keys: dict | None = None) -> str:
    """Turn a requested provider (or 'auto'/None) into a concrete one."""
    avail = available_providers(user_keys)
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
