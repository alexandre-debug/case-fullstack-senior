"""Feature A (cancelar) e Feature B (reprocessar), incluindo a corrida com a finalização do worker."""
import threading

import pytest
import worker

from conftest import cota, esperar, estado, eventos, job_em_execucao

from test_concorrencia import em_paralelo


@pytest.mark.empresa(limite=50, cota=100)
def test_cancelar_job_na_fila(db, cliente, empresa):
    job_id = cliente("POST", "/jobs", json={"kind": "report"}).json()["id"]
    db.execute("UPDATE jobs SET status='queued' WHERE id=%s", (job_id,))  # garante o estado, mesmo se o worker pegou

    resposta = cliente("POST", f"/jobs/{job_id}/cancel")

    assert resposta.status_code == 200
    assert resposta.json()["status"] == "cancelled"
    assert cliente("POST", f"/jobs/{job_id}/cancel").status_code == 409, "cancelar de novo deve ser 409"


@pytest.mark.parametrize("status", ["done", "failed", "cancelled"])
def test_cancelar_job_em_estado_terminal_e_409(db, cliente, empresa, status):
    job_id = db.execute(
        "INSERT INTO jobs (company_id, kind, status) VALUES (%s, 'report', %s) RETURNING id", (empresa, status)
    ).fetchone()[0]

    assert cliente("POST", f"/jobs/{job_id}/cancel").status_code == 409
    assert estado(db, job_id)[0] == status


def test_nao_da_para_cancelar_job_de_outra_empresa(db, api, empresa):
    """O alvo precisa ser cancelável: com um job terminal, o 404 viria do estado e não do isolamento."""
    outra = db.execute("INSERT INTO companies (name) VALUES ('vizinha') RETURNING id").fetchone()[0]
    job_id = db.execute(
        "INSERT INTO jobs (company_id, kind, status) VALUES (%s, 'report', 'queued') RETURNING id", (outra,)
    ).fetchone()[0]
    try:
        for papel in ("user", "admin"):  # admin é da empresa, não da plataforma
            resposta = api.post(f"/jobs/{job_id}/cancel", headers={"X-Auth": f"{empresa}:{papel}"})
            assert resposta.status_code == 404, f"{papel} de outra empresa conseguiu agir sobre o job"
        assert estado(db, job_id)[0] == "queued"
    finally:
        db.execute("DELETE FROM jobs WHERE company_id=%s", (outra,))
        db.execute("DELETE FROM companies WHERE id=%s", (outra,))


def test_worker_nao_finaliza_job_cancelado(db, conexao, cliente, empresa):
    """Cancelamento commitado antes: a finalização do worker não grava resultado nem cobra cota."""
    job = job_em_execucao(db, empresa, attempts=1)
    antes = cota(db, empresa)

    assert cliente("POST", f"/jobs/{job.id}/cancel").status_code == 200
    with pytest.raises(worker.LostJob) as erro:
        worker.finish(conexao, job, "resultado")

    assert erro.value.status == "cancelled", "o worker precisa saber que foi cancelamento"
    assert estado(db, job.id) == ("cancelled", 1, 0)
    assert cota(db, empresa) == antes


def test_cancelamento_perde_para_finalizacao_ja_commitada(db, conexao, cliente, empresa):
    """Ordem inversa: o worker terminou primeiro, então o cancelamento responde 409."""
    job = job_em_execucao(db, empresa, attempts=1)
    antes = cota(db, empresa)

    worker.finish(conexao, job, "resultado")
    resposta = cliente("POST", f"/jobs/{job.id}/cancel")

    assert resposta.status_code == 409
    assert estado(db, job.id) == ("done", 1, 1)
    assert antes - cota(db, empresa) == 1


def test_corrida_cancelar_x_finalizar_tem_vencedor_unico(db, conexao, cliente, empresa):
    """As duas operações disputam a mesma linha ao mesmo tempo: uma vence e a outra não deixa efeito."""
    for _ in range(5):
        job = job_em_execucao(db, empresa, attempts=1)
        antes = cota(db, empresa)
        resultado = {}

        def finalizar():
            try:
                worker.finish(conexao, job, "resultado")
                resultado["worker"] = "venceu"
            except worker.LostJob:
                resultado["worker"] = "perdeu"

        thread = threading.Thread(target=finalizar)
        thread.start()
        resultado["cancel"] = cliente("POST", f"/jobs/{job.id}/cancel").status_code
        thread.join()

        status, _, resultados = estado(db, job.id)
        cobrado = antes - cota(db, empresa)
        if resultado["cancel"] == 200:
            assert (status, resultados, cobrado) == ("cancelled", 0, 0)
            assert resultado["worker"] == "perdeu"
            assert "completed" not in eventos(db, job.id)
        else:
            assert resultado["cancel"] == 409
            assert (status, resultados, cobrado) == ("done", 1, 1)
            assert resultado["worker"] == "venceu"


@pytest.mark.empresa(limite=50, cota=100)
def test_retry_concorrente_reprocessa_uma_vez_so(db, cliente, empresa):
    """Duplo clique no Reprocessar: só um retry é aceito e só um evento é gravado."""
    job_id = db.execute(
        """INSERT INTO jobs (company_id, kind, status, attempts, last_error)
           VALUES (%s, 'report', 'failed', 1, 'falha de teste') RETURNING id""",
        (empresa,),
    ).fetchone()[0]

    respostas = em_paralelo(5, lambda _: cliente("POST", f"/jobs/{job_id}/retry").status_code)

    assert sorted(respostas) == [200, 409, 409, 409, 409]
    assert eventos(db, job_id).count("retried") == 1


