"""Любой OpenAI-совместимый провайдер по одному ключу.

Зачем. Под каждого поставщика приходилось писать свой класс, а поставщики
меняются: у одного поставщика состав бесплатных моделей сменился дважды за
неделю, и обкатанная модель просто исчезла. Здесь наоборот — ключ один, а всё
остальное выясняется само:

  * поставщик угадывается по виду ключа (`gsk_…` — Groq, `sk-proj-…` — OpenAI и
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

import ipaddress
import json
import socket
import time
import urllib.error
import urllib.parse
import urllib.request

from . import config, logs
from .llm_base import (GenerationCancelled, _KeyProviderMixin, _http_post_json,
                       _safe_url)

log = logs.get("vtx.custom")

# Способы передать ключ. Bearer берут почти все, поэтому он первый; «none» —
# свой сервер (vLLM, llama.cpp, LM Studio, Ollama), который ключа не спрашивает.
AUTH_STYLES = ("bearer", "api-key", "x-api-key", "none")

# Таймауты. Медленный поставщик не должен подвешивать подключение: молчание до
# таймаута шлюза выглядит как «непонятно, что происходит».
MODELS_TIMEOUT = 10
PING_TIMEOUT = 15
DETECT_DEADLINE = 25


def _auth_headers(style: str, key: str) -> dict:
    if style == "none" or not key:
        return {}
    if style == "bearer":
        return {"Authorization": f"Bearer {key}"}
    return {style: key}


def normalize_url(raw: str) -> str:
    """Привести адрес к базовому виду.

    Люди вставляют полный URL из документации — вместе с /chat/completions или
    /models. Без обрезки вышло бы «…/chat/completions/models», и адрес молча не
    работал бы.
    """
    u = (raw or "").strip().rstrip("/")
    for tail in ("/chat/completions", "/completions", "/models", "/embeddings"):
        if u.endswith(tail):
            u = u[: -len(tail)].rstrip("/")
    return u


def _is_private_host(host: str) -> bool:
    """Адрес внутри своей сети или имя сервиса docker — туда http:// можно."""
    if not host:
        return False
    if host in ("localhost", "host.docker.internal"):
        return True
    if "." not in host:
        return True                     # имя сервиса compose: app, vllm, ollama
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        try:
            ip = ipaddress.ip_address(socket.gethostbyname(host))
        except (OSError, ValueError):
            return False
    return bool(ip.is_private or ip.is_loopback)


def check_url(raw: str) -> str:
    """Проверить адрес ДО того, как послать туда ключ. Вернуть текст ошибки.

    Две опасности. Служебные адреса облака (169.254.169.254 и подобные): запрос
    туда с сервера возвращает учётные данные самой машины. И открытый http:// на
    публичный хост — ключ уйдёт по сети незашифрованным.
    """
    if not raw:
        return ""
    parts = urllib.parse.urlsplit(raw if "://" in raw else "https://" + raw)
    host = (parts.hostname or "").lower()
    if not host:
        return "Адрес не похож на URL — нужен вид https://сервер/v1."
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
    if ip is not None and (ip.is_link_local or ip.is_reserved or ip.is_multicast):
        return ("Этот адрес служебный, запросы туда запрещены: по нему облачные "
                "машины отдают собственные учётные данные.")
    if parts.scheme not in ("http", "https"):
        return "Поддерживаются только адреса http:// и https://."
    if parts.scheme == "http" and not _is_private_host(host):
        return ("Для публичного адреса нужен https://. Открытый http:// "
                "разрешён только для своих серверов в локальной сети — иначе "
                "ключ уйдёт по сети незашифрованным.")
    return ""


def unsupported_reason(key: str) -> str:
    """Ключ, который НЕЛЬЗЯ никуда отправлять, и почему."""
    k = (key or "").strip()
    for prefix, why in config.CUSTOM_UNSUPPORTED:
        if k.startswith(prefix):
            return why
    return ""


