import { useEffect, useState } from "react";
import { Brain, Plus, Trash2, Save, Globe, FolderKanban, AlertTriangle, Repeat, Eraser } from "lucide-react";
import { Page } from "../components/Layout";
import { Card, useToast } from "../components/ui";
import { api } from "../lib/api";
import { setUnsaved } from "../lib/unsaved";

type Project = { name: string; text: string };
const snap = (glob: string, projects: Project[]) => JSON.stringify({ glob, projects });

// Карточка повторяющейся встречи (серии): проект, участники и роли, цели,
// глоссарий — пишет человек; тип протокола и приоритет закрепляются здесь же.
type Series = {
  key: string; title: string; context: string; preset: string; priority: string;
  weeek_project_id: string | null; last_date: string; last_summary: string;
  open_tasks: number; meetings: number;
};
const PRIORITY_LABELS: Record<string, string> = {
  normal: "обычный", urgent: "срочный протокол", record_only: "только запись, без протокола",
};

function SeriesCard({ s, presets, onChanged, onDeleted }:
  { s: Series; presets: { value: string; label: string }[]; onChanged: (s: any) => void; onDeleted: () => void }) {
  const [d, setD] = useState<Series>(s);
  const [saving, setSaving] = useState(false);
  const toast = useToast();
  const dirty = JSON.stringify(d) !== JSON.stringify(s);
  useEffect(() => { setD(s); }, [s]);
  const id = (f: string) => `series-${s.key.replace(/\W+/g, "-")}-${f}`;
  async function save() {
    setSaving(true);
    try {
      const r = await api.post("/api/series", { key: s.key, title: d.title, context: d.context,
        preset: d.preset, priority: d.priority, weeek_project_id: d.weeek_project_id ?? "" });
      toast("Карточка серии сохранена"); onChanged(r);
    } catch (e: any) { toast(e.message, true); } finally { setSaving(false); }
  }
  async function forget() {
    if (!confirm("Стереть память о прошлой встрече этой серии? Карточка останется.")) return;
    try { await api.post(`/api/series/${encodeURIComponent(s.key)}/forget-last`); toast("Память о прошлой встрече стёрта"); onChanged(null); }
    catch (e: any) { toast(e.message, true); }
  }
  async function del() {
    if (!confirm(`Удалить карточку серии «${s.title}»?`)) return;
    try { await api.del(`/api/series/${encodeURIComponent(s.key)}`); toast("Серия удалена"); onDeleted(); }
    catch (e: any) { toast(e.message, true); }
  }
  return (
    <div className="glass2 rounded-2xl p-3.5 mb-2.5">
      <div className="flex items-center gap-2.5 mb-2 flex-wrap">
        <input className="field" style={{ margin: 0, flex: 1, minWidth: 220 }} value={d.title} aria-label="Название серии"
          onChange={(e) => setD({ ...d, title: e.target.value })} />
        <span className="text-[11.5px]" style={{ color: "var(--muted)" }}>
          {s.meetings ? `встреч: ${s.meetings}` : "ещё не было протоколов"}
          {s.last_date ? ` · последняя ${s.last_date}` : ""}
          {s.open_tasks ? ` · открытых задач: ${s.open_tasks}` : ""}</span>
        <button className="btn btn-danger flex-none" onClick={del} aria-label={`Удалить серию ${s.title}`}><Trash2 size={14} /></button>
      </div>
      <label className="lbl" htmlFor={id("ctx")}>Контекст серии: проект и суть, участники и роли, цели встречи, термины</label>
      <textarea id={id("ctx")} className="field" rows={6} value={d.context} onChange={(e) => setD({ ...d, context: e.target.value })}
        placeholder={"Проект: … (что это и для кого)\nУчастники: Зоя Р — руководитель; Кирилл Бубнов — фронтенд…\nЦель встречи: …\nТермины: ТЗ — техническое задание; «Обычное дело» — …"} />
      <div className="grid md:grid-cols-3 gap-3 mt-2">
        <div>
          <label className="lbl" htmlFor={id("preset")}>Тип протокола</label>
          <select id={id("preset")} className="field" value={d.preset} onChange={(e) => setD({ ...d, preset: e.target.value })}>
            <option value="">авто — по названию встречи</option>
            {presets.map((p) => <option key={p.value} value={p.value}>{p.label}</option>)}
          </select>
        </div>
        <div>
          <label className="lbl" htmlFor={id("prio")}>Приоритет</label>
          <select id={id("prio")} className="field" value={d.priority} onChange={(e) => setD({ ...d, priority: e.target.value })}>
            {Object.entries(PRIORITY_LABELS).map(([v, l]) => <option key={v} value={v}>{l}</option>)}
          </select>
        </div>
        <div>
          <label className="lbl" htmlFor={id("wp")}>Проект Weeek для задач (id)</label>
          <input id={id("wp")} className="field" value={d.weeek_project_id || ""} placeholder="как в настройках Weeek"
            onChange={(e) => setD({ ...d, weeek_project_id: e.target.value })} />
        </div>
      </div>
      {s.last_summary && (
        <details className="mt-2 text-[12px]" style={{ color: "var(--muted)" }}>
          <summary className="cursor-pointer">Память о прошлой встрече ({s.last_date}) — уходит в следующий протокол</summary>
          <div className="mt-1">{s.last_summary}</div>
          <button className="btn btn-ghost mt-2" onClick={forget}><Eraser size={13} /> Стереть память о прошлой встрече</button>
        </details>
      )}
      <div className="flex justify-end mt-2">
        <button className="btn btn-primary" disabled={saving || !dirty} onClick={save}><Save size={14} /> {saving ? "Сохраняю…" : "Сохранить карточку"}</button>
      </div>
    </div>
  );
}

