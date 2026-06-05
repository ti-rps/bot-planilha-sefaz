"""Smoke test: o grafo de módulos do worker carrega headless?

Valida a premissa da FASE 6 — worker/cli/runner importam sem Tkinter nem
tkcalendar (que ficaram fora de requirements-worker.txt). Se algum módulo do
caminho do worker importasse GUI no topo, este teste quebraria no container.
"""
import importlib

import pytest

# Módulos que o container precisa carregar. main.py (Tkinter) NÃO entra.
WORKER_PATH_MODULES = [
    "errors",
    "retry_policy",
    "diagnostico",
    "ler_planilha",
    "empresa_resolver",
    "captcha_solver",
    "baixar_planilha_sefaz",
    "runner",
    "maestro_client",
    "cancellation_watcher",
    "email_report",
    "cli",
    "worker",
]


@pytest.mark.parametrize("modname", WORKER_PATH_MODULES)
def test_modulo_importa(modname):
    assert importlib.import_module(modname) is not None


def test_worker_path_nao_puxa_tkinter():
    """Nenhum módulo do caminho do worker pode importar tkinter/tkcalendar.

    Importa só os módulos do worker num subprocesso limpo e falha se tkinter
    ou tkcalendar aparecerem em sys.modules — seriam deps de GUI vazando pro
    container (que não as instala).
    """
    import subprocess
    import sys

    codigo = (
        "import sys; "
        "import worker, cli, runner; "
        "leak = [m for m in ('tkinter', 'tkcalendar') if m in sys.modules]; "
        "sys.exit('VAZOU: ' + ','.join(leak) if leak else 0)"
    )
    r = subprocess.run([sys.executable, "-c", codigo], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
