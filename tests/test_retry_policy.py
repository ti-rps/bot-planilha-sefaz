"""Testes da política de retry por error_class (FASE 2)."""
import pytest

import retry_policy as rp


@pytest.mark.parametrize("ec", ["CAPTCHA_FAILED", "JOB_TIMEOUT", "RATE_LIMITED", "PORTAL_DOWN"])
def test_classes_ambar_sao_retryable(ec):
    assert rp.is_retryable(ec) is True
    assert rp.max_attempts(ec) >= 2


@pytest.mark.parametrize(
    "ec",
    ["CREDENTIAL_INVALID", "IP_BLOCKED", "INFRA_DESTINO_INDISPONIVEL",
     "INVALID_PARAMETERS", "UNKNOWN", None, "INEXISTENTE"],
)
def test_classes_nao_retryable(ec):
    assert rp.is_retryable(ec) is False
    assert rp.max_attempts(ec) == 1
    # Sem retry → sem backoff.
    assert rp.backoff_seconds(ec, 2) == 0.0


def test_primeira_tentativa_sem_espera():
    assert rp.backoff_seconds("RATE_LIMITED", 1) == 0.0


def test_backoff_exponencial():
    # base=5, cap=60. attempt=2 → 5; attempt=3 → 10; attempt=4 → 20.
    assert rp.backoff_seconds("RATE_LIMITED", 2) == 5.0
    assert rp.backoff_seconds("RATE_LIMITED", 3) == 10.0
    assert rp.backoff_seconds("RATE_LIMITED", 4) == 20.0


def test_backoff_respeita_cap():
    # PORTAL_DOWN: base=10, cap=60. attempt grande satura no cap.
    assert rp.backoff_seconds("PORTAL_DOWN", 10) == 60.0


# --- passe de retry no fim do lote (2026-06-05) ---

@pytest.mark.parametrize("ec", ["CREDENTIAL_INVALID", "INVALID_PARAMETERS", "IP_BLOCKED"])
def test_passe_exclui_credencial_parametros_ip(ec):
    assert rp.retentavel_no_lote(ec) is False


@pytest.mark.parametrize(
    "ec",
    ["CAPTCHA_FAILED", "JOB_TIMEOUT", "RATE_LIMITED", "PORTAL_DOWN",
     "INFRA_DESTINO_INDISPONIVEL", "UNKNOWN", None],
)
def test_passe_retenta_o_resto(ec):
    # Tudo que não é credencial/parâmetros/IP entra na passe — inclusive UNKNOWN.
    assert rp.retentavel_no_lote(ec) is True
