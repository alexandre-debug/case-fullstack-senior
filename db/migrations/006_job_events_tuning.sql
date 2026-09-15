-- Ajustes de revisão na linha do tempo dos jobs.

-- Sem ON DELETE CASCADE, qualquer expurgo futuro de jobs esbarraria na FK do histórico.
ALTER TABLE job_events DROP CONSTRAINT job_events_job_id_fkey;
ALTER TABLE job_events ADD CONSTRAINT job_events_job_id_fkey FOREIGN KEY (job_id) REFERENCES jobs(id) ON DELETE CASCADE;

-- A única leitura é "eventos do job, em ordem": a chave primária (job_id, id) já atende,
-- e o índice da chave antiga (id) não era usado por nenhuma consulta nem por FK.
ALTER TABLE job_events DROP CONSTRAINT job_events_pkey;
ALTER TABLE job_events ADD PRIMARY KEY (job_id, id);
DROP INDEX job_events_job_id_idx;
