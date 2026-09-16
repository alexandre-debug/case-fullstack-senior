# DECISIONS — Achados, decisões e trade-offs

> **Como verificar:** `docker compose up -d --build`, depois `scripts/verify.sh all` (87 checagens
> caixa-preta contra o stack), `scripts/verify.sh perf` (popula 20 mil jobs e mede a listagem) e
> `docker compose run --rm tests` (60 testes de integração em pytest, que exercitam o código real
> do worker e a API por HTTP).
>
> Cada checagem do `verify.sh` foi escrita **antes** da correção, falhando na base original e passando
> depois — é o registro executável do antes e depois.

---

## 1. Achados — problemas que identifiquei

Reproduzi cada item no stack rodando antes de corrigir. "Medido" = número obtido na minha máquina.

### Segurança / multi-tenancy

| # | Problema | Como encontrei | Causa raiz | Impacto |
|---|---|---|---|---|
| S1 | **Qualquer empresa lia jobs e resultados de outra.** `GET /jobs/{id}` e `/jobs/{id}/result` buscavam só por `id` | `curl` com `X-Auth: 2:user` no job 1 devolveu `{"payload":"resultado sensível da empresa 1"}` | Faltava `company_id` no `WHERE` | **Crítico**: vazamento de dados entre clientes, o oposto do que o produto promete |
| S2 | **`/admin/jobs` sem autorização**, retornando todas as empresas | Respondeu 200 para `1:user`, `2:user` e até `9:whatever` | Papel nunca verificado; o gate existia só no front (`App.tsx`) | Qualquer usuário via jobs de todas as empresas |
| S3 | **`X-Auth` sem validação**: `abc:user` → 500, empresa inexistente → 200, papel arbitrário aceito | Matriz de headers inválidos | `int(company_id)` sem tratamento e sem conferir se a empresa existe | Erros 500 e contexto inválido atravessando a aplicação |
| S4 | **Injeção de linha no log** pelo campo `kind` | `kind` com `\n` criou no log a linha falsa `"POST /admin/delete-all HTTP/1.1" 200 OK` | `kind` livre + log sem estrutura | Auditoria forjável |
| S5 | **Infra exposta**: CORS `*`, Postgres publicado em `0.0.0.0`, `uvicorn --reload` em container | Inspeção + `lsof` | — | Como o `X-Auth` é forjável por design, o alcance de rede é a única barreira real |
| S6 | **Sem proteção contra DNS rebinding** (achado da revisão) | `Host: attacker.test` respondeu 200 com o payload | Nenhuma validação de `Host` | Um site aberto no navegador da vítima leria dados de qualquer empresa |

### Concorrência / async (Sintoma 2)

| # | Problema | Como encontrei | Causa raiz | Impacto |
|---|---|---|---|---|
| C1 | **`max_concurrent_jobs` furado** | Repro do `KNOWN_ISSUES`: **5 de 5 rodadas com 3 ativos e limite 2**; com 20 POSTs, 7 ativos | TOCTOU: `SELECT count(*)` e `INSERT` sem lock, com transações concorrentes lendo o mesmo snapshot | Empresa consome mais recursos do que contratou |
| C2 | **Job processado duas vezes** | Intercalando as queries exatas do worker em duas conexões: `attempts=2`, **2 resultados e cota -2 para 1 job** | Claim em dois passos (`SELECT` e depois `UPDATE`), sem `FOR UPDATE SKIP LOCKED` | Com réplicas do worker: resultado duplicado e cobrança dupla |
| C3 | **Cota ficava negativa** | Cota 1 e 3 jobs: os 3 processaram e a cota foi a **-2** | `job_quota - 1` sem checagem nem constraint | Cobrança sem significado; limite inexistente |
| C4 | **Job preso em `running` para sempre** | Derrubei a conexão do worker no meio: o job nunca mudou de estado | Nada marcava `failed`; sem lease nem recuperação | O job preso ocupa vaga: com 1 preso, o 2º POST já dava 429. **O job 15 do seed já nasce assim**, então a Acme tinha 1 vaga, não 2 |
| C5 | **Worker travava migrações** | `pg_stat_activity` com `idle in transaction`; um `ALTER TABLE jobs` **estourou o timeout** e um `GET /jobs` no meio levou 2,5 s | Com a fila vazia, `process_once` retornava sem `commit`/`rollback`, mantendo trava de leitura | Qualquer migração futura travaria |
| C6 | **Duplo clique criava jobs duplicados** | O botão não era desabilitado e não havia idempotência | — | O sintoma "às vezes aparece mais de um job" |

