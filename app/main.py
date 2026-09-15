"""FastAPI app: web upload UI + REST API for transcription jobs."""
from __future__ import annotations

import json
import os
import re
import time
import threading
from pathlib import Path

from fastapi import (Depends, FastAPI, File, Form, HTTPException, Request,
                     Response, UploadFile)
from fastapi.responses import (HTMLResponse, PlainTextResponse, FileResponse,
                               JSONResponse, RedirectResponse)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, ConfigDict, Field

from . import config, db, events, llm, analyze, logs, security, sms, user_creds
from .jobs import store, STATUS_DONE, STATUS_CANCELLED

log = logs.get("vtx.main")


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
                engines.append({"value": f"ollama:{m}", "label": _ollama_label(m),
                                "provider": "ollama", "group": "Локально · Ollama",
                                "model": m})
        else:
            # Ollama enabled but server down / no models yet — offer the default.
            engines.append({"value": "ollama",
                            "label": f"Локально · {config.OLLAMA_MODEL} (по умолчанию)",
                            "provider": "ollama", "group": "Локально · Ollama",
                            "model": config.OLLAMA_MODEL})
    for p in avail:
        if p == "ollama":
            continue
        if p == "custom":
            # Список пришёл от самого поставщика при подключении ключа и лежит
            # рядом с ним. Прибивать его в коде нельзя: у каждого поставщика он
            # свой и меняется — ради этого универсальный провайдер и заведён.
            from .llm_custom import is_denied
            # Берём подключения из ОБЩЕГО источника: там и ключи из интерфейса,
            # и заданное переменными окружения (Yandex Cloud). Читая только
            # user_keys, список молча терял env-подключение.
            creds = [{"key": k, "extra": ex}
                     for k, ex in config.provider_creds("custom", user_keys)]
            seen: set[str] = set()
            for cred in creds:
                try:
                    cfg = json.loads(cred.get("extra") or "{}")
                except ValueError:
                    continue
                for m in cfg.get("models") or []:
                    if m in seen:
                        continue
                    # Модель, которую отвергли ВСЕ ключи, показывать незачем:
                    # выбрать её — значит получить 404 в момент сборки
                    # протокола. Если хоть один ключ её тянет, оставляем:
                    # права выдаются на ключ, и ротация до него дойдёт.
                    if all(is_denied(c.get("key") or "", m) for c in creds):
                        continue
                    seen.add(m)
                    # Группа — имя поставщика из подключения: под «своим
                    # ключом» живут модели РАЗНЫХ сервисов (OpenRouter, Yandex
                    # Cloud, свой сервер), и в общем списке их не различить.
                    where = cfg.get("hint") or cfg.get("base_url") or "Свой ключ"
                    engines.append({"value": f"custom:{m}", "label": f"{where} · {m}",
                                    "provider": "custom", "group": where, "model": m})
            if not seen:
                engines.append({"value": "custom", "label": "Свой ключ · по умолчанию",
                                "provider": "custom", "group": "Свой ключ",
                                "model": "по умолчанию"})
            continue
        tiers = config.PROVIDER_MODELS.get(p)
        if tiers:
            # One entry per model tier so the user picks how powerful it is.
            # The tier label already names the brand (Llama / GigaChat / …).
            group = config.PROVIDER_LABELS.get(p, p)
            for t in tiers:
                engines.append({"value": f"{p}:{t['value']}", "label": t["label"],
                                "provider": p, "group": group, "model": t["label"]})
        else:
            group = config.PROVIDER_LABELS.get(p, p)
            engines.append({"value": p, "label": group, "provider": p,
                            "group": group, "model": "по умолчанию"})
    return engines

app = FastAPI(title="Voice Transcriber", version="1.0")

# Маршруты автоматизации (26 штук про Weeek/запись/облако) вынесены в свой
# модуль — пути не изменились, префикс задан у роутера.
from .api_automation import router as _automation_router   # noqa: E402
app.include_router(_automation_router)
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

# Built React SPA (Vite → frontend/dist) — this IS the whole app UI. Hashed
# assets are served from /assets; the SPA shell (index.html) is served for "/"
# and every other client route by _register_spa() at the end of this module.
# The dir is absent in dev until `npm run build`, so mounting is guarded.
SPA_DIR = Path(__file__).parent.parent / "frontend" / "dist"
if (SPA_DIR / "assets").is_dir():
    app.mount("/assets", StaticFiles(directory=str(SPA_DIR / "assets")), name="spa-assets")


def _register_spa() -> None:
    """Serve the React SPA as the entire app UI: index.html for `/` and every
    other non-API, non-file path so client-side routing (deep links, refresh)
    works. Registered LAST so real routes (auth pages, /api/*, /healthz) win."""
    index = SPA_DIR / "index.html"
    if not index.exists():
        return
    spa_root = SPA_DIR.resolve()

    @app.get("/", include_in_schema=False)
    @app.get("/{full_path:path}", include_in_schema=False)
    def spa(full_path: str = "", user: str = Depends(current_user)):
        # Unknown API paths must 404, not return the HTML shell.
        if full_path.startswith("api/"):
            raise HTTPException(404, "Not found")
        # Real files (favicon, etc.) win — but ONLY inside the built SPA dir.
        # Resolve and confirm containment so "../../data/secret.key" can't escape
        # (path-traversal guard: without it any logged-in user could read
        # arbitrary server files, e.g. the master key or users.json).
        if full_path:
            target = (spa_root / full_path).resolve()
            if target.is_file() and target.is_relative_to(spa_root):
                return FileResponse(target)
        return FileResponse(index)


# ===========================================================================
# Authentication gate
# ===========================================================================
# Paths reachable WITHOUT a session. Everything else requires login.
_PUBLIC_PATHS = {"/login", "/register", "/recover", "/healthz",
                 "/api/auth/login", "/api/auth/register",
                 "/api/auth/recover/request", "/api/auth/recover/verify"}
_SECURE_COOKIE = os.getenv("VTX_HTTPS", "0") == "1"


