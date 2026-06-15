"""Regressão do filtro de download (incidente 03/06).

O Chromium do Linux cria temporários `.org.chromium.Chromium.XXXX` durante a
escrita — que NÃO terminam em `.crdownload`. A versão antiga classificava esse
temporário como "finalizado", renomeava e ele sumia no meio do os.rename
(FileNotFoundError), zerando os CSVs. `_download_em_progresso` deve reconhecer
esses temporários como ainda-em-progresso e só o CSV real como finalizado.
"""
import logging

import baixar_planilha_sefaz as bs

logger = logging.getLogger("test")


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


# --- _finalizar_download_se_pronto (hardening do falso JOB_TIMEOUT, ARPEC 12/06) ---

def _patch_io(monkeypatch):
    """Neutraliza efeitos colaterais lentos/externos do helper nos testes."""
    monkeypatch.setattr(bs.diag, "evento", lambda *a, **k: None)
    monkeypatch.setattr(bs.time, "sleep", lambda *_a, **_k: None)


def test_finalizar_sem_csv_devolve_none(tmp_path, monkeypatch):
    _patch_io(monkeypatch)
    (tmp_path / ".org.chromium.Chromium.2ZlEyj").write_bytes(b"baixando")
    out = bs._finalizar_download_se_pronto(
        str(tmp_path), "ARPEC", "destinatario", "01/05/2026", "run", logger, 0.0)
    assert out is None


def test_finalizar_loop_espera_se_ha_temporario(tmp_path, monkeypatch):
    """Modo loop (exigir_dir_limpo=True): CSV pronto MAS dotfile residual → espera."""
    _patch_io(monkeypatch)
    (tmp_path / "rpt_cons_dest.csv").write_bytes(b"a,b,c\n1,2,3\n")
    (tmp_path / ".org.chromium.Chromium.2ZlEyj").write_bytes(b"lock")
    out = bs._finalizar_download_se_pronto(
        str(tmp_path), "ARPEC", "destinatario", "01/05/2026", "run", logger, 0.0,
        exigir_dir_limpo=True)
    assert out is None  # conservador: não conclui com temporário presente


def test_finalizar_loop_conclui_dir_limpo(tmp_path, monkeypatch):
    _patch_io(monkeypatch)
    (tmp_path / "rpt_cons_dest.csv").write_bytes(b"a,b,c\n1,2,3\n")
    out = bs._finalizar_download_se_pronto(
        str(tmp_path), "ARPEC DISTRIBUIDORA", "destinatario", "01/05/2026",
        "run", logger, 0.0, exigir_dir_limpo=True)
    assert out is not None
    assert out.endswith("DESTINATARIO 052026 ARPEC DISTRIBUIDORA.csv")
    import os
    assert os.path.exists(out)


def test_salvaguarda_aceita_csv_mesmo_com_dotfile(tmp_path, monkeypatch):
    """O caso ARPEC 12/06: no timeout o CSV está pronto, mas um dotfile residual
    bloquearia a conclusão normal. exigir_dir_limpo=False salva o download."""
    _patch_io(monkeypatch)
    (tmp_path / "rpt_cons_dest.csv").write_bytes(b"a,b,c\n1,2,3\n")
    (tmp_path / ".org.chromium.Chromium.2ZlEyj").write_bytes(b"lock")
    out = bs._finalizar_download_se_pronto(
        str(tmp_path), "ARPEC", "destinatario", "01/05/2026", "run", logger, 0.0,
        exigir_dir_limpo=False)
    assert out is not None
    assert out.endswith("DESTINATARIO 052026 ARPEC.csv")
    import os
    assert os.path.exists(out)
