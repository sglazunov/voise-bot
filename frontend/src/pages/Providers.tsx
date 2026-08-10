import { useEffect, useState } from "react";
import {
  BrainCircuit, Plus, Trash2, CheckCircle2, KeyRound, Cpu, ExternalLink, X, AlertTriangle,
} from "lucide-react";
import { Page } from "../components/Layout";
import { Card, useToast } from "../components/ui";
import { api } from "../lib/api";

type Provider = { id: string; label: string; available: boolean; needs_key: boolean; keys: number };
type Engine = { value: string; label: string };

// Extra field hints for providers that need a second value beyond the key.
const EXTRA: Record<string, { label: string; ph: string } | undefined> = {
  yandex: { label: "Folder ID (каталог)", ph: "b1g..." },
  gigachat: { label: "Scope (необязательно)", ph: "GIGACHAT_API_PERS" },
};

export default function Providers() {
  const [providers, setProviders] = useState<Provider[]>([]);
  const [engines, setEngines] = useState<Engine[]>([]);
  const [ollama, setOllama] = useState<any>(null);
  const [ollamaUrl, setOllamaUrl] = useState("");
  const toast = useToast();

  const load = () => api.get("/api/providers").then((d) => {
    setProviders(d.providers || []);
    setEngines(d.engines || []);
    setOllama(d.ollama_status || null);
    setOllamaUrl(d.ollama_install_url || "");
  }).catch(() => {});
  useEffect(() => { load(); }, []);

  const modelsOf = (id: string) =>
    engines.filter((e) => e.value === id || e.value.startsWith(id + ":"));

  return (
    <Page title="Нейросети" subtitle="Ключи LLM-провайдеров, выбор нейросети и её модели для протокола"
      onRefresh={load}>
      <Card className="mb-3.5">
        <div className="flex items-start gap-3">
          <div className="grid place-items-center rounded-xl flex-none" style={{ width: 40, height: 40, background: "rgba(45,212,191,.13)" }}>
            <BrainCircuit size={19} color="var(--accent)" /></div>
          <div className="text-[12.5px] leading-relaxed" style={{ color: "var(--muted)" }}>
            Протокол строит выбранная нейросеть. Введите API-ключ провайдера — он
            хранится <b style={{ color: "var(--txt)" }}>в вашем аккаунте, зашифрованным</b>.
            После подключения провайдер и его модели станут доступны в «Распознавании»
            (движок протокола) и в «Планировщике». Можно добавить несколько ключей —
            они автоматически чередуются при лимитах.
          </div>
        </div>
      </Card>

      <div className="grid lg:grid-cols-2 gap-3.5">
        {providers.map((p) =>
          p.id === "ollama"
            ? <OllamaCard key={p.id} p={p} ollama={ollama} url={ollamaUrl} models={modelsOf("ollama")} />
            : <ProviderCard key={p.id} p={p} models={modelsOf(p.id)} onChange={load} toast={toast} />
        )}
      </div>
    </Page>
  );
}

function ModelList({ models }: { models: Engine[] }) {
  if (!models.length) return null;
  return (
    <div className="mt-3">
      <div className="text-[11px] font-bold uppercase tracking-wide mb-1.5" style={{ color: "var(--muted)" }}>Доступные модели</div>
      <div className="flex flex-wrap gap-1.5">
        {models.map((m) => (
          <span key={m.value} className="chip" style={{ color: "var(--txt)" }}><Cpu size={11} /> {m.label}</span>
        ))}
      </div>
    </div>
  );
}

