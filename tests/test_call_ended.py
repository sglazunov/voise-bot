"""Завершённая организатором встреча останавливает запись сразу.

Боевой случай: организатор завершил встречу для всех, все вышли — а бот
продолжал писать. Пустой экран попадал в файл, потом ещё и распознавался.
Косвенная улика: фраза «Групповой звонок завершился» оказалась в списке
участников протокола — значит провисела в кадре достаточно долго, чтобы её
успел прочитать OCR.

Причина: выход из цикла был завязан на ИСЧЕЗНОВЕНИЕ кнопок управления
(is_in_call), а на экране завершения часть из них остаётся — is_in_call
продолжал отвечать True. Поэтому экран завершения распознаётся отдельно, по
тексту, и проверяется ПЕРВЫМ.
"""
from app.automation.recorder.browser import TelemostBot


class _Bot(TelemostBot):
    """Бот без браузера: сценарий страницы задаётся флагами."""

    class _Page:
        """Страница-заглушка: цикл ждёт таймауты через неё."""
        def wait_for_timeout(self, _ms):
            pass

    def __init__(self, ended=False, in_call=True, participants=2):
        self._on_log = lambda *_: None
        self.cfg = {}
        self._page = self._Page()
        self._ended, self._in_call, self._n = ended, in_call, participants

    # Подменяем только чтение страницы — логика цикла остаётся настоящей.
    def call_ended(self):
        return self._ended

    def is_in_call(self):
        return self._in_call

    def participant_count(self):
        return self._n

    def alone_screen(self):
        return False

    def maybe_chat_stop(self, word):
        return False

    def _mute_self(self):
        pass


def test_завершение_встречи_останавливает_запись():
    bot = _Bot(ended=True)
    assert bot.wait_until_end(None, max_sec=9999, alone_sec=90) == "call_ended"


def test_завершение_важнее_чем_наличие_кнопок():
    """Главное: на экране завершения кнопки ещё видны, и раньше это удерживало
    бота в цикле. Теперь текст экрана перевешивает."""
    bot = _Bot(ended=True, in_call=True, participants=5)
    assert bot.wait_until_end(None, max_sec=9999, alone_sec=90) == "call_ended"


def test_идущая_встреча_не_прерывается():
    bot = _Bot(ended=False, in_call=True, participants=3)
    # Максимум 0 секунд — цикл обязан выйти по времени, а не по «завершено».
    assert bot.wait_until_end(None, max_sec=0, alone_sec=90) == "max_duration"


def test_ручная_остановка_по_прежнему_работает():
    bot = _Bot(ended=False)
    assert bot.wait_until_end(lambda: True, max_sec=9999, alone_sec=90) == "stopped"


def test_чат_открывается_повторно_если_кнопки_не_было(monkeypatch):
    """Ревью 30.10: кнопка «Чат» не нашлась в первые секунды — и стоп-слово
    не работало всю встречу. Теперь бот пробует открыть чат снова, пока не
    откроет (раз в ~30 с), а открытый не трогает."""
    import app.automation.recorder.browser as br
    clock = [1000.0]
    monkeypatch.setattr(br.time, "time", lambda: clock[0])
    monkeypatch.setattr(br.time, "sleep", lambda s: clock.__setitem__(0, clock[0] + s))

    class B(_Bot):
        tries = 0

        def open_chat(self, quiet=False):
            B.tries += 1
            if B.tries >= 3:
                self._chat_opened_once = True

        def maybe_chat_stop(self, word):
            return bool(getattr(self, "_chat_opened_once", False))

    b = B()
    assert b.wait_until_end(None, max_sec=9999, alone_sec=90,
                            chat_stop_word="стоп") == "chat_stop"
    assert B.tries == 3
    assert clock[0] - 1000.0 < 120, "повтор не реже раза в 30 с"


def test_открытый_чат_не_переоткрывается(monkeypatch):
    import app.automation.recorder.browser as br
    clock = [1000.0]
    monkeypatch.setattr(br.time, "time", lambda: clock[0])
    monkeypatch.setattr(br.time, "sleep", lambda s: clock.__setitem__(0, clock[0] + s))

    class B(_Bot):
        tries = 0

        def open_chat(self, quiet=False):
            B.tries += 1
            self._chat_opened_once = True

    b = B()
    assert b.wait_until_end(None, max_sec=200, alone_sec=90,
                            chat_stop_word="стоп") == "max_duration"
    assert B.tries == 1
