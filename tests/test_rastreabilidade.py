"""Sintoma 3: ligar a submissão na API ao processamento no worker e saber por que um job falhou."""
import pytest
import worker

from conftest import esperar, estado, eventos, job_em_execucao


@pytest.mark.empresa(limite=50, cota=100)
def test_request_id_do_cliente_e_devolvido_e_gravado(db, cliente, empresa):
    request_id = f"teste-{empresa}"

    resposta = cliente("POST", "/jobs", json={"kind": "report"}, headers={"X-Request-ID": request_id})
    job_id = resposta.json()["id"]

    assert resposta.headers["X-Request-ID"] == request_id
    assert db.execute("SELECT request_id FROM jobs WHERE id=%s", (job_id,)).fetchone()[0] == request_id
    detalhe = cliente("GET", f"/jobs/{job_id}").json()
    assert detalhe["request_id"] == request_id


def test_request_id_e_gerado_quando_o_cliente_nao_manda(cliente):
    resposta = cliente("GET", "/jobs", params={"limit": 1})

    gerado = resposta.headers["X-Request-ID"]
    assert len(gerado) == 32 and all(c in "0123456789abcdef" for c in gerado)


def test_request_id_inseguro_e_substituido(cliente):
    """Um id com espaços ou aspas poderia forjar linhas de log."""
    resposta = cliente("GET", "/jobs", params={"limit": 1}, headers={"X-Request-ID": 'a b"c'})

    assert resposta.headers["X-Request-ID"] != 'a b"c'


@pytest.mark.empresa(limite=50, cota=100)
def test_linha_do_tempo_do_caminho_feliz(db, cliente, empresa):
    job_id = cliente("POST", "/jobs", json={"kind": "report"}).json()["id"]

    assert esperar(lambda: estado(db, job_id)[0] == "done"), "o worker não concluiu o job"
    assert eventos(db, job_id) == ["created", "claimed", "completed"]


@pytest.mark.empresa(limite=50, cota=100)
def test_linha_do_tempo_com_falha_e_reprocessamento(db, cliente, empresa):
    job_id = db.execute(
        """INSERT INTO jobs (company_id, kind, status, attempts, last_error)
           VALUES (%s, 'report', 'failed', 1, 'falha de teste') RETURNING id""",
        (empresa,),
    ).fetchone()[0]
    db.execute("INSERT INTO job_events (job_id, event, attempt) VALUES (%s, 'failed', 1)", (job_id,))

    cliente("POST", f"/jobs/{job_id}/retry")

    assert esperar(lambda: estado(db, job_id)[0] == "done"), "o worker não reprocessou o job"
    assert eventos(db, job_id) == ["failed", "retried", "claimed", "completed"]


def test_job_que_falha_registra_o_motivo(db, conexao, cliente, empresa):
    job = job_em_execucao(db, empresa, attempts=1)

    worker.fail(conexao, job, "ConnectionError: destino recusou a conexão")

    detalhe = cliente("GET", f"/jobs/{job.id}").json()
    assert detalhe["status"] == "failed"
    assert detalhe["last_error"] == "ConnectionError: destino recusou a conexão"
    assert eventos(db, job.id) == ["claimed", "failed"]


def test_erro_interno_do_banco_nao_vaza_para_o_cliente(db, conexao, cliente, empresa):
    """O texto do Postgres (tabela, ctid, PIDs) fica no log; o cliente recebe só a primeira linha."""
    job = job_em_execucao(db, empresa, attempts=1)
    erro_cru = 'LockNotAvailable: canceling statement due to lock timeout\nCONTEXT: while updating tuple (0,8) in relation "companies"'

    worker.fail(conexao, job, erro_cru.splitlines()[0])

    last_error = cliente("GET", f"/jobs/{job.id}").json()["last_error"]
    assert "\n" not in last_error
    assert "CONTEXT" not in last_error and "relation" not in last_error


def test_eventos_de_outra_empresa_nao_sao_visiveis(api, db, empresa):
    outra = db.execute("INSERT INTO companies (name) VALUES ('vizinha-eventos') RETURNING id").fetchone()[0]
    job_id = db.execute(
        "INSERT INTO jobs (company_id, kind, status) VALUES (%s, 'report', 'done') RETURNING id", (outra,)
    ).fetchone()[0]
    db.execute("INSERT INTO job_events (job_id, event) VALUES (%s, 'created')", (job_id,))
    try:
        assert api.get(f"/jobs/{job_id}/events", headers={"X-Auth": f"{empresa}:user"}).status_code == 404
    finally:
        db.execute("DELETE FROM jobs WHERE company_id=%s", (outra,))
        db.execute("DELETE FROM companies WHERE id=%s", (outra,))
