# -*- coding: utf-8 -*-
# Copyright 2026 Expodo (https://expodo.fr)
# License LGPL-3
"""Tests unitaires du coeur du moteur d'expressions.

Exécutables sans instance Odoo :

    python3 -m unittest discover -s tests_offline -t .

Les moteurs ``domain`` et ``account_codes`` ne sont exercés par aucun rapport
de TVA de localisation (ils servent au bilan et au compte de résultat, dont
les définitions ne sont pas livrées en Community). Ils sont donc couverts ici
à partir de la grammaire de référence, et non par les fichiers réels.
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine.accounts import sum_account_codes
from engine.formula import round_value
from engine.formula import (
    FormulaError,
    apply_subformula,
    collect_refs,
    evaluate,
    parse_account_codes_formula,
    parse_aggregation_formula,
    parse_expression_ref,
    parse_subformula,
)
from engine.resolver import (
    CyclicDependencyError,
    ExpressionSpec,
    compile_expressions,
    evaluate_all,
    resolve_order,
)
from engine.shortcuts import expand_shortcut

DELTA = 1e-9


class TestAccountCodes(unittest.TestCase):
    """Moteur ``account_codes`` : préfixes de comptes."""

    def test_simple_prefix(self):
        terms = parse_account_codes_formula("21")
        self.assertEqual(len(terms), 1)
        self.assertEqual(terms[0].prefix, "21")
        self.assertEqual(terms[0].sign, 1)
        self.assertEqual(terms[0].balance_character, "")

    def test_multiple_signed_terms(self):
        terms = parse_account_codes_formula("6 + 7 - 60")
        self.assertEqual([t.prefix for t in terms], ["6", "7", "60"])
        self.assertEqual([t.sign for t in terms], [1, 1, -1])

    def test_leading_minus(self):
        terms = parse_account_codes_formula("-21")
        self.assertEqual(len(terms), 1)
        self.assertEqual(terms[0].sign, -1)
        self.assertEqual(terms[0].prefix, "21")

    def test_excluded_prefixes(self):
        terms = parse_account_codes_formula(r"21\(210,211)")
        self.assertEqual(terms[0].prefix, "21")
        self.assertEqual(terms[0].excluded_prefixes, ("210", "211"))

    def test_balance_character(self):
        for formula, prefix, character in (("400D", "400", "D"), ("400C", "400", "C")):
            with self.subTest(formula=formula):
                term = parse_account_codes_formula(formula)[0]
                self.assertEqual(term.prefix, prefix)
                self.assertEqual(term.balance_character, character)

    def test_alphabetic_prefix(self):
        """Un préfixe alphabétique est accepté tant qu'il ne finit pas par D/C."""
        term = parse_account_codes_formula("ABC1")[0]
        self.assertEqual(term.prefix, "ABC1")
        self.assertEqual(term.balance_character, "")

    def test_trailing_letter_is_read_as_balance_character(self):
        """Ambiguïté assumée de la grammaire : un préfixe finissant par D ou C
        voit sa dernière lettre interprétée comme un filtre débit/crédit.
        Comportement identique au moteur natif — à documenter, pas à corriger.
        """
        term = parse_account_codes_formula("ABC")[0]
        self.assertEqual(term.prefix, "AB")
        self.assertEqual(term.balance_character, "C")

    def test_underscore_rejected(self):
        """Le jeu de caractères des préfixes est [A-Za-z0-9.] — pas d'underscore.
        Les codes de groupe type ``ASSET_CURRENT`` ne sont donc pas utilisables
        avec ce moteur : il adresse des préfixes de numéros de compte.
        """
        with self.assertRaises(FormulaError):
            parse_account_codes_formula("ASSET_CURRENT")

    def test_tag_prefix(self):
        term = parse_account_codes_formula("tag(l10n_fr.tag_demo)")[0]
        self.assertTrue(term.is_tag)
        self.assertEqual(term.tag_ref, "l10n_fr.tag_demo")

    def test_combined(self):
        terms = parse_account_codes_formula(r"21\(210)D - 28C")
        self.assertEqual(len(terms), 2)
        self.assertEqual(terms[0].excluded_prefixes, ("210",))
        self.assertEqual(terms[0].balance_character, "D")
        self.assertEqual(terms[1].sign, -1)
        self.assertEqual(terms[1].balance_character, "C")

    def test_empty_formula_rejected(self):
        with self.assertRaises(FormulaError):
            parse_account_codes_formula("   ")


