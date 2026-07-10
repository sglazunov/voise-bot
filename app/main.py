"""FastAPI app: web upload UI + REST API for transcription jobs."""
from __future__ import annotations

import os
import shutil
from pathlib import Path

from fastapi import (Depends, FastAPI, File, Form, HTTPException, Request,
                     Response, UploadFile)
from fastapi.responses import (HTMLResponse, PlainTextResponse, FileResponse,
                               JSONResponse, RedirectResponse)
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from . import config, llm, analyze, security, user_creds
from .jobs import store, STATUS_DONE, STATUS_ANALYZING, STATUS_CANCELLED


def _provider_list(user_keys: dict | None = None) -> list[dict]:
    """Currently configured/available providers with display labels."""
    return [
        {"id": p, "label": config.PROVIDER_LABELS.get(p, p)}
        for p in config.available_providers(user_keys)
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


def _engine_list(user_keys: dict | None = None) -> list[dict]:
    """Engines for the UI picker: each installed Ollama model + cloud providers.

    Ollama models are returned as value "ollama:<model>"; cloud providers as
    their plain id. The tuned/default model is sorted first.
    """
    avail = config.available_providers(user_keys)
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
        tiers = config.PROVIDER_MODELS.get(p)
        if tiers:
            # One entry per model tier so the user picks how powerful it is.
            # The tier label already names the brand (Llama / GigaChat / …).
            for t in tiers:
                engines.append({"value": f"{p}:{t['value']}", "label": t["label"]})
        else:
            engines.append({"value": p, "label": config.PROVIDER_LABELS.get(p, p)})
    return engines

app = FastAPI(title="Voice Transcriber", version="1.0")
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


# ===========================================================================
# Authentication gate
# ===========================================================================
# Paths reachable WITHOUT a session. Everything else requires login.
_PUBLIC_PATHS = {"/login", "/register", "/recover", "/healthz",
                 "/api/auth/login", "/api/auth/register", "/api/auth/recover"}
_SECURE_COOKIE = os.getenv("VTX_HTTPS", "0") == "1"


@app.middleware("http")
async def _auth_gate(request: Request, call_next):
    """Resolve the session into request.state.user; block everything else.

    HTML routes redirect to /login; API routes get a 401. This is the single
    choke point that makes the whole site private on a public domain."""
    user = security.session_user(request.cookies.get(security.SESSION_COOKIE))
    request.state.user = user
    path = request.url.path
    if user or path in _PUBLIC_PATHS:
        resp = await call_next(request)
    elif path.startswith("/api/"):
        resp = JSONResponse({"detail": "Требуется вход."}, status_code=401)
    else:
        resp = RedirectResponse("/login", status_code=303)
    # Baseline security headers on every response.
    resp.headers.setdefault("X-Frame-Options", "DENY")           # clickjacking
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("Referrer-Policy", "same-origin")
    if _SECURE_COOKIE:  # only meaningful once TLS is in front
        resp.headers.setdefault("Strict-Transport-Security",
                                "max-age=31536000; includeSubDomains")
    return resp


def _client_ip(request: Request) -> str:
    """Client IP for rate limiting; honours the reverse proxy's X-Forwarded-For
    only when explicitly trusted (VTX_TRUST_PROXY=1, set alongside the proxy)."""
    if os.getenv("VTX_TRUST_PROXY", "0") == "1":
        xff = request.headers.get("x-forwarded-for", "")
        if xff:
            return xff.split(",")[0].strip()
    return request.client.host if request.client else "?"


def require_admin(request: Request) -> str:
    """Dependency for endpoints that change GLOBAL server state (installs,
    server-wide tokens): only the administrator (first user) may call them."""
    user = getattr(request.state, "user", None)
    if not user:
        raise HTTPException(401, "Требуется вход.")
    if not security.is_admin(user):
        raise HTTPException(403, "Только администратор (первый пользователь) "
                                 "может менять серверные настройки.")
    return user


def current_user(request: Request) -> str:
    """Dependency: the authenticated login (the gate guarantees it is set)."""
    user = getattr(request.state, "user", None)
    if not user:
        raise HTTPException(401, "Требуется вход.")
    return user


def _set_session_cookie(resp: Response, token: str) -> None:
    resp.set_cookie(security.SESSION_COOKIE, token, httponly=True,
                    samesite="lax", secure=_SECURE_COOKIE,
                    max_age=security.SESSION_TTL, path="/")


class Credentials(BaseModel):
    username: str
    password: str
    code: str = ""
    phone: str = ""   # required at registration; used for password recovery


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    if getattr(request.state, "user", None):
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(
        "login.html", {"request": request, "mode": "login",
                       "first_run": not security.list_users()})


@app.get("/register", response_class=HTMLResponse)
def register_page(request: Request):
    return templates.TemplateResponse(
        "login.html", {"request": request, "mode": "register",
                       "first_run": not security.list_users()})


@app.post("/api/auth/register")
def auth_register(body: Credentials, request: Request):
    ip = _client_ip(request)
    wait = security.throttle_check(f"reg:{ip}")
    if wait:
        raise HTTPException(429, f"Слишком много попыток. Подождите {wait} с.")
    res = security.create_user(body.username, body.password, body.code, body.phone)
    if not res.get("ok"):
        security.throttle_fail(f"reg:{ip}")  # wrong/guessed registration codes
        raise HTTPException(400, res.get("error") or "Не удалось зарегистрироваться.")
    token = security.create_session(res["username"])
    resp = JSONResponse({"ok": True, "username": res["username"]})
    _set_session_cookie(resp, token)
    return resp


@app.post("/api/auth/login")
def auth_login(body: Credentials, request: Request):
    ip = _client_ip(request)
    uname = security.normalize_username(body.username)
    keys = (f"login:{ip}", f"login:{ip}:{uname}")
    wait = security.throttle_check(*keys)
    if wait:
        raise HTTPException(429, f"Слишком много неудачных попыток. Подождите {wait} с.")
    if not security.verify_user(body.username, body.password):
        security.throttle_fail(*keys)
        raise HTTPException(401, "Неверный логин или пароль.")
    security.throttle_clear(*keys)
    token = security.create_session(body.username)
    resp = JSONResponse({"ok": True, "username": uname})
    _set_session_cookie(resp, token)
    return resp


@app.post("/api/auth/logout")
def auth_logout(request: Request):
    security.destroy_session(request.cookies.get(security.SESSION_COOKIE))
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(security.SESSION_COOKIE, path="/")
    return resp


@app.get("/api/auth/me")
def auth_me(user: str = Depends(current_user)):
    return {"username": user}


# ---- Password recovery by phone (public, heavily throttled) ---------------
@app.get("/recover", response_class=HTMLResponse)
def recover_page(request: Request):
    return templates.TemplateResponse(
        "login.html", {"request": request, "mode": "recover", "first_run": False})


class RecoverBody(BaseModel):
    username: str
    phone: str
    new_password: str


@app.post("/api/auth/recover")
def auth_recover(body: RecoverBody, request: Request):
    """Reset the password when login + registered phone match. Every failure
    counts against the brute-force window (phone numbers are guessable, so the
    throttle is what makes this safe on a public domain)."""
    ip = _client_ip(request)
    uname = security.normalize_username(body.username)
    keys = (f"rec:{ip}", f"rec:{ip}:{uname}")
    wait = security.throttle_check(*keys)
    if wait:
        raise HTTPException(429, f"Слишком много попыток. Подождите {wait} с.")
    res = security.reset_password_by_phone(body.username, body.phone, body.new_password)
    if not res.get("ok"):
        security.throttle_fail(*keys)
        raise HTTPException(400, res.get("error") or "Не удалось восстановить пароль.")
    security.throttle_clear(*keys)
    return {"ok": True, "detail": "Пароль изменён — войдите с новым паролем."}


# ---- Profile (phone + password management) ---------------------------------
@app.get("/profile", response_class=HTMLResponse)
def profile_page(request: Request, user: str = Depends(current_user)):
    return templates.TemplateResponse("profile.html", {"request": request})


@app.get("/api/profile")
def profile_info(user: str = Depends(current_user)):
    return {"username": user, "phone_masked": security.masked_phone(user),
            "is_admin": security.is_admin(user)}


class PhoneChange(BaseModel):
    password: str
    phone: str


@app.post("/api/profile/phone")
def profile_change_phone(body: PhoneChange, user: str = Depends(current_user)):
    """Change the recovery phone; requires the CURRENT password."""
    res = security.set_phone(user, body.password, body.phone)
    if not res.get("ok"):
        raise HTTPException(400, res.get("error"))
    return {"ok": True, "phone_masked": security.masked_phone(user)}


class PasswordChange(BaseModel):
    new_password: str
    old_password: str = ""
    phone: str = ""


@app.post("/api/profile/password")
def profile_change_password(body: PasswordChange, user: str = Depends(current_user)):
    """Change the password, confirming identity by old password OR by phone."""
    res = security.change_password(user, body.new_password,
                                   old=body.old_password or None,
                                   phone=body.phone or None)
    if not res.get("ok"):
        raise HTTPException(400, res.get("error"))
    return {"ok": True}


@app.on_event("startup")
def _start_scheduler() -> None:
    """Start the meeting-automation scheduler. It self-gates on the `enabled`
    setting, so it's safe to always run — it idles until turned on in the UI."""
    try:
        from .automation.scheduler import scheduler
        scheduler.start()
    except Exception:
        pass
    # First-run auto-setup: install whatever this machine is missing (OCR engine,
    # ffmpeg, Chromium for the bot) and pre-download all speech models — in the
    # background, no clicks. Disable with VTX_AUTO_SETUP=0.
    if os.getenv("VTX_AUTO_SETUP", "1") == "1":
        try:
            from . import autosetup
            autosetup.ensure_all()
        except Exception:
            pass
    elif os.getenv("VTX_PRELOAD_MODELS", "1") == "1":
        try:
            from . import whisper_setup
            whisper_setup.preload_all()
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
def index(request: Request, user: str = Depends(current_user)):
    uk = user_creds.load(user)
    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "username": user,
            "jobs": [j.to_public() for j in store.list(owner=user)],
            "diarization_enabled": config.DIARIZATION_ENABLED,
            "analysis_enabled": bool(config.available_providers(uk)),
            "providers": _provider_list(uk),
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
    identify_speakers: bool = Form(False),
    deliver_protocol_cloud: bool = Form(False),
    deliver_weeek_task: str = Form(""),
    user: str = Depends(current_user),
):
    ext = Path(file.filename or "").suffix.lower()
    if ext not in ALLOWED_EXT:
        raise HTTPException(400, f"Неподдерживаемый формат: {ext or '?'}")

    # Unique per upload: two users uploading files with the same name must not
    # overwrite each other's source (job.filename keeps the original for display).
    import uuid as _uuid
    dest = config.UPLOAD_DIR / f"{_uuid.uuid4().hex[:10]}__{_safe_stem(file.filename)}{ext}"
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
    want_analyze = analyze and bool(config.available_providers(user_creds.load(user)))
    model_sel = model.strip() if model.strip() in ALLOWED_MODELS else ""
    job = store.create(file.filename, str(dest), language, want_diar,
                       initial_prompt=hint.strip(), glossary=glossary.strip(),
                       analyze=want_analyze, provider=provider,
                       analysis_instructions=instructions.strip(),
                       analysis_prompt=custom_prompt.strip(),
                       capture_screen=capture_screen,
                       identify_speakers=identify_speakers,
                       deliver_protocol_cloud=deliver_protocol_cloud,
                       deliver_weeek_task=deliver_weeek_task, model=model_sel,
                       owner=user)
    return JSONResponse({"job_id": job.id, **job.to_public()}, status_code=201)


