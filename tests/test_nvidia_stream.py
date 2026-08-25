"""Потоковый режим NVIDIA — на живом локальном HTTP-сервере.

Зачем поток: обычным запросом длинная генерация не доживает до конца. На
часовой встрече 11.08 сервер NVIDIA сам ответил 504 «gateway timeout» — ждать
весь ответ целиком их шлюз не готов. В потоке куски идут сразу, соединение
живо, и ограничение по времени применяется к паузе между кусками.

Живой ключ для проверки не нужен: формат SSE стандартный, поэтому поднимаем
свой сервер и отдаём заранее записанные потоки, включая кривые.
"""
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from app import llm
from app import llm_nvidia

# Что отдаёт сервер: имя случая → (код, тело).
CASES = {
    "ok": (200,
           'data: {"choices":[{"delta":{"content":"Прото"}}]}\n\n'
           '\n'                                    # keep-alive
           'data: {"choices":[{"delta":{"content":"кол "}}]}\n\n'
           'data: {"choices":[{"delta":{"content":"готов"}}]}\n\n'
           'data: [DONE]\n\n'),
    # Кусок с битым JSON пропускаем, остальное собираем.
    "broken": (200,
               'data: {"choices":[{"delta":{"content":"нача"}}]}\n\n'
               'data: {"choices":[{"delta":{"conte\n\n'
               'data: {"choices":[{"delta":{"content":"ло"}}]}\n\n'
               'data: [DONE]\n\n'),
    # Оборвался без [DONE] — половина ответа, отдавать её наверх нельзя.
    "cut": (200,
            'data: {"choices":[{"delta":{"content":"половина"}}]}\n\n'),
    "gw": (504, '{"error":"gateway timeout"}'),
}


class _Handler(BaseHTTPRequestHandler):
    def do_POST(self):                       # noqa: N802
        code, body = CASES[self.path.strip("/")]
        raw = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type",
                         "text/event-stream" if code == 200 else "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *a):               # тишина в выводе тестов
        pass


@pytest.fixture(scope="module")
def server():
    srv = HTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()


def _stream(server, case, **kw):
    return llm_nvidia._nvidia_stream(f"{server}/{case}", {"model": "m"}, {}, **kw)


def test_обычный_поток_собирается(server):
    assert _stream(server, "ok") == "Протокол готов"


def test_битый_кусок_не_ломает_разбор(server):
    """Один нечитаемый кусок — не повод терять весь ответ."""
    assert _stream(server, "broken") == "начало"


def test_обрыв_без_done_это_ошибка(server):
    """Половина ответа выглядит как готовый протокол, только без разделов.
    Отдавать её наверх нельзя — пусть решает обычный запрос."""
    with pytest.raises(RuntimeError, match="оборван"):
        _stream(server, "cut")


def test_ошибка_шлюза_приходит_с_кодом(server):
    """504 сервер отдаёт обычным JSON, а не потоком. Парсер не должен пытаться
    читать тело ошибки как поток."""
    with pytest.raises(RuntimeError, match="504"):
        _stream(server, "gw")


def test_стоп_действует_внутри_потока(server):
    """Один вызов в потоке живёт минутами: «Стоп» обязан работать внутри него,
    иначе вернётся баг «нажал стоп — ничего не произошло»."""
    with pytest.raises(llm.GenerationCancelled):
        _stream(server, "ok", should_stop=lambda: True)


def test_поток_включён_по_умолчанию():
    assert llm_nvidia._NVIDIA_STREAM is True
    # Пауза между кусками щедрая: на загруженном бесплатном тарифе они
    # приходят с задержками в десятки секунд.
    assert llm_nvidia._NVIDIA_CHUNK_TIMEOUT >= 60


