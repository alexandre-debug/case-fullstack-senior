# Relay — Serviço de Processamento de Jobs em Background

## O que é Relay?

Relay é um **serviço multi-tenant de processamento de jobs em background**. Empresas (tenants) submetem jobs para processamento assíncrono; um worker processa em background; usuários acompanham o status e baixam resultados — tudo isolado por empresa.

Este repositório contém uma **base de case deliberadamente imperfeita**. Não é um modelo a seguir em produção — é um ponto de partida educacional desenhado para ser estendido.

## Stack

| Camada | Tecnologia |
|---|---|
| **API** | FastAPI (Python 3.12) |
| **Banco de dados** | PostgreSQL 16 |
| **Worker** | Processo Python separado |
| **Frontend** | React 18 + Vite + TanStack Query + shadcn/ui |
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

### Autenticação fake

Relay usa um sistema de autenticação **simplificado** para fins educacionais. Toda requisição HTTP precisa do header `X-Auth` no formato:

```
X-Auth: <company_id>:<role>
```

Exemplos:
- `X-Auth: 1:user` — usuário comum da empresa 1
- `X-Auth: 2:admin` — admin da empresa 2

**Não há assinatura nem validação criptográfica — é só um contexto.** Na web UI, existe um dropdown para trocar entre 4 usuários fake:
- Empresa 1 (Acme): `user@acme.test` (user), `admin@acme.test` (admin)
- Empresa 2 (Globex): `user@globex.test` (user), `admin@globex.test` (admin)

## Dados seeded

Duas empresas estão pré-criadas:

| ID | Nome | Max concorrentes | Cota inicial |
|---|---|---|---|
| 1 | Acme | 2 | 20 |
| 2 | Globex | 2 | 100 |

Cada empresa tem um usuário comum e um admin. ~30 jobs distribuídos entre os dois tenants em vários status (done, failed, running) para você explorar.

## Endpoints principais

### Listagem de jobs

```
GET /jobs
```

Retorna todos os jobs da empresa do usuário autenticado, ordenados por `created_at` DESC.

**Resposta:**
```json
[
  {
    "id": 1,
    "kind": "report",
    "status": "done",
    "created_at": "2026-07-20T12:34:56.000Z",
    "result_count": 1
  }
]
```

**Status de job:** `queued` | `running` | `done` | `failed`

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
  "status": "done"
}
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

**Resposta:**
```json
{
  "id": 42,
  "status": "queued"
}
```

### Admin: ver todos os jobs

```
GET /admin/jobs
```

Endpoint administrativo: retorna jobs de todas as empresas.

**Resposta:**
```json
[
  {
    "id": 1,
    "company_id": 1,
    "status": "done"
  },
  {
    "id": 2,
    "company_id": 2,
    "status": "running"
  }
]
```

## O que precisa ser feito

Veja o arquivo `TASKS.md` para o enunciado completo do case. Em resumo:

1. **Feature A:** Implementar cancelamento de jobs em processamento.
2. **Feature B:** Implementar retry de jobs com idempotência (não duplicar resultado, não cobrar quota em dobro).
3. **Diagnosticar e corrigir** os problemas listados em `KNOWN_ISSUES.md`.
4. **`DECISIONS.md`:** Documentar achados, correções e trade-offs.

## Problemas conhecidos

Veja `KNOWN_ISSUES.md` para uma lista de sintomas operacionais reportados.

## Segurança

**Importante:** Reporte e corrija **qualquer problema de segurança ou corretude que encontrar, mesmo os não listados em KNOWN_ISSUES.md.** A base é intencionalmente imperfeita — faz parte do case encontrar e corrigir todos os problemas.

## Estrutura do repositório

```
relay/
├─ README.md                    # Este arquivo
├─ TASKS.md                     # Enunciado: features, correções, extras
├─ KNOWN_ISSUES.md             # Sintomas reportados
├─ DECISIONS.md                # (stub) A preencher pelo candidato
├─ docker-compose.yml
├─ .env.example
├─ db/
│  ├─ schema.sql               # Schema do banco
│  └─ seed.sql                 # Dados pré-seeded
├─ api/                        # API FastAPI
├─ worker/                     # Processador de jobs
└─ web/                        # React SPA
```

## Perguntas?

Este case é auto-contido. Qualquer informação que você precisar está nestes arquivos ou é deduzível rodando o stack.
