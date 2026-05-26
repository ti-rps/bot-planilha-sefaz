"""CLI headless do bot-planilha-sefaz.

Mesmo backend da GUI (runner.run_batch), sem Tkinter. Permite rodar e
testar em ambientes sem display (WSL sem WSLg, container Docker, CI).

Uso:
    python cli.py --data-inicial 01/05/2026 --data-fim 02/05/2026 \\
        --destinatario [empresas...]

    # 1 empresa pelo código
    python cli.py --destinatario --data-inicial 01/05/2026 --data-fim 02/05/2026 12345

    # várias por substring da razão social (case-insensitive)
    python cli.py --destinatario --data-inicial 01/05/2026 --data-fim 02/05/2026 "RPS Centro" "RPS Norte"

    # todas com Senha Robô preenchida na Sheet
    python cli.py --destinatario --data-inicial 01/05/2026 --data-fim 02/05/2026 --all

    # output JSON (pipeable: ... --json | jq .status)
    python cli.py --destinatario --data-inicial 01/05/2026 --data-fim 02/05/2026 12345 --json

    # log no stderr também
    python cli.py --destinatario --data-inicial 01/05/2026 --data-fim 02/05/2026 12345 -v

Exit code:
    0 — status terminal completed ou completed_no_invoices
    1 — status terminal failed (incluindo PARTIAL_FAILURE em alguns casos)
    2 — erro de uso (parâmetros inválidos, empresa não encontrada, etc.)
"""
import argparse
import json
import logging
import os
import re
import sys
import time

import diagnostico as diag
import ler_planilha as lp
import runner
from empresa_resolver import resolver_empresas


_DATE_PATTERN = re.compile(r"^\d{2}/\d{2}/\d{4}$")


def configurar_logger(verbose: bool) -> logging.Logger:
    data_hoje = time.strftime("%d-%m-%Y")
    if not os.path.exists("log"):
        os.makedirs("log")

    handlers = [logging.FileHandler(f"log/{data_hoje}.log", encoding="utf-8")]
    if verbose:
        stderr_handler = logging.StreamHandler(stream=sys.stderr)
        handlers.append(stderr_handler)

    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        handlers=handlers,
        force=True,
    )
    return logging.getLogger("cli")


def parse_args():
    p = argparse.ArgumentParser(
        description="bot-planilha-sefaz headless (sem Tkinter)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("empresas", nargs="*", help="Códigos ou substring de razão social (case-insensitive)")
    p.add_argument("--all", action="store_true", help="Todas as empresas com Senha Robô preenchida")
    p.add_argument("--data-inicial", required=True, metavar="DD/MM/YYYY")
    p.add_argument("--data-fim", required=True, metavar="DD/MM/YYYY")
    p.add_argument("--destinatario", action="store_true")
    p.add_argument("--remetente", action="store_true")
    p.add_argument("--max-workers", type=int, default=int(os.getenv("MAX_WORKERS", "3")))
    p.add_argument("--json", action="store_true", help="Output JSON em vez de texto")
    p.add_argument("-v", "--verbose", action="store_true", help="Log no stderr também (default: só em log/)")
    return p.parse_args()


def main():
    args = parse_args()

    if not _DATE_PATTERN.match(args.data_inicial):
        sys.exit(f"ERRO: --data-inicial '{args.data_inicial}' não é dd/MM/yyyy")
    if not _DATE_PATTERN.match(args.data_fim):
        sys.exit(f"ERRO: --data-fim '{args.data_fim}' não é dd/MM/yyyy")
    if not (args.destinatario or args.remetente):
        sys.exit("ERRO: precisa de --destinatario e/ou --remetente")
    if not args.empresas and not args.all:
        sys.exit("ERRO: precisa de empresas posicionais ou --all")
    if args.empresas and args.all:
        sys.exit("ERRO: use empresas posicionais OU --all, não ambos")

    logger = configurar_logger(args.verbose)

    try:
        df = lp.get_df(logger)
    except Exception as e:
        sys.exit(f"ERRO ao carregar Sheet: {type(e).__name__}: {e}")
    if df is None or df.empty:
        sys.exit("ERRO: Sheet carregada vazia")

    empresas = resolver_empresas(df, args.empresas, args.all, logger)
    total = len(empresas)
    run_id = diag.gerar_run_id()

    logger.info(f"[diag] CLI run_id={run_id} | MAX_WORKERS={args.max_workers} | empresas={total}")
    diag.evento(run_id, None, None, "batch", "start",
                extras={"max_workers": args.max_workers,
                        "total_empresas": total,
                        "data_inicial": args.data_inicial,
                        "data_final": args.data_fim,
                        "destinatario": args.destinatario,
                        "remetente": args.remetente,
                        "modo": "cli"})

    if not args.json:
        print(f"Resolvendo {total} empresa(s) | run_id={run_id} | workers={args.max_workers}", file=sys.stderr)

    def progress(processed, total):
        if not args.json:
            print(f"[{processed}/{total}] processado", file=sys.stderr)

    summary, status, error_class, partial_success = runner.run_batch(
        empresas, args.data_inicial, args.data_fim,
        args.destinatario, args.remetente,
        df, logger,
        max_workers=args.max_workers,
        run_id=run_id,
        progress_callback=progress,
    )

    try:
        relatorio_path = diag.gerar_relatorio(run_id, args.max_workers, total)
    except Exception as e_rel:
        logger.error(f"Falha ao gerar relatório: {e_rel}")
        relatorio_path = None

    diag.evento(run_id, None, None, "batch", "end",
                extras={"status": status,
                        "error_class": error_class,
                        "partial_success": partial_success,
                        "ok": len(summary["ok"]),
                        "no_data": len(summary["no_data"]),
                        "failed": len(summary["failed"])})

    if args.json:
        payload = {
            "run_id": run_id,
            "status": status,
            "error_class": error_class,
            "partial_success": partial_success,
            "summary": summary,
            "relatorio_path": relatorio_path,
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(runner.formatar_mensagem_summary(summary, status, error_class, partial_success, relatorio_path))

    sys.exit(0 if status in ("completed", "completed_no_invoices") else 1)


if __name__ == "__main__":
    main()
