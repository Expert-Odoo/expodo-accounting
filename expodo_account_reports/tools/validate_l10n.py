# -*- coding: utf-8 -*-
# Copyright 2026 Expodo (https://expodo.fr)
# License LGPL-3
"""Valide le moteur d'expressions contre un fichier de localisation réel.

Usage :
    python3 tools/validate_l10n.py <chemin_vers_tax_report_data.xml>

Le script analyse le XML d'une localisation Odoo (ex. ``l10n_fr_account``),
reconstruit le graphe d'expressions, vérifie qu'il est acyclique et
entièrement résolvable, puis l'évalue avec des valeurs de feuilles
synthétiques. C'est le test de non-régression principal du moteur : si la
CA3 passe, la grammaire est correctement implémentée.
"""

import argparse
import random
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine.formula import FormulaError  # noqa: E402
from engine.shortcuts import SHORTCUT_ENGINES, SHORTCUT_LABEL, find_shortcut  # noqa: E402
from engine.resolver import (  # noqa: E402
    ExpressionSpec,
    compile_expressions,
    evaluate_all,
    resolve_order,
)


def _direct_field(record: ET.Element, name: str) -> Optional[str]:
    """Valeur d'un champ scalaire, enfant direct du record."""
    for child in record:
        if child.tag == "field" and child.get("name") == name:
            return (child.text or "").strip()
    return None


def _field_records(record: ET.Element, name: str, model: str) -> List[ET.Element]:
    """Records d'un modèle donné, imbriqués dans un champ o2m direct."""
    for child in record:
        if child.tag == "field" and child.get("name") == name:
            return [r for r in child if r.tag == "record" and r.get("model") == model]
    return []


