-- Sintoma 3: rastrear um job da submissão na API até o processamento (ou a falha) no worker.

-- Id da requisição que criou o job: a API grava, o worker repete em todo log do job.
ALTER TABLE jobs ADD COLUMN request_id TEXT;

-- Linha do tempo do job, gravada na mesma transação de cada mudança de estado (não existe transição sem evento).
-- Eventos: created, claimed, completed, failed, released (devolvido à fila pelo próprio worker) e
-- lease_expired (recuperado pelo reaper). Jobs anteriores a esta migração não têm histórico.
CREATE TABLE job_events (
  id BIGSERIAL PRIMARY KEY,
  job_id INT NOT NULL REFERENCES jobs(id),
  event TEXT NOT NULL,
  attempt INT,
  request_id TEXT,
  detail TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX job_events_job_id_idx ON job_events (job_id, id);
