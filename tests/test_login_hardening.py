"""Hardening do login/CAPTCHA (2026-06-05), a partir de log de produção real.

Cobre os dois helpers novos de baixar_planilha_sefaz:
- _clicar_com_retry: tolera StaleElementReferenceException no clique do "Entrar".
- _captcha_alert_presente: detecta/aceita o alert "Código Captcha incorreto".
"""
import logging

import pytest
from selenium.common.exceptions import (
    StaleElementReferenceException,
    NoAlertPresentException,
)

import baixar_planilha_sefaz as bs


logger = logging.getLogger("test-login-hardening")


@pytest.fixture(autouse=True)
def _sem_sleep(monkeypatch):
    monkeypatch.setattr(bs.time, "sleep", lambda *_a, **_k: None)


class _FakeWait:
    """wait.until(cond) ignora a condição e devolve sempre o mesmo elemento."""
    def __init__(self, element):
        self._element = element

    def until(self, _cond):
        return self._element


class _FlakyButton:
    def __init__(self, falhas):
        self.falhas = falhas
        self.clicks = 0

    def click(self):
        self.clicks += 1
        if self.clicks <= self.falhas:
            raise StaleElementReferenceException("stale not found")


def test_clicar_com_retry_recupera_de_stale():
    btn = _FlakyButton(falhas=1)            # 1º clique stale, 2º ok
    bs._clicar_com_retry(None, _FakeWait(btn), ("by", "loc"), logger, tentativas=3)
    assert btn.clicks == 2


def test_clicar_com_retry_esgota_e_levanta():
    btn = _FlakyButton(falhas=9)            # sempre stale
    with pytest.raises(StaleElementReferenceException):
        bs._clicar_com_retry(None, _FakeWait(btn), ("by", "loc"), logger, tentativas=3)
    assert btn.clicks == 3                  # tentou exatamente o teto


class _FakeAlert:
    def __init__(self, text):
        self.text = text
        self.accepted = False

    def accept(self):
        self.accepted = True


class _SwitchTo:
    def __init__(self, alert):
        self._alert = alert

    @property
    def alert(self):
        if self._alert is None:
            raise NoAlertPresentException("no alert")
        return self._alert


class _FakeDriver:
    def __init__(self, alert=None):
        self.switch_to = _SwitchTo(alert)


def test_captcha_alert_presente_aceita_e_devolve_texto():
    alerta = _FakeAlert("Código Captcha incorreto. Por favor, tente novamente.")
    driver = _FakeDriver(alert=alerta)
    texto = bs._captcha_alert_presente(driver)
    assert "Captcha incorreto" in texto
    assert alerta.accepted is True          # o alert foi dispensado


def test_captcha_alert_ausente_devolve_none():
    assert bs._captcha_alert_presente(_FakeDriver(alert=None)) is None
