# -*- coding: utf-8 -*-
# Copyright 2026 Expodo (https://expodo.fr)
# License LGPL-3
"""Analyse des formules du moteur de rapports ``account.report``.

Ce module est **volontairement dépourvu de toute dépendance Odoo**. Il ne
manipule que des chaînes et des nombres, ce qui permet de le tester en
isolation, hors instance, et de valider la grammaire contre les fichiers de
localisation réels avant même d'installer le module.

Grammaire de référence : ``odoo/addons/account/models/account_report.py``
(module ``account``, édition Community, LGPL-3), qui définit les expressions
régulières faisant autorité pour les moteurs ``account_codes`` et
``aggregation``. Les fonctions ci-dessous sont une implémentation
indépendante de cette grammaire publique.
"""

import re
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Set, Tuple

# --------------------------------------------------------------------------
# Grammaire (alignée sur les regex publiques du module `account`)
# --------------------------------------------------------------------------

ACCOUNT_CODES_SPLIT_RE = re.compile(r"(?=[+-])")
ACCOUNT_CODES_TERM_RE = re.compile(
    r"^(?P<sign>[+-]?)"
    r"(?P<prefix>([A-Za-z\d.]*|tag\([\w.]+\))((?=\\)|(?<=[^CD])))"
    r"(\\\((?P<excluded>([A-Za-z\d.]+,)*[A-Za-z\d.]*)\))?"
    r"(?P<balance_character>[DC]?)$"
)

NUMBER_RE = r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?"
LINE_CODE_RE = r"[+-]?[\s(]*[^().\s*/+\-]+\.[^().\s*/+\-]+"
OPERATOR_RE = r"[\s*/+\-]"
HARD_FORMULAS = ("sum_children",)

AGGREGATION_FORMULA_RE = re.compile(
    f'{"|".join(HARD_FORMULAS)}|'
    rf"[\s(]*(?:{NUMBER_RE}|{LINE_CODE_RE})[\s)]*"
    rf"(?:{OPERATOR_RE}[\s(]*(?:{NUMBER_RE}|{LINE_CODE_RE})[\s)]*)*"
)

TAG_PREFIX_RE = re.compile(r"^tag\((?P<ref>[\w.]+)\)$")
MONETARY_BOUND_RE = re.compile(r"^(?P<currency>[A-Z]{3})\(\s*(?P<amount>-?[\d.]+)\s*\)$")
CROSS_REPORT_RE = re.compile(r"^cross_report\((?P<report>.+)\)$")
ROUND_RE = re.compile(r"^round\(\s*(?P<digits>-?\d+)\s*\)$")

#: Tranche d'ancienneté ``aged(min, max)``, bornes en jours révolus depuis la
#: date d'échéance ; ``max`` peut valoir ``*`` pour « et au-delà ».
#: Extension Expodo : Community ne définit aucun mécanisme de tranche, et un
#: domaine XML statique ne peut pas porter des bornes calculées depuis la date
#: de clôture demandée.
AGED_RE = re.compile(r"^aged\(\s*(?P<min>-?\d+)\s*,\s*(?P<max>-?\d+|\*)\s*\)$")

#: Sous-formules acceptées par le moteur ``domain``.
#: Les deux dernières sont des extensions Expodo : Community ne définit
#: aucun agrégat débit/crédit, pourtant indispensable à une balance.
DOMAIN_SUBFORMULAS = (
    "sum", "-sum", "sum_if_pos", "sum_if_neg", "count_rows",
    "sum_debit", "sum_credit",
    # Colonnes de détail, et non agrégats : la date et le tiers de
    # l'écriture. Un grand livre sans date n'est pas un grand livre — on y
    # cherche *quand* un compte a bougé autant que de combien.
    "line_date",
    "line_partner",
    # Nombre de **pièces**, et non de lignes d'écriture.
    #
    # Sur un journal, « nombre » désigne les pièces : un lecteur qui voit
    # trente sur un journal de quinze écritures croit à un double
    # enregistrement. La distinction n'apparaît qu'en comparant au grand
    # livre, ce que personne ne fait.
    "count_moves",
)

