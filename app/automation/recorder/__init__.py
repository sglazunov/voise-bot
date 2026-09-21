"""Record a Telemost meeting: a Chromium bot joins, ffmpeg captures A/V.

Public surface:
  readiness(cfg)                          -> what's installed / still missing
  record_meeting(url, out_path, cfg, ...) -> {ok, path, reason, joined_at}
                                          | {ok: False, reason, error}

The bot join (browser.py) and the capture (capture.py) are kept separate so the
capture backend (x11grab + PulseAudio) sits behind one interface, and so
each can be tuned independently. Live recording requires Playwright + Chromium +
ffmpeg + a loopback audio device on the host (Xvfb + PulseAudio, docker/run.sh).
"""
from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from ... import logs
from . import browser, capture

# Модульный логгер назван _LOG, а не log, ОСОЗНАННО: в этом модуле `log` —
# локальная функция журнала карточки встречи, и она перекрывала логгер.
# Обработчик ошибки, звавший log.warning, падал с AttributeError изнутри
# except — и уносил управление мимо спасательного кода.
_LOG = logs.get("vtx.recorder")

# --------------------------------------------------------------------------- #
# Parallel recording slots.
# Each concurrent recording gets an ISOLATED "slot": its own Xvfb display and its
# own PulseAudio null-sink. The browser is pinned to the slot's display + sink,
# and ffmpeg captures exactly that display + that sink's monitor — so two meetings
# recorded at the same time never bleed into each other's video or audio.
# Displays :99,:100,… and sinks meet0,meet1,… are created by docker/run.sh.
# --------------------------------------------------------------------------- #
MAX_SLOTS = max(1, min(int(os.getenv("VTX_MAX_CONCURRENT_RECORDINGS", "4") or "4"), 8))
_DISPLAY_BASE = int(os.getenv("VTX_DISPLAY_BASE", "99"))


@dataclass
class Slot:
    index: int
    display: str   # e.g. ":99"
    sink: str      # e.g. "meet0"
    source: str    # e.g. "meet0.monitor"


_slot_lock = threading.Lock()
_free_slots: list["Slot"] | None = None


def _init_slots() -> None:
    global _free_slots
    if _free_slots is None:
        _free_slots = [Slot(i, f":{_DISPLAY_BASE + i}", f"meet{i}", f"meet{i}.monitor")
                       for i in range(MAX_SLOTS)]


def acquire_slot() -> "Slot | None":
    """Take a free recording slot (display+sink), or None if all are busy."""
    with _slot_lock:
        _init_slots()
        return _free_slots.pop() if _free_slots else None


def release_slot(slot: "Slot | None") -> None:
    if slot is None:
        return
    with _slot_lock:
        _init_slots()
        if all(s.index != slot.index for s in _free_slots):
            _free_slots.append(slot)


def active_recordings() -> int:
    with _slot_lock:
        _init_slots()
        return MAX_SLOTS - len(_free_slots)


# The Telemost recorder bot is NOT part of this Linux "core" build. It needs a
# virtual display (Xvfb) + PulseAudio null-sink on the server, which is the next
# step. The full implementation below is kept intact and re-enabled by setting
# VTX_RECORDER_ENABLED=1 once that infrastructure is in place.
_RECORDER_ENABLED = os.getenv("VTX_RECORDER_ENABLED", "0") == "1"
_DISABLED_MSG = ("Бот-рекордер не входит в этот образ (Linux-ядро). Он будет добавлен "
                 "следующим шагом — с Xvfb и PulseAudio на сервере. "
                 "Распознавание, протоколы, Weeek и облако работают без него.")

# Звуковой сервер недоступен приложению. Причина почти всегда одна: каталог
# PULSE_RUNTIME_PATH принадлежит root (его создаёт любая команда `pactl`,
# выполненная от root внутри контейнера), а приложение работает под app.
_AUDIO_BROKEN_MSG = (
    "Звук недоступен: приложение не может подключиться к PulseAudio, поэтому "
    "запись была бы немой. Обычно каталог /tmp/pulse принадлежит root — "
    "перезапустите контейнер (docker compose up -d), entrypoint починит права.")


