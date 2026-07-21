from fastapi import FastAPI, Depends, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from auth import current_ctx
from db import get_conn
from logging_setup import log
app = FastAPI(title="Relay")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
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

@app.get("/jobs/{job_id}")
def get_job(job_id: int, ctx=Depends(current_ctx)):
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT id, company_id, kind, status FROM jobs WHERE id=%s", (job_id,))
        row = cur.fetchone()
        if not row: raise HTTPException(404, "not found")
        return {"id": row[0], "company_id": row[1], "kind": row[2], "status": row[3]}

@app.get("/jobs/{job_id}/result")
def get_result(job_id: int, ctx=Depends(current_ctx)):
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT payload FROM job_results WHERE job_id=%s", (job_id,))
        row = cur.fetchone()
        if not row: raise HTTPException(404, "no result")
        return {"payload": row[0]}

class NewJob(BaseModel):
    kind: str
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

@app.get("/admin/jobs")
def admin_jobs(ctx=Depends(current_ctx)):
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT id, company_id, status FROM jobs ORDER BY id")
        return [{"id": r[0], "company_id": r[1], "status": r[2]} for r in cur.fetchall()]