@app.middleware("http")
async def _auth_gate(request: Request, call_next):
    """Resolve the session into request.state.user; block everything else.

    HTML routes redirect to /login; API routes get a 401. This is the single
    choke point that makes the whole site private on a public domain."""
    user = security.session_user(request.cookies.get(security.SESSION_COOKIE))
    request.state.user = user
    path = request.url.path
    if user and _cross_site_write(request):
        # CSRF: до сих пор защита держалась только на SameSite=Lax у куки.
        # Sec-Fetch-Site ставит сам браузер, со страницы атакующего его не
        # подделать; запросы со своего сайта и навигация проходят.
        resp = JSONResponse({"detail": "Запрос с чужого сайта отклонён."},
                            status_code=403)
    elif user or path in _PUBLIC_PATHS:
        resp = await call_next(request)
    elif path.startswith("/api/"):
        resp = JSONResponse({"detail": "Требуется вход."}, status_code=401)
    else:
        resp = RedirectResponse("/login", status_code=303)
    # Baseline security headers on every response.
    resp.headers.setdefault("X-Frame-Options", "DENY")           # clickjacking
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("Referrer-Policy", "same-origin")
    # CSP: SPA собирается локально, внешних CDN нет — достаточно 'self'.
    # Инлайн-стили нужны React (style={{…}}) и Tailwind; инлайн-скрипт —
    # только один, в index.html (тема до загрузки), поэтому 'unsafe-inline'
    # у script-src нет: тема выставляется тем же скриптом через хэш ниже.
    resp.headers.setdefault("Content-Security-Policy", _CSP)
    if _SECURE_COOKIE:  # only meaningful once TLS is in front
        resp.headers.setdefault("Strict-Transport-Security",
                                "max-age=31536000; includeSubDomains")
    return resp


