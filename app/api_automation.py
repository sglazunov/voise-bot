"""Маршруты автоматизации встреч: Weeek → запись → облако → протокол.

Вынесены из main.py: 26 маршрутов на 400 строк — почти четверть модуля, и все
они про одну подсистему (app/automation). Пути не изменились: префикс
/api/automation задан у роутера.
"""
from __future__ import annotations

import os
import tempfile
import threading
import time
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, ConfigDict

from . import logs, security
from .deps import NotesBody, current_user

log = logs.get("vtx.api.automation")

router = APIRouter(prefix="/api/automation", tags=["automation"])


# --------------------------------------------------------------------------- #
# Meeting automation (Weeek → record → cloud → protocol). See app/automation.
# --------------------------------------------------------------------------- #
@router.get("/settings")
def automation_settings(user: str = Depends(current_user)):
    """Current automation settings (secrets redacted to presence flags)."""
    from .automation import settings as auto_settings
    return auto_settings.redacted(user)


class AutomationSettings(BaseModel):
    """Настройки автоматизации. Список ключей — из settings._DEFAULTS.

    Раньше здесь был РУЧНОЙ список полей, и он отстал от умолчаний на
    одиннадцать ключей: telegram_bot_token/telegram_chat_id, strict_verify,
    live_transcribe/live_interval_min, analyze_preset, identify_speakers,
    chat_stop_word, weeek_use_record_field/weeek_record_field,
    end_if_nobody_joins_sec. Pydantic молча отбрасывал лишние поля — фронт их
    слал, `model_dump()` не содержал, и настройки НЕ СОХРАНЯЛИСЬ вообще:
    Telegram-уведомления нельзя было включить, стоп-слово — сменить, пресет и
    строгая проверка навсегда оставались умолчанием.

    Теперь модель принимает любые ключи, но сохраняются только известные — так
    список не может снова разойтись с _DEFAULTS.
    """
    model_config = ConfigDict(extra="allow")

    def known(self) -> dict:
        """Присланные значения, оставив только существующие настройки."""
        from .automation import settings as auto_settings
        allowed = set(auto_settings._DEFAULTS)
        return {k: v for k, v in self.model_dump().items() if k in allowed}


@router.post("/settings")
def automation_save(body: AutomationSettings, user: str = Depends(current_user)):
    """Persist automation settings. Only non-null fields are updated."""
    from .automation import settings as auto_settings
    # None отбрасываем — так фронт помечает «поле не трогали». Пустая строка
    # при этом ЗНАЧИМА: ею очищают поле (проект Weeek, фильтры, папка).
    values = {k: v for k, v in body.known().items() if v is not None}
    auto_settings.save(user, values)
    return auto_settings.redacted(user)


@router.get("/meetings")
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


@router.post("/meetings/{task_id}/decision")
def automation_meeting_decision(task_id: str, body: MeetingDecision,
                                user: str = Depends(current_user)):
    """Choose whether the bot records this specific meeting (overrides filters)."""
    from .automation.scheduler import scheduler
    return scheduler.set_decision(user, task_id, body.record)


@router.get("/clouds/status")
def automation_clouds_status(user: str = Depends(current_user)):
    """Per-backend cloud readiness + which one is selected (for the UI)."""
    from .automation import settings as auto_settings, clouds
    return clouds.readiness(auto_settings.load(user))


@router.post("/clouds/test")
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


@router.get("/scheduler/status")
def automation_scheduler_status(user: str = Depends(current_user)):
    """Scheduler state + the meetings it's tracking and their pipeline status."""
    from .automation.scheduler import scheduler
    scheduler.start()  # idempotent — ensures it's running even if startup was skipped
    return scheduler.status(user)


@router.post("/scheduler/run-now")
def automation_scheduler_run_now(task_id: str, user: str = Depends(current_user)):
    """Manually record a known meeting right now (poll Weeek first to populate)."""
    from .automation.scheduler import scheduler
    res = scheduler.run_now(user, task_id)
    if not res.get("ok"):
        raise HTTPException(400, res.get("error"))
    return res


@router.post("/scheduler/poll-now")
def automation_scheduler_poll_now(user: str = Depends(current_user)):
    """Force an immediate Weeek re-poll (the manual «Обновить статус» button)."""
    from .automation.scheduler import scheduler
    res = scheduler.poll_now(user)
    if not res.get("ok"):
        raise HTTPException(400, res.get("error"))
    return res


class MeetingLinksIn(BaseModel):
    task_id: str
    video_url: str = ""
    protocol_url: str = ""