def is_chat_model(model_id: str) -> bool:
    """Годится ли модель для сборки протокола.

    В /models лежат вперемешку эмбеддинги, реранкеры, распознавание речи и
    картиночные модели. Протокол они не соберут, а список замусоривают и сбивают
    счётчик «доступно моделей — N».
    """
    low = (model_id or "").lower()
    return not any(bad in low for bad in config.CUSTOM_NON_CHAT)


# Пары «ключ + модель», на которых поставщик ответил «not found for account».
# Ключ — только его отпечаток, сам ключ здесь не хранится.
#
# Зачем помнить: 404 у одного ключа не значит 404 у другого — права выдаются на
# ключ. Поэтому запрет ЧАСТНЫЙ, и следующая попытка идёт другим ключом. Срок
# суточный: права меняются на стороне поставщика, и вечный запрет означал бы,
# что вернувшаяся модель больше никогда не будет использована.
_denied: dict[tuple[str, str], float] = {}
DENY_TTL = 24 * 3600


def _key_id(key: str) -> str:
    import hashlib
    return hashlib.sha256((key or "").encode("utf-8")).hexdigest()[:12]


def mark_denied(key: str, model: str) -> None:
    """Запомнить, что эта модель этому ключу не выдана."""
    if not model:
        return
    _denied[(_key_id(key), model)] = time.time() + DENY_TTL
    log.info("Модель %s недоступна ключу …%s — не пробуем сутки", model,
             _key_id(key)[-4:])


def is_denied(key: str, model: str) -> bool:
    until = _denied.get((_key_id(key), model))
    if until is None:
        return False
    if until < time.time():
        _denied.pop((_key_id(key), model), None)   # срок вышел — пробуем снова
        return False
    return True


def ping_model(base_url: str, key: str, style: str, model: str) -> tuple[bool, str]:
    """Дешёвый вызов: модель есть в списке — но выдана ли она ключу?

    Ровно этот разрыв стоил недели разбирательств: поставщик показывал модель в
    каталоге и отвечала 404 при вызове. Один токен стоит почти ничего, а знать
    это лучше при подключении, чем в момент сборки протокола.
    """
    payload = {"model": model, "max_tokens": 1, "temperature": 0,
               "messages": [{"role": "user", "content": "ok"}]}
    try:
        _http_post_json(base_url.rstrip("/") + "/chat/completions", payload,
                        _auth_headers(style, key), timeout=PING_TIMEOUT,
                        max_retries=0)
        return True, ""
    except Exception as e:              # noqa: BLE001
        return False, str(e)[:200]


def hint_for(key: str) -> tuple[str, str]:
    """Подсказка по виду ключа: (название поставщика, адрес) или пустые строки."""
    k = (key or "").strip()
    for prefix, name, url in config.CUSTOM_KEY_PREFIXES:
        if k.startswith(prefix):
            return name, url
    return "", ""


def list_models(base_url: str, key: str, style: str,
                timeout: int = MODELS_TIMEOUT) -> list[str]:
    """Модели поставщика. Пустой список — значит адрес или ключ не подошли."""
    url = base_url.rstrip("/") + "/models"
    req = urllib.request.Request(url, method="GET")
    for h, v in _auth_headers(style, key).items():
        req.add_header(h, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError):
        # Тело ответа не логируем: у некоторых поставщиков в нём эхом стоит ключ.
        return []
    items = data.get("data") if isinstance(data, dict) else data
    out = []
    for it in items or []:
        mid = it.get("id") if isinstance(it, dict) else str(it)
        if mid:
            out.append(str(mid))
    return out


def split_models(raw: str) -> list[str]:
    """Разобрать список моделей, записанных через запятую.

    На один ключ Yandex Cloud вешается несколько моделей, и выбирать между
    ними нужно в интерфейсе. Разделитель — запятая: в самих именах её нет, а
    слэши и двоеточия есть («gpt://<folder>/deepseek-v4-flash/latest»).
    """
    out, seen = [], set()
    for part in (raw or "").replace(chr(10), ",").split(","):
        m = part.strip()
        if m and m not in seen:
            seen.add(m)
            out.append(m)
    return out


