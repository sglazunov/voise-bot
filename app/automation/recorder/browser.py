"""Telemost join via Playwright/Chromium — guest and authenticated modes.

Two ways in (settings `auth_mode`):
  * "guest"   — open the link, type a display name, join without logging in.
  * "profile" — reuse a persistent browser profile that's already logged into a
                Yandex account (run `login()` once to sign in interactively).

Telemost's DOM is not a stable public contract, so selectors are best-effort
lists with fallbacks plus screenshots/logging for tuning on the real site.
Playwright is imported lazily so the rest of the app runs without it.
"""
from __future__ import annotations

import os
import re
import shutil
import sys
import tempfile
import time
import wave
from pathlib import Path

from ... import config

# Max letter-word tokens on a chat line for it to count as a deliberate stop
# COMMAND, not a sentence that merely mentions the word. Override via env.
BROWSER_STOP_MAX_TOKENS = int(os.getenv("VTX_CHAT_STOP_MAX_TOKENS", "6"))

# Candidate selectors (first match wins). Tune against the live site if needed.
_NAME_INPUTS = [
    'input[name="name"]', 'input[placeholder*="мя"]',
    'input[placeholder*="name" i]', 'input[type="text"]',
]
# Telemost first shows an interstitial ("Вы подключаетесь… → Продолжить в
# браузере") before the pre-join screen. We must click through it.
_CONTINUE_BROWSER = [
    'button:has-text("Продолжить в браузере")',
    'a:has-text("Продолжить в браузере")',
    'button:has-text("Continue in browser")',
    'a:has-text("Continue in browser")',
    'button:has-text("Продолжить")', 'a:has-text("Продолжить")',
]
_JOIN_BUTTONS = [
    'button:has-text("Подключиться")', 'button:has-text("Войти")',
    'button:has-text("Присоединиться")', 'button:has-text("Join")',
    'button:has-text("Продолжить")',
    '[data-testid*="join"]', 'button[type="submit"]',
]
_MUTE_MIC = [
    'button[aria-label*="икрофон"]', 'button[aria-label*="mic" i]',
    '[data-testid*="microphone"]',
]
_MUTE_CAM = [
    'button[aria-label*="амер"]', 'button[aria-label*="camera" i]',
    '[data-testid*="camera"]',
]
# Buttons whose label means the device is currently ON → clicking turns it OFF.
# (We only click these, so we never accidentally UN-mute an already-muted bot.)
_MIC_IS_ON = [
    'button[aria-label*="ыключить микрофон"]',   # "Выключить микрофон"
    'button[aria-label*="ыключить звук"]', 'button[aria-label*="mute" i]',
]
_CAM_IS_ON = [
    'button[aria-label*="ыключить камер"]', 'button[aria-label*="ыключить видео"]',
    'button[aria-label*="stop video" i]', 'button[aria-label*="turn off camera" i]',
]
# Controls that exist only while in the call (any one present = in call).
_IN_CALL = [
    'button:has-text("Участники")', 'button:has-text("Демонстрация")',
    'button:has-text("Чат")', 'button:has-text("Показать всех")',
    'button[aria-label*="частник"]', 'button[aria-label*="емонстрац"]',
    'button[aria-label*="авершить"]', 'button[aria-label*="окинуть"]',
    'button[aria-label*="ыйти"]', 'button[aria-label*="leave" i]',
    'button[aria-label*="hang" i]', '[data-testid*="hangup"]',
    'button:has-text("Завершить")', 'button:has-text("Покинуть")',
]
# --- Telemost native recording controls -------------------------------------
# Confirmed from the live UI: the bottom "•••" (More) button opens a menu whose
# first item is «Записать на компьютер»; while recording it becomes «Остановить
# запись».
_REC_MORE = [  # the bottom-bar "•••" (More) button that holds the record item
    'button[aria-label="Ещё"]', 'button[aria-label*="Ещё"]',
    'button[aria-label*="ещё"]', 'button[aria-label*="Дополнит"]',
    'button[aria-label*="More" i]', 'button[aria-label*="menu" i]',
    'button[aria-haspopup="menu"]', '[data-testid*="more"]',
    '[data-testid*="menu-button"]', 'button:has-text("•••")', 'button:has-text("…")',
]
_REC_START = [  # the «Записать на компьютер» menu item
    '[role="menuitem"]:has-text("Записать на компьютер")',
    'text="Записать на компьютер"', 'text=Записать на компьютер',
    'button:has-text("Записать на компьютер")',
    'text=Запись на компьютер', 'text=Сохранить на компьютер',
]
_REC_CONFIRM = [  # an optional confirmation dialog
    'button:has-text("Начать запись")', 'button:has-text("Записать")',
    'button:has-text("Начать")', 'button:has-text("Продолжить")',
    'button:has-text("Понятно")',
]
_REC_STOP = [
    '[role="menuitem"]:has-text("Остановить запись")',
    'text="Остановить запись"', 'text=Остановить запись',
    'button:has-text("Остановить запись")', 'text=Завершить запись',
]
# Launch args: auto-accept mic/cam prompts; fake mic so we never send real audio;
# suppress the noisy first-run/default-browser/translate popups that otherwise
# show up in the recording.
_LAUNCH_ARGS = [
    "--use-fake-ui-for-media-stream",
    "--use-fake-device-for-media-stream",
    "--autoplay-policy=no-user-gesture-required",
    "--disable-blink-features=AutomationControlled",
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-features=Translate,TranslateUI",
]
# In a Linux container Chromium must run without the sandbox (esp. as root) and
# not rely on the tiny default /dev/shm. Audio just follows the default Pulse
# sink ("meet"), so no extra flag is needed for capture.
if sys.platform.startswith("linux"):
    _LAUNCH_ARGS += ["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"]


