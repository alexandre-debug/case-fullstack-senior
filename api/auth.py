from fastapi import Header, HTTPException
def current_ctx(x_auth: str | None = Header(default=None)):
    # formato: "<company_id>:<role>"  (sem assinatura — simplificação do case)
    if not x_auth or ":" not in x_auth:
        raise HTTPException(401, "missing X-Auth")
    company_id, role = x_auth.split(":", 1)
    return {"company_id": int(company_id), "role": role}
