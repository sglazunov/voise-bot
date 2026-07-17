"""A minimal single-worker job queue.

Why single worker: on a 4 GB / 4-core machine we must never run two Whisper
transcriptions at once or the box will swap to the HDD and crawl/OOM. So jobs
are processed strictly one at a time by one background thread.

Job state is persisted to a JSON file so results survive a restart.
"""
from __future__ import annotations

import json
import queue
import re
import threading
import time
import traceback
import uuid
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Dict, Optional

from . import config, db, formats, glossary
from .transcribe import transcribe_file

STATUS_QUEUED = "queued"
STATUS_RUNNING = "running"
STATUS_PAUSED = "paused"
STATUS_ANALYZING = "analyzing"
STATUS_DONE = "done"
STATUS_ERROR = "error"
STATUS_CANCELLED = "cancelled"


def _friendly_error(exc: BaseException) -> str:
    """A SHORT human message for the UI instead of a raw Python traceback.
    The full traceback still goes to the server log for debugging."""
    traceback.print_exc()
    s = str(exc) or exc.__class__.__name__
    low = s.lower()
    if "503" in low or "unavailable" in low or "high demand" in low or "overload" in low:
        return ("Движок ИИ сейчас перегружен и не отвечает (503). Это временно — "
                "нажмите «Пересобрать» чуть позже или выберите другой движок "
                "во вкладке «Нейросети».")
    if "429" in low or "rate limit" in low or "too many requests" in low or "quota" in low:
        return ("Исчерпан лимит запросов к движку ИИ (429). Подождите сброса квоты, "
                "добавьте ещё один ключ или выберите другой движок.")
    if "401" in low or "403" in low or "api key" in low or "api_key" in low \
            or "unauthorized" in low or "permission" in low:
        return ("Ключ движка ИИ отклонён (нет доступа). Проверьте ключ "
                "во вкладке «Нейросети».")
    if "404" in low or "not found" in low or "no longer available" in low:
        return ("Выбранная модель недоступна для этого ключа. Выберите другую "
                "модель во вкладке «Нейросети».")
    if "timed out" in low or "timeout" in low:
        return "Движок ИИ не ответил вовремя (таймаут). Попробуйте «Пересобрать»."
    return f"Не удалось собрать протокол: {s.splitlines()[0][:200]}"


def _team(owner: str | None) -> str:
    """Resolve a login to its TEAM (team-admin login). Jobs are owned by the team
    so everyone in it shares the recognition history."""
    if not owner:
        return owner or ""
    try:
        from . import security
        return security.team_of(owner)
    except Exception:
        return owner


def _owner_keys(owner: str | None):
    """The owner's per-user LLM API keys, so the protocol is built with THEIR
    keys (not another user's). None → fall back to server env keys."""
    if not owner:
        return None
    try:
        from . import user_creds
        return user_creds.load(owner)
    except Exception:
        return None


class JobCancelled(Exception):
    """Raised from the segment callback to abort a running transcription."""


@dataclass
class Job:
    id: str
    filename: str
    audio_path: str
    language: str
    diarize: bool
    model: str = ""                # Whisper model override (accuracy), "" = default
    initial_prompt: str = ""       # known names/terms to bias spelling
    glossary: str = ""             # "wrong=right" replacement rules
    analyze: bool = False          # generate AI protocol + Word doc
    provider: str = "auto"         # LLM provider for the protocol
    analysis_instructions: str = ""  # user's custom prompt additions
    analysis_prompt: str = ""        # expert mode: full prompt override
    capture_screen: bool = False     # OCR on-screen text from the video
    identify_speakers: bool = False  # read WHO spoke from the video (active-tile name)
    deliver_protocol_cloud: bool = False  # upload the .docx protocol to the user's cloud
    deliver_weeek_task: str = ""     # Weeek task id/URL to attach the protocol link to
    context_hint: str = ""           # matches saved AI-context projects (meeting/project name)
    delete_audio_when_done: bool = False  # delete the source media after processing
                                          # (recordings already sent to the UI's cloud)
    owner: str = ""                  # the login that owns this job (isolation)
    status: str = STATUS_QUEUED
    progress: float = 0.0          # 0..1
    created_at: float = field(default_factory=time.time)
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
    error: Optional[str] = None
    duration: Optional[float] = None
    speakers: Optional[int] = None
    diarization_error: Optional[str] = None  # why "who spoke" didn't run, if asked
    speaker_error: Optional[str] = None      # why video speaker-ID didn't run, if asked
    screen_error: Optional[str] = None       # why screen OCR didn't run, if asked
    protocol_cloud_url: Optional[str] = None  # cloud link of the delivered protocol
    delivery_error: Optional[str] = None     # why cloud/Weeek delivery didn't happen
    video_participants: list = field(default_factory=list)  # names read off the call grid
    screen_segments: int = 0                 # number of on-screen text snapshots
    analysis: Optional[dict] = None        # structured analysis result (latest)
    analysis_error: Optional[str] = None   # error message if analysis failed
    docx_providers: list = field(default_factory=list)  # engines a Word doc exists for

    def to_public(self) -> dict:
        d = asdict(self)
        d.pop("audio_path", None)  # don't leak server paths
        return d


