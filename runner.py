"""Backend compartilhado entre GUI (main.py), CLI (cli.py) e worker (FASE 5).

Sem globals, sem dependência de UI. Toda função recebe explicitamente
`df` e `logger`. Quem chama (GUI/CLI/Maestro) é responsável por carregar
a Sheet e configurar o logger.

`run_batch` é o entry point único: pool de threads + agregação canônica
do summary + classificação dos 4 status terminais do rps-maestro.

Para integração com o rps-maestro (FASE 4), `run_batch` aceita dois kwargs
opcionais (default None — CLI/GUI não passam):

- `on_log`: callback `(level, message, actionable)` chamado em pontos chave
  (~3-5 linhas/empresa). Espelha o padrão do bot-xml-gms: poucos logs no
  Maestro, DEBUG denso fica no log/ local.
- `cancel_event`: `threading.Event` setado pelo CancellationWatcher. Quando
  set durante a coleta, `run_batch` cancela futures pending e devolve
  `status="canceled"` com `summary["processed_before_cancel"]` populado.
"""
import concurrent.futures
import os
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Callable, Optional

import baixar_planilha_sefaz as bs
import ler_planilha as lp
import retry_policy
from errors import BotPlanilhaError, InvalidParametersError


OnLog = Callable[[str, str, bool], None]


_SISTEMICAS_INFRA = {"IP_BLOCKED", "PORTAL_DOWN", "INFRA_DESTINO_INDISPONIVEL", "RATE_LIMITED"}

# FASE 2: 3 IP_BLOCKED consecutivos no batch → circuit breaker abre,
# empresas restantes viram skipped. Threshold definido em alinhamento com
# o usuário em 2026-05-26.
_IP_BLOCK_CIRCUIT_THRESHOLD = 3


def _resultado_empresa(razao_social, *, status, error_class=None, error_type=None, message=None, actionable=False):
    return {
        "empresa": razao_social,
        "status": status,
        "error_class": error_class,
        "error_type": error_type,
        "message": message,
        "actionable": actionable,
    }


def processar_empresa(row_data, data_inicial, data_final, destinatario, remetente, dir_temp, driver, wait, logger, run_id=None):
    login_val = lp.get_login(logger, row_data)
    senha_val = lp.get_senha(logger, row_data)
    razao_social = lp.get_empresa(logger, row_data)

    login_str = str(login_val) if login_val is not None else ""
    senha_str = str(senha_val) if senha_val is not None else ""

    if not (login_str.strip() and senha_str.strip()):
        if not login_str.strip() and not senha_str.strip():
            motivo = "Login e Senha vazios"
        elif not login_str.strip():
            motivo = "Login vazio"
        else:
            motivo = "Senha vazia"
        raise InvalidParametersError(
            f"Credenciais ausentes na Sheet para {razao_social}: {motivo}",
            empresa=razao_social,
        )

    if not (destinatario or remetente):
        raise InvalidParametersError(
            f"Nenhum tipo selecionado (destinatário/remetente) para {razao_social}",
            empresa=razao_social,
        )

    resultados = []
    if destinatario:
        logger.info(f"Baixando como Destinatário para a empresa {razao_social}")
        resultados.append(bs.download(logger, row_data, razao_social, login_str, senha_str, data_inicial, data_final, dir_temp, tipo="destinatario", driver=driver, wait=wait, run_id=run_id))
    if remetente:
        logger.info(f"Baixando como Remetente para a empresa {razao_social}")
        resultados.append(bs.download(logger, row_data, razao_social, login_str, senha_str, data_inicial, data_final, dir_temp, tipo="remetente", driver=driver, wait=wait, run_id=run_id))

    return "ok" if "ok" in resultados else "no_data"


def _executar_empresa_uma_vez(empresa_data_series, razao_social, data_inicial, data_final,
                              destinatario, remetente, run_id, logger):
    """Uma tentativa singular: cria driver, baixa, fecha driver. Sem retry.

    Retorna o dict canônico de `_resultado_empresa`. Usado pelo envelope de
    retry abaixo — não chamar direto fora do runner.
    """
    dir_temp = str(uuid.uuid4())
    download_dir_thread = os.path.join("downloads", dir_temp)
    os.makedirs(download_dir_thread, exist_ok=True)

    driver_instance = None
    try:
        driver_instance, wait_instance = bs.configurar_driver(logger, download_dir_thread)
        empresa_status = processar_empresa(
            empresa_data_series, data_inicial, data_final,
            destinatario, remetente,
            dir_temp, driver_instance, wait_instance, logger, run_id=run_id,
        )
        return _resultado_empresa(razao_social, status=empresa_status)
    except BotPlanilhaError as e:
        logger.error(f"[{e.error_class}] {razao_social}: {e.message}")
        return _resultado_empresa(razao_social, status="failed",
                                  error_class=e.error_class,
                                  error_type=type(e).__name__,
                                  message=e.message,
                                  actionable=e.actionable)
    except Exception as e:
        logger.exception(f"Erro inesperado ao processar {razao_social} na thread")
        return _resultado_empresa(razao_social, status="failed",
                                  error_class="UNKNOWN",
                                  error_type=type(e).__name__,
                                  message=str(e),
                                  actionable=False)
    finally:
        if driver_instance:
            try:
                driver_instance.quit()
                logger.info(f"Driver para {razao_social} finalizado.")
            except Exception as e_quit:
                logger.error(f"Erro ao tentar fechar o driver para {razao_social}: {e_quit}")


