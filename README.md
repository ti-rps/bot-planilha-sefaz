# bot-planilha-sefaz

Baixa planilhas (CSV) de notas fiscais no portal SEFAZ-BA por empresa e move
para o drive de rede fiscal. Roda em três modos sobre o mesmo backend
(`runner.run_batch`):

- **GUI** (`main.py`) — Tkinter, uso desktop interativo. _Sai com a migração._
- **CLI** (`cli.py`) — headless, sem display. Para testes e execução manual.
- **Worker** (`worker.py`) — consome a fila RabbitMQ do **rps-maestro** e
  reporta status/logs via API HTTP. Modo de produção (Docker).

A fonte viva de empresas e credenciais é uma Google Sheet (login, senha robô,
CNPJ, flag contribuinte). O CAPTCHA é resolvido via Capsolver.

## Deploy em produção (Docker — SRVRPS03)

O worker roda em container conectado ao RabbitMQ/API do rps-maestro.
Para o playbook completo de cutover (pré-flight, `parameterSchema` do
Maestro, smoke job, período paralelo, rollback, deprecação da Tkinter)
ver [CUTOVER.md](CUTOVER.md).

### Pré-requisitos no host

1. **Share CIFS** `\\SRVDOC01\REDE\FISCAL` montado em `/mnt/fiscal` via
   `/etc/fstab` (o container faz bind mount, sem privilégio). Validar antes
   de subir — `move_planilha` marca `INFRA_DESTINO_INDISPONIVEL` se o destino
   sumir.
2. **Credencial Google** em `credentials/citric-nimbus-436114-g8-daacef9f0900.json`
   (gitignored — copiar manualmente).
3. **`.env`** preenchido a partir de [`.env.example`](.env.example).
4. **Migration 000009** (`job_logs.actionable`) aplicada no DB do Maestro
   (ALTER manual em ambiente existente).

### Subir

```bash
docker compose -f docker-compose.prod.yml up -d --build
docker compose -f docker-compose.prod.yml logs -f worker
```

O worker conecta no broker, declara a fila `bot-planilha-tasks` (durable, com
`x-dead-letter-exchange: maestro.dlx`) e aguarda jobs. `SIGTERM` (de
`docker stop`) é repassado pelo tini e fecha a conexão sem deixar job órfão.

### Imagem

`python:3.12-slim-bookworm` + `chromium`/`chromium-driver` do apt (versões
casadas, sem repo externo). Roda como usuário não-root `worker` (uid 10001).
Chrome em `--headless=new` via `HEADLESS=true`.

### Parâmetros do job (mensagem da fila)

| Campo          | Tipo        | Obrigatório | Default |
|----------------|-------------|-------------|---------|
| `data_inicial` | `dd/MM/yyyy`| sim         | —       |
| `data_fim`     | `dd/MM/yyyy`| sim         | —       |
| `destinatario` | bool        | um dos dois | false   |
| `remetente`    | bool        | um dos dois | false   |
| `empresas`     | list[str]   | não         | vazio = todas com Senha Robô |
| `enviar_email_credenciais`  | bool      | não | false |
| `email_credenciais_destino` | str/list  | não | `EMAIL_CREDENCIAIS_DESTINO` do `.env` |