@router.post("/meetings/links")
def automation_meeting_links(inp: MeetingLinksIn, user: str = Depends(current_user)):
    """Manually attach the video and/or protocol link to a Weeek task — for
    meetings where automation could not deliver them (cloud refused the upload,
    the LLM was down, the file only exists on someone's laptop…)."""
    from .automation import settings as auto_settings, weeek
    team = security.team_of(user)
    cfg = auto_settings.load(team)
    token = cfg.get("weeek_token")
    if not token:
        raise HTTPException(400, "Сначала подключите Weeek в «Автоматизации».")
    task_id = _clean_task_id(inp.task_id)
    if not task_id:
        raise HTTPException(400, "Не указана задача Weeek (нужен номер или ссылка).")
    pairs = []   # (человекочитаемое имя, поле Weeek, url, эмодзи)
    for label, field_key, default, url, emoji in (
            ("видео", "weeek_video_field", "Видео встречи", inp.video_url, "🎥"),
            ("протокол", "weeek_protocol_field", "Протокол встречи", inp.protocol_url, "📄")):
        url = (url or "").strip()
        if not url:
            continue
        if not url.startswith(("http://", "https://")):
            raise HTTPException(400, f"Ссылка на {label} должна начинаться с http(s)://")
        pairs.append((label, (cfg.get(field_key) or default).strip(), url, emoji))
    if not pairs:
        raise HTTPException(400, "Укажите хотя бы одну ссылку.")
    attached, failed = [], []
    for label, field, url, emoji in pairs:
        res = weeek.set_custom_field(token, task_id, field, url)
        if res.get("ok"):
            attached.append(f"{label} → поле «{field}»")
            continue
        # The link must not get lost — leave it as a comment instead.
        if weeek.add_comment(token, task_id, f"{emoji} {label.capitalize()} встречи: {url}"):
            attached.append(f"{label} → комментарий (поле: {res.get('error')})")
        else:
            failed.append(f"{label}: {res.get('error') or 'ошибка Weeek'}")
    if failed and not attached:
        raise HTTPException(502, "Не удалось прикрепить: " + "; ".join(failed))
    detail = "Прикреплено: " + "; ".join(attached)
    if failed:
        detail += ". Не удалось: " + "; ".join(failed)
    return {"ok": not failed, "detail": detail}


@router.get("/meetings/{task_id}/live")
def meeting_live(task_id: str, user: str = Depends(current_user)):
    """Д10: live transcript while the bot records; the final transcript after."""
    from .automation.scheduler import scheduler
    res = scheduler.live_view(user, task_id)
    if not res.get("ok"):
        raise HTTPException(404, res.get("error"))
    return res


@router.get("/meetings/{task_id}/screenshot")
def meeting_screenshot(task_id: str, kind: str = "png",
                       user: str = Depends(current_user)):
    """Скриншот (`kind=png`) или HTML (`kind=html`) страницы, на которой бот
    НЕ СМОГ войти на встречу. Раньше файлы лежали в папке записей и «почему
    не зашёл» разбиралось по ssh; теперь — прямо с карточки встречи."""
    from .automation.scheduler import scheduler
    if kind not in ("png", "html"):
        raise HTTPException(400, "kind: png | html")
    path = scheduler.join_screenshot(user, task_id, kind)
    if path is None:
        raise HTTPException(404, "Скриншота для этой встречи нет.")
    media = "image/png" if kind == "png" else "text/html; charset=utf-8"
    # Сохранённая страница Телемоста отдаётся как ФАЙЛ, а не рендерится:
    # чужие скрипты в контексте нашего сайта не нужны.
    headers = {"Cache-Control": "no-store"}
    # ⚠️ Имя записи кириллическое («21.09.2026, 09:00. - ОД сайт…»), а в
    # HTTP-заголовок годится только латиница: подстановка path.name роняла
    # ответ на кодировке — «ошибка при скачивании». Имя файла — своё, ASCII.
    safe = "".join(ch for ch in str(task_id) if ch.isalnum() or ch in "-_")[:40] or "meeting"
    if kind == "html":
        media = "text/plain; charset=utf-8"
        return FileResponse(path, media_type=media, headers=headers,
                            filename=f"telemost-{safe}.join-failed.html",
                            content_disposition_type="attachment")
    return FileResponse(path, media_type=media, headers=headers)