function ProviderCard({ p, models, onChange, toast }:
  { p: Provider; models: Engine[]; onChange: () => void; toast: (m: string, bad?: boolean) => void }) {
  const [key, setKey] = useState("");
  const [extra, setExtra] = useState("");
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState("");
  const [keys, setKeys] = useState<any[] | null>(null);
  const [verifying, setVerifying] = useState(false);
  const ex = EXTRA[p.id];

  async function connect() {
    if (!key.trim()) { toast("Введите ключ", true); return; }
    setBusy(true); setNote("");
    try {
      const r = await api.post("/api/providers/connect", { provider: p.id, api_key: key, extra });
      setKey(""); setExtra(""); setNote(r.note || ""); toast(`${p.label}: ключ подключён`);
      onChange(); if (keys !== null) loadKeys();
    } catch (e: any) { toast(e.message, true); } finally { setBusy(false); }
  }
  // Перебор идёт 2-3 минуты (пауза между пробами держит нас под лимитом
  // NVIDIA), поэтому запускаем его в фоне и опрашиваем ход. Раньше запрос
  // висел всё это время, и кнопка выглядела зависшей.
  async function verifyNvidia() {
    setVerifying(true); setNote("Проверяю модели…");
    try {
      await api.post("/api/providers/nvidia/verify");
      for (;;) {
        await new Promise((r) => setTimeout(r, 3000));
        const s = await api.get("/api/providers/nvidia/verify");
        if (s.running) { setNote(`Проверяю модели: ${s.done} из ${s.total}…`); continue; }
        const n = (s.models || []).length;
        setNote(n ? `Доступно моделей по вашему ключу: ${n}. Список ниже обновлён.`
                  : "Ни одна модель не ответила — проверьте, что модели включены в аккаунте NVIDIA.");
        onChange();
        break;
      }
    } catch (e: any) { toast(e.message, true); setNote(""); }
    finally { setVerifying(false); }
  }
  async function loadKeys() {
    try { const r = await api.get(`/api/providers/keys?provider=${p.id}`); setKeys(r.keys || []); }
    catch (e: any) { toast(e.message, true); }
  }
  async function removeKey(index: number) {
    try { await api.post("/api/providers/keys/remove", { provider: p.id, index }); loadKeys(); onChange(); }
    catch (e: any) { toast(e.message, true); }
  }
  async function disconnect() {
    if (!confirm(`Удалить все ключи ${p.label}?`)) return;
    try { await api.post("/api/providers/disconnect", { provider: p.id }); setKeys(null); onChange(); toast("Ключи удалены"); }
    catch (e: any) { toast(e.message, true); }
  }

  return (
    <Card>
      <div className="flex items-center gap-3 mb-3">
        <div className="grid place-items-center rounded-xl flex-none" style={{ width: 38, height: 38, background: "rgba(45,212,191,.12)" }}>
          <KeyRound size={17} color="var(--accent)" /></div>
        <div className="min-w-0 flex-1">
          <div className="font-bold text-[14.5px] truncate">{p.label}</div>
          <div className="text-[11.5px]" style={{ color: "var(--muted)" }}>{p.id}</div>
        </div>
        {p.available
          ? <span className="chip" style={{ color: "#5eead4", background: "rgba(52,211,153,.16)" }}>
              <CheckCircle2 size={12} /> {p.keys} ключ{p.keys === 1 ? "" : p.keys < 5 ? "а" : "ей"}</span>
          : <span className="chip" style={{ color: "var(--muted)" }}>не подключён</span>}
      </div>

      <label className="lbl">API-ключ</label>
      <input className="field" type="password" value={key} onChange={(e) => setKey(e.target.value)}
        placeholder={p.available ? "добавить ещё ключ" : "вставьте ключ"} />
      {ex && (
        <>
          <label className="lbl mt-2">{ex.label}</label>
          <input className="field" value={extra} onChange={(e) => setExtra(e.target.value)} placeholder={ex.ph} />
        </>
      )}
      {note && (
        <div className="glass2 rounded-xl p-2.5 mt-2 text-[12px] flex gap-2" style={{ color: "var(--warn)" }}>
          <AlertTriangle size={14} className="flex-none mt-0.5" /> {note}</div>
      )}
      <div className="flex gap-2 mt-3 flex-wrap">
        <button className="btn btn-primary" onClick={connect} disabled={busy}>
          <Plus size={15} /> {busy ? "Проверка…" : "Подключить"}</button>
        {p.keys > 0 && (
          <button className="btn btn-ghost" onClick={() => (keys === null ? loadKeys() : setKeys(null))}>
            {keys === null ? "Мои ключи" : "Скрыть"}</button>
        )}
        {p.id === "nvidia" && p.keys > 0 && (
          // Каталог NVIDIA перечисляет всё опубликованное, а аккаунту выдана
          // лишь часть — узнать это можно только вызовом каждой модели.
          <button className="btn btn-ghost" onClick={verifyNvidia} disabled={verifying}>
            {verifying ? "Проверяю модели…" : "Проверить модели"}</button>
        )}
        {p.keys > 0 && <button className="btn btn-danger" onClick={disconnect}><Trash2 size={14} /> Отключить</button>}
      </div>

      {keys && keys.length > 0 && (
        <div className="mt-3 space-y-1.5">
          {keys.map((k) => (
            <div key={k.index} className="glass2 rounded-xl px-3 py-2 flex items-center gap-2 text-[12.5px]">
              <KeyRound size={13} color="var(--muted)" />
              <span className="font-mono">{k.masked}</span>
              {k.extra && <span style={{ color: "var(--muted)" }}>· {k.extra}</span>}
              <button className="ml-auto btn-danger grid place-items-center" style={{ width: 26, height: 26, borderRadius: 8 }}
                onClick={() => removeKey(k.index)} title="Удалить ключ"><X size={13} /></button>
            </div>
          ))}
        </div>
      )}

      <ModelList models={models} />
    </Card>
  );
}

function OllamaCard({ p, ollama, url, models }: { p: Provider; ollama: any; url: string; models: Engine[] }) {
  const running = ollama?.running ?? ollama?.ok ?? p.available;
  return (
    <Card>
      <div className="flex items-center gap-3 mb-3">
        <div className="grid place-items-center rounded-xl flex-none" style={{ width: 38, height: 38, background: "rgba(45,212,191,.12)" }}>
          <Cpu size={17} color="var(--accent)" /></div>
        <div className="min-w-0 flex-1">
          <div className="font-bold text-[14.5px] truncate">{p.label}</div>
          <div className="text-[11.5px]" style={{ color: "var(--muted)" }}>локально · без ключа</div>
        </div>
        {running
          ? <span className="chip" style={{ color: "#5eead4", background: "rgba(52,211,153,.16)" }}><CheckCircle2 size={12} /> работает</span>
          : <span className="chip" style={{ color: "var(--muted)" }}>не запущен</span>}
      </div>
      <div className="text-[12.5px]" style={{ color: "var(--muted)" }}>
        {running
          ? "Локальная модель готова — выбирайте её как движок протокола."
          : "Ollama не обнаружена. Запустите её на сервере/хосте, чтобы строить протокол оффлайн и бесплатно."}
      </div>
      {!running && url && (
        <a className="btn btn-ghost mt-3" href={url} target="_blank" rel="noreferrer">
          <ExternalLink size={14} /> Установить Ollama</a>
      )}
      <ModelList models={models} />
    </Card>
  );
}
