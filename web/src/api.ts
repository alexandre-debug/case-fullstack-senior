export const API = import.meta.env.VITE_API_URL;

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

// Resposta de erro vira exceção: o React Query trata como erro e mantém na tela os últimos dados bons,
// em vez de renderizar o corpo do erro como se fosse uma lista vazia.
async function request(method: string, path: string, auth: string, body?: unknown, headers: Record<string, string> = {}) {
  const r = await fetch(`${API}${path}`, {
    method,
    headers: { "X-Auth": auth, ...(body ? { "Content-Type": "application/json" } : {}), ...headers },
    body: body ? JSON.stringify(body) : undefined,
  });
  const data = await r.json().catch(() => null);
  if (!r.ok) throw new ApiError(r.status, typeof data?.detail === "string" ? data.detail : `HTTP ${r.status}`);
  return data;
}

export const get = (path: string, auth: string) => request("GET", path, auth);
export const post = (path: string, auth: string, body?: unknown, headers?: Record<string, string>) =>
  request("POST", path, auth, body, headers);