def processar_empresa_thread(empresa_values, data_inicial, data_final,
                             destinatario_selecionado, remetente_selecionado,
                             df, logger, run_id=None,
                             cancel_event: Optional[threading.Event] = None):
    """Envelope com retry por error_class (FASE 2).

    Validações de input (df/empresa na Sheet) ficam fora do retry — falham
    direto com INVALID_PARAMETERS actionable. Só a execução real (driver +
    download) entra no loop.

    Política de retry vive em `retry_policy.py`. Entre tentativas usa
    `cancel_event.wait(backoff)` se o batch passou um event — assim um
    cancel pelo Maestro interrompe o backoff em vez de esperar todo ele.
    """
    razao_social = empresa_values[1]

    if df is None:
        logger.error("DataFrame de empresas não fornecido.")
        return _resultado_empresa(razao_social, status="failed",
                                  error_class="INVALID_PARAMETERS",
                                  error_type="RuntimeError",
                                  message="DataFrame de empresas não carregado",
                                  actionable=True)

    empresa_df_row_results = df[df['RAZÃO SOCIAL'] == razao_social]
    if empresa_df_row_results.empty:
        logger.error(f"Empresa {razao_social} não encontrada no DataFrame (dentro da thread).")
        return _resultado_empresa(razao_social, status="failed",
                                  error_class="INVALID_PARAMETERS",
                                  error_type="LookupError",
                                  message=f"Empresa {razao_social} não encontrada na Sheet",
                                  actionable=True)

    empresa_data_series = empresa_df_row_results.iloc[0]

    attempt = 1
    resultado = _executar_empresa_uma_vez(
        empresa_data_series, razao_social, data_inicial, data_final,
        destinatario_selecionado, remetente_selecionado, run_id, logger,
    )

    while resultado["status"] == "failed" and retry_policy.is_retryable(resultado["error_class"]):
        ec = resultado["error_class"]
        total = retry_policy.max_attempts(ec)
        if attempt >= total:
            logger.warning(
                f"Empresa {razao_social}: esgotou {total} tentativa(s) com error_class={ec}. "
                f"Devolvendo falha."
            )
            break

        wait = retry_policy.backoff_seconds(ec, attempt + 1)
        logger.info(
            f"Empresa {razao_social}: retry {attempt + 1}/{total} em {wait:.1f}s "
            f"(falha anterior: {ec})"
        )
        if cancel_event is not None:
            if cancel_event.wait(timeout=wait):
                logger.info(f"Empresa {razao_social}: cancel acionado durante backoff — abortando retry.")
                return resultado
        else:
            time.sleep(wait)

        attempt += 1
        resultado = _executar_empresa_uma_vez(
            empresa_data_series, razao_social, data_inicial, data_final,
            destinatario_selecionado, remetente_selecionado, run_id, logger,
        )

    return resultado


def classificar_run(summary, *, canceled: bool = False):
    """Decide (status, error_class, partial_success) a partir do summary.

    Mapeia pros 4 status terminais do rps-maestro:
      - canceled=True (cancel_event acionado durante o batch) → canceled
      - tudo ok                            → completed
      - tudo no_data                       → completed_no_invoices
      - todas falharam, mesma error_class  → failed (com aquela error_class)
      - misto                              → completed + partial_success=True (PARTIAL_FAILURE)
    """
    if canceled:
        return ("canceled", None, False)

    n_ok = len(summary["ok"])
    n_no_data = len(summary["no_data"])
    n_failed = len(summary["failed"])
    total = n_ok + n_no_data + n_failed

    if total == 0:
        return ("failed", "UNKNOWN", False)

    if n_failed == 0:
        if n_ok > 0:
            return ("completed", None, False)
        return ("completed_no_invoices", None, False)

    if n_failed == total:
        classes = {f["error_class"] for f in summary["failed"]}
        if len(classes) == 1:
            return ("failed", classes.pop(), False)
        if classes.issubset(_SISTEMICAS_INFRA):
            return ("failed", "PORTAL_DOWN", False)
        return ("failed", "UNKNOWN", False)

    return ("completed", "PARTIAL_FAILURE", True)


