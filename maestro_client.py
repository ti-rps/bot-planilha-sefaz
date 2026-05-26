"""Cliente HTTP do rps-maestro pra o worker do bot-planilha-sefaz.

Wrappa as 5 rotas de `/api/v1/worker/jobs/:id/...` que o contrato exige
(ver [[reference-rps-maestro-contract]]). Espelha 1:1 a implementação
canônica do bot-xml-gms (`worker.py`) — quando este código divergir do
de lá, o bot-xml-gms vence.

Pontos do contrato que essa classe materializa:
- `X-Worker-API-Key` enviado apenas se a env tiver valor (middleware do
  Maestro tem bypass-se-vazio: dev key=="" passa sem validar).
- `get_status` é o idempotency check — chama ANTES de qualquer ação,
  descarta a mensagem (basic_ack) se `terminal: true`.
- `check_cancellation` tem dupla função: detecta pedido de cancelamento
  E atualiza `last_heartbeat_at` no Maestro (side effect server-side).
  Cadência de 15s casa com janela de 5min do retry_worker (~20 polls).
- `report_log` aceita `actionable: bool` (PR C, migration 000009) pra
  marcar linhas que exigem intervenção humana — vira borda âmbar no painel.
- `report_finish` aceita `result` como map livre; convenção `summary` +
  `partial_success` + `error_class` é responsabilidade do caller (runner).

Erros HTTP/rede:
- POST /start, /log, /finish: retornam bool (True = sucesso). Erro só vira
  log local — não interrompe execução (espelha bot-xml-gms).
- GET /status: retorna dict | None. None cobre erro de rede, 404 ou
  status não-terminal — caller assume "siga processando".
- GET /cancellation: retorna bool. 4xx (exceto 404) e 5xx propagam pra
  o CancellationWatcher tratar como transiente.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

import requests


class MaestroClient:
    """Cliente HTTP do rps-maestro pras 5 rotas de worker.

    Stateless além das URLs/keys. Pode ser instanciado por job ou
    compartilhado entre threads — `requests.request` é thread-safe.
    """

    def __init__(
        self,
        base_url: str,
        api_key: Optional[str] = None,
        *,
        timeout: float = 10.0,
        logger: Optional[logging.Logger] = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key or ""
        self.timeout = timeout
        self.logger = logger or logging.getLogger(__name__)

    def _headers(self) -> dict:
        # Bypass-se-vazio: middleware do Maestro aceita req sem header
        # quando MAESTRO_WORKER_API_KEY=="" (modo dev). Quando populada,
        # mandamos em TODAS as rotas — não dá pra autenticar metade.
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["X-Worker-API-Key"] = self.api_key
        return headers

    def _url(self, path: str) -> str:
        return f"{self.base_url}/{path.lstrip('/')}"

    def _make_request(
        self,
        method: str,
        path: str,
        payload: Optional[dict] = None,
    ) -> bool:
        url = self._url(path)
        try:
            response = requests.request(
                method=method,
                url=url,
                json=payload,
                timeout=self.timeout,
                headers=self._headers(),
            )
        except requests.exceptions.RequestException as e:
            self.logger.error(f"Erro de rede em {method} {path}: {e}")
            return False
        except Exception as e:
            self.logger.error(f"Erro inesperado em {method} {path}: {e}")
            return False

        if response.status_code in (200, 201, 204):
            return True

        self.logger.warning(
            f"{method} {path} retornou HTTP {response.status_code}: {response.text}"
        )
        return False

    def get_status(self, job_id: str) -> Optional[dict]:
        """Idempotency check. Retorna o dict do Maestro se terminal, None caso contrário.

        Chamar ANTES de qualquer outra ação no job. Se retornar dict (terminal:
        true), faz `basic_ack` no RabbitMQ e descarta a mensagem — é redelivery
        de um ack que falhou.

        - HTTP 200 + `terminal: true` → retorna dict.
        - HTTP 200 + `terminal: false` → retorna None (caller segue normal).
        - HTTP 404 → retorna None (job sumiu do Maestro).
        - Erro de rede/5xx → log warning + retorna None (não bloqueia; pior
          caso reprocessa, melhor que travar).
        """
        url = self._url(f"/api/v1/worker/jobs/{job_id}/status")
        try:
            response = requests.get(url, headers=self._headers(), timeout=self.timeout)
        except requests.exceptions.RequestException as e:
            self.logger.warning(f"get_status: erro de rede consultando job {job_id}: {e}")
            return None

        if response.status_code == 200:
            data = response.json()
            if data.get("terminal"):
                return data
            return None

        if response.status_code == 404:
            return None

        self.logger.warning(
            f"get_status: HTTP {response.status_code} para job {job_id}: {response.text}"
        )
        return None

    def report_start(self, job_id: str) -> bool:
        self.logger.info(f"Reportando início do job {job_id}")
        return self._make_request("POST", f"/api/v1/worker/jobs/{job_id}/start")

    def report_log(
        self,
        job_id: str,
        level: str,
        message: str,
        *,
        actionable: bool = False,
    ) -> bool:
        """POST /log. Levels válidos (case-sensitive, CHECK no DB):
        DEBUG, INFO, WARNING, WARN, ERROR, CRITICAL.

        `actionable=True` destaca a linha no painel (borda âmbar + ⚠) —
        usar quando operador precisa intervir (corrigir Sheet, validar share).
        Default False espelha bot-xml-gms.
        """
        payload = {"level": level, "message": message, "actionable": actionable}
        return self._make_request("POST", f"/api/v1/worker/jobs/{job_id}/log", payload)

    def check_cancellation(self, job_id: str) -> bool:
        """GET /cancellation. Dupla função: detecta cancelamento E faz heartbeat.

        Cada chamada atualiza `last_heartbeat_at` server-side — sem polling
        regular, retry_worker do Maestro marca job como `failed` após
        heartbeatTimeout=5min. Cadência 15s dá ~20 polls de margem.

        - HTTP 200 → retorna bool de `cancellation_requested`.
        - HTTP 404 → retorna False (job sumiu, não derruba bot).
        - 4xx (exceto 404) / 5xx → propaga via response.raise_for_status()
          pro CancellationWatcher logar warning e tentar de novo.
        """
        url = self._url(f"/api/v1/worker/jobs/{job_id}/cancellation")
        response = requests.get(url, headers=self._headers(), timeout=self.timeout)

        if response.status_code == 200:
            return bool(response.json().get("cancellation_requested", False))

        if response.status_code == 404:
            self.logger.warning(f"check_cancellation: job {job_id} retornou 404")
            return False

        response.raise_for_status()
        return False

    def report_finish(
        self,
        job_id: str,
        status: str,
        result: Optional[dict[str, Any]] = None,
    ) -> bool:
        """POST /finish. Status: completed | completed_no_invoices | failed | canceled.

        `result` é JSONB livre no Maestro — convenção `partial_success`,
        `summary` canônica e `error_class` vive no caller (runner).
        """
        payload = {"status": status, "result": result or {}}
        self.logger.info(f"Reportando finish do job {job_id}: status={status}")
        return self._make_request("POST", f"/api/v1/worker/jobs/{job_id}/finish", payload)
