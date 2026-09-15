#!/usr/bin/env bash
# Verificação caixa-preta do Relay contra o stack rodando (docker compose up).
#
# Cada checagem descreve o comportamento esperado DEPOIS das correções: na base
# original ela documenta o problema (FAIL); depois das correções, comprova (PASS).
#
# Uso: scripts/verify.sh [schema|security|concurrency|trace|perf|all]
#   all (padrão) roda tudo exceto perf, que popula a empresa 1 com 20 mil jobs.
#
# Atenção: cria jobs e dados no banco. Use apenas em ambiente local descartável.
set -uo pipefail

cd "$(dirname "$0")/.."
if [ -f .env ]; then set -a; . ./.env; set +a; fi

API="http://localhost:${API_PORT:-8000}"
BODY='{"kind":"report"}'
PASS=0
FAIL=0

sql() { docker compose exec -T db psql -U relay -d relay -At -v ON_ERROR_STOP=1 -c "$1"; }
http_code() { curl -s -o /dev/null -w '%{http_code}' --max-time 30 "$@"; }

check() { # check <descrição> <esperado> <obtido>
  if [ "$2" = "$3" ]; then
    PASS=$((PASS + 1)); printf '  PASS  %s\n' "$1"
  else
    FAIL=$((FAIL + 1)); printf '  FAIL  %s (esperado: %s | obtido: %s)\n' "$1" "$2" "$3"
  fi
}

# Espera a fila esvaziar e nenhum job estar rodando ativamente (até 60s).
wait_queue() {
  local i=0
  while [ $i -lt 60 ]; do
    [ "$(sql "SELECT count(*) FROM jobs WHERE status='queued' OR (status='running' AND updated_at > now() - interval '30 seconds')")" = "0" ] && return
    sleep 1; i=$((i + 1))
  done
}

# Lê a resposta de /admin/jobs (lista ou {"items": [...]}) e imprime as empresas presentes.
companies_in() {
  python3 -c '
import json, sys
try:
    data = json.load(sys.stdin)
except ValueError:
    print("resposta não-JSON")
    sys.exit()
items = data.get("items", data) if isinstance(data, dict) else data
print(sorted({j["company_id"] for j in items}) if isinstance(items, list) else data)'
}

schema() {
  echo "== Schema e migrações"
  check "migrações versionadas aplicadas (tabela schema_migrations)" 1 \
    "$(sql "SELECT (to_regclass('public.schema_migrations') IS NOT NULL)::int")"
  check "INSERT em companies sem id funciona (sequence do seed ajustada)" ok \
    "$(sql "BEGIN; INSERT INTO companies (name) VALUES ('verify'); ROLLBACK;" >/dev/null 2>&1 && echo ok || echo erro)"
  check "jobs.status restrito por CHECK" 1 \
    "$(sql "SELECT (count(*) > 0)::int FROM pg_constraint WHERE conrelid='jobs'::regclass AND contype='c' AND pg_get_constraintdef(oid) LIKE '%status%'")"
  check "job_results.job_id é único" 1 \
    "$(sql "SELECT (count(*) > 0)::int FROM pg_index i JOIN pg_attribute a ON a.attrelid=i.indrelid AND a.attnum=i.indkey[0] WHERE i.indrelid='job_results'::regclass AND i.indisunique AND i.indnatts=1 AND a.attname='job_id'")"
  check "companies.job_quota não pode ficar negativa (CHECK)" 1 \
    "$(sql "SELECT (count(*) > 0)::int FROM pg_constraint WHERE conrelid='companies'::regclass AND contype='c' AND pg_get_constraintdef(oid) LIKE '%job_quota%'")"
}

