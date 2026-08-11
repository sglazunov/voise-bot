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
    return llm._nvidia_stream(f"{server}/{case}", {"model": "m"}, {}, **kw)


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
    assert llm._NVIDIA_STREAM is True
    # Пауза между кусками щедрая: на загруженном бесплатном тарифе они
    # приходят с задержками в десятки секунд.
    assert llm._NVIDIA_CHUNK_TIMEOUT >= 60
