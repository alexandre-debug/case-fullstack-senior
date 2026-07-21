CREATE TABLE companies (
  id SERIAL PRIMARY KEY,
  name TEXT NOT NULL,
  max_concurrent_jobs INT NOT NULL DEFAULT 2,
  job_quota INT NOT NULL DEFAULT 100
);

CREATE TABLE users (
  id SERIAL PRIMARY KEY,
  company_id INT NOT NULL REFERENCES companies(id),
  email TEXT NOT NULL,
  role TEXT NOT NULL DEFAULT 'user'
);

CREATE TABLE jobs (
  id SERIAL PRIMARY KEY,
  company_id INT NOT NULL REFERENCES companies(id),
  created_by INT REFERENCES users(id),
  kind TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'queued',
  attempts INT NOT NULL DEFAULT 0,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE job_results (
  id SERIAL PRIMARY KEY,
  job_id INT NOT NULL REFERENCES jobs(id),
  payload TEXT NOT NULL
);
