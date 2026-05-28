"""Testes do worker.process_message com fakes (sem broker/browser/rede).

Constrói um RabbitMQWorker real (o __init__ não conecta em nada) e injeta:
- FakeMaestro no lugar do MaestroClient (grava chamadas, sem HTTP);
- _ensure_df / _resolver_lista_empresas / _run_batch_with_heartbeat
  monkeypatchados pra não tocar Sheet, Selenium nem RabbitMQ.

Assim exercitamos o contrato do handler: idempotência, validação de
parâmetros, report_start/finish e semântica de ack/nack.
"""
import json

import pandas as pd
import pytest

import worker as worker_mod
import baixar_planilha_sefaz as bs


# ----------------------------- fakes -----------------------------

class FakeMaestro:
    """Substitui MaestroClient. Grava chamadas; não faz rede."""

    def __init__(self, terminal_status=None):
        self._terminal = terminal_status  # dict | None
        self.starts = []
        self.logs = []
        self.finishes = []  # (job_id, status, result)

    def get_status(self, job_id):
        return self._terminal

    def report_start(self, job_id):
        self.starts.append(job_id)
        return True

    def report_log(self, job_id, level, message, *, actionable=False):
        self.logs.append((job_id, level, message, actionable))
        return True

    def report_finish(self, job_id, status, result=None):
        self.finishes.append((job_id, status, result))
        return True

    def check_cancellation(self, job_id):
        return False


class FakeMethod:
    def __init__(self, tag=1):
        self.delivery_tag = tag


class FakeChannel:
    """Grava acks/nacks no lugar do pika channel."""

    def __init__(self):
        self.acks = []
        self.nacks = []  # (tag, requeue)

    def basic_ack(self, delivery_tag):
        self.acks.append(delivery_tag)

    def basic_nack(self, delivery_tag, requeue):
        self.nacks.append((delivery_tag, requeue))


# ----------------------------- fixtures -----------------------------

@pytest.fixture
def make_worker(monkeypatch):
    """Fábrica de worker isolado. `batch_result` é o que o batch devolve."""
    # diag escreve arquivos JSONL — neutraliza.
    monkeypatch.setattr(worker_mod.diag, "evento", lambda *a, **k: None)
    monkeypatch.setattr(worker_mod.diag, "gerar_run_id", lambda: "run-test")

    def _factory(terminal_status=None, batch_result=None, resolved=("EMP A",)):
        w = worker_mod.RabbitMQWorker()
        w.client = FakeMaestro(terminal_status=terminal_status)
        # Sheet: DataFrame não-vazio (conteúdo irrelevante — resolver é fake).
        monkeypatch.setattr(w, "_ensure_df", lambda: pd.DataFrame({"x": [1]}))
        monkeypatch.setattr(w, "_resolver_lista_empresas", lambda df, p: list(resolved))
        if batch_result is not None:
            monkeypatch.setattr(w, "_run_batch_with_heartbeat",
                                lambda *a, **k: batch_result)
        return w

    return _factory


def _msg(job_id="job-1", **params):
    base = {"data_inicial": "01/05/2026", "data_fim": "31/05/2026",
            "destinatario": True, "remetente": False}
    base.update(params)
    return json.dumps({"job_id": job_id, "parameters": base}).encode()


def _summary(ok=None, no_data=None, failed=None):
    return {"ok": list(ok or []), "no_data": list(no_data or []),
            "failed": list(failed or [])}


# ----------------------------- _validar_parametros -----------------------------

def test_validar_parametros_ok(make_worker):
    w = make_worker()
    di, dfim, dest, remet, emp = w._validar_parametros(
        {"data_inicial": "01/05/2026", "data_fim": "31/05/2026", "destinatario": True})
    assert (di, dfim, dest, remet, emp) == ("01/05/2026", "31/05/2026", True, False, [])


@pytest.mark.parametrize("params", [
    {"data_fim": "31/05/2026", "destinatario": True},                    # falta data_inicial
    {"data_inicial": "1/5/26", "data_fim": "31/05/2026", "destinatario": True},  # formato
    {"data_inicial": "01/05/2026", "data_fim": "31/05/2026"},            # nem dest nem remet
    {"data_inicial": "01/05/2026", "data_fim": "31/05/2026", "destinatario": True, "empresas": "ABC"},  # empresas não-lista
])
def test_validar_parametros_invalidos(make_worker, params):
    w = make_worker()
    with pytest.raises(worker_mod.JobValidationError):
        w._validar_parametros(params)


# ----------------------------- _detectar_ip_block -----------------------------

def test_ip_block_exige_dois_sinais():
    # 1 sinal só não dispara (reduz falso positivo).
    assert bs._detectar_ip_block("acesso negado") is False
    # 2+ sinais disparam.
    assert bs._detectar_ip_block("acesso negado: muitas tentativas") is True
    assert bs._detectar_ip_block("texto qualquer sem sinal") is False


# ----------------------------- process_message -----------------------------

def test_happy_path_completed(make_worker):
    w = make_worker(batch_result=(_summary(ok=["EMP A"]), "completed", None, False))
    ch, method = FakeChannel(), FakeMethod()

    w.process_message(ch, method, None, _msg())

    assert w.client.starts == ["job-1"]
    assert len(w.client.finishes) == 1
    job_id, status, result = w.client.finishes[0]
    assert status == "completed"
    assert result["summary"]["ok"] == ["EMP A"]
    assert ch.acks == [1] and ch.nacks == []  # ack, sem nack


def test_idempotencia_descarta_redelivery(make_worker):
    # Job já terminal no Maestro → ack e descarta, sem reprocessar.
    w = make_worker(terminal_status={"status": "completed", "terminal": True})
    ch, method = FakeChannel(), FakeMethod(tag=7)

    w.process_message(ch, method, None, _msg())

    assert w.client.starts == []        # não reiniciou
    assert w.client.finishes == []      # não refinalizou
    assert ch.acks == [7] and ch.nacks == []


def test_parametros_invalidos_nack_sem_requeue(make_worker):
    w = make_worker()
    ch, method = FakeChannel(), FakeMethod(tag=3)
    # Sem data_inicial → JobValidationError no handler.
    body = json.dumps({"job_id": "job-x",
                       "parameters": {"data_fim": "31/05/2026", "destinatario": True}}).encode()

    w.process_message(ch, method, None, body)

    assert len(w.client.finishes) == 1
    _, status, result = w.client.finishes[0]
    assert status == "failed"
    assert result["error_class"] == "INVALID_PARAMETERS"
    assert ch.nacks == [(3, False)] and ch.acks == []  # permanente: não reenfileira


def test_json_invalido_nack_sem_requeue(make_worker):
    w = make_worker()
    ch, method = FakeChannel(), FakeMethod(tag=9)

    w.process_message(ch, method, None, b"{nao eh json")

    assert ch.nacks == [(9, False)] and ch.acks == []


def test_partial_failure_reporta_partial(make_worker):
    summary = _summary(ok=["EMP A"], failed=[
        {"empresa": "EMP B", "error_class": "CAPTCHA_FAILED",
         "error_type": "CaptchaFailedError", "message": "falhou"}])
    w = make_worker(batch_result=(summary, "completed", "PARTIAL_FAILURE", True))
    ch, method = FakeChannel(), FakeMethod()

    w.process_message(ch, method, None, _msg())

    _, status, result = w.client.finishes[0]
    assert status == "completed"
    assert result["partial_success"] is True
    assert result["error_class"] == "PARTIAL_FAILURE"
    assert ch.acks == [1]