security() {
  echo "== Segurança e isolamento entre empresas"
  local job result_job
  job=$(sql "SELECT id FROM jobs WHERE company_id=1 ORDER BY id LIMIT 1")
  result_job=$(sql "SELECT j.id FROM jobs j JOIN job_results r ON r.job_id=j.id WHERE j.company_id=1 ORDER BY j.id LIMIT 1")
  check "GET /jobs/{id} de outra empresa -> 404" 404 "$(http_code "$API/jobs/$job" -H 'X-Auth: 2:user')"
  check "GET /jobs/{id}/result de outra empresa -> 404" 404 "$(http_code "$API/jobs/$result_job/result" -H 'X-Auth: 2:user')"
  check "GET /jobs/{id}/result da própria empresa -> 200" 200 "$(http_code "$API/jobs/$result_job/result" -H 'X-Auth: 1:user')"
  check "GET /admin/jobs como user -> 403" 403 "$(http_code "$API/admin/jobs" -H 'X-Auth: 1:user')"
  check "GET /admin/jobs como admin traz só a própria empresa" "[1]" \
    "$(curl -s --max-time 30 "$API/admin/jobs" -H 'X-Auth: 1:admin' | companies_in)"
  check "X-Auth ausente -> 401" 401 "$(http_code "$API/jobs")"
  check "X-Auth com company_id não numérico -> 401" 401 "$(http_code "$API/jobs" -H 'X-Auth: abc:user')"
  check "X-Auth com empresa inexistente -> 401" 401 "$(http_code "$API/jobs" -H 'X-Auth: 999:user')"
  check "X-Auth com role desconhecida -> 401" 401 "$(http_code "$API/jobs" -H 'X-Auth: 1:superuser')"
  check "POST /jobs com kind vazio -> 422" 422 \
    "$(http_code -X POST "$API/jobs" -H 'X-Auth: 2:user' -H 'Content-Type: application/json' -d '{"kind":""}')"
  check "POST /jobs com quebra de linha no kind (log injection) -> 422" 422 \
    "$(http_code -X POST "$API/jobs" -H 'X-Auth: 2:user' -H 'Content-Type: application/json' -d '{"kind":"report\nINFO: linha forjada"}')"
  check "CORS não libera origem arbitrária" "" \
    "$(curl -s -D - -o /dev/null --max-time 30 "$API/jobs" -H 'X-Auth: 1:user' -H 'Origin: http://evil.test' | tr -d '\r' | grep -i '^access-control-allow-origin' || true)"
  check "CORS libera a origem da web UI" "access-control-allow-origin: http://localhost:${WEB_PORT:-5173}" \
    "$(curl -s -D - -o /dev/null --max-time 30 "$API/jobs" -H 'X-Auth: 1:user' -H "Origin: http://localhost:${WEB_PORT:-5173}" | tr -d '\r' | grep -i '^access-control-allow-origin' || true)"
  check "GET /admin/jobs como admin da empresa 2 traz só a empresa 2" "[2]" \
    "$(curl -s --max-time 30 "$API/admin/jobs" -H 'X-Auth: 2:admin' | companies_in)"
  check "X-Auth com sufixo após a role (1:admin:x) -> 401" 401 "$(http_code "$API/jobs" -H 'X-Auth: 1:admin:x')"
  check "POST /jobs com kind desconhecido -> 422" 422 \
    "$(http_code -X POST "$API/jobs" -H 'X-Auth: 2:user' -H 'Content-Type: application/json' -d '{"kind":"export"}')"

  check "GET /jobs/{id} de outra empresa como admin -> 404" 404 "$(http_code "$API/jobs/$job" -H 'X-Auth: 2:admin')"
  check "GET /jobs/{id}/result de outra empresa como admin -> 404" 404 "$(http_code "$API/jobs/$result_job/result" -H 'X-Auth: 2:admin')"
  check "404 de outra empresa é idêntico ao de id inexistente" \
    "$(curl -s --max-time 30 "$API/jobs/2147483647/result" -H 'X-Auth: 2:user')" \
    "$(curl -s --max-time 30 "$API/jobs/$result_job/result" -H 'X-Auth: 2:user')"
  check "GET /admin/jobs com empresa inexistente -> 401" 401 "$(http_code "$API/admin/jobs" -H 'X-Auth: 999:admin')"
  check "Host desconhecido (DNS rebinding) -> 400" 400 "$(http_code "$API/jobs" -H 'X-Auth: 1:user' -H 'Host: evil.test')"

  local web="http://localhost:${WEB_PORT:-5173}"
  check "preflight do GET da UI (X-Auth) -> 200" "200 $web" "$(preflight "$web" GET x-auth)"
  check "preflight do POST da UI (Content-Type, X-Auth) -> 200" "200 $web" "$(preflight "$web" POST content-type,x-auth)"
  check "preflight da UI aberta por 127.0.0.1 -> 200" "200 http://127.0.0.1:${WEB_PORT:-5173}" \
    "$(preflight "http://127.0.0.1:${WEB_PORT:-5173}" GET x-auth)"
  check "preflight de origem arbitrária -> 400" "400 " "$(preflight http://evil.test POST content-type,x-auth)"

  wait_queue
  check "POST /jobs da UI ({\"kind\":\"report\"}) continua 200" 200 \
    "$(http_code -X POST "$API/jobs" -H 'X-Auth: 2:user' -H 'Content-Type: application/json' -d "$BODY")"
}

