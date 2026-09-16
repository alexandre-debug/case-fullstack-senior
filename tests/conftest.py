"""Fixtures dos testes de integração.

Os testes rodam contra o stack de pé (API, worker e banco) e exercitam o código real:
as funções do worker são importadas de /worker, não reimplementadas aqui.

Cada teste usa uma empresa própria, criada e removida na hora, então nada depende do seed
nem atrapalha outro teste — e o worker real, que processa a fila inteira, não interfere nos
casos determinísticos (eles operam sobre jobs já em 'running', que ninguém mais toca).
"""
import os
import sys
import time

import httpx
import psycopg
import pytest

sys.path.insert(0, "/worker")
import worker  # noqa: E402  (precisa do sys.path acima)

DATABASE_URL = os.environ["DATABASE_URL"]
API_URL = os.environ.get("API_URL", "http://api:8000")


@pytest.fixture(scope="session")
def db():
    with psycopg.connect(DATABASE_URL, autocommit=True) as conn:
        yield conn


@pytest.fixture
def conexao():
    """Conexão nova, configurada como a do worker (lock_timeout etc.)."""
    with psycopg.connect(DATABASE_URL, autocommit=True, options=worker.DB_OPTIONS) as conn:
        yield conn


@pytest.fixture
def empresa(db, request):
    """Empresa isolada para o teste. Aceita marcador @pytest.mark.empresa(limite=N, cota=N)."""
    marcador = request.node.get_closest_marker("empresa")
    limite, cota = (marcador.kwargs.get("limite", 2), marcador.kwargs.get("cota", 100)) if marcador else (2, 100)
    company_id = db.execute(
        "INSERT INTO companies (name, max_concurrent_jobs, job_quota) VALUES (%s, %s, %s) RETURNING id",
        (f"teste-{request.node.name}"[:60], limite, cota),
    ).fetchone()[0]
    yield company_id
    # job_events cai por ON DELETE CASCADE (migração 006); job_results precisa ir antes dos jobs.
    db.execute("DELETE FROM job_results WHERE job_id IN (SELECT id FROM jobs WHERE company_id=%s)", (company_id,))
    db.execute("DELETE FROM jobs WHERE company_id=%s", (company_id,))
    db.execute("DELETE FROM companies WHERE id=%s", (company_id,))


@pytest.fixture
def api():
    with httpx.Client(base_url=API_URL, timeout=30, headers={"Host": "localhost"}) as client:
        yield client


@pytest.fixture
def cliente(api, empresa):
    """Cliente HTTP já autenticado como usuário comum da empresa do teste."""

    def request(method, path, **kwargs):
        headers = {"X-Auth": f"{empresa}:user", **kwargs.pop("headers", {})}
        return api.request(method, path, headers=headers, **kwargs)

    return request


def job_em_execucao(db, company_id, attempts=1, lease="1 hour"):
    """Insere um job como se um worker o tivesse acabado de pegar.

    Um job em 'running' com lease válido não é tocado pelo worker real nem pelo reaper,
    então os testes de corrida ficam determinísticos mesmo com o stack rodando.
    """
    job_id = db.execute(
        f"""INSERT INTO jobs (company_id, kind, status, attempts, started_at, lease_expires_at)
            VALUES (%s, 'report', 'running', %s, now(), now() + interval '{lease}') RETURNING id""",
        (company_id, attempts),
    ).fetchone()[0]
    db.execute("INSERT INTO job_events (job_id, event, attempt) VALUES (%s, 'claimed', %s)", (job_id, attempts))
    return worker.Job(job_id, company_id, "report", attempts, None)


def estado(db, job_id):
    return db.execute(
        """SELECT status, attempts, (SELECT count(*) FROM job_results WHERE job_id = j.id)
           FROM jobs j WHERE id = %s""",
        (job_id,),
    ).fetchone()


def cota(db, company_id):
    return db.execute("SELECT job_quota FROM companies WHERE id=%s", (company_id,)).fetchone()[0]


def eventos(db, job_id):
    return [r[0] for r in db.execute("SELECT event FROM job_events WHERE job_id=%s ORDER BY id", (job_id,)).fetchall()]


def esperar(condicao, timeout=30):
    """Espera uma condição ficar verdadeira (o worker é assíncrono); devolve o último valor."""
    limite = time.monotonic() + timeout
    while time.monotonic() < limite:
        valor = condicao()
        if valor:
            return valor
        time.sleep(0.2)
    return condicao()


def pytest_configure(config):
    config.addinivalue_line("markers", "empresa(limite, cota): configura a empresa do teste")