@app.get("/api/jobs")
def list_jobs(user: str = Depends(current_user)):
    return [j.to_public() for j in store.list(owner=user)]


# ---- LLM providers (protocol engine) --------------------------------------
class ProviderKey(BaseModel):
    provider: str
    api_key: str
    extra: str = ""   # YandexGPT: folder id · GigaChat: scope (optional)


@app.get("/api/providers")
def list_providers(user: str = Depends(current_user)):
    """All known providers + which are usable for THIS user (their own keys)."""
    uk = user_creds.load(user)
    avail = set(config.available_providers(uk))
    counts = user_creds.counts(user)  # how many personal keys per provider
    return {
        "available": config.available_providers(uk),
        "engines": _engine_list(uk),
        "ollama_status": llm.ollama_status(),
        "ollama_install_url": "https://ollama.com/download",
        "providers": [
            {
                "id": p,
                "label": config.PROVIDER_LABELS.get(p, p),
                "available": p in avail,
                "needs_key": p in config.KEY_PROVIDERS,
                "keys": counts.get(p, 0),
            }
            for p in config.PROVIDER_ORDER
        ],
    }


@app.post("/api/providers/connect")
def connect_provider(body: ProviderKey, user: str = Depends(current_user)):
    """Save an LLM API key IN THIS USER'S ACCOUNT (encrypted) and verify it.

    The key is stored per-user: entered once, isolated from other users, and it
    survives restarts. On success the provider becomes selectable for this user.
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

    # Verify the key with a cheap non-JSON ping BEFORE saving, so a bad key
    # fails fast and nothing is persisted.
    trial = {provider: [{"key": key, "extra": extra}]}
    try:
        llm.get_provider(provider, trial).complete("Ответь одним словом: ok",
                                                   max_tokens=5, force_json=False)
    except Exception as e:
        raise HTTPException(400, f"Не удалось подключиться: {e}")
    # Append to the provider's key POOL (several keys rotate on rate limits).
    user_creds.add(user, provider, key, extra)
    return {"ok": True, "connected": provider,
            "keys": user_creds.counts(user).get(provider, 1),
            "providers": _provider_list(user_creds.load(user))}


@app.post("/api/providers/disconnect")
def disconnect_provider(body: ProviderKey, user: str = Depends(current_user)):
    """Remove ALL of this user's saved keys for a provider."""
    user_creds.clear(user, body.provider.strip().lower())
    return {"ok": True, "providers": _provider_list(user_creds.load(user))}


