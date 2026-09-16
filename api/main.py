import base64, logging, os, re, time, uuid
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Literal
from fastapi import FastAPI, Depends, Header, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import JSONResponse
from psycopg import errors
from psycopg_pool import PoolTimeout
from pydantic import BaseModel
from auth import current_ctx, require_admin
from db import get_conn, pool
from logging_setup import configure_logging, log, request_id_var

configure_logging()

PAGE_SIZE_MAX = 200
REQUEST_ID = re.compile(r"[A-Za-z0-9._:-]{1,128}")

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

@app.middleware("http")
async def request_context(request: Request, call_next):
    # Aceita o X-Request-ID do cliente, se for seguro para log, ou gera um. Ele volta no header da resposta,
    # entra em toda linha de log desta requisição e é gravado no job, para o worker logar com o mesmo id.
    incoming = request.headers.get("x-request-id", "")
    request_id = incoming if REQUEST_ID.fullmatch(incoming) else uuid.uuid4().hex
    token = request_id_var.set(request_id)
    started = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception:
        log("erro não tratado", level=logging.ERROR, exc_info=True, method=request.method, path=request.url.path)
        response = JSONResponse({"detail": "erro interno", "request_id": request_id}, status_code=500)
    response.headers["X-Request-ID"] = request_id
    log(
        "request",
        method=request.method,
        path=request.url.path,
        status=response.status_code,
        duration_ms=round((time.perf_counter() - started) * 1000, 1),
        company_id=getattr(request.state, "company_id", None),
    )
    request_id_var.reset(token)
    return response

