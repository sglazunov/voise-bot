"""Сводное ревью 30.10.2026: P1 планировщика и протокола.

Каждый тест падал до правки:
1. снапшоты читались из БД под глобальным локом планировщика (регресс V20);
2. «одна комната — один бот»: занятость комнаты проверялась под одним локом,
   а слот захватывался под другим — между ними успевала стартовать запись
   «по ссылке» в ту же комнату;
3. локальное облако — общая папка всех команд: одинаковое имя файла у двух
   команд перезаписывало чужую запись;
4. расход токенов пропадал, если сборка протокола упала или её отменили;
5. механическая опора (`_find_support`, ≥60 % основ) считалась
   подтверждением — Word писал «подтверждены расшифровкой», а Weeek
   заводил такие задачи сам;
6. срок задачи Weeek и день встречи брались из номера версии в названии
   («Ревью 2.4.15» → 02.04.2015).
"""
from __future__ import annotations

import json
import re
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from app import analyze, config, usage
from app import weeek_tasks as wt
from app.automation import clouds, delivery
from app.automation import scheduler as sched_mod
from app.automation.scheduler import MeetingState, Scheduler
from app.automation.weeek import Meeting

URL = "https://telemost.yandex.ru/j/78283935125180"


# --------------------------------------------------------------------------- #
# 1. Снапшоты — вне глобального лока
# --------------------------------------------------------------------------- #
class TestСнапшотыВнеЛока:
    def test_новая_карточка_восстанавливается_без_чтения_бд_под_локом(self, monkeypatch):
        s = Scheduler()
        under_lock: list[bool] = []

        def load(user):
            under_lock.append(s._lock.locked())
            return {f"t:42:{START.isoformat()}": {"state": "done", "detail": "Готово",
                                                 "cloud_url": "https://disk/x"}}

        START = datetime(2026, 10, 30, 10, 0, tzinfo=timezone.utc)
        monkeypatch.setattr(sched_mod.snapshots, "load", load)
        monkeypatch.setattr(sched_mod.weeek, "upcoming_meetings", lambda *a, **k: [
            Meeting(task_id=42, title="Планёрка", url=URL, start=START),
            Meeting(task_id=43, title="Ревью", url=URL, start=START + timedelta(hours=1))])
        monkeypatch.setattr(s, "_wipe_inherited", lambda *a, **k: None)
        s._poll("t", {})
        assert under_lock and not any(under_lock), "снапшоты читаются под self._lock"
        # Исход прошлого процесса всё равно восстановлен — из того же чтения.
        st = s._states[f"t:42:{START.isoformat()}"]
        assert st.state == "done" and st.cloud_url == "https://disk/x"


# --------------------------------------------------------------------------- #
# 2. Проверка комнаты и захват — под одним локом
# --------------------------------------------------------------------------- #
@pytest.fixture
def sched(monkeypatch):
    monkeypatch.setenv("VTX_RECORDER_ENABLED", "1")
    monkeypatch.setattr(sched_mod.stats, "record_meeting", lambda st, o: None)
    monkeypatch.setattr(sched_mod.snapshots, "save", lambda st: None)
    monkeypatch.setattr(sched_mod.auto_settings, "load", lambda u: {"lookahead_min": 2})
    s = Scheduler()
    s._boot_time = 0
    s.started, s.released = [], []
    monkeypatch.setattr(sched_mod.recorder, "release_slot", lambda slot: s.released.append(slot))

    class T:
        def __init__(self, *a, target=None, args=(), **k):
            self.args = args

        def start(self):
            s.started.append(self.args[0])
    monkeypatch.setattr(sched_mod.threading, "Thread", T)
    return s