@router.get("/meetings/{task_id}/notes")
def meeting_notes_get(task_id: str, user: str = Depends(current_user)):
    """Заметки участника по встрече."""
    from .automation.scheduler import scheduler
    res = scheduler.get_meeting_notes(user, task_id)
    if not res.get("ok"):
        raise HTTPException(404, res.get("error"))
    return res


@router.post("/meetings/{task_id}/notes")
def meeting_notes_set(task_id: str, body: NotesBody, user: str = Depends(current_user)):
    """Participant's notes typed during/after the meeting (Д6+Д10): stored on
    the meeting, forwarded to its recognition job as soon as it exists."""
    from .automation.scheduler import scheduler
    res = scheduler.set_meeting_notes(user, task_id, body.notes)
    if not res.get("ok"):
        raise HTTPException(404, res.get("error"))
    return res


@router.post("/scheduler/stop-recording")
def automation_scheduler_stop_recording(task_id: str | None = None,
                                        user: str = Depends(current_user)):
    """Stop recording(s): a specific meeting (task_id) or all of the user's."""
    from .automation.scheduler import scheduler
    res = scheduler.stop_recording(user, task_id)
    if not res.get("ok"):
        raise HTTPException(400, res.get("error"))
    return res


@router.get("/recorder/status")
def automation_recorder_status(user: str = Depends(current_user)):
    """What the Telemost recorder needs (Playwright/ffmpeg/audio) — for the UI."""
    from .automation import settings as auto_settings, recorder
    return recorder.readiness(auto_settings.load(user))


@router.get("/recorder/login-status")
def automation_recorder_login_status(user: str = Depends(current_user)):
    """Whether the recorder profile is logged into Yandex (for the UI hint)."""
    from .automation import settings as auto_settings
    from .automation.recorder import browser
    return browser.login_status(auto_settings.load(user))


@router.get("/recorder/audio-test")
def automation_recorder_audio_test(user: str = Depends(current_user)):
    """Record a few seconds from the chosen audio device and report its level —
    so the user can verify the meeting's sound actually reaches it."""
    from .automation import settings as auto_settings
    from .automation.recorder import capture
    cfg = auto_settings.load(user)
    return capture.test_audio_level("ffmpeg")


@router.post("/recorder/login")
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


class LoginAction(BaseModel):
    kind: str = "click"          # click | type | key | scroll | goto
    x: float = 0
    y: float = 0
    text: str = ""
    key: str = "Enter"
    dy: float = 240
    url: str = ""


@router.post("/recorder/login/open")
def automation_login_open(user: str = Depends(current_user)):
    """Открыть сессию входа в Яндекс: браузер на СЕРВЕРЕ, экран — в интерфейсе.

    Прежняя кнопка входа открывала headed-браузер внутрь Xvfb, и увидеть его
    удалённо было нельзя. Без входа бот заходит гостем, а гостю Телемост не
    показывает чат — стоп-слово не работает.
    """
    from .automation import settings as auto_settings
    from .automation.recorder import browser
    if not browser.playwright_available():
        raise HTTPException(400, "Playwright не установлен.")
    try:
        ses = browser.login_open(auto_settings.load(user), security.team_of(user))
    except RuntimeError as e:
        raise HTTPException(409, str(e))
    return {"ok": True, "width": ses.size[0], "height": ses.size[1]}


@router.get("/recorder/login/screen")
def automation_login_screen(user: str = Depends(current_user)):
    """Текущий экран браузера входа (PNG) — только своей команды."""
    from .automation.recorder import browser
    ses = browser.login_get(security.team_of(user))
    if ses is None:
        raise HTTPException(409, "Сессия входа не запущена.")
    shot = ses.screenshot()
    if not shot:
        raise HTTPException(202, "Экран ещё не готов.")
    return Response(content=shot, media_type="image/png",
                    headers={"Cache-Control": "no-store"})


@router.post("/recorder/login/action")
def automation_login_action(body: LoginAction, user: str = Depends(current_user)):
    """Клик, ввод текста, клавиша или переход — внутрь браузера входа своей
    команды. Переход (`goto`) — только на страницы паспорта Яндекса."""
    from .automation.recorder import browser
    ses = browser.login_get(security.team_of(user))
    if ses is None:
        raise HTTPException(409, "Сессия входа не запущена.")
    if body.kind == "goto" and not browser._allowed_login_url(body.url or ""):
        raise HTTPException(400, "Переход разрешён только на страницы входа Яндекса.")
    ses.send(body.kind, x=body.x, y=body.y, text=body.text,
             key=body.key, dy=body.dy, url=body.url)
    return {"ok": True}


