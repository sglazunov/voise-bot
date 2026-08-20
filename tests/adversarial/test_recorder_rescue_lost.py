"""Атака: сбой посреди встречи снова теряет уже записанный файл.

В `app/automation/recorder/__init__.py:113` локальная переменная
`log = on_log or (lambda *_: None)` ЗАТЕНЯЕТ модульный логгер `log` (строка 23).
А в обработчике сбоя (строки 258 и 274) код зовёт `log.warning(...)` — то есть
`.warning` у функции. Это `AttributeError`, и он вылетает ИЗНУТРИ `except`,
не давая дойти до спасения файла:

    except Exception as e:
        try:
            if rec: rec.stop()
        except Exception:
            log.warning(...)          # <-- AttributeError, выход из record_meeting
        try:
            done = Path(out_path)
            if done.exists() and done.stat().st_size > 0:
                return {"ok": True, ...}   # <-- сюда уже не попадаем

Это ровно тот сценарий, который закрывали пунктом K5 ревью («сбой посреди
встречи терял готовую запись»): `Scheduler._run` ловит исключение, ставит
карточке `error`, а состояние `error` не входит в `pending` — восстановление
после перезапуска такую встречу не подбирает. Час записи пропадает.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.automation import recorder
from app.automation.recorder import browser, capture


class _FakeBot:
    def __init__(self, cfg, on_log=None, display=None, sink=None):
        self._log = on_log or (lambda *_: None)

    def join(self, url, should_stop=None):
        return True

    def wait_until_end(self, *a, **kw):
        raise RuntimeError("вёрстка Телемоста поменялась посреди встречи")

    def screenshot(self, path):
        pass

    def close(self):
        pass


class _FakeRec:
    """ffmpeg, который писал исправно, но на аварийной остановке даёт сбой —
    обычное дело, когда процесс уже умер сам."""

    running = True

    def __init__(self, out_path, cfg, on_log=None, display=None, source=None):
        self.out = Path(out_path)

    def start(self):
        self.out.parent.mkdir(parents=True, exist_ok=True)
        self.out.write_bytes(b"\x00" * (5 * 1024 * 1024))   # «час записи»

    def stop(self):
        raise OSError("ffmpeg уже не отвечает")

    def error_tail(self):
        return ""


@pytest.fixture
def _armed(monkeypatch):
    monkeypatch.setattr(recorder, "_RECORDER_ENABLED", True)
    monkeypatch.setattr(browser, "TelemostBot", _FakeBot)
    monkeypatch.setattr(capture, "FFmpegRecorder", _FakeRec)
    monkeypatch.setattr(capture, "test_audio_level",
                        lambda *a, **kw: {"ok": True, "has_sound": True})
    monkeypatch.setattr(recorder.time, "sleep", lambda *_: None)


def test_finished_recording_survives_a_mid_meeting_failure(_armed, tmp_path):
    out = tmp_path / "17.09.2026, 10:00. - Планёрка.mp4"
    slot = recorder.Slot(0, ":99", "meet0", "meet0.monitor")

    res = recorder.record_meeting("https://telemost.yandex.ru/j/abc", str(out),
                                  {"max_meeting_min": 240}, on_log=lambda *_: None,
                                  slot=slot)

    assert out.exists() and out.stat().st_size > 0      # файл записан
    assert res.get("ok") is True, (
        f"записанный файл не подхвачен после сбоя: {res}")
    assert res.get("path") == str(out)


def test_record_meeting_never_raises_attributeerror(_armed, tmp_path):
    """Даже если спасать нечего, record_meeting обязан вернуть {ok: False,...},
    а не выбросить наружу исключение из собственного обработчика ошибок."""
    out = tmp_path / "empty.mp4"
    slot = recorder.Slot(0, ":99", "meet0", "meet0.monitor")
    try:
        res = recorder.record_meeting("https://telemost.yandex.ru/j/abc", str(out),
                                      {"max_meeting_min": 240},
                                      on_log=lambda *_: None, slot=slot)
    except AttributeError as e:
        pytest.fail(f"record_meeting уронил обработчик собственных ошибок: {e}")
    assert isinstance(res, dict)