@app.get("/api/providers/keys")
def provider_keys(provider: str, user: str = Depends(current_user)):
    """This user's saved keys for a provider, MASKED — to browse/remove them."""
    prov = provider.strip().lower()
    entries = user_creds.load(user).get(prov) or []

    def mask(k: str) -> str:
        k = k or ""
        return "•" * len(k) if len(k) <= 8 else f"{k[:4]}…{k[-4:]}"

    return {"provider": prov, "keys": [
        {"index": i, "masked": mask(e.get("key", "")), "extra": (e.get("extra") or "")}
        for i, e in enumerate(entries)]}


class KeyRef(BaseModel):
    provider: str
    index: int


@app.post("/api/providers/keys/remove")
def provider_keys_remove(body: KeyRef, user: str = Depends(current_user)):
    """Remove ONE saved key of a provider by its index."""
    prov = body.provider.strip().lower()
    user_creds.remove_at(user, prov, body.index)
    return {"ok": True, "keys": user_creds.counts(user).get(prov, 0),
            "providers": _provider_list(user_creds.load(user))}


def _require_owned(job_id: str, user: str):
    job = store.get_owned(job_id, user)
    if not job:
        raise HTTPException(404, "Задача не найдена")
    return job


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str, user: str = Depends(current_user)):
    return _require_owned(job_id, user).to_public()


