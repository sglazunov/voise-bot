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

import os
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .. import config, security
from . import clouds, recorder, settings as auto_settings, weeek

# How late after start we'll still auto-join (avoids joining long-finished
# meetings on the first poll after startup).
_LATE_GRACE_SEC = 10 * 60
_TICK_SEC = 15  # loop granularity; actual Weeek polling honours poll_interval_sec


@dataclass
class MeetingState:
    key: str
    task_id: Any
    title: str
    url: str
    start: datetime | None
    owner: str = ""                   # which login this meeting belongs to
    state: str = "scheduled"          # scheduled|no_time|missed|recording|uploading|transcribing|done|error
    detail: str = ""
    job_id: str | None = None
    cloud_url: str | None = None
    stop_flag: bool = False           # manual "stop this recording"
    logs: list = field(default_factory=list)

    def public(self) -> dict:
        return {"task_id": self.task_id, "title": self.title, "url": self.url,
                "start": self.start.isoformat() if self.start else None,
                "state": self.state, "detail": self.detail,
                "job_id": self.job_id, "cloud_url": self.cloud_url}


class Scheduler:
    def __init__(self) -> None:
        self._states: dict[str, MeetingState] = {}
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._last_poll: dict[str, float] = {}  # per-user last Weeek poll
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

    def status(self, user: str) -> dict:
        cfg = auto_settings.load(user)
        with self._lock:
            meetings = [s.public() for s in sorted(
                (s for s in self._states.values() if s.owner == user),
                key=lambda s: (s.start is None, s.start or datetime.max.replace(
                    tzinfo=timezone.utc)))]
        # Don't let old "missed" meetings pile up: keep only the single most
        # recent one, plus everything else (upcoming / in-progress / done).
        missed = [m for m in meetings if m.get("state") == "missed"]
        if len(missed) > 1:
            latest = max(missed, key=lambda m: m.get("start") or "")
            meetings = [m for m in meetings
                        if m.get("state") != "missed" or m is latest]
        active = recorder.active_recordings()
        return {"running": bool(self._thread and self._thread.is_alive()),
                "enabled": bool(cfg.get("enabled")),
                "recording": active > 0,
                "active": active, "max_parallel": recorder.MAX_SLOTS,
                "last_poll": self._last_poll.get(user, 0.0), "meetings": meetings}

    # -- main loop ----------------------------------------------------------
    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                # Run automation independently for each registered user, under
                # their own (decrypted) tokens and settings — full isolation.
                for user in security.list_users():
                    try:
                        cfg = auto_settings.load(user)
                        if not (cfg.get("enabled") and cfg.get("weeek_token")):
                            continue
                        interval = max(30, int(cfg.get("poll_interval_sec", 120)))
                        if time.time() - self._last_poll.get(user, 0.0) >= interval:
                            self._poll(user, cfg)
                            self._last_poll[user] = time.time()
                        self._maybe_trigger(user, cfg)
                    except Exception:  # one user's failure must not stop others
                        pass
            except Exception:  # never let the loop die
                pass
            self._stop.wait(_TICK_SEC)

    def _tz(self, cfg: dict):
        try:
            from zoneinfo import ZoneInfo
            return ZoneInfo(cfg.get("timezone") or "Europe/Moscow")
        except Exception:
            return timezone.utc

    def _poll(self, user: str, cfg: dict) -> None:
        meetings = weeek.upcoming_meetings(
            cfg.get("weeek_token"), cfg.get("weeek_project_id"), self._tz(cfg))
        with self._lock:
            for m in meetings:
                key = f"{user}:{m.task_id}:{m.start.isoformat() if m.start else 'no-time'}"
                st = self._states.get(key)
                if st is None:
                    st = MeetingState(key=key, task_id=m.task_id, title=m.title,
                                      url=m.url, start=m.start, owner=user)
                    if m.start is None:
                        st.state, st.detail = "no_time", "В задаче не указано время встречи."
                    self._states[key] = st
                elif st.state == "scheduled":
                    st.url, st.title, st.start = m.url, m.title, m.start

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
        for _, chosen in candidates:
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
            threading.Thread(target=self._run, args=(chosen, slot), daemon=True).start()

    # -- per-meeting pipeline ----------------------------------------------
    def _run(self, st: MeetingState, slot) -> None:
        try:
            user = st.owner
            cfg = auto_settings.load(user)

            def log(msg: str) -> None:
                msg = str(msg)
                st.logs.append(msg)
                # Surface live progress on the card instead of a frozen
                # "joining…" line (skip the very verbose ffmpeg command dump).
                if not msg.startswith("ffmpeg:"):
                    self._set(st, "recording", msg)

            self._set(st, "recording", "Бот заходит на встречу…")
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
            out = str(rec_dir / fname)

            res = recorder.record_meeting(
                st.url, out, cfg, on_log=log, slot=slot,
                should_stop=lambda: (self._stop.is_set()
                                     or st.stop_flag
                                     or not auto_settings.get(user, "enabled")))
            if not res.get("ok"):
                self._set(st, "error", res.get("error") or "Запись не удалась.")
                return
            out = res.get("path") or out  # telemost mode may save .webm, not .mp4

            # Upload to the chosen cloud (best effort — failure isn't fatal).
            self._set(st, "uploading", "Выгружаю запись в облако…")
            up = clouds.upload(out, Path(out).name, cfg)
            if up.get("ok"):
                st.cloud_url = up.get("url")  # public share link (or None)
                if up.get("public_note"):
                    log(up["public_note"])
            else:
                log(f"Облако: {up.get('error')}")

            # Write the recording link into the task's «Видео встречи» custom field.
            field = (cfg.get("weeek_video_field") or "").strip()
            if cfg.get("weeek_set_video_field", True) and st.cloud_url and field:
                res = weeek.set_custom_field(cfg.get("weeek_token"), st.task_id,
                                             field, st.cloud_url)
                log(f"Поле «{field}» в Weeek: "
                    + ("заполнено ✓" if res.get("ok") else f"не удалось — {res.get('error')}"))

            # Keep the video ONLY where the UI points. If it was delivered
            # elsewhere (a remote cloud, or a local folder other than the staging
            # dir), the staging copy in data/recordings is redundant and must not
            # linger inside the server. We delete it after the pipeline is done
            # (transcription still needs to read it first).
            delivered_elsewhere = False
            if up.get("ok"):
                if up.get("backend") != "local":
                    delivered_elsewhere = True
                else:
                    try:
                        delivered_elsewhere = (Path(up.get("path") or "").resolve()
                                               != Path(out).resolve())
                    except OSError:
                        delivered_elsewhere = False

            # Optionally hand off to the transcription / protocol pipeline.
            # Recording always happens; transcription and protocol are separate
            # toggles so the user records only what they need.
            do_transcribe = bool(cfg.get("do_transcribe", True))
            do_protocol = bool(cfg.get("do_protocol", True))
            job = None
            if do_transcribe:
                stage = ("Распознаю речь и собираю протокол…" if do_protocol
                         else "Распознаю речь…")
                self._set(st, "transcribing", stage)
                job = store.create(
                    filename=Path(out).name, audio_path=out,
                    language=config.DEFAULT_LANGUAGE, diarize=False,
                    analyze=do_protocol,
                    provider=cfg.get("analyze_provider") or "auto",
                    capture_screen=bool(cfg.get("ocr_screen", True)),
                    # Telemost recordings always show the active speaker (green
                    # tile) + names, so read WHO spoke straight from the video.
                    identify_speakers=bool(cfg.get("identify_speakers", True)),
                    delete_audio_when_done=delivered_elsewhere,
                    owner=user)
                st.job_id = job.id
                # Once the protocol (.docx) is built, upload it to the same cloud
                # and link it in Weeek — in a background waiter so the recording
                # lock isn't held during transcription.
                if do_protocol and cfg.get("upload_protocol", True):
                    threading.Thread(
                        target=self._await_and_upload_protocol,
                        args=(user, st.task_id, job.id, cfg, Path(out).stem),
                        daemon=True).start()
            elif delivered_elsewhere:
                # No transcription — nothing else needs the file; drop it now.
                for p in (out, out + ".ffmpeg.log"):
                    try:
                        Path(p).unlink(missing_ok=True)
                    except OSError:
                        pass

            # Post the cloud link back to Weeek (protocol is produced later).
            if cfg.get("post_back_to_weeek") and st.cloud_url:
                tail = (f"\nРаспознавание/протокол: job {job.id}." if job
                        else "\nРаспознавание отключено — только запись.")
                ok = weeek.add_comment(
                    cfg.get("weeek_token"), st.task_id,
                    f"🎥 Запись встречи: {st.cloud_url}{tail}")
                log(f"Комментарий в Weeek: {'ок' if ok else 'не удалось'}")

            where = "в облаке" if st.cloud_url else "локально"
            if not do_transcribe:
                detail = f"Готово. Запись {where} (распознавание отключено)."
            elif not do_protocol:
                detail = f"Готово. Запись {where}, распознавание — job {job.id} (без протокола)."
            else:
                detail = f"Готово. Запись {where}, распознавание+протокол — job {job.id}."
            self._set(st, "done", detail)
        except Exception as e:  # noqa: BLE001
            self._set(st, "error", f"Сбой: {e}")
        finally:
            recorder.release_slot(slot)

    def _await_and_upload_protocol(self, user: str, task_id, job_id: str,
                                   cfg: dict, base_name: str) -> None:
        """Wait for the job's protocol (.docx) to be generated, then upload it to
        the same cloud as the recording and write its link into the Weeek field.
        The .docx stays downloadable in the UI."""
        deadline = time.time() + 2 * 3600
        while time.time() < deadline:
            job = store.get(job_id)
            if job is None:
                return
            if job.status in ("done", "error", "cancelled"):
                break
            time.sleep(5)
        job = store.get(job_id)
        provs = getattr(job, "docx_providers", None) if job else None
        if not job or job.status != "done" or not provs:
            return
        docx = store.docx_path(job_id, provs[-1])
        if not docx.exists():
            return
        # Protocols go into their OWN cloud folder (separate from the recordings).
        up = clouds.upload(str(docx), f"{base_name} - протокол.docx", cfg,
                           folder=(cfg.get("protocol_folder") or "").strip() or None)
        if not (up.get("ok") and up.get("url")):
            return
        field = (cfg.get("weeek_protocol_field") or "").strip()
        if cfg.get("weeek_set_protocol_field", True) and field:
            weeek.set_custom_field(cfg.get("weeek_token"), task_id, field, up["url"])

    def _set(self, st: MeetingState, state: str, detail: str) -> None:
        with self._lock:
            st.state, st.detail = state, detail

    def stop_recording(self, user: str | None = None, task_id=None) -> dict:
        """Stop recording(s) in progress. With `task_id` — just that meeting;
        otherwise all of `user`'s active recordings."""
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
        """Record/skip a specific meeting for `user` (override the filters)."""
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
        """Manually trigger recording for one of `user`'s meetings."""
        with self._lock:
            st = next((s for s in self._states.values()
                       if s.owner == user and str(s.task_id) == str(task_id)), None)
        if not st:
            return {"ok": False, "error": "Встреча не найдена (сначала опрос Weeek)."}
        if st.state == "recording":
            return {"ok": False, "error": "Эта встреча уже записывается."}
        slot = recorder.acquire_slot()
        if slot is None:
            return {"ok": False, "error": f"Все слоты записи заняты "
                    f"(до {recorder.MAX_SLOTS} одновременно). Попробуйте позже."}
        # Claim before spawning so a concurrent tick can't double-launch.
        with self._lock:
            st.state, st.detail, st.stop_flag = "recording", "Бот заходит на встречу…", False
        threading.Thread(target=self._run, args=(st, slot), daemon=True).start()
        return {"ok": True, "detail": "Запись запущена."}


# Imported here (not at top) to avoid a heavy import cycle at module load.
from ..jobs import store  # noqa: E402

scheduler = Scheduler()