class TestAggregationParsing(unittest.TestCase):
    """Moteur ``aggregation`` : analyse syntaxique."""

    def test_single_reference(self):
        node = parse_aggregation_formula("box_A1.balance")
        self.assertEqual(collect_refs(node), {("box_A1", "balance")})

    def test_addition(self):
        node = parse_aggregation_formula("box_A1.balance_from_tags + box_A1.adjustment")
        self.assertEqual(
            collect_refs(node),
            {("box_A1", "balance_from_tags"), ("box_A1", "adjustment")},
        )

    def test_numeric_literal_not_a_reference(self):
        """Régression : ``0.2`` doit être un nombre, pas une référence."""
        node = parse_aggregation_formula("box_08_base.balance * 0.2")
        self.assertEqual(collect_refs(node), {("box_08_base", "balance")})

    def test_numeric_line_code_still_a_reference(self):
        """Un code de ligne purement numérique reste une référence."""
        node = parse_aggregation_formula("08.balance + 09.balance")
        self.assertEqual(collect_refs(node), {("08", "balance"), ("09", "balance")})

    def test_sum_children_is_special(self):
        self.assertIsNone(parse_aggregation_formula("sum_children"))

    def test_invalid_formula_rejected(self):
        for formula in ("box_A1.balance +", "box_A1", "&&&"):
            with self.subTest(formula=formula), self.assertRaises(FormulaError):
                parse_aggregation_formula(formula)

    def test_reference_without_label_rejected(self):
        with self.assertRaises(FormulaError):
            parse_expression_ref("box_A1")


class TestAggregationEvaluation(unittest.TestCase):
    """Moteur ``aggregation`` : arithmétique."""

    def _eval(self, formula, values):
        return evaluate(parse_aggregation_formula(formula), values)

    def test_operator_precedence(self):
        values = {("a", "b"): 2.0, ("c", "d"): 3.0, ("e", "f"): 4.0}
        self.assertAlmostEqual(
            self._eval("a.b + c.d * e.f", values), 14.0, delta=DELTA
        )

    def test_parentheses(self):
        values = {("a", "b"): 2.0, ("c", "d"): 3.0, ("e", "f"): 4.0}
        self.assertAlmostEqual(
            self._eval("(a.b + c.d) * e.f", values), 20.0, delta=DELTA
        )

    def test_unary_minus(self):
        self.assertAlmostEqual(
            self._eval("-a.b + c.d", {("a", "b"): 5.0, ("c", "d"): 2.0}),
            -3.0, delta=DELTA,
        )

    def test_division_by_zero_yields_zero(self):
        """Un dénominateur nul est une donnée absente, pas une exception."""
        self.assertEqual(self._eval("a.b / c.d", {("a", "b"): 5.0, ("c", "d"): 0.0}), 0.0)

    def test_missing_value_raises(self):
        with self.assertRaises(FormulaError):
            self._eval("a.b + c.d", {("a", "b"): 1.0})


