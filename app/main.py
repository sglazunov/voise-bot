"""FastAPI app: web upload UI + REST API for transcription jobs."""
from __future__ import annotations

import os
import shutil
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, PlainTextResponse, FileResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from . import config, llm, analyze
from .jobs import store, STATUS_DONE, STATUS_ANALYZING, STATUS_CANCELLED


def _provider_list() -> list[dict]:
    """Currently configured/available providers with display labels."""
    return [
        {"id": p, "label": config.PROVIDER_LABELS.get(p, p)}
        for p in config.available_providers()
    ]


def _ollama_label(name: str) -> str:
    """Human label for one installed Ollama model (marks custom / default)."""
    short = name.split(":")[0]
    default_short = config.OLLAMA_MODEL.split(":")[0]
    disp = name[:-7] if name.endswith(":latest") else name  # drop noisy :latest
    tags = []
    if short.startswith("vtx"):
        tags.append("настроенная")           # our Modelfile-built model
    if short == default_short:
        tags.append("по умолчанию")
    if not tags:
        tags.append("базовая")
    return f"Локально · {disp} ({', '.join(tags)})"


def _engine_list() -> list[dict]:
    """Engines for the UI picker: each installed Ollama model + cloud providers.

    Ollama models are returned as value "ollama:<model>"; cloud providers as
    their plain id. The tuned/default model is sorted first.
    """
    avail = config.available_providers()
    engines: list[dict] = []
    if "ollama" in avail:
        models = llm.list_ollama_models()
        default_short = config.OLLAMA_MODEL.split(":")[0]

        def rank(m: str) -> tuple:
            short = m.split(":")[0]
            return (0 if short == default_short else
                    1 if short.startswith("vtx") else 2, m)

        if models:
            for m in sorted(models, key=rank):
                engines.append({"value": f"ollama:{m}", "label": _ollama_label(m)})
        else:
            # Ollama enabled but server down / no models yet — offer the default.
            engines.append({"value": "ollama",
                            "label": f"Локально · {config.OLLAMA_MODEL} (по умолчанию)"})
    for p in avail:
        if p == "ollama":
            continue
        engines.append({"value": p, "label": config.PROVIDER_LABELS.get(p, p)})
    return engines

app = FastAPI(title="Voice Transcriber", version="1.0")
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


@app.on_event("startup")
def _start_scheduler() -> None:
    """Start the meeting-automation scheduler. It self-gates on the `enabled`
    setting, so it's safe to always run — it idles until turned on in the UI."""
    try:
        from .automation.scheduler import scheduler
        scheduler.start()
    except Exception:
        pass

ALLOWED_EXT = {".mp3", ".wav", ".m4a", ".ogg", ".oga", ".opus", ".flac", ".aac",
               ".mp4", ".mkv", ".webm", ".mov", ".wma", ".amr"}

# Whisper models selectable per job (accuracy vs speed).
ALLOWED_MODELS = ["tiny", "base", "small", "medium", "large-v3", "large-v3-turbo"]


# ---- Web UI ---------------------------------------------------------------
@app.get("/automation", response_class=HTMLResponse)
def automation_page(request: Request):
    """Settings + control panel for the meeting-automation pipeline."""
    return templates.TemplateResponse("automation.html", {"request": request})


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "jobs": [j.to_public() for j in store.list()],
            "diarization_enabled": config.DIARIZATION_ENABLED,
            "analysis_enabled": bool(config.available_providers()),
            "providers": _provider_list(),
            "model": config.MODEL,
            "models": ALLOWED_MODELS,
            "max_upload_mb": config.MAX_UPLOAD_MB,
        },
    )


# ---- REST API -------------------------------------------------------------
@app.post("/api/jobs")
async def create_job(
    file: UploadFile = File(...),
    language: str = Form(config.DEFAULT_LANGUAGE),
    model: str = Form(""),
    diarize: bool = Form(False),
    analyze: bool = Form(False),
    provider: str = Form("auto"),
    hint: str = Form(""),
    glossary: str = Form(""),
    instructions: str = Form(""),
    custom_prompt: str = Form(""),
    capture_screen: bool = Form(False),
):
    ext = Path(file.filename or "").suffix.lower()
    if ext not in ALLOWED_EXT:
        raise HTTPException(400, f"Неподдерживаемый формат: {ext or '?'}")

    dest = config.UPLOAD_DIR / f"{_safe_stem(file.filename)}{ext}"
    size = 0
    limit = config.MAX_UPLOAD_MB * 1024 * 1024
    with dest.open("wb") as out:
        while chunk := await file.read(1024 * 1024):
            size += len(chunk)
            if size > limit:
                out.close()
                dest.unlink(missing_ok=True)
                raise HTTPException(413, f"Файл больше {config.MAX_UPLOAD_MB} МБ")
            out.write(chunk)

    want_diar = diarize  # honour the UI toggle; readiness is reported separately
    want_analyze = analyze and bool(config.available_providers())
    model_sel = model.strip() if model.strip() in ALLOWED_MODELS else ""
    job = store.create(file.filename, str(dest), language, want_diar,
                       initial_prompt=hint.strip(), glossary=glossary.strip(),
                       analyze=want_analyze, provider=provider,
                       analysis_instructions=instructions.strip(),
                       analysis_prompt=custom_prompt.strip(),
                       capture_screen=capture_screen, model=model_sel)
    return JSONResponse({"job_id": job.id, **job.to_public()}, status_code=201)


