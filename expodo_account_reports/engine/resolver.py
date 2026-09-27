# -*- coding: utf-8 -*-
# Copyright 2026 Expodo (https://expodo.fr)
# License LGPL-3
"""Graphe de dépendances et ordre d'évaluation des expressions.

Sans dépendance Odoo, comme :mod:`formula`. L'appelant fournit une
description plate des expressions ; ce module en déduit l'ordre dans lequel
les évaluer et effectue le calcul des expressions ``aggregation``.

Les moteurs terminaux (``domain``, ``account_codes``, ``tax_tags``,
``external``) sont des feuilles : leurs valeurs sont calculées côté Odoo et
injectées ici. C'est cette séparation qui rend le coeur testable hors
instance.
"""

from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Set

from .formula import (
    ExpressionKey,
    FormulaError,
    Node,
    Subformula,
    apply_subformula,
    collect_refs,
    evaluate,
    parse_aggregation_formula,
    parse_subformula,
)

#: Moteurs dont la valeur est produite hors du graphe (requêtes SQL/ORM).
LEAF_ENGINES = ("domain", "account_codes", "tax_tags", "external", "custom")

#: Préfixe des libellés dont la valeur provient d'une période antérieure.
#: Ces expressions ne créent donc pas de dépendance dans la période courante.
CARRYOVER_APPLIED_PREFIX = "_applied_carryover_"


class CyclicDependencyError(FormulaError):
    """Le graphe d'expressions contient un cycle."""


@dataclass
class ExpressionSpec:
    """Description d'une expression, indépendante de l'ORM."""

    line_code: str
    label: str
    engine: str
    formula: str
    subformula: Optional[str] = None
    #: Codes des lignes enfants, requis uniquement pour ``sum_children``.
    children_codes: List[str] = field(default_factory=list)

    @property
    def key(self) -> ExpressionKey:
        return (self.line_code, self.label)


@dataclass
class CompiledExpression:
    """Expression analysée, prête à être évaluée."""

    spec: ExpressionSpec
    ast: Optional[Node]
    sub: Subformula
    dependencies: Set[ExpressionKey]
    is_sum_children: bool = False

    @property
    def key(self) -> ExpressionKey:
        return self.spec.key

    @property
    def is_leaf(self) -> bool:
        return self.spec.engine in LEAF_ENGINES


def compile_expressions(specs: List[ExpressionSpec]) -> Dict[ExpressionKey, CompiledExpression]:
    """Analyse toutes les expressions et calcule leurs dépendances directes."""
    compiled: Dict[ExpressionKey, CompiledExpression] = {}

    for spec in specs:
        sub = parse_subformula(spec.subformula, engine=spec.engine)

        if spec.engine != "aggregation":
            compiled[spec.key] = CompiledExpression(
                spec=spec, ast=None, sub=sub, dependencies=set()
            )
            continue

        ast = parse_aggregation_formula(spec.formula)
        is_sum_children = ast is None

        if is_sum_children:
            deps = {(code, spec.label) for code in spec.children_codes}
        else:
            deps = collect_refs(ast)

        deps |= set(sub.extra_refs)
        # Une expression de report antérieur est une donnée d'entrée, pas une
        # dépendance de la période en cours : l'inclure créerait un faux cycle.
        deps = {
            dep for dep in deps
            if not dep[1].startswith(CARRYOVER_APPLIED_PREFIX)
        }

        compiled[spec.key] = CompiledExpression(
            spec=spec, ast=ast, sub=sub, dependencies=deps,
            is_sum_children=is_sum_children,
        )

    return compiled


def resolve_order(
    compiled: Dict[ExpressionKey, CompiledExpression],
    roots: Optional[Set[ExpressionKey]] = None,
) -> List[ExpressionKey]:
    """Tri topologique des expressions à évaluer.

    Retourne les clés dans un ordre tel que toute expression apparaît après
    ses dépendances. Si ``roots`` est fourni, seul le sous-graphe nécessaire
    à ces expressions est retourné — c'est ce qui permet de ne calculer que
    les lignes réellement affichées.
    """
    if roots is None:
        roots = set(compiled)

    order: List[ExpressionKey] = []
    state: Dict[ExpressionKey, int] = {}  # 0 = en cours, 1 = terminé
    stack: List[ExpressionKey] = []

    def visit(key: ExpressionKey) -> None:
        if state.get(key) == 1:
            return
        if state.get(key) == 0:
            cycle = " -> ".join("%s.%s" % k for k in stack[stack.index(key):] + [key])
            raise CyclicDependencyError("Cycle de dépendances : %s" % cycle)
        if key not in compiled:
            # Référence vers une expression absente : c'est une erreur de
            # définition, pas une valeur nulle silencieuse.
            raise FormulaError("Expression référencée introuvable : %s.%s" % key)

        state[key] = 0
        stack.append(key)
        for dependency in sorted(compiled[key].dependencies):
            visit(dependency)
        stack.pop()
        state[key] = 1
        order.append(key)

    for root in sorted(roots):
        visit(root)

    return order


def evaluate_all(
    compiled: Dict[ExpressionKey, CompiledExpression],
    leaf_values: Dict[ExpressionKey, float],
    roots: Optional[Set[ExpressionKey]] = None,
    on_missing_leaf: Optional[Callable[[ExpressionKey], float]] = None,
    rounding: str = "HALF-UP",
) -> Dict[ExpressionKey, float]:
    """Évalue le graphe complet.

    :param leaf_values: valeurs des moteurs terminaux, calculées côté Odoo.
    :param on_missing_leaf: appelé si une feuille n'a pas de valeur fournie.
        Par défaut la feuille vaut 0.0 — une ligne sans écriture est un zéro
        comptable légitime, pas une erreur.
    :param rounding: mode d'arrondi entier du rapport (``integer_rounding``).
    """
    order = resolve_order(compiled, roots=roots)
    values: Dict[ExpressionKey, float] = {}

    for key in order:
        expression = compiled[key]

        if expression.is_leaf:
            if key in leaf_values:
                raw = leaf_values[key]
            elif on_missing_leaf is not None:
                raw = on_missing_leaf(key)
            else:
                raw = 0.0
        elif expression.is_sum_children:
            raw = sum(
                values.get((code, expression.spec.label), 0.0)
                for code in expression.spec.children_codes
            )
        else:
            raw = evaluate(expression.ast, values)

        values[key] = apply_subformula(raw, expression.sub, values, rounding=rounding)

    return values