class TestSubformulas(unittest.TestCase):
    """Sous-formules et leur application."""

    def test_round(self):
        sub = parse_subformula("round(0)")
        self.assertEqual(sub.kind, "round")
        self.assertEqual(apply_subformula(1234.56, sub), 1235.0)
        self.assertIsInstance(apply_subformula(1234.56, sub), float)

    def test_round_two_digits(self):
        sub = parse_subformula("round(2)")
        self.assertAlmostEqual(apply_subformula(1.23456, sub), 1.23, delta=DELTA)

    def test_if_above(self):
        sub = parse_subformula("if_above(EUR(0))")
        self.assertEqual(apply_subformula(10.0, sub), 10.0)
        self.assertEqual(apply_subformula(-10.0, sub), 0.0)
        self.assertEqual(apply_subformula(0.0, sub), 0.0)

    def test_if_below(self):
        sub = parse_subformula("if_below(EUR(0))")
        self.assertEqual(apply_subformula(-10.0, sub), -10.0)
        self.assertEqual(apply_subformula(10.0, sub), 0.0)

    def test_if_between(self):
        sub = parse_subformula("if_between(EUR(0), EUR(100))")
        self.assertEqual(apply_subformula(50.0, sub), 50.0)
        self.assertEqual(apply_subformula(150.0, sub), 0.0)

    def test_if_other_expr_above(self):
        sub = parse_subformula("if_other_expr_above(box_A1.balance_rounded, EUR(0))")
        self.assertEqual(sub.other_ref, ("box_A1", "balance_rounded"))
        self.assertEqual(sub.extra_refs, (("box_A1", "balance_rounded"),))
        values = {("box_A1", "balance_rounded"): 5.0}
        self.assertEqual(apply_subformula(42.0, sub, values), 42.0)
        values[("box_A1", "balance_rounded")] = -5.0
        self.assertEqual(apply_subformula(42.0, sub, values), 0.0)

    def test_external_options(self):
        sub = parse_subformula("editable;rounding=0", engine="external")
        self.assertTrue(sub.editable)
        self.assertEqual(sub.rounding, 0)

    def test_external_aggregator(self):
        sub = parse_subformula("most_recent", engine="external")
        self.assertEqual(sub.aggregator, "most_recent")

    def test_domain_subformula(self):
        self.assertEqual(parse_subformula("-sum", engine="domain").domain_aggregator, "-sum")
        with self.assertRaises(FormulaError):
            parse_subformula("nope", engine="domain")

    def test_cross_report(self):
        sub = parse_subformula("cross_report(l10n_fr_account.tax_report)")
        self.assertEqual(sub.report_ref, "l10n_fr_account.tax_report")

    def test_unknown_subformula_rejected(self):
        with self.assertRaises(FormulaError):
            parse_subformula("if_maybe(EUR(0))")

    def test_malformed_bound_rejected(self):
        with self.assertRaises(FormulaError):
            parse_subformula("if_above(0)")


class TestShortcuts(unittest.TestCase):
    """Champs raccourcis de ``account.report.line``."""

    def test_aggregation_shortcut(self):
        self.assertEqual(
            expand_shortcut("aggregation_formula", "box_X4.balance"),
            ("aggregation", "box_X4.balance", None),
        )

    def test_domain_shortcut_splits_subformula(self):
        engine, formula, subformula = expand_shortcut(
            "domain_formula", "sum([('account_id.code', '=like', '6%')])"
        )
        self.assertEqual(engine, "domain")
        self.assertEqual(subformula, "sum")
        self.assertEqual(formula, "[('account_id.code', '=like', '6%')]")

    def test_domain_shortcut_negative_sum(self):
        _, _, subformula = expand_shortcut("domain_formula", "-sum([('id', '>', 0)])")
        self.assertEqual(subformula, "-sum")

    def test_external_shortcut_is_a_figure_type(self):
        self.assertEqual(
            expand_shortcut("external_formula", "percentage"),
            ("external", "most_recent", "editable;rounding=0"),
        )
        self.assertEqual(
            expand_shortcut("external_formula", "monetary"),
            ("external", "sum", "editable"),
        )

    def test_malformed_domain_shortcut_rejected(self):
        with self.assertRaises(ValueError):
            expand_shortcut("domain_formula", "[('id', '>', 0)]")