class JobStore:
    def __init__(self) -> None:
        self._jobs: Dict[str, Job] = {}
        # Live partial transcript per job (in-memory only), for streaming UI.
        self._partial: Dict[str, list] = {}
        # Live analysis progress per job (stage + streamed text), in-memory only.
        self._analysis: Dict[str, dict] = {}
        # Per-job control flags (pause/cancel), in-memory only.
        self._control: Dict[str, dict] = {}
        # Serialises on-demand re-analysis so two LLM runs can't overlap.
        self._reanalyze_lock = threading.Lock()
        self._last_persist: float = 0.0
        self._lock = threading.Lock()
        self._queue: "queue.Queue[str]" = queue.Queue()
        self._load()
        self._purge_old()
        worker = threading.Thread(target=self._worker_loop, daemon=True, name="vtx-worker")
        worker.start()
        cleaner = threading.Thread(target=self._cleaner_loop, daemon=True, name="vtx-cleaner")
        cleaner.start()

    # ---- persistence -------------------------------------------------------
    def _load(self) -> None:
        try:
            if db.enabled():
                raw = db.jobs_load()
            elif config.JOBS_FILE.exists():
                raw = json.loads(config.JOBS_FILE.read_text(encoding="utf-8"))
            else:
                return
            for d in raw:
                job = Job(**d)
                # Nothing survives a restart mid-flight (no worker resumes it).
                if job.status in (STATUS_RUNNING, STATUS_QUEUED, STATUS_PAUSED):
                    job.status = STATUS_ERROR
                    job.error = "Прервано (сервис был перезапущен)."
                elif job.status == STATUS_ANALYZING:
                    # Transcript is already saved — keep it, just flag the
                    # analysis so the user can re-run it in one click.
                    job.status = STATUS_DONE
                    job.analysis_error = ("Анализ прерван (сервис перезапущен). "
                                          "Нажмите «Повторить анализ».")
                self._jobs[job.id] = job
        except Exception:
            pass

    def _save(self) -> None:
        data = [asdict(j) for j in self._jobs.values()]
        if db.enabled():
            db.jobs_save(data)
            return
        tmp = config.JOBS_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(config.JOBS_FILE)

    # ---- public API --------------------------------------------------------
    def create(self, filename: str, audio_path: str, language: str, diarize: bool,
               initial_prompt: str = "", glossary: str = "",
               analyze: bool = False, provider: str = "auto",
               analysis_instructions: str = "", analysis_prompt: str = "",
               capture_screen: bool = False, identify_speakers: bool = False,
               deliver_protocol_cloud: bool = False, deliver_weeek_task: str = "",
               context_hint: str = "", model: str = "",
               delete_audio_when_done: bool = False, owner: str = "") -> Job:
        job = Job(
            id=uuid.uuid4().hex[:12],
            filename=filename,
            audio_path=audio_path,
            language=language,
            diarize=diarize,
            model=model,
            initial_prompt=initial_prompt,
            glossary=glossary,
            analyze=analyze,
            provider=provider,
            analysis_instructions=analysis_instructions,
            analysis_prompt=analysis_prompt,
            capture_screen=capture_screen,
            identify_speakers=identify_speakers,
            deliver_protocol_cloud=deliver_protocol_cloud,
            deliver_weeek_task=(deliver_weeek_task or "").strip(),
            context_hint=(context_hint or "").strip(),
            delete_audio_when_done=delete_audio_when_done,
            owner=_team(owner),   # jobs belong to the TEAM, not the individual
        )
        with self._lock:
            self._jobs[job.id] = job
            self._save()
        self._queue.put(job.id)
        return job

    def get(self, job_id: str) -> Optional[Job]:
        return self._jobs.get(job_id)

    def get_owned(self, job_id: str, owner: str) -> Optional[Job]:
        """Return the job only if it belongs to `owner` — the isolation check
        every per-job endpoint must use so one login can't touch another's jobs."""
        job = self._jobs.get(job_id)
        if job is None or job.owner != _team(owner):
            return None
        return job

    def partial(self, job_id: str) -> list:
        """Segments transcribed so far (live), for the streaming UI."""
        return self._partial.get(job_id, [])

    def analysis_progress(self, job_id: str) -> dict:
        """Live analysis stage + streamed text (in-memory), for the UI."""
        return self._analysis.get(job_id, {})

    def _on_analysis(self, job_id: str):
        """Build an on_progress(stage, text) callback that updates the live buffer."""
        def cb(stage: str, text: str = "") -> None:
            self._analysis[job_id] = {"stage": stage, "text": text,
                                      "chars": len(text)}
        return cb

    def list(self, owner: str | None = None) -> list[Job]:
        jobs = self._jobs.values()
        if owner is not None:
            team = _team(owner)
            jobs = [j for j in jobs if j.owner == team]
        return sorted(jobs, key=lambda j: j.created_at, reverse=True)

    def result_path(self, job_id: str, fmt: str) -> Path:
        return config.RESULT_DIR / f"{job_id}.{fmt}"

    def docx_path(self, job_id: str, provider: str) -> Path:
        """Per-engine Word document, so docs from different engines coexist.

        The provider can be like "ollama:qwen2.5:7b" — sanitise it so the
        colon/slash don't produce an invalid filename.
        """
        safe = re.sub(r"[^A-Za-z0-9._-]", "-", provider or "engine")
        return config.RESULT_DIR / f"{job_id}__{safe}.docx"

    # ---- control (pause / resume / cancel) ---------------------------------
    def pause(self, job_id: str) -> Job:
        job = self._require(job_id)
        if job.status not in (STATUS_RUNNING,):
            raise ValueError("Поставить на паузу можно только идущую задачу.")
        self._control.setdefault(job_id, {})["pause"] = True
        return job

    def resume(self, job_id: str) -> Job:
        job = self._require(job_id)
        if job.status not in (STATUS_PAUSED, STATUS_RUNNING):
            raise ValueError("Возобновить можно только приостановленную задачу.")
        self._control.setdefault(job_id, {})["pause"] = False
        return job

    def cancel(self, job_id: str) -> Job:
        job = self._require(job_id)
        if job.status in (STATUS_DONE, STATUS_ERROR, STATUS_CANCELLED):
            raise ValueError("Задача уже завершена.")
        ctrl = self._control.setdefault(job_id, {})
        ctrl["cancel"] = True
        ctrl["pause"] = False  # unblock a paused worker so it can see the cancel
        # A job still in the queue (never started) can be finalised right now.
        if job.status == STATUS_QUEUED:
            self._finalise_cancel(job)
        return job

    def _require(self, job_id: str) -> Job:
        job = self._jobs.get(job_id)
        if not job:
            raise KeyError("Задача не найдена")
        return job

    # ---- re-run analysis on an already-transcribed job ---------------------
    def reanalyze(self, job_id: str, provider: str | None = None,
                  instructions: str | None = None,
                  custom_prompt: str | None = None,
                  deliver_protocol_cloud: bool | None = None,
                  deliver_weeek_task: str | None = None) -> Job:
        """Re-run the LLM protocol on the stored transcript (no re-transcribe).

        `provider` (optional) switches the engine for this retry. `instructions`
        and `custom_prompt` (optional) replace the prompt additions / expert
        full-prompt for this run.
        """
        job = self._require(job_id)
        txt_path = self.result_path(job_id, "txt")
        if not txt_path.exists():
            raise ValueError("Нет транскрипции для анализа.")
        # Prefer the continuous, timestamp-free text (cleaner input for the LLM).
        plain_path = self.result_path(job_id, "plain")
        src_path = plain_path if plain_path.exists() else txt_path
        if not self._reanalyze_lock.acquire(blocking=False):
            raise ValueError("Анализ уже выполняется, подождите.")
        if provider:
            job.provider = provider
        if instructions is not None:
            job.analysis_instructions = instructions
        if custom_prompt is not None:
            job.analysis_prompt = custom_prompt
        if deliver_protocol_cloud is not None:
            job.deliver_protocol_cloud = deliver_protocol_cloud
        if deliver_weeek_task is not None:
            job.deliver_weeek_task = (deliver_weeek_task or "").strip()
        job.protocol_cloud_url = None  # regenerate → re-deliver fresh
        job.analyze = True
        threading.Thread(target=self._do_reanalyze,
                         args=(job, src_path.read_text(encoding="utf-8")),
                         daemon=True).start()
        return job

    def _do_reanalyze(self, job: Job, txt: str) -> None:
        if "УЧАСТНИКИ ЗВОНКА" not in txt:
            txt += _participants_block(job)
        if "ПОСТОЯННЫЙ КОНТЕКСТ" not in txt:
            txt += _context_block(job)
        self._analysis[job.id] = {"stage": "Готовлю анализ…", "text": "", "chars": 0}
        self._set(job, status=STATUS_ANALYZING, analysis_error=None)
        try:
            from .analyze import analyze_transcript, AnalysisCancelled
            from .docx_export import generate_report

            result = analyze_transcript(
                txt, provider=job.provider,
                extra_instructions=job.analysis_instructions,
                custom_prompt=job.analysis_prompt,
                on_progress=self._on_analysis(job.id),
                cancel_check=lambda: self._control.get(job.id, {}).get("cancel"),
                keys=_owner_keys(job.owner))
            prov = result.get("_provider") or job.provider
            segs = []
            jp = self.result_path(job.id, "json")
            if jp.exists():
                try:
                    segs = json.loads(jp.read_text(encoding="utf-8")).get("segments", [])
                except Exception:
                    segs = []
            generate_report(
                out_path=self.docx_path(job.id, prov),
                filename=job.filename, segments=segs,
                analysis=result, duration=job.duration,
            )
            if prov not in job.docx_providers:
                job.docx_providers.append(prov)
            self._deliver_protocol(job, self.docx_path(job.id, prov))
            self._set(job, status=STATUS_DONE, analysis=result,
                      analysis_error=None, docx_providers=job.docx_providers)
        except AnalysisCancelled:
            # Transcript stays intact; just drop back to a finished state.
            self._control.pop(job.id, None)
            self._set(job, status=STATUS_DONE,
                      analysis_error="Сборка протокола отменена.")
        except Exception as e:
            self._set(job, status=STATUS_DONE, analysis_error=_friendly_error(e))
        finally:
            self._reanalyze_lock.release()

    # ---- re-run a failed/cancelled job from scratch ------------------------
    def retry(self, job_id: str) -> Job:
        """Re-run recognition (and its options + optional protocol) for a failed
        or cancelled job, reusing the originally uploaded file if it still exists.
        Recognition can't resume mid-way, so this restarts the pipeline cleanly."""
        job = self._require(job_id)
        if job.status not in (STATUS_ERROR, STATUS_CANCELLED):
            raise ValueError("Продолжить можно только задачу с ошибкой или отменённую.")
        if not job.audio_path or not Path(job.audio_path).exists():
            raise ValueError("Исходный файл больше недоступен — загрузите его заново.")
        self._control.pop(job_id, None)
        self._analysis.pop(job_id, None)
        self._partial[job_id] = []
        self._set(job, status=STATUS_QUEUED, progress=0.0, error=None,
                  analysis_error=None, started_at=None, finished_at=None,
                  protocol_cloud_url=None, delivery_error=None)
        self._queue.put(job_id)
        return job

    # ---- deliver the protocol to cloud + Weeek (manual jobs) ---------------
    def _deliver_protocol(self, job: Job, docx_path: Path) -> None:
        """Best-effort: upload the .docx protocol to the user's cloud (as set up
        in «Автоматизация») and attach the link to a Weeek task, if the job asked
        for it. Never raises — problems are recorded in job.delivery_error."""
        if not (job.deliver_protocol_cloud or job.deliver_weeek_task):
            return
        try:
            from .automation import settings as auto_settings, clouds, weeek
            cfg = auto_settings.load(job.owner)
            self._set(job, delivery_error=None)
            url = job.protocol_cloud_url
            # A Weeek link needs a public URL, so uploading is required either way.
            if not url:
                base = Path(job.filename or "protocol").stem
                up = clouds.upload(str(docx_path), f"{base} - протокол.docx", cfg,
                                   folder=(cfg.get("protocol_folder") or "").strip() or None)
                if up.get("ok") and up.get("url"):
                    url = up["url"]
                    self._set(job, protocol_cloud_url=url)
                else:
                    self._set(job, delivery_error=(up.get("error")
                              or "Не удалось выгрузить протокол в облако. Проверьте облако в «Автоматизации»."))
                    return
            if job.deliver_weeek_task:
                token = cfg.get("weeek_token")
                if not token:
                    self._set(job, delivery_error="Не задан токен Weeek — задайте его в «Автоматизации».")
                    return
                tid = _weeek_task_id(job.deliver_weeek_task)
                field = (cfg.get("weeek_protocol_field") or "Протокол встречи").strip()
                if cfg.get("weeek_set_protocol_field", True) and field:
                    res = weeek.set_custom_field(token, tid, field, url)
                    if not res.get("ok"):
                        # Fall back to a comment if the custom field write fails.
                        if not weeek.add_comment(token, tid, f"📄 Протокол встречи: {url}"):
                            self._set(job, delivery_error=(res.get("error")
                                      or "Не удалось записать ссылку в задачу Weeek."))
                elif cfg.get("post_back_to_weeek", True):
                    # Field writing is switched off — the link still must reach
                    # the task, as a comment.
                    if not weeek.add_comment(token, tid, f"📄 Протокол встречи: {url}"):
                        self._set(job, delivery_error="Не удалось оставить комментарий в Weeek.")
        except Exception as e:  # noqa: BLE001 — delivery must never break a job
            self._set(job, delivery_error=str(e))

    def redeliver(self, job_id: str, weeek_task: str | None = None,
                  cloud: bool | None = None) -> str:
        """(Re)attach the protocol of an existing job: set/refresh the delivery
        flags and, if the protocol is already built, deliver right now
        (synchronously). For a still-running job the flags alone are enough —
        the worker delivers at completion. Used by the scheduler's post-restart
        resume and by manual re-attach."""
        job = self._require(job_id)
        if weeek_task is not None:
            job.deliver_weeek_task = str(weeek_task).strip()
        if cloud is not None:
            job.deliver_protocol_cloud = bool(cloud)
        provs = job.docx_providers or []
        if job.status != STATUS_DONE or not provs:
            return "pending"    # worker (or «Пересобрать») delivers later
        self._deliver_protocol(job, self.docx_path(job.id, provs[-1]))
        return job.delivery_error or "delivered"

    # ---- retention / cleanup -----------------------------------------------
    def _purge_old(self) -> None:
        """Delete jobs (and their files) older than the retention window."""
        max_age = config.RESULT_RETENTION_HOURS * 3600
        now = time.time()
        removed = False
        for job in list(self._jobs.values()):
            ref = job.finished_at or job.created_at or now
            if now - ref > max_age:
                self._delete_job_files(job)
                self._jobs.pop(job.id, None)
                self._partial.pop(job.id, None)
                self._control.pop(job.id, None)
                removed = True
        if removed:
            with self._lock:
                self._save()

    def _delete_job_files(self, job: Job) -> None:
        for fmt in ("txt", "plain", "srt", "json", "docx", "screen.txt"):
            self.result_path(job.id, fmt).unlink(missing_ok=True)
        for p in config.RESULT_DIR.glob(f"{job.id}__*.docx"):  # per-engine docs
            p.unlink(missing_ok=True)
        try:
            p = Path(job.audio_path)
            p.unlink(missing_ok=True)
            p.with_suffix(".16k.wav").unlink(missing_ok=True)  # diarization temp
        except Exception:
            pass

    def _cleaner_loop(self) -> None:
        while True:
            time.sleep(1800)  # every 30 min
            try:
                self._purge_old()
            except Exception:
                pass

    # ---- worker ------------------------------------------------------------
    def _set(self, job: Job, persist: bool = True, **kw) -> None:
        with self._lock:
            for k, v in kw.items():
                setattr(job, k, v)
            if persist:
                self._save()
            else:
                # Throttle disk writes for high-frequency progress updates.
                now = time.time()
                if now - self._last_persist > 3:
                    self._save()
                    self._last_persist = now
        # A finished job's outcome is recorded for the business metrics — job
        # rows themselves are purged by retention. Upsert by id, so re-running
        # (retry / reanalyze) just refreshes the row.
        if kw.get("status") in (STATUS_DONE, STATUS_ERROR):
            from . import stats
            stats.record(job)

    def _worker_loop(self) -> None:
        while True:
            job_id = self._queue.get()
            job = self._jobs.get(job_id)
            if job is None or job.status != STATUS_QUEUED:
                continue
            self._process(job)

    def _process(self, job: Job) -> None:
        self._set(job, status=STATUS_RUNNING, started_at=time.time(), progress=0.0)
        self._partial[job.id] = []
        ctrl = self._control.setdefault(job.id, {})
        ctrl.update({"pause": False, "cancel": ctrl.get("cancel", False)})
        try:
            partial = self._partial[job.id]
            rules = glossary.parse(job.glossary)

            def on_start() -> None:
                self._set(job, persist=False, progress=0.01)

            def on_segment(seg, total: float) -> None:
                # Cooperative pause/cancel — checked between segments.
                if ctrl.get("cancel"):
                    raise JobCancelled()
                if ctrl.get("pause"):
                    self._set(job, status=STATUS_PAUSED, persist=True)
                    while ctrl.get("pause") and not ctrl.get("cancel"):
                        time.sleep(0.3)
                    if ctrl.get("cancel"):
                        raise JobCancelled()
                    self._set(job, status=STATUS_RUNNING, persist=True)
                # Apply the correction glossary in place so both the live stream
                # and the final transcript get the fixed spelling.
                if rules:
                    seg.text = glossary.apply(seg.text, rules)
                partial.append({"start": seg.start, "end": seg.end, "text": seg.text})
                if total > 0:
                    self._set(job, persist=False, progress=min(seg.end / total, 0.999))

            # Video sharpens recognition: read the participants' names off the
            # Telemost tiles FIRST and hand them to Whisper as a prompt — real
            # names are then written as shown on screen, not guessed by sound.
            # The full grid scan is also the AUTHORITATIVE participants list for
            # the protocol (everyone connected to the call, no LLM guessing).
            initial_prompt = job.initial_prompt
            if job.identify_speakers:
                try:
                    from . import speaker_id
                    if speaker_id.is_video(job.audio_path):
                        names = (speaker_id.scan_participants(job.audio_path)
                                 or speaker_id.scan_names(job.audio_path))
                        if names:
                            job.video_participants = names
                            initial_prompt = (f"{initial_prompt} "
                                              f"Участники встречи: {', '.join(names)}."
                                              ).strip()
                except Exception:  # a video quirk must not block transcription
                    pass

            segments, meta = transcribe_file(
                job.audio_path, language=job.language, on_segment=on_segment,
                on_start=on_start, initial_prompt=initial_prompt,
                model_name=job.model or None,
            )

            n_speakers = None
            diar_err = None
            if job.diarize:
                try:
                    from .diarize import diarize as run_diarize
                    wav = _ensure_wav(job.audio_path)
                    segments = run_diarize(wav, segments)
                    n_speakers = len({s.speaker for s in segments if s.speaker})
                except Exception as e:  # diarization is best-effort
                    diar_err = str(e)
                    meta["diarization_error"] = diar_err

            # Read WHO spoke straight from the video (active-speaker tile + name).
            # For Telemost recordings this is far more reliable than guessing from
            # text, and the real names override diarization's "Спикер N".
            speaker_err = None
            if job.identify_speakers:
                try:
                    from . import speaker_id
                    if speaker_id.is_video(job.audio_path):
                        segments = speaker_id.identify_speakers(job.audio_path, segments)
                        named = {s.speaker for s in segments if s.speaker}
                        if named:
                            n_speakers = len(named)
                        else:
                            speaker_err = ("Не удалось распознать имена говорящих с "
                                           "видео (не найдена подсветка активного "
                                           "участника). Проверьте запись/настройки.")
                    else:
                        speaker_err = "Файл не является видео — некому распознавать говорящих."
                except Exception as e:  # best-effort; never break transcription
                    speaker_err = str(e)
                    meta["speaker_id_error"] = speaker_err

            # Write all output formats to disk.
            txt_content = formats.to_txt(segments)
            (self.result_path(job.id, "txt")).write_text(txt_content, encoding="utf-8")
            # Continuous, timestamp-free text — the readable on-screen view and the
            # clean input the protocol LLM analyses (fewer tokens, less noise).
            plain_content = formats.to_plain(segments)
            (self.result_path(job.id, "plain")).write_text(plain_content, encoding="utf-8")
            (self.result_path(job.id, "srt")).write_text(
                formats.to_srt(segments), encoding="utf-8")
            (self.result_path(job.id, "json")).write_text(
                formats.to_json(segments, meta), encoding="utf-8")

            # Optional: capture on-screen text from the video (OCR).
            screen_block = ""
            screen_err = None
            screen_segs = 0
            if job.capture_screen:
                try:
                    from . import screen_ocr
                    if screen_ocr.is_video(job.audio_path):
                        items = screen_ocr.extract_screen_text(job.audio_path)
                        screen_block = screen_ocr.to_block(items)
                        screen_segs = len(items)
                        if screen_block:
                            self.result_path(job.id, "screen.txt").write_text(
                                screen_block, encoding="utf-8")
                    else:
                        screen_err = "Файл не является видео — нечего распознавать с экрана."
                except Exception as e:
                    screen_err = str(e)

            # Text fed to the protocol analysis = continuous speech (no timestamps)
            # + on-screen text + the participants read off the call grid + the
            # user's standing AI context (who's who / project essence).
            analysis_input = plain_content + (("\n\n" + screen_block) if screen_block else "")
            analysis_input += _participants_block(job)
            analysis_input += _context_block(job)

            # AI analysis + Word document (optional, requires ANTHROPIC_API_KEY)
            analysis_result = None
            analysis_err = None
            if job.analyze:
                self._analysis[job.id] = {"stage": "Готовлю анализ…", "text": "", "chars": 0}
                self._set(job, status=STATUS_ANALYZING, persist=True)
                try:
                    from .analyze import analyze_transcript, AnalysisCancelled
                    from .docx_export import generate_report

                    analysis_result = analyze_transcript(
                        analysis_input, provider=job.provider,
                        extra_instructions=job.analysis_instructions,
                        custom_prompt=job.analysis_prompt,
                        on_progress=self._on_analysis(job.id),
                        cancel_check=lambda: self._control.get(job.id, {}).get("cancel"),
                        keys=_owner_keys(job.owner))
                    prov = analysis_result.get("_provider") or job.provider
                    segs_dicts = [
                        {"start": s.start, "end": s.end,
                         "text": s.text, "speaker": s.speaker}
                        for s in segments
                    ]
                    generate_report(
                        out_path=self.docx_path(job.id, prov),
                        filename=job.filename,
                        segments=segs_dicts,
                        analysis=analysis_result,
                        duration=meta.get("duration"),
                    )
                    if prov not in job.docx_providers:
                        job.docx_providers.append(prov)
                    # Optionally push the protocol to cloud + Weeek (best-effort).
                    self._deliver_protocol(job, self.docx_path(job.id, prov))
                except AnalysisCancelled:
                    # Transcript (txt/srt/json) is already saved; just stop here.
                    self._control.pop(job.id, None)
                    self._set(job, status=STATUS_CANCELLED, finished_at=time.time())
                    return
                except Exception as e:
                    analysis_err = _friendly_error(e)

            self._set(
                job,
                status=STATUS_DONE,
                progress=1.0,
                finished_at=time.time(),
                duration=meta.get("duration"),
                speakers=n_speakers,
                diarization_error=diar_err,
                speaker_error=speaker_err,
                screen_error=screen_err,
                screen_segments=screen_segs,
                analysis=analysis_result,
                analysis_error=analysis_err,
            )
        except JobCancelled:
            self._finalise_cancel(job)
        except Exception:
            self._set(
                job,
                status=STATUS_ERROR,
                error=traceback.format_exc(limit=3),
                finished_at=time.time(),
            )
        finally:
            # For meeting recordings already delivered to the UI's cloud, don't
            # keep an internal copy of the video — drop the source after processing.
            if getattr(job, "delete_audio_when_done", False):
                self._delete_source(job)

    def _delete_source(self, job: Job) -> None:
        """Remove the source media file and its capture sidecars."""
        try:
            src = Path(job.audio_path)
            for p in (src, Path(str(src) + ".ffmpeg.log"),
                      src.with_suffix(".16k.wav"), src.with_suffix(".join-failed.png")):
                try:
                    p.unlink(missing_ok=True)
                except OSError:
                    pass
        except Exception:
            pass

    def _finalise_cancel(self, job: Job) -> None:
        """Mark a job cancelled, keeping whatever was transcribed so far."""
        partial = self._partial.get(job.id, [])
        if partial:
            try:
                lines = []
                for s in partial:
                    sec = int(s.get("start", 0))
                    ts = f"{sec // 60:02d}:{sec % 60:02d}"
                    lines.append(f"[{ts}] {s.get('text', '').strip()}")
                self.result_path(job.id, "txt").write_text(
                    "\n".join(lines) + "\n", encoding="utf-8")
            except Exception:
                pass
        self._control.pop(job.id, None)
        self._set(job, status=STATUS_CANCELLED, finished_at=time.time())


