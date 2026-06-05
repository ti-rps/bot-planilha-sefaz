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
from errors import BotPlanilhaError, InvalidParametersError, OperationCanceled


OnLog = Callable[[str, str, bool], None]

# Passe de retry no fim do lote (2026-06-05): além da 1ª tentativa, reexecuta
# as falhas transitórias mais N vezes. Total de tentativas = 1 + RETRY_PASSES.
_RETRY_PASSES = max(0, int(os.getenv("RETRY_PASSES", "2")))


_SISTEMICAS_INFRA = {"IP_BLOCKED", "PORTAL_DOWN", "INFRA_DESTINO_INDISPONIVEL", "RATE_LIMITED"}

# FASE 2: 3 IP_BLOCKED consecutivos no batch → circuit breaker abre,
# empresas restantes viram skipped. Threshold definido em alinhamento com
# o usuário em 2026-05-26.
_IP_BLOCK_CIRCUIT_THRESHOLD = 3


def _resultado_empresa(razao_social, *, status, error_class=None, error_type=None,
                       message=None, actionable=False, login=None, timestamp=None,
                       tipo=None):
    return {
        "empresa": razao_social,
        "status": status,
        "error_class": error_class,
        "error_type": error_type,
        "message": message,
        "actionable": actionable,
        # Detalhes extras p/ o relatório de credenciais por e-mail (item 5).
        "login": login,
        "timestamp": timestamp,
        # Tipo da consulta (destinatario|remetente). Desde 2026-06-05 a unidade
        # de trabalho é (empresa, tipo): cada tipo roda com driver/login próprios
        # (desacopla a falha e elimina o "já logado" da sessão compartilhada).
        "tipo": tipo,
    }


def processar_empresa(row_data, data_inicial, data_final, tipo, dir_temp, driver, wait, logger, run_id=None, cancel_event=None):
    """Uma consulta (UM tipo) de uma empresa. Devolve "ok" | "no_data".

    Desde 2026-06-05 processa UM tipo por chamada (destinatario OU remetente) —
    a unidade de trabalho do batch é (empresa, tipo). Levanta
    InvalidParametersError se a Sheet não tem login/senha.
    """
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

    rotulo = "Destinatário" if tipo == "destinatario" else "Remetente"
    logger.info(f"Baixando como {rotulo} para a empresa {razao_social}")
    return bs.download(logger, row_data, razao_social, login_str, senha_str,
                       data_inicial, data_final, dir_temp, tipo=tipo,
                       driver=driver, wait=wait, run_id=run_id, cancel_event=cancel_event)


def _executar_empresa_uma_vez(empresa_data_series, razao_social, data_inicial, data_final,
                              tipo, run_id, logger, cancel_event=None):
    """Uma tentativa singular de (empresa, tipo): cria driver, baixa, fecha. Sem retry.

    Cada chamada usa um driver/login próprios — é o que desacopla os tipos e
    elimina o problema de "já logado" da sessão compartilhada. Retorna o dict
    canônico de `_resultado_empresa` (com `tipo`). As passes de retry vivem em
    `run_batch`.
    """
    login_mascarado = lp._mascarar(empresa_data_series.get("Login")) if hasattr(empresa_data_series, "get") else None
    agora = datetime.now(timezone.utc).isoformat()

    dir_temp = str(uuid.uuid4())
    download_dir_thread = os.path.join("downloads", dir_temp)
    os.makedirs(download_dir_thread, exist_ok=True)

    driver_instance = None
    try:
        driver_instance, wait_instance = bs.configurar_driver(logger, download_dir_thread)
        empresa_status = processar_empresa(
            empresa_data_series, data_inicial, data_final, tipo,
            dir_temp, driver_instance, wait_instance, logger, run_id=run_id,
            cancel_event=cancel_event,
        )
        return _resultado_empresa(razao_social, status=empresa_status, tipo=tipo)
    except OperationCanceled:
        # Cancelamento cooperativo no meio da empresa: não é falha.
        logger.info(f"{razao_social} ({tipo}): cancelado em andamento.")
        return _resultado_empresa(razao_social, status="canceled", tipo=tipo)
    except BotPlanilhaError as e:
        logger.error(f"[{e.error_class}] {razao_social} ({tipo}): {e.message}")
        return _resultado_empresa(razao_social, status="failed",
                                  error_class=e.error_class,
                                  error_type=type(e).__name__,
                                  message=e.message,
                                  actionable=e.actionable,
                                  login=login_mascarado,
                                  timestamp=agora, tipo=tipo)
    except Exception as e:
        logger.exception(f"Erro inesperado ao processar {razao_social} ({tipo}) na thread")
        return _resultado_empresa(razao_social, status="failed",
                                  error_class="UNKNOWN",
                                  error_type=type(e).__name__,
                                  message=str(e),
                                  actionable=False,
                                  login=login_mascarado,
                                  timestamp=agora, tipo=tipo)
    finally:
        if driver_instance:
            try:
                driver_instance.quit()
                logger.info(f"Driver para {razao_social} ({tipo}) finalizado.")
            except Exception as e_quit:
                logger.error(f"Erro ao tentar fechar o driver para {razao_social} ({tipo}): {e_quit}")