# Инлайн-скрипты разрешаются ХЭШАМИ, а не 'unsafe-inline'. Хэши считаются
# по реальному HTML, а не пишутся руками: 02.09 на бою сломались регистрация
# и вход — в CSP стоял один хэш (тема из index.html), а на странице входа два
# своих инлайн-скрипта (обработчик формы с «{{ mode }}» внутри и своя тема).
# Браузер молча блокировал оба: кнопка не делала ничего, ошибок не показывалось.
_SCRIPT_RE = re.compile(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", re.S | re.I)
_CSP_TEMPLATE = ("default-src 'self'; img-src 'self' data: blob:; media-src 'self' blob:; "
                 "style-src 'self' 'unsafe-inline'; font-src 'self' data:; "
                 "script-src 'self'{scripts}; connect-src 'self'; "
                 "frame-ancestors 'none'; object-src 'none'; base-uri 'self'; "
                 "form-action 'self'")


def inline_script_hashes(html: str) -> list[str]:
    """sha256 (base64) тела каждого инлайн-<script> в HTML — для CSP."""
    import base64
    import hashlib
    return [base64.b64encode(hashlib.sha256(m.group(1).encode("utf-8")).digest()).decode()
            for m in _SCRIPT_RE.finditer(html or "")]


def csp_for(hashes) -> str:
    src = " ".join(f"'sha256-{h}'" for h in dict.fromkeys(hashes))
    return _CSP_TEMPLATE.format(scripts=(" " + src) if src else "")


def _index_hashes() -> list[str]:
    try:
        return inline_script_hashes((SPA_DIR / "index.html").read_text(encoding="utf-8"))
    except OSError:
        return [os.getenv("VTX_CSP_THEME_HASH", "RvyXR+TwrsIaxYQ4S5hUjStMbeLO2T0Xv8kK1EgOAjc=")]


_INDEX_HASHES = _index_hashes()
_CSP = csp_for(_INDEX_HASHES)


def _login_page(request: Request, **ctx) -> HTMLResponse:
    """Страница входа/регистрации/восстановления с CSP под ЕЁ инлайн-скрипты."""
    html = templates.get_template("login.html").render({"request": request, **ctx})
    resp = HTMLResponse(html)
    resp.headers["Content-Security-Policy"] = csp_for(
        inline_script_hashes(html) + _INDEX_HASHES)
    return resp


def _cross_site_write(request: Request) -> bool:
    """Меняющий состояние запрос к API, пришедший с ЧУЖОГО сайта."""
    if request.method not in ("POST", "PUT", "PATCH", "DELETE"):
        return False
    if not request.url.path.startswith("/api/"):
        return False
    site = (request.headers.get("sec-fetch-site") or "").lower()
    if site and site not in ("same-origin", "same-site", "none"):
        return True
    origin = request.headers.get("origin")
    if origin and not site:
        # Старый браузер без Sec-Fetch-Site: сверяем Origin с Host.
        host = request.headers.get("host", "")
        try:
            from urllib.parse import urlsplit
            return urlsplit(origin).netloc.lower() != host.lower()
        except ValueError:
            return True
    return False


def _client_ip(request: Request) -> str:
    """Client IP for rate limiting; honours the reverse proxy's X-Forwarded-For
    only when explicitly trusted (VTX_TRUST_PROXY=1, set alongside the proxy)."""
    if os.getenv("VTX_TRUST_PROXY", "0") == "1":
        xff = request.headers.get("x-forwarded-for", "")
        if xff:
            # ПОСЛЕДНИЙ элемент — тот, который дописал ближайший прокси, то есть
            # единственный, которому можно верить. Первый элемент подставляет
            # сам клиент: за nginx (он ДОПИСЫВАЕТ, а не перезаписывает заголовок)
            # это позволяло назваться чужим адресом и обойти защиту от перебора
            # паролей. Caddy заголовок перезаписывает, но опираться на выбор
            # прокси в коде нельзя.
            return xff.split(",")[-1].strip()
    return request.client.host if request.client else "?"


# Зависимости require_admin здесь больше нет: единственными маршрутами,
# менявшими ГЛОБАЛЬНОЕ состояние сервера, были установка компонентов и токен
# HuggingFace — они убраны (в контейнере установка невозможна, а токен задаётся
# переменной окружения). Всё остальное в приложении принадлежит команде, а не
# серверу. Понятие «основатель» осталось: security.is_super_admin. Если снова
# появится общесерверная операция — вернуть такую зависимость поверх него.


# current_user и NotesBody живут в deps.py: их спрашивают и вынесенные роутеры,
# а импортировать main.py из роутера — замкнуть круг.
from .deps import NotesBody, current_user   # noqa: E402,F401


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
    return _login_page(request, mode="login", first_run=not security.list_users())


@app.get("/register", response_class=HTMLResponse)
def register_page(request: Request):
    return _login_page(request, mode="register", first_run=not security.list_users())


@app.post("/api/auth/register")
def auth_register(body: Credentials, request: Request):
    """Регистрация: создаёт учётку и сразу открывает сессию."""
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
    """Вход по логину и паролю; ставит cookie сессии."""
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
    """Выход: гасит сессию и снимает cookie."""
    security.destroy_session(request.cookies.get(security.SESSION_COOKIE))
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(security.SESSION_COOKIE, path="/")
    return resp


# ---- AI context (standing knowledge base for the protocol AI) --------------
@app.get("/api/context")
def get_context(user: str = Depends(current_user)):
    """Постоянный контекст команды для ИИ: общий текст и проекты."""
    from . import ai_context
    return ai_context.load(user)


class ContextBody(BaseModel):
    # JSON key "global" is a Python keyword → accept it via an alias.
    global_: str = Field("", alias="global")
    projects: list[dict] = []
    model_config = {"populate_by_name": True}


@app.post("/api/context")
def save_context(body: ContextBody, user: str = Depends(current_user)):
    """Сохранить постоянный контекст команды."""
    from . import ai_context
    return ai_context.save(user, {"global": body.global_, "projects": body.projects})


# ---- Задачи из протокола → Weeek (черновики с подтверждением) --------------
def _job_weeek_view(job) -> dict:
    from . import weeek_tasks
    from .automation import settings as auto_settings
    cfg = auto_settings.load(job.owner)
    items = list(job.weeek_tasks or [])
    return {
        "enabled": bool(cfg.get("weeek_tasks_enabled", True)),
        "connected": bool(cfg.get("weeek_token")),
        "items": [{**d, "selected": weeek_tasks.default_selected(d, cfg)} for d in items],
        "defaults": {"project_id": cfg.get("weeek_tasks_project_id"),
                     "board_id": cfg.get("weeek_tasks_board_id"),
                     "column_id": cfg.get("weeek_tasks_column_id")},
        "members": (cfg.get("weeek_members_cache") or {}).get("members") or [],
    }


@app.get("/api/jobs/{job_id}/weeek-tasks")
def job_weeek_tasks(job_id: str, user: str = Depends(current_user)):
    """Черновики задач протокола для Weeek + участники воркспейса."""
    job = _require_owned(job_id, user)
    if not job.weeek_tasks and job.analysis:
        store.prepare_weeek_tasks(job)
        store._save(job)
    return _job_weeek_view(job)


@app.post("/api/jobs/{job_id}/weeek-tasks/prepare")
def job_weeek_tasks_prepare(job_id: str, user: str = Depends(current_user)):
    """Пересобрать черновики из текущего протокола (созданные не теряются)."""
    job = _require_owned(job_id, user)
    from .automation import settings as auto_settings
    members = (auto_settings.load(user).get("weeek_members_cache") or {}).get("members") or []
    store.prepare_weeek_tasks(job, members=members)
    store._save(job)
    return _job_weeek_view(job)


class WeeekTasksCreate(BaseModel):
    items: list[dict] = []


@app.post("/api/jobs/{job_id}/weeek-tasks/create")
def job_weeek_tasks_create(job_id: str, body: WeeekTasksCreate,
                           user: str = Depends(current_user)):
    """Создать отмеченные черновики в Weeek. Ответ — по каждой строке."""
    from . import meeting_series, weeek_tasks
    from .automation import settings as auto_settings
    job = _require_owned(job_id, user)
    cfg = auto_settings.load(user)
    token = cfg.get("weeek_token")
    if not token:
        raise HTTPException(400, "Сначала подключите Weeek в «Автоматизации».")
    if not body.items:
        raise HTTPException(400, "Не выбрано ни одной задачи.")
    if not job.weeek_tasks:
        store.prepare_weeek_tasks(job)
    title = meeting_series.display_title(job.context_hint or job.filename)
    date = meeting_series.date_from_title(job.context_hint or job.filename) or ""
    results = weeek_tasks.create_selected(
        job.weeek_tasks, body.items[:100], token, title, date, user,
        protocol_url=job.protocol_cloud_url or "")
    store._save(job)
    created = sum(1 for r in results if (r or {}).get("ok"))
    if created:
        events.record(events.TASK_CREATED, user=user, job_id=job_id,
                      once_a_day=False, extra={"count": created})
    return {"results": results, **_job_weeek_view(job)}


@app.post("/api/jobs/{job_id}/weeek-tasks/{key}/skip")
def job_weeek_tasks_skip(job_id: str, key: str, user: str = Depends(current_user)):
    """Пометить черновик «не заводить» (задача в Weeek не создаётся)."""
    job = _require_owned(job_id, user)
    for d in job.weeek_tasks or []:
        if d.get("key") == key and d.get("status") != "created":
            d["status"] = "skipped"
            store._save(job)
            return _job_weeek_view(job)
    raise HTTPException(404, "Черновик не найден или уже создан.")


# ---- Серии встреч: карточка повторяющейся встречи + память о прошлой -------
@app.get("/api/series")
def list_series(user: str = Depends(current_user)):
    """Все серии команды: карточка, закреплённый тип, приоритет, итоги прошлой."""
    from . import meeting_series
    return {"series": meeting_series.list_for_ui(user),
            "priorities": list(meeting_series.PRIORITIES)}


class SeriesBody(BaseModel):
    key: str = ""               # ключ серии или название встречи (нормализуется)
    title: str | None = None
    context: str | None = None
    preset: str | None = None
    priority: str | None = None
    weeek_project_id: str | int | None = None


@app.post("/api/series")
def save_series(body: SeriesBody, user: str = Depends(current_user)):
    """Создать/обновить карточку серии встреч."""
    from . import meeting_series
    fields = {k: v for k, v in body.model_dump().items() if k != "key" and v is not None}
    try:
        return meeting_series.upsert(user, body.key or body.title or "", fields)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.delete("/api/series/{key}")
def delete_series(key: str, user: str = Depends(current_user)):
    from . import meeting_series
    if not meeting_series.delete(user, key):
        raise HTTPException(404, "Серия не найдена")
    return {"ok": True}


@app.post("/api/series/{key}/forget-last")
def series_forget_last(key: str, user: str = Depends(current_user)):
    """Стереть память о прошлой встрече серии (карточка остаётся)."""
    from . import meeting_series
    if not meeting_series.forget_last(user, key):
        raise HTTPException(404, "Серия не найдена")
    return {"ok": True}


# ---- Password recovery by phone (public, heavily throttled) ---------------
@app.get("/recover", response_class=HTMLResponse)
def recover_page(request: Request):
    return _login_page(request, mode="recover", first_run=False)


class RecoverRequestBody(BaseModel):
    username: str
    phone: str


class RecoverVerifyBody(BaseModel):
    username: str
    phone: str
    code: str
    new_password: str


# Neutral answer to step 1: whatever happens, the client is told the same thing,
# so it never reveals whether a given login/phone pair exists.
_RECOVER_SENT = {"ok": True,
                 "detail": "Если логин и телефон совпадают, код отправлен по SMS."}


@app.post("/api/auth/recover/request")
def auth_recover_request(body: RecoverRequestBody, request: Request):
    """Step 1 — send a one-time code to the registered phone (if login+phone
    match). Response is deliberately identical on success and failure. Throttled
    per IP and per IP+login; a per-code resend cooldown prevents SMS spam."""
    ip = _client_ip(request)
    uname = security.normalize_username(body.username)
    keys = (f"rec:{ip}", f"rec:{ip}:{uname}")
    wait = security.throttle_check(*keys)
    if wait:
        raise HTTPException(429, f"Слишком много попыток. Подождите {wait} с.")
    res = security.generate_recovery_code(body.username, body.phone)
    if res.get("ok"):
        code = res["code"]
        msg = (f"Код восстановления пароля MeetFlowAI: {code}. "
               f"Действует {security.RECOVERY_CODE_TTL // 60} мин. "
               f"Никому его не сообщайте.")
        sms.send("+" + res["phone"], msg)
    elif res.get("error") == "cooldown":
        # Живой код уже отправлен — второй раз не шлём. Ответ при этом обязан
        # остаться ТЕМ ЖЕ: в cooldown попадают только после верной пары
        # логин+телефон, поэтому отдельное поле retry_after сообщало бы
        # проверяющему, что пара угадана. Достаточно двух запросов подряд, чтобы
        # подтвердить связку «логин ↔ личный телефон» — половина того, что нужно
        # для социальной инженерии со сбросом пароля. Заявленная в коде цель
        # «no account enumeration» этого не допускает.
        pass
    else:
        security.throttle_fail(*keys)   # wrong login/phone counts as an attempt
    return _RECOVER_SENT


@app.post("/api/auth/recover/verify")
def auth_recover_verify(body: RecoverVerifyBody, request: Request):
    """Step 2 — check the code and set the new password; revokes all sessions."""
    ip = _client_ip(request)
    uname = security.normalize_username(body.username)
    keys = (f"rec:{ip}", f"rec:{ip}:{uname}")
    wait = security.throttle_check(*keys)
    if wait:
        raise HTTPException(429, f"Слишком много попыток. Подождите {wait} с.")
    res = security.confirm_recovery_code(body.username, body.phone,
                                         body.code, body.new_password)
    if not res.get("ok"):
        security.throttle_fail(*keys)
        raise HTTPException(400, res.get("error") or "Не удалось восстановить пароль.")
    security.throttle_clear(*keys)
    return {"ok": True, "detail": "Пароль изменён — войдите с новым паролем."}


# ---- Profile (phone + password management) ---------------------------------
@app.get("/api/profile")
def profile_info(user: str = Depends(current_user)):
    """Профиль: логин, маска телефона, роль, команда, код приглашения."""
    is_admin = security.is_admin(user)
    team = security.team_of(user)
    return {"username": user, "phone_masked": security.masked_phone(user),
            "is_admin": is_admin,
            "team": team,
            # The invite code lets others join THIS team; only the admin has one.
            # It rotates daily — expires_at drives the countdown in the UI.
            "invite_code": security.invite_code_of(user) if is_admin else None,
            "invite_expires_at": security.invite_code_expires_at(user) if is_admin else None,
            "team_size": len(security.team_members(team)),
            # Who is in this workspace (admin only — for managing the team).
            "team_members": security.team_member_list(team) if is_admin else []}


class TeamKick(BaseModel):
    username: str


@app.post("/api/profile/team/remove")
def profile_team_remove(body: TeamKick, user: str = Depends(current_user)):
    """Remove a member from the admin's workspace (they get their own empty one
    and are logged out immediately)."""
    res = security.remove_from_team(user, body.username)
    if not res.get("ok"):
        raise HTTPException(400, res.get("error"))
    team = security.team_of(user)
    return {"ok": True, "team_members": security.team_member_list(team),
            "team_size": len(security.team_members(team))}


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


class AccountDelete(BaseModel):
    password: str


@app.post("/api/profile/delete")
def profile_delete(body: AccountDelete, request: Request,
                   user: str = Depends(current_user)):
    """Permanently delete the current account (login, phone, all data). Requires
    the current password. Clears the session — the client redirects to /login."""
    res = security.delete_account(user, body.password)
    if not res.get("ok"):
        raise HTTPException(400, res.get("error"))
    security.destroy_session(request.cookies.get(security.SESSION_COOKIE))
    resp = JSONResponse({"ok": True, "detail": "Аккаунт удалён."})
    resp.delete_cookie(security.SESSION_COOKIE)
    return resp


@app.on_event("startup")
def _start_scheduler() -> None:
    """Start the meeting-automation scheduler. It self-gates on the `enabled`
    setting, so it's safe to always run — it idles until turned on in the UI."""
    if db.enabled():
        db.init_schema()  # ensure tables exist (idempotent)
    try:
        from .automation.scheduler import scheduler
        scheduler.start()
    except Exception:
        log.error("Планировщик встреч не запустился — автоматической записи не "
                  "будет", exc_info=True)
    # Д14: index pre-existing finished jobs for full-text search (idempotent).
    import threading as _th
    _th.Thread(target=store.backfill_search, daemon=True,
               name="vtx-search-backfill").start()
    # Подготовка при старте: модель распознавания скачивается заранее, в фоне —
    # иначе первая же встреча ждёт полтора гигабайта загрузки. Объём задаёт
    # VTX_PRELOAD_MODELS, выключается целиком через VTX_AUTO_SETUP=0.
    # (Ветка «иначе» здесь раньше качала модель СИНХРОННО, блокируя старт.)
    if os.getenv("VTX_AUTO_SETUP", "1") == "1":
        try:
            from . import autosetup
            autosetup.ensure_all()
        except Exception:
            log.warning("Предзагрузка модели распознавания не началась",
                        exc_info=True)

ALLOWED_EXT = {".mp3", ".wav", ".m4a", ".ogg", ".oga", ".opus", ".flac", ".aac",
               ".mp4", ".mkv", ".webm", ".mov", ".wma", ".amr"}

# Whisper models selectable per job (accuracy vs speed).
ALLOWED_MODELS = ["tiny", "base", "small", "medium", "large-v3", "large-v3-turbo"]


# The whole UI is the React SPA (served by _register_spa at the end of this
# module for "/" and every client route); there are no Jinja app pages anymore.


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
    context_hint: str = Form(""),
    user_notes: str = Form(""),
    preset: str = Form(""),
    user: str = Depends(current_user),
):
    """Создать задачу распознавания: файл плюс опции обработки."""
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
                       deliver_weeek_task=deliver_weeek_task,
                       context_hint=context_hint, model=model_sel,
                       user_notes=user_notes, preset=preset, owner=user)
    return JSONResponse({"job_id": job.id, **job.to_public()}, status_code=201)