class TestОднаКомнатаОдинБот:
    def test_запись_по_ссылке_между_проверкой_и_захватом(self, sched, monkeypatch):
        now = datetime.now(timezone.utc)
        chosen = MeetingState(key="t:200:new", task_id="200", title="ЭМО Встреча",
                              url=URL, start=now - timedelta(seconds=5), owner="t",
                              state="scheduled")
        sched._states[chosen.key] = chosen
        adhoc = MeetingState(key="t:link-1:x", task_id="link-20261030-ab12",
                             title="По ссылке", url=URL, start=now, owner="t",
                             state="scheduled")

        def acquire():
            # Пока _maybe_trigger был вне лока (писал снапшоты, брал слот),
            # человек нажал «Бот по ссылке» в ту же комнату.
            with sched._lock:
                adhoc.state = "recording"
                sched._states[adhoc.key] = adhoc
            return "slot-1"
        monkeypatch.setattr(sched_mod.recorder, "acquire_slot", acquire)

        sched._maybe_trigger("t", {"lookahead_min": 2})

        assert chosen.state != "recording", "в комнату зашёл второй бот"
        assert chosen not in sched.started
        assert sched.released == ["slot-1"]

    def test_свободная_комната_захватывается_как_раньше(self, sched, monkeypatch):
        now = datetime.now(timezone.utc)
        chosen = MeetingState(key="t:200:new", task_id="200", title="ЭМО Встреча",
                              url=URL, start=now - timedelta(seconds=5), owner="t",
                              state="scheduled")
        sched._states[chosen.key] = chosen
        monkeypatch.setattr(sched_mod.recorder, "acquire_slot", lambda: "slot-1")
        sched._maybe_trigger("t", {"lookahead_min": 2})
        assert chosen.state == "recording" and sched.started == [chosen]
        assert sched.released == []


# --------------------------------------------------------------------------- #
# 3. Локальное облако — папка команды
# --------------------------------------------------------------------------- #
NAME = "30.10.2026, 11:00. - Планёрка.mp4"


class TestЛокальноеОблако:
    @pytest.fixture(autouse=True)
    def _data(self, tmp_path, monkeypatch):
        monkeypatch.setattr(config, "DATA_DIR", tmp_path)
        self.tmp = tmp_path

    def _src(self, name: str, body: str) -> str:
        p = self.tmp / "src" / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body, encoding="utf-8")
        return str(p)

    def test_одинаковое_имя_у_двух_команд_не_перезаписывается(self):
        ra = clouds.upload(self._src("a.mp4", "A"), NAME, {"cloud": "local"}, owner="alpha")
        rb = clouds.upload(self._src("b.mp4", "B"), NAME, {"cloud": "local"}, owner="beta")
        assert ra["ok"] and rb["ok"]
        assert ra["path"] != rb["path"]
        assert Path(ra["path"]).read_text(encoding="utf-8") == "A"
        assert Path(rb["path"]).read_text(encoding="utf-8") == "B"

    def test_подпапка_встречи_и_папка_протоколов_тоже_у_команды(self):
        ra = clouds.upload(self._src("a.mp4", "A"), NAME, {"cloud": "local"},
                           subfolder="Разовые встречи", owner="alpha")
        rb = clouds.upload(self._src("b.mp4", "B"), NAME, {"cloud": "local"},
                           subfolder="Разовые встречи", owner="beta")
        assert Path(ra["path"]).read_text(encoding="utf-8") == "A"
        assert Path(rb["path"]).read_text(encoding="utf-8") == "B"
        pa = clouds.upload(self._src("a.docx", "PA"), "x - протокол.docx",
                           {"cloud": "local"}, folder="disk:/Протоколы", owner="alpha")
        pb = clouds.upload(self._src("b.docx", "PB"), "x - протокол.docx",
                           {"cloud": "local"}, folder="disk:/Протоколы", owner="beta")
        assert Path(pa["path"]).read_text(encoding="utf-8") == "PA"
        assert Path(pb["path"]).read_text(encoding="utf-8") == "PB"

    def test_своя_папка_команды_не_меняется(self):
        own = self.tmp / "my-disk"
        r = clouds.upload(self._src("a.mp4", "A"), NAME,
                          {"cloud": "local", "local_dir": str(own)}, owner="alpha")
        assert Path(r["path"]).parent == own

    def test_выгрузка_записи_передаёт_команду(self):
        r = delivery.upload_with_retry(self._src("a.mp4", "A"), {"cloud": "local"},
                                       lambda m: None, attempts=1, owner="alpha")
        assert r["ok"] and "alpha" in Path(r["path"]).parts

    def test_все_вызовы_выгрузки_в_коде_передают_команду(self):
        # Забытый owner молча вернул бы общую папку — ловим это статически.
        root = Path(__file__).resolve().parents[1] / "app"
        bad = []
        call = re.compile(r"\b(clouds\.upload|upload_with_retry|publish_or_upload)\(")
        for f in root.rglob("*.py"):
            src = f.read_text(encoding="utf-8")
            for m in call.finditer(src):
                if src[max(0, m.start() - 4):m.start()] == "def ":
                    continue
                depth, i = 0, m.end() - 1
                while i < len(src):
                    depth += {"(": 1, ")": -1}.get(src[i], 0)
                    if depth == 0:
                        break
                    i += 1
                args = src[m.end():i]
                if "owner=" not in args:
                    line = src.count("\n", 0, m.start()) + 1
                    bad.append(f"{f.relative_to(root)}:{line}")
        assert not bad, f"выгрузка без owner: {bad}"


