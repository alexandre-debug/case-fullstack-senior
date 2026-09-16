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

# Espera a fila esvaziar e nenhum job estar rodando com lease válido (até 60s).
wait_queue() {
  local i=0
  while [ $i -lt 60 ]; do
    [ "$(sql "SELECT count(*) FROM jobs WHERE status='queued' OR (status='running' AND coalesce(lease_expires_at, updated_at + interval '30 seconds') > now())")" = "0" ] && return
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

# Simula o preflight do navegador; imprime "<status> <access-control-allow-origin>".
preflight() { # preflight <origem> <método> <headers>
  curl -s -o /dev/null -D - --max-time 30 -X OPTIONS "$API/jobs" -H "Origin: $1" \
    -H "Access-Control-Request-Method: $2" -H "Access-Control-Request-Headers: $3" |
    tr -d '\r' | awk 'NR == 1 { code = $2 } tolower($1) == "access-control-allow-origin:" { origin = $2 } END { print code, origin }'
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
  check "job em queued sempre tem tentativas restantes (CHECK)" 1 \
    "$(sql "SELECT (count(*) > 0)::int FROM pg_constraint WHERE conrelid='jobs'::regclass AND contype='c' AND pg_get_constraintdef(oid) LIKE '%attempts < max_attempts%'")"
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
  accepted=$({
    curl -s -o /dev/null -w '%{http_code}\n' --max-time 30 -X POST "$API/jobs" -H 'X-Auth: 1:user' -H 'Content-Type: application/json' -d "$BODY" &
    curl -s -o /dev/null -w '%{http_code}\n' --max-time 30 -X POST "$API/jobs" -H 'X-Auth: 1:user' -H 'Content-Type: application/json' -d "$BODY" &
    wait
  } | grep -c '^200$')
  active=$(sql "SELECT count(*) FROM jobs WHERE company_id=1 AND status IN ('queued','running')")
  check "repro do KNOWN_ISSUES (2 POST simultâneos na empresa 1): ao menos um aceito" 1 \
    "$([ "$accepted" -gt 0 ] && echo 1 || echo 0)"
  check "repro do KNOWN_ISSUES (2 POST simultâneos na empresa 1) respeita o limite" "<= $max" \
    "$([ "$active" -le "$max" ] && echo "<= $max" || echo "$active")"

  check "nenhum job com mais de um resultado" 0 \
    "$(sql "SELECT count(*) FROM (SELECT job_id FROM job_results GROUP BY job_id HAVING count(*) > 1) d")"
  check "nenhuma empresa com cota negativa" 0 "$(sql "SELECT count(*) FROM companies WHERE job_quota < 0")"
  check "nenhum job preso em running com lease vencido há mais de 2 min" 0 \
    "$(sql "SELECT count(*) FROM jobs WHERE status='running' AND coalesce(lease_expires_at, updated_at + interval '30 seconds') < now() - interval '2 minutes'" 2>/dev/null)"

  wait_queue
  local key ids
  key="verify-$$-$(date +%s)"
  ids=$(seq 5 | xargs -P 5 -I{} curl -s -w '\n' --max-time 30 -X POST "$API/jobs" -H 'X-Auth: 2:user' \
    -H 'Content-Type: application/json' -H "Idempotency-Key: $key" -d "$BODY" |
    python3 -c 'import json, sys; print(len({json.loads(l).get("id") for l in sys.stdin if l.strip()}))')
  check "5 POST simultâneos com a mesma Idempotency-Key criam um único job" "1 id, 1 job" \
    "$ids id, $(sql "SELECT count(*) FROM jobs WHERE idempotency_key='$key'" 2>/dev/null) job"
  check "mesma Idempotency-Key com outro kind -> 422" 422 \
    "$(http_code -X POST "$API/jobs" -H 'X-Auth: 2:user' -H 'Content-Type: application/json' -H "Idempotency-Key: $key" -d '{"kind":"import"}')"

  wait_queue
  sql "UPDATE companies SET job_quota = 0 WHERE id=2" >/dev/null
  check "POST /jobs sem cota -> 402" 402 \
    "$(http_code -X POST "$API/jobs" -H 'X-Auth: 2:user' -H 'Content-Type: application/json' -d "$BODY")"
  sql "UPDATE companies SET job_quota = 1000000 WHERE id=2" >/dev/null

  wait_queue
  sleep 6
  check "worker não mantém transação aberta com a fila vazia" 0 \
    "$(sql "SELECT count(*) FROM pg_stat_activity WHERE datname='relay' AND state LIKE 'idle in transaction%' AND now() - xact_start > interval '5 seconds'")"
}

new_job() { # new_job <empresa> -> id
  curl -s --max-time 30 -X POST "$API/jobs" -H "X-Auth: $1:user" -H 'Content-Type: application/json' -d "$BODY" |
    python3 -c 'import json, sys; print(json.load(sys.stdin).get("id", ""))'
}

cancel_call() { # cancel_call <empresa> <job> -> "<status http> <status do job>"
  curl -s -w '|%{http_code}' --max-time 30 -X POST "$API/jobs/$2/cancel" -H "X-Auth: $1:user" | python3 -c '
import json, sys
body, code = sys.stdin.read().rsplit("|", 1)
try:
    status = json.loads(body).get("status", "")
except ValueError:
    status = ""
print(code, status)'
}

wait_status() { # wait_status <job> <status> (até 30s); devolve != 0 no timeout
  local i
  for i in $(seq 1 60); do
    [ "$(sql "SELECT status FROM jobs WHERE id=$1")" = "$2" ] && return 0
    sleep 0.5
  done
  # Sem isto, uma pré-condição não atingida viraria um FAIL enganoso na checagem seguinte.
  check "job $1 chegou a $2 (pré-condição)" "$2" "$(sql "SELECT status FROM jobs WHERE id=$1")"
  return 1
}

job_state() { # job_state <job> -> "<status> <n> resultados"
  sql "SELECT status || ' ' || (SELECT count(*) FROM job_results WHERE job_id=$1) || ' resultados' FROM jobs WHERE id=$1"
}

events_list() {
  python3 -c 'import json, sys; print(" ".join(e["event"] for e in json.load(sys.stdin)))'
}

cancel() {
  echo "== Cancelamento (Feature A)"
  local job done_job quota_before quota_after
  wait_queue
  quota_before=$(sql "SELECT job_quota FROM companies WHERE id=2")

  # Job na fila: com o worker parado, o cancelamento acontece antes de qualquer claim.
  docker compose stop worker >/dev/null 2>&1
  job=$(new_job 2)
  check "cancelar job na fila -> 200 cancelled" "200 cancelled" "$(cancel_call 2 "$job")"
  check "cancelar de novo -> 409" 409 "$(http_code -X POST "$API/jobs/$job/cancel" -H 'X-Auth: 2:user')"
  docker compose start worker >/dev/null 2>&1
  sleep 5
  check "job cancelado na fila não é processado" "cancelled 0 resultados" "$(job_state "$job")"
  check "linha do tempo do job cancelado na fila" "created cancelled" \
    "$(curl -s --max-time 30 "$API/jobs/$job/events" -H 'X-Auth: 2:user' | events_list)"

  # Job rodando: o worker precisa parar o trabalho e não finalizar.
  job=$(new_job 2)
  wait_status "$job" running
  check "cancelar job rodando -> 200 cancelled" "200 cancelled" "$(cancel_call 2 "$job")"
  sleep 6
  check "job cancelado enquanto rodava não vira done nem grava resultado" "cancelled 0 resultados" "$(job_state "$job")"
  check "worker registrou que parou por causa do cancelamento" 1 \
    "$(docker compose logs --no-color --no-log-prefix worker | grep -F "\"job_id\": $job," | grep -c '"event": "cancel_observed"' | awk '{ print ($1 > 0) }')"
  check "linha do tempo do job cancelado rodando" "created claimed cancelled" \
    "$(curl -s --max-time 30 "$API/jobs/$job/events" -H 'X-Auth: 2:user' | events_list)"

  done_job=$(sql "SELECT id FROM jobs WHERE company_id=2 AND status='done' ORDER BY id DESC LIMIT 1")
  check "cancelar job concluído -> 409" 409 "$(http_code -X POST "$API/jobs/$done_job/cancel" -H 'X-Auth: 2:user')"
  check "cancelar job inexistente -> 404" 404 "$(http_code -X POST "$API/jobs/2147483647/cancel" -H 'X-Auth: 2:user')"

  # O alvo cross-tenant precisa ser CANCELÁVEL: com um job terminal, o 404 viria do estado e o teste passaria
  # mesmo que o filtro por empresa fosse removido.
  local alheio
  docker compose stop worker >/dev/null 2>&1
  alheio=$(new_job 1)
  check "cancelar job cancelável de outra empresa -> 404" 404 "$(http_code -X POST "$API/jobs/$alheio/cancel" -H 'X-Auth: 2:user')"
  check "o mesmo como admin da outra empresa -> 404 (admin não é papel de plataforma)" 404 \
    "$(http_code -X POST "$API/jobs/$alheio/cancel" -H 'X-Auth: 2:admin')"
  check "job da outra empresa continua na fila, intacto" queued "$(sql "SELECT status FROM jobs WHERE id=$alheio")"
  check "o dono consegue cancelar o mesmo job" "200 cancelled" "$(cancel_call 1 "$alheio")"

  # Vaga liberada de verdade: encher o limite, confirmar o 429, cancelar um e confirmar que abriu vaga.
  local cheio_a cheio_b
  cheio_a=$(new_job 2); cheio_b=$(new_job 2)
  check "com o limite cheio, novo job -> 429" 429 \
    "$(http_code -X POST "$API/jobs" -H 'X-Auth: 2:user' -H 'Content-Type: application/json' -d "$BODY")"
  cancel_call 2 "$cheio_a" >/dev/null
  check "depois de cancelar um, há vaga para outro job" 200 \
    "$(http_code -X POST "$API/jobs" -H 'X-Auth: 2:user' -H 'Content-Type: application/json' -d "$BODY")"
  sql "UPDATE jobs SET status='cancelled', finished_at=now() WHERE company_id=2 AND status IN ('queued','running')" >/dev/null
  docker compose start worker >/dev/null 2>&1

  quota_after=$(sql "SELECT job_quota FROM companies WHERE id=2")
  check "nenhum job cancelado consumiu cota" 0 "$((quota_before - quota_after))"

  # A corrida do enunciado: cancelar perto do fim da janela de trabalho, várias vezes, aceitando os dois
  # desfechos — mas exigindo que estado e efeitos combinem entre si.
  local rodada codigo estado incoerentes=0 vencedor_cancel=0 vencedor_worker=0
  for rodada in 1 2 3 4 5 6; do
    wait_queue
    job=$(new_job 2)
    wait_status "$job" running
    sleep 2.7  # JOB_WORK_SECONDS=3: o cancelamento chega junto do instante da finalização
    codigo=$(cancel_call 2 "$job" | cut -d' ' -f1)
    wait_job "$job"
    estado=$(sql "SELECT status || ' ' || (SELECT count(*) FROM job_results WHERE job_id=$job) ||
                         (SELECT count(*) FROM job_events WHERE job_id=$job AND event='completed') FROM jobs WHERE id=$job")
    case "$codigo $estado" in
      "200 cancelled 00") vencedor_cancel=$((vencedor_cancel + 1)) ;;  # cancelou: sem resultado, sem conclusão
      "409 done 11") vencedor_worker=$((vencedor_worker + 1)) ;;       # worker ganhou: 1 resultado, 1 conclusão
      *) incoerentes=$((incoerentes + 1)); printf '  info  rodada incoerente: HTTP %s, job %s\n' "$codigo" "$estado" ;;
    esac
  done
  printf '  info  corrida cancelar x finalizar: %s vezes o cancelamento venceu, %s vezes o worker\n' "$vencedor_cancel" "$vencedor_worker"
  check "toda corrida cancelar x finalizar terminou coerente (200=cancelado sem efeito, 409=concluído com efeito)" 0 "$incoerentes"
}