export default function Context() {
  const [glob, setGlob] = useState("");
  const [projects, setProjects] = useState<Project[]>([]);
  const [saving, setSaving] = useState(false);
  // Snapshot of the last saved state — anything different means unsaved edits.
  const [savedSnap, setSavedSnap] = useState<string | null>(null);
  const [series, setSeries] = useState<Series[]>([]);
  const [presets, setPresets] = useState<{ value: string; label: string }[]>([]);
  const [newSeries, setNewSeries] = useState("");
  const toast = useToast();

  const load = () => api.get("/api/context").then((d) => {
    const g = d.global || "", p = d.projects || [];
    setGlob(g); setProjects(p); setSavedSnap(snap(g, p));
  }).catch(() => {});
  const loadSeries = () => api.get("/api/series").then((d) => setSeries(d.series || [])).catch(() => {});
  useEffect(() => { load(); loadSeries();
    api.get("/api/presets").then((d) => setPresets(d.presets || [])).catch(() => {}); }, []);

  const dirty = savedSnap !== null && snap(glob, projects) !== savedSnap;

  // Tell the sidebar guard, and warn on tab close/reload too.
  useEffect(() => {
    setUnsaved(dirty, "Контекст не сохранён. Уверены, что хотите покинуть страницу? "
      + "Введённые изменения будут потеряны.");
  }, [dirty]);
  useEffect(() => () => setUnsaved(false), []);   // leaving the page clears it
  useEffect(() => {
    if (!dirty) return;
    const h = (e: BeforeUnloadEvent) => { e.preventDefault(); e.returnValue = ""; };
    window.addEventListener("beforeunload", h);
    return () => window.removeEventListener("beforeunload", h);
  }, [dirty]);

  const setP = (i: number, k: keyof Project, v: string) =>
    setProjects((ps) => ps.map((p, j) => (j === i ? { ...p, [k]: v } : p)));
  const addP = () => setProjects((ps) => [...ps, { name: "", text: "" }]);
  const delP = (i: number) => setProjects((ps) => ps.filter((_, j) => j !== i));

  async function onSave() {
    setSaving(true);
    try {
      const clean = projects.filter((p) => p.name.trim());
      const d = await api.post("/api/context", { global: glob, projects: clean });
      const g = d.global || "", p = d.projects || [];
      setGlob(g); setProjects(p); setSavedSnap(snap(g, p));   // now clean again
      toast("Контекст сохранён");
    } catch (e: any) { toast(e.message, true); } finally { setSaving(false); }
  }
  async function addSeries() {
    const t = newSeries.trim();
    if (!t) return;
    try { await api.post("/api/series", { key: t, title: t }); setNewSeries(""); loadSeries(); toast("Серия добавлена"); }
    catch (e: any) { toast(e.message, true); }
  }

  return (
    <Page title="Контекст для ИИ" subtitle="Постоянные знания, чтобы протокол не путал роли, названия и суть проектов"
      actions={<>
        {dirty && (
          <span className="chip" style={{ color: "var(--warn)", background: "rgba(251,191,36,.14)" }}>
            <AlertTriangle size={12} /> не сохранено</span>
        )}
        {/* stays enabled if the initial load failed (savedSnap === null) */}
        <button className="btn btn-primary" onClick={onSave}
          disabled={saving || (savedSnap !== null && !dirty)}>
          <Save size={15} /> {saving ? "Сохраняю…" : "Сохранить"}</button>
      </>}>
      <Card className="mb-3.5">
        <div className="flex items-start gap-3">
          <div className="grid place-items-center rounded-xl flex-none" style={{ width: 40, height: 40, background: "rgba(45,212,191,.13)" }}>
            <Brain size={19} color="var(--accent)" /></div>
          <div className="text-[12.5px] leading-relaxed" style={{ color: "var(--muted)" }}>
            Общий блок добавляется в <b style={{ color: "var(--txt)" }}>каждый</b> протокол. Контекст проекта
            подставляется, когда название проекта (целыми словами; псевдонимы через «|») встречается в названии встречи или имени файла.
            Карточка <b style={{ color: "var(--txt)" }}>серии</b> — для повторяющихся встреч Weeek: она читается перед расшифровкой
            вместе с итогами прошлой встречи этой серии. ИИ использует это для понимания — но не выдумывает факты встречи.
          </div>
        </div>
      </Card>

      <Card className="mb-3.5">
        <div className="flex items-center gap-2 mb-3"><Repeat size={17} color="var(--accent)" />
          <h2 className="font-bold text-[15px]">Повторяющиеся встречи (серии)</h2>
          <span className="chip ml-auto" style={{ color: "var(--muted)" }}>контекст + память прошлой встречи</span></div>
        <div className="text-[12px] mb-3" style={{ color: "var(--muted)" }}>
          Серия создаётся сама после первого протокола встречи (по названию задачи Weeek без даты). Заполните карточку:
          кто участвует и в какой роли, что за проект, цели встречи, термины. Здесь же закрепляется тип протокола
          и приоритет («только запись» — протокол не собирается).
        </div>
        {series.length ? series.map((s) => (
          <SeriesCard key={s.key} s={s} presets={presets}
            onChanged={() => loadSeries()} onDeleted={() => loadSeries()} />
        )) : <div className="text-[13px] py-2" style={{ color: "var(--muted)" }}>Серий пока нет — они появятся после первых протоколов, либо добавьте вручную.</div>}
        <div className="flex gap-2 mt-2">
          <input className="field" style={{ margin: 0 }} value={newSeries} onChange={(e) => setNewSeries(e.target.value)}
            placeholder="Название встречи как в Weeek, напр. «Встреча лидеров»" aria-label="Название новой серии"
            onKeyDown={(e) => { if (e.key === "Enter") addSeries(); }} />
          <button className="btn btn-ghost flex-none" onClick={addSeries}><Plus size={15} /> Серия</button>
        </div>
      </Card>

      <Card className="mb-3.5">
        <div className="flex items-center gap-2 mb-2"><Globe size={17} color="var(--accent)" />
          <h2 className="font-bold text-[15px]">Общий контекст</h2>
          <span className="chip ml-auto" style={{ color: "var(--muted)" }}>всегда включён</span></div>
        <label className="sr-only" htmlFor="ctx-global">Общий контекст</label>
        <textarea id="ctx-global" className="field" rows={7} value={glob} onChange={(e) => setGlob(e.target.value)}
          placeholder="Кто есть кто, роли, термины, устойчивые сокращения…&#10;Напр.: Иванова А. — директор; «Аврора» — проект перехода на новую CRM." />
        <div className="text-[11.5px] mt-1 text-right" style={{ color: "var(--muted)" }}>{glob.length} / 20000</div>
      </Card>

      <Card>
        <div className="flex items-center gap-2 mb-3"><FolderKanban size={17} color="var(--accent)" />
          <h2 className="font-bold text-[15px]">Контекст по проектам</h2>
          <button className="btn btn-ghost ml-auto" onClick={addP}><Plus size={15} /> Проект</button></div>
        {projects.length ? projects.map((p, i) => (
          <div key={i} className="glass2 rounded-2xl p-3.5 mb-2.5">
            <div className="flex items-center gap-2.5 mb-2">
              <input className="field" style={{ margin: 0 }} value={p.name} onChange={(e) => setP(i, "name", e.target.value)}
                aria-label="Название проекта"
                placeholder="Название проекта как в названии встречи; псевдонимы через | (напр. «ОД | Обычное дело»)" />
              <button className="btn btn-danger flex-none" onClick={() => delP(i)} aria-label="Удалить проект"><Trash2 size={14} /></button>
            </div>
            <textarea className="field" rows={4} value={p.text} onChange={(e) => setP(i, "text", e.target.value)}
              aria-label="Контекст проекта" placeholder="Суть проекта, участники, роли, цели…" />
            <div className="text-[11.5px] mt-1 text-right" style={{ color: "var(--muted)" }}>{p.text.length} / 10000</div>
          </div>
        )) : <div className="text-[13px] py-2" style={{ color: "var(--muted)" }}>Пока нет проектов. Добавьте, чтобы уточнять контекст точечно.</div>}
      </Card>
    </Page>
  );
}
