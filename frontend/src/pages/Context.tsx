import { useEffect, useState } from "react";
import { Brain, Plus, Trash2, Save, Globe, FolderKanban, AlertTriangle } from "lucide-react";
import { Page } from "../components/Layout";
import { Card, useToast } from "../components/ui";
import { api } from "../lib/api";
import { setUnsaved } from "../lib/unsaved";

type Project = { name: string; text: string };
const snap = (glob: string, projects: Project[]) => JSON.stringify({ glob, projects });

export default function Context() {
  const [glob, setGlob] = useState("");
  const [projects, setProjects] = useState<Project[]>([]);
  const [saving, setSaving] = useState(false);
  // Snapshot of the last saved state — anything different means unsaved edits.
  const [savedSnap, setSavedSnap] = useState<string | null>(null);
  const toast = useToast();

  const load = () => api.get("/api/context").then((d) => {
    const g = d.global || "", p = d.projects || [];
    setGlob(g); setProjects(p); setSavedSnap(snap(g, p));
  }).catch(() => {});
  useEffect(() => { load(); }, []);

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
            подставляется автоматически, когда название проекта встречается в теме встречи или имени файла.
            ИИ использует это для понимания — но не выдумывает факты встречи.
          </div>
        </div>
      </Card>

      <Card className="mb-3.5">
        <div className="flex items-center gap-2 mb-2"><Globe size={17} color="var(--accent)" />
          <div className="font-bold text-[15px]">Общий контекст</div>
          <span className="chip ml-auto" style={{ color: "var(--muted)" }}>всегда включён</span></div>
        <textarea className="field" rows={7} value={glob} onChange={(e) => setGlob(e.target.value)}
          placeholder="Кто есть кто, роли, термины, устойчивые сокращения…&#10;Напр.: Иванова А. — директор; «Аврора» — проект перехода на новую CRM." />
        <div className="text-[11.5px] mt-1 text-right" style={{ color: "var(--muted)" }}>{glob.length} / 20000</div>
      </Card>

      <Card>
        <div className="flex items-center gap-2 mb-3"><FolderKanban size={17} color="var(--accent)" />
          <div className="font-bold text-[15px]">Контекст по проектам</div>
          <button className="btn btn-ghost ml-auto" onClick={addP}><Plus size={15} /> Проект</button></div>
        {projects.length ? projects.map((p, i) => (
          <div key={i} className="glass2 rounded-2xl p-3.5 mb-2.5">
            <div className="flex items-center gap-2.5 mb-2">
              <input className="field" style={{ margin: 0 }} value={p.name} onChange={(e) => setP(i, "name", e.target.value)}
                placeholder="Название проекта (как в теме встречи)" />
              <button className="btn btn-danger flex-none" onClick={() => delP(i)}><Trash2 size={14} /></button>
            </div>
            <textarea className="field" rows={4} value={p.text} onChange={(e) => setP(i, "text", e.target.value)}
              placeholder="Суть проекта, участники, роли, цели…" />
          </div>
        )) : <div className="text-[13px] py-2" style={{ color: "var(--muted)" }}>Пока нет проектов. Добавьте, чтобы уточнять контекст точечно.</div>}
      </Card>
    </Page>
  );
}
