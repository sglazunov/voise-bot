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

# Storage backend. When DATABASE_URL is set, structured state (users, sessions,
# recovery codes, jobs, settings, LLM keys, meeting states, AI context) lives in
# PostgreSQL instead of the JSON files under DATA_DIR — the app logic is
# identical either way (see app/db.py). Media files always stay on disk/cloud.
# Empty = keep the file backend (used by tests and simple local runs).
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()

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

# Whisper anti-hallucination filters (Д4). A segment that Whisper itself thinks
# is probably silence (no_speech_prob) AND decodes with low confidence
# (avg_logprob) is a classic "dreamed up" phrase — drop it. Runs of identical
# consecutive segments (the looping-on-silence failure mode) are collapsed.
NS_PROB_MAX = float(os.getenv("VTX_NS_PROB_MAX", "0.6"))
LOGPROB_MIN = float(os.getenv("VTX_LOGPROB_MIN", "-1.0"))
REPEAT_COLLAPSE_AT = int(os.getenv("VTX_REPEAT_COLLAPSE_AT", "3"))

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

# --- Paid: Anthropic Claude (per-token billing) ---
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
ANALYSIS_MODEL = os.getenv("VTX_ANALYSIS_MODEL", "claude-sonnet-4-6")

# --- Free: Groq cloud (free tier; get a key at https://console.groq.com) ---
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
GROQ_MODEL = os.getenv("VTX_GROQ_MODEL", "llama-3.3-70b-versatile")

# --- NVIDIA NIM (build.nvidia.com) — бесплатно, без карты ---------------------
# OpenAI-совместимый API, ключ вида `nvapi-…` с build.nvidia.com/settings/api-keys.
# Каталог — сотня открытых моделей (Kimi, DeepSeek, Qwen, Llama, Nemotron…),
# поэтому список моделей НЕ хардкодим: он запрашивается по ключу (llm.nvidia_models).
# Ограничение: ~40 запросов в минуту на ключ и на все модели сразу — на длинной
# встрече map-reduce может упереться, тогда помогает второй ключ (ротация).
NVIDIA_API_KEY = os.getenv("NVIDIA_API_KEY", "")
# Умолчание — только на случай, когда каталог недоступен: список моделей всё
# равно приходит по ключу, и выбирают из него. Идентификатор взят из каталога
# дословно (не «kimi-k2-instruct», которого там нет).
# ВАЖНО: /v1/models перечисляет ВЕСЬ опубликованный каталог, а не то, что вправе
# вызывать конкретный аккаунт. Недоступная модель отвечает 404 «Function …: Not
# found for account …» — на боевом ключе так вела себя kimi-k2.6. Поэтому
# умолчанием стоит первая по нашему же ранжированию: 1M контекста, то есть час
# встречи влезает целиком.
NVIDIA_MODEL = os.getenv("VTX_NVIDIA_MODEL", "deepseek-ai/deepseek-v4-pro")

# --- Google Gemini (free tier; key at https://aistudio.google.com/apikey) ---
# Pin a CONCRETE model (not the gemini-flash-latest alias) so behaviour and free
# limits don't silently change when Google re-points the alias — that surprise is
# exactly what killed the 2.5 models. gemini-3.1-flash-lite is the current stable,
# most generous free choice (~1500 req/day). Override with VTX_GEMINI_MODEL.
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL = os.getenv("VTX_GEMINI_MODEL", "gemini-3.1-flash-lite")

# --- YandexGPT (Yandex Cloud: API key + folder id) ---
YANDEX_API_KEY = os.getenv("YANDEX_API_KEY", "")
YANDEX_FOLDER_ID = os.getenv("YANDEX_FOLDER_ID", "")
YANDEX_MODEL = os.getenv("VTX_YANDEX_MODEL", "yandexgpt/latest")

# --- GigaChat / Sber (Authorization key = base64 client_id:secret) ---
GIGACHAT_AUTH_KEY = os.getenv("GIGACHAT_AUTH_KEY", "")
GIGACHAT_SCOPE = os.getenv("GIGACHAT_SCOPE", "GIGACHAT_API_PERS")
GIGACHAT_MODEL = os.getenv("VTX_GIGACHAT_MODEL", "GigaChat")