# --------------------------------------------------------------------------- #
# 4. Расход токенов при сбое и отмене
# --------------------------------------------------------------------------- #
class TestРасходПриСбое:
    def _job(self, jid: str):
        from app.jobs import STATUS_DONE, Job, store
        job = Job(id=jid, filename="Планёрка.mp4", audio_path="", language="ru",
                  diarize=False, owner="alice", analyze=True, status=STATUS_DONE,
                  created_at=time.time())
        store._jobs[job.id] = job
        return job, store

    @pytest.mark.parametrize("exc", ["fail", "cancel"])
    def test_пересборка(self, monkeypatch, exc):
        job, store = self._job(f"p1usage-{exc}")

        def fake(*a, **kw):
            usage.record("gemini/x", 1000, 0, 200)
            if exc == "cancel":
                raise analyze.AnalysisCancelled()
            raise RuntimeError("шлюз упал")
        monkeypatch.setattr(analyze, "analyze_transcript", fake)
        store._reanalyze_lock.acquire()
        store._do_reanalyze(job, "[00:01] текст встречи")
        assert (job.llm_usage or {}).get("calls") == 1
        assert job.llm_usage["in"] == 1000 and job.llm_usage["out"] == 200

    def test_отмена_во_время_проверки_цитат(self, monkeypatch):
        job, store = self._job("p1usage-verify")

        def fake_analyze(*a, **kw):
            usage.record("gemini/x", 500, 0, 50)
            return {"summary": "s", "tasks": [{"task": "x"}]}

        def fake_verify(*a, **kw):
            usage.record("gemini/x", 300, 0, 30)
            raise analyze.AnalysisCancelled()
        monkeypatch.setattr(analyze, "analyze_transcript", fake_analyze)
        monkeypatch.setattr(analyze, "verify_protocol", fake_verify)
        store._reanalyze_lock.acquire()
        store._do_reanalyze(job, "[00:01] текст встречи")
        assert job.llm_usage.get("calls") == 2 and job.llm_usage["in"] == 800

    def test_первый_прогон_тоже(self, monkeypatch):
        job, store = self._job("p1usage-first")

        def fake(*a, **kw):
            usage.record("gemini/x", 700, 0, 70)
            raise RuntimeError("шлюз упал")
        monkeypatch.setattr(analyze, "analyze_transcript", fake)
        with pytest.raises(RuntimeError):
            store._protocol_pass(job, "текст", context="")
        assert job.llm_usage.get("calls") == 1


# --------------------------------------------------------------------------- #
# 5. Механическая опора — не подтверждение
# --------------------------------------------------------------------------- #
SPEECH = "\n".join([
    "[04:30] Зоя Р: по серверу у нас что?",
    "[04:46] Зоя Р: Кирилл, надо обновить сервер, и закрыть задачу по конструктору.",
    "[04:57] Кирилл Бубнов: да, сделаю.",
    "[05:20] Зоя Р: ладно, идём дальше.",
])


class _Empty:
    name = "x"
    model = "m"

    def complete(self, prompt, max_tokens=2000, force_json=True, **kw):
        return json.dumps({"items": []})     # модель цитат не нашла


