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

from . import (config, db, formats, glossary, logs, meeting_series, names, redact,
               usage)
from .transcribe import transcribe_file

log = logs.get("vtx.jobs")

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
    # "Nothing was recognised" is already a finished, actionable sentence —
    # pass it through untouched instead of wrapping it in a second prefix.
    from .analyze import NoTranscript
    if isinstance(exc, NoTranscript):
        return s
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
    user_notes: str = ""             # participant's own live notes — the protocol's skeleton (Д6)
    preset: str = ""                 # protocol preset (Д11): planerka|design|demo|one_on_one|custom
    # Почему остановилась запись, из которой взялась эта задача: silence |
    # max_duration | chat_stop | call_ended | left_call | nobody_joined |
    # thinned_out | stopped | error. Рекордер возвращает это с самого начала,
    # но никто не читал — переживает ретеншн в meeting_stats.
    stop_reason: str = ""
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
    transcribe_sec: Optional[float] = None   # чистое время распознавания
    # Расход модели на эту задачу: {"calls","in","cached","out","by_model"}.
    # Копится за ВСЕ прогоны (первый + пересборки), потому что деньги тоже
    # тратятся за все. Переживает ретеншн в meeting_stats (см. stats.record).
    llm_usage: dict = field(default_factory=dict)
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
    weeek_tasks: list = field(default_factory=list)     # черновики задач для Weeek

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
        # Чем занята задача после расшифровки (спикеры, OCR) — тоже только в
        # памяти: переживать перезапуск нечему, прерванная задача всё равно
        # начинается заново.
        self._stage: Dict[str, str] = {}
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
            log.error("Не удалось восстановить задачи после старта", exc_info=True)

    _save_lock = threading.Lock()

    def _save(self, job: "Job | None" = None) -> None:
        """Сохранить состояние. На Postgres, если передана одна задача, пишется
        ТОЛЬКО она: раньше каждое обновление процента переписывало всю таблицу
        задач — при десятках записей это заметная нагрузка на ровном месте, да
        ещё и затирало параллельные изменения соседних задач."""
        if db.enabled():
            if job is not None:
                # Задачи, которой больше нет в памяти, в базе быть не должно.
                # Иначе поток, держащий свою ссылку на Job (воркер, ожидатель
                # протокола, «Пересобрать»), своим следующим _set ВСТАВИТ строку
                # обратно — уже после того, как ретеншн удалил и её, и все её
                # файлы. После перезапуска такая задача поднимается из базы как
                # «Готово», а скачивание любого формата отдаёт 404.
                if job.id not in self._jobs:
                    log.info("Задача %s уже удалена — состояние не сохраняю", job.id)
                    return
                db.job_upsert(asdict(job))
            else:
                db.jobs_save([asdict(j) for j in self._jobs.values()])
            return
        data = [asdict(j) for j in self._jobs.values()]
        # The worker thread and API threads may save concurrently; the shared
        # .tmp path must not be replaced out from under another writer.
        with self._save_lock:
            tmp = config.JOBS_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                           encoding="utf-8")
            tmp.replace(config.JOBS_FILE)

    # ---- public API --------------------------------------------------------
    def create(self, filename: str, audio_path: str, language: str, diarize: bool,
               initial_prompt: str = "", glossary: str = "",
               analyze: bool = False, provider: str = "auto",
               analysis_instructions: str = "", analysis_prompt: str = "",
               capture_screen: bool = False, identify_speakers: bool = False,
               deliver_protocol_cloud: bool = False, deliver_weeek_task: str = "",
               context_hint: str = "", model: str = "",
               delete_audio_when_done: bool = False, owner: str = "",
               user_notes: str = "", preset: str = "",
               stop_reason: str = "") -> Job:
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
            user_notes=(user_notes or "").strip(),
            preset=(preset or "").strip(),
            stop_reason=(stop_reason or "").strip(),
            delete_audio_when_done=delete_audio_when_done,
            owner=_team(owner),   # jobs belong to the TEAM, not the individual
        )
        with self._lock:
            self._jobs[job.id] = job
            self._save(job)
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

    def stage(self, job_id: str) -> str:
        """Чем задача занята ПОСЛЕ расшифровки. Полоса прогресса показывает
        только распознавание фрагментов, а за ним идут разметка говорящих и
        чтение текста с экрана — каждая на десятки минут. Без этой строки
        человек видит «распознаётся, 100%» и считает, что всё зависло."""
        return self._stage.get(job_id, "")

    def _set_stage(self, job_id: str, text: str) -> None:
        if text:
            self._stage[job_id] = text
        else:
            self._stage.pop(job_id, None)

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
        # Пока задача в работе, второй сборки протокола быть не должно. Раньше
        # проверялось только наличие расшифровки на диске, а она есть и во время
        # анализа, и от прошлого прогона: воркер и «Пересобрать» шли параллельно,
        # оба звали доставку — в задачу Weeek уходили ДВЕ ссылки на протокол
        # одной встречи и два комментария, движок тратил лимит вдвое, а какой из
        # двух протоколов останется, решал случай. _reanalyze_lock тут не
        # помогает: он разводит только ручные пересборки между собой и про
        # воркера ничего не знает.
        if job.status in (STATUS_QUEUED, STATUS_RUNNING, STATUS_PAUSED,
                          STATUS_ANALYZING):
            raise ValueError(
                "Задача ещё в работе — протокол собирается сам. "
                "Дождитесь окончания и нажмите «Пересобрать», если результат "
                "не устроит.")
        txt_path = self.result_path(job_id, "txt")
        if not txt_path.exists():
            raise ValueError("Нет транскрипции для анализа.")
        # Prefer the TIMECODED transcript (Д1): the map-reduce needs the [мм:сс]
        # marks for chronology, and grounding (Д5) quotes against it.
        src_path = txt_path
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

    def set_notes(self, job_id: str, notes: str) -> Job:
        """Attach/replace the participant's live notes (Д6). They join the next
        protocol generation — hit «Пересобрать» to apply them to an old job."""
        job = self._require(job_id)
        job.user_notes = (notes or "").strip()[:20000]
        self._save(job)
        return job

    def _rebuild_docx(self, job: Job) -> None:
        """Re-render the Word document from the current job.analysis."""
        from .docx_export import generate_report
        prov = (job.docx_providers[-1] if job.docx_providers
                else (job.analysis or {}).get("_provider") or job.provider)
        segs = []
        jp = self.result_path(job.id, "json")
        if jp.exists():
            try:
                segs = json.loads(jp.read_text(encoding="utf-8")).get("segments", [])
            except Exception:  # noqa: BLE001
                segs = []
        generate_report(out_path=self.docx_path(job.id, prov),
                        filename=job.filename, segments=segs,
                        analysis=job.analysis, duration=job.duration)
        if prov not in job.docx_providers:
            job.docx_providers.append(prov)

    def update_analysis(self, job_id: str, patch: dict) -> Job:
        """Д13: apply the user's manual edits to the protocol and re-render the
        Word doc. Human edits are TRUSTED — the grounding flags are dropped
        (their indexes no longer line up, and the human just reviewed it)."""
        job = self._require(job_id)
        if not job.analysis:
            raise ValueError("У задачи ещё нет протокола.")
        from .analyze import (_normalise_detailed, _normalise_participants,
                              _normalise_tasks)
        a = dict(job.analysis)
        if "summary" in patch:
            a["summary"] = str(patch["summary"] or "").strip()[:8000]
        if "participants" in patch:
            a["participants"] = _normalise_participants(patch["participants"])[:60]
        if "detailed" in patch:
            a["detailed"] = _normalise_detailed(patch["detailed"])[:50]
        for key in ("key_thoughts", "conclusions", "decisions"):
            if key in patch:
                a[key] = [str(x).strip()[:600] for x in (patch[key] or [])
                          if str(x).strip()][:60]
        for key in ("tasks", "minor_tasks", "done_tasks"):
            if key in patch:
                a[key] = _normalise_tasks(patch[key])[:120]
        a.pop("verification", None)
        a.pop("_warning", None)
        a["_edited"] = True
        job.analysis = a
        self.prepare_weeek_tasks(job)
        self._rebuild_docx(job)
        self._save(job)
        self.index_search(job)
        return job

    def prepare_weeek_tasks(self, job: Job, members: list | None = None) -> list:
        """Черновики задач для Weeek из текущего протокола (без сети: участники
        берутся из кэша настроек). Статусы созданных переносятся."""
        try:
            from . import meeting_series, weeek_tasks
            from .automation import settings as auto_settings
            cfg = auto_settings.load(job.owner)
            if not cfg.get("weeek_tasks_enabled", True) or not job.analysis:
                return job.weeek_tasks
            if members is None:
                members = (cfg.get("weeek_members_cache") or {}).get("members") or []
            title = job.context_hint or job.filename
            _key, series = meeting_series.find(job.owner, title)
            job.weeek_tasks = weeek_tasks.prepare(
                job.id, job.analysis, cfg,
                weeek_tasks.meeting_date_of(title, job.created_at),
                members=members, previous=job.weeek_tasks, series=series)
        except Exception:  # noqa: BLE001 — черновики не должны ломать протокол
            log.warning("Черновики задач Weeek не подготовлены (%s)", job.id, exc_info=True)
        return job.weeek_tasks

    def regen_topic(self, job_id: str, index: int,
                    provider: str | None = None) -> Job:
        """Д13: re-generate ONE topic's details via the LLM, leave the rest."""
        job = self._require(job_id)
        a = dict(job.analysis or {})
        detailed = list(a.get("detailed") or [])
        if not (0 <= index < len(detailed)):
            raise ValueError("Нет темы с таким номером.")
        txt_path = self.result_path(job_id, "txt")
        if not txt_path.exists():
            raise ValueError("Нет расшифровки для перегенерации.")
        from .analyze import regen_topic_details
        with usage.collect() as acc:
            detailed[index] = regen_topic_details(
                txt_path.read_text(encoding="utf-8"), detailed[index],
                provider=provider or job.provider, keys=_owner_keys(job.owner),
                user_notes=job.user_notes)
        self._add_usage(job, acc)
        a["detailed"] = detailed
        a.pop("verification", None)   # indexes may shift meaning — cleared
        a["_edited"] = True
        job.analysis = a
        self._rebuild_docx(job)
        self._save(job)
        self.index_search(job)
        return job

    def ask(self, job_id: str, question: str) -> str:
        """Д13: Q&A over this meeting's transcript (answer carries timecodes)."""
        job = self._require(job_id)
        txt_path = self.result_path(job_id, "txt")
        if not txt_path.exists():
            raise ValueError("Нет расшифровки — не по чему искать ответ.")
        from .analyze import ask_meeting
        # Вопрос по встрече — тоже вызов модели и тоже деньги команды:
        # расшифровка уходит в запрос целиком или большими окнами.
        with usage.collect() as acc:
            answer = ask_meeting(txt_path.read_text(encoding="utf-8"), question,
                                 provider=job.provider, keys=_owner_keys(job.owner),
                                 user_notes=job.user_notes)
        self._add_usage(job, acc)
        return answer

    def index_search(self, job: Job) -> None:
        """Д14: (re)index this job's transcript + protocol for full-text search.
        Postgres-only (file backend searches by scanning); best-effort."""
        if not db.enabled():
            return
        try:
            parts = []
            p = self.result_path(job.id, "txt")
            if p.exists():
                parts.append(p.read_text(encoding="utf-8"))
            a = job.analysis or {}
            parts.append(str(a.get("summary") or ""))
            parts += [f"{d.get('topic', '')}. {d.get('details', '')}"
                      for d in (a.get("detailed") or []) if isinstance(d, dict)]
            for key in ("tasks", "minor_tasks", "done_tasks"):
                parts += [str(x.get("task") or "") for x in (a.get(key) or [])
                          if isinstance(x, dict)]
            parts += [str(x) for x in (a.get("decisions") or [])]
            body = "\n".join(x for x in parts if x)[:500_000]
            if body:
                db.search_save(job.id, job.owner, Path(job.filename).stem,
                               body, job.created_at or time.time())
        except Exception:  # noqa: BLE001 — поиск не должен ломать конвейер
            log.warning("Задача %s не попала в поисковый индекс", job.id, exc_info=True)

    def backfill_search(self) -> None:
        """Index the done jobs that existed before the search feature."""
        if not db.enabled():
            return
        try:
            teams = {j.owner for j in self._jobs.values()}
            for team in teams:
                indexed = db.search_ids(team)
                for job in self._jobs.values():
                    if (job.owner == team and job.status == STATUS_DONE
                            and job.id not in indexed):
                        self.index_search(job)
        except Exception:  # noqa: BLE001
            log.warning("Дозаполнение поискового индекса прервано", exc_info=True)

    def _enforce_participants(self, job: Job, result: dict) -> dict:
        """Участники протокола = РОВНО те, кто был в окне Телемоста (подписи
        плиток). На встрече часто говорят О других людях — промпт просит модель
        не вписывать их в участники, но это лишь просьба; здесь список
        заменяется механически, так что обсуждаемый человек попасть в
        «Участники» не может. Роли, которые модель определила по разговору,
        сохраняются у совпавших имён. Без распознанных плиток (аудио-файл,
        сбой сканера) поведение прежнее — по тексту."""
        tiles = _tile_names(job)
        if not result or not tiles:
            return result

        def norm(s: str) -> str:
            return (s or "").strip().lower().replace("ё", "е")

        roles: dict[str, str] = {}
        for p in (result.get("participants") or []):
            if isinstance(p, dict) and (p.get("name") or "").strip():
                roles[norm(p["name"])] = str(p.get("role") or "").strip()
        out = []
        for name in tiles:
            n = norm(name)
            role = roles.get(n, "")
            if not role:  # «Сергей Глазунов» на плитке vs «Сергей» у модели
                for k, v in roles.items():
                    if v and (n in k or k in n):
                        role = v
                        break
            out.append({"name": name, "role": role})
        result["participants"] = out
        return result

    def _preset_extra(self, job: Job) -> str:
        """Д11: the preset's emphasis rules + the user's own instructions.

        Тип протокола, закреплённый в карточке серии, побеждает автоопределение
        по названию (и «универсальный»), но не явный выбор другого типа руками.
        """
        try:
            from .analyze import preset_rules, preset_for_title
            from .automation import settings as auto_settings
            custom = auto_settings.load(job.owner).get("custom_presets") or {}
            preset = job.preset or ""
            title = job.context_hint or job.filename
            pinned = meeting_series.pinned_preset(job.owner, title)
            if pinned and preset in ("", "universal", "auto", preset_for_title(title)):
                preset = pinned
            rules = preset_rules(preset, custom)
        except Exception:  # noqa: BLE001
            rules = ""
        parts = [p for p in (rules, job.analysis_instructions) if (p or "").strip()]
        return "\n\n".join(parts)

    def _remember_series(self, job: Job, result: dict | None) -> None:
        """Память серии: решения и задачи этой встречи станут справкой для
        следующей встречи с тем же названием. Не ломает задачу."""
        if not result:
            return
        try:
            title = job.context_hint or job.filename
            date = meeting_series.date_from_title(title) or time.strftime(
                "%d.%m.%Y", time.localtime(job.created_at))
            # Сначала сверка с прошлой встречей (память ещё о ней), потом запись.
            carried = meeting_series.carry_over(job.owner, title, result, job_id=job.id)
            if carried:
                result["_carried"] = carried
            key = meeting_series.remember(job.owner, title, result,
                                          job_id=job.id, date=date)
            if key:
                result["_series"] = {"key": key,
                                     "title": meeting_series.display_title(title)}
        except Exception:  # noqa: BLE001
            log.warning("Память серии не обновлена (%s)", job.id, exc_info=True)

    def _add_usage(self, job: Job, acc: dict) -> None:
        """Долить расход прогона к расходу задачи.

        Складываем, а не заменяем: пересборка — это ещё один полный проход по
        встрече, и её токены тоже оплачены. Иначе счётчик показывал бы только
        последний прогон и занижал расход команды.
        """
        if not acc or not acc.get("calls"):
            return
        # Поимённый список вызовов уезжает в журнал событий, а в задаче остаются
        # только итоги. ⚠️ Хранить его ещё и в `Job.llm_usage` нельзя: это
        # JSONB-колонка, которую переписывает КАЖДОЕ обновление задачи, а список
        # растёт с каждой пересборкой.
        calls_log = list(acc.get("log") or [])
        if calls_log:
            from . import events
            events.record_many(calls_log, user=job.owner, job_id=job.id,
                               kind=events.LLM_CALL)
        total = dict(job.llm_usage or {})
        usage._merge(total, acc)
        total.pop("log", None)
        self._set(job, llm_usage=total)
        # ⚠️ Строка метрик пишется при смене статуса, а вопрос по встрече и
        # перегенерация темы случаются ПОСЛЕ того, как задача уже `done`: их
        # токены копились в задаче и до метрик не доезжали (И3). Перезаписываем
        # строку — она upsert по id задачи, дубля не будет.
        if job.status in (STATUS_DONE, STATUS_ERROR, STATUS_CANCELLED):
            from . import stats
            stats.record(job)

    def _maybe_verify(self, job: Job, result: dict, transcript: str) -> dict:
        """Д5: grounding pass over the fresh protocol (strict mode, on by
        default; per-team switch «strict_verify» in automation settings). Never
        breaks the job — on failure the protocol ships with verification.error."""
        if not result or job.analysis_prompt:
            return result   # expert-mode prompts may change the schema — skip
        try:
            from .automation import settings as auto_settings
            if not auto_settings.load(job.owner).get("strict_verify", True):
                return result
            from .analyze import verify_protocol
            return verify_protocol(
                result, transcript, user_notes=job.user_notes,
                provider=job.provider, keys=_owner_keys(job.owner),
                on_progress=self._on_analysis(job.id),
                cancel_check=lambda: self._control.get(job.id, {}).get("cancel"))
        except Exception as e:  # noqa: BLE001
            from .analyze import AnalysisCancelled
            if isinstance(e, AnalysisCancelled):
                raise
            result.setdefault("verification", {})["error"] = str(e)
            return result

    def _do_reanalyze(self, job: Job, txt: str) -> None:
        # Текст с экрана при пересборке раньше терялся: читался только txt.
        # Подмешиваем сохранённый OCR (уже маскированный) — как в первом прогоне.
        if "ТЕКСТ С ЭКРАНА" not in txt:
            sp = self.result_path(job.id, "screen.txt")
            if sp.exists():
                try:
                    blk = redact.redact_text(sp.read_text(encoding="utf-8")).strip()
                    if blk:
                        txt += "\n\n" + blk
                except OSError:
                    pass
        context = _participants_block(job) + _context_block(job)
        self._analysis[job.id] = {"stage": "Готовлю анализ…", "text": "", "chars": 0}
        self._set(job, status=STATUS_ANALYZING, analysis_error=None)
        try:
            from .analyze import analyze_transcript, AnalysisCancelled
            from .docx_export import generate_report

            with usage.collect() as acc:
                result = analyze_transcript(
                    txt, provider=job.provider,
                    extra_instructions=self._preset_extra(job),
                    custom_prompt=job.analysis_prompt,
                    on_progress=self._on_analysis(job.id),
                    cancel_check=lambda: self._control.get(job.id, {}).get("cancel"),
                    keys=_owner_keys(job.owner),
                    user_notes=job.user_notes, context=context)
                result = self._maybe_verify(job, result, txt)
            self._add_usage(job, acc)
            result = self._enforce_participants(job, result)
            self._remember_series(job, result)
            job.analysis = result
            self.prepare_weeek_tasks(job)
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
            # finished_at MUST be restored: reanalyze() nulls it, and retention
            # ages a job by finished_at — leaving it None made the job count as
            # old as its CREATION and get purged early.
            self._set(job, status=STATUS_DONE, analysis=result,
                      analysis_error=None, docx_providers=job.docx_providers,
                      finished_at=time.time())
        except AnalysisCancelled:
            # Transcript stays intact; just drop back to a finished state.
            self._control.pop(job.id, None)
            self._set(job, status=STATUS_DONE, finished_at=time.time(),
                      analysis_error="Сборка протокола отменена.")
        except Exception as e:
            self._set(job, status=STATUS_DONE, finished_at=time.time(),
                      analysis_error=_friendly_error(e))
        finally:
            self._reanalyze_lock.release()

    # ---- re-run a failed/cancelled job from scratch ------------------------
    def retry(self, job_id: str) -> Job:
        """Re-run recognition (and its options + optional protocol) for a failed
        or cancelled job, reusing the originally uploaded file if it still exists.
        Recognition can't resume mid-way, so this restarts the pipeline cleanly."""
        job = self._require(job_id)
        if job.status in (STATUS_QUEUED, STATUS_RUNNING, STATUS_PAUSED, STATUS_ANALYZING):
            # После перезапуска сервиса планировщик сам возвращает прерванную
            # задачу в очередь, но карточка ещё показывает прежнюю ошибку.
            # Человек жмёт «Повторить» и раньше получал невнятное «продолжить
            # можно только задачу с ошибкой» — теперь видно, что всё идёт своим
            # ходом и ждать нужно, а не чинить.
            raise ValueError("Задача уже в работе — она вернулась в очередь после "
                             "перезапуска сервиса. Повтор не нужен, дождитесь "
                             "распознавания.")
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
        if not (job.deliver_protocol_cloud or job.deliver_weeek_task):
            return ("у задачи не настроена доставка (не из планировщика и без "
                    "привязки к задаче Weeek)")
        provs = job.docx_providers or []
        if job.status != STATUS_DONE or not provs:
            return "pending"    # worker (or «Пересобрать») delivers later
        self._deliver_protocol(job, self.docx_path(job.id, provs[-1]))
        return job.delivery_error or "delivered"

    # ---- retention / cleanup -----------------------------------------------
    # Незавершённые задачи ретеншн НЕ трогает. Отбор шёл по возрасту
    # (finished_at или created_at) и не смотрел на статус, а воркер
    # распознавания один на всех: очередь из нескольких многочасовых встреч
    # спокойно уезжает за сутки (окно по умолчанию), и особенно после простоя
    # сервиса. Задача, до которой ещё не дошли руки, удалялась вместе с mp4
    # записью встречи — а если выгрузка в облако не прошла, записи не
    # оставалось нигде. Ждать нужно, пока задача не закончится: у done/error/
    # cancelled есть finished_at, от него срок и считается.
    _UNFINISHED = (STATUS_QUEUED, STATUS_RUNNING, STATUS_PAUSED, STATUS_ANALYZING)

    def _purge_old(self) -> None:
        """Delete jobs (and their files) older than the retention window."""
        max_age = config.RESULT_RETENTION_HOURS * 3600
        now = time.time()
        removed = False
        for job in list(self._jobs.values()):
            if job.status in self._UNFINISHED:
                continue
            ref = job.finished_at or job.created_at or now
            if now - ref > max_age:
                self._delete_job_files(job)
                if db.enabled():
                    try:
                        db.search_delete(job.id)
                    except Exception:  # noqa: BLE001
                        log.warning("Не удалось убрать задачу %s из индекса",
                                    job.id, exc_info=True)
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
            log.warning("Не удалось удалить исходник задачи %s", job.id, exc_info=True)

    def _cleaner_loop(self) -> None:
        while True:
            time.sleep(1800)  # every 30 min
            try:
                self._purge_old()
            except Exception:
                log.warning("Чистка старых задач сорвалась", exc_info=True)

    # ---- worker ------------------------------------------------------------
    def _set(self, job: Job, persist: bool = True, **kw) -> None:
        with self._lock:
            for k, v in kw.items():
                setattr(job, k, v)
            if persist:
                self._save(job)
            else:
                # Throttle disk writes for high-frequency progress updates.
                now = time.time()
                if now - self._last_persist > 3:
                    self._save(job)
                    self._last_persist = now
        # A finished job's outcome is recorded for the business metrics — job
        # rows themselves are purged by retention. Upsert by id, so re-running
        # (retry / reanalyze) just refreshes the row.
        # ⚠️ Отменённая задача тоже пишется в метрики. Раньше условие ловило
        # только done/error, и отменённая встреча исчезала бесследно — даже
        # как отказ (И2 в docs/ТЗ-МЕТРИКИ.md).
        if kw.get("status") in (STATUS_DONE, STATUS_ERROR, STATUS_CANCELLED):
            from . import stats
            stats.record(job)
            if kw.get("status") == STATUS_DONE:
                self.index_search(job)   # Д14: transcript+protocol become searchable

    def _worker_loop(self) -> None:
        while True:
            job_id = self._queue.get()
            job = self._jobs.get(job_id)
            if job is None or job.status != STATUS_QUEUED:
                continue
            self._process(job)

    # Whisper's initial_prompt biases decoding toward the spellings it has just
    # "read" — the cheapest fix for mangled имена/термины. ~224 tokens is the
    # window Whisper actually keeps of it, so pack by PRIORITY: the user's own
    # prompt, then people's names (standing context + tile captions from the
    # team's PAST videos), then project names, then the glossary's correct
    # spellings. The live tile scan of THIS video appends on top later.
    _PROMPT_TOKENS = 224

    # Tile-caption scans of past videos catch UI noise besides real names (chat
    # snippets, «Стоп запись», dates, URLs). Whisper's prompt budget is tiny —
    # only feed it strings that actually look like a person's name.
    _NAME_STOP = names.NAME_STOP      # общий список, см. app/names.py

    # Логика имён — в app/names.py, общая с speaker_id: раньше она жила только
    # здесь, и метки спикеров чистились по-своему (в списке участников «Зоя Р»,
    # а в тексте протокола «ЗояР»).
    @classmethod
    def _looks_like_name(cls, s: str) -> bool:
        return names.looks_like_name(s)

    @staticmethod
    def _strip_ui_badge(s: str) -> str:
        return names.normalise(s)

    def _auto_initial_prompt(self, job: Job) -> str:
        budget = self._PROMPT_TOKENS * 3          # ~3 chars per token
        parts: list[str] = []
        if (job.initial_prompt or "").strip():
            parts.append(job.initial_prompt.strip())
            budget -= len(parts[0])
        try:
            from . import ai_context
            team = _team(job.owner)
            names = ai_context.known_names(team)
            # Tile captions from the team's recent recordings: real people,
            # spelled exactly as Telemost shows them.
            # Multi-word captions are almost surely people («Мельников Алексей»);
            # single Cyrillic words might be («Елизавета») — queue them LAST;
            # single Latin words are Figma/UI leftovers of screen-share OCR — drop.
            seen = {n.lower() for n in names}
            multi, single = [], []
            for j in self.list(owner=team)[:20]:
                for n in (getattr(j, "video_participants", None) or []):
                    if not self._looks_like_name(n) or n.lower() in seen:
                        continue
                    seen.add(n.lower())
                    if " " in n.strip():
                        multi.append(n)
                    elif re.search(r"[а-яёА-ЯЁ]", n):
                        single.append(n)
            names += multi + single
            projects = ai_context.project_names(team)
        except Exception:  # prompt building must never break transcription
            names, projects = [], []
        rights = [right for _, right in glossary.parse(job.glossary)]

        def take(items: list[str], label: str) -> None:
            nonlocal budget
            picked: list[str] = []
            for it in items:
                cost = len(it) + 2
                if budget - cost < 0:
                    break
                picked.append(it)
                budget -= cost
            if picked:
                parts.append(f"{label}: {', '.join(picked)}.")

        take(names, "Участники")
        take(projects, "Проекты")
        take(list(dict.fromkeys(rights)), "Термины")
        return " ".join(parts).strip()

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
            initial_prompt = self._auto_initial_prompt(job)
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
                except Exception:  # особенность видео не должна ломать расшифровку
                    log.info("Имена участников с видео прочитать не удалось",
                             exc_info=True)

            _t0 = time.time()
            segments, meta = transcribe_file(
                job.audio_path, language=job.language, on_segment=on_segment,
                on_start=on_start, initial_prompt=initial_prompt,
                model_name=job.model or None,
            )
            # Чистое время распознавания. Мерить его как finished_at-started_at
            # нельзя: пересборка протокола сдвигает конец, начало остаётся от
            # первого прогона, и часовая встреча выглядела как семь часов
            # работы — на таких числах о скорости судить невозможно.
            self._set(job, persist=False, transcribe_sec=time.time() - _t0)
            # Запланированные встречи модель не указывают — она берётся из
            # VTX_MODEL. Раньше поле оставалось пустым, и по завершённым
            # задачам нельзя было понять, чем их считали: сравнить скорость
            # medium и large-v3-turbo по факту было не на чем. Записываем
            # ту модель, которая реально работала.
            if not job.model:
                self._set(job, persist=False,
                          model=str(meta.get("model") or config.MODEL))

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
            # Отмена ловится и МЕЖДУ этапами: распознавание уже позади, а
            # впереди разметка спикеров и OCR экрана — каждый на десятки минут.
            # Раньше «Стоп» здесь не действовал: проверка была только между
            # фрагментами распознавания, то есть в уже пройденном цикле.
            if ctrl.get("cancel"):
                raise JobCancelled()

            if job.identify_speakers:
                self._set_stage(job.id, "Определяю, кто говорил (разметка по видео)…")
                try:
                    from . import speaker_id
                    if speaker_id.is_video(job.audio_path):
                        segments = speaker_id.identify_speakers(
                            job.audio_path, segments,
                            should_stop=lambda: bool(ctrl.get("cancel")))
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
            # Проверка отмены — СНАРУЖИ try: внутри стоит `except Exception`,
            # который проглотил бы JobCancelled и превратил отмену в «ошибку
            # OCR», а задача продолжила бы выполняться.
            if ctrl.get("cancel"):
                raise JobCancelled()
            screen_block = ""
            screen_err = None
            screen_segs = 0
            if job.capture_screen:
                self._set_stage(job.id, "Читаю текст с экрана (слайды, документы)…")
                try:
                    from . import screen_ocr
                    if screen_ocr.is_video(job.audio_path):
                        items = screen_ocr.extract_screen_text(
                            job.audio_path,
                            should_stop=lambda: bool(ctrl.get("cancel")))
                        screen_block = screen_ocr.to_block(
                            items, speech_chars=len(txt_content))
                        screen_segs = len(items)
                        # Ключи и пароли с показанного экрана — не содержание
                        # встречи: маскируем ДО модели и ДО записи на диск.
                        screen_block, n_red = redact.redact(screen_block)
                        if n_red:
                            meta["screen_redacted"] = n_red
                            log.info("Задача %s: скрыто секретов с экрана: %d",
                                     job.id, n_red)
                        if screen_block:
                            self.result_path(job.id, "screen.txt").write_text(
                                screen_block, encoding="utf-8")
                    else:
                        screen_err = "Файл не является видео — нечего распознавать с экрана."
                except Exception as e:
                    screen_err = str(e)

            # Text fed to the protocol analysis = the TIMECODED transcript (with
            # speaker labels when known) + on-screen text + the participants read
            # off the call grid + the user's standing AI context. Timecodes let
            # the map-reduce keep the meeting's chronology and dedup overlap-zone
            # tasks; the prompts instruct the model not to leak them into output.
            analysis_input = txt_content + (("\n\n" + screen_block) if screen_block else "")
            # Контекст — ОТДЕЛЬНО от текста: он ставится перед каждым запросом
            # к модели, а не дописывается в хвост (см. analyze_transcript).
            analysis_context = _participants_block(job) + _context_block(job)

            # AI analysis + Word document (optional, requires ANTHROPIC_API_KEY)
            analysis_result = None
            analysis_err = None
            if job.analyze:
                self._analysis[job.id] = {"stage": "Готовлю анализ…", "text": "", "chars": 0}
                self._set_stage(job.id, "")     # дальше стадии показывает анализ
                self._set(job, status=STATUS_ANALYZING, persist=True)
                try:
                    from .analyze import analyze_transcript, AnalysisCancelled
                    from .docx_export import generate_report

                    with usage.collect() as acc:
                        analysis_result = analyze_transcript(
                            analysis_input, provider=job.provider,
                            extra_instructions=self._preset_extra(job),
                            custom_prompt=job.analysis_prompt,
                            on_progress=self._on_analysis(job.id),
                            cancel_check=lambda: self._control.get(job.id, {}).get("cancel"),
                            keys=_owner_keys(job.owner),
                            user_notes=job.user_notes, context=analysis_context)
                        analysis_result = self._maybe_verify(
                            job, analysis_result, analysis_input)
                    self._add_usage(job, acc)
                    analysis_result = self._enforce_participants(job, analysis_result)
                    self._remember_series(job, analysis_result)
                    job.analysis = analysis_result
                    self.prepare_weeek_tasks(job)
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
        except Exception as e:      # noqa: BLE001
            # В карточку — человеческое объяснение, трейсбек в журнал
            # контейнера. Раньше пользователь видел сырой стек Python и не мог
            # понять ни причины, ни что делать дальше.
            traceback.print_exc()
            self._set(
                job,
                status=STATUS_ERROR,
                error=(f"Сбой обработки: {e}. Запись цела — нажмите «Повторить». "
                       "Подробности в журнале сервиса "
                       "(docker compose logs app)."),
                finished_at=time.time(),
            )
        finally:
            # Исходник удаляем ТОЛЬКО у успешно завершённой задачи. finally
            # срабатывает и на ветке except, а карточка упавшей задачи прямым
            # текстом обещает «Запись цела — нажмите «Повторить»»: без исходника
            # кнопка отвечает отказом, и восстановить можно лишь вручную —
            # скачать видео из облака и загрузить заново. Планировщик ставит
            # delete_audio_when_done всем записям, если облако не локальное, то
            # есть на бою это касалось каждой упавшей встречи. Ровно ту же
            # ошибку уже чинили в _late_upload; здесь она осталась.
            if (getattr(job, "delete_audio_when_done", False)
                    and job.status == STATUS_DONE):
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
                    log.debug("Временный файл %s не удалён", p, exc_info=True)
        except Exception:
            log.warning("Чистка временных файлов задачи не удалась", exc_info=True)

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
    """Постоянный контекст команды (общий + проекты, подходящие к этой встрече)
    и карточка серии встреч с итогами прошлой встречи этой серии."""
    parts: list[str] = []
    try:
        from . import ai_context
        hint = f"{job.context_hint} {job.filename}"
        blk = ai_context.block_for(job.owner, hint)
        if blk:
            parts.append(blk)
    except Exception:
        log.debug("Постоянный контекст не собран", exc_info=True)
    try:
        blk = meeting_series.block_for(job.owner, job.context_hint or job.filename)
        if blk:
            parts.append(blk)
    except Exception:
        log.debug("Контекст серии не собран", exc_info=True)
    return "".join("\n\n" + p for p in parts)


