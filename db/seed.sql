INSERT INTO companies (id, name, max_concurrent_jobs, job_quota) VALUES
  (1, 'Acme', 2, 20), (2, 'Globex', 2, 100);
INSERT INTO users (id, company_id, email, role) VALUES
  (1, 1, 'user@acme.test', 'user'), (2, 1, 'admin@acme.test', 'admin'),
  (3, 2, 'user@globex.test', 'user'), (4, 2, 'admin@globex.test', 'admin');
INSERT INTO jobs (company_id, created_by, kind, status)
  SELECT 1, 1, 'report', 'done' FROM generate_series(1, 12);
INSERT INTO jobs (company_id, created_by, kind, status) VALUES
  (1,1,'report','failed'), (1,1,'import','failed'), (1,1,'report','running');
INSERT INTO jobs (company_id, created_by, kind, status)
  SELECT 2, 3, 'report', 'done' FROM generate_series(1, 12);
INSERT INTO job_results (job_id, payload)
  SELECT id, 'resultado sensível da empresa ' || company_id FROM jobs WHERE status='done';
SELECT setval('jobs_id_seq', (SELECT max(id) FROM jobs));
