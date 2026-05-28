"""Testes da classificação de status terminal a partir do summary (FASE 1/2).

classificar_run mapeia o resultado do batch nos 4 status terminais do
rps-maestro. É a peça que decide o que o worker reporta no report_finish.
"""
import runner


def _summary(ok=None, no_data=None, failed=None):
    return {
        "ok": list(ok or []),
        "no_data": list(no_data or []),
        "failed": list(failed or []),
    }


def _falha(empresa, error_class):
    return {"empresa": empresa, "error_class": error_class,
            "error_type": "X", "message": "m"}


def test_tudo_ok_vira_completed():
    status, ec, partial = runner.classificar_run(_summary(ok=["A", "B"]))
    assert (status, ec, partial) == ("completed", None, False)


def test_tudo_no_data_vira_completed_no_invoices():
    status, ec, partial = runner.classificar_run(_summary(no_data=["A"]))
    assert (status, ec, partial) == ("completed_no_invoices", None, False)


def test_todas_falham_mesma_classe_vira_failed_com_a_classe():
    s = _summary(failed=[_falha("A", "CREDENTIAL_INVALID"),
                         _falha("B", "CREDENTIAL_INVALID")])
    status, ec, partial = runner.classificar_run(s)
    assert (status, ec, partial) == ("failed", "CREDENTIAL_INVALID", False)


def test_todas_falham_infra_mista_vira_failed_portal_down():
    s = _summary(failed=[_falha("A", "IP_BLOCKED"),
                         _falha("B", "INFRA_DESTINO_INDISPONIVEL")])
    status, ec, partial = runner.classificar_run(s)
    assert (status, ec, partial) == ("failed", "PORTAL_DOWN", False)


def test_todas_falham_classes_dispares_vira_failed_unknown():
    s = _summary(failed=[_falha("A", "CREDENTIAL_INVALID"),
                         _falha("B", "CAPTCHA_FAILED")])
    status, ec, partial = runner.classificar_run(s)
    assert (status, ec, partial) == ("failed", "UNKNOWN", False)


def test_misto_ok_e_falha_vira_partial_failure():
    s = _summary(ok=["A"], failed=[_falha("B", "CAPTCHA_FAILED")])
    status, ec, partial = runner.classificar_run(s)
    assert (status, ec, partial) == ("completed", "PARTIAL_FAILURE", True)


def test_cancelado_tem_prioridade():
    s = _summary(ok=["A"], failed=[_falha("B", "X")])
    status, ec, partial = runner.classificar_run(s, canceled=True)
    assert (status, ec, partial) == ("canceled", None, False)


def test_summary_vazio_vira_failed_unknown():
    status, ec, partial = runner.classificar_run(_summary())
    assert (status, ec, partial) == ("failed", "UNKNOWN", False)
