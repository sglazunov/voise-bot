import { useState } from "react";
import { HardDrive, FolderInput, UploadCloud, AlertTriangle } from "lucide-react";
import { Page } from "../components/Layout";
import { Card, useToast } from "../components/ui";
import { useSettings } from "../lib/useSettings";
import { api } from "../lib/api";

const DEST = [
  { id: "local", icon: HardDrive, title: "Локальный диск", sub: "data/recordings на сервере" },
  { id: "gdrive", icon: FolderInput, title: "Google Drive", sub: "OAuth · refresh token" },
];
// Снятые бэкенды. Пока такой лежит в сохранённых настройках, ни одна карточка
// не выбрана — без явного предупреждения это выглядит как рабочая выгрузка.
const REMOVED: Record<string, string> = {
  yandex_disk: "Яндекс.Диск отключён: выберите Google Drive и заполните его поля. " +
    "Пока облако не выбрано, записи встреч остаются на сервере.",
};

export default function CloudPage() {
  const { s, set, save , ready } = useSettings();
  // Учётка Google: сервер отдаёт эти три поля как «есть/нет», а не значением,
  // поэтому вводим их отдельным состоянием и шлём только заполненные.
  const [gsec, setGsec] = useState({ client_id: "", client_secret: "", refresh_token: "" });
  const toast = useToast();
  const gd = s.gdrive || {};
  const cloud = s.cloud || "local";

  async function onSave() {
    try {
      // Пустая строка = «очистить». null бэкенд трактует как «поле не меняли»,
      // из-за чего папку протоколов нельзя было стереть.
      const patch: any = { cloud, protocol_folder: s.protocol_folder ?? "",
        gdrive: { folder_id: gd.folder_id ?? "" } };
      for (const k of ["client_id", "client_secret", "refresh_token"] as const)
        if (gsec[k].trim()) patch.gdrive[k] = gsec[k].trim();
      await save(patch);
      setGsec({ client_id: "", client_secret: "", refresh_token: "" });
      toast("Облако сохранено");
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

      {REMOVED[cloud] && (
        <Card className="mb-3.5">
          <div className="flex gap-2.5 items-start">
            <AlertTriangle size={17} style={{ color: "var(--warn, #f59e0b)", flexShrink: 0, marginTop: 2 }} />
            <div className="text-[13px]">{REMOVED[cloud]}</div>
          </div>
        </Card>
      )}
      {cloud === "gdrive" && (
        <Card>
          <div className="font-bold text-[15px]">Учётная запись Google</div>
          <div className="text-[12px] mb-3" style={{ color: "var(--muted)" }}>
            OAuth-клиент из Google Cloud Console (тип «Desktop app») и refresh token,
            полученный для scope <span className="font-mono">drive.file</span>.
            Заполненные поля хранятся на сервере в зашифрованном виде и обратно не отдаются.
          </div>
          <div className="grid md:grid-cols-2 gap-3">
            <div><label className="lbl">client_id</label>
              <input className="field" value={gsec.client_id}
                onChange={(e) => setGsec({ ...gsec, client_id: e.target.value })}
                placeholder={gd.client_id ? "•••••••• сохранён" : "…apps.googleusercontent.com"} /></div>
            <div><label className="lbl">client_secret</label>
              <input className="field" type="password" value={gsec.client_secret}
                onChange={(e) => setGsec({ ...gsec, client_secret: e.target.value })}
                placeholder={gd.client_secret ? "•••••••• сохранён" : "вставьте секрет"} /></div>
            <div><label className="lbl">refresh_token</label>
              <input className="field" type="password" value={gsec.refresh_token}
                onChange={(e) => setGsec({ ...gsec, refresh_token: e.target.value })}
                placeholder={gd.refresh_token ? "•••••••• сохранён" : "1//0…"} /></div>
            <div><label className="lbl">ID папки для записи</label>
              <input className="field" value={gd.folder_id || ""}
                onChange={(e) => set("gdrive", { ...gd, folder_id: e.target.value })}
                placeholder="пусто = корень My Drive" /></div>
          </div>
          <div className="mt-3">
            <label className="lbl">Папка для протокола</label>
            <input className="field" value={s.protocol_folder || ""}
              onChange={(e) => set("protocol_folder", e.target.value)}
              placeholder="ID папки; пусто = туда же, куда запись" />
          </div>
          <div className="flex gap-2.5 mt-3">
            <button className="btn btn-primary" onClick={onSave} disabled={!ready}>Сохранить</button>
            <button className="btn btn-ghost" onClick={test}><UploadCloud size={15} /> Тест загрузки</button>
          </div>
        </Card>
      )}
      {cloud !== "gdrive" && !REMOVED[cloud] && (
        <Card><div className="flex gap-2.5"><button className="btn btn-primary" onClick={onSave} disabled={!ready}>Сохранить</button></div></Card>
      )}
    </Page>
  );
}
