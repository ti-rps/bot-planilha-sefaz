"""Regressão do filtro de download (incidente 03/06).

O Chromium do Linux cria temporários `.org.chromium.Chromium.XXXX` durante a
escrita — que NÃO terminam em `.crdownload`. A versão antiga classificava esse
temporário como "finalizado", renomeava e ele sumia no meio do os.rename
(FileNotFoundError), zerando os CSVs. `_download_em_progresso` deve reconhecer
esses temporários como ainda-em-progresso e só o CSV real como finalizado.
"""
import baixar_planilha_sefaz as bs


def test_temporarios_navegador_sao_em_progresso():
    em_progresso = [
        ".org.chromium.Chromium.2j4Fcj",   # Chromium Linux (o do incidente)
        ".com.google.Chrome.AbCdEf",       # Chrome Linux
        "planilha.csv.crdownload",          # Chrome/Windows
        "download.tmp",
        "download.part",
        ".algum-dotfile-parcial",
    ]
    for nome in em_progresso:
        assert bs._download_em_progresso(nome) is True, nome


def test_csv_final_nao_e_em_progresso():
    assert bs._download_em_progresso("DESTINATÁRIO 052026 A SILVA LEMOS LTDA.csv") is False
    assert bs._download_em_progresso("relatorio.csv") is False


def test_selecao_de_finalizados_ignora_temporario_do_chromium():
    """Replica a lógica de seleção do loop de espera de baixar_planilha."""
    arquivos = [".org.chromium.Chromium.2j4Fcj"]  # ainda baixando
    em_progresso = [a for a in arquivos if bs._download_em_progresso(a)]
    finalizados = [a for a in arquivos
                   if not bs._download_em_progresso(a) and a.lower().endswith(".csv")]
    # Não pode considerar concluído: há temporário e nenhum CSV.
    assert em_progresso == [".org.chromium.Chromium.2j4Fcj"]
    assert finalizados == []
    assert not (finalizados and not em_progresso)

    # Depois que o Chromium renomeia para o CSV final:
    arquivos = ["DESTINATÁRIO 052026 A SILVA LEMOS LTDA.csv"]
    em_progresso = [a for a in arquivos if bs._download_em_progresso(a)]
    finalizados = [a for a in arquivos
                   if not bs._download_em_progresso(a) and a.lower().endswith(".csv")]
    assert em_progresso == []
    assert finalizados == ["DESTINATÁRIO 052026 A SILVA LEMOS LTDA.csv"]
    assert finalizados and not em_progresso
