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

import hashlib
import json
import os
import re
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Protocol

from . import config


# Фундамент вынесен в соседний модуль (llm_base): общий HTTP, разбор лимитов,
# отмена генерации, протокол провайдера. Имена ре-экспортируются: снаружи и в
# тестах обращаются к llm.X.
from .llm_base import (                 # noqa: F401 — часть публичного API
    GenerationCancelled, LLMProvider, _KeyProviderMixin,
    _http_post_json, _is_rate_limit, is_key_rejected, _retry_after, _safe_url,
)
from .llm_custom import CustomProvider, text_of  # noqa: F401 — часть публичного API


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

class GroqProvider(_KeyProviderMixin):
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
        out = _http_post_json(url, payload, headers, timeout=180,
                              max_retries=self._retries)
        # Разбор общий с «своим ключом»: content бывает null (рассуждающие
        # модели) или списком кусков, и голое .strip() падало на None.
        text, _ = text_of(out)
        if not text:
            raise RuntimeError(f"{self.name}: модель вернула пустой ответ.")
        return text



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
class GeminiProvider(_KeyProviderMixin):
    """Google Gemini. Free tier, needs GEMINI_API_KEY. May be region-blocked."""

    name = "gemini"

    def __init__(self, model: str | None = None, api_key: str | None = None,
                 extra: str | None = None) -> None:
        self.model = model or config.GEMINI_MODEL
        self.api_key = api_key or config.GEMINI_API_KEY

    def complete(self, prompt: str, max_tokens: int = 2000, force_json: bool = True) -> str:
        model = self.model
        # Ключ — ЗАГОЛОВКОМ, а не в адресе. В адресе он попадал в текст ошибки
        # (_http_post_json печатает url), оттуда — в причину отката, в карточку
        # задачи и в шапку Word-протокола. То есть секрет уезжал в документ,
        # который потом кладут в облако и Weeek.
        url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
               f"{model}:generateContent")
        gen = {"temperature": 0.1, "maxOutputTokens": max_tokens}
        if force_json:
            gen["responseMimeType"] = "application/json"
        payload = {"contents": [{"parts": [{"text": prompt}]}], "generationConfig": gen}
        out = _http_post_json(url, payload,
                              headers={"x-goog-api-key": self.api_key},
                              max_retries=self._retries)
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
        out = _http_post_json(url, payload, headers,
                              max_retries=self._retries)
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
    # Сбер выпускает сертификаты своим корневым центром («Russian Trusted Root
    # CA»), которого нет в системном хранилище — поэтому проверка и была
    # выключена целиком. Это плохо: ключ авторизации уходил по соединению, чью
    # подлинность никто не подтверждал. Правильный путь — положить их корневой
    # сертификат в образ и указать его здесь (VTX_GIGACHAT_CA либо стандартный
    # путь /usr/local/share/ca-certificates/russian_trusted_root_ca.crt).
    # Пока файла нет, проверка отключается КАК И РАНЬШЕ, но об этом пишется
    # предупреждение — молча ходить без проверки нельзя.
    _ctx = ssl.create_default_context()
    _ca_path = os.getenv(
        "VTX_GIGACHAT_CA",
        "/usr/local/share/ca-certificates/russian_trusted_root_ca.crt")
    if os.path.exists(_ca_path):
        _ctx.load_verify_locations(cafile=_ca_path)
    else:
        import warnings as _warnings
        _warnings.warn(
            "GigaChat: сертификат Минцифры не найден (%s) — TLS-проверка "
            "отключена, ключ уходит по непроверенному соединению. Положите "
            "файл в образ или задайте VTX_GIGACHAT_CA." % _ca_path,
            RuntimeWarning, stacklevel=2)
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
        # Разбор общий с «своим ключом»: content бывает null (рассуждающие
        # модели) или списком кусков, и голое .strip() падало на None.
        text, _ = text_of(out)
        if not text:
            raise RuntimeError(f"{self.name}: модель вернула пустой ответ.")
        return text


