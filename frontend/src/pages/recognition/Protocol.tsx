// Протокол: показ, редактор и вопрос по встрече. Вынесено из Recognition.tsx.
import { useState } from "react";
import { ChevronRight, FileText, Loader2, RotateCcw, Sparkles } from "lucide-react";
import { Card, useToast } from "../../components/ui";
import { api } from "../../lib/api";

export function List({ title, items, verify }: { title: string; items?: any[]; verify?: any[] }) {
  if (!items?.length) return null;
  return (
    <div className="mb-4">
      <div className="font-bold text-[13.5px] mb-1.5" style={{ color: "var(--accent)" }}>{title}</div>
      <ul className="space-y-1.5">
        {items.map((it, i) => {
          const v = verify?.[i];                       // Д5: grounding info (optional)
          const unverified = v && !v.ok;
          return (
            <li key={i} className="text-[13px] leading-relaxed flex gap-2"
              style={unverified ? { color: "var(--muted)", opacity: 0.85 } : undefined}>
              <ChevronRight size={14} className="flex-none mt-0.5" color="var(--muted)" />
              <span className="min-w-0">
                {typeof it === "string" ? it : `${it.task}${it.owner && it.owner !== "—" ? ` — ${it.owner}` : ""}`}
                {unverified && (
                  <span className="ml-1.5 chip whitespace-nowrap text-[10.5px]"
                    style={{ color: "var(--warn)", background: "rgba(251,191,36,.12)" }}
                    title="В расшифровке не нашлось дословного подтверждения — проверьте пункт">
                    ⚠ проверьте</span>
                )}
                {v?.ok && v.quote && (
                  <details className="mt-0.5">
                    <summary className="text-[11px] cursor-pointer" style={{ color: "var(--muted)" }}>
                      основание{v.t ? ` · ${v.t}` : ""}{v.source === "notes" ? " · из заметок" : ""}</summary>
                    <div className="text-[11.5px] italic mt-0.5 pl-2" style={{ color: "var(--muted)", borderLeft: "2px solid var(--line)" }}>
                      «{v.quote}»</div>
                  </details>
                )}
              </span>
            </li>
          );
        })}
      </ul>
    </div>
  );
}

// Д12: render the transcript from JSON segments, greying-out and underlining
// the spots Whisper itself decoded with low confidence — that's exactly where
// names and numbers need a human glance.

export const taskLine = (t: any) => `${t.task}${t.owner ? ` — ${t.owner}` : ""}`;
export const parseTasks = (s: string) => s.split("\n").map((l) => l.trim()).filter(Boolean)
  .map((l) => { const i = l.lastIndexOf(" — "); return i > 0
    ? { task: l.slice(0, i).trim(), owner: l.slice(i + 3).trim() }
    : { task: l, owner: "" }; });
export const parseLines = (s: string) => s.split("\n").map((l) => l.trim()).filter(Boolean);

export function ProtocolEditor({ a, jobId, onSaved, onCancel, toast }:
  { a: any; jobId: string; onSaved: (a: any) => void; onCancel: () => void; toast: any }) {
  const [d, setD] = useState<any>(() => ({
    summary: a.summary || "",
    detailed: (a.detailed || []).map((t: any) => ({ topic: t.topic || "", details: t.details || "" })),
    decisions: (a.decisions || []).join("\n"),
    tasks: (a.tasks || []).map(taskLine).join("\n"),
    minor_tasks: (a.minor_tasks || []).map(taskLine).join("\n"),
    done_tasks: (a.done_tasks || []).map(taskLine).join("\n"),
  }));
  const [busy, setBusy] = useState(false);
  const [regen, setRegen] = useState<number | null>(null);
  async function save() {
    setBusy(true);
    try {
      const r = await api.patch(`/api/jobs/${jobId}/analysis`, { analysis: {
        summary: d.summary, detailed: d.detailed,
        decisions: parseLines(d.decisions),
        tasks: parseTasks(d.tasks), minor_tasks: parseTasks(d.minor_tasks),
        done_tasks: parseTasks(d.done_tasks) } });
      toast("Правки сохранены, Word-протокол пересобран");
      onSaved(r.analysis);
    } catch (e: any) { toast(e.message, true); }
    finally { setBusy(false); }
  }
  async function regenTopic(i: number) {
    setRegen(i);
    try {
      const r = await api.post(`/api/jobs/${jobId}/regen-topic`, { index: i });
      setD({ ...d, detailed: (r.analysis.detailed || []).map((t: any) => ({ topic: t.topic || "", details: t.details || "" })) });
      toast("Раздел перегенерирован");
    } catch (e: any) { toast(e.message, true); }
    finally { setRegen(null); }
  }
  const lbl = (s: string) => <label className="lbl mt-3">{s}</label>;
  return (
    <div>
      {lbl("Кратко")}
      <textarea className="field" rows={3} value={d.summary} onChange={(e) => setD({ ...d, summary: e.target.value })} />
      {lbl("Темы")}
      {d.detailed.map((t: any, i: number) => (
        <div key={i} className="glass2 rounded-xl p-2.5 mb-2">
          <div className="flex gap-2 items-center mb-1.5">
            <input className="field" value={t.topic}
              onChange={(e) => { const dd = [...d.detailed]; dd[i] = { ...t, topic: e.target.value }; setD({ ...d, detailed: dd }); }} />
            <button className="btn btn-ghost flex-none" disabled={regen !== null}
              title="Перегенерировать этот раздел нейросетью (остальное не трогается)"
              onClick={() => regenTopic(i)}>
              {regen === i ? <Loader2 size={14} className="animate-spin" /> : <RotateCcw size={14} />}</button>
          </div>
          <textarea className="field" rows={4} value={t.details}
            onChange={(e) => { const dd = [...d.detailed]; dd[i] = { ...t, details: e.target.value }; setD({ ...d, detailed: dd }); }} />
        </div>
      ))}
      {lbl("Решения (по строке)")}
      <textarea className="field" rows={3} value={d.decisions} onChange={(e) => setD({ ...d, decisions: e.target.value })} />
      {lbl("Задачи (строка: «задача — ответственный»)")}
      <textarea className="field" rows={4} value={d.tasks} onChange={(e) => setD({ ...d, tasks: e.target.value })} />
      {lbl("Мелкие задачи")}
      <textarea className="field" rows={3} value={d.minor_tasks} onChange={(e) => setD({ ...d, minor_tasks: e.target.value })} />
      {lbl("Уже сделано")}
      <textarea className="field" rows={2} value={d.done_tasks} onChange={(e) => setD({ ...d, done_tasks: e.target.value })} />
      <div className="flex gap-2 justify-end mt-3">
        <button className="btn btn-ghost" onClick={onCancel}>Отмена</button>
        <button className="btn btn-primary" disabled={busy} onClick={save}>
          {busy ? <Loader2 size={14} className="animate-spin" /> : <FileText size={14} />} Сохранить правки</button>
      </div>
    </div>
  );
}

