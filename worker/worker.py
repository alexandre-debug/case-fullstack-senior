import logging, os, random, signal, sys, time
from dataclasses import dataclass
import psycopg
from psycopg import errors
from logging_setup import configure_logging, log

configure_logging()

def env_number(name, default, minimum, maximum):
    raw = os.environ.get(name, default)
    try:
        value = float(raw)
    except ValueError:
        value = float("nan")
    if not minimum <= value <= maximum:  # também recusa nan
        log("configuração inválida", level=logging.CRITICAL, variable=name, value=raw, minimum=minimum, maximum=maximum)
        sys.exit(2)
    return value

LEASE_SECONDS = env_number("JOB_LEASE_SECONDS", "30", 3, 3600)
WORK_SECONDS = env_number("JOB_WORK_SECONDS", "1", 0, 3600)  # simula trabalho
FAILURE_RATE = env_number("SIMULATED_FAILURE_RATE", "0", 0, 1)  # fração das tentativas que falham de propósito
RENEW_SECONDS = LEASE_SECONDS / 3
CANCEL_CHECK_SECONDS = 1  # com que frequência o worker confere se o job foi cancelado enquanto trabalha
SHUTDOWN_GRACE_SECONDS = 5  # no SIGTERM: termina o job se faltar menos que isso, senão devolve para a fila
TICK_SECONDS = 0.5
POLL_SECONDS = 2
RECOVER_EVERY_SECONDS = 5
# Sem timeout, um detentor de lock travado prenderia o worker (e o job dele) indefinidamente.
DB_OPTIONS = "-c lock_timeout=5s -c idle_in_transaction_session_timeout=60s"
# Erros com a conexão viva que valem nova tentativa: deadlock/serialização, lock_timeout, cancelamento.
TRANSIENT_ERRORS = (errors.TransactionRollback, errors.LockNotAvailable, errors.QueryCanceled)

stopping = False

@dataclass
class Job:
    id: int
    company_id: int
    kind: str
    attempt: int
    request_id: str | None  # da requisição que criou o job: liga este log ao log da API

    def log(self, msg, level=logging.INFO, exc_info=None, **fields):
        log(msg, level=level, exc_info=exc_info, job_id=self.id, company_id=self.company_id, attempt=self.attempt, request_id=self.request_id, **fields)

class LostJob(Exception):
    """Esta tentativa não é mais a dona do job (cancelado, lease vencido e re-enfileirado, ou já finalizado)."""
    def __init__(self, status=None):
        super().__init__(status or "desconhecido")
        self.status = status

class QuotaExhausted(Exception):
    pass

class Shutdown(Exception):
    pass

def request_stop(signum, frame):
    global stopping
    stopping = True

# Toda mudança de estado grava o evento correspondente em job_events na mesma instrução ou transação,
# então a linha do tempo nunca diverge do status do job.

def claim(conn):
    # SELECT + UPDATE numa única instrução, com SKIP LOCKED: dois workers nunca pegam o mesmo job.
    # O prazo do lease fica gravado na linha, então réplicas com JOB_LEASE_SECONDS diferentes não roubam jobs vivos.
    row = conn.execute(
        """WITH claimed AS (
             UPDATE jobs SET status='running', attempts=attempts+1, started_at=now(), updated_at=now(),
                    lease_expires_at = now() + make_interval(secs => %(lease)s)
             WHERE id = (SELECT id FROM jobs WHERE status='queued' ORDER BY id FOR UPDATE SKIP LOCKED LIMIT 1)
             RETURNING id, company_id, kind, attempts, request_id
           ), logged AS (
             INSERT INTO job_events (job_id, event, attempt, request_id) SELECT id, 'claimed', attempts, request_id FROM claimed
           )
           SELECT id, company_id, kind, attempts, request_id FROM claimed""",
        {"lease": LEASE_SECONDS},
    ).fetchone()
    return Job(*row) if row else None

def renew_lease(conn, job):
    cur = conn.execute(
        "UPDATE jobs SET lease_expires_at = now() + make_interval(secs => %s) WHERE id=%s AND status='running' AND attempts=%s",
        (LEASE_SECONDS, job.id, job.attempt),
    )
    return cur.rowcount == 1

def still_ours(conn, job):
    # Leitura barata (por chave primária, sem escrever) para perceber cancelamento no meio do trabalho.
    return conn.execute(
        "SELECT 1 FROM jobs WHERE id=%s AND status='running' AND attempts=%s", (job.id, job.attempt)
    ).fetchone() is not None

def current_status(conn, job):
    row = conn.execute("SELECT status FROM jobs WHERE id=%s", (job.id,)).fetchone()
    return row[0] if row else None