@app.get("/api/jobs")
def list_jobs():
    return [j.to_public() for j in store.list()]


# ---- LLM providers (protocol engine) --------------------------------------
class ProviderKey(BaseModel):
    provider: str
    api_key: str
    extra: str = ""   # YandexGPT: folder id · GigaChat: scope (optional)


@app.get("/api/providers")
def list_providers():
    """All known providers + which are currently usable (for the UI picker)."""
    avail = set(config.available_providers())
    return {
        "available": config.available_providers(),
        "engines": _engine_list(),
        "ollama_status": llm.ollama_status(),
        "ollama_install_url": "https://ollama.com/download",
        "providers": [
            {
                "id": p,
                "label": config.PROVIDER_LABELS.get(p, p),
                "available": p in avail,
                "needs_key": p in config.KEY_PROVIDERS,
            }
            for p in config.PROVIDER_ORDER
        ],
    }


@app.post("/api/providers/connect")
def connect_provider(body: ProviderKey):
    """Set an API key at runtime and verify it with a tiny live call.

    The key is held in memory only (not persisted to disk). On success the
    provider becomes selectable in the engine picker immediately.
    """
    provider = body.provider.strip().lower()
    key = body.api_key.strip()
    extra = body.extra.strip()
    if provider not in config.KEY_PROVIDERS:
        raise HTTPException(400, f"Подключение по ключу не поддерживается для '{provider}'")
    if not key:
        raise HTTPException(400, "Введите ключ")
    if provider == "yandex" and not extra:
        raise HTTPException(400, "Для YandexGPT укажите folder id (идентификатор каталога)")

    # Tentatively set the credentials, then validate with a cheap non-JSON ping
    # so bad credentials fail fast and are rolled back.
    config.set_provider_key(provider, key, extra)
    try:
        llm.get_provider(provider).complete("Ответь одним словом: ok",
                                            max_tokens=5, force_json=False)
    except Exception as e:
        config.set_provider_key(provider, "", "")  # roll back the bad key
        raise HTTPException(400, f"Не удалось подключиться: {e}")

    return {"ok": True, "connected": provider, "providers": _provider_list()}


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str):
    job = store.get(job_id)
    if not job:
        raise HTTPException(404, "Задача не найдена")
    return job.to_public()


@app.post("/api/jobs/{job_id}/pause")
def pause_job(job_id: str):
    return _control(job_id, "pause")


@app.post("/api/jobs/{job_id}/resume")
def resume_job(job_id: str):
    return _control(job_id, "resume")


@app.post("/api/jobs/{job_id}/cancel")
def cancel_job(job_id: str):
    return _control(job_id, "cancel")


class ReanalyzeBody(BaseModel):
    provider: str = "auto"
    instructions: str | None = None
    custom_prompt: str | None = None


@app.post("/api/jobs/{job_id}/reanalyze")
def reanalyze_job(job_id: str, body: ReanalyzeBody):
    try:
        job = store.reanalyze(job_id, provider=body.provider,
                              instructions=body.instructions,
                              custom_prompt=body.custom_prompt)
    except KeyError:
        raise HTTPException(404, "Задача не найдена")
    except ValueError as e:
        raise HTTPException(409, str(e))
    return job.to_public()


@app.get("/api/prompt/default")
def prompt_default():
    """The built-in analysis prompt, for the expert-mode editor."""
    return {"prompt": analyze.EXPERT_PROMPT_DEFAULT}


def _control(job_id: str, action: str):
    fn = {"pause": store.pause, "resume": store.resume, "cancel": store.cancel}[action]
    try:
        job = fn(job_id)
    except KeyError:
        raise HTTPException(404, "Задача не найдена")
    except ValueError as e:
        raise HTTPException(409, str(e))
    return job.to_public()


