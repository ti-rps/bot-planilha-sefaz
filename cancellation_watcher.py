"""Watcher daemon que polla cancelamento e mantém heartbeat no rps-maestro.

Padrão canônico do bot-xml-gms (`src/utils/cancellation_watcher.py`). Tem
duas funções acopladas:

1. Detectar cancelamento solicitado via UI e sinalizar o batch (via
   `threading.Event`).
2. Servir de heartbeat — cada GET `/worker/jobs/:id/cancellation` atualiza
   `last_heartbeat_at` server-side, impedindo que retry_worker do Maestro
   considere o job órfão (janela default 5min).

Uso recomendado como context manager:

    with CancellationWatcher(client.check_cancellation, job_id) as watcher:
        runner.run_batch(..., cancel_event=watcher.cancel_event)

Cadência de 15s casa com janela de 5min do Maestro (~20 polls de margem).
"""
from __future__ import annotations

import logging
import threading
from typing import Callable, Optional


logger = logging.getLogger(__name__)


class CancellationWatcher:
    def __init__(
        self,
        check_fn: Callable[[str], bool],
        job_id: str,
        *,
        poll_interval: float = 15.0,
        on_cancel: Optional[Callable[[], None]] = None,
    ):
        self.check_fn = check_fn
        self.job_id = job_id
        self.poll_interval = poll_interval
        self.on_cancel = on_cancel
        self.cancel_event = threading.Event()
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def _run(self) -> None:
        logger.info(
            f"CancellationWatcher iniciado (job {self.job_id}, intervalo {self.poll_interval}s)"
        )
        while not self._stop_event.is_set():
            try:
                if self.check_fn(self.job_id):
                    logger.warning(f"Cancelamento solicitado para job {self.job_id}")
                    if self.on_cancel is not None:
                        try:
                            self.on_cancel()
                        except Exception as cb_err:
                            logger.warning(
                                f"CancellationWatcher: callback on_cancel falhou: {cb_err}"
                            )
                    self.cancel_event.set()
                    return
            except Exception as poll_err:
                # Blip de rede ou restart do Maestro não deve abortar o job.
                # Tenta de novo no próximo ciclo.
                logger.warning(
                    f"CancellationWatcher: falha transiente no poll: {poll_err}"
                )

            self._stop_event.wait(timeout=self.poll_interval)

        logger.info(f"CancellationWatcher encerrado (job {self.job_id})")

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._thread = threading.Thread(
            target=self._run,
            name=f"cancel-watcher-{self.job_id}",
            daemon=True,
        )
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop_event.set()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=timeout)

    def __enter__(self) -> "CancellationWatcher":
        self.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> bool:
        self.stop()
        return False
