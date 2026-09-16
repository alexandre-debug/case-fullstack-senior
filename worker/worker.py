import os, time, psycopg
def process_once(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT id, company_id, kind FROM jobs WHERE status='queued' ORDER BY id LIMIT 1")
        row = cur.fetchone()
        if not row: return False
        job_id, company_id, kind = row
        cur.execute("UPDATE jobs SET status='running', attempts=attempts+1, updated_at=now() WHERE id=%s", (job_id,))
        conn.commit()
        print(f"processando {job_id}")
        time.sleep(1)  # simula trabalho
        cur.execute("INSERT INTO job_results (job_id, payload) VALUES (%s,%s)", (job_id, f"resultado sensível da empresa {company_id}"))
        cur.execute("UPDATE companies SET job_quota = job_quota - 1 WHERE id=%s", (company_id,))
        cur.execute("UPDATE jobs SET status='done', updated_at=now() WHERE id=%s", (job_id,))
        conn.commit()
        return True
def main():
    conn = psycopg.connect(os.environ["DATABASE_URL"])
    while True:
        if not process_once(conn): time.sleep(2)
if __name__ == "__main__": main()