def _manual_only(base_url: str) -> bool:
    """Шлюз, который не отдаёт список моделей по /v1/models.

    Такой, например, Yandex Cloud AI Studio: моделей у него много, но
    перечислять их через OpenAI-совместимый эндпоинт он не умеет, а имя модели
    включает идентификатор каталога — «gpt://<folder>/deepseek-v4-flash/latest».
    Для таких подключаемся по указанной вручную модели и проверяем её вызовом.
    """
    host = (urllib.parse.urlsplit(base_url).hostname or "").lower()
    return any(host == h or host.endswith("." + h)
               for h in config.CUSTOM_MANUAL_MODEL_HOSTS)


def detect(key: str, base_url: str = "", model: str = "") -> dict:
    """Определить поставщика по ключу: адрес, способ авторизации, модели.

    Возвращает {ok, base_url, auth, models, hint, error}. Ключ не логируется и
    в ответ не попадает.
    """
    key = (key or "").strip()
    base_url = normalize_url(base_url)
    stop = unsupported_reason(key)
    if stop:
        # Отказываем ДО перебора адресов: иначе ключ ушёл бы третьим лицам.
        return {"ok": False, "error": stop}
    bad = check_url(base_url)
    if bad:
        return {"ok": False, "error": bad}
    if not key and not base_url:
        # Без ключа можно подключить только СВОЙ сервер, и тогда нужен адрес:
        # угадывать по пустому ключу нечего.
        return {"ok": False,
                "error": "Укажите ключ — или адрес API, если это ваш сервер "
                         "без ключа (например http://vllm:8000/v1)."}
    hint, known_url = hint_for(key)
    urls = [u for u in (base_url, known_url) if u] or list(config.CUSTOM_GUESS_URLS)
    started = time.time()

    wanted = split_models(model)
    if base_url and (wanted or _manual_only(base_url)):
        # Список моделей не спрашиваем: либо шлюз его не отдаёт, либо человек
        # прямо назвал модели. Проверяем вызовом — это надёжнее любого списка.
        if not wanted:
            return {"ok": False, "hint": hint,
                    "error": "Этот шлюз не отдаёт список моделей — укажите их "
                             "вручную, через запятую. Для Yandex Cloud имя "
                             "выглядит как gpt://<идентификатор-каталога>/"
                             "deepseek-v4-flash/latest."}
        # На один ключ Yandex Cloud вешается НЕСКОЛЬКО моделей, поэтому
        # проверяем каждую: рабочие берём, про остальные говорим честно.
        for style in AUTH_STYLES:
            if not key and style != "none":
                continue
            ok_models, failed = [], []
            for m in wanted:
                ok, why = ping_model(base_url, key, style, m)
                (ok_models if ok else failed).append(m if ok else (m, why))
            if ok_models:
                log.info("Моделей подтверждено вызовом: %d из %d, %s, авторизация %s",
                         len(ok_models), len(wanted), _safe_url(base_url), style)
                out = {"ok": True, "base_url": base_url, "auth": style,
                       "models": ok_models, "hint": hint, "manual": True}
                if failed:
                    out["failed"] = [m for m, _ in failed]
                return out
            last = failed[0][1] if failed else "нет ответа"
        names = ", ".join(wanted)
        return {"ok": False, "hint": hint,
                "error": f"Ни одна из моделей ({names}) не ответила по этому "
                         f"адресу: {last}"}

    for url in urls:
        for style in AUTH_STYLES:
            if time.time() - started > DETECT_DEADLINE:
                return {"ok": False, "hint": hint,
                        "error": f"Определение заняло дольше {DETECT_DEADLINE} с "
                                 "и остановлено. Укажите адрес API вручную."}
            if not key and style != "none":
                continue        # без ключа перебирать заголовки незачем
            found = list_models(url, key, style)
            if not found:
                continue
            # Не-чат модели убираем: эмбеддинги и реранкеры протокол не соберут,
            # а список замусоривают и сбивают счётчик «доступно моделей — N».
            models = [m for m in found if is_chat_model(m)]
            if not models:
                continue
            log.info("Ключ опознан: %s, моделей %d из %d, авторизация %s, %.1f c",
                     _safe_url(url), len(models), len(found), style,
                     time.time() - started)
            return {"ok": True, "base_url": url.rstrip("/"), "auth": style,
                    "models": models, "hint": hint,
                    "skipped": len(found) - len(models)}
    if not base_url:
        return {"ok": False, "hint": hint,
                "error": "Ключ не подошёл ни к одному известному адресу. "
                         "Укажите адрес API поставщика — обычно он оканчивается "
                         "на /v1."}
    return {"ok": False, "hint": hint,
            "error": ("По этому адресу ответа нет. Проверьте адрес и ключ; для "
                      "своего сервера убедитесь, что он отвечает на "
                      f"{base_url}/models и доступен из контейнера приложения "
                      "(localhost внутри контейнера — это САМ контейнер, а не "
                      "хост: укажите имя сервиса из docker-compose.yml или "
                      "host.docker.internal).")}