@app.post("/api/jobs/{job_id}/pause")
def pause_job(job_id: str, user: str = Depends(current_user)):
    return _control(job_id, "pause", user)


@app.post("/api/jobs/{job_id}/resume")
def resume_job(job_id: str, user: str = Depends(current_user)):
    return _control(job_id, "resume", user)


@app.post("/api/jobs/{job_id}/cancel")
def cancel_job(job_id: str, user: str = Depends(current_user)):
    return _control(job_id, "cancel", user)


@app.post("/api/jobs/{job_id}/retry")
def retry_job(job_id: str, user: str = Depends(current_user)):
    """Re-run a failed/cancelled recognition job from scratch (same file+options)."""
    _require_owned(job_id, user)
    try:
        job = store.retry(job_id)
    except KeyError:
        raise HTTPException(404, "Задача не найдена")
    except ValueError as e:
        raise HTTPException(409, str(e))
    return job.to_public()


class ReanalyzeBody(BaseModel):
    provider: str = "auto"
    instructions: str | None = None
    custom_prompt: str | None = None
    deliver_protocol_cloud: bool | None = None
    deliver_weeek_task: str | None = None


@app.post("/api/jobs/{job_id}/reanalyze")
def reanalyze_job(job_id: str, body: ReanalyzeBody, user: str = Depends(current_user)):
    _require_owned(job_id, user)
    try:
        job = store.reanalyze(job_id, provider=body.provider,
                              instructions=body.instructions,
                              custom_prompt=body.custom_prompt,
                              deliver_protocol_cloud=body.deliver_protocol_cloud,
                              deliver_weeek_task=body.deliver_weeek_task)
    except KeyError:
        raise HTTPException(404, "Задача не найдена")
    except ValueError as e:
        raise HTTPException(409, str(e))
    return job.to_public()