def test_nao_da_para_reprocessar_job_de_outra_empresa(db, api, empresa):
    outra = db.execute("INSERT INTO companies (name) VALUES ('vizinha-retry') RETURNING id").fetchone()[0]
    job_id = db.execute(
        """INSERT INTO jobs (company_id, kind, status, attempts, last_error)
           VALUES (%s, 'report', 'failed', 1, 'falha de teste') RETURNING id""",
        (outra,),
    ).fetchone()[0]
    try:
        for papel in ("user", "admin"):
            resposta = api.post(f"/jobs/{job_id}/retry", headers={"X-Auth": f"{empresa}:{papel}"})
            assert resposta.status_code == 404, f"{papel} de outra empresa reprocessou o job"
        assert estado(db, job_id)[0] == "failed"
    finally:
        db.execute("DELETE FROM jobs WHERE company_id=%s", (outra,))
        db.execute("DELETE FROM companies WHERE id=%s", (outra,))


@pytest.mark.empresa(limite=50, cota=100)
def test_retry_limpa_o_erro_da_tentativa_anterior(db, cliente, empresa):
    """O job volta para a fila sem o erro antigo; a falha continua registrada na linha do tempo."""
    job_id = db.execute(
        """INSERT INTO jobs (company_id, kind, status, attempts, last_error, started_at, finished_at)
           VALUES (%s, 'report', 'failed', 1, 'falha de teste', now(), now()) RETURNING id""",
        (empresa,),
    ).fetchone()[0]
    db.execute("INSERT INTO job_events (job_id, event, attempt, detail) VALUES (%s, 'failed', 1, 'falha de teste')", (job_id,))

    cliente("POST", f"/jobs/{job_id}/retry")

    detalhe = cliente("GET", f"/jobs/{job_id}").json()
    assert detalhe["last_error"] is None, "a UI mostraria um job enfileirado com o erro da tentativa anterior"
    assert detalhe["started_at"] is None
    assert "failed" in eventos(db, job_id), "a falha precisa continuar registrada na linha do tempo"


@pytest.mark.empresa(limite=50, cota=100)
def test_retry_completo_grava_resultado_e_cobra_uma_vez(db, cliente, empresa):
    """Ciclo falha -> retry -> conclusão: um resultado e uma cobrança no ciclo inteiro."""
    antes = cota(db, empresa)
    job_id = db.execute(
        """INSERT INTO jobs (company_id, kind, status, attempts, last_error)
           VALUES (%s, 'report', 'failed', 1, 'falha de teste') RETURNING id""",
        (empresa,),
    ).fetchone()[0]

    assert cliente("POST", f"/jobs/{job_id}/retry").status_code == 200
    assert esperar(lambda: estado(db, job_id)[0] == "done"), "o worker não reprocessou o job"

    assert estado(db, job_id) == ("done", 2, 1), "o resultado precisa ser gravado uma única vez"
    assert antes - cota(db, empresa) == 1, "a cota precisa ser cobrada uma única vez"


def test_retry_respeita_o_maximo_de_tentativas(db, cliente, empresa):
    job_id = db.execute(
        """INSERT INTO jobs (company_id, kind, status, attempts, max_attempts)
           VALUES (%s, 'report', 'failed', 3, 3) RETURNING id""",
        (empresa,),
    ).fetchone()[0]

    resposta = cliente("POST", f"/jobs/{job_id}/retry")

    assert resposta.status_code == 409
    assert "tentativas" in resposta.json()["detail"]
    assert estado(db, job_id)[0] == "failed"


@pytest.mark.parametrize("status", ["queued", "running", "done", "cancelled"])
def test_retry_so_vale_para_job_failed(db, cliente, empresa, status):
    job_id = db.execute(
        "INSERT INTO jobs (company_id, kind, status) VALUES (%s, 'report', %s) RETURNING id", (empresa, status)
    ).fetchone()[0]

    assert cliente("POST", f"/jobs/{job_id}/retry").status_code == 409
    assert estado(db, job_id)[0] == status


@pytest.mark.empresa(limite=1, cota=100)
def test_retry_nao_fura_o_limite_de_concorrencia(db, cliente, empresa):
    """O retry passa pela mesma admissão de um job novo: sem vaga, o job continua failed."""
    db.execute("INSERT INTO jobs (company_id, kind, status) VALUES (%s, 'report', 'queued')", (empresa,))
    job_id = db.execute(
        """INSERT INTO jobs (company_id, kind, status, attempts, last_error)
           VALUES (%s, 'report', 'failed', 1, 'falha de teste') RETURNING id""",
        (empresa,),
    ).fetchone()[0]

    resposta = cliente("POST", f"/jobs/{job_id}/retry")

    assert resposta.status_code == 429
    assert estado(db, job_id)[0] == "failed", "o 429 precisa desfazer a volta para a fila"


@pytest.mark.empresa(limite=50, cota=0)
def test_retry_sem_cota_e_402(db, cliente, empresa):
    job_id = db.execute(
        """INSERT INTO jobs (company_id, kind, status, attempts, last_error)
           VALUES (%s, 'report', 'failed', 1, 'falha de teste') RETURNING id""",
        (empresa,),
    ).fetchone()[0]

    assert cliente("POST", f"/jobs/{job_id}/retry").status_code == 402
    assert estado(db, job_id)[0] == "failed"
