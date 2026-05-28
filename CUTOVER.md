# Cutover do bot-planilha-sefaz (FASE 7)

Playbook operacional para passar o bot de **Tkinter desktop** para o **worker
container disparado pelo rps-maestro**. A premissa é cutover gradual:
worker entra em paralelo com a Tkinter, observa-se 7 dias, e só então a
Tkinter é deprecated.

> Pré-fase: FASES 1–6 commitadas em `main` (`0cef833`). A suíte local (46
> testes) passa em ~1s. Falta validação real no SRVRPS03.

---

## 1. Pré-flight no SRVRPS03

Antes de qualquer job real, conferir nesta ordem:

| # | Item | Comando / verificação | Falha = |
|---|------|----------------------|---------|
| 1 | **Share CIFS** `\\SRVDOC01\REDE\FISCAL` montado | `mountpoint /mnt/fiscal && ls "/mnt/fiscal/00 PLANILHA SEFAZ"` | Decisão #11 não cumprida — corrigir `/etc/fstab` antes. |
| 2 | **Migration 000009** (`job_logs.actionable`) aplicada no DB do Maestro | `psql ... -c "\d job_logs"` (coluna `actionable boolean` presente) | Reports com `actionable=true` quebram — coordenar com time do Maestro. |
| 3 | **Credencial Google** em `./credentials/citric-nimbus-436114-g8-daacef9f0900.json` | `ls credentials/` | Sem isso o worker falha no primeiro `_ensure_df`. |
| 4 | **`.env`** preenchido a partir de [`.env.example`](.env.example) | Conferir `MAESTRO_API_URL`, `MAESTRO_RABBITMQ_*`, `API_KEY` (Capsolver), `GOOGLE_SHEET_URL`, `SEFAZ_DEST`, `SEFAZ_REMET` | Worker sobe mas não conecta — ou pior, conecta mas sem captcha. |
| 5 | **Imagem builda** | `docker compose -f docker-compose.prod.yml build` | Investigar antes de subir. |
| 6 | **Worker conecta no RabbitMQ** | `docker compose -f docker-compose.prod.yml up -d worker && docker compose logs worker` — esperar log `Worker pronto. Aguardando tarefas...` | Conferir host/porta/credenciais. |
| 7 | **Automação cadastrada no Maestro** com `queue_name=bot-planilha-tasks` e o [parameterSchema](#3-parameterschema-do-maestro) | UI do Maestro lista a automação na seção de cadastro | Worker recebe mensagens com schema diferente do esperado. |

---

## 2. Cadastro da automação no Maestro

Campos do cadastro (no painel do rps-maestro, mesma rotina usada pelo
bot-xml-gms — ver `docs/automations.md` no repo do Maestro para a sintaxe
exata):

| Campo | Valor |
|-------|-------|
| **Nome** | `bot-planilha-sefaz` |
| **Descrição** | Baixa planilha consolidada de NFes do portal SEFAZ-BA por empresa e move para o share `\\SRVDOC01\REDE\FISCAL\00 PLANILHA SEFAZ\<EMPRESA>\<YYYY>\<MMYYYY>\` |
| **Queue name** | `bot-planilha-tasks` |
| **Worker API key** | mesma chave que está em `MAESTRO_WORKER_API_KEY` no `.env` do worker (ou vazio em dev) |
| **parameterSchema** | ver §3 |

---

## 3. parameterSchema do Maestro

Proposta dos campos do form gerado pelo Maestro. **Adaptar à sintaxe exata
do parameterSchema** que o painel do Maestro espera — usar o
bot-xml-gms como referência viva quando houver dúvida de formato.

```json
{
  "parameters": [
    {
      "name": "data_inicial",
      "type": "date",
      "label": "Data inicial",
      "required": true,
      "format": "dd/MM/yyyy",
      "placeholder_help": "Aceita {{today}}, {{yesterday}}, {{first_of_month}}, {{first_of_last_month}} em schedules"
    },
    {
      "name": "data_fim",
      "type": "date",
      "label": "Data final",
      "required": true,
      "format": "dd/MM/yyyy",
      "placeholder_help": "Aceita {{today}}, {{yesterday}}, {{last_of_month}}, {{last_of_last_month}} em schedules"
    },
    {
      "name": "destinatario",
      "type": "boolean",
      "label": "Consultar como DESTINATÁRIO",
      "default": false,
      "constraint": "destinatario || remetente deve ser true"
    },
    {
      "name": "remetente",
      "type": "boolean",
      "label": "Consultar como REMETENTE",
      "default": false,
      "constraint": "destinatario || remetente deve ser true"
    },
    {
      "name": "empresas",
      "type": "list_text",
      "label": "Empresas (opcional — vazio = todas com Senha Robô na Sheet)",
      "required": false,
      "default": [],
      "help": "Aceita código da empresa OU substring da razão social (case-insensitive). PUT é full replace; sem sync periódico da Sheet."
    }
  ]
}
```

Regras de validação **já implementadas no worker** (`worker._validar_parametros`):
- `data_inicial`/`data_fim` no formato exato `dd/MM/yyyy` (10 chars, `/` nas posições 2 e 5).
- Pelo menos um de `destinatario`/`remetente` precisa ser `true`.
- `empresas` precisa ser lista (ou ausente). Strings são aceitas dentro da lista.
- Sheet vazia/inacessível → `failed/INVALID_PARAMETERS` actionable.

Mensagens inválidas voltam ao operador como `failed/INVALID_PARAMETERS` com
`nack(requeue=false)` — não voltam pra fila.

### Exemplo de schedule diário (placeholders)

Rotina "todo dia 1 às 06h baixa o mês anterior":
- `data_inicial`: `{{first_of_last_month}}`
- `data_fim`: `{{last_of_last_month}}`
- `destinatario`: `true`
- `remetente`: `false`
- `empresas`: `[]`

Lembrete da decisão #9: placeholders **só expandem em schedules**, não em
execução manual via UI.

---

## 4. Primeiro smoke job

Mínimo viável pra validar a plumbing toda (RabbitMQ → worker → SEFAZ →
share → Maestro). Criar pela **UI do Maestro** (não publicar mensagem
crua na fila — a Maestro UI é a fonte canônica de jobs).

**Parâmetros sugeridos:**

| Campo | Valor |
|-------|-------|
| `data_inicial` | data fixa recente (ex: 1º dia do mês corrente) |
| `data_fim` | mesmo dia, ou hoje |
| `destinatario` | `true` |
| `remetente` | `false` |
| `empresas` | **1 empresa só** — escolher uma com poucas notas (smoke barato) |

**Mensagem equivalente** que chega na fila (referência — operador não
publica isto manualmente):

```json
{
  "job_id": "<uuid gerado pelo Maestro>",
  "parameters": {
    "data_inicial": "01/05/2026",
    "data_fim": "01/05/2026",
    "destinatario": true,
    "remetente": false,
    "empresas": ["12345"]
  }
}
```

**Aceitação do smoke:**
- Worker pega a mensagem (`docker compose logs worker | grep "Mensagem recebida"`).
- `report_start` chega no Maestro (chip `running` no painel).
- Heartbeat se mantém (`last_heartbeat_at` atualizando — sem warning de
  `noHeartbeat`).
- `report_finish` com `status=completed` (ou `completed_no_invoices` se a
  empresa não emitiu nada no dia).
- Arquivo aparece em `/mnt/fiscal/00 PLANILHA SEFAZ/<EMPRESA>/<YYYY>/<MMYYYY>/`
  com formato `DESTINATÁRIO <MMYYYY> <EMPRESA>.csv`.
- Idempotência: rerodar o mesmo `job_id` (via cancel + retry no painel)
  e ver o segundo dispatch ackear sem reprocessar (log `Descartando redelivery`).

---

## 5. Período paralelo (D+0 a D+7)

| Dia | Tkinter | Worker | O que observar |
|-----|---------|--------|----------------|
| D+0 | uso normal | smoke + 1 schedule diário 04:00 | smoke acima passa; schedule noturno completa antes do operador chegar |
| D+1–3 | uso normal pra ad-hoc | schedule diário cobre a rotina padrão | Maestro UI: `successRate24h` ≥ 95%; nenhum log `actionable=true` recorrente; arquivos no share batem com o que a Tkinter produziria |
| D+4–7 | fallback se algo der errado | carga completa | Sem regressão; tempos de batch dentro do esperado (compare com histórico da Tkinter) |
| D+7+ | **deprecate** (§7) | produção | Critérios de §6 cumpridos |

**Não rodar Tkinter e worker pra o mesmo `(empresa, período, tipo)`
simultaneamente** — ambos escrevem no mesmo destino e disputam o arquivo
final.

---

## 6. Observabilidade

Onde olhar durante o período paralelo:

- **Maestro UI** — chip de status (verde/amarelo/vermelho), filtro por
  `error_class`, chip "parcial" amarelo no header pra `PARTIAL_FAILURE`.
- **`job_logs`** com `actionable=true` (borda âmbar + ⚠) — qualquer linha
  com esse marcador exige intervenção. Investigar antes de promover.
- **Container logs**: `docker compose -f docker-compose.prod.yml logs -f worker`.
- **Arquivos do diagnóstico**: o worker grava JSONL detalhado em
  `/app/log/eventos-<run_id>.jsonl` e screenshots de erro em
  `/app/log/evidencias/`. Esses volumes vêm bind-mounted em `./log` no host.
- **Capsolver dashboard** — pra confirmar que o gasto de captcha não
  explodiu (sinal de retry agressivo ou IP block oculto).

**Sinais de regressão (rollback):**
- `successRate24h` cai > 10pp em relação à baseline da Tkinter.
- Volume de `IP_BLOCKED` consecutivo dispara circuit breaker mais de 1×/dia.
- Worker fica `failed` por `noHeartbeat` (5 min sem `check_cancellation`)
  — indica trava no Chrome ou no `move_planilha`.
- Arquivos não chegam no share (`INFRA_DESTINO_INDISPONIVEL` recorrente).

---

## 7. Rollback

Reversão é **stop do container + operador volta a usar Tkinter**:

```bash
# 1. Parar o worker (jobs em curso terminam a empresa atual via cancel).
docker compose -f docker-compose.prod.yml down

# 2. Desabilitar/pausar os schedules da automação no Maestro UI
#    (caso contrário a fila acumula mensagens sem consumer).

# 3. Operador usa Tkinter normalmente — bot-planilha-sefaz Tkinter continua
#    intocado no repo (main.py + requirements.txt) até a §8.
```

Mensagens enfileiradas durante a parada ficam em `bot-planilha-tasks`
(durable). Ao subir o worker de novo, ele consome o backlog.

---

## 8. Deprecate Tkinter (D+7+, só após §5 OK)

Quando o cutover for promovido:

1. Mover `main.py` pra `legacy/main.py` (preserva histórico) e adicionar
   nota de deprecação apontando pra `cli.py` (uso ad-hoc) e
   `worker.py` (produção).
2. Remover `tkcalendar` de `requirements.txt` (e o próprio
   `requirements.txt` se for redundante com `requirements-worker.txt` —
   manter um único arquivo evita drift).
3. Atualizar README removendo a menção a GUI nos modos de execução.
4. Atualizar a memória do projeto (`project_overview.md` /
   `project_roadmap_worker.md`) marcando o roadmap como concluído.

Até a deprecação, **não tocar em `main.py`** — é o fallback.

---

## 9. Checklist condensado

```
[ ] /mnt/fiscal montado e gravável
[ ] migration 000009 aplicada no DB do Maestro
[ ] credentials/<google-sa>.json presente no host
[ ] .env preenchido (Maestro + RabbitMQ + Capsolver + Sheet + SEFAZ urls)
[ ] docker compose -f docker-compose.prod.yml build  → OK
[ ] docker compose -f docker-compose.prod.yml up -d  → log "Worker pronto"
[ ] automação cadastrada no Maestro (queue=bot-planilha-tasks, parameterSchema §3)
[ ] smoke job §4 → completed + arquivo no share
[ ] idempotência: rerun do mesmo job_id → descarta redelivery
[ ] schedule diário D+1 → observar 7 dias §5
[ ] critérios §6 OK → deprecate Tkinter §8
```
