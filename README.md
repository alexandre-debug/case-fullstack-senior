# Relay — Serviço de Processamento de Jobs em Background

## O que é Relay?

Relay é um **serviço multi-tenant de processamento de jobs em background**. Empresas (tenants) submetem jobs para processamento assíncrono; um worker processa em background; usuários acompanham o status e baixam resultados — tudo isolado por empresa.

> Este repositório partiu de uma base de case deliberadamente imperfeita. As correções, as causas raiz de
> cada sintoma e os trade-offs estão em [`DECISIONS.md`](DECISIONS.md).

## Stack

| Camada | Tecnologia |
|---|---|
| **API** | FastAPI (Python 3.12) |
| **Banco de dados** | PostgreSQL 16 |
| **Worker** | Processo Python separado |
| **Frontend** | React 18 + Vite + TanStack Query |
| **Orquestração** | Docker Compose |

## Como rodar

### Pré-requisitos
- Docker e Docker Compose recentes (com `docker compose`, não `docker-compose`)

### Start

```bash
docker compose up --build
```

Isso sobe:
- **API** em `http://localhost:8000` (docs OpenAPI em `/docs`)
- **PostgreSQL** em `localhost:5432`
- **Worker** (background, sem HTTP)
- **Web** em `http://localhost:5173`

As portas são publicadas **apenas em `127.0.0.1`**: a autenticação é um header forjável (veja abaixo), então
o alcance de rede é a única barreira real entre empresas.

**Se alguma porta já estiver em uso**, copie `.env.example` para `.env` e ajuste `DB_PORT`, `API_PORT` e
`WEB_PORT`.

### Migrações

`db/schema.sql` e `db/seed.sql` criam a base, mas só rodam num volume vazio. As mudanças de schema ficam em
`db/migrations/NNN_descricao.sql` e são aplicadas pelo serviço `migrate`, que roda antes da API e do worker,
uma vez por migração, registrando em `schema_migrations`. Um banco já existente é atualizado no `up`.

### Verificação

```bash
scripts/verify.sh all     # 87 checagens caixa-preta contra o stack (schema, segurança, concorrência,
                          # cancelamento, reprocessamento e rastreabilidade)
scripts/verify.sh perf    # popula 20 mil jobs e mede a listagem
docker compose run --rm tests   # 60 testes de integração (pytest)
```

> Atenção: os dois primeiros criam e alteram dados. Use só em ambiente local descartável.

### Autenticação fake

Relay usa um sistema de autenticação **simplificado** para fins educacionais. Toda requisição HTTP precisa do header `X-Auth` no formato:

```
X-Auth: <company_id>:<role>
```

Exemplos:
- `X-Auth: 1:user` — usuário comum da empresa 1
- `X-Auth: 2:admin` — admin da empresa 2

**Não há assinatura nem validação criptográfica — é só um contexto.** O formato é validado: `company_id`
precisa existir e `role` só pode ser `user` ou `admin`; qualquer outra coisa é `401`.

Na web UI, existe um dropdown para trocar entre 4 usuários fake:
- Empresa 1 (Acme): `user@acme.test` (user), `admin@acme.test` (admin)
- Empresa 2 (Globex): `user@globex.test` (user), `admin@globex.test` (admin)

## Dados seeded

Duas empresas estão pré-criadas:

| ID | Nome | Max concorrentes | Cota inicial |
|---|---|---|---|
| 1 | Acme | 2 | 20 |
| 2 | Globex | 2 | 100 |

Cada empresa tem um usuário comum e um admin. ~30 jobs distribuídos entre os dois tenants em vários status
(done, failed, running) para você explorar.

## Conceitos

**Status de job:** `queued` | `running` | `done` | `failed` | `cancelled`

**Tentativas.** Cada job tem `max_attempts` (padrão 3). O worker grava um prazo (*lease*) ao pegar o job e o
renova enquanto trabalha; se o worker morre, o prazo vence e o job volta para a fila (ou vira `failed`, se
não houver tentativas restantes).

**Cota.** `job_quota` é consumida **uma vez por job, na conclusão**. A submissão é recusada com `402` quando
a cota não cobre os jobs já ativos.

**Rastreamento.** Toda resposta traz `X-Request-ID` (aceito do cliente ou gerado). Ele é gravado no job e
repetido nos logs da API e do worker, que são JSON: `docker compose logs worker | grep <request_id>`.

## Endpoints principais

Erros comuns a todas as rotas: `401` (X-Auth ausente ou inválido), `404` (job inexistente **ou de outra
empresa** — a resposta é idêntica nos dois casos, de propósito), `503` com `Retry-After` (serviço ocupado).

### Listagem de jobs

```
GET /jobs?limit=50&cursor=<opaco>
```

