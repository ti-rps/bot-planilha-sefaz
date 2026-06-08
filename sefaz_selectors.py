"""Carrega os selectors do portal SEFAZ-BA de config/selectors.yaml.

Externalizado (2026-06-08, ref. bot-xml-gms) pra que mudança de layout do portal
seja edição de YAML, não de código. Carregado UMA vez no import; exposto em
`SELECTORS` (dict aninhado login/formulario/...).

NOTA: o módulo se chama `sefaz_selectors` (não `selectors`) de propósito —
`selectors` é módulo da stdlib e não pode ser sombreado na raiz do projeto.
"""
import os

import yaml

_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config", "selectors.yaml")

with open(_PATH, encoding="utf-8") as _f:
    SELECTORS: dict = yaml.safe_load(_f)
