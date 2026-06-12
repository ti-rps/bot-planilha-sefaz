"""Validação de período (data_inicial / data_fim) compartilhada.

Ponto único de verdade pros três pontos de entrada — worker (fila), cli
(headless) e main (GUI) — pra não repetir regra e não divergir.

Motivação (bug de virada de ano): a validação antiga só checava FORMATO
(comprimento 10, barras nas posições certas / regex dd/MM/yyyy). Isso aceitava
data semanticamente inválida — `01/13/2025` — e período invertido ou no futuro,
que é exatamente o erro típico de janeiro: trocar só o mês e esquecer o ano
(ex.: consultar dez/2026 em vez de dez/2025). O portal devolvia "consulta
vazia", o bot arquivava como SEM DADOS e a falha passava silenciosa.

Aqui fazemos parse de verdade (`strptime`) e três checagens semânticas:
- ambas as datas existem no calendário;
- data_inicial <= data_fim;
- data_inicial não está no futuro (heurística pro erro de virada de ano).

Levanta `ValueError` com mensagem PT-BR clara. Cada caller traduz pro seu
mecanismo de erro (JobValidationError, sys.exit, messagebox).
"""
from datetime import date, datetime

FORMATO = "%d/%m/%Y"


def parse_data(valor, campo: str) -> date:
    """Faz parse de uma data dd/MM/yyyy. Levanta ValueError com msg do campo."""
    if not isinstance(valor, str):
        raise ValueError(
            f"{campo} precisa ser texto no formato dd/MM/yyyy, "
            f"recebi {type(valor).__name__}"
        )
    try:
        return datetime.strptime(valor, FORMATO).date()
    except ValueError:
        # strptime pega tanto formato errado ('1/5/26') quanto data que não
        # existe no calendário ('01/13/2025', '31/02/2026').
        raise ValueError(f"{campo} '{valor}' não é uma data válida dd/MM/yyyy")


def validar_periodo(data_inicial, data_fim, *, hoje: date | None = None):
    """Valida o par (data_inicial, data_fim) e devolve as duas datas parseadas.

    `hoje` é injetável pra testes; default é a data corrente.

    Levanta ValueError em: data inexistente, período invertido, ou início no
    futuro (provável erro de virada de ano).
    """
    di = parse_data(data_inicial, "data_inicial")
    dfim = parse_data(data_fim, "data_fim")

    if di > dfim:
        raise ValueError(
            f"data_inicial ({data_inicial}) não pode ser depois de "
            f"data_fim ({data_fim})"
        )

    hoje = hoje or date.today()
    if di > hoje:
        raise ValueError(
            f"data_inicial ({data_inicial}) está no futuro "
            f"(hoje é {hoje.strftime(FORMATO)}) — provável erro de virada de ano"
        )

    return di, dfim
