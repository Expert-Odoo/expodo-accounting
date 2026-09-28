# -*- coding: utf-8 -*-
# Copyright 2026 Expodo (https://expodo.fr)
# License LGPL-3
"""Matrice systématique : chaque rapport croisé avec chaque commande.

Les tests de `test_reports.py` vérifient des comportements choisis. Ceux-ci
vérifient une **couverture** : tout rapport du module, soumis à toute
combinaison de filtres, doit répondre sans erreur et de façon cohérente.

Motivation. Les défauts rencontrés sur ce module n'étaient pas des erreurs de
calcul — ils étaient aux **frontières** : entre le navigateur et le serveur
(options sérialisées réutilisées telles quelles), entre un rapport et un
autre (un moteur exercé par la CA3 mais pas par le bilan), entre un filtre et
un autre. Un test qui reste à l'intérieur d'une couche ne les voit pas.

Le coût d'une matrice est faible et sa couverture combinatoire élevée : douze
rapports par huit scénarios font près de cent vérifications pour une centaine
de lignes de code.
"""

import unittest
from datetime import date

from dateutil.relativedelta import relativedelta

from odoo import Command, fields
from odoo.tests import HttpCase, TransactionCase, tagged


@tagged("post_install", "-at_install")
class TestReportMatrix(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.partner = cls.env["res.partner"].create({"name": "Partenaire matrice"})
        tax = cls.env["account.tax"].search([
            ("type_tax_use", "=", "sale"), ("amount", "=", 20.0),
            ("tax_exigibility", "=", "on_invoice"),
        ], limit=1)

        # Un jeu minimal mais représentatif : une vente taxée, un achat,
        # un avoir. Assez pour que chaque rapport ait quelque chose à dire.
        for move_type, amount, ref in (
            ("out_invoice", 1000.0, "MATRICE-VENTE"),
            ("in_invoice", 400.0, "MATRICE-ACHAT"),
            ("out_refund", 150.0, "MATRICE-AVOIR"),
        ):
            move = cls.env["account.move"].create({
                "move_type": move_type,
                "partner_id": cls.partner.id,
                "invoice_date": "2026-05-20",
                "date": "2026-05-20",
                "ref": ref,
                "invoice_line_ids": [Command.create({
                    "name": ref, "quantity": 1, "price_unit": amount,
                    "tax_ids": [Command.set(tax.ids)] if move_type != "in_invoice" else [],
                })],
            })
            move.action_post()

        cls.year = {
            "mode": "range", "filter": "fiscalyear",
            "date_from": date(2026, 1, 1), "date_to": date(2026, 12, 31),
        }

    # ------------------------------------------------------------------

    def _module_reports(self):
        """Tous les rapports exposés par un menu, packs de localisation inclus.

        On part des menus et non des enregistrements : c'est ce que
        l'utilisateur peut réellement ouvrir. Un rapport défini mais sans
        menu n'a pas à être testé ; un menu pointant vers un rapport cassé
        doit l'être.
        """
        reports = self.env["account.report"]
        for action in self.env["ir.actions.client"].search([
            ("tag", "=", "expodo_account_report"),
        ]):
            context = action.context
            if isinstance(context, str):
                context = self.env["ir.actions.client"]._get_eval_context() and eval(context)
            report_id = (context or {}).get("report_id")
            if report_id:
                reports |= self.env["account.report"].browse(report_id).exists()
        return reports

    #: Les scénarios de filtres, appliqués à chaque rapport.
    def _scenarios(self):
        return {
            "période par défaut": {},
            "exercice complet": {"date": self.year},
            "mois sans écriture": {"date": {
                "mode": "range", "filter": "custom",
                "date_from": date(2026, 2, 1), "date_to": date(2026, 2, 28),
            }},
            "journée unique": {"date": {
                "mode": "range", "filter": "custom",
                "date_from": date(2026, 5, 20), "date_to": date(2026, 5, 20),
            }},
            "brouillons inclus": {"date": self.year, "all_entries": True},
            "comparaison période précédente": {
                "date": self.year,
                "comparison": {"filter": "previous_period", "number_period": 1},
            },
            "comparaison exercice précédent": {
                "date": self.year,
                "comparison": {"filter": "same_last_year", "number_period": 1},
            },
            "comparaison sur trois périodes": {
                "date": self.year,
                "comparison": {"filter": "same_last_year", "number_period": 3},
            },
        }

    # ------------------------------------------------------------------
    # La matrice
    # ------------------------------------------------------------------

    def test_every_report_under_every_scenario(self):
        """Tout rapport, sous tout scénario, doit répondre sans erreur."""
        reports = self._module_reports()
        self.assertTrue(reports, "Aucun rapport exposé : la matrice est vide")

        failures = []
        for report in reports:
            for label, options in self._scenarios().items():
                try:
                    report.expodo_get_report_data(dict(options))
                except Exception as error:  # noqa: BLE001 — on collecte tout
                    failures.append("%s / %s : %s: %s" % (
                        report.display_name, label,
                        type(error).__name__, error,
                    ))
        self.assertFalse(
            failures,
            "%d combinaisons en échec :\n  %s" % (len(failures), "\n  ".join(failures)),
        )

    def test_every_report_survives_an_options_round_trip(self):
        """Les options renvoyées doivent être réutilisables telles quelles.

        C'est le contrat implicite de l'interface : elle renvoie au serveur ce
        qu'il vient de lui donner. L'avoir rompu rendait **toute** commande
        inopérante, sur tous les rapports à la fois.
        """
        failures = []
        for report in self._module_reports():
            try:
                first = report.expodo_get_report_data({"date": self.year})
                report.expodo_get_report_data(first["options"])
            except Exception as error:  # noqa: BLE001
                failures.append("%s : %s: %s" % (
                    report.display_name, type(error).__name__, error))
        self.assertFalse(failures, "Aller-retour rompu :\n  " + "\n  ".join(failures))

    def test_every_unfoldable_line_expands(self):
        """Toute ligne annoncée dépliable doit se déplier sans erreur.

        Annoncer un dépliage impossible est pire que ne pas le proposer :
        l'utilisateur clique et obtient une erreur.
        """
        failures = []
        for report in self._module_reports():
            data = report.expodo_get_report_data({"date": self.year})
            for line in data["lines"]:
                if not line.get("unfoldable"):
                    continue
                try:
                    report.expodo_expand_line(line["line_id"], data["options"])
                except Exception as error:  # noqa: BLE001
                    failures.append("%s / %s : %s: %s" % (
                        report.display_name, line["name"],
                        type(error).__name__, error))
        self.assertFalse(failures, "Dépliages en échec :\n  " + "\n  ".join(failures))

    def test_every_auditable_cell_opens_its_entries(self):
        """Tout montant cliquable doit ouvrir les écritures qui le composent.

        L'invariant dépend de l'agrégat de la colonne, et c'est le point
        délicat :

        * une colonne de **solde** doit vérifier « somme des écritures =
          montant affiché » ;
        * une colonne de **nombre de lignes** doit vérifier « nombre
          d'écritures = valeur affichée ».

        Comparer une somme à un compteur n'a aucun sens. Une première version
        de ce test prenait la dernière colonne sans regarder sa nature et
        signalait six faux positifs : sur les Journaux et les Écritures
        ouvertes, la dernière colonne compte des pièces.
        """
        failures = []
        for report in self._module_reports():
            data = report.expodo_get_report_data({"date": self.year})

            # Nature de chaque colonne, lue sur les expressions du rapport.
            #
            # La clé porte la ligne **et** le libellé. Keyée sur le seul
            # libellé, la table s'écrasait : tous les états nomment leur
            # colonne « balance », si bien que la nature de la dernière
            # expression rencontrée s'appliquait à toutes les lignes. Sur le
            # tableau de flux, où la plupart des lignes portent `-sum` et la
            # dernière `sum`, le test comparait des montants inversés et
            # signalait cinq défauts qui n'en étaient pas.
            aggregates = {}
            for line in report.line_ids:
                for expression in line.expression_ids:
                    aggregates[(line.id, expression.label)] = (
                        expression.subformula or "sum")

            for line in data["lines"]:
                if not line.get("unfoldable"):
                    continue
                children = report.expodo_expand_line(line["line_id"], data["options"])
                for child in children[:3]:
                    for cell in child["columns"]:
                        if not cell.get("auditable"):
                            continue
                        displayed = cell["raw"].get("main")
                        aggregate = aggregates.get(
                            (line["line_id"], cell["label"]), "sum")
                        if displayed is None or aggregate not in (
                                "sum", "-sum", "count_rows"):
                            continue

                        # La colonne cliquée est transmise : sa portée de
                        # date détermine les écritures à ouvrir. Sans elle, une
                        # colonne cumulée ouvrirait les écritures de la seule
                        # période, et le test comparerait deux périodes
                        # différentes en croyant comparer deux calculs.
                        action = report.expodo_action_audit(
                            child["line_id"], data["options"],
                            group=child.get("group"),
                            expression_label=cell["label"],
                        )
                        lines = self.env["account.move.line"].search(action["domain"])

                        if aggregate == "count_rows":
                            actual, unit = float(len(lines)), "écritures"
                        else:
                            actual, unit = sum(lines.mapped("balance")), "solde"
                            # Une colonne `-sum` affiche l'opposé du solde :
                            # un flux de trésorerie présente une créance qui
                            # augmente comme une sortie. Les écritures
                            # ouvertes restent celles de la ligne, au signe
                            # comptable ; c'est ce lien qu'on vérifie.
                            if aggregate == "-sum":
                                actual = -actual

                        if abs(actual - displayed) > 0.01:
                            failures.append(
                                "%s / %s / colonne %s (%s) : affiché %.2f, %s %.2f"
                                % (report.display_name, child["name"], cell["label"],
                                   aggregate, displayed, unit, actual)
                            )
        self.assertFalse(failures, "Audits incohérents :\n  " + "\n  ".join(failures))

    def test_every_report_exports_in_both_formats(self):
        """Tout rapport doit s'exporter en PDF et en XLSX.

        Un export cassé sur un seul rapport passe inaperçu jusqu'à ce qu'un
        utilisateur en ait besoin — généralement en clôture.
        """
        failures = []
        for report in self._module_reports():
            options = report._expodo_serialize_options(
                report._expodo_get_options({"date": self.year})
            )
            try:
                blob = report._expodo_export_xlsx(options)
                self.assertGreater(len(blob), 500)
            except Exception as error:  # noqa: BLE001
                failures.append("%s / XLSX : %s: %s" % (
                    report.display_name, type(error).__name__, error))
            try:
                pdf = report._expodo_export_pdf(options)
                self.assertTrue(pdf.startswith(b"%PDF-"))
            except Exception as error:  # noqa: BLE001
                failures.append("%s / PDF : %s: %s" % (
                    report.display_name, type(error).__name__, error))
        self.assertFalse(failures, "Exports en échec :\n  " + "\n  ".join(failures))

    def test_every_report_is_denied_without_accounting_rights(self):
        """Aucun rapport ne doit être lisible sans droit comptable.

        Contrôlé sur l'ensemble : une exception oubliée sur un seul rapport
        suffit à exposer le bilan.
        """
        outsider = self.env["res.users"].create({
            "name": "Sans droits matrice", "login": "matrice_sans_droits",
            "group_ids": [Command.link(self.env.ref("base.group_user").id)],
        })
        leaks = []
        for report in self._module_reports():
            try:
                report.with_user(outsider).expodo_get_report_data({"date": self.year})
                leaks.append(report.display_name)
            except Exception:  # noqa: BLE001 — toute erreur vaut refus
                pass
        self.assertFalse(leaks, "Rapports accessibles sans droit : %s" % ", ".join(leaks))

    def test_totals_are_stable_across_equivalent_periods(self):
        """Une même période exprimée de deux façons doit donner le même total.

        « Exercice 2026 » et « du 1er janvier au 31 décembre 2026 » sont la
        même chose. Un écart signalerait que le filtre de période influence
        le calcul autrement que par ses bornes.
        """
        report = self.env.ref("expodo_account_reports.report_balance_fr")

        def total(date_options):
            options = report._expodo_get_options({"date": date_options})
            rows = report.line_ids[0]._expodo_expand(report, options, "main")
            return sum(row["values"].get("debit", 0.0) for row in rows)

        as_fiscalyear = total(self.year)
        as_custom = total({
            "mode": "range", "filter": "custom",
            "date_from": date(2026, 1, 1), "date_to": date(2026, 12, 31),
        })
        self.assertAlmostEqual(as_fiscalyear, as_custom, places=2)


@tagged("post_install", "-at_install")
class TestReportTour(HttpCase):
    """Parcours utilisateur dans un vrai navigateur.

    Les tests serveur ne voient pas le navigateur. Une suite complète et verte
    coexistait avec une interface dont **aucune commande** ne fonctionnait :
    filtres, recherche, dépliage, export. Ce test ferme cet angle mort.
    """

    @classmethod
    def setUpClass(cls):
        """Se saute proprement lorsqu'aucun serveur HTTP n'est disponible.

        `HttpCase` démarre son propre serveur ; si le port est déjà pris —
        typiquement une instance de développement qui tourne — la classe
        entière échoue au démarrage. Une erreur permanente dans la sortie des
        tests apprend à ignorer les erreurs, ce qui est pire que le test
        manquant.
        """
        try:
            super().setUpClass()
        except AttributeError as erreur:
            if "server_port" not in str(erreur):
                raise
            raise unittest.SkipTest(
                "Aucun serveur HTTP disponible : le parcours navigateur ne "
                "peut pas s'exécuter ici. Il tourne en intégration continue, "
                "où le port est libre."
            ) from erreur

    def test_les_routes_d_export_repondent_en_http(self):
        """Le PDF ne peut être prouvé qu'en requête HTTP réelle.

        Le rendu wkhtmltopdf échoue si le document référence une ressource
        externe : il tente de la récupérer, n'a pas le réseau, et sort en
        « ProtocolUnknownError ». Appelée depuis un shell, la même méthode
        réussit — la résolution d'URL y diffère. Le défaut n'apparaît donc que
        chez l'utilisateur, et aucun test appelant le Python directement ne
        peut l'attraper.

        Le contrôle porte sur les octets de tête plutôt que sur le type MIME :
        une page d'erreur renvoyée en 200 avec le bon en-tête resterait du
        HTML, et c'est précisément la forme que prend l'échec.
        """
        rapport = self.env.ref("expodo_account_reports.report_balance_sheet")
        # La route est en `auth="user"` : sans session, elle répond 303 vers la
        # page de connexion, donc du HTML, et le contrôle de signature échoue
        # en accusant l'export alors que seule l'authentification manquait.
        #
        # Un utilisateur créé pour l'occasion plutôt que `admin` : le mot de
        # passe de l'administrateur dépend de la base, et le test doit tourner
        # aussi bien ici qu'en intégration continue. Il porte les droits que
        # la route exige, ce qui vérifie du même coup que ces droits suffisent.
        mot_de_passe = "expodo_export_http"
        comptable = self.env["res.users"].create({
            "name": "Comptable export",
            "login": mot_de_passe,
            "password": mot_de_passe,
            "group_ids": [Command.set([
                self.env.ref("base.group_user").id,
                self.env.ref("account.group_account_manager").id,
            ])],
        })
        self.authenticate(comptable.login, mot_de_passe)
        for extension, signature in (("pdf", b"%PDF"), ("xlsx", b"PK\x03\x04")):
            reponse = self.url_open(
                "/expodo_account_reports/export/%s/%s?options={}"
                % (extension, rapport.id))
            self.assertEqual(
                reponse.status_code, 200,
                "La route d'export %s doit répondre" % extension)
            self.assertTrue(
                reponse.content.startswith(signature),
                "L'export %s ne commence pas par sa signature : %r"
                % (extension, reponse.content[:40]))

    def test_report_tour(self):
        partner = self.env["res.partner"].create({"name": "Client parcours"})
        # Les écritures sont datées du jour, jamais en dur.
        #
        # Le parcours ouvre la balance générale, qui s'affiche par défaut sur
        # le mois en cours. Des factures datées de mai 2026 tombaient hors
        # période dès juin : la ligne racine restait identique avec et sans
        # les brouillons, et l'étape qui vérifie ce filtre échouait pour une
        # raison qui n'avait rien à voir avec ce qu'elle teste.
        #
        # Le défaut est resté invisible tant que le parcours était sauté faute
        # de navigateur dans l'image.
        jour = fields.Date.to_string(fields.Date.context_today(self.env.user))
        for state, amount, day in (("posted", 1000.0, jour),
                                   ("draft", 500.0, jour)):
            move = self.env["account.move"].create({
                "move_type": "out_invoice",
                "partner_id": partner.id,
                "invoice_date": day, "date": day,
                "invoice_line_ids": [Command.create({
                    "name": "Parcours", "quantity": 1, "price_unit": amount,
                })],
            })
            if state == "posted":
                move.action_post()

        self.env.ref("base.user_admin").write({
            "group_ids": [Command.link(
                self.env.ref("account.group_account_manager").id
            )],
        })
        self.start_tour(
            "/odoo/action-expodo_account_reports.action_balance",
            "expodo_account_report_tour",
            login="admin",
        )


@tagged("post_install", "-at_install")
class TestExerciceNonCalendaire(TransactionCase):
    """L'exercice fiscal ne commence pas partout le 1er janvier.

    Plusieurs pays du Golfe, le Royaume-Uni, l'Inde, le Japon et l'Australie
    ouvrent leur exercice à une autre date. Un état qui suppose l'année civile
    y produirait des périodes fausses sans rien signaler — c'est le genre de
    défaut qui ne se voit qu'une fois le client installé.

    Le module ne calcule pas les bornes lui-même : il délègue à
    `compute_fiscalyear_dates` d'Odoo, qui lit le réglage de la société. Ce
    test vérifie que cette délégation tient, et surtout que le contrôle
    interne « écart avec le résultat du bilan » reste nul sur un exercice
    décalé — car c'est lui qui garantit que le résultat porté au bilan couvre
    la même période que le compte de résultat.
    """

    def _resultat_et_ecart(self, date_from, date_to):
        donnees = self.env["ir.model.data"]
        rapport = self.env["account.report"].browse(
            donnees._xmlid_to_res_id("expodo_account_reports.report_profit_loss"))
        options = rapport._expodo_get_options({})
        options["date"] = dict(
            options["date"], date_from=date_from, date_to=date_to, mode="range")
        _donnees, lignes = rapport._expodo_export_rows(options, limite=100000)

        resultat = ecart = None
        for ligne in lignes:
            colonne = (ligne.get("columns") or [{}])[0]
            brut = colonne.get("raw") or {}
            valeur = brut.get("main") if isinstance(brut, dict) else brut
            nom = (ligne.get("name") or "").strip().upper()
            if nom in ("NET RESULT", "RÉSULTAT NET", "RESULTAT NET"):
                resultat = valeur
            elif "GAP" in nom or "ÉCART" in nom or "ECART" in nom:
                ecart = valeur
        return resultat, ecart

    def test_exercice_clos_au_30_juin(self):
        societe = self.env.company
        societe.write({"fiscalyear_last_month": "6", "fiscalyear_last_day": 30})

        bornes = societe.compute_fiscalyear_dates(date(2026, 9, 18))
        self.assertEqual(
            (bornes["date_from"].month, bornes["date_from"].day), (7, 1),
            "Un exercice clos le 30 juin doit s'ouvrir le 1er juillet")
        self.assertEqual(
            (bornes["date_to"].month, bornes["date_to"].day), (6, 30),
            "et se clore le 30 juin")

        _resultat, ecart = self._resultat_et_ecart(
            str(bornes["date_from"]), str(bornes["date_to"]))
        if ecart is not None:
            self.assertAlmostEqual(
                ecart, 0.0, places=2,
                msg="Sur un exercice décalé, le résultat du compte de résultat "
                    "doit toujours coïncider avec celui porté au bilan")

    def test_le_filtre_exercice_suit_le_reglage_de_la_societe(self):
        societe = self.env.company
        societe.write({"fiscalyear_last_month": "3", "fiscalyear_last_day": 31})

        donnees = self.env["ir.model.data"]
        rapport = self.env["account.report"].browse(
            donnees._xmlid_to_res_id("expodo_account_reports.report_balance_sheet"))
        options = rapport._expodo_get_options({})

        if options["date"].get("filter") == "fiscalyear":
            self.assertEqual(
                options["date"]["date_from"].month, 4,
                "Un exercice clos le 31 mars doit ouvrir le filtre au 1er avril, "
                "et non au 1er janvier")


@tagged("post_install", "-at_install")
class TestBilanOhadaSurPlanMinimal(TransactionCase):
    """Le bilan SYSCOHADA, exercé sur un plan monté pour l'occasion.

    Les trois contrôles voisins ne tournent que si la base porte un plan
    SYSCOHADA. Aucune ne l'a jamais porté : ils sautaient tous, à toutes les
    exécutions, sur toutes les bases. Deux états étaient donc livrés sans avoir
    jamais été calculés une seule fois, et c'est ainsi que la classe 13 a pu
    n'être lue par aucune ligne du bilan pendant tout le développement.

    L'état ne connaît que des préfixes de comptes : une douzaine de comptes
    créés à la main aux bons préfixes l'exercent aussi fidèlement que le plan
    complet, et sans dépendre d'une localisation installée.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.societe = cls.env["res.company"].create({"name": "Société SYSCOHADA"})
        cls.env.user.company_ids = [Command.link(cls.societe.id)]
        cls.env = cls.env(context=dict(
            cls.env.context, allowed_company_ids=[cls.societe.id]))
        cls.journal = cls.env["account.journal"].create({
            "name": "Opérations diverses", "code": "ODS", "type": "general",
            "company_id": cls.societe.id,
        })
        cls.comptes = {}
        for code, nom, nature in (
                ("101000", "Capital social", "equity"),
                ("121000", "Report à nouveau créditeur", "equity"),
                ("131000", "Résultat net : bénéfice", "equity"),
                ("401000", "Fournisseurs", "liability_payable"),
                ("411000", "Clients", "asset_receivable"),
                ("521000", "Banques", "asset_cash"),
                ("601000", "Achats de marchandises", "expense"),
                ("701000", "Ventes de marchandises", "income"),
        ):
            cls.comptes[code[:3]] = cls.env["account.account"].create({
                "code": code, "name": nom, "account_type": nature,
                "company_ids": [Command.link(cls.societe.id)],
            })

    def _ecriture(self, jour, debit, credit, montant, libelle):
        ecriture = self.env["account.move"].create({
            "company_id": self.societe.id,
            "journal_id": self.journal.id,
            "date": jour,
            "ref": libelle,
            "line_ids": [
                Command.create({"name": libelle, "account_id": self.comptes[debit].id,
                                "debit": montant, "credit": 0.0}),
                Command.create({"name": libelle, "account_id": self.comptes[credit].id,
                                "debit": 0.0, "credit": montant}),
            ],
        })
        ecriture.action_post()
        return ecriture

    def _bilan(self, jour):
        rapport = self.env.ref("expodo_account_reports.report_bilan_ohada")
        exercice = self.societe.compute_fiscalyear_dates(jour)
        options = rapport._expodo_get_options({"date": {
            "mode": "range", "filter": "custom",
            "date_from": exercice["date_from"], "date_to": jour}})
        return rapport._expodo_compute_values(options, "main")

    def test_le_bilan_syscohada_s_equilibre_sur_un_plan_monte(self):
        exercice = self.societe.compute_fiscalyear_dates(date(2026, 6, 15))
        self._ecriture(exercice["date_from"], "521", "101", 30000.0, "Capital")
        self._ecriture(date(2026, 6, 15), "411", "701", 12000.0, "Vente")
        self._ecriture(date(2026, 6, 16), "601", "401", 4500.0, "Achat")

        valeurs = self._bilan(exercice["date_to"])
        self.assertAlmostEqual(
            valeurs[("OHADA_CONTROLE", "balance")], 0.0, places=2,
            msg="Actif et passif doivent coïncider")
        self.assertAlmostEqual(
            valeurs[("OHADA_RESULTAT", "balance")], 7500.0, places=2,
            msg="Le résultat net est la différence des classes 6 et 7")

    def test_le_resultat_porte_en_classe_13_reste_au_bilan(self):
        """Une fois l'exercice clôturé, le résultat quitte les classes 6 et 7.

        Il est porté en classe 13. Aucune ligne du bilan ne lisait cette
        classe : le bénéfice disparaissait du passif au lendemain de la
        clôture, et la ligne de contrôle s'allumait d'un montant égal au
        résultat. Le défaut ne pouvait pas se voir, faute d'un seul test
        atteignant cet état.
        """
        exercice = self.societe.compute_fiscalyear_dates(date(2026, 6, 15))
        self._ecriture(exercice["date_from"], "521", "101", 30000.0, "Capital")
        self._ecriture(date(2026, 6, 15), "411", "701", 12000.0, "Vente")
        self._ecriture(date(2026, 6, 16), "601", "401", 4500.0, "Achat")

        avant = self._bilan(exercice["date_to"])

        # Clôture : les comptes de gestion sont soldés, le résultat rejoint la
        # classe 13, à la date du dernier jour de l'exercice.
        self.env["account.move"].create({
            "company_id": self.societe.id,
            "journal_id": self.journal.id,
            "date": exercice["date_to"],
            "ref": "CLOTURE",
            "line_ids": [
                Command.create({"name": "Solde des ventes",
                                "account_id": self.comptes["701"].id,
                                "debit": 12000.0, "credit": 0.0}),
                Command.create({"name": "Solde des achats",
                                "account_id": self.comptes["601"].id,
                                "debit": 0.0, "credit": 4500.0}),
                Command.create({"name": "Résultat de l'exercice",
                                "account_id": self.comptes["131"].id,
                                "debit": 0.0, "credit": 7500.0}),
            ],
        }).action_post()

        apres = self._bilan(exercice["date_to"])
        self.assertAlmostEqual(
            apres[("OHADA_CONTROLE", "balance")], 0.0, places=2,
            msg="Le bilan doit rester équilibré après la clôture")
        self.assertAlmostEqual(
            apres[("OHADA_RESULTAT", "balance")],
            avant[("OHADA_RESULTAT", "balance")], places=2,
            msg="Le résultat ne doit pas disparaître en passant en classe 13")

    def test_le_compte_de_resultat_syscohada_se_recoupe(self):
        """La cascade des soldes intermédiaires doit retomber sur le résultat.

        Cet état non plus n'avait jamais été calculé sur des écritures : sa
        ligne de contrôle restait à zéro parce que tout l'état restait à zéro.
        """
        exercice = self.societe.compute_fiscalyear_dates(date(2026, 6, 15))
        self._ecriture(date(2026, 6, 15), "411", "701", 12000.0, "Vente")
        self._ecriture(date(2026, 6, 16), "601", "401", 4500.0, "Achat")

        rapport = self.env.ref("expodo_account_reports.report_resultat_ohada")
        options = rapport._expodo_get_options({"date": {
            "mode": "range", "filter": "custom",
            "date_from": exercice["date_from"], "date_to": exercice["date_to"]}})
        valeurs = rapport._expodo_compute_values(options, "main")
        self.assertAlmostEqual(
            valeurs[("OHADA_CONTROLE_RES", "balance")], 0.0, places=2,
            msg="Le résultat issu de la cascade doit égaler celui des classes "
                "6, 7 et 8")

    def test_le_resultat_anterieur_quitte_la_ligne_de_l_exercice(self):
        """Le bénéfice d'un exercice clos n'est pas celui de l'exercice suivant."""
        exercice = self.societe.compute_fiscalyear_dates(date(2026, 6, 15))
        lendemain = exercice["date_to"] + relativedelta(days=1)
        self._ecriture(exercice["date_from"], "521", "101", 30000.0, "Capital")
        self._ecriture(date(2026, 6, 15), "411", "701", 12000.0, "Vente")

        suivant = self._bilan(lendemain)
        self.assertAlmostEqual(
            suivant[("OHADA_RESULTAT", "balance")], 0.0, places=2,
            msg="L'exercice suivant s'ouvre sans résultat")
        self.assertAlmostEqual(
            suivant[("OHADA_REPORT", "balance")], 12000.0, places=2,
            msg="Le bénéfice de l'exercice clos revient au report à nouveau")
        self.assertAlmostEqual(
            suivant[("OHADA_CONTROLE", "balance")], 0.0, places=2)


class TestEtatsOhada(TransactionCase):
    """Les états SYSCOHADA doivent s'équilibrer et se recouper.

    Ces deux états sont déclaratifs : aucune ligne de Python ne les calcule.
    Leur mode de défaillance n'est donc pas l'exception mais le total
    silencieusement faux — un compte présent au plan mais absent de tous les
    regroupements disparaît de l'état sans que rien ne le signale.

    Deux garde-fous existent dans les états eux-mêmes : la ligne de contrôle
    d'équilibre du bilan, et l'écart entre le résultat de la cascade des
    soldes intermédiaires et le résultat calculé globalement. Ce test les
    exerce plutôt que de recalculer les postes un à un, car ce sont eux qui
    protègeront le jour où le plan SYSCOHADA évoluera.

    Ils ne tournent que si un plan SYSCOHADA est chargé : sur une base
    française ou allemande les classes 8 et 9 n'existent pas, et l'état n'a
    rien à dire.
    """

    def _plan_syscohada(self):
        """Vrai si le plan comptable de la base est un plan SYSCOHADA.

        On le reconnaît à la classe 8, réservée aux opérations hors activités
        ordinaires, qui n'existe ni au PCG ni dans les plans anglo-saxons.
        """
        return bool(self.env["account.account"].search_count([
            ("code", "=like", "8%")]))

    def _ligne(self, xmlid, code_ligne):
        rapport = self.env["account.report"].browse(
            self.env["ir.model.data"]._xmlid_to_res_id(xmlid))
        options = rapport._expodo_get_options({})
        _donnees, lignes = rapport._expodo_export_rows(options, limite=100000)
        for ligne in lignes:
            if (ligne.get("code") or "") == code_ligne:
                colonne = (ligne.get("columns") or [{}])[0]
                brut = colonne.get("raw")
                return brut.get("main") if isinstance(brut, dict) else brut
        return None

    def test_le_bilan_syscohada_est_equilibre(self):
        if not self._plan_syscohada():
            self.skipTest("Plan comptable non SYSCOHADA")
        ecart = self._ligne(
            "expodo_account_reports.report_bilan_ohada", "OHADA_CONTROLE")
        if ecart is not None:
            self.assertAlmostEqual(
                ecart, 0.0, places=2,
                msg="Actif et passif doivent coïncider. Un écart signale un "
                    "compte du plan absent des regroupements, ou un signe "
                    "oublié sur une ligne de passif")

    def test_le_resultat_syscohada_recoupe_le_bilan(self):
        if not self._plan_syscohada():
            self.skipTest("Plan comptable non SYSCOHADA")
        ecart = self._ligne(
            "expodo_account_reports.report_resultat_ohada", "OHADA_CONTROLE_RES")
        if ecart is not None:
            self.assertAlmostEqual(
                ecart, 0.0, places=2,
                msg="Le résultat issu de la cascade des soldes intermédiaires "
                    "doit égaler le résultat calculé sur les classes 6, 7 et 8")

    def test_les_etats_ohada_ne_sont_rattaches_a_aucun_pays(self):
        """Le SYSCOHADA est supranational : dix-sept États le partagent.

        Le rattacher à un pays ferait qu'il se substitue à l'état universel
        pour ce seul pays et reste invisible pour les seize autres. Le laisser
        sans pays le rend visible partout, au prix de ne pas se substituer
        automatiquement — compromis assumé et documenté dans l'en-tête des
        fichiers.
        """
        donnees = self.env["ir.model.data"]
        for xmlid in ("expodo_account_reports.report_bilan_ohada",
                      "expodo_account_reports.report_resultat_ohada"):
            rapport = self.env["account.report"].browse(
                donnees._xmlid_to_res_id(xmlid))
            self.assertFalse(
                rapport.country_id,
                "%s ne doit être rattaché à aucun pays : le SYSCOHADA couvre "
                "dix-sept États" % xmlid)
            self.assertTrue(
                rapport.root_report_id,
                "%s doit rester rattaché à l'état universel correspondant, "
                "faute de quoi il n'apparaît pas comme une variante" % xmlid)


@tagged("post_install", "-at_install")
class TestGroupementParGrillesDeTaxe(TransactionCase):
    """Une ligne groupée peut tirer ses montants de grilles de TVA.

    Régression, trouvée en éprouvant la localisation espagnole. Le moteur
    n'acceptait de grouper que des lignes portant une expression `domain`.
    Or 73 lignes du plan espagnol groupent par compte des montants issus du
    moteur `tax_tags` : les déclarations 111, 115, 303 et 420 refusaient de
    s'afficher, soit quatre sur huit.

    Le défaut venait d'une hypothèse jamais formulée — qu'une ligne groupée
    tire forcément ses montants d'un domaine. Rien ne l'impose : le
    groupement porte sur le résultat, pas sur le moteur qui le produit. Les
    deux premières localisations éprouvées, française et allemande, ne
    groupent aucune ligne à grilles, d'où l'angle mort.
    """

    def _lignes_groupees_a_grilles(self):
        """Lignes groupées dont aucune expression n'est de moteur `domain`."""
        trouvees = self.env["account.report.line"]
        for ligne in self.env["account.report.line"].search([("groupby", "!=", False)]):
            moteurs = set(ligne.expression_ids.mapped("engine"))
            if "domain" not in moteurs and "tax_tags" in moteurs:
                trouvees |= ligne
        return trouvees

    def test_une_ligne_a_grilles_se_groupe_sans_expression_domain(self):
        lignes = self._lignes_groupees_a_grilles()
        if not lignes:
            self.skipTest("Aucune ligne groupée à grilles dans cette localisation")

        ligne = lignes[0]
        rapport = ligne.report_id
        options = rapport._expodo_get_options({})
        # L'appel doit aboutir. Avant le correctif il levait une ValidationError
        # exigeant une expression `domain`.
        try:
            rapport._expodo_export_rows(options, limite=100000)
        except ValidationError as erreur:
            self.fail(
                "Une ligne groupée à grilles de TVA doit pouvoir se développer : %s"
                % erreur)

    def test_toutes_les_declarations_du_pays_s_affichent(self):
        """Aucune déclaration de la localisation installée ne doit échouer.

        C'est le test qui a révélé le défaut : compter les déclarations qui
        s'affichent, plutôt que d'en vérifier une choisie d'avance. Une
        localisation dont cinq déclarations sur huit échouent passait
        inaperçue tant qu'on n'éprouvait que la première.
        """
        declarations = self.env["account.report"].search([
            ("root_report_id.name", "ilike", "tax")])
        if not declarations:
            self.skipTest("Aucune déclaration de taxes dans cette base")

        echecs = []
        for rapport in declarations:
            try:
                options = rapport._expodo_get_options({})
                rapport._expodo_export_rows(options, limite=100000)
            except Exception as erreur:
                echecs.append("%s : %s" % (rapport.name, erreur))

        self.assertFalse(
            echecs,
            "Ces déclarations refusent de s'afficher :\n" + "\n".join(echecs))


@tagged("post_install", "-at_install")
class TestResumeGeneral(TransactionCase):
    """Résumé général.

    Cet état ne calcule rien de neuf : il rapproche des grandeurs présentes
    ailleurs. Son risque n'est donc pas l'erreur de calcul mais la
    **divergence** — afficher un résultat différent de celui du compte de
    résultat, ou une trésorerie différente de celle du bilan.

    Deux écrans qui se contredisent sont pires qu'un écran manquant : ils
    ruinent la confiance dans l'ensemble des états, y compris ceux qui sont
    justes.
    """

    def _valeurs(self, xmlid):
        donnees = self.env["ir.model.data"]
        rapport = self.env["account.report"].browse(
            donnees._xmlid_to_res_id(xmlid))
        options = rapport._expodo_get_options({})
        _d, lignes = rapport._expodo_export_rows(options, limite=100000)
        valeurs = {}
        for ligne in lignes:
            colonne = (ligne.get("columns") or [{}])[0]
            brut = colonne.get("raw")
            valeur = brut.get("main") if isinstance(brut, dict) else brut
            if ligne.get("code"):
                valeurs[ligne["code"]] = valeur or 0.0
        return valeurs

    def test_le_resultat_coincide_avec_le_compte_de_resultat(self):
        """Le chiffre le plus lu de l'état doit être le bon.

        Un dirigeant qui ouvre le résumé puis le compte de résultat et lit
        deux résultats différents ne saura pas lequel croire — et aura raison
        de ne croire ni l'un ni l'autre.
        """
        resume = self._valeurs("expodo_account_reports.report_executive_summary")
        compte = self._valeurs("expodo_account_reports.report_profit_loss")
        if "EXEC_RESULTAT" not in resume or "PL_NET" not in compte:
            self.skipTest("Codes de ligne indisponibles")
        self.assertAlmostEqual(
            resume["EXEC_RESULTAT"], compte["PL_NET"], places=2,
            msg="Le résultat du résumé général doit être celui du compte de "
                "résultat, au centime près")

    def test_le_besoin_en_fonds_de_roulement_se_deduit_de_ses_composantes(self):
        v = self._valeurs("expodo_account_reports.report_executive_summary")
        if "EXEC_BFR" not in v:
            self.skipTest("État indisponible")
        self.assertAlmostEqual(
            v["EXEC_BFR"],
            v["EXEC_CREANCES"] + v["EXEC_STOCKS"] - v["EXEC_DETTES"]
            - v.get("EXEC_AUTRES_DETTES", 0.0),
            places=2,
            msg="Besoin en fonds de roulement = créances + stocks et autres "
                "créances − dettes fournisseurs − autres dettes")

    def test_les_soldes_de_bilan_sont_lus_depuis_l_origine(self):
        """Une trésorerie est un solde, pas un flux.

        La lire sur la seule période afficherait la variation de trésorerie et
        non le disponible. L'erreur est invisible sur une base de test montée
        sur un seul exercice, et devient énorme dès la deuxième année.
        """
        donnees = self.env["ir.model.data"]
        rapport = self.env["account.report"].browse(
            donnees._xmlid_to_res_id(
                "expodo_account_reports.report_executive_summary"))
        for code in ("EXEC_DISPO", "EXEC_CREANCES", "EXEC_DETTES",
                     "EXEC_AUTRES_DETTES", "EXEC_STOCKS", "EXEC_CAPITAUX"):
            ligne = rapport.line_ids.filtered(lambda l: l.code == code)
            if not ligne:
                continue
            for expression in ligne.expression_ids:
                self.assertEqual(
                    expression.date_scope, "from_beginning",
                    "La ligne %s porte un solde de bilan : elle doit se lire "
                    "depuis l'origine, pas sur la période" % code)


@tagged("post_install", "-at_install")
class TestReleveClient(TransactionCase):
    """Relevé de compte client.

    Ce n'est pas un document interne : il part chez le client. Son exigence
    n'est donc pas seulement l'exactitude, c'est la **défendabilité**. Une
    seule ligne contestable — une facture déjà réglée, un total qui ne tombe
    pas juste — et le client conteste le relevé entier, y compris ses lignes
    justes. Le recouvrement s'arrête là.
    """

    def _rapport(self):
        donnees = self.env["ir.model.data"]
        return self.env["account.report"].browse(
            donnees._xmlid_to_res_id(
                "expodo_account_reports.report_customer_statement"))

    def _lignes(self):
        rapport = self._rapport()
        options = rapport._expodo_get_options({})
        _d, lignes = rapport._expodo_export_rows(options, limite=100000)
        return lignes

    @staticmethod
    def _valeurs(ligne):
        sorties = []
        for colonne in (ligne.get("columns") or []):
            brut = colonne.get("raw")
            sorties.append(
                (brut.get("main") if isinstance(brut, dict) else brut) or 0.0)
        return sorties

    def test_les_tranches_d_anciennete_se_somment_au_total(self):
        """Un total qui ne tombe pas juste discrédite le relevé entier.

        C'est la première chose qu'un client vérifie quand il cherche à gagner
        du temps.
        """
        for ligne in self._lignes():
            valeurs = self._valeurs(ligne)
            if len(valeurs) < 5:
                continue
            total, *tranches = valeurs
            self.assertAlmostEqual(
                total, sum(tranches), places=2,
                msg="Sur « %s », le total dû (%.2f) doit égaler la somme des "
                    "tranches (%.2f)"
                    % ((ligne.get("name") or "")[:40], total, sum(tranches)))

    def test_les_ecritures_lettrees_sont_exclues(self):
        """Réclamer une facture déjà réglée fait contester tout le relevé."""
        rapport = self._rapport()
        ligne = rapport.line_ids.filtered(
            lambda l: l.code == "STATEMENT_CUSTOMERS")
        self.assertTrue(ligne, "Ligne des clients introuvable")
        for expression in ligne.expression_ids:
            self.assertIn(
                "full_reconcile_id", expression.formula or "",
                "Chaque expression du relevé doit écarter les écritures "
                "lettrées, faute de quoi des factures réglées y figureraient")

    def test_les_soldes_sont_lus_depuis_l_origine(self):
        """Une créance ouverte l'an dernier reste due aujourd'hui.

        La lire sur la seule période en cours la ferait disparaître du relevé
        — c'est-à-dire précisément la créance la plus ancienne, celle qu'il
        faut réclamer en premier.
        """
        rapport = self._rapport()
        ligne = rapport.line_ids.filtered(
            lambda l: l.code == "STATEMENT_CUSTOMERS")
        for expression in ligne.expression_ids:
            self.assertEqual(
                expression.date_scope, "from_beginning",
                "L'expression %s doit se lire depuis l'origine"
                % expression.label)

    def test_le_releve_porte_le_solde_net_du_tiers(self):
        """Créances et dettes confondues, comme l'édition Enterprise.

        Un tiers qui est à la fois client et fournisseur apparaît pour ce
        qu'il doit **net**. La convention se discute — un relevé qui déduit ce
        qu'on doit au destinataire peut se faire contester — mais elle est
        celle de la référence, et deux outils qui annoncent des soldes
        différents au même client posent un problème plus grand que la
        convention elle-même.
        """
        rapport = self._rapport()
        ligne = rapport.line_ids.filtered(
            lambda l: l.code == "STATEMENT_CUSTOMERS")
        for expression in ligne.expression_ids:
            self.assertIn(
                "asset_receivable", expression.formula or "",
                "Le relevé doit porter les comptes clients")
            self.assertIn(
                "liability_payable", expression.formula or "",
                "Et les comptes fournisseurs, pour donner le solde net")


@tagged("post_install", "-at_install")
class TestReleveIntracommunautaire(TransactionCase):
    """Relevé des livraisons et prestations intracommunautaires.

    Le contrôle de cet état est **croisé et automatique** : l'administration
    rapproche ce que le vendeur déclare avoir livré de ce que l'acheteur
    déclare avoir acquis, dans l'autre pays. Un écart déclenche une demande,
    parfois des années plus tard.

    Deux erreurs sont donc coûteuses : omettre un client, et y faire figurer
    une opération intérieure.
    """

    def _rapport(self):
        donnees = self.env["ir.model.data"]
        return self.env["account.report"].browse(
            donnees._xmlid_to_res_id(
                "expodo_account_reports.report_ec_sales_list"))

    def test_les_colonnes_biens_et_services_se_somment_au_total(self):
        rapport = self._rapport()
        options = rapport._expodo_get_options({})
        _d, lignes = rapport._expodo_export_rows(options, limite=100000)
        for ligne in lignes:
            valeurs = []
            for colonne in (ligne.get("columns") or []):
                brut = colonne.get("raw")
                valeurs.append(
                    (brut.get("main") if isinstance(brut, dict) else brut) or 0.0)
            if len(valeurs) < 3:
                continue
            total, biens, services = valeurs[0], valeurs[1], valeurs[2]
            self.assertAlmostEqual(
                total, biens + services, places=2,
                msg="Sur « %s », le total (%.2f) doit égaler biens + services "
                    "(%.2f)" % ((ligne.get("name") or "")[:40], total,
                                biens + services))

    def test_la_selection_ne_repose_pas_sur_le_nom_des_taxes(self):
        """Sélectionner par « 0% EU G » se casserait hors de France.

        Chaque localisation nomme ses taxes à sa façon. Le critère retenu est
        la portée de la taxe et le pays du client, deux notions qu'Odoo tient
        identiquement partout.
        """
        rapport = self._rapport()
        ligne = rapport.line_ids.filtered(lambda l: l.code == "EC_SALES_PARTNERS")
        self.assertTrue(ligne)
        for expression in ligne.expression_ids:
            formule = expression.formula or ""
            self.assertNotIn(
                "EU G", formule,
                "La sélection ne doit pas dépendre du libellé d'une taxe")
            self.assertNotIn(
                "EU S", formule,
                "La sélection ne doit pas dépendre du libellé d'une taxe")
            self.assertIn(
                "country_group_ids", formule,
                "L'appartenance du client à l'Union doit être le critère")

    def test_seules_les_ventes_sont_retenues(self):
        """Un achat intracommunautaire ne se déclare pas sur cet état.

        Il relève d'une autre obligation, dans l'autre sens. L'y faire
        figurer doublerait le montant déclaré par le pays d'en face.
        """
        rapport = self._rapport()
        ligne = rapport.line_ids.filtered(lambda l: l.code == "EC_SALES_PARTNERS")
        for expression in ligne.expression_ids:
            self.assertIn(
                "'income'", expression.formula or "",
                "Seuls les comptes de produits entrent dans le relevé")

    def test_le_groupement_descend_jusqu_a_la_piece(self):
        """L'administration d'en face conteste des montants, pas des totaux."""
        rapport = self._rapport()
        ligne = rapport.line_ids.filtered(lambda l: l.code == "EC_SALES_PARTNERS")
        self.assertEqual(
            ligne.groupby, "partner_id,id",
            "Le relevé doit se déplier du client jusqu'à la pièce")


@tagged("post_install", "-at_install")
class TestResolutionDesPresentations(TransactionCase):
    """Un état qu'aucun utilisateur ne peut atteindre n'existe pas.

    Les présentations statutaires ne sont pas offertes par un menu propre :
    l'utilisateur ouvre « Bilan » et reçoit celle de son référentiel. La
    résolution retenait la variante par pays, ce qui convenait au plan
    français mais laissait les présentations SYSCOHADA inaccessibles — elles
    ne portent aucun pays, puisque le référentiel en couvre dix-sept et
    qu'Odoo n'en accepte qu'un.

    Le défaut était invisible : les états existaient, se calculaient, passaient
    les tests appelés directement. Seul le chemin réel de l'utilisateur le
    révélait.
    """

    def _resolu(self, kind):
        return self.env["account.report"].browse(
            self.env["account.report"].expodo_resolve_statutory_report(kind))

    def test_la_presentation_nationale_prime_sur_l_universelle(self):
        """La présentation du pays l'emporte, quand le pays en fournit une.

        La recherche portait sur n'importe quel état rattaché à une racine.
        Sur une base espagnole, elle tombait sur une déclaration de taxes,
        déduisait « compte de résultat » de son intitulé et comparait la
        présentation espagnole du résultat à une déclaration fiscale. Le
        contrôle ne vise que les deux états statutaires, désignés par leur
        racine et non par leur nom.
        """
        pays = self.env.company.account_fiscal_country_id
        racines = {
            "balance_sheet": self.env.ref(
                "expodo_account_reports.report_balance_sheet"),
            "profit_loss": self.env.ref(
                "expodo_account_reports.report_profit_loss"),
        }
        vus = 0
        for kind, racine in racines.items():
            nationale = self.env["account.report"].search([
                ("country_id", "=", pays.id),
                ("root_report_id", "=", racine.id),
            ], limit=1)
            if not nationale:
                continue
            vus += 1
            self.assertEqual(
                self._resolu(kind).country_id, pays,
                "La présentation nationale de « %s » doit primer" % racine.name)
        if not vus:
            self.skipTest("Aucune présentation nationale pour ce pays")

    def test_une_presentation_supranationale_est_atteignable(self):
        """Reconnue au plan comptable, faute de pays pour la désigner."""
        supranationales = self.env["account.report"].search([
            ("country_id", "=", False),
            ("expodo_chart_prefix", "!=", False),
        ])
        if not supranationales:
            self.skipTest("Aucune présentation supranationale déclarée")

        for presentation in supranationales:
            correspond = presentation._expodo_chart_matches()
            if not correspond:
                continue
            kind = ("balance_sheet"
                    if "balance" in (presentation.root_report_id.with_context(lang="en_US").name or "").lower()
                    else "profit_loss")
            self.assertEqual(
                self._resolu(kind), presentation,
                "Sur un plan qu'elle reconnaît, la présentation "
                "supranationale doit être retenue")

    def test_un_plan_etranger_ne_declenche_pas_une_presentation_supranationale(self):
        """L'erreur symétrique, et la plus coûteuse.

        Offrir un bilan SYSCOHADA à une société espagnole afficherait des
        postes qui n'existent pas dans son référentiel, avec un contrôle
        d'équilibre en défaut.
        """
        for presentation in self.env["account.report"].search([
                ("country_id", "=", False),
                ("expodo_chart_prefix", "!=", False)]):
            if presentation._expodo_chart_matches():
                continue
            kind = ("balance_sheet"
                    if "balance" in (presentation.root_report_id.with_context(lang="en_US").name or "").lower()
                    else "profit_loss")
            self.assertNotEqual(
                self._resolu(kind), presentation,
                "Une présentation dont le préfixe est absent du plan ne doit "
                "jamais être retenue")

    def test_la_reconnaissance_ne_depend_pas_des_ecritures(self):
        """Un plan fraîchement installé n'a aucune écriture et reste ce plan."""
        supranationales = self.env["account.report"].search([
            ("country_id", "=", False),
            ("expodo_chart_prefix", "!=", False)], limit=1)
        if not supranationales:
            self.skipTest("Aucune présentation supranationale déclarée")
        attendu = bool(self.env["account.account"].search_count([
            ("code", "=like", supranationales.expodo_chart_prefix + "%")]))
        self.assertEqual(supranationales._expodo_chart_matches(), attendu)


@tagged("post_install", "-at_install")
class TestAccessibiliteDepuisLesMenus(TransactionCase):
    """Chaque menu doit mener à quelque chose qui fonctionne.

    C'est le chemin de l'utilisateur, et c'est celui que les tests
    n'empruntent jamais : ils appellent les états directement, par leur
    identifiant. Un état parfaitement juste mais qu'aucun menu ne désigne
    passe tous les tests et n'existe pour personne — c'est arrivé aux
    présentations SYSCOHADA, restées inatteignables jusqu'à ce qu'on regarde
    de ce côté.
    """

    def _menus_de_module(self, module):
        donnees = self.env["ir.model.data"].search([
            ("module", "=", module), ("model", "=", "ir.ui.menu")])
        return self.env["ir.ui.menu"].sudo().browse(donnees.mapped("res_id")).exists()

    def test_aucun_modele_ne_traverse_les_societes(self):
        """Un modèle qui porte une société doit être cloisonné.

        Les droits d'accès disent qui peut lire un modèle ; seule une règle
        d'enregistrement dit quels enregistrements. Sans elle, l'utilisateur
        d'une société lit les budgets, les exercices, les niveaux de relance,
        les annotations et les déclarations de toutes les autres. Rien ne le
        signale, ni à l'écran ni dans les journaux.

        Neuf modèles étaient dans ce cas. Le contrôle vaut surtout chez un
        intégrateur, qui tient plusieurs dossiers dans la même base : c'est
        le premier lecteur de ce module.
        """
        nus = []
        for modele in self.env["ir.model"].search([("model", "=like", "expodo.%")]):
            M = self.env[modele.model]
            # Les modèles transitoires vivent le temps d'un assistant et
            # s'effacent : Odoo ne les cloisonne pas non plus.
            if M._abstract or M._transient:
                continue
            if not ({"company_id", "company_ids"} & set(M._fields)):
                continue
            if not self.env["ir.rule"].sudo().search_count(
                    [("model_id", "=", modele.id)]):
                nus.append(modele.model)
        self.assertEqual(
            nus, [],
            "Ces modèles portent une société sans règle qui cloisonne leurs "
            "enregistrements : %s" % ", ".join(nus))

    def test_un_gestionnaire_comptable_voit_ce_que_nous_enracinons(self):
        """Celui qui évalue le module doit voir ce qu'il vient d'installer.

        Un administrateur fraîchement installé ne porte que « Accounting /
        Administrator ». Ce groupe n'implique pas « lecture seule » dans
        Odoo : une racine déclarée avec le seul groupe de lecture disparaît
        donc pour lui. C'est arrivé à la racine des immobilisations, qui
        emportait avec elle les échéances à comptabiliser et le contrôle de
        cohérence. Trois écrans invisibles, et un module qui paraît ne rien
        faire au moment précis où on le juge.

        Le contrôle ne porte que sur les menus dont toute la lignée nous
        appartient. Ceux que nous accrochons à un menu d'Odoo dépendent de la
        visibilité de ce dernier, qui ne nous regarde pas.
        """
        gestionnaire = self.env["res.users"].create({
            "name": "Gestionnaire fraîchement installé",
            "login": "gestionnaire_evaluation",
            "group_ids": [Command.set([
                self.env.ref("base.group_user").id,
                self.env.ref("account.group_account_manager").id])],
        })
        charges = self.env["ir.ui.menu"].with_user(gestionnaire).load_menus(False)
        visibles = {int(k) for k in charges if str(k).isdigit()}

        modules = set(self.env["ir.module.module"].search([
            ("name", "=like", "expodo_account%"),
            ("state", "=", "installed")]).mapped("name"))
        notres = set()
        for module in modules:
            notres |= set(self._menus_de_module(module).ids)

        def lignee_a_nous(menu):
            noeud = menu.parent_id
            while noeud:
                if noeud.id not in notres:
                    return False
                noeud = noeud.parent_id
            return True

        aveugles = []
        for module in sorted(modules):
            for menu in self._menus_de_module(module):
                if not lignee_a_nous(menu) or menu.id in visibles:
                    continue
                aveugles.append("%s / %s" % (module, menu.name))
        self.assertEqual(
            aveugles, [],
            "Un gestionnaire comptable ne voit pas ces menus, que nous "
            "enracinons pourtant nous-mêmes : %s" % ", ".join(aveugles))

    def test_qui_voit_un_menu_peut_lire_ce_qu_il_ouvre(self):
        """Un menu visible qui refuse l'accès est pire qu'un menu absent.

        L'utilisateur clique, reçoit « Vous n'êtes pas autorisé à accéder à
        … », et conclut que le module est cassé ou que ses droits sont mal
        posés. C'est arrivé au lettrage automatique, dont le menu ne
        déclarait aucun groupe alors que l'assistant n'était ouvert qu'au
        comptable : le gestionnaire comptable, qui ne porte pas ce groupe,
        ouvrait le menu et tombait sur l'erreur.

        Le contrôle remonte la chaîne des parents : un menu sans groupe
        hérite de la visibilité de son parent, et c'est cette visibilité
        effective qu'il faut comparer aux droits de lecture du modèle.
        """
        comptables = self.env["res.groups"].browse([
            self.env.ref("account.group_account_%s" % nom).id
            for nom in ("basic", "readonly", "user", "manager")
        ])
        acces = self.env["ir.model.access"].sudo()
        ecarts = []
        for module in self.env["ir.module.module"].search([
                ("name", "=like", "expodo_account%"),
                ("state", "=", "installed")]).mapped("name"):
            for menu in self._menus_de_module(module):
                action = menu.action
                modele = getattr(action, "res_model", None) if action else None
                if not modele or not modele.startswith("expodo."):
                    continue

                # Visibilité effective : les groupes du menu, ou à défaut
                # ceux du premier parent qui en déclare.
                voyants = self.env["res.groups"]
                noeud = menu
                while noeud and not voyants:
                    voyants = noeud.group_ids
                    noeud = noeud.parent_id
                voyants = voyants & comptables
                if not voyants:
                    continue

                lecteurs = acces.search([
                    ("model_id", "=", self.env["ir.model"]._get(modele).id),
                    ("perm_read", "=", True),
                ]).mapped("group_id")
                if not lecteurs:
                    continue  # accessible à tous : rien à comparer

                for groupe in voyants:
                    # `all_implied_ids` porte les groupes hérités : un
                    # comptable a « lecture seule », et l'autorisation peut
                    # avoir été posée sur l'un ou sur l'autre.
                    if not (groupe.all_implied_ids | groupe) & lecteurs:
                        ecarts.append(
                            "%s : %s voit « %s » mais ne peut pas lire %s"
                            % (module, groupe.name, menu.name, modele))

        self.assertEqual(
            ecarts, [],
            "Menus visibles sans droit de lecture :\n  " + "\n  ".join(ecarts))

    def test_chaque_menu_a_un_parent_et_une_action(self):
        """Un menu orphelin ne s'affiche nulle part.

        Un menu sans action s'affiche et ne fait rien, ce qui est pire : le
        lecteur croit que la fonctionnalité est cassée.
        """
        orphelins = []
        for module in self.env["ir.module.module"].search([
                ("name", "=like", "expodo_account%"),
                ("state", "=", "installed")]).mapped("name"):
            for menu in self._menus_de_module(module):
                if not menu.parent_id and not menu.child_id:
                    orphelins.append("%s / %s : sans parent ni enfant"
                                     % (module, menu.name))
                if not menu.action and not menu.child_id:
                    orphelins.append("%s / %s : sans action ni enfant"
                                     % (module, menu.name))
        self.assertFalse(
            orphelins, "Menus inatteignables :\n  " + "\n  ".join(orphelins))

    def test_chaque_menu_d_etat_mene_a_un_etat_calculable(self):
        """Le contrôle qui aurait attrapé le SYSCOHADA inatteignable."""
        import ast as _ast

        rapports = self.env["account.report"]
        echecs = []
        for menu in self._menus_de_module("expodo_account_reports"):
            if not menu.action or menu.action._name != "ir.actions.client":
                continue
            contexte = menu.action.context
            if isinstance(contexte, str):
                try:
                    contexte = _ast.literal_eval(contexte)
                except (ValueError, SyntaxError):
                    contexte = {}
            identifiant = (contexte or {}).get("report_id")
            nature = (contexte or {}).get("report_kind")

            if not identifiant and nature:
                try:
                    identifiant = (
                        rapports.expodo_resolve_tax_report() if nature == "tax"
                        else rapports.expodo_resolve_statutory_report(nature))
                except Exception as erreur:
                    echecs.append("%s : %s" % (menu.name, erreur))
                    continue

            if not identifiant:
                echecs.append("%s : aucun état désigné" % menu.name)
                continue

            etat = rapports.browse(identifiant).exists()
            if not etat:
                echecs.append("%s : état introuvable" % menu.name)
                continue

            try:
                donnees = etat.expodo_get_report_data({})
            except Exception as erreur:
                echecs.append("%s → %s : %s" % (menu.name, etat.name, erreur))
                continue
            if not donnees.get("lines"):
                echecs.append("%s → %s : aucune ligne" % (menu.name, etat.name))

        self.assertFalse(
            echecs, "Menus menant à un état en échec :\n  " + "\n  ".join(echecs))

    def test_chaque_etat_livre_est_atteignable(self):
        """Le contrôle inverse : un état livré doit avoir un chemin.

        Le précédent vérifie qu'aucun menu ne mène nulle part. Il ne dit rien
        des états qui n'ont pas de menu du tout. Trois l'étaient — le résumé
        général, le relevé de compte client, le relevé intracommunautaire :
        définis, traduits, testés, et atteignables par aucun clic.

        Trois chemins comptent : un menu qui désigne l'état, directement ou
        par sa nature ; le sélecteur de présentation, qui atteint un état
        rattaché à une racine par `root_report_id` ; et cette racine
        elle-même, que le sélecteur propose au même titre — c'est ainsi que
        le bilan universel s'atteint depuis le bilan français.
        """
        import ast as _ast

        rapports = self.env["account.report"]
        vises = set()
        for action in self.env["ir.actions.client"].search(
                [("tag", "=", "expodo_account_report")]):
            contexte = action.context
            if isinstance(contexte, str):
                try:
                    contexte = _ast.literal_eval(contexte)
                except (ValueError, SyntaxError):
                    contexte = {}
            contexte = contexte or {}
            if contexte.get("report_id"):
                vises.add(contexte["report_id"])
            elif contexte.get("report_kind"):
                nature = contexte["report_kind"]
                try:
                    vises.add(
                        rapports.expodo_resolve_tax_report() if nature == "tax"
                        else rapports.expodo_resolve_statutory_report(nature))
                except Exception:
                    continue

        inatteignables = []
        for etat in rapports.search([]):
            if not etat.line_ids:
                continue
            xmlid = etat.get_external_id().get(etat.id) or ""
            if not xmlid.startswith("expodo_"):
                continue
            if etat.id in vises or etat.root_report_id:
                continue
            # La racine d'une présentation visée est proposée par le même
            # sélecteur que ses variantes.
            if rapports.browse(sorted(vises)).mapped("root_report_id.id") \
                    and etat.id in rapports.browse(
                        sorted(vises)).mapped("root_report_id.id"):
                continue
            inatteignables.append("%s (%s)" % (etat.name, xmlid))

        self.assertFalse(
            inatteignables,
            "États livrés sans aucun chemin dans l'interface :\n  "
            + "\n  ".join(inatteignables))


@tagged("post_install", "-at_install")
class TestDroitsDUnComptable(TransactionCase):
    """Tout ce qui précède a été vérifié en superutilisateur.

    Un module qui fonctionne pour l'administrateur et casse pour un comptable
    est une publication ratée : le premier utilisateur réel est justement
    celui qui n'a pas tous les droits.

    Le contrôle porte sur le chargement réel des menus — `load_menus`, ce que
    le client web appelle — et non sur une recherche. Une recherche sur
    `ir.ui.menu` n'applique pas le filtre de groupes, et croire le contraire
    fait voir des défauts qui n'existent pas.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        groupes = cls.env.ref("base.group_user") | cls.env.ref(
            "account.group_account_user")
        cls.comptable = cls.env["res.users"].create({
            "name": "Comptable de test",
            "login": "comptable_test_expodo",
            "group_ids": [(6, 0, groupes.ids)],
        })

    def test_un_comptable_peut_consulter_tous_les_etats(self):
        """Consulter n'est pas administrer : lire un bilan est le métier."""
        donnees = self.env["ir.model.data"].search([
            ("module", "=", "expodo_account_reports"),
            ("model", "=", "account.report")])
        rapports = self.env["account.report"].browse(
            donnees.mapped("res_id")).exists()
        if not rapports:
            self.skipTest("Aucun état installé")

        echecs = []
        for rapport in rapports.with_user(self.comptable):
            try:
                rapport.expodo_get_report_data({})
            except Exception as erreur:
                echecs.append("%s : %s" % (rapport.name, erreur))
        self.assertFalse(
            echecs,
            "États inaccessibles à un comptable :\n  " + "\n  ".join(echecs))

    def test_aucun_menu_visible_ne_mene_a_un_refus(self):
        """Un menu qui s'affiche et refuse fait croire à une panne.

        L'utilisateur ne distingue pas « vous n'avez pas le droit » de « le
        logiciel est cassé » : dans les deux cas il appelle son prestataire.
        """
        env_comptable = self.env(user=self.comptable)
        try:
            arbre = env_comptable["ir.ui.menu"].load_menus(False)
        except Exception:
            self.skipTest("load_menus indisponible dans ce contexte")
        charges = {int(cle) for cle in arbre if str(cle).isdigit()}

        modules = self.env["ir.module.module"].search([
            ("name", "=like", "expodo_account%"),
            ("state", "=", "installed")]).mapped("name")

        echecs = []
        for module in modules:
            for donnee in self.env["ir.model.data"].search([
                    ("module", "=", module), ("model", "=", "ir.ui.menu")]):
                if donnee.res_id not in charges:
                    continue
                menu = self.env["ir.ui.menu"].browse(donnee.res_id).exists()
                action = menu.action if menu else None
                if not action or action._name != "ir.actions.act_window":
                    continue
                try:
                    env_comptable[action.res_model].check_access("read")
                except Exception:
                    echecs.append("%s → %s" % (menu.name, action.res_model))

        self.assertFalse(
            echecs,
            "Menus visibles menant à un refus d'accès :\n  "
            + "\n  ".join(echecs))

    def test_les_operations_de_cloture_restent_au_gestionnaire(self):
        """Clôturer, produire un FEC, reporter des charges : actes de responsable.

        Un comptable ne doit pas pouvoir figer un exercice ni produire le
        fichier remis à l'administration.
        """
        reserves = ["expodo.fec.export", "expodo.year.closing",
                    "expodo.deferral.wizard", "expodo.revaluation.wizard"]
        ouverts = []
        for modele in reserves:
            if modele not in self.env.registry.models:
                continue
            try:
                self.env(user=self.comptable)[modele].check_access("create")
                ouverts.append(modele)
            except Exception:
                pass
        self.assertFalse(
            ouverts,
            "Ces opérations devraient être réservées au gestionnaire : %s"
            % ", ".join(ouverts))


@tagged("post_install", "-at_install")
class TestLignesDeControle(TransactionCase):
    """Les états qui se contrôlent eux-mêmes doivent être écoutés.

    Neuf états livrés portent une ligne dont le libellé dit « doit être
    nul » : écart actif-passif du bilan, écart entre le résultat du bilan et
    celui du compte de résultat, écart entre les flux classés et le mouvement
    réel de trésorerie. Ces lignes sont le meilleur détecteur du module,
    puisqu'elles comparent deux chemins de calcul indépendants.

    Aucune ne l'était : le contrôle s'affichait à l'écran et personne ne le
    lisait. Une définition fausse pouvait donc rendre un bilan boiteux sans
    faire rougir la suite, ce qui est arrivé avec l'affectation du résultat.

    Le test est général : toute ligne de contrôle ajoutée plus tard est
    surveillée sans qu'on ait à y penser.
    """

    def _etats_pertinents(self):
        """Les états qu'un utilisateur de cette base peut réellement ouvrir.

        Un état national dont le plan comptable n'est pas celui de la société
        n'est pas proposé, et ses préfixes tomberaient sur d'autres comptes.
        Le mesurer ici reviendrait à contrôler un état que personne ne voit.
        """
        etats = self.env["account.report"]
        for rapport in self.env["account.report"].search([]):
            identifiant = rapport.get_external_id().get(rapport.id) or ""
            if not identifiant.startswith("expodo_account_reports."):
                continue
            if rapport.expodo_chart_prefix and not rapport._expodo_chart_matches():
                continue
            etats |= rapport
        return etats

    def test_aucun_controle_ne_signale_d_ecart(self):
        controles = 0
        for rapport in self._etats_pertinents():
            lignes = rapport.line_ids.filtered(
                lambda l: "must be zero"
                in (l.with_context(lang="en_US").name or ""))
            for ligne in lignes:
                controles += 1
                for annee in (2026, 2027):
                    options = rapport._expodo_get_options({"date": {
                        "mode": "range", "filter": "custom",
                        "date_from": date(annee, 1, 1),
                        "date_to": date(annee, 12, 31)}})
                    valeur = rapport._expodo_compute_values(
                        options, "main").get((ligne.code, "balance"), 0.0)
                    with self.subTest(etat=rapport.name, ligne=ligne.code,
                                      exercice=annee):
                        self.assertAlmostEqual(
                            valeur, 0.0, places=2,
                            msg="L'état se déclare lui-même en écart : "
                                "deux chemins de calcul ne donnent pas le "
                                "même chiffre")
        self.assertGreaterEqual(
            controles, 5,
            "Les lignes de contrôle sont repérées par leur libellé anglais "
            "« must be zero » ; si le compte tombe, c'est la convention qui "
            "a changé et le test ne contrôle plus rien")


@tagged("post_install", "-at_install")
class TestJustificationDesPostesTiers(TransactionCase):
    """Les états de tiers doivent justifier le bilan, à la date d'arrêté.

    C'est leur raison d'être : présenter tiers par tiers ce que le bilan
    donne en un chiffre. Trois états y concourent, la balance âgée, les
    écritures ouvertes et le relevé client, et les trois retenaient les
    lignes non lettrées **aujourd'hui** au lieu des lignes ouvertes **à la
    date d'arrêté**. Une facture de décembre réglée en mars disparaissait,
    et l'écart avec le bilan grandissait à mesure que les règlements
    rentraient.

    Le contrôle porte sur deux exercices : le défaut est invisible sur
    l'exercice courant, où presque rien n'est encore lettré.
    """

    def _valeur(self, xmlid, code, annee, label="balance"):
        rapport = self.env.ref("expodo_account_reports." + xmlid)
        options = rapport._expodo_get_options({"date": {
            "mode": "range", "filter": "custom",
            "date_from": date(annee, 1, 1), "date_to": date(annee, 12, 31)}})
        return round(rapport._expodo_compute_values(
            options, "main").get((code, label), 0.0), 2)

    def test_la_balance_agee_clients_egale_le_poste_du_bilan(self):
        for annee in (2026, 2027):
            with self.subTest(exercice=annee):
                self.assertAlmostEqual(
                    self._valeur("report_aged_receivable_fr", "AGEDR", annee),
                    self._valeur("report_balance_sheet", "BS_RECEIVABLE", annee),
                    places=2,
                    msg="La balance âgée détaille le poste client du bilan : "
                        "les deux totaux ne peuvent pas différer")

    def test_la_balance_agee_fournisseurs_egale_le_poste_du_bilan(self):
        for annee in (2026, 2027):
            with self.subTest(exercice=annee):
                self.assertAlmostEqual(
                    abs(self._valeur("report_aged_payable_fr", "AGEDP", annee)),
                    abs(self._valeur("report_balance_sheet", "BS_PAYABLE", annee)),
                    places=2,
                    msg="La balance âgée fournisseurs détaille le poste "
                        "fournisseurs du bilan")

    def test_les_ecritures_ouvertes_recouvrent_les_deux_postes(self):
        """L'état porte les clients et les fournisseurs, avec leur sens
        comptable : les dettes y sont créditrices, donc négatives, là où le
        bilan les présente en positif au passif."""
        for annee in (2026, 2027):
            with self.subTest(exercice=annee):
                self.assertAlmostEqual(
                    self._valeur("report_open_items_fr",
                                 "OPEN_PARTENAIRES", annee),
                    self._valeur("report_balance_sheet", "BS_RECEIVABLE", annee)
                    - self._valeur("report_balance_sheet", "BS_PAYABLE", annee),
                    places=2,
                    msg="Les écritures ouvertes justifient les deux postes de "
                        "tiers du bilan")
