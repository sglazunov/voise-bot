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
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .. import config, db, security
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

    def public(self) -> dict:
        return {"task_id": self.task_id, "title": self.title, "url": self.url,
                "start": self.start.isoformat() if self.start else None,
                "state": self.state, "detail": self.detail,
                "job_id": self.job_id, "cloud_url": self.cloud_url,
                "upload_error": self.upload_error,
                "has_live": bool(self.live_text), "has_notes": bool(self.live_notes),
                "do_protocol": self.do_protocol, "record_flag": self.record_flag}


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
                "last_poll": self._last_poll.get(user, 0.0), "meetings": meetings}

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
                    except Exception:  # one team's failure must not stop others
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
        rec_field = cfg.get("weeek_record_field") or "Запись встречи"
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
            # A RECURRING Weeek task carries the PREVIOUS occurrence's links in
            # its custom fields (the task is duplicated with old values). Wipe
            # «Видео встречи»/«Протокол встречи» at recording start, so the task
            # never shows last week's video/protocol as if they were today's;
            # the fresh links are written below as they become ready.
            threading.Thread(target=self._wipe_stale_links,
                             args=(st, cfg, log), daemon=True).start()
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
            # Persist the file path BEFORE recording starts: if the process dies
            # mid-meeting, the restart scan (_resume_pending) finds the fMP4 by
            # this path and queues it for processing instead of losing it.
            st.out_path = out
            self._save_state(st)

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
                                     or not auto_settings.get(user, "enabled")))
            if not res.get("ok"):
                self._set(st, "error", res.get("error") or "Запись не удалась.")
                return
            out = res.get("path") or out  # telemost mode may save .webm, not .mp4
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
            up = self._upload_with_retry(out, cfg, log)
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
                self._write_weeek_field(cfg.get("weeek_token"), st.task_id,
                                        field, st.cloud_url, log)

            # Keep the video ONLY where the UI points. If it was delivered
            # elsewhere (a remote cloud, or a local folder other than the staging
            # dir), the staging copy in data/recordings is redundant and must not
            # linger inside the server. We delete it after the pipeline is done
            # (transcription still needs to read it first).
            delivered_elsewhere = self._delivered_elsewhere(up, out)

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
                self._save_state(st)  # remember the job across restarts
                # Once the protocol (.docx) is built, upload it to the same cloud
                # and link it in Weeek — in a background waiter so the recording
                # lock isn't held during transcription.
                if do_protocol and cfg.get("upload_protocol", True):
                    threading.Thread(
                        target=self._await_and_upload_protocol,
                        args=(st, job.id, cfg, Path(out).stem),
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
    def _wipe_stale_links(self, st: MeetingState, cfg: dict, log) -> None:
        """Blank the video/protocol link fields of the task when recording
        starts — a recurring task inherits LAST week's links otherwise."""
        token = cfg.get("weeek_token")
        for opt, fname in (("weeek_set_video_field", "weeek_video_field"),
                           ("weeek_set_protocol_field", "weeek_protocol_field")):
            fld = (cfg.get(fname) or "").strip()
            if not (cfg.get(opt, True) and fld and token):
                continue
            try:
                res = weeek.set_custom_field(token, st.task_id, fld, "")
                if res.get("ok"):
                    log(f"Поле «{fld}»: очищено от прошлой встречи.")
            except Exception:  # cosmetic step — never blocks the recording
                pass

    def _write_weeek_field(self, token, task_id, field: str, value: str,
                           log, attempts: int = 3) -> bool:
        """Write a link into a Weeek custom field, retrying — the link for THIS
        meeting must actually land, not silently stay last week's."""
        err = None
        for i in range(attempts):
            if i:
                time.sleep(10 * i)
            try:
                res = weeek.set_custom_field(token, task_id, field, value)
            except Exception as e:  # noqa: BLE001
                res = {"ok": False, "error": str(e)}
            if res.get("ok"):
                log(f"Поле «{field}» в Weeek: заполнено ✓")
                return True
            err = res.get("error")
        log(f"Поле «{field}» в Weeek: не удалось — {err}")
        return False

    # -- recording delivery: the local file is only a staging copy ----------
    def _upload_with_retry(self, out: str, cfg: dict, log, attempts: int = 3) -> dict:
        """Upload the recording, retrying a few times with a pause — one network
        hiccup must not leave a meeting's video stranded on the server."""
        last: dict = {}
        for i in range(attempts):
            if i:
                log(f"Облако: повтор выгрузки {i + 1}/{attempts}…")
                time.sleep(20 * i)
            try:
                last = clouds.upload(out, Path(out).name, cfg)
            except Exception as e:  # noqa: BLE001 — an uploader bug isn't fatal
                last = {"ok": False, "error": str(e)}
            if last.get("ok"):
                return last
            log(f"Облако (попытка {i + 1}/{attempts}): {last.get('error')}")
        return last or {"ok": False, "error": "облако недоступно"}

    @staticmethod
    def _delivered_elsewhere(up: dict, out: str) -> bool:
        """True when the upload put the file somewhere OTHER than the staging
        path — then the local copy is redundant and must be deleted."""
        if not up.get("ok"):
            return False
        if up.get("backend") != "local":
            return True
        try:
            return Path(up.get("path") or "").resolve() != Path(out).resolve()
        except OSError:
            return False

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
            up = self._upload_with_retry(out, cfg, lambda *_: None, attempts=1)
            if not up.get("ok"):
                st.upload_error = up.get("error") or "облако недоступно"
                continue
            st.cloud_url = up.get("url")
            st.upload_error = None
            token = cfg.get("weeek_token")
            field = (cfg.get("weeek_video_field") or "").strip()
            if cfg.get("weeek_set_video_field", True) and st.cloud_url and field:
                self._write_weeek_field(token, st.task_id, field, st.cloud_url,
                                        lambda *_: None)
            if cfg.get("post_back_to_weeek") and st.cloud_url:
                weeek.add_comment(token, st.task_id,
                                  f"🎥 Запись встречи: {st.cloud_url}")
            self._save_state(st)  # the late-delivered cloud link survives restarts
            if self._delivered_elsewhere(up, out):
                job = store.get(st.job_id) if st.job_id else None
                if job and job.status in ("queued", "running", "paused", "analyzing"):
                    # Recognition still reads the file — it deletes it on finish.
                    job.delete_audio_when_done = True
                else:
                    for p in (out, out + ".ffmpeg.log"):
                        try:
                            Path(p).unlink(missing_ok=True)
                        except OSError:
                            pass
            return

    def _await_and_upload_protocol(self, st: MeetingState, job_id: str,
                                   cfg: dict, base_name: str) -> None:
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

        def report(msg: str) -> None:
            """Log + (best-effort) leave a comment in the task, so the user sees
            why the protocol didn't attach."""
            log(msg)
            if token and cfg.get("post_back_to_weeek", True):
                try:
                    weeek.add_comment(token, task_id, msg)
                except Exception:
                    pass

        deadline = time.time() + 2 * 3600
        while time.time() < deadline:
            job = store.get(job_id)
            if job is None:
                report("⚠ Протокол не прикреплён: задача распознавания пропала "
                       "(сервис перезапускался во время обработки).")
                return
            if job.status in ("done", "error", "cancelled"):
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

    def _snap_path(self, user: str) -> Path:
        return security.user_dir(user) / "meetings.json"

    def _load_snaps(self, user: str) -> dict:
        try:
            if db.enabled():
                return db.meetings_load(user)
            return json.loads(self._snap_path(user).read_text(encoding="utf-8")) or {}
        except (OSError, ValueError):
            return {}

    def _save_state(self, st: MeetingState) -> None:
        try:
            snaps = self._load_snaps(st.owner)
            snaps[st.key] = {"state": st.state, "detail": st.detail,
                             "job_id": st.job_id, "cloud_url": st.cloud_url,
                             "out_path": st.out_path,
                             "live_notes": st.live_notes,
                             "do_protocol": st.do_protocol,
                             "saved_at": time.time()}
            if len(snaps) > 200:  # keep the newest 200
                for k in sorted(snaps, key=lambda k: snaps[k].get("saved_at", 0))[:-200]:
                    snaps.pop(k, None)
            if db.enabled():
                db.meetings_save(st.owner, snaps)
                return
            p = self._snap_path(st.owner)
            tmp = p.with_suffix(".tmp")
            tmp.write_text(json.dumps(snaps, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, p)
        except Exception:  # persistence is best-effort, never breaks the loop
            pass

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
                snaps = self._load_snaps(team)
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
                    pass
                threading.Thread(
                    target=self._await_and_upload_protocol,
                    args=(st, jid, cfg, Path(job.filename).stem),
                    daemon=True).start()

    def _finalize_orphan(self, st: MeetingState, cfg: dict, out: str) -> None:
        """A recording the crash orphaned: run the same post-recording pipeline
        the normal path would have — upload the video, queue transcription with
        delivery flags, start the waiter and the Weeek link write."""
        do_protocol = bool(cfg.get("do_protocol", True))
        do_transcribe = bool(cfg.get("do_transcribe", True))
        deliver = bool(do_protocol and cfg.get("upload_protocol", True))
        from ..analyze import preset_for_title
        preset_cfg = str(cfg.get("analyze_preset") or "auto")
        preset = preset_for_title(st.title) if preset_cfg == "auto" else preset_cfg

        def log(msg: str) -> None:
            st.logs.append(str(msg))

        log("Запись прервана перезапуском сервиса — файл цел, дообрабатываю.")
        if not st.cloud_url:
            up = self._upload_with_retry(out, cfg, log, attempts=1)
            if up.get("ok"):
                st.cloud_url = up.get("url")
                field = (cfg.get("weeek_video_field") or "").strip()
                if cfg.get("weeek_set_video_field", True) and st.cloud_url and field:
                    self._write_weeek_field(cfg.get("weeek_token"), st.task_id,
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
                args=(st, job.id, cfg, Path(out).stem),
                daemon=True).start()

    def _restore_snapshot(self, st: MeetingState) -> None:
        """Fill a freshly discovered MeetingState from its saved outcome."""
        snap = self._load_snaps(st.owner).get(st.key)
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
            self._save_state(st)

    # -- Д10: live transcript during the recording ---------------------------
    def _find_state(self, user: str, task_id) -> "MeetingState | None":
        team = security.team_of(user)
        with self._lock:
            for st in self._states.values():
                if st.owner == team and str(st.task_id) == str(task_id):
                    return st
        return None

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
        self._save_state(st)
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
        ffmpeg = cfg.get("ffmpeg_path") or "ffmpeg"

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