class TestОтменаСквозьОбёртки:
    """Отмена должна доходить до провайдера и через ротацию ключей. При ОДНОМ
    ключе обёртки нет, и тест провайдера этого не поймает — а при двух ключах
    цепочка видит уже обёртку, а не сам движок."""

    class _Fake:
        name = "nvidia"
        accepts_should_stop = True

        def __init__(self, model=None, api_key=None, extra=None):
            self.api_key = api_key

        def complete(self, prompt, max_tokens=2000, force_json=True,
                     should_stop=None):
            if should_stop and should_stop():
                raise llm.GenerationCancelled()
            return "ответ"

    def test_ротация_передаёт_отмену_внутрь(self):
        rot = llm._RotatingProvider(self._Fake, None,
                                    [("k1", ""), ("k2", "")])
        assert rot.accepts_should_stop is True
        assert rot.complete("тест") == "ответ"
        with pytest.raises(llm.GenerationCancelled):
            rot.complete("тест", should_stop=lambda: True)

    def test_цепочка_видит_признак_а_не_тип(self):
        """Проверка по isinstance пропускала обёртку ротации."""
        rot = llm._RotatingProvider(self._Fake, None, [("k1", ""), ("k2", "")])
        chain = llm._FallbackChain([rot])
        with pytest.raises(llm.GenerationCancelled):
            chain.complete("тест", should_stop=lambda: True)

    def test_движки_без_поддержки_не_получают_лишний_аргумент(self):
        """У Groq/Gemini такого параметра нет — передача сломала бы вызов."""
        class Plain:
            name = "groq"

            def __init__(self, *a, **k):
                pass

            def complete(self, prompt, max_tokens=2000, force_json=True):
                return "ok"

        rot = llm._RotatingProvider(Plain, None, [("k1", "")])
        assert rot.accepts_should_stop is False
        assert rot.complete("тест", should_stop=lambda: True) == "ok"


class TestПотокБезDONE:
    """Не всякий шлюз шлёт «[DONE]». Раньше такой поток считался оборванным, и
    ГОТОВЫЙ ответ выбрасывался ради обычного запроса — а тот на длинной
    генерации получает от NVIDIA 504. Мы просим JSON, и у него есть надёжный
    признак целости: он разбирается целиком."""

    def test_целый_json_принимается(self):
        assert llm_nvidia._looks_complete_json('{"topics": [{"topic": "a"}]}')

    def test_обрезанный_json_не_принимается(self):
        assert not llm_nvidia._looks_complete_json('{"topics": [{"topic": "a"')

    def test_просто_текст_не_принимается(self):
        assert not llm_nvidia._looks_complete_json("почти готовый протокол")

    def test_пусто_не_принимается(self):
        assert not llm_nvidia._looks_complete_json("")


def test_первый_кусок_ждём_дольше_остальных():
    """Модель сначала читает промпт целиком — на часовой встрече это десятки
    тысяч символов, и до первого куска проходит заметно больше времени, чем
    между кусками потом."""
    assert llm_nvidia._NVIDIA_FIRST_TIMEOUT > llm_nvidia._NVIDIA_CHUNK_TIMEOUT


