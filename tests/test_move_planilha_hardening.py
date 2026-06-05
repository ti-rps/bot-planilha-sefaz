"""Hardening do move (2026-06-05): falso-sucesso vira falha de verdade.

Em share de rede o move pode "voltar ok" sem o arquivo materializar (ou com
0 bytes). move_planilha agora confere existência + tamanho no destino e levanta
ShareUnavailableError nesses casos, em vez de logar sucesso.
"""
import logging

import pytest

import baixar_planilha_sefaz as bs
from errors import ShareUnavailableError


logger = logging.getLogger("test-move")


def test_move_ok_quando_arquivo_tem_conteudo(tmp_path, monkeypatch):
    monkeypatch.setenv("DESTINO_BASE", str(tmp_path))
    origem = tmp_path / "origem" / "DESTINATÁRIO 052026 EMP X.csv"
    origem.parent.mkdir(parents=True)
    origem.write_text("col1,col2\n1,2\n", encoding="utf-8")

    bs.move_planilha(logger, "EMP X", str(origem), "01/05/2026")

    destino = tmp_path / "EMP X" / "2026" / "052026" / "DESTINATÁRIO 052026 EMP X.csv"
    assert destino.exists() and destino.stat().st_size > 0
    assert not origem.exists()  # foi movido


def test_move_detecta_arquivo_vazio(tmp_path, monkeypatch):
    monkeypatch.setenv("DESTINO_BASE", str(tmp_path))
    origem = tmp_path / "origem" / "DESTINATÁRIO 052026 EMP Y.csv"
    origem.parent.mkdir(parents=True)
    origem.write_bytes(b"")  # 0 bytes — escrita no share "não persistiu"

    with pytest.raises(ShareUnavailableError):
        bs.move_planilha(logger, "EMP Y", str(origem), "01/05/2026")