@app.get("/api/jobs/{job_id}/partial")
def get_partial(job_id: str):
    """Live transcript-so-far for the streaming UI."""
    job = store.get(job_id)
    if not job:
        raise HTTPException(404, "Задача не найдена")
    return {
        "status": job.status,
        "progress": job.progress,
        "segments": store.partial(job_id),
        "analysis": store.analysis_progress(job_id),
    }


@app.get("/api/jobs/{job_id}/result")
def get_result(job_id: str, format: str = "txt", provider: str = ""):
    job = store.get(job_id)
    if not job:
        raise HTTPException(404, "Задача не найдена")
    if format not in {"txt", "srt", "json", "docx", "screen"}:
        raise HTTPException(400, "format должен быть txt | srt | json | docx | screen")

    # Word protocol: one document per engine. Pick the requested provider, or
    # default to the most recently generated one.
    if format == "docx":
        prov = provider or (job.docx_providers[-1] if job.docx_providers else "")
        if not prov:
            raise HTTPException(404, "Протокол ещё не сгенерирован")
        path = store.docx_path(job_id, prov)
        if not path.exists():
            raise HTTPException(404, "Протокол для этого движка отсутствует")
        return FileResponse(
            path,
            media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            filename=f"{_safe_stem(job.filename)}_{prov}_протокол.docx",
        )

    # Allow download whenever the file exists — covers finished jobs and the
    # partial transcript saved when a job is cancelled. Still-running jobs
    # simply have no file yet and fall through to 404 below.
    fmt_file = "screen.txt" if format == "screen" else format
    path = store.result_path(job_id, fmt_file)
    if not path.exists():
        if job.status not in {STATUS_DONE, STATUS_CANCELLED}:
            raise HTTPException(409, f"Задача ещё не готова (статус: {job.status})")
        raise HTTPException(404, "Результат отсутствует")
    if format in ("txt", "screen"):
        return PlainTextResponse(path.read_text(encoding="utf-8"))
    media = "application/json" if format == "json" else "text/plain"
    return FileResponse(path, media_type=media,
                        filename=f"{_safe_stem(job.filename)}.{format}")


def _safe_stem(name: str | None) -> str:
    stem = Path(name or "audio").stem
    keep = "".join(c if c.isalnum() or c in "-_ " else "_" for c in stem).strip()
    return (keep or "audio")[:80]


@app.get("/api/diarization/status")
def diarization_status():
    """What's needed for speaker diarization (for the UI toggle hints)."""
    from .diarize import readiness
    return readiness()


class HfToken(BaseModel):
    token: str


@app.post("/api/diarization/token")
def diarization_token(body: HfToken):
    """Set the HuggingFace token (for diarization) at runtime; report readiness."""
    from .diarize import readiness
    token = body.token.strip()
    if not token:
        raise HTTPException(400, "Введите токен")
    config.set_hf_token(token)
    return readiness()


@app.get("/api/screen/status")
def screen_status():
    """What's needed for on-screen text capture (OCR) — for the UI toggle hints."""
    from .screen_ocr import readiness
    return readiness()


@app.get("/api/ollama/status")
def ollama_status():
    """Whether the local engine (Ollama) is installed/ready + install progress."""
    from . import ollama_setup
    return ollama_setup.status()


@app.post("/api/ollama/install")
def ollama_install():
    """Download & install Ollama + the protocol model on demand (background)."""
    from . import ollama_setup
    return ollama_setup.install()


@app.post("/api/ollama/install/cancel")
def ollama_install_cancel():
    """Request cancellation of an in-progress Ollama install."""
    from . import ollama_setup
    return ollama_setup.cancel()


# ---- Optional dependencies (install into the app's venv from the UI) -------
@app.get("/api/setup/deps")
def deps_status():
    """Readiness + install progress of optional deps (playwright/diariz/ffmpeg)."""
    from . import deps_setup
    return deps_setup.status()


@app.post("/api/setup/deps/{component}/install")
def deps_install(component: str):
    """Install one optional dependency into the app's venv (background)."""
    from . import deps_setup
    if component not in deps_setup.COMPONENTS:
        raise HTTPException(404, "Неизвестный компонент")
    return deps_setup.install(component)


# ---- Recognition models (pre-download from the UI, no transcription) -------
@app.get("/api/model/status")
def model_status(name: str = ""):
    """Whether a Whisper model is downloaded + download progress."""
    from . import whisper_setup
    return whisper_setup.status(name)


@app.post("/api/model/download")
def model_download(name: str = ""):
    """Download a Whisper model into the cache (background, no transcription)."""
    from . import whisper_setup
    return whisper_setup.download(name)