@app.get("/api/prompt/default")
def prompt_default():
    """The built-in analysis prompt, for the expert-mode editor."""
    return {"prompt": analyze.EXPERT_PROMPT_DEFAULT}


def _control(job_id: str, action: str, user: str):
    _require_owned(job_id, user)
    fn = {"pause": store.pause, "resume": store.resume, "cancel": store.cancel}[action]
    try:
        job = fn(job_id)
    except KeyError:
        raise HTTPException(404, "Задача не найдена")
    except ValueError as e:
        raise HTTPException(409, str(e))
    return job.to_public()


@app.get("/api/jobs/{job_id}/partial")
def get_partial(job_id: str, user: str = Depends(current_user)):
    """Live transcript-so-far for the streaming UI."""
    job = _require_owned(job_id, user)
    return {
        "status": job.status,
        "progress": job.progress,
        "segments": store.partial(job_id),
        "analysis": store.analysis_progress(job_id),
    }


@app.get("/api/jobs/{job_id}/result")
def get_result(job_id: str, format: str = "txt", provider: str = "",
               user: str = Depends(current_user)):
    job = _require_owned(job_id, user)
    if format not in {"txt", "plain", "srt", "json", "docx", "screen"}:
        raise HTTPException(400, "format должен быть txt | plain | srt | json | docx | screen")

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
    if format == "plain" and not path.exists():
        path = store.result_path(job_id, "txt")  # fallback for jobs made before plain existed
    if not path.exists():
        if job.status not in {STATUS_DONE, STATUS_CANCELLED}:
            raise HTTPException(409, f"Задача ещё не готова (статус: {job.status})")
        raise HTTPException(404, "Результат отсутствует")
    if format in ("txt", "plain", "screen"):
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
def diarization_token(body: HfToken, user: str = Depends(require_admin)):
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


@app.get("/api/speakers/status")
def speakers_status():
    """What's needed to read WHO spoke from the video — for the UI toggle hints."""
    from .speaker_id import readiness
    return readiness()


@app.get("/api/protocol-delivery/status")
def protocol_delivery_status(user: str = Depends(current_user)):
    """Whether the manual page can push a protocol to cloud + Weeek. Reuses the
    settings configured on the «Автоматизация» page (cloud + Weeek token)."""
    from .automation import settings as auto_settings, clouds
    cfg = auto_settings.load(user)
    cl = clouds.readiness(cfg)
    selected = cl.get("selected") or "local"
    backend = (cl.get("backends") or {}).get(selected) or {}
    return {
        "cloud_ready": bool(backend.get("ready")),
        "cloud_name": backend.get("label") or selected,
        "weeek_ready": bool(cfg.get("weeek_token")),
        "protocol_folder": cfg.get("protocol_folder") or "",
        "protocol_field": cfg.get("weeek_protocol_field") or "Протокол встречи",
    }


@app.get("/api/ollama/status")
def ollama_status():
    """Whether the local engine (Ollama) is installed/ready + install progress."""
    from . import ollama_setup
    return ollama_setup.status()


@app.post("/api/ollama/install")
def ollama_install(user: str = Depends(require_admin)):
    """Download & install Ollama + the protocol model on demand (background)."""
    from . import ollama_setup
    return ollama_setup.install()


@app.post("/api/ollama/install/cancel")
def ollama_install_cancel(user: str = Depends(require_admin)):
    """Request cancellation of an in-progress Ollama install."""
    from . import ollama_setup
    return ollama_setup.cancel()


# ---- Optional dependencies (install into the app's venv from the UI) -------
@app.get("/api/setup/auto")
def autosetup_status():
    """Progress of the automatic first-run setup (OCR engine, ffmpeg, bot browser,
    speech models) + per-component readiness."""
    from . import autosetup
    return autosetup.status()


