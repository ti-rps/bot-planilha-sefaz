"""Consumer RabbitMQ do bot-planilha-sefaz (FASE 5).

Espelha estruturalmente `bot-xml-gms/worker.py` (referência canônica em
[[reference-bot-xml-gms-patterns]]). Conecta no broker do rps-maestro,
processa uma mensagem por vez (prefetch=1) e cumpre o contrato HTTP de
worker:

1. **Idempotency check** via `MaestroClient.get_status` ANTES de tudo —
   se o job já está terminal no Maestro (redelivery por ack falhado),
   basic_ack e descarta sem reprocessar.
2. **`report_start`** marca o job como running.
3. **`CancellationWatcher`** poll de 15s do `/cancellation` — dupla
   função: detecta pedido de cancelamento via UI e mantém
   `last_heartbeat_at` server-side (impede `failed` por timeout do
   retry_worker do Maestro).
4. **`runner.run_batch`** roda em thread separada enquanto a main thread
   drena `connection.process_data_events` (mantém heartbeat do RabbitMQ
   — default 600s — vivo durante batches longos).
5. **`report_finish`** com `result` na shape canônica (status terminal +
   `summary` + `partial_success` + `error_class` quando aplicável).
6. **Transient vs permanent**: ConnectionError/TimeoutError → requeue=True.
   Resto → requeue=False (DLQ ou descarte).

Schema esperado em `parameters` (definido em
[[project-roadmap-worker]] / [[project-maestro-decisions]] #1):
    - `data_inicial`: str dd/MM/yyyy (obrigatório)
    - `data_fim`: str dd/MM/yyyy (obrigatório)
    - `destinatario`: bool (default false)
    - `remetente`: bool (default false) — pelo menos um dos dois true
    - `empresas`: list[str] (opcional). Vazio/ausente = todas com 'Senha
      Robô' preenchida na Sheet.
"""
from __future__ import annotations

import json
import logging
import logging.config
import os
import signal
import socket
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import pika
import requests
from dotenv import load_dotenv
from gspread.exceptions import APIError

import diagnostico as diag
import ler_planilha as lp
import runner
from cancellation_watcher import CancellationWatcher
from empresa_resolver import resolver_empresas
from maestro_client import MaestroClient


load_dotenv()


# Cadência do polling de cancelamento. 15s casa com janela de 5min do
# retry_worker do Maestro (~20 polls de margem). Mesmo valor do bot-xml-gms.
_CANCELLATION_POLL_INTERVAL = 15.0

# Drenagem de heartbeat do RabbitMQ enquanto o batch roda. Mantém abaixo
# do heartbeat default (600s) com folga.
_HEARTBEAT_PUMP_INTERVAL = 30


def _setup_logger() -> logging.Logger:
    Path("log").mkdir(exist_ok=True)
    data_hoje = time.strftime("%d-%m-%Y")
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        handlers=[
            logging.FileHandler(f"log/worker-{data_hoje}.log", encoding="utf-8"),
            logging.StreamHandler(stream=sys.stdout),
        ],
        force=True,
    )
    return logging.getLogger("worker")


class JobValidationError(Exception):
    """Mensagem da fila inválida (campos faltando, formato errado).

    Tratada como permanente: ack=requeue=False + report_finish failed.
    Reposting com a mesma mensagem só repete o erro.
    """