def text_of(out: dict) -> tuple[str, str]:
    """Текст ответа и finish_reason из OpenAI-совместимого ответа.

    `choices[0].message.content` бывает не только строкой:

      * СПИСКОМ кусков `[{"type": "text", "text": …}]` — так отвечают
        некоторые шлюзы;
      * `null` — у рассуждающих моделей (DeepSeek R1 и родня): текст они
        кладут в `reasoning_content`, а `content` оставляют пустым; так же
        выходит, когда лимит токенов кончился прямо в рассуждениях
        (`finish_reason = "length"`).

    Прямое `["content"].strip()` на этом падало с «'NoneType' object has no
    attribute 'strip'» — встреча уходила запасному движку, и по такому тексту
    в шапке протокола понять причину было нельзя.
    """
    choices = (out or {}).get("choices") or []
    if not choices:
        return "", ""
    ch = choices[0] or {}
    finish = str(ch.get("finish_reason") or "")
    msg = ch.get("message") or ch.get("delta") or {}
    raw = msg.get("content")
    if isinstance(raw, list):
        raw = "".join(part.get("text", "") for part in raw
                      if isinstance(part, dict))
    text = str(raw or "").strip()
    if not text:
        # Рассуждения — не ответ, но лучше пустоты: протокол там нередко есть,
        # а разбор JSON всё равно ищет фигурную скобку в любом тексте.
        text = str(msg.get("reasoning_content") or "").strip()
    return text, finish


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
        text, finish = text_of(out)
        if not text and "response_format" in payload:
            # Строгий JSON-режим часть шлюзов принимает, но отвечает на него
            # пустотой. Отличить это от настоящей пустоты можно только одним
            # способом — переспросить без него. Разбор JSON у нас всё равно
            # свой, так что режим ничего не гарантировал.
            payload.pop("response_format", None)
            out = _http_post_json(url, payload, self._headers(), timeout=300,
                                  max_retries=self._retries)
            text, finish = text_of(out)
        if not text:
            more = " (лимит токенов кончился прямо в ответе)" if finish == "length" else ""
            raise RuntimeError(
                f"Модель «{self.model}» вернула пустой ответ{more}. У "
                "рассуждающих моделей весь лимит уходит в рассуждения — "
                "возьмите модель без рассуждений или другого поставщика.")
        return text

    def complete_guarded(self, *a, **kw) -> str:
        """complete с запоминанием «модель этому ключу не выдана».

        Отдельным методом, чтобы обычный complete оставался прозрачным: пометка
        — побочный эффект, и прятать её внутрь общего пути не хочется.
        """
        try:
            return self.complete(*a, **kw)
        except Exception as e:          # noqa: BLE001
            low = str(e).lower()
            if "not found for account" in low or "model not found" in low:
                mark_denied(self.api_key, self.model)
            raise