# Só a origem da web UI. Defesa em profundidade: a auth é por header, que o navegador não envia sozinho.
app.add_middleware(
    CORSMiddleware,
    allow_origins=env_list("CORS_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173"),
    allow_methods=["GET", "POST"],
    allow_headers=["X-Auth", "Content-Type", "Idempotency-Key", "X-Request-ID"],
    expose_headers=["X-Request-ID"],
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
JOBS_PAGE_SQL = """SELECT j.id, j.company_id, j.kind, j.status, j.created_at, j.attempts, j.max_attempts, j.last_error,
       (SELECT count(*) FROM job_results r WHERE r.job_id = j.id)
FROM jobs j
WHERE j.company_id = %(company_id)s {after}
ORDER BY j.created_at DESC, j.id DESC
LIMIT %(limit)s"""
FIRST_PAGE_SQL = JOBS_PAGE_SQL.format(after="")
NEXT_PAGE_SQL = JOBS_PAGE_SQL.format(after="AND (j.created_at, j.id) < (%(created_at)s::timestamptz, %(id)s::int)")

def jobs_page(company_id: int, limit: int, cursor: str | None):
    params = {"company_id": company_id, "limit": limit + 1}
    if cursor is not None:
        params["created_at"], params["id"] = decode_cursor(cursor)
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(NEXT_PAGE_SQL if cursor is not None else FIRST_PAGE_SQL, params)
        rows = cur.fetchall()
    next_cursor = encode_cursor(rows[limit - 1][4], rows[limit - 1][0]) if len(rows) > limit else None
    return rows[:limit], next_cursor

# cursor vazio é 422, não primeira página: senão um cliente que monte "cursor=" no fim da lista entraria em loop.
PAGE_LIMIT = Query(50, ge=1, le=PAGE_SIZE_MAX)
PAGE_CURSOR = Query(None, min_length=1, max_length=200)

@app.get("/jobs")
def list_jobs(ctx=Depends(current_ctx), limit: int = PAGE_LIMIT, cursor: str | None = PAGE_CURSOR):
    rows, next_cursor = jobs_page(ctx["company_id"], limit, cursor)
    return {
        "items": [
            {
                "id": r[0], "kind": r[2], "status": r[3], "created_at": r[4].isoformat(),
                "attempts": r[5], "max_attempts": r[6], "last_error": r[7], "result_count": r[8],
            }
            for r in rows
        ],
        "next_cursor": next_cursor,
    }

# Nas rotas por id, o company_id vai no WHERE: job de outra empresa responde o mesmo 404 de um id inexistente.
# Os ids continuam sequenciais e globais, então ainda dá para inferir o volume de jobs de outras empresas
# (aceito conscientemente; ids opacos ficaram fora do escopo).
JOB_FIELDS = ("id", "company_id", "kind", "status", "attempts", "max_attempts", "last_error", "request_id", "created_at", "started_at", "finished_at")

@app.get("/jobs/{job_id}")
def get_job(job_id: int, ctx=Depends(current_ctx)):
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(f"SELECT {', '.join(JOB_FIELDS)} FROM jobs WHERE id=%s AND company_id=%s", (job_id, ctx["company_id"]))
        row = cur.fetchone()
        if not row: raise HTTPException(404, "job não encontrado")
        return dict(zip(JOB_FIELDS, row))

@app.get("/jobs/{job_id}/events")
def get_job_events(job_id: int, ctx=Depends(current_ctx)):
    # Linha do tempo do job: qual requisição o criou, cada tentativa e por que falhou ou voltou para a fila.
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT 1 FROM jobs WHERE id=%s AND company_id=%s", (job_id, ctx["company_id"]))
        if cur.fetchone() is None: raise HTTPException(404, "job não encontrado")
        cur.execute("SELECT event, attempt, request_id, detail, created_at FROM job_events WHERE job_id=%s ORDER BY id", (job_id,))
        return [dict(zip(("event", "attempt", "request_id", "detail", "created_at"), r)) for r in cur.fetchall()]

@app.get("/jobs/{job_id}/result")
def get_result(job_id: int, ctx=Depends(current_ctx)):
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT r.payload FROM job_results r JOIN jobs j ON j.id = r.job_id WHERE r.job_id=%s AND j.company_id=%s",
            (job_id, ctx["company_id"]),
        )
        row = cur.fetchone()
        if not row: raise HTTPException(404, "resultado não encontrado")
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
    request_id = request_id_var.get()
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
                log("job reaproveitado pela Idempotency-Key", event="replayed", job_id=existing[0], company_id=company_id)
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
            "INSERT INTO jobs (company_id, kind, status, idempotency_key, request_id) VALUES (%s,%s,'queued',%s,%s) RETURNING id",
            (company_id, body.kind, idempotency_key, request_id),
        )
        job_id = cur.fetchone()[0]
        cur.execute("INSERT INTO job_events (job_id, event, request_id) VALUES (%s, 'created', %s)", (job_id, request_id))
        conn.commit()
        log("job criado", event="created", job_id=job_id, company_id=company_id, kind=body.kind)
        return {"id": job_id, "status": "queued"}

# As duas transições abaixo travam a linha do job (FOR UPDATE) e decidem tudo sob essa trava: o status que
# justifica o 409 é o mesmo que impediu o UPDATE. Reler o status num SELECT à parte daria respostas
# contraditórias (ex.: "job em queued não pode ser cancelado", se um retry concorrente commitasse no meio).
TRANSITION_SQL = """WITH target AS (
  SELECT id, status AS old_status, attempts, max_attempts FROM jobs
  WHERE id=%(id)s AND company_id=%(company_id)s FOR UPDATE
), changed AS (
  UPDATE jobs SET {set_clause}
  FROM target WHERE jobs.id = target.id AND {condition}
  RETURNING jobs.id, jobs.attempts
), logged AS (
  INSERT INTO job_events (job_id, event, attempt, request_id, detail)
  SELECT c.id, %(event)s, {event_attempt}, %(request_id)s, %(detail)s FROM changed c JOIN target t ON t.id = c.id
)
SELECT t.old_status, t.attempts, t.max_attempts, EXISTS (SELECT 1 FROM changed) FROM target t"""

