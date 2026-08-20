// Показ расшифровки: живая лента во время распознавания и готовый текст с
// пометками низкой уверенности. Вынесено из Recognition.tsx.
import { useEffect, useRef } from "react";
import { Loader2 } from "lucide-react";
export const LOWCONF = -0.7;
export function fmtTs(sec: number): string {
  const s = Math.floor(sec || 0), h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60);
  return h ? `${h}:${String(m).padStart(2, "0")}:${String(s % 60).padStart(2, "0")}`
           : `${String(m).padStart(2, "0")}:${String(s % 60).padStart(2, "0")}`;
}

export function LiveTranscript({ segments }: { segments: any[] }) {
  const boxRef = useRef<HTMLDivElement>(null);
  const stick = useRef(true);
  // Липкая автопрокрутка: пока человек внизу — доматываем к свежему тексту, но
  // если он отлистал назад читать, не дёргаем его обратно каждые две секунды.
  useEffect(() => {
    const el = boxRef.current;
    if (el && stick.current) el.scrollTop = el.scrollHeight;
  }, [segments.length]);

  return (
    <div>
      <div className="flex items-center gap-2 text-[11.5px] mb-2" style={{ color: "var(--muted)" }}>
        <Loader2 size={12} className="animate-spin" color="var(--accent)" />
        распознаётся вживую · фрагментов: {segments.length}
      </div>
      <div ref={boxRef} className="max-h-[420px] overflow-y-auto pr-1"
        onScroll={(e) => {
          const el = e.currentTarget;
          stick.current = el.scrollHeight - el.scrollTop - el.clientHeight < 40;
        }}>
        {/* Живые фрагменты приходят как {start, end, text} — имён говорящих в
            них ещё нет (спикеры определяются после распознавания), поэтому
            показываем сплошной текст. */}
        <div className="text-[12.5px] leading-relaxed">
          {segments.map((s: any, i: number) => String(s.text || "").trim()).join(" ")}
        </div>
      </div>
    </div>
  );
}


export function SegmentView({ segments }: { segments: any[] }) {
  const anyConf = segments.some((s) => typeof s.avg_logprob === "number");
  let lastSpeaker: string | null | undefined = undefined;
  return (
    <div className="text-[12.5px] leading-relaxed">
      {anyConf && (
        <div className="text-[11px] mb-2" style={{ color: "var(--muted)" }}>
          <span style={{ borderBottom: "1px dotted var(--warn)" }}>Подчёркнутое</span> — низкая
          уверенность распознавания: проверьте имена и цифры.
        </div>
      )}
      {segments.map((s, i) => {
        const low = typeof s.avg_logprob === "number" && s.avg_logprob < LOWCONF;
        const speakerChanged = s.speaker !== lastSpeaker;
        lastSpeaker = s.speaker;
        return (
          <div key={i} className={speakerChanged && s.speaker ? "mt-2" : ""}>
            {speakerChanged && s.speaker && (
              <div className="font-semibold text-[12px]" style={{ color: "var(--accent)" }}>
                [{fmtTs(s.start)}] {s.speaker}:</div>
            )}
            <span
              title={low ? `Whisper не уверен в этом фрагменте (logprob ${s.avg_logprob?.toFixed(2)})` : undefined}
              style={low ? { color: "var(--muted)", borderBottom: "1px dotted var(--warn)" } : undefined}>
              {!s.speaker && <span style={{ color: "var(--muted)" }}>[{fmtTs(s.start)}] </span>}
              {s.text}{" "}
            </span>
          </div>
        );
      })}
    </div>
  );
}

// Д13: инлайн-редактор протокола. Списки редактируются построчно; задачи —
// «текст — ответственный». Правки человека доверенные: verification снимается.