def formatar_mensagem_summary(summary, status, error_class, partial_success, relatorio_path):
    n_ok = len(summary["ok"])
    n_no_data = len(summary["no_data"])
    n_failed = len(summary["failed"])
    total = n_ok + n_no_data + n_failed

    linhas = [f"Status: {status}" + (" (parcial)" if partial_success else "")]
    if error_class:
        linhas.append(f"Categoria: {error_class}")
    linhas.append(f"Total: {total} empresa(s)")
    linhas.append(f"  OK: {n_ok}")
    linhas.append(f"  Sem dados: {n_no_data}")
    linhas.append(f"  Falhas: {n_failed}")
    if summary.get("skipped"):
        linhas.append(f"  Puladas: {len(summary['skipped'])}")
    if summary.get("ip_block_circuit_open"):
        linhas.append("  ⚠ Circuit breaker IP_BLOCKED ativo — IP bloqueado pelo SEFAZ-BA, validar antes de rerodar.")

    if summary["failed"]:
        linhas.append("")
        linhas.append("Falhas:")
        for f in summary["failed"][:10]:
            linhas.append(f"  - {f['empresa']} [{f['error_class']}]: {f['message']}")
        if len(summary["failed"]) > 10:
            linhas.append(f"  ... e mais {len(summary['failed']) - 10}.")

    if relatorio_path:
        linhas.append("")
        linhas.append(f"Relatório: {relatorio_path}")
    return "\n".join(linhas)


def _safe_on_log(on_log: Optional[OnLog], level: str, message: str, actionable: bool, logger):
    if on_log is None:
        return
    try:
        on_log(level, message, actionable)
    except Exception as cb_err:
        logger.warning(f"on_log levantou exceção (ignorando): {cb_err}")


