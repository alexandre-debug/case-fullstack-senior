import { API } from "./api";

export function SubmitForm({ auth }: { auth: string }) {
  async function submit() {
    await fetch(`${API}/jobs`, { method: "POST", headers: { "X-Auth": auth, "Content-Type": "application/json" }, body: JSON.stringify({ kind: "report" }) });
  }
  return <button onClick={submit}>Enviar job</button>;
}
