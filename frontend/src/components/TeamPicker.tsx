import { useEffect, useState } from "react";
import { Users } from "lucide-react";
import { Card, LockNote, Switch } from "./ui";
import { api } from "../lib/api";

// Выбор команды для основателя сервера + замок «управляется сервером».
// Остальным — только пояснение, если часть настроек их команды под замком.
export function TeamPicker({ s, team, setTeam, onManaged }: {
  s: Record<string, any>; team: string; setTeam: (t: string) => void;
  onManaged: (on: boolean) => Promise<void>;
}) {
  const [teams, setTeams] = useState<any[] | null>(null);
  useEffect(() => {
    if (!s.can_manage_teams || teams) return;
    api.get("/api/automation/teams").then((d) => setTeams(d.teams || [])).catch(() => setTeams([]));
  }, [s.can_manage_teams, teams]);
  // Серверные ключи (флаг замка, пути на диске сервера, экономика) закрыты у
  // ЛЮБОЙ команды — пояснение про замок нужно только команде под замком.
  const anyLocked = !!s.managed_by_server && (s.locked_keys || []).length > 0;
  return (
    <>
      {s.can_manage_teams && (
        <Card className="mb-3.5">
          <div className="flex items-center gap-2 mb-2"><Users size={17} color="var(--accent)" />
            <div className="font-bold text-[15px]">Команда</div>
            <span className="chip ml-auto" style={{ color: "var(--muted)" }}>видно только основателю сервера</span></div>
          <div className="text-[12px] mb-3" style={{ color: "var(--muted)" }}>
            Клиенту на этом сервере — своя команда: он регистрируется сам, а вы задаёте ему токен,
            проект и доску встреч, место для задач, облако и включаете замок. Под замком участники клиента
            (и его админ) видят эти поля, но изменить не могут.</div>
          <div className="grid md:grid-cols-2 gap-3 items-end">
            <div><label className="lbl" htmlFor="tp-team">Чьи настройки открыть</label>
              <select id="tp-team" className="field" value={team} onChange={(e) => setTeam(e.target.value)}>
                <option value="">Своя команда</option>
                {(teams || []).filter((t: any) => !t.own).map((t: any) => (
                  <option key={t.team} value={t.team}>
                    {t.team}{t.managed_by_server ? " · под замком" : ""}{t.weeek_board_id ? ` · доска ${t.weeek_board_id}` : ""}
                  </option>))}
              </select></div>
            {team && (
              <div className="flex items-center gap-3 py-1">
                <Switch on={!!s.managed_by_server}
                  onChange={() => onManaged(!s.managed_by_server).then(() => setTeams(null))} />
                <span className="text-[13px]">Настройки управляются сервером (замок)</span>
              </div>)}
          </div>
        </Card>
      )}
      {anyLocked && <LockNote>Подключение к Weeek, доска встреч, место для задач и облако этой команды
        управляются администратором сервера: поля видны, но изменить их нельзя.</LockNote>}
    </>
  );
}