# Só sai de queued/running. Quem grava primeiro vence a corrida com o worker: se a finalização commitar antes,
# este UPDATE não casa e a resposta é 409; se o cancelamento commitar antes, é a finalização do worker que não
# casa, e ele descarta o resultado sem cobrar cota.
# O evento só registra tentativa quando o job estava de fato rodando (em queued, a tentativa anterior já terminou).
CANCEL_SQL = TRANSITION_SQL.format(
    set_clause="status='cancelled', finished_at=now(), lease_expires_at=NULL, updated_at=now()",
    condition="target.old_status IN ('queued', 'running')",
    event_attempt="CASE WHEN t.old_status = 'running' THEN c.attempts END",
)

# failed -> queued só acontece uma vez: dois cliques simultâneos disputam a mesma linha e o segundo encontra o
# job já em queued (409). attempts nunca é zerado, então o limite de tentativas continua valendo, e o CHECK
# jobs_queued_attempts_check garante no banco que nenhum job volta para a fila sem tentativa sobrando.
RETRY_SQL = TRANSITION_SQL.format(
    set_clause="status='queued', finished_at=NULL, lease_expires_at=NULL, updated_at=now()",
    condition="target.old_status = 'failed' AND target.attempts < target.max_attempts",
    event_attempt="c.attempts",
)

def transition(cur, sql, job_id: int, company_id: int, event: str, detail: str | None = None):
    cur.execute(sql, {"id": job_id, "company_id": company_id, "event": event, "detail": detail, "request_id": request_id_var.get()})
    row = cur.fetchone()
    if row is None:  # job de outra empresa responde o mesmo 404 de um id inexistente
        raise HTTPException(404, "job não encontrado")
    return row  # (status anterior, attempts, max_attempts, mudou)

@app.post("/jobs/{job_id}/cancel")
def cancel_job(job_id: int, ctx=Depends(current_ctx)):
    company_id = ctx["company_id"]
    with get_conn() as conn, conn.cursor() as cur:
        old_status, _, _, cancelled = transition(cur, CANCEL_SQL, job_id, company_id, "cancelled")
        if not cancelled:
            raise HTTPException(409, f"job em {old_status} não pode ser cancelado")
        conn.commit()
        log("job cancelado", event="cancelled", job_id=job_id, company_id=company_id)
        return {"id": job_id, "status": "cancelled"}

@app.post("/jobs/{job_id}/retry")
def retry_job(job_id: int, ctx=Depends(current_ctx)):
    company_id = ctx["company_id"]
    with get_conn() as conn, conn.cursor() as cur:
        old_status, attempts, max_attempts, retried = transition(
            cur, RETRY_SQL, job_id, company_id, "retried", "reprocessamento solicitado"
        )
        if not retried:
            if old_status != "failed":
                raise HTTPException(409, f"job em {old_status} não pode ser reprocessado")
            raise HTTPException(409, f"job esgotou as {max_attempts} tentativas")
        # Voltar para a fila passa pela mesma admissão de um job novo (ordem de locks job -> empresa):
        # o retry não fura o limite de concorrência nem a cota. O job já conta como ativo aqui, daí o ">".
        cur.execute("SELECT max_concurrent_jobs, job_quota FROM companies WHERE id=%s FOR NO KEY UPDATE", (company_id,))
        limit, quota = cur.fetchone()
        cur.execute("SELECT count(*) FROM jobs WHERE company_id=%s AND status IN ('queued','running')", (company_id,))
        active = cur.fetchone()[0]
        if active > limit:
            raise HTTPException(429, "limite de jobs concorrentes atingido")
        if active > quota:
            raise HTTPException(402, "cota restante já reservada para os jobs em andamento")
        conn.commit()
        log("job reprocessado", event="retried", job_id=job_id, company_id=company_id, attempts=attempts)
        return {"id": job_id, "status": "queued", "attempts": attempts}

# Não existe papel de plataforma: o admin é da empresa e vê todos os jobs só da própria empresa.
@app.get("/admin/jobs")
def admin_jobs(ctx=Depends(require_admin), limit: int = PAGE_LIMIT, cursor: str | None = PAGE_CURSOR):
    rows, next_cursor = jobs_page(ctx["company_id"], limit, cursor)
    return {"items": [{"id": r[0], "company_id": r[1], "kind": r[2], "status": r[3]} for r in rows], "next_cursor": next_cursor}