def processar_empresa_thread(empresa_values, data_inicial, data_final, tipo,
                             df, logger, run_id=None,
                             cancel_event: Optional[threading.Event] = None):
    """Uma tentativa de (empresa, tipo): resolve a linha na Sheet + executa.

    O retry NÃO vive aqui — é uma PASSE DIFERIDA no fim do lote, orquestrada por
    `run_batch`, e agora por UNIDADE (empresa, tipo). Aqui ficou só a validação
    de input (INVALID_PARAMETERS actionable) + 1 execução de um tipo.
    """
    razao_social = empresa_values[1]

    if df is None:
        logger.error("DataFrame de empresas não fornecido.")
        return _resultado_empresa(razao_social, status="failed",
                                  error_class="INVALID_PARAMETERS",
                                  error_type="RuntimeError",
                                  message="DataFrame de empresas não carregado",
                                  actionable=True, tipo=tipo)

    empresa_df_row_results = df[df['RAZÃO SOCIAL'] == razao_social]
    if empresa_df_row_results.empty:
        logger.error(f"Empresa {razao_social} não encontrada no DataFrame (dentro da thread).")
        return _resultado_empresa(razao_social, status="failed",
                                  error_class="INVALID_PARAMETERS",
                                  error_type="LookupError",
                                  message=f"Empresa {razao_social} não encontrada na Sheet",
                                  actionable=True, tipo=tipo)

    empresa_data_series = empresa_df_row_results.iloc[0]

    return _executar_empresa_uma_vez(
        empresa_data_series, razao_social, data_inicial, data_final,
        tipo, run_id, logger, cancel_event=cancel_event,
    )


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


