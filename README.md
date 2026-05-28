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

## Desenvolvimento / execução manual

```bash
# CLI headless — 1 empresa por código, período, modo destinatário
python cli.py --destinatario --data-inicial 01/05/2026 --data-fim 31/05/2026 12345

# todas as empresas com Senha Robô, output JSON
python cli.py --destinatario --data-inicial 01/05/2026 --data-fim 31/05/2026 --all --json
```