fail_job() { # fail_job <empresa> <request id> -> id de um job em failed (falha simulada com o código real do worker)
  local job
  wait_queue
  docker compose stop worker >/dev/null 2>&1
  job=$(curl -s --max-time 30 -X POST "$API/jobs" -H "X-Auth: $1:user" -H 'Content-Type: application/json' \
    -H "X-Request-ID: $2" -d "$BODY" | python3 -c 'import json, sys; print(json.load(sys.stdin).get("id", ""))')
  docker compose run --rm --no-deps -T -e SIMULATED_FAILURE_RATE=1 -e JOB_WORK_SECONDS=0 worker \
    python -c 'import os, psycopg, worker; worker.process_once(psycopg.connect(os.environ["DATABASE_URL"], autocommit=True))' >/dev/null 2>&1
  docker compose start worker >/dev/null 2>&1
  echo "$job"
}

retry() {
  echo "== Reprocessamento (Feature B)"
  local job outro_a outro_b quota_before respostas
  quota_before=$(sql "SELECT job_quota FROM companies WHERE id=2")
  job=$(fail_job 2 "verify-retry-$$")
  check "job de teste ficou failed com motivo" "failed com last_error" \
    "$(sql "SELECT status || CASE WHEN last_error IS NULL THEN ' sem last_error' ELSE ' com last_error' END FROM jobs WHERE id=$job")"

  # Duplo clique: 5 retries simultâneos e só um pode reprocessar.
  respostas=$(seq 5 | xargs -P 5 -I{} curl -s -o /dev/null -w '%{http_code}\n' --max-time 30 \
    -X POST "$API/jobs/$job/retry" -H 'X-Auth: 2:user' | sort | uniq -c | tr '\n' ' ' | tr -s ' ' | sed 's/^ *//; s/ *$//')
  check "5 retries simultâneos: 1 aceito, 4 recusados" "1 200 4 409" "$respostas"
  check "um único evento retried" 1 "$(sql "SELECT count(*) FROM job_events WHERE job_id=$job AND event='retried'")"
  wait_job "$job"
  check "reprocessamento conclui com um único resultado" "done 1 resultados" "$(job_state "$job")"
  check "cota cobrada uma única vez no ciclo (falha + retry)" 1 \
    "$((quota_before - $(sql "SELECT job_quota FROM companies WHERE id=2")))"
  check "linha do tempo do reprocessamento" "created claimed failed retried claimed completed" \
    "$(curl -s --max-time 30 "$API/jobs/$job/events" -H 'X-Auth: 2:user' | events_list)"
  check "retry de job concluído -> 409" 409 "$(http_code -X POST "$API/jobs/$job/retry" -H 'X-Auth: 2:user')"
  check "retry de job de outra empresa -> 404" 404 "$(http_code -X POST "$API/jobs/$job/retry" -H 'X-Auth: 1:user')"

  # Tentativas esgotadas.
  job=$(fail_job 2 "verify-retry-max-$$")
  sql "UPDATE jobs SET attempts = max_attempts WHERE id=$job" >/dev/null
  check "retry com tentativas esgotadas -> 409" 409 "$(http_code -X POST "$API/jobs/$job/retry" -H 'X-Auth: 2:user')"
  check "job continua failed" failed "$(sql "SELECT status FROM jobs WHERE id=$job")"

  # O retry passa pela mesma admissão de um job novo: limite de concorrência e cota valem igual.
  job=$(fail_job 2 "verify-retry-limite-$$")
  docker compose stop worker >/dev/null 2>&1
  outro_a=$(new_job 2); outro_b=$(new_job 2)
  check "retry com o limite de concorrência cheio -> 429" 429 "$(http_code -X POST "$API/jobs/$job/retry" -H 'X-Auth: 2:user')"
  cancel_call 2 "$outro_a" >/dev/null; cancel_call 2 "$outro_b" >/dev/null
  sql "UPDATE companies SET job_quota = 0 WHERE id=2" >/dev/null
  check "retry sem cota -> 402" 402 "$(http_code -X POST "$API/jobs/$job/retry" -H 'X-Auth: 2:user')"
  sql "UPDATE companies SET job_quota = 1000000 WHERE id=2" >/dev/null
  check "job continua failed depois dos 429/402" failed "$(sql "SELECT status FROM jobs WHERE id=$job")"
  docker compose start worker >/dev/null 2>&1
  check "retry funciona quando há vaga e cota" 200 "$(http_code -X POST "$API/jobs/$job/retry" -H 'X-Auth: 2:user')"
  wait_job "$job"
  check "job reprocessado conclui com um único resultado" "done 1 resultados" "$(job_state "$job")"
}