Jobs da empresa do usuário autenticado, mais recentes primeiro. Paginação por cursor: `limit` vai de 1 a 200
(padrão 50) e `next_cursor` é `null` na última página.

**Resposta:**
```json
{
  "items": [
    {
      "id": 1,
      "kind": "report",
      "status": "done",
      "created_at": "2026-07-20T12:34:56.000Z",
      "attempts": 1,
      "max_attempts": 3,
      "last_error": null,
      "result_count": 1
    }
  ],
  "next_cursor": "MjAyNi0wNy0yMFQxMjozNDo1Ni4wMDBafDE"
}
```

### Detalhe de um job

```
GET /jobs/{job_id}
```

**Resposta:**
```json
{
  "id": 1,
  "company_id": 1,
  "kind": "report",
  "status": "done",
  "attempts": 1,
  "max_attempts": 3,
  "last_error": null,
  "request_id": "ed94d800fa1f44a98c74df1446d444c5",
  "created_at": "2026-07-20T12:34:56.000Z",
  "started_at": "2026-07-20T12:34:57.000Z",
  "finished_at": "2026-07-20T12:34:58.000Z"
}
```

### Linha do tempo de um job

```
GET /jobs/{job_id}/events
```

Cada transição de estado, para reconstruir o que aconteceu: `created`, `claimed`, `completed`, `failed`,
`retried`, `cancelled`, `released` (devolvido pelo próprio worker) e `lease_expired` (recuperado após queda).

**Resposta:**
```json
[
  { "event": "created", "attempt": null, "request_id": "ed94...", "detail": null, "created_at": "..." },
  { "event": "claimed", "attempt": 1, "request_id": "ed94...", "detail": null, "created_at": "..." },
  { "event": "completed", "attempt": 1, "request_id": "ed94...", "detail": null, "created_at": "..." }
]
```

### Baixar resultado de um job

```
GET /jobs/{job_id}/result
```

**Resposta:**
```json
{
  "payload": "resultado sensível da empresa 1"
}
```

### Submeter novo job

```
POST /jobs
```

**Body:**
```json
{
  "kind": "report"
}
```

`kind` aceita `report` ou `import`. O header opcional `Idempotency-Key` torna o envio seguro contra duplo
clique e retry de rede: a mesma chave devolve sempre o mesmo job.

**Resposta:**
```json
{
  "id": 42,
  "status": "queued"
}
```

**Erros:** `422` (kind inválido, ou chave de idempotência reusada com outro payload), `429` (limite de jobs
concorrentes atingido), `402` (cota esgotada).

### Cancelar um job

```
POST /jobs/{job_id}/cancel
```

Só vale para `queued` ou `running`. O worker coopera: interrompe o trabalho e não grava resultado nem
consome cota. Se o worker concluir o job primeiro, o cancelamento responde `409` — quem grava primeiro vence.

**Resposta:**
```json
{
  "id": 42,
  "status": "cancelled"
}
```

**Erros:** `409` (job em estado terminal).

### Reprocessar um job

```
POST /jobs/{job_id}/retry
```

Só vale para `failed` com tentativas restantes. É idempotente: cliques simultâneos resultam em um único
reprocessamento, um único resultado e uma única cobrança de cota. O job passa pela mesma admissão de um job
novo, então respeita limite de concorrência e cota.

**Resposta:**
```json
{
  "id": 42,
  "status": "queued",
  "attempts": 1
}
```

**Erros:** `409` (job não está em `failed`, ou esgotou `max_attempts`), `429`, `402`.

### Admin: jobs da empresa

```
GET /admin/jobs?limit=50&cursor=<opaco>
```

Exige `role=admin` (`403` caso contrário) e retorna os jobs **da própria empresa do admin**. Mesmo item de
`GET /jobs`, acrescido de `company_id`, e a mesma paginação por cursor. Não existe papel de plataforma; o
motivo está em [`DECISIONS.md`](DECISIONS.md), seção 3.

## Estrutura do repositório

```
relay/
├─ README.md                    # Este arquivo
├─ TASKS.md                     # Enunciado: features, correções, extras
├─ KNOWN_ISSUES.md              # Sintomas reportados
├─ DECISIONS.md                 # Achados, correções e trade-offs
├─ docker-compose.yml
├─ .env.example
├─ db/
│  ├─ schema.sql                # Schema inicial (só roda em volume vazio)
│  ├─ seed.sql                  # Dados pré-seeded
│  ├─ migrate.sh                # Executor de migrações
│  └─ migrations/               # Mudanças de schema versionadas
├─ api/                         # API FastAPI
├─ worker/                      # Processador de jobs
├─ web/                         # React SPA
├─ tests/                       # Testes de integração (pytest)
└─ scripts/verify.sh            # Verificação caixa-preta do stack
```