> Os dois últimos controlam o [relatório de credenciais inválidas por e-mail](#relatório-de-credenciais-inválidas-por-e-mail-opcional).
> Para a UI do Maestro expô-los, eles precisam estar no `parameterSchema` da
> automação (mudança no rps-maestro). O worker já trata a ausência: sem
> `enviar_email_credenciais=true`, nada é enviado.

### Relatório de credenciais inválidas por e-mail (opcional)

Ao fim de um lote, as empresas que falharam no login por **usuário/senha
inválidos** (`error_class = CREDENTIAL_INVALID`) podem ser enviadas num e-mail
com a lista detalhada (empresa, login mascarado, horário, motivo). Útil porque
senha errada **não é retentada** (e 3 tentativas erradas bloqueiam o IP na
SEFAZ) — o operador recebe a lista pra corrigir na planilha.

**Como ligar** (por job, via parâmetros da requisição no Maestro):

- `enviar_email_credenciais: true` — liga o envio para aquele job.
- `email_credenciais_destino: "fulano@rps.com.br"` — opcional; sobrescreve o
  destino default. Aceita string ou lista de e-mails.

**SMTP** (segredo — fica só no `.env` do worker, nunca na requisição):

```bash
SMTP_HOST=smtp.office365.com   # ou smtp.gmail.com
SMTP_PORT=587
SMTP_USER=conta@rps.com.br
SMTP_PASSWORD=...              # senha de APP (Gmail/O365 com 2FA), não a senha normal
SMTP_FROM=                     # opcional; default = SMTP_USER
SMTP_USE_TLS=true              # STARTTLS na porta 587
EMAIL_CREDENCIAIS_DESTINO=fiscal@rpscontabil.com.br   # default se o request não mandar
```

**Reaproveitando a conta de outro bot (ex.: BergBot):** o worker também aceita os
nomes `EMAIL_USER` / `EMAIL_PASSWORD` (e `EMAIL_HOST` / `EMAIL_FROM`) usados pelas
outras automações — então dá pra copiar o mesmo bloco. Sem host explícito, assume
`smtp.gmail.com`. Ou seja, para a conta Gmail/Workspace `fiscal@`, basta:

```bash
EMAIL_USER=fiscal@rpscontabil.com.br
EMAIL_PASSWORD=<senha-de-app-do-gmail>     # 16 chars, gerada nas configs da conta Google
```

**Comportamento / garantias:**

- **Desligado por default.** Sem `enviar_email_credenciais=true`, não envia.
- **Sem SMTP configurado** (`SMTP_HOST`/`SMTP_USER`/`SMTP_PASSWORD` vazios) → o
  envio é **pulado** com um aviso no log; o job **não falha**.
- **Best-effort:** qualquer erro no envio é logado e ignorado — nunca altera o
  status do job (o e-mail sai depois do `report_finish`/ack).
- Mexeu só no `.env`? Basta `docker compose -f docker-compose.prod.yml up -d`
  (recria o container; **não** precisa rebuildar a imagem).

### Tuning (opcional)

| Variável | Default | O que faz |
|----------|---------|-----------|
| `RETRY_PASSES` | `2` | Passes extras no fim do lote para falhas transitórias (total = 1 + N tentativas). Não retenta credencial/parâmetros/IP bloqueado. |
| `CANCELLATION_POLL_INTERVAL_S` | `5` | Frequência (s) com que o worker checa cancelamento no Maestro. Também serve de heartbeat. |

#### Campo `empresas` — como informar

- É uma **lista de texto** (array JSON de strings), **um identificador por item**.
  Cada item é tratado como **string** — o worker faz `str()` antes de resolver —
  então o código no formato `1061-1` (xxx-x) funciona normalmente (o hífen já o
  torna texto). **Não** mande uma única string com vírgulas; tem que ser lista.
- Cada item casa primeiro com a coluna **`Código`** da Sheet, de forma **exata**
  (só normaliza maiúsc/minúsc e espaços). Escreva idêntico ao da planilha:
  `1061-1` — `1061` ou `10611` **não** casam. Se não bater nenhum código, tenta
  como **substring da `RAZÃO SOCIAL`**.
- **Cada item precisa bater em exatamente UMA empresa.** Código duplicado na
  Sheet → erro "ambíguo"; código inexistente → erro — em ambos o job termina
  como `INVALID_PARAMETERS`.
- **Vazio/ausente** = processa **todas** as empresas com `Senha Robô` preenchida.

## Desenvolvimento / execução manual

```bash
# CLI headless — 1 empresa por código, período, modo destinatário
python cli.py --destinatario --data-inicial 01/05/2026 --data-fim 31/05/2026 12345

# todas as empresas com Senha Robô, output JSON
python cli.py --destinatario --data-inicial 01/05/2026 --data-fim 31/05/2026 --all --json
```