@router.post("/recorder/login/close")
def automation_login_close(user: str = Depends(current_user)):
    """Закрыть окно входа в Яндекс на сервере (своей команды)."""
    from .automation.recorder import browser
    ses = browser.login_sessions.get(security.team_of(user))
    if ses is not None:
        ses.close()
    return {"ok": True}


class RecorderTest(BaseModel):
    url: str
    seconds: int = 30


@router.post("/recorder/test")
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


@router.get("/weeek/projects")
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


@router.get("/weeek/members")
def automation_weeek_members(refresh: int = 0, user: str = Depends(current_user)):
    """Участники воркспейса Weeek (кэш на сутки в настройках команды) — для
    выбора исполнителя задачи из протокола."""
    from .automation import settings as auto_settings, weeek
    cfg = auto_settings.load(user)
    token = cfg.get("weeek_token")
    if not token:
        raise HTTPException(400, "Сначала подключите Weeek.")
    cache = cfg.get("weeek_members_cache") or {}
    fresh = time.time() - float(cache.get("at") or 0) < 86400
    if cache.get("members") and fresh and not refresh:
        return {"members": cache["members"], "cached": True}
    try:
        members = weeek.list_members(token)
    except weeek.WeeekError as e:
        if cache.get("members"):
            return {"members": cache["members"], "cached": True, "error": str(e)}
        raise HTTPException(502, str(e))
    auto_settings.save(user, {"weeek_members_cache": {"at": time.time(), "members": members}})
    return {"members": members, "cached": False}


@router.get("/weeek/boards")
def automation_weeek_boards(project_id: str = "", user: str = Depends(current_user)):
    from .automation import settings as auto_settings, weeek
    token = auto_settings.get(user, "weeek_token")
    if not token:
        raise HTTPException(400, "Сначала подключите Weeek.")
    if not project_id.strip().isdigit():
        raise HTTPException(400, "Нужен числовой id проекта.")
    try:
        return {"boards": weeek.list_boards(token, project_id.strip())}
    except weeek.WeeekError as e:
        raise HTTPException(502, str(e))


@router.get("/weeek/board-columns")
def automation_weeek_board_columns(board_id: str = "", user: str = Depends(current_user)):
    from .automation import settings as auto_settings, weeek
    token = auto_settings.get(user, "weeek_token")
    if not token:
        raise HTTPException(400, "Сначала подключите Weeek.")
    if not board_id.strip().isdigit():
        raise HTTPException(400, "Нужен числовой id доски.")
    try:
        return {"columns": weeek.list_board_columns(token, board_id.strip())}
    except weeek.WeeekError as e:
        raise HTTPException(502, str(e))


class UserMapBody(BaseModel):
    name: str
    user_id: str = ""          # пусто = убрать соответствие


@router.post("/weeek/user-map")
def automation_weeek_user_map(body: UserMapBody, user: str = Depends(current_user)):
    """Запомнить «имя в протоколе → участник Weeek» для всей команды."""
    from .automation import settings as auto_settings
    name = (body.name or "").strip()[:120]
    if not name:
        raise HTTPException(400, "Пустое имя.")
    cfg = auto_settings.load(user)
    m = dict(cfg.get("weeek_user_map") or {})
    if body.user_id.strip():
        m[name] = body.user_id.strip()[:80]
    else:
        m.pop(name, None)
    auto_settings.save(user, {"weeek_user_map": m})
    return {"weeek_user_map": m}


@router.get("/weeek/probe")
def automation_weeek_probe(task_id: str, user: str = Depends(current_user)):
    """Return the raw JSON of one Weeek task — used to pin date/link field names."""
    from .automation import settings as auto_settings, weeek
    token = auto_settings.get(user, "weeek_token")
    if not token:
        raise HTTPException(400, "Сначала задайте токен Weeek в настройках.")
    task_id = _clean_task_id(task_id)
    if not task_id:
        raise HTTPException(400, "Нужен номер задачи Weeek или ссылка на неё.")
    try:
        return weeek.probe_task(token, task_id)
    except weeek.WeeekError as e:
        raise HTTPException(502, str(e))


def _clean_task_id(raw: str) -> str:
    """Номер задачи Weeek из номера или ссылки; '' если это не номер. Значение
    уходит в путь запроса к API — произвольная строка туда попадать не должна."""
    from .jobs import _weeek_task_id
    tid = _weeek_task_id(raw or "")
    return tid if tid.isdigit() else ""