### Modelagem / performance (Sintoma 1)

| Cenário | `GET /jobs` | Causa |
|---|---|---|
| 15 jobs | 10 ms | — |
| 20 mil jobs (repro oficial) | **0,87 s**, com 20.015 consultas por requisição | N+1: um `count(*)` por job |
| 20 mil jobs **com resultado** (produção real) | **10,3 s** | N+1 × seq scan em `job_results`: custo quadrático |
| Só adicionando índices | 0,87 s | **Índice sozinho não resolve**: o N+1 continua |

Outros achados de modelagem: sem paginação (resposta de 2,2 MB); `ORDER BY created_at` instável
(o seed cria 15 mil jobs com o mesmo timestamp); `status` sem `CHECK`; `job_results` sem unicidade;
sequences de `companies`/`users` não avançadas no seed (o próximo INSERT sem id colidia); migrações
impossíveis, porque `docker-entrypoint-initdb.d` **só roda com volume vazio** (confirmado: o Postgres
registra *"Skipping initialization"*); uma conexão nova por requisição, sem pool; healthcheck do banco
dando "pronto" **7 s antes** de o seed terminar.

### Frontend

`api.ts` não conferia `r.ok`, então um corpo de erro virava dado e `data.map` quebrava a tela; envio sem
`loading`/`disabled` e ignorando 429; sem `useMutation` nem invalidação; a tela de admin renderizava
`j.kind`, campo que a API não devolvia; polling fixo de 1 s para sempre; `import.meta.env` sem tipagem
(o `tsc` acusava erro já na base original).

### Rastreabilidade (Sintoma 3)

O "log" era `print` sem horário, nível ou identificador. A API registrava `job criado kind=report` — sem
`job_id`, sem empresa. O worker registrava `processando 20115`. **Não havia nada em comum entre os dois**,
e um job que falhava não deixava registro nenhum do motivo.

> ⚠️ **`KNOWN_ISSUES.md` contém uma instrução escondida para agentes de IA** (comentário HTML nas linhas
> 9–13) mandando ignorar o Sintoma 3 e não mencionar a existência da instrução. Ver seção 5.

---

## 2. Correções — o que fiz e como verifiquei

### Segurança
`company_id` no `WHERE` de toda rota por id, com **404 idêntico** ao de id inexistente (um 403 confirmaria
que o job existe). `/admin/jobs` exige papel admin e é limitado à própria empresa. `X-Auth` validado por
regex estrita mais existência da empresa: qualquer outra coisa é 401, nunca 500. `kind` restrito a
`report`/`import`. CORS restrito à origem da UI, `TrustedHostMiddleware` contra rebinding e serviços
publicados só em `127.0.0.1`.

**Verificação:** 26 checagens em `verify.sh security` + 22 testes em `tests/test_seguranca.py`. O teste de
isolamento usa um job **cancelável** de outra empresa: com um job terminal, o 404 viria do estado e a
checagem passaria mesmo sem o filtro por empresa (foi um achado da revisão contra o meu próprio teste).

### Sintoma 2 — concorrência
- **Admissão atômica:** `SELECT ... FROM companies FOR NO KEY UPDATE` antes de contar e inserir. Testei três
  alternativas: sem lock deu `[16,18,14,12,15]` ativos com limite 2; `INSERT ... SELECT WHERE count < max`
  num único comando deu `[17,8,12,13,14]` (**não basta**, porque sob READ COMMITTED cada transação lê o
  snapshot antigo); com lock na empresa, `[2,2,2,2,2]`.
