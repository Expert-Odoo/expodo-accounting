# -*- coding: utf-8 -*-
# Copyright 2026 Expodo (https://expodo.fr)
# License LGPL-3
"""Tests des états financiers au format du plan comptable général français.

Ces états ne dépendent d'aucune localisation : ils n'emploient que des
préfixes de comptes. Ils vivent donc dans le module principal, résolus à
l'exécution pour une société française et invisibles ailleurs.
"""

from datetime import date

from dateutil.relativedelta import relativedelta

from odoo import Command
from odoo.exceptions import UserError
from odoo.tests import TransactionCase, tagged
from odoo.tools.misc import format_date


@tagged("post_install", "-at_install")
class TestFrenchStatements(TransactionCase):

    def setUp(self):
        super().setUp()
        # Ces contrôles portent sur le plan comptable général français.
        #
        # Exécutés sur une base espagnole ou allemande, ils comparaient les
        # préfixes du bilan français aux comptes d'un autre référentiel et
        # signalaient des centaines de comptes « non couverts » : un échec
        # qui ne dit rien du module. Le module se vend à l'international ;
        # sa suite doit pouvoir tourner ailleurs qu'en France.
        pays = (self.env.company.account_fiscal_country_id.code
                or self.env.company.country_id.code)
        if pays != "FR":
            self.skipTest("Contrôles propres au plan comptable français")


    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.customer = cls.env["res.partner"].create({"name": "Client pack FR"})
        cls.supplier = cls.env["res.partner"].create({"name": "Fournisseur pack FR"})
        cls.sale_tax = cls.env["account.tax"].search([
            ("type_tax_use", "=", "sale"), ("amount", "=", 20.0),
            ("tax_exigibility", "=", "on_invoice"),
        ], limit=1)
        cls.purchase_tax = cls.env["account.tax"].search([
            ("type_tax_use", "=", "purchase"), ("amount", "=", 20.0),
            ("tax_exigibility", "=", "on_invoice"),
        ], limit=1)
        cls.period = {
            "mode": "range", "filter": "fiscalyear",
            "date_from": date(2026, 1, 1), "date_to": date(2026, 12, 31),
        }

    def _invoice(self, move_type, partner, amount, tax, ref, day="2026-03-15"):
        move = self.env["account.move"].create({
            "move_type": move_type, "partner_id": partner.id,
            "invoice_date": day, "date": day, "ref": ref,
            "invoice_line_ids": [Command.create({
                "name": ref, "quantity": 1, "price_unit": amount,
                "tax_ids": [Command.set(tax.ids)] if tax else [Command.clear()],
            })],
        })
        move.action_post()
        return move

    def _values(self, xmlid, period=None):
        report = self.env.ref(xmlid)
        options = report._expodo_get_options({"date": period or self.period})
        return report, options, report._expodo_compute_values(options, "main")

    def test_balance_sheet_balances(self):
        """T29 — Actif = Passif, quel que soit le jeu d'écritures."""
        self._invoice("out_invoice", self.customer, 2000.0, self.sale_tax, "V1")
        self._invoice("in_invoice", self.supplier, 500.0, self.purchase_tax, "A1")
        _, _, values = self._values("expodo_account_reports.report_bilan_fr")
        self.assertAlmostEqual(
            values[("BILAN_ECART", "balance")], 0.0, places=2,
            msg="Le bilan ne s'équilibre pas : actif %.2f, passif %.2f" % (
                values[("BILAN_ACTIF", "balance")],
                values[("BILAN_PASSIF", "balance")],
            ),
        )

    def test_income_statement_recomposes(self):
        """Le détail par rubrique doit égaler le solde global des classes 6 et 7.

        C'est le garde-fou contre une classe de comptes oubliée dans le détail :
        sans ce contrôle, l'omission passe inaperçue jusqu'à ce qu'un client
        s'en aperçoive.
        """
        self._invoice("out_invoice", self.customer, 1500.0, None, "V2")
        self._invoice("in_invoice", self.supplier, 400.0, None, "A2")
        _, _, values = self._values("expodo_account_reports.report_resultat_fr")
        self.assertAlmostEqual(values[("RES_ECART", "balance")], 0.0, places=2)

    def test_income_statement_moves_the_balance_sheet_result(self):
        """Ce que le compte de résultat gagne, le bilan le porte.

        Les deux l'obtiennent par des formules différentes : le compte de
        résultat par recomposition des rubriques, le bilan par les préfixes de
        comptes.

        La mesure porte sur l'**écart** avant et après, jamais sur les deux
        totaux, parce que la base d'essai porte déjà ce qu'elle porte.

        Et elle porte sur les capitaux propres, non sur la seule ligne de
        résultat. Le bilan répartit le bénéfice entre l'exercice en cours et
        le report à nouveau selon la date de l'écriture, alors que le compte
        de résultat lit la période demandée : comparer à la ligne de résultat
        seule revenait à exiger que les deux fenêtres coïncident, ce qui n'est
        vrai que sur un exercice calendaire. Les capitaux propres, eux, les
        recueillent dans tous les cas — et c'est la propriété qui importe : un
        produit comptabilisé se retrouve à l'identique des deux côtés.
        """
        _, _, pl_avant = self._values("expodo_account_reports.report_resultat_fr")
        _, _, bs_avant = self._values("expodo_account_reports.report_bilan_fr")

        self._invoice("out_invoice", self.customer, 3000.0, self.sale_tax, "V3")
        self._invoice("in_invoice", self.supplier, 1300.0, self.purchase_tax, "A3")

        _, _, pl_apres = self._values("expodo_account_reports.report_resultat_fr")
        _, _, bs_apres = self._values("expodo_account_reports.report_bilan_fr")

        gain_resultat = (pl_apres[("RES_NET", "balance")]
                         - pl_avant[("RES_NET", "balance")])
        gain_bilan = (bs_apres[("BILAN_CAPITAUX", "balance")]
                      - bs_avant[("BILAN_CAPITAUX", "balance")])
        self.assertAlmostEqual(gain_resultat, 1700.0, places=2)
        self.assertAlmostEqual(gain_resultat, gain_bilan, places=2)

    def _ecriture_simple(self, jour, montant, libelle):
        """Un produit net : créance au débit, vente au crédit."""
        comptes = self.env["account.account"]
        client = comptes.search(
            [("code", "=like", "411%"), ("company_ids", "in", self.env.company.id)],
            limit=1)
        vente = comptes.search(
            [("code", "=like", "706%"), ("company_ids", "in", self.env.company.id)],
            limit=1)
        if not client or not vente:
            self.skipTest("Plan comptable français incomplet sur cette base")
        journal = self.env["account.journal"].search(
            [("type", "=", "general"), ("company_id", "=", self.env.company.id)],
            limit=1)
        ecriture = self.env["account.move"].create({
            "journal_id": journal.id, "date": jour, "ref": libelle,
            "line_ids": [
                Command.create({"name": libelle, "account_id": client.id,
                                "debit": montant, "credit": 0.0}),
                Command.create({"name": libelle, "account_id": vente.id,
                                "debit": 0.0, "credit": montant}),
            ],
        })
        ecriture.action_post()
        return ecriture

    def test_le_resultat_de_l_exercice_ne_porte_que_l_exercice(self):
        """Un bénéfice d'un exercice antérieur n'est pas le résultat de celui-ci.

        Sur une comptabilité jamais clôturée — le cas de toute reprise
        d'historique, et de bien des bases en production — les comptes de
        gestion portent tous les exercices à la fois. La ligne « résultat de
        l'exercice » les additionnait donc tous, et affichait le cumul de
        trois ans sous le titre d'un seul.

        Rien ne le signalait : le bilan s'équilibrait, puisque le total des
        capitaux propres, lui, était juste. Seule la répartition mentait, et
        c'est l'une des premières lignes que lit un expert-comptable.

        Le contrôle mesure des écarts plutôt que des soldes : la base d'essai
        porte déjà ce qu'elle porte, et ce qui doit tenir est le classement
        d'un euro nouveau, pas le montant affiché.
        """
        arrete = date(2026, 6, 15)
        exercice = self.env.company.compute_fiscalyear_dates(arrete)
        avant = exercice["date_from"] + relativedelta(days=-1)
        periode = {"mode": "range", "filter": "custom",
                   "date_from": exercice["date_from"], "date_to": arrete}

        def soldes():
            return self._values(
                "expodo_account_reports.report_bilan_fr", periode)[2]

        depart = soldes()
        self._ecriture_simple(avant, 1000.0, "Produit d'un exercice antérieur")
        self._ecriture_simple(arrete, 400.0, "Produit de l'exercice en cours")
        arrivee = soldes()

        def ecart(code):
            return arrivee[(code, "balance")] - depart[(code, "balance")]

        self.assertAlmostEqual(
            ecart("BILAN_RESULTAT"), 400.0, places=2,
            msg="Le résultat de l'exercice doit porter les 400 de l'exercice "
                "en cours, et eux seuls")
        self.assertAlmostEqual(
            ecart("BILAN_REPORT"), 1000.0, places=2,
            msg="Les 1 000 de l'exercice antérieur reviennent au report à "
                "nouveau")
        self.assertAlmostEqual(
            arrivee[("BILAN_ECART", "balance")], 0.0, places=2,
            msg="Le bilan doit rester équilibré")

    def test_un_etat_arrete_annonce_une_date_et_non_un_intervalle(self):
        """L'en-tête de colonne dit ce que l'état lit, dans la langue du lecteur.

        Un bilan porte des soldes arrêtés à une date. Sa borne d'ouverture
        n'existe que pour donner son amplitude à la colonne de comparaison :
        l'annoncer en tête de colonne laisse croire qu'on peut la déplacer,
        alors qu'elle ne change aucun montant.

        Et les dates y étaient interpolées telles quelles, donc en ISO. Un
        bilan français s'ouvrait sur « Du 2026-07-01 au 2026-09-27 », seule
        ligne du document à ne pas être en français. L'en-tête des exports
        avait été corrigé ; l'écran dont ils sortent ne l'était pas, si bien
        que le fichier transmis et la page annonçaient deux périodes.
        """
        arrete = self.env.ref("expodo_account_reports.report_bilan_fr")
        groupe = arrete._expodo_get_options(
            {"date": self.period})["column_groups"]["main"]
        libelle = arrete._expodo_column_group_label("main", groupe)
        self.assertIn(
            format_date(self.env, groupe["date"]["date_to"]), libelle,
            "Un état arrêté annonce sa date de clôture")
        self.assertNotIn(
            format_date(self.env, groupe["date"]["date_from"]), libelle,
            "Un état arrêté n'annonce pas de date d'ouverture : elle ne change "
            "aucun montant affiché")
        self.assertNotIn(
            groupe["date"]["date_to"].isoformat(), libelle,
            "Les dates suivent la langue du lecteur, pas le format ISO")

        intervalle = self.env.ref("expodo_account_reports.report_resultat_fr")
        groupe = intervalle._expodo_get_options(
            {"date": self.period})["column_groups"]["main"]
        libelle = intervalle._expodo_column_group_label("main", groupe)
        for borne in ("date_from", "date_to"):
            self.assertIn(
                format_date(self.env, groupe["date"][borne]), libelle,
                "Un état de flux annonce ses deux bornes")
        self.assertNotIn(
            groupe["date"]["date_to"].isoformat(), libelle,
            "Les dates suivent la langue du lecteur, pas le format ISO")

    def test_une_cloture_ne_change_rien_au_bilan(self):
        """Clôturer déplace des montants dans les capitaux propres, sans plus.

        La clôture solde les comptes de gestion et porte le résultat en classe
        12. Rien n'est gagné ni perdu : le bilan lu avant et le bilan lu après
        doivent être identiques, ligne à ligne, à la date de clôture comme dans
        l'exercice suivant.

        C'est l'invariant que la répartition entre « résultat de l'exercice »
        et « report à nouveau » doit respecter, et celui qui casse au premier
        oubli. Retirer la classe 12 de la ligne de résultat, par exemple, fait
        disparaître le bénéfice du bilan au lendemain de la clôture : les
        comptes de gestion sont soldés, le compte 120000 n'est lu nulle part,
        et le contrôle actif-passif s'allume d'un montant égal au résultat.

        Ce contrôle n'existait pas : la clôture avait ses tests, le bilan les
        siens, et personne ne lisait le bilan après une clôture.
        """
        if "expodo.closing.engine" not in self.env:
            self.skipTest("Module de clôture absent de cette base")
        moteur = self.env["expodo.closing.engine"]
        journal = self.env["account.journal"].search(
            [("type", "=", "general"), ("company_id", "=", self.env.company.id)],
            limit=1)

        arrete = date(2026, 6, 15)
        exercice = self.env.company.compute_fiscalyear_dates(arrete)
        lendemain = exercice["date_to"] + relativedelta(days=1)

        def bilan(jour):
            valeurs = self._values(
                "expodo_account_reports.report_bilan_fr",
                {"mode": "range", "filter": "custom",
                 "date_from": exercice["date_from"], "date_to": jour})[2]
            return {cle: round(montant, 2) for cle, montant in valeurs.items()}

        self._ecriture_simple(arrete, 2500.0, "Produit de l'exercice à clôturer")
        avant_cloture = bilan(exercice["date_to"])
        avant_suivant = bilan(lendemain)

        try:
            ecriture = moteur.ecriture_cloture(
                self.env.company, exercice["date_from"], exercice["date_to"],
                journal)
        except UserError as refus:
            self.skipTest("Clôture impossible sur cette base : %s" % refus)
        self.assertTrue(ecriture, "La clôture doit produire une écriture")
        ecriture.action_post()

        self.assertEqual(
            bilan(exercice["date_to"]), avant_cloture,
            "Le bilan à la date de clôture ne doit pas bouger d'un centime")
        self.assertEqual(
            bilan(lendemain), avant_suivant,
            "Le bilan du lendemain non plus : la clôture déplace des montants "
            "à l'intérieur des capitaux propres, elle n'en crée aucun")

    def test_account_codes_debit_credit_suffix(self):
        """T06 — Le suffixe D/C filtre les comptes par sens de solde.

        Il ne sélectionne pas la colonne débit ou crédit. Sur un compte ayant
        500 au débit et 200 au crédit, `411D` renvoie 300 — le solde.
        """
        self._invoice("out_invoice", self.customer, 1000.0, None, "V6")
        _, _, values = self._values("expodo_account_reports.report_bilan_fr")
        # La créance client est à solde débiteur : elle doit figurer à l'actif
        # et non au passif.
        self.assertGreater(values[("BILAN_CREANCES", "balance")], 0.0)

    def test_audit_action_declares_its_views(self):
        """Régression : sans `views`, le client échoue dans `_preprocessAction`.

        Une action construite en Python et renvoyée par RPC ne passe pas par
        `ir.actions.act_window` : le client ne peut pas déduire les vues de
        `view_mode` seul.
        """
        report, options, _ = self._values("expodo_account_reports.report_balance_fr")
        action = report.expodo_action_audit(
            report.line_ids[0].id, report._expodo_serialize_options(options)
        )
        self.assertTrue(action.get("views"), "L'action d'audit doit déclarer ses vues")

    def test_every_balance_sheet_account_is_covered(self):
        """Aucun compte de bilan ne doit échapper aux regroupements PCG.

        Régression : le préfixe 292 — dépréciations des immobilisations mises
        en concession — n'était couvert par aucune ligne. Les montants de ces
        comptes disparaissaient du bilan.

        Aucun test ne l'avait vu, et c'est instructif : dans un jeu d'essai le
        compte est vide, et un compte vide ne déséquilibre rien. La ligne de
        contrôle restait donc à zéro et les 108 tests passaient. Le défaut ne
        se serait manifesté que chez un client ayant déprécié une
        immobilisation en concession — au pire moment, et sans explication.

        Ce contrôle porte sur la structure du rapport, pas sur des montants :
        c'est le seul moyen d'attraper un trou qui ne se voit pas tant que le
        compte concerné est vide.
        """
        import re

        rapport = self.env.ref("expodo_account_reports.report_bilan_fr")
        prefixes = set()
        for ligne in rapport.line_ids:
            for expression in ligne.expression_ids:
                if expression.engine == "account_codes":
                    prefixes.update(re.findall(r"\d+", expression.formula or ""))
        self.assertTrue(prefixes, "Le bilan doit porter des préfixes")

        orphelins = []
        for compte in self.env["account.account"].search([]):
            code = compte.code or ""
            if not code or code[0] not in "12345":
                continue
            if not any(code.startswith(p) for p in prefixes):
                orphelins.append("%s %s" % (code, compte.name))

        self.assertFalse(
            orphelins,
            "Comptes absents du bilan, donc invisibles dans l'état :\n  "
            + "\n  ".join(orphelins[:15]))

    def test_every_profit_and_loss_account_is_covered(self):
        """Même contrôle sur le compte de résultat, classes 6 et 7."""
        import re

        rapport = self.env.ref("expodo_account_reports.report_resultat_fr")
        prefixes = set()
        for ligne in rapport.line_ids:
            for expression in ligne.expression_ids:
                if expression.engine == "account_codes":
                    prefixes.update(re.findall(r"\d+", expression.formula or ""))

        orphelins = []
        for compte in self.env["account.account"].search([]):
            code = compte.code or ""
            if not code or code[0] not in "67":
                continue
            if not any(code.startswith(p) for p in prefixes):
                orphelins.append("%s %s" % (code, compte.name))

        self.assertFalse(
            orphelins,
            "Comptes absents du compte de résultat :\n  "
            + "\n  ".join(orphelins[:15]))


