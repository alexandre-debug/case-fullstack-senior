"""Garantias de concorrência do Sintoma 2: limite de admissão, claim único e cota cobrada uma vez."""
import threading

import pytest
import worker

from conftest import cota, esperar, estado, eventos, job_em_execucao


def em_paralelo(n, funcao):
    """Roda `funcao(i)` em n threads que partem juntas (barreira), devolvendo os resultados em ordem."""
    barreira, resultados = threading.Barrier(n), [None] * n

    def executar(i):
        try:
            barreira.wait()
            resultados[i] = funcao(i)
        except Exception as exc:  # o teste decide o que fazer com a falha
            resultados[i] = exc

    threads = [threading.Thread(target=executar, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return resultados


@pytest.mark.empresa(limite=2, cota=100)
def test_admissao_simultanea_respeita_o_limite(db, cliente, empresa):
    """20 submissões ao mesmo tempo não podem furar max_concurrent_jobs (era o repro do KNOWN_ISSUES)."""
    respostas = em_paralelo(20, lambda _: cliente("POST", "/jobs", json={"kind": "report"}).status_code)

    ativos = db.execute(
        "SELECT count(*) FROM jobs WHERE company_id=%s AND status IN ('queued','running')", (empresa,)
    ).fetchone()[0]
    assert 200 in respostas, "nenhuma submissão foi aceita; o teste não provaria nada"
    assert ativos <= 2, f"{ativos} jobs ativos com limite 2"
    assert set(respostas) <= {200, 429}


@pytest.mark.empresa(limite=50, cota=100)
def test_mesma_idempotency_key_cria_um_job_so(db, cliente, empresa):
    """Duplo clique (ou retry de rede) com a mesma chave devolve sempre o mesmo job."""
    chave = {"Idempotency-Key": f"teste-{empresa}"}
    respostas = em_paralelo(5, lambda _: cliente("POST", "/jobs", json={"kind": "report"}, headers=chave).json())

    ids = {r["id"] for r in respostas}
    criados = db.execute("SELECT count(*) FROM jobs WHERE company_id=%s", (empresa,)).fetchone()[0]
    assert len(ids) == 1, f"a mesma chave gerou os jobs {ids}"
    assert criados == 1


@pytest.mark.empresa(limite=50, cota=100)
def test_claim_nunca_entrega_o_mesmo_job_duas_vezes(db, empresa):
    """Vários workers disputando a fila: cada job é pego por um só (FOR UPDATE SKIP LOCKED).

    Os jobs ficam em 'running' de propósito — devolvê-los à fila permitiria que fossem pegos de novo,
    e o teste deixaria de medir a exclusão mútua.
    """
    import psycopg

    from conftest import DATABASE_URL

    db.execute(
        "INSERT INTO jobs (company_id, kind, status) SELECT %s, 'report', 'queued' FROM generate_series(1, 30)",
        (empresa,),
    )

    def pegar_ate_esvaziar(_):
        pegos, vazios = [], 0
        with psycopg.connect(DATABASE_URL, autocommit=True, options=worker.DB_OPTIONS) as conn:
            while vazios < 3:
                job = worker.claim(conn)
                if job is None:
                    vazios += 1
                    continue
                vazios = 0
                if job.company_id == empresa:  # o worker real também consome desta fila
                    pegos.append(job.id)
        return pegos

    pegos = [job_id for lista in em_paralelo(6, pegar_ate_esvaziar) for job_id in lista]
    assert pegos, "nenhum job foi pego; o teste não provaria nada"
    assert len(pegos) == len(set(pegos)), "o mesmo job foi pego por mais de um worker"

    duplicados = db.execute(
        """SELECT count(*) FROM (
             SELECT job_id, attempt FROM job_events WHERE event='claimed'
               AND job_id IN (SELECT id FROM jobs WHERE company_id=%s)
             GROUP BY 1, 2 HAVING count(*) > 1) d""",
        (empresa,),
    ).fetchone()[0]
    assert duplicados == 0, "a mesma tentativa foi registrada duas vezes"
    assert db.execute(
        "SELECT count(*) FROM jobs WHERE company_id=%s AND attempts > 1", (empresa,)
    ).fetchone()[0] == 0, "nenhum job deveria ter sido pego mais de uma vez"


def test_finalizacao_de_tentativa_antiga_nao_grava_nem_cobra(db, conexao, empresa):
    """Worker zumbi (lease vencido, job já re-entregue) não pode gravar resultado nem consumir cota."""
    job = job_em_execucao(db, empresa, attempts=1)
    antes = cota(db, empresa)
    # O reaper devolve o job e outro worker o pega: agora a tentativa atual é a 2.
    db.execute("UPDATE jobs SET attempts=2 WHERE id=%s", (job.id,))

    with pytest.raises(worker.LostJob):
        worker.finish(conexao, job, "resultado do zumbi")

    assert estado(db, job.id) == ("running", 2, 0)
    assert cota(db, empresa) == antes


def test_finalizacao_repetida_cobra_uma_vez_so(db, conexao, empresa):
    """Re-entrega da mesma tentativa (worker que não viu o commit) não duplica resultado nem cobrança."""
    job = job_em_execucao(db, empresa, attempts=1)
    antes = cota(db, empresa)

    worker.finish(conexao, job, "resultado")
    with pytest.raises(worker.LostJob):
        worker.finish(conexao, job, "resultado")

    assert estado(db, job.id) == ("done", 1, 1)
    assert cota(db, empresa) - antes == -1


@pytest.mark.empresa(limite=2, cota=0)
def test_cota_zerada_impede_a_conclusao_em_vez_de_ficar_negativa(db, conexao, empresa):
    job = job_em_execucao(db, empresa, attempts=1)

    with pytest.raises(worker.QuotaExhausted):
        worker.finish(conexao, job, "resultado")

    assert estado(db, job.id) == ("running", 1, 0), "a transação inteira deve ser desfeita"
    assert cota(db, empresa) == 0, "a cota nunca pode ficar negativa"


def test_lease_vencido_devolve_o_job_para_a_fila(db, conexao, empresa):
    """Worker que caiu no meio do trabalho: o job volta para a fila em vez de ficar preso em running."""
    job = job_em_execucao(db, empresa, attempts=1, lease="-1 minute")

    worker.recover_expired(conexao)

    status, attempts, _ = estado(db, job.id)
    assert (status, attempts) == ("queued", 1)
    assert eventos(db, job.id) == ["claimed", "lease_expired"]


def test_lease_vencido_sem_tentativas_restantes_marca_failed(db, conexao, empresa):
    job = job_em_execucao(db, empresa, attempts=3, lease="-1 minute")  # max_attempts padrão = 3

    worker.recover_expired(conexao)

    status, _, _ = estado(db, job.id)
    assert status == "failed"
    assert db.execute("SELECT last_error FROM jobs WHERE id=%s", (job.id,)).fetchone()[0]


@pytest.mark.empresa(limite=2, cota=100)
def test_ciclo_completo_pela_api_cobra_uma_vez(db, cliente, empresa):
    """Caminho feliz ponta a ponta: o worker real processa e a cota cai exatamente 1."""
    antes = cota(db, empresa)
    job_id = cliente("POST", "/jobs", json={"kind": "report"}).json()["id"]

    assert esperar(lambda: estado(db, job_id)[0] == "done"), "o worker não concluiu o job"
    assert estado(db, job_id) == ("done", 1, 1)
    assert antes - cota(db, empresa) == 1
