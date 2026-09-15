-- Sintoma 1: listagem por empresa, mais recentes primeiro, paginada por cursor (created_at, id).
-- O índice entrega cada página já ordenada, então o custo depende do tamanho da página, não do total de jobs.
CREATE INDEX jobs_company_created_idx ON jobs (company_id, created_at DESC, id DESC);
