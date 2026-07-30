"""LLM provider abstraction for protocol generation.

Three providers, one tiny interface (`complete(prompt) -> text`):

  ollama     — fully local, free, offline (needs Ollama running)
  groq       — free cloud tier (OpenAI-compatible API)
  anthropic  — paid, per-token (highest quality)

Ollama and Groq are called over plain HTTP via the stdlib (no extra deps);
Anthropic uses its official SDK. Each provider is asked to return raw JSON —
parsing/validation happens in analyze.py.
"""
from __future__ import annotations

import json
import os
import re
import ssl
import time
import urllib.error
import urllib.request
import uuid
from typing import Protocol

from . import config


class GenerationCancelled(Exception):
    """Raised from inside a streaming generation when the caller asks to stop,
    so cancellation is responsive mid-stream (not only between chunks)."""


class LLMProvider(Protocol):
    name: str

    def complete(self, prompt: str, max_tokens: int = 2000, force_json: bool = True) -> str:
        """Send the prompt to the model and return its text response.

        force_json asks the backend to constrain the answer to valid JSON
        (used for the real analysis). Set False for a plain ping, e.g. when
        validating an API key.
        """
        ...


# ---------------------------------------------------------------------------
def _retry_after(e: urllib.error.HTTPError, body: str, default: float) -> float:
    """Seconds to wait before retrying a 429/503, from header or response body."""
    ra = e.headers.get("Retry-After") if e.headers else None
    if ra:
        try:
            return float(ra)
        except ValueError:
            pass
    # Groq's body says e.g. "Please try again in 12.34s".
    m = re.search(r"try again in ([\d.]+)\s*s", body or "")
    if m:
        try:
            return float(m.group(1))
        except ValueError:
            pass
    return default


def _http_post_json(url: str, payload: dict, headers: dict, timeout: int = 180,
                    max_retries: int = 3) -> dict:
    """POST a JSON body and return the parsed JSON response.

    Retries on 429 (rate limit) / 503, honouring Retry-After — free cloud tiers
    (e.g. Groq) rate-limit easily when a long transcript is analysed in chunks.
    """
    data = json.dumps(payload).encode("utf-8")
    attempt = 0
    while True:
        req = urllib.request.Request(url, data=data, method="POST")
        req.add_header("Content-Type", "application/json")
        # A browser-like User-Agent: some providers (e.g. Groq) sit behind
        # Cloudflare, which rejects the default "Python-urllib/x.y" agent with a
        # 403 / error 1010 ("banned by browser signature").
        req.add_header("User-Agent",
                       "Mozilla/5.0 (X11; Linux x86_64) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/124.0 Safari/537.36")
        req.add_header("Accept", "application/json")
        for k, v in headers.items():
            req.add_header(k, v)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")
            if e.code in (429, 503) and attempt < max_retries:
                wait = min(_retry_after(e, body, default=8 * (attempt + 1)), 30)
                time.sleep(wait + 0.5)
                attempt += 1
                continue
            raise RuntimeError(f"HTTP {e.code} от {url}: {body[:300]}") from e
        except urllib.error.URLError as e:
            raise RuntimeError(f"Не удалось подключиться к {url}: {e.reason}") from e


# ---------------------------------------------------------------------------
def list_ollama_models() -> list[str]:
    """Names of models installed in the local Ollama server (empty if down)."""
    url = config.OLLAMA_URL.rstrip("/") + "/api/tags"
    try:
        with urllib.request.urlopen(url, timeout=3) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return [m.get("name", "") for m in data.get("models", []) if m.get("name")]
    except Exception:
        return []


def ollama_status() -> str:
    """Whether Ollama is usable here — for install guidance in the UI.

    'running'   — server answers (ready to use)
    'installed' — the CLI exists but the server isn't up yet
    'missing'   — Ollama is not installed
    """
    try:
        with urllib.request.urlopen(config.OLLAMA_URL.rstrip("/") + "/api/version",
                                    timeout=2):
            return "running"
    except Exception:
        pass
    import shutil
    return "installed" if shutil.which("ollama") else "missing"


# The ceiling for Ollama's context window (matches Modelfile's num_ctx). The
# ACTUAL window per request is sized to the prompt, so short calls stay fast.
OLLAMA_NUM_CTX = int(os.getenv("VTX_OLLAMA_NUM_CTX", "32768"))


