"""Любой OpenAI-совместимый провайдер по одному ключу.

Зачем. Под каждого поставщика приходилось писать свой класс, а поставщики
меняются: NVIDIA за неделю дважды поменяла состав бесплатных моделей и отобрала
DeepSeek. Здесь наоборот — ключ один, а всё остальное выясняется само:

  * поставщик угадывается по виду ключа (`nvapi-…` — NVIDIA, `gsk_…` — Groq и
    так далее), для незнакомого нужен только адрес API;
  * адрес проверяется запросом списка моделей — заодно это проверка, что ключ
    жив;
  * способ авторизации подбирается перебором: почти все берут
    `Authorization: Bearer`, но, например, MiMo от Xiaomi ждёт заголовок
    `api-key`;
  * список моделей приходит от самого поставщика, прибивать его не нужно.

Всё найденное хранится рядом с ключом, поэтому при следующем запуске ничего не
определяется заново.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request

from . import logs
from .llm_base import (GenerationCancelled, _KeyProviderMixin, _http_post_json,
                       _safe_url)

log = logs.get("vtx.custom")

# Известные поставщики по виду ключа. Совпадение по префиксу — подсказка, а не
# приговор: адрес всё равно проверяется запросом, и если поставщик не тот,
# проверка это покажет.
KNOWN = (
    ("nvapi-", "NVIDIA NIM", "https://integrate.api.nvidia.com/v1"),
    ("gsk_", "Groq", "https://api.groq.com/openai/v1"),
    ("sk-ant-", "Anthropic", ""),          # не OpenAI-совместим, см. ниже
    ("sk-or-", "OpenRouter", "https://openrouter.ai/api/v1"),
    ("sk-proj-", "OpenAI", "https://api.openai.com/v1"),
    ("AIza", "Google Gemini", ""),         # свой формат, см. ниже
)
# Куда заглянуть, когда вид ключа ничего не говорит. Порядок — от более
# вероятного к менее.
GUESS_URLS = (
    "https://api.deepseek.com/v1",
    "https://api.xiaomimimo.com/v1",
    "https://openrouter.ai/api/v1",
    "https://api.openai.com/v1",
)
# Способы передать ключ. Bearer — почти везде; api-key ждёт MiMo от Xiaomi;
# «none» — свой сервер (vLLM, llama.cpp, LM Studio, Ollama), который обычно
# вообще не спрашивает ключа.
AUTH_STYLES = ("bearer", "api-key", "x-api-key", "none")


def _auth_headers(style: str, key: str) -> dict:
    if style == "none" or not key:
        return {}
    if style == "bearer":
        return {"Authorization": f"Bearer {key}"}
    return {style: key}


def hint_for(key: str) -> tuple[str, str]:
    """Подсказка по виду ключа: (название поставщика, адрес) или пустые строки."""
    k = (key or "").strip()
    for prefix, name, url in KNOWN:
        if k.startswith(prefix):
            return name, url
    return "", ""


def list_models(base_url: str, key: str, style: str, timeout: int = 15) -> list[str]:
    """Модели поставщика. Пустой список — значит адрес или ключ не подошли."""
    url = base_url.rstrip("/") + "/models"
    req = urllib.request.Request(url, method="GET")
    for h, v in _auth_headers(style, key).items():
        req.add_header(h, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError):
        return []
    items = data.get("data") if isinstance(data, dict) else data
    out = []
    for it in items or []:
        mid = it.get("id") if isinstance(it, dict) else str(it)
        if mid:
            out.append(str(mid))
    return out


def detect(key: str, base_url: str = "") -> dict:
    """Определить поставщика по ключу: адрес, способ авторизации, модели.

    Возвращает {ok, base_url, auth, models, hint, error}. Ключ не логируется и
    в ответ не попадает.
    """
    key = (key or "").strip()
    base_url = (base_url or "").strip()
    if not key and not base_url:
        # Без ключа можно подключить только СВОЙ сервер, и тогда нужен адрес:
        # угадывать по пустому ключу нечего.
        return {"ok": False,
                "error": "Укажите ключ — или адрес API, если это ваш сервер "
                         "без ключа (например http://vllm:8000/v1)."}
    hint, known_url = hint_for(key)
    urls = [u for u in (base_url, known_url) if u] or list(GUESS_URLS)
    for url in urls:
        for style in AUTH_STYLES:
            models = list_models(url, key, style)
            if models:
                log.info("Ключ опознан: %s, моделей %d, авторизация %s",
                         _safe_url(url), len(models), style)
                return {"ok": True, "base_url": url.rstrip("/"), "auth": style,
                        "models": models, "hint": hint}
    if not base_url:
        return {"ok": False, "hint": hint,
                "error": "Ключ не подошёл ни к одному известному адресу. "
                         "Укажите адрес API поставщика — обычно он оканчивается "
                         "на /v1."}
    return {"ok": False, "hint": hint,
            "error": ("По этому адресу ответа нет. Проверьте адрес и ключ; для "
                      "своего сервера убедитесь, что он отвечает на "
                      f"{base_url.rstrip('/')}/models и доступен из контейнера "
                      "приложения (localhost внутри контейнера — это САМ "
                      "контейнер, а не хост).")}


class CustomProvider(_KeyProviderMixin):
    """Поставщик, описанный настройками, а не отдельным классом.

    `extra` несёт JSON с адресом, способом авторизации и моделью — так эти
    сведения переживают перезапуск и не определяются заново на каждый запрос.
    """

    name = "custom"
    accepts_should_stop = True

    def __init__(self, model: str | None = None, api_key: str | None = None,
                 extra: str | None = None) -> None:
        self.api_key = api_key or ""
        cfg = {}
        if extra:
            try:
                cfg = json.loads(extra)
            except ValueError:
                cfg = {}
        self.base_url = str(cfg.get("base_url") or "").rstrip("/")
        self.auth = str(cfg.get("auth") or "bearer")
        self.model = model or str(cfg.get("model") or "")

    def _headers(self) -> dict:
        return _auth_headers(self.auth, self.api_key)

    def complete(self, prompt: str, max_tokens: int = 2000,
                 force_json: bool = True, should_stop=None) -> str:
        if not self.base_url or not self.model:
            raise RuntimeError(
                "Провайдер не настроен: нет адреса API или модели. Подключите "
                "ключ заново на странице «Нейросети».")
        url = self.base_url + "/chat/completions"
        payload = {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens,
            "temperature": 0.1,
        }
        if force_json:
            payload["response_format"] = {"type": "json_object"}
        try:
            out = _http_post_json(url, payload, self._headers(), timeout=300,
                                  max_retries=self._retries)
        except Exception as e:              # noqa: BLE001
            # response_format понимают не все: повторяем без него, JSON всё
            # равно проверяется на нашей стороне.
            if force_json and "response_format" in str(e).lower():
                payload.pop("response_format", None)
                out = _http_post_json(url, payload, self._headers(), timeout=300,
                                      max_retries=self._retries)
            else:
                raise
        if should_stop and should_stop():
            raise GenerationCancelled()
        return out["choices"][0]["message"]["content"].strip()
