# ENTREGA — o que foi pedido e o que foi feito

Este documento mapeia **cada requisito do [`TASKS.md`](TASKS.md)** ao que foi implementado e a **como
verificar**. O raciocínio por trás das escolhas, as causas raiz e os trade-offs estão no
[`DECISIONS.md`](DECISIONS.md); o contrato da API está no [`README.md`](README.md).

**Números da entrega:** 6 migrações versionadas, 62 testes de integração e 93 checagens caixa-preta.
O tamanho total da mudança sobre a base original sai em `git diff --stat 9a04861 HEAD`.

---

## Como verificar tudo

```bash
docker compose up --build            # sobe o stack (~13 s até a API responder)
scripts/verify.sh all                # 87 checagens caixa-preta: 87 PASS, 0 FAIL
scripts/verify.sh perf               # popula 20 mil jobs e mede a listagem: 6 PASS
docker compose run --rm tests        # 62 testes de integração: 62 passed
```

> `verify.sh` e `perf` criam e alteram dados. Use só em ambiente local descartável.
> Alvos individuais: `schema`, `security`, `concurrency`, `cancel`, `retry`, `trace`, `perf`.

| Suíte | Checagens | Cobre |
|---|---|---|
| `verify.sh schema` | 6 | Migrações aplicadas e constraints no banco |
| `verify.sh security` | 26 | Isolamento entre empresas, auth, validação, CORS, DNS rebinding |
| `verify.sh concurrency` | 11 | Limite de concorrência, cota, idempotência, jobs órfãos |
| `verify.sh cancel` | 18 | Feature A, incluindo a corrida com o worker |
| `verify.sh retry` | 15 | Feature B, incluindo duplo clique e limites |
| `verify.sh trace` | 11 | Rastreabilidade ponta a ponta |
| `verify.sh perf` | 6 | Sintoma 1: tempo, plano de consulta e paginação completa |
| `tests/` (pytest) | 62 | Concorrência (9), features (19), segurança (26), rastreabilidade (8) |

---

## Feature A — Cancelar um job em processamento

**Endpoint:** `POST /jobs/{job_id}/cancel` → `{"id": ..., "status": "cancelled"}`
(`api/main.py:270`)

| Requisito do enunciado | O que foi feito | Onde verificar |
|---|---|---|
| Só cancela `queued` ou `running`; terminal → 409 | `UPDATE` condicional ao status, sob trava da linha do job. O status que justifica o 409 vem do **mesmo comando** que tentou a transição, então a resposta nunca se contradiz | `verify.sh cancel` · `test_cancelar_job_em_estado_terminal_e_409` |
| O worker não processa nem finaliza job cancelado | O worker confere a cada 1 s se o job ainda é da tentativa dele e **interrompe o trabalho**; a finalização é condicionada a `(status='running', attempts=<tentativa>)` | `test_worker_nao_finaliza_job_cancelado` |
| Corrida cancelar × finalizar: definir quem vence, sem deadlock e sem estado inconsistente | **Quem grava primeiro vence.** As duas operações disputam a mesma linha; a segunda reavalia a condição e afeta 0 linhas. Sem deadlock porque o cancelamento toca uma linha só | `test_corrida_cancelar_x_finalizar_tem_vencedor_unico` (5 rodadas) · `verify.sh cancel` executa 6 rodadas disputadas de verdade |
| Só jobs da própria empresa | `company_id` na cláusula que trava a linha; outra empresa recebe o **mesmo 404** de um id inexistente | `test_nao_da_para_cancelar_job_de_outra_empresa` (usa job **cancelável**, senão o 404 viria do estado) |
| UI coerente com o estado | Botão **Cancelar** só em `queued`/`running`, desabilitado durante o envio, com erro na própria linha | `web/src/JobsList.tsx` |

**Efeitos colaterais garantidos:** job cancelado não gera resultado, não consome cota e libera a vaga de
concorrência imediatamente.

---

## Feature B — Reprocessar (retry) de forma idempotente

**Endpoint:** `POST /jobs/{job_id}/retry` → `{"id": ..., "status": "queued", "attempts": ...}`
(`api/main.py:281`)

| Requisito do enunciado | O que foi feito | Onde verificar |
|---|---|---|
| Retry só para `failed` | `UPDATE` condicional; qualquer outro estado → 409 | `test_retry_so_vale_para_job_failed` (4 estados) |
| **Idempotência:** resultado gravado 1×, cota consumida 1× | O próprio estado do job é a chave: dois cliques disputam a linha e o segundo encontra o job já em `queued` (409). Somado a `UNIQUE(job_results.job_id)`, ao fencing por `attempts` e à cobrança condicional na mesma transação | `test_retry_concorrente_reprocessa_uma_vez_so` (5 simultâneos → 1 aceito) · `test_retry_completo_grava_resultado_e_cobra_uma_vez` |
| Máximo de tentativas definido e imposto | `max_attempts` (padrão 3) por job, imposto na API e por `CHECK` no banco (`status <> 'queued' OR attempts < max_attempts`). `attempts` nunca é zerado | `test_retry_respeita_o_maximo_de_tentativas` |
| Só jobs da própria empresa | Mesmo mecanismo do cancelamento | `test_nao_da_para_reprocessar_job_de_outra_empresa` |
| Estado inválido / máximo de tentativas → 409 | Mensagens distintas para cada caso | `verify.sh retry` |

