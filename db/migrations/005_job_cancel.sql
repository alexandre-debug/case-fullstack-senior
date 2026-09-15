-- Feature A: cancelar um job que ainda está na fila ou rodando.
-- job_events ganha o evento 'cancelled' (a tabela não restringe o vocabulário por CHECK).
ALTER TABLE jobs DROP CONSTRAINT jobs_status_check;
ALTER TABLE jobs ADD CONSTRAINT jobs_status_check CHECK (status IN ('queued', 'running', 'done', 'failed', 'cancelled'));
