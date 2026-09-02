"""Background scheduler: Weeek meetings -> record -> cloud -> transcribe+protocol.

A single daemon thread polls Weeek on an interval. When a meeting is due (around
its start time) it records it with the Telemost bot, uploads the file to the
chosen cloud, then hands the recording to the existing job pipeline
(store.create with analyze=True) which produces the transcript + Word protocol.
Optionally posts a comment with the cloud link back to the Weeek task.

One recording runs at a time (a browser + ffmpeg are exclusive). Everything is
gated by the `enabled` setting so the user controls it from the UI.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .. import config, db, logs, meeting_series, security
from . import (clouds, delivery, recorder, settings as auto_settings,
               snapshots, weeek)

# Модульный логгер назван _LOG, а не log, ОСОЗНАННО: в этом модуле `log` —
# локальная функция журнала карточки встречи, и она перекрывала логгер.
# Обработчик ошибки, звавший log.warning, падал с AttributeError изнутри
# except — и уносил управление мимо спасательного кода.
_LOG = logs.get("vtx.scheduler")

# How late after start we'll still auto-join (avoids joining long-finished
# meetings on the first poll after startup).
_LATE_GRACE_SEC = 10 * 60
_TICK_SEC = 15
# Слоты одной задачи в этом окне считаются ОДНОЙ встречей с переносом
# времени (недельный recurring — 7 суток, не попадает).
_RESLOT_WINDOW_SEC = 20 * 3600  # loop granularity; actual Weeek polling honours poll_interval_sec


@dataclass
class MeetingState:
    key: str
    task_id: Any
    title: str
    url: str
    start: datetime | None
    owner: str = ""                   # which login this meeting belongs to
    state: str = "scheduled"          # scheduled|no_time|missed|recording|uploading|transcribing|analyzing|done|error
    detail: str = ""
    job_id: str | None = None
    cloud_url: str | None = None
    out_path: str | None = None       # local recording file — lets a restart pick it up
    upload_error: str | None = None   # why the video didn't reach the cloud (retrying)
    do_protocol: bool = False         # whether this meeting also builds a protocol
    record_flag: bool | None = None   # Weeek checkbox «Запись встречи»: True/False/unset
    stop_flag: bool = False           # manual "stop this recording"
    logs: list = field(default_factory=list)
    # Д10: live transcript grown during the recording + the participant's notes
    # taken alongside it (notes attach to the job once it exists).
    live_text: str = ""
    live_updated_at: float = 0.0
    live_notes: str = ""

    def recording_exists(self) -> bool:
        """Лежит ли на диске непустая запись этой встречи."""
        if not self.out_path:
            return False
        try:
            return Path(self.out_path).stat().st_size > 0
        except OSError:
            return False

    def public(self) -> dict:
        return {"task_id": self.task_id, "title": self.title, "url": self.url,
                "start": self.start.isoformat() if self.start else None,
                "state": self.state, "detail": self.detail,
                "job_id": self.job_id, "cloud_url": self.cloud_url,
                "upload_error": self.upload_error,
                "has_live": bool(self.live_text), "has_notes": bool(self.live_notes),
                "do_protocol": self.do_protocol, "record_flag": self.record_flag,
                # Есть ли уже записанный файл. Нужно интерфейсу: у красной
                # карточки он предлагал «Подключиться», и повторный заход
                # затирал готовую запись — имя файла детерминировано.
                "has_recording": self.recording_exists(),
                # Лог рекордера копился в памяти, но наружу отдавалась только
                # ПОСЛЕДНЯЯ строка (как detail). Из-за этого любую проблему бота
                # — не сработавшее стоп-слово, не найденную кнопку чата, запись
                # пустой комнаты — приходилось разбирать вслепую. Отдаём хвост.
                "logs": list(self.logs)[-80:]}


def _free_path(path: Path) -> Path:
    """Путь, по которому ещё нет файла: «имя.mp4» → «имя (2).mp4» и так далее.

    Имя записи складывается из даты, времени и названия задачи, то есть строго
    определено. Значит, повторный заход на ту же встречу открыл бы ffmpeg-ом ТОТ
    ЖЕ файл и затёр готовую запись. Так же совпадают имена у двух встреч с
    одинаковым названием и временем. Файл записи — единственный исходник и для
    выгрузки, и для «Повторить», поэтому переписывать его нельзя никогда.
    """
    if not path.exists():
        return path
    stem, suffix, parent = path.stem, path.suffix, path.parent
    for n in range(2, 100):
        cand = parent / f"{stem} ({n}){suffix}"
        if not cand.exists():
            return cand
    return parent / f"{stem} ({int(time.time())}){suffix}"


# Час (по часовому поясу команды), когда чистятся поля «Видео встречи» и
# «Протокол встречи» у повторяющихся задач Weeek. Ночью — чтобы прошлые ссылки
# были доступны весь рабочий день и исчезали задолго до новой встречи.
_WIPE_HOUR = int(os.getenv("VTX_FIELD_WIPE_HOUR", "4"))
# Сколько часов длится окно чистки. Пропустили (сервис был выключен) — ждём
# следующей ночи: чистить днём хуже, чем не почистить вовсе.
_WIPE_WINDOW_H = int(os.getenv("VTX_FIELD_WIPE_WINDOW_H", "2"))

_ROOM_RE = re.compile(r"/j/([a-z0-9_-]+)", re.I)


def _protocol_wanted(cfg: dict, owner: str, title: str, log=None) -> bool:
    """Собирать ли протокол этой встречи: общий тумблер do_protocol И приоритет
    серии — карточка серии с «только запись» отключает протокол (экономия
    лимитов движка на встречах, где нужна лишь запись)."""
    if not cfg.get("do_protocol", True):
        return False
    try:
        if meeting_series.priority_of(owner, title) == "record_only":
            if log:
                log("Серия встреч помечена «только запись» — протокол не собираю.")
            return False
    except Exception:  # noqa: BLE001 — карточка серии не должна ломать запись
        _LOG.debug("Приоритет серии не прочитан", exc_info=True)
    return True


def room_key(url: str) -> str:
    """Комната Телемоста без учёта ФОРМЫ ссылки.

    Защита «одна ссылка — один бот» сравнивала строки дословно, а на одну и ту
    же комнату ссылка приходит по-разному: с параметрами после «?», с лишним
    слешем, в другом регистре, иногда без протокола. Достаточно любого такого
    расхождения — и на встречу заходили ДВА «Протокол-бота». Со стороны это
    выглядело как «стоп-слово срабатывает через раз»: по команде выходил один
    бот, второй продолжал писать.
    """
    u = (url or "").strip().lower()
    m = _ROOM_RE.search(u)
    if m:
        return m.group(1)
    return u.split("?")[0].split("#")[0].rstrip("/")


class Scheduler:
    def __init__(self) -> None:
        self._states: dict[str, MeetingState] = {}
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._last_poll: dict[str, float] = {}  # per-user last Weeek poll
        # Последняя ошибка опроса по командам — показывается в интерфейсе.
        self._last_error: dict[str, str] = {}
        self._boot_time = 0.0  # session start; meetings older than this are missed

    # -- lifecycle ----------------------------------------------------------
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._boot_time = time.time()  # don't auto-join meetings already past now
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="vtx-scheduler")
        self._thread.start()
        # A restart killed the previous process's delivery threads; pick their
        # meetings back up so Weeek links don't silently get lost on deploys.
        threading.Thread(target=self._resume_pending, daemon=True,
                         name="vtx-resume").start()

    def status(self, user: str) -> dict:
        user = security.team_of(user)   # members see the team's meetings
        cfg = auto_settings.load(user)
        with self._lock:
            meetings = [s.public() for s in sorted(
                (s for s in self._states.values() if s.owner == user),
                key=lambda s: (s.start is None, s.start or datetime.max.replace(
                    tzinfo=timezone.utc)))]
        # Don't let old "missed" meetings pile up — but EVERY missed meeting of
        # the last 24 h must stay visible: each still has its «Подключиться»,
        # and hiding one (as the old keep-only-latest rule did) left a LIVE
        # meeting with no way to send the bot manually.
        missed = [m for m in meetings if m.get("state") == "missed"]
        if len(missed) > 1:
            from datetime import timedelta
            cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
            fresh = [m for m in missed if (m.get("start") or "") >= cutoff]
            keep = fresh or [max(missed, key=lambda m: m.get("start") or "")]
            keep_ids = {id(m) for m in keep}
            meetings = [m for m in meetings
                        if m.get("state") != "missed" or id(m) in keep_ids]
        # Reflect the LIVE pipeline stage (recognition → protocol → done) from
        # the job's own status, so the UI shows real progress after recording.
        for m in meetings:
            self._enrich_from_job(m)
        active = recorder.active_recordings()
        return {"running": bool(self._thread and self._thread.is_alive()),
                "enabled": bool(cfg.get("enabled")),
                "recording": active > 0,
                "active": active, "max_parallel": recorder.MAX_SLOTS,
                "last_poll": self._last_poll.get(user, 0.0), "meetings": meetings,
                # Последний сбой опроса — иначе «автоматика работает», а встречи
                # не появляются, и причина видна только в логах контейнера.
                "last_error": self._last_error.get(user, "")}

    def poll_now(self, user: str) -> dict:
        """Force an immediate Weeek re-poll for this team — the manual «Обновить
        статус» button — so new meetings and status appear without waiting for
        the next tick."""
        user = security.team_of(user)
        cfg = auto_settings.load(user)
        if not cfg.get("weeek_token"):
            return {"ok": False, "error": "Сначала задайте токен Weeek."}
        self.start()  # idempotent
        try:
            self._poll(user, cfg)
            self._last_poll[user] = time.time()
            # Кнопка «Обновить из Weeek» обновляет список встреч, но НЕ должна
            # запускать записи при выключенной автоматике: раньше она их
            # запускала, и мастер-тумблер выглядел неработающим.
            if cfg.get("enabled"):
                self._maybe_trigger(user, cfg)
        except weeek.WeeekError as e:
            return {"ok": False, "error": str(e)}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": f"Сбой опроса: {e}"}
        return {"ok": True, "detail": "Обновлено из Weeek."}

    # -- main loop ----------------------------------------------------------
    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                # Run automation ONCE PER TEAM, under the team-admin's (decrypted)
                # tokens and settings — members share it, so no duplicate records.
                for user in security.list_teams():
                    try:
                        cfg = auto_settings.load(user)
                        if not (cfg.get("enabled") and cfg.get("weeek_token")):
                            continue
                        interval = max(30, int(cfg.get("poll_interval_sec", 120)))
                        if time.time() - self._last_poll.get(user, 0.0) >= interval:
                            self._poll(user, cfg)
                            self._last_poll[user] = time.time()
                        self._maybe_trigger(user, cfg)
                        self._nightly_wipe(user, cfg)
                        self._last_error.pop(user, None)
                    except Exception as e:  # сбой одной команды не валит другие
                        _LOG.warning("Опрос команды %s сорвался", user, exc_info=True)
                        self._last_error[user] = f"{type(e).__name__}: {e}"[:300]
            except Exception:  # never let the loop die
                _LOG.error("Сбой в цикле планировщика", exc_info=True)
            self._stop.wait(_TICK_SEC)

    def _nightly_wipe(self, user: str, cfg: dict) -> None:
        """Раз в сутки очистить в Weeek поля видео и протокола у сегодняшних встреч.

        Повторяющаяся задача Weeek — это ОДНА задача, и её кастомные поля несут
        ссылки прошлого проведения. Если их не чистить, вчерашнее видео выглядит
        как сегодняшнее.

        Раньше чистка шла в момент старта записи. Это работало, но прошлые
        ссылки исчезали ровно тогда, когда встреча начиналась, — а именно в этот
        момент к ним чаще всего и обращаются («что решили в прошлый раз»).
        Ночной проход оставляет весь рабочий день на прошлые ссылки и убирает их
        задолго до новой встречи.

        Отметка о выполнении хранится в настройках: иначе перезапуск сервиса
        днём запустил бы чистку повторно и стёр УЖЕ СВЕЖИЕ ссылки.
        """
        tz = self._tz(cfg)
        now = datetime.now(tz)
        # Только НОЧНОЕ окно. Проверять «сегодня ещё не чистили» недостаточно:
        # сервис, запущенный впервые днём, тут же стёр бы ссылки, записанные
        # утренней встречей. Окно в пару часов даёт запас на перезапуски, но не
        # позволяет чистке случиться среди рабочего дня.
        if not (_WIPE_HOUR <= now.hour < _WIPE_HOUR + _WIPE_WINDOW_H):
            return
        today = now.date().isoformat()
        if cfg.get("last_field_wipe") == today:
            return
        token = cfg.get("weeek_token")
        if not token:
            return
        try:
            meetings = weeek.upcoming_meetings(
                token, cfg.get("weeek_project_id"), tz)
        except Exception:               # noqa: BLE001 — чистка не обязана удаться
            _LOG.warning("Ночная чистка полей: не удалось получить встречи",
                         exc_info=True)
            return
        done = 0
        today_date = now.date()
        for m in meetings:
            # ТОЛЬКО сегодняшние проведения. Раньше чистились ВСЕ задачи со
            # ссылкой на Телемост — и разовая встреча прошлой недели теряла
            # прикреплённые (в том числе руками) ссылки на видео и протокол.
            start = getattr(m, "start", None)
            if start is not None and start.astimezone(tz).date() != today_date:
                continue
            try:
                delivery.wipe_stale_links(m.task_id, cfg, lambda _m: None)
                done += 1
            except Exception:           # noqa: BLE001
                _LOG.warning("Ночная чистка: задача %s не очищена", m.task_id,
                             exc_info=True)
        auto_settings.save(user, {"last_field_wipe": today})
        _LOG.info("Ночная чистка полей Weeek (%02d:00): очищено задач %d",
                  _WIPE_HOUR, done)

    def _tz(self, cfg: dict):
        try:
            from zoneinfo import ZoneInfo
            return ZoneInfo(cfg.get("timezone") or "Europe/Moscow")
        except Exception:
            return timezone.utc

    def _poll(self, user: str, cfg: dict) -> None:
        meetings = weeek.upcoming_meetings(
            cfg.get("weeek_token"), cfg.get("weeek_project_id"), self._tz(cfg))
        rec_field = cfg.get("weeek_record_field") or "Запись встречи"
        # Снапшоты (а на Postgres это запрос к базе) читаем ДО лока: под ним
        # стоят status(), обновления состояния из потоков записи и остановка
        # записи. Держать их на время запроса к БД незачем.
        snaps_for_dedup = snapshots.load(user)
        with self._lock:
            for m in meetings:
                # The Weeek «Запись встречи» checkbox (True=record, False=skip,
                # None=field absent → fall back to filters). Re-read every poll so
                # toggling it in Weeek takes effect before the meeting starts.
                flag = weeek.custom_field_bool(m.raw, rec_field)
                key = f"{user}:{m.task_id}:{m.start.isoformat() if m.start else 'no-time'}"
                st = self._states.get(key)
                if st is None:
                    st = MeetingState(key=key, task_id=m.task_id, title=m.title,
                                      url=m.url, start=m.start, owner=user,
                                      record_flag=flag)
                    if m.start is None:
                        st.state, st.detail = "no_time", "В задаче не указано время встречи."
                    # A restart wipes the in-memory states; without this, a
                    # meeting the PREVIOUS process already recorded would come
                    # back as «пропущена». Restore its real outcome from disk.
                    self._restore_snapshot(st)
                    self._states[key] = st
                elif st.state == "scheduled":
                    st.url, st.title, st.start = m.url, m.title, m.start
                    st.record_flag = flag

            # ПЕРЕНОС ВРЕМЕНИ в задаче: карточка привязана к (task_id, start),
            # так что смена времени рождает НОВЫЙ слот, а старый остаётся —
            # дубль. Уборка:
            # 1) пустой слот (scheduled/no_time/missed), которого больше нет в
            #    выдаче Weeek (время перенесли / задачу удалили) — убрать;
            # 2) слот «пропущена», если ЭТУ задачу в ТОТ ЖЕ день уже записали
            #    под другим временем (перенос времени задним числом после
            #    записи) — шумовой дубль, убрать. Повторяющиеся встречи с тем
            #    же id в другие дни не задеваются.
            current = {(str(m.task_id),
                        m.start.isoformat() if m.start else "no-time")
                       for m in meetings}
            fetched_ids = {str(m.task_id) for m in meetings}
            # Слоты одной задачи ближе _RESLOT_WINDOW друг к другу — это ОДНА
            # встреча с переносом времени (день по UTC сравнивать нельзя:
            # полночь по Москве — это ещё 21:00 UTC накануне). Недельный
            # recurring отстоит на 7 суток и под окно не попадает.
            recorded_slots: list[tuple[str, datetime]] = [
                (str(s.task_id), s.start)
                for s in self._states.values()
                if s.owner == user and s.start is not None and s.state in (
                    "recording", "uploading", "transcribing", "analyzing", "done")]
            # После рестарта записанный слот живёт только в СНАПШОТЕ (его ключа
            # нет в выдаче Weeek) — без этого «пропущена»-дубль не распознался бы.
            for sk, sv in snaps_for_dedup.items():
                parts = sk.split(":", 2)
                if (len(parts) == 3 and sv.get("state") in (
                        "recording", "uploading", "transcribing",
                        "analyzing", "done", "error")):
                    try:
                        recorded_slots.append((parts[1],
                                               datetime.fromisoformat(parts[2])))
                    except ValueError:
                        pass
            for k, s in list(self._states.items()):
                if s.owner != user or s.state not in ("scheduled", "no_time", "missed"):
                    continue
                slot = (str(s.task_id),
                        s.start.isoformat() if s.start else "no-time")
                stale = str(s.task_id) in fetched_ids and slot not in current
                dup_missed = (s.state == "missed" and s.start is not None
                              and any(tid == str(s.task_id)
                                      and abs((s.start - st0).total_seconds())
                                      <= _RESLOT_WINDOW_SEC
                                      for tid, st0 in recorded_slots))
                if stale or dup_missed:
                    self._states.pop(k, None)
                    if dup_missed and s.start is not None:
                        # Вместо шумового дубля вернуть НАСТОЯЩУЮ карточку — тот
                        # слот, под которым встреча была записана.
                        self._revive_recorded_slot(
                            user, str(s.task_id), s.start, snaps_for_dedup)

    @staticmethod
    def _kw(raw) -> list[str]:
        text = str(raw or "").replace("\n", ",")
        return [w.strip().lower() for w in text.split(",") if w.strip()]

    def _passes_filter(self, st: "MeetingState", cfg: dict) -> tuple[bool, str]:
        """Apply the user's "which meetings to record" rules. Empty = record all.

        A per-meeting manual choice wins over everything: explicitly ON always
        records, explicitly OFF never does. With no choice, the default mode
        applies (record all, or record only chosen), then the keyword/time rules.
        """
        dec = (cfg.get("rec_decisions") or {}).get(str(st.task_id))
        if dec is True:
            return True, ""
        if dec is False:
            return False, "выключена вручную"
        # Weeek checkbox «Запись встречи»: an explicit toggle wins over the default
        # mode and keyword/time filters (but a manual override above still wins).
        if cfg.get("weeek_use_record_field", True) and st.record_flag is not None:
            if st.record_flag:
                return True, ""
            fname = cfg.get("weeek_record_field") or "Запись встречи"
            return False, f"выключено в Weeek (поле «{fname}»)"
        if not cfg.get("rec_default_on", True):
            return False, "режим «только выбранные» — не отмечена"
        title = (st.title or "").lower()
        exc = self._kw(cfg.get("rec_exclude"))
        if exc and any(k in title for k in exc):
            return False, "исключено по слову в названии"
        inc = self._kw(cfg.get("rec_include"))
        if inc and not any(k in title for k in inc):
            return False, "название не содержит нужных слов"
        if st.start is not None:
            local = st.start.astimezone(self._tz(cfg))
            days = cfg.get("rec_days") or []
            if days and local.weekday() not in [int(d) for d in days]:
                return False, "день недели не выбран"
            frm = (cfg.get("rec_time_from") or "").strip()
            to = (cfg.get("rec_time_to") or "").strip()
            if frm or to:
                hm = local.strftime("%H:%M")
                if not ((frm or "00:00") <= hm <= (to or "23:59")):
                    return False, f"время {hm} вне окна {frm or '00:00'}–{to or '23:59'}"
        return True, ""

    def _maybe_trigger(self, user: str, cfg: dict) -> None:
        # Auto-record only when the bot is enabled in this build.
        if os.getenv("VTX_RECORDER_ENABLED", "0") != "1":
            return
        now = datetime.now(timezone.utc).timestamp()
        lookahead = int(cfg.get("lookahead_min", 2)) * 60
        candidates = []
        with self._lock:
            for st in self._states.values():
                if st.owner != user:
                    continue
                if st.state != "scheduled" or st.start is None:
                    continue
                start = st.start.timestamp()
                # Don't auto-join a meeting that was already past when the app
                # started this session (e.g. a stale meeting after a restart) —
                # only record meetings that come due while we're running.
                if start < self._boot_time:
                    st.state, st.detail = "missed", "Началась до запуска приложения — пропущено."
                    continue
                if now < start - lookahead:
                    continue  # not yet
                if now > start + _LATE_GRACE_SEC:
                    st.state, st.detail = "missed", "Время начала прошло — пропущено."
                    continue
                ok, why = self._passes_filter(st, cfg)
                if not ok:
                    st.state, st.detail = "skipped", f"Не записываем: {why}."
                    continue
                candidates.append((start, st))
        # Launch as many due meetings as there are FREE recording slots — up to
        # MAX_SLOTS run in parallel, each isolated on its own display + sink.
        candidates.sort(key=lambda x: x[0])  # earliest-starting first
        # ОДНА ссылка Телемоста = ОДНА одновременная запись. Перенос времени
        # (или две задачи с одной ссылкой) порождает два слота одной встречи —
        # без этого на звонок заходили ДВА бота.
        with self._lock:
            busy_urls = {room_key(s.url) for s in self._states.values()
                         if s.owner == user and s.url
                         and s.state == "recording"}
        for _, chosen in candidates:
            if chosen.url and room_key(chosen.url) in busy_urls:
                with self._lock:
                    if chosen.state == "scheduled":
                        chosen.state, chosen.detail = (
                            "skipped", "Бот уже в этом звонке (та же ссылка "
                                       "записывается): всё сказанное попадёт в ту "
                                       "запись. Ссылки этой задаче можно "
                                       "прикрепить вручную (кнопка-скрепка).")
                continue
            slot = recorder.acquire_slot()
            if slot is None:
                break  # all slots busy — the rest wait for the next tick
            # Claim atomically BEFORE spawning, so the next tick/poller sees it's
            # taken and never starts a second browser for the same meeting.
            claimed = False
            with self._lock:
                if chosen.state == "scheduled":
                    chosen.state, chosen.detail = "recording", "Бот заходит на встречу…"
                    chosen.stop_flag = False
                    claimed = True
            if not claimed:
                recorder.release_slot(slot)
                continue
            busy_urls.add(room_key(chosen.url))
            threading.Thread(target=self._run, args=(chosen, slot), daemon=True).start()

    # -- per-meeting pipeline ----------------------------------------------
    def _run(self, st: MeetingState, slot, manual: bool = False) -> None:
        """`manual=True` — запуск кнопкой «Подключиться».

        Тогда мастер-тумблер «Автоматика» в условие остановки НЕ входит: при
        выключенной автоматике should_stop возвращал True сразу, вход в звонок
        прерывался на первом же клике, и человек получал ложное «изменилась
        вёрстка Телемоста» вместо «автоматика выключена».
        """
        try:
            user = st.owner
            cfg = auto_settings.load(user)

            def log(msg: str) -> None:
                msg = str(msg)
                st.logs.append(msg)
                # И в журнал контейнера тоже. Раньше лог бота жил ТОЛЬКО в
                # памяти карточки: в `docker compose logs` было видно лишь
                # «run-now → 200 OK» и тишину, а почему бот не зашёл на встречу
                # — нигде. Причём после перезапуска карточка обнуляется, и
                # разбираться становится не по чему.
                _LOG.info("встреча %s: %s", st.task_id, msg)
                # Показываем ход записи вместо застывшего «захожу…», но НЕ
                # переводим состояние. Раньше здесь стояло _set(st,"recording"),
                # и запоздавшее сообщение из фонового потока (стирание ссылок в
                # Weeek ретраится минутами) возвращало карточку в «recording»
                # после «error» или «uploading». Комната навсегда считалась
                # занятой, и все следующие встречи по той же постоянной ссылке
                # получали «Бот уже в этом звонке» до перезапуска.
                if not msg.startswith("ffmpeg:") and st.state == "recording":
                    self._set(st, "recording", msg)

            self._set(st, "recording", "Бот заходит на встречу…")
            # Поля «Видео встречи»/«Протокол встречи» у повторяющейся задачи
            # несут ссылки ПРОШЛОГО проведения — их чистит ночной проход
            # (_nightly_wipe), а не старт записи. Так у людей остаётся весь день
            # на прошлые ссылки, и они не пропадают в момент, когда встреча
            # только началась.
            # Human-readable file name: «ДД.ММ.ГГГГ, ЧЧ:ММ. - <название задачи>»
            # in the workspace timezone.
            when = (st.start.astimezone(self._tz(cfg)) if st.start
                    else datetime.now(self._tz(cfg)))
            stamp = when.strftime("%d.%m.%Y, %H:%M")
            bad = '\\/*?"<>|'  # strip chars that break file names / URLs (keep the time ':')
            title = "".join(" " if c in bad else c for c in str(st.title or "")).strip()
            title = " ".join(title.split())[:80]
            fname = f"{stamp}. - {title}.mp4" if title else f"{stamp}.mp4"
            rec_dir = security.user_dir(user) / "recordings"  # private per-user
            rec_dir.mkdir(parents=True, exist_ok=True)
            out = str(_free_path(rec_dir / fname))
            # Persist the file path BEFORE recording starts: if the process dies
            # mid-meeting, the restart scan (_resume_pending) finds the fMP4 by
            # this path and queues it for processing instead of losing it.
            st.out_path = out
            snapshots.save(st)

            # Д10: pseudo-live transcript — the fMP4 is readable while being
            # written, so a side thread transcribes the growing tail every N min.
            if cfg.get("live_transcribe", True) and cfg.get("do_transcribe", True):
                threading.Thread(target=self._live_transcribe_loop,
                                 args=(st, cfg, out), daemon=True,
                                 name="vtx-live-transcribe").start()

            res = recorder.record_meeting(
                st.url, out, cfg, on_log=log, slot=slot,
                should_stop=lambda: (self._stop.is_set()
                                     or st.stop_flag
                                     or (not manual
                                         and not auto_settings.get(user, "enabled"))))
            # Слот освобождаем СРАЗУ после записи: дальше идут выгрузка в
            # облако и запись полей Weeek с ретраями — это минуты, а экран и
            # звуковой приёмник для них не нужны. Раньше слот держался до
            # конца, и параллельные встречи не получали свободного слота, а
            # через десять минут помечались «пропущена».
            recorder.release_slot(slot)
            slot = None
            if not res.get("ok"):
                self._set(st, "error", res.get("error") or "Запись не удалась.")
                return
            out = res.get("path") or out
            st.out_path = out
            if res.get("audio_warning"):
                log("⚠ Во время встречи были периоды без звука — проверьте запись.")
                from . import notify
                notify.send(cfg, f"⚠ «{st.title}»: во время записи были периоды "
                                 "без звука — проверьте запись.")

            # Upload to the chosen cloud. Recordings must NOT live on this
            # server — the local file is only a staging copy, so a failed upload
            # retries here and then keeps retrying in the background until the
            # video lands in the cloud (see _late_upload).
            self._set(st, "uploading", "Выгружаю запись в облако…")
            up = delivery.upload_with_retry(out, cfg, log)
            if up.get("ok"):
                st.cloud_url = up.get("url")  # public share link (or None)
                st.upload_error = None
                if up.get("public_note"):
                    log(up["public_note"])
            else:
                st.upload_error = up.get("error") or "облако недоступно"
                log(f"Облако: {st.upload_error}")

            # Write the recording link into the task's «Видео встречи» custom field.
            field = (cfg.get("weeek_video_field") or "").strip()
            if cfg.get("weeek_set_video_field", True) and st.cloud_url and field:
                delivery.write_weeek_field(cfg.get("weeek_token"), st.task_id,
                                        field, st.cloud_url, log)

            # Keep the video ONLY where the UI points. If it was delivered
            # elsewhere (a remote cloud, or a local folder other than the staging
            # dir), the staging copy in data/recordings is redundant and must not
            # linger inside the server. We delete it after the pipeline is done
            # (transcription still needs to read it first).
            delivered_elsewhere = delivery.delivered_elsewhere(up, out)

            # Optionally hand off to the transcription / protocol pipeline.
            # Recording always happens; transcription and protocol are separate
            # toggles so the user records only what they need.
            do_transcribe = bool(cfg.get("do_transcribe", True))
            do_protocol = _protocol_wanted(cfg, user, st.title, log)
            job = None
            if do_transcribe:
                stage = ("Распознаю речь и собираю протокол…" if do_protocol
                         else "Распознаю речь…")
                self._set(st, "transcribing", stage)
                deliver = bool(do_protocol and cfg.get("upload_protocol", True))
                from ..analyze import preset_for_title
                preset_cfg = str(cfg.get("analyze_preset") or "auto")
                preset = (preset_for_title(st.title) if preset_cfg == "auto"
                          else preset_cfg)
                job = store.create(
                    filename=Path(out).name, audio_path=out,
                    language=config.DEFAULT_LANGUAGE, diarize=False,
                    analyze=do_protocol,
                    # Delivery (cloud upload + Weeek link) lives IN the job: it
                    # survives service restarts and re-runs on «Пересобрать»,
                    # unlike a waiter thread of this process.
                    deliver_protocol_cloud=deliver,
                    deliver_weeek_task=str(st.task_id) if deliver else "",
                    provider=cfg.get("analyze_provider") or "auto",
                    capture_screen=bool(cfg.get("ocr_screen", True)),
                    # Telemost recordings always show the active speaker (green
                    # tile) + names, so read WHO spoke straight from the video.
                    identify_speakers=True,  # Д7: спикеры с видео — безусловно для записей бота
                    # Meeting title → matches the user's per-project AI context.
                    context_hint=str(st.title or ""),
                    user_notes=st.live_notes,
                    preset=preset,
                    delete_audio_when_done=delivered_elsewhere,
                    owner=user)
                st.job_id = job.id
                snapshots.save(st)  # remember the job across restarts
                # Once the protocol (.docx) is built, upload it to the same cloud
                # and link it in Weeek — in a background waiter so the recording
                # lock isn't held during transcription.
                if do_protocol and cfg.get("upload_protocol", True):
                    threading.Thread(
                        target=self._await_and_upload_protocol,
                        args=(st, job.id, cfg),
                        daemon=True).start()
            elif delivered_elsewhere:
                # No transcription — nothing else needs the file; drop it now.
                for p in (out, out + ".ffmpeg.log"):
                    try:
                        Path(p).unlink(missing_ok=True)
                    except OSError:
                        pass

            # The cloud didn't take the video: keep retrying in the background
            # until it does — recordings must not be stored on this server.
            if not up.get("ok"):
                threading.Thread(target=self._late_upload,
                                 args=(user, st, cfg, out),
                                 daemon=True, name="vtx-late-upload").start()

            # Post the cloud link back to Weeek (protocol is produced later).
            if cfg.get("post_back_to_weeek") and st.cloud_url:
                tail = (f"\nРаспознавание/протокол: job {job.id}." if job
                        else "\nРаспознавание отключено — только запись.")
                ok = weeek.add_comment(
                    cfg.get("weeek_token"), st.task_id,
                    f"🎥 Запись встречи: {st.cloud_url}{tail}")
                log(f"Комментарий в Weeek: {'ок' if ok else 'не удалось'}")

            where = ("в облаке" if st.cloud_url else
                     "пока локально — облако не приняло, выгружаю в фоне"
                     if st.upload_error else "локально")
            if not do_transcribe:
                self._set(st, "done", f"Готово. Запись {where} (распознавание отключено).")
            else:
                # Recording is finished; transcription + protocol run in the job
                # worker. The live stage (video recognition → protocol) is shown
                # in status() straight from the job's own state.
                st.do_protocol = bool(do_protocol)
                self._set(st, "transcribing",
                          f"Запись {where}. Распознаю видео… — job {job.id}")
        except Exception as e:  # noqa: BLE001
            self._set(st, "error", f"Сбой: {e}")
        finally:
            recorder.release_slot(slot)

    # -- Weeek custom-field writes (retried; stale links wiped) --------------

    def _late_upload(self, user: str, st: MeetingState, cfg: dict, out: str) -> None:
        """The cloud refused the video at recording time. Keep retrying (every
        5 min, up to 4 h): on success fill the Weeek field/comment exactly like
        the normal path would have, then get rid of the local copy — videos are
        not stored on this server."""
        deadline = time.time() + 4 * 3600
        while time.time() < deadline and not self._stop.is_set():
            time.sleep(300)
            if not Path(out).exists():
                return  # purged (retention) — nothing left to deliver
            up = delivery.upload_with_retry(out, cfg, lambda *_: None, attempts=1)
            if not up.get("ok"):
                st.upload_error = up.get("error") or "облако недоступно"
                continue
            st.cloud_url = up.get("url")
            st.upload_error = None
            token = cfg.get("weeek_token")
            field = (cfg.get("weeek_video_field") or "").strip()
            if cfg.get("weeek_set_video_field", True) and st.cloud_url and field:
                delivery.write_weeek_field(token, st.task_id, field, st.cloud_url,
                                        lambda *_: None)
            if cfg.get("post_back_to_weeek") and st.cloud_url:
                weeek.add_comment(token, st.task_id,
                                  f"🎥 Запись встречи: {st.cloud_url}")
            snapshots.save(st)  # the late-delivered cloud link survives restarts
            if delivery.delivered_elsewhere(up, out):
                job = store.get(st.job_id) if st.job_id else None
                if job is not None and job.status != "done":
                    # Recognition still reads the file — it deletes it on finish.
                    # Задачи со статусом error/cancelled тоже держим: именно из
                    # них человек жмёт «Повторить», а без исходника повтор
                    # невозможен («файл больше недоступен»). Ровно в таком
                    # статусе задачу оставляет перезапуск сервиса. Диск не течёт:
                    # незавершённые записи всё равно убирает retention.
                    job.delete_audio_when_done = True
                else:
                    for p in (out, out + ".ffmpeg.log"):
                        try:
                            Path(p).unlink(missing_ok=True)
                        except OSError:
                            pass
            return

    def _await_and_upload_protocol(self, st: MeetingState, job_id: str,
                                   cfg: dict) -> None:
        """Wait for the job to finish and REPORT the protocol outcome. The
        upload + Weeek attach itself is done by the job (deliver_* flags), so it
        survives restarts and re-runs on «Пересобрать»; this waiter only makes
        sure the result is never silent: a missing «Протокол встречи» field is
        always explained by a Weeek comment, and the meeting snapshot is closed
        (done/error) instead of hanging in «распознаю» forever."""
        task_id = st.task_id
        token = cfg.get("weeek_token")

        def log(msg: str) -> None:
            st.logs.append(str(msg))
            _LOG.info("встреча %s: %s", st.task_id, msg)

        def report(msg: str) -> None:
            """Log + (best-effort) leave a comment in the task, so the user sees
            why the protocol didn't attach."""
            log(msg)
            if token and cfg.get("post_back_to_weeek", True):
                try:
                    weeek.add_comment(token, task_id, msg)
                except Exception:
                    _LOG.warning("Комментарий в задачу %s не ушёл: %s",
                                task_id, msg, exc_info=True)

        # Ждём, пока задача ЖИВА. Двухчасовой срок объявлял провал даже тогда,
        # когда задача честно стояла в очереди: воркер распознавания один, и
        # три встречи подряд легко отодвигают последнюю на несколько часов.
        # В Weeek уходил комментарий «протокол не собрался», в Telegram —
        # тревога, карточка краснела, а через час протокол благополучно
        # появлялся. Срок теперь работает только против ЗАВИСШЕЙ задачи:
        # пока статус меняется или она в очереди, ждём дальше.
        alive_states = ("queued", "running", "paused", "analyzing")
        deadline = time.time() + 2 * 3600
        while True:
            job = store.get(job_id)
            if job is None:
                report("⚠ Протокол не прикреплён: задача распознавания пропала "
                       "(сервис перезапускался во время обработки).")
                return
            if job.status in ("done", "error", "cancelled"):
                break
            if job.status in alive_states:
                deadline = time.time() + 2 * 3600   # жива — срок сдвигаем
            elif time.time() > deadline:
                break
            time.sleep(5)

        job = store.get(job_id)
        if not job:
            return
        where = "в облаке" if st.cloud_url else "локально"
        provs = getattr(job, "docx_providers", None)
        if job.status != "done" or not provs:
            why = (job.analysis_error or "").strip().splitlines()
            reason = why[-1] if why else f"статус задачи «{job.status}»"
            report(f"⚠ Протокол не собрался, поэтому не прикреплён. Причина: "
                   f"{reason}. Откройте задачу распознавания и нажмите "
                   f"«Пересобрать» — готовый протокол прикрепится сам.")
            from . import notify
            notify.send(cfg, f"⚠ Протокол «{st.title or task_id}» не собрался: "
                             f"{reason}. Нажмите «Пересобрать» в приложении.")
            self._set(st, "error",
                      f"Протокол не собрался — job {job_id}. «Пересобрать» "
                      f"допоставит его в Weeek. Запись {where}.")
            return
        err = (getattr(job, "delivery_error", "") or "").strip()
        if err:
            report(f"⚠ Протокол готов, но прикрепить не удалось: {err}")
            self._set(st, "error",
                      f"Протокол готов, но не прикреплён: {err} — job {job_id}.")
            return
        log("Протокол прикреплён к задаче Weeek ✓")
        from . import notify
        proto_url = getattr(job, "protocol_cloud_url", None)
        notify.send(cfg, f"📄 Протокол готов: «{st.title or task_id}» — "
                         "прикреплён к задаче Weeek."
                         + (f"\n{proto_url}" if proto_url else ""))
        self._set(st, "done",
                  f"Готово. Запись {where}, протокол прикреплён — job {job_id}.")

    def _enrich_from_job(self, m: dict) -> None:
        """Overlay the live transcription/protocol stage onto a meeting dict,
        read from its job's own status (recognition → protocol → done)."""
        jid = m.get("job_id")
        if not jid:
            return
        job = store.get(jid)
        if job is None:
            return
        # Ссылка на протокол в облаке — чтобы окно «Прикрепить» подставляло её
        # само. Раньше поле оставалось пустым, кнопка была заблокирована, и это
        # выглядело как «нажал — ничего не произошло».
        if getattr(job, "protocol_cloud_url", None):
            m["protocol_url"] = job.protocol_cloud_url
        where = ("в облаке" if m.get("cloud_url") else
                 "⚠ пока локально — выгружаю в облако повторно"
                 if m.get("upload_error") else "локально")
        dp = m.get("do_protocol")
        st = job.status
        if st in ("queued", "running", "paused"):
            pct = int((job.progress or 0) * 100)
            tail = f" {pct}%" if pct else ""
            m["state"] = "transcribing"
            m["detail"] = f"Распознаю видео…{tail} — запись {where}, job {jid}"
        elif st == "analyzing":
            m["state"] = "analyzing"
            m["detail"] = f"Генерирую протокол… — запись {where}, job {jid}"
        elif st == "done":
            m["state"] = "done"
            m["detail"] = (f"Готово. Запись {where}, распознавание+протокол — job {jid}."
                           if dp else
                           f"Готово. Запись {where}, распознавание — job {jid} (без протокола).")
        elif st == "error":
            m["state"] = "error"
            m["detail"] = f"Ошибка распознавания — job {jid}."
        elif st == "cancelled":
            m["state"] = "done"
            m["detail"] = f"Распознавание отменено — запись {where}, job {jid}."

    # -- meeting-state persistence (survives restarts) -----------------------
    # Deploys restart the process, wiping the in-memory states; these snapshots
    # keep the outcome of already-handled meetings, so they don't come back as
    # «пропущена» after every `docker compose up -d --build`.
    _PERSIST_STATES = {"recording", "uploading", "transcribing", "analyzing",
                       "done", "error"}


    def _revive_recorded_slot(self, user: str, task_id: str,
                              near: datetime, snaps: dict) -> None:
        """Rebuild the RECORDED slot of this task (within the reslot window of
        `near`) from its snapshot, so the meetings list shows «Готово. Запись…»
        instead of nothing after the slot vanished from Weeek (время в задаче
        поменяли задним числом)."""
        for sk, sv in snaps.items():
            parts = sk.split(":", 2)
            if (len(parts) != 3 or parts[1] != task_id
                    or sk in self._states
                    or sv.get("state") not in (
                        "recording", "uploading", "transcribing",
                        "analyzing", "done", "error")):
                continue
            try:
                start = datetime.fromisoformat(parts[2])
            except ValueError:
                continue
            if abs((near - start).total_seconds()) > _RESLOT_WINDOW_SEC:
                continue
            title = Path(sv["out_path"]).stem if sv.get("out_path") else ""
            self._states[sk] = MeetingState(
                key=sk, task_id=task_id, title=title, url="", start=start,
                owner=user, state=sv.get("state") or "done",
                detail=sv.get("detail") or "", job_id=sv.get("job_id"),
                cloud_url=sv.get("cloud_url"), out_path=sv.get("out_path"),
                live_notes=str(sv.get("live_notes") or ""),
                do_protocol=bool(sv.get("do_protocol")))
            return

    def _resume_pending(self) -> None:
        """After a restart: meetings whose snapshot froze mid-pipeline still have
        a job in the store — re-arm their delivery. redeliver() puts the deliver
        flags (back) onto the job: a finished protocol attaches right now, a
        running job attaches at completion, and even a failed one attaches later
        when the user hits «Пересобрать». A fresh waiter thread then closes the
        snapshot and explains any failure in Weeek."""
        pending = ("recording", "uploading", "transcribing", "analyzing")
        cutoff = time.time() - 48 * 3600
        for team in security.list_teams():
            try:
                cfg = auto_settings.load(team)
                if not cfg.get("weeek_token"):
                    continue
                snaps = snapshots.load(team)
            except Exception:  # noqa: BLE001 — one broken team must not stop the rest
                continue
            for key, snap in snaps.items():
                jid = snap.get("job_id")
                if (snap.get("state") not in pending
                        or (snap.get("saved_at") or 0) < cutoff):
                    continue
                parts = key.split(":", 2)
                task_id = parts[1] if len(parts) > 1 else None
                if not task_id:
                    continue
                try:
                    start = (datetime.fromisoformat(parts[2])
                             if len(parts) > 2 else None)
                except ValueError:
                    start = None

                # Д8: the process died DURING the recording — no job yet, but
                # the fMP4 on disk is readable up to the crash. Queue it for the
                # normal transcribe→protocol→deliver pipeline.
                out_path = snap.get("out_path")
                if not jid and out_path and Path(out_path).exists() \
                        and Path(out_path).stat().st_size > 0:
                    st = MeetingState(
                        key=key, task_id=task_id, title=Path(out_path).stem,
                        url="", start=start, owner=team, state="transcribing",
                        detail="", out_path=out_path,
                        cloud_url=snap.get("cloud_url"),
                        live_notes=str(snap.get("live_notes") or ""),
                        do_protocol=bool(cfg.get("do_protocol", True)))
                    with self._lock:
                        st = self._states.setdefault(key, st)
                    self._finalize_orphan(st, cfg, out_path)
                    continue

                if not jid:
                    continue
                job = store.get(jid)
                if job is None:
                    continue    # job already purged by retention — nothing left
                # Д8: the restart interrupted the RECOGNITION itself (worker died
                # mid-transcription). The source file is still there — requeue it
                # instead of leaving an «error» card that needs a manual retry.
                if (job.status == "error"
                        and "перезапущен" in (job.error or "")
                        and job.audio_path and Path(job.audio_path).exists()):
                    try:
                        store.retry(jid)
                    except Exception:  # noqa: BLE001
                        _LOG.warning("Не удалось перезапустить задачу %s после "
                                    "рестарта", jid, exc_info=True)
                st = MeetingState(
                    key=key, task_id=task_id, title=Path(job.filename).stem,
                    url="", start=start, owner=team,
                    state=snap.get("state") or "transcribing",
                    detail=snap.get("detail") or "", job_id=jid,
                    cloud_url=snap.get("cloud_url"),
                    out_path=snap.get("out_path"),
                    live_notes=str(snap.get("live_notes") or ""),
                    do_protocol=bool(snap.get("do_protocol")))
                with self._lock:
                    st = self._states.setdefault(key, st)
                if not (st.do_protocol and cfg.get("upload_protocol", True)):
                    continue
                try:
                    store.redeliver(jid, weeek_task=task_id, cloud=True)
                except Exception:  # noqa: BLE001
                    _LOG.warning("Повторная доставка задачи %s не удалась", jid,
                                exc_info=True)
                threading.Thread(
                    target=self._await_and_upload_protocol,
                    args=(st, jid, cfg),
                    daemon=True).start()

    def _finalize_orphan(self, st: MeetingState, cfg: dict, out: str) -> None:
        """A recording the crash orphaned: run the same post-recording pipeline
        the normal path would have — upload the video, queue transcription with
        delivery flags, start the waiter and the Weeek link write."""
        do_transcribe = bool(cfg.get("do_transcribe", True))

        def log(msg: str) -> None:
            st.logs.append(str(msg))
            _LOG.info("встреча %s: %s", st.task_id, msg)

        do_protocol = _protocol_wanted(cfg, st.owner, st.title, log)
        deliver = bool(do_protocol and cfg.get("upload_protocol", True))
        from ..analyze import preset_for_title
        preset_cfg = str(cfg.get("analyze_preset") or "auto")
        preset = preset_for_title(st.title) if preset_cfg == "auto" else preset_cfg

        log("Запись прервана перезапуском сервиса — файл цел, дообрабатываю.")
        if not st.cloud_url:
            up = delivery.upload_with_retry(out, cfg, log, attempts=1)
            if up.get("ok"):
                st.cloud_url = up.get("url")
                field = (cfg.get("weeek_video_field") or "").strip()
                if cfg.get("weeek_set_video_field", True) and st.cloud_url and field:
                    delivery.write_weeek_field(cfg.get("weeek_token"), st.task_id,
                                            field, st.cloud_url, log)
            else:
                st.upload_error = up.get("error") or "облако недоступно"
                threading.Thread(target=self._late_upload,
                                 args=(st.owner, st, cfg, out),
                                 daemon=True, name="vtx-late-upload").start()
        if not do_transcribe:
            self._set(st, "done", "Готово после перезапуска: запись "
                      + ("в облаке" if st.cloud_url else "локально")
                      + " (распознавание отключено).")
            return
        job = store.create(
            filename=Path(out).name, audio_path=out,
            language=config.DEFAULT_LANGUAGE, diarize=False,
            analyze=do_protocol,
            deliver_protocol_cloud=deliver,
            deliver_weeek_task=str(st.task_id) if deliver else "",
            provider=cfg.get("analyze_provider") or "auto",
            capture_screen=bool(cfg.get("ocr_screen", True)),
            identify_speakers=True,  # Д7: спикеры с видео — безусловно для записей бота
            context_hint=str(st.title or ""),
            user_notes=st.live_notes,
            preset=preset,
            owner=st.owner)
        st.job_id = job.id
        self._set(st, "transcribing",
                  f"Восстановлено после перезапуска. Распознаю видео… — job {job.id}")
        if deliver:
            threading.Thread(
                target=self._await_and_upload_protocol,
                args=(st, job.id, cfg),
                daemon=True).start()

    def _restore_snapshot(self, st: MeetingState) -> None:
        """Fill a freshly discovered MeetingState from its saved outcome."""
        snap = snapshots.load(st.owner).get(st.key)
        if not snap:
            return
        st.job_id = snap.get("job_id")
        st.cloud_url = snap.get("cloud_url")
        st.do_protocol = bool(snap.get("do_protocol"))
        state = snap.get("state")
        if state in ("done", "error"):
            st.state, st.detail = state, snap.get("detail", "")
        else:
            # The restart caught it mid-pipeline. The recording itself finished
            # or not — judge by whether the cloud already has the video.
            st.state = "done" if st.cloud_url else "error"
            st.detail = ("Прервано перезапуском сервиса — запись в облаке."
                         if st.cloud_url else
                         "Прервано перезапуском сервиса.")

    def _set(self, st: MeetingState, state: str, detail: str) -> None:
        with self._lock:
            st.state, st.detail = state, detail
        if state in self._PERSIST_STATES:
            snapshots.save(st)

    # -- Д10: live transcript during the recording ---------------------------
    # Порядок предпочтения карточек одной и той же задачи Weeek. У повторяющейся
    # встречи задача ОДНА (task_id один), а карточек столько, сколько было и
    # будет её проведений: ключ — «команда:задача:время начала». Возвращать
    # первую попавшуюся нельзя: это самая ранняя известная, то есть прошлая.
    # Заметки участника (единственный ручной вклад в протокол) уезжали в
    # прошлонедельную карточку, а окно живой расшифровки показывало прошлый
    # разговор — и это выглядело как «бот пишет не ту встречу».
    _ACTIVE_STATES = ("recording", "uploading", "transcribing", "analyzing")

    def _find_state(self, user: str, task_id) -> "MeetingState | None":
        team = security.team_of(user)
        with self._lock:
            mine = [st for st in self._states.values()
                    if st.owner == team and str(st.task_id) == str(task_id)]
        if not mine:
            return None
        # 1) та, что идёт прямо сейчас — заметки пишут во время встречи;
        for st in mine:
            if st.state in self._ACTIVE_STATES:
                return st
        # 2) иначе самая поздняя из УЖЕ НАЧАВШИХСЯ. Weeek передвигает дату
        #    повторяющейся задачи сразу после встречи, и карточка следующей
        #    недели появляется раньше, чем человек допишет заметки: «самая
        #    поздняя вообще» отдавала будущий слот, и заметки уезжали в протокол
        #    следующего проведения;
        now = datetime.now(timezone.utc)
        dated = [st for st in mine if st.start is not None]
        started = [st for st in dated if st.start <= now]
        if started:
            return max(started, key=lambda st: st.start)
        # 3) ничего не начиналось — ближайшая будущая;
        if dated:
            return min(dated, key=lambda st: st.start)
        # 4) на крайний случай — хоть какая-то (у встреч без времени).
        return mine[0]

    def live_view(self, user: str, task_id) -> dict:
        """What the «живая расшифровка» modal shows: the growing live text while
        recording, then the job's final transcript once it exists."""
        st = self._find_state(user, task_id)
        if st is None:
            return {"ok": False, "error": "Встреча не найдена."}
        from ..jobs import store
        if st.job_id:
            job = store.get(st.job_id)
            if job is not None:
                txt = store.result_path(job.id, "txt")
                if txt.exists():
                    return {"ok": True, "final": True, "recording": False,
                            "text": txt.read_text(encoding="utf-8")}
                partial = store.partial(job.id)
                if partial:
                    return {"ok": True, "final": False, "recording": False,
                            "text": "\n".join(p.get("text", "") for p in partial)}
        return {"ok": True, "final": False,
                "recording": st.state == "recording",
                "updated_at": st.live_updated_at, "text": st.live_text}

    def set_meeting_notes(self, user: str, task_id, notes: str) -> dict:
        """Notes typed during/after the meeting. Before the job exists they live
        on the meeting state (and its snapshot); once the job is there, they go
        onto it (protocol skeleton, Д6)."""
        st = self._find_state(user, task_id)
        if st is None:
            return {"ok": False, "error": "Встреча не найдена."}
        st.live_notes = (notes or "").strip()[:20000]
        if st.job_id:
            from ..jobs import store
            try:
                store.set_notes(st.job_id, st.live_notes)
            except KeyError:
                pass
        snapshots.save(st)
        return {"ok": True, "has_notes": bool(st.live_notes)}

    def get_meeting_notes(self, user: str, task_id) -> dict:
        st = self._find_state(user, task_id)
        if st is None:
            return {"ok": False, "error": "Встреча не найдена."}
        return {"ok": True, "notes": st.live_notes}

    def _live_transcribe_loop(self, st: MeetingState, cfg: dict, out: str) -> None:
        """Every N minutes transcribe the NEW tail of the growing fMP4, so the
        meeting page shows text while people are still talking. The final
        full-file transcription (better context, speakers) replaces this."""
        interval = max(60, int(cfg.get("live_interval_min", 5)) * 60)
        lang = config.DEFAULT_LANGUAGE
        processed = 0.0
        ffmpeg = "ffmpeg"

        def fmt(sec: float) -> str:
            m, s = divmod(int(sec), 60)
            h, m = divmod(m, 60)
            return f"{h:d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"

        while not self._stop.is_set() and st.state == "recording":
            if self._stop.wait(interval):
                return
            if st.state != "recording" or not Path(out).exists():
                return
            try:
                import subprocess
                probe = subprocess.run(
                    [ffmpeg.replace("ffmpeg", "ffprobe"), "-v", "error",
                     "-show_entries", "format=duration", "-of", "csv=p=0", out],
                    capture_output=True, text=True, timeout=30)
                dur = float((probe.stdout or "0").strip() or 0)
            except Exception:
                continue
            end = dur - 3.0     # don't read the fragment still being written
            if end - processed < 30:
                continue        # nothing meaningful accrued yet
            wav = f"{out}.live.wav"
            try:
                cut = subprocess.run(
                    [ffmpeg, "-y", "-v", "error", "-ss", str(processed),
                     "-to", str(end), "-i", out, "-vn", "-ac", "1",
                     "-ar", "16000", wav], capture_output=True, timeout=120)
                if cut.returncode != 0 or not Path(wav).exists():
                    continue
                from ..transcribe import transcribe_file
                got = transcribe_file(wav, language=lang, nonblocking=True)
                if got is None:
                    continue    # model busy with a real job — skip this tick
                segs, _meta = got
                lines = [f"[{fmt(processed + s.start)}] {s.text}" for s in segs]
                if lines:
                    st.live_text = (st.live_text + "\n" + "\n".join(lines)).strip()
                    st.live_updated_at = time.time()
                processed = end
            except Exception:   # live text is best-effort, never break recording
                continue
            finally:
                try:
                    Path(wav).unlink(missing_ok=True)
                except OSError:
                    pass

    def stop_recording(self, user: str | None = None, task_id=None) -> dict:
        """Stop recording(s) in progress. With `task_id` — just that meeting;
        otherwise all of the team's active recordings."""
        if user is not None:
            user = security.team_of(user)
        n = 0
        with self._lock:
            for st in self._states.values():
                if st.state != "recording":
                    continue
                if user is not None and st.owner != user:
                    continue
                if task_id is not None and str(st.task_id) != str(task_id):
                    continue
                st.stop_flag = True
                n += 1
        if not n:
            return {"ok": False, "error": "Сейчас запись не идёт."}
        return {"ok": True, "detail": f"Останавливаю запись ({n})…"}

    def set_decision(self, user: str, task_id: str, record) -> dict:
        """Record/skip a specific meeting for the team (override the filters)."""
        user = security.team_of(user)
        cfg = auto_settings.load(user)
        decisions = dict(cfg.get("rec_decisions") or {})
        if record is None:
            decisions.pop(str(task_id), None)
        else:
            decisions[str(task_id)] = bool(record)
        auto_settings.save(user, {"rec_decisions": decisions})
        # If we'd already skipped it, allow a re-evaluation on the next tick.
        with self._lock:
            for st in self._states.values():
                if (st.owner == user and str(st.task_id) == str(task_id)
                        and st.state in ("skipped", "missed")):
                    if st.start is not None and st.start.timestamp() >= self._boot_time:
                        st.state, st.detail = "scheduled", ""
        return {"ok": True, "task_id": str(task_id), "record": record}

    def run_now(self, user: str, task_id: str) -> dict:
        """Manually trigger recording for one of the team's meetings."""
        user = security.team_of(user)
        with self._lock:
            st = next((s for s in self._states.values()
                       if s.owner == user and str(s.task_id) == str(task_id)), None)
        if not st:
            return {"ok": False, "error": "Встреча не найдена (сначала опрос Weeek)."}
        # Имя файла записи детерминировано (дата, время и название задачи),
        # поэтому повторный заход открыл бы ffmpeg-ом ТОТ ЖЕ файл и затёр
        # готовую запись. У красной карточки интерфейс предлагал «Подключиться»
        # даже тогда, когда упало распознавание или сборка протокола, а запись
        # была цела — один клик уничтожал единственную локальную копию.
        if st.recording_exists():
            return {"ok": False, "error":
                    "У этой встречи уже есть запись — повторный заход затёр бы "
                    "её. Нужен протокол — нажмите «Пересобрать». Нужна новая "
                    "запись — сначала удалите старую с сервера."}
        # Слот берём ДО проверки, чтобы проверка-и-захват состояния прошли под
        # одним локом. Раньше проверка «уже записывается» и присвоение
        # state="recording" стояли в РАЗНЫХ блоках лока: между ними успевал
        # вклиниться планировщик, и на встречу заходили два бота.
        slot = recorder.acquire_slot()
        if slot is None:
            return {"ok": False, "error": f"Все слоты записи заняты "
                    f"(до {recorder.MAX_SLOTS} одновременно). Попробуйте позже."}
        with self._lock:
            if st.state == "recording":
                recorder.release_slot(slot)
                return {"ok": False, "error": "Эта встреча уже записывается."}
            if st.url and any(room_key(s.url) == room_key(st.url)
                              and s.state == "recording" and s.key != st.key
                              for s in self._states.values() if s.owner == user):
                recorder.release_slot(slot)
                return {"ok": False, "error": "Бот уже в этом звонке — эта "
                        "ссылка сейчас записывается. Второй бот в ту же комнату "
                        "ничего не добавит; ссылки можно прикрепить к задаче "
                        "вручную после записи."}
            st.state, st.detail, st.stop_flag = "recording", "Бот заходит на встречу…", False
        threading.Thread(target=self._run, args=(st, slot, True),
                         daemon=True).start()
        return {"ok": True, "detail": "Запись запущена."}


# Imported here (not at top) to avoid a heavy import cycle at module load.
from ..jobs import store  # noqa: E402

scheduler = Scheduler()
