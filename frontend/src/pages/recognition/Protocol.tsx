// Протокол: показ, редактор, задачи → Weeek и вопрос по встрече.
//
// Порядок разделов — для руководителя: сначала решения и задачи (ради них
// протокол и открывают), потом «о чём говорили» и подробный разбор. Раньше
// решения стояли после 10–20 карточек тем.
import { useEffect, useState } from "react";
import { ChevronRight, ExternalLink, FileText, Loader2, RotateCcw } from "lucide-react";
import { useToast } from "../../components/ui";
import { api } from "../../lib/api";

type Verify = { ok?: boolean; quote?: string; t?: string; source?: string; match?: string; note?: string; owner_source?: string } | null;

function VerifyMark({ v }: { v?: Verify }) {
  // null — пункт не проверялся (проверка оборвалась): молчим, а не пугаем.
  if (v === null || v === undefined) return null;
  if (!v.ok) {
    return (
      <>
        <span className="ml-1.5 chip whitespace-nowrap text-[10.5px]"
          style={{ color: "var(--warn)", background: "rgba(251,191,36,.12)" }}
          title={v.note || "В расшифровке не нашлось дословного подтверждения — проверьте пункт"}>
          ⚠ {v.note ? v.note : "проверьте"}</span>
        {/* Цитаты нет, но место в разговоре нашлось по словам пункта: это
            ориентир, где слушать, а НЕ подтверждение — так и написано. */}
        {v.t && <span className="ml-1.5 text-[11px]" style={{ color: "var(--muted)" }}>
          без основания · примерно {v.t}</span>}
      </>
    );
  }
  if (!v.quote) return null;
  return (
    <details className="mt-0.5">
      <summary className="text-[11px] cursor-pointer" style={{ color: "var(--muted)" }}>
        {/* Значка «≈» больше нет: «цитата приблизительная» и «основания нет»
            — разные вещи, и различает их слово, а не значок. */}
        основание{v.t ? ` · ${v.t}` : ""}{v.source === "notes" ? " · из заметок" : ""}{v.match === "approx" ? " · цитата приблизительная" : ""}
        {v.note ? ` · ${v.note}` : ""}{v.owner_source === "обращение" ? " · ответственный из обращения" : ""}</summary>
      <div className="text-[11.5px] italic mt-0.5 pl-2" style={{ color: "var(--muted)", borderLeft: "2px solid var(--line)" }}>
        «{v.quote}»</div>
    </details>
  );
}

export function List({ title, items, verify }: { title: string; items?: any[]; verify?: Verify[] }) {
  if (!items?.length) return null;
  return (
    <section className="mb-4" aria-label={title}>
      <h3 className="font-bold text-[13.5px] mb-1.5" style={{ color: "var(--accent-text, var(--accent))" }}>{title}</h3>
      <ul className="space-y-1.5">
        {items.map((it, i) => {
          const v = verify?.[i];
          const unverified = v && !v.ok;
          const text = typeof it === "string" ? it
            : `${it.task}${it.owner && it.owner !== "—" ? ` — ${it.owner}` : ""}${it.due ? ` (срок: ${it.due})` : ""}`;
          return (
            <li key={i} className="text-[13px] leading-relaxed flex gap-2"
              style={unverified ? { color: "var(--muted)", opacity: 0.85 } : undefined}>
              <ChevronRight size={14} className="flex-none mt-0.5" color="var(--muted)" aria-hidden="true" />
              <span className="min-w-0">{text}<VerifyMark v={v} /></span>
            </li>
          );
        })}
      </ul>
    </section>
  );
}

/** «Что спрашивали и что ответили»: документ нашли? баг починен? — ответы
 *  для тех, кого на встрече не было. «без ответа» подсвечивается: это тоже факт. */
