"""Passe de retry no fim do lote (2026-06-05).

Exercita run_batch monkeypatchando `processar_empresa_thread` (sem Selenium/df):
cada empresa devolve um resultado roteirizado por nome + nº de chamadas. Valida
que falhas transitórias são reprocessadas, que credencial/IP NÃO são, e o teto
de tentativas (1 + RETRY_PASSES).
"""
import logging

import runner


logger = logging.getLogger("test-retry-pass")


def _ev(nome):
    # shape (Código, RAZÃO SOCIAL, CNPJ, Status, Contribuinte)
    return (nome, nome, "00000000000000", "ATIVO", "NAO")


def _rodar(monkeypatch, roteiro):
    """roteiro: fn(razao, n_chamada) -> dict canônico. Devolve (summary, status, ec, partial)."""
    chamadas = {}

    def fake_thread(ev, di, dfim, tipo, df, lg, run_id=None, cancel_event=None):
        razao = ev[1]
        chamadas[razao] = chamadas.get(razao, 0) + 1
        return roteiro(razao, chamadas[razao])

    monkeypatch.setattr(runner, "processar_empresa_thread", fake_thread)
    empresas = [_ev("EMP_OK"), _ev("EMP_FLAKY"), _ev("EMP_CRED"),
                _ev("EMP_IP"), _ev("EMP_DEAD")]
    out = runner.run_batch(
        empresas, "01/05/2026", "31/05/2026", True, False,
        df=None, logger=logger, max_workers=2, run_id="run-test",
    )
    return out, chamadas


def _roteiro(razao, n):
    if razao == "EMP_OK":
        return runner._resultado_empresa(razao, status="ok")
    if razao == "EMP_FLAKY":
        # falha transitória na 1ª, recupera na 2ª (passe de retry).
        if n == 1:
            return runner._resultado_empresa(razao, status="failed",
                                             error_class="JOB_TIMEOUT",
                                             error_type="JobTimeoutError", message="timeout")
        return runner._resultado_empresa(razao, status="ok")
    if razao == "EMP_CRED":
        return runner._resultado_empresa(razao, status="failed",
                                         error_class="CREDENTIAL_INVALID",
                                         error_type="CredentialInvalidError",
                                         message="senha", actionable=True,
                                         login="****99", timestamp="2026-06-05T00:00:00Z")
    if razao == "EMP_IP":
        return runner._resultado_empresa(razao, status="failed",
                                         error_class="IP_BLOCKED",
                                         error_type="IpBlockedError", message="bloqueado")
    # EMP_DEAD: falha transitória que NUNCA recupera.
    return runner._resultado_empresa(razao, status="failed",
                                     error_class="PORTAL_DOWN",
                                     error_type="PortalDownError", message="OPS")


def test_passe_recupera_flaky_e_respeita_exclusoes(monkeypatch):
    (summary, status, ec, partial), chamadas = _rodar(monkeypatch, _roteiro)

    # OK de primeira.
    assert "EMP_OK" in summary["ok"]
    assert chamadas["EMP_OK"] == 1

    # Flaky recuperou na passe de retry (2 chamadas: inicial + 1 retry).
    assert "EMP_FLAKY" in summary["ok"]
    assert chamadas["EMP_FLAKY"] == 2

    # Credencial NÃO é retentada (1 chamada só) e fica em failed.
    assert chamadas["EMP_CRED"] == 1
    # IP bloqueado NÃO é retentado (1 chamada só).
    assert chamadas["EMP_IP"] == 1

    # Transitória que nunca recupera esgota 1 + RETRY_PASSES tentativas.
    assert chamadas["EMP_DEAD"] == 1 + runner._RETRY_PASSES

    falhas = {f["empresa"] for f in summary["failed"]}
    assert falhas == {"EMP_CRED", "EMP_IP", "EMP_DEAD"}

    # Misto (ok + falhas) → completed + partial_success.
    assert status == "completed"
    assert partial is True


def test_credencial_carrega_login_e_timestamp_no_summary(monkeypatch):
    (summary, *_), _ = _rodar(monkeypatch, _roteiro)
    cred = next(f for f in summary["failed"] if f["empresa"] == "EMP_CRED")
    # Detalhes que o e-mail usa precisam sobreviver até o summary.
    assert cred["login"] == "****99"
    assert cred["timestamp"] == "2026-06-05T00:00:00Z"
    assert cred["error_class"] == "CREDENTIAL_INVALID"


def test_sem_falhas_nao_dispara_passe(monkeypatch):
    chamadas = {}

    def fake_thread(ev, *a, **k):
        chamadas[ev[1]] = chamadas.get(ev[1], 0) + 1
        return runner._resultado_empresa(ev[1], status="ok")

    monkeypatch.setattr(runner, "processar_empresa_thread", fake_thread)
    summary, status, ec, partial = runner.run_batch(
        [_ev("A"), _ev("B")], "01/05/2026", "31/05/2026", True, False,
        df=None, logger=logger, max_workers=2,
    )
    assert chamadas == {"A": 1, "B": 1}      # nenhuma passe extra
    assert status == "completed" and not partial
    assert set(summary["ok"]) == {"A", "B"}