def work(conn, job):
    start = time.monotonic()
    next_renewal, next_check = start + RENEW_SECONDS, start + CANCEL_CHECK_SECONDS
    while (remaining := start + WORK_SECONDS - time.monotonic()) > 0:
        if stopping and remaining > SHUTDOWN_GRACE_SECONDS:
            raise Shutdown
        time.sleep(min(remaining, TICK_SECONDS))
        now = time.monotonic()
        if now >= next_renewal:  # renovar o lease também prova que o job ainda é desta tentativa
            if not renew_lease(conn, job):
                raise LostJob(current_status(conn, job))
            next_renewal, next_check = now + RENEW_SECONDS, now + CANCEL_CHECK_SECONDS
        elif now >= next_check:
            # Cooperação com o cancelamento: para o trabalho assim que o job deixa de ser desta tentativa.
            if not still_ours(conn, job):
                raise LostJob(current_status(conn, job))
            next_check = now + CANCEL_CHECK_SECONDS
    if random.random() < FAILURE_RATE:
        raise RuntimeError("falha simulada (SIMULATED_FAILURE_RATE)")
    return f"resultado sensível da empresa {job.company_id}"

def finish(conn, job, payload):
    # Resultado, cobrança, status e evento numa transação, e só se o job ainda é desta tentativa (status + attempts):
    # uma tentativa antiga ou repetida não grava resultado nem cobra cota. Ordem de locks: job -> empresa.
    with conn.transaction():
        done = conn.execute(
            """UPDATE jobs SET status='done', finished_at=now(), lease_expires_at=NULL, last_error=NULL, updated_at=now()
               WHERE id=%s AND status='running' AND attempts=%s""",
            (job.id, job.attempt),
        ).rowcount
        if not done:
            raise LostJob(current_status(conn, job))
        if not conn.execute("UPDATE companies SET job_quota = job_quota - 1 WHERE id=%s AND job_quota > 0", (job.company_id,)).rowcount:
            raise QuotaExhausted
        conn.execute("INSERT INTO job_results (job_id, payload) VALUES (%s, %s)", (job.id, payload))
        conn.execute(
            "INSERT INTO job_events (job_id, event, attempt, request_id) VALUES (%s, 'completed', %s, %s)",
            (job.id, job.attempt, job.request_id),
        )

def finish_with_retry(conn, job, payload):
    # finish é idempotente pelo fencing, então repetir depois de deadlock ou lock_timeout é seguro.
    for attempt in range(1, 4):
        try:
            return finish(conn, job, payload)
        except TRANSIENT_ERRORS:
            if conn.broken or attempt == 3:
                raise
            time.sleep(0.2 * attempt)

def fail(conn, job, error):
    return conn.execute(
        """WITH failed AS (
             UPDATE jobs SET status='failed', finished_at=now(), lease_expires_at=NULL, last_error=%(error)s, updated_at=now()
             WHERE id=%(id)s AND status='running' AND attempts=%(attempt)s
             RETURNING id, attempts, request_id
           )
           INSERT INTO job_events (job_id, event, attempt, request_id, detail)
           SELECT id, 'failed', attempts, request_id, %(error)s FROM failed""",
        {"error": error, "id": job.id, "attempt": job.attempt},
    ).rowcount == 1

def release(conn, job, reason):
    # Devolve o job agora em vez de esperar o lease vencer: mesma regra do reaper, condicionada a esta tentativa.
    row = conn.execute(
        """WITH released AS (
             UPDATE jobs SET status = CASE WHEN attempts < max_attempts THEN 'queued' ELSE 'failed' END,
                    finished_at = CASE WHEN attempts < max_attempts THEN NULL ELSE now() END,
                    lease_expires_at = NULL, last_error = %(reason)s, updated_at = now()
             WHERE id=%(id)s AND status='running' AND attempts=%(attempt)s
             RETURNING id, attempts, request_id, status
           ), logged AS (
             INSERT INTO job_events (job_id, event, attempt, request_id, detail)
             SELECT id, CASE status WHEN 'queued' THEN 'released' ELSE 'failed' END, attempts, request_id, %(reason)s FROM released
           )
           SELECT status FROM released""",
        {"reason": reason, "id": job.id, "attempt": job.attempt},
    ).fetchone()
    return row[0] if row else None

def persist(conn, job, action, detail):
    # Grava o desfecho do job. Se a própria gravação falhar, o motivo original ainda fica registrado,
    # com job, tentativa e request_id — senão a causa da falha se perderia.
    try:
        return action(conn, job, detail[:2000])
    except Exception:
        job.log("não foi possível gravar o desfecho do job", level=logging.ERROR, exc_info=True, desfecho=detail)
        raise