@app.get("/api/setup/deps")
def deps_status():
    """Readiness + install progress of optional deps (playwright/diariz/ffmpeg)."""
    from . import deps_setup
    return deps_setup.status()


@app.post("/api/setup/deps/{component}/install")
def deps_install(component: str, user: str = Depends(require_admin)):
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
def automation_settings(user: str = Depends(current_user)):
    """Current automation settings (secrets redacted to presence flags)."""
    from .automation import settings as auto_settings
    return auto_settings.redacted(user)


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
    weeek_set_video_field: bool | None = None
    weeek_video_field: str | None = None
    upload_protocol: bool | None = None
    protocol_folder: str | None = None
    weeek_set_protocol_field: bool | None = None
    weeek_protocol_field: str | None = None
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
def automation_save(body: AutomationSettings, user: str = Depends(current_user)):
    """Persist automation settings. Only non-null fields are updated."""
    from .automation import settings as auto_settings
    values = {k: v for k, v in body.model_dump().items() if v is not None}
    auto_settings.save(user, values)
    return auto_settings.redacted(user)


@app.get("/api/automation/meetings")
def automation_meetings(user: str = Depends(current_user)):
    """Upcoming meeting tasks from Weeek that carry a Telemost link."""
    from .automation import settings as auto_settings, weeek
    from datetime import timezone
    cfg = auto_settings.load(user)
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
    # Show upcoming meetings + only the single most recent past one (older past
    # meetings just clutter the list).
    from datetime import datetime
    now = datetime.now(timezone.utc)
    past = [m for m in meetings if m.start and m.start < now]
    keep = id(max(past, key=lambda m: m.start)) if past else None
    meetings = [m for m in meetings
                if not (m.start and m.start < now) or id(m) == keep]
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
def automation_meeting_decision(task_id: str, body: MeetingDecision,
                                user: str = Depends(current_user)):
    """Choose whether the bot records this specific meeting (overrides filters)."""
    from .automation.scheduler import scheduler
    return scheduler.set_decision(user, task_id, body.record)


@app.get("/api/automation/clouds/status")
def automation_clouds_status(user: str = Depends(current_user)):
    """Per-backend cloud readiness + which one is selected (for the UI)."""
    from .automation import settings as auto_settings, clouds
    return clouds.readiness(auto_settings.load(user))


@app.post("/api/automation/clouds/test")
def automation_clouds_test(backend: str | None = None,
                           user: str = Depends(current_user)):
    """Upload a tiny test file to the selected (or given) cloud to verify creds."""
    import tempfile
    from datetime import datetime, timezone
    from .automation import settings as auto_settings, clouds
    cfg = auto_settings.load(user)
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
def automation_scheduler_status(user: str = Depends(current_user)):
    """Scheduler state + the meetings it's tracking and their pipeline status."""
    from .automation.scheduler import scheduler
    scheduler.start()  # idempotent — ensures it's running even if startup was skipped
    return scheduler.status(user)


@app.post("/api/automation/scheduler/run-now")
def automation_scheduler_run_now(task_id: str, user: str = Depends(current_user)):
    """Manually record a known meeting right now (poll Weeek first to populate)."""
    from .automation.scheduler import scheduler
    res = scheduler.run_now(user, task_id)
    if not res.get("ok"):
        raise HTTPException(400, res.get("error"))
    return res


@app.post("/api/automation/scheduler/poll-now")
def automation_scheduler_poll_now(user: str = Depends(current_user)):
    """Force an immediate Weeek re-poll (the manual «Обновить статус» button)."""
    from .automation.scheduler import scheduler
    res = scheduler.poll_now(user)
    if not res.get("ok"):
        raise HTTPException(400, res.get("error"))
    return res


@app.post("/api/automation/scheduler/stop-recording")
def automation_scheduler_stop_recording(task_id: str | None = None,
                                        user: str = Depends(current_user)):
    """Stop recording(s): a specific meeting (task_id) or all of the user's."""
    from .automation.scheduler import scheduler
    res = scheduler.stop_recording(user, task_id)
    if not res.get("ok"):
        raise HTTPException(400, res.get("error"))
    return res


@app.get("/api/automation/recorder/status")
def automation_recorder_status(user: str = Depends(current_user)):
    """What the Telemost recorder needs (Playwright/ffmpeg/audio) — for the UI."""
    from .automation import settings as auto_settings, recorder
    return recorder.readiness(auto_settings.load(user))