def _context_block(job: "Job") -> str:
    """The user's standing AI context (global + projects matching this meeting),
    prepended to the analysis so the AI knows roles / project essence up front."""
    try:
        from . import ai_context
        hint = f"{job.context_hint} {job.filename}"
        blk = ai_context.block_for(job.owner, hint)
        return ("\n\n" + blk) if blk else ""
    except Exception:
        return ""


def _participants_block(job: "Job") -> str:
    """The «who is on the call» block for the LLM — names read off the video
    grid are authoritative, so the protocol lists exactly these people."""
    names = getattr(job, "video_participants", None) or []
    if not names:
        return ""
    return ("\n\n=== УЧАСТНИКИ ЗВОНКА (распознано с видео) ===\n"
            + ", ".join(names))


def _weeek_task_id(raw: str) -> str:
    """Accept either a bare Weeek task id or a task URL and return the id.
    Task URLs end with the numeric id (e.g. .../task/12345)."""
    s = (raw or "").strip()
    nums = re.findall(r"\d+", s)
    if ("http" in s or "/" in s) and nums:
        return nums[-1]
    return s


def _ensure_wav(audio_path: str) -> str:
    """pyannote wants a 16 kHz mono wav. Convert via ffmpeg if needed."""
    import subprocess

    src = Path(audio_path)
    if src.suffix.lower() == ".wav":
        return str(src)
    wav = src.with_suffix(".16k.wav")
    if not wav.exists():
        subprocess.run(
            ["ffmpeg", "-y", "-i", str(src), "-ac", "1", "-ar", "16000", str(wav)],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    return str(wav)


store = JobStore()