def run_batch(
    empresas_list,
    data_inicial,
    data_final,
    destinatario,
    remetente,
    df,
    logger,
    *,
    max_workers=3,
    run_id=None,
    progress_callback=None,
    on_log: Optional[OnLog] = None,
    cancel_event: Optional[threading.Event] = None,
):
    """Roda o batch de empresas em pool de threads e devolve a tupla canônica.

    Args:
        empresas_list: list de tuples (Código, RAZÃO SOCIAL, CNPJ, Status, Contribuinte) —
            mesmo shape que a GUI passa (treeview.item(id, 'values')).
        data_inicial, data_final: strings dd/MM/yyyy.
        destinatario, remetente: bools.
        df: pandas DataFrame da Sheet.
        logger: logging.Logger.
        max_workers: tamanho do pool. Default 3.
        run_id: opcional; se None, gera um novo via diagnostico.gerar_run_id.
        progress_callback: opcional fn(processed: int, total: int). Chamada a
            cada empresa concluída pra UIs atualizarem progress bar. NÃO usar
            pra mutar Tkinter direto — encapsular com root.after se for GUI.
        on_log: opcional callback (level, message, actionable). Quando passado,
            run_batch emite ~3 linhas por empresa (start/end + erro com
            actionable propagado do BotPlanilhaError). Espelha o padrão do
            bot-xml-gms. CLI/GUI passam None.
        cancel_event: opcional threading.Event. Quando set durante a coleta,
            run_batch cancela futures pending (empresas não iniciadas), aguarda
            as in-flight terminarem e devolve status="canceled". As empresas
            concluídas até o cancel viram summary["processed_before_cancel"].

    Returns:
        (summary, status, error_class, partial_success):
        - summary: {"ok": [str], "failed": [{empresa, error_class, error_type, message}],
                    "no_data": [str], "skipped": [str]} — shape canônica do rps-maestro.
                    Quando cancelado, ganha "processed_before_cancel": [str] e
                    "canceled_at": isoformat UTC.
        - status, error_class, partial_success: ver classificar_run.
    """
    import diagnostico as diag

    if run_id is None:
        run_id = diag.gerar_run_id()

    summary = {"ok": [], "failed": [], "no_data": [], "skipped": []}
    total = len(empresas_list)
    processed = 0
    canceled = False
    ip_block_consecutivos = 0
    ip_block_circuit_open = False

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_razao = {
            executor.submit(
                processar_empresa_thread, ev, data_inicial, data_final,
                destinatario, remetente, df, logger, run_id,
                cancel_event,
            ): ev[1]
            for ev in empresas_list
        }

        _safe_on_log(on_log, "INFO", f"Processando {total} empresa(s) | workers={max_workers}", False, logger)

        for future in concurrent.futures.as_completed(future_to_razao):
            razao_social_key = future_to_razao[future]
            resultado_status = None
            resultado_ec = None
            try:
                resultado = future.result()
                resultado_status = resultado["status"]
                resultado_ec = resultado["error_class"]
                if resultado_status == "ok":
                    summary["ok"].append(resultado["empresa"])
                    logger.info(f"Empresa {razao_social_key} processada com sucesso.")
                    _safe_on_log(on_log, "INFO", f"OK: {razao_social_key}", False, logger)
                elif resultado_status == "no_data":
                    summary["no_data"].append(resultado["empresa"])
                    logger.info(f"Empresa {razao_social_key} concluída sem dados.")
                    _safe_on_log(on_log, "INFO", f"Sem dados: {razao_social_key}", False, logger)
                else:
                    summary["failed"].append({
                        "empresa": resultado["empresa"],
                        "error_class": resultado_ec,
                        "error_type": resultado["error_type"],
                        "message": resultado["message"],
                    })
                    logger.error(f"[{resultado_ec}] {razao_social_key}: {resultado['message']}")
                    _safe_on_log(
                        on_log, "ERROR",
                        f"[{resultado_ec}] {razao_social_key}: {resultado['message']}",
                        bool(resultado.get("actionable", False)), logger,
                    )
            except concurrent.futures.CancelledError:
                # Future foi cancelada pelo cancel_event OU pelo circuit breaker.
                # Não conta como falha — vai pra skipped pra rastreio.
                summary["skipped"].append(razao_social_key)
                logger.info(f"Empresa {razao_social_key} pulada (cancel ou circuit).")
                continue
            except Exception as exc_f:
                logger.error(f"Exceção ao obter resultado da future para {razao_social_key}: {exc_f}", exc_info=True)
                summary["failed"].append({
                    "empresa": razao_social_key,
                    "error_class": "UNKNOWN",
                    "error_type": type(exc_f).__name__,
                    "message": str(exc_f),
                })
                _safe_on_log(on_log, "ERROR", f"[UNKNOWN] {razao_social_key}: {exc_f}", False, logger)
                resultado_ec = "UNKNOWN"
                resultado_status = "failed"

            processed += 1
            if progress_callback:
                try:
                    progress_callback(processed, total)
                except Exception as cb_err:
                    logger.warning(f"progress_callback levantou exceção (ignorando): {cb_err}")

            # Circuit breaker IP_BLOCKED (FASE 2): N falhas IP_BLOCKED consecutivas
            # abrem o circuito e pulam o resto do batch. "Consecutivas" aqui é em
            # ordem de as_completed (proxy razoável pra "rajada"). Reset em qualquer
            # outro outcome (ok / no_data / failed com outra classe).
            if resultado_status == "failed" and resultado_ec == "IP_BLOCKED":
                ip_block_consecutivos += 1
            else:
                ip_block_consecutivos = 0

            if not ip_block_circuit_open and ip_block_consecutivos >= _IP_BLOCK_CIRCUIT_THRESHOLD:
                ip_block_circuit_open = True
                pending = [f for f in future_to_razao if not f.done()]
                n_cancelled = sum(1 for f in pending if f.cancel())
                msg = (
                    f"Circuit breaker IP_BLOCKED aberto após {ip_block_consecutivos} "
                    f"falhas consecutivas. {n_cancelled} empresa(s) restantes puladas."
                )
                logger.warning(msg)
                _safe_on_log(on_log, "WARNING", msg, True, logger)

            if cancel_event is not None and cancel_event.is_set() and not canceled:
                canceled = True
                pending = [f for f in future_to_razao if not f.done()]
                cancelled_count = sum(1 for f in pending if f.cancel())
                logger.warning(
                    f"Cancelamento solicitado: {cancelled_count}/{len(pending)} futures pending canceladas; "
                    f"aguardando in-flight terminar."
                )
                _safe_on_log(
                    on_log, "WARNING",
                    "Cancelamento solicitado. Aguardando empresa(s) em andamento.",
                    False, logger,
                )

    if canceled:
        summary["processed_before_cancel"] = (
            list(summary["ok"]) + list(summary["no_data"]) + [f["empresa"] for f in summary["failed"]]
        )
        summary["canceled_at"] = datetime.now(timezone.utc).isoformat()

    if ip_block_circuit_open:
        summary["ip_block_circuit_open"] = True

    status, error_class, partial_success = classificar_run(summary, canceled=canceled)
    return summary, status, error_class, partial_success