@app.get("/api/jobs")
def list_jobs(user: str = Depends(current_user)):
    """Список задач команды."""
    return [j.to_public() for j in store.list(owner=user)]


@app.get("/api/stats")
def team_stats(days: int = 30, user: str = Depends(current_user)):
    """Business metrics for the Overview page, aggregated over the team."""
    from . import stats
    return stats.summary(user, days=max(1, min(int(days), 365)))


@app.get("/api/stats/report")
def team_report(days: int = 30, user: str = Depends(current_user)):
    """Отчёт клиенту на одну страницу (docs/ТЗ-МЕТРИКИ.md И58): встречи, решения,
    задачи, сэкономленное время с формулой, надёжность словами и «укол в
    процесс». Без себестоимости и движков — их клиенту не показываем (И59)."""
    from . import stats
    return stats.client_report(user, days=max(1, min(int(days), 365)))


@app.get("/api/stats/owner")
def owner_report_api(days: int = 7, user: str = Depends(current_user)):
    """Недельная таблица владельца сервиса по ВСЕМ командам (И56) и список
    спящих платящих (И48). Только основателю сервера — здесь видны чужие команды."""
    if not security.is_super_admin(user):
        raise HTTPException(403, "Только основателю сервера.")
    from . import stats
    return stats.owner_report(days=max(1, min(int(days), 365)))