# --------------------------------------------------------------------------- #
# Meeting automation (Weeek → record → cloud → protocol). See app/automation.
# --------------------------------------------------------------------------- #
@app.get("/api/automation/settings")
def automation_settings():
    """Current automation settings (secrets redacted to presence flags)."""
    from .automation import settings as auto_settings
    return auto_settings.redacted()


class AutomationSettings(BaseModel):
    weeek_token: str | None = None
    weeek_project_id: str | int | None = None
    timezone: str | None = None
    cloud: str | None = None
    local_dir: str | None = None
    yandex_disk: dict | None = None
    gdrive: dict | None = None
    enabled: bool | None = None
    do_transcribe: bool | None = None
    do_protocol: bool | None = None
    ocr_screen: bool | None = None
    analyze_provider: str | None = None
    poll_interval_sec: int | None = None
    lookahead_min: int | None = None
    bot_join_name: str | None = None
    post_back_to_weeek: bool | None = None
    # recorder
    record_mode: str | None = None
    auth_mode: str | None = None
    browser_profile_dir: str | None = None
    ffmpeg_path: str | None = None
    audio_device: str | None = None
    capture_video: bool | None = None
    headless: bool | None = None
    join_timeout_sec: int | None = None
    end_when_alone_sec: int | None = None
    min_participants: int | None = None
    max_meeting_min: int | None = None
    # which meetings to auto-record (empty = all)
    rec_time_from: str | None = None
    rec_time_to: str | None = None
    rec_days: list | None = None
    rec_include: str | None = None
    rec_exclude: str | None = None
    rec_default_on: bool | None = None
    rec_decisions: dict | None = None


@app.post("/api/automation/settings")
def automation_save(body: AutomationSettings):
    """Persist automation settings. Only non-null fields are updated."""
    from .automation import settings as auto_settings
    values = {k: v for k, v in body.model_dump().items() if v is not None}
    auto_settings.save(values)
    return auto_settings.redacted()


@app.get("/api/automation/meetings")
def automation_meetings():
    """Upcoming meeting tasks from Weeek that carry a Telemost link."""
    from .automation import settings as auto_settings, weeek
    from datetime import timezone
    cfg = auto_settings.load()
    token = cfg.get("weeek_token")
    if not token:
        raise HTTPException(400, "Сначала задайте токен Weeek в настройках.")
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo(cfg.get("timezone") or "Europe/Moscow")
    except Exception:
        tz = timezone.utc
    try:
        meetings = weeek.upcoming_meetings(token, cfg.get("weeek_project_id"), tz)
    except weeek.WeeekError as e:
        raise HTTPException(502, str(e))
    decisions = cfg.get("rec_decisions") or {}
    return {"default_on": bool(cfg.get("rec_default_on", True)),
            "meetings": [
        {"task_id": m.task_id, "title": m.title, "url": m.url,
         "start": m.start.isoformat() if m.start else None,
         "project_id": m.project_id,
         "decision": decisions.get(str(m.task_id))}  # True/False/None
        for m in meetings
    ]}


class MeetingDecision(BaseModel):
    record: bool | None = None  # True=record, False=skip, None=use default mode


@app.post("/api/automation/meetings/{task_id}/decision")
def automation_meeting_decision(task_id: str, body: MeetingDecision):
    """Choose whether the bot records this specific meeting (overrides filters)."""
    from .automation.scheduler import scheduler
    return scheduler.set_decision(task_id, body.record)


@app.get("/api/automation/clouds/status")
def automation_clouds_status():
    """Per-backend cloud readiness + which one is selected (for the UI)."""
    from .automation import settings as auto_settings, clouds
    return clouds.readiness(auto_settings.load())


@app.post("/api/automation/clouds/test")
def automation_clouds_test(backend: str | None = None):
    """Upload a tiny test file to the selected (or given) cloud to verify creds."""
    import tempfile
    from datetime import datetime, timezone
    from .automation import settings as auto_settings, clouds
    cfg = auto_settings.load()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False,
                                     encoding="utf-8") as f:
        f.write("voice-transcriber cloud test\n")
        tmp = f.name
    try:
        res = clouds.upload(tmp, f"vtx-test-{stamp}.txt", cfg, backend=backend)
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass
    if not res.get("ok"):
        raise HTTPException(502, res.get("error") or "Не удалось загрузить.")
    return res


@app.get("/api/automation/scheduler/status")
def automation_scheduler_status():
    """Scheduler state + the meetings it's tracking and their pipeline status."""
    from .automation.scheduler import scheduler
    scheduler.start()  # idempotent — ensures it's running even if startup was skipped
    return scheduler.status()


