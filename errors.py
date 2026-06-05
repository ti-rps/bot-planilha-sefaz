"""Hierarquia de exceções tipadas do bot-planilha-sefaz.

Cada exceção carrega:
- error_class: string canônica do enum do rps-maestro (10 valores: CREDENTIAL_INVALID,
  IP_BLOCKED, CAPTCHA_FAILED, INFRA_DESTINO_INDISPONIVEL, INVALID_PARAMETERS,
  RATE_LIMITED, PORTAL_DOWN, JOB_TIMEOUT, PARTIAL_FAILURE, UNKNOWN). Ver
  docs/automations.md §5.3.1 do repo rps-maestro.
- actionable: True quando operador precisa intervir fora do worker (corrigir senha
  na Sheet, validar share, etc). Mapeia direto pro campo actionable do POST /log
  do Maestro (PR C, migration 000009).

PARTIAL_FAILURE e UNKNOWN não viram exceção — são valores que só aparecem no
result.error_class top-level: PARTIAL_FAILURE quando o batch termina parcial,
UNKNOWN quando o runner captura Exception genérica (fallback).
"""


class OperationCanceled(Exception):
    """Cancelamento cooperativo de uma empresa in-flight.

    Levantada quando o `cancel_event` do batch é detectado em um ponto seguro
    no meio do processamento de uma empresa (antes do login, no loop de CAPTCHA,
    no loop de espera do download). NÃO herda de BotPlanilhaError de propósito:
    não é falha — o runner a mapeia para "skipped"/canceled, não para "failed".
    """

    def __init__(self, message: str = "Operação cancelada", *, empresa: str | None = None):
        super().__init__(message)
        self.empresa = empresa
        self.message = message


class BotPlanilhaError(Exception):
    error_class: str = "UNKNOWN"
    actionable: bool = False

    def __init__(self, message: str, *, empresa: str | None = None):
        super().__init__(message)
        self.empresa = empresa
        self.message = message


class CredentialInvalidError(BotPlanilhaError):
    error_class = "CREDENTIAL_INVALID"
    actionable = True


class IpBlockedError(BotPlanilhaError):
    error_class = "IP_BLOCKED"
    actionable = True


class CaptchaFailedError(BotPlanilhaError):
    error_class = "CAPTCHA_FAILED"
    actionable = False


class ShareUnavailableError(BotPlanilhaError):
    error_class = "INFRA_DESTINO_INDISPONIVEL"
    actionable = True


class InvalidParametersError(BotPlanilhaError):
    error_class = "INVALID_PARAMETERS"
    actionable = True


class RateLimitedError(BotPlanilhaError):
    error_class = "RATE_LIMITED"
    actionable = False


class PortalDownError(BotPlanilhaError):
    error_class = "PORTAL_DOWN"
    actionable = False


class JobTimeoutError(BotPlanilhaError):
    error_class = "JOB_TIMEOUT"
    actionable = False