export function StatusList({ items }: { items?: any[] }) {
  const rows = (items || []).filter((s) => s && s.item);
  if (!rows.length) return null;
  return (
    <section className="mb-4" aria-label="Что спрашивали и что ответили">
      <h3 className="font-bold text-[13.5px] mb-1.5" style={{ color: "var(--accent-text, var(--accent))" }}>Что спрашивали и что ответили</h3>
      <ul className="space-y-1.5">
        {rows.map((s, i) => {
          const open = s.status === "без ответа";
          return (
            <li key={i} className="text-[13px] leading-relaxed flex gap-2">
              <ChevronRight size={14} className="flex-none mt-0.5" color="var(--muted)" aria-hidden="true" />
              <span className="min-w-0">
                {s.item}
                <span className="chip ml-1.5" style={{ color: open ? "var(--warn)" : "var(--txt)" }}>{s.status || "—"}</span>
                {s.note ? <span style={{ color: "var(--muted)" }}> — {s.note}</span> : null}
              </span>
            </li>
          );
        })}
      </ul>
    </section>
  );
}

/* Оговорки о состоянии системы, сказанные по ходу демо: что временно, отключено,
   скрыто, заглушка. Не решения и не задачи — раньше не попадали никуда, а
   тестировщик узнавал про скрытую вкладку только из сырой расшифровки. */
export function CaveatList({ items }: { items?: any[] }) {
  const rows = (items || []).filter((c) => c && c.item);
  if (!rows.length) return null;
  return (
    <section className="mb-4" aria-label="Оговорки: что временно, отключено или скрыто">
      <h3 className="font-bold text-[13.5px] mb-1.5" style={{ color: "var(--accent-text, var(--accent))" }}>
        Оговорки: что временно, отключено или скрыто</h3>
      <ul className="space-y-1.5">
        {rows.map((c, i) => {
          const hot = c.kind === "риск" || c.kind === "скрыто";
          return (
            <li key={i} className="text-[13px] leading-relaxed flex gap-2">
              <ChevronRight size={14} className="flex-none mt-0.5" color="var(--muted)" aria-hidden="true" />
              <span className="min-w-0">
                {c.item}
                <span className="chip ml-1.5" style={{ color: hot ? "var(--warn)" : "var(--txt)" }}>{c.kind || "—"}</span>
                {c.note ? <span style={{ color: "var(--muted)" }}> — {c.note}</span> : null}
              </span>
            </li>
          );
        })}
      </ul>
    </section>
  );
}

export const taskLine = (t: any) => `${t.task}${t.owner ? ` — ${t.owner}` : ""}`;
// Разделитель «задача — ответственный»: тире с пробелами в КОНЦЕ строки после
// короткого хвоста без точек. «Согласовать бюджет — до пятницы» ответственным
// не станет: «до пятницы» — не имя (есть пробел + предлог).
export const parseTasks = (s: string) => s.split("\n").map((l) => l.trim()).filter(Boolean)
  .map((l) => {
    const m = l.match(/^(.*\S)\s+—\s+([A-Za-zА-Яа-яЁё][\wА-Яа-яЁё.\- ]{0,40})$/);
    if (m && !/^(до|к|на|в|через|по)\s/i.test(m[2])) return { task: m[1].trim(), owner: m[2].trim() };
    return { task: l, owner: "" };
  });
export const parseLines = (s: string) => s.split("\n").map((l) => l.trim()).filter(Boolean);