// Д13: чат по встрече — вопрос → ответ с таймкодами из расшифровки.
export function AskBlock({ jobId }: { jobId: string }) {
  const [q, setQ] = useState("");
  const [answer, setAnswer] = useState("");
  const [busy, setBusy] = useState(false);
  const toast = useToast();
  async function ask() {
    if (q.trim().length < 3 || busy) return;
    setBusy(true); setAnswer("");
    try { const r = await api.post(`/api/jobs/${jobId}/ask`, { question: q.trim() }); setAnswer(r.answer || ""); }
    catch (e: any) { toast(e.message, true); }
    finally { setBusy(false); }
  }
  return (
    <div className="glass2 rounded-2xl p-3 mt-4">
      <div className="text-[12.5px] font-semibold mb-2">💬 Спросить по встрече</div>
      <div className="flex gap-2">
        <input className="field" placeholder="например: что решили по тегам?"
          value={q} onChange={(e) => setQ(e.target.value)}
          onKeyDown={(e) => { if (e.key === "Enter") ask(); }} />
        <button className="btn btn-primary flex-none" disabled={busy} onClick={ask}>
          {busy ? <Loader2 size={14} className="animate-spin" /> : "Спросить"}</button>
      </div>
      {answer && (
        <div className="text-[12.5px] leading-relaxed mt-2.5 whitespace-pre-wrap">{answer}</div>
      )}
    </div>
  );
}

export function Protocol({ a }: { a: any }) {
  if (!a) return null;
  return (
    <div>
      {a._warning && (
        <div className="glass2 rounded-2xl p-3 mb-4 text-[12.5px]" style={{ color: "var(--warn)" }}>
          ⚠ {a._warning}
        </div>
      )}
      {a.participants?.length ? (
        <div className="mb-4">
          <div className="font-bold text-[13.5px] mb-1.5" style={{ color: "var(--accent)" }}>Участники</div>
          <div className="flex flex-wrap gap-1.5">
            {a.participants.map((p: any, i: number) => (
              <span key={i} className="chip" style={{ color: "var(--txt)" }}>{p.name}{p.role ? ` · ${p.role}` : ""}</span>
            ))}
          </div>
        </div>
      ) : null}
      {a.summary && (<div className="mb-4"><div className="font-bold text-[13.5px] mb-1.5" style={{ color: "var(--accent)" }}>Кратко</div>
        <div className="text-[13px] leading-relaxed">{a.summary}</div></div>)}
      {a.detailed?.length ? (
        <div className="mb-4"><div className="font-bold text-[13.5px] mb-1.5" style={{ color: "var(--accent)" }}>По темам</div>
          {a.detailed.map((t: any, i: number) => (
            <div key={i} className="glass2 rounded-xl p-3 mb-2">
              <div className="font-semibold text-[13px] mb-1">{t.topic}</div>
              <div className="text-[12.5px] leading-relaxed" style={{ color: "var(--muted)" }}>{t.details}</div>
            </div>
          ))}
        </div>
      ) : null}
      <List title="Ключевые мысли" items={a.key_thoughts} />
      <List title="Выводы" items={a.conclusions} />
      <List title="Решения" items={a.decisions} verify={a.verification?.decisions} />
      <List title="Уже сделано" items={a.done_tasks} verify={a.verification?.done_tasks} />
      <List title="Задачи" items={a.tasks} verify={a.verification?.tasks} />
      <List title="Мелкие задачи" items={a.minor_tasks} verify={a.verification?.minor_tasks} />
    </div>
  );
}