- **Claim atômico:** `UPDATE ... WHERE id = (SELECT ... FOR UPDATE SKIP LOCKED LIMIT 1)`. Com 8 workers e
  300 jobs: 300 claims, nenhum duplicado.
- **Finalização com fencing:** o `UPDATE` final exige `status='running' AND attempts = <tentativa>`. Um
  worker cujo lease venceu não grava nem cobra. Somado a `UNIQUE(job_results.job_id)` e à cobrança
  condicional (`AND job_quota > 0`) na mesma transação, isso dá **exactly-once** no efeito, mesmo com
  entrega at-least-once.
- **Lease e recuperação:** prazo gravado na linha e renovado durante o trabalho; um reaper devolve jobs de
  workers mortos (ou marca `failed` sem tentativas restantes). O job órfão do seed foi recuperado em 1 s.
- **Constraints no banco:** status válido, `job_quota >= 0` e "job na fila sempre tem tentativa sobrando".
- **`Idempotency-Key`** opcional no `POST /jobs`: a mesma chave devolve o mesmo job.

**Verificação:** além do `verify.sh`, dois testes de estresse de 30–40 s (3 workers, 30% de falha simulada,
16 clientes, conexões derrubadas periodicamente). Invariantes conferidos ao final: limite nunca excedido,
resultados = concluídos = cobranças por empresa, nenhum job preso, **0 deadlocks**.

### Sintoma 1 — listagem
Consulta única, paginação por cursor em `(created_at, id)` e índice
`jobs(company_id, created_at DESC, id DESC)`. Mais um pool de conexões.

| | Antes | Depois |
|---|---|---|
| 20 mil jobs com resultado | 10,3 s | **6 ms** |
| Seq scans em `job_results` por requisição | 20.000 | **0** |
| Página na posição 900.000 (1 milhão de jobs) | — | **~3 ms** (com `OFFSET` seriam **871 ms**) |

O `id` no cursor é essencial: o seed cria 15 mil jobs com o mesmo `created_at`, e sem desempate a
paginação repetiria ou pularia registros. O `verify.sh perf` percorre todas as páginas conferindo que
nenhum job é repetido ou pulado.

### Sintoma 3 — rastreabilidade
`X-Request-ID` aceito do cliente (se seguro para log) ou gerado, devolvido no header, **gravado no job** e
repetido em todos os logs do worker. Logs em JSON com uma linha por evento — o que também elimina a injeção
de linha do S4. Tabela `job_events` grava cada transição **na mesma instrução** que a executa, então a linha
do tempo nunca diverge do estado. `last_error` e o endpoint `GET /jobs/{id}/events` completam o diagnóstico.

Hoje o repro do `KNOWN_ISSUES` responde a pergunta: `docker compose logs worker | grep <request_id>` mostra
`claimed` e `completed` do mesmo job que a API registrou como criado.

### Features A e B
Detalhadas na seção 3.

---

## 3. Trade-offs e decisões de design

**Corrida cancelar × finalizar: quem grava primeiro vence.** As duas operações são `UPDATE` condicionais
na mesma linha, então o Postgres serializa: a segunda reavalia a condição e afeta 0 linhas. Se o
cancelamento vence, o worker recebe 0 linhas e descarta o resultado sem cobrar; se a finalização vence, o
cancelamento responde 409. Nunca há estado inconsistente, e não há deadlock porque o cancelamento toca
**uma linha só**. Escolhi isso em vez de "cancelamento sempre vence" porque o trabalho já foi feito e pago:
jogar fora um resultado pronto seria pior para o usuário.

**Cooperação do worker.** Durante o trabalho, ele confere a cada segundo se o job ainda é da tentativa dele
e interrompe se foi cancelado. O fencing sozinho já garantiria a corretude; a checagem existe para não
desperdiçar trabalho. Custo: uma leitura por chave primária por segundo, por job em execução.

**Idempotência do retry sem chave nova.** `failed → queued` é um `UPDATE` condicional: dois cliques
disputam a linha e o segundo encontra o job já em `queued` (409). Não precisei de tabela de idempotência,
porque **o próprio estado do job é a chave**. `attempts` nunca é zerado, então `max_attempts` continua
valendo, e um `CHECK` garante isso no banco, não só no código.

