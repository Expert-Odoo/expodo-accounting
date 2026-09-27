# -*- coding: utf-8 -*-
# Copyright 2026 Expodo (https://expodo.fr)
# License LGPL-3
"""Développement des champs raccourcis de ``account.report.line``.

Odoo permet de déclarer une expression ``balance`` directement sur la ligne
via un champ raccourci (``aggregation_formula``, ``domain_formula``,
``account_codes_formula``, ``external_formula``, ``tax_tags_formula``) plutôt
que par un bloc ``expression_ids`` complet. Ces champs ne sont pas stockés :
ils alimentent un inverse qui crée l'expression.

La CA3 française utilise massivement cette forme courte. Toute lecture d'un
XML de localisation qui l'ignore produit un graphe incomplet — c'est
exactement ce qui fait échouer une réimplémentation naïve.
"""

import re
from typing import Dict, Optional, Tuple

DOMAIN_SHORTCUT_RE = re.compile(r"(-?sum)\((.*)\)", re.S)

#: Nom du champ raccourci -> moteur d'expression correspondant.
SHORTCUT_ENGINES = {
    "domain_formula": "domain",
    "account_codes_formula": "account_codes",
    "aggregation_formula": "aggregation",
    "external_formula": "external",
    "tax_tags_formula": "tax_tags",
}

#: Libellé de l'expression créée par un raccourci.
SHORTCUT_LABEL = "balance"


def expand_shortcut(field_name: str, value: str) -> Tuple[str, str, Optional[str]]:
    """Traduit un champ raccourci en ``(moteur, formule, sous-formule)``.

    ``external_formula`` est un cas particulier : sa valeur n'est pas une
    formule mais un type d'affichage (``monetary``, ``percentage``, …) qui
    détermine à la fois la formule et la sous-formule générées.
    """
    engine = SHORTCUT_ENGINES[field_name]
    value = (value or "").strip()

    if engine == "domain":
        match = DOMAIN_SHORTCUT_RE.match(value)
        if not match:
            raise ValueError("domain_formula invalide : %r (attendu sum(...))" % value)
        subformula, formula = match.groups()
        return engine, formula.lstrip(" \t\n"), subformula

    if engine == "external":
        if value == "percentage":
            return engine, "most_recent", "editable;rounding=0"
        if value == "monetary":
            return engine, "sum", "editable"
        return engine, "most_recent", "editable"

    return engine, value.lstrip(" \t\n"), None


def find_shortcut(fields: Dict[str, str]) -> Optional[Tuple[str, str, Optional[str]]]:
    """Cherche un raccourci parmi les champs directs d'une ligne."""
    for field_name in SHORTCUT_ENGINES:
        value = fields.get(field_name)
        if value:
            return expand_shortcut(field_name, value)
    return None
