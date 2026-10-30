"""Эмбеддинги кусков встреч — фаза 1 памяти встреч (30.10.2026).

Зачем. Память встреч (`meeting_index`, таблица `meeting_chunks`) ищется
полнотекстом Postgres: русская морфология ловит «решили/решение», но не
переформулировку — «сколько стоит сайт» и «бюджет на лендинг» для tsvector
разные вещи. Вектор текста сравнивает смысл, и гибрид «полнотекст + вектор»
(`analyze.retrieve_meetings`) достаёт и точные слова, и близкие по смыслу.

Что здесь. Три провайдера с одним интерфейсом — `embed_texts(texts, kind)` и
`dim()`: Yandex (text-search-doc / text-search-query, 256 измерений), Gemini
(gemini-embedding-001, размерность задаётся запросом) и Ollama (любая модель
эмбеддингов на своём сервере). `kind` различает документ и запрос: у Yandex
это две разные модели, у Gemini — `taskType`; тексты кусков считаются как
«doc», вопрос пользователя — как «query».

Правила, на которых всё держится:
- ВЫКЛЮЧЕНО по умолчанию (`VTX_EMBED_PROVIDER` пуст): память встреч работает
  полнотекстом ровно как раньше, образ базы без pgvector ничего не замечает.
- Ошибка провайдера — предупреждение в лог `vtx.embeddings`, не исключение:
  эмбеддинги — надстройка, и они не должны ломать ни сборку протокола, ни
  индексацию архива, ни ответ на вопрос (там останется полнотекст).
- Ключи — как у движков протокола: сначала ключ команды из интерфейса
  (`user_creds`), потом `.env` (`config.provider_creds`).
"""
from __future__ import annotations

from . import config, logs
from .llm_base import _http_post_json

log = logs.get("vtx.embeddings")

KIND_DOC = "doc"
KIND_QUERY = "query"

# Потолок текста на один вектор: у Yandex лимит 2048 токенов, у Gemini — 2048,
# у моделей Ollama обычно 512–8192; кусок расшифровки — 40 строк, то есть
# 2–4 тысячи символов, так что обрезка касается только простыней.
MAX_CHARS = 8_000
# Кусков в одной пачке при индексации (у Yandex и Gemini — один текст на
# запрос, пачка лишь ограничивает, сколько записей ждёт один UPDATE).
BATCH = 32
# Повторов на 429/503 внутри одного запроса. Меньше, чем у движков протокола:
# эмбеддинги считаются внутри конвейера задачи и при ответе на вопрос, и
# ждать по полминуты на каждом из тридцати кусков нельзя.
_RETRIES = 2

_DEFAULT_DIM = {"yandex": 256, "gemini": 768}
_DEFAULT_MODEL = {"gemini": "gemini-embedding-001", "ollama": "nomic-embed-text"}


def enabled() -> bool:
    return config.EMBED_PROVIDER in ("yandex", "gemini", "ollama")


def _clip(text: str) -> str:
    return (text or "").strip()[:MAX_CHARS]


class _Provider:
    """Общее: обход пустых текстов, обрезка, остановка после первой ошибки.

    `embed_texts` возвращает список ТОЙ ЖЕ длины, что `texts`: пустой текст и
    текст, на котором провайдер ответил ошибкой, дают `[]`. После первой
    ошибки остальные тексты пачки не отправляются (`[]`): мёртвый ключ или
    лежащий сервер не должны стоить тридцати повторов с ожиданием.
    """
    name = ""

    def __init__(self, model: str = "", dim: int = 0) -> None:
        self.model = model
        self._dim = int(dim or 0)

    def dim(self) -> int:
        """Размерность вектора; 0 — ещё не известна (Ollama до первого ответа)."""
        return self._dim

    def _embed_one(self, text: str, kind: str) -> list[float]:
        raise NotImplementedError

    def embed_texts(self, texts: list[str], kind: str = KIND_DOC) -> list[list[float]]:
        out: list[list[float]] = []
        failed = False
        for raw in texts:
            text = _clip(raw)
            if not text or failed:
                out.append([])
                continue
            try:
                vec = self._embed_one(text, kind)
            except Exception as e:  # noqa: BLE001 — любая ошибка провайдера = нет вектора
                log.warning("Эмбеддинг %s не посчитан: %s", self.name, e)
                failed = True
                out.append([])
                continue
            out.append(self._accept(vec))
        return out

    def _accept(self, vec) -> list[float]:
        """Проверка ответа: список чисел ожидаемой размерности. Первый ответ
        задаёт размерность, если она не была известна (Ollama)."""
        if not isinstance(vec, list) or not vec:
            log.warning("Эмбеддинг %s: пустой или неожиданный ответ", self.name)
            return []
        try:
            vec = [float(x) for x in vec]
        except (TypeError, ValueError):
            log.warning("Эмбеддинг %s: в ответе не числа", self.name)
            return []
        if not self._dim:
            self._dim = len(vec)
        elif len(vec) != self._dim:
            log.warning("Эмбеддинг %s: размерность %d вместо %d — вектор отброшен",
                        self.name, len(vec), self._dim)
            return []
        return vec