export function ProtocolEditor({ a, jobId, onSaved, onCancel, toast }:
  { a: any; jobId: string; onSaved: (a: any) => void; onCancel: () => void; toast: any }) {
  const [d, setD] = useState<any>(() => ({
    summary: a.summary || "",
    participants: (a.participants || []).map((p: any) => `${p.name}${p.role ? ` — ${p.role}` : ""}`).join("\n"),
    detailed: (a.detailed || []).map((t: any) => ({ topic: t.topic || "", details: t.details || "" })),
    decisions: (a.decisions || []).join("\n"),
    conclusions: (a.conclusions || []).join("\n"),
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
        participants: parseLines(d.participants).map((l) => { const i = l.indexOf(" — ");
          return i > 0 ? { name: l.slice(0, i).trim(), role: l.slice(i + 3).trim() } : { name: l, role: "" }; }),
        decisions: parseLines(d.decisions), conclusions: parseLines(d.conclusions),
        tasks: parseTasks(d.tasks), minor_tasks: parseTasks(d.minor_tasks),
        done_tasks: parseTasks(d.done_tasks) } });
      toast("Правки сохранены, Word-протокол пересобран. Пометки проверки сняты — правки человека.");
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
  const lbl = (s: string, id: string) => <label className="lbl mt-3" htmlFor={id}>{s}</label>;
  return (
    <div>
      {lbl("Участники (строка: «Имя — роль»)", "ed-parts")}
      <textarea id="ed-parts" className="field" rows={3} value={d.participants} onChange={(e) => setD({ ...d, participants: e.target.value })} />
      {lbl("Кратко", "ed-summary")}
      <textarea id="ed-summary" className="field" rows={3} value={d.summary} onChange={(e) => setD({ ...d, summary: e.target.value })} />
      {lbl("Решения (по строке)", "ed-dec")}
      <textarea id="ed-dec" className="field" rows={3} value={d.decisions} onChange={(e) => setD({ ...d, decisions: e.target.value })} />
      {lbl("Задачи (строка: «задача — ответственный»)", "ed-tasks")}
      <textarea id="ed-tasks" className="field" rows={4} value={d.tasks} onChange={(e) => setD({ ...d, tasks: e.target.value })} />
      {lbl("Мелкие задачи", "ed-minor")}
      <textarea id="ed-minor" className="field" rows={3} value={d.minor_tasks} onChange={(e) => setD({ ...d, minor_tasks: e.target.value })} />
      {lbl("Уже сделано", "ed-done")}
      <textarea id="ed-done" className="field" rows={2} value={d.done_tasks} onChange={(e) => setD({ ...d, done_tasks: e.target.value })} />
      {lbl("Выводы и открытые вопросы (по строке)", "ed-concl")}
      <textarea id="ed-concl" className="field" rows={2} value={d.conclusions} onChange={(e) => setD({ ...d, conclusions: e.target.value })} />
      {lbl("Темы", "ed-topic-0")}
      {d.detailed.map((t: any, i: number) => (
        <div key={i} className="glass2 rounded-xl p-2.5 mb-2">
          <div className="flex gap-2 items-center mb-1.5">
            <input id={`ed-topic-${i}`} className="field" value={t.topic} aria-label={`Название темы ${i + 1}`}
              onChange={(e) => { const dd = [...d.detailed]; dd[i] = { ...t, topic: e.target.value }; setD({ ...d, detailed: dd }); }} />
            <button className="btn btn-ghost flex-none" disabled={regen !== null}
              title="Перегенерировать этот раздел нейросетью (остальное не трогается)"
              aria-label={`Перегенерировать тему ${i + 1}`}
              onClick={() => regenTopic(i)}>
              {regen === i ? <Loader2 size={14} className="animate-spin" /> : <RotateCcw size={14} />}</button>
          </div>
          <textarea className="field" rows={4} value={t.details} aria-label={`Текст темы ${i + 1}`}
            onChange={(e) => { const dd = [...d.detailed]; dd[i] = { ...t, details: e.target.value }; setD({ ...d, detailed: dd }); }} />
        </div>
      ))}
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
  const [history, setHistory] = useState<{ q: string; a: string }[]>([]);
  const [busy, setBusy] = useState(false);
  const toast = useToast();
  async function ask() {
    if (q.trim().length < 3 || busy) return;
    setBusy(true);
    try {
      const r = await api.post(`/api/jobs/${jobId}/ask`, { question: q.trim() });
      setHistory((h) => [{ q: q.trim(), a: r.answer || "" }, ...h].slice(0, 10));
      setQ("");
    }
    catch (e: any) { toast(e.message, true); }
    finally { setBusy(false); }
  }
  return (
    <section className="glass2 rounded-2xl p-3 mt-4" aria-label="Спросить по встрече">
      <h3 className="text-[12.5px] font-semibold mb-2">💬 Спросить по встрече</h3>
      <div className="flex gap-2">
        <input className="field" placeholder="например: что решили по тегам?" aria-label="Вопрос по встрече"
          value={q} onChange={(e) => setQ(e.target.value)}
          onKeyDown={(e) => { if (e.key === "Enter") ask(); }} />
        <button className="btn btn-primary flex-none" disabled={busy} onClick={ask}>
          {busy ? <Loader2 size={14} className="animate-spin" /> : "Спросить"}</button>
      </div>
      {history.map((h, i) => (
        <div key={i} className="mt-2.5 text-[12.5px] leading-relaxed">
          <div className="font-semibold">{h.q}</div>
          <div className="whitespace-pre-wrap" style={{ color: i ? "var(--muted)" : undefined }}>{h.a}</div>
        </div>
      ))}
    </section>
  );
}

// --- Задачи → Weeek: таблица с чекбоксами, исполнителем и сроком -------------
type Draft = {
  key: string; section: string; index: number; title: string; task: string;
  owner_name: string; owner_user_id: string | null; owner_match: string;
  due: string | null; due_raw: string | null; grounded: boolean; quote: string; t: string;
  status: string; weeek_task_id: any; weeek_url: string | null; error: string | null;
  selected?: boolean;
};
type Member = { id: string; name: string; email?: string };

export function WeeekTasks({ jobId, tasksCount }: { jobId: string; tasksCount: number }) {
  const [data, setData] = useState<any>(null);
  const [rows, setRows] = useState<Draft[]>([]);
  const [checked, setChecked] = useState<Record<string, boolean>>({});
  const [busy, setBusy] = useState(false);
  const [open, setOpen] = useState(false);
  const toast = useToast();

  const load = async () => {
    try {
      const d = await api.get(`/api/jobs/${jobId}/weeek-tasks`);
      setData(d);
      const items: Draft[] = d.items || [];
      setRows(items);
      const c: Record<string, boolean> = {};
      items.forEach((it) => { c[it.key] = !!it.selected; });
      setChecked(c);
    } catch (e: any) { setData({ error: e.message }); }
  };
  useEffect(() => { if (tasksCount > 0) load(); /* eslint-disable-line react-hooks/exhaustive-deps */ }, [jobId, tasksCount]);

  if (!tasksCount || !data || data.enabled === false) return null;
  const members: Member[] = data.members || [];
  const drafts = rows.filter((r) => r.status !== "skipped");
  const selectable = drafts.filter((r) => r.status !== "created");
  const nSel = selectable.filter((r) => checked[r.key]).length;

  const setRow = (key: string, patch: Partial<Draft>) =>
    setRows((rs) => rs.map((r) => (r.key === key ? { ...r, ...patch } : r)));

  async function pickOwner(r: Draft, userId: string) {
    setRow(r.key, { owner_user_id: userId || null, owner_match: userId ? "map" : "none" });
    // Запоминаем «имя в протоколе → участник» для всей команды: в следующий
    // раз «Зоя Р» подставится сама.
    if (r.owner_name && userId) {
      try { await api.post("/api/automation/weeek/user-map", { name: r.owner_name, user_id: userId }); }
      catch { /* не критично */ }
    }
  }

  async function create() {
    const items = selectable.filter((r) => checked[r.key]).map((r) => ({
      key: r.key, title: r.title, owner_user_id: r.owner_user_id || undefined, due: r.due || undefined }));
    if (!items.length) return;
    setBusy(true);
    try {
      const d = await api.post(`/api/jobs/${jobId}/weeek-tasks/create`, { items });
      const ok = (d.results || []).filter((x: any) => x.ok).length;
      const bad = (d.results || []).filter((x: any) => !x.ok);
      toast(bad.length ? `Создано ${ok}, не удалось ${bad.length}: ${bad[0].error}` : `В Weeek создано задач: ${ok}`, bad.length > 0);
      setData(d); setRows(d.items || []);
    } catch (e: any) { toast(e.message, true); }
    finally { setBusy(false); }
  }
  async function skip(key: string) {
    try { const d = await api.post(`/api/jobs/${jobId}/weeek-tasks/${encodeURIComponent(key)}/skip`); setRows(d.items || []); }
    catch (e: any) { toast(e.message, true); }
  }
  async function refreshMembers() {
    try { await api.get("/api/automation/weeek/members?refresh=1"); await load(); toast("Участники Weeek обновлены"); }
    catch (e: any) { toast(e.message, true); }
  }

  const created = rows.filter((r) => r.status === "created").length;
  return (
    <section className="glass2 rounded-2xl p-3 mb-4" aria-label="Задачи в Weeek">
      <button type="button" onClick={() => setOpen((o) => !o)} aria-expanded={open}
        className="w-full flex items-center gap-2 text-left" style={{ background: "none", border: 0, padding: 0, cursor: "pointer" }}>
        <span className="text-[12.5px] font-semibold">🗂 Задачи → в Weeek</span>
        <span className="text-[11.5px]" style={{ color: "var(--muted)" }}>
          {data.error ? data.error : !data.connected ? "Weeek не подключён — подключите на странице «Weeek»"
            : created ? `создано ${created} из ${drafts.length}` : `черновиков: ${drafts.length}, отмечено ${nSel}`}
        </span>
      </button>
      {open && !data.error && (
        <div className="mt-3">
          <div className="text-[11.5px] mb-2" style={{ color: "var(--muted)" }}>
            По умолчанию отмечены задачи с дословным основанием и однозначным исполнителем. Исполнителя и срок можно поправить перед созданием.
            {!members.length && (
              <> Список участников Weeek пуст — <button className="underline" onClick={refreshMembers} style={{ background: "none", border: 0, color: "inherit", cursor: "pointer" }}>загрузить</button>.</>
            )}
          </div>
          <div style={{ overflowX: "auto" }}>
            <table className="w-full text-[12.5px]" style={{ borderCollapse: "collapse" }}>
              <thead>
                <tr style={{ color: "var(--muted)" }}>
                  <th scope="col" className="text-left p-1"><input type="checkbox" aria-label="Выбрать все"
                    checked={selectable.length > 0 && nSel === selectable.length}
                    onChange={(e) => { const c = { ...checked }; selectable.forEach((r) => { c[r.key] = e.target.checked; }); setChecked(c); }} /></th>
                  <th scope="col" className="text-left p-1">Задача</th>
                  <th scope="col" className="text-left p-1">Исполнитель</th>
                  <th scope="col" className="text-left p-1">Срок</th>
                  <th scope="col" className="text-left p-1">Статус</th>
                </tr>
              </thead>
              <tbody>
                {drafts.map((r) => {
                  const done = r.status === "created";
                  return (
                    <tr key={r.key} style={{ borderTop: "1px solid var(--line)", opacity: done ? 0.75 : 1 }}>
                      <td className="p-1 align-top">
                        {!done && <input type="checkbox" checked={!!checked[r.key]} aria-label={`Выбрать: ${r.title}`}
                          onChange={(e) => setChecked({ ...checked, [r.key]: e.target.checked })} />}
                      </td>
                      <td className="p-1 align-top" style={{ minWidth: 220 }}>
                        {done ? <span>{r.title}</span> : (
                          <input className="field" value={r.title} aria-label="Название задачи" style={{ margin: 0 }}
                            onChange={(e) => setRow(r.key, { title: e.target.value })} />
                        )}
                        {r.quote && <div className="text-[11px] mt-0.5" style={{ color: "var(--muted)" }}>
                          основание{r.t ? ` · ${r.t}` : ""}: «{r.quote.slice(0, 140)}{r.quote.length > 140 ? "…" : ""}»</div>}
                        {!r.grounded && <div className="text-[11px] mt-0.5" style={{ color: "var(--warn)" }}>⚠ без дословного основания</div>}
                      </td>
                      <td className="p-1 align-top" style={{ minWidth: 150 }}>
                        {done ? <span>{members.find((m) => m.id === r.owner_user_id)?.name || r.owner_name || "—"}</span> : (
                          <>
                            <select className="field" style={{ margin: 0 }} value={r.owner_user_id || ""} aria-label="Исполнитель"
                              onChange={(e) => pickOwner(r, e.target.value)}>
                              <option value="">— не назначен</option>
                              {members.map((m) => <option key={m.id} value={m.id}>{m.name}</option>)}
                            </select>
                            {r.owner_name && r.owner_match !== "exact" && r.owner_match !== "map" && (
                              <div className="text-[11px] mt-0.5" style={{ color: "var(--warn)" }}>
                                в протоколе: {r.owner_name}{r.owner_match === "fuzzy" ? " (похоже, но проверьте)" : " (не найден в Weeek)"}</div>
                            )}
                          </>
                        )}
                      </td>
                      <td className="p-1 align-top" style={{ minWidth: 130 }}>
                        {done ? <span>{r.due || "—"}</span> : (
                          <input type="date" className="field" style={{ margin: 0 }} value={r.due || ""} aria-label="Срок"
                            onChange={(e) => setRow(r.key, { due: e.target.value || null })} />
                        )}
                        {r.due_raw && <div className="text-[11px] mt-0.5" style={{ color: "var(--muted)" }}>сказали: {r.due_raw}</div>}
                      </td>
                      <td className="p-1 align-top whitespace-nowrap">
                        {done ? (r.weeek_url ? <a href={r.weeek_url} target="_blank" rel="noreferrer" className="inline-flex items-center gap-1">создано <ExternalLink size={12} /></a> : <span>создано #{String(r.weeek_task_id)}</span>)
                          : r.status === "failed" ? <span style={{ color: "#f87171" }} title={r.error || ""}>ошибка</span>
                          : <button className="btn-ghost text-[11.5px]" onClick={() => skip(r.key)} aria-label={`Не заводить: ${r.title}`}>не заводить</button>}
                        {r.status === "failed" && r.error && <div className="text-[11px]" style={{ color: "var(--muted)", whiteSpace: "normal", maxWidth: 200 }}>{r.error}</div>}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
          <div className="flex gap-2 justify-end mt-3 flex-wrap">
            <button className="btn btn-ghost" onClick={refreshMembers}>Обновить участников</button>
            <button className="btn btn-primary" disabled={busy || !nSel || !data.connected} onClick={create}>
              {busy ? <Loader2 size={14} className="animate-spin" /> : null} Создать в Weeek ({nSel})</button>
          </div>
        </div>
      )}
    </section>
  );
}

export function Protocol({ a, jobId }: { a: any; jobId?: string }) {
  if (!a) return null;
  const ver = a.verification || {};
  const verErr = ver.error;
  const series = a._series;
  const engine = [a._provider, a._model].filter(Boolean).join(" · ");
  return (
    <div>
      {Array.isArray(a._fallback) && a._fallback.length > 0 && (
        <div className="glass2 rounded-2xl p-3 mb-4 text-[12.5px] font-semibold" style={{ color: "var(--warn)" }} role="status">
          ЧЕРНОВИК: выбранный движок не ответил, протокол собран запасным и может быть неполным — пересоберите позже.
          <span className="font-normal"> {String(a._fallback[0]).slice(0, 160)}</span></div>
      )}
      {a._warning && (
        <div className="glass2 rounded-2xl p-3 mb-4 text-[12.5px]" style={{ color: "var(--warn)" }} role="status">⚠ {a._warning}</div>
      )}
      {a._schema_miss && (
        <div className="glass2 rounded-2xl p-3 mb-4 text-[12.5px]" style={{ color: "var(--warn)" }} role="status">
          ⚠ Модель ответила не по схеме — протокол собран из того, что удалось разобрать. Лучше «Пересобрать».</div>
      )}
      {verErr && (
        <div className="glass2 rounded-2xl p-3 mb-4 text-[12.5px]" style={{ color: "var(--muted)" }} role="status">
          Проверка по расшифровке выполнена не полностью: {String(verErr).slice(0, 160)}. Пункты без «основания» не проверялись.</div>
      )}
      {(series || engine || a._quality?.no_actionable) && (
        <div className="text-[11.5px] mb-3 flex flex-wrap gap-x-3 gap-y-1" style={{ color: "var(--muted)" }}>
          {series && <span>серия: <b style={{ color: "var(--txt)" }}>{series.title}</b></span>}
          {engine && <span>движок: {engine}</span>}
          {a._quality?.no_actionable && <span style={{ color: "var(--warn)" }}>⚠ в протоколе нет ни решений, ни задач — проверьте запись</span>}
        </div>
      )}
      {a.participants?.length ? (
        <section className="mb-4" aria-label="Участники">
          <h3 className="font-bold text-[13.5px] mb-1.5" style={{ color: "var(--accent-text, var(--accent))" }}>Участники</h3>
          <div className="flex flex-wrap gap-1.5">
            {a.participants.map((p: any, i: number) => (
              <span key={i} className="chip" style={{ color: "var(--txt)" }}>{p.name}{p.role ? ` · ${p.role}` : ""}</span>
            ))}
          </div>
        </section>
      ) : null}
      {a.summary && (<section className="mb-4" aria-label="Кратко">
        <h3 className="font-bold text-[13.5px] mb-1.5" style={{ color: "var(--accent-text, var(--accent))" }}>Кратко</h3>
        <div className="text-[13px] leading-relaxed">{a.summary}</div></section>)}
      <List title="Решения" items={a.decisions} verify={ver.decisions} />
      <List title="Задачи" items={(a.tasks || []).filter((t: any) => t.owner && t.owner !== "—")}
        verify={(a.tasks || []).map((t: any, i: number) => [t, ver.tasks?.[i]]).filter(([t]: any) => t.owner && t.owner !== "—").map(([, v]: any) => v)} />
      <List title="Задачи без ответственного — назначьте исполнителя"
        items={(a.tasks || []).filter((t: any) => !t.owner || t.owner === "—")}
        verify={(a.tasks || []).map((t: any, i: number) => [t, ver.tasks?.[i]]).filter(([t]: any) => !t.owner || t.owner === "—").map(([, v]: any) => v)} />
      {jobId && <WeeekTasks jobId={jobId} tasksCount={(a.tasks?.length || 0) + (a.minor_tasks?.length || 0)} />}
      <List title="Мелкие задачи" items={a.minor_tasks} verify={ver.minor_tasks} />
      <List title="Уже сделано" items={a.done_tasks} verify={ver.done_tasks} />
      <StatusList items={a.statuses} />
      <CaveatList items={a.caveats} />
      {a._carried?.items?.length ? (
        <section className="mb-4" aria-label="Задачи прошлой встречи">
          <h3 className="font-bold text-[13.5px] mb-1.5" style={{ color: "var(--accent-text, var(--accent))" }}>
            Задачи прошлой встречи ({a._carried.date || "?"}): что с ними</h3>
          <ul className="space-y-1.5">
            {a._carried.items.map((x: any, i: number) => (
              <li key={i} className="text-[13px] leading-relaxed flex gap-2">
                <ChevronRight size={14} className="flex-none mt-0.5" color="var(--muted)" aria-hidden="true" />
                <span className="min-w-0">{x.task}{x.owner ? ` — ${x.owner}` : ""}
                  <span className="chip ml-1.5" style={{ color: x.status === "без упоминания" ? "var(--warn)" : "var(--txt)" }}>{x.status}</span></span>
              </li>
            ))}
          </ul>
        </section>
      ) : null}
      <List title="Выводы и открытые вопросы" items={a.conclusions} />
      {a.detailed?.length ? (
        <details className="mb-4" open={!a.decisions?.length && !a.tasks?.length}>
          <summary className="font-bold text-[13.5px] mb-1.5 cursor-pointer" style={{ color: "var(--accent-text, var(--accent))" }}>
            По темам ({a.detailed.length})</summary>
          {a.detailed.map((t: any, i: number) => (
            <div key={i} className="glass2 rounded-xl p-3 mb-2">
              <h4 className="font-semibold text-[13px] mb-1">{t.topic}
                {t._unsupported !== undefined && t._unsupported !== null ? (
                  <span className="chip ml-1.5 text-[10.5px]" style={{ color: "var(--warn)" }}
                    title="Часть сведений этого раздела не нашлась в расшифровке">⚠ частично без опоры</span>
                ) : null}</h4>
              <div className="text-[12.5px] leading-relaxed" style={{ color: "var(--muted)" }}>{t.details}</div>
            </div>
          ))}
        </details>
      ) : null}
      <List title="Ключевые мысли" items={a.key_thoughts} />
    </div>
  );
}