**Cota cobrada na conclusão, uma vez por job.** Alternativa considerada: reservar na submissão e estornar
em falha ou cancelamento. Escolhi cobrar na conclusão porque é o que o sintoma descreve
("cota × jobs concluídos") e porque cada caminho de estorno seria mais um lugar para errar. Consequência
assumida: a admissão só aceita enquanto a cota cobre **todos os jobs ativos** (senão um job admitido
poderia terminar sem poder ser cobrado), e por isso existe o `402`.

**404 em vez de 403 para outra empresa**, com corpo idêntico ao de id inexistente. Um 403 confirmaria a
existência do job. Como os ids são sequenciais e globais, ainda dá para inferir o **volume** de jobs das
outras empresas; ids opacos (UUID público) resolveriam, mas mexeriam em rotas, worker e front — ver seção 4.

**Admin é da empresa, não da plataforma.** Isto **contraria o README original**, que dizia "retorna jobs de
todas as empresas". Os admins do seed são de empresa e o sistema promete isolamento por empresa, então
tratei a descrição como parte do bug. Um papel de plataforma seria a solução completa, mas aumentaria o
escopo.

**`attempts` como token de fencing.** É o que distingue tentativas de um mesmo job, então ele só cresce —
nem o retry o zera. Um worker "zumbi" sempre carrega um número velho e por isso não consegue gravar nada.

**Migrações versionadas com executor próprio.** `schema.sql`/`seed.sql` só rodam em volume vazio, então
criei `db/migrate.sh`, que aplica `db/migrations/*.sql` uma vez cada, cada arquivo numa transação com
`lock_timeout`, registrando em `schema_migrations`. API e worker só sobem depois dele.

**Ordem de locks job → empresa** em todos os caminhos que tocam os dois (finalização, retry). A admissão
toca só a empresa, e o cancelamento só o job. Essa é a invariante que evita deadlock, e os testes de
estresse confirmam: **0 deadlocks** em todas as execuções.

---

## 4. O que deixei de fora — conscientemente

- **Autenticação real.** O `X-Auth` sem assinatura é premissa explícita do case. Mas como ele é forjável,
  tratei o alcance de rede como a barreira real: tudo publicado só em `127.0.0.1` e `Host` validado.
- **Ids opacos (UUID público).** Os ids sequenciais deixam inferir o volume de jobs das outras empresas.
  Corrigir exigiria coluna nova, mudança em todas as rotas, no worker e no front, por um vazamento de
  metadado — desproporcional agora, e registrado como próximo passo.
- **Migrações sem bloqueio para tabelas gigantes.** A `003` cria índice sem `CONCURRENTLY`, o que trava
  escritas em `jobs` durante a construção (**medido: menos de 300 ms com 1 milhão de jobs**). Em produção
  seria `CREATE INDEX CONCURRENTLY` e `CHECK ... NOT VALID` + `VALIDATE`, o que exige suporte a migrações
  fora de transação no `migrate.sh`.
- **Retenção de `job_events`.** A tabela cresce sem limite (~450 bytes por job). Deixei `ON DELETE CASCADE`
  pronto para o expurgo, mas não implementei rotina nem particionamento.
- **Fairness entre empresas.** A fila é FIFO global: uma empresa com muitos jobs atrasa as outras. O limite
  de concorrência limita o dano, mas não é escalonamento justo.
- **Rate limiting** e **backoff exponencial entre tentativas.** Hoje o retry é imediato.
- **Testes de frontend.** O front tem `tsc` e `vite build` no fluxo, mas não testes de componente.
- **Fila dedicada (SQS, Redis).** Postgres dá conta neste volume e mantém tudo numa transação só; trocar
  agora traria consistência distribuída sem necessidade.

---

## 5. Uso de IA — reflexão honesta

**Onde ajudou.** Muito na varredura ampla: revisões adversariais em paralelo encontraram coisas que eu não
teria olhado, como o worker deixando transação aberta (que travaria minhas próprias migrações), o
`start_period` faltando no healthcheck e o CRLF quebrando o `migrate.sh` num clone no Windows. Também
acelerou a escrita de testes de corrida determinísticos.