# Espera um job terminar (até 30s).
wait_job() {
  local i
  for i in $(seq 1 30); do
    [ "$(sql "SELECT status IN ('done', 'failed', 'cancelled') FROM jobs WHERE id::text='$1'")" = "t" ] && return
    sleep 1
  done
}

# Resume GET /jobs/{id}/events como "evento:request_id ...".
events_summary() {
  python3 -c '
import json, sys
try:
    print(" ".join(e["event"] + ":" + str(e["request_id"]) for e in json.load(sys.stdin)))
except Exception as exc:
    print("resposta inválida:", exc)'
}

trace() {
  echo "== Rastreabilidade (Sintoma 3)"
  local rid resp job
  rid="verify-$$-$(date +%s)"
  wait_queue
  resp=$(curl -s -i --max-time 30 -X POST "$API/jobs" -H 'X-Auth: 2:user' \
    -H 'Content-Type: application/json' -H "X-Request-ID: $rid" -d "$BODY" | tr -d '\r')
  check "POST /jobs devolve o X-Request-ID recebido" "$rid" \
    "$(printf '%s\n' "$resp" | awk 'tolower($1) == "x-request-id:" { print $2 }')"
  job=$(printf '%s\n' "$resp" | tail -n 1 | python3 -c 'import json, sys; print(json.load(sys.stdin).get("id", ""))' 2>/dev/null)
  job=${job:-sem-job}
  wait_job "$job"
  check "log da API contém o request id" 1 \
    "$(docker compose logs --no-color api | grep -c -- "$rid" | awk '{ print ($1 > 0) }')"
  check "log do worker liga o request id ao job $job" 1 \
    "$(docker compose logs --no-color worker | grep -- "$rid" | grep -c -- "\"job_id\": $job," | awk '{ print ($1 > 0) }')"
  check "linha do tempo do job: created -> claimed -> completed, com o request id" \
    "created:$rid claimed:$rid completed:$rid" \
    "$(curl -s --max-time 30 "$API/jobs/$job/events" -H 'X-Auth: 2:user' | events_summary)"
  check "API gera X-Request-ID quando o cliente não manda" 1 \
    "$(curl -s -D - -o /dev/null --max-time 30 "$API/jobs?limit=1" -H 'X-Auth: 2:user' | tr -d '\r' | awk 'tolower($1) == "x-request-id:" { print ($2 ~ /^[0-9a-f]+$/ && length($2) == 32) }')"
  check "X-Request-ID inseguro para log é trocado por um gerado" 1 \
    "$(curl -s -D - -o /dev/null --max-time 30 "$API/jobs?limit=1" -H 'X-Auth: 2:user' -H 'X-Request-ID: a b"c' | tr -d '\r' | awk 'tolower($1) == "x-request-id:" { print ($2 ~ /^[0-9a-f]+$/ && length($2) == 32) }')"
  check "logs da API e do worker são JSON, uma linha por evento" ok \
    "$(docker compose logs --no-color --no-log-prefix --tail 100 api worker | python3 -c '