_PROVIDERS = {
    "custom": CustomProvider,
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
    if resolved == "custom" and model:
        # Под «своим ключом» лежат подключения к РАЗНЫМ сервисам, и ключ одного
        # к моделям другого отношения не имеет: запрос модели Yandex Cloud с
        # ключом OpenRouter — это гарантированный 401. Берём те подключения,
        # где запрошенная модель объявлена; если ни в одном её нет (список
        # моделей мог устареть), работаем как раньше — по всем.
        creds = _creds_with_model(creds, model) or creds
    if len(creds) == 1:
        k, ex = creds[0]
        return cls(model=model, api_key=k, extra=ex)
    return _RotatingProvider(cls, model, creds)


def _creds_with_model(creds: list[tuple[str, str]], model: str) -> list[tuple[str, str]]:
    """Подключения, у которых объявлена именно эта модель."""
    out = []
    for key, extra in creds:
        try:
            cfg = json.loads(extra) if extra else {}
        except ValueError:
            continue
        if not isinstance(cfg, dict):
            continue
        names = list(cfg.get("models") or [])
        if cfg.get("model"):
            names.append(cfg["model"])
        if model in names:
            out.append((key, extra))
    return out


_KEY_COOLDOWN_SEC = 60.0   # a rate-limited key rests this long (Groq's TPM window)
# Отвергнутый ключ (401/403) на встрече уже не оживёт: чинить его надо руками,
# а до тех пор он только тратит время на каждом вызове map-reduce.
_DEAD_KEY_SEC = 24 * 3600.0
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
        self._dead: dict[int, str] = {}         # key index -> почему отвергнут
        self._instances: dict[int, object] = {}
        # Умеет ли обёрнутый движок прерываться внутри вызова. Без этого при
        # ДВУХ ключах отмена в потоке молча переставала работать: цепочка
        # смотрит на тип, а тип здесь — обёртка, а не сам провайдер.
        self.accepts_should_stop = getattr(cls, "accepts_should_stop", False)

    @property
    def model(self) -> str:
        """Модель, которой сейчас работаем.

        Обёртка обязана отдавать её наравне с именем: при ДВУХ ключах в шапке
        протокола оставалось голое имя провайдера, и было не понять, какая
        модель его собрала. Свойство ЛЕНИВОЕ: у поставщика с каталогом создание
        провайдера без явной модели лезет в сеть, и делать это при построении
        цепочки не нужно.
        """
        if self._model:
            return self._model
        # Берём у ЛЮБОГО уже созданного ключа: курсор _i после успешного вызова
        # сдвигается на следующий ключ, который может быть ещё не создан.
        # Модель у всех ключей одна и та же.
        for inst in self._instances.values():
            m = getattr(inst, "model", "")
            if m:
                return str(m)
        return ""

    def _inst(self, i: int):
        if i not in self._instances:
            k, ex = self._creds[i]
            inst = self._cls(model=self._model, api_key=k, extra=ex)
            # При нескольких ключах провайдер не должен спать внутри себя —
            # см. _retry_inside: ждать надо не на исчерпанном ключе, а перейти
            # к следующему.
            if not self._retry_inside and hasattr(inst, "max_retries"):
                inst.max_retries = 0
            self._instances[i] = inst
        return self._instances[i]

    @property
    def _retry_inside(self) -> bool:
        """Ждать ли внутри одного ключа. При НЕСКОЛЬКИХ ключах — нет: смысл
        ротации в том, чтобы при 429 сразу уйти на следующий ключ, а
        _http_post_json до трёх раз спал по 30 секунд на том же самом, и до
        ротации дело почти не доходило."""
        return len(self._creds) < 2

    def complete(self, prompt: str, max_tokens: int = 2000,
                 force_json: bool = True, should_stop=None) -> str:
        n = len(self._creds)
        deadline = time.time() + KEY_TOTAL_WAIT_SEC
        kw = {"should_stop": should_stop} if self.accepts_should_stop else {}
        while True:
            now = time.time()
            order = [(self._i + k) % n for k in range(n)]
            ready = [i for i in order if self._cooldown.get(i, 0.0) <= now]
            if ready:
                for i in ready:
                    try:
                        out = self._inst(i).complete(prompt, max_tokens,
                                                     force_json, **kw)
                        self._i = (i + 1) % n   # spread the NEXT request onto the next key
                        return out
                    except Exception as e:      # noqa: BLE001
                        if _is_rate_limit(e):
                            # Park this key and move on. Even with ONE key we
                            # park-and-wait: the window resets in seconds.
                            self._cooldown[i] = time.time() + _cooldown_from(e)
                            continue
                        if is_key_rejected(e) and n > 1:
                            # Ключ отвергнут (401/403) — это про НЕГО, а не про
                            # движок: соседний ключ в пуле может быть от совсем
                            # другого сервиса. Раньше здесь был безусловный
                            # raise, и одно протухшее подключение уводило всю
                            # встречу запасному движку, ни разу не попробовав
                            # рабочий ключ.
                            self._dead[i] = str(e)
                            self._cooldown[i] = time.time() + _DEAD_KEY_SEC
                            continue
                        raise
            alive = [i for i in range(n) if i not in self._dead]
            if not alive:
                raise RuntimeError(
                    f"Все {n} ключа(ей) «{self.name}» отвергнуты сервисом: "
                    + " | ".join(list(self._dead.values())[:3]))
            # Every key is cooling. A rate-limit window is SECONDS — wait it out
            # and keep the protocol on THESE keys (their budgets sum up across
            # the meeting), instead of bailing to another engine. Only a wait
            # that's too long (a real outage / brutal quota) falls through to
            # the provider chain.
            # Ждём только живые ключи: отвергнутый лежит сутки, и по нему
            # ожидание вышло бы бесконечным.
            wait = max(min(self._cooldown.get(i, 0.0) for i in alive) - time.time(), 0.5)
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
        self.skipped: list[str] = []   # почему пропущены движки выше по списку

    @property
    def name(self) -> str:
        return str(getattr(self._backends[self._i], "name", "llm"))

    @property
    def model(self) -> str:
        """Модель ТОГО движка, который отвечает сейчас — чтобы в протоколе было
        видно, кто его собрал, когда сработал откат на другого провайдера."""
        return str(getattr(self._backends[self._i], "model", "") or "")

    @property
    def accepts_should_stop(self) -> bool:
        """Умеет ли ХОТЬ ОДИН движок цепочки прерываться внутри вызова.

        Без этого признака «Стоп» не доходил до движка, умеющего прерываться,
        если в цепочке не было Ollama: вызывающий код смотрел только на
        supports_stream (то есть на выдачу токенов наружу). Прерывание бывает и
        без выдачи токенов — это разные вещи.
        """
        return any(getattr(b, "accepts_should_stop", False)
                   for b in self._backends)

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

    def complete(self, prompt: str, max_tokens: int = 2000, force_json: bool = True,
                 on_token=None, should_stop=None, json_schema: dict | None = None) -> str:
        errors = []
        n = len(self._backends)
        tight: list = []          # движки, отложенные из-за тесного бюджета
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
                    # Откладываем, но НЕ выбрасываем. Раньше здесь стоял
                    # continue, и «тесный» движок терялся навсегда: если
                    # остальные падали, вызов заканчивался «Ни один движок не
                    # ответил», хотя рабочий движок был — просто с урезанным
                    # ответом. Он лучше, чем ничего: попробуем его в конце.
                    tight.append((i, b, mt))
                    errors.append(f"{getattr(b, 'name', '?')}: бюджет ответа "
                                  f"~{mt} ток. слишком мал для полного протокола")
                    continue
            try:
                if isinstance(b, OllamaProvider):
                    out = b.complete(prompt, max_tokens=mt, force_json=force_json,
                                     on_token=on_token, should_stop=should_stop,
                                     json_schema=json_schema)
                elif getattr(b, "accepts_should_stop", False):
                    # В потоке один вызов живёт минутами — «Стоп» обязан
                    # действовать внутри него, а не только между вызовами.
                    # Проверяем ПРИЗНАК, а не тип: при нескольких ключах здесь
                    # лежит обёртка ротации, и проверка типа её не узнавала.
                    out = b.complete(prompt, mt, force_json,
                                     should_stop=should_stop)
                else:
                    out = b.complete(prompt, mt, force_json)
                self._i = i
                # Почему выбранный движок не отработал. Раньше это молча
                # терялось: человек выбирал DeepSeek, протокол собирал Gemini,
                # и узнать причину было неоткуда.
                for msg in errors:
                    if msg not in self.skipped and len(self.skipped) < 3:
                        self.skipped.append(msg)
                return out
            except GenerationCancelled:
                raise               # user cancellation is not a provider failure
            except Exception as e:  # noqa: BLE001
                errors.append(f"{getattr(b, 'name', '?')}: {e}")
        # Никто не ответил — пробуем отложенных. Урезанный протокол лучше, чем
        # полное отсутствие протокола после часа распознавания.
        for i, b, mt in tight:
            try:
                out = (b.complete(prompt, mt, force_json, should_stop=should_stop)
                       if getattr(b, "accepts_should_stop", False)
                       else b.complete(prompt, mt, force_json))
                self._i = i
                for msg in errors:
                    if msg not in self.skipped and len(self.skipped) < 3:
                        self.skipped.append(msg)
                return out
            except GenerationCancelled:
                raise
            except Exception as e:      # noqa: BLE001
                errors.append(f"{getattr(b, 'name', '?')} (тесный бюджет): {e}")
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
