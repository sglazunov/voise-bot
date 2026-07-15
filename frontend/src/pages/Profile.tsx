import { useEffect, useState } from "react";
import { UserCircle, Phone, KeyRound, ShieldCheck } from "lucide-react";
import { Page } from "../components/Layout";
import { Card, useToast } from "../components/ui";
import { api } from "../lib/api";
import { formatRuPhone } from "../lib/phone";

export default function Profile() {
  const [info, setInfo] = useState<any>(null);
  const [phone, setPhone] = useState(""); const [phonePwd, setPhonePwd] = useState("");
  const [oldPwd, setOldPwd] = useState(""); const [newPwd, setNewPwd] = useState("");
  const toast = useToast();

  const load = () => api.get("/api/profile").then(setInfo).catch(() => {});
  useEffect(() => { load(); }, []);

  async function savePhone() {
    try { const r = await api.post("/api/profile/phone", { password: phonePwd, phone });
      setInfo((i: any) => ({ ...i, phone_masked: r.phone_masked })); setPhone(""); setPhonePwd(""); toast("Телефон обновлён"); }
    catch (e: any) { toast(e.message, true); }
  }
  async function savePassword() {
    if (newPwd.length < 6) return toast("Пароль минимум 6 символов", true);
    try { await api.post("/api/profile/password", { new_password: newPwd, old_password: oldPwd }); setOldPwd(""); setNewPwd(""); toast("Пароль изменён"); }
    catch (e: any) { toast(e.message, true); }
  }

  return (
    <Page title="Профиль" subtitle="Учётная запись, телефон восстановления и пароль">
      <div className="grid lg:grid-cols-2 gap-3.5 items-start">
        <Card>
          <div className="flex items-center gap-3">
            <div className="grid place-items-center rounded-2xl flex-none" style={{ width: 52, height: 52, background: "linear-gradient(135deg,var(--accent),var(--accent2))" }}>
              <UserCircle size={26} color="var(--accent-ink)" /></div>
            <div><div className="font-bold text-[16px]">{info?.username || "—"}</div>
              <div className="text-[12px] flex items-center gap-1.5 mt-0.5" style={{ color: "var(--muted)" }}>
                {info?.is_admin ? <><ShieldCheck size={13} color="var(--accent)" /> Администратор</> : "Пользователь"}</div></div>
          </div>
          <div className="glass2 rounded-2xl px-4 py-3 mt-4 flex items-center justify-between">
            <div className="flex items-center gap-2"><Phone size={15} color="var(--muted)" />
              <span className="text-[13px]">Телефон восстановления</span></div>
            <span className="text-[13px] font-semibold">{info?.phone_masked || "не задан"}</span>
          </div>
        </Card>

        <Card>
          <div className="flex items-center gap-2 mb-3"><Phone size={17} color="var(--accent)" />
            <div className="font-bold text-[15px]">Сменить телефон</div></div>
          <label className="lbl">Новый телефон</label>
          <input className="field" value={phone} onChange={(e) => setPhone(formatRuPhone(e.target.value))} placeholder="+7 900 000-00-00" />
          <label className="lbl mt-3">Текущий пароль</label>
          <input className="field" type="password" value={phonePwd} onChange={(e) => setPhonePwd(e.target.value)} />
          <button className="btn btn-primary mt-3" onClick={savePhone}>Сохранить телефон</button>
        </Card>

        <Card>
          <div className="flex items-center gap-2 mb-3"><KeyRound size={17} color="var(--accent)" />
            <div className="font-bold text-[15px]">Сменить пароль</div></div>
          <div className="grid md:grid-cols-2 gap-3">
            <div><label className="lbl">Текущий пароль</label>
              <input className="field" type="password" value={oldPwd} onChange={(e) => setOldPwd(e.target.value)} /></div>
            <div><label className="lbl">Новый пароль</label>
              <input className="field" type="password" value={newPwd} onChange={(e) => setNewPwd(e.target.value)} placeholder="минимум 6 символов" /></div>
          </div>
          <button className="btn btn-primary mt-3" onClick={savePassword}>Изменить пароль</button>
        </Card>
      </div>
    </Page>
  );
}
