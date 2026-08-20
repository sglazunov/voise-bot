"""NVIDIA NIM: каталог моделей, проверка прав ключа, потоковый вызов, провайдер.

Отдельный модуль, потому что это не «ещё один провайдер в двадцать строк», а
подсистема на 450 строк: каталог у NVIDIA большой и меняется, ключ даёт доступ
лишь к части моделей (каталог != права), а длинные встречи требуют потокового
ответа — без него запрос упирается в таймаут прокси и возвращает 504.

Опирается только на llm_base, поэтому импорт не замыкается на llm.py.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

from . import config, logs
from .llm_base import (
    GenerationCancelled, _KeyProviderMixin, _http_post_json, _is_rate_limit,
    _safe_url,
)

log = logs.get("vtx.nvidia")


# ---------------------------------------------------------------------------
# Каталог NVIDIA большой (100+ моделей) и меняется, поэтому жёстко его не
# перечисляем: список берём по ключу через /v1/models, а этой таблицей лишь
# ранжируем — что показывать первым. Чем меньше число, тем выше в списке.
#
# Приоритет под нашу задачу (час русской речи → строгий JSON-протокол): нужны
# сильный русский, длинный контекст и послушность формату. Reasoning-модели
# (deepseek-r1 и подобные) стоят ниже: они склонны «размышлять» в ответе, а нам
# нужен чистый JSON.
# Семейства моделей: чем меньше число, тем выше в списке. Сравнение по
# ПОДСТРОКЕ, а не по полному имени — иначе новая версия («deepseek-v4-flash»
# против прописанного «deepseek-v3») выпадала бы в конец как незнакомая, то есть
# самая свежая модель оказывалась бы худшей по порядку.
_NVIDIA_FAMILY = (
    ("deepseek", 0),      # V4: окно 1M токенов — час встречи влезает целиком
    ("kimi", 0),          # K2.6: 262K, structured output, сильный русский
    ("nemotron-3-ultra", 1),
    ("nemotron-3-super", 2),
    ("qwen", 2),
    ("glm", 2),
    ("minimax", 3),
    ("step-", 3),
    ("gpt-oss", 3),
    ("nemotron", 4),
    ("llama", 4),
    ("mistral", 4),
    ("gemma", 5),
    ("sarvam", 6),
    ("phi", 6),
)

# В каталоге NVIDIA не только чат-модели: эмбеддинги, синтез речи, зрение,
# автопилот, модерация, даже белки (esm2). В списке движков протокола им делать
# нечего — иначе человек выбирает «magpie-tts» и получает непонятную ошибку уже
# после встречи. Отсекаем по назначению, а не по вендору.
_NVIDIA_NOT_CHAT = (
    "embed", "esm2",                                  # эмбеддинги, биология
    "tts", "voicechat", "studio-voice", "parakeet",    # речь
    "riva",                                           # перевод/ASR-сервисы
    "guard", "content-safety",                         # модерация
    "cosmos", "paligemma", "bevformer", "sparsedrive", "streampetr",
    "synthetic-video-detector", "ising-calibration",   # зрение, видео, автопилот
    "-vl", "vision",                                   # только картинки
    "rerank", "retriever",
    # Замечено в боевом каталоге 06.08 — всё это не умеет вести диалог:
    "bge-", "e5-", "nvclip", "nv-embedqa",             # эмбеддинги под другими именами
    "reward",                                          # оценивают ответы, а не пишут
    "-parse", "ocdrnet", "ocr",                        # разбор документов и картинок
    "diffusion",                                       # генерация изображений
    "chatqa",                                          # заточены под RAG-ответ по куску текста
    "codegemma", "codellama", "codestral", "-coder",   # только код, не деловой русский
)
# Reasoning-модели: «размышляют» в ответе, что мешает строгому JSON протокола.
_NVIDIA_REASONING = ("-r1", "/r1", "reason", "thinking")
# Мелкие/облегчённые варианты — на час русской речи заметно слабее.
_NVIDIA_SMALL = ("8b", "7b", "4b", "3b", "1.5b", "mini", "-lite", "small")
# Быстрые варианты той же версии («flash», «turbo») слабее полных («pro»,
# «max»). Для протокола важнее качество, поэтому внутри одной версии полная
# идёт первой: без этого «deepseek-v4-flash» опережал «deepseek-v4-pro» просто
# потому, что «f» раньше «p» по алфавиту.
_NVIDIA_FAST = ("flash", "turbo", "instant")
_NVIDIA_FULL = ("pro", "max", "-large", "ultra")


def _nvidia_version(model_id: str) -> float:
    """Номер версии из имени модели: «v4» → 4, «3.3» → 3.3, иначе 0.

    Нужен, чтобы новая версия семейства шла впереди старой без правок кода:
    каталог обновляется чаще, чем этот файл."""
    m = re.search(r"[-/]v(\d+(?:\.\d+)?)", model_id)
    if m:
        return float(m.group(1))
    m = re.search(r"(\d+\.\d+)", model_id)
    return float(m.group(1)) if m else 0.0


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
    # Оставляем только то, что может собрать протокол: см. _NVIDIA_NOT_CHAT.
    ids = [i for i in ids
           if not any(t in i.lower() for t in _NVIDIA_NOT_CHAT)]

    def rank(mid: str) -> tuple:
        low = mid.lower()
        fam_i, base = next(((i, r) for i, (name, r) in enumerate(_NVIDIA_FAMILY)
                            if name in low), (99, 8))
        if any(t in low for t in _NVIDIA_REASONING):
            base += 5            # reasoning — ниже обычных моделей семейства
        # У MoE в имени два числа: всего параметров и активных («80b-a3b» — 80
        # млрд всего, 3 млрд активных). Сила модели — по ПЕРВОМУ, поэтому
        # суффикс активных вырезаем: иначе qwen3-next-80b-a3b считался мелкой
        # моделью из-за «a3b» и уезжал в конец списка.
        sized = re.sub(r"-a\d+(?:\.\d+)?b", "", low)
        if any(t in sized for t in _NVIDIA_SMALL):
            base += 2            # облегчённые — после полноразмерных
        # Семейство идёт в ключе ПЕРЕД версией: номера версий разных семейств
        # несравнимы («deepseek v3» против «kimi k2.6» — ни о чём), поэтому
        # версия решает только внутри одного семейства.
        # Дальше: свежая версия впереди (минус — сортировка по возрастанию),
        # затем полная модель перед быстрой.
        variant = 0 if any(t in low for t in _NVIDIA_FULL) else \
                  2 if any(t in low for t in _NVIDIA_FAST) else 1
        return (base, fam_i, -_nvidia_version(low), variant, low)

    return sorted(ids, key=rank)


# ---- какие модели ключ РЕАЛЬНО может вызвать ------------------------------
# GET /v1/models отдаёт весь опубликованный каталог, а не права аккаунта:
# недоступная модель отвечает 404 «Function '<uuid>': Not found for account».
# Узнать это можно только вызовом, поэтому проверяем по одному дешёвому запросу
# на модель и складываем результат в кэш — на боевом ключе из сотни каталожных
# моделей рабочими оказываются единицы.
_NVIDIA_PROBE_MAX = int(os.getenv("VTX_NVIDIA_PROBE_MAX", "30"))
_NVIDIA_CACHE_TTL = int(os.getenv("VTX_NVIDIA_CACHE_TTL", str(7 * 24 * 3600)))
# Пауза между пробами: лимит ~40 запросов в минуту, держимся заметно ниже,
# иначе перебор упирается в 429 и сам портит себе результат.
_NVIDIA_PROBE_PAUSE = float(os.getenv("VTX_NVIDIA_PROBE_PAUSE", "2"))
_nvidia_probe_lock = threading.Lock()
# Ход перебора — чтобы интерфейс показывал прогресс, а не держал запрос
# открытым две минуты (и не выглядел зависшим).
_nvidia_progress: dict = {"running": False, "done": 0, "total": 0}


def nvidia_probe_state() -> dict:
    return dict(_nvidia_progress)


def nvidia_start_verify(api_key: str) -> dict:
    """Запустить перебор в фоне и сразу вернуть состояние."""
    if not _nvidia_probe_lock.locked():
        threading.Thread(target=nvidia_verify_models, args=(api_key,),
                         daemon=True, name="vtx-nvidia-probe").start()
        time.sleep(0.2)          # дать потоку выставить running
    return nvidia_probe_state()

# Сколько ждём ответа. Прежние 180 с срывали КАЖДУЮ сборку протокола:
# «nvidia: The read operation timed out» — и работу молча забирал запасной
# движок. Проверка моделей при этом проходила, потому что просит один токен и
# отвечает мгновенно; отсюда и загадка «модель доступна, но не работает».
# Бесплатный тариф NVIDIA медленный: 8000 токенов протокола там генерируются
# минутами.
# Потолок намеренно умеренный: воркер один, и повисший запрос задерживает всю
# очередь. Платим это ожидание один раз за протокол — цепочка отката помнит,
# кто ответил, и следующие вызовы начинает уже с него.
_NVIDIA_TIMEOUT_MAX = int(os.getenv("VTX_NVIDIA_TIMEOUT", "420"))


def _nvidia_timeout(max_tokens: int) -> int:
    """Время ожидания под размер ответа: пробе в один токен десять минут не
    нужны, а полному протоколу 180 секунд не хватает."""
    return max(60, min(_NVIDIA_TIMEOUT_MAX, 120 + int(max_tokens) // 8))


# Потоковый режим. Обычным запросом длинная генерация не доживает до конца:
# на часовой встрече сервер NVIDIA сам отвечает 504 «gateway timeout» — ждать
# весь ответ целиком их шлюз не готов. В потоке куски идут сразу, соединение
# всё время живо, и ограничение по времени применяется к ПАУЗЕ между кусками,
# а не ко всей генерации.
_NVIDIA_STREAM = os.getenv("VTX_NVIDIA_STREAM", "1") == "1"
_NVIDIA_CHUNK_TIMEOUT = int(os.getenv("VTX_NVIDIA_CHUNK_TIMEOUT", "120"))


def _nvidia_stream(url: str, payload: dict, headers: dict,
                   timeout: int = _NVIDIA_CHUNK_TIMEOUT, should_stop=None) -> str:
    """Собрать ответ из SSE-потока OpenAI-совместимого API.

    Формат: строки «data: {json}», конец — «data: [DONE]». Нас интересует
    choices[0].delta.content. Пустые строки и служебные поля пропускаем.
    """
    body = json.dumps({**payload, "stream": True}).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "text/event-stream")
    for k, v in headers.items():
        req.add_header(k, v)
    parts: list[str] = []
    done = False
    try:
        # Ошибку сервер отдаёт обычным JSON, а не потоком; urllib поднимает её
        # как HTTPError до первой строки, поэтому парсер тела ошибки не видит.
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            for raw in resp:            # построчно: таймаут — на каждый кусок
                if should_stop and should_stop():
                    raise GenerationCancelled()
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue            # пустые строки — keep-alive
                chunk = line[5:].strip()
                if chunk == "[DONE]":
                    done = True
                    break
                try:
                    d = json.loads(chunk)
                except ValueError:
                    continue            # рваный кусок пропускаем, не падаем
                choices = d.get("choices") or [{}]
                piece = (choices[0].get("delta") or {}).get("content") or ""
                if piece:
                    parts.append(piece)
    except urllib.error.HTTPError as e:
        body_txt = e.read().decode("utf-8", "replace")
        raise RuntimeError(f"HTTP {e.code} от {url}: {body_txt[:300]}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"Не удалось подключиться к {url}: {e.reason}") from e
    if not done:
        # Поток кончился без «[DONE]» — это обрыв, а не короткий ответ. Отдать
        # накопленное наверх нельзя: обрезанный JSON выглядит как готовый
        # протокол, только без половины разделов. Пусть решает обычный запрос.
        raise RuntimeError("поток оборван до конца ответа")
    return "".join(parts)


# «Function '<uuid>': Not found for account '<id>'» — единственный ответ,
# который действительно означает «модель этому ключу не выдана».
_NVIDIA_NO_ACCESS = ("not found for account", "not_found", "404")


def _nvidia_not_entitled(err: Exception) -> bool:
    s = str(err).lower()
    return any(t in s for t in _NVIDIA_NO_ACCESS)


def _nvidia_cache_file() -> Path:
    return config.DATA_DIR / "nvidia_usable.json"


def _nvidia_key_id(api_key: str) -> str:
    """Ключи в кэше не храним — только их отпечаток."""
    return hashlib.sha256((api_key or "").encode()).hexdigest()[:16]


def _nvidia_cache_read() -> dict:
    try:
        return json.loads(_nvidia_cache_file().read_text(encoding="utf-8")) or {}
    except (OSError, ValueError):
        return {}


def nvidia_usable_models(api_key: str | None = None) -> list[str] | None:
    """Проверенный список для ключа, либо None — если ещё не проверяли."""
    key = api_key or config.NVIDIA_API_KEY
    if not key:
        return None
    rec = _nvidia_cache_read().get(_nvidia_key_id(key))
    if not isinstance(rec, dict):
        return None
    if time.time() - float(rec.get("at") or 0) > _NVIDIA_CACHE_TTL:
        return None
    models = rec.get("models")
    return models if isinstance(models, list) else None


_nvidia_default_cache: dict[str, tuple[float, str]] = {}
_NVIDIA_DEFAULT_TTL = 600.0     # 10 минут: каталог меняется днями, не минутами


def nvidia_default_model(api_key: str | None = None) -> str:
    """Лучшая доступная модель: сначала проверенная по ключу, затем верхняя из
    каталога, и только если сети нет — имя из настроек.

    Результат кэшируется в памяти на 10 минут. Без кэша КАЖДОЕ создание
    провайдера без явной модели ходило в /v1/models с таймаутом 20 секунд — а
    провайдер создаётся на каждый analyze/verify/ask/regen. На длинной встрече
    это десятки лишних походов в сеть, и все они на критическом пути.
    """
    key = api_key or config.NVIDIA_API_KEY
    if not key:
        return config.NVIDIA_MODEL
    ident = _nvidia_key_id(key)
    hit = _nvidia_default_cache.get(ident)
    if hit and time.time() - hit[0] < _NVIDIA_DEFAULT_TTL:
        return hit[1]
    usable = nvidia_usable_models(key)
    model = usable[0] if usable else ""
    if not model:
        catalog = nvidia_models(key)
        model = catalog[0] if catalog else ""
    model = model or config.NVIDIA_MODEL
    _nvidia_default_cache[ident] = (time.time(), model)
    return model


def nvidia_ensure_verified(api_key: str | None = None) -> None:
    """Запустить перебор в фоне, если проверенного списка ещё нет.

    Перебор стартовал только при добавлении ключа — и если в этот момент
    контейнер перезапускали, поток погибал вместе с ним, а повторить было
    нечем: список движков навсегда оставался каталогом. Теперь проверка
    догоняет сама при первом обращении к списку."""
    key = api_key or config.NVIDIA_API_KEY
    if not key or nvidia_usable_models(key) is not None:
        return
    if _nvidia_probe_lock.locked():       # уже идёт
        return
    threading.Thread(target=nvidia_verify_models, args=(key,),
                     daemon=True, name="vtx-nvidia-probe").start()


def nvidia_verify_models(api_key: str, on_log=None, force: bool = True) -> list[str]:
    """Перебрать каталог и оставить модели, которые ответили. Долго (десятки
    секунд) — вызывать в фоне. Результат кладётся в кэш.

    `force=False` — «дождись чужой проверки и верни её результат»: так кнопка
    не запускает второй перебор поверх уже идущего фонового. Раньше она
    вставала на замок, дожидалась конца фонового прохода и начинала всё
    заново — минута ожидания превращалась в две.

    Лимит NVIDIA ~40 запросов в минуту на ключ и на все модели сразу, поэтому
    проверяем не весь каталог, а верхушку ранжированного списка."""
    if not api_key:
        return []
    with _nvidia_probe_lock:          # два параллельных перебора съели бы лимит
        if not force:
            done = nvidia_usable_models(api_key)
            if done is not None:
                return done
        catalog = nvidia_models(api_key)[:_NVIDIA_PROBE_MAX]
        _nvidia_progress.update(running=True, done=0, total=len(catalog))
        try:
            return _nvidia_probe_loop(catalog, api_key, on_log)
        finally:
            # Флаг обязан сняться даже при сбое, иначе интерфейс навсегда
            # останется в состоянии «проверяю».
            _nvidia_progress.update(running=False)


def _nvidia_probe_loop(catalog: list[str], api_key: str, on_log) -> list[str]:
    """Сам перебор: по одному дешёвому запросу на модель, с паузами."""
    ok: list[str] = []
    for n, mid in enumerate(catalog):
        if n:
            time.sleep(_NVIDIA_PROBE_PAUSE)   # держимся ниже лимита
        try:
            NvidiaProvider(model=mid, api_key=api_key).complete(
                "ok", max_tokens=1, force_json=False)
            ok.append(mid)
        except Exception as e:    # noqa: BLE001
            # Недоступной считаем ТОЛЬКО модель, про которую сервис прямо
            # сказал «нет такой для этого аккаунта». Лимит, таймаут, 500 —
            # это про наш запрос, а не про права: выбросить по ним модель
            # значит соврать в списке. Раньше выбрасывалось по любой ошибке.
            if _nvidia_not_entitled(e):
                continue
            if _is_rate_limit(e):
                time.sleep(_NVIDIA_PROBE_PAUSE * 4)
            ok.append(mid)
        _nvidia_progress.update(done=n + 1)
        if on_log:
            on_log(f"NVIDIA: проверено {n + 1} из {len(catalog)}, "
                   f"доступно {len(ok)}")
    cache = _nvidia_cache_read()
    cache[_nvidia_key_id(api_key)] = {"at": time.time(), "models": ok}
    try:
        _nvidia_cache_file().parent.mkdir(parents=True, exist_ok=True)
        _nvidia_cache_file().write_text(
            json.dumps(cache, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass
    return ok


class NvidiaProvider(_KeyProviderMixin):
    """NVIDIA NIM (build.nvidia.com) — OpenAI-совместимый, ключ `nvapi-…`.

    Бесплатный, без карты. Важное ограничение: лимит ~40 запросов в минуту на
    ключ И НА ВСЕ МОДЕЛИ СРАЗУ. Наш map-reduce на длинной встрече делает
    десятки запросов, поэтому на больших записях он может упереться в лимит —
    ротация ключей (_RotatingProvider) здесь особенно к месту.
    """

    name = "nvidia"
    # Признак для цепочки и обёртки ротации: этот движок умеет прерываться
    # ВНУТРИ вызова (в потоке один вызов длится минутами).
    accepts_should_stop = True

    def __init__(self, model: str | None = None, api_key: str | None = None,
                 extra: str | None = None) -> None:
        self.api_key = api_key or config.NVIDIA_API_KEY
        # Умолчание НЕ прибиваем к имени модели: каталог NVIDIA живой. За неделю
        # «deepseek-v4-pro» из него исчез, а появился «deepseek-v4-flash-0731» —
        # прибитое имя молча превращается в 404 в момент сборки протокола.
        # Поэтому берём лучшую из доступных по ключу, а имя из config —
        # только если каталог недоступен.
        self.model = model or nvidia_default_model(self.api_key)

    def complete(self, prompt: str, max_tokens: int = 2000,
                 force_json: bool = True, should_stop=None) -> str:
        url = "https://integrate.api.nvidia.com/v1/chat/completions"
        payload = {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens,
            "temperature": 0.1,
        }
        headers = {"Authorization": f"Bearer {self.api_key}"}
        tmo = _nvidia_timeout(max_tokens)

        def post(pl):
            """Таймаут подписываем числом: «read operation timed out» не
            говорит, сколько ждали, и по строке отката в протоколе нельзя
            понять, поднимать ли потолок ещё или дело в другом."""
            try:
                return _http_post_json(url, pl, headers, timeout=tmo)
            except Exception as e:      # noqa: BLE001
                if "timed out" in str(e).lower():
                    raise RuntimeError(
                        f"ответ не пришёл за {tmo} с (просили {max_tokens} "
                        f"токенов) — модель медленнее потолка "
                        f"VTX_NVIDIA_TIMEOUT") from e
                raise
        if force_json:
            payload["response_format"] = {"type": "json_object"}

        # Сначала поток: только так длинная генерация доживает до конца.
        # Отказ по правам или лимиту пробрасываем как есть — это не про способ
        # передачи; всё прочее (сеть, неподдержанный stream) отдаём обычному
        # запросу, чтобы новый режим не отнял работающее.
        if _NVIDIA_STREAM:
            try:
                text = _nvidia_stream(url, payload, headers,
                                      should_stop=should_stop)
                if text.strip():
                    return text.strip()
            except GenerationCancelled:
                raise                   # отмена — не повод пробовать иначе
            except Exception as e:      # noqa: BLE001
                if _nvidia_not_entitled(e) or _is_rate_limit(e):
                    raise
                # Молчать здесь нельзя. Дальше идёт обычный запрос, и на длинной
                # генерации он ловит от шлюза NVIDIA 504 — именно эта 504 и
                # попадала в шапку протокола, а настоящая причина (почему
                # оборвался ПОТОК) не оставляла следа нигде.
                log.warning("NVIDIA: поток не удался (%s), пробую обычным "
                            "запросом — на длинном ответе шлюз, скорее всего, "
                            "ответит 504", e)

        if force_json:
            try:
                out = post(payload)
                return out["choices"][0]["message"]["content"].strip()
            except Exception as e:      # noqa: BLE001
                # В каталоге сотня моделей, и не каждая понимает
                # response_format. Не теряем запрос из-за этого: повторяем без
                # него — JSON всё равно валидируется на нашей стороне.
                if "response_format" not in str(e).lower():
                    raise
                payload.pop("response_format", None)
        out = post(payload)
        return out["choices"][0]["message"]["content"].strip()