_name_key = names.key      # общий ключ дедупликации, см. app/names.py


def _known_names(job: "Job") -> list[str]:
    """Имена, которые написал человек: постоянный контекст + карточка серии."""
    out: list[str] = []
    try:
        from . import ai_context
        out += ai_context.known_names(_team(job.owner))
    except Exception:  # noqa: BLE001
        pass
    try:
        out += meeting_series.known_names(job.owner, job.context_hint or job.filename)
    except Exception:  # noqa: BLE001
        pass
    return out


def _tile_names(job: "Job") -> list[str]:
    """Name-like tile captions of THIS call, deduped (scanner noise dropped).
    Искажённые OCR подписи («ЗЖКИРИЛЛ БУБНОВ», «Mapua H») приводятся к
    известным именам из контекста и карточки серии."""
    seen: set[str] = set()
    out: list[str] = []
    known = _known_names(job)
    for n in (getattr(job, "video_participants", None) or []):
        if not JobStore._looks_like_name(n):
            continue
        clean = names.canonical(JobStore._strip_ui_badge(n), known)
        key = _name_key(clean)
        if key in seen:
            continue
        seen.add(key)
        out.append(clean)
    return out


def _participants_block(job: "Job") -> str:
    """The «who is on the call» block for the LLM — names read off the video
    grid are authoritative, so the protocol lists exactly these people."""
    names = _tile_names(job)
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
