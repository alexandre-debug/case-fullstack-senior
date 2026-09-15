import base64, os
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Literal
from fastapi import FastAPI, Depends, Header, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import JSONResponse
from psycopg import errors
from psycopg_pool import PoolTimeout
from pydantic import BaseModel
from auth import current_ctx, require_admin
from db import get_conn, pool
from logging_setup import log

PAGE_SIZE_MAX = 200

def env_list(name: str, default: str) -> list[str]:
    return [item.strip() for item in os.environ.get(name, default).split(",") if item.strip()]

@asynccontextmanager
async def lifespan(app):
    pool.open()  # não bloqueia: as conexões são abertas em background
    yield
    pool.close()

app = FastAPI(title="Relay", lifespan=lifespan)
# Host fora da lista responde 400: impede DNS rebinding, que chegaria à API como mesma origem e passaria por fora do CORS.
app.add_middleware(TrustedHostMiddleware, allowed_hosts=env_list("ALLOWED_HOSTS", "localhost,127.0.0.1"))
# Só a origem da web UI. Defesa em profundidade: a auth é por header, que o navegador não envia sozinho.
app.add_middleware(
    CORSMiddleware,
    allow_origins=env_list("CORS_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173"),
    allow_methods=["GET", "POST"],
    allow_headers=["X-Auth", "Content-Type", "Idempotency-Key"],
)

# Nos dois casos (db.py) é melhor pedir nova tentativa do que empilhar threads esperando.
@app.exception_handler(errors.LockNotAvailable)
def lock_not_available(request, exc):
    # lock_timeout: outra operação segura a linha há mais de 5s.
    return JSONResponse({"detail": "recurso travado por outra operação, tente novamente"}, status_code=503, headers={"Retry-After": "1"})

@app.exception_handler(PoolTimeout)
def pool_timeout(request, exc):
    # Nenhuma conexão livre no pool em 5s.
    return JSONResponse({"detail": "servidor ocupado, tente novamente"}, status_code=503, headers={"Retry-After": "1"})

# Cursor opaco = posição do último item entregue (created_at, id), em base64 sem padding para ir limpo na URL.
def encode_cursor(created_at: datetime, job_id: int) -> str:
    return base64.urlsafe_b64encode(f"{created_at.isoformat()}|{job_id}".encode()).decode().rstrip("=")

def decode_cursor(cursor: str) -> tuple[datetime, int]:
    try:
        created_at, job_id = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)).decode().split("|")
        created_at, job_id = datetime.fromisoformat(created_at), int(job_id)
        if not 0 < job_id < 2**31:
            raise ValueError
        return created_at, job_id
    except ValueError:  # inclui base64 inválido e bytes que não são UTF-8
        raise HTTPException(422, "cursor inválido")

# Uma query por página (antes: 1 + N queries, cada count varrendo job_results) e paginação por cursor em
# (created_at, id): cada página é um range em jobs_company_created_idx, com custo proporcional ao tamanho da
# página, não ao total de jobs nem à profundidade (com OFFSET, cada página seria mais lenta que a anterior).
# O id desempata created_at iguais (inserts em lote gravam o mesmo now() em vários jobs).
JOBS_PAGE_SQL = """SELECT j.id, j.company_id, j.kind, j.status, j.created_at,
       (SELECT count(*) FROM job_results r WHERE r.job_id = j.id)
FROM jobs j
WHERE j.company_id = %(company_id)s {after}
ORDER BY j.created_at DESC, j.id DESC
LIMIT %(limit)s"""
FIRST_PAGE_SQL = JOBS_PAGE_SQL.format(after="")
NEXT_PAGE_SQL = JOBS_PAGE_SQL.format(after="AND (j.created_at, j.id) < (%(created_at)s::timestamptz, %(id)s::int)")

def jobs_page(company_id: int, limit: int, cursor: str | None):
    params = {"company_id": company_id, "limit": limit + 1}
    if cursor:
        params["created_at"], params["id"] = decode_cursor(cursor)
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(NEXT_PAGE_SQL if cursor else FIRST_PAGE_SQL, params)
        rows = cur.fetchall()
    next_cursor = encode_cursor(rows[limit - 1][4], rows[limit - 1][0]) if len(rows) > limit else None
    return rows[:limit], next_cursor

@app.get("/jobs")
def list_jobs(
    ctx=Depends(current_ctx),
    limit: int = Query(50, ge=1, le=PAGE_SIZE_MAX),
    cursor: str | None = Query(None, max_length=200),
):
    rows, next_cursor = jobs_page(ctx["company_id"], limit, cursor)
    return {
        "items": [{"id": r[0], "kind": r[2], "status": r[3], "created_at": r[4].isoformat(), "result_count": r[5]} for r in rows],
        "next_cursor": next_cursor,
    }

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
def admin_jobs(
    ctx=Depends(require_admin),
    limit: int = Query(50, ge=1, le=PAGE_SIZE_MAX),
    cursor: str | None = Query(None, max_length=200),
):
    rows, next_cursor = jobs_page(ctx["company_id"], limit, cursor)
    return {"items": [{"id": r[0], "company_id": r[1], "kind": r[2], "status": r[3]} for r in rows], "next_cursor": next_cursor}
