# -*- coding: utf-8 -*-
# Copyright 2026 Expodo (https://expodo.fr)
# License LGPL-3
"""Sélection des comptes pour le moteur ``account_codes``.

Logique de calcul pure, sans dépendance Odoo (CDC T-02). Le modèle Odoo se
contente de rassembler les données — soldes agrégés par compte, codes et tags
— et délègue ici la décision d'inclure ou non chaque compte.

Sémantique des suffixes ``D`` et ``C``, telle que spécifiée par la
documentation publique d'Odoo : un compte n'est retenu que si son préfixe
correspond **et** si le solde total de ses écritures sur la période est du
sens demandé. C'est un filtre portant sur le compte, et non une sélection de
la colonne débit ou crédit.

    Le compte 210001 a un solde de -42, le compte 210002 un solde de 25.
    La formule ``21D`` ne retient que 210002 et renvoie 25.
"""

from typing import Callable, Dict, Iterable, Mapping, Optional, Set, Tuple

from .formula import AccountCodeTerm

#: Description d'un compte : ``(code, identifiants de tags)``.
AccountInfo = Tuple[str, Set[int]]


def account_matches_term(
    term: AccountCodeTerm,
    code: str,
    tag_ids: Set[int],
    balance: float,
    tag_resolver: Optional[Callable[[str], Set[int]]] = None,
) -> bool:
    """Le compte est-il retenu par ce terme de formule ?"""
    if term.is_tag:
        if tag_resolver is None:
            raise ValueError(
                "Un résolveur de tags est requis pour la formule %r" % term.tag_ref
            )
        if not (tag_resolver(term.tag_ref) & tag_ids):
            return False
    elif not code.startswith(term.prefix):
        return False

    if any(code.startswith(excluded) for excluded in term.excluded_prefixes):
        return False

    if term.balance_character == "D" and balance <= 0:
        return False
    if term.balance_character == "C" and balance >= 0:
        return False

    return True


def sum_account_codes(
    terms: Iterable[AccountCodeTerm],
    balance_by_account: Mapping[int, float],
    account_info: Mapping[int, AccountInfo],
    tag_resolver: Optional[Callable[[str], Set[int]]] = None,
) -> float:
    """Évalue une formule ``account_codes`` sur des soldes déjà agrégés.

    :param balance_by_account: ``{id de compte: solde de la période}``.
    :param account_info: ``{id de compte: (code, identifiants de tags)}``.
    :param tag_resolver: traduit une référence ``tag(...)`` en identifiants.
    """
    total = 0.0
    # Les résolutions de tags sont mémorisées : une formule peut répéter la
    # même référence sur plusieurs termes.
    resolved: Dict[str, Set[int]] = {}

    def resolve(reference: str) -> Set[int]:
        if reference not in resolved:
            resolved[reference] = tag_resolver(reference)
        return resolved[reference]

    for term in terms:
        for account_id, balance in balance_by_account.items():
            code, tag_ids = account_info.get(account_id, ("", set()))
            if account_matches_term(
                term, code, tag_ids, balance,
                tag_resolver=resolve if tag_resolver else None,
            ):
                total += term.sign * balance

    return total


def account_coefficients(
    terms: Iterable[AccountCodeTerm],
    balance_by_account: Mapping[int, float],
    account_info: Mapping[int, AccountInfo],
    tag_resolver: Optional[Callable[[str], Set[int]]] = None,
) -> Dict[int, float]:
    """Coefficient de chaque compte retenu par une formule ``account_codes``.

    ``sum_account_codes`` rend un total ; cette fonction en rend le détail :
    pour chaque compte, la somme des signes des termes qui le retiennent. Les
    deux disent la même chose — ``somme(coefficient x solde)`` vaut le total —
    mais le détail permet de ventiler la ligne par compte, par tiers ou par
    journal sans réécrire la sélection des comptes.

    Le sens du solde (suffixes ``D`` et ``C``) reste jugé sur le solde
    **total** du compte, jamais sur celui d'un sous-groupe. Un compte retenu
    l'est tout entier, et ses écritures se répartissent ensuite : sans cette
    règle, le détail d'une ligne ne sommerait plus à la ligne elle-même, ce
    qui est la seule chose qu'un dépliage ne doit jamais faire.
    """
    coefficients: Dict[int, float] = {}
    resolved: Dict[str, Set[int]] = {}

    def resolve(reference: str) -> Set[int]:
        if reference not in resolved:
            resolved[reference] = tag_resolver(reference)
        return resolved[reference]

    for term in terms:
        for account_id, balance in balance_by_account.items():
            code, tag_ids = account_info.get(account_id, ("", set()))
            if account_matches_term(
                term, code, tag_ids, balance,
                tag_resolver=resolve if tag_resolver else None,
            ):
                coefficients[account_id] = coefficients.get(account_id, 0.0) + term.sign

    return coefficients
