import os
from typing import Literal
from fastapi import FastAPI, Depends, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import JSONResponse
from psycopg import errors
from pydantic import BaseModel
from auth import current_ctx, require_admin
from db import get_conn
from logging_setup import log

def env_list(name: str, default: str) -> list[str]:
    return [item.strip() for item in os.environ.get(name, default).split(",") if item.strip()]

app = FastAPI(title="Relay")
# Host fora da lista responde 400: impede DNS rebinding, que chegaria à API como mesma origem e passaria por fora do CORS.
app.add_middleware(TrustedHostMiddleware, allowed_hosts=env_list("ALLOWED_HOSTS", "localhost,127.0.0.1"))
# Só a origem da web UI. Defesa em profundidade: a auth é por header, que o navegador não envia sozinho.
app.add_middleware(
    CORSMiddleware,
    allow_origins=env_list("CORS_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173"),
    allow_methods=["GET", "POST"],
    allow_headers=["X-Auth", "Content-Type", "Idempotency-Key"],
)

@app.exception_handler(errors.LockNotAvailable)
def lock_not_available(request, exc):
    # lock_timeout (db.py): alguém segura a linha há mais de 5s; melhor pedir nova tentativa do que empilhar threads.
    return JSONResponse({"detail": "recurso ocupado, tente novamente"}, status_code=503, headers={"Retry-After": "1"})

@app.get("/jobs")
def list_jobs(ctx=Depends(current_ctx)):
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT id, kind, status, created_at FROM jobs WHERE company_id=%s ORDER BY created_at DESC", (ctx["company_id"],))
        rows = cur.fetchall()
        out = []
        for r in rows:
            cur.execute("SELECT count(*) FROM job_results WHERE job_id=%s", (r[0],))
            out.append({"id": r[0], "kind": r[1], "status": r[2], "created_at": r[3].isoformat(), "result_count": cur.fetchone()[0]})
        return out

# Nas rotas por id, o company_id vai no WHERE: job de outra empresa responde o mesmo 404 de um id inexistente.
# Os ids continuam sequenciais e globais, então ainda dá para inferir o volume de jobs de outras empresas
# (aceito conscientemente; ids opacos ficaram fora do escopo).
@app.get("/jobs/{job_id}")
def get_job(job_id: int, ctx=Depends(current_ctx)):
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT id, company_id, kind, status FROM jobs WHERE id=%s AND company_id=%s", (job_id, ctx["company_id"]))
        row = cur.fetchone()
        if not row: raise HTTPException(404, "not found")
        return {"id": row[0], "company_id": row[1], "kind": row[2], "status": row[3]}

@app.get("/jobs/{job_id}/result")
def get_result(job_id: int, ctx=Depends(current_ctx)):
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT r.payload FROM job_results r JOIN jobs j ON j.id = r.job_id WHERE r.job_id=%s AND j.company_id=%s",
            (job_id, ctx["company_id"]),
        )
        row = cur.fetchone()
        if not row: raise HTTPException(404, "no result")
        return {"payload": row[0]}

class NewJob(BaseModel):
    kind: Literal["report", "import"]
@app.post("/jobs")
def create_job(
    body: NewJob,
    ctx=Depends(current_ctx),
    idempotency_key: str | None = Header(default=None, min_length=1, max_length=128),
):
    company_id = ctx["company_id"]
    with get_conn() as conn, conn.cursor() as cur:
        # Trava a linha da empresa: admissões da mesma empresa passam uma de cada vez, então a contagem
        # continua válida até o INSERT (antes, count e insert soltos deixavam 20 POSTs criarem 7 jobs com limite 2).
        cur.execute("SELECT max_concurrent_jobs, job_quota FROM companies WHERE id=%s FOR NO KEY UPDATE", (company_id,))
        limit, quota = cur.fetchone()
        if idempotency_key:
            # Mesma chave = mesma intenção: devolve o job já criado (duplo clique, retry de rede).
            cur.execute("SELECT id, status, kind FROM jobs WHERE company_id=%s AND idempotency_key=%s", (company_id, idempotency_key))
            existing = cur.fetchone()
            if existing:
                if existing[2] != body.kind:
                    raise HTTPException(422, "Idempotency-Key já usada com outro payload")
                return {"id": existing[0], "status": existing[1]}
        if quota <= 0:
            raise HTTPException(402, "cota de jobs esgotada")
        cur.execute("SELECT count(*) FROM jobs WHERE company_id=%s AND status IN ('queued','running')", (company_id,))
        active = cur.fetchone()[0]
        if active >= limit:
            raise HTTPException(429, "limite de jobs concorrentes atingido")
        # A cota é cobrada na conclusão; só admitir enquanto ela cobre todos os jobs ativos garante que
        # nenhum job admitido chegue ao fim sem poder ser cobrado.
        if active >= quota:
            raise HTTPException(402, "cota restante já reservada para os jobs em andamento")
        cur.execute(
            "INSERT INTO jobs (company_id, kind, status, idempotency_key) VALUES (%s,%s,'queued',%s) RETURNING id",
            (company_id, body.kind, idempotency_key),
        )
        job_id = cur.fetchone()[0]
        conn.commit()
        log(f"job criado id={job_id} company={company_id} kind={body.kind}")
        return {"id": job_id, "status": "queued"}

# Não existe papel de plataforma: o admin é da empresa e vê todos os jobs só da própria empresa.
@app.get("/admin/jobs")
def admin_jobs(ctx=Depends(require_admin)):
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT id, company_id, kind, status FROM jobs WHERE company_id=%s ORDER BY id", (ctx["company_id"],))
        return [{"id": r[0], "company_id": r[1], "kind": r[2], "status": r[3]} for r in cur.fetchall()]
