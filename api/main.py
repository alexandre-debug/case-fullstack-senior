import os
from typing import Literal
from fastapi import FastAPI, Depends, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
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
    allow_headers=["X-Auth", "Content-Type"],
)
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
def create_job(body: NewJob, ctx=Depends(current_ctx)):
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT max_concurrent_jobs FROM companies WHERE id=%s", (ctx["company_id"],))
        limit = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM jobs WHERE company_id=%s AND status IN ('queued','running')", (ctx["company_id"],))
        running = cur.fetchone()[0]
        if running >= limit:
            raise HTTPException(429, "limite de jobs concorrentes atingido")
        cur.execute("INSERT INTO jobs (company_id, kind, status) VALUES (%s,%s,'queued') RETURNING id", (ctx["company_id"], body.kind))
        conn.commit()
        log(f"job criado kind={body.kind}")
        return {"id": cur.fetchone()[0], "status": "queued"}

# Não existe papel de plataforma: o admin é da empresa e vê todos os jobs só da própria empresa.
@app.get("/admin/jobs")
def admin_jobs(ctx=Depends(require_admin)):
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT id, company_id, kind, status FROM jobs WHERE company_id=%s ORDER BY id", (ctx["company_id"],))
        return [{"id": r[0], "company_id": r[1], "kind": r[2], "status": r[3]} for r in cur.fetchall()]
