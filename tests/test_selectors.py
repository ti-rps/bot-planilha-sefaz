"""selectors.yaml externalizado (2026-06-08).

Garante que o YAML carrega e tem as chaves que o baixar_planilha_sefaz consome
— se alguém renomear/remover uma chave no YAML, o teste pega antes de produção.
"""
import sefaz_selectors


def test_selectors_carrega_dict():
    assert isinstance(sefaz_selectors.SELECTORS, dict)


def test_selectors_tem_as_chaves_consumidas():
    sel = sefaz_selectors.SELECTORS
    assert set(sel["login"]) >= {"usuario", "senha", "botao_entrar", "msg_erro"}
    assert set(sel["formulario"]) >= {
        "tabela", "filtro_periodo", "data_inicial", "data_final",
        "btn_aplicar", "consulta_vazia", "btn_gerar_planilha",
    }
    # valores não-vazios
    assert all(v for v in sel["login"].values())
    assert all(v for v in sel["formulario"].values())
