export const API = import.meta.env.VITE_API_URL;
export async function get(path: string, auth: string) {
  const r = await fetch(`${API}${path}`, { headers: { "X-Auth": auth } });
  return r.json();
}