@tagged("post_install", "-at_install")
class TestSoldesIntermediairesDeGestion(TransactionCase):
    """Cascade des soldes intermédiaires de gestion.

    Huit soldes enchaînés offrent huit occasions d'oublier un compte. L'oubli
    ne produit ni erreur ni déséquilibre : simplement un résultat net faux et
    plausible. Les tests portent donc sur la cohérence de la cascade, pas sur
    l'affichage de chaque ligne.
    """

    def setUp(self):
        super().setUp()
        # Ces contrôles portent sur le plan comptable général français.
        #
        # Exécutés sur une base espagnole ou allemande, ils comparaient les
        # préfixes du bilan français aux comptes d'un autre référentiel et
        # signalaient des centaines de comptes « non couverts » : un échec
        # qui ne dit rien du module. Le module se vend à l'international ;
        # sa suite doit pouvoir tourner ailleurs qu'en France.
        pays = (self.env.company.account_fiscal_country_id.code
                or self.env.company.country_id.code)
        if pays != "FR":
            self.skipTest("Contrôles propres au plan comptable français")


    def _valeurs(self):
        donnees = self.env["ir.model.data"]
        rapport = self.env["account.report"].browse(
            donnees._xmlid_to_res_id("expodo_account_reports.report_sig_fr"))
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

    def test_la_cascade_retombe_sur_le_resultat_global(self):
        """Le contrôle le plus important de l'état.

        Si un compte du plan n'est repris dans aucun des huit soldes, il
        disparaît du résultat net sans que rien ne le signale. Cette
        vérification est le seul filet.
        """
        valeurs = self._valeurs()
        if "SIG_CONTROLE" not in valeurs:
            self.skipTest("État SIG indisponible")
        self.assertAlmostEqual(
            valeurs["SIG_CONTROLE"], 0.0, places=2,
            msg="Le résultat de la cascade doit égaler le résultat calculé "
                "sur les classes 6 et 7. Un écart signale un compte oublié "
                "dans un des soldes intermédiaires.")

    def test_les_soldes_s_enchainent_dans_l_ordre_du_pcg(self):
        """Chaque solde se déduit du précédent, sans saut.

        L'ordre n'est pas une commodité de lecture : il est normalisé, et les
        ratios des banques et des centres de gestion s'appuient dessus.
        """
        v = self._valeurs()
        if "SIG_EBE" not in v:
            self.skipTest("État SIG indisponible")

        self.assertAlmostEqual(
            v["SIG_VALEUR_AJOUTEE"],
            v["SIG_MARGE"] + v["SIG_PRODUCTION"] - v["SIG_CONSOMMATIONS"],
            places=2, msg="Valeur ajoutée = marge + production − consommations")

        self.assertAlmostEqual(
            v["SIG_EBE"],
            v["SIG_VALEUR_AJOUTEE"] + v["SIG_SUBVENTIONS"]
            - v["SIG_IMPOTS_TAXES"] - v["SIG_PERSONNEL"],
            places=2,
            msg="Excédent brut = valeur ajoutée + subventions − impôts et "
                "taxes − charges de personnel")

        self.assertAlmostEqual(
            v["SIG_RESULTAT_COURANT"],
            v["SIG_RESULTAT_EXPL"] + v["SIG_PROD_FIN"] - v["SIG_CHARGES_FIN"],
            places=2,
            msg="Résultat courant = résultat d'exploitation + financier")

    def test_les_achats_de_marchandises_ne_comptent_qu_une_fois(self):
        """Le piège du compte 60, qui se scinde entre deux soldes.

        Le 607 appartient à la marge commerciale, le reste de la classe 60 aux
        consommations en provenance des tiers. Oublier de l'exclure des
        consommations le compterait deux fois et écraserait la valeur ajoutée
        du montant des achats de marchandises — une erreur invisible sur une
        entreprise de pur négoce, mais qui fausse tout dès qu'elle achète
        aussi des matières.
        """
        v = self._valeurs()
        if "SIG_CONSOMMATIONS" not in v:
            self.skipTest("État SIG indisponible")

        achats_marchandises = 0.0
        for compte, solde in self.env["account.move.line"]._read_group(
                domain=[
                    ("company_id", "=", self.env.company.id),
                    ("parent_state", "=", "posted"),
                    ("account_id.code", "=like", "607%"),
                ],
                groupby=["account_id"], aggregates=["balance:sum"]):
            achats_marchandises += solde

        if not achats_marchandises:
            self.skipTest("Aucun achat de marchandises dans cette base")

        consommations_brutes = 0.0
        for compte, solde in self.env["account.move.line"]._read_group(
                domain=[
                    ("company_id", "=", self.env.company.id),
                    ("parent_state", "=", "posted"),
                    ("account_id.code", "=like", "60%"),
                ],
                groupby=["account_id"], aggregates=["balance:sum"]):
            consommations_brutes += solde

        self.assertLess(
            v["SIG_CONSOMMATIONS"], consommations_brutes,
            "Les consommations doivent exclure les achats de marchandises, "
            "déjà portés à la marge commerciale")