class TestResolver(unittest.TestCase):
    """Graphe de dépendances et évaluation d'ensemble."""

    @staticmethod
    def _spec(code, label, engine, formula, subformula=None, children=()):
        return ExpressionSpec(code, label, engine, formula, subformula, list(children))

    def test_evaluation_order_and_result(self):
        specs = [
            self._spec("a", "balance", "tax_tags", "+TAG_A"),
            self._spec("b", "balance", "tax_tags", "+TAG_B"),
            self._spec("total", "balance", "aggregation", "a.balance + b.balance"),
        ]
        compiled = compile_expressions(specs)
        values = evaluate_all(
            compiled,
            {("a", "balance"): 100.0, ("b", "balance"): 250.0},
        )
        self.assertAlmostEqual(values[("total", "balance")], 350.0, delta=DELTA)

    def test_chained_aggregations(self):
        specs = [
            self._spec("l1", "balance", "tax_tags", "+T1"),
            self._spec("l2", "balance", "aggregation", "l1.balance * 0.2"),
            self._spec("l3", "balance", "aggregation", "l2.balance + l1.balance"),
        ]
        compiled = compile_expressions(specs)
        values = evaluate_all(compiled, {("l1", "balance"): 1000.0})
        self.assertAlmostEqual(values[("l3", "balance")], 1200.0, delta=DELTA)

    def test_sum_children(self):
        specs = [
            self._spec("c1", "balance", "tax_tags", "+A"),
            self._spec("c2", "balance", "tax_tags", "+B"),
            self._spec("parent", "balance", "aggregation", "sum_children",
                       children=("c1", "c2")),
        ]
        compiled = compile_expressions(specs)
        values = evaluate_all(
            compiled, {("c1", "balance"): 30.0, ("c2", "balance"): 12.0}
        )
        self.assertAlmostEqual(values[("parent", "balance")], 42.0, delta=DELTA)

    def test_missing_leaf_defaults_to_zero(self):
        """Une ligne sans écriture vaut zéro : c'est un résultat, pas une erreur."""
        specs = [
            self._spec("a", "balance", "tax_tags", "+A"),
            self._spec("t", "balance", "aggregation", "a.balance + 5"),
        ]
        values = evaluate_all(compile_expressions(specs), {})
        self.assertAlmostEqual(values[("t", "balance")], 5.0, delta=DELTA)

    def test_cycle_detected(self):
        specs = [
            self._spec("a", "balance", "aggregation", "b.balance"),
            self._spec("b", "balance", "aggregation", "a.balance"),
        ]
        with self.assertRaises(CyclicDependencyError):
            resolve_order(compile_expressions(specs))

    def test_unknown_reference_raises(self):
        specs = [self._spec("a", "balance", "aggregation", "ghost.balance")]
        with self.assertRaises(FormulaError):
            resolve_order(compile_expressions(specs))

    def test_carryover_does_not_create_a_cycle(self):
        """Le report d'une période antérieure est une entrée, pas une boucle."""
        specs = [
            self._spec("box22", "_applied_carryover_balance", "external", "sum"),
            self._spec("box22", "balance", "aggregation",
                       "box22._applied_carryover_balance + box27.balance"),
            self._spec("box27", "balance", "tax_tags", "+X"),
            self._spec("box27", "_carryover_balance", "aggregation", "box27.balance"),
        ]
        compiled = compile_expressions(specs)
        order = resolve_order(compiled)
        self.assertEqual(len(order), 4)
        values = evaluate_all(
            compiled,
            {("box22", "_applied_carryover_balance"): 500.0, ("box27", "balance"): 120.0},
        )
        self.assertAlmostEqual(values[("box22", "balance")], 620.0, delta=DELTA)

    def test_partial_graph_for_displayed_lines_only(self):
        """Seul le sous-graphe nécessaire aux lignes affichées est calculé."""
        specs = [
            self._spec("a", "balance", "tax_tags", "+A"),
            self._spec("b", "balance", "aggregation", "a.balance"),
            self._spec("unused", "balance", "tax_tags", "+Z"),
        ]
        order = resolve_order(compile_expressions(specs), roots={("b", "balance")})
        self.assertEqual(order, [("a", "balance"), ("b", "balance")])

    def test_subformula_applied_after_aggregation(self):
        specs = [
            self._spec("a", "balance", "tax_tags", "+A"),
            self._spec("b", "balance", "aggregation", "a.balance", subformula="round(0)"),
        ]
        values = evaluate_all(compile_expressions(specs), {("a", "balance"): 1234.56})
        self.assertEqual(values[("b", "balance")], 1235.0)

    def test_guard_uses_post_subformula_value_of_the_other_expression(self):
        """La garde lit la valeur finale de l'autre expression, pas sa valeur brute."""
        specs = [
            self._spec("a", "raw", "tax_tags", "+A"),
            self._spec("a", "rounded", "aggregation", "a.raw", subformula="round(0)"),
            self._spec("a", "balance", "aggregation", "a.rounded",
                       subformula="if_other_expr_above(a.rounded, EUR(0))"),
        ]
        compiled = compile_expressions(specs)
        values = evaluate_all(compiled, {("a", "raw"): -0.4})
        # -0.4 arrondi à 0 -> la garde "> 0" n'est pas satisfaite.
        self.assertEqual(values[("a", "rounded")], 0.0)
        self.assertEqual(values[("a", "balance")], 0.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestAccountSelection(unittest.TestCase):
    """Sélection des comptes du moteur ``account_codes``.

    Les cas ci-dessous reprennent les exemples de la documentation publique
    d'Odoo, qui fait autorité sur la sémantique des suffixes D et C.
    """

    #: Exemple documenté : 210001 solde -42, 210002 solde 25.
    BALANCES = {1: -42.0, 2: 25.0, 3: 300.0}
    INFO = {1: ("210001", set()), 2: ("210002", set()), 3: ("101000", {7})}

    def _sum(self, formula, resolver=None):
        return sum_account_codes(
            parse_account_codes_formula(formula),
            self.BALANCES, self.INFO, tag_resolver=resolver,
        )

    def test_prefix_sums_balances(self):
        self.assertAlmostEqual(self._sum("21"), -17.0, delta=DELTA)

    def test_debit_suffix_filters_on_account_balance_sign(self):
        """``21D`` retient 210002 (solde 25) et écarte 210001 (solde -42)."""
        self.assertAlmostEqual(self._sum("21D"), 25.0, delta=DELTA)

    def test_credit_suffix(self):
        self.assertAlmostEqual(self._sum("21C"), -42.0, delta=DELTA)

    def test_suffix_is_not_a_debit_column_selector(self):
        """Régression : ``D`` ne signifie pas « somme de la colonne débit ».

        Sur un compte de solde débiteur 300, ``101D`` renvoie le solde 300,
        et non le cumul des mouvements au débit.
        """
        self.assertAlmostEqual(self._sum("101D"), 300.0, delta=DELTA)
        self.assertAlmostEqual(self._sum("101C"), 0.0, delta=DELTA)

    def test_empty_exclusion_makes_the_letter_part_of_the_prefix(self):
        """``21D\\()`` cible les comptes commençant par ``21D``, sans filtre de signe."""
        terms = parse_account_codes_formula(r"21D\()")
        self.assertEqual(terms[0].prefix, "21D")
        self.assertEqual(terms[0].balance_character, "")

    def test_exclusion(self):
        self.assertAlmostEqual(self._sum(r"21\(210001)"), 25.0, delta=DELTA)

    def test_signed_terms(self):
        self.assertAlmostEqual(self._sum("21 - 101"), -317.0, delta=DELTA)

    def test_tag_selection(self):
        self.assertAlmostEqual(
            self._sum("tag(my_module.my_tag)", resolver=lambda ref: {7}),
            300.0, delta=DELTA,
        )

    def test_numeric_tag_reference(self):
        self.assertAlmostEqual(
            self._sum("tag(7)", resolver=lambda ref: {int(ref)}),
            300.0, delta=DELTA,
        )

    def test_tag_with_credit_suffix(self):
        self.assertAlmostEqual(
            self._sum("tag(my_module.my_tag)C", resolver=lambda ref: {7}),
            0.0, delta=DELTA,
        )

    def test_tag_combined_with_prefix(self):
        self.assertAlmostEqual(
            self._sum("tag(my_module.my_tag) + 21", resolver=lambda ref: {7}),
            283.0, delta=DELTA,
        )

    def test_tag_without_resolver_raises(self):
        with self.assertRaises(ValueError):
            self._sum("tag(my_module.my_tag)")


class TestAgedSubformula(unittest.TestCase):
    """Tranches d'ancienneté — extension Expodo au moteur ``domain``."""

    def test_parses_bounds(self):
        sub = parse_subformula("aged(31,60)", engine="domain")
        self.assertEqual(sub.kind, "aged")
        self.assertEqual((sub.aged_min, sub.aged_max), (31, 60))

    def test_open_ended_upper_bound(self):
        sub = parse_subformula("aged(121,*)", engine="domain")
        self.assertEqual(sub.aged_min, 121)
        self.assertIsNone(sub.aged_max)

    def test_negative_lower_bound_for_not_yet_due(self):
        sub = parse_subformula("aged(-99999,0)", engine="domain")
        self.assertEqual((sub.aged_min, sub.aged_max), (-99999, 0))

    def test_inverted_range_rejected(self):
        with self.assertRaises(FormulaError):
            parse_subformula("aged(60,31)", engine="domain")

    def test_aggregator_is_a_plain_sum(self):
        """Une tranche n'est pas un agrégat différent : c'est une somme filtrée."""
        self.assertEqual(
            parse_subformula("aged(1,30)", engine="domain").domain_aggregator, "sum"
        )

    def test_value_passes_through_unchanged(self):
        sub = parse_subformula("aged(1,30)", engine="domain")
        self.assertEqual(apply_subformula(1234.56, sub), 1234.56)

    def test_buckets_are_contiguous(self):
        """Régression : les tranches doivent couvrir tous les jours sans trou.

        Avec une borne basse stricte, une échéance tombant exactement à la
        date de référence n'appartenait à aucune tranche — « non échu »
        exigeant une échéance postérieure et « 1 à 30 jours » une échéance
        antérieure d'au moins un jour. Le cas se produit pour toute facture
        échue le jour même, donc constamment.
        """
        buckets = [
            parse_subformula(s, engine="domain")
            for s in ("aged(-99999,0)", "aged(1,30)", "aged(31,60)",
                      "aged(61,90)", "aged(91,120)", "aged(121,*)")
        ]
        # Chaque jour d'ancienneté de 0 à 400 doit tomber dans exactement une
        # tranche. La borne basse étant inclusive et la haute exclusive côté
        # SQL, on reproduit ici la même règle d'appartenance.
        for days in range(0, 401):
            matching = [
                b for b in buckets
                if days >= b.aged_min and (b.aged_max is None or days <= b.aged_max)
            ]
            self.assertEqual(
                len(matching), 1,
                "ancienneté de %d jours : %d tranches correspondantes"
                % (days, len(matching)),
            )


class TestRounding(unittest.TestCase):
    """Arrondi entier — `account.report.integer_rounding`.

    `round()` de Python applique l'arrondi **bancaire** : le demi va vers le
    pair. `round(436.5)` vaut 436, `round(437.5)` vaut 438. L'administration
    fiscale française impose l'arrondi au plus proche, demi vers le haut.

    Le défaut est invisible sur la plupart des jeux de données et n'apparaît
    que lorsqu'une case tombe exactement sur un demi-euro — ce qui arrive dès
    qu'un taux de 5,5 % s'applique à un montant rond. Il sous-déclare alors
    d'un euro, sur un formulaire transmis au fisc.
    """

    def test_half_goes_up_not_to_even(self):
        for value, expected in ((436.5, 437.0), (437.5, 438.0),
                                (0.5, 1.0), (1.5, 2.0), (2.5, 3.0)):
            with self.subTest(value=value):
                self.assertEqual(round_value(value, 0), expected)

    def test_python_round_would_be_wrong(self):
        """Constate explicitement l'écart avec le comportement natif."""
        self.assertEqual(round(436.5), 436)      # arrondi bancaire
        self.assertEqual(round_value(436.5, 0), 437.0)  # arrondi réglementaire

    def test_negative_half_goes_away_from_zero(self):
        self.assertEqual(round_value(-436.5, 0), -437.0)

    def test_up_and_down_modes(self):
        self.assertEqual(round_value(436.2, 0, "UP"), 437.0)
        self.assertEqual(round_value(436.8, 0, "DOWN"), 436.0)

    def test_decimal_places(self):
        self.assertAlmostEqual(round_value(1.2345, 2), 1.23, delta=DELTA)
        self.assertAlmostEqual(round_value(1.2355, 2), 1.24, delta=DELTA)

    def test_subformula_uses_the_report_rounding(self):
        sub = parse_subformula("round(0)")
        self.assertEqual(apply_subformula(436.5, sub, rounding="HALF-UP"), 437.0)
        self.assertEqual(apply_subformula(436.5, sub, rounding="DOWN"), 436.0)


class TestBornesSurValeurExterne(unittest.TestCase):
    """Une borne peut s'attacher à une valeur saisie manuellement.

    Régression. La déclaration espagnole Mod 130 écrit sa case 18
    `editable;rounding=2;if_above(EUR(0))` : le montant saisi ne compte que
    s'il est positif. La grammaire connaissait `if_above` comme sous-formule
    autonome mais pas comme option d'une chaîne `external`, et la déclaration
    entière refusait de s'afficher.

    Le défaut n'était pas dans la borne ni dans le moteur external pris
    séparément — chacun fonctionnait. Il était dans leur combinaison, qui
    n'avait jamais été exercée parce qu'aucune localisation testée ne
    l'employait. Les deux premières localisations éprouvées, française et
    allemande, ne s'en servent pas.
    """

    def test_chaine_external_avec_borne_superieure(self):
        sub = parse_subformula("editable;rounding=2;if_above(EUR(0))",
                               engine="external")
        self.assertEqual(sub.kind, "external")
        self.assertTrue(sub.editable)
        self.assertEqual(sub.rounding, 2)
        self.assertEqual(sub.bound_kind, "if_above")
        self.assertAlmostEqual(sub.bounds[0].amount, 0.0, delta=DELTA)

    def test_la_borne_superieure_filtre_les_valeurs(self):
        sub = parse_subformula("editable;if_above(EUR(0))", engine="external")
        self.assertAlmostEqual(apply_subformula(120.0, sub), 120.0, delta=DELTA)
        self.assertAlmostEqual(apply_subformula(-40.0, sub), 0.0, delta=DELTA)
        self.assertAlmostEqual(apply_subformula(0.0, sub), 0.0, delta=DELTA)

    def test_la_borne_inferieure_filtre_en_sens_inverse(self):
        sub = parse_subformula("editable;if_below(EUR(0))", engine="external")
        self.assertAlmostEqual(apply_subformula(-40.0, sub), -40.0, delta=DELTA)
        self.assertAlmostEqual(apply_subformula(120.0, sub), 0.0, delta=DELTA)

    def test_une_chaine_external_sans_borne_reste_intacte(self):
        sub = parse_subformula("editable;rounding=2", engine="external")
        self.assertIsNone(sub.bound_kind)
        self.assertAlmostEqual(apply_subformula(-40.0, sub), -40.0, delta=DELTA)

    def test_une_option_external_inconnue_reste_refusee(self):
        """Étendre la grammaire ne doit pas la rendre permissive.

        Accepter silencieusement une option inconnue ferait afficher un
        montant faux là où l'on veut un refus net.
        """
        with self.assertRaises(FormulaError):
            parse_subformula("editable;quelque_chose_dinconnu", engine="external")