class RabbitMQWorker:
    def __init__(self):
        self.logger = _setup_logger()

        self.worker_id = os.getenv("WORKER_ID", f"worker-{socket.gethostname()}")
        self.rabbitmq_host = os.getenv("MAESTRO_RABBITMQ_HOST", "localhost")
        self.rabbitmq_port = int(os.getenv("MAESTRO_RABBITMQ_PORT", "5672"))
        self.rabbitmq_user = os.getenv("MAESTRO_RABBITMQ_USER", "guest")
        self.rabbitmq_password = os.getenv("MAESTRO_RABBITMQ_PASSWORD", "guest")
        self.queue_name = os.getenv("MAESTRO_RABBITMQ_QUEUE", "bot-planilha-tasks")
        self.max_workers = max(1, int(os.getenv("MAX_WORKERS", "3")))

        self.maestro_api_url = os.getenv("MAESTRO_API_URL", "http://localhost:8080")
        self.client = MaestroClient(
            base_url=self.maestro_api_url,
            api_key=os.getenv("MAESTRO_WORKER_API_KEY", ""),
            logger=self.logger,
        )

        self.connection: Optional[pika.BlockingConnection] = None
        self.channel = None
        self.should_stop = False
        # cancel_event do job em andamento (None quando ocioso). O handler de
        # sinal seta esse event para fazer SIGTERM/SIGINT virarem cancelamento
        # cooperativo em vez de hard-kill no meio do job.
        self._active_cancel_event: Optional[threading.Event] = None

        # DataFrame da Sheet carregado uma vez no boot. Cada job-loop relê via
        # _ensure_df pra pegar mudanças entre jobs (operador edita Sheet a
        # qualquer momento). Cache otimiza só dentro do mesmo job.
        self.df_cache = None
        self.df_loaded_at: Optional[float] = None
        self._df_ttl_s = float(os.getenv("SHEET_CACHE_TTL_S", "300"))

        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)

    def _signal_handler(self, signum, frame):
        # Segundo sinal = saída forçada (escape hatch se o gracioso travar).
        if self.should_stop:
            self.logger.warning(f"Segundo sinal {signum} recebido — forçando saída.")
            sys.exit(1)

        self.should_stop = True
        cancel_event = self._active_cancel_event
        if cancel_event is not None:
            # Há job em andamento: vira cancelamento cooperativo. O run_batch
            # cancela as pending, aguarda as in-flight, devolve status=canceled,
            # e process_message reporta canceled + ack ANTES de sair. Sem isso,
            # o job ficava preso 'running' no Maestro e a mensagem era reentregue.
            self.logger.info(
                f"Recebido sinal {signum}. Cancelando job em andamento "
                f"(graceful) — aguardando empresa(s) in-flight terminarem..."
            )
            cancel_event.set()
        else:
            # Ocioso: nada rodando. Acorda o IO loop pra parar de consumir e sair.
            self.logger.info(f"Recebido sinal {signum}. Encerrando (worker ocioso)...")
            self._request_stop_consuming()

    def _request_stop_consuming(self) -> None:
        """Pede ao loop do pika para parar de consumir, de forma thread-safe.

        Chamado tanto do handler de sinal (worker ocioso) quanto do fim do
        process_message (quando should_stop foi setado durante um job). Usa
        add_callback_threadsafe porque mexer no canal direto de outro contexto
        não é seguro com a BlockingConnection.
        """
        conn = self.connection
        if conn is None or conn.is_closed:
            return
        try:
            conn.add_callback_threadsafe(self._stop_consuming)
        except Exception as e:
            self.logger.warning(f"Falha ao agendar stop_consuming: {e}")

    def _stop_consuming(self) -> None:
        try:
            if self.channel is not None and self.channel.is_open:
                self.channel.stop_consuming()
        except Exception as e:
            self.logger.warning(f"Falha em stop_consuming: {e}")

    def _ensure_df(self):
        """Lazy-load do DataFrame da Sheet com TTL. Reusa entre jobs próximos."""
        now = time.monotonic()
        if (
            self.df_cache is not None
            and self.df_loaded_at is not None
            and (now - self.df_loaded_at) < self._df_ttl_s
        ):
            return self.df_cache
        self.df_cache = lp.get_df(self.logger)
        self.df_loaded_at = now
        return self.df_cache

    def connect(self):
        max_retries = 5
        retry_delay = 5
        for attempt in range(max_retries):
            try:
                self.logger.info(
                    f"Conectando ao RabbitMQ em {self.rabbitmq_host}:{self.rabbitmq_port}..."
                )
                credentials = pika.PlainCredentials(self.rabbitmq_user, self.rabbitmq_password)
                parameters = pika.ConnectionParameters(
                    host=self.rabbitmq_host,
                    port=self.rabbitmq_port,
                    credentials=credentials,
                    heartbeat=600,
                    blocked_connection_timeout=300,
                )
                self.connection = pika.BlockingConnection(parameters)
                self.channel = self.connection.channel()
                self.channel.queue_declare(
                    queue=self.queue_name,
                    durable=True,
                    arguments={"x-dead-letter-exchange": "maestro.dlx"},
                )
                self.channel.basic_qos(prefetch_count=1)
                self.logger.info(f"Conectado. Aguardando mensagens em '{self.queue_name}'.")
                return
            except Exception as e:
                self.logger.error(f"Falha ao conectar ({attempt + 1}/{max_retries}): {e}")
                if attempt < max_retries - 1:
                    time.sleep(retry_delay)
                else:
                    raise

    def _safe_ack(self, ch, method, requeue: Optional[bool] = None) -> None:
        """Ack ou nack tolerante a canal fechado. Espelha bot-xml-gms."""
        try:
            if requeue is None:
                ch.basic_ack(delivery_tag=method.delivery_tag)
            else:
                ch.basic_nack(delivery_tag=method.delivery_tag, requeue=requeue)
        except (
            pika.exceptions.ChannelWrongStateError,
            pika.exceptions.StreamLostError,
            pika.exceptions.ConnectionClosed,
        ) as e:
            self.logger.warning(
                f"Canal fechado antes do {'ack' if requeue is None else 'nack'} — "
                f"job já reportado; reentrega cairá no idempotency check. {e}"
            )

    def _validar_parametros(self, params: dict) -> tuple[str, str, bool, bool, list[str]]:
        """Valida o payload e devolve campos prontos pro run_batch.

        Levanta JobValidationError quando faltam campos obrigatórios ou
        formato é inválido. Caller faz report_finish failed com INVALID_PARAMETERS.
        """
        data_inicial = params.get("data_inicial")
        data_fim = params.get("data_fim")
        destinatario = bool(params.get("destinatario", False))
        remetente = bool(params.get("remetente", False))
        empresas_raw = params.get("empresas") or []

        if not data_inicial or not data_fim:
            raise JobValidationError("Campos obrigatórios faltando: data_inicial, data_fim")
        if not isinstance(data_inicial, str) or len(data_inicial) != 10 or data_inicial[2] != "/" or data_inicial[5] != "/":
            raise JobValidationError(f"data_inicial '{data_inicial}' não está no formato dd/MM/yyyy")
        if not isinstance(data_fim, str) or len(data_fim) != 10 or data_fim[2] != "/" or data_fim[5] != "/":
            raise JobValidationError(f"data_fim '{data_fim}' não está no formato dd/MM/yyyy")
        if not (destinatario or remetente):
            raise JobValidationError("Precisa de pelo menos um: destinatario ou remetente")
        if not isinstance(empresas_raw, list):
            raise JobValidationError(f"empresas precisa ser list, recebi {type(empresas_raw).__name__}")

        return data_inicial, data_fim, destinatario, remetente, [str(e) for e in empresas_raw]

    def _resolver_lista_empresas(self, df, empresas_param: list[str]):
        """Wrap empresa_resolver pra capturar sys.exit como JobValidationError.

        empresa_resolver foi escrito pro CLI (sai com sys.exit em erro). No
        worker queremos virar uma exception tratada pelo loop principal.
        """
        original_exit = sys.exit
        capture = {}

        def _capture(msg=None):
            capture["msg"] = str(msg) if msg is not None else ""
            raise JobValidationError(capture["msg"])

        sys.exit = _capture
        try:
            return resolver_empresas(
                df, empresas_param, todas_ativas=(len(empresas_param) == 0), logger=self.logger,
            )
        finally:
            sys.exit = original_exit

    def _run_batch_with_heartbeat(
        self,
        empresas,
        data_inicial,
        data_fim,
        destinatario,
        remetente,
        df,
        run_id,
        on_log,
        cancel_event,
    ):
        """Roda run_batch numa thread separada e drena process_data_events.

        Padrão do bot-xml-gms (`_run_bot_with_heartbeat`): pika
        BlockingConnection é single-threaded. Se o handler trava por horas
        processando, o heartbeat do RabbitMQ (default 600s) estoura e a
        conexão morre — pior, o job já reportou pro Maestro mas o RabbitMQ
        re-enfileira na próxima conexão.

        Roda o batch numa daemon thread; a main thread fica em
        `process_data_events(time_limit=30)` em loop. Quando a thread do
        batch termina, propaga o retorno (ou exceção).
        """
        result_holder: dict = {}
        exc_holder: dict = {}

        def _target():
            try:
                result_holder["ret"] = runner.run_batch(
                    empresas, data_inicial, data_fim,
                    destinatario, remetente,
                    df, self.logger,
                    max_workers=self.max_workers,
                    run_id=run_id,
                    on_log=on_log,
                    cancel_event=cancel_event,
                )
            except BaseException as e:
                exc_holder["exc"] = e

        thread = threading.Thread(target=_target, name=f"batch-{run_id}", daemon=True)
        thread.start()

        while thread.is_alive():
            try:
                self.connection.process_data_events(time_limit=_HEARTBEAT_PUMP_INTERVAL)
            except Exception as e:
                self.logger.warning(f"process_data_events falhou durante batch: {e}")
                thread.join()
                raise

        if "exc" in exc_holder:
            raise exc_holder["exc"]
        return result_holder["ret"]

    def _montar_result(
        self,
        *,
        job_id,
        status,
        error_class,
        partial_success,
        summary,
        started_at,
        completed_at,
    ) -> dict:
        """Monta result na shape canônica (alinhada com bot-xml-gms).

        Topo: status, started_at, completed_at, duration_seconds, summary,
        job_id. Quando aplicável: partial_success, error_class, error,
        canceled_at, stage.
        """
        result = {
            "status": status,
            "started_at": started_at.isoformat(),
            "completed_at": completed_at.isoformat(),
            "duration_seconds": round((completed_at - started_at).total_seconds(), 2),
            "summary": summary,
            "job_id": job_id,
        }
        if partial_success:
            result["partial_success"] = True
        if error_class:
            result["error_class"] = error_class

        if status == "failed" and summary.get("failed"):
            primeira = summary["failed"][0]
            result["error"] = primeira.get("message")
            result["error_type"] = primeira.get("error_type")

        if status == "canceled":
            # bot-xml-gms convention: canceled_at com timezone UTC (já vem de run_batch).
            if "canceled_at" in summary:
                result["canceled_at"] = summary["canceled_at"]
            result["stage"] = "run_batch"

        return result

    def process_message(self, ch, method, properties, body):
        job_id = None
        started_at = datetime.now(timezone.utc)
        try:
            message = json.loads(body)
            job_id = message.get("job_id")
            params = message.get("parameters") or {}

            self.logger.info(f"Mensagem recebida: job_id={job_id}")

            if not job_id:
                raise JobValidationError("Campo obrigatório 'job_id' não encontrado na mensagem")

            # Idempotency check (FASE 4): se o job já está terminal no Maestro,
            # essa mensagem é redelivery de ack que falhou. Ackeia e descarta.
            terminal = self.client.get_status(job_id)
            if terminal is not None:
                self.logger.warning(
                    f"Job {job_id} já está terminal no Maestro "
                    f"(status={terminal.get('status')}). Descartando redelivery."
                )
                self._safe_ack(ch, method)
                return

            data_inicial, data_fim, destinatario, remetente, empresas_param = self._validar_parametros(params)

            self.client.report_start(job_id)
            self.client.report_log(job_id, "INFO", f"Job {job_id} iniciado")

            df = self._ensure_df()
            if df is None or df.empty:
                raise JobValidationError("Sheet não carregada ou vazia")

            empresas = self._resolver_lista_empresas(df, empresas_param)
            self.client.report_log(
                job_id, "INFO",
                f"Resolvi {len(empresas)} empresa(s) | período {data_inicial} → {data_fim}",
            )

            run_id = diag.gerar_run_id()
            diag.evento(run_id, None, None, "batch", "start",
                        extras={"max_workers": self.max_workers,
                                "total_empresas": len(empresas),
                                "data_inicial": data_inicial,
                                "data_fim": data_fim,
                                "destinatario": destinatario,
                                "remetente": remetente,
                                "modo": "worker",
                                "job_id": job_id})

            def _on_log(level, message, actionable):
                self.client.report_log(job_id, level, message, actionable=actionable)

            def _on_cancel():
                self.client.report_log(
                    job_id, "WARNING",
                    "Cancelamento solicitado pelo usuário. Encerrando após empresa atual.",
                )

            with CancellationWatcher(
                check_fn=self.client.check_cancellation,
                job_id=job_id,
                poll_interval=_CANCELLATION_POLL_INTERVAL,
                on_cancel=_on_cancel,
            ) as watcher:
                # Expõe o cancel_event ao handler de sinal: SIGTERM/SIGINT
                # durante o job viram cancelamento cooperativo.
                self._active_cancel_event = watcher.cancel_event
                try:
                    summary, status, error_class, partial_success = self._run_batch_with_heartbeat(
                        empresas, data_inicial, data_fim,
                        destinatario, remetente, df, run_id,
                        on_log=_on_log, cancel_event=watcher.cancel_event,
                    )
                finally:
                    self._active_cancel_event = None

            completed_at = datetime.now(timezone.utc)

            diag.evento(run_id, None, None, "batch", "end",
                        extras={"status": status, "error_class": error_class,
                                "partial_success": partial_success,
                                "ok": len(summary["ok"]),
                                "no_data": len(summary["no_data"]),
                                "failed": len(summary["failed"]),
                                "skipped": len(summary.get("skipped", []))})

            result = self._montar_result(
                job_id=job_id, status=status, error_class=error_class,
                partial_success=partial_success, summary=summary,
                started_at=started_at, completed_at=completed_at,
            )

            self.client.report_finish(job_id, status, result)
            self._safe_ack(ch, method)
            self.logger.info(f"Job {job_id} finalizado: status={status}")

        except json.JSONDecodeError as e:
            self.logger.error(f"JSON inválido: {e}")
            if job_id:
                self.client.report_log(job_id, "ERROR", f"JSON inválido: {e}")
                self.client.report_finish(job_id, "failed", {
                    "error": f"JSON inválido: {e}",
                    "error_type": "JSONDecodeError",
                    "error_class": "INVALID_PARAMETERS",
                })
            self._safe_ack(ch, method, requeue=False)

        except JobValidationError as e:
            self.logger.error(f"Validação: {e}")
            if job_id:
                self.client.report_log(job_id, "ERROR", str(e), actionable=True)
                self.client.report_finish(job_id, "failed", {
                    "error": str(e),
                    "error_type": "JobValidationError",
                    "error_class": "INVALID_PARAMETERS",
                })
            self._safe_ack(ch, method, requeue=False)

        except APIError as e:
            # gspread APIError = problema no Google Sheets (rate limit, auth).
            # Transitório quando 429/5xx, permanente quando 403/404.
            self.logger.error(f"Google Sheets API error: {e}")
            if job_id:
                try:
                    self.client.report_log(job_id, "ERROR", f"Sheet inacessível: {e}", actionable=True)
                    self.client.report_finish(job_id, "failed", {
                        "error": str(e),
                        "error_type": "APIError",
                        "error_class": "INFRA_DESTINO_INDISPONIVEL",
                    })
                except Exception:
                    pass
            self._safe_ack(ch, method, requeue=False)

        except Exception as e:
            self.logger.error(f"Erro inesperado no job {job_id}: {e}", exc_info=True)
            if job_id:
                try:
                    self.client.report_log(job_id, "ERROR", f"Erro inesperado: {e}")
                    self.client.report_finish(job_id, "failed", {
                        "error": str(e),
                        "error_type": type(e).__name__,
                        "error_class": "UNKNOWN",
                    })
                except requests.exceptions.RequestException as api_error:
                    self.logger.critical(
                        f"FALHA CRÍTICA ao reportar pro Maestro: {api_error}", exc_info=True,
                    )
                except Exception as inner:
                    self.logger.critical(f"FALHA CRÍTICA inesperada: {inner}", exc_info=True)

            is_transient = isinstance(e, (ConnectionError, TimeoutError))
            if is_transient:
                self.logger.warning("Falha transitória — mensagem será reprocessada.")
            else:
                self.logger.error("Falha permanente — mensagem descartada.")
            self._safe_ack(ch, method, requeue=is_transient)

        finally:
            # Se um SIGTERM/SIGINT chegou durante o job, já reportamos o status
            # terminal e ackamos acima; agora paramos o consumo para sair limpo
            # (sem deixar a mensagem unacked → sem reprocesso na próxima subida).
            if self.should_stop:
                self._request_stop_consuming()

    def start(self):
        self.logger.info("=" * 60)
        self.logger.info(f"Bot Planilha SEFAZ Worker — id={self.worker_id}")
        self.logger.info(f"RabbitMQ: {self.rabbitmq_host}:{self.rabbitmq_port} queue={self.queue_name}")
        self.logger.info(f"Maestro API: {self.maestro_api_url}")
        self.logger.info("=" * 60)

        try:
            self.connect()
            self.channel.basic_consume(
                queue=self.queue_name,
                on_message_callback=self.process_message,
                auto_ack=False,
            )
            self.logger.info("Worker pronto. Aguardando tarefas...")
            self.channel.start_consuming()
        except KeyboardInterrupt:
            self.logger.info("Interrompido pelo usuário")
        except Exception as e:
            self.logger.critical(f"Erro fatal no worker: {e}", exc_info=True)
            raise
        finally:
            if self.connection and not self.connection.is_closed:
                try:
                    self.connection.close()
                except Exception:
                    pass
            self.logger.info("Worker encerrado.")


def main():
    try:
        worker = RabbitMQWorker()
        worker.start()
        return 0
    except Exception as e:
        logging.critical(f"Falha ao iniciar worker: {e}", exc_info=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
