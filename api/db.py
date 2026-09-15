import os
from psycopg_pool import ConnectionPool

# Pool: sem handshake TCP + autenticação a cada requisição (antes eram duas conexões novas por request, contando a auth).
pool = ConnectionPool(
    os.environ["DATABASE_URL"],
    # connect_timeout: com o banco inacessível, abrir conexão falha em segundos.
    # lock_timeout: um detentor de lock travado vira 503 (main.py), em vez de uma fila infinita de threads.
    kwargs={"connect_timeout": 5, "options": "-c lock_timeout=5s -c idle_in_transaction_session_timeout=15s"},
    min_size=1,
    max_size=int(os.environ.get("DB_POOL_MAX_SIZE", "10")),
    timeout=5,  # espera máxima por uma conexão livre; estourou, vira 503
    check=ConnectionPool.check_connection,  # descarta conexões mortas (ex.: banco reiniciado) antes de entregar
    open=False,
)

def get_conn():
    # No fim do with a conexão volta para o pool: commit se não houve exceção, rollback se houve.
    return pool.connection()
