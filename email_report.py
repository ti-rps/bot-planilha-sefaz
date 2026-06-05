"""Relatório por e-mail das empresas com credenciais inválidas (2026-06-05).

Pedido pelo Enzzo: ao fim do lote, as empresas que falharam no login com
CREDENTIAL_INVALID viram uma lista detalhada enviada por e-mail (default
fiscal@rpscontabil.com.br). Características:

- **Opcional:** ligado/desligado por parâmetro da requisição HTTP
  (`enviar_email_credenciais`), lido no worker a partir da mensagem da fila.
- **Destinatário configurável:** por parâmetro (`email_credenciais_destino`),
  com fallback pra EMAIL_CREDENCIAIS_DESTINO do .env e, por fim, o default.
- **SMTP (host/porta/user/senha) vem do .env** — segredo, nunca da requisição.
- **Best-effort:** nunca levanta exceção; um e-mail que falha NÃO derruba o job.

Só depende da stdlib (smtplib/ssl/email) — sem dep nova no container.
"""
from __future__ import annotations

import os
import smtplib
import ssl
from email.message import EmailMessage
from typing import Optional


_DESTINO_FALLBACK = "fiscal@rpscontabil.com.br"


def destino_padrao() -> str:
    """Destinatário default quando a requisição não manda um."""
    return os.getenv("EMAIL_CREDENCIAIS_DESTINO", _DESTINO_FALLBACK)


def _smtp_config() -> dict:
    """Lê a config SMTP do ambiente.

    Aceita tanto `SMTP_*` quanto os nomes `EMAIL_*` usados por outras automações
    da casa (ex.: BergBot) — assim dá pra reaproveitar a MESMA conta/segredo sem
    renomear variável. Quando não há host explícito mas há credencial, assume
    `smtp.gmail.com` (a conta fiscal@ é Google Workspace e autentica com senha de
    app), de modo que só `EMAIL_USER` + `EMAIL_PASSWORD` já bastam.
    """
    user = os.getenv("SMTP_USER") or os.getenv("EMAIL_USER")
    password = os.getenv("SMTP_PASSWORD") or os.getenv("EMAIL_PASSWORD")
    host = os.getenv("SMTP_HOST") or os.getenv("EMAIL_HOST")
    if not host and user:
        host = "smtp.gmail.com"
    return {
        "host": host,
        "port": int(os.getenv("SMTP_PORT", "587")),
        "user": user,
        "password": password,
        "from_addr": os.getenv("SMTP_FROM") or os.getenv("EMAIL_FROM") or user,
        "use_tls": os.getenv("SMTP_USE_TLS", "true").strip().lower() in ("1", "true", "yes"),
    }


def _formatar_corpo(falhas: list, periodo: Optional[str] = None) -> str:
    linhas = [
        "As empresas abaixo falharam no login do portal SEFAZ-BA por "
        "usuário/senha inválidos.",
        "Corrija as credenciais na planilha de empresas antes de reprocessar.",
        "",
    ]
    if periodo:
        linhas.append(f"Período do lote: {periodo}")
    linhas.append(f"Total: {len(falhas)} empresa(s) com credencial inválida")
    linhas.append("")
    for i, f in enumerate(falhas, 1):
        linhas.append(f"{i}. {f.get('empresa', '(sem nome)')}")
        if f.get("login"):
            linhas.append(f"   Login (mascarado): {f['login']}")
        if f.get("timestamp"):
            linhas.append(f"   Quando: {f['timestamp']}")
        if f.get("message"):
            linhas.append(f"   Detalhe: {f['message']}")
        linhas.append("")
    linhas.append("— bot-planilha-sefaz (automação SEFAZ-BA)")
    return "\n".join(linhas)


def enviar_relatorio_credenciais(falhas, *, destino, logger, periodo=None, job_id=None) -> bool:
    """Envia o e-mail com a lista de credenciais inválidas. Devolve True se enviou.

    `falhas`: list de dicts no shape de summary["failed"] (empresa, login,
    timestamp, message). Best-effort: loga e devolve False em vez de levantar.
    """
    if not falhas:
        return False

    cfg = _smtp_config()
    if not cfg["host"] or not cfg["user"] or not cfg["password"]:
        logger.warning(
            "E-mail de credenciais NÃO enviado: SMTP não configurado "
            "(defina SMTP_HOST, SMTP_USER e SMTP_PASSWORD no .env)."
        )
        return False
    if not destino:
        logger.warning("E-mail de credenciais NÃO enviado: destinatário vazio.")
        return False

    assunto = f"[bot-planilha-sefaz] {len(falhas)} empresa(s) com credencial inválida"
    if job_id:
        assunto += f" — job {job_id}"

    msg = EmailMessage()
    msg["Subject"] = assunto
    msg["From"] = cfg["from_addr"]
    msg["To"] = ", ".join(destino) if isinstance(destino, (list, tuple)) else str(destino)
    msg.set_content(_formatar_corpo(falhas, periodo=periodo))

    try:
        with smtplib.SMTP(cfg["host"], cfg["port"], timeout=30) as servidor:
            if cfg["use_tls"]:
                servidor.starttls(context=ssl.create_default_context())
            servidor.login(cfg["user"], cfg["password"])
            servidor.send_message(msg)
        logger.info(
            f"E-mail de credenciais inválidas enviado para {msg['To']} "
            f"({len(falhas)} empresa(s))."
        )
        return True
    except Exception as e:
        logger.error(f"Falha ao enviar e-mail de credenciais inválidas (seguindo): {e}")
        return False
