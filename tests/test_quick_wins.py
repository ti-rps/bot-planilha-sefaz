"""Fixes da revisão geral (2026-06-08)."""
import logging
import os

import baixar_planilha_sefaz as bs
import ler_planilha as lp


logger = logging.getLogger("test-quick-wins")


def test_get_iscontribuinte_devolve_contribuinte_nao_senha():
    row = {"Contribuinte": "S", "Senha Robô": "segredo123"}
    assert lp.get_isContribuinte(logger, row) == "S"   # antes devolvia a senha


def test_verificar_arquivo_em_uso_ausente_e_false(tmp_path):
    # Arquivo que não existe NÃO está "em uso".
    assert bs.verificar_arquivo_em_uso(str(tmp_path / "nao_existe.csv")) is False


def test_verificar_arquivo_em_uso_existente_livre_e_false(tmp_path):
    arq = tmp_path / "ok.csv"
    arq.write_text("a,b\n1,2\n", encoding="utf-8")
    assert bs.verificar_arquivo_em_uso(str(arq)) is False


def test_sanitizar_nome_vazia_dispara_guard_no_download(tmp_path, monkeypatch):
    # Razão só com símbolos sanitiza pra "" — download deve recusar (guard novo)
    # antes de criar driver/path. sanitizar_nome é a precondição do guard.
    assert bs.sanitizar_nome("@#$%&*") == ""
    from errors import InvalidParametersError
    import pytest
    with pytest.raises(InvalidParametersError):
        bs.download(logger, {}, "@#$%&*", "u", "s", "01/05/2026", "31/05/2026",
                    "dir-x", tipo="destinatario")