#: Modes d'arrondi entiers d'``account.report.integer_rounding``.
#:
#: ``round()`` de Python applique l'arrondi **bancaire** : le demi va vers le
#: pair, donc ``round(436.5)`` vaut 436 et non 437. L'administration fiscale
#: française impose l'arrondi au plus proche, demi vers le haut. Utiliser
#: l'arrondi de Python sous-déclare d'un euro chaque case tombant sur un demi,
#: sur un formulaire transmis au fisc.
ROUNDING_MODES = {
    "HALF-UP": ROUND_HALF_UP,
    "UP": ROUND_CEILING,
    "DOWN": ROUND_FLOOR,
}

#: Sous-formules acceptées par le moteur ``external``.
EXTERNAL_SUBFORMULAS = ("sum", "most_recent")


class FormulaError(ValueError):
    """Formule ou sous-formule syntaxiquement invalide."""


# --------------------------------------------------------------------------
# Références d'expression
# --------------------------------------------------------------------------

#: Une expression est identifiée par ``(code de ligne, libellé)``.
ExpressionKey = Tuple[str, str]


def parse_expression_ref(token: str) -> ExpressionKey:
    """``"box_A1.balance"`` -> ``("box_A1", "balance")``."""
    cleaned = token.strip().strip("()").strip()
    if cleaned.startswith(("+", "-")):
        cleaned = cleaned[1:].strip()
    if cleaned.count(".") != 1:
        raise FormulaError(
            "Référence d'expression invalide : %r (attendu code_ligne.libelle)" % token
        )
    line_code, label = cleaned.split(".")
    if not line_code or not label:
        raise FormulaError("Référence d'expression incomplète : %r" % token)
    return line_code, label


# --------------------------------------------------------------------------
# Moteur `account_codes`
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class AccountCodeTerm:
    """Un terme de formule ``account_codes``, ex. ``-21\\(210,211)D``."""

    sign: int
    prefix: str
    excluded_prefixes: Tuple[str, ...] = ()
    balance_character: str = ""  # '' | 'D' | 'C'
    tag_ref: Optional[str] = None

    @property
    def is_tag(self) -> bool:
        return self.tag_ref is not None


def parse_account_codes_formula(formula: str) -> List[AccountCodeTerm]:
    """Découpe une formule ``account_codes`` en termes signés.

    Exemples acceptés : ``21``, ``-21``, ``21\\(210,211)``, ``400D``,
    ``ASSET_CURRENT``, ``tag(l10n_fr.tag_x)``, ``6 + 7 - 60``.
    """
    if not formula or not formula.strip():
        raise FormulaError("Formule account_codes vide")

    terms: List[AccountCodeTerm] = []
    for raw_token in ACCOUNT_CODES_SPLIT_RE.split(formula.replace(" ", "")):
        if not raw_token:
            # Le split produit un premier token vide si la formule débute par un signe.
            continue
        match = ACCOUNT_CODES_TERM_RE.match(raw_token)
        if not match:
            raise FormulaError("Terme account_codes invalide : %r" % raw_token)

        prefix = match.group("prefix")
        tag_match = TAG_PREFIX_RE.match(prefix)
        excluded = match.group("excluded")

        terms.append(
            AccountCodeTerm(
                sign=-1 if match.group("sign") == "-" else 1,
                prefix="" if tag_match else prefix,
                excluded_prefixes=tuple(p for p in (excluded or "").split(",") if p),
                balance_character=match.group("balance_character") or "",
                tag_ref=tag_match.group("ref") if tag_match else None,
            )
        )

    if not terms:
        raise FormulaError("Formule account_codes sans terme exploitable : %r" % formula)
    return terms


# --------------------------------------------------------------------------
# Moteur `aggregation` — analyseur syntaxique descendant récursif
# --------------------------------------------------------------------------

@dataclass
class Node:
    """Noeud d'AST arithmétique."""

    kind: str  # 'num' | 'ref' | 'binop'
    value: float = 0.0
    ref: Optional[ExpressionKey] = None
    op: str = ""
    left: Optional["Node"] = None
    right: Optional["Node"] = None