class TestМеханическаяОпора:
    def _verified(self, monkeypatch):
        monkeypatch.setattr(analyze.llm, "get_provider_chain", lambda *a, **k: _Empty())
        res = {"tasks": [{"task": "Обновить сервер и закрыть задачу по конструктору",
                          "owner": "Кирилл Бубнов"}],
               "minor_tasks": [], "done_tasks": [], "decisions": [], "detailed": []}
        return analyze.verify_protocol(res, SPEECH)

    def test_опора_помечена_и_не_считается_подтверждённой(self, monkeypatch):
        out = self._verified(monkeypatch)
        v = out["verification"]["tasks"][0]
        assert v["support"] == "mechanical"
        assert v["quote"] and v["t"] == "04:46"           # место в разговоре — есть
        stats = out["verification"]["stats"]
        assert stats["confirmed"] == 0 and stats["mechanical"] == 1
        assert stats["version"] != "2026-09-14"
        assert analyze.VERIFY_VERSION == stats["version"]

    def test_weeek_не_заводит_такую_задачу_сам(self, monkeypatch):
        out = self._verified(monkeypatch)
        # Даже если исполнитель восстановлен из обращения в реплике.
        out["tasks"][0]["owner"] = "Кирилл Бубнов"
        members = [{"id": "u-k", "firstName": "Кирилл", "lastName": "Бубнов"}]
        cfg = {"weeek_tasks_only_grounded": True}
        d = wt.prepare("j", out, cfg, date(2026, 10, 30), members)[0]
        assert d["owner_match"] == "exact"
        assert d["grounded"] is False and d["support"] == "mechanical"
        assert d["quote"]                                  # основание видно человеку
        assert wt.default_selected(d, cfg) is False

    def test_word_не_пишет_подтверждено(self):
        from docx import Document
        from docx.shared import RGBColor
        from app.docx_export import _apply_verification
        doc = Document()
        par = doc.add_paragraph("Обновить сервер")
        _apply_verification(par, {"ok": True, "quote": "надо обновить сервер",
                                  "t": "04:46", "match": "approx",
                                  "support": "mechanical"}, RGBColor)
        txt = par.text
        assert "подобрано по словам" in txt and "Основание" not in txt


# --------------------------------------------------------------------------- #
# 6. Срок Weeek и день встречи — не из номера версии
# --------------------------------------------------------------------------- #
class TestСрокНеИзВерсии:
    BASE = date(2026, 10, 30)

    def test_номер_версии_в_задаче_не_срок(self):
        a = {"tasks": [{"task": "Провести ревью 2.4.15", "owner": "—"},
                       {"task": "Обсудить итоги 15 октября", "owner": "—"}],
             "verification": {}}
        d1, d2 = wt.prepare("j", a, {}, self.BASE, [])
        assert d1["due"] is None and d2["due"] is None

    def test_явный_срок_в_задаче_и_в_поле_due_работают(self):
        a = {"tasks": [{"task": "Выпустить 2.4.15 до пятницы", "owner": "—"},
                       {"task": "Сдать отчёт к 05.11", "owner": "—"},
                       {"task": "Релиз 2.4.15", "owner": "—", "due": "к 5 ноября"}],
             "verification": {}}
        d1, d2, d3 = wt.prepare("j", a, {}, self.BASE, [])
        assert d1["due"] == "2026-11-06"
        assert d2["due"] == "2026-11-05"
        assert d3["due"] == "2026-11-05"

    def test_день_встречи_только_из_начала_имени_файла(self):
        ts = datetime(2026, 10, 30, 12, 0).timestamp()
        assert wt.meeting_date_of("Ревью 2.4.15", ts) == date(2026, 10, 30)
        assert wt.meeting_date_of("Подготовка 05.11.2026", ts) == date(2026, 10, 30)
        assert wt.meeting_date_of("20.10.2026, 11:30. - Ревью 2.4.15.mp4", ts) == date(2026, 10, 20)

    def test_черновики_берут_день_из_имени_файла(self, monkeypatch):
        from app.jobs import Job, store
        job = Job(id="p1due01", filename="20.10.2026, 11:30. - Ревью 2.4.15.mp4",
                  audio_path="", language="ru", diarize=False, owner="alice",
                  analyze=True, status="done", context_hint="Ревью 2.4.15",
                  created_at=datetime(2026, 10, 30, 12, 0).timestamp())
        job.analysis = {"tasks": [{"task": "Проверить", "owner": "—", "due": "завтра"}],
                        "verification": {}}
        monkeypatch.setattr("app.automation.settings.load",
                            lambda u: {"weeek_tasks_enabled": True})
        drafts = store.prepare_weeek_tasks(job)
        assert drafts[0]["due"] == "2026-10-21"
