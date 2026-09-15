import os, random, signal, sys, time, traceback
from dataclasses import dataclass
import psycopg
from psycopg import errors

def env_number(name, default, minimum, maximum):
    raw = os.environ.get(name, default)
    try:
        value = float(raw)
    except ValueError:
        value = float("nan")
    if not minimum <= value <= maximum:  # também recusa nan
        sys.exit(f"configuração inválida: {name}={raw!r} (use um número entre {minimum} e {maximum})")
    return value

LEASE_SECONDS = env_number("JOB_LEASE_SECONDS", "30", 3, 3600)
WORK_SECONDS = env_number("JOB_WORK_SECONDS", "1", 0, 3600)  # simula trabalho
FAILURE_RATE = env_number("SIMULATED_FAILURE_RATE", "0", 0, 1)  # fração das tentativas que falham de propósito
RENEW_SECONDS = LEASE_SECONDS / 3
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

class LostJob(Exception):
    """Esta tentativa não é mais a dona do job (lease vencido e job re-enfileirado, ou já finalizado)."""

class QuotaExhausted(Exception):
    pass

class Shutdown(Exception):
    pass

def request_stop(signum, frame):
    global stopping
    stopping = True

def claim(conn):
    # SELECT + UPDATE numa única instrução, com SKIP LOCKED: dois workers nunca pegam o mesmo job.
    # O prazo do lease fica gravado na linha, então réplicas com JOB_LEASE_SECONDS diferentes não roubam jobs vivos.
    row = conn.execute(
        """UPDATE jobs SET status='running', attempts=attempts+1, started_at=now(), updated_at=now(),
                  lease_expires_at = now() + make_interval(secs => %s)
           WHERE id = (SELECT id FROM jobs WHERE status='queued' ORDER BY id FOR UPDATE SKIP LOCKED LIMIT 1)
           RETURNING id, company_id, kind, attempts""",
        (LEASE_SECONDS,),
    ).fetchone()
    return Job(*row) if row else None

def renew_lease(conn, job):
    cur = conn.execute(
        "UPDATE jobs SET lease_expires_at = now() + make_interval(secs => %s) WHERE id=%s AND status='running' AND attempts=%s",
        (LEASE_SECONDS, job.id, job.attempt),
    )
    return cur.rowcount == 1

def work(conn, job):
    start = time.monotonic()
    next_renewal = start + RENEW_SECONDS
    while (remaining := start + WORK_SECONDS - time.monotonic()) > 0:
        if stopping and remaining > SHUTDOWN_GRACE_SECONDS:
            raise Shutdown
        time.sleep(min(remaining, TICK_SECONDS))
        if time.monotonic() >= next_renewal:
            if not renew_lease(conn, job):
                raise LostJob
            next_renewal = time.monotonic() + RENEW_SECONDS
    if random.random() < FAILURE_RATE:
        raise RuntimeError("falha simulada (SIMULATED_FAILURE_RATE)")
    return f"resultado sensível da empresa {job.company_id}"

def finish(conn, job, payload):
    # Resultado, cobrança e status numa transação, e só se o job ainda é desta tentativa (status + attempts):
    # uma tentativa antiga ou repetida não grava resultado nem cobra cota. Ordem de locks: job -> empresa.
    with conn.transaction():
        done = conn.execute(
            """UPDATE jobs SET status='done', finished_at=now(), lease_expires_at=NULL, last_error=NULL, updated_at=now()
               WHERE id=%s AND status='running' AND attempts=%s""",
            (job.id, job.attempt),
        ).rowcount
        if not done:
            raise LostJob
        if not conn.execute("UPDATE companies SET job_quota = job_quota - 1 WHERE id=%s AND job_quota > 0", (job.company_id,)).rowcount:
            raise QuotaExhausted
        conn.execute("INSERT INTO job_results (job_id, payload) VALUES (%s, %s)", (job.id, payload))

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
    conn.execute(
        """UPDATE jobs SET status='failed', finished_at=now(), lease_expires_at=NULL, last_error=%s, updated_at=now()
           WHERE id=%s AND status='running' AND attempts=%s""",
        (error.replace("\x00", "")[:2000], job.id, job.attempt),
    )