class OllamaProvider:
    """Local Ollama server. Free, offline, no API key.

    A specific model can be chosen per job; `name` carries it (e.g.
    "ollama:vtx-protocol") so per-model Word docs and labels stay distinct.
    """

    def __init__(self, model: str | None = None) -> None:
        self.model = model or config.OLLAMA_MODEL
        self.name = f"ollama:{self.model}"

    def complete(self, prompt: str, max_tokens: int = 2000, force_json: bool = True,
                 on_token=None, should_stop=None, json_schema: dict | None = None) -> str:
        url = config.OLLAMA_URL.rstrip("/") + "/api/generate"
        options = {"temperature": 0.1, "num_predict": max_tokens}
        # Ollama's DEFAULT context is tiny (~2-4k tokens) and overflow is SILENT
        # truncation — the model just never sees the end of a long prompt. Size
        # num_ctx to the actual request (prompt + answer + margin), capped.
        est = len(prompt) // 3 + max_tokens + 512
        if est > 8192:
            options["num_ctx"] = min(OLLAMA_NUM_CTX, ((est // 1024) + 1) * 1024)
        payload = {
            "model": self.model,
            "prompt": prompt,
            "stream": bool(on_token),
            "options": options,
        }
        if json_schema:
            # Structured outputs: the schema constrains decoding, so the model
            # physically can't return invalid JSON or drop a required field.
            payload["format"] = json_schema
        elif force_json:
            payload["format"] = "json"  # constrain output to valid JSON
        if not on_token:
            out = _http_post_json(url, payload, headers={}, timeout=600)
            return (out.get("response") or "").strip()
        # Streaming: Ollama returns NDJSON; surface the growing text live.
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, data=data, method="POST")
        req.add_header("Content-Type", "application/json")
        acc: list[str] = []
        try:
            with urllib.request.urlopen(req, timeout=600) as resp:
                for line in resp:
                    # Check cancellation on every NDJSON line so «Отменить» is
                    # responsive mid-generation, not only between chunks.
                    if should_stop and should_stop():
                        raise GenerationCancelled()
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    tok = obj.get("response", "")
                    if tok:
                        acc.append(tok)
                        try:
                            on_token("".join(acc))
                        except Exception:
                            pass
                    if obj.get("done"):
                        break
        except urllib.error.URLError as e:
            raise RuntimeError(f"Не удалось подключиться к {url}: {e.reason}") from e
        return "".join(acc).strip()


# ---------------------------------------------------------------------------
class GroqProvider:
    """Groq cloud, OpenAI-compatible. Free tier, needs GROQ_API_KEY."""

    name = "groq"

    def __init__(self, model: str | None = None, api_key: str | None = None,
                 extra: str | None = None) -> None:
        self.model = model or config.GROQ_MODEL
        self.api_key = api_key or config.GROQ_API_KEY

    def complete(self, prompt: str, max_tokens: int = 2000, force_json: bool = True) -> str:
        url = "https://api.groq.com/openai/v1/chat/completions"
        payload = {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens,
            "temperature": 0.1,
        }
        if force_json:
            payload["response_format"] = {"type": "json_object"}
        headers = {"Authorization": f"Bearer {self.api_key}"}
        out = _http_post_json(url, payload, headers, timeout=180)
        return out["choices"][0]["message"]["content"].strip()


# ---------------------------------------------------------------------------
# Каталог NVIDIA большой (100+ моделей) и меняется, поэтому жёстко его не
# перечисляем: список берём по ключу через /v1/models, а этой таблицей лишь
# ранжируем — что показывать первым. Чем меньше число, тем выше в списке.
#
# Приоритет под нашу задачу (час русской речи → строгий JSON-протокол): нужны
# сильный русский, длинный контекст и послушность формату. Reasoning-модели
# (deepseek-r1 и подобные) стоят ниже: они склонны «размышлять» в ответе, а нам
# нужен чистый JSON.
_NVIDIA_RANK = (
    ("moonshotai/kimi", 0),          # Kimi K2 — длинный контекст, сильный русский
    ("deepseek-ai/deepseek-v3", 1),
    ("qwen/qwen3", 2),
    ("qwen/qwen2.5-72b", 2),
    ("meta/llama-3.3-70b", 3),
    ("nvidia/llama-3.3-nemotron-super", 3),
    ("mistralai/mistral-large", 4),
    ("deepseek-ai/deepseek-r1", 6),  # reasoning — ниже: мешает строгому JSON
)


def nvidia_models(api_key: str | None = None) -> list[str]:
    """Модели, доступные КОНКРЕТНОМУ ключу (GET /v1/models), лучшие — первыми.

    Список приходит с сервера, а не из кода: каталог NVIDIA обновляется, и
    захардкоженные идентификаторы устарели бы молча — «модель не найдена» в
    момент сборки протокола."""
    key = api_key or config.NVIDIA_API_KEY
    if not key:
        return []
    try:
        req = urllib.request.Request(
            "https://integrate.api.nvidia.com/v1/models",
            headers={"Authorization": f"Bearer {key}"})
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception:      # noqa: BLE001 — нет сети/ключ отклонён: просто пусто
        return []
    ids = [str(m.get("id")) for m in (data.get("data") or []) if m.get("id")]

    def rank(mid: str) -> tuple:
        low = mid.lower()
        for prefix, r in _NVIDIA_RANK:
            if low.startswith(prefix):
                return (r, low)
        return (9, low)          # всё остальное — после известных, по алфавиту

    return sorted(ids, key=rank)


class NvidiaProvider:
    """NVIDIA NIM (build.nvidia.com) — OpenAI-совместимый, ключ `nvapi-…`.

    Бесплатный, без карты. Важное ограничение: лимит ~40 запросов в минуту на
    ключ И НА ВСЕ МОДЕЛИ СРАЗУ. Наш map-reduce на длинной встрече делает
    десятки запросов, поэтому на больших записях он может упереться в лимит —
    ротация ключей (_RotatingProvider) здесь особенно к месту.
    """

    name = "nvidia"

    def __init__(self, model: str | None = None, api_key: str | None = None,
                 extra: str | None = None) -> None:
        self.model = model or config.NVIDIA_MODEL
        self.api_key = api_key or config.NVIDIA_API_KEY

    def complete(self, prompt: str, max_tokens: int = 2000, force_json: bool = True) -> str:
        url = "https://integrate.api.nvidia.com/v1/chat/completions"
        payload = {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens,
            "temperature": 0.1,
        }
        headers = {"Authorization": f"Bearer {self.api_key}"}
        if force_json:
            payload["response_format"] = {"type": "json_object"}
            try:
                out = _http_post_json(url, payload, headers, timeout=180)
                return out["choices"][0]["message"]["content"].strip()
            except Exception as e:      # noqa: BLE001
                # В каталоге сотня моделей, и не каждая понимает
                # response_format. Не теряем запрос из-за этого: повторяем без
                # него — JSON всё равно валидируется на нашей стороне.
                if "response_format" not in str(e).lower():
                    raise
                payload.pop("response_format", None)
        out = _http_post_json(url, payload, headers, timeout=180)
        return out["choices"][0]["message"]["content"].strip()


# ---------------------------------------------------------------------------
class AnthropicProvider:
    """Anthropic Claude. Paid per token, highest quality."""

    name = "anthropic"

    def __init__(self, model: str | None = None, api_key: str | None = None,
                 extra: str | None = None) -> None:
        self.model = model or config.ANALYSIS_MODEL
        self.api_key = api_key or config.ANTHROPIC_API_KEY

    def complete(self, prompt: str, max_tokens: int = 2000, force_json: bool = True) -> str:
        # Claude follows the "return only JSON" instruction in the prompt well,
        # so force_json needs no special API flag here.
        try:
            import anthropic
        except ImportError:
            raise RuntimeError(
                "Пакет anthropic не установлен. Выполните: pip install anthropic"
            )
        client = anthropic.Anthropic(api_key=self.api_key)
        message = client.messages.create(
            model=self.model,
            max_tokens=max_tokens,
            temperature=0.1,
            messages=[{"role": "user", "content": prompt}],
        )
        return message.content[0].text.strip()


# ---------------------------------------------------------------------------
class GeminiProvider:
    """Google Gemini. Free tier, needs GEMINI_API_KEY. May be region-blocked."""

    name = "gemini"

    def __init__(self, model: str | None = None, api_key: str | None = None,
                 extra: str | None = None) -> None:
        self.model = model or config.GEMINI_MODEL
        self.api_key = api_key or config.GEMINI_API_KEY

    def complete(self, prompt: str, max_tokens: int = 2000, force_json: bool = True) -> str:
        model = self.model
        url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
               f"{model}:generateContent?key={self.api_key}")
        gen = {"temperature": 0.1, "maxOutputTokens": max_tokens}
        if force_json:
            gen["responseMimeType"] = "application/json"
        payload = {"contents": [{"parts": [{"text": prompt}]}], "generationConfig": gen}
        out = _http_post_json(url, payload, headers={})
        try:
            return out["candidates"][0]["content"]["parts"][0]["text"].strip()
        except (KeyError, IndexError) as e:
            raise RuntimeError(f"Неожиданный ответ Gemini: {str(out)[:200]}") from e


# ---------------------------------------------------------------------------
class YandexProvider:
    """YandexGPT (Yandex Cloud Foundation Models). Needs API key + folder id."""

    name = "yandex"

    def __init__(self, model: str | None = None, api_key: str | None = None,
                 extra: str | None = None) -> None:
        self.model = model or config.YANDEX_MODEL
        self.api_key = api_key or config.YANDEX_API_KEY
        self.folder = extra or config.YANDEX_FOLDER_ID

    def complete(self, prompt: str, max_tokens: int = 2000, force_json: bool = True) -> str:
        url = "https://llm.api.cloud.yandex.net/foundationModels/v1/completion"
        payload = {
            "modelUri": f"gpt://{self.folder}/{self.model}",
            "completionOptions": {"stream": False, "temperature": 0.1,
                                  "maxTokens": str(max_tokens)},
            "messages": [{"role": "user", "text": prompt}],
        }
        headers = {"Authorization": f"Api-Key {self.api_key}",
                   "x-folder-id": self.folder}
        out = _http_post_json(url, payload, headers)
        try:
            return out["result"]["alternatives"][0]["message"]["text"].strip()
        except (KeyError, IndexError) as e:
            raise RuntimeError(f"Неожиданный ответ YandexGPT: {str(out)[:200]}") from e


# ---------------------------------------------------------------------------
class GigaChatProvider:
    """GigaChat (Sber). OAuth: an Authorization key is exchanged for a short-
    lived access token, then chat completions are called.

    Sber serves its endpoints behind the Russian Trusted Root CA, which Python
    doesn't ship. To keep setup zero-config we skip TLS verification for Sber's
    own hosts only. To verify properly instead, install the Russian CA bundle
    and remove the unverified context below.
    """

    name = "gigachat"
    _token: str = ""
    _exp: float = 0.0
    _ctx = ssl.create_default_context()
    _ctx.check_hostname = False
    _ctx.verify_mode = ssl.CERT_NONE

    def __init__(self, model: str | None = None, api_key: str | None = None,
                 extra: str | None = None) -> None:
        self.model = model or config.GIGACHAT_MODEL
        self.api_key = api_key or config.GIGACHAT_AUTH_KEY
        self.scope = extra or config.GIGACHAT_SCOPE

    def _get_token(self) -> str:
        if self._token and time.time() < self._exp - 30:
            return self._token
        url = "https://ngw.devices.sberbank.ru:9443/api/v2/oauth"
        body = f"scope={self.scope}".encode("utf-8")
        req = urllib.request.Request(url, data=body, method="POST")
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
        req.add_header("Accept", "application/json")
        req.add_header("RqUID", str(uuid.uuid4()))
        req.add_header("Authorization", f"Basic {self.api_key}")
        try:
            with urllib.request.urlopen(req, timeout=30, context=self._ctx) as resp:
                d = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")[:300]
            raise RuntimeError(f"GigaChat OAuth HTTP {e.code}: {body}") from e
        self._token = d["access_token"]
        # expires_at is epoch milliseconds; fall back to ~25 min.
        self._exp = (d.get("expires_at", 0) / 1000) or (time.time() + 1500)
        return self._token

    def complete(self, prompt: str, max_tokens: int = 2000, force_json: bool = True) -> str:
        token = self._get_token()
        url = "https://gigachat.devices.sberbank.ru/api/v1/chat/completions"
        payload = {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.1,
            "max_tokens": max_tokens,
        }
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, data=data, method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("Accept", "application/json")
        req.add_header("Authorization", f"Bearer {token}")
        try:
            with urllib.request.urlopen(req, timeout=180, context=self._ctx) as resp:
                out = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")[:300]
            raise RuntimeError(f"GigaChat HTTP {e.code}: {body}") from e
        return out["choices"][0]["message"]["content"].strip()


_PROVIDERS = {
    "ollama": OllamaProvider,
    "groq": GroqProvider,
    "nvidia": NvidiaProvider,
    "gemini": GeminiProvider,
    "yandex": YandexProvider,
    "gigachat": GigaChatProvider,
    "anthropic": AnthropicProvider,
}


def get_provider(name: str | None, keys: dict | None = None) -> LLMProvider:
    """Resolve 'auto'/None to a concrete configured provider and instantiate it.

    A specific model (tier) may be carried as "<provider>:<model>", e.g.
    "groq:llama-3.1-8b-instant", "gigachat:GigaChat-Pro", "ollama:qwen2.5:7b".
    The part after the FIRST ':' is the model; the rest of an ollama tag (which
    itself contains ':') is preserved.
    """
    model = None
    base = name
    if name and ":" in name:
        base, model = name.split(":", 1)
    resolved = config.resolve_provider(base, keys)
    cls = _PROVIDERS[resolved]
    if resolved == "ollama":
        return cls(model=model)
    creds = config.provider_creds(resolved, keys) or [("", "")]
    if len(creds) == 1:
        k, ex = creds[0]
        return cls(model=model, api_key=k, extra=ex)
    return _RotatingProvider(cls, model, creds)


def _is_rate_limit(e: Exception) -> bool:
    """Whether an error means the current API key hit its rate/quota limit."""
    s = f"{type(e).__name__} {e}".lower()
    return ("429" in s or "too many requests" in s or "rate limit" in s
            or "rate_limit" in s or "ratelimit" in s or "quota" in s
            or "resource_exhausted" in s or "insufficient_quota" in s)


_KEY_COOLDOWN_SEC = 60.0   # a rate-limited key rests this long (Groq's TPM window)
# When EVERY key is cooling, wait for the earliest reset (the TPM window is
# ~a minute) rather than abandoning the engine — the keys' budgets then SUM UP
# over the whole meeting. Waits longer than KEY_WAIT_SEC per round, or
# KEY_TOTAL_WAIT_SEC per request, mean a real outage → fall to the next engine.
KEY_WAIT_SEC = float(os.getenv("VTX_KEY_WAIT_SEC", "90"))
KEY_TOTAL_WAIT_SEC = float(os.getenv("VTX_KEY_TOTAL_WAIT_SEC", "300"))


def _cooldown_from(e: Exception, default: float = _KEY_COOLDOWN_SEC) -> float:
    """How long to rest a key that hit its limit — from the provider's own hint
    ("Please try again in 12.34s") when it gives one."""
    m = re.search(r"try again in ([\d.]+)\s*s", str(e))
    if m:
        try:
            return min(max(float(m.group(1)) + 1.0, 5.0), 300.0)
        except ValueError:
            pass
    return default


class _RotatingProvider:
    """Wraps a provider with a POOL of API keys — possibly from DIFFERENT accounts.

    Keys are used ROUND-ROBIN: every request goes to the next key, so the
    per-minute budgets (TPM/RPM) of several accounts ADD UP instead of one key
    carrying the whole job. A key that reports a rate limit is parked on a short
    cooldown (taken from the provider's "try again in Xs" hint when present) and
    skipped until it recovers. Only when every key is resting do we fail.
    """

    def __init__(self, cls, model, creds: list[tuple[str, str]]):
        self._cls = cls
        self._model = model
        self._creds = creds
        self.name = getattr(cls, "name", "llm")
        self._i = 0                             # round-robin cursor (next key to use)
        self._cooldown: dict[int, float] = {}   # key index -> resting until (unix ts)
        self._instances: dict[int, object] = {}

    def _inst(self, i: int):
        if i not in self._instances:
            k, ex = self._creds[i]
            self._instances[i] = self._cls(model=self._model, api_key=k, extra=ex)
        return self._instances[i]

    def complete(self, prompt: str, max_tokens: int = 2000, force_json: bool = True) -> str:
        n = len(self._creds)
        deadline = time.time() + KEY_TOTAL_WAIT_SEC
        while True:
            now = time.time()
            order = [(self._i + k) % n for k in range(n)]
            ready = [i for i in order if self._cooldown.get(i, 0.0) <= now]
            if ready:
                for i in ready:
                    try:
                        out = self._inst(i).complete(prompt, max_tokens, force_json)
                        self._i = (i + 1) % n   # spread the NEXT request onto the next key
                        return out
                    except Exception as e:      # noqa: BLE001
                        if _is_rate_limit(e):
                            # Park this key and move on. Even with ONE key we
                            # park-and-wait: the window resets in seconds.
                            self._cooldown[i] = time.time() + _cooldown_from(e)
                            continue
                        raise
            # Every key is cooling. A rate-limit window is SECONDS — wait it out
            # and keep the protocol on THESE keys (their budgets sum up across
            # the meeting), instead of bailing to another engine. Only a wait
            # that's too long (a real outage / brutal quota) falls through to
            # the provider chain.
            wait = max(min(self._cooldown.values()) - time.time(), 0.5)
            if wait > KEY_WAIT_SEC or time.time() + wait > deadline:
                raise RuntimeError(
                    f"Все {n} ключа(ей) «{self.name}» упёрлись в лимит, сброс "
                    f"через ~{int(wait)} с — это дольше обычного окна. "
                    "Добавьте ещё ключ или выберите другой движок.")
            time.sleep(wait + 0.3)


class _FallbackChain:
    """Provider-level failover: when the chosen engine is down (5xx, network,
    every key rate-limited), the next CONFIGURED provider takes over instead of
    the whole protocol dying with one cloud. The chain remembers which provider
    answered last and starts there — one meeting's map-reduce makes many calls,
    and flip-flopping back to a dead provider would pay the timeout every time."""

    def __init__(self, backends: list):
        self._backends = backends
        self._i = 0     # index of the provider that served the last call

    @property
    def name(self) -> str:
        return str(getattr(self._backends[self._i], "name", "llm"))

    @property
    def supports_stream(self) -> bool:
        return any(isinstance(b, OllamaProvider) for b in self._backends)

    def prefer_cloud(self) -> bool:
        """Move the cursor to the first CLOUD backend (Ollama stays as
        fallback). A long meeting on a CPU-only local engine takes HOURS —
        prompt evaluation alone runs at tens of tokens/sec; a cloud engine does
        the same call in seconds. No-op (False) when only Ollama is configured."""
        for i, b in enumerate(self._backends):
            if not isinstance(b, OllamaProvider):
                self._i = i
                return True
        return False

    @property
    def cloud_available(self) -> bool:
        return any(not isinstance(b, OllamaProvider) for b in self._backends)

    def complete(self, prompt: str, max_tokens: int = 2000, force_json: bool = True,
                 on_token=None, should_stop=None, json_schema: dict | None = None) -> str:
        errors = []
        n = len(self._backends)
        for k in range(n):
            i = (self._i + k) % n
            b = self._backends[i]
            # The caller sized max_tokens for the PRIMARY provider; re-clamp for
            # the one actually being tried, or Groq rejects the request with 413.
            mt = max_tokens
            tpm = config.PROVIDER_TPM.get(str(getattr(b, "name", "")).split(":")[0])
            if tpm:
                mt = max(1200, min(mt, tpm - len(prompt) // 3 - 400))
                # A big request (the final protocol asks for >=8000 tokens) that
                # this provider can only answer with <4000 would come back
                # TRUNCATED — broken JSON, lost detail. Prefer a provider that
                # fits; fall back to the tight one only when it's all we have.
                if max_tokens >= 8000 and mt < 4000 and k < n - 1:
                    errors.append(f"{getattr(b, 'name', '?')}: бюджет ответа "
                                  f"~{mt} ток. слишком мал для полного протокола")
                    continue
            try:
                if isinstance(b, OllamaProvider):
                    out = b.complete(prompt, max_tokens=mt, force_json=force_json,
                                     on_token=on_token, should_stop=should_stop,
                                     json_schema=json_schema)
                else:
                    out = b.complete(prompt, mt, force_json)
                self._i = i
                return out
            except GenerationCancelled:
                raise               # user cancellation is not a provider failure
            except Exception as e:  # noqa: BLE001
                errors.append(f"{getattr(b, 'name', '?')}: {e}")
        raise RuntimeError("Ни один движок ИИ не ответил. " + " | ".join(errors[:3]))


def get_provider_chain(name: str | None, keys: dict | None = None):
    """The requested provider first, then every other configured one as fallback
    (in PROVIDER_ORDER — free/local engines first). With a single configured
    provider this is just that provider."""
    primary = get_provider(name, keys)
    pname = str(getattr(primary, "name", "")).split(":")[0]
    backends = [primary]
    # Fallback order: other CLOUD engines first, Ollama LAST — a rate-limited
    # Groq should degrade to Gemini (seconds), not to a CPU-bound local model
    # (tens of minutes per call on a long prompt).
    rest = [p for p in config.available_providers(keys) if p != pname]
    rest.sort(key=lambda p: p == "ollama")
    for p in rest:
        try:
            backends.append(get_provider(p, keys))
        except Exception:  # noqa: BLE001 — an unconfigurable fallback just drops out
            continue
    return _FallbackChain(backends) if len(backends) > 1 else primary
