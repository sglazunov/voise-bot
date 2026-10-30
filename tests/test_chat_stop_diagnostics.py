"""30.10.2026: «стоп» видно, а бот сидит — и разбирать нечего: HTML чата
нигде не сохранялся. Теперь, если бот хоть раз видел стоп-слово, в конце
записи рядом с ней ложится HTML всех фреймов (`<запись>.chat.html`), а
уборка знает про этот файл.

⚠️ В именах файлов здесь нет двоеточия: на Windows (машина разработчика)
«11:00» превращается в альтернативный поток NTFS, и файла с таким именем нет."""
from pathlib import Path

import pytest

from app import jobs
from app.automation import recorder
from app.automation.recorder import browser, capture


class _Bot:
    made = []

    def __init__(self, cfg, on_log=None, display=None, sink=None):
        self._on_log = on_log or (lambda *_: None)
        self._chat_last_n = 0
        _Bot.made.append(self)

    def join(self, url, should_stop=None): return True
    def wait_until_end(self, *a, **kw):
        self._chat_last_n = 3            # видел три «стоп» в истории
        return "max_duration"
    def dump_html(self, path): self.dumped = path
    def leave_call(self, *a, **kw): self.left = True
    def screenshot(self, path): pass
    def close(self): pass


class _Rec:
    running = True
    def __init__(self, out_path, cfg, on_log=None, display=None, source=None):
        self.out = Path(out_path)
    def start(self):
        self.out.parent.mkdir(parents=True, exist_ok=True)
        self.out.write_bytes(b"\x00" * 1024)
    def stop(self): pass
    def error_tail(self): return ""


@pytest.fixture
def _armed(monkeypatch):
    monkeypatch.setattr(recorder, "_RECORDER_ENABLED", True)
    monkeypatch.setattr(browser, "TelemostBot", _Bot)
    monkeypatch.setattr(capture, "FFmpegRecorder", _Rec)
    monkeypatch.setattr(capture, "test_audio_level",
                        lambda *a, **kw: {"ok": True, "has_sound": True})
    monkeypatch.setattr(recorder.time, "sleep", lambda *_: None)
    _Bot.made.clear()


def test_html_чата_сохраняется_когда_стоп_видели(_armed, tmp_path):
    out = tmp_path / "30.10.2026, 11-00. - Планёрка.mp4"
    slot = recorder.Slot(0, ":99", "meet0", "meet0.monitor")
    res = recorder.record_meeting("https://telemost.yandex.ru/j/abc", str(out),
                                  {"chat_stop_word": "стоп", "auth_mode": "profile"},
                                  on_log=lambda *_: None, slot=slot)
    assert res["ok"] is True
    bot = _Bot.made[-1]
    assert getattr(bot, "dumped", None) == str(out.with_suffix(".chat.html"))
    assert bot.left is True


def test_html_чата_не_пишется_без_стоп_слова(_armed, tmp_path):
    out = tmp_path / "x.mp4"
    slot = recorder.Slot(0, ":99", "meet0", "meet0.monitor")
    recorder.record_meeting("https://telemost.yandex.ru/j/abc", str(out),
                            {"chat_stop_word": "", "auth_mode": "profile"},
                            on_log=lambda *_: None, slot=slot)
    assert not hasattr(_Bot.made[-1], "dumped")


def test_дамп_чата_в_списке_побочных_файлов(tmp_path):
    rec = tmp_path / "30.10.2026, 11-00. - Планёрка.mp4"
    for name in ("30.10.2026, 11-00. - Планёрка.chat.html",
                 "30.10.2026, 11-00. - Планёрка.chat.frame1.html"):
        (tmp_path / name).write_text("x")
    names = {p.name for p in jobs.media_sidecars(str(rec))}
    assert "30.10.2026, 11-00. - Планёрка.chat.html" in names
    assert "30.10.2026, 11-00. - Планёрка.chat.frame1.html" in names
