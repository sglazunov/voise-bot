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
  const { s, set, save , ready } = useSettings();
  const [ytoken, setYtoken] = useState("");
  // Учётка Google: сервер отдаёт эти три поля как «есть/нет», а не значением,
  // поэтому вводим их отдельным состоянием и шлём только заполненные.
  const [gsec, setGsec] = useState({ client_id: "", client_secret: "", refresh_token: "" });
  const toast = useToast();
  const yd = s.yandex_disk || {};
  const gd = s.gdrive || {};
  const cloud = s.cloud || "local";

  async function onSave() {
    try {
      // Пустая строка = «очистить». null бэкенд трактует как «поле не меняли»,
      // из-за чего папку протоколов нельзя было стереть.
      const patch: any = { cloud, protocol_folder: s.protocol_folder ?? "",
        yandex_disk: { folder: yd.folder ?? "" },
        gdrive: { folder_id: gd.folder_id ?? "" } };
      if (ytoken.trim()) patch.yandex_disk.token = ytoken.trim();
      for (const k of ["client_id", "client_secret", "refresh_token"] as const)
        if (gsec[k].trim()) patch.gdrive[k] = gsec[k].trim();
      await save(patch);
      setYtoken(""); setGsec({ client_id: "", client_secret: "", refresh_token: "" });
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
            <button className="btn btn-primary" onClick={onSave} disabled={!ready}>Сохранить</button>
            <button className="btn btn-ghost" onClick={test}><UploadCloud size={15} /> Тест загрузки</button>
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
      {cloud !== "yandex_disk" && cloud !== "gdrive" && (
        <Card><div className="flex gap-2.5"><button className="btn btn-primary" onClick={onSave} disabled={!ready}>Сохранить</button></div></Card>
      )}
    </Page>
  );
}
