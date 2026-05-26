"""Política de retry por error_class do bot-planilha-sefaz.

Resolve [[known-issues]] #1 (falhas em lote): hoje o batch trata qualquer
exceção como falha terminal, mesmo erros transitórios. Esta política
deixa o envelope em `runner.processar_empresa_thread` saber quais classes
retentar, quantas vezes, e com que backoff.

Convenções (alinhadas com o enum semântico do rps-maestro,
[[project-maestro-decisions]] #5):

- **Âmbar (retry):** CAPTCHA_FAILED, JOB_TIMEOUT, RATE_LIMITED, PORTAL_DOWN.
- **Vermelho (NÃO retry — actionable ou estrutural):** CREDENTIAL_INVALID,
  IP_BLOCKED, INFRA_DESTINO_INDISPONIVEL, INVALID_PARAMETERS.
- **UNKNOWN:** não retry — preserva o sintoma forense em vez de mascarar.
- IP_BLOCKED especificamente é tratado fora desta política, no circuit
  breaker do `runner.run_batch` (3 consecutivos → pula resto do batch).

Backoff é exponencial: `base * 2^(attempt-2)`, limitado por `cap`.
`attempt=1` é a primeira tentativa (sem espera); `attempt=2` é após a
primeira falha (espera `base`); `attempt=3` espera `base*2`; etc.
"""
from __future__ import annotations


# (max_attempts, base_backoff_s, cap_backoff_s)
# max_attempts inclui a tentativa inicial.
_POLICY: dict[str, tuple[int, float, float]] = {
    "CAPTCHA_FAILED": (2, 2.0, 10.0),
    "JOB_TIMEOUT":    (2, 5.0, 30.0),
    "RATE_LIMITED":   (3, 5.0, 60.0),
    "PORTAL_DOWN":    (3, 10.0, 60.0),
}


def is_retryable(error_class: str | None) -> bool:
    return error_class in _POLICY


def max_attempts(error_class: str | None) -> int:
    """Quantas tentativas totais (inclui a 1ª) pra esse error_class.

    Retorna 1 (sem retry) quando o error_class não está na política.
    """
    if error_class not in _POLICY:
        return 1
    return _POLICY[error_class][0]


def backoff_seconds(error_class: str | None, attempt: int) -> float:
    """Segundos de espera antes da tentativa `attempt` (1-indexed).

    attempt=1 → 0s (primeira tentativa). attempt=2 → base. attempt=3 → base*2.
    Capa em `cap` definido pela política. Retorna 0 quando o error_class não
    é retryable (caller já não deveria chamar).
    """
    if error_class not in _POLICY or attempt <= 1:
        return 0.0
    _, base, cap = _POLICY[error_class]
    seconds = base * (2 ** (attempt - 2))
    return min(seconds, cap)
