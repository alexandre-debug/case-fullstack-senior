import os, psycopg
def get_conn():
    # connect_timeout: com o banco inacessível a requisição falha em segundos, em vez de prender uma thread por minutos.
    return psycopg.connect(os.environ["DATABASE_URL"], connect_timeout=5)
