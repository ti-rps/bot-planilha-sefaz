"""Backend compartilhado entre GUI (main.py) e CLI (cli.py).

Sem globals, sem dependência de UI. Toda função recebe explicitamente
`df` e `logger`. Quem chama (GUI ou CLI) é responsável por carregar a
Sheet e configurar o logger.

`run_batch` é o entry point único: pool de threads + agregação canônica
do summary + classificação dos 4 status terminais do rps-maestro.
"""
import os
import uuid
import concurrent.futures

import baixar_planilha_sefaz as bs
import ler_planilha as lp
from errors import BotPlanilhaError, InvalidParametersError


_SISTEMICAS_INFRA = {"IP_BLOCKED", "PORTAL_DOWN", "INFRA_DESTINO_INDISPONIVEL", "RATE_LIMITED"}


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


def processar_empresa_thread(empresa_values, data_inicial, data_final, destinatario_selecionado, remetente_selecionado, df, logger, run_id=None):
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
    dir_temp = str(uuid.uuid4())
    download_dir_thread = os.path.join("downloads", dir_temp)
    os.makedirs(download_dir_thread, exist_ok=True)

    driver_instance = None
    try:
        driver_instance, wait_instance = bs.configurar_driver(logger, download_dir_thread)
        empresa_status = processar_empresa(
            empresa_data_series, data_inicial, data_final,
            destinatario_selecionado, remetente_selecionado,
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


def classificar_run(summary):
    """Decide (status, error_class, partial_success) a partir do summary.

    Mapeia pros 4 status terminais do rps-maestro:
      - tudo ok                            → completed
      - tudo no_data                       → completed_no_invoices
      - todas falharam, mesma error_class  → failed (com aquela error_class)
      - misto                              → completed + partial_success=True (PARTIAL_FAILURE)
    """
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


def run_batch(empresas_list, data_inicial, data_final, destinatario, remetente, df, logger, *, max_workers=3, run_id=None, progress_callback=None):
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

    Returns:
        (summary, status, error_class, partial_success):
        - summary: {"ok": [str], "failed": [{empresa, error_class, error_type, message}],
                    "no_data": [str], "skipped": [str]} — shape canônica do rps-maestro.
        - status, error_class, partial_success: ver classificar_run.
    """
    # Import lazy pra evitar circularidade no diag (ele importa runner em alguns paths? a checar)
    import diagnostico as diag

    if run_id is None:
        run_id = diag.gerar_run_id()

    summary = {"ok": [], "failed": [], "no_data": [], "skipped": []}
    total = len(empresas_list)
    processed = 0

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_razao = {
            executor.submit(processar_empresa_thread, ev, data_inicial, data_final, destinatario, remetente, df, logger, run_id): ev[1]
            for ev in empresas_list
        }

        for future in concurrent.futures.as_completed(future_to_razao):
            razao_social_key = future_to_razao[future]
            try:
                resultado = future.result()
                if resultado["status"] == "ok":
                    summary["ok"].append(resultado["empresa"])
                    logger.info(f"Empresa {razao_social_key} processada com sucesso.")
                elif resultado["status"] == "no_data":
                    summary["no_data"].append(resultado["empresa"])
                    logger.info(f"Empresa {razao_social_key} concluída sem dados.")
                else:
                    summary["failed"].append({
                        "empresa": resultado["empresa"],
                        "error_class": resultado["error_class"],
                        "error_type": resultado["error_type"],
                        "message": resultado["message"],
                    })
                    logger.error(f"[{resultado['error_class']}] {razao_social_key}: {resultado['message']}")
            except Exception as exc_f:
                logger.error(f"Exceção ao obter resultado da future para {razao_social_key}: {exc_f}", exc_info=True)
                summary["failed"].append({
                    "empresa": razao_social_key,
                    "error_class": "UNKNOWN",
                    "error_type": type(exc_f).__name__,
                    "message": str(exc_f),
                })

            processed += 1
            if progress_callback:
                try:
                    progress_callback(processed, total)
                except Exception as cb_err:
                    logger.warning(f"progress_callback levantou exceção (ignorando): {cb_err}")

    status, error_class, partial_success = classificar_run(summary)
    return summary, status, error_class, partial_success
