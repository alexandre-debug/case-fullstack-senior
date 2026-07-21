// 4 "usuários" fake para trocar via dropdown. Formato do header: "<company_id>:<role>".
export const FAKE_USERS = ["1:user", "1:admin", "2:user", "2:admin"];

export function roleOf(auth: string): string {
  return auth.split(":")[1] ?? "";
}

export function companyOf(auth: string): string {
  return auth.split(":")[0] ?? "";
}

export function AuthSwitcher({
  auth,
  onChange,
}: {
  auth: string;
  onChange: (auth: string) => void;
}) {
  return (
    <label style={{ display: "flex", alignItems: "center", gap: 8 }}>
      Usuário:
      <select value={auth} onChange={(e) => onChange(e.target.value)}>
        {FAKE_USERS.map((u) => (
          <option key={u} value={u}>
            {u}
          </option>
        ))}
      </select>
    </label>
  );
}