@app.get("/api/automation/recorder/audio-devices")
def automation_recorder_audio_devices(user: str = Depends(current_user)):
    """List the PulseAudio sources ffmpeg can capture the meeting sound from."""
    from .automation import settings as auto_settings
    from .automation.recorder import capture
    cfg = auto_settings.load(user)
    return {"devices": capture.list_audio_devices(cfg.get("ffmpeg_path") or "ffmpeg")}


@app.get("/api/automation/recorder/login-status")
def automation_recorder_login_status(user: str = Depends(current_user)):
    """Whether the recorder profile is logged into Yandex (for the UI hint)."""
    from .automation import settings as auto_settings
    from .automation.recorder import browser
    return browser.login_status(auto_settings.load(user))


@app.get("/api/automation/recorder/audio-test")
def automation_recorder_audio_test(user: str = Depends(current_user)):
    """Record a few seconds from the chosen audio device and report its level —
    so the user can verify the meeting's sound actually reaches it."""
    from .automation import settings as auto_settings
    from .automation.recorder import capture
    cfg = auto_settings.load(user)
    return capture.test_audio_level(cfg.get("ffmpeg_path") or "ffmpeg",
                                    (cfg.get("audio_device") or "").strip())


@app.post("/api/automation/recorder/login")
def automation_recorder_login(user: str = Depends(current_user)):
    """Open a headed browser so the user logs into Yandex once (profile mode)."""
    import threading
    from .automation import settings as auto_settings
    from .automation.recorder import browser
    if not browser.playwright_available():
        raise HTTPException(400, "Playwright не установлен: pip install playwright "
                                 "&& playwright install chromium")
    cfg = auto_settings.load(user)
    threading.Thread(target=browser.login, args=(cfg,), daemon=True).start()
    return {"started": True,
            "detail": "Открывается окно браузера — войдите в Яндекс и закройте его."}


class RecorderTest(BaseModel):
    url: str
    seconds: int = 30


@app.post("/api/automation/recorder/test")
def automation_recorder_test(body: RecorderTest, user: str = Depends(current_user)):
    """Manually join a Telemost link and record for a few seconds, to verify
    the bot + capture work on this machine before automating."""
    import time
    from .automation import settings as auto_settings, recorder
    cfg = auto_settings.load(user)
    out = str(security.user_dir(user) / "recordings" /
              f"test-{time.strftime('%Y%m%d-%H%M%S')}.mp4")
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    deadline = time.time() + max(5, min(int(body.seconds), 300))
    logs: list[str] = []
    slot = recorder.acquire_slot()
    if slot is None:
        raise HTTPException(409, f"Все слоты записи заняты (до {recorder.MAX_SLOTS}).")
    try:
        res = recorder.record_meeting(
            body.url, out, cfg, slot=slot,
            on_log=lambda m: logs.append(str(m)),
            should_stop=lambda: time.time() > deadline)
    finally:
        recorder.release_slot(slot)
    res["logs"] = logs
    return res


@app.get("/api/automation/weeek/projects")
def automation_weeek_projects(user: str = Depends(current_user)):
    """List the workspace's projects (id + name) so the user can pick which one
    to record. The token sees all projects; `projectId` is what scopes it."""
    from .automation import settings as auto_settings, weeek
    token = auto_settings.get(user, "weeek_token")
    if not token:
        raise HTTPException(400, "Сначала задайте токен Weeek и нажмите «Сохранить».")
    try:
        return {"projects": weeek.list_projects(token)}
    except weeek.WeeekError as e:
        raise HTTPException(502, str(e))


@app.get("/api/automation/weeek/probe")
def automation_weeek_probe(task_id: str, user: str = Depends(current_user)):
    """Return the raw JSON of one Weeek task — used to pin date/link field names."""
    from .automation import settings as auto_settings, weeek
    token = auto_settings.get(user, "weeek_token")
    if not token:
        raise HTTPException(400, "Сначала задайте токен Weeek в настройках.")
    try:
        return weeek.probe_task(token, task_id)
    except weeek.WeeekError as e:
        raise HTTPException(502, str(e))


@app.get("/healthz")
def healthz():
    return {"ok": True, "model": config.MODEL, "diarization": config.DIARIZATION_ENABLED}