import json, sys
lines = [l for l in sys.stdin if l.strip()]
bad = 0
for l in lines:
    try:
        json.loads(l)
    except ValueError:
        bad += 1
print("ok" if lines and not bad else f"{bad} de {len(lines)} linhas não são JSON")')"

  # Falha controlada: com o worker parado, o código real do worker processa o job com falha simulada.
  local rid_fail job_fail run_log
  rid_fail="verify-falha-$$-$(date +%s)"
  wait_queue
  docker compose stop worker >/dev/null 2>&1
  job_fail=$(curl -s --max-time 30 -X POST "$API/jobs" -H 'X-Auth: 2:user' -H 'Content-Type: application/json' \
    -H "X-Request-ID: $rid_fail" -d "$BODY" | python3 -c 'import json, sys; print(json.load(sys.stdin).get("id", ""))' 2>/dev/null)
  # O worker avulso pega o job mais antigo da fila, então a checagem só vale se o job de teste for o único enfileirado.
  check "job de teste é o único na fila antes da falha controlada" "$job_fail" \
    "$(sql "SELECT coalesce(string_agg(id::text, ','), 'nenhum') FROM jobs WHERE status='queued'")"
  run_log=$(docker compose run --rm --no-deps -T -e SIMULATED_FAILURE_RATE=1 -e JOB_WORK_SECONDS=0 worker \
    python -c 'import os, psycopg, worker; worker.process_once(psycopg.connect(os.environ["DATABASE_URL"], autocommit=True))' 2>&1)
  docker compose start worker >/dev/null 2>&1
  check "job que falhou fica failed e diz por quê (last_error)" "failed com last_error" \
    "$(curl -s --max-time 30 "$API/jobs/${job_fail:-0}" -H 'X-Auth: 2:user' | python3 -c '