# ---- LLM providers (protocol engine) --------------------------------------
class ProviderKey(BaseModel):
    provider: str
    api_key: str
    extra: str = ""   # YandexGPT: folder id · GigaChat: scope · custom: адрес API
    # Только для «своего ключа»: имя модели, когда шлюз не отдаёт список
    # (Yandex Cloud AI Studio — как раз такой).
    model: str = ""


@app.get("/api/providers")
def list_providers(user: str = Depends(current_user)):
    """All known providers + which are usable for THIS user (their own keys)."""
    uk = user_creds.load(user)
    avail = set(config.available_providers(uk))
    counts = user_creds.counts(user)  # how many personal keys per provider
    return {
        "available": config.available_providers(uk),
        # Отключённые через VTX_PROVIDER_DISABLED. Раньше такой провайдер
        # исчезал отовсюду МОЛЧА — ни карточки, ни движка, ни объяснения; на
        # бою так «пропал Gemini», и искали его в коде, а не в .env.
        "disabled": sorted(config.PROVIDER_DISABLED),
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
    # Свой сервер (vLLM, llama.cpp, Ollama, LM Studio) обычно ключа не
    # спрашивает — там достаточно адреса.
    if not key and not (provider == "custom" and extra):
        raise HTTPException(400, "Введите ключ")
    if provider == "yandex" and not extra:
        raise HTTPException(400, "Для YandexGPT укажите folder id (идентификатор каталога)")

    if provider == "custom":
        # Один ключ — и всё остальное выясняется само: адрес API, способ
        # авторизации и список моделей. Так не нужен отдельный класс под
        # каждого поставщика: состав моделей у них меняется — бывало, что
        # бесплатный набор менялся дважды за неделю.
        #
        # В `extra` пользователь может передать адрес, если поставщик незнакомый
        # (Groq, OpenAI, Gemini и другие угадываются по виду ключа).
        from .llm_custom import detect, ping_model
        found = detect(key, extra, body.model)
        if not found.get("ok"):
            raise HTTPException(400, found.get("error") or "Ключ не подошёл.")
        models = found["models"]
        # Модель есть в списке — но выдана ли она ключу? Именно этот разрыв
        # стоил недели разбирательств: поставщик показывал модель в каталоге и
        # отвечал 404 при вызове. Один токен стоит почти ничего, а знать это
        # лучше сейчас, чем в момент сборки протокола.
        default = models[0]
        if found.get("manual"):
            # Модель уже проверена вызовом внутри detect — второй раз незачем.
            ok_ping, why = True, ""
        else:
            ok_ping, why = ping_model(found["base_url"], key, found["auth"], default)
        where = found["hint"] or found["base_url"]
        skipped = found.get("skipped") or 0
        tail = f" Не-чат моделей пропущено: {skipped}." if skipped else ""
        # На один ключ можно повесить несколько моделей: о тех, что не
        # отозвались, говорим прямо — иначе человек будет искать их в списке.
        if found.get("failed"):
            tail += (" Не ответили: " + ", ".join(found["failed"][:5]) + ".")
        if ok_ping:
            note = (f"Ключ принят: {where}, доступно моделей — {len(models)}. "
                    f"Модель по умолчанию — «{default}»; сменить можно в "
                    f"«Движке протокола».{tail}")
        else:
            # Подключение всё равно создаём: ключ живой, просто эта модель ему
            # не выдана — остальные могут работать.
            default = ""
            note = (f"Ключ принят: {where}, моделей в списке — {len(models)}. "
                    f"Но «{models[0]}» этому ключу недоступна ({why}). "
                    f"Выберите другую в «Движке протокола».{tail}")
        extra = json.dumps({"base_url": found["base_url"], "auth": found["auth"],
                            "model": default, "models": models[:200],
                            "hint": found.get("hint") or "",
                            "detected_at": time.time()},
                           ensure_ascii=False)
        user_creds.add(user, provider, key, extra)
        return {"ok": True, "connected": provider, "note": note,
                "keys": user_creds.counts(user).get(provider, 1),
                "models": models[:200],
                "providers": _provider_list(user_creds.load(user))}

    # Verify the key with a cheap non-JSON ping BEFORE saving, so a bad key
    # fails fast and nothing is persisted.
    trial = {provider: [{"key": key, "extra": extra}]}
    note = None
    try:
        # ⚠️ Не 5 токенов: у рассуждающих моделей (gpt-oss на Groq, DeepSeek R1)
        # лимит считает и рассуждения, и в пять токенов ответ не влезает вовсе
        # — ключ отвергался с «модель вернула пустой ответ». 64 токена стоят
        # доли цента и дают место подумать и ответить.
        llm.get_provider(provider, trial).complete("Ответь одним словом: ok",
                                                   max_tokens=64, force_json=False)
    except Exception as e:
        es = str(e).lower()
        # A 429 / quota error means the key AUTHENTICATED but is rate-limited —
        # it's a valid key that will work once the limit resets (and it rotates
        # with your other keys), so save it with a note instead of rejecting it.
        if llm._is_rate_limit(e):
            note = ("Ключ принят, но сейчас упёрся в лимит (429). Он рабочий — "
                    "заработает после сброса квоты; при нескольких ключах они "
                    "чередуются автоматически.")
        # A 404 «model not found / no longer available» is about the MODEL, not
        # the key (a bad key would be 400/403 API_KEY_INVALID). The key
        # authenticated — save it and tell the user to pick a working model.
        elif "404" in es or "not found" in es or "no longer available" in es \
                or "not available" in es or "not_found" in es:
            # Текст был написан под Gemini и дословно предлагал «выберите
            # другую модель Gemini» — какой бы провайдер ни подключали.
            note = ("Ключ принят, но модель по умолчанию недоступна для "
                    "этого аккаунта. Откройте «Движок протокола» и выберите "
                    "другую модель — в списке показаны опубликованные "
                    f"модели, но доступны не все. Ответ сервиса: {e}")
        else:
            raise HTTPException(400, f"Не удалось подключиться: {e}")
    # Append to the provider's key POOL (several keys rotate on rate limits).
    user_creds.add(user, provider, key, extra)
    return {"ok": True, "connected": provider, "note": note,
            "keys": user_creds.counts(user).get(provider, 1),
            "providers": _provider_list(user_creds.load(user))}


class ProviderRef(BaseModel):
    """Только имя провайдера. Отдельно от ProviderKey, где api_key обязателен:
    «Отключить» шлёт один provider и всегда получало 422, а в тосте — «[object
    Object]» (детали ошибки FastAPI приходят списком)."""
    provider: str


@app.post("/api/providers/custom/refresh")
def refresh_custom_models(user: str = Depends(current_user)):
    """Перечитать список моделей у поставщика сохранённым ключом.

    Состав моделей меняется на их стороне: поставщик может за неделю убрать
    десяток моделей. Без обновления в выборе движка остаются имена, которые
    уже отвечают 404 — и узнаётся это в момент сборки протокола, когда встреча
    уже записана.
    """
    from .llm_custom import detect
    entries = user_creds.load(user).get("custom") or []
    if not entries:
        raise HTTPException(400, "Провайдер «свой ключ» не подключён.")
    added: list[str] = []
    removed: list[str] = []
    updated = 0
    for idx, e in enumerate(entries):
        try:
            cfg = json.loads(e.get("extra") or "{}")
        except ValueError:
            cfg = {}
        was = list(cfg.get("models") or [])
        found = detect(e.get("key") or "", cfg.get("base_url") or "")
        if not found.get("ok"):
            continue
        now = found["models"]
        added += [m for m in now if m not in was]
        removed += [m for m in was if m not in now]
        cfg.update(base_url=found["base_url"], auth=found["auth"],
                   models=now[:200], detected_at=time.time())
        # Выбранная модель могла исчезнуть у поставщика. Молча подменять её
        # нельзя — человек выбирал осознанно; сбрасываем и говорим об этом.
        if cfg.get("model") and cfg["model"] not in now:
            cfg["model"] = ""
        user_creds.set_extra(user, "custom", idx, json.dumps(cfg, ensure_ascii=False))
        updated += 1
    if not updated:
        raise HTTPException(502, "Ни один ключ не ответил — список не обновлён.")
    gone = sorted(set(removed))
    note = f"Обновлено ключей: {updated}. Добавлено: {len(set(added))}, удалено: {len(gone)}."
    if gone:
        note += " Пропали у поставщика: " + ", ".join(gone[:8])
    return {"ok": True, "added": sorted(set(added)), "removed": gone, "note": note,
            "engines": _engine_list(user_creds.load(user))}


@app.post("/api/providers/disconnect")
def disconnect_provider(body: ProviderRef, user: str = Depends(current_user)):
    """Remove ALL of this user's saved keys for a provider."""
    user_creds.clear(user, body.provider.strip().lower())
    return {"ok": True, "providers": _provider_list(user_creds.load(user))}


@app.get("/api/providers/keys")
def provider_keys(provider: str, user: str = Depends(current_user)):
    """This user's saved keys for a provider, MASKED — to browse/remove them."""
    prov = provider.strip().lower()
    entries = user_creds.load(user).get(prov) or []

    def mask(k: str) -> str:
        # Приставка ключа говорит, чей он (nvapi-, gsk_, sk-or-), поэтому
        # показываем шесть первых символов — по ним ключ узнаётся среди
        # нескольких, а восстановить его нельзя.
        k = k or ""
        return "•" * len(k) if len(k) <= 12 else f"{k[:6]}…{k[-4:]}"

    def side(e: dict) -> str:
        """Что показать рядом с ключом. У «своего ключа» в extra лежит JSON со
        списком моделей — выводить его целиком незачем, полезен адрес."""
        raw = e.get("extra") or ""
        if prov != "custom":
            return raw
        try:
            cfg = json.loads(raw)
        except ValueError:
            return ""
        where = cfg.get("hint") or cfg.get("base_url") or ""
        n = len(cfg.get("models") or [])
        return f"{where} · моделей: {n}" if n else str(where)

    # Срок жизни ключа — для поставщиков, у которых он объявлен: когда ключ
    # истекает, протоколы начинают молча собираться запасным движком, и без
    # подсказки причину ищут долго. Если срока нет, отдаём только дату
    # добавления.
    ttl = config.KEY_TTL_DAYS.get(prov)
    now = time.time()

    def life(at: float) -> dict:
        if not at:
            return {}                       # ключ добавлен до появления даты
        out = {"added_at": at}
        if ttl:
            out["expires_at"] = at + ttl * 86400
            out["days_left"] = int((at + ttl * 86400 - now) // 86400)
        return out

    return {"provider": prov, "keys": [
        {"index": i, "masked": mask(e.get("key", "")),
         "extra": side(e), **life(float(e.get("at") or 0))}
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
    """Одна задача: статус, прогресс, результаты."""
    job = _require_owned(job_id, user)
    # `stage` — чем задача занята ПОСЛЕ расшифровки: полоса прогресса к тому
    # моменту уже на 100%, а работы ещё на десятки минут.
    return {**job.to_public(), "stage": store.stage(job_id)}


@app.post("/api/jobs/{job_id}/opened")
def job_opened(job_id: str, user: str = Depends(current_user)):
    """Человек открыл протокол встречи (docs/ТЗ-МЕТРИКИ.md И7, И9—И11).

    ⚠️ Ставится ТОЛЬКО по осознанному действию человека — клику по встрече или
    по вкладке «Протокол». Звать это из опроса (`GET /api/jobs`, `/partial`,
    статус автоматики) НЕЛЬЗЯ: «доля прочитанных протоколов» — главная метрика
    ценности, и от опроса она превратится в метрику трафика.

    Протокола нет — события нет: открыли карточку идущей записи, а не документ.
    Дедупликация «встреча + человек + сутки» живёт в `events.record`.
    """
    job = _require_owned(job_id, user)
    if not job.analysis:
        return {"ok": True, "recorded": False}
    return {"ok": True,
            "recorded": events.record(events.OPENED, user=user, job_id=job_id)}


@app.post("/api/jobs/{job_id}/pause")
def pause_job(job_id: str, user: str = Depends(current_user)):
    """Пауза: воркер замирает между сегментами, сделанное сохраняется."""
    return _control(job_id, "pause", user)


@app.post("/api/jobs/{job_id}/resume")
def resume_job(job_id: str, user: str = Depends(current_user)):
    """Продолжить приостановленную задачу."""
    return _control(job_id, "resume", user)


@app.post("/api/jobs/{job_id}/cancel")
def cancel_job(job_id: str, user: str = Depends(current_user)):
    """Остановить задачу; уже распознанная часть сохраняется."""
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
    """Пересобрать протокол по той же расшифровке — без повторного распознавания."""
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
    # ⚠️ Пересборка УСПЕШНОГО протокола — это качество: человек прочитал и
    # остался недоволен. Пересборка после ошибки — надёжность. Разделяем по
    # флагу (И21), а не сваливаем в одну кучу.
    events.record(events.REANALYZED, user=user, job_id=job_id, once_a_day=False,
                  extra={"after_error": bool(job.analysis_error) or not job.analysis})
    return job.to_public()


@app.get("/api/search")
def search_meetings(q: str = "", user: str = Depends(current_user)):
    """Д14: full-text search over the team's transcripts and protocols."""
    q = (q or "").strip()
    if len(q) < 2:
        return {"results": []}
    team = security.team_of(user)
    if db.enabled():
        rows = db.search_query(team, q)
        return {"results": [
            {"job_id": r["job_id"], "title": r["title"],
             "created_at": r["created_at"],
             "snippet": (r.get("snippet") or "").replace("<<", "⟦").replace(">>", "⟧")}
            for r in rows]}
    # File backend: linear scan of the team's transcripts (fine for small sets).
    out = []
    low = q.lower()
    for job in store.list(owner=user):
        p = store.result_path(job.id, "txt")
        if not p.exists():
            continue
        text = p.read_text(encoding="utf-8")
        i = text.lower().find(low)
        if i < 0:
            continue
        out.append({"job_id": job.id, "title": Path(job.filename).stem,
                    "created_at": job.created_at,
                    "snippet": "…" + text[max(0, i - 60):i + 90].replace("\n", " ") + "…"})
        if len(out) >= 20:
            break
    return {"results": out}


@app.post("/api/automation/notify/test")
def notify_test(user: str = Depends(current_user)):
    """Send a test Telegram message with the team's saved token/chat (Д15)."""
    from .automation import notify, settings as auto_settings
    res = notify.test(auto_settings.load(security.team_of(user)))
    if not res.get("ok"):
        raise HTTPException(400, res.get("error"))
    return {"ok": True, "detail": "Тестовое сообщение отправлено в Telegram."}


@app.get("/api/system/recommend")
def system_recommend(user: str = Depends(current_user)):
    """Д15: recommend a Whisper model from the machine's RAM/CPU, so the user
    doesn't pick blind."""
    ram_gb = cpu = None
    try:
        with open("/proc/meminfo", encoding="utf-8") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    ram_gb = round(int(line.split()[1]) / 1024 / 1024, 1)
                    break
        cpu = os.cpu_count()
    except OSError:
        pass
    if ram_gb is None:
        return {"model": "", "detail": ""}
    if ram_gb >= 12:
        model, why = "large-v3-turbo", "точная и быстрая, ей хватает вашей памяти"
    elif ram_gb >= 6:
        model, why = "medium", "разумный баланс для этой памяти"
    else:
        model, why = "small", "мало памяти — компактная модель надёжнее"
    return {"model": model, "ram_gb": ram_gb, "cpu": cpu,
            "detail": f"Для этой машины ({ram_gb} ГБ RAM, {cpu} CPU) рекомендуем "
                      f"«{model}» — {why}."}


@app.get("/api/presets")
def list_presets(user: str = Depends(current_user)):
    """Д11: protocol presets — builtins + the team's custom ones."""
    from .automation import settings as auto_settings
    team = security.team_of(user)
    custom = auto_settings.load(team).get("custom_presets") or {}
    out = [{"value": k, "label": v["label"]} for k, v in analyze.PROTOCOL_PRESETS.items()]
    out += [{"value": k, "label": f"{k} (свой)"} for k in sorted(custom)]
    return {"presets": out}


class PresetBody(BaseModel):
    name: str
    rules: str = ""


@app.post("/api/presets")
def save_preset(body: PresetBody, user: str = Depends(current_user)):
    """Save/update the team's custom preset (empty rules = delete)."""
    from .automation import settings as auto_settings
    team = security.team_of(user)
    name = re.sub(r"[^\w\- ]", "", body.name).strip()[:40]
    if not name:
        raise HTTPException(400, "Укажите имя пресета.")
    custom = dict(auto_settings.load(team).get("custom_presets") or {})
    if body.rules.strip():
        custom[name] = body.rules.strip()[:4000]
    else:
        custom.pop(name, None)
    auto_settings.save(team, {"custom_presets": custom})
    return {"ok": True, "presets": sorted(custom)}


@app.post("/api/jobs/{job_id}/notes")
def set_job_notes(job_id: str, body: NotesBody, user: str = Depends(current_user)):
    """Attach the participant's live meeting notes to a job (Д6). They join the
    next protocol generation («Пересобрать» applies them to an existing one)."""
    _require_owned(job_id, user)
    job = store.set_notes(job_id, body.notes)
    return {"ok": True, "has_notes": bool(job.user_notes)}


class AnalysisPatch(BaseModel):
    analysis: dict


@app.patch("/api/jobs/{job_id}/analysis")
def patch_analysis(job_id: str, body: AnalysisPatch, user: str = Depends(current_user)):
    """Д13: save the user's manual protocol edits (docx re-renders)."""
    _require_owned(job_id, user)
    try:
        job = store.update_analysis(job_id, body.analysis or {})
    except ValueError as e:
        raise HTTPException(409, str(e))
    # Правка руками — самый сильный признак, что протокол прочитали и он нужен
    # (И12, И19). `once_a_day=False`: правок за день бывает много, и каждая
    # что-то говорит о качестве.
    events.record(events.EDITED, user=user, job_id=job_id, once_a_day=False)
    return {"ok": True, "analysis": job.analysis}


class RegenTopicBody(BaseModel):
    index: int
    provider: str | None = None


@app.post("/api/jobs/{job_id}/regen-topic")
def regen_topic(job_id: str, body: RegenTopicBody, user: str = Depends(current_user)):
    """Д13: re-generate ONE topic of the protocol via the LLM."""
    _require_owned(job_id, user)
    try:
        job = store.regen_topic(job_id, body.index, provider=body.provider)
    except ValueError as e:
        raise HTTPException(409, str(e))
    events.record(events.REGEN, user=user, job_id=job_id, once_a_day=False)
    return {"ok": True, "analysis": job.analysis}


class AskBody(BaseModel):
    question: str


@app.post("/api/jobs/{job_id}/ask")
def ask_meeting_api(job_id: str, body: AskBody, user: str = Depends(current_user)):
    """Д13: chat over the meeting — answer with timecodes from the transcript."""
    _require_owned(job_id, user)
    q = (body.question or "").strip()
    if len(q) < 3:
        raise HTTPException(400, "Сформулируйте вопрос.")
    try:
        answer = store.ask(job_id, q)
    except ValueError as e:
        raise HTTPException(409, str(e))
    # ⚠️ Текст вопроса в событие НЕ идёт: содержание разговоров в аналитику не
    # попадает ни в каком виде (И60). Нужен только факт.
    events.record(events.ASKED, user=user, job_id=job_id, once_a_day=False)
    return {"ok": True, "answer": answer}


@app.post("/api/jobs/{job_id}/redeliver")
def redeliver_job(job_id: str, user: str = Depends(current_user)):
    """Re-attempt the protocol DELIVERY only (cloud upload + Weeek link) — for
    when the protocol built fine but the upload/attach failed. No LLM re-run."""
    _require_owned(job_id, user)
    res = store.redeliver(job_id)
    if res == "delivered":
        return {"ok": True, "detail": "Протокол выгружен и прикреплён к задаче Weeek."}
    if res == "pending":
        raise HTTPException(409, "Протокол ещё не готов — доставка выполнится сама после сборки.")
    raise HTTPException(502, f"Доставка не удалась: {res}")


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
    """Результат задачи в выбранном формате: txt, plain, srt, json, docx, screen."""
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
        # no-store: адрес у документа один и тот же, а содержимое после
        # «Пересобрать» новое. Без запрета браузер отдавал СТАРЫЙ Word из
        # своего кэша (эвристика по Last-Modified) — «скачивается старая версия».
        # Выгрузка — человеческое действие (И12): протокол унесли из сервиса.
        # ⚠️ Что с ним было дальше, мы не знаем: «переслал команде» и
        # «распечатал и забыл» здесь неотличимы — читать только вместе с И9.
        events.record(events.EXPORTED, user=user, job_id=job_id)
        return FileResponse(
            path,
            media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            filename=f"{_safe_stem(job.filename)}_{prov}_протокол.docx",
            headers={"Cache-Control": "no-store"},
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
        return PlainTextResponse(path.read_text(encoding="utf-8"),
                                 headers={"Cache-Control": "no-store"})
    media = "application/json" if format == "json" else "text/plain"
    return FileResponse(path, media_type=media,
                        filename=f"{_safe_stem(job.filename)}.{format}",
                        headers={"Cache-Control": "no-store"})


def _safe_stem(name: str | None) -> str:
    stem = Path(name or "audio").stem
    keep = "".join(c if c.isalnum() or c in "-_ " else "_" for c in stem).strip()
    return (keep or "audio")[:80]


# Отдельные маршруты готовности (diarization/screen/speakers) убраны: то же
# самое отдаёт /api/setup/deps, и именно его показывает блок «Готовность к
# записи» на «Обзоре». Задание токена HuggingFace жило только в памяти процесса
# и терялось при перезапуске — токен задаётся переменной окружения HF_TOKEN.


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
    """Поднят ли локальный движок (Ollama) и есть ли на нём нужная модель."""
    from . import ollama_setup
    return ollama_setup.status()


# ---- Готовность необязательных компонентов --------------------------------
# Установку отсюда убрали: в контейнере она была невозможна (нет root и sudo,
# pip без root кладёт пакеты вне тома), а кнопок к этим маршрутам не было ни в
# одном экране. Всё нужное собрано в образе — осталась только проверка.
@app.get("/api/setup/auto")
def autosetup_status():
    """Ход подготовки при старте (предзагрузка модели речи) + готовность
    компонентов."""
    from . import autosetup
    return autosetup.status()


@app.get("/api/setup/deps")
def deps_status():
    """Готовность необязательных компонентов (playwright/диаризация/ffmpeg/OCR)."""
    from . import deps_setup
    return deps_setup.status()


# ---- Recognition models (pre-download from the UI, no transcription) -------
def _known_model(name: str) -> str:
    """Имя модели из списка, который предлагает интерфейс, — или отказ.

    faster-whisper трактует имя со слэшем как идентификатор репозитория
    HuggingFace и качает его в кэш контейнера. Маршрут скачивания открыт любому
    вошедшему, а регистрация в сервисе открыта всем, кто знает адрес: без этой
    проверки посторонний мог занять весь диск чужими репозиториями, а забитый
    диск останавливает и запись встреч, и распознавание, и Postgres. Пустое
    имя означает «модель по умолчанию» и допустимо.
    """
    name = (name or "").strip()
    if not name:
        return ""
    if name not in ALLOWED_MODELS:
        raise HTTPException(400, f"Неизвестная модель: {name[:60]}")
    return name


@app.get("/api/model/status")
def model_status(name: str = "", user: str = Depends(current_user)):
    """Whether a Whisper model is downloaded + download progress."""
    from . import whisper_setup
    return whisper_setup.status(_known_model(name))


@app.post("/api/model/download")
def model_download(name: str = "", user: str = Depends(current_user)):
    """Download a Whisper model into the cache (background, no transcription).

    Операция ОБЩЕСЕРВЕРНАЯ: кэш моделей и диск общие для всех команд."""
    from . import whisper_setup
    return whisper_setup.download(_known_model(name))




@app.get("/healthz")
def healthz():
    return {"ok": True, "model": config.MODEL, "diarization": config.DIARIZATION_ENABLED,
            "build": os.getenv("VTX_BUILD", "dev")}


# Register the SPA routes last, once current_user (their auth dependency) exists.
_register_spa()