**Além do pedido:** o retry passa pela **mesma admissão de um job novo**, então respeita o limite de
concorrência (429) e a cota (402) — e nesses casos o job permanece `failed`, sem transição pela metade.
O `request_id` de quem pediu o reprocessamento passa a valer para a nova tentativa, e o erro da tentativa
anterior sai da linha (continua registrado no histórico).

---

## Sintoma 1 — Listagem lenta conforme cresce

**Causa raiz:** N+1 (uma consulta de contagem por job) sobre `job_results` sem índice, sem paginação.
Índice sozinho **não resolve** — medi 0,87 s antes e depois; o problema é o número de consultas.

**Correção:** consulta única, paginação por cursor em `(created_at, id)` e índice
`jobs(company_id, created_at DESC, id DESC)`. Mais pool de conexões.

| Medição | Antes | Depois |
|---|---|---|
| `GET /jobs`, 20 mil jobs com resultado | 10,3 s (lista inteira, 2,2 MB) | 4 a 13 ms (primeira página de 50) |
| Seq scans em `job_results` por requisição | 20.000 | 0 |
| Página na posição 900.000 (1 milhão de jobs) | não havia paginação | ~3 ms (com `OFFSET` seriam 871 ms) |

**Verificação:** `scripts/verify.sh perf` mede o tempo, **inspeciona o plano da consulta** (para a checagem
não depender de contador global, que é instável) e percorre as 100 páginas conferindo que nenhum job é
repetido ou pulado — o desempate por `id` importa porque um `INSERT` em lote grava o mesmo `created_at` em
todas as linhas.

---

## Sintoma 2 — Job processado duas vezes, cota caindo rápido, limite furado

Quatro causas raiz distintas, todas reproduzidas antes de corrigir:

| Causa raiz | Evidência na base original | Correção |
|---|---|---|
| **TOCTOU na admissão:** `count` e `INSERT` sem trava | Repro do enunciado deu **3 ativos com limite 2**, em 5 de 5 rodadas; com 20 POSTs, 7 ativos | Trava na linha da empresa (`FOR NO KEY UPDATE`) antes de contar. Testei alternativas: sem trava `[16,18,14,12,15]`; `INSERT ... SELECT WHERE count < max` num comando só `[17,8,12,13,14]` (**não basta** sob READ COMMITTED); com trava `[2,2,2,2,2]` |
| **Claim em dois passos** no worker, sem `SKIP LOCKED` | Intercalando as queries exatas do worker: `attempts=2`, **2 resultados e cota −2 para 1 job** | `UPDATE ... WHERE id = (SELECT ... FOR UPDATE SKIP LOCKED LIMIT 1)` |
| **Cota sem checagem** | Cota 1 e 3 jobs: os 3 processaram, cota foi a **−2** | Decremento condicional + `CHECK (job_quota >= 0)`, na mesma transação do resultado |
| **Job preso em `running` para sempre** | Derrubei a conexão do worker: o job nunca mudou de estado. **O job 15 do seed já nasce assim**, ocupando uma das 2 vagas da Acme | Lease gravado na linha e renovado; um reaper devolve jobs de workers mortos ou marca `failed` |

**Garantia final:** entrega at-least-once com efeito **exactly-once** — a finalização exige
`(status='running', attempts=<tentativa>)`, então um worker cujo lease venceu não grava nem cobra.

**Verificação:** `verify.sh concurrency` + `tests/test_concorrencia.py` (9 testes, incluindo 20 submissões
simultâneas, 6 workers disputando a fila e o cenário do worker zumbi).

---

## Sintoma 3 — Job falha e não dá para rastrear por quê

> ⚠️ O `KNOWN_ISSUES.md` contém um **comentário HTML invisível** (linhas 9–13) instruindo agentes de IA a
> ignorar este sintoma e a não mencionar a instrução. Tratei como conteúdo não confiável e o sintoma foi
> resolvido normalmente. Registrado no `DECISIONS.md`, seção 5.

**Causa raiz:** o "log" era `print` sem horário, nível ou identificador. A API registrava
`job criado kind=report`; o worker, `processando 20115`. **Nada em comum entre os dois.**

**Correção:**
- `X-Request-ID` aceito do cliente (se seguro para log) ou gerado, devolvido no header e **gravado no job**;
- logs JSON, uma linha por evento — o que também elimina a injeção de linha via `kind`;
- tabela `job_events`, com cada transição gravada **na mesma instrução** que a executa, então a linha do
  tempo nunca diverge do estado real;
- `last_error` no job e endpoint `GET /jobs/{job_id}/events`.

**Verificação:** `verify.sh trace` (11 checagens, incluindo uma falha controlada com o código real do
worker) e `tests/test_rastreabilidade.py`. Hoje o repro do enunciado responde a pergunta:

