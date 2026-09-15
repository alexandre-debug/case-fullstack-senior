#!/bin/sh
# Aplica db/migrations/NNN_descricao.sql em ordem, uma única vez cada, registrando em schema_migrations.
#
# schema.sql + seed.sql (docker-entrypoint-initdb.d) são a base e só rodam num volume vazio;
# as migrações valem tanto para um banco novo quanto para um já existente.
#
# Convenções e limites:
# - cada arquivo roda numa única transação com lock_timeout de 10s: a subida falha rápido em vez
#   de travar, e uma falha no meio desfaz o arquivo inteiro;
# - por isso não use BEGIN/COMMIT nos arquivos, e comandos que não rodam em transação
#   (ex.: CREATE INDEX CONCURRENTLY) não são suportados;
# - não há lock entre execuções simultâneas: se duas rodarem juntas, uma falha e faz rollback.
set -eu
export PGOPTIONS="-c client_min_messages=warning"

tries=0
until pg_isready -q; do
  tries=$((tries + 1))
  if [ "$tries" -ge 60 ]; then
    echo "migrate: banco inacessível em ${PGHOST:-?} após 60s" >&2
    exit 1
  fi
  sleep 1
done

psql -X -q -v ON_ERROR_STOP=1 -c "CREATE TABLE IF NOT EXISTS schema_migrations (
  version TEXT PRIMARY KEY,
  applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
)"

for file in /db/migrations/*.sql; do
  [ -e "$file" ] || continue
  version=$(basename "$file" .sql)
  case "$version" in
    ??? | [!0-9]* | ?[!0-9]* | ??[!0-9]* | ???[!_]* | *[!abcdefghijklmnopqrstuvwxyz0123456789_]*)
      echo "migrate: nome inválido '$file' (use NNN_descricao.sql com a-z, 0-9 e _)" >&2
      exit 1
      ;;
  esac

  # Atribuição separada: com set -e, uma falha do psql aqui aborta em vez de parecer "pendente".
  applied=$(psql -X -At -v ON_ERROR_STOP=1 -c "SELECT 1 FROM schema_migrations WHERE version = '$version'")
  if [ -n "$applied" ]; then
    continue
  fi

  echo "migrate: aplicando $version"
  psql -X -q -o /dev/null -v ON_ERROR_STOP=1 --single-transaction \
    -c "SET LOCAL lock_timeout = '10s'" \
    -f "$file" \
    -c "INSERT INTO schema_migrations (version) VALUES ('$version')"
done

echo "migrate: banco em dia"