def _screen_wh() -> tuple[int, int]:
    """Xvfb display size (W, H) from VTX_SCREEN_RES, default 1920x1080."""
    res = os.environ.get("VTX_SCREEN_RES", "1920x1080x24").split("x")
    try:
        return int(res[0]), int(res[1])
    except (ValueError, IndexError):
        return 1920, 1080


def _silent_wav() -> str:
    """Path to a 1-second silent WAV, created once, for the fake mic input."""
    path = os.path.join(tempfile.gettempdir(), "vtx-silence.wav")
    if not os.path.exists(path):
        try:
            with wave.open(path, "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(16000)
                w.writeframes(b"\x00\x00" * 16000)  # 1 s of silence
        except OSError:
            pass
    return path


def playwright_available() -> bool:
    try:
        import playwright.sync_api  # noqa: F401
        return True
    except Exception:
        return False


def readiness(cfg: dict) -> dict:
    if not playwright_available():
        return {"ready": False,
                "detail": "Не установлен Playwright. Выполните: pip install "
                          "playwright  &&  playwright install chromium"}
    mode = cfg.get("auth_mode") or "guest"
    if mode == "profile":
        prof = _profile_dir(cfg)
        if not any(prof.iterdir()) if prof.exists() else True:
            return {"ready": False, "mode": mode,
                    "detail": "Режим 'profile': профиль пуст — войдите один раз "
                              "через кнопку «Войти в Яндекс»."}
        return {"ready": True, "mode": mode, "detail": f"Профиль: {prof}"}
    return {"ready": True, "mode": mode, "detail": "Режим: гость (по ссылке)."}


def login_status(cfg: dict) -> dict:
    """Check whether the recorder profile is actually logged into Yandex.

    Loads passport.yandex.ru/profile in a headless copy of the profile: if it
    stays on /profile the session is valid; if it redirects to /auth it isn't."""
    if (cfg.get("auth_mode") or "guest") != "profile":
        return {"logged_in": None,
                "detail": "Режим входа — «Гость»: вход в Яндекс не используется. "
                          "Для записи Телемоста переключите на «Авторизованный»."}
    if not playwright_available():
        return {"logged_in": None, "detail": "Playwright не установлен."}
    try:
        from playwright.sync_api import sync_playwright
        user_dir = str(_profile_dir(cfg))
        with sync_playwright() as pw:
            ctx = pw.chromium.launch_persistent_context(
                user_dir, headless=True, args=_LAUNCH_ARGS)
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.goto("https://passport.yandex.ru/profile",
                      wait_until="domcontentloaded", timeout=20000)
            page.wait_for_timeout(2500)
            url = page.url
            try:
                ctx.close()
            except Exception:
                pass
        logged = "/auth" not in url
        return {"logged_in": logged,
                "detail": ("Вход в Яндекс выполнен ✓ — бот будет писать как этот аккаунт."
                           if logged else
                           "Не вошли в Яндекс. Нажмите «Войти в Яндекс» и авторизуйтесь "
                           "аккаунтом, создавшим встречу.")}
    except Exception as e:  # noqa: BLE001
        return {"logged_in": None, "detail": f"Не удалось проверить вход: {e}"}


def _profile_dir(cfg: dict) -> Path:
    raw = (cfg.get("browser_profile_dir") or "").strip()
    base = Path(raw) if raw else (config.DATA_DIR / "browser-profile")
    base.mkdir(parents=True, exist_ok=True)
    return base


class TelemostBot:
    """Drive a Chromium instance into a Telemost call and back out."""

    def __init__(self, cfg: dict, on_log=None, display=None, sink=None):
        self.cfg = cfg
        self._on_log = on_log or (lambda *_: None)
        self._display = display   # per-slot Xvfb display (e.g. ":99")
        self._sink = sink         # per-slot PulseAudio sink (e.g. "meet0")
        self._pw = None
        self._ctx = None
        self._page = None
        self._temp_profile = None
        # Chat stop-word bookkeeping (see maybe_chat_stop).
        self._chat_baseline = None    # stop-word lines present at first read
        self._chat_last_peek = 0.0    # last time we read the chat
        self._chat_last_n = -1        # last logged count (for live diagnostics)
        # Сами строки со стоп-словом, уже виденные. Считать их КОЛИЧЕСТВО
        # оказалось ненадёжно: чат в комнате общий и не чистится, а Телемост
        # держит в DOM только видимую часть — при прокрутке старое сообщение
        # уходит, новое приходит, и счётчик не растёт. Строка сообщения включает
        # время («стоп 16:49»), поэтому новое сообщение отличимо от старого.
        self._chat_seen: set[str] = set()
        self._chat_opened_once = False

    # -- lifecycle ----------------------------------------------------------
    def _launch(self, headless: bool | None = None):
        from playwright.sync_api import sync_playwright
        self._pw = sync_playwright().start()
        # Screen capture needs a real on-screen window for ffmpeg to grab, so the
        # recording browser is never headless (the explicit param still wins, e.g.
        # the login helper).
        headless = False if headless is None else headless
        mode = self.cfg.get("auth_mode") or "guest"
        use_profile = (mode == "profile")
        # Guest mode keeps no state, so use a FRESH temp profile per run. A fixed
        # dir gets a Chromium "singleton" lock: if a previous bot window is still
        # open (e.g. a meeting that didn't end cleanly), Chrome hands off to it and
        # the new process exits → "Target page/browser has been closed". A unique
        # dir sidesteps that entirely. The authenticated profile must persist, so
        # there we just clear any stale lock left by a crashed run.
        if use_profile:
            user_dir = str(_profile_dir(self.cfg))
            Path(user_dir).mkdir(parents=True, exist_ok=True)
            for lock in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
                try:
                    (Path(user_dir) / lock).unlink()
                except OSError:
                    pass
        else:
            user_dir = tempfile.mkdtemp(prefix="tm-guest-")
            self._temp_profile = user_dir
        # For recording, open a real maximised window (full screen width) so the
        # capture is as large as possible; headless contexts keep a fixed size.
        args = list(_LAUNCH_ARGS)
        # Feed the fake mic a SILENT file: Chromium's default fake audio is a
        # beep, so if the mic ever gets re-enabled (Telemost re-renders after a
        # while) the bot would transmit that beep. Silence guarantees it can't.
        args.append(f"--use-file-for-fake-audio-capture={_silent_wav()}")
        if not headless:
            args.append("--window-position=0,0")
            if sys.platform.startswith("linux"):
                # No window manager under Xvfb, so --start-maximized / the CDP
                # maximise don't fill the screen. Size the window to the whole
                # display and go fullscreen (also hides the toolbar/tabs from
                # the recording).
                w, h = _screen_wh()
                args += [f"--window-size={w},{h}", "--start-fullscreen"]
            else:
                args.append("--start-maximized")
        # Pin the browser process to this slot's display + audio sink, so its
        # window renders on the slot's Xvfb and its sound plays into the slot's
        # PulseAudio sink (which ffmpeg records) — full isolation between parallel
        # recordings. PULSE_SINK routes all of Chromium's playback to that sink.
        launch_env = None
        if self._display or self._sink:
            launch_env = dict(os.environ)
            if self._display:
                launch_env["DISPLAY"] = self._display
            if self._sink:
                launch_env["PULSE_SINK"] = self._sink
        self._ctx = self._pw.chromium.launch_persistent_context(
            user_dir, headless=headless, args=args, env=launch_env,
            permissions=["microphone", "camera"],
            accept_downloads=True,   # Telemost "Запись на компьютер" → a download
            no_viewport=not headless,  # use the actual window size when headed
            viewport=None if not headless else {"width": 1280, "height": 720})
        self._page = self._ctx.pages[0] if self._ctx.pages else self._ctx.new_page()
        self._download_path = None
        self._ctx.on("download", self._on_download)
        # CDP maximise fills the Xvfb screen. We
        # already forced --window-size + --start-fullscreen (no WM to maximise).
        if not headless and not sys.platform.startswith("linux"):
            self._maximize_window()

    def _maximize_window(self) -> None:
        """Maximise to full screen width via CDP (reliable across DPI, unlike
        --start-maximized which Playwright often overrides)."""
        try:
            cdp = self._ctx.new_cdp_session(self._page)
            win = cdp.send("Browser.getWindowForTarget")
            cdp.send("Browser.setWindowBounds", {
                "windowId": win["windowId"],
                "bounds": {"windowState": "maximized"}})
        except Exception as e:  # noqa: BLE001
            self._on_log(f"Не удалось развернуть окно: {e}")

    # -- Telemost native recording -----------------------------------------
    def _on_download(self, dl) -> None:
        """Capture the file Telemost produces when recording stops."""
        try:
            suggested = dl.suggested_filename or "recording.webm"
            ext = Path(suggested).suffix or ".webm"
            target = str(Path(self._out_path).with_suffix(ext)) if getattr(
                self, "_out_path", None) else suggested
            dl.save_as(target)
            self._download_path = target
            self._on_log(f"Файл записи получен от Телемоста: {target}")
        except Exception as e:  # noqa: BLE001
            self._on_log(f"Не удалось сохранить запись: {e}")

    def _aborted(self) -> bool:
        sc = getattr(self, "_should_stop", None)
        try:
            return bool(sc and sc())
        except Exception:
            return False

    def _click_any(self, selectors, overall_ms=12000, poll_ms=500) -> bool:
        """Poll ALL selectors repeatedly until one is clickable or we time out.

        Much better than waiting `timeout` on each selector in turn (that could
        block for selectors×timeout — minutes — when the page hasn't loaded the
        expected control yet). Bails early if the user pressed «Остановить»."""
        deadline = time.time() + overall_ms / 1000
        while time.time() < deadline and not self._aborted():
            for sel in selectors:
                try:
                    el = self._page.query_selector(sel)
                    if el and el.is_visible():
                        el.click()
                        return True
                except Exception:
                    pass
            self._page.wait_for_timeout(poll_ms)
        return False

    def _fill_any(self, selectors, value, overall_ms=6000) -> bool:
        deadline = time.time() + overall_ms / 1000
        while time.time() < deadline:
            for sel in selectors:
                try:
                    el = self._page.query_selector(sel)
                    if el and el.is_visible():
                        el.fill(value)
                        return True
                except Exception:
                    pass
            self._page.wait_for_timeout(400)
        return False

    # -- joining ------------------------------------------------------------
    def join(self, url: str, should_stop=None) -> bool:
        """Open the meeting and get into the call. Returns True on success."""
        self._should_stop = should_stop
        join_budget = int(self.cfg.get("join_timeout_sec", 60))
        self._launch()
        self._on_log(f"Открываю встречу: {url}")
        self._page.goto(url, wait_until="domcontentloaded", timeout=60000)
        self._page.wait_for_timeout(3000)

        # 1) Interstitial: "Продолжить в браузере".
        if self._click_any(_CONTINUE_BROWSER, overall_ms=10000):
            self._on_log("Прошёл заглушку «Продолжить в браузере».")
            self._page.wait_for_timeout(4000)
        else:
            self._on_log("Заглушки «Продолжить в браузере» не было (или уже пройдена).")

        # 2) Display name on the guest pre-join form (if asked).
        name = self.cfg.get("bot_join_name") or "Протокол-бот"
        if self._fill_any(_NAME_INPUTS, name, overall_ms=8000):
            self._on_log(f"Указал имя: {name}")

        # 3) Mute mic & camera before joining (best effort).
        self._click_any(_MUTE_MIC, overall_ms=2500)
        self._click_any(_MUTE_CAM, overall_ms=2500)

        # 4) Join the call (poll up to the configured budget).
        joined = self._click_any(_JOIN_BUTTONS, overall_ms=join_budget * 1000)
        self._on_log("Нажал кнопку входа, подключаюсь…" if joined
                     else "Кнопку входа не нашёл — возможно, уже в звонке.")
        self._page.wait_for_timeout(6000)
        # Make sure the bot is muted in the call (no sound goes OUT from it).
        self.ensure_muted()
        in_call = self.is_in_call()
        self._on_log("Бот в звонке ✓" if in_call
                     else "Не вижу элементов звонка — проверяю ещё раз…")
        return in_call or joined

    def window_title(self) -> str | None:
        """The browser window's title — used by ffmpeg to grab just this window.

        Brings the window to the foreground first so x11grab captures it cleanly
        (a fully occluded window can grab black)."""
        try:
            self._page.bring_to_front()
        except Exception:
            pass
        try:
            t = (self._page.title() or "").strip()
            return t or None
        except Exception:
            return None

    def ensure_muted(self) -> None:
        """Turn the bot's mic and camera OFF (only if currently ON, so we never
        un-mute). Prevents the bot from sending any audio/video into the call."""
        for sels, what in ((_MIC_IS_ON, "микрофон"), (_CAM_IS_ON, "камеру")):
            for sel in sels:
                try:
                    el = self._page.query_selector(sel)
                    if el and el.is_visible():
                        el.click()
                        self._on_log(f"Выключил {what} бота.")
                        break
                except Exception:
                    pass

    def is_in_call(self) -> bool:
        for sel in _IN_CALL:
            try:
                if self._page.query_selector(sel):
                    return True
            except Exception:
                continue
        return False

    def _open_more_and_click(self, item_selectors, overall_ms: int = 12000) -> bool:
        """Open the bottom «•••» menu and click one of `item_selectors`.

        Polls so it works whether the menu is already open or needs opening, and
        whether the «•••» button has an aria-label we recognise."""
        deadline = time.time() + overall_ms / 1000
        while time.time() < deadline and not self._aborted():
            # menu already open?
            for sel in item_selectors:
                try:
                    el = self._page.query_selector(sel)
                    if el and el.is_visible():
                        el.click()
                        return True
                except Exception:
                    pass
            # open a "•••" candidate, then look for the item
            for msel in _REC_MORE:
                try:
                    mb = self._page.query_selector(msel)
                    if mb and mb.is_visible():
                        mb.click()
                        self._page.wait_for_timeout(600)
                        for sel in item_selectors:
                            it = self._page.query_selector(sel)
                            if it and it.is_visible():
                                it.click()
                                return True
                        # close the menu (Esc) so the next candidate is clean
                        try:
                            self._page.keyboard.press("Escape")
                        except Exception:
                            pass
                except Exception:
                    pass
            self._page.wait_for_timeout(500)
        return False

    def start_recording(self, out_path: str) -> bool:
        """Open «•••» → «Записать на компьютер». Returns True if it started."""
        self._out_path = out_path
        self._download_path = None
        started = self._open_more_and_click(_REC_START, overall_ms=15000)
        if started:
            self._page.wait_for_timeout(1200)
            self._click_any(_REC_CONFIRM, overall_ms=2500)  # optional dialog
            self._on_log("Запись Телемоста запущена.")
        else:
            self._on_log("Не нашёл пункт «Записать на компьютер».")
        return started

    def stop_recording(self) -> None:
        """Open «•••» → «Остановить запись» so Telemost finalises & saves."""
        try:
            stopped = self._open_more_and_click(_REC_STOP, overall_ms=8000)
            self._click_any(_REC_CONFIRM, overall_ms=2000)  # confirm "завершить"
            self._on_log(f"Остановка записи Телемоста: {stopped}")
        except Exception as e:  # noqa: BLE001
            self._on_log(f"Стоп записи: {e}")

    def wait_for_download(self, timeout: int = 240) -> str | None:
        """Wait until Telemost's recording file has been saved locally."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._download_path:
                return self._download_path
            self._page.wait_for_timeout(1000)
        return self._download_path

    def participant_count(self) -> int | None:
        """Best-effort count of participants (None if it can't be read).

        Telemost shows the count on the bottom «Участники» button (e.g. the badge
        reads "1" when only the bot is in the room). We read that number from the
        button's text / aria-label rather than counting DOM tiles, which the old
        selectors never matched."""
        for sel in ('button:has-text("Участники")', 'button:has-text("Participants")',
                    'button[aria-label*="частник"]', 'button[aria-label*="articipant" i]'):
            try:
                el = self._page.query_selector(sel)
                if not el:
                    continue
                txt = ((el.inner_text() or "") + " "
                       + (el.get_attribute("aria-label") or ""))
                m = re.search(r"\d+", txt)
                if m:
                    return int(m.group())
            except Exception:
                continue
        return None

    # When the bot is the only one in the room, Telemost replaces the participant
    # tiles with an invite prompt ("отправьте им ссылку…"). A reliable "alone"
    # signal that doesn't depend on reading a number.
    _ALONE_HINTS = (
        'text=пригласить других участников', 'text=отправьте им ссылку',
        'text=invite', 'text=Share the link',
    )

    def alone_screen(self) -> bool:
        for sel in self._ALONE_HINTS:
            try:
                if self._page.query_selector(sel):
                    return True
            except Exception:
                continue
        return False

    # Экран, который Телемост показывает, когда организатор завершил встречу для
    # всех. Ловим его ПО ТЕКСТУ, а не по исчезновению кнопок: часть управления на
    # нём остаётся, is_in_call продолжает возвращать True, и бот писал пустой
    # экран дальше. На боевой встрече так и вышло — фраза «Групповой звонок
    # завершился» даже попала в список участников, потому что провисела в кадре
    # достаточно долго, чтобы её распознал OCR.
    _ENDED_HINTS = (
        'text=Групповой звонок завершился', 'text=звонок завершился',
        'text=Звонок завершён', 'text=Звонок завершен',
        'text=Встреча завершена', 'text=Конференция завершена',
        'text=The call has ended', 'text=Call ended',
    )

    def call_ended(self) -> bool:
        """Организатор завершил встречу для всех — писать больше нечего."""
        for sel in self._ENDED_HINTS:
            try:
                if self._page.query_selector(sel):
                    return True
            except Exception:
                continue
        return False

    def screenshot(self, path: str) -> None:
        try:
            self._page.screenshot(path=path, full_page=False)
        except Exception as e:
            self._on_log(f"Скриншот не удался: {e}")

    # ---- meeting chat: the stop word --------------------------------------
    # Participants can end the recording from INSIDE the call: write the stop
    # word (e.g. «стоп») as a message in the Telemost chat — the bot stops and
    # leaves. That's how a few people can stay behind for a private talk.
    # The toolbar button is literally labelled «Чат» (see the real Telemost UI);
    # the opened panel sits on the right with a «Сообщение…» input and a ✕.
    _CHAT_BTN = ('button:has-text("Чат")', 'button:has-text("Chat")',
                 'button[aria-label*="чат" i]', 'button[aria-label*="chat" i]')
    # Signs the panel is open. CRUCIAL: the bot joins as a GUEST, and a guest's
    # chat has NO message input — only a «Войдите, чтобы написать сообщение»
    # bar. Missing that made _chat_open() always False, so the bot re-clicked
    # «Чат» every few seconds, toggling the panel open/closed and reading it
    # half the time closed — the stop word never fired.
    _CHAT_OPEN_HINTS = ('input[placeholder*="Сообщение" i]',
                        'textarea[placeholder*="Сообщение" i]',
                        'input[placeholder*="Message" i]',
                        'text=Войдите, чтобы написать',
                        'text=Sign in to write')

    def _chat_open(self) -> bool:
        """Is the chat panel open (works for both guest and signed-in modes)?"""
        for sel in self._CHAT_OPEN_HINTS:
            try:
                el = self._page.query_selector(sel)
                if el and el.is_visible():
                    return True
            except Exception:
                continue
        return False

    def open_chat(self) -> None:
        """Open the chat panel ONCE at the start of recording and NEVER touch
        the «Чат» button again — it's a toggle, so re-clicking it would close a
        panel that's already open (that's why the chat kept closing after a
        minute). If it's already open we don't click at all."""
        try:
            if self._chat_open():
                self._chat_opened_once = True
                self._on_log("Чат уже открыт — слежу за стоп-словом.")
                return
            self._page.mouse.move(500, 400)     # reveal the auto-hiding toolbar
            self._page.mouse.move(640, 660)
        except Exception:
            pass
        if self._click_any(self._CHAT_BTN, overall_ms=6000, poll_ms=300):
            self._chat_opened_once = True
            for _ in range(25):
                self._page.wait_for_timeout(200)
                if self._chat_open():
                    break
            self._on_log("Открыл чат — слежу за стоп-словом (кнопку «Чат» больше "
                         "не трогаю).")
        else:
            self._on_log("⚠ Кнопка «Чат» не найдена — стоп-слово может не "
                         "сработать (пришлите скриншот встречи).")

    def _read_all_text(self) -> str | None:
        """Visible text of the main frame AND every child frame. Telemost may
        render the chat inside an iframe, so reading only the top document would
        miss the messages (the earlier "0 слов стоп" symptom)."""
        parts = []
        try:
            frames = list(self._page.frames)     # includes the main frame
        except Exception:
            frames = []
        for fr in frames:
            try:
                t = fr.inner_text("body")
                if t and t.strip():
                    parts.append(t)
            except Exception:
                continue
        return "\n".join(parts) if parts else None

    @staticmethod
    def _is_stop_line(line: str, word: str) -> bool:
        """Is this chat line the stop command?

        Robust to how Telemost lays out a message: whether «стоп» sits on its
        own line OR inline after the author name/time («Зоя Р. 12:53 стоп»).
        Rule: the stop word must appear as a STANDALONE letter-token, and the
        line must be short (a command, not a sentence). Digits, punctuation and
        emoji are ignored, so «стоп!», «стоп 12:50», «стоп 🔴» all count."""
        low = line.strip().lower().replace("ё", "е")
        w = word.replace("ё", "е")
        # Letter-only tokens (Unicode letters); drops author-name punctuation,
        # timestamps and emoji so only real words remain.
        tokens = re.findall(r"[^\W\d_]+", low, re.UNICODE)
        if w not in tokens:
            return False
        return len(tokens) <= BROWSER_STOP_MAX_TOKENS

    def maybe_chat_stop(self, word: str) -> bool:
        """True when a NEW stop-word message appeared in the chat.

        Never clicks anything — the panel was opened once by open_chat(). Reads
        all frames' text every ~5 s, counts standalone stop-word lines, and fires
        when the count grows above the first-seen baseline. The live count is
        logged whenever it changes, so a miss is diagnosable from the log."""
        word = (word or "").strip().lower()
        if not word:
            return False
        now = time.time()
        if now - self._chat_last_peek < 5:
            return False
        self._chat_last_peek = now

        text = self._read_all_text()
        if text is None:
            return False
        lines = text.splitlines()
        n = sum(1 for ln in lines if self._is_stop_line(ln, word))
        if n != self._chat_last_n:
            # When the word IS on the page but no line qualified as a command,
            # show a sample — so a layout change is diagnosable from the card log
            # («вижу слово, но строка не похожа на команду» vs «слова нет вовсе»).
            if n == 0 and any(word in ln.lower().replace("ё", "е") for ln in lines):
                sample = next(ln.strip() for ln in lines
                              if word in ln.lower().replace("ё", "е"))[:80]
                self._on_log(f"Чат: слово «{word}» вижу, но не как отдельную "
                             f"команду (строка: «{sample}»). Напишите «{word}» "
                             "отдельным сообщением.")
            else:
                self._on_log(f"Чат: сообщений «{word}» видно {n}.")
            self._chat_last_n = n
        # Сами строки: сообщение несёт время («стоп 16:49»), поэтому новое
        # отличимо от старого. Это надёжнее счётчика — при прокрутке
        # виртуализированного списка количество видимых строк скачет в обе
        # стороны, и рост «нового сообщения» терялся.
        hits = {ln.strip() for ln in lines if self._is_stop_line(ln, word)}
        if self._chat_baseline is None:
            self._chat_baseline = n           # ignore whatever was already there
            self._chat_seen = hits            # всё, что уже лежало в чате
            return False
        fresh = hits - self._chat_seen
        self._chat_seen |= hits
        if fresh:
            self._on_log(f"Чат: новое сообщение «{word}» — останавливаю запись.")
            return True
        # Запасной признак на случай, когда две команды совпали дословно
        # (одна минута, одно слово): тогда множество не растёт, а счётчик да.
        if n > self._chat_baseline:
            self._chat_baseline = n
            return True
        if n < self._chat_baseline:
            self._chat_baseline = n
        return False

    def wait_until_end(self, should_stop, max_sec: int, alone_sec: int,
                       min_participants: int = 1, chat_stop_word: str = "") -> str:
        """Block until the meeting ends. Returns the reason it stopped.

        Stop conditions (first wins): manual stop (`should_stop`), the stop word
        written in the meeting chat (`chat_stop_word`), hard time cap
        (`max_sec`), the bot dropped out of the call, or the room thinned to
        `min_participants` or fewer for `alone_sec` — but only AFTER real
        participants were seen, so joining early (empty room) doesn't end it.
        """
        start = time.time()
        thin_since = None
        seen_others = False           # has anyone besides the bot ever appeared?
        gone_since = None             # since when is_in_call has been False
        last_mute = 0.0               # keep the bot muted for the whole meeting
        # If nobody ever joins, don't sit for the full max_sec — leave after this.
        never_joined_sec = int(self.cfg.get("end_if_nobody_joins_sec", 300))
        # Give the call a moment to render its controls before we judge it.
        self._page.wait_for_timeout(6000)
        # Open the chat ONCE now; from here maybe_chat_stop only reads it.
        if (chat_stop_word or "").strip():
            try:
                self.open_chat()
            except Exception:
                pass
        while True:
            if should_stop and should_stop():
                return "stopped"
            try:
                if self.maybe_chat_stop(chat_stop_word):
                    return "chat_stop"
            except Exception:  # chat probing must never crash the recording
                pass
            if time.time() - start > max_sec:
                return "max_duration"
            # Re-assert mute periodically: Telemost can reset the mic/cam after a
            # reconnect or a long session, so muting once at join isn't enough.
            if time.time() - last_mute > 20:
                try:
                    self._page.mouse.move(400, 300)   # nudge UI to reveal controls
                    self._page.mouse.move(400, 680)
                except Exception:
                    pass
                self.ensure_muted()
                last_mute = time.time()
            # Явное «встреча завершена» — выходим сразу, без выдержки: это не
            # мигание интерфейса, а конец встречи. Проверяем ДО is_in_call,
            # потому что на этом экране часть кнопок управления остаётся и
            # is_in_call считает, что мы всё ещё в звонке.
            if self.call_ended():
                return "call_ended"

            if not self.is_in_call():
                # Don't bail on a transient miss (UI re-render); only conclude the
                # call ended after the controls have been absent for a while.
                gone_since = gone_since or time.time()
                if time.time() - gone_since > 25:
                    return "left_call"
                time.sleep(3)
                continue
            gone_since = None

            # Two independent "am I alone?" signals: the «Участники» count and the
            # invite-prompt screen Telemost shows when the bot is by itself.
            n = self.participant_count()
            alone = self.alone_screen()
            # Someone else is present if the count says 2+, or the invite prompt is
            # gone (a participant tile replaced it) while we could read no number.
            if (n is not None and n >= 2) or (n is None and not alone):
                seen_others = True
            is_alone = alone or (n is not None and n <= max(1, min_participants))

            if is_alone:
                thin_since = thin_since or time.time()
                alone_for = time.time() - thin_since
                # Others were here and left → end soon. Nobody ever joined → wait a
                # bit longer (host may be late) before giving up.
                limit = alone_sec if seen_others else max(alone_sec, never_joined_sec)
                if alone_for > limit:
                    return "thinned_out" if seen_others else "nobody_joined"
            else:
                thin_since = None
            time.sleep(5)

    def close(self) -> None:
        for closer in (lambda: self._ctx and self._ctx.close(),
                       lambda: self._pw and self._pw.stop()):
            try:
                closer()
            except Exception:
                pass
        self._ctx = self._page = self._pw = None
        # Drop the throwaway guest profile so temp dirs don't pile up.
        if self._temp_profile:
            shutil.rmtree(self._temp_profile, ignore_errors=True)
            self._temp_profile = None


def login(cfg: dict, on_log=None) -> None:
    """Open a HEADED browser on the profile so the user can log into Yandex once.
    Blocks until the window is closed; the session is saved in the profile dir."""
    from playwright.sync_api import sync_playwright
    log = on_log or (lambda *_: None)
    user_dir = str(_profile_dir(cfg))
    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(
            user_dir, headless=False, args=_LAUNCH_ARGS)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto("https://passport.yandex.ru/auth", wait_until="domcontentloaded")
        log("Войдите в Яндекс в открывшемся окне, затем закройте его.")
        # Wait until the user closes the context.
        try:
            while ctx.pages:
                page.wait_for_timeout(1000)
        except Exception:
            pass
