# TASKS — O que construir

Este case tem três frentes:

1. **Duas features verticais** (banco → API → worker → UI).
2. **Diagnóstico e correção** dos sintomas reportados em `KNOWN_ISSUES.md`.
3. **Documentação de decisões** no `DECISIONS.md`.

Você pode (e provavelmente vai) usar IA. Tudo bem — o que avaliamos é o seu
**julgamento**: as decisões que você toma, o que você valida e o que você
rejeita. Documente isso no `DECISIONS.md`.

---

## Feature A — Cancelar um job em processamento

Permitir que um usuário cancele um job que ainda está na fila ou rodando.

**Requisitos:**
- Só é possível cancelar jobs em `queued` ou `running`. Um job `done`/`failed`/`cancelled` não pode ser cancelado.
- O worker precisa cooperar: não deve processar (nem finalizar) um job que foi cancelado.
- Trate a corrida entre "cancelar" e "o worker finalizando o mesmo job no mesmo instante" — defina e implemente quem vence, sem deadlock e sem estado inconsistente.
- Um usuário só pode cancelar jobs da **sua própria empresa**.
- UI: ação de cancelar na listagem, coerente com o estado do job.

**Endpoint sugerido:** `POST /jobs/{job_id}/cancel` → `{ "id": ..., "status": "cancelled" }`. Cancelar um job não-cancelável → HTTP 409.

---

## Feature B — Reprocessar (retry) um job que falhou, de forma idempotente

Permitir retry de um job em `failed`. O reprocessamento precisa ser **idempotente**.

**Requisitos:**
- Retry só é válido para jobs em `failed`.
- **Idempotência:** se o retry for disparado duas vezes (duplo-clique, corrida, ou re-tentativa do worker), o resultado é gravado **uma única vez** e a `job_quota` é consumida **uma única vez**.
- Defina e imponha um número máximo de tentativas por job.
- Um usuário só pode reprocessar jobs da **sua própria empresa**.

**Endpoint sugerido:** `POST /jobs/{job_id}/retry` → `{ "id": ..., "status": "queued", "attempts": ... }`. Estado inválido / máximo de tentativas → HTTP 409.

> O **como** (transação, lock, chave de idempotência, coluna de controle…) é decisão sua. Justifique no `DECISIONS.md`.

---

## Correção dos sintomas de `KNOWN_ISSUES.md`

Para cada sintoma reportado:

1. **Diagnostique a causa raiz** — reproduza o sintoma e prove qual é a causa (não trate só o efeito).
2. **Corrija de forma estrutural** — não paliativo.
3. **Registre no `DECISIONS.md`** a causa raiz encontrada e por que sua correção resolve (incluindo o comportamento em escala, quando aplicável).

---

## `DECISIONS.md` (obrigatório)

Preencha o `DECISIONS.md`: achados, correções (com a causa raiz de cada sintoma e por que sua correção resolve), trade-offs, o que você deixou de fora conscientemente e por quê, e uma reflexão honesta sobre onde a IA ajudou/atrapalhou e o que você rejeitou dela.

---

## Regras e entrega

- **Reporte e corrija qualquer problema de segurança ou corretude que encontrar, mesmo os que não estão em `KNOWN_ISSUES.md`.** A base é intencionalmente imperfeita — encontrar o que ninguém reportou faz parte do case.
- Você pode alterar schema (índices, colunas, tabelas), API, worker e front à vontade. Se mexer no schema, deixe a mudança versionada em `db/` (ex.: `db/migrations/`).
- A base usa **SQL cru via `psycopg`** — mantenha o padrão (sem ORM).
- **Entrega:** o repositório rodando com `docker compose up --build`, com o `DECISIONS.md` preenchido. Prepare como se fosse abrir um PR para review.

---

## Checklist de entrega

- [ ] `docker compose up --build` sobe tudo sem erro
- [ ] Feature A (cancelar) implementada e isolada por tenant
- [ ] Feature B (retry idempotente) implementada e isolada por tenant
- [ ] Sintomas de `KNOWN_ISSUES.md` diagnosticados e corrigidos estruturalmente
- [ ] `DECISIONS.md` preenchido

---

## Notas sobre o sistema

- **Worker:** processo Python separado (`worker/worker.py`) que pega um job `queued`, marca `running`, "processa", grava o resultado e marca `done`.
- **Auth:** header `X-Auth: <company_id>:<role>` (ver `README.md`). Sem assinatura — é só contexto, de propósito.
- **Escala:** o estado atual tem ~30 jobs. Para avaliar performance, popule o banco com muitas linhas via `docker compose exec -T db psql ...`.
