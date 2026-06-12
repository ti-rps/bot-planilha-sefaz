"""Testes da validação de período compartilhada (guarda contra virada de ano)."""
from datetime import date

import pytest

from validacao_datas import parse_data, validar_periodo


# `hoje` fixo pra tornar a checagem de futuro determinística.
HOJE = date(2026, 6, 12)


def test_periodo_valido_devolve_datas():
    di, dfim = validar_periodo("01/05/2026", "31/05/2026", hoje=HOJE)
    assert di == date(2026, 5, 1)
    assert dfim == date(2026, 5, 31)


def test_periodo_de_um_dia_ok():
    di, dfim = validar_periodo("10/06/2026", "10/06/2026", hoje=HOJE)
    assert di == dfim == date(2026, 6, 10)


@pytest.mark.parametrize("valor", [
    "01/13/2025",   # mês 13 não existe
    "31/02/2026",   # 31 de fevereiro não existe
    "1/5/26",       # formato curto
    "2026-05-01",   # formato ISO, não dd/MM/yyyy
    "",             # vazio
])
def test_data_inexistente_ou_mal_formada(valor):
    with pytest.raises(ValueError):
        parse_data(valor, "data_inicial")


def test_parse_rejeita_nao_string():
    with pytest.raises(ValueError):
        parse_data(20260501, "data_inicial")


def test_periodo_invertido():
    with pytest.raises(ValueError, match="não pode ser depois"):
        validar_periodo("31/05/2026", "01/05/2026", hoje=HOJE)


def test_data_inicial_no_futuro_erro_de_virada_de_ano():
    # Caso clássico de janeiro: trocou o mês, esqueceu o ano (dez/2026 em vez
    # de dez/2025). Início no futuro → rejeitado com dica de virada de ano.
    with pytest.raises(ValueError, match="virada de ano"):
        validar_periodo("01/12/2026", "31/12/2026", hoje=HOJE)


def test_data_fim_no_futuro_ok_se_inicial_nao_for():
    # Período que começa hoje/passado e termina no futuro é permitido — só o
    # INÍCIO no futuro é sinal forte de erro de data.
    di, dfim = validar_periodo("01/06/2026", "30/06/2026", hoje=HOJE)
    assert di == date(2026, 6, 1)