# Simula o preflight do navegador; imprime "<status> <access-control-allow-origin>".
preflight() { # preflight <origem> <método> <headers>
  curl -s -o /dev/null -D - --max-time 30 -X OPTIONS "$API/jobs" -H "Origin: $1" \
    -H "Access-Control-Request-Method: $2" -H "Access-Control-Request-Headers: $3" |
    tr -d '\r' | awk 'NR == 1 { code = $2 } tolower($1) == "access-control-allow-origin:" { origin = $2 } END { print code, origin }'
}

concurrency() {
  echo "== Concorrência e cota (Sintoma 2)"
  local max worst=0 accepted=0 active round
  max=$(sql "SELECT max_concurrent_jobs FROM companies WHERE id=2")
  for round in 1 2 3; do
    wait_queue
    accepted=$((accepted + $(seq 20 | xargs -P 20 -I{} curl -s -o /dev/null -w '%{http_code}\n' --max-time 30 -X POST "$API/jobs" \
      -H 'X-Auth: 2:user' -H 'Content-Type: application/json' -d "$BODY" | grep -c '^200$')))
    active=$(sql "SELECT count(*) FROM jobs WHERE company_id=2 AND status IN ('queued','running')")
    [ "$active" -gt "$worst" ] && worst=$active
  done
  check "POSTs simultâneos foram aceitos ao menos uma vez (senão o teste de limite não prova nada)" 1 \
    "$([ "$accepted" -gt 0 ] && echo 1 || echo 0)"
  check "20 POST simultâneos respeitam max_concurrent_jobs (pior de 3 rodadas)" "<= $max" \
    "$([ "$worst" -le "$max" ] && echo "<= $max" || echo "$worst")"

  wait_queue
  max=$(sql "SELECT max_concurrent_jobs FROM companies WHERE id=1")
  curl -s -o /dev/null --max-time 30 -X POST "$API/jobs" -H 'X-Auth: 1:user' -H 'Content-Type: application/json' -d "$BODY" &
  curl -s -o /dev/null --max-time 30 -X POST "$API/jobs" -H 'X-Auth: 1:user' -H 'Content-Type: application/json' -d "$BODY" &
  wait
  active=$(sql "SELECT count(*) FROM jobs WHERE company_id=1 AND status IN ('queued','running')")
  check "repro do KNOWN_ISSUES (2 POST simultâneos na empresa 1) respeita o limite" "<= $max" \
    "$([ "$active" -le "$max" ] && echo "<= $max" || echo "$active")"

  check "nenhum job com mais de um resultado" 0 \
    "$(sql "SELECT count(*) FROM (SELECT job_id FROM job_results GROUP BY job_id HAVING count(*) > 1) d")"
  check "nenhuma empresa com cota negativa" 0 "$(sql "SELECT count(*) FROM companies WHERE job_quota < 0")"

  wait_queue
  sleep 6
  check "worker não mantém transação aberta com a fila vazia" 0 \
    "$(sql "SELECT count(*) FROM pg_stat_activity WHERE datname='relay' AND state LIKE 'idle in transaction%' AND now() - xact_start > interval '5 seconds'")"
}