# --- Free: Ollama (fully local, no key; runs on this machine) ---
# Ollama в образ НЕ входит — это внешний сервер, который подключают отдельно.
# Поэтому по умолчанию она ВЫКЛЮЧЕНА: включённой она всегда попадала в список
# доступных движков, resolve_provider никогда не говорил «не настроено», и
# задача без единого ключа падала на «Не удалось подключиться к
# host.docker.internal» вместо понятного «движок не настроен».
# Включить: VTX_OLLAMA=1 и OLLAMA_URL на свой сервер.
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://host.docker.internal:11434")
OLLAMA_MODEL = os.getenv("VTX_OLLAMA_MODEL", "qwen2.5:7b")
OLLAMA_ENABLED = os.getenv("VTX_OLLAMA", "0") == "1"

# Human-friendly labels shown in the UI provider picker.
PROVIDER_LABELS = {
    # Любой поставщик по одному ключу: адрес, способ авторизации и список
    # моделей определяются сами. Нужен, потому что состав бесплатных моделей у
    # поставщиков меняется, а писать новый класс под каждого — тупик.
    "custom": "Любой провайдер по ключу (OpenAI-совместимый)",
    "ollama": "Локально · Ollama (бесплатно, оффлайн)",
    "groq": "Groq · Llama (бесплатно, облако)",
    "nvidia": "NVIDIA NIM · Kimi/DeepSeek/Qwen (бесплатно, ~40 запросов/мин)",
    "gemini": "Google Gemini (по ключу)",
    "yandex": "YandexGPT (ключ + folder id)",
    "gigachat": "GigaChat / Sber (по ключу)",
    "anthropic": "Claude (платно по токенам, точнее)",
}
# Order = preference for "auto" (free/local first, paid last).
# «Авто» берёт ПЕРВЫЙ настроенный движок из этого списка.
# Порядок — по замерам на боевых встречах (см. CLAUDE.md «Выбор движка»):
# NVIDIA/DeepSeek даёт вдвое более полный протокол, Gemini устойчив и идёт
# запасным, Groq оказался худшим (дробит темы, теряет задачи, отвечает 413 даже
# на двадцатиминутной встрече) — поэтому он больше НЕ первый. Ollama последняя:
# на CPU-сервере локальный протокол считается десятки минут.
# Переопределяется через VTX_PROVIDER_ORDER="ollama,groq,…".
# «custom» первым: свой ключ подключают намеренно и под конкретную модель,
# значит он и есть выбор пользователя. Остальные — как раньше.
_default_order = "custom,nvidia,gemini,groq,yandex,gigachat,anthropic,ollama"
PROVIDER_ORDER = [p.strip() for p in
                  os.getenv("VTX_PROVIDER_ORDER", _default_order).split(",")
                  if p.strip()]

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
        {"value": "gemini-3.1-flash-lite", "label": "Gemini 3.1 Flash-Lite · быстрая (~1500/день, по умолчанию)"},
        {"value": "gemini-3.5-flash", "label": "Gemini 3.5 Flash · мощнее"},
        {"value": "gemini-flash-latest", "label": "Gemini Flash (алиас — актуальная, но лимиты могут меняться)"},
        {"value": "gemini-flash-lite-latest", "label": "Gemini Flash-Lite (алиас — актуальная)"},
        {"value": "gemini-pro-latest", "label": "Gemini Pro (алиас) · максимум"},
    ],
    "anthropic": [
        {"value": "claude-sonnet-4-6", "label": "Claude Sonnet · баланс"},
        {"value": "claude-opus-4-8", "label": "Claude Opus · максимум"},
        {"value": "claude-haiku-4-5-20251001", "label": "Claude Haiku · быстрая/дешёвая"},
    ],
}

# Providers configurable from the UI by an API key (+ optional extra field).
KEY_PROVIDERS = {"anthropic", "groq", "nvidia", "gemini", "yandex", "gigachat",
                 "custom"}

# Сколько дней живёт ключ провайдера. Пока известен только у NVIDIA: бесплатный
# `nvapi-…` выдаётся на полгода. Когда он истекает, протоколы начинают молча
# собираться запасным движком — в шапке появляется «Выбранный движок не
# ответил», а причина неочевидна. Интерфейс показывает остаток по этой цифре.
KEY_TTL_DAYS = {"nvidia": 183}

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
        "nvidia": (NVIDIA_API_KEY, ""),
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
    for p in ("groq", "nvidia", "gemini", "yandex", "gigachat", "anthropic"):
        if _has_provider_key(p, user_keys):
            out.append(p)
    return [p for p in PROVIDER_ORDER if p in out]



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


for _d in (DATA_DIR, UPLOAD_DIR, RESULT_DIR):
    _d.mkdir(parents=True, exist_ok=True)