import json, sys
d = json.load(sys.stdin)
print(str(d.get("status")) + (" com last_error" if d.get("last_error") else " sem last_error"))')"
  check "linha do tempo da falha: created -> claimed -> failed, com o request id" \
    "created:$rid_fail claimed:$rid_fail failed:$rid_fail" \
    "$(curl -s --max-time 30 "$API/jobs/${job_fail:-0}/events" -H 'X-Auth: 2:user' | events_summary)"
  check "log do worker da falha traz request id, job e erro" 1 \
    "$(printf '%s\n' "$run_log" | grep -F "$rid_fail" | grep -F '"event": "failed"' | grep -c '"error": ' | awk '{ print ($1 > 0) }')"
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
  t=$(curl -s -o /dev/null -w '%{time_total}' --max-time 600 "$API/jobs" -H 'X-Auth: 1:user')
  printf '  info  GET /jobs (empresa 1 com %s jobs): %ss\n' "$(sql "SELECT count(*) FROM jobs WHERE company_id=1")" "$t"

  # Plano da consulta real da listagem. Inspecionar o plano em vez do contador pg_stat_user_tables:
  # aquele é cumulativo e global (autovacuum e outras sessões entram na conta), o que tornava a checagem
  # instável logo após uma carga grande.
  check "GET /jobs varre job_results por índice, não sequencialmente (N+1 eliminado)" "sem seq scan" \
    "$(sql "EXPLAIN (FORMAT JSON) SELECT j.id, j.company_id, j.kind, j.status, j.created_at, j.attempts,
              j.max_attempts, j.last_error, (SELECT count(*) FROM job_results r WHERE r.job_id = j.id)
            FROM jobs j WHERE j.company_id = 1 ORDER BY j.created_at DESC, j.id DESC LIMIT 51" |
      python3 -c '