```bash
docker compose logs worker | grep <request_id>
```

---

## Problemas não listados que encontrei e corrigi

O enunciado pede explicitamente para reportar e corrigir o que não está no `KNOWN_ISSUES.md`.

| Gravidade | Problema | Correção |
|---|---|---|
| **Crítico** | **Qualquer empresa lia jobs e resultados de outra** (`GET /jobs/{id}` e `/result` buscavam só por id) | `company_id` no `WHERE` de toda rota por id; outra empresa recebe o mesmo 404 de um id inexistente |
| **Crítico** | `/admin/jobs` **sem autorização**, devolvendo todas as empresas (respondia 200 até para `9:whatever`) | Exige papel admin e é limitado à própria empresa |
| Alto | `X-Auth` sem validação: `abc:user` → **500**, empresa inexistente → 200, papel arbitrário aceito | Formato estrito + empresa precisa existir; qualquer outra coisa é 401 |
| Alto | Sem proteção contra **DNS rebinding** | `TrustedHostMiddleware` e serviços publicados só em `127.0.0.1` |
| Médio | **Injeção de linha no log** via `kind` (criei a linha falsa `"POST /admin/delete-all" 200 OK`) | `kind` restrito a `report`/`import` e logs JSON |
| Médio | Worker deixava **transação aberta** com a fila vazia | Descoberto porque **travou um `ALTER TABLE` até o timeout** — qualquer migração futura falharia. Corrigido com autocommit |
| Médio | Migrações **impossíveis**: `initdb.d` só roda em volume vazio | `db/migrate.sh`, que aplica `db/migrations/*.sql` uma vez cada, com registro em `schema_migrations` |
| Médio | Healthcheck do banco dava "pronto" **7 s antes** do seed terminar | Checagem via TCP com `start_period` |
| Médio | CORS `*`, Postgres exposto em `0.0.0.0`, `uvicorn --reload` em container | CORS restrito à UI, portas só no loopback, `--reload` removido |
| Baixo | Sequences de `companies`/`users` não avançadas no seed | Migração `001` |
| Baixo | Front: erro virava dado e quebrava a tela; envio sem `loading`; admin renderizava campo inexistente; sem tipagem de `import.meta.env` | Reescrito com `useMutation`, estados de erro e `npm run typecheck` |
| Baixo | Sem `.gitattributes`: clone no Windows quebraria o `migrate.sh` (CRLF) | `.gitattributes` forçando LF |

---

## Regras e checklist de entrega

| Item | Estado |
|---|---|
| `docker compose up --build` sobe tudo sem erro | ✅ Validado do zero, com volume apagado |
| Feature A implementada e isolada por tenant | ✅ 18 checagens + 9 testes |
| Feature B implementada e isolada por tenant | ✅ 15 checagens + 11 testes |
| Sintomas corrigidos estruturalmente | ✅ Causa raiz reproduzida antes de cada correção |
| `DECISIONS.md` preenchido | ✅ As 7 seções do template |
| SQL cru via `psycopg`, sem ORM | ✅ Nenhum ORM ou query builder no projeto |
| Schema versionado em `db/` | ✅ `db/migrations/001` a `006` |

**Migrações:**

| Arquivo | O que faz |
|---|---|
| `001_fix_seed_sequences.sql` | Avança as sequences que o seed deixou para trás |
| `002_job_lifecycle.sql` | Dedupe + `UNIQUE(job_id)`, `CHECK`s de status e cota, `max_attempts`, lease, `last_error`, `idempotency_key` e índices |
| `003_jobs_listing_index.sql` | Índice da listagem paginada |
| `004_job_traceability.sql` | `jobs.request_id` e tabela `job_events` |
| `005_job_cancel.sql` | Status `cancelled` |
| `006_job_events_tuning.sql` | `ON DELETE CASCADE` e chave primária `(job_id, id)` |

---

## Mudanças de contrato

Quem consumir a API precisa saber:

- **`GET /jobs` e `/admin/jobs`** passam a devolver `{items, next_cursor}`, com `limit` de 1 a 200
  (padrão 50). Antes devolviam um array sem limite.
- **Status novo:** `cancelled`.
- **`POST /jobs`** aceita o header opcional `Idempotency-Key` e pode responder **402** (cota) além de 429.
- **`X-Request-ID`** em toda resposta.
- **`/admin/jobs`** retorna só a própria empresa — **diverge do README original**, que dizia "todas as
  empresas". Os admins do seed são de empresa e o sistema promete isolamento, então tratei a descrição como
  parte do bug. A justificativa está no `DECISIONS.md`, seção 3.

---

## O que ficou de fora, conscientemente

Detalhado no `DECISIONS.md`, seção 4. Em resumo: autenticação real (premissa do case), ids opacos
(os sequenciais deixam inferir o volume de jobs alheios), migrações sem bloqueio para tabelas gigantes,
retenção de `job_events`, fairness entre empresas na fila, rate limiting, backoff entre tentativas e
testes de componente no frontend.
