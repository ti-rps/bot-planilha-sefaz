"""Resolução de identifiers (códigos / razão social) pra tuples canônicos.

Extraído de `cli.py` (FASE 3) pra ser compartilhado com o worker RabbitMQ
(FASE 5). A Sheet é a fonte de verdade da lista de empresas e suas
credenciais (ver [[project-open-question-empresa-selection]]).

O contrato com o rps-maestro (PR A em [[project-maestro-decisions]]):
`parameters.empresas: list[text]` opcional. Vazio = todas ativas na Sheet.
"""
from __future__ import annotations

import sys


def resolver_empresas(df, identifiers, todas_ativas: bool, logger=None):
    """Resolve identifiers (códigos ou substring de razão social) → list de tuples.

    Cada tuple tem o shape (Código, RAZÃO SOCIAL, CNPJ, Status, Contribuinte) —
    mesmo formato que a GUI passa via treeview.item(id, 'values').

    Args:
        df: DataFrame da Sheet (de ler_planilha.get_df).
        identifiers: list[str] com códigos ou substrings de razão social.
            Ignorado quando todas_ativas=True.
        todas_ativas: True = ignora identifiers e devolve todas com 'Senha Robô'
            preenchida (= empresas operacionalmente prontas).
        logger: opcional, só usado pra warning de erros não-fatais.

    Comportamento de erro:
        - Levanta SystemExit (via sys.exit) quando identifier não bate em
          ninguém, bate em múltiplos (ambíguo), ou todas_ativas=True devolve
          lista vazia. Comportamento herdado do cli.py (FASE 3).

    No worker (FASE 5), o caller deve capturar SystemExit e converter em
    failed/INVALID_PARAMETERS antes de propagar — sys.exit em consumer
    derruba o processo inteiro.
    """
    if todas_ativas:
        rows = []
        for _, row in df.iterrows():
            senha_robo = str(row.get("Senha Robô", "")).strip()
            if senha_robo:
                rows.append(row)
        if not rows:
            sys.exit("ERRO: nenhuma empresa com 'Senha Robô' preenchida na Sheet")
    else:
        rows = []
        for ident in identifiers:
            ident_low = ident.lower().strip()
            match = df[df["Código"].astype(str).str.lower() == ident_low]
            if match.empty:
                match = df[df["RAZÃO SOCIAL"].astype(str).str.lower().str.contains(ident_low, na=False)]
            if match.empty:
                sys.exit(f"ERRO: nenhuma empresa bateu com '{ident}'")
            if len(match) > 1:
                nomes = match["RAZÃO SOCIAL"].astype(str).tolist()[:5]
                sys.exit(f"ERRO: '{ident}' é ambíguo ({len(match)} matches): " + ", ".join(nomes))
            rows.append(match.iloc[0])

    out = []
    for r in rows:
        codigo = str(r.get("Código", ""))
        razao = str(r.get("RAZÃO SOCIAL", ""))
        cnpj = str(r.get("CNPJ", ""))
        senha_robo = str(r.get("Senha Robô", "")).strip()
        status_text = "Disponível" if senha_robo else "Indisponível"
        contrib = "Sim" if r.get("Contribuinte") == "S" else "Não"
        out.append((codigo, razao, cnpj, status_text, contrib))
    return out