class YandexEmbeddings(_Provider):
    """Yandex Cloud Foundation Models: две модели — для документов и для
    запросов, 256 измерений. Один текст на запрос."""
    name = "yandex"

    def __init__(self, api_key: str, folder: str, dim: int = 0) -> None:
        super().__init__("", dim or _DEFAULT_DIM["yandex"])
        self.api_key = api_key
        self.folder = folder

    def _embed_one(self, text: str, kind: str) -> list[float]:
        model = "text-search-query" if kind == KIND_QUERY else "text-search-doc"
        out = _http_post_json(
            "https://llm.api.cloud.yandex.net/foundationModels/v1/textEmbedding",
            {"modelUri": f"emb://{self.folder}/{model}/latest", "text": text},
            # Ключ — заголовком, как у YandexProvider: в адрес он не попадает
            # и в текст ошибки не уезжает.
            {"Authorization": f"Api-Key {self.api_key}", "x-folder-id": self.folder},
            max_retries=_RETRIES)
        return (out or {}).get("embedding")


class GeminiEmbeddings(_Provider):
    """Google Gemini: `taskType` различает документ и запрос, размерность
    задаётся `outputDimensionality` (без неё модель отдаёт 3072)."""
    name = "gemini"

    def __init__(self, api_key: str, model: str = "", dim: int = 0) -> None:
        super().__init__(model or _DEFAULT_MODEL["gemini"], dim or _DEFAULT_DIM["gemini"])
        self.api_key = api_key

    def _embed_one(self, text: str, kind: str) -> list[float]:
        task = "RETRIEVAL_QUERY" if kind == KIND_QUERY else "RETRIEVAL_DOCUMENT"
        out = _http_post_json(
            f"https://generativelanguage.googleapis.com/v1beta/models/{self.model}:embedContent",
            {"content": {"parts": [{"text": text}]}, "taskType": task,
             "outputDimensionality": self._dim},
            {"x-goog-api-key": self.api_key},
            max_retries=_RETRIES)
        return ((out or {}).get("embedding") or {}).get("values")


class OllamaEmbeddings(_Provider):
    """Ollama `/api/embed`: принимает список текстов разом; документ и запрос
    у этих моделей не различаются. Размерность — из первого ответа, если
    `VTX_EMBED_DIM` не задан."""
    name = "ollama"

    def __init__(self, model: str = "", dim: int = 0, url: str = "") -> None:
        super().__init__(model or _DEFAULT_MODEL["ollama"], dim)
        self.url = (url or config.OLLAMA_URL).rstrip("/") + "/api/embed"

    def embed_texts(self, texts: list[str], kind: str = KIND_DOC) -> list[list[float]]:
        clipped = [_clip(t) for t in texts]
        send = [t for t in clipped if t]
        if not send:
            return [[] for _ in clipped]
        try:
            out = _http_post_json(self.url, {"model": self.model, "input": send}, {},
                                  max_retries=_RETRIES)
            vecs = (out or {}).get("embeddings")
            if not isinstance(vecs, list) or len(vecs) != len(send):
                raise RuntimeError(f"ожидалось {len(send)} векторов, в ответе "
                                   f"{len(vecs) if isinstance(vecs, list) else 'не список'}")
        except Exception as e:  # noqa: BLE001
            log.warning("Эмбеддинг ollama не посчитан: %s", e)
            return [[] for _ in clipped]
        it = iter(vecs)
        return [self._accept(next(it)) if t else [] for t in clipped]


# --------------------------------------------------------------------------- #
# Выбор провайдера и ключа
# --------------------------------------------------------------------------- #
def _first_cred(provider: str, team: str | None) -> tuple[str, str]:
    """(ключ, extra) — ключ команды из интерфейса, иначе из .env."""
    user_keys = None
    if team:
        try:
            from . import user_creds
            user_keys = user_creds.load(team)
        except Exception:  # noqa: BLE001 — нет ключей команды → ключи сервера
            log.warning("Ключи команды %s не прочитаны, беру .env", team, exc_info=True)
    creds = config.provider_creds(provider, user_keys)
    return creds[0] if creds else ("", "")


def provider_for(team: str | None = None) -> _Provider | None:
    """Провайдер по настройкам, None — выключено или не настроен (ключа нет).
    Отсутствие ключа — предупреждение, не ошибка: память встреч продолжает
    работать полнотекстом."""
    name = config.EMBED_PROVIDER
    if not enabled():
        return None
    model, dim = config.EMBED_MODEL, config.EMBED_DIM
    if name == "ollama":
        return OllamaEmbeddings(model, dim)
    key, extra = _first_cred(name, team)
    if name == "yandex":
        if not key or not extra:
            log.warning("VTX_EMBED_PROVIDER=yandex, но ключа и каталога Yandex нет "
                        "(ни у команды, ни в .env) — эмбеддинги не считаются")
            return None
        return YandexEmbeddings(key, extra, dim)
    if not key:
        log.warning("VTX_EMBED_PROVIDER=gemini, но ключа Gemini нет (ни у команды, "
                    "ни в .env) — эмбеддинги не считаются")
        return None
    return GeminiEmbeddings(key, model, dim)


def embed_texts(team: str | None, texts: list[str],
                kind: str = KIND_DOC) -> list[list[float]]:
    """Векторы текстов (список той же длины, `[]` — не посчитан). Никогда не
    поднимает исключение; выключено — все `[]`."""
    try:
        p = provider_for(team)
        if p is None:
            return [[] for _ in texts]
        return p.embed_texts(list(texts), kind)
    except Exception:  # noqa: BLE001 — страховка от неожиданного: наверх ничего
        log.warning("Эмбеддинги не посчитаны", exc_info=True)
        return [[] for _ in texts]


def embed_query(team: str | None, question: str) -> list[float] | None:
    """Вектор вопроса пользователя; None — выключено или не посчитался."""
    vecs = embed_texts(team, [question], KIND_QUERY)
    return vecs[0] if vecs and vecs[0] else None