trace() {
  echo "== Rastreabilidade (Sintoma 3)"
  local rid resp job i
  rid="verify-$$-$(date +%s)"
  wait_queue
  resp=$(curl -s -i --max-time 30 -X POST "$API/jobs" -H 'X-Auth: 2:user' \
    -H 'Content-Type: application/json' -H "X-Request-ID: $rid" -d "$BODY" | tr -d '\r')
  check "POST /jobs devolve o X-Request-ID recebido" "$rid" \
    "$(printf '%s\n' "$resp" | awk 'tolower($1) == "x-request-id:" { print $2 }')"
  job=$(printf '%s\n' "$resp" | tail -n 1 | python3 -c 'import json, sys; print(json.load(sys.stdin).get("id", ""))' 2>/dev/null)
  job=${job:-sem-job}
  for i in $(seq 1 30); do
    [ "$(sql "SELECT status IN ('done', 'failed', 'cancelled') FROM jobs WHERE id::text='$job'")" = "t" ] && break
    sleep 1
  done
  check "log da API contém o request id" 1 \
    "$(docker compose logs --no-color api | grep -c -- "$rid" | awk '{ print ($1 > 0) }')"
  check "log do worker liga o request id ao job $job" 1 \
    "$(docker compose logs --no-color worker | grep -- "$rid" | grep -c -- "$job" | awk '{ print ($1 > 0) }')"
}

perf() {
  echo "== Listagem (Sintoma 1)"
  local n before after t
  n=$(sql "SELECT count(*) FROM jobs WHERE company_id=1")
  if [ "$n" -lt 20000 ]; then
    echo "  populando a empresa 1 com $((20000 - n)) jobs concluídos, cada um com resultado..."
    sql "WITH novos AS (INSERT INTO jobs (company_id, kind, status) SELECT 1, 'report', 'done' FROM generate_series(1, $((20000 - n))) RETURNING id)
         INSERT INTO job_results (job_id, payload) SELECT id, 'resultado sensível da empresa 1' FROM novos" >/dev/null
    sql "ANALYZE" >/dev/null
  fi
  before=$(sql "SELECT seq_scan FROM pg_stat_user_tables WHERE relname='job_results'")
  t=$(curl -s -o /dev/null -w '%{time_total}' --max-time 600 "$API/jobs" -H 'X-Auth: 1:user')
  sleep 11 # estatísticas de outras sessões podem levar até ~10s para aparecer
  after=$(sql "SELECT seq_scan FROM pg_stat_user_tables WHERE relname='job_results'")
  printf '  info  GET /jobs (empresa 1 com %s jobs): %ss\n' "$(sql "SELECT count(*) FROM jobs WHERE company_id=1")" "$t"
  check "GET /jobs não faz seq scan em job_results" 0 "$((after - before))"
  check "GET /jobs responde em menos de 300 ms" 1 "$(python3 -c "print(int($t < 0.3))")"
}

if [ "$(http_code "$API/docs")" != "200" ]; then
  echo "API não responde em $API. Suba o stack com: docker compose up -d --build"
  exit 2
fi

case "${1:-all}" in
  schema) schema ;;
  security) security ;;
  concurrency) concurrency ;;
  trace) trace ;;
  perf) perf ;;
  all) schema; security; concurrency; trace ;;
  *) echo "uso: $0 [schema|security|concurrency|trace|perf|all]"; exit 2 ;;
esac

printf '\n%s PASS, %s FAIL\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ]