# L'ordre des alternatives est significatif. `0.2` et `box_08.balance` sont
# tous deux de la forme <jeton>.<jeton> : on tente d'abord le nombre, avec une
# anticipation négative qui le rejette s'il est en réalité le début d'une
# référence (cas d'un code de ligne purement numérique, ex. `08.balance`).
_TOKEN_RE = re.compile(
    r"""
    (?P<space>\s+)
  | (?P<lpar>\()
  | (?P<rpar>\))
  | (?P<op>[*/+\-])
  | (?P<num>(?:\d+(?:\.\d*)?|\.\d+)(?![\w.]))
  | (?P<ref>[^().\s*/+\-]+\.[^().\s*/+\-]+)
    """,
    re.VERBOSE,
)


def _tokenize(formula: str) -> List[Tuple[str, str]]:
    tokens: List[Tuple[str, str]] = []
    pos = 0
    while pos < len(formula):
        match = _TOKEN_RE.match(formula, pos)
        if not match:
            raise FormulaError(
                "Caractère inattendu à la position %d dans %r" % (pos, formula)
            )
        pos = match.end()
        kind = match.lastgroup
        if kind == "space":
            continue
        tokens.append((kind, match.group()))
    return tokens


class _Parser:
    """Grammaire : expr := term (('+'|'-') term)* ; term := factor (('*'|'/') factor)*"""

    def __init__(self, tokens: Sequence[Tuple[str, str]], formula: str):
        self._tokens = list(tokens)
        self._pos = 0
        self._formula = formula

    def _peek(self) -> Optional[Tuple[str, str]]:
        return self._tokens[self._pos] if self._pos < len(self._tokens) else None

    def _next(self) -> Tuple[str, str]:
        token = self._peek()
        if token is None:
            raise FormulaError("Fin de formule inattendue : %r" % self._formula)
        self._pos += 1
        return token

    def parse(self) -> Node:
        node = self._parse_expr()
        if self._peek() is not None:
            raise FormulaError(
                "Jeton résiduel %r dans %r" % (self._peek()[1], self._formula)
            )
        return node

    def _parse_expr(self) -> Node:
        node = self._parse_term()
        while (token := self._peek()) and token[0] == "op" and token[1] in "+-":
            self._next()
            node = Node(kind="binop", op=token[1], left=node, right=self._parse_term())
        return node

    def _parse_term(self) -> Node:
        node = self._parse_factor()
        while (token := self._peek()) and token[0] == "op" and token[1] in "*/":
            self._next()
            node = Node(kind="binop", op=token[1], left=node, right=self._parse_factor())
        return node

    def _parse_factor(self) -> Node:
        kind, text = self._next()
        if kind == "op" and text in "+-":
            operand = self._parse_factor()
            if text == "-":
                return Node(
                    kind="binop", op="*", left=Node(kind="num", value=-1.0), right=operand
                )
            return operand
        if kind == "lpar":
            node = self._parse_expr()
            closing = self._next()
            if closing[0] != "rpar":
                raise FormulaError("Parenthèse fermante manquante : %r" % self._formula)
            return node
        if kind == "num":
            return Node(kind="num", value=float(text))
        if kind == "ref":
            return Node(kind="ref", ref=parse_expression_ref(text))
        raise FormulaError("Jeton inattendu %r dans %r" % (text, self._formula))


def parse_aggregation_formula(formula: str) -> Optional[Node]:
    """Retourne l'AST d'une formule ``aggregation``.

    Retourne ``None`` pour la formule spéciale ``sum_children``, dont les
    opérandes ne sont pas dans la formule mais dans la hiérarchie des lignes.
    """
    stripped = (formula or "").strip()
    if not stripped:
        raise FormulaError("Formule aggregation vide")
    if stripped in HARD_FORMULAS:
        return None
    if not AGGREGATION_FORMULA_RE.fullmatch(stripped):
        raise FormulaError("Formule aggregation invalide : %r" % formula)
    return _Parser(_tokenize(stripped), stripped).parse()


def collect_refs(node: Optional[Node]) -> Set[ExpressionKey]:
    """Toutes les expressions référencées par un AST."""
    if node is None:
        return set()
    if node.kind == "ref":
        return {node.ref}
    if node.kind == "binop":
        return collect_refs(node.left) | collect_refs(node.right)
    return set()


