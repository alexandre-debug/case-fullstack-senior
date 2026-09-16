import os, psycopg
def get_conn():
    return psycopg.connect(os.environ["DATABASE_URL"])