@tagged("post_install", "-at_install")
class TestAffectationDuResultat(TransactionCase):
    """Affectation du résultat par le compte de résultat non affecté d'Odoo.

    Odoo ne passe pas d'écriture de clôture : pour affecter un résultat,
    l'utilisateur débite le compte de type ``equity_unaffected`` (999999) et
    crédite les réserves ou le report à nouveau. C'est la méthode standard,
    celle que suit l'édition Enterprise.

    Défaut trouvé pendant le portage en 20.0, par comparaison avec Enterprise
    sur un jeu d'écritures identique, et présent à l'identique en 19.0.
    """

    def setUp(self):
        super().setUp()
        pays = (self.env.company.account_fiscal_country_id.code
                or self.env.company.country_id.code)
        if pays != "FR":
            self.skipTest("Contrôles propres au plan comptable français")
        societe = self.env.company
        comptes = self.env["account.account"]

        def compte(domaine):
            trouve = comptes.search(
                domaine + [("company_ids", "in", societe.id)], limit=1, order="code")
            if not trouve:
                self.skipTest("Plan comptable incomplet : %s" % domaine)
            return trouve

        self.client_ = compte([("code", "=like", "411%")])
        self.vente = compte([("code", "=like", "706%")])
        self.reserves = compte([("code", "=like", "1068%")])
        self.non_affecte = compte([("account_type", "=", "equity_unaffected")])
        self.journal = self.env["account.journal"].search(
            [("type", "=", "general"), ("company_id", "=", societe.id)], limit=1)

    def _ecriture(self, jour, debit, credit, montant):
        ecriture = self.env["account.move"].create({
            "journal_id": self.journal.id, "date": jour, "ref": "AFFECTATION",
            "line_ids": [
                Command.create({"name": "A", "account_id": debit.id,
                                "debit": montant, "credit": 0.0}),
                Command.create({"name": "A", "account_id": credit.id,
                                "debit": 0.0, "credit": montant}),
            ],
        })
        ecriture.action_post()

    def _valeurs(self, xmlid, annee):
        rapport = self.env.ref(xmlid)
        options = rapport._expodo_get_options({"date": {
            "mode": "range", "filter": "custom",
            "date_from": date(annee, 1, 1), "date_to": date(annee, 12, 31)}})
        return rapport._expodo_compute_values(options, "main")

    def _scenario(self):
        """Un bénéfice en 2031, affecté aux réserves en 2032."""
        self._ecriture(date(2031, 6, 1), self.client_, self.vente, 1000.0)
        self._ecriture(date(2032, 5, 31), self.non_affecte, self.reserves, 1000.0)

    def test_le_bilan_francais_reste_equilibre_apres_affectation(self):
        """Le compte non affecté doit être lu, sinon le bilan boite.

        Sans ligne qui lise ``equity_unaffected``, l'affectation gonfle les
        réserves sans rien retirer au report à nouveau : le bilan est faux du
        montant affecté, et rien à l'écran ne le signale.
        """
        self._scenario()
        avant = self._valeurs("expodo_account_reports.report_bilan_fr", 2031)
        apres = self._valeurs("expodo_account_reports.report_bilan_fr", 2032)

        # L'équilibre est le contrôle décisif : c'est lui qui tombait, de
        # 1 000 exactement, tant qu'aucune ligne ne lisait le compte non
        # affecté.
        self.assertAlmostEqual(
            apres[("BILAN_ECART", "balance")], 0.0, places=2,
            msg="Le bilan doit rester équilibré après une affectation du résultat")

        # Les variations plutôt que les montants : la base de développement
        # porte d'autres écritures, et un contrôle en valeur absolue y mesure
        # l'historique de la base au lieu de mesurer l'affectation.
        def variation(code):
            return apres[(code, "balance")] - avant[(code, "balance")]

        self.assertAlmostEqual(
            variation("BILAN_CAPITAL"), 1000.0, places=2,
            msg="Les réserves reçoivent le résultat affecté")
        self.assertAlmostEqual(
            variation("BILAN_REPORT"), 0.0, places=2,
            msg="Le report à nouveau accueille le résultat antérieur puis le "
                "rend à l'affectation : au net il ne bouge pas")
        self.assertAlmostEqual(
            variation("BILAN_RESULTAT"), -1000.0, places=2,
            msg="Le résultat de l'exercice ne porte plus le bénéfice de 2031")


    def test_une_rubrique_sans_objet_ne_s_affiche_pas(self):
        """`hide_if_zero` doit effacer la ligne, pas seulement la déclarer.

        Le champ existe dans le coeur d'Odoo et trois lignes du module s'en
        servaient, mais le moteur ne le lisait pas : l'affectation du résultat
        restait affichée à zéro sur le bilan des sociétés qui n'affectent
        jamais par ce compte, c'est-à-dire la plupart.
        """
        rapport = self.env.ref("expodo_account_reports.report_bilan_fr")
        options = rapport._expodo_get_options({"date": {
            "mode": "range", "filter": "custom",
            "date_from": date(2031, 1, 1), "date_to": date(2031, 12, 31)}})
        lignes = rapport._expodo_serialize_report_lines(
            options, {"main": rapport._expodo_compute_values(options, "main")})
        codes = [l["code"] for l in lignes]
        self.assertNotIn(
            "BILAN_REPORT_AFFECTE", codes,
            "Sans aucune affectation, la rubrique n'a rien à dire et ne doit "
            "pas occuper une ligne du bilan")

        self._scenario()
        lignes = rapport._expodo_serialize_report_lines(
            options, {"main": rapport._expodo_compute_values(options, "main")})
        options_2032 = rapport._expodo_get_options({"date": {
            "mode": "range", "filter": "custom",
            "date_from": date(2032, 1, 1), "date_to": date(2032, 12, 31)}})
        lignes_2032 = rapport._expodo_serialize_report_lines(
            options_2032,
            {"main": rapport._expodo_compute_values(options_2032, "main")})
        self.assertIn(
            "BILAN_REPORT_AFFECTE", [l["code"] for l in lignes_2032],
            "Dès qu'une affectation existe, la rubrique doit reparaître")

    def test_le_bilan_universel_range_l_affectation_dans_le_resultat(self):
        """``equity_unaffected`` appartient au résultat, pas au capital.

        Rangé dans le capital, il fait paraître les réserves inchangées après
        une affectation et laisse le résultat gonflé du même montant. Les
        totaux restent justes, donc seule la lecture ligne à ligne le révèle.
        """
        self._scenario()
        avant = self._valeurs("expodo_account_reports.report_balance_sheet", 2031)
        apres = self._valeurs("expodo_account_reports.report_balance_sheet", 2032)
        self.assertAlmostEqual(
            apres[("BS_CAPITAL", "balance")] - avant[("BS_CAPITAL", "balance")],
            1000.0, places=2,
            msg="L'affectation doit se voir dans les capitaux propres")
        self.assertAlmostEqual(
            apres[("BS_RESULT", "balance")] - avant[("BS_RESULT", "balance")],
            -1000.0, places=2,
            msg="et retirer d'autant le résultat non affecté")
