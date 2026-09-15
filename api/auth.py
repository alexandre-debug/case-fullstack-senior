import re
from fastapi import Depends, Header, HTTPException, Request
from db import get_conn

# formato: "<company_id>:<role>"  (sem assinatura — simplificação do case)
# Até 9 dígitos cabe em INT; qualquer outra coisa (espaços, role desconhecida, sufixos) é 401, não 500.
X_AUTH = re.compile(r"([0-9]{1,9}):(user|admin)")

def current_ctx(request: Request, x_auth: str | None = Header(default=None)):
    match = X_AUTH.fullmatch(x_auth or "")
    if not match:
        raise HTTPException(401, "X-Auth ausente ou inválido")
    company_id, role = int(match[1]), match[2]
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT 1 FROM companies WHERE id=%s", (company_id,))
        if cur.fetchone() is None:
            raise HTTPException(401, "X-Auth ausente ou inválido")
    request.state.company_id = company_id  # para o log de acesso (main.py)
    return {"company_id": company_id, "role": role}

def require_admin(ctx=Depends(current_ctx)):
    if ctx["role"] != "admin":
        raise HTTPException(403, "acesso restrito a admin")
    return ctx
