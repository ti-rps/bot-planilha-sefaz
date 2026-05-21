# diagnostico.py
"""
Instrumentação estruturada (JSON lines) para diagnosticar falhas em lote.
Cada evento vira uma linha em log/diagnostico-<dd-mm-yyyy>.jsonl.
Evidências (screenshot + HTML) em log/diagnostico-<run_id>/.
Não altera comportamento do bot — só observa.
"""
import os
import json
import time
import uuid
import threading
from datetime import datetime
from contextlib import contextmanager
from collections import defaultdict

_LOG_DIR = "log"
_lock = threading.Lock()


def gerar_run_id() -> str:
    return uuid.uuid4().hex[:8]


def _caminho_jsonl() -> str:
    data_hoje = time.strftime("%d-%m-%Y")
    return os.path.join(_LOG_DIR, f"diagnostico-{data_hoje}.jsonl")


def evento(run_id, empresa, tipo, fase, status,
           *, tentativa=None, duracao_ms=None, erro=None, extras=None):
    if run_id is None:
        return
    os.makedirs(_LOG_DIR, exist_ok=True)
    registro = {
        "ts": datetime.now().isoformat(timespec="milliseconds"),
        "run_id": run_id,
        "empresa": empresa,
        "tipo": tipo,
        "fase": fase,
        "status": status,
    }
    if tentativa is not None:
        registro["tentativa"] = tentativa
    if duracao_ms is not None:
        registro["duracao_ms"] = duracao_ms
    if erro is not None:
        registro["erro"] = erro
    if extras:
        registro["extras"] = extras
    linha = json.dumps(registro, ensure_ascii=False)
    with _lock:
        with open(_caminho_jsonl(), "a", encoding="utf-8") as f:
            f.write(linha + "\n")


@contextmanager
def fase(run_id, empresa, tipo, nome, *, tentativa=None, extras=None):
    if run_id is None:
        yield
        return
    inicio = time.monotonic()
    try:
        yield
    except Exception as e:
        dur = int((time.monotonic() - inicio) * 1000)
        evento(run_id, empresa, tipo, nome, "fail",
               tentativa=tentativa, duracao_ms=dur,
               erro=f"{type(e).__name__}: {e}", extras=extras)
        raise
    else:
        dur = int((time.monotonic() - inicio) * 1000)
        evento(run_id, empresa, tipo, nome, "ok",
               tentativa=tentativa, duracao_ms=dur, extras=extras)


def diretorio_evidencias(run_id) -> str:
    pasta = os.path.join(_LOG_DIR, f"diagnostico-{run_id}")
    os.makedirs(pasta, exist_ok=True)
    return pasta


def _slug(texto: str) -> str:
    s = "".join(c if c.isalnum() or c in "-_" else "_" for c in str(texto))
    return s[:80] or "x"


def salvar_evidencia(driver, run_id, empresa, tipo, fase_nome, sufixo=""):
    if run_id is None or driver is None:
        return {}
    pasta = diretorio_evidencias(run_id)
    nome = f"{_slug(empresa)}-{_slug(tipo)}-{_slug(fase_nome)}"
    if sufixo:
        nome += f"-{_slug(sufixo)}"
    paths = {}
    try:
        p = os.path.join(pasta, f"{nome}.png")
        driver.save_screenshot(p)
        paths["screenshot"] = p
    except Exception:
        pass
    try:
        p = os.path.join(pasta, f"{nome}.html")
        with open(p, "w", encoding="utf-8") as f:
            f.write(driver.page_source)
        paths["html"] = p
    except Exception:
        pass
    return paths


def gerar_relatorio(run_id, max_workers, total_empresas) -> str:
    caminho_jsonl = _caminho_jsonl()
    if not os.path.exists(caminho_jsonl):
        return ""

    eventos_run = []
    with open(caminho_jsonl, "r", encoding="utf-8") as f:
        for linha in f:
            try:
                ev = json.loads(linha)
                if ev.get("run_id") == run_id:
                    eventos_run.append(ev)
            except Exception:
                continue

    jobs = defaultdict(lambda: {"fases": [], "status_final": "incompleto"})
    fases_falhadas = defaultdict(int)

    for ev in eventos_run:
        chave = (ev.get("empresa"), ev.get("tipo"))
        jobs[chave]["fases"].append(
            (ev.get("fase"), ev.get("status"), ev.get("duracao_ms"), ev.get("erro"))
        )
        if ev.get("fase") == "job" and ev.get("status") in ("ok", "fail"):
            jobs[chave]["status_final"] = ev["status"]

    sucessos = sum(1 for j in jobs.values() if j["status_final"] == "ok")
    falhas = sum(1 for j in jobs.values() if j["status_final"] == "fail")
    incompletos = sum(1 for j in jobs.values() if j["status_final"] == "incompleto")

    for chave, j in jobs.items():
        if j["status_final"] == "fail":
            fases_fail = [f for f, s, _, _ in j["fases"] if s == "fail" and f != "job"]
            fases_falhadas[fases_fail[-1] if fases_fail else "desconhecido"] += 1

    ts_inicio = eventos_run[0]["ts"] if eventos_run else "N/A"
    ts_fim = eventos_run[-1]["ts"] if eventos_run else "N/A"

    linhas = [
        f"Relatório do run {run_id}",
        f"Iniciado: {ts_inicio}",
        f"Encerrado: {ts_fim}",
        f"MAX_WORKERS: {max_workers}",
        f"Empresas selecionadas: {total_empresas}",
        f"Jobs executados (empresa x tipo): {len(jobs)}",
        f"  Sucessos:    {sucessos}",
        f"  Falhas:      {falhas}",
        f"  Incompletos: {incompletos}",
        "",
        "Falhas por fase:",
    ]
    if fases_falhadas:
        for fase_nome, qtd in sorted(fases_falhadas.items(), key=lambda x: -x[1]):
            linhas.append(f"  {fase_nome}: {qtd}")
    else:
        linhas.append("  (nenhuma)")

    linhas += ["", "Detalhe por job:"]
    for (empresa, tipo), j in sorted(jobs.items(), key=lambda x: (str(x[0][0]), str(x[0][1]))):
        if j["status_final"] == "ok":
            linhas.append(f"  [OK]   {empresa} ({tipo})")
        else:
            ult_fail = next(
                ((f, e) for f, s, _, e in reversed(j["fases"]) if s == "fail" and f != "job"),
                (None, None),
            )
            fase_nome, erro = ult_fail
            linhas.append(
                f"  [{j['status_final'].upper()}] {empresa} ({tipo}) "
                f"- fase '{fase_nome or '?'}': {erro or '?'}"
            )

    relatorio_path = os.path.join(_LOG_DIR, f"relatorio-{run_id}.txt")
    with open(relatorio_path, "w", encoding="utf-8") as f:
        f.write("\n".join(linhas) + "\n")
    return relatorio_path