def _executar_pool(work_items, data_inicial, data_final,
                   df, logger, *, max_workers, run_id, on_log, cancel_event,
                   progress_callback):
    """Roda UM pool de threads sobre `work_items` (lista de tuples (ev, tipo)).

    Devolve (resultados, skipped, canceled, ip_circuit_aberto):
      - resultados: dict (razao, tipo) -> dict canônico (ok/no_data/failed/canceled);
      - skipped: list de chaves (razao, tipo) cujas futures foram canceladas;
      - canceled: bool — cancel_event acionado durante este pool;
      - ip_circuit_aberto: bool — circuit breaker IP_BLOCKED abriu neste pool.

    A orquestração de passes (run_batch) é quem agrega isso entre tentativas.
    """
    resultados = {}
    skipped = []
    canceled = False
    ip_block_consecutivos = 0
    ip_circuit_aberto = False
    total = len(work_items)
    processed = 0

    def _rotulo(chave):
        return f"{chave[0]} ({chave[1]})"

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_key = {
            executor.submit(
                processar_empresa_thread, ev, data_inicial, data_final,
                tipo, df, logger, run_id, cancel_event,
            ): (ev[1], tipo)
            for ev, tipo in work_items
        }

        for future in concurrent.futures.as_completed(future_to_key):
            chave = future_to_key[future]
            razao_social_key = chave[0]
            resultado_status = None
            resultado_ec = None
            try:
                resultado = future.result()
                resultado_status = resultado["status"]
                resultado_ec = resultado["error_class"]
                resultados[chave] = resultado
                if resultado_status == "ok":
                    logger.info(f"Empresa {_rotulo(chave)} processada com sucesso.")
                    _safe_on_log(on_log, "INFO", f"OK: {_rotulo(chave)}", False, logger)
                elif resultado_status == "no_data":
                    logger.info(f"Empresa {_rotulo(chave)} concluída sem dados.")
                    _safe_on_log(on_log, "INFO", f"Sem dados: {_rotulo(chave)}", False, logger)
                elif resultado_status == "canceled":
                    logger.info(f"Empresa {_rotulo(chave)} cancelada em andamento.")
                else:
                    logger.error(f"[{resultado_ec}] {_rotulo(chave)}: {resultado['message']}")
                    _safe_on_log(
                        on_log, "ERROR",
                        f"[{resultado_ec}] {_rotulo(chave)}: {resultado['message']}",
                        bool(resultado.get("actionable", False)), logger,
                    )
            except concurrent.futures.CancelledError:
                # Future cancelada pelo cancel_event OU pelo circuit breaker.
                skipped.append(chave)
                logger.info(f"Empresa {_rotulo(chave)} pulada (cancel ou circuit).")
                continue
            except Exception as exc_f:
                logger.error(f"Exceção ao obter resultado da future para {_rotulo(chave)}: {exc_f}", exc_info=True)
                resultados[chave] = _resultado_empresa(
                    razao_social_key, status="failed", error_class="UNKNOWN",
                    error_type=type(exc_f).__name__, message=str(exc_f), tipo=chave[1],
                )
                _safe_on_log(on_log, "ERROR", f"[UNKNOWN] {_rotulo(chave)}: {exc_f}", False, logger)
                resultado_ec = "UNKNOWN"
                resultado_status = "failed"

            processed += 1
            if progress_callback:
                try:
                    progress_callback(processed, total)
                except Exception as cb_err:
                    logger.warning(f"progress_callback levantou exceção (ignorando): {cb_err}")

            # Circuit breaker IP_BLOCKED: N falhas IP_BLOCKED consecutivas (em
            # ordem de as_completed) pulam o resto do pool. Reset em qualquer
            # outro outcome.
            if resultado_status == "failed" and resultado_ec == "IP_BLOCKED":
                ip_block_consecutivos += 1
            else:
                ip_block_consecutivos = 0

            if not ip_circuit_aberto and ip_block_consecutivos >= _IP_BLOCK_CIRCUIT_THRESHOLD:
                ip_circuit_aberto = True
                pending = [f for f in future_to_key if not f.done()]
                n_cancelled = sum(1 for f in pending if f.cancel())
                msg = (
                    f"Circuit breaker IP_BLOCKED aberto após {ip_block_consecutivos} "
                    f"falhas consecutivas. {n_cancelled} empresa(s) restantes puladas."
                )
                logger.warning(msg)
                _safe_on_log(on_log, "WARNING", msg, True, logger)

            if cancel_event is not None and cancel_event.is_set() and not canceled:
                canceled = True
                pending = [f for f in future_to_key if not f.done()]
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

    return resultados, skipped, canceled, ip_circuit_aberto


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

    total = len(empresas_list)
    _safe_on_log(on_log, "INFO", f"Processando {total} empresa(s) | workers={max_workers}", False, logger)

    # Unidade de trabalho = (empresa, tipo). Cada tipo selecionado vira um item
    # independente: roda com driver/login próprios (desacopla falha + elimina o
    # "já logado") e é retentado isoladamente na passe. Destinatário-só => 1
    # unidade por empresa (comportamento idêntico ao anterior).
    tipos = [t for t, ligado in (("destinatario", destinatario), ("remetente", remetente)) if ligado]
    work_items = [(ev, t) for ev in empresas_list for t in tipos]
    by_key = {(ev[1], t): (ev, t) for ev, t in work_items}

    # final: (razao, tipo) -> resultado da ÚLTIMA passe que tocou a unidade.
    final: dict = {}
    skipped_all: list = []   # chaves (razao, tipo) puladas (cancel/circuit)
    canceled = False
    ip_block_circuit_open = False

    total_passes = 1 + _RETRY_PASSES
    lista_atual = list(work_items)

    for passe in range(total_passes):
        if not lista_atual:
            break
        if passe > 0:
            msg = (f"Passe de retry {passe}/{_RETRY_PASSES}: reprocessando "
                   f"{len(lista_atual)} unidade(s) (empresa+tipo) com falha transitória.")
            logger.info(msg)
            _safe_on_log(on_log, "INFO", msg, False, logger)

        resultados, skipped, cancld, ipc = _executar_pool(
            lista_atual, data_inicial, data_final,
            df, logger, max_workers=max_workers, run_id=run_id, on_log=on_log,
            cancel_event=cancel_event,
            # Só mexe na progress bar na 1ª passe (senão a barra "anda pra trás").
            progress_callback=(progress_callback if passe == 0 else None),
        )

        for chave, r in resultados.items():
            final[chave] = r
        # Só conta como skipped quem NUNCA completou (1ª passe). Numa passe de
        # retry, uma future cancelada mantém o "failed" anterior — não viramos
        # failed em skipped (evita contagem dupla).
        skipped_all.extend([k for k in skipped if k not in final])
        if cancld:
            canceled = True
        if ipc:
            ip_block_circuit_open = True

        if canceled or ip_block_circuit_open:
            break

        if passe < total_passes - 1:
            lista_atual = [
                by_key[k] for k, r in final.items()
                if r["status"] == "failed"
                and retry_policy.retentavel_no_lote(r["error_class"])
                and k in by_key
            ]
        else:
            lista_atual = []

    # Agrega as unidades (empresa, tipo) -> 1 status por empresa pro summary
    # canônico do rps-maestro. Precedência: falha vence (precisa de atenção; o
    # tipo que deu certo já tem o CSV no share), depois ok, no_data, e por fim
    # canceled (in-flight) -> skipped.
    por_empresa: dict = {}
    for (razao, _tipo), r in final.items():
        por_empresa.setdefault(razao, []).append(r)

    summary = {"ok": [], "failed": [], "no_data": [], "skipped": []}
    for razao, rs in por_empresa.items():
        falhas = [r for r in rs if r["status"] == "failed"]
        if falhas:
            f = next((x for x in falhas if x.get("actionable")), falhas[0])
            summary["failed"].append({
                "empresa": f["empresa"],
                "error_class": f["error_class"],
                "error_type": f["error_type"],
                "message": f["message"],
                "actionable": f.get("actionable", False),
                "login": f.get("login"),
                "timestamp": f.get("timestamp"),
                "tipo": f.get("tipo"),
            })
        elif any(r["status"] == "ok" for r in rs):
            summary["ok"].append(razao)
        elif any(r["status"] == "no_data" for r in rs):
            summary["no_data"].append(razao)
        else:
            summary["skipped"].append(razao)

    # Empresas cujas unidades foram TODAS puladas (nenhuma completou) -> skipped.
    empresas_completas = {razao for (razao, _t) in final}
    for (razao, _t) in skipped_all:
        if razao not in empresas_completas and razao not in summary["skipped"]:
            summary["skipped"].append(razao)

    if canceled:
        summary["processed_before_cancel"] = (
            list(summary["ok"]) + list(summary["no_data"]) + [f["empresa"] for f in summary["failed"]]
        )
        summary["canceled_at"] = datetime.now(timezone.utc).isoformat()

    if ip_block_circuit_open:
        summary["ip_block_circuit_open"] = True

    status, error_class, partial_success = classificar_run(summary, canceled=canceled)
    return summary, status, error_class, partial_success
