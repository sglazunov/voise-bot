import { useState } from "react";
import { HardDrive, Cloud as CloudIcon, FolderInput, UploadCloud } from "lucide-react";
import { Page } from "../components/Layout";
import { Card, useToast } from "../components/ui";
import { useSettings } from "../lib/useSettings";
import { api } from "../lib/api";

const DEST = [
  { id: "local", icon: HardDrive, title: "Локальный диск", sub: "data/recordings на сервере" },
  { id: "yandex_disk", icon: CloudIcon, title: "Яндекс Диск", sub: "OAuth-токен · disk.write" },
  { id: "gdrive", icon: FolderInput, title: "Google Drive", sub: "OAuth · refresh token" },
];

export default function CloudPage() {
  const { s, set, save } = useSettings();
  const [ytoken, setYtoken] = useState("");
  const toast = useToast();
  const yd = s.yandex_disk || {};
  const cloud = s.cloud || "local";

  async function onSave() {
    try {
      const patch: any = { cloud, protocol_folder: s.protocol_folder || null,
        yandex_disk: { folder: yd.folder || null } };
      if (ytoken.trim()) patch.yandex_disk.token = ytoken.trim();
      await save(patch); setYtoken(""); toast("Облако сохранено");
    } catch (e: any) { toast(e.message, true); }
  }
  async function test() {
    try { const r = await api.post("/api/automation/clouds/test"); toast(r.url ? "✓ Загрузка удалась" : "Готово"); }
    catch (e: any) { toast(e.message, true); }
  }

  return (
    <Page title="Облако" subtitle="Куда выгружать готовые записи">
      <Card className="mb-3.5">
        <div className="font-bold text-[15px]">Куда сохранять запись</div>
        <div className="text-[12px] mb-3" style={{ color: "var(--muted)" }}>Готовый mp4 выгружается в облако. Секреты хранятся локально на сервере.</div>
        <div className="grid md:grid-cols-3 gap-2.5">
          {DEST.map((d) => {
            const on = cloud === d.id;
            return (
              <button key={d.id} onClick={() => set("cloud", d.id)}
                className="glass2 rounded-2xl p-4 text-left transition"
                style={on ? { borderColor: "var(--accent)", boxShadow: "0 10px 26px -14px var(--accent)" } : {}}>
                <div className="grid place-items-center rounded-xl mb-3"
                  style={{ width: 38, height: 38, background: on ? "rgba(45,212,191,.16)" : "rgba(120,180,190,.1)" }}>
                  <d.icon size={17} color={on ? "var(--accent)" : "var(--muted)"} /></div>
                <div className="font-bold text-[14px]">{d.title}</div>
                <div className="text-[12px] mt-1" style={{ color: "var(--muted)" }}>{d.sub}</div>
              </button>
            );
          })}
        </div>
      </Card>

      {cloud === "yandex_disk" && (
        <Card>
          <label className="lbl">OAuth-токен Я.Диска</label>
          <input className="field" type="password" value={ytoken} onChange={(e) => setYtoken(e.target.value)}
            placeholder={yd.token ? "•••••••• сохранён" : "вставьте токен"} />
          <div className="text-[12px] mt-1.5" style={{ color: "var(--muted)" }}>scope: disk.read + disk.write</div>
          <div className="grid md:grid-cols-2 gap-3 mt-3">
            <div><label className="lbl">Папка для записи</label>
              <input className="field" value={yd.folder || ""} onChange={(e) => set("yandex_disk", { ...yd, folder: e.target.value })} placeholder="disk:/Телемост/Записи" /></div>
            <div><label className="lbl">Папка для протокола</label>
              <input className="field" value={s.protocol_folder || ""} onChange={(e) => set("protocol_folder", e.target.value)} placeholder="disk:/Телемост/Протоколы" /></div>
          </div>
          <div className="flex gap-2.5 mt-3">
            <button className="btn btn-primary" onClick={onSave}>Сохранить</button>
            <button className="btn btn-ghost" onClick={test}><UploadCloud size={15} /> Тест загрузки</button>
          </div>
        </Card>
      )}
      {cloud !== "yandex_disk" && (
        <Card><div className="flex gap-2.5"><button className="btn btn-primary" onClick={onSave}>Сохранить</button></div></Card>
      )}
    </Page>
  );
}
