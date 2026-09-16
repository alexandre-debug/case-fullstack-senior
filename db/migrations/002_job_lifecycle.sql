-- Sintoma 2 (job processado duas vezes, cota caindo rápido, limite de concorrência furado)
-- e ciclo de vida dos jobs que o worker precisa para ser seguro sob concorrência e falhas.

-- O worker antigo podia processar o mesmo job duas vezes: mantém só o primeiro resultado de cada job.
DELETE FROM job_results r
USING job_results older
WHERE r.job_id = older.job_id AND r.id > older.id;
ALTER TABLE job_results ADD CONSTRAINT job_results_job_id_key UNIQUE (job_id);

-- O worker antigo decrementava a cota sem checar; cota negativa não tem significado, então vira 0.
UPDATE companies SET job_quota = 0 WHERE job_quota < 0;
ALTER TABLE companies
  ADD CONSTRAINT companies_job_quota_check CHECK (job_quota >= 0),
  -- 0 é válido: empresa pausada (a admissão responde 429).
  ADD CONSTRAINT companies_max_concurrent_jobs_check CHECK (max_concurrent_jobs >= 0);

ALTER TABLE jobs
  ADD COLUMN max_attempts INT NOT NULL DEFAULT 3 CHECK (max_attempts >= 1),
  ADD COLUMN started_at TIMESTAMPTZ,
  ADD COLUMN finished_at TIMESTAMPTZ,
  -- Prazo gravado por quem pegou o job (e renovado enquanto trabalha); vencido = worker caiu ou travou.
  ADD COLUMN lease_expires_at TIMESTAMPTZ,
  ADD COLUMN last_error TEXT,
  ADD COLUMN idempotency_key TEXT,
  ADD CONSTRAINT jobs_status_check CHECK (status IN ('queued', 'running', 'done', 'failed')),
  -- attempts é o token de fencing do worker e só cresce: um job só volta para a fila se ainda tiver tentativas.
  ADD CONSTRAINT jobs_queued_attempts_check CHECK (status <> 'queued' OR attempts < max_attempts);

-- Jobs já em running ganham 30s de lease a partir da última atualização: um órfão (como o do seed)
-- é recuperado assim que o lease vence; um job realmente em execução ainda tem tempo de terminar.
UPDATE jobs SET lease_expires_at = updated_at + interval '30 seconds' WHERE status = 'running';

-- Mesma Idempotency-Key na mesma empresa = mesmo job (duplo clique, retry de rede).
CREATE UNIQUE INDEX jobs_company_idempotency_key_idx ON jobs (company_id, idempotency_key) WHERE idempotency_key IS NOT NULL;
-- Fila do worker e contagem de ativos na admissão.
CREATE INDEX jobs_queued_idx ON jobs (id) WHERE status = 'queued';
CREATE INDEX jobs_company_active_idx ON jobs (company_id) WHERE status IN ('queued', 'running');
-- Jobs em running para o reaper. Por id, e não por lease_expires_at: renovar o lease não altera
-- coluna indexada, então o UPDATE continua HOT.
CREATE INDEX jobs_running_idx ON jobs (id) WHERE status = 'running';
