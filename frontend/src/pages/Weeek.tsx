import { useState } from "react";
import { Link2, RefreshCw, Trash2, FolderTree, CheckCircle2 } from "lucide-react";
import { Page } from "../components/Layout";
import { Card, Select, useToast } from "../components/ui";
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
