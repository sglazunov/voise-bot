import { useState } from "react";
import { Link2, RefreshCw, Trash2, FolderTree, CheckCircle2, ListChecks } from "lucide-react";
import { Page } from "../components/Layout";
import { Card, Select, Switch, useToast } from "../components/ui";
import { useSettings } from "../lib/useSettings";
import { api } from "../lib/api";
import { TZ_RU, TZ_CIS, otherZones, tzLabel } from "../lib/timezones";

export default function Weeek() {
  const { s, set, save, reload , ready } = useSettings();
  const [token, setToken] = useState("");
  const [projects, setProjects] = useState<any[] | null>(null);
  const [probeId, setProbeId] = useState("");
  const [probeRes, setProbeRes] = useState<any>(null);
  const [probing, setProbing] = useState(false);
  const toast = useToast();
  async function probe() {
    setProbing(true); setProbeRes(null);
    try { setProbeRes(await api.get(`/api/automation/weeek/probe?task_id=${encodeURIComponent(probeId.trim())}`)); }
    catch (e: any) { toast(e.message, true); }
    finally { setProbing(false); }
  }
  const connected = !!s.weeek_token;

  async function onSave() {
    try {
      // Пустая строка = «очистить»: null бэкенд игнорирует как «не меняли».
      const patch: any = { weeek_project_id: s.weeek_project_id ?? "", timezone: s.timezone ?? "" };
      if (token.trim()) patch.weeek_token = token.trim();
      await save(patch); setToken(""); toast("Weeek сохранён"); reload();
    } catch (e: any) { toast(e.message, true); }
  }
  async function loadProjects() {
    try { const d = await api.get("/api/automation/weeek/projects"); setProjects(d.projects || d || []); }
    catch (e: any) { toast(e.message, true); }
  }
  async function resetToken() {
    if (!confirm("Сбросить токен Weeek?")) return;
    try { await save({ weeek_token: "" }); toast("Токен сброшен"); reload(); } catch (e: any) { toast(e.message, true); }
  }
  // Задачи из протокола → Weeek: черновики с подтверждением человеком.
  const [boards, setBoards] = useState<any[] | null>(null);
  const [columns, setColumns] = useState<any[] | null>(null);
  const [members, setMembers] = useState<any[] | null>(null);
  async function saveTasks() {
    try {
      await save({
        weeek_tasks_enabled: !!s.weeek_tasks_enabled, weeek_tasks_auto: !!s.weeek_tasks_auto,
        weeek_tasks_only_grounded: !!s.weeek_tasks_only_grounded, weeek_tasks_include_minor: !!s.weeek_tasks_include_minor,
        weeek_tasks_project_id: s.weeek_tasks_project_id ?? "", weeek_tasks_board_id: s.weeek_tasks_board_id ?? "",
        weeek_tasks_column_id: s.weeek_tasks_column_id ?? "",
        weeek_tasks_default_due_days: s.weeek_tasks_default_due_days === "" || s.weeek_tasks_default_due_days == null ? null : Number(s.weeek_tasks_default_due_days),
        weeek_user_map: s.weeek_user_map || {},
      });
      toast("Настройки задач сохранены"); reload();
    } catch (e: any) { toast(e.message, true); }
  }
  async function loadBoards() {
    const pid = String(s.weeek_tasks_project_id || s.weeek_project_id || "").trim();
    if (!pid) { toast("Сначала укажите id проекта", true); return; }
    try { const d = await api.get(`/api/automation/weeek/boards?project_id=${encodeURIComponent(pid)}`); setBoards(d.boards || []); }
    catch (e: any) { toast(e.message, true); }
  }
  async function loadColumns() {
    const bid = String(s.weeek_tasks_board_id || "").trim();
    if (!bid) { toast("Сначала выберите доску", true); return; }
    try { const d = await api.get(`/api/automation/weeek/board-columns?board_id=${encodeURIComponent(bid)}`); setColumns(d.columns || []); }
    catch (e: any) { toast(e.message, true); }
  }
  async function loadMembers() {
    try { const d = await api.get("/api/automation/weeek/members?refresh=1"); setMembers(d.members || []); toast(`Участников: ${(d.members || []).length}`); }
    catch (e: any) { toast(e.message, true); }
  }
  const userMap: Record<string, string> = s.weeek_user_map || {};

  return (
    <Page title="Weeek" subtitle="Источник задач со встречами и ссылками на Телемост">
      <div className="grid lg:grid-cols-2 gap-3.5">
        <Card>
          <div className="flex items-center gap-3 mb-3">
            <div className="grid place-items-center rounded-xl" style={{ width: 40, height: 40, background: "rgba(45,212,191,.13)" }}>
              <Link2 size={19} color="var(--accent)" /></div>
            <div><div className="font-bold text-[15px]">Подключение к Weeek</div>
              <div className="text-[12px]" style={{ color: "var(--muted)" }}>Откуда система берёт задачи со встречами</div></div>
            {connected && <span className="chip ml-auto" style={{ color: "#5eead4", background: "rgba(52,211,153,.16)" }}>
              <CheckCircle2 size={12} /> подключено</span>}
          </div>
          <label className="lbl">API-токен Weeek</label>
          <input className="field" type="password" value={token} onChange={(e) => setToken(e.target.value)}
            placeholder={connected ? "•••••••• сохранён (оставьте пустым)" : "вставьте токен"} />
          <div className="text-[12px] mt-1.5" style={{ color: "var(--muted)" }}>Персональный ключ из настроек воркспейса. Хранится локально на сервере.</div>
          <div className="grid grid-cols-2 gap-3 mt-3">
            <div><label className="lbl">ID проекта</label>
              <input className="field" value={s.weeek_project_id || ""} onChange={(e) => set("weeek_project_id", e.target.value)} placeholder="пусто = все" /></div>
            <div><label className="lbl">Часовой пояс</label>
              <Select value={s.timezone || "Europe/Moscow"} onChange={(v) => set("timezone", v)}
                options={[
                  { label: "Россия", options: TZ_RU.map((z) => ({ value: z, label: tzLabel(z) })) },
                  { label: "СНГ", options: TZ_CIS.map((z) => ({ value: z, label: tzLabel(z) })) },
                  ...(otherZones().length ? [{ label: "Все зоны", options: otherZones().map((z) => ({ value: z, label: tzLabel(z) })) }] : []),
                ]} /></div>
          </div>
          <div className="text-[12px] mt-1.5" style={{ color: "var(--muted)" }}>Пусто = все проекты</div>
          <div className="flex gap-2.5 mt-3 flex-wrap">
            <button className="btn btn-primary" onClick={onSave} disabled={!ready}>Сохранить</button>
            <button className="btn btn-ghost" onClick={loadProjects}><RefreshCw size={15} /> Мои проекты</button>
            <button className="btn btn-danger" onClick={resetToken}><Trash2 size={14} /> Сбросить токен</button>
          </div>
        </Card>

        {/* Диагностика: показать сырой ответ Weeek по одной задаче. Маршрут был,
            но обратиться к нему можно было только curl'ом. Нужен, когда встреча
            «не подхватилась»: сразу видно, как называются поля даты и ссылки. */}
        <Card>
          <div className="flex items-center gap-2 mb-3"><FolderTree size={17} color="var(--accent)" />
            <div className="font-bold text-[15px]">Диагностика задачи</div></div>
          <div className="text-[12px] mb-2" style={{ color: "var(--muted)" }}>
            Если встреча не попала в план — посмотрите, что по ней отдаёт Weeek:
            как называются поля с датой, ссылкой и галочкой записи.
          </div>
          <div className="flex gap-2.5 flex-wrap">
            <input className="field flex-1" value={probeId} onChange={(e) => setProbeId(e.target.value)}
              placeholder="ID задачи в Weeek" />
            <button className="btn btn-ghost" onClick={probe} disabled={!probeId.trim() || probing}>
              {probing ? "Смотрю…" : "Показать"}</button>
          </div>
          {probeRes && (
            <pre className="glass2 rounded-2xl p-3 mt-3 text-[11.5px] whitespace-pre-wrap"
              style={{ maxHeight: 320, overflow: "auto" }}>{JSON.stringify(probeRes, null, 2)}</pre>
          )}
        </Card>

        <Card className="lg:col-span-2">
          <div className="flex items-center gap-2 mb-3"><ListChecks size={17} color="var(--accent)" />
            <div className="font-bold text-[15px]">Задачи из протокола → Weeek</div>
            <span className="chip ml-auto" style={{ color: "var(--muted)" }}>черновики с подтверждением</span></div>
          <div className="text-[12px] mb-3" style={{ color: "var(--muted)" }}>
            После сборки протокола бот готовит черновики задач: формулировка, исполнитель из воркспейса, срок из речи
            («до пятницы» → дата), основание-цитата. На странице протокола вы отмечаете нужные и жмёте «Создать в Weeek».
            Формы запросов к Weeek сверены с документацией, но перед первым использованием проверьте одну задачу в тестовом проекте.
          </div>
          <div className="grid md:grid-cols-2 gap-x-6 gap-y-2">
            {[["weeek_tasks_enabled", "Готовить черновики задач"],
              ["weeek_tasks_only_grounded", "По умолчанию отмечать только задачи с дословным основанием"],
              ["weeek_tasks_include_minor", "Брать и мелкие задачи"],
              ["weeek_tasks_auto", "Создавать автоматически без подтверждения (только с основанием и однозначным исполнителем)"]].map(([k, l]) => (
              <div key={k} className="flex items-center gap-3 py-1">
                <Switch on={!!s[k]} onChange={() => set(k, !s[k])} />
                <span className="text-[13px]">{l}</span>
              </div>
            ))}
          </div>
          <div className="grid md:grid-cols-4 gap-3 mt-3">
            <div><label className="lbl" htmlFor="wt-project">Проект (id)</label>
              <input id="wt-project" className="field" value={s.weeek_tasks_project_id || ""} placeholder={s.weeek_project_id ? `как встречи: ${s.weeek_project_id}` : "id проекта"}
                onChange={(e) => set("weeek_tasks_project_id", e.target.value)} /></div>
            <div><label className="lbl" htmlFor="wt-board">Доска (id)</label>
              <div className="flex gap-1">
                <input id="wt-board" className="field" value={s.weeek_tasks_board_id || ""} onChange={(e) => set("weeek_tasks_board_id", e.target.value)} />
                <button className="btn btn-ghost flex-none" onClick={loadBoards} aria-label="Загрузить доски"><RefreshCw size={14} /></button></div>
              {boards && <select className="field mt-1" aria-label="Выбрать доску" value={s.weeek_tasks_board_id || ""} onChange={(e) => set("weeek_tasks_board_id", e.target.value)}>
                <option value="">— доска</option>{boards.map((b: any) => <option key={b.id} value={b.id}>{b.name} (#{b.id})</option>)}</select>}</div>
            <div><label className="lbl" htmlFor="wt-col">Колонка (id)</label>
              <div className="flex gap-1">
                <input id="wt-col" className="field" value={s.weeek_tasks_column_id || ""} onChange={(e) => set("weeek_tasks_column_id", e.target.value)} />
                <button className="btn btn-ghost flex-none" onClick={loadColumns} aria-label="Загрузить колонки"><RefreshCw size={14} /></button></div>
              {columns && <select className="field mt-1" aria-label="Выбрать колонку" value={s.weeek_tasks_column_id || ""} onChange={(e) => set("weeek_tasks_column_id", e.target.value)}>
                <option value="">— колонка</option>{columns.map((c: any) => <option key={c.id} value={c.id}>{c.name} (#{c.id})</option>)}</select>}</div>
            <div><label className="lbl" htmlFor="wt-due">Срок по умолчанию, дней</label>
              <input id="wt-due" className="field" type="number" min={0} value={s.weeek_tasks_default_due_days ?? ""} placeholder="без срока"
                onChange={(e) => set("weeek_tasks_default_due_days", e.target.value)} /></div>
          </div>
          <div className="mt-3">
            <div className="flex items-center gap-2 flex-wrap">
              <span className="text-[12.5px] font-semibold">Соответствие имён: протокол → участник Weeek</span>
              <button className="btn btn-ghost" onClick={loadMembers}><RefreshCw size={14} /> Участники Weeek{members ? ` (${members.length})` : ""}</button>
            </div>
            <div className="text-[11.5px] mt-1" style={{ color: "var(--muted)" }}>
              Пополняется само, когда вы выбираете исполнителя на странице протокола. Здесь можно убрать лишнее.</div>
            {Object.keys(userMap).length ? (
              <ul className="mt-2 space-y-1">
                {Object.entries(userMap).map(([name, uid]) => (
                  <li key={name} className="flex items-center gap-2 text-[12.5px]">
                    <span>{name}</span><span style={{ color: "var(--muted)" }}>→ {(members || []).find((m: any) => m.id === uid)?.name || uid}</span>
                    <button className="btn-ghost" aria-label={`Убрать соответствие для ${name}`}
                      onClick={() => { const m = { ...userMap }; delete m[name]; set("weeek_user_map", m); }}><Trash2 size={13} /></button>
                  </li>
                ))}
              </ul>
            ) : <div className="text-[12px] mt-1" style={{ color: "var(--muted)" }}>Пока пусто.</div>}
          </div>
          <div className="flex gap-2.5 mt-3">
            <button className="btn btn-primary" onClick={saveTasks} disabled={!ready}>Сохранить настройки задач</button>
          </div>
        </Card>

        <Card>
          <div className="flex items-center gap-2 mb-3"><FolderTree size={17} color="var(--accent)" /><div className="font-bold text-[15px]">Мои проекты</div></div>
          {projects === null ? <div className="text-[13px]" style={{ color: "var(--muted)" }}>Нажмите «Мои проекты», чтобы загрузить список.</div>
            : projects.length ? projects.map((p: any) => (
              <div key={p.id} className="glass2 rounded-2xl px-4 py-3 mb-2 flex items-center justify-between">
                <div><div className="font-bold text-[14px]">{p.name}</div>
                  <div className="text-[11.5px]" style={{ color: "var(--muted)" }}>ID {p.id}</div></div>
              </div>
            )) : <div className="text-[13px]" style={{ color: "var(--muted)" }}>Проекты не найдены.</div>}
        </Card>
      </div>
    </Page>
  );
}
