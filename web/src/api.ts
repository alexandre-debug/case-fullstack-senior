export const API = import.meta.env.VITE_API_URL;

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

// Resposta de erro vira exceção: o React Query trata como erro e mantém na tela os últimos dados bons,
// em vez de renderizar o corpo do erro como se fosse uma lista vazia.
async function request(method: string, path: string, auth: string) {
  const r = await fetch(`${API}${path}`, { method, headers: { "X-Auth": auth } });
  const body = await r.json().catch(() => null);
  if (!r.ok) throw new ApiError(r.status, typeof body?.detail === "string" ? body.detail : `HTTP ${r.status}`);
  return body;
}

export const get = (path: string, auth: string) => request("GET", path, auth);
export const post = (path: string, auth: string) => request("POST", path, auth);
