// Вопрос по ВСЕМ встречам команды — по памяти встреч, которая переживает
// ретеншн (сервер: POST /api/ask). Ответ приходит со ссылками на встречу,
// дату и таймкод; источник, который ещё лежит в истории, открывается кликом.
import { useEffect, useState } from "react";
import { Loader2, MessageCircleQuestion } from "lucide-react";
import { useToast } from "../../components/ui";
import { api } from "../../lib/api";

type Source = { doc_id: string; title: string; meeting_date: string; ts: string; kind: string; is_draft: boolean };
type Answer = { answer: string; sources: Source[]; found: number };

export function AskAll({ question, knownJobIds, onPick }: {
  question: string; knownJobIds: Set<string>; onPick: (jobId: string) => void;
}) {
  const [busy, setBusy] = useState(false);
  const [res, setRes] = useState<Answer | null>(null);
  const [asked, setAsked] = useState("");
  const [status, setStatus] = useState<{ enabled: boolean; docs: number } | null>(null);
  const toast = useToast();
  useEffect(() => {
    api.get("/api/ask/status").then(setStatus).catch(() => setStatus(null));
  }, []);
  // Новый вопрос — старый ответ не должен выглядеть ответом на него.
  useEffect(() => { if (question.trim() !== asked) setRes(null); }, [question, asked]);

  async function ask() {
    const q = question.trim();
    if (q.length < 3 || busy) return;
    setBusy(true);
    try {
      const r = await api.post("/api/ask", { question: q });
      setRes(r); setAsked(q);
    } catch (e: any) { toast(e.message, true); }
    finally { setBusy(false); }
  }
  if (status && !status.enabled) return null;
  const empty = status && status.docs === 0;
  return (
    <div className="mb-2.5">
      <button className="btn btn-ghost w-full" disabled={busy || question.trim().length < 3 || !!empty}
        title={empty ? "Память встреч пуста — проиндексируйте архив (scripts/index_archive.py)" : "Ответ по всем встречам команды со ссылками на встречу и таймкод"}
        onClick={ask} aria-label="Спросить по всем встречам">
        {busy ? <Loader2 size={14} className="animate-spin" /> : <MessageCircleQuestion size={14} />}
        {busy ? "Ищу по встречам…" : empty ? "Память встреч пуста" : `Спросить по всем встречам${status ? ` (${status.docs})` : ""}`}
      </button>
      {res && (
        <section className="glass2 rounded-2xl p-3 mt-2 text-[12.5px] leading-relaxed" aria-label="Ответ по всем встречам">
          <div className="whitespace-pre-wrap">{res.answer}</div>
          {res.sources.length > 0 && (
            <div className="mt-2 flex flex-wrap gap-1.5">
              {res.sources.map((s, i) => {
                const label = `${s.title}${s.meeting_date ? ` · ${s.meeting_date}` : ""}${s.ts ? ` [${s.ts}]` : ""}`;
                const open = knownJobIds.has(s.doc_id);
                return (
                  <button key={i} className="rounded-lg px-2 py-1 text-[11px]"
                    style={{ background: "rgba(45,212,191,.13)", color: open ? undefined : "var(--muted)", cursor: open ? "pointer" : "default" }}
                    title={open ? "Открыть запись" : "Запись уже не в истории (архив)"}
                    onClick={() => open && onPick(s.doc_id)}>
                    {s.is_draft ? "✎ " : ""}{label}
                  </button>
                );
              })}
            </div>
          )}
        </section>
      )}
    </div>
  );
}