import json, sys

def varreduras(no):
    yield no.get("Node Type", "") + " em " + no.get("Relation Name", "")
    for filho in no.get("Plans", []):
        yield from varreduras(filho)

plano = json.load(sys.stdin)[0]["Plan"]
ruins = [v for v in varreduras(plano) if v == "Seq Scan em job_results"]
print(ruins[0] if ruins else "sem seq scan")')"
  check "GET /jobs responde em menos de 300 ms" 1 "$(python3 -c "print(int($t < 0.3))")"

  # Percorre todas as páginas: nenhum job repetido ou pulado, mesmo com milhares de created_at iguais.
  local walk n_seen n_unique pages slowest total
  walk=$(python3 - "$API" <<'EOF'
import json, sys, time, urllib.parse, urllib.request
api, seen, cursor, pages, slowest = sys.argv[1], [], None, 0, 0.0
while True:
    url = f"{api}/jobs?limit=200" + (f"&cursor={urllib.parse.quote(cursor)}" if cursor else "")
    started = time.monotonic()
    page = json.load(urllib.request.urlopen(urllib.request.Request(url, headers={"X-Auth": "1:user"})))
    slowest, pages = max(slowest, time.monotonic() - started), pages + 1
    seen += [job["id"] for job in page["items"]]
    cursor = page["next_cursor"]
    if not cursor:
        break
print(len(seen), len(set(seen)), pages, f"{slowest:.3f}")
EOF
)
  read -r n_seen n_unique pages slowest <<< "${walk:-0 0 0 99}"
  total=$(sql "SELECT count(*) FROM jobs WHERE company_id=1")
  printf '  info  %s páginas de até 200 jobs; página mais lenta: %ss\n' "$pages" "$slowest"
  check "paginação percorre todos os jobs da empresa, sem repetir nem pular" "$total vistos, $total únicos" "$n_seen vistos, $n_unique únicos"
  check "nenhuma página (inclusive as últimas) passa de 300 ms" 1 "$(python3 -c "print(int($slowest < 0.3))")"
  check "limit acima do máximo -> 422" 422 "$(http_code "$API/jobs?limit=1000" -H 'X-Auth: 1:user')"
  check "cursor inválido -> 422" 422 "$(http_code "$API/jobs?cursor=lixo" -H 'X-Auth: 1:user')"
}

