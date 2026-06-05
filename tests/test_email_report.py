"""Relatório de credenciais inválidas por e-mail (2026-06-05).

Sem rede: o smtplib.SMTP é trocado por um fake que captura a mensagem.
"""
import logging

import pytest

import email_report


logger = logging.getLogger("test-email")

FALHAS = [
    {"empresa": "EMP A", "login": "****99", "timestamp": "2026-06-05T10:00:00Z",
     "message": "Usuário ou senha inválidos"},
    {"empresa": "EMP B", "login": "****11", "timestamp": "2026-06-05T10:01:00Z",
     "message": "Usuário ou senha inválidos"},
]


@pytest.fixture(autouse=True)
def _limpa_smtp_env(monkeypatch):
    for k in ("SMTP_HOST", "SMTP_USER", "SMTP_PASSWORD", "SMTP_FROM",
              "SMTP_PORT", "SMTP_USE_TLS", "EMAIL_CREDENCIAIS_DESTINO",
              "EMAIL_HOST", "EMAIL_USER", "EMAIL_PASSWORD", "EMAIL_FROM"):
        monkeypatch.delenv(k, raising=False)


class _FakeSMTP:
    """Context manager que finge um servidor SMTP e grava o que recebeu."""
    enviados = []
    last = None

    def __init__(self, host, port, timeout=None):
        self.host, self.port = host, port
        self.started_tls = False
        self.logged = None
        _FakeSMTP.last = self

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def starttls(self, context=None):
        self.started_tls = True

    def login(self, user, password):
        self.logged = (user, password)

    def send_message(self, msg):
        _FakeSMTP.enviados.append(msg)


def test_pulado_sem_falhas():
    assert email_report.enviar_relatorio_credenciais([], destino="x@y.com", logger=logger) is False


def test_pulado_sem_smtp_configurado():
    # SMTP_* ausentes (fixture limpa) → não envia, devolve False, não levanta.
    assert email_report.enviar_relatorio_credenciais(FALHAS, destino="x@y.com", logger=logger) is False


def test_destino_padrao_usa_env(monkeypatch):
    assert email_report.destino_padrao() == "fiscal@rpscontabil.com.br"
    monkeypatch.setenv("EMAIL_CREDENCIAIS_DESTINO", "outro@rps.com.br")
    assert email_report.destino_padrao() == "outro@rps.com.br"


def test_envia_com_smtp_fake(monkeypatch):
    monkeypatch.setenv("SMTP_HOST", "smtp.fake")
    monkeypatch.setenv("SMTP_USER", "bot@rps.com.br")
    monkeypatch.setenv("SMTP_PASSWORD", "segredo")
    _FakeSMTP.enviados.clear()
    monkeypatch.setattr(email_report.smtplib, "SMTP", _FakeSMTP)

    ok = email_report.enviar_relatorio_credenciais(
        FALHAS, destino="fiscal@rpscontabil.com.br", logger=logger,
        periodo="01/05/2026 a 31/05/2026", job_id="job-7",
    )
    assert ok is True
    assert len(_FakeSMTP.enviados) == 1
    msg = _FakeSMTP.enviados[0]
    assert msg["To"] == "fiscal@rpscontabil.com.br"
    assert msg["From"] == "bot@rps.com.br"
    assert "job-7" in msg["Subject"]
    corpo = msg.get_content()
    assert "EMP A" in corpo and "EMP B" in corpo
    assert "****99" in corpo            # login mascarado no corpo
    assert "01/05/2026 a 31/05/2026" in corpo


def test_reaproveita_credenciais_email_user_password(monkeypatch):
    # Só EMAIL_USER/EMAIL_PASSWORD (convenção do BergBot), sem SMTP_*: deve
    # funcionar e assumir smtp.gmail.com como host por padrão.
    monkeypatch.setenv("EMAIL_USER", "fiscal@rpscontabil.com.br")
    monkeypatch.setenv("EMAIL_PASSWORD", "senha-de-app")
    _FakeSMTP.enviados.clear()
    monkeypatch.setattr(email_report.smtplib, "SMTP", _FakeSMTP)

    ok = email_report.enviar_relatorio_credenciais(
        FALHAS, destino="fiscal@rpscontabil.com.br", logger=logger,
    )
    assert ok is True
    assert _FakeSMTP.last.host == "smtp.gmail.com"        # default p/ Gmail
    assert _FakeSMTP.last.logged == ("fiscal@rpscontabil.com.br", "senha-de-app")
    assert _FakeSMTP.enviados[0]["From"] == "fiscal@rpscontabil.com.br"