@app.post("/api/automation/scheduler/run-now")
def automation_scheduler_run_now(task_id: str):
    """Manually record a known meeting right now (poll Weeek first to populate)."""
    from .automation.scheduler import scheduler
    res = scheduler.run_now(task_id)
    if not res.get("ok"):
        raise HTTPException(400, res.get("error"))
    return res


@app.post("/api/automation/scheduler/stop-recording")
def automation_scheduler_stop_recording():
    """Stop the recording in progress (e.g. the main meeting is over)."""
    from .automation.scheduler import scheduler
    res = scheduler.stop_recording()
    if not res.get("ok"):
        raise HTTPException(400, res.get("error"))
    return res


@app.get("/api/automation/recorder/status")
def automation_recorder_status():
    """What the Telemost recorder needs (Playwright/ffmpeg/audio) — for the UI."""
    from .automation import settings as auto_settings, recorder
    return recorder.readiness(auto_settings.load())


@app.get("/api/automation/recorder/audio-devices")
def automation_recorder_audio_devices():
    """List dshow audio devices ffmpeg can capture (Windows)."""
    from .automation import settings as auto_settings
    from .automation.recorder import capture
    cfg = auto_settings.load()
    return {"devices": capture.list_audio_devices(cfg.get("ffmpeg_path") or "ffmpeg")}


@app.get("/api/automation/recorder/login-status")
def automation_recorder_login_status():
    """Whether the recorder profile is logged into Yandex (for the UI hint)."""
    from .automation import settings as auto_settings
    from .automation.recorder import browser
    return browser.login_status(auto_settings.load())


@app.get("/api/automation/recorder/audio-test")
def automation_recorder_audio_test():
    """Record a few seconds from the chosen audio device and report its level —
    so the user can verify the meeting's sound actually reaches it."""
    from .automation import settings as auto_settings
    from .automation.recorder import capture
    cfg = auto_settings.load()
    return capture.test_audio_level(cfg.get("ffmpeg_path") or "ffmpeg",
                                    (cfg.get("audio_device") or "").strip())


@app.post("/api/automation/recorder/login")
def automation_recorder_login():
    """Open a headed browser so the user logs into Yandex once (profile mode)."""
    import threading
    from .automation import settings as auto_settings
    from .automation.recorder import browser
    if not browser.playwright_available():
        raise HTTPException(400, "Playwright не установлен: pip install playwright "
                                 "&& playwright install chromium")
    cfg = auto_settings.load()
    threading.Thread(target=browser.login, args=(cfg,), daemon=True).start()
    return {"started": True,
            "detail": "Открывается окно браузера — войдите в Яндекс и закройте его."}


class RecorderTest(BaseModel):
    url: str
    seconds: int = 30


@app.post("/api/automation/recorder/test")
def automation_recorder_test(body: RecorderTest):
    """Manually join a Telemost link and record for a few seconds, to verify
    the bot + capture work on this machine before automating."""
    import time
    from .automation import settings as auto_settings, recorder
    cfg = auto_settings.load()
    out = str(config.DATA_DIR / "recordings" /
              f"test-{time.strftime('%Y%m%d-%H%M%S')}.mp4")
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    deadline = time.time() + max(5, min(int(body.seconds), 300))
    logs: list[str] = []
    res = recorder.record_meeting(
        body.url, out, cfg,
        on_log=lambda m: logs.append(str(m)),
        should_stop=lambda: time.time() > deadline)
    res["logs"] = logs
    return res


@app.get("/api/automation/weeek/projects")
def automation_weeek_projects():
    """List the workspace's projects (id + name) so the user can pick which one
    to record. The token sees all projects; `projectId` is what scopes it."""
    from .automation import settings as auto_settings, weeek
    token = auto_settings.get("weeek_token")
    if not token:
        raise HTTPException(400, "Сначала задайте токен Weeek и нажмите «Сохранить».")
    try:
        return {"projects": weeek.list_projects(token)}
    except weeek.WeeekError as e:
        raise HTTPException(502, str(e))


@app.get("/api/automation/weeek/probe")
def automation_weeek_probe(task_id: str):
    """Return the raw JSON of one Weeek task — used to pin date/link field names."""
    from .automation import settings as auto_settings, weeek
    token = auto_settings.get("weeek_token")
    if not token:
        raise HTTPException(400, "Сначала задайте токен Weeek в настройках.")
    try:
        return weeek.probe_task(token, task_id)
    except weeek.WeeekError as e:
        raise HTTPException(502, str(e))


@app.get("/healthz")
def healthz():
    return {"ok": True, "model": config.MODEL, "diarization": config.DIARIZATION_ENABLED}