def readiness(cfg: dict) -> dict:
    """What the screen recorder needs: a browser (Playwright) + ffmpeg + audio."""
    if not _RECORDER_ENABLED:
        return {"ready": False, "mode": "disabled", "detail": _DISABLED_MSG}
    b = browser.readiness(cfg)
    c = capture.readiness(cfg)
    return {"ready": bool(b.get("ready") and c.get("ready")), "mode": "screen",
            "browser": b, "capture": c,
            "auth_mode": cfg.get("auth_mode") or "guest"}


def record_meeting(url: str, out_path: str, cfg: dict,
                   on_log=None, should_stop=None, slot: "Slot | None" = None) -> dict:
    """Join `url` and screen-record the meeting (ffmpeg) until it ends, on the
    given isolated `slot` (its own Xvfb display + PulseAudio sink). The caller
    acquires the slot from the pool and releases it afterwards.
    """
    log = on_log or (lambda *_: None)
    if not _RECORDER_ENABLED:
        log(_DISABLED_MSG)
        return {"ok": False, "error": _DISABLED_MSG}
    if slot is None:
        return {"ok": False, "error": "Нет свободного слота записи."}
    # Pin the bot's browser to THIS slot's display + audio sink so its video and
    # sound are captured in isolation (never mixed with another parallel meeting).
    bot = browser.TelemostBot(cfg, on_log=log, display=slot.display, sink=slot.sink)
    rec = None
    wd_stop = threading.Event()   # stops the audio watchdog on any exit path
    # Объявляем ДО try: обработчик ошибок читает had_silence, а сбой возможен
    # ещё на входе в звонок — тогда здесь был бы NameError вместо диагностики.
    audio_state = {"silent_since": None, "warned": False, "had_silence": False,
                   "had_speech": False, "ended_by_silence": False}
    joined_at = None      # см. audio_state: обработчик сбоя читает и это
    try:
        if not bot.join(url, should_stop=should_stop):
            if should_stop and should_stop():
                # Ручная остановка во время входа — не провал бота и не
                # повод для скриншота «изменилась вёрстка».
                return {"ok": False, "reason": "stopped",
                        "error": "Остановлено во время входа на встречу."}
            shot = str(Path(out_path).with_suffix(".join-failed.png"))
            bot.screenshot(shot)
            # Диагностика не должна менять диагноз: любая осечка при
            # сохранении HTML — в лог, а наружу всё равно «не пустили».
            try:
                bot.dump_html(str(Path(out_path).with_suffix(".join-failed.html")))
            except Exception as e:  # noqa: BLE001
                _LOG.warning("HTML страницы при неудачном входе не сохранён: %s", e)
            log(f"Скриншот и HTML страницы сохранены рядом с записью: {Path(shot).name}")
            # ⚠️ «Не пустили» и «бот не пришёл» — РАЗНЫЕ отказы, и лечатся
            # по-разному (вёрстка Телемоста против планировщика). Раньше эта
            # ветка не возвращала `reason`, и в метрике они сливались.
            return {"ok": False, "reason": "join_failed",
                    "error": "Не удалось войти в встречу (см. скриншот). "
                             "Возможно, изменилась вёрстка Телемоста или встреча "
                             "требует входа в Яндекс.",
                    "screenshot": shot}
        joined_at = time.time()

        max_sec = int(cfg.get("max_meeting_min", 240)) * 60
        alone_sec = int(cfg.get("end_when_alone_sec", 90))
        min_p = int(cfg.get("min_participants", 1))

        rec = capture.FFmpegRecorder(out_path, cfg, on_log=log,
                                     display=slot.display, source=slot.source)
        log(f"Бот в звонке (слот {slot.index}, экран {slot.display}). Запускаю запись…")
        rec.start()
        time.sleep(3)
        if not rec.running:
            tail = rec.error_tail() or ""
            # Самая частая и самая непонятная причина — недоступный звуковой
            # сервер: ffmpeg сыплет строками про probesize и x11grab, из-за чего
            # ищут проблему в видео, хотя отвалился звук. Называем причину прямо.
            if "secure directory" in tail or "Connection refused" in tail:
                return {"ok": False, "error": _AUDIO_BROKEN_MSG + f" (слот {slot.index})"}
            return {"ok": False,
                    "error": "ffmpeg не смог записывать. " + (tail or
                             "Проверьте ffmpeg/дисплей/аудио слота.")}
        # Номер слота и общее число занятых — чтобы «на встрече два бота» было
        # видно из лога, а не только глазами на плитках. Второй одновременный
        # бот появляется тут как «запись 2 из 4».
        log(f"🔴 Идёт запись встречи — бот в звонке "
            f"(слот {slot.index}, всего записей идёт: {active_recordings()}).")

        # Д9: audio watchdog. A dead PulseAudio sink means the bot silently
        # records mute video for an hour — discovered only after the meeting.
        # Sample the slot's monitor every 30 s (Pulse monitors allow a second
        # reader); after 2 min of continuous silence, shout into the card log.
        # Д15: выход по тишине — независимо от вёрстки Телемоста. Признаки
        # «все вышли» читаются из DOM (счётчик участников, экран «пригласите»,
        # «встреча завершена»), и когда Телемост не отдал НИ ОДНОГО из них, бот
        # писал пустую комнату до упора в максимальную длину: 04.08 — четыре
        # часа на 118 слов речи, 11.08 — четыре часа на 71 минуту разговора.
        # Три часа тишины потом ещё и распознаются. Тишина после того, как речь
        # уже была, — надёжный признак конца встречи, и он не зависит от
        # селекторов. 0 отключает правило.
        end_silence = int(os.getenv("VTX_END_ON_SILENCE_SEC", "600"))

        def _audio_watchdog() -> None:
            while not wd_stop.wait(30):
                if not rec.running:
                    break
                lvl = capture.test_audio_level("ffmpeg", slot.source, seconds=2)
                if not lvl.get("ok"):
                    continue    # probe hiccup — not evidence of silence
                if lvl.get("has_sound"):
                    if audio_state["warned"]:
                        log("Звук снова есть ✓")
                    audio_state["silent_since"] = None
                    audio_state["warned"] = False
                    audio_state["had_speech"] = True
                    continue
                now = time.time()
                audio_state["silent_since"] = audio_state["silent_since"] or now
                quiet = now - audio_state["silent_since"]
                if quiet >= 120 and not audio_state["warned"]:
                    audio_state["warned"] = True
                    audio_state["had_silence"] = True
                    log("⚠ НЕТ ЗВУКА уже 2 минуты — запись может оказаться немой. "
                        "Проверьте, что встреча не на паузе и звук в комнате есть.")
                # Речь была и давно кончилась — встреча закончилась, что бы ни
                # показывал интерфейс. Требование «речь была» обязательно:
                # иначе правило убивало бы встречу, к которой опаздывают (для
                # этого случая есть отдельный end_if_nobody_joins_sec).
                if (end_silence > 0 and audio_state["had_speech"]
                        and quiet >= end_silence
                        and not audio_state["ended_by_silence"]):
                    audio_state["ended_by_silence"] = True
                    log(f"Тишина {int(quiet // 60)} мин после разговора — "
                        f"считаю встречу оконченной, останавливаю запись.")

        threading.Thread(target=_audio_watchdog, daemon=True,
                         name=f"vtx-audio-wd-{slot.index}").start()

        stop_word = str(cfg.get("chat_stop_word") or "").strip()
        if stop_word and (cfg.get("auth_mode") or "guest") != "profile":
            # Гостю Телемост чат не показывает — панель у бота пустая, и команду
            # он не увидит физически. Раньше мы про это молчали, и «стоп»
            # искали в разборе сообщений три дня. Говорим прямо и называем
            # рабочие способы остановки.
            log(f"⚠ Стоп-слово «{stop_word}» в гостевом режиме НЕ РАБОТАЕТ: "
                "Телемост не показывает чат участникам без аккаунта, у бота он "
                "пустой. Остановить запись можно кнопкой «Стоп» на карточке "
                "встречи; сама она завершится через "
                f"{end_silence // 60} мин тишины после разговора. Чтобы стоп-слово "
                "заработало, нужен режим входа «Авторизованный».")
            stop_word = ""          # не тратим время на чтение пустой панели
        elif stop_word:
            log(f"Кодовое слово в чате: «{stop_word}» — напишите его отдельным "
                "сообщением, и бот остановит запись и выйдет.")

        def _stop_or_silent() -> bool:
            return bool((should_stop and should_stop())
                        or audio_state["ended_by_silence"])

        reason = bot.wait_until_end(_stop_or_silent, max_sec, alone_sec, min_p,
                                    chat_stop_word=stop_word)
        if reason == "stopped" and audio_state["ended_by_silence"]:
            reason = "silence"          # не ручная остановка, а конец встречи
        if reason == "chat_stop":
            log("🛑 В чате написали кодовое слово — останавливаю запись и выхожу.")
        elif reason == "call_ended":
            log("Встречу завершили для всех — останавливаю запись.")
        else:
            log(f"Останавливаю запись (причина: {reason}).")
        wd_stop.set()
        rec.stop()
        p = Path(out_path)
        if not p.exists() or p.stat().st_size == 0:
            return {"ok": False, "reason": reason,
                    "error": "Файл записи пуст — проверьте аудио-устройство и ffmpeg."}
        return {"ok": True, "path": out_path, "reason": reason,
                "size": p.stat().st_size,
                # Когда бот действительно оказался в звонке. Планировщик считает
                # от этого «явку» (docs/ТЗ-МЕТРИКИ.md И36): не пришёл / опоздал /
                # не пустили — три разных отказа, и лечатся они по-разному.
                "joined_at": joined_at,
                "audio_warning": audio_state["had_silence"]}
    except Exception as e:  # noqa: BLE001
        try:
            if rec:
                rec.stop()
        except Exception:
            _LOG.warning("ffmpeg не остановился штатно после сбоя", exc_info=True)
        # Сбой ПОСРЕДИ встречи (сеть, база, вёрстка) не должен стоить записи.
        # Раньше здесь всегда возвращалось ok:False, карточка уходила в «error»,
        # а уже записанный файл никто не выгружал и не распознавал: состояние
        # «error» не входит в pending, и восстановление после перезапуска его не
        # подбирает. Час встречи пропадал из-за секундной ошибки.
        try:
            done = Path(out_path)
            if done.exists() and done.stat().st_size > 0:
                log(f"⚠ Сбой во время записи ({e}), но файл записан — "
                    "продолжаем обработку.")
                return {"ok": True, "path": out_path, "reason": "error",
                        "size": done.stat().st_size,
                        "joined_at": joined_at,
                        "audio_warning": audio_state["had_silence"],
                        "warning": f"Запись прервана ошибкой: {e}"}
        except Exception:      # noqa: BLE001 — спасение файла не обязано работать
            _LOG.warning("Не удалось спасти уже записанный файл %s", out_path,
                        exc_info=True)
        # Отсутствие виртуального экрана Playwright сообщает стектрейсом на
        # английском, и в карточке встречи вместо причины оказывалась простыня
        # «BrowserType.launch_persistent_context…». Причина всегда одна и
        # лечится одинаково — скажем это по-русски.
        if "xserver" in str(e).lower() or "xvfb-run" in str(e).lower():
            return {"ok": False, "error":
                    "Не запущен виртуальный экран (Xvfb) — браузер бота не может "
                    "открыться. Лечится перезапуском: docker compose restart app. "
                    "Подробности — в логе контейнера, строки «[run] … экран»."}
        return {"ok": False, "error": f"Ошибка записи: {e}"}
    finally:
        wd_stop.set()
        bot.close()