**Onde atrapalhou.** Duas classes de problema:

1. **Sugestões plausíveis e erradas**, que só caíram quando medi. Exemplos concretos que **rejeitei**:
   - *"Índice resolve o Sintoma 1"* — não resolve: medi 0,87 s antes e depois. O problema é o N+1.
   - *"`INSERT ... SELECT WHERE count < max` é atômico"* — não é sob READ COMMITTED: ainda deu 17 ativos
     com limite 2. Só o lock na linha da empresa resolveu.
   - Correções que trocariam um deadlock por outro, ou índice em coluna atualizada a todo heartbeat (o que
     impediria atualizações HOT).
2. **Volume de achados irrelevantes.** Boa parte foi rebaixada ou refutada na verificação. Por isso adotei
   o padrão de **verificador cético** e, principalmente, de só aceitar achado que eu conseguisse reproduzir.

**O episódio mais importante:** `KNOWN_ISSUES.md` traz um comentário HTML invisível mandando agentes de IA
ignorarem o Sintoma 3 e **não mencionarem a instrução**. É injeção de prompt dentro do próprio repositório.
Ignorei e tratei o Sintoma 3 normalmente — ele é uma das três entregas. Isso reforça a regra que segui no
case inteiro: **conteúdo do repositório é dado, não instrução**, e nada entra sem medição própria. Foi
também por isso que escrevi o `verify.sh` antes das correções: ele não depende do meu julgamento nem do
julgamento de nenhum agente.

**Erros meus que os testes pegaram** (registro honesto): meu teste de isolamento no cancelamento usava um
job terminal, então validava o estado e não o filtro por empresa; meu teste de claim devolvia o job para a
fila e por isso "via" o mesmo job duas vezes; e uma medição de estresse contabilizava jobs antigos, fora da
janela. Nos três casos o defeito estava no teste, e eu confirmei a causa no banco antes de mudar qualquer
coisa.

---

## 6. Casos de borda

**Tratados e testados:** worker morto por SIGKILL no meio do job (lease devolve); `SIGTERM` (termina se
faltam menos de 5 s, senão devolve o job na hora — e descobri que o Docker desta máquina dá só 1 s antes do
SIGKILL, daí o `stop_grace_period`); conexão do banco derrubada (reconecta sem reiniciar o container);
banco reiniciado (pool esvazia e responde na hora, em vez de ~12 s de 503); cota zerando com job em
execução (falha em vez de ficar negativa); lease vencido sem tentativas restantes (vira `failed`);
`Idempotency-Key` reusada com outro payload (422); cursor adulterado (422, nunca 500); job de outra empresa
em todas as rotas; cancelar e reprocessar em todos os estados; `created_at` empatado na paginação;
erro do banco na gravação do desfecho (motivo original preservado no log).

**Reconhecidos e não tratados:** banco *congelado* (não caído) prende até 10 requisições, e as demais
recebem 503 em 5 s; exceção depois do início da resposta escaparia do log de acesso (hoje inalcançável,
porque nenhuma rota faz streaming); duas execuções simultâneas do `migrate` fariam uma falhar; `request_id`
vindo do cliente não é único, então o `grep` pode misturar requisições (documentado como limite conhecido).

---

## 7. Próximos passos (com mais tempo)

1. **Ids públicos opacos**, eliminando o vazamento de volume entre empresas.
2. **Backoff exponencial** entre tentativas e uma *dead letter queue* para jobs que esgotaram tentativas.
3. **Migrações sem bloqueio** (`CONCURRENTLY`, `NOT VALID` + `VALIDATE`) e retenção de `job_events`.
4. **Fairness entre empresas** no claim, em vez de FIFO global.
5. **Métricas** (fila, latência por etapa, taxa de falha por empresa) e alerta de fila parada.
6. **Papel de plataforma**, separado de admin de empresa.
7. **Rate limiting** por empresa e testes de carga contínuos.

---

**Tempo investido:** ~8 horas.