def evaluate(node: Optional[Node], values: Dict[ExpressionKey, float]) -> float:
    """Évalue un AST à partir d'un dictionnaire de valeurs déjà résolues."""
    if node is None:
        raise FormulaError("sum_children doit être résolu par l'appelant")
    if node.kind == "num":
        return node.value
    if node.kind == "ref":
        if node.ref not in values:
            raise FormulaError("Valeur manquante pour %s.%s" % node.ref)
        return values[node.ref]
    left = evaluate(node.left, values)
    right = evaluate(node.right, values)
    if node.op == "+":
        return left + right
    if node.op == "-":
        return left - right
    if node.op == "*":
        return left * right
    if node.op == "/":
        # Une division par zéro dans un rapport comptable est une donnée
        # absente, pas une erreur : on neutralise la ligne.
        return 0.0 if right == 0 else left / right
    raise FormulaError("Opérateur non supporté : %r" % node.op)


# --------------------------------------------------------------------------
# Sous-formules
# --------------------------------------------------------------------------

@dataclass
class MonetaryBound:
    currency: str
    amount: float


@dataclass
class Subformula:
    """Représentation normalisée d'une sous-formule d'expression."""

    raw: str = ""
    kind: str = "none"
    # kind == 'round'
    digits: Optional[int] = None
    # kind == 'if_above' | 'if_below' | 'if_between'
    bounds: Tuple[MonetaryBound, ...] = ()
    # kind == 'if_other_expr_above' | 'if_other_expr_below'
    other_ref: Optional[ExpressionKey] = None
    # kind == 'cross_report'
    report_ref: Optional[str] = None
    # moteur external
    editable: bool = False
    rounding: Optional[int] = None
    aggregator: Optional[str] = None
    #: Borne éventuelle attachée à une chaîne `external`
    #: (`if_above` ou `if_below`), distincte de `kind` qui vaut ici
    #: « external ». Les deux ne peuvent pas partager le même champ :
    #: la chaîne est d'abord une sous-formule external, la borne n'en
    #: est qu'une option.
    bound_kind: Optional[str] = None
    # moteur domain
    domain_aggregator: Optional[str] = None
    # kind == 'aged' : bornes en jours, `aged_max` à None pour « et au-delà »
    aged_min: Optional[int] = None
    aged_max: Optional[int] = None
    extra_refs: Tuple[ExpressionKey, ...] = field(default=())


def _parse_bound(token: str) -> MonetaryBound:
    match = MONETARY_BOUND_RE.match(token.strip())
    if not match:
        raise FormulaError("Borne monétaire invalide : %r (attendu EUR(0))" % token)
    return MonetaryBound(match.group("currency"), float(match.group("amount")))


def _split_args(raw: str) -> List[str]:
    """Découpe les arguments de premier niveau d'un appel ``f(a, b)``."""
    args, depth, current = [], 0, ""
    for char in raw:
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        if char == "," and depth == 0:
            args.append(current)
            current = ""
        else:
            current += char
    if current.strip():
        args.append(current)
    return [arg.strip() for arg in args]