def release(conn, job, reason):
    # Devolve o job agora em vez de esperar o lease vencer: mesma regra do reaper, condicionada a esta tentativa.
    conn.execute(
        """UPDATE jobs SET status = CASE WHEN attempts < max_attempts THEN 'queued' ELSE 'failed' END,
                  finished_at = CASE WHEN attempts < max_attempts THEN NULL ELSE now() END,
                  lease_expires_at = NULL, last_error = %s, updated_at = now()
           WHERE id=%s AND status='running' AND attempts=%s""",
        (reason.replace("\x00", "")[:2000], job.id, job.attempt),
    )

def recover_expired(conn):
    # Lease vencido = o worker caiu ou travou no meio do job. Volta para a fila se ainda há tentativas, senão failed.
    # SKIP LOCKED: não espera por (nem trava com) quem estiver atualizando o job neste instante.
    # Sem lease_expires_at = job pego por um worker anterior à migração 002: conta a partir da última atualização.
    rows = conn.execute(
        """WITH expired AS (
             SELECT id FROM jobs
             WHERE status='running'
               AND (lease_expires_at < now() OR (lease_expires_at IS NULL AND updated_at < now() - make_interval(secs => %s)))
             ORDER BY id FOR UPDATE SKIP LOCKED
           )
           UPDATE jobs j
           SET status = CASE WHEN j.attempts < j.max_attempts THEN 'queued' ELSE 'failed' END,
               finished_at = CASE WHEN j.attempts < j.max_attempts THEN NULL ELSE now() END,
               lease_expires_at = NULL,
               last_error = 'lease vencido: o worker parou de responder na tentativa ' || j.attempts,
               updated_at = now()
           FROM expired WHERE j.id = expired.id
           RETURNING j.id, j.status""",
        (LEASE_SECONDS,),
    ).fetchall()
    for job_id, status in rows:
        print(f"job {job_id}: lease vencido, agora {status}")

def process_once(conn):
    job = claim(conn)
    if job is None:
        return False
    print(f"processando job {job.id} (empresa {job.company_id}, tentativa {job.attempt})")
    try:
        finish_with_retry(conn, job, work(conn, job))
        print(f"job {job.id} concluído")
    except LostJob:
        print(f"job {job.id}: a tentativa {job.attempt} não é mais a atual; resultado descartado")
    except Shutdown:
        release(conn, job, f"worker encerrado durante a tentativa {job.attempt}")
        print(f"job {job.id} devolvido: worker encerrando")
    except QuotaExhausted:
        fail(conn, job, "cota de jobs esgotada na conclusão")
        print(f"job {job.id} falhou: cota esgotada")
    except Exception as exc:
        if conn.broken:
            raise  # conexão perdida: o lease vence e o job é recuperado
        if isinstance(exc, TRANSIENT_ERRORS):
            release(conn, job, f"erro transitório do banco na tentativa {job.attempt}: {exc}")
            print(f"job {job.id} devolvido: {exc}")
        else:
            fail(conn, job, f"{type(exc).__name__}: {exc}")
            print(f"job {job.id} falhou:\n{traceback.format_exc()}")
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
                print("worker conectado")
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
                print(f"conexão com o banco indisponível ({exc}); tentando de novo em {POLL_SECONDS}s")
            elif isinstance(exc, TRANSIENT_ERRORS):
                print(f"erro transitório do banco ({exc}); tentando de novo em {POLL_SECONDS}s")
            else:
                print(f"erro inesperado; tentando de novo em {POLL_SECONDS}s\n{traceback.format_exc()}")
            time.sleep(POLL_SECONDS)
    if conn is not None:
        conn.close()
    print("worker encerrado")

if __name__ == "__main__": main()