class TestЗапаснаяМодель:
    """Бесплатный NIM грузит модели по-разному: замер на боевом ключе дал 12 с
    на шестнадцать токенов у deepseek-v4-flash против 0,6 с у nemotron. Занятая
    модель на длинной генерации отвечает 529 или молчит, и раньше протокол
    целиком уходил запасному ДВИЖКУ. Ключ при этом живой, соседние модели
    свободны — правильнее сменить модель, а не движок."""

    def _prov(self, monkeypatch, busy, log):
        """Модель подобрана АВТОМАТИЧЕСКИ — только такую и можно подменять;
        выбранную человеком не трогаем (см. отдельный тест ниже). Все варианты
        одного семейства: замену ищем среди родни."""
        monkeypatch.setattr(llm_nvidia, "nvidia_default_model",
                            lambda key=None: "deepseek-ai/deepseek-занята")
        p = llm_nvidia.NvidiaProvider(api_key="nvapi-x")
        monkeypatch.setattr(llm_nvidia, "nvidia_usable_models",
                            lambda key=None: ["deepseek-ai/deepseek-занята",
                                              "deepseek-ai/deepseek-раз",
                                              "deepseek-ai/deepseek-два"])

        def fake(model, prompt, max_tokens, force_json, should_stop):
            log.append(model)
            if model in busy:
                raise RuntimeError('HTTP 529: {"type":"Overloaded"}')
            return '{"ok": true}'

        monkeypatch.setattr(p, "_complete_one", fake)
        return p

    def test_занятая_модель_меняется_на_свободную(self, monkeypatch):
        log: list[str] = []
        p = self._prov(monkeypatch, {"deepseek-ai/deepseek-занята"}, log)
        assert p.complete("текст") == '{"ok": true}'
        assert log == ["deepseek-ai/deepseek-занята", "deepseek-ai/deepseek-раз"]

    def test_имя_использованной_модели_запоминается(self, monkeypatch):
        """Шапка протокола берёт имя модели у провайдера — называть занятую
        было бы неправдой."""
        p = self._prov(monkeypatch, {"deepseek-ai/deepseek-занята"}, [])
        p.complete("текст")
        assert p.model == "deepseek-ai/deepseek-раз"

    def test_если_заняты_все_ошибка_пробрасывается(self, monkeypatch):
        log: list[str] = []
        p = self._prov(monkeypatch, {"deepseek-ai/deepseek-занята",
                                     "deepseek-ai/deepseek-раз",
                                     "deepseek-ai/deepseek-два"}, log)
        with pytest.raises(RuntimeError):
            p.complete("текст")
        assert len(log) == 3, "должны быть перебраны все доступные модели"

    def test_обычная_ошибка_не_ведёт_к_смене_модели(self, monkeypatch):
        """Смена модели — ответ на перегрузку. Ошибка в самом запросе так не
        лечится, и молча уводить её в другую модель нельзя."""
        log: list[str] = []
        p = llm_nvidia.NvidiaProvider(model="модель", api_key="nvapi-x")
        monkeypatch.setattr(llm_nvidia, "nvidia_usable_models",
                            lambda key=None: ["модель", "другая"])

        def fake(model, *a, **k):
            log.append(model)
            raise RuntimeError("HTTP 400: bad request")

        monkeypatch.setattr(p, "_complete_one", fake)
        with pytest.raises(RuntimeError):
            p.complete("текст")
        assert log == ["модель"]


class TestСемействоМодели:
    """Движок выбирают за качество протокола. Значит и замену занятой модели
    надо искать сначала СРЕДИ РОДНИ: у DeepSeek в каталоге несколько вариантов,
    качество у них близкое, а очередь — разная. Менять deepseek на nemotron
    ради скорости — менять то, ради чего движок и выбрали."""

    def test_семейство_вычисляется(self):
        f = llm_nvidia._family
        assert f("deepseek-ai/deepseek-v4-flash-0731") == "deepseek"
        assert f("deepseek-ai/deepseek-r1") == "deepseek"
        assert f("nvidia/nemotron-3-ultra-550b-a55b") == "nemotron"
        assert f("openai/gpt-oss-20b") == "gpt"

    def test_чужое_семейство_не_берётся(self, monkeypatch):
        """Боевая проверка: подмена deepseek на nemotron дала протокол без единой
        цитаты-основания — ноль подтверждённых задач из девяти. Это хуже, чем
        уйти к запасному движку, у которого неподтверждённых около четверти."""
        monkeypatch.setattr(llm_nvidia, "nvidia_default_model",
                            lambda key=None: "deepseek-ai/deepseek-v4-flash-0731")
        p = llm_nvidia.NvidiaProvider(api_key="nvapi-x")
        monkeypatch.setattr(llm_nvidia, "nvidia_usable_models", lambda key=None: [
            "deepseek-ai/deepseek-v4-flash-0731",
            "nvidia/nemotron-3-ultra-550b-a55b",
            "openai/gpt-oss-20b",
        ])
        assert p._standby_models() == [], "чужие модели брать нельзя"

    def test_родня_пробуется_первой(self, monkeypatch):
        monkeypatch.setattr(llm_nvidia, "nvidia_default_model",
                            lambda key=None: "deepseek-ai/deepseek-v4-flash-0731")
        p = llm_nvidia.NvidiaProvider(api_key="nvapi-x")
        monkeypatch.setattr(llm_nvidia, "nvidia_usable_models", lambda key=None: [
            "nvidia/nemotron-3-ultra-550b-a55b",       # чужая, но первая в списке
            "deepseek-ai/deepseek-v4-flash-0731",      # текущая
            "deepseek-ai/deepseek-r1",                 # родня
        ])
        assert p._standby_models()[0] == "deepseek-ai/deepseek-r1"

    def test_ожидание_очереди_укладывается_в_бюджет(self):
        """Повторы должны успевать упереться в бюджет, а не заканчиваться
        раньше него: иначе заявленные пять минут ожидания были бы неправдой."""
        worst = llm_nvidia._STREAM_RETRIES * llm_nvidia._STREAM_RETRY_MAX_WAIT
        assert worst >= llm_nvidia._NVIDIA_WAIT_BUDGET


