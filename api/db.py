import os, psycopg
def get_conn():
    # connect_timeout: com o banco inacessível a requisição falha em segundos, em vez de prender uma thread por minutos.
    # lock_timeout: um detentor de lock travado vira 503 (ver main.py), em vez de uma fila infinita de threads.
    return psycopg.connect(
        os.environ["DATABASE_URL"],
        connect_timeout=5,
        options="-c lock_timeout=5s -c idle_in_transaction_session_timeout=15s",
    )
