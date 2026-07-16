import { useEffect, useRef, useState } from "react";
import { UserCircle, Phone, KeyRound, ShieldCheck, Trash2, AlertTriangle, Users, Copy, Check, UserMinus, User, ChevronDown } from "lucide-react";
import { Page } from "../components/Layout";
import { Card, Modal, Popover, useToast } from "../components/ui";
import { api, logout } from "../lib/api";
import { formatRuPhone } from "../lib/phone";

export default function Profile() {
  const [info, setInfo] = useState<any>(null);
  const [phone, setPhone] = useState(""); const [phonePwd, setPhonePwd] = useState("");
  const [oldPwd, setOldPwd] = useState(""); const [newPwd, setNewPwd] = useState("");
  const [delOpen, setDelOpen] = useState(false); const [delPwd, setDelPwd] = useState(""); const [deleting, setDeleting] = useState(false);
  const [copied, setCopied] = useState(false);
  const [membersOpen, setMembersOpen] = useState(false);
  const membersRef = useRef<HTMLDivElement>(null);   // anchor for the members Popover
  const toast = useToast();

  async function copyCode() {
    try { await navigator.clipboard.writeText(info?.invite_code || ""); setCopied(true); setTimeout(() => setCopied(false), 1500); }
    catch { toast("Не удалось скопировать", true); }
  }
  async function kick(username: string) {
    if (!confirm(`Исключить «${username}» из рабочего пространства?\n\nОн потеряет доступ к общим настройкам и данным команды (аккаунт останется, но окружение станет пустым).`)) return;
    try {
      const r = await api.post("/api/profile/team/remove", { username });
      setInfo((i: any) => ({ ...i, team_members: r.team_members, team_size: r.team_size }));
      toast(`«${username}» исключён из команды`);
    } catch (e: any) { toast(e.message, true); }
  }
  const fmtExpiry = (ts?: number) => ts
    ? new Date(ts * 1000).toLocaleString("ru-RU", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" })
    : "";

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
  async function deleteAccount() {
    if (!delPwd) return toast("Введите пароль для подтверждения", true);
    setDeleting(true);
    try {
      await api.post("/api/profile/delete", { password: delPwd });
      // account gone + session revoked -> back to the login page
      window.location.href = "/login";
    } catch (e: any) { toast(e.message, true); setDeleting(false); }
  }

  return (
    <Page title="Профиль" subtitle="Учётная запись, телефон восстановления и пароль">
      <div className="grid lg:grid-cols-2 gap-3.5 items-start">
        {/* Left column packs on its own, so «Сменить телефон» sits right under the
            account card instead of waiting out the tall Team card next to it. */}
        <div className="flex flex-col gap-3.5">
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
        </div>

        {/* Right column — team + password */}
        <div className="flex flex-col gap-3.5">
        <Card>
          <div className="flex items-center gap-2 mb-3"><Users size={17} color="var(--accent)" />
            <div className="font-bold text-[15px]">Команда</div></div>
          {info?.is_admin ? (
            <>
              <div className="text-[12.5px] mb-2.5 leading-relaxed" style={{ color: "var(--muted)" }}>
                Вы — админ своего окружения. Передайте <b style={{ color: "var(--txt)" }}>код приглашения</b>:
                человек регистрируется с ним и получает доступ к вашим настройкам, токенам и API-ключам.
              </div>
              <label className="lbl">Код приглашения</label>
              <div className="glass2 rounded-2xl px-4 py-3 flex items-center gap-3">
                <code className="text-[16px] font-mono tracking-widest flex-1" style={{ color: "var(--accent)" }}>
                  {info?.invite_code || "…"}</code>
                <button className="btn btn-ghost flex-none" onClick={copyCode}>
                  {copied ? <><Check size={14} /> Готово</> : <><Copy size={14} /> Копировать</>}</button>
              </div>
              <div className="text-[12px] mt-2" style={{ color: "var(--muted)" }}>
                Код обновляется раз в сутки — действует до <b style={{ color: "var(--txt)" }}>{fmtExpiry(info?.invite_expires_at)}</b>.
                По одному коду может войти сколько угодно людей.
              </div>

              <div className="mt-4 relative" ref={membersRef}>
                <label className="lbl">Участники рабочего пространства</label>
                <button type="button" onClick={() => setMembersOpen((o) => !o)}
                  className="field flex items-center justify-between gap-2 text-left"
                  style={{ cursor: "pointer", borderColor: membersOpen ? "var(--accent)" : undefined }}>
                  <span className="flex items-center gap-2 min-w-0">
                    <Users size={15} color="var(--muted)" className="flex-none" />
                    <span className="truncate">Логины в команде</span>
                    <span className="chip flex-none" style={{ color: "var(--accent)" }}>{info?.team_size ?? 1}</span>
                  </span>
                  <ChevronDown size={16} color="var(--muted)"
                    style={{ flex: "0 0 auto", transition: ".18s", transform: membersOpen ? "rotate(180deg)" : "none" }} />
                </button>
                <Popover anchorRef={membersRef} open={membersOpen}
                  onClose={() => setMembersOpen(false)} className="space-y-1.5">
                  {(info?.team_members || []).map((m: any) => (
                    <div key={m.username} className="glass2 rounded-xl px-3 py-2 flex items-center gap-2">
                      <User size={14} color="var(--muted)" className="flex-none" />
                      <span className="text-[13px] font-semibold truncate">{m.username}</span>
                      {m.is_admin
                        ? <span className="chip flex-none" style={{ color: "var(--accent)" }}>админ</span>
                        : <button className="ml-auto btn btn-danger flex-none" onClick={() => kick(m.username)}
                            title="Убрать из рабочего пространства">
                            <UserMinus size={13} /> Исключить</button>}
                    </div>
                  ))}
                </Popover>
              </div>
            </>
          ) : (
            <div className="text-[13px] leading-relaxed" style={{ color: "var(--muted)" }}>
              Вы в команде <b style={{ color: "var(--txt)" }}>{info?.team}</b> и работаете на её общих
              настройках, токенах и ключах.
            </div>
          )}
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
      </div>

      {/* Danger zone — permanent account deletion (full width, below both columns) */}
      <Card className="mt-3.5">
          <div style={{ border: "1px solid rgba(248,113,113,.4)", borderRadius: 16, padding: 16, background: "rgba(248,113,113,.05)" }}>
            <div className="flex items-center gap-2 mb-1"><AlertTriangle size={17} color="#f87171" />
              <div className="font-bold text-[15px]" style={{ color: "#fca5a5" }}>Опасная зона</div></div>
            <div className="text-[12.5px] mb-3" style={{ color: "var(--muted)" }}>
              Полное удаление аккаунта: логин, телефон, токены, ключи, контекст и записи — всё
              безвозвратно. Отменить нельзя.
            </div>
            <button className="btn btn-danger" onClick={() => { setDelPwd(""); setDelOpen(true); }}>
              <Trash2 size={14} /> Удалить аккаунт</button>
          </div>
        </Card>

      <Modal open={delOpen} onClose={() => !deleting && setDelOpen(false)}
        title={<span style={{ color: "#fca5a5" }}>Точно удалить аккаунт навсегда?</span>}>
        <div className="text-[13px] leading-relaxed mb-3" style={{ color: "var(--muted)" }}>
          Аккаунт <b style={{ color: "var(--txt)" }}>{info?.username}</b> и все его данные (логин,
          телефон, токены Weeek/облака, ключи нейросетей, контекст, встречи и записи) будут удалены
          <b style={{ color: "#fca5a5" }}> без возможности восстановления</b>.
        </div>
        <label className="lbl">Подтвердите паролем</label>
        <input className="field" type="password" autoFocus value={delPwd}
          onChange={(e) => setDelPwd(e.target.value)}
          onKeyDown={(e) => { if (e.key === "Enter") deleteAccount(); }}
          placeholder="ваш текущий пароль" />
        <div className="flex gap-2.5 mt-4 justify-end">
          <button className="btn btn-ghost" onClick={() => setDelOpen(false)} disabled={deleting}>Отмена</button>
          <button className="btn btn-danger" onClick={deleteAccount} disabled={deleting}>
            <Trash2 size={14} /> {deleting ? "Удаление…" : "Удалить навсегда"}</button>
        </div>
      </Modal>
    </Page>
  );
}