def parse_subformula(subformula: Optional[str], engine: str = "aggregation") -> Subformula:
    """Normalise la sous-formule d'une expression selon son moteur."""
    raw = (subformula or "").strip()
    if not raw:
        return Subformula(raw="", kind="none")

    if engine == "domain":
        aged = AGED_RE.match(raw)
        if aged:
            minimum = int(aged.group("min"))
            maximum = aged.group("max")
            maximum = None if maximum == "*" else int(maximum)
            if maximum is not None and maximum < minimum:
                raise FormulaError(
                    "Tranche d'ancienneté inversée : %r (max < min)" % raw
                )
            return Subformula(raw=raw, kind="aged", domain_aggregator="sum",
                              aged_min=minimum, aged_max=maximum)
        if raw not in DOMAIN_SUBFORMULAS:
            raise FormulaError("Sous-formule domain inconnue : %r" % raw)
        return Subformula(raw=raw, kind="domain", domain_aggregator=raw)

    if engine == "external":
        parts = [part.strip() for part in raw.split(";") if part.strip()]
        result = Subformula(raw=raw, kind="external")
        for part in parts:
            if part == "editable":
                result.editable = True
            elif part.startswith("rounding="):
                result.rounding = int(part.split("=", 1)[1])
            elif part in EXTERNAL_SUBFORMULAS:
                result.aggregator = part
            elif part.startswith("if_above(") or part.startswith("if_below("):
                # Une borne peut s'attacher à une valeur saisie manuellement.
                #
                # La déclaration espagnole Mod 130 écrit
                # `editable;rounding=2;if_above(EUR(0))` : la case ne compte
                # que si le montant saisi est positif. La grammaire connaissait
                # déjà `if_above` comme sous-formule autonome, mais pas comme
                # option d'une chaîne `external` — la déclaration refusait donc
                # de s'afficher entièrement.
                nom = part.split("(", 1)[0]
                args = _split_args(part[len(nom) + 1 : -1])
                if len(args) != 1:
                    raise FormulaError("%s attend 1 argument : %r" % (nom, part))
                result.bound_kind = nom
                result.bounds = (_parse_bound(args[0]),)
            else:
                raise FormulaError("Option external inconnue : %r" % part)
        return result

    if (match := ROUND_RE.match(raw)):
        return Subformula(raw=raw, kind="round", digits=int(match.group("digits")))

    if (match := CROSS_REPORT_RE.match(raw)):
        return Subformula(raw=raw, kind="cross_report", report_ref=match.group("report").strip())

    for name in ("if_other_expr_above", "if_other_expr_below"):
        if raw.startswith(name + "("):
            args = _split_args(raw[len(name) + 1 : -1])
            if len(args) != 2:
                raise FormulaError("%s attend 2 arguments : %r" % (name, raw))
            ref = parse_expression_ref(args[0])
            return Subformula(
                raw=raw, kind=name, other_ref=ref,
                bounds=(_parse_bound(args[1]),), extra_refs=(ref,),
            )

    for name in ("if_above", "if_below"):
        if raw.startswith(name + "("):
            args = _split_args(raw[len(name) + 1 : -1])
            if len(args) != 1:
                raise FormulaError("%s attend 1 argument : %r" % (name, raw))
            return Subformula(raw=raw, kind=name, bounds=(_parse_bound(args[0]),))

    if raw.startswith("if_between("):
        args = _split_args(raw[len("if_between(") : -1])
        if len(args) != 2:
            raise FormulaError("if_between attend 2 arguments : %r" % raw)
        return Subformula(
            raw=raw, kind="if_between",
            bounds=(_parse_bound(args[0]), _parse_bound(args[1])),
        )

    raise FormulaError("Sous-formule non reconnue : %r" % raw)


def round_value(value: float, digits: int, rounding: str = "HALF-UP") -> float:
    """Arrondit sans jamais passer par ``round()`` de Python.

    :param rounding: mode déclaré par le rapport (`integer_rounding`).
    """
    mode = ROUNDING_MODES.get(rounding or "HALF-UP", ROUND_HALF_UP)
    quantum = Decimal(1).scaleb(-digits) if digits > 0 else Decimal(10) ** -digits
    return float(Decimal(str(value)).quantize(quantum, rounding=mode))


def apply_subformula(
    value: float,
    sub: Subformula,
    values: Optional[Dict[ExpressionKey, float]] = None,
    rounding: str = "HALF-UP",
) -> float:
    """Applique la sous-formule à la valeur brute d'une expression.

    :param rounding: mode d'arrondi entier déclaré par le rapport.
    """
    if sub.kind == "external" and sub.bound_kind:
        borne = sub.bounds[0].amount
        if sub.bound_kind == "if_above":
            return value if value > borne else 0.0
        return value if value < borne else 0.0
    if sub.kind in ("none", "domain", "external", "cross_report", "aged"):
        return value
    if sub.kind == "round":
        return round_value(value, sub.digits, rounding)
    if sub.kind == "if_above":
        return value if value > sub.bounds[0].amount else 0.0
    if sub.kind == "if_below":
        return value if value < sub.bounds[0].amount else 0.0
    if sub.kind == "if_between":
        low, high = sub.bounds[0].amount, sub.bounds[1].amount
        return value if low <= value <= high else 0.0
    if sub.kind in ("if_other_expr_above", "if_other_expr_below"):
        if values is None or sub.other_ref not in values:
            raise FormulaError("Valeur manquante pour %s.%s" % sub.other_ref)
        other = values[sub.other_ref]
        bound = sub.bounds[0].amount
        satisfied = other > bound if sub.kind.endswith("above") else other < bound
        return value if satisfied else 0.0
    raise FormulaError("Sous-formule non applicable : %r" % sub.kind)