if [ "$(http_code "$API/docs")" != "200" ]; then
  echo "API não responde em $API. Suba o stack com: docker compose up -d --build"
  exit 2
fi

# O verify não pode gastar a cota real das empresas: fixa uma cota alta durante a execução e restaura no fim.
QUOTAS=$(sql "SELECT string_agg(id || ':' || job_quota, ' ') FROM companies WHERE id IN (1, 2)")
restore_quotas() { for pair in $QUOTAS; do sql "UPDATE companies SET job_quota = ${pair#*:} WHERE id = ${pair%%:*}" >/dev/null; done; }
# Algumas checagens param o worker de propósito; o trap garante que ele volte mesmo se o script for interrompido.
trap 'restore_quotas; docker compose start worker >/dev/null 2>&1' EXIT
sql "UPDATE companies SET job_quota = 1000000 WHERE id IN (1, 2)" >/dev/null

case "${1:-all}" in
  schema) schema ;;
  security) security ;;
  concurrency) concurrency ;;
  cancel) cancel ;;
  retry) retry ;;
  trace) trace ;;
  perf) perf ;;
  all) schema; security; concurrency; cancel; retry; trace ;;
  *) echo "uso: $0 [schema|security|concurrency|cancel|retry|trace|perf|all]"; exit 2 ;;
esac

printf '\n%s PASS, %s FAIL\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ]
