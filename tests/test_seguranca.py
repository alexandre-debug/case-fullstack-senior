"""Isolamento entre empresas, autenticação e validação de entrada."""
import pytest

from conftest import estado


@pytest.fixture
def vizinha(db):
    """Outra empresa, com um job concluído e resultado — o alvo das tentativas de acesso indevido."""
    company_id = db.execute("INSERT INTO companies (name) VALUES ('vizinha') RETURNING id").fetchone()[0]
    job_id = db.execute(
        "INSERT INTO jobs (company_id, kind, status) VALUES (%s, 'report', 'done') RETURNING id", (company_id,)
    ).fetchone()[0]
    db.execute("INSERT INTO job_results (job_id, payload) VALUES (%s, 'resultado sensível da vizinha')", (job_id,))
    yield company_id, job_id
    db.execute("DELETE FROM job_results WHERE job_id=%s", (job_id,))
    db.execute("DELETE FROM jobs WHERE company_id=%s", (company_id,))
    db.execute("DELETE FROM companies WHERE id=%s", (company_id,))


@pytest.mark.parametrize("rota", ["/jobs/{}", "/jobs/{}/result", "/jobs/{}/events"])
@pytest.mark.parametrize("papel", ["user", "admin"])
def test_nao_da_para_ler_job_de_outra_empresa(api, empresa, vizinha, rota, papel):
    _, job_id = vizinha

    resposta = api.get(rota.format(job_id), headers={"X-Auth": f"{empresa}:{papel}"})

    assert resposta.status_code == 404
    assert "sensível" not in resposta.text


def test_404_de_outra_empresa_e_igual_ao_de_id_inexistente(api, empresa, vizinha):
    """Respostas diferentes revelariam que o job existe."""
    _, job_id = vizinha
    cabecalho = {"X-Auth": f"{empresa}:user"}

    alheio = api.get(f"/jobs/{job_id}/result", headers=cabecalho)
    inexistente = api.get("/jobs/2147483647/result", headers=cabecalho)

    assert (alheio.status_code, alheio.json()) == (inexistente.status_code, inexistente.json())


def test_admin_so_enxerga_a_propria_empresa(api, db, empresa, vizinha):
    outra_empresa, _ = vizinha
    db.execute("INSERT INTO jobs (company_id, kind, status) VALUES (%s, 'report', 'done')", (empresa,))

    resposta = api.get("/admin/jobs", headers={"X-Auth": f"{empresa}:admin"})

    assert resposta.status_code == 200
    empresas = {item["company_id"] for item in resposta.json()["items"]}
    assert empresas == {empresa}, f"o admin viu jobs de {empresas}"
    assert outra_empresa not in empresas


def test_admin_exige_papel_admin(api, empresa):
    assert api.get("/admin/jobs", headers={"X-Auth": f"{empresa}:user"}).status_code == 403


@pytest.mark.parametrize(
    "x_auth",
    ["", "abc:user", "999999999:user", "1:hacker", "1:admin:x", "1:ADMIN", "-1:user", "1:", ":user", "1 :user"],
    ids=["vazio", "nao-numerico", "empresa-inexistente", "papel-invalido", "sufixo", "maiusculas", "negativo",
         "sem-papel", "sem-empresa", "espaco-interno"],
)
def test_x_auth_invalido_e_401(api, x_auth):
    """Nenhuma variação pode virar 500 nem passar: antes, 'abc:user' derrubava a requisição.

    Valores que um cliente HTTP conforme nem consegue transmitir (espaço inicial, caracteres não-ASCII)
    são cobertos por scripts/verify.sh, que usa curl.
    """
    cabecalho = {"X-Auth": x_auth} if x_auth else {}

    assert api.get("/jobs", headers=cabecalho).status_code == 401


@pytest.mark.parametrize(
    "kind", ["", "export", "report\nINFO: linha forjada", "a" * 10_000], ids=["vazio", "desconhecido", "quebra-de-linha", "gigante"]
)
def test_kind_invalido_e_422(cliente, kind):
    assert cliente("POST", "/jobs", json={"kind": kind}).status_code == 422


def test_host_desconhecido_e_recusado(api, empresa):
    """Defesa contra DNS rebinding: a API só responde nos hosts declarados."""
    resposta = api.get("/jobs", headers={"X-Auth": f"{empresa}:user", "Host": "evil.test"})

    assert resposta.status_code == 400


def test_cursor_invalido_e_422(cliente):
    for cursor in ("lixo", "", "a" * 500):
        assert cliente("GET", "/jobs", params={"cursor": cursor}).status_code == 422


def test_limite_de_pagina_e_validado(cliente):
    assert cliente("GET", "/jobs", params={"limit": 1000}).status_code == 422
    assert cliente("GET", "/jobs", params={"limit": 0}).status_code == 422
    assert cliente("GET", "/jobs", params={"limit": 200}).status_code == 200