def parse_report_lines(path: Path) -> Tuple[List[ExpressionSpec], Dict[str, str]]:
    """Extrait les expressions d'un XML de rapport, à plat.

    Retourne les specs et une table ``id XML de ligne -> code``, utile pour
    diagnostiquer les lignes dépourvues de code.
    """
    root = ET.parse(path).getroot()
    specs: List[ExpressionSpec] = []
    line_names: Dict[str, str] = {}
    anonymous = 0

    def walk(line: ET.Element) -> str:
        nonlocal anonymous
        code = _direct_field(line, "code")
        if not code:
            # Une ligne sans code ne peut pas être référencée par une formule ;
            # on lui attribue une clé interne pour rester traçable.
            anonymous += 1
            code = "__anon_%d__" % anonymous
        line_names[line.get("id") or code] = code

        children = _field_records(line, "children_ids", "account.report.line")
        children_codes = [walk(child) for child in children]

        # Un raccourci sur la ligne remplace l'expression `balance` déclarée
        # en forme longue (cf. _create_report_expression côté Odoo).
        shortcut = find_shortcut({
            name: _direct_field(line, name) or "" for name in SHORTCUT_ENGINES
        })

        for expr in _field_records(line, "expression_ids", "account.report.expression"):
            label = _direct_field(expr, "label") or ""
            if shortcut and label == SHORTCUT_LABEL:
                continue
            specs.append(
                ExpressionSpec(
                    line_code=code,
                    label=label,
                    engine=_direct_field(expr, "engine") or "",
                    formula=_direct_field(expr, "formula") or "",
                    subformula=_direct_field(expr, "subformula"),
                    children_codes=children_codes,
                )
            )

        if shortcut:
            engine, formula, subformula = shortcut
            specs.append(
                ExpressionSpec(
                    line_code=code,
                    label=SHORTCUT_LABEL,
                    engine=engine,
                    formula=formula,
                    subformula=subformula,
                    children_codes=children_codes,
                )
            )
        return code

    for report in root.iter("record"):
        if report.get("model") != "account.report":
            continue
        for line in _field_records(report, "line_ids", "account.report.line"):
            walk(line)

    return specs, line_names


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("xml", type=Path, help="Fichier XML de rapport à valider")
    parser.add_argument("--seed", type=int, default=20260902)
    args = parser.parse_args()

    specs, _ = parse_report_lines(args.xml)
    print("Fichier            : %s" % args.xml.name)
    print("Expressions lues   : %d" % len(specs))

    by_engine: Dict[str, int] = {}
    for spec in specs:
        by_engine[spec.engine] = by_engine.get(spec.engine, 0) + 1
    print("Par moteur         : %s" % ", ".join(
        "%s=%d" % item for item in sorted(by_engine.items())
    ))

    # --- 1. Compilation : la grammaire est-elle intégralement couverte ? ---
    try:
        compiled = compile_expressions(specs)
    except FormulaError as error:
        print("\n[ECHEC] Compilation : %s" % error)
        return 1
    print("Compilation        : OK (%d expressions)" % len(compiled))

    # --- 2. Résolution du graphe : cycles, références orphelines ---
    try:
        order = resolve_order(compiled)
    except FormulaError as error:
        print("\n[ECHEC] Résolution : %s" % error)
        return 1
    print("Tri topologique    : OK (%d noeuds ordonnés)" % len(order))

    # Vérification explicite : toute dépendance précède son consommateur.
    position = {key: index for index, key in enumerate(order)}
    for key, expression in compiled.items():
        for dependency in expression.dependencies:
            if position[dependency] >= position[key]:
                print("\n[ECHEC] Ordre invalide : %s.%s avant %s.%s" % (key + dependency))
                return 1
    print("Ordre vérifié      : OK")

    # --- 3. Évaluation avec des valeurs de feuilles synthétiques ---
    random.seed(args.seed)
    leaf_values = {
        key: round(random.uniform(-50_000, 50_000), 2)
        for key, expression in compiled.items()
        if expression.is_leaf
    }
    try:
        values = evaluate_all(compiled, leaf_values)
    except FormulaError as error:
        print("\n[ECHEC] Évaluation : %s" % error)
        return 1
    print("Évaluation         : OK (%d valeurs)" % len(values))

    # --- 4. Contrôles de cohérence métier ---
    failures = 0

    # 4a. Les expressions à sous-formule round(0) doivent être entières.
    rounded = [
        key for key, expression in compiled.items()
        if expression.sub.kind == "round" and expression.sub.digits == 0
    ]
    non_integers = [key for key in rounded if values[key] != int(values[key])]
    if non_integers:
        print("\n[ECHEC] %d expressions round(0) non entières, ex. %s.%s"
              % (len(non_integers), *non_integers[0]))
        failures += 1
    else:
        print("Arrondis round(0)  : OK (%d expressions entières)" % len(rounded))

    # 4b. Les sous-formules if_other_expr_above doivent neutraliser les négatifs.
    guarded = [
        (key, expression) for key, expression in compiled.items()
        if expression.sub.kind == "if_other_expr_above"
    ]
    leaks = [
        key for key, expression in guarded
        if values[expression.sub.other_ref] <= expression.sub.bounds[0].amount
        and values[key] != 0.0
    ]
    if leaks:
        print("\n[ECHEC] %d gardes if_other_expr_above non appliquées, ex. %s.%s"
              % (len(leaks), *leaks[0]))
        failures += 1
    else:
        print("Gardes if_other_*  : OK (%d expressions gardées)" % len(guarded))

    # 4c. Recalcul indépendant d'une addition simple, pour valider l'arithmétique.
    checked = 0
    for key, expression in compiled.items():
        spec = expression.spec
        if expression.is_leaf or expression.is_sum_children:
            continue
        if spec.formula.count("+") != 1 or any(op in spec.formula for op in "-*/"):
            continue
        left, right = (parse.strip() for parse in spec.formula.split("+"))
        try:
            expected = values[tuple(left.split("."))] + values[tuple(right.split("."))]
        except KeyError:
            continue
        from engine.formula import apply_subformula
        expected = apply_subformula(expected, expression.sub, values)
        if abs(expected - values[key]) > 1e-9:
            print("\n[ECHEC] Arithmétique : %s.%s attendu %s, obtenu %s"
                  % (key + (expected, values[key])))
            failures += 1
            break
        checked += 1
    if not failures:
        print("Arithmétique       : OK (%d additions recalculées)" % checked)

    print()
    if failures:
        print("RESULTAT : %d contrôle(s) en échec" % failures)
        return 1
    print("RESULTAT : moteur validé sur %s" % args.xml.name)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
