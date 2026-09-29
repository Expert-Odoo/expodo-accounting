# -*- coding: utf-8 -*-
# Copyright 2026 Expodo (https://expodo.fr)
# License LGPL-3
"""Tests d'intégration, exécutés dans Odoo contre une vraie base.

Complètent `test_engine.py`, qui teste la grammaire des formules sans base de
données. Ici on vérifie ce que seul un environnement réel peut révéler :
requêtes SQL, droits d'accès, sérialisation vers le navigateur, identités
comptables.

Plusieurs cas sont des **régressions** : ils correspondent à des défauts
réellement rencontrés, dont aucun n'était détectable en test unitaire.
"""

import json
from datetime import date

from odoo import Command, fields
from odoo.exceptions import AccessError, ValidationError, UserError
from odoo.tests import TransactionCase, tagged


@tagged("post_install", "-at_install")
class TestExpodoReports(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env.company
        cls.customer = cls.env["res.partner"].create({"name": "Client de test"})
        cls.supplier = cls.env["res.partner"].create({"name": "Fournisseur de test"})

        cls.sale_tax = cls.env["account.tax"].search([
            ("type_tax_use", "=", "sale"),
            ("amount", "=", 20.0),
            ("tax_exigibility", "=", "on_invoice"),
        ], limit=1)
        cls.purchase_tax = cls.env["account.tax"].search([
            ("type_tax_use", "=", "purchase"),
            ("amount", "=", 20.0),
            ("tax_exigibility", "=", "on_invoice"),
        ], limit=1)

        cls.period = {
            "mode": "range", "filter": "fiscalyear",
            "date_from": date(2026, 1, 1), "date_to": date(2026, 12, 31),
        }

    # ------------------------------------------------------------------
    # Utilitaires
    # ------------------------------------------------------------------

    def _invoice(self, move_type, partner, amount, tax, ref, day="2026-03-15"):
        move = self.env["account.move"].create({
            "move_type": move_type,
            "partner_id": partner.id,
            "invoice_date": day,
            "date": day,
            "ref": ref,
            "invoice_line_ids": [Command.create({
                "name": ref,
                "quantity": 1,
                "price_unit": amount,
                "tax_ids": [Command.set(tax.ids)] if tax else [Command.clear()],
            })],
        })
        move.action_post()
        return move

    def _values(self, xmlid, period=None):
        report = self.env.ref(xmlid, raise_if_not_found=False)
        if not report:
            # Les états français vivent dans un module distinct : le coeur ne
            # dépend d'aucune localisation, et sa suite ne doit pas échouer
            # lorsqu'il est installé seul.
            self.skipTest("%s absent (module de localisation non installé)" % xmlid)
        options = report._expodo_get_options({"date": period or self.period})
        return report, options, report._expodo_compute_values(options, "main")

    def _count_queries(self, callback):
        """Nombre de requêtes SQL émises par `callback`."""
        self.env.invalidate_all()
        counter = [0]
        cursor_class = type(self.env.cr)
        original = cursor_class.execute

        def counting(cursor, *args, **kwargs):
            counter[0] += 1
            return original(cursor, *args, **kwargs)

        cursor_class.execute = counting
        try:
            callback()
        finally:
            cursor_class.execute = original
        return counter[0]

    # ------------------------------------------------------------------
    # Identités comptables (CDC §8)
    # ------------------------------------------------------------------

    def test_trial_balance_debit_equals_credit(self):
        """Total débit = total crédit sur la balance générale."""
        self._invoice("out_invoice", self.customer, 800.0, self.sale_tax, "V4")
        report, options, _ = self._values("expodo_account_reports.report_balance_fr")
        rows = report.line_ids[0]._expodo_expand(report, options, "main")
        debit = sum(row["values"].get("debit", 0.0) for row in rows)
        credit = sum(row["values"].get("credit", 0.0) for row in rows)
        self.assertAlmostEqual(debit, credit, places=2)
        self.assertGreater(debit, 0.0, "Aucune écriture agrégée : test sans valeur")

    # ------------------------------------------------------------------
    # Moteurs
    # ------------------------------------------------------------------

    def test_tax_tags_sign(self):
        """T07 — Le signe vient du préfixe `-` de la formule.

        Une vente porte un solde créditeur, donc négatif en base ; la CA3
        l'attend positif. 35 des 99 expressions `tax_tags` de la CA3 française
        sont concernées.
        """
        self._invoice("out_invoice", self.customer, 1000.0, self.sale_tax, "V5")
        report = self.env.ref("l10n_fr_account.tax_report", raise_if_not_found=False)
        if not report:
            self.skipTest("l10n_fr_account absent")
        _, _, values = self._values("l10n_fr_account.tax_report")
        self.assertGreater(
            values[("box_A1", "balance")], 0.0,
            "La base A1 doit être positive malgré un solde créditeur en base",
        )

    def test_aged_buckets_sum_to_total(self):
        """T30 — La somme des tranches d'ancienneté égale le total.

        Régression : avec une borne basse stricte, une échéance tombant
        exactement à la date de référence n'appartenait à aucune tranche.
        """
        move = self._invoice("out_invoice", self.customer, 1000.0, None, "V7")
        term = move.line_ids.filtered(lambda l: l.display_type == "payment_term")
        term.date_maturity = date(2026, 12, 31)

        report = self.env.ref("expodo_account_reports.report_aged_receivable_fr")
        options = report._expodo_get_options({"date": self.period})
        rows = report.line_ids[0]._expodo_expand(report, options, "main")
        self.assertTrue(rows, "Aucune créance : test sans valeur")
        for row in rows:
            buckets = sum(row["values"].get("t%d" % i, 0.0) for i in range(6))
            self.assertAlmostEqual(
                buckets, row["values"].get("balance", 0.0), places=2,
                msg="Tranches %.2f, total %.2f pour %s" % (
                    buckets, row["values"].get("balance", 0.0), row["name"]),
            )

    # ------------------------------------------------------------------
    # Performance (CDC T-07b)
    # ------------------------------------------------------------------

    def test_query_count_independent_of_report_size(self):
        """T31 — Le nombre de requêtes ne suit pas la taille du rapport.

        Régression : `_get_matching_tags()` appelée expression par expression
        produisait 109 requêtes au lieu de 12 sur la CA3 ; la résolution des
        libellés groupe par groupe en produisait 58 au lieu de 8 sur le grand
        livre. Dans les deux cas le rapport était juste, seulement inutilisable
        sur une base volumineuse.
        """
        self._invoice("out_invoice", self.customer, 1000.0, self.sale_tax, "V8")

        small, _, _ = self._values("expodo_account_reports.report_livre_caisse_fr")
        small_options = small._expodo_get_options({"date": self.period})
        small_count = self._count_queries(
            lambda: small._expodo_compute_values(small_options, "main")
        )

        big = self.env.ref("l10n_fr_account.tax_report", raise_if_not_found=False)
        if not big:
            self.skipTest("l10n_fr_account absent")
        big_options = big._expodo_get_options({"date": self.period})
        big._expodo_compute_values(big_options, "main")
        big_count = self._count_queries(
            lambda: big._expodo_compute_values(big_options, "main")
        )

        expressions = sum(len(line.expression_ids) for line in big.line_ids)
        self.assertGreater(expressions, 100, "Rapport de référence trop petit")
        self.assertLess(
            big_count, small_count * 4,
            "Le rapport à %d expressions coûte %d requêtes contre %d pour un "
            "rapport à une ligne : le coût suit la taille."
            % (expressions, big_count, small_count),
        )

    # ------------------------------------------------------------------
    # Interface et droits (CDC F-40)
    # ------------------------------------------------------------------

    def test_report_data_is_json_serializable(self):
        """Régression : le formatage monétaire renvoyait du `Markup` HTML.

        Intransmissible dans une réponse JSON, donc l'interface ne pouvait
        pas se charger.
        """
        import json
        report, _, _ = self._values("expodo_account_reports.report_balance_fr")
        data = report.expodo_get_report_data({"date": self.period})
        json.dumps(data)  # lève si une valeur n'est pas sérialisable

    def test_entry_points_are_public(self):
        """Régression : Odoo refuse d'appeler par RPC une méthode en `_`.

        Les quatre points d'entrée de l'interface doivent donc être publics.
        """
        report = self.env["account.report"]
        for method in ("expodo_get_report_data", "expodo_expand_line",
                       "expodo_action_audit", "expodo_action_export"):
            self.assertTrue(
                hasattr(report, method),
                "%s doit être publique pour être appelable depuis le navigateur"
                % method,
            )

    def test_accounting_manager_has_access(self):
        """Régression : le contrôle d'accès rejetait l'administrateur comptable.

        En v19 la hiérarchie n'est pas linéaire : `group_account_manager`
        implique « Facturation » mais pas « Lecture seule ». Ne tester que
        `group_account_readonly` excluait la personne même censée lire les
        rapports.
        """
        manager = self.env["res.users"].create({
            "name": "Comptable", "login": "comptable_test",
            "group_ids": [Command.link(
                self.env.ref("account.group_account_manager").id
            )],
        })
        report = self.env.ref("expodo_account_reports.report_balance_fr")
        report.with_user(manager).expodo_get_report_data({"date": self.period})

    def test_user_without_accounting_rights_is_denied(self):
        """Un utilisateur sans droit comptable ne doit pas lire le bilan."""
        outsider = self.env["res.users"].create({
            "name": "Sans droits", "login": "sans_droits_test",
            "group_ids": [Command.link(self.env.ref("base.group_user").id)],
        })
        # Un rapport du coeur : le bilan appartient au pack français, qui peut
        # ne pas être installé.
        report = self.env.ref("expodo_account_reports.report_balance_fr")
        with self.assertRaises(AccessError):
            report.with_user(outsider).expodo_get_report_data({"date": self.period})

    def test_audit_domain_matches_displayed_value(self):
        """T19 — La somme des écritures ouvertes égale le montant cliqué.

        C'est la seule garantie que le drill-down soit honnête : un domaine
        approché produirait une liste plausible mais sans rapport avec le
        chiffre affiché.
        """
        self._invoice("out_invoice", self.customer, 1200.0, None, "V9")
        report, options, _ = self._values("expodo_account_reports.report_balance_fr")
        serialized = report._expodo_serialize_options(options)
        rows = report.line_ids[0]._expodo_expand(report, options, "main")
        self.assertTrue(rows, "Aucun compte mouvementé : test sans valeur")

        target = rows[0]
        # `_expodo_expand` expose `group_field`/`group_id` ; la clé `group`
        # n'est construite que par la sérialisation destinée au navigateur.
        group = {"field": target["group_field"], "id": target["group_id"]}
        action = report.expodo_action_audit(
            report.line_ids[0].id, serialized, group=group
        )
        lines = self.env["account.move.line"].search(action["domain"])
        self.assertAlmostEqual(
            sum(lines.mapped("balance")), target["values"]["balance"], places=2,
            msg="L'audit de « %s » n'ouvre pas les écritures de son montant"
                % target["name"],
        )

    # ------------------------------------------------------------------
    # Exports (CDC F-30 à F-32)
    # ------------------------------------------------------------------

    def test_xlsx_amounts_are_numeric(self):
        """T22 — Les montants doivent être des nombres, pas du texte.

        Un export dont les chiffres arrivent en chaînes est inutilisable pour
        la révision comptable, qui consiste précisément à les recalculer.
        """
        import io
        import re
        import zipfile

        self._invoice("out_invoice", self.customer, 900.0, self.sale_tax, "V10")
        report, options, _ = self._values("expodo_account_reports.report_balance_fr")
        blob = report._expodo_export_xlsx(report._expodo_serialize_options(options))

        archive = zipfile.ZipFile(io.BytesIO(blob))
        sheet = archive.read("xl/worksheets/sheet1.xml").decode()
        numeric = re.findall(r'<c r="[B-Z]\d+"[^>]*><v>([-0-9.]+)</v>', sheet)
        self.assertGreater(
            len(numeric), 5,
            "Moins de 6 cellules numériques : les montants sont probablement "
            "écrits en texte",
        )

    def test_pdf_is_generated(self):
        """T23 — Le PDF doit être un document valide et non vide."""
        self._invoice("out_invoice", self.customer, 700.0, None, "V11")
        report, options, _ = self._values("expodo_account_reports.report_bilan_fr")
        pdf = report._expodo_export_pdf(report._expodo_serialize_options(options))
        self.assertTrue(pdf.startswith(b"%PDF-"), "En-tête PDF absent")
        self.assertGreater(len(pdf), 1000, "PDF suspicieusement court")

    # ------------------------------------------------------------------
    # Aller-retour des options (régressions d'usage)
    # ------------------------------------------------------------------

    def test_options_survive_a_round_trip(self):
        """Régression : changer un filtre cassait le rapport.

        Les options qui reviennent du navigateur portent des dates en chaînes,
        ayant fait l'aller-retour par JSON. Les réutiliser sans les reconvertir
        levait `'str' object has no attribute 'isoformat'` au premier clic sur
        n'importe quel filtre.

        Aucun test ne l'atteignait : ils passaient tous des objets `date`.
        """
        report = self.env.ref("expodo_account_reports.report_balance_fr")
        first = report.expodo_get_report_data({"date": self.period})
        # Ce que le navigateur renvoie : exactement ce qu'il a reçu.
        second = report.expodo_get_report_data(first["options"])
        self.assertEqual(
            first["options"]["date"]["date_from"],
            second["options"]["date"]["date_from"],
        )

    def test_draft_filter_changes_the_result(self):
        """Le filtre « inclure les brouillons » doit modifier les montants.

        Mesuré en **écart** et non en valeur absolue : la base de
        développement peut déjà contenir des brouillons validés par ailleurs,
        et un test qui postule une base vierge échoue pour une raison qui n'a
        rien à voir avec ce qu'il vérifie.
        """
        report = self.env.ref("expodo_account_reports.report_balance_fr")

        def total(all_entries):
            options = report._expodo_get_options({
                "date": self.period, "all_entries": all_entries,
            })
            rows = report.line_ids[0]._expodo_expand(report, options, "main")
            return sum(row["values"].get("debit", 0.0) for row in rows)

        before = total(True)
        draft = self.env["account.move"].create({
            "move_type": "out_invoice",
            "partner_id": self.customer.id,
            "invoice_date": "2026-04-10",
            "date": "2026-04-10",
            "invoice_line_ids": [Command.create({
                "name": "Vente brouillon", "quantity": 1, "price_unit": 5000.0,
            })],
        })
        self.assertEqual(draft.state, "draft")

        self.assertAlmostEqual(
            total(True) - before, 5000.0, places=2,
            msg="Le nouveau brouillon doit s'ajouter au total avec le filtre",
        )
        self.assertNotAlmostEqual(
            total(True), total(False), places=2,
            msg="Le filtre brouillons doit changer le résultat",
        )

    def test_comparison_produces_a_second_column_group(self):
        """La comparaison doit ajouter un groupe de colonnes daté."""
        report = self.env.ref("expodo_account_reports.report_balance_fr")
        data = report.expodo_get_report_data({
            "date": self.period,
            "comparison": {"filter": "same_last_year", "number_period": 1},
        })
        self.assertEqual(
            len(data["column_groups"]), 2,
            "La comparaison avec l'exercice précédent doit produire 2 groupes",
        )
        groups = list(data["options"]["column_groups"].values())
        self.assertNotEqual(
            groups[0]["date"]["date_from"], groups[1]["date"]["date_from"],
            "Les deux groupes doivent couvrir des périodes différentes",
        )

    def test_empty_period_yields_zero(self):
        """Une période sans écriture ne doit produire aucun groupe."""
        report = self.env.ref("expodo_account_reports.report_balance_fr")
        options = report._expodo_get_options({"date": {
            "mode": "range", "filter": "custom",
            "date_from": date(2024, 2, 1), "date_to": date(2024, 2, 29),
        }})
        rows = report.line_ids[0]._expodo_expand(report, options, "main")
        self.assertFalse(rows, "Aucune écriture attendue sur cette période")

    def test_invalid_date_is_rejected_clearly(self):
        """Régression : une date invalide explosait loin de son origine.

        La saisie au clavier dans un champ `type="date"` émet des états
        intermédiaires — « 102202-01-01 ». Laissés passer, ils remontaient
        jusqu'au formatage sous la forme « 'str' object has no attribute
        'isoformat' », message inexploitable pour l'utilisateur comme pour le
        développeur.
        """
        report = self.env.ref("expodo_account_reports.report_balance_fr")
        with self.assertRaises(ValidationError):
            report._expodo_get_options({"date": {
                "mode": "range", "filter": "custom",
                "date_from": "102202-01-01", "date_to": "2026-12-31",
            }})

    def test_missing_date_falls_back_to_default_period(self):
        """Une borne absente ne doit pas propager un `None`."""
        report = self.env.ref("expodo_account_reports.report_balance_fr")
        options = report._expodo_get_options({"date": {
            "mode": "range", "filter": "custom",
            "date_from": None, "date_to": None,
        }})
        self.assertIsNotNone(options["date"]["date_from"])
        self.assertIsNotNone(options["date"]["date_to"])

    def test_inverted_period_is_rejected(self):
        """Une période à l'envers doit lever, pas produire un rapport vide."""
        report = self.env.ref("expodo_account_reports.report_balance_fr")
        with self.assertRaises(ValidationError):
            report._expodo_get_options({"date": {
                "mode": "range", "filter": "custom",
                "date_from": date(2026, 12, 31), "date_to": date(2026, 1, 1),
            }})

    def test_cash_flow_reconciles_with_cash_accounts(self):
        """Le tableau de flux doit se recomposer exactement.

        La partie double impose que la somme des mouvements de tous les comptes
        vaille zéro sur une période. La variation de trésorerie égale donc
        l'opposé des mouvements de tous les autres comptes, et la somme des
        rubriques du tableau égale le mouvement réel des comptes de trésorerie
        **par construction**.

        L'écart ne peut donc être non nul que si une catégorie de comptes a été
        oubliée dans le détail du rapport. C'est un test de complétude, pas
        d'arithmétique — et c'est précisément l'erreur qu'un tableau de flux
        écrit à la main commet.
        """
        self._invoice("out_invoice", self.customer, 2200.0, self.sale_tax, "CF1")
        self._invoice("in_invoice", self.supplier, 700.0, self.purchase_tax, "CF2")

        report = self.env.ref("expodo_account_reports.report_cash_flow")
        options = report._expodo_get_options({"date": self.period})
        values = report._expodo_compute_values(options, "main")

        self.assertAlmostEqual(
            values[("CF_CHECK", "balance")], 0.0, places=2,
            msg="Flux recomposé %.2f, mouvement réel %.2f — une catégorie de "
                "comptes manque dans le rapport" % (
                    values.get(("CF_NET_CHANGE", "balance"), 0.0),
                    values.get(("CF_ACTUAL", "balance"), 0.0)),
        )

    def test_cash_flow_covers_every_account_type(self):
        """Toutes les catégories de comptes doivent figurer dans le tableau.

        Contrôle statique complétant le précédent : il échoue même sur une base
        où la catégorie oubliée n'a aucun mouvement, donc où l'écart serait nul
        par accident.
        """
        import ast

        report = self.env.ref("expodo_account_reports.report_cash_flow")
        couverts = set()
        for line in report.line_ids:
            if line.code in ("CF_ACTUAL", "CF_OPENING", "CF_CLOSING"):
                continue
            for expression in line.expression_ids:
                if expression.engine != "domain":
                    continue
                for condition in ast.literal_eval(expression.formula):
                    if isinstance(condition, (list, tuple)) and \
                            condition[0] == "account_id.account_type":
                        valeurs = condition[2]
                        couverts |= set(
                            valeurs if isinstance(valeurs, (list, tuple)) else [valeurs]
                        )

        tous = {code for code, _ in
                self.env["account.account"]._fields["account_type"].selection}
        manquants = tous - couverts - {"asset_cash"}
        self.assertFalse(
            manquants,
            "Catégories de comptes absentes du tableau de flux : %s"
            % ", ".join(sorted(manquants)),
        )

    def test_day_book_is_chronological_and_balanced(self):
        """Le livre-journal doit être chronologique et équilibré par journée.

        Le rapport « Journaux » présente les écritures par journal auxiliaire.
        Le livre-journal au sens légal est différent : chronologique, tous
        journaux confondus. C'est sa forme qui est vérifiée en cas de contrôle.

        Chaque journée doit s'équilibrer, la partie double s'appliquant à
        chaque écriture donc à chaque date.
        """
        self._invoice("out_invoice", self.customer, 900.0, self.sale_tax, "DB1",
                      day="2026-06-10")
        self._invoice("in_invoice", self.supplier, 300.0, self.purchase_tax, "DB2",
                      day="2026-06-04")

        report = self.env.ref("expodo_account_reports.report_day_book")
        options = report._expodo_get_options({"date": self.period})
        rows = report.line_ids[0]._expodo_expand(report, options, "main")
        self.assertTrue(rows, "Aucune journée : test sans valeur")

        # L'ordre se contrôle sur la clé de groupement, jamais sur le
        # libellé : celui-ci est formaté pour le lecteur, et « 15/03 » passe
        # après « 12/10 » dans l'ordre alphabétique. Le test regardait le
        # libellé, il ne tenait que parce que les dates sortaient alors au
        # format de stockage.
        dates = [str(row["group_id"]) for row in rows]
        self.assertEqual(
            dates, sorted(dates),
            "Le livre-journal doit être présenté dans l'ordre chronologique",
        )
        for row in rows:
            self.assertAlmostEqual(
                row["values"].get("debit", 0.0), row["values"].get("credit", 0.0),
                places=2,
                msg="La journée %s ne s'équilibre pas" % row["name"],
            )

    def test_opening_period_follows_the_report_declaration(self):
        """Régression : `default_opening_date_filter` était ignoré.

        Le livre-journal, les journaux, les livres de banque et de caisse, la
        balance générale et le grand livre déclarent le mois en cours. Ouverts
        sur l'exercice entier, ils produisent des dizaines de milliers de
        lignes sur une base réelle, là où l'utilisateur en attend quelques
        centaines. C'est aussi la période sur laquelle l'édition de référence
        les ouvre.

        La méthode était par ailleurs `@api.model`, donc sans accès à
        l'enregistrement dont elle devait lire le champ.
        """
        attendus = {
            "expodo_account_reports.report_day_book": "month",
            "expodo_account_reports.report_journaux_fr": "month",
            "expodo_account_reports.report_balance_fr": "month",
            "expodo_account_reports.report_grand_livre_fr": "month",
            "expodo_account_reports.report_livre_banque_fr": "month",
            "expodo_account_reports.report_livre_caisse_fr": "month",
            "expodo_account_reports.report_profit_loss": "fiscalyear",
        }
        for xmlid, duree in attendus.items():
            report = self.env.ref(xmlid, raise_if_not_found=False)
            if not report:
                continue
            with self.subTest(rapport=report.name):
                options = report._expodo_get_options()
                debut = options["date"]["date_from"]
                fin = options["date"]["date_to"]
                jours = (fin - debut).days
                if duree == "month":
                    self.assertLess(
                        jours, 32,
                        "%s déclare le mois mais s'ouvre sur %d jours"
                        % (report.name, jours))
                else:
                    self.assertGreater(
                        jours, 300,
                        "%s déclare l'exercice mais s'ouvre sur %d jours"
                        % (report.name, jours))

    def test_an_irregular_fiscal_year_is_followed_everywhere(self):
        """Un exercice qui ne finit pas le 31 décembre doit être suivi.

        Tout ce qui lit une période part de l'exercice de la société :
        l'ouverture par défaut d'un compte de résultat, la borne d'ouverture
        que reçoit un état arrêté à une date, l'amplitude d'une colonne
        comparée. Sur un exercice civil, une erreur d'exercice se confond
        avec l'année et ne se voit pas. Un exercice clos au 30 juin la rend
        visible, et c'est un cas courant : commerce, agriculture, écoles.
        """
        societe = self.env.company
        societe.fiscalyear_last_day = 30
        societe.fiscalyear_last_month = "6"

        bilan = self.env.ref("expodo_account_reports.report_bilan_fr")
        resultat = self.env.ref("expodo_account_reports.report_resultat_fr")

        # Un état arrêté à une date reçoit l'ouverture de **son** exercice.
        options = bilan._expodo_get_options()
        debut = societe.compute_fiscalyear_dates(
            options["date"]["date_to"])["date_from"]
        self.assertEqual(options["date"]["date_from"], debut)
        self.assertEqual(debut.month, 7, "L'exercice doit s'ouvrir en juillet")

        # Le compte de résultat s'ouvre sur l'exercice, pas sur l'année civile.
        periode = resultat._expodo_get_options()["date"]
        self.assertEqual(periode["date_from"].month, 7)
        self.assertEqual(periode["date_to"].month, 6)

        # La colonne comparée recule d'un exercice entier.
        compare = bilan._expodo_get_options({
            "comparison": {"filter": "previous_period", "number_period": 1},
        })
        groupes = list(compare["column_groups"].values())
        self.assertEqual(len(groupes), 2)
        principale, comparee = groupes[0]["date"], groupes[1]["date"]
        self.assertEqual(
            comparee["date_to"].year, principale["date_to"].year - 1,
            "La comparaison doit reculer d'un exercice, pas d'un jour")

    def test_a_statement_of_position_offers_only_its_closing_date(self):
        """Un état arrêté à une date n'affiche pas de date de début.

        Le bilan, les balances âgées, les écritures ouvertes et le relevé
        client lisent tous depuis l'origine : leurs expressions ignorent
        `date_from`. Ils l'affichaient pourtant, et on pouvait la déplacer
        d'un exercice entier sans qu'un seul chiffre bouge. Un filtre qui ne
        filtre rien fait douter de tout le reste de l'état.
        """
        arretes = (
            "expodo_account_reports.report_balance_sheet",
            "expodo_account_reports.report_bilan_fr",
            "expodo_account_reports.report_aged_receivable_fr",
            "expodo_account_reports.report_aged_payable_fr",
            "expodo_account_reports.report_open_items_fr",
            "expodo_account_reports.report_customer_statement",
        )
        for xmlid in arretes:
            report = self.env.ref(xmlid, raise_if_not_found=False)
            if not report:
                continue
            with self.subTest(rapport=report.name):
                self.assertFalse(
                    report.filter_date_range,
                    "%s affiche une date de début qu'il n'utilise pas"
                    % report.name)

    def test_the_closing_date_of_such_a_statement_can_move_back_a_year(self):
        """Reculer la clôture ne doit pas buter sur l'ouverture.

        L'interface de ces états ne présente qu'une borne. Si l'ouverture
        restait figée sur l'exercice courant, demander le bilan au 31 décembre
        de l'an dernier plaçait la clôture avant l'ouverture et la demande
        était refusée — l'utilisateur ne pouvait plus consulter aucun
        exercice antérieur.
        """
        report = self.env.ref("expodo_account_reports.report_balance_sheet")
        options = report._expodo_get_options({
            "date": {
                "mode": "range", "filter": "custom",
                "date_from": date(2024, 6, 30), "date_to": date(2024, 6, 30),
            },
        })
        debut = options["date"]["date_from"]
        fin = options["date"]["date_to"]
        self.assertEqual(fin, date(2024, 6, 30))
        self.assertLessEqual(debut, fin)
        # La borne est ramenée au début de l'exercice de la clôture : c'est
        # elle qui donne son amplitude à la colonne de comparaison.
        self.assertEqual(
            debut,
            self.company.compute_fiscalyear_dates(fin)["date_from"])

    def test_comparing_such_a_statement_steps_back_a_whole_year(self):
        """La comparaison recule d'un exercice, pas d'un jour.

        Ces états n'ayant qu'une date, la tentation est de poser l'ouverture
        égale à la clôture. La période précédente ne reculerait alors que
        d'une journée : on comparerait le bilan du 31 décembre à celui du
        30 décembre, ce qui ne dit rien.
        """
        report = self.env.ref("expodo_account_reports.report_balance_sheet")
        options = report._expodo_get_options({
            "date": {
                "mode": "range", "filter": "custom",
                "date_from": date(2026, 12, 31), "date_to": date(2026, 12, 31),
            },
            "comparison": {"filter": "previous_period", "number_period": 1},
        })
        groupes = list(options["column_groups"].values())
        self.assertEqual(len(groupes), 2)
        compare = groupes[1]["date"]
        self.assertEqual(compare["date_to"], date(2025, 12, 31))

    def test_a_cash_book_shows_the_movements_of_the_period_it_displays(self):
        """Les entrées et sorties suivent la période affichée.

        Elles lisaient depuis l'origine, comme le solde : changer les dates ne
        déplaçait aucun chiffre et un livre ouvert sur un mois affichait les
        encaissements de toutes les années. Seul le solde se cumule, parce
        qu'un livre de banque se lit contre un relevé.
        """
        report = self.env.ref(
            "expodo_account_reports.report_livre_banque_fr",
            raise_if_not_found=False)
        journal = self.env["account.journal"].search(
            [("type", "=", "bank"), ("company_id", "=", self.company.id)],
            limit=1)
        if not report or not journal:
            self.skipTest("Livre de banque ou journal de banque absent")

        contrepartie = self.env["account.account"].search(
            [("account_type", "=", "expense"),
             ("company_ids", "in", self.company.id)], limit=1)
        banque = journal.default_account_id
        for jour, montant in ((date(2021, 5, 10), 300.0),
                              (date(2026, 3, 10), 500.0)):
            self.env["account.move"].create({
                "journal_id": journal.id,
                "date": jour,
                "line_ids": [
                    Command.create({"account_id": contrepartie.id,
                                    "debit": montant, "credit": 0.0}),
                    Command.create({"account_id": banque.id,
                                    "debit": 0.0, "credit": montant}),
                ],
            }).action_post()

        def mouvements(debut, fin):
            options = report._expodo_get_options({
                "date": {"mode": "range", "filter": "custom",
                         "date_from": debut, "date_to": fin},
            })
            cle = list(options["column_groups"])[0]
            valeurs = report._expodo_compute_values(options, cle)
            code = report.line_ids[0].code
            return {label: valeurs.get((code, label)) or 0.0
                    for label in ("debit", "credit", "balance")}

        mars = mouvements(date(2026, 3, 1), date(2026, 3, 31))
        tout = mouvements(date(2015, 1, 1), date(2026, 12, 31))
        self.assertAlmostEqual(
            mars["credit"], 500.0, places=2,
            msg="Le mois affiché doit ne porter que ses propres mouvements")
        self.assertAlmostEqual(
            tout["credit"], 800.0, places=2,
            msg="Élargir la période doit ramener les mouvements antérieurs")
        # Le solde, lui, se cumule : il doit valoir la même chose des deux
        # côtés puisqu'il est arrêté à la même date de clôture.
        self.assertAlmostEqual(
            tout["balance"], mouvements(date(2026, 1, 1),
                                        date(2026, 12, 31))["balance"],
            places=2,
            msg="Le solde ne doit pas dépendre de la date d'ouverture")

    def test_a_cash_book_does_not_count_both_sides_of_its_entries(self):
        """Le livre ne retient que les lignes du compte de trésorerie.

        Son domaine ne portait que sur le journal, donc sur les deux côtés de
        chaque écriture : le règlement d'un fournisseur comptait une fois en
        entrée pour la contrepartie et une fois en sortie pour la banque. Les
        deux colonnes affichaient le même total et le solde valait zéro quelle
        que soit la période — un livre de banque qui ne montre jamais de
        solde ne peut pas se rapprocher d'un relevé.
        """
        report = self.env.ref(
            "expodo_account_reports.report_livre_banque_fr",
            raise_if_not_found=False)
        journal = self.env["account.journal"].search(
            [("type", "=", "bank"), ("company_id", "=", self.company.id)],
            limit=1)
        if not report or not journal:
            self.skipTest("Livre de banque ou journal de banque absent")

        contrepartie = self.env["account.account"].search(
            [("account_type", "=", "expense"),
             ("company_ids", "in", self.company.id)], limit=1)
        self.env["account.move"].create({
            "journal_id": journal.id,
            "date": date(2026, 4, 8),
            "line_ids": [
                Command.create({"account_id": contrepartie.id,
                                "debit": 120.0, "credit": 0.0}),
                Command.create({"account_id": journal.default_account_id.id,
                                "debit": 0.0, "credit": 120.0}),
            ],
        }).action_post()

        options = report._expodo_get_options({
            "date": {"mode": "range", "filter": "custom",
                     "date_from": date(2026, 4, 1),
                     "date_to": date(2026, 4, 30)},
        })
        cle = list(options["column_groups"])[0]
        valeurs = report._expodo_compute_values(options, cle)
        code = report.line_ids[0].code
        self.assertAlmostEqual(valeurs.get((code, "debit")) or 0.0, 0.0,
                               places=2)
        self.assertAlmostEqual(valeurs.get((code, "credit")) or 0.0, 120.0,
                               places=2)

    def test_aged_reports_open_on_today(self):
        """Les balances âgées s'arrêtent à aujourd'hui.

        L'ancienneté d'une créance se compte depuis son échéance jusqu'au jour
        où on la lit. Ce test portait auparavant l'exigence inverse — s'arrêter
        au dernier mois clos, au motif que le mois en cours bouge encore — et
        cette exigence était fausse : une facture échue le 15 du mois
        s'affichait comme non échue tant que le mois n'était pas terminé.

        Or c'est exactement la créance qu'il faut relancer : la plus récemment
        échue est aussi la plus facile à recouvrer.
        """
        for xmlid in ("expodo_account_reports.report_aged_receivable_fr",
                      "expodo_account_reports.report_aged_payable_fr"):
            report = self.env.ref(xmlid, raise_if_not_found=False)
            if not report:
                continue
            with self.subTest(rapport=report.name):
                fin = report._expodo_get_options()["date"]["date_to"]
                self.assertEqual(
                    fin, fields.Date.context_today(report),
                    "La balance âgée doit calculer l'ancienneté à ce jour")

    def test_aged_reports_read_from_the_very_start(self):
        """Une créance ouverte l'an dernier reste due aujourd'hui.

        Avec une portée restreinte à la période affichée, la balance âgée ne
        montrait que les pièces émises pendant cette période. Les impayés les
        plus anciens — ceux qu'il faut traiter en premier — en étaient absents.
        """
        for xmlid in ("expodo_account_reports.report_aged_receivable_fr",
                      "expodo_account_reports.report_aged_payable_fr"):
            report = self.env.ref(xmlid, raise_if_not_found=False)
            if not report:
                continue
            for ligne in report.line_ids:
                for expression in ligne.expression_ids:
                    self.assertEqual(
                        expression.date_scope, "from_beginning",
                        "%s / %s doit se lire depuis l'origine"
                        % (report.name, expression.label))

    def test_reports_do_not_leak_across_companies(self):
        """Les options venant du navigateur ne doivent pas ouvrir une autre société.

        `company_ids` fait l'aller-retour par le client : un utilisateur peut
        forger la valeur et demander une société à laquelle il n'a pas droit.
        Le cloisonnement ne repose donc pas sur cette option, mais sur le fait
        que le moteur interroge les écritures par `_search`, qui applique les
        règles d'enregistrement d'Odoo.

        Ce test verrouille cette propriété : remplacer un jour `_search` par du
        SQL direct — pour gagner en vitesse, par exemple — la romprait sans que
        rien ne le signale.
        """
        autre = self.env["res.company"].create({"name": "Société voisine"})
        limite = self.env["res.users"].create({
            "name": "Voisin", "login": "reports_voisin",
            "company_id": autre.id,
            "company_ids": [Command.set([autre.id])],
            "group_ids": [Command.link(
                self.env.ref("account.group_account_manager").id)],
        })
        self._invoice("out_invoice", self.customer, 4321.0, self.sale_tax, "CLOISON")

        report = self.env.ref("expodo_account_reports.report_balance_fr")
        donnees = report.with_user(limite).expodo_get_report_data({
            "date": self.period,
            "company_ids": [self.company.id],   # société forgée
        })
        maximum = 0.0
        for ligne in donnees["lines"]:
            for cellule in ligne["columns"]:
                valeur = cellule["raw"].get("main")
                if isinstance(valeur, (int, float)):
                    maximum = max(maximum, abs(valeur))
        self.assertEqual(
            maximum, 0.0,
            "Un utilisateur d'une autre société ne doit rien lire, même en "
            "désignant explicitement la société dans les options")

    def test_export_contains_every_level_of_detail(self):
        """Régression : l'export s'arrêtait au premier niveau de dépliage.

        Le grand livre exporté contenait ses comptes mais aucune écriture,
        c'est-à-dire une balance. Le fichier s'ouvrait, les totaux étaient
        justes, et il manquait précisément ce qu'on vient y chercher : le
        détail. Le défaut ne se voit qu'en comparant le nombre de lignes du
        fichier à celui de la base.

        Mesuré sur le grand livre, seul rapport à deux niveaux dont le second
        porte l'essentiel de l'information.
        """
        import io
        import re
        import zipfile

        for reference in ("GL1", "GL2", "GL3"):
            self._invoice("out_invoice", self.customer, 500.0, self.sale_tax, reference)

        report = self.env.ref("expodo_account_reports.report_grand_livre_fr")
        options = report._expodo_serialize_options(
            report._expodo_get_options({"date": self.period}))

        _, lignes = report._expodo_export_rows(options)
        niveaux = {ligne.get("level", 0) for ligne in lignes}
        self.assertIn(
            2, niveaux,
            "L'export du grand livre doit descendre jusqu'aux écritures, "
            "pas s'arrêter aux comptes")

        classeur = report._expodo_export_xlsx(options)
        feuille = zipfile.ZipFile(io.BytesIO(classeur)).read(
            "xl/worksheets/sheet1.xml").decode()
        rangs = len(re.findall(r"<row ", feuille))
        self.assertGreaterEqual(
            rangs, len(lignes),
            "Le classeur doit contenir toutes les lignes préparées")

    def test_truncation_row_is_added_and_does_not_break_the_file(self):
        """La ligne d'avertissement doit exister et rester exportable.

        Elle est construite à partir des colonnes du rapport : présumer du nom
        de leurs clés la faisait échouer, et une ligne d'avertissement qui
        casse l'export vaut moins que pas d'avertissement du tout.
        """
        for reference in ("TR1", "TR2", "TR3", "TR4"):
            self._invoice("out_invoice", self.customer, 100.0, self.sale_tax, reference)

        report = self.env.ref("expodo_account_reports.report_grand_livre_fr")
        options = report._expodo_serialize_options(
            report._expodo_get_options({"date": self.period}))

        _, lignes = report._expodo_export_rows(options, limite=3)
        self.assertLessEqual(len(lignes), 4, "La borne doit être respectée")
        self.assertIn(
            "truncated", lignes[-1]["name"],
            "Une ligne doit signaler que l'export est tronqué")
        self.assertTrue(
            lignes[-1]["columns"],
            "La ligne d'avertissement doit porter les colonnes du rapport")

    def test_pdf_is_bounded_lower_than_the_spreadsheet(self):
        """Le PDF doit avoir sa propre borne, bien plus basse.

        Mesuré : 4 000 lignes produisent 400 Ko en 2,3 secondes. La même pente
        donne près d'une minute à la borne du tableur — la requête HTTP
        expirerait avant la fin. Et un PDF de quatre cents pages n'est lu par
        personne.
        """
        report = self.env.ref("expodo_account_reports.report_grand_livre_fr")
        self.assertLess(
            report.EXPODO_EXPORT_MAX_ROWS_PDF,
            report.EXPODO_EXPORT_MAX_ROWS,
            "Le PDF doit être borné plus bas que le tableur")
        self.assertGreaterEqual(
            report.EXPODO_EXPORT_MAX_ROWS_PDF, 5000,
            "Une borne trop basse amputerait des grands livres légitimes")

    def test_export_is_bounded(self):
        """Un export sans borne épuiserait la mémoire du serveur.

        Un grand livre d'un grand compte peut porter des centaines de milliers
        d'écritures. La borne doit exister et être haute : en deçà on ampute
        des exports légitimes, au-delà on produit un fichier qu'aucun tableur
        n'ouvre.
        """
        report = self.env.ref("expodo_account_reports.report_grand_livre_fr")
        self.assertTrue(
            report.EXPODO_EXPORT_MAX_ROWS,
            "Une borne doit être définie")
        self.assertGreaterEqual(
            report.EXPODO_EXPORT_MAX_ROWS, 10000,
            "Une borne basse amputerait des exports légitimes")

    # ------------------------------------------------------------------
    # Filtres et états d'écriture
    # ------------------------------------------------------------------

    def test_journal_filter_partitions_the_total(self):
        """Filtrer par journal doit partitionner exactement le total.

        La somme des journaux pris un par un doit égaler le total sans filtre :
        un écart signalerait des écritures comptées deux fois, ou aucune.
        C'est le contrôle que l'oeil ne fait jamais, parce qu'il faudrait
        additionner cinq écrans.
        """
        self._invoice("out_invoice", self.customer, 1000.0, self.sale_tax, "JF1")
        self._invoice("in_invoice", self.supplier, 400.0, self.purchase_tax, "JF2")

        report = self.env.ref("expodo_account_reports.report_balance_fr")

        def debit_total(options):
            resolues = report._expodo_get_options(options)
            lignes = report.line_ids[0]._expodo_expand(report, resolues, "main")
            return sum(l["values"].get("debit", 0.0) for l in lignes)

        total = debit_total({"date": self.period})
        somme = 0.0
        for journal in self.env["account.journal"].search([]):
            somme += debit_total({"date": self.period, "journal_ids": [journal.id]})

        self.assertAlmostEqual(
            somme, total, places=2,
            msg="La somme des journaux (%.2f) doit égaler le total (%.2f)"
                % (somme, total))

    def test_partner_filter_is_additive(self):
        """Filtrer sur deux partenaires doit donner la somme de chacun."""
        self._invoice("out_invoice", self.customer, 800.0, self.sale_tax, "PF1")
        self._invoice("in_invoice", self.supplier, 300.0, self.purchase_tax, "PF2")

        report = self.env.ref("expodo_account_reports.report_balance_fr")

        def debit_total(partenaires):
            options = report._expodo_get_options({
                "date": self.period, "partner_ids": partenaires})
            lignes = report.line_ids[0]._expodo_expand(report, options, "main")
            return sum(l["values"].get("debit", 0.0) for l in lignes)

        separes = (debit_total([self.customer.id])
                   + debit_total([self.supplier.id]))
        ensemble = debit_total([self.customer.id, self.supplier.id])
        self.assertAlmostEqual(separes, ensemble, places=2)

    def test_cancelled_entries_are_never_included(self):
        """Une écriture annulée ne doit jamais entrer dans un rapport.

        Y compris en mode « brouillons inclus » : ce mode sert à voir ce qui
        n'est pas encore comptabilisé, pas à ressusciter ce qui a été annulé.
        """
        facture = self._invoice(
            "out_invoice", self.customer, 6500.0, self.sale_tax, "ANNUL")
        facture.button_draft()
        facture.button_cancel()

        report = self.env.ref("expodo_account_reports.report_balance_fr")

        def debit_total(brouillons):
            options = report._expodo_get_options({
                "date": self.period, "all_entries": brouillons})
            lignes = report.line_ids[0]._expodo_expand(report, options, "main")
            return sum(l["values"].get("debit", 0.0) for l in lignes)

        self.assertAlmostEqual(
            debit_total(False), debit_total(True), places=2,
            msg="L'écriture annulée ne doit apparaître dans aucun des deux modes")

    def test_foreign_currency_entries_are_reported_in_company_currency(self):
        """Un rapport restitue la monnaie de tenue, jamais la devise d'origine.

        Une écriture de 1 100 USD à un taux de 1,1 vaut 1 000 € : c'est ce
        montant que le rapport doit porter. Restituer la devise d'origine
        produirait un état qui ne s'équilibre plus dès qu'une seconde devise
        apparaît.
        """
        devise = self.env["res.currency"].search([("name", "=", "USD")], limit=1)
        if not devise:
            self.skipTest("Le dollar n'existe pas dans cette base")
        devise.sudo().active = True
        # Le taux est posé s'il n'existe pas : un test ne maîtrise pas ce que
        # la base contient déjà, et une contrainte d'unicité le ferait échouer
        # pour une raison étrangère à ce qu'il vérifie.
        taux = self.env["res.currency.rate"].search([
            ("currency_id", "=", devise.id),
            ("name", "=", date(2026, 4, 1)),
            ("company_id", "=", self.company.id),
        ], limit=1)
        if not taux:
            self.env["res.currency.rate"].create({
                "currency_id": devise.id, "name": date(2026, 4, 1),
                "rate": 1.1, "company_id": self.company.id,
            })

        report = self.env.ref("expodo_account_reports.report_balance_fr")

        def totaux():
            options = report._expodo_get_options({"date": self.period})
            lignes = report.line_ids[0]._expodo_expand(report, options, "main")
            return (sum(l["values"].get("debit", 0.0) for l in lignes),
                    sum(l["values"].get("credit", 0.0) for l in lignes))

        # La classe de test ne porte pas de journal ni de comptes dédiés :
        # on les prend dans le plan comptable de la base, comme le ferait un
        # utilisateur.
        journal = self.env["account.journal"].search([
            ("type", "=", "general"), ("company_id", "=", self.company.id),
        ], limit=1)
        compte_debit = self.env["account.account"].search([
            ("account_type", "=", "asset_current")], limit=1)
        compte_credit = self.env["account.account"].search([
            ("account_type", "=", "income")], limit=1)
        if not (journal and compte_debit and compte_credit):
            self.skipTest("Le plan comptable ne permet pas ce test")

        avant_debit, _ = totaux()
        ecriture = self.env["account.move"].create({
            "journal_id": journal.id,
            "date": date(2026, 4, 1),
            "currency_id": devise.id,
            "line_ids": [
                Command.create({
                    "name": "usd", "account_id": compte_debit.id,
                    "debit": 1000.0, "credit": 0.0,
                    "currency_id": devise.id, "amount_currency": 1100.0}),
                Command.create({
                    "name": "usd", "account_id": compte_credit.id,
                    "debit": 0.0, "credit": 1000.0,
                    "currency_id": devise.id, "amount_currency": -1100.0}),
            ],
        })
        ecriture.action_post()

        apres_debit, apres_credit = totaux()
        self.assertAlmostEqual(
            apres_debit - avant_debit, 1000.0, places=2,
            msg="Le rapport doit porter 1 000 € et non 1 100 USD")
        self.assertAlmostEqual(
            apres_debit, apres_credit, places=2,
            msg="L'équilibre doit tenir en présence d'une devise étrangère")

    def test_audit_reconciles_on_every_report(self):
        """Le drill-down doit concorder sur tous les rapports, pas seulement un.

        Les rapports ajoutés après coup — tableau de flux, livre-journal,
        états universels — n'avaient jamais été audités. Un drill-down
        approché donne une liste plausible dont la somme diffère du montant
        cliqué : c'est pire qu'une absence de drill-down, parce que
        l'utilisateur y croit.
        """
        self._invoice("out_invoice", self.customer, 2500.0, self.sale_tax, "AUD1")
        self._invoice("in_invoice", self.supplier, 900.0, self.purchase_tax, "AUD2")

        rapports = (
            "report_balance_fr", "report_cash_flow", "report_day_book",
            "report_balance_sheet", "report_profit_loss", "report_grand_livre_fr",
        )
        for reference in rapports:
            report = self.env.ref(
                "expodo_account_reports." + reference, raise_if_not_found=False)
            if not report:
                continue
            with self.subTest(rapport=report.name):
                donnees = report.expodo_get_report_data({"date": self.period})
                cible = None
                for ligne in donnees["lines"]:
                    for cellule in ligne["columns"]:
                        valeur = cellule["raw"].get("main")
                        if (cellule.get("auditable")
                                and isinstance(valeur, (int, float))
                                and abs(valeur) > 1):
                            cible = (ligne, cellule)
                            break
                    if cible:
                        break
                if not cible:
                    continue

                ligne, cellule = cible
                # La colonne cliquée est transmise, comme le fait l'écran.
                #
                # Sans elle, le test s'appuyait sur la colonne que la méthode
                # retient par défaut, et vérifiait donc autre chose que la
                # cellule qu'il venait de choisir. Tant que ce défaut était la
                # première expression de la ligne, les deux coïncidaient par
                # accident sur la balance générale, dont la première colonne
                # est le solde initial.
                action = report.expodo_action_audit(
                    ligne["line_id"], donnees["options"], group=ligne.get("group"),
                    expression_label=cellule["label"])
                ecritures = self.env["account.move.line"].search(
                    action.get("domain") or [])
                montant = cellule["raw"]["main"]
                candidats = (
                    sum(ecritures.mapped("balance")),
                    -sum(ecritures.mapped("balance")),
                    sum(ecritures.mapped("debit")),
                    sum(ecritures.mapped("credit")),
                )
                ecart = min(abs(c - montant) for c in candidats)
                self.assertLess(
                    ecart, 0.01,
                    "%s : le montant affiché (%.2f) ne correspond à aucune "
                    "somme des écritures ouvertes par le drill-down"
                    % (report.name, montant))

    def test_spreadsheet_amounts_are_numbers(self):
        """Les montants doivent être des nombres, jamais du texte.

        Un export dont les chiffres arrivent en chaînes est inutilisable pour
        la révision comptable, qui consiste précisément à les recalculer. Le
        fichier s'ouvre pourtant sans erreur : le défaut ne se voit qu'en
        tentant une somme.
        """
        import io
        import re
        import zipfile

        self._invoice("out_invoice", self.customer, 1200.0, self.sale_tax, "NUM1")

        for reference in ("report_balance_fr", "report_cash_flow",
                          "report_grand_livre_fr"):
            report = self.env.ref(
                "expodo_account_reports." + reference, raise_if_not_found=False)
            if not report:
                continue
            with self.subTest(rapport=report.name):
                options = report._expodo_serialize_options(
                    report._expodo_get_options({"date": self.period}))
                classeur = zipfile.ZipFile(
                    io.BytesIO(report._expodo_export_xlsx(options)))
                feuille = classeur.read("xl/worksheets/sheet1.xml").decode()
                partagees = (
                    classeur.read("xl/sharedStrings.xml").decode()
                    if "xl/sharedStrings.xml" in classeur.namelist() else "")
                libelles = re.findall(r"<t[^>]*>(.*?)</t>", partagees, re.S)
                cellules = re.findall(
                    r'<c r="([A-Z]+)(\d+)"([^>]*)>(.*?)</c>', feuille, re.S)

                # La ligne d'en-tête est celle dont la première colonne porte
                # le libellé de la colonne des intitulés.
                entete = 0
                for colonne, rang, attributs, contenu in cellules:
                    if colonne == "A" and 't="s"' in attributs:
                        index = re.search(r"<v>(\d+)</v>", contenu)
                        if index and int(index.group(1)) < len(libelles):
                            if libelles[int(index.group(1))] in ("Label", "Libellé"):
                                entete = int(rang)
                                break

                # Les colonnes de détail — date, tiers — sont légitimement du
                # texte : ce sont des attributs de l'écriture, pas des
                # montants. Leurs colonnes sont identifiées par le type de
                # figure déclaré sur l'état, et non devinées à la position.
                colonnes_texte = set()
                lettre = "B"
                for definition in report._expodo_column_defs():
                    if definition["figure_type"] in ("date", "string",
                                                     "boolean", "datetime"):
                        colonnes_texte.add(lettre)
                    lettre = chr(ord(lettre) + 1)

                en_texte = [
                    colonne + rang
                    for colonne, rang, attributs, _ in cellules
                    if colonne != "A" and colonne not in colonnes_texte
                    and int(rang) > entete
                    and ('t="s"' in attributs or 't="inlineStr"' in attributs)
                ]
                self.assertFalse(
                    en_texte,
                    "%s : montants écrits en texte en %s"
                    % (report.name, ", ".join(en_texte[:5])))

    def test_audit_window_is_titled_with_the_clicked_line(self):
        """Régression : la fenêtre portait le nom de la ligne parente.

        Cliquer « 512001 Bank » dans le grand livre ouvrait une fenêtre
        intitulée « Comptes ». Les montants étaient justes, seul le titre
        mentait — et dans un contexte d'audit, où l'on vérifie précisément
        d'où vient un chiffre, un intitulé erroné est pire qu'absent : il
        fait croire qu'on regarde autre chose.
        """
        self._invoice("out_invoice", self.customer, 1500.0, self.sale_tax, "TIT1")

        cas = (
            ("expodo_account_reports.report_grand_livre_fr", "un compte"),
            ("expodo_account_reports.report_auxiliaire_fr", "un partenaire"),
            ("expodo_account_reports.report_day_book", "une date"),
        )
        for reference, nature in cas:
            report = self.env.ref(reference, raise_if_not_found=False)
            if not report:
                continue
            with self.subTest(rapport=report.name):
                donnees = report.expodo_get_report_data({"date": self.period})
                enfants = report.expodo_expand_line(
                    donnees["lines"][0]["line_id"], donnees["options"])
                if not enfants:
                    continue
                enfant = enfants[0]
                action = report.expodo_action_audit(
                    enfant["line_id"], donnees["options"],
                    group=enfant.get("group"))
                self.assertNotEqual(
                    action["name"],
                    self.env._("Journal items — %(line)s",
                               line=donnees["lines"][0]["name"]),
                    "%s : la fenêtre ne doit pas porter le nom de la ligne "
                    "parente alors qu'on a cliqué %s" % (report.name, nature))
                self.assertIn(
                    enfant["name"].strip()[:12], action["name"],
                    "%s : le titre doit reprendre la ligne cliquée" % report.name)

    # ------------------------------------------------------------------
    # Points d'entrée jamais couverts jusqu'ici
    # ------------------------------------------------------------------

    def test_tax_report_resolution(self):
        """Résolution de la déclaration selon le pays de la société.

        Cette méthode n'était couverte par aucun test — et c'est précisément
        elle qui a échoué en navigateur, laissant le rapport afficher
        « false » sans le moindre message.
        """
        identifiant = self.env["account.report"].expodo_resolve_tax_report()
        rapport = self.env["account.report"].browse(identifiant)
        self.assertTrue(rapport.exists())
        self.assertTrue(
            rapport.line_ids,
            "La déclaration résolue doit porter des lignes")
        pays = (self.company.account_fiscal_country_id
                or self.company.country_id)
        if rapport.country_id:
            self.assertEqual(
                rapport.country_id, pays,
                "La déclaration doit être celle du pays de la société")

    def test_statutory_report_resolution(self):
        """Bilan et compte de résultat : version nationale si elle existe.

        Même angle mort, mêmes conséquences : un menu qui n'affiche rien.
        """
        for genre, racine in (
            ("balance_sheet", "expodo_account_reports.report_balance_sheet"),
            ("profit_loss", "expodo_account_reports.report_profit_loss"),
        ):
            with self.subTest(genre=genre):
                identifiant = self.env["account.report"] \
                    .expodo_resolve_statutory_report(genre)
                rapport = self.env["account.report"].browse(identifiant)
                self.assertTrue(rapport.exists())
                universel = self.env.ref(racine)
                self.assertIn(
                    rapport, universel | self.env["account.report"].search(
                        [("root_report_id", "=", universel.id)]),
                    "Le rapport résolu doit être l'universel ou l'une de ses "
                    "déclinaisons nationales")

    def test_unknown_report_kind_is_refused(self):
        """Un genre inconnu doit lever, pas renvoyer un rapport au hasard."""
        with self.assertRaises(UserError):
            self.env["account.report"].expodo_resolve_statutory_report("inexistant")

    def test_export_entry_point(self):
        """Le point d'entrée appelé par le navigateur pour exporter.

        Les tests appelaient les méthodes internes ; celle que le client
        invoque réellement n'était jamais exercée.
        """
        report = self.env.ref("expodo_account_reports.report_balance_fr")
        options = report._expodo_serialize_options(
            report._expodo_get_options({"date": self.period}))
        for format_ in ("pdf", "xlsx"):
            with self.subTest(format=format_):
                action = report.expodo_action_export(options, format_)
                self.assertEqual(action.get("type"), "ir.actions.act_url")
                self.assertIn(format_, action.get("url", ""))

    def test_the_exported_period_is_written_in_the_readers_language(self):
        """Les dates de l'en-tête suivent la langue du document.

        Interpolées telles quelles, elles sortaient en ISO : un bilan
        entièrement rédigé en français s'ouvrait sur « Au 2026-09-26 ». Le
        document part chez une banque ou un expert-comptable ; la date doit y
        être écrite comme le reste.
        """
        report = self.env.ref("expodo_account_reports.report_bilan_fr")
        options = report._expodo_get_options({
            "date": {"mode": "range", "filter": "custom",
                     "date_from": date(2026, 1, 1), "date_to": date(2026, 9, 26)},
        })
        # La langue est prise parmi celles que la base porte réellement.
        #
        # Le test nommait `fr_FR`, absente d'une base espagnole ou allemande :
        # il échouait sur « Invalid language code » au lieu de vérifier quoi
        # que ce soit. Le module se vend dans sept langues ; ses tests ne
        # peuvent pas en supposer une.
        langue = self.env["res.lang"].search(
            [("date_format", "!=", "%Y-%m-%d")], limit=1)
        if not langue:
            self.skipTest("Aucune langue installée ne formate autrement qu'en ISO")
        attendu = date(2026, 9, 26).strftime(langue.date_format)
        libelle = report.with_context(
            lang=langue.code)._expodo_export_period_label(options)
        self.assertIn(attendu, libelle)
        self.assertNotIn("2026-09-26", libelle)

    def test_a_statement_of_position_announces_a_single_date(self):
        """L'en-tête de l'export dit la même période que l'écran.

        Un état arrêté à une date n'offre qu'une borne à l'écran. Son export
        annonçait « du 1er janvier au 26 septembre », ce qui laissait croire
        que déplacer une date de début changerait les montants.
        """
        report = self.env.ref("expodo_account_reports.report_bilan_fr")
        options = report._expodo_get_options()
        libelle = report.with_context(lang="en_US")._expodo_export_period_label(
            options)
        self.assertTrue(
            libelle.startswith("As of"),
            "Un état arrêté à une date annonce une seule borne : %s" % libelle)

    def test_unknown_export_format_is_refused(self):
        report = self.env.ref("expodo_account_reports.report_balance_fr")
        options = report._expodo_serialize_options(report._expodo_get_options())
        with self.assertRaises(UserError):
            report.expodo_action_export(options, "docx")

    def test_export_url_stays_within_server_limits(self):
        """Régression : exporter après avoir tout déplié produisait une URL rejetée.

        Les options voyagent dans la chaîne de requête de l'URL de
        téléchargement. Sur un plan comptable réel — 1 300 comptes sur la base
        de développement — déplier puis exporter produisait une URL de plus de
        24 000 caractères, là où un serveur en accepte 8 192. L'utilisateur ne
        voyait qu'un téléchargement qui échoue, sans explication.

        La liste des lignes dépliées ne sert à rien à l'export, qui développe
        de toute façon tous les niveaux : elle est désormais vidée avant de
        construire l'URL. Ce test vérifie que l'export l'ignore, de sorte que
        la vider reste sans conséquence.
        """
        import json
        import urllib.parse

        report = self.env.ref("expodo_account_reports.report_grand_livre_fr")
        options = report._expodo_serialize_options(
            report._expodo_get_options({"date": self.period}))

        # Un export identique, avec et sans lignes dépliées.
        avec = dict(options)
        avec["unfolded_lines"] = ["%d_account_id_%d" % (report.line_ids[0].id, i)
                                  for i in range(800)]
        _, sans_lignes = report._expodo_export_rows(options)
        _, avec_lignes = report._expodo_export_rows(avec)
        self.assertEqual(
            len(sans_lignes), len(avec_lignes),
            "L'export doit ignorer les lignes dépliées : les transmettre "
            "n'apporte rien et allonge l'URL sans limite")

        url = "/expodo_account_reports/export/xlsx/%d?options=%s" % (
            report.id, urllib.parse.quote(json.dumps(options)))
        self.assertLess(
            len(url), 8192,
            "L'URL d'export doit rester sous la limite usuelle des serveurs "
            "(%d caractères)" % len(url))

    def test_expanded_lines_carry_every_column_group(self):
        """Régression : le dépliage produisait une ligne par groupe de colonnes.

        Avec une comparaison active, chaque compte apparaissait deux fois —
        une fois avec les chiffres de l'exercice, une fois avec ceux de
        l'exercice précédent — chacune ne portant que la moitié des colonnes.

        À l'écran les doublons se confondaient : deux lignes du même nom,
        l'une avec la moitié gauche remplie, l'autre avec la moitié droite.
        C'est l'ouverture d'un classeur exporté qui l'a révélé, des lignes à
        quatre colonnes au milieu de lignes à sept.
        """
        self._invoice("out_invoice", self.customer, 1800.0, self.sale_tax, "CMP1")

        report = self.env.ref("expodo_account_reports.report_balance_fr")
        comparaison = {"filter": "same_last_year", "number_period": 1}
        donnees = report.expodo_get_report_data({
            "date": self.period, "comparison": comparaison})
        self.assertEqual(
            len(donnees["column_groups"]), 2,
            "Le test suppose une comparaison active")

        enfants = report.expodo_expand_line(
            donnees["lines"][0]["line_id"], donnees["options"])
        self.assertTrue(enfants, "Le dépliage doit produire des lignes")

        noms = [e["name"] for e in enfants]
        self.assertEqual(
            len(noms), len(set(noms)),
            "Chaque compte ne doit apparaître qu'une fois, pas une fois par "
            "groupe de colonnes")

        groupes = {g["key"] for g in donnees["column_groups"]}
        for enfant in enfants:
            for cellule in enfant["columns"]:
                self.assertEqual(
                    set(cellule["raw"]), groupes,
                    "%s : chaque cellule doit porter tous les groupes de "
                    "colonnes" % enfant["name"])
                for valeur in cellule["raw"].values():
                    self.assertIsNotNone(
                        valeur,
                        "%s : un compte sans mouvement sur la période comparée "
                        "vaut zéro, pas « rien ». Une cellule vide oblige le "
                        "lecteur à deviner si le compte n'existait pas ou s'il "
                        "n'a rien enregistré." % enfant["name"])

    def _ecriture_nulle_sur_un_compte(self, compte, montant, jour):
        """Écriture qui débite et crédite le même compte : solde nul partout.

        Le compte a bougé — il existe des écritures à auditer — mais aucune
        colonne de la balance ne le distingue d'un compte jamais touché.
        """
        journal = self.env["account.journal"].search(
            [("type", "=", "general")], limit=1)
        move = self.env["account.move"].create({
            "journal_id": journal.id,
            "date": jour,
            "ref": "NULLE",
            "line_ids": [
                Command.create({
                    "name": "NULLE D", "account_id": compte.id,
                    "debit": montant, "credit": 0.0}),
                Command.create({
                    "name": "NULLE C", "account_id": compte.id,
                    "debit": 0.0, "credit": montant}),
            ],
        })
        move.action_post()
        return move

    def test_un_compte_nul_sur_toute_la_periode_ne_figure_pas_a_la_balance(self):
        """La balance générale ne liste pas les comptes qu'elle n'a rien à dire.

        Le solde de la balance se lit depuis l'origine : le groupement ramène
        donc tout compte ayant bougé un jour, même si la période demandée n'en
        retient rien. Sur une base réelle ouverte sur le mois en cours, cela
        donnait vingt lignes à zéro pour huit lignes utiles, et la balance
        devenait illisible au moment précis où le comptable l'ouvre.

        Le compte reste affiché dès que l'une des colonnes le distingue.
        """
        compte = self.env["account.account"].search(
            [("account_type", "=", "expense")], limit=1)
        self.assertTrue(compte, "Le test suppose un compte de charge")
        self._ecriture_nulle_sur_un_compte(compte, 500.0, "2026-03-20")
        self._invoice("out_invoice", self.customer, 1200.0, self.sale_tax,
                      "BAL0", day="2026-06-10")

        report = self.env.ref("expodo_account_reports.report_balance_fr")

        juin = {"mode": "range", "filter": "custom",
                "date_from": date(2026, 6, 1), "date_to": date(2026, 6, 30)}
        donnees = report.expodo_get_report_data({"date": juin})
        enfants = report.expodo_expand_line(
            donnees["lines"][0]["line_id"], donnees["options"])
        self.assertTrue(
            enfants, "La facture de juin doit laisser des comptes à afficher")
        noms = [e["name"] for e in enfants]
        self.assertFalse(
            [n for n in noms if n.startswith(compte.code)],
            "%s est nul sur toute la période et depuis l'origine : il ne "
            "doit pas occuper une ligne. Lignes rendues : %s"
            % (compte.code, noms))
        for enfant in enfants:
            valeurs = [v for cellule in enfant["columns"]
                       for v in cellule["raw"].values()]
            self.assertTrue(
                any(v not in (0.0, 0, None, "", False) for v in valeurs),
                "%s ne porte que des zéros et aurait dû être écarté"
                % enfant["name"])

        annee = report.expodo_get_report_data({"date": self.period})
        noms_annee = [
            e["name"] for e in report.expodo_expand_line(
                annee["lines"][0]["line_id"], annee["options"])]
        self.assertTrue(
            [n for n in noms_annee if n.startswith(compte.code)],
            "Sur l'exercice entier le compte porte 500 au débit et 500 au "
            "crédit : le masquer effacerait un mouvement réel. Lignes "
            "rendues : %s" % noms_annee)

    def test_le_detail_des_ecritures_n_est_jamais_masque(self):
        """Le filtre des groupes nuls ne doit pas manger le grand livre.

        Une écriture porte une date et un libellé : ces colonnes ne valent
        jamais zéro, donc aucune ligne de détail ne peut être prise pour un
        groupe vide — y compris une écriture de montant nul.
        """
        compte = self.env["account.account"].search(
            [("account_type", "=", "expense")], limit=1)
        self._ecriture_nulle_sur_un_compte(compte, 700.0, "2026-04-05")

        report = self.env.ref("expodo_account_reports.report_grand_livre_fr")
        donnees = report.expodo_get_report_data({"date": self.period})
        comptes = report.expodo_expand_line(
            donnees["lines"][0]["line_id"], donnees["options"])
        cible = [c for c in comptes if c["name"].startswith(compte.code)]
        self.assertTrue(
            cible,
            "Le compte a 700 au débit et 700 au crédit sur l'exercice : il "
            "doit figurer au grand livre")

        ecritures = report.expodo_expand_line(
            cible[0]["line_id"], donnees["options"],
            parent_group=cible[0]["group"], level=cible[0]["next_level"])
        self.assertTrue(
            ecritures,
            "Les deux écritures du compte doivent rester visibles au détail")

    def test_export_rows_are_all_the_same_width(self):
        """Toutes les lignes exportées doivent avoir le même nombre de colonnes.

        Une ligne plus courte décale la lecture du tableur et fausse toute
        formule appliquée à une colonne.
        """
        self._invoice("out_invoice", self.customer, 900.0, self.sale_tax, "LRG1")

        report = self.env.ref("expodo_account_reports.report_balance_fr")
        options = report._expodo_serialize_options(report._expodo_get_options({
            "date": self.period,
            "comparison": {"filter": "same_last_year", "number_period": 1}}))
        donnees, lignes = report._expodo_export_rows(options)
        attendu = len(donnees["columns"]) * len(donnees["column_groups"])

        for ligne in lignes:
            largeur = sum(
                len(cellule.get("raw") or {}) for cellule in ligne["columns"])
            self.assertEqual(
                largeur, attendu,
                "« %s » porte %d valeurs au lieu de %d"
                % (ligne.get("name", "?"), largeur, attendu))

    def test_spreadsheet_header_is_readable(self):
        """Régression : les en-têtes de colonnes comparées étaient tronqués.

        « Credit (Compared: from 2025-01-01 to 2025-12-31) » fait une
        cinquantaine de caractères dans une colonne de treize. Sans repli, et
        la cellule voisine étant occupée, le tableur coupe le texte :
        l'utilisateur lisait « Credit (Compared: from… » sur les six colonnes
        et ne distinguait plus les deux périodes.

        Le défaut portait précisément sur la fonction de comparaison, celle
        pour laquelle on ouvre le fichier.
        """
        import io
        import re
        import zipfile

        report = self.env.ref("expodo_account_reports.report_balance_fr")
        options = report._expodo_serialize_options(report._expodo_get_options({
            "date": self.period,
            "comparison": {"filter": "same_last_year", "number_period": 1}}))
        classeur = zipfile.ZipFile(io.BytesIO(report._expodo_export_xlsx(options)))

        styles = classeur.read("xl/styles.xml").decode()
        self.assertIn(
            'wrapText="1"', styles,
            "L'en-tête doit autoriser le retour à la ligne")

        feuille = classeur.read("xl/worksheets/sheet1.xml").decode()
        # L'ancrage sur l'espace est nécessaire : sans lui, `[^>]*` avale
        # jusqu'à `customHeight="1"` et l'expression lit 1 au lieu de 48.
        entetes = re.findall(r'<row r="(\d+)"[^>]*\sht="(\d+)"', feuille)
        self.assertTrue(
            entetes and any(int(h) >= 30 for _, h in entetes),
            "La ligne d'en-tête doit être assez haute pour le texte replié")

    def test_special_characters_survive_the_export(self):
        """Un libellé exotique ne doit casser ni le classeur ni le PDF.

        Une apostrophe, un guillemet ou une esperluette dans un nom de compte
        casse un XML mal échappé. Le fichier ne s'ouvre alors plus du tout —
        et le plan comptable français est plein d'apostrophes.
        """
        import io
        import zipfile

        compte = self.env["account.account"].create({
            "code": "299999",
            "name": "Caractères \"exotiques\" & <balises> d'essai — 100%",
            "account_type": "asset_fixed",
        })
        journal = self.env["account.journal"].search([
            ("type", "=", "general")], limit=1)
        contrepartie = self.env["account.account"].search([
            ("account_type", "=", "asset_current")], limit=1)
        ecriture = self.env["account.move"].create({
            "journal_id": journal.id,
            "date": date(2026, 5, 20),
            "line_ids": [
                Command.create({"name": "x", "account_id": compte.id,
                                "debit": 1234.56, "credit": 0.0}),
                Command.create({"name": "x", "account_id": contrepartie.id,
                                "debit": 0.0, "credit": 1234.56}),
            ],
        })
        ecriture.action_post()

        report = self.env.ref("expodo_account_reports.report_balance_fr")
        options = report._expodo_serialize_options(
            report._expodo_get_options({"date": self.period}))

        classeur = report._expodo_export_xlsx(options)
        archive = zipfile.ZipFile(io.BytesIO(classeur))
        self.assertIn(
            "xl/worksheets/sheet1.xml", archive.namelist(),
            "Le classeur doit rester lisible")
        chaines = archive.read("xl/sharedStrings.xml").decode()
        self.assertIn(
            "exotiques", chaines,
            "Le libellé doit figurer dans le classeur")

        document = report._expodo_export_pdf(options)
        self.assertTrue(
            document.startswith(b"%PDF-"),
            "Le PDF doit rester valide malgré les caractères spéciaux")

    def test_report_on_an_empty_period(self):
        """Une période sans écriture doit s'afficher et s'exporter.

        Le premier mois d'une base neuve est vide : c'est souvent le premier
        écran qu'un nouvel utilisateur ouvre.
        """
        vide = {"mode": "range", "filter": "custom",
                "date_from": date(2019, 1, 1), "date_to": date(2019, 1, 31)}
        for reference in ("report_balance_fr", "report_cash_flow",
                          "report_day_book", "report_aged_receivable_fr"):
            report = self.env.ref(
                "expodo_account_reports." + reference, raise_if_not_found=False)
            if not report:
                continue
            with self.subTest(rapport=report.name):
                donnees = report.expodo_get_report_data({"date": vide})
                self.assertIsInstance(donnees["lines"], list)
                options = report._expodo_serialize_options(
                    report._expodo_get_options({"date": vide}))
                self.assertTrue(
                    report._expodo_export_xlsx(options)[:2] == b"PK",
                    "%s : l'export doit rester valide sur une période vide"
                    % report.name)

    def test_every_report_agrees_between_screen_and_spreadsheet(self):
        """Écran et classeur doivent porter les mêmes montants, sur tous les rapports.

        C'est l'invariant qui compte le plus pour un export comptable : le
        fichier transmis à l'expert-comptable doit dire exactement ce que
        l'utilisateur avait sous les yeux. Un écart, même d'un centime sur un
        seul rapport, ruine la confiance dans les quinze autres.

        Le contrôle porte sur tous les rapports d'un coup, parce qu'un défaut
        de sérialisation touche rarement un seul.
        """
        import io
        import re
        import zipfile

        self._invoice("out_invoice", self.customer, 3300.0, self.sale_tax, "AGR1")
        self._invoice("in_invoice", self.supplier, 770.0, self.purchase_tax, "AGR2")

        ecarts = []
        rapports = self.env["account.report"].search([])
        for report in rapports:
            donnee = self.env["ir.model.data"].search([
                ("model", "=", "account.report"), ("res_id", "=", report.id),
            ], limit=1)
            if not (donnee.module or "").startswith("expodo"):
                continue

            options = report._expodo_serialize_options(
                report._expodo_get_options({"date": self.period}))
            _, lignes = report._expodo_export_rows(options)
            ecran = sum(
                valeur for ligne in lignes
                for valeur in [(ligne["columns"][0].get("raw") or {}).get("main")]
                if isinstance(valeur, (int, float)))

            archive = zipfile.ZipFile(
                io.BytesIO(report._expodo_export_xlsx(options)))
            feuille = archive.read("xl/worksheets/sheet1.xml").decode()
            classeur = 0.0
            for cellule in re.finditer(
                    r'<c r="B\d+"([^>]*)>(?:<v>([-\d.]+)</v>)?</c>', feuille):
                # Une cellule de type chaîne porte un index dans <v>, pas un
                # montant : la compter fausse la somme d'autant.
                if 't="s"' in cellule.group(1) or not cellule.group(2):
                    continue
                classeur += float(cellule.group(2))

            if abs(ecran - classeur) > 0.01:
                ecarts.append("%s : écran %.2f, classeur %.2f"
                              % (report.name, ecran, classeur))

        self.assertFalse(
            ecarts,
            "L'export diverge de l'écran :\n  " + "\n  ".join(ecarts))

    def test_no_pdf_ends_on_a_blank_page(self):
        """Aucun rapport ne doit produire de page finale vide.

        Une page blanche en fin de document laisse croire à un contenu perdu,
        et se remarque d'autant plus sur un document transmis à un tiers.
        """
        import io

        try:
            from PyPDF2 import PdfReader
        except ImportError:
            try:
                from pypdf import PdfReader
            except ImportError:
                self.skipTest("Aucun lecteur PDF disponible ici")

        vides = []
        for report in self.env["account.report"].search([]):
            donnee = self.env["ir.model.data"].search([
                ("model", "=", "account.report"), ("res_id", "=", report.id),
            ], limit=1)
            if not (donnee.module or "").startswith("expodo"):
                continue
            options = report._expodo_serialize_options(
                report._expodo_get_options({"date": self.period}))
            lecteur = PdfReader(io.BytesIO(report._expodo_export_pdf(options)))
            for numero, page in enumerate(lecteur.pages, start=1):
                if len((page.extract_text() or "").strip()) < 50:
                    vides.append("%s page %d" % (report.name, numero))

        self.assertFalse(vides, "Pages quasi vides : " + ", ".join(vides))

    def test_pdf_keeps_printable_side_margins(self):
        """Régression : le tableau touchait le bord de la page.

        Mesuré avant correction : le contenu allait de 0 à 592 points sur une
        page de 595 — marge gauche nulle, marge droite d'un millimètre. Aucune
        imprimante n'imprime dans les cinq derniers millimètres : la colonne
        du solde était coupée à l'impression, alors que le PDF paraissait
        parfait à l'écran.

        Les marges latérales ne se règlent pas par `specific_paperformat_args`
        — Odoo n'y lit que `margin-top`, `margin-bottom`, `header-spacing` et
        `dpi`. Elles sont posées dans la feuille de style du gabarit, et ce
        test vérifie que la règle y figure toujours.
        """
        rendu = self.env["ir.qweb"]._render(
            "expodo_account_reports.report_pdf_document",
            {
                "header": {"report_name": "x", "company_name": "y",
                           "date_from": "2026-01-01", "date_to": "2026-12-31",
                           "period": "x", "label_column": "Label", "filters": []},
                "labels": {"label": "Label"},
                "column_groups": [{"key": "main", "label": ""}],
                "columns": [{"label": "balance", "name": "Balance",
                             "figure_type": "monetary", "blank_if_zero": False}],
                "lines": [],
            },
        )
        self.assertIn(
            "8mm", str(rendu),
            "Le gabarit PDF doit poser des marges latérales, faute de quoi "
            "la dernière colonne est coupée à l'impression")

    def test_report_without_lines_explains_itself(self):
        """Un rapport que la localisation n'a pas rempli doit le dire.

        Hors de France, certaines localisations fournissent une déclaration
        de taxes sans aucune ligne — la localisation américaine, par exemple.
        L'utilisateur ouvrait alors un tableau vide, sans rien pour distinguer
        « je n'ai pas d'écritures » de « ce rapport n'existe pas chez moi ».

        Les deux situations appellent des actions opposées, et l'écran ne
        permettait pas de les séparer.
        """
        vide = self.env["account.report"].create({
            "name": "Rapport sans lignes",
            "filter_date_range": True,
        })
        donnees = vide.expodo_get_report_data({"date": self.period})
        self.assertEqual(donnees["lines"], [])
        self.assertTrue(
            donnees.get("notice"),
            "Un rapport sans ligne définie doit porter un message explicatif")

        rempli = self.env.ref("expodo_account_reports.report_balance_fr")
        self.assertFalse(
            rempli.expodo_get_report_data({"date": self.period}).get("notice"),
            "Un rapport qui a des lignes ne doit porter aucun message, même "
            "lorsqu'aucune écriture ne tombe dans la période")

    def test_cash_flow_net_result_matches_the_profit_and_loss(self):
        """Le résultat net du tableau de flux doit être celui du compte de résultat.

        Régression : la ligne « Résultat net » excluait les dotations, si bien
        qu'elle affichait le résultat *avant* amortissement sous ce nom —
        61 317,60 là où le compte de résultat disait 57 117,60. La dotation
        apparaissait en outre en négatif, ce qui se lisait « l'amortissement a
        consommé de la trésorerie », l'inverse de ce qu'il fait.

        Le total du tableau tombait juste malgré tout : les deux erreurs se
        compensaient et la ligne de contrôle restait à zéro. Aucun test ne
        pouvait le voir, puisque tous vérifiaient des identités internes. Seul
        le lecteur était trompé — et un comptable qui lit un résultat net
        contredisant son compte de résultat cesse de faire confiance au reste.

        Découvert sur une base allemande : sur la base française, le compte de
        dotation n'est pas typé « amortissement », la ligne valait donc zéro et
        l'écart restait invisible.
        """
        self._invoice("out_invoice", self.customer, 5000.0, self.sale_tax, "CF1")
        self._invoice("in_invoice", self.supplier, 1200.0, self.purchase_tax, "CF2")

        flux = self.env.ref("expodo_account_reports.report_cash_flow")
        resultat = self.env["account.report"].browse(
            self.env["account.report"].expodo_resolve_statutory_report("profit_loss"))

        def valeur(rapport, fragment):
            # Les intitulés de lignes sont traduits : la langue est épinglée,
            # sinon le test cherche « NET RESULT » dans « RÉSULTAT NET » et ne
            # trouve rien, pour une raison étrangère à ce qu'il vérifie.
            lignes = rapport.with_context(lang="en_US").expodo_get_report_data(
                {"date": self.period})["lines"]
            for ligne in lignes:
                if fragment.lower() in ligne["name"].lower():
                    return ligne["columns"][0]["raw"].get("main")
            return None

        net_flux = valeur(flux, "Net result")
        net_resultat = valeur(resultat, "NET RESULT")
        self.assertIsNotNone(net_flux)
        self.assertIsNotNone(net_resultat)
        self.assertAlmostEqual(
            net_flux, net_resultat, places=2,
            msg="Le tableau de flux annonce %s de résultat net, le compte de "
                "résultat %s" % (net_flux, net_resultat))

    def test_depreciation_is_added_back_not_deducted(self):
        """Une dotation est réintégrée : elle n'a pas consommé de trésorerie.

        Le signe se vérifie sur une base où le compte de dotation porte bien
        le type « amortissement ». Ailleurs la ligne vaut zéro et le test ne
        conclut rien — c'est précisément ce qui avait masqué le défaut.
        """
        # Le compte est **créé** plutôt que cherché.
        #
        # Le plan comptable français livré par Odoo ne type aucun de ses
        # comptes 681 en « amortissement » : ce test se sautait donc à chaque
        # exécution, sur la seule base où la suite tourne. Il passait au vert
        # sans jamais rien vérifier — c'est-à-dire qu'il donnait la confiance
        # sans la mériter, ce qui est pire qu'un test absent.
        compte = self.env["account.account"].search([
            ("account_type", "=", "expense_depreciation")], limit=1)
        if not compte:
            compte = self.env["account.account"].create({
                "code": "681999",
                "name": "Dotation pour ce test",
                "account_type": "expense_depreciation",
            })

        journal = self.env["account.journal"].search([
            ("type", "=", "general")], limit=1)
        contrepartie = self.env["account.account"].search([
            ("account_type", "=", "asset_fixed")], limit=1)
        self.env["account.move"].create({
            "journal_id": journal.id,
            "date": date(2026, 6, 30),
            "line_ids": [
                Command.create({"name": "dot", "account_id": compte.id,
                                "debit": 900.0, "credit": 0.0}),
                Command.create({"name": "dot", "account_id": contrepartie.id,
                                "debit": 0.0, "credit": 900.0}),
            ],
        }).action_post()

        flux = self.env.ref("expodo_account_reports.report_cash_flow")
        for ligne in flux.expodo_get_report_data({"date": self.period})["lines"]:
            # L'intitulé a changé le jour où la ligne a cessé de ne porter que
            # des dotations : on reconnaît la ligne par son code, qui lui ne
            # bouge pas, et à défaut par l'un ou l'autre des deux libellés.
            nom = (ligne.get("name") or "").lower()
            if ("depreciation and provisions" in nom
                    or "non-cash items" in nom
                    or "sans effet de trésorerie" in nom):
                montant = ligne["columns"][0]["raw"].get("main")
                self.assertGreater(
                    montant, 0,
                    "La dotation doit être réintégrée, donc positive : "
                    "un montant négatif se lit « l'amortissement a consommé "
                    "de la trésorerie »")
                return
        self.fail("La ligne de réintégration des dotations est introuvable")

    def test_special_export_rows_render_in_pdf(self):
        """Les lignes spéciales doivent se rendre comme les autres.

        Régression : la ligne de troncature et le message « rapport sans
        lignes » portaient la clé `value` au singulier, là où le gabarit lit
        `values`. Le rendu levait une `KeyError`.

        Ces deux lignes n'apparaissent que dans les cas rares qu'elles sont
        censées expliquer — un export de plus de vingt mille lignes, une
        localisation qui ne fournit pas ses grilles. Le défaut ne pouvait donc
        sortir que chez un client, au moment précis où il avait besoin de
        l'explication.
        """
        import io

        report = self.env.ref("expodo_account_reports.report_grand_livre_fr")
        self._invoice("out_invoice", self.customer, 700.0, self.sale_tax, "SPE1")
        options = report._expodo_serialize_options(
            report._expodo_get_options({"date": self.period}))

        _, lignes = report._expodo_export_rows(options, limite=3)
        self.assertIn("truncated", lignes[-1]["name"])
        for cellule in lignes[-1]["columns"]:
            self.assertIn(
                "values", cellule,
                "La ligne de troncature doit porter la clé attendue par le "
                "gabarit, faute de quoi le PDF ne se rend plus")

        vide = self.env["account.report"].create({
            "name": "Sans lignes", "filter_date_range": True})
        donnees, rangs = vide._expodo_export_rows(
            vide._expodo_serialize_options(
                vide._expodo_get_options({"date": self.period})))
        self.assertEqual(len(rangs), 1)
        for cellule in rangs[0]["columns"]:
            self.assertIn("values", cellule)

        document = vide._expodo_export_pdf(
            vide._expodo_serialize_options(
                vide._expodo_get_options({"date": self.period})))
        self.assertTrue(
            document.startswith(b"%PDF-"),
            "Le PDF d'un rapport sans lignes doit se rendre et porter le message")

    def test_amount_columns_are_not_width_constrained(self):
        """Les colonnes de montants ne doivent pas avoir de largeur fixe.

        Régression : une largeur de 88 px, posée pour tenter de réduire le
        nombre de pages, faisait déborder les montants en dollars —
        « $ 506,803.18 » dépasse cette largeur et, avec `white-space: nowrap`,
        empiète sur la colonne voisine. Le document devenait illisible.

        En euros le montant passait de justesse : le défaut ne s'est vu que sur
        une base américaine. La tentative n'apportait par ailleurs rien, le
        nombre de pages étant commandé par le nombre de lignes.
        """
        gabarit = self.env.ref("expodo_account_reports.report_pdf_document")
        self.assertNotIn(
            "88px", gabarit.arch,
            "Aucune largeur fixe ne doit contraindre les colonnes de montants")

    def test_other_presentations_of_the_same_statement_are_reachable(self):
        """Un état livré mais inatteignable n'existe pas.

        Le menu ne désigne qu'une présentation par état. Les autres — soldes
        intermédiaires de gestion, tableau de flux en méthode directe — étaient
        définies, testées et traduites, mais aucun chemin de l'interface n'y
        menait. L'édition Enterprise les offre par un sélecteur en en-tête.

        Le test tient aussi l'autre bout : la présentation d'un référentiel que
        la société n'utilise pas ne doit pas être proposée, faute de quoi une
        société française se verrait offrir un bilan SYSCOHADA.
        """
        if (self.env.company.account_fiscal_country_id.code
                or self.env.company.country_id.code) != "FR":
            self.skipTest("Les présentations françaises ne sont pas proposées "
                          "hors de France, et c'est le comportement voulu")

        resultat = self.env.ref("expodo_account_reports.report_resultat_fr")
        sig = self.env.ref("expodo_account_reports.report_sig_fr")
        ohada = self.env.ref("expodo_account_reports.report_bilan_ohada")

        proposees = resultat._expodo_available_variants()
        identifiants = [v["id"] for v in proposees]
        self.assertIn(
            sig.id, identifiants,
            "Les soldes intermédiaires de gestion doivent être atteignables "
            "depuis le compte de résultat")
        self.assertIn(
            resultat.id, identifiants,
            "La présentation courante doit figurer dans la liste")

        bilan = self.env.ref("expodo_account_reports.report_bilan_fr")
        self.assertNotIn(
            ohada.id, [v["id"] for v in bilan._expodo_available_variants()],
            "Une société française ne doit pas se voir proposer le SYSCOHADA")

        grand_livre = self.env.ref("expodo_account_reports.report_grand_livre_fr")
        self.assertEqual(
            grand_livre._expodo_available_variants(), [],
            "Un état sans autre présentation ne doit pas afficher de sélecteur")

    def test_a_prefix_line_unfolds_into_its_accounts(self):
        """Un bilan doit dire de quels comptes ses montants viennent.

        Les dix états structurés du module — bilans, comptes de résultat, SIG —
        sont bâtis sur le moteur `account_codes`, que le moteur de dépliage ne
        savait pas développer : il exigeait une expression `domain` ou
        `tax_tags`. Aucune de leurs lignes n'était donc dépliable. On lisait
        « Créances 76 500 » sans pouvoir savoir de quels comptes il s'agissait,
        là où l'édition Enterprise déplie chacune de ses lignes.

        Le contrôle porte sur la seule chose qu'un dépliage ne doit jamais
        faire : le détail somme au total. Sans quoi le lecteur qui vérifie
        découvre que ses deux lectures se contredisent.
        """
        if (self.env.company.account_fiscal_country_id.code
                or self.env.company.country_id.code) != "FR":
            self.skipTest("La ligne Créances est celle du bilan français ; "
                          "ses préfixes ne désignent rien sur un autre plan")

        # Le test pose sa propre créance.
        #
        # Il s'appuyait sur les écritures déjà présentes dans la base de
        # développement. Sur une base fraîchement installée — celle de tout
        # acheteur — la ligne Créances était vide, le dépliage ne rendait
        # rien, et le test échouait sans qu'aucun défaut existe. Un test qui
        # dépend des données du moment ne dit rien du module.
        self._invoice("out_invoice", self.customer, 1200.0, None, "DEPLI")

        rapport = self.env.ref("expodo_account_reports.report_bilan_fr")
        ligne = self.env.ref("expodo_account_reports.bilan_fr_creances")
        self.assertEqual(
            ligne.groupby, "account_id",
            "Une ligne terminale de bilan doit se déplier par compte")

        options = rapport._expodo_get_options({"date": self.period})
        donnees = rapport.expodo_get_report_data({"date": self.period})
        total = None
        for affichee in donnees["lines"]:
            if affichee["line_id"] == ligne.id:
                total = affichee["columns"][0]["raw"].get("main")
        self.assertIsNotNone(total, "La ligne Créances doit figurer au bilan")

        sous_lignes = rapport.expodo_expand_line(ligne.id, options)
        self.assertTrue(sous_lignes, "Le dépliage doit rendre au moins un compte")
        detail = sum(
            (sous["columns"][0]["raw"].get("main") or 0.0) for sous in sous_lignes)
        self.assertAlmostEqual(
            detail, total, places=2,
            msg="Le détail par compte annonce %s là où la ligne annonce %s"
                % (detail, total))

    def test_the_journal_filter_has_a_control(self):
        """Un filtre déclaré doit être atteignable depuis l'écran.

        Les états déclarent `filter_journals` et le moteur lit `journal_ids`
        depuis toujours ; il manquait la commande qui le renseigne. Le filtre
        existait des deux côtés sans que personne puisse s'en servir.
        """
        grand_livre = self.env.ref("expodo_account_reports.report_grand_livre_fr")
        donnees = grand_livre.expodo_get_report_data({"date": self.period})
        journaux = donnees["report"].get("journals")
        self.assertTrue(
            grand_livre.filter_journals,
            "Le grand livre doit déclarer le filtre de journaux")
        self.assertTrue(
            journaux, "Un état qui déclare le filtre doit proposer des journaux")
        societes = donnees["options"]["company_ids"]
        for choix in journaux:
            self.assertIn(
                self.env["account.journal"].browse(choix["id"]).company_id.id,
                societes,
                "Un journal hors du périmètre de sociétés ne doit pas être proposé")

        agee = self.env.ref("expodo_account_reports.report_aged_receivable_fr")
        self.assertEqual(
            agee.expodo_get_report_data({"date": self.period})["report"].get(
                "journals"), [],
            "Un état qui ne déclare pas le filtre ne doit proposer aucun journal")

        # Le choix restreint réellement les montants.
        balance = self.env.ref("expodo_account_reports.report_balance_fr")
        base = balance.expodo_get_report_data({"date": self.period})
        total = base["lines"][0]["columns"][1]["raw"]["main"] or 0.0
        cumul = 0.0
        for choix in base["report"]["journals"]:
            options = dict(base["options"])
            options["journal_ids"] = [choix["id"]]
            cumul += balance.expodo_get_report_data(
                options)["lines"][0]["columns"][1]["raw"]["main"] or 0.0
        self.assertAlmostEqual(
            cumul, total, places=2,
            msg="La somme des journaux doit valoir le total : %s contre %s"
                % (cumul, total))

    def test_options_survive_the_trip_to_the_browser(self):
        """Ce qui part au navigateur doit revenir intact.

        Les options repartent et reviennent à chaque dépliage, chaque clic sur
        un montant, chaque export. Si la sérialisation en perd une partie, le
        premier affichage est juste et tout ce qui suit ne l'est plus, sans que
        rien ne le signale. C'est ce qui est arrivé au domaine de la
        ventilation horizontale, réduit à néant par un groupe de colonnes
        recopié à sa seule période.

        Deux exigences : les options doivent passer par JSON, et le rapport
        recalculé sur les options revenues doit donner exactement les mêmes
        montants.
        """
        journal = self.env["account.journal"].search([], limit=1)
        variantes = [
            ("simple", {}),
            ("comparaison", {"comparison": {"filter": "previous_period",
                                            "number_period": 2}}),
            ("brouillons", {"all_entries": True}),
            ("journal", {"journal_ids": journal.ids}),
        ]
        noms = ("report_bilan_fr", "report_balance_fr", "report_grand_livre_fr",
                "report_aged_receivable_fr", "report_cash_flow")

        def empreinte(donnees):
            return [
                (ligne["line_id"], sorted(
                    (cle, valeur)
                    for cellule in ligne["columns"]
                    for cle, valeur in cellule["raw"].items()))
                for ligne in donnees["lines"]
            ]

        for nom in noms:
            rapport = self.env.ref("expodo_account_reports." + nom)
            for libelle, extra in variantes:
                depart = dict(extra)
                depart["date"] = self.period
                donnees = rapport.expodo_get_report_data(depart)
                try:
                    revenues = json.loads(json.dumps(donnees["options"]))
                except TypeError as erreur:
                    raise AssertionError(
                        "%s / %s : les options ne passent pas par JSON : %s"
                        % (nom, libelle, erreur)) from erreur
                self.assertEqual(
                    empreinte(rapport.expodo_get_report_data(revenues)),
                    empreinte(donnees),
                    "%s / %s : les montants changent après l'aller-retour"
                    % (nom, libelle))

    def test_every_opening_period_odoo_offers_is_understood(self):
        """Aucune période déclarée ne doit retomber silencieusement ailleurs.

        `default_opening_date_filter` propose neuf valeurs. Le moteur n'en
        traduisait que cinq, et les quatre autres retombaient sur l'exercice
        sans rien signaler. Une déclaration de taxes s'ouvrait donc sur douze
        mois au lieu du mois à déclarer, un rapport trimestriel sur l'année.

        Le contrôle porte sur la liste elle-même : si Odoo en ajoute une, le
        test la signale au lieu de la laisser tomber dans le repli.
        """
        modele = self.env["account.report"]
        offertes = [
            valeur for valeur, _ in
            modele._fields["default_opening_date_filter"].selection
        ]
        inconnues = [v for v in offertes if v not in modele.EXPODO_OPENING_PERIODS]
        self.assertEqual(
            inconnues, [],
            "Ces périodes déclarables ne sont pas traduites par le moteur et "
            "retombent sur l'exercice : %s" % ", ".join(inconnues))

        rapport = self.env.ref("expodo_account_reports.report_bilan_fr")
        repere = date(2026, 9, 15)
        for valeur in offertes:
            options = rapport._expodo_default_date_options(
                period=modele.EXPODO_OPENING_PERIODS[valeur], anchor=repere)
            self.assertLessEqual(
                options["date_from"], options["date_to"],
                "La période %s produit un intervalle à l'envers" % valeur)

    def test_the_tax_return_opens_on_the_vat_return(self):
        """Le menu doit ouvrir la déclaration de TVA, pas une autre.

        Plusieurs localisations livrent plusieurs déclarations. L'Espagne en
        livre six : le Mod 303 pour la TVA, le Mod 111 pour les retenues sur
        salaires, le Mod 115 pour les loyers. Retenir la première venue
        ouvrait le menu sur le Mod 111, une déclaration de retenues à la
        source, là où le comptable attend la TVA.

        Le contrôle ne nomme aucun formulaire : il vérifie la propriété qui
        départage, à savoir que la déclaration retenue est celle que les
        taxes de vente et d'achat de la société alimentent le plus.
        """
        rapports = self.env["account.report"]
        retenu = rapports.browse(rapports.expodo_resolve_tax_report())
        pays = self.env.company.account_fiscal_country_id \
            or self.env.company.country_id
        generique = self.env.ref("account.generic_tax_report",
                                 raise_if_not_found=False)
        candidats = rapports.search([("country_id", "=", pays.id)]).filtered(
            lambda r: generique and r.root_report_id == generique)
        if len(candidats) < 2:
            self.assertTrue(retenu, "Une déclaration doit être désignée")
            return

        taxes = self.env["account.tax"].search([
            ("type_tax_use", "in", ("sale", "purchase"))])
        portees = set(taxes.repartition_line_ids.tag_ids.ids)

        def recouvrement(rapport):
            expressions = rapport.line_ids.expression_ids.filtered(
                lambda e: e.engine == "tax_tags")
            if not expressions:
                return 0
            return len(set(expressions._get_matching_tags().ids) & portees)

        meilleur = max(recouvrement(c) for c in candidats)
        self.assertEqual(
            recouvrement(retenu), meilleur,
            "La déclaration ouverte (%s) n'est pas celle que les taxes de la "
            "société alimentent le plus" % retenu.name)

    def test_the_tax_return_opens_on_the_period_it_declares(self):
        """Une déclaration s'ouvre sur la période qu'elle doit déclarer.

        Les localisations déclarent `this_return_period` ou
        `previous_return_period` sur leur déclaration de taxes. Ces deux
        valeurs n'étaient pas reconnues et retombaient sur l'exercice : une
        société française ouvrait sa CA3 sur les douze mois de l'année, et le
        total à payer affiché n'était celui d'aucune déclaration réellement
        déposable.

        En septembre on déclare août, d'où la période précédente. La
        périodicité vient du type de déclaration ; à défaut, le mois.
        """
        rapport = self.env["account.report"].browse(
            self.env["account.report"].expodo_resolve_tax_report())
        if rapport.default_opening_date_filter not in (
                "this_return_period", "previous_return_period"):
            self.skipTest("La déclaration de ce pays ne vise pas une période de dépôt")

        aujourdhui = date(2026, 9, 15)
        options = rapport._expodo_default_date_options(anchor=aujourdhui)
        self.assertEqual(
            (options["date_from"], options["date_to"]),
            (date(2026, 8, 1), date(2026, 8, 31)),
            "Par défaut la déclaration mensuelle doit s'ouvrir sur le mois "
            "précédent, or elle s'ouvre du %s au %s"
            % (options["date_from"], options["date_to"]))

        self.env["expodo.return.type"].create({
            "name": "Déclaration trimestrielle du test",
            "periodicity": "quarterly",
            "report_id": rapport.id,
        })
        options = rapport._expodo_default_date_options(anchor=aujourdhui)
        self.assertEqual(
            (options["date_from"], options["date_to"]),
            (date(2026, 4, 1), date(2026, 6, 30)),
            "Avec un dépôt trimestriel, la déclaration doit s'ouvrir sur le "
            "trimestre précédent")

    def test_every_accounting_role_can_open_every_report(self):
        """Le menu et le contenu doivent s'accorder.

        Les menus sont offerts aux trois roles comptables d'Odoo, comme en
        edition Enterprise. Mais les modeles du module n'etaient ouverts qu'au
        comptable et au gestionnaire : un auditeur en lecture seule, ou un
        utilisateur basique, voyait les quatorze entrees de menu et se heurtait
        a un refus d'acces sur dix-neuf rapports sur vingt-deux. Un seul modele
        bloquait, l'exercice fiscal, que le calcul de periode lit pour presque
        tous les etats.

        Voir un menu et ne pas pouvoir l'ouvrir est pire que ne pas le voir :
        l'integrateur croit son installation cassee.

        Le test tient aussi l'autre bout : un employe sans droit comptable ne
        doit rien pouvoir lire, les points d'entree etant appelables par RPC.
        """
        rapports = self.env["account.report"].search([
            ("id", "in", [
                self.env.ref("expodo_account_reports." + xmlid).id
                for xmlid in (
                    "report_bilan_fr", "report_resultat_fr", "report_sig_fr",
                    "report_balance_fr", "report_grand_livre_fr",
                    "report_auxiliaire_fr", "report_journaux_fr",
                    "report_open_items_fr", "report_aged_receivable_fr",
                    "report_aged_payable_fr", "report_livre_banque_fr",
                    "report_livre_caisse_fr", "report_day_book",
                    "report_cash_flow", "report_executive_summary",
                )
            ]),
        ])

        def utilisateur(nom, groupe):
            compte = self.env["res.users"].create({
                "name": nom, "login": "%s_droits" % nom})
            compte.group_ids = [Command.set([
                self.env.ref("base.group_user").id,
                self.env.ref(groupe).id,
            ])]
            return compte

        for groupe in ("account.group_account_readonly",
                       "account.group_account_basic",
                       "account.group_account_user",
                       "account.group_account_manager"):
            compte = utilisateur(groupe.split("_")[-1], groupe)
            for rapport in rapports:
                try:
                    rapport.with_user(compte).expodo_get_report_data(None)
                except AccessError as erreur:
                    raise AssertionError(
                        "%s ne peut pas ouvrir %s : %s"
                        % (groupe, rapport.display_name, erreur)) from erreur

        # Ouvrir ne suffit pas : le depliage, l'audit et l'export lisent
        # d'autres modeles. Les annotations, notamment, sont relues a chaque
        # export, et leur absence de droit en lecture faisait echouer les
        # vingt-deux exports pour ces deux roles.
        for groupe in ("account.group_account_readonly",
                       "account.group_account_basic"):
            compte = utilisateur("parcours_" + groupe.split("_")[-1], groupe)
            for xmlid in ("report_bilan_fr", "report_balance_fr",
                          "report_grand_livre_fr"):
                rapport = self.env.ref(
                    "expodo_account_reports." + xmlid).with_user(compte)
                try:
                    donnees = rapport.expodo_get_report_data(None)
                    options = donnees["options"]
                    for ligne in donnees["lines"]:
                        if not ligne.get("unfoldable"):
                            continue
                        sous = rapport.expodo_expand_line(ligne["line_id"], options)
                        if sous:
                            rapport.expodo_action_audit(
                                ligne["line_id"], options, sous[0]["group"],
                                "main", sous[0]["columns"][0]["label"])
                        break
                    rapport._expodo_export_xlsx(rapport._expodo_get_options(None))
                except AccessError as erreur:
                    raise AssertionError(
                        "%s ne peut pas parcourir %s : %s"
                        % (groupe, xmlid, erreur)) from erreur

        simple = self.env["res.users"].create({
            "name": "Sans comptabilite", "login": "sans_compta_droits"})
        simple.group_ids = [Command.set([self.env.ref("base.group_user").id])]
        with self.assertRaises(
            AccessError,
            msg="Un employe sans droit comptable ne doit pas lire le bilan",
        ):
            rapports[0].with_user(simple).expodo_get_report_data(None)

    def test_a_click_on_a_figure_opens_exactly_that_figure(self):
        """Les écritures ouvertes doivent totaliser le montant cliqué.

        Le domaine d'audit ne considérait que les expressions du moteur
        `domain`. Le bilan et le compte de résultat français, bâtis sur des
        préfixes de comptes, n'en portent aucune : la recherche revenait vide,
        la portée de date retombait sur la période et la restriction de
        comptes disparaissait. Cliquer « Banque 163 220 » ouvrait six
        écritures totalisant −24 720 ; cliquer « Créances » ouvrait le grand
        livre entier.

        C'est le défaut le plus grave que puisse porter un état d'audit : il
        fait douter de tous les autres chiffres, y compris des justes. Le
        contrôle balaie donc les seize états, lignes et sous-lignes, plutôt
        qu'un cas choisi.

        La comparaison porte sur la valeur absolue : une formule préfixée d'un
        signe moins affiche l'opposé du solde qu'elle rassemble, et c'est la
        convention attendue, pas un écart.
        """
        # Le test pose ses propres écritures, sur les deux exercices.
        #
        # Il lisait les écritures déjà présentes dans la base de
        # développement, dont l'exercice antérieur est mouvementé. Sur une
        # base fraîchement installée, la colonne comparée était vide partout :
        # le balayage ne rencontrait aucun montant à vérifier et le test
        # échouait faute de matière, sans qu'aucun défaut existe.
        self._invoice("out_invoice", self.customer, 1200.0, None, "AUD1")
        self._invoice("in_invoice", self.supplier, 550.0, None, "AUD2")
        self._invoice("out_invoice", self.customer, 800.0, None, "AUD3",
                      day="2025-06-10")
        self._invoice("in_invoice", self.supplier, 300.0, None, "AUD4",
                      day="2025-09-20")

        noms = [
            "report_balance_sheet", "report_profit_loss", "report_bilan_fr",
            "report_resultat_fr", "report_sig_fr", "report_bilan_ohada",
            "report_resultat_ohada", "report_cash_flow", "report_cash_flow_direct",
            "report_balance_fr", "report_grand_livre_fr", "report_auxiliaire_fr",
            "report_open_items_fr", "report_day_book", "report_livre_banque_fr",
            "report_livre_caisse_fr",
        ]
        lignes_modele = self.env["account.move.line"]
        controles = 0

        def verifie(rapport, ligne_id, groupe, cellule, intitule):
            montant = cellule["raw"].get("main")
            if not isinstance(montant, (int, float)) or abs(montant) < 0.005:
                return 0
            action = rapport.expodo_action_audit(
                ligne_id, options, groupe, "main", "balance")
            total = sum(lignes_modele.search(action["domain"]).mapped("balance"))
            self.assertAlmostEqual(
                abs(total), abs(montant), places=2,
                msg="%s : la cellule annonce %s, les écritures ouvertes %s"
                    % (intitule, montant, total))
            return 1

        for nom in noms:
            rapport = self.env.ref("expodo_account_reports." + nom)
            donnees = rapport.expodo_get_report_data({"date": self.period})
            options = donnees["options"]
            for ligne in donnees["lines"]:
                for cellule in ligne["columns"]:
                    if cellule["label"] != "balance" or not cellule.get("auditable"):
                        continue
                    controles += verifie(
                        rapport, ligne["line_id"], None, cellule,
                        "%s / %s" % (nom, ligne["name"]))
                if not ligne.get("unfoldable"):
                    continue
                for sous in rapport.expodo_expand_line(ligne["line_id"], options)[:6]:
                    for cellule in sous["columns"]:
                        if cellule["label"] != "balance" or not cellule.get("auditable"):
                            continue
                        controles += verifie(
                            rapport, ligne["line_id"], sous["group"], cellule,
                            "%s / %s" % (nom, sous["name"]))

        # La colonne comparée doit ouvrir les écritures de **sa** période.
        # Le domaine d'audit reçoit la clé du groupe de colonnes ; s'il
        # l'ignorait, un clic sur l'exercice précédent ouvrirait les écritures
        # de l'exercice courant, ce qui ne se voit pas : les deux listes se
        # ressemblent.
        compares = 0
        for nom in ("report_bilan_fr", "report_resultat_fr", "report_balance_fr"):
            rapport = self.env.ref("expodo_account_reports." + nom)
            depart = rapport.expodo_get_report_data({"date": self.period})["options"]
            avec = dict(depart)
            avec["comparison"] = {"filter": "same_last_year", "number_period": 1}
            donnees = rapport.expodo_get_report_data(avec)
            options = donnees["options"]
            cles = list(options["column_groups"].keys())
            if len(cles) < 2:
                continue
            comparee = cles[1]
            for ligne in donnees["lines"]:
                if not ligne.get("unfoldable"):
                    continue
                for sous in rapport.expodo_expand_line(ligne["line_id"], options):
                    for cellule in sous["columns"]:
                        if cellule["label"] != "balance" or not cellule.get("auditable"):
                            continue
                        montant = cellule["raw"].get(comparee)
                        if not isinstance(montant, (int, float)) or abs(montant) < 0.005:
                            continue
                        action = rapport.expodo_action_audit(
                            ligne["line_id"], options, sous["group"], comparee, "balance")
                        total = sum(
                            lignes_modele.search(action["domain"]).mapped("balance"))
                        compares += 1
                        self.assertAlmostEqual(
                            abs(total), abs(montant), places=2,
                            msg="%s / %s : la colonne comparée annonce %s, les "
                                "écritures ouvertes %s"
                                % (nom, sous["name"], montant, total))
        self.assertGreater(
            compares, 5,
            "Le balayage doit rencontrer des montants dans la colonne comparée")

        self.assertGreater(
            controles, 30,
            "Le balayage doit rencontrer des montants auditables ; s'il n'en "
            "trouve plus, c'est que les états ont cessé d'être auditables")

    def test_every_unfolded_line_sums_to_its_total(self):
        """Le détail d'une ligne doit valoir la ligne, partout.

        C'est la seule chose qu'un dépliage ne doit jamais rater. Un lecteur
        qui déplie « Créances 76 500 » et trouve 74 300 sous les comptes cesse
        de croire au reste de l'état, et il a raison.

        Le contrôle balaie les neuf états structurés et leurs deux moteurs —
        préfixes de comptes et domaines — plutôt qu'une ligne choisie : les
        écarts de ce genre n'apparaissent pas là où on les cherche.
        """
        noms = [
            "report_bilan_fr", "report_resultat_fr", "report_sig_fr",
            "report_cash_flow", "report_cash_flow_direct",
            "report_bilan_ohada", "report_resultat_ohada",
            "report_balance_sheet", "report_profit_loss",
        ]
        controles = 0
        for nom in noms:
            rapport = self.env.ref("expodo_account_reports." + nom)
            donnees = rapport.expodo_get_report_data({"date": self.period})
            options = donnees["options"]
            for ligne in donnees["lines"]:
                if not ligne.get("unfoldable"):
                    continue
                sous_lignes = rapport.expodo_expand_line(ligne["line_id"], options)
                for rang, colonne in enumerate(ligne["columns"]):
                    total = colonne["raw"].get("main") or 0.0
                    detail = sum(
                        (sous["columns"][rang]["raw"].get("main") or 0.0)
                        for sous in sous_lignes)
                    controles += 1
                    self.assertAlmostEqual(
                        detail, total, places=2,
                        msg="%s : la ligne %s annonce %s, son détail %s"
                            % (nom, ligne["name"], total, detail))
        self.assertGreater(
            controles, 50,
            "Le balayage doit rencontrer des lignes dépliables ; s'il n'en "
            "trouve plus, c'est que les états ont cessé de se déplier")

    def test_a_prefix_line_reads_the_code_of_another_selected_company(self):
        """Le code d'un compte dépend de la société qui le regarde.

        `account.account.code` est calculé pour la société active. Dans un
        périmètre à deux sociétés, un compte codé chez la filiale mais pas chez
        la société active renvoyait un code vide — et un code vide ne
        correspond à aucun préfixe. Le compte et tout ce qu'il portait
        disparaissaient du bilan et du compte de résultat, sans ligne d'écart
        pour le dire : les deux états restaient équilibrés entre eux, seulement
        amputés.

        Découvert par l'écart de 4 200 entre le résultat net du tableau de flux,
        qui lit les types de comptes, et celui du compte de résultat français,
        qui lit les codes. L'édition Enterprise retombe, elle, sur le code que
        le compte porte dans l'une de ses propres sociétés.
        """
        filiale = self.env["res.company"].create({"name": "Filiale du test"})
        compte = self.env["account.account"].create({
            "name": "Vente chez la filiale",
            "account_type": "income",
            "company_ids": [Command.set([filiale.id])],
            "code": "707777",
        })
        self.assertFalse(
            compte.with_company(self.env.company).code,
            "Le compte ne doit pas porter de code chez la société active")

        infos = self.env["account.report"]._expodo_account_info(compte)
        self.assertEqual(
            infos[compte.id][0], "707777",
            "Le moteur doit retomber sur le code porté chez la filiale")

    def test_detail_columns_stay_empty_on_a_mixed_group(self):
        """Une ligne agrégée n'affiche un tiers que s'il est le seul.

        Le grand livre porte une colonne Tiers et une colonne Date. Sur la
        ligne d'un compte, le moteur en prenait le minimum : le compte
        fournisseurs, qui en porte trois, s'affichait au nom de celui dont
        l'identifiant était le plus petit.

        Un compte collectif lu comme un compte individuel fausse tout ce qu'on
        en déduit — et rien à l'écran ne signalait que la valeur était
        arbitraire. La règle est donc l'aveu d'ignorance : la valeur s'affiche
        quand elle est certaine, la cellule reste vide quand elle varie.
        """
        journal = self.env["account.journal"].search([("type", "=", "purchase")], limit=1)
        compte = self.env["account.account"].search([
            ("account_type", "=", "liability_payable")], limit=1)
        contrepartie = self.env["account.account"].search([
            ("account_type", "=", "expense")], limit=1)
        if not (journal and compte and contrepartie):
            self.skipTest("Le plan comptable ne permet pas ce test")

        premier, second = self.env["res.partner"].create([
            {"name": "Fournisseur un"}, {"name": "Fournisseur deux"}])
        for tiers, jour in ((premier, date(2026, 3, 1)), (second, date(2026, 4, 1))):
            self.env["account.move"].create({
                "journal_id": journal.id,
                "date": jour,
                "line_ids": [
                    Command.create({"name": "achat", "account_id": contrepartie.id,
                                    "partner_id": tiers.id,
                                    "debit": 100.0, "credit": 0.0}),
                    Command.create({"name": "achat", "account_id": compte.id,
                                    "partner_id": tiers.id,
                                    "debit": 0.0, "credit": 100.0}),
                ],
            }).action_post()

        rapport = self.env.ref("expodo_account_reports.report_grand_livre_fr")
        options = rapport._expodo_get_options({
            "date": {"mode": "range", "filter": "custom",
                     "date_from": date(2026, 1, 1), "date_to": date(2026, 12, 31)}})
        donnees, lignes = rapport._expodo_export_rows(options, limite=100000)

        etiquettes = [c.get("name") for c in donnees.get("columns", [])]
        rangs = {nom: i for i, nom in enumerate(etiquettes)}
        self.assertIn("Partner", rangs, "Le grand livre doit porter une colonne Tiers")

        cible = None
        for ligne in lignes:
            if (ligne.get("name") or "").startswith(compte.code):
                cible = ligne
                break
        self.assertIsNotNone(cible, "Le compte fournisseurs doit figurer à l'état")

        cellule = (cible.get("columns") or [])[rangs["Partner"]]
        brut = cellule.get("raw")
        valeur = brut.get("main") if isinstance(brut, dict) else brut
        self.assertFalse(
            valeur,
            "Un compte portant deux tiers ne doit en afficher aucun, or il "
            "affiche %s" % valeur)

    def test_detail_columns_are_kept_when_the_group_agrees(self):
        """Laisser vide ce qui varie ne doit pas effacer ce qui est certain.

        La correction précédente se satisferait d'une colonne toujours vide.
        Ce test tient l'autre bout : un groupe dont toutes les écritures
        portent le même tiers doit continuer de l'afficher.
        """
        journal = self.env["account.journal"].search([("type", "=", "purchase")], limit=1)
        compte = self.env["account.account"].search([
            ("account_type", "=", "liability_payable")], limit=1)
        contrepartie = self.env["account.account"].search([
            ("account_type", "=", "expense")], limit=1)
        if not (journal and compte and contrepartie):
            self.skipTest("Le plan comptable ne permet pas ce test")

        unique = self.env["res.partner"].create({"name": "Fournisseur unique"})
        self.env["account.move"].create({
            "journal_id": journal.id,
            "date": date(2027, 5, 4),
            "line_ids": [
                Command.create({"name": "achat", "account_id": contrepartie.id,
                                "partner_id": unique.id, "debit": 60.0, "credit": 0.0}),
                Command.create({"name": "achat", "account_id": compte.id,
                                "partner_id": unique.id, "debit": 0.0, "credit": 60.0}),
            ],
        }).action_post()

        rapport = self.env.ref("expodo_account_reports.report_grand_livre_fr")
        options = rapport._expodo_get_options({
            "date": {"mode": "range", "filter": "custom",
                     "date_from": date(2027, 1, 1), "date_to": date(2027, 12, 31)}})
        donnees, lignes = rapport._expodo_export_rows(options, limite=100000)
        rangs = {c.get("name"): i for i, c in enumerate(donnees.get("columns", []))}

        for ligne in lignes:
            if not (ligne.get("name") or "").startswith(compte.code):
                continue
            cellule = (ligne.get("columns") or [])[rangs["Partner"]]
            brut = cellule.get("raw")
            valeur = brut.get("main") if isinstance(brut, dict) else brut
            self.assertEqual(
                valeur, unique.id,
                "Un groupe homogène doit continuer d'afficher son tiers")
            return
        self.skipTest("Le compte n'apparaît pas sur cette période")


@tagged("post_install", "-at_install")
class TestBalanceGeneraleOuverture(TransactionCase):
    """Solde d'ouverture des comptes de gestion dans la balance générale.

    Un compte de gestion est soldé à chaque clôture : son solde d'ouverture
    repart donc du début de l'exercice, jamais de l'origine des écritures.
    Le défaut est invisible sur la ligne de contrôle — le total restait nul —
    et n'apparaît qu'en lisant compte par compte.

    Trouvé pendant le portage en 20.0, par comparaison avec Enterprise sur un
    jeu d'écritures identique, et présent à l'identique en 19.0.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        societe = cls.env.company
        comptes = cls.env["account.account"]
        cls.client_ = comptes.search([("account_type", "=", "asset_receivable"),
                                      ("company_ids", "in", societe.id)], limit=1)
        cls.vente = comptes.search([("account_type", "=", "income"),
                                    ("company_ids", "in", societe.id)], limit=1)
        cls.journal = cls.env["account.journal"].search(
            [("type", "=", "general"), ("company_id", "=", societe.id)], limit=1)
        cls.rapport = cls.env.ref("expodo_account_reports.report_balance_fr")

    def _vente(self, jour, montant):
        ecriture = self.env["account.move"].create({
            "journal_id": self.journal.id, "date": jour, "ref": "OUVERTURE",
            "line_ids": [
                Command.create({"name": "V", "account_id": self.client_.id,
                                "debit": montant, "credit": 0.0}),
                Command.create({"name": "V", "account_id": self.vente.id,
                                "debit": 0.0, "credit": montant}),
            ],
        })
        ecriture.action_post()

    def _balance(self, debut, fin):
        options = self.rapport._expodo_get_options({"date": {
            "mode": "range", "filter": "custom", "date_from": debut, "date_to": fin}})
        ligne = self.env.ref("expodo_account_reports.line_balance_fr")
        lignes = {r["group_id"]: r["values"]
                  for r in ligne._expodo_expand(self.rapport, options, "main")}
        return self.rapport._expodo_compute_values(options, "main"), lignes

    def test_un_compte_de_gestion_repart_du_debut_de_l_exercice(self):
        """Trois ventes, trois exercices, une seule compte dans l'ouverture.

        La vente de 2031 appartient à un exercice clos : elle est soldée et ne
        doit pas peser sur l'ouverture de septembre 2032. Seule celle de
        février 2032 le doit. Le compte de tiers, lui, reste cumulatif : son
        ouverture porte les trois.
        """
        _t, avant = self._balance(date(2032, 9, 1), date(2032, 9, 30))
        self._vente(date(2031, 6, 1), 1000.0)
        self._vente(date(2032, 2, 1), 300.0)
        self._vente(date(2032, 9, 10), 50.0)
        _t, apres = self._balance(date(2032, 9, 1), date(2032, 9, 30))

        vide = {"initial": 0.0, "balance": 0.0}
        v0, v1 = avant.get(self.vente.id, vide), apres.get(self.vente.id)
        self.assertTrue(v1, "Le compte de vente doit figurer à la balance")
        self.assertAlmostEqual(
            v1["initial"] - v0["initial"], -300.0, places=2,
            msg="L'ouverture ne doit retenir que l'exercice en cours, pas les "
                "1 000 de l'exercice clos")
        self.assertAlmostEqual(
            v1["balance"] - v0["balance"], -350.0, places=2,
            msg="Le solde de clôture porte l'exercice entier")

        c1 = apres.get(self.client_.id)
        self.assertAlmostEqual(
            c1["initial"] - c1["balance"], -50.0, places=2,
            msg="Un compte de bilan reste cumulatif depuis l'origine")

    def test_le_resultat_anterieur_garde_la_balance_equilibree(self):
        """Ce que l'ouverture retire aux comptes de gestion doit reparaître.

        Sans ligne de résultat antérieur, remettre les comptes de gestion à
        zéro en début d'exercice déséquilibrerait la balance du résultat des
        exercices précédents.
        """
        avant, _l = self._balance(date(2032, 9, 1), date(2032, 9, 30))
        self._vente(date(2031, 6, 1), 1000.0)
        apres, _l = self._balance(date(2032, 9, 1), date(2032, 9, 30))
        for etiquette in ("initial", "balance"):
            self.assertAlmostEqual(
                apres[("BAL_ANTERIEUR", etiquette)]
                - avant.get(("BAL_ANTERIEUR", etiquette), 0.0),
                -1000.0, places=2,
                msg="Le résultat de 2031 doit se retrouver sur sa propre ligne")
            self.assertAlmostEqual(
                apres[("BAL_TOTAL", etiquette)], 0.0, places=2,
                msg="La balance doit rester équilibrée")




@tagged("post_install", "-at_install")
class TestGrandsLivresSoldeFinal(TransactionCase):
    """Solde des grands livres : solde final, et non mouvement de la période.

    Constaté en comparant avec Enterprise (port 20.0, grand livre et grand
    livre des tiers sur 2026) : notre colonne « Solde » portait le seul
    mouvement de la période (client : 20 000) quand Enterprise porte le solde
    final (38 000). Un compte ou un tiers sans mouvement dans la période,
    mais avec un solde, disparaissait de l'état.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        societe = cls.env.company
        comptes = cls.env["account.account"]
        cls.client_ = comptes.search([("account_type", "=", "asset_receivable"),
                                      ("company_ids", "in", societe.id)], limit=1)
        cls.vente = comptes.search([("account_type", "=", "income"),
                                    ("company_ids", "in", societe.id)], limit=1)
        cls.journal = cls.env["account.journal"].search(
            [("type", "=", "general"), ("company_id", "=", societe.id)], limit=1)
        cls.tiers = cls.env["res.partner"].create({"name": "Tiers solde final"})
        cls.dormant = cls.env["res.partner"].create({"name": "Tiers sans mouvement"})

    def _vente(self, jour, montant, tiers):
        ecriture = self.env["account.move"].create({
            "journal_id": self.journal.id, "date": jour, "ref": "SOLDE",
            "line_ids": [
                Command.create({"name": "V", "account_id": self.client_.id,
                                "partner_id": tiers.id, "debit": montant, "credit": 0.0}),
                Command.create({"name": "V", "account_id": self.vente.id,
                                "debit": 0.0, "credit": montant}),
            ],
        })
        ecriture.action_post()
        return ecriture

    def _lignes(self, xmlid_rapport, xmlid_ligne, champ_cle, cle):
        rapport = self.env.ref("expodo_account_reports." + xmlid_rapport)
        ligne = self.env.ref("expodo_account_reports." + xmlid_ligne)
        options = rapport._expodo_get_options({"date": {
            "mode": "range", "filter": "custom",
            "date_from": date(2032, 3, 1), "date_to": date(2032, 3, 31)}})
        premier = {r["group_id"]: r for r in ligne._expodo_expand(rapport, options, "main")}
        detail = []
        if cle in premier:
            detail = ligne._expodo_expand(
                rapport, options, "main", level=1,
                parent_domain=[(champ_cle, "=", cle)])
        return premier, detail

    def test_le_grand_livre_des_tiers_porte_le_solde_final(self):
        self._vente(date(2031, 6, 1), 1000.0, self.tiers)
        self._vente(date(2032, 3, 10), 200.0, self.tiers)
        self._vente(date(2031, 7, 1), 300.0, self.dormant)
        premier, detail = self._lignes("report_auxiliaire_fr", "line_aux_fr",
                                       "partner_id", self.tiers.id)
        tiers = premier[self.tiers.id]["values"]
        self.assertAlmostEqual(tiers["debit"], 200.0, places=2)
        self.assertAlmostEqual(tiers["initial"], 1000.0, places=2)
        self.assertAlmostEqual(tiers["balance"], 1200.0, places=2,
                               msg="Le solde est le solde final du tiers")
        self.assertIn(self.dormant.id, premier,
                      "Un tiers avec un solde mais sans mouvement doit figurer")
        self.assertEqual(len(detail), 1,
                         "Le détail ne liste que les écritures de la période")

    def test_le_grand_livre_porte_le_solde_final_du_compte(self):
        avant, _d = self._lignes("report_grand_livre_fr", "line_gl_fr",
                                 "account_id", self.client_.id)
        a = avant.get(self.client_.id, {"values": {"initial": 0.0, "balance": 0.0}})["values"]
        self._vente(date(2031, 6, 1), 1000.0, self.tiers)
        self._vente(date(2032, 3, 10), 200.0, self.tiers)
        apres, detail = self._lignes("report_grand_livre_fr", "line_gl_fr",
                                     "account_id", self.client_.id)
        c = apres[self.client_.id]["values"]
        self.assertAlmostEqual(c["initial"] - a["initial"], 1000.0, places=2)
        self.assertAlmostEqual(c["balance"] - a["balance"], 1200.0, places=2)
        self.assertAlmostEqual(c["balance"], c["initial"] + c["debit"] - c["credit"], places=2)
        self.assertTrue(detail)
        self.assertTrue(all(str(r["values"].get("line_date"))[:7] == "2032-03" for r in detail),
                        "Le détail ne liste que des écritures datées de la période : %s"
                        % [r["values"].get("line_date") for r in detail])


@tagged("post_install", "-at_install")
class TestLignesMasqueesSiNulles(TransactionCase):
    """`hide_if_zero` : la ligne et ses filles s'effacent quand tout est nul.

    Le champ était posé sur plusieurs lignes (résultat antérieur, résultats
    affectés) mais aucun code ne le lisait : les lignes nulles s'affichaient
    toujours. Constaté au port 20.0, en vérifiant l'écran du bilan.
    """

    def _codes(self, annee):
        rapport = self.env.ref("expodo_account_reports.report_balance_fr")
        donnees = rapport.expodo_get_report_data({"date": {
            "mode": "range", "filter": "custom",
            "date_from": date(annee, 3, 1), "date_to": date(annee, 3, 31)}})
        return {ligne["code"] for ligne in donnees["lines"]}

    def test_une_ligne_nulle_marquee_s_efface_et_reparait_des_qu_elle_porte_un_montant(self):
        societe = self.env.company
        comptes = self.env["account.account"]
        client = comptes.search([("account_type", "=", "asset_receivable"),
                                 ("company_ids", "in", societe.id)], limit=1)
        vente = comptes.search([("account_type", "=", "income"),
                                ("company_ids", "in", societe.id)], limit=1)
        journal = self.env["account.journal"].search(
            [("type", "=", "general"), ("company_id", "=", societe.id)], limit=1)
        # Aucun résultat avant 2011 dans une base de test : la ligne est nulle.
        self.assertNotIn("BAL_ANTERIEUR", self._codes(2011))
        ecriture = self.env["account.move"].create({
            "journal_id": journal.id, "date": date(2010, 6, 1),
            "line_ids": [
                Command.create({"name": "V", "account_id": client.id, "debit": 100.0}),
                Command.create({"name": "V", "account_id": vente.id, "credit": 100.0}),
            ],
        })
        ecriture.action_post()
        self.assertIn("BAL_ANTERIEUR", self._codes(2011))




@tagged("post_install", "-at_install")
class TestFluxDeTresorerieAffectation(TransactionCase):
    """Une affectation de résultat ne produit aucun flux de trésorerie.

    Constaté en comparant avec Enterprise (port 20.0) : Odoo étiquette le
    compte de résultat non affecté (999999) « Investing & Extraordinary
    Activities ». Nos lignes d'éléments sans effet de trésorerie retenaient les
    comptes de cette étiquette : l'affectation du résultat 2025 ajoutait 8 600
    aux flux d'exploitation et les retirait des flux d'investissement. Le total
    restait juste, la ventilation non.
    """

    def test_l_affectation_ne_deplace_rien_entre_les_flux(self):
        societe = self.env.company
        comptes = self.env["account.account"]

        def compte(domaine):
            return comptes.search(domaine + [("company_ids", "in", societe.id)],
                                  limit=1, order="code")

        client = compte([("account_type", "=", "asset_receivable")])
        vente = compte([("account_type", "=", "income")])
        reserves = compte([("account_type", "=", "equity")])
        non_affecte = compte([("account_type", "=", "equity_unaffected")])
        journal = self.env["account.journal"].search(
            [("type", "=", "general"), ("company_id", "=", societe.id)], limit=1)
        rapport = self.env.ref("expodo_account_reports.report_cash_flow")

        def valeurs():
            options = rapport._expodo_get_options({"date": {
                "mode": "range", "filter": "custom",
                "date_from": date(2032, 1, 1), "date_to": date(2032, 12, 31)}})
            return rapport._expodo_compute_values(options, "main")

        def ecriture(jour, debit, credit):
            self.env["account.move"].create({
                "journal_id": journal.id, "date": jour,
                "line_ids": [
                    Command.create({"name": "A", "account_id": debit.id, "debit": 1000.0}),
                    Command.create({"name": "A", "account_id": credit.id, "credit": 1000.0}),
                ],
            }).action_post()

        avant = valeurs()
        ecriture(date(2031, 6, 1), client, vente)
        ecriture(date(2032, 5, 31), non_affecte, reserves)
        apres = valeurs()
        for code in ("CF_OPERATING", "CF_INVESTING", "CF_FINANCING"):
            self.assertAlmostEqual(
                apres[(code, "balance")] - avant[(code, "balance")], 0.0, places=2,
                msg="%s ne doit pas bouger : une affectation n'est pas un flux" % code)

    def test_les_capitaux_de_la_synthese_integrent_l_affectation(self):
        """Même cause, synthèse de direction : le compte non affecté était
        compté dans les capitaux, et masquait la hausse des réserves."""
        societe = self.env.company
        comptes = self.env["account.account"]
        reserves = comptes.search([("account_type", "=", "equity"),
                                   ("company_ids", "in", societe.id)], limit=1, order="code")
        non_affecte = comptes.search([("account_type", "=", "equity_unaffected"),
                                      ("company_ids", "in", societe.id)], limit=1)
        journal = self.env["account.journal"].search(
            [("type", "=", "general"), ("company_id", "=", societe.id)], limit=1)
        rapport = self.env.ref("expodo_account_reports.report_executive_summary")

        def capitaux():
            options = rapport._expodo_get_options({"date": {
                "mode": "range", "filter": "custom",
                "date_from": date(2032, 1, 1), "date_to": date(2032, 12, 31)}})
            return rapport._expodo_compute_values(options, "main")[("EXEC_CAPITAUX", "balance")]

        avant = capitaux()
        self.env["account.move"].create({
            "journal_id": journal.id, "date": date(2032, 5, 31),
            "line_ids": [
                Command.create({"name": "A", "account_id": non_affecte.id, "debit": 1000.0}),
                Command.create({"name": "A", "account_id": reserves.id, "credit": 1000.0}),
            ],
        }).action_post()
        self.assertAlmostEqual(capitaux() - avant, 1000.0, places=2)

    def test_une_dotation_aux_amortissements_est_un_element_sans_effet_de_tresorerie(self):
        """Une dotation n'est pas un décaissement.

        Le plan comptable français type la dotation 6811 en charge ordinaire
        (`expense_other`), pas en `expense_depreciation` : elle n'était donc pas
        réintégrée aux flux d'exploitation, et la variation de l'amortissement
        cumulé (2818) apparaissait en encaissement d'investissement. Constaté
        au port 20.0 : exploitation 37 600 contre 40 000 chez Enterprise.
        """
        societe = self.env.company
        comptes = self.env["account.account"]
        charge = comptes.search([("code", "=like", "6811%"), ("account_type", "!=", "expense_depreciation"),
                                 ("company_ids", "in", societe.id)], limit=1)
        amortissement = comptes.search([("code", "=like", "281%"),
                                        ("company_ids", "in", societe.id)], limit=1)
        if not charge or not amortissement:
            self.skipTest("Cas du plan comptable français (6811 non typé dotation)")
        journal = self.env["account.journal"].search(
            [("type", "=", "general"), ("company_id", "=", societe.id)], limit=1)
        rapport = self.env.ref("expodo_account_reports.report_cash_flow")

        def valeurs():
            options = rapport._expodo_get_options({"date": {
                "mode": "range", "filter": "custom",
                "date_from": date(2032, 1, 1), "date_to": date(2032, 12, 31)}})
            return rapport._expodo_compute_values(options, "main")

        avant = valeurs()
        self.env["account.move"].create({
            "journal_id": journal.id, "date": date(2032, 12, 31),
            "line_ids": [
                Command.create({"name": "Dotation", "account_id": charge.id, "debit": 1000.0}),
                Command.create({"name": "Dotation", "account_id": amortissement.id, "credit": 1000.0}),
            ],
        }).action_post()
        apres = valeurs()
        for code in ("CF_OPERATING", "CF_INVESTING", "CF_FINANCING"):
            self.assertAlmostEqual(
                apres[(code, "balance")] - avant[(code, "balance")], 0.0, places=2,
                msg="%s ne doit pas bouger : une dotation n'est pas un flux" % code)


@tagged("post_install", "-at_install")
class TestBorneDExport(TransactionCase):
    """Le plafond d'export doit tenir, y compris sur les lignes de tête.

    Il n'était vérifié qu'en descendant dans les lignes dépliées : les lignes
    de premier niveau s'ajoutaient sans contrôle. Un bilan sortait ses
    vingt-deux rubriques avec un plafond de trois, et le garde-fou censé
    protéger la mémoire et le temps de rendu ne protégeait rien.
    """

    def test_l_export_ne_depasse_jamais_son_plafond(self):
        rapport = self.env.ref("expodo_account_reports.report_bilan_fr")
        options = rapport._expodo_serialize_options(
            rapport._expodo_get_options({"date": {
                "mode": "range", "filter": "custom",
                "date_from": date(2026, 1, 1), "date_to": date(2026, 12, 31)}}))
        for plafond in (3, 5, 10, 25):
            _donnees, lignes = rapport._expodo_export_rows(options, limite=plafond)
            self.assertLessEqual(
                len(lignes), plafond + 1, msg=
                "Avec un plafond de %d, l'export a produit %d lignes. La "
                "ligne d'avertissement de troncature est seule tolérée "
                "au-delà." % (plafond, len(lignes)))
            self.assertTrue(
                any(l.get("id") == "expodo_truncated" for l in lignes),
                "Un export tronqué doit le dire au lecteur")


@tagged("post_install", "-at_install")
class TestJetonsDeDomaine(TransactionCase):
    """Trois états ont besoin de données que le domaine ne peut pas nommer.

    Une formule de domaine est lue par ``literal_eval``, qui refuse tout appel
    de fonction : c'est voulu, un domaine ne doit pas pouvoir exécuter de code.
    Elle ne peut donc désigner ni le pays de la société, ni les comptes de ses
    journaux, connus seulement à l'exécution. Des jetons littéraux, remplacés
    après lecture, comblent le manque sans rouvrir la porte.

    Trois défauts trouvés au port 20.0 en comparant avec Enterprise, vérifiés
    ici avant correction.
    """

    def setUp(self):
        super().setUp()
        self.societe = self.env.company
        self.journal_general = self.env["account.journal"].search(
            [("type", "=", "general"), ("company_id", "=", self.societe.id)], limit=1)

    def _compte(self, domaine):
        return self.env["account.account"].search(
            domaine + [("company_ids", "in", self.societe.id)], limit=1, order="code")

    def _ecrire(self, journal, jour, lignes):
        self.env["account.move"].create({
            "journal_id": journal.id, "date": jour,
            "line_ids": [Command.create(l) for l in lignes],
        }).action_post()

    def _valeurs(self, xmlid, d1, d2):
        rapport = self.env.ref("expodo_account_reports." + xmlid)
        options = rapport._expodo_get_options({"date": {
            "mode": "range", "filter": "custom", "date_from": d1, "date_to": d2}})
        return rapport._expodo_compute_values(options, "main")

    def test_la_liste_des_ventes_ue_exclut_le_pays_de_la_societe(self):
        """Une déclaration intracommunautaire recense les *autres* États.

        Retenir tout client d'un pays membre y faisait entrer les clients
        nationaux, qui en forment l'essentiel : la déclaration transmise à
        l'administration était fausse d'un montant considérable.
        """
        pays = self.societe.account_fiscal_country_id or self.societe.country_id
        if pays.code != "FR":
            self.skipTest("Scénario écrit pour une société française")
        taxe = self.env["account.tax"].search(
            [("type_tax_use", "=", "sale"), ("company_id", "=", self.societe.id)], limit=1)
        if not taxe:
            self.skipTest("Aucune taxe de vente dans cette base")

        rapport = self.env.ref("expodo_account_reports.report_ec_sales_list")
        ligne = rapport.line_ids[0]

        def total():
            options = rapport._expodo_get_options({"date": {
                "mode": "range", "filter": "custom",
                "date_from": date(2032, 4, 1), "date_to": date(2032, 4, 30)}})
            lignes = ligne._expodo_expand(rapport, options, "main")
            return (round(sum((x["values"].get("balance") or 0.0) for x in lignes), 2),
                    {x["name"] for x in lignes})

        avant, _noms = total()
        francais = self.env["res.partner"].create(
            {"name": "Client national", "country_id": self.env.ref("base.fr").id})
        allemand = self.env["res.partner"].create(
            {"name": "Client allemand", "country_id": self.env.ref("base.de").id})
        for tiers, montant in ((francais, 100.0), (allemand, 2000.0)):
            self.env["account.move"].create({
                "move_type": "out_invoice", "partner_id": tiers.id,
                "invoice_date": date(2032, 4, 10), "date": date(2032, 4, 10),
                "invoice_line_ids": [Command.create({
                    "name": "UE", "quantity": 1, "price_unit": montant,
                    "tax_ids": [Command.set(taxe.ids)]})]}).action_post()

        apres, noms = total()
        self.assertAlmostEqual(
            apres - avant, 2000.0, places=2,
            msg="Seul le client allemand relève de la liste intracommunautaire")
        self.assertNotIn(
            francais.display_name, noms,
            "Un client du pays de la société n'a rien à faire dans la liste")

    def test_le_livre_de_caisse_ne_retient_que_le_compte_de_caisse(self):
        """Un versement vers la banque n'est pas une entrée de caisse.

        L'état retenait toute ligne d'un compte de trésorerie passée au
        journal : un versement de la caisse vers la banque comptait son côté
        banque en entrée, et le total des entrées était faux du montant
        versé.
        """
        journaux = self.env["account.journal"]
        banque = journaux.search(
            [("type", "=", "bank"), ("company_id", "=", self.societe.id)], limit=1)
        caisse = journaux.search(
            [("type", "=", "cash"), ("company_id", "=", self.societe.id)], limit=1)
        if not banque:
            self.skipTest("Aucun journal de banque dans cette base")
        if not caisse:
            # La V20 ne crée plus de journal de caisse par défaut ; la V19 non
            # plus sur toutes les localisations.
            caisse = journaux.create({
                "name": "Caisse", "type": "cash", "code": "CSH19",
                "company_id": self.societe.id})

        rapport = self.env.ref("expodo_account_reports.report_livre_caisse_fr")
        ligne = rapport.line_ids[0]

        def mouvements():
            options = rapport._expodo_get_options({"date": {
                "mode": "range", "filter": "custom",
                "date_from": date(2032, 5, 1), "date_to": date(2032, 5, 31)}})
            lignes = ligne._expodo_expand(rapport, options, "main")
            return {k: round(sum((x["values"].get(k) or 0.0) for x in lignes), 2)
                    for k in ("debit", "credit")}

        avant = mouvements()
        self._ecrire(caisse, date(2032, 5, 12), [
            {"name": "versement", "account_id": banque.default_account_id.id, "debit": 50.0},
            {"name": "versement", "account_id": caisse.default_account_id.id, "credit": 50.0}])
        apres = mouvements()
        self.assertAlmostEqual(
            apres["debit"] - avant["debit"], 0.0, places=2,
            msg="Le côté banque du versement n'est pas une entrée de caisse")
        self.assertAlmostEqual(
            apres["credit"] - avant["credit"], 50.0, places=2,
            msg="Seule la sortie de caisse doit être retenue")

    def test_les_flux_directs_ne_comptent_que_ce_qui_passe_par_la_caisse(self):
        """La méthode directe ne connaît que l'encaissé et le décaissé.

        Les lignes additionnaient tous les mouvements des comptes étiquetés :
        un achat resté impayé réduisait l'exploitation sans qu'un euro soit
        sorti, et l'encaissement d'un client, dont le compte 411 ne porte
        aucune étiquette, tombait en « mouvements non classés ». C'est
        précisément la différence avec la méthode indirecte qui disparaissait.
        """
        banque = self.env["account.journal"].search(
            [("type", "=", "bank"), ("company_id", "=", self.societe.id)], limit=1)
        if not banque:
            self.skipTest("Aucun journal de banque dans cette base")
        achats = self.env["account.journal"].search(
            [("type", "=", "purchase"), ("company_id", "=", self.societe.id)],
            limit=1) or self.journal_general

        codes = ("CFD_OPERATING", "CFD_UNCLASSIFIED", "CFD_NET_CHANGE", "CFD_ACTUAL")
        avant = self._valeurs("report_cash_flow_direct",
                              date(2032, 6, 1), date(2032, 6, 30))

        self._ecrire(achats, date(2032, 6, 5), [
            {"name": "achat non payé",
             "account_id": self._compte([("account_type", "=", "expense")]).id,
             "debit": 700.0},
            {"name": "achat non payé",
             "account_id": self._compte([("account_type", "=", "liability_payable")]).id,
             "credit": 700.0}])
        self._ecrire(banque, date(2032, 6, 20), [
            {"name": "encaissement", "account_id": banque.default_account_id.id,
             "debit": 300.0},
            {"name": "encaissement",
             "account_id": self._compte([("account_type", "=", "asset_receivable")]).id,
             "credit": 300.0}])

        apres = self._valeurs("report_cash_flow_direct",
                              date(2032, 6, 1), date(2032, 6, 30))
        ecart = {c: round(apres.get((c, "balance"), 0.0)
                          - avant.get((c, "balance"), 0.0), 2) for c in codes}
        self.assertAlmostEqual(
            ecart["CFD_OPERATING"], 300.0, places=2,
            msg="Seul l'encaissement est un flux d'exploitation ; l'achat "
                "impayé n'a rien décaissé")
        self.assertAlmostEqual(
            ecart["CFD_UNCLASSIFIED"], 0.0, places=2,
            msg="L'encaissement client doit être classé, pas laissé de côté")
        self.assertAlmostEqual(
            ecart["CFD_NET_CHANGE"], 300.0, places=2,
            msg="La variation de trésorerie est celle des comptes de trésorerie")


@tagged("post_install", "-at_install")
class TestBesoinEnFondsDeRoulement(TransactionCase):
    """Le besoin en fonds de roulement du résumé général.

    Le besoin en fonds de roulement mesure ce que le cycle d'exploitation
    immobilise : ce que les clients doivent, plus les stocks, moins ce qui
    reste dû aux tiers. Une taxe collectée est encaissée du client et reversée
    à l'État : elle gonfle la créance et la dette du même montant, et ne pèse
    donc pas sur le besoin.

    L'état retenait la créance sans la dette. Chaque vente gonflait le besoin
    du montant de la taxe, et la position nette de trésorerie, qui s'en
    déduit, paraissait d'autant plus mauvaise.
    """

    def setUp(self):
        super().setUp()
        self.societe = self.env.company
        self.journal = self.env["account.journal"].search(
            [("type", "=", "general"), ("company_id", "=", self.societe.id)], limit=1)

    def _compte(self, type_compte):
        return self.env["account.account"].search(
            [("account_type", "=", type_compte),
             ("company_ids", "in", self.societe.id)], limit=1, order="code")

    def _valeurs(self):
        rapport = self.env.ref(
            "expodo_account_reports.report_executive_summary")
        options = rapport._expodo_get_options({"date": {
            "mode": "range", "filter": "custom",
            "date_from": date(2032, 7, 1), "date_to": date(2032, 7, 31)}})
        return rapport._expodo_compute_values(options, "main")

    def test_la_taxe_collectee_ne_gonfle_pas_le_besoin_en_fonds_de_roulement(self):
        """Une vente de 1 200 dont 200 de taxe immobilise 1 000, pas 1 200."""
        avant = self._valeurs()
        self.env["account.move"].create({
            "journal_id": self.journal.id, "date": date(2032, 7, 15),
            "line_ids": [
                Command.create({
                    "name": "vente",
                    "account_id": self._compte("asset_receivable").id,
                    "debit": 1200.0}),
                Command.create({
                    "name": "vente",
                    "account_id": self._compte("income").id,
                    "credit": 1000.0}),
                Command.create({
                    "name": "taxe collectée",
                    "account_id": self._compte("liability_current").id,
                    "credit": 200.0}),
            ]}).action_post()
        apres = self._valeurs()
        ecart = apres.get(("EXEC_BFR", "balance"), 0.0) - avant.get(
            ("EXEC_BFR", "balance"), 0.0)
        self.assertAlmostEqual(
            ecart, 1000.0, places=2,
            msg="La taxe collectée est due à l'État : elle ne fait pas partie "
                "de ce que le cycle d'exploitation immobilise")

    def test_les_autres_dettes_sont_une_composante_du_besoin(self):
        """La ligne doit exister, et se lire depuis l'origine comme les autres.

        Un solde de bilan lu sur la seule période afficherait sa variation.
        L'erreur reste invisible sur une base montée sur un seul exercice.
        """
        rapport = self.env.ref(
            "expodo_account_reports.report_executive_summary")
        ligne = rapport.line_ids.filtered(
            lambda l: l.code == "EXEC_AUTRES_DETTES")
        self.assertTrue(
            ligne, "Le besoin en fonds de roulement doit montrer les autres "
                   "dettes d'exploitation, pas seulement les fournisseurs")
        for expression in ligne.expression_ids:
            self.assertEqual(expression.date_scope, "from_beginning")
        bfr = rapport.line_ids.filtered(lambda l: l.code == "EXEC_BFR")
        self.assertIn(
            "EXEC_AUTRES_DETTES", bfr.expression_ids.mapped("formula")[0],
            "Les autres dettes doivent être retranchées du besoin")


@tagged("post_install", "-at_install")
class TestPositionNetteDeTresorerie(TransactionCase):
    """La ligne qui répond à « puis-je payer mes fournisseurs ce mois-ci ».

    Elle retranchait le besoin en fonds de roulement des disponibilités.
    Or le besoin est déjà un solde net : ce que les tiers doivent moins ce
    qu'on leur doit. Le retrancher inversait le signe des deux composantes.
    Une facture fournisseur impayée améliorait la position, une créance
    client la dégradait, c'est-à-dire l'inverse de ce que le dirigeant qui
    lit cette ligne a besoin de savoir.
    """

    def setUp(self):
        super().setUp()
        self.societe = self.env.company
        self.journal = self.env["account.journal"].search(
            [("type", "=", "general"), ("company_id", "=", self.societe.id)], limit=1)

    def _compte(self, type_compte):
        return self.env["account.account"].search(
            [("account_type", "=", type_compte),
             ("company_ids", "in", self.societe.id)], limit=1, order="code")

    def _ecrire(self, jour, compte_debit, compte_credit, montant):
        self.env["account.move"].create({
            "journal_id": self.journal.id, "date": jour,
            "line_ids": [
                Command.create({"name": "x", "account_id": compte_debit.id,
                                "debit": montant}),
                Command.create({"name": "x", "account_id": compte_credit.id,
                                "credit": montant}),
            ]}).action_post()

    def _valeurs(self):
        rapport = self.env.ref(
            "expodo_account_reports.report_executive_summary")
        options = rapport._expodo_get_options({"date": {
            "mode": "range", "filter": "custom",
            "date_from": date(2032, 8, 1), "date_to": date(2032, 8, 31)}})
        return rapport._expodo_compute_values(options, "main")

    def _position(self, valeurs):
        return valeurs.get(("EXEC_POSITION_NETTE", "balance"), 0.0)

    def test_une_dette_fournisseur_degrade_la_position(self):
        """Devoir mille de plus ne peut pas améliorer la position."""
        depart = self._position(self._valeurs())
        self._ecrire(date(2032, 8, 10), self._compte("expense"),
                     self._compte("liability_payable"), 1000.0)
        apres = self._position(self._valeurs())
        self.assertAlmostEqual(
            apres - depart, -1000.0, places=2,
            msg="Une facture fournisseur non payée pèse sur la capacité à "
                "payer, elle ne l'améliore pas")

    def test_une_creance_client_compense_une_dette_de_meme_montant(self):
        """Devoir mille et se voir devoir mille laisse la position inchangée."""
        depart = self._position(self._valeurs())
        self._ecrire(date(2032, 8, 10), self._compte("expense"),
                     self._compte("liability_payable"), 1000.0)
        self._ecrire(date(2032, 8, 11), self._compte("asset_receivable"),
                     self._compte("income"), 1000.0)
        apres = self._position(self._valeurs())
        self.assertAlmostEqual(
            apres - depart, 0.0, places=2,
            msg="Une créance et une dette de même montant se compensent")

    def test_la_position_est_l_actif_circulant_net(self):
        """Identité de lecture : ce qui est disponible ou le deviendra,
        moins ce qui est dû à court terme."""
        v = self._valeurs()
        attendu = (v.get(("EXEC_DISPO", "balance"), 0.0)
                   + v.get(("EXEC_CREANCES", "balance"), 0.0)
                   + v.get(("EXEC_STOCKS", "balance"), 0.0)
                   - v.get(("EXEC_DETTES", "balance"), 0.0)
                   - v.get(("EXEC_AUTRES_DETTES", "balance"), 0.0))
        self.assertAlmostEqual(
            self._position(v), attendu, places=2,
            msg="Disponibilités plus besoin en fonds de roulement, et non "
                "moins : le besoin est déjà un solde net")


@tagged("post_install", "-at_install")
class TestMasquageDesLignesNulles(TransactionCase):
    """Le filtre que deux états annonçaient sans l'offrir.

    ``filter_hide_0_lines`` était posé sur le relevé client et sur le relevé
    intracommunautaire, et lu nulle part. Les deux états promettaient de
    masquer les lignes à zéro et montraient chaque tiers sans mouvement.
    C'est le même défaut que ``hide_if_zero``, sur un autre champ : une
    déclaration que le moteur ignore.
    """

    def _rapport(self):
        return self.env.ref("expodo_account_reports.report_ec_sales_list")

    def test_l_etat_declare_le_filtre(self):
        self.assertIn(
            self._rapport().filter_hide_0_lines, ("optional", "by_default"),
            "Le relevé intracommunautaire annonce ce filtre")

    def test_le_filtre_arrive_dans_les_options(self):
        rapport = self._rapport()
        options = rapport._expodo_get_options({})
        self.assertIn(
            "hide_0_lines", options,
            "Un filtre absent des options ne peut être ni lu ni transmis")

    def test_le_navigateur_recoit_de_quoi_afficher_le_filtre(self):
        donnees = self._rapport().expodo_get_report_data({})
        self.assertIn("filter_hide_0_lines", donnees["report"])
        self.assertIn("filter_period_comparison", donnees["report"])

    def test_une_ligne_nulle_disparait_quand_le_filtre_est_actif(self):
        """Sur un état dont toutes les lignes sont à zéro, plus rien ne reste.

        La période est choisie loin des écritures de la base : ce qui est
        mesuré est l'effet du filtre, pas le contenu de la base.
        """
        rapport = self.env.ref(
            "expodo_account_reports.report_executive_summary")
        periode = {"date": {"mode": "range", "filter": "custom",
                            "date_from": date(2034, 1, 1),
                            "date_to": date(2034, 1, 31)}}
        sans = rapport.expodo_get_report_data(periode)
        avec = rapport.expodo_get_report_data(
            dict(periode, hide_0_lines=True))
        self.assertGreater(
            len(sans["lines"]), len(avec["lines"]),
            "Le filtre doit retirer des lignes, sinon il ne sert à rien")

    def test_le_filtre_ne_masque_pas_les_intitules(self):
        """Un titre de rubrique ne porte aucune valeur : le masquer ferait
        disparaître l'en-tête au-dessus de ses lignes."""
        rapport = self.env.ref(
            "expodo_account_reports.report_executive_summary")
        lignes = rapport.expodo_get_report_data({
            "date": {"mode": "range", "filter": "custom",
                     "date_from": date(2034, 1, 1), "date_to": date(2034, 1, 31)},
            "hide_0_lines": True})["lines"]
        codes = [l.get("code") for l in lignes]
        self.assertIn(
            "EXEC_ACTIVITE", codes,
            "Les intitulés de rubrique restent, ils ne portent pas de chiffre")


@tagged("post_install", "-at_install")
class TestEcrituresOuvertesAUneDatePassee(TransactionCase):
    """Ce qui était ouvert à la date d'arrêté, et non ce qui l'est aujourd'hui.

    Les écritures ouvertes et le relevé client retenaient les lignes non
    lettrées, sans regarder **quand** le lettrage a eu lieu. Une facture de
    décembre réglée en mars disparaissait donc de l'état arrêté au
    31 décembre, alors qu'elle y était bien due.

    L'état sert précisément à justifier le poste client d'un bilan arrêté :
    imprimé en mars, il ne correspondait plus au bilan de décembre, et l'écart
    grandissait à mesure que les règlements rentraient. Personne ne peut
    deviner cette cause en regardant l'écran : les deux états paraissent
    normaux, chacun de son côté.
    """

    def setUp(self):
        super().setUp()
        self.societe = self.env.company
        self.journal = self.env["account.journal"].search(
            [("type", "=", "general"), ("company_id", "=", self.societe.id)], limit=1)
        comptes = self.env["account.account"]
        self.client_ = comptes.search(
            [("account_type", "=", "asset_receivable"), ("reconcile", "=", True),
             ("company_ids", "in", self.societe.id)], limit=1, order="code")
        self.vente = comptes.search(
            [("account_type", "=", "income"),
             ("company_ids", "in", self.societe.id)], limit=1, order="code")
        self.banque = comptes.search(
            [("account_type", "=", "asset_cash"),
             ("company_ids", "in", self.societe.id)], limit=1, order="code")
        self.tiers = self.env["res.partner"].create({"name": "Client d'arrêté"})

    def _ecrire(self, jour, compte_debit, compte_credit, montant):
        ecriture = self.env["account.move"].create({
            "journal_id": self.journal.id, "date": jour,
            "line_ids": [
                Command.create({
                    "name": "x", "account_id": compte_debit.id,
                    "partner_id": self.tiers.id, "date_maturity": jour,
                    "debit": montant, "credit": 0.0}),
                Command.create({
                    "name": "x", "account_id": compte_credit.id,
                    "partner_id": self.tiers.id,
                    "debit": 0.0, "credit": montant}),
            ]})
        ecriture.action_post()
        return ecriture

    def _ouvertes(self, xmlid, code):
        rapport = self.env.ref("expodo_account_reports." + xmlid)
        options = rapport._expodo_get_options({"date": {
            "mode": "range", "filter": "custom",
            "date_from": date(2036, 1, 1), "date_to": date(2036, 12, 31)}})
        return round(rapport._expodo_compute_values(
            options, "main").get((code, "balance"), 0.0), 2)

    def _facture_reglee_l_annee_suivante(self):
        facture = self._ecrire(date(2036, 3, 1), self.client_, self.vente, 1000.0)
        reglement = self._ecrire(date(2037, 2, 1), self.banque, self.client_, 1000.0)
        lignes = (facture.line_ids + reglement.line_ids).filtered(
            lambda l: l.account_id == self.client_)
        lignes.reconcile()
        self.assertTrue(
            lignes[0].full_reconcile_id,
            "Le scénario suppose un lettrage effectif")

    def test_une_facture_lettree_plus_tard_reste_ouverte_a_l_arrete(self):
        avant = self._ouvertes("report_open_items_fr", "OPEN_PARTENAIRES")
        self._facture_reglee_l_annee_suivante()
        apres = self._ouvertes("report_open_items_fr", "OPEN_PARTENAIRES")
        self.assertAlmostEqual(
            apres - avant, 1000.0, places=2,
            msg="Au 31 décembre, cette facture était due : le règlement de "
                "février suivant ne peut pas l'effacer rétroactivement")

    def test_le_releve_client_suit_la_meme_regle(self):
        avant = self._ouvertes("report_customer_statement", "STATEMENT_CUSTOMERS")
        self._facture_reglee_l_annee_suivante()
        apres = self._ouvertes("report_customer_statement", "STATEMENT_CUSTOMERS")
        self.assertAlmostEqual(
            apres - avant, 1000.0, places=2,
            msg="Le relevé envoyé au client doit dire ce qu'il devait à la "
                "date d'arrêté")

    def test_les_ecritures_ouvertes_justifient_le_poste_client_du_bilan(self):
        """Les deux états doivent bouger du même montant.

        C'est la raison d'être de l'état : justifier le solde du bilan tiers
        par tiers. Deux écrans qui se contredisent sont pires qu'un écran
        manquant.
        """
        bilan = self.env.ref("expodo_account_reports.report_balance_sheet")

        def creances():
            options = bilan._expodo_get_options({"date": {
                "mode": "range", "filter": "custom",
                "date_from": date(2036, 1, 1), "date_to": date(2036, 12, 31)}})
            return round(bilan._expodo_compute_values(
                options, "main").get(("BS_RECEIVABLE", "balance"), 0.0), 2)

        creances_avant = creances()
        ouvertes_avant = self._ouvertes("report_open_items_fr", "OPEN_PARTENAIRES")
        self._facture_reglee_l_annee_suivante()
        self.assertAlmostEqual(
            creances() - creances_avant,
            self._ouvertes("report_open_items_fr", "OPEN_PARTENAIRES")
            - ouvertes_avant,
            places=2,
            msg="Le bilan et les écritures ouvertes doivent varier ensemble")

    def test_une_facture_lettree_avant_l_arrete_ne_figure_pas(self):
        """La correction ne doit pas faire réapparaître ce qui était soldé."""
        avant = self._ouvertes("report_open_items_fr", "OPEN_PARTENAIRES")
        facture = self._ecrire(date(2036, 3, 1), self.client_, self.vente, 700.0)
        reglement = self._ecrire(date(2036, 6, 1), self.banque, self.client_, 700.0)
        lignes = (facture.line_ids + reglement.line_ids).filtered(
            lambda l: l.account_id == self.client_)
        lignes.reconcile()
        self.assertAlmostEqual(
            self._ouvertes("report_open_items_fr", "OPEN_PARTENAIRES") - avant,
            0.0, places=2,
            msg="Réglée en juin, la facture n'est plus ouverte au 31 décembre")


@tagged("post_install", "-at_install")
class TestLibellesDesGroupes(TransactionCase):
    """Ce qu'un groupe affiche doit suffire à l'identifier.

    Deux défauts que seule une lecture d'écran révèle, et qu'aucun total
    ne trahit.
    """

    def _groupes(self, xmlid, code, annee=2026):
        rapport = self.env.ref("expodo_account_reports." + xmlid)
        donnees = rapport.expodo_get_report_data({"date": {
            "mode": "range", "filter": "custom",
            "date_from": date(annee, 1, 1), "date_to": date(annee, 12, 31)}})
        ligne = [l for l in donnees["lines"] if l.get("code") == code][0]
        enfants = rapport.expodo_expand_line(ligne["line_id"], donnees["options"])
        lignes = enfants.get("lines") if isinstance(enfants, dict) else enfants
        return [l.get("name") for l in lignes]

    def test_le_livre_journal_date_ses_groupes_dans_la_langue_du_lecteur(self):
        """Une date de groupe sortait telle qu'elle est stockée.

        Le livre-journal affichait « 2026-01-31 » à un lecteur français, dans
        un état dont la chronologie est la raison d'être. Les colonnes de date
        du même écran, elles, étaient bien formatées : deux dates du même
        tableau ne s'écrivaient pas de la même façon.
        """
        groupes = self._groupes("report_day_book", "DAYBOOK")
        if not groupes:
            self.skipTest("Aucune écriture sur la période")
        for nom in groupes:
            with self.subTest(groupe=nom):
                self.assertNotRegex(
                    nom or "", r"^\d{4}-\d{2}-\d{2}$",
                    "Une date affichée telle qu'elle est stockée n'est pas "
                    "une date lisible")

    def test_le_livre_journal_reste_chronologique(self):
        """La chronologie est la valeur probante de cet état.

        Le tri se faisait sur le libellé affiché. Tant que les dates
        sortaient au format de stockage, l'ordre alphabétique était l'ordre
        chronologique, et le défaut dormait. Formatées pour le lecteur, elles
        ne le sont plus : « 15/03 » passe après « 12/10 ». Un livre-journal
        désordonné n'est plus un livre-journal.
        """
        rapport = self.env.ref("expodo_account_reports.report_day_book")
        donnees = rapport.expodo_get_report_data({"date": {
            "mode": "range", "filter": "custom",
            "date_from": date(2026, 1, 1), "date_to": date(2026, 12, 31)}})
        ligne = [l for l in donnees["lines"]
                 if l.get("code") == "DAYBOOK"][0]
        enfants = rapport.expodo_expand_line(
            ligne["line_id"], donnees["options"])
        lignes = enfants.get("lines") if isinstance(enfants, dict) else enfants
        if len(lignes) < 2:
            self.skipTest("Moins de deux journées sur la période")
        cles = [l.get("group", {}).get("id") for l in lignes]
        cles = [str(c) for c in cles if c]
        self.assertEqual(
            cles, sorted(cles),
            "Les journées doivent se suivre dans l'ordre du calendrier, quel "
            "que soit le format d'affichage de la langue")

    def test_deux_journaux_homonymes_restent_distincts(self):
        """En multi-société, trois « Opérations diverses » se ressemblaient.

        Le libellé se réduisait au nom du journal. Sur une base qui en porte
        plusieurs, l'état des journaux alignait des lignes identiques, chacune
        avec des montants différents et rien pour dire laquelle est laquelle.
        """
        journaux = self.env["account.journal"].search([
            ("company_id", "in", self.env.companies.ids)])
        noms = journaux.mapped("name")
        homonymes = {nom for nom in noms if noms.count(nom) > 1}
        if not homonymes:
            self.skipTest("Aucun journal homonyme sur cette base")
        groupes = self._groupes("report_journaux_fr", "JRN_JOURNAUX")
        self.assertEqual(
            len(groupes), len(set(groupes)),
            "Deux lignes de l'état ne peuvent pas porter le même libellé : "
            "le lecteur n'a aucun moyen de les distinguer")

    def test_le_libelle_d_un_journal_porte_son_code(self):
        """Comme pour un compte, le code prime : c'est ainsi qu'on lit un
        journal."""
        groupes = self._groupes("report_journaux_fr", "JRN_JOURNAUX")
        if not groupes:
            self.skipTest("Aucune écriture sur la période")
        codes = self.env["account.journal"].search([
            ("company_id", "in", self.env.companies.ids)]).mapped("code")
        self.assertTrue(
            any(any((code or "") in (nom or "") for code in codes)
                for nom in groupes),
            "Aucun libellé ne porte le code de son journal")