def recover_expired(conn):
    # Lease vencido = o worker caiu ou travou no meio do job. Volta para a fila se ainda há tentativas, senão failed.
    # SKIP LOCKED: não espera por (nem trava com) quem estiver atualizando o job neste instante.
    # Sem lease_expires_at = job pego por um worker anterior à migração 002: conta a partir da última atualização.
    rows = conn.execute(
        """WITH expired AS (
             SELECT id FROM jobs
             WHERE status='running'
               AND (lease_expires_at < now() OR (lease_expires_at IS NULL AND updated_at < now() - make_interval(secs => %(lease)s)))
             ORDER BY id FOR UPDATE SKIP LOCKED
           ), recovered AS (
             UPDATE jobs j
             SET status = CASE WHEN j.attempts < j.max_attempts THEN 'queued' ELSE 'failed' END,
                 finished_at = CASE WHEN j.attempts < j.max_attempts THEN NULL ELSE now() END,
                 lease_expires_at = NULL,
                 last_error = 'lease vencido: o worker parou de responder na tentativa ' || j.attempts,
                 updated_at = now()
             FROM expired WHERE j.id = expired.id
             RETURNING j.id, j.company_id, j.kind, j.attempts, j.request_id, j.status, j.last_error
           ), logged AS (
             INSERT INTO job_events (job_id, event, attempt, request_id, detail)
             SELECT id, CASE status WHEN 'queued' THEN 'lease_expired' ELSE 'failed' END, attempts, request_id, last_error FROM recovered
           )
           SELECT id, company_id, kind, attempts, request_id, status FROM recovered""",
        {"lease": LEASE_SECONDS},
    ).fetchall()
    for job_id, company_id, kind, attempt, request_id, status in rows:
        Job(job_id, company_id, kind, attempt, request_id).log(
            "lease vencido: o worker parou de responder", level=logging.WARNING, event="lease_expired", new_status=status
        )

def process_once(conn):
    job = claim(conn)
    if job is None:
        return False
    job.log("job pego", event="claimed")
    started = time.monotonic()
    try:
        finish_with_retry(conn, job, work(conn, job))
        job.log("job concluído", event="completed", duration_ms=round((time.monotonic() - started) * 1000))
    except LostJob as lost:
        if lost.status == "cancelled":
            job.log("job cancelado: trabalho interrompido, nada gravado nem cobrado", level=logging.WARNING, event="cancel_observed")
        else:
            job.log("tentativa não é mais a atual; resultado descartado", level=logging.WARNING, event="lost", job_status=lost.status)
    except Shutdown:
        status = persist(conn, job, release, f"worker encerrado durante a tentativa {job.attempt}")
        job.log("job devolvido: worker encerrando", level=logging.WARNING, event="released" if status else "lost", new_status=status)
    except QuotaExhausted:
        applied = persist(conn, job, fail, "cota de jobs esgotada na conclusão")
        job.log("job falhou: cota esgotada na conclusão", level=logging.ERROR, event="failed" if applied else "lost")
    except Exception as exc:
        # Só a primeira linha vai para o banco: DETAIL/CONTEXT do Postgres (tabelas, ctid, PIDs) ficam no log,
        # que é interno, e não em last_error, que o cliente lê.
        detail = f"{type(exc).__name__}: {(str(exc).splitlines() or [''])[0]}"
        if conn.closed or conn.broken:
            job.log("conexão perdida durante o job; o lease vai devolvê-lo", level=logging.WARNING, event="connection_lost", error=detail)
            raise
        if isinstance(exc, TRANSIENT_ERRORS):
            status = persist(conn, job, release, f"erro transitório do banco na tentativa {job.attempt}: {detail}")
            job.log("job devolvido: erro transitório do banco", level=logging.WARNING, exc_info=True,
                    event="released" if status else "lost", new_status=status, error=detail)
        else:
            applied = persist(conn, job, fail, detail)
            job.log("job falhou", level=logging.ERROR, exc_info=True, event="failed" if applied else "lost", error=detail)
    return True

def main():
    signal.signal(signal.SIGTERM, request_stop)  # o Python é o PID 1: sem handler, o SIGTERM do docker stop é ignorado
    conn, next_recovery = None, 0.0
    while not stopping:
        try:
            if conn is None or conn.closed or conn.broken:
                # autocommit: fora de conn.transaction() cada comando fecha a própria transação, então o
                # worker não fica "idle in transaction" segurando lock em jobs (o que travava ALTER TABLE).
                conn = psycopg.connect(os.environ["DATABASE_URL"], autocommit=True, options=DB_OPTIONS)
                log("worker conectado", lease_seconds=LEASE_SECONDS, work_seconds=WORK_SECONDS, failure_rate=FAILURE_RATE)
            if time.monotonic() >= next_recovery:
                recover_expired(conn)
                next_recovery = time.monotonic() + RECOVER_EVERY_SECONDS
            if not process_once(conn):
                time.sleep(POLL_SECONDS)
        except Exception as exc:
            # Nada aqui derruba o processo (com restart: on-failure viraria crash loop).
            # Conexão perdida: reconecta, e o lease cobre o job que estava em andamento.
            # Erro com a conexão viva (ex.: lock_timeout no claim durante uma migração): tenta de novo nela mesma.
            if conn is None or conn.closed or conn.broken:
                log("conexão com o banco indisponível", level=logging.WARNING, error=str(exc), retry_in_seconds=POLL_SECONDS)
            elif isinstance(exc, TRANSIENT_ERRORS):
                log("erro transitório do banco", level=logging.WARNING, error=str(exc), retry_in_seconds=POLL_SECONDS)
            else:
                log("erro inesperado no loop do worker", level=logging.ERROR, exc_info=True, retry_in_seconds=POLL_SECONDS)
            time.sleep(POLL_SECONDS)
    if conn is not None:
        conn.close()
    log("worker encerrado")

if __name__ == "__main__": main()
