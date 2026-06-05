"""Desacoplamento (empresa, tipo) — destinatário+remetente (2026-06-05).

Quando os dois tipos são marcados, cada (empresa, tipo) é uma unidade
independente: a passe de retry reprocessa só o tipo que falhou, sem refazer o
que já deu certo. Monkeypatcha processar_empresa_thread (sem Selenium).
"""
import logging

import runner


logger = logging.getLogger("test-dual-tipo")


def _ev(nome):
    return (nome, nome, "00000000000000", "ATIVO", "NAO")


def _rodar(monkeypatch, roteiro, empresas):
    chamadas = {}

    def fake_thread(ev, di, dfim, tipo, df, lg, run_id=None, cancel_event=None):
        chave = (ev[1], tipo)
        chamadas[chave] = chamadas.get(chave, 0) + 1
        return roteiro(ev[1], tipo, chamadas[chave])

    monkeypatch.setattr(runner, "processar_empresa_thread", fake_thread)
    out = runner.run_batch(
        empresas, "01/05/2026", "31/05/2026",
        True, True,                      # destinatario E remetente
        df=None, logger=logger, max_workers=3, run_id="run-dual",
    )
    return out, chamadas


def test_retry_so_no_tipo_que_falhou(monkeypatch):
    # EMP_X: destinatário sempre ok; remetente falha transitória 1× e recupera.
    def roteiro(razao, tipo, n):
        if tipo == "destinatario":
            return runner._resultado_empresa(razao, status="ok", tipo=tipo)
        if n == 1:
            return runner._resultado_empresa(razao, status="failed",
                                             error_class="JOB_TIMEOUT",
                                             error_type="JobTimeoutError",
                                             message="timeout", tipo=tipo)
        return runner._resultado_empresa(razao, status="ok", tipo=tipo)

    (summary, status, ec, partial), chamadas = _rodar(monkeypatch, roteiro, [_ev("EMP_X")])

    # destinatário NÃO foi refeito; remetente sim (inicial + 1 retry).
    assert chamadas[("EMP_X", "destinatario")] == 1
    assert chamadas[("EMP_X", "remetente")] == 2
    # Ambos terminaram ok → empresa ok, sem falha.
    assert summary["ok"] == ["EMP_X"]
    assert summary["failed"] == []
    assert status == "completed"


def test_falha_de_credencial_num_tipo_marca_empresa_failed(monkeypatch):
    # EMP_Y: destinatário ok; remetente credencial inválida (não retentável).
    def roteiro(razao, tipo, n):
        if tipo == "destinatario":
            return runner._resultado_empresa(razao, status="ok", tipo=tipo)
        return runner._resultado_empresa(razao, status="failed",
                                         error_class="CREDENTIAL_INVALID",
                                         error_type="CredentialInvalidError",
                                         message="senha", actionable=True,
                                         login="****9", timestamp="t", tipo=tipo)

    (summary, status, ec, partial), chamadas = _rodar(monkeypatch, roteiro, [_ev("EMP_Y")])

    # Credencial não é retentada; destinatário rodou 1× (ok, CSV salvo no share).
    assert chamadas[("EMP_Y", "destinatario")] == 1
    assert chamadas[("EMP_Y", "remetente")] == 1
    # Falha vence no rollup por empresa, carregando o tipo e a classe.
    assert summary["ok"] == []
    assert len(summary["failed"]) == 1
    f = summary["failed"][0]
    assert f["empresa"] == "EMP_Y"
    assert f["error_class"] == "CREDENTIAL_INVALID"
    assert f["tipo"] == "remetente"