def test_выбранная_человеком_модель_не_подменяется(monkeypatch):
    """Движок выбирают за качество протокола. Боевая проверка показала цену
    подмены: nemotron вместо deepseek дал протокол без единой цитаты-основания —
    ноль подтверждённых задач из девяти. Поэтому явный выбор неприкосновенен:
    занята — ждём, не дождались — уходим к запасному ДВИЖКУ, но не к чужой
    модели."""
    monkeypatch.setattr(llm_nvidia, "nvidia_usable_models", lambda key=None: [
        "deepseek-ai/deepseek-v4-flash-0731", "deepseek-ai/deepseek-r1"])
    chosen = llm_nvidia.NvidiaProvider(model="deepseek-ai/deepseek-v4-flash-0731",
                                       api_key="nvapi-x")
    assert chosen._standby_models() == []

    monkeypatch.setattr(llm_nvidia, "nvidia_default_model",
                        lambda key=None: "deepseek-ai/deepseek-v4-flash-0731")
    auto = llm_nvidia.NvidiaProvider(api_key="nvapi-x")
    assert auto._standby_models() == ["deepseek-ai/deepseek-r1"]


class TestБюджетОжидания:
    """Ждать NVIDIA дольше минуты бессмысленно: бесплатный NIM либо отвечает
    быстро, либо занят всерьёз и за пять минут не освободится. Бюджет
    ограничивает ОЖИДАНИЕ — очередь на их стороне, — а не саму генерацию:
    начавшийся ответ обрывать незачем, длинный протокол законно идёт минутами."""

    def test_бюджет_около_пяти_минут(self):
        """Подождать очередь выбранной модели лучше, чем сразу отдать встречу
        запасной: движок выбирают за качество протокола."""
        assert 240 <= llm_nvidia._NVIDIA_WAIT_BUDGET <= 360

    def test_истёкший_бюджет_прекращает_ожидание(self):
        import time as _t
        with pytest.raises(RuntimeError, match="не ответила"):
            llm_nvidia._nvidia_stream("https://x/y", {"model": "m"}, {},
                                      deadline=_t.time() - 1)

    def test_повторы_и_паузы_укладываются_в_бюджет(self):
        """Повторов не должно быть столько, чтобы одни паузы съели бюджет."""
        worst = llm_nvidia._STREAM_RETRIES * llm_nvidia._STREAM_RETRY_MAX_WAIT
        assert worst >= llm_nvidia._NVIDIA_WAIT_BUDGET, (
            "повторы должны успевать упереться в бюджет, а не заканчиваться раньше")
