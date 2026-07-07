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
import re
import ssl
import time
import urllib.error
import urllib.request
import uuid
from typing import Protocol

from . import config


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
                       "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
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
    import os
    import shutil
    exe = os.path.join(os.environ.get("LOCALAPPDATA", ""),
                       "Programs", "Ollama", "ollama.exe")
    if os.path.exists(exe) or shutil.which("ollama"):
        return "installed"
    return "missing"


class OllamaProvider:
    """Local Ollama server. Free, offline, no API key.

    A specific model can be chosen per job; `name` carries it (e.g.
    "ollama:vtx-protocol") so per-model Word docs and labels stay distinct.
    """

    def __init__(self, model: str | None = None) -> None:
        self.model = model or config.OLLAMA_MODEL
        self.name = f"ollama:{self.model}"

    def complete(self, prompt: str, max_tokens: int = 2000, force_json: bool = True,
                 on_token=None) -> str:
        url = config.OLLAMA_URL.rstrip("/") + "/api/generate"
        payload = {
            "model": self.model,
            "prompt": prompt,
            "stream": bool(on_token),
            "options": {"temperature": 0.1, "num_predict": max_tokens},
        }
        if force_json:
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


class _RotatingProvider:
    """Wraps a provider with a POOL of API keys. On a rate-limit error it moves to
    the next key and retries the same request — so long protocol generation isn't
    interrupted when one key is exhausted. Fails only if ALL keys are limited."""

    def __init__(self, cls, model, creds: list[tuple[str, str]]):
        self._cls = cls
        self._model = model
        self._creds = creds
        self.name = getattr(cls, "name", "llm")
        self._i = 0                 # current key index (sticks to a working one)
        self._instances: dict[int, object] = {}

    def _inst(self, i: int):
        if i not in self._instances:
            k, ex = self._creds[i]
            self._instances[i] = self._cls(model=self._model, api_key=k, extra=ex)
        return self._instances[i]

    def complete(self, prompt: str, max_tokens: int = 2000, force_json: bool = True) -> str:
        n = len(self._creds)
        limited = []
        for attempt in range(n):
            i = (self._i + attempt) % n
            try:
                out = self._inst(i).complete(prompt, max_tokens, force_json)
                self._i = i           # keep using this key for the next chunk
                return out
            except Exception as e:    # noqa: BLE001
                if _is_rate_limit(e) and n > 1:
                    limited.append(i + 1)
                    continue          # try the next key
                raise
        raise RuntimeError(
            f"Все {n} API-ключа(ей) исчерпали лимит (ключи {limited}). "
            "Добавьте ещё ключ или подождите сброса лимита.")
