# -*- coding: utf-8 -*-
# Copyright 2026 Expodo (https://expodo.fr)
# License LGPL-3
"""Budgets.

L'erreur de signe est le risque principal. Un budget de vente comparé à un
solde créditeur non renversé donne un écart du double du montant, dans le
mauvais sens — et le chiffre reste plausible.
"""

from datetime import date

from dateutil.relativedelta import relativedelta

from odoo import Command, fields
from odoo.exceptions import ValidationError
from odoo.tests import TransactionCase, tagged


@tagged("post_install", "-at_install")
class TestBudgets(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.societe = cls.env.company
        cls.journal = cls.env["account.journal"].search(
            [("type", "=", "general"), ("company_id", "=", cls.societe.id)], limit=1)
        comptes = cls.env["account.account"]
        cls.compte_client = comptes.search(
            [("account_type", "=", "asset_receivable")], limit=1)

        # Des comptes créés pour ce test, et pour lui seul.
        #
        # Réutiliser un compte du plan ferait entrer dans le réalisé les
        # écritures que d'autres tests — ou les données de démonstration — y
        # ont laissées. Le premier essai comparait ainsi un budget à un
        # réalisé doublé, et l'assertion échouait pour une raison sans rapport
        # avec ce qu'elle vérifiait.
        cls.compte_charge = comptes.create({
            "code": "699001", "name": "Charge d'essai budget",
            "account_type": "expense"})
        cls.compte_vente = comptes.create({
            "code": "799001", "name": "Produit d'essai budget",
            "account_type": "income"})

        # Le budget couvre largement, pour que les périodes de prorata —
        # calculées autour de la date du jour — tiennent à l'intérieur.
        cls.budget = cls.env["expodo.budget"].create({
            "name": "Budget d'essai",
            "company_id": cls.societe.id,
            "date_from": date(2020, 1, 1),
            "date_to": date(2035, 12, 31),
        })

    def _mouvement(self, compte, montant, jour=date(2026, 3, 1)):
        """Porte `montant` au débit de `compte`, contrepartie au client."""
        ecriture = self.env["account.move"].create({
            "journal_id": self.journal.id,
            "date": jour,
            "line_ids": [
                Command.create({
                    "name": "Mouvement", "account_id": compte.id,
                    "debit": montant if montant > 0 else 0.0,
                    "credit": -montant if montant < 0 else 0.0}),
                Command.create({
                    "name": "Contrepartie", "account_id": self.compte_client.id,
                    "debit": -montant if montant < 0 else 0.0,
                    "credit": montant if montant > 0 else 0.0}),
            ],
        })
        ecriture.action_post()
        return ecriture

    def _ligne(self, compte, prevu, debut=date(2026, 1, 1), fin=date(2026, 12, 31)):
        return self.env["expodo.budget.line"].create({
            "budget_id": self.budget.id,
            "account_id": compte.id,
            "date_from": debut, "date_to": fin,
            "planned_amount": prevu,
        })

    # ------------------------------------------------------------------
    # Signes
    # ------------------------------------------------------------------

    def test_une_charge_realisee_est_positive(self):
        self._mouvement(self.compte_charge, 4000.0)
        ligne = self._ligne(self.compte_charge, 10000.0)
        self.assertAlmostEqual(ligne.actual_amount, 4000.0, places=2)

    def test_un_produit_realise_est_positif_lui_aussi(self):
        """Le piège du module.

        Les produits ont un solde créditeur, donc négatif. Sans renversement,
        un budget de vente de cent mille face à un réalisé de moins quatre-vingt
        mille donnerait un écart de moins cent quatre-vingt mille — plausible,
        et faux du double.
        """
        self._mouvement(self.compte_vente, -8000.0)
        ligne = self._ligne(self.compte_vente, 20000.0)
        self.assertAlmostEqual(
            ligne.actual_amount, 8000.0, places=2,
            msg="Un produit réalisé doit se présenter positif, comme son budget")

    def test_l_ecart_se_lit_dans_le_bon_sens(self):
        self._mouvement(self.compte_charge, 12000.0)
        ligne = self._ligne(self.compte_charge, 10000.0)
        self.assertAlmostEqual(
            ligne.variance, 2000.0, places=2,
            msg="Un dépassement de charge doit donner un écart positif")

    # ------------------------------------------------------------------
    # Prorata
    # ------------------------------------------------------------------

    def test_le_budget_au_prorata_suit_la_part_ecoulee(self):
        """Sans lui, l'écart paraît toujours favorable en cours de période."""
        aujourd_hui = fields.Date.context_today(self.env.user)
        ligne = self._ligne(
            self.compte_charge, 36500.0,
            debut=aujourd_hui - relativedelta(days=99),
            fin=aujourd_hui + relativedelta(days=265))
        # Cent jours écoulés sur trois cent soixante-cinq.
        self.assertAlmostEqual(
            ligne.prorated_amount, 36500.0 * 100 / 365, delta=200.0)

    def test_une_periode_achevee_prorate_la_totalite(self):
        ligne = self._ligne(
            self.compte_charge, 5000.0,
            debut=date(2026, 1, 1), fin=date(2026, 1, 31))
        self.assertAlmostEqual(ligne.prorated_amount, 5000.0, places=2)

    def test_une_periode_non_commencee_ne_prorate_rien(self):
        ligne = self._ligne(
            self.compte_charge, 5000.0,
            debut=date(2034, 1, 1), fin=date(2034, 12, 31))
        self.assertAlmostEqual(ligne.prorated_amount, 0.0, places=2)

    # ------------------------------------------------------------------
    # Périodes
    # ------------------------------------------------------------------

    def test_une_ligne_ne_deborde_pas_de_son_budget(self):
        """Elle ne serait comptée dans aucun total."""
        with self.assertRaises(ValidationError):
            self._ligne(self.compte_charge, 1000.0,
                        debut=date(2019, 6, 1), fin=date(2020, 6, 30))

    def test_une_periode_a_l_envers_est_refusee(self):
        with self.assertRaises(ValidationError):
            self._ligne(self.compte_charge, 1000.0,
                        debut=date(2026, 6, 30), fin=date(2026, 1, 1))

    def test_deux_lignes_du_meme_compte_sur_des_mois_differents(self):
        """Chacune ne voit que les écritures de sa propre période."""
        self._mouvement(self.compte_charge, 1000.0, jour=date(2026, 2, 10))
        self._mouvement(self.compte_charge, 3000.0, jour=date(2026, 5, 10))
        fevrier = self._ligne(self.compte_charge, 2000.0,
                              debut=date(2026, 2, 1), fin=date(2026, 2, 28))
        mai = self._ligne(self.compte_charge, 2000.0,
                          debut=date(2026, 5, 1), fin=date(2026, 5, 31))
        self.assertAlmostEqual(fevrier.actual_amount, 1000.0, places=2)
        self.assertAlmostEqual(mai.actual_amount, 3000.0, places=2)

    # ------------------------------------------------------------------
    # Totaux et navigation
    # ------------------------------------------------------------------

    def test_les_totaux_du_budget_somment_ses_lignes(self):
        self._mouvement(self.compte_charge, 4000.0)
        self._ligne(self.compte_charge, 10000.0)
        self._ligne(self.compte_vente, 20000.0)
        self.assertAlmostEqual(self.budget.total_planned, 30000.0, places=2)
        self.assertAlmostEqual(
            self.budget.total_variance,
            self.budget.total_actual - self.budget.total_planned, places=2)

    def test_une_ligne_ouvre_les_ecritures_de_son_realise(self):
        """Un écart qu'on ne peut pas ouvrir se discute indéfiniment."""
        self._mouvement(self.compte_charge, 4000.0)
        ligne = self._ligne(self.compte_charge, 10000.0)
        action = ligne.action_open_entries()
        self.assertEqual(action["res_model"], "account.move.line")
        lignes = self.env["account.move.line"].search(action["domain"])
        self.assertAlmostEqual(
            sum(lignes.mapped("balance")), 4000.0, places=2,
            msg="Les écritures ouvertes doivent être exactement celles du "
                "réalisé affiché")

    def test_les_brouillons_ne_comptent_pas_dans_le_realise(self):
        """Un budget qui inclurait les brouillons se dégraderait à chaque
        saisie non validée, puis reviendrait en arrière à l'annulation."""
        self.env["account.move"].create({
            "journal_id": self.journal.id,
            "date": date(2026, 4, 1),
            "line_ids": [
                Command.create({
                    "name": "Non validé", "account_id": self.compte_charge.id,
                    "debit": 9999.0, "credit": 0.0}),
                Command.create({
                    "name": "Non validé", "account_id": self.compte_client.id,
                    "debit": 0.0, "credit": 9999.0}),
            ],
        })
        ligne = self._ligne(self.compte_charge, 10000.0)
        self.assertAlmostEqual(ligne.actual_amount, 0.0, places=2)


@tagged("post_install", "-at_install")
class TestBudgetMixte(TransactionCase):
    """Un budget qui mêle produits et charges.

    Les deux natures se saisissent en positif, pour ne pas demander un
    nombre négatif à celui qui prévoit vingt mille de ventes. Les additionner
    revient alors à ajouter des euros gagnés à des euros dépensés : le total
    n'a aucun sens, et il est d'autant plus trompeur qu'il paraît juste.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.societe = cls.env.company
        comptes = cls.env["account.account"]
        cls.charge = comptes.search(
            [("account_type", "=", "expense"),
             ("company_ids", "in", cls.societe.id)], limit=1, order="code")
        cls.produit = comptes.search(
            [("account_type", "=", "income"),
             ("company_ids", "in", cls.societe.id)], limit=1, order="code")
        cls.journal = cls.env["account.journal"].search(
            [("type", "=", "general"), ("company_id", "=", cls.societe.id)],
            limit=1)
        cls.contrepartie = comptes.search(
            [("account_type", "=", "asset_receivable"),
             ("company_ids", "in", cls.societe.id)], limit=1, order="code")

    def _ecrire(self, compte, debit, credit):
        self.env["account.move"].create({
            "journal_id": self.journal.id, "date": date(2033, 6, 15),
            "line_ids": [
                Command.create({"name": "b", "account_id": compte.id,
                                "debit": debit, "credit": credit}),
                Command.create({"name": "b", "account_id": self.contrepartie.id,
                                "debit": credit, "credit": debit}),
            ]}).action_post()

    def _budget(self):
        return self.env["expodo.budget"].create({
            "name": "Budget mixte", "company_id": self.societe.id,
            "date_from": date(2033, 1, 1), "date_to": date(2033, 12, 31),
            "line_ids": [
                Command.create({
                    "account_id": self.charge.id, "planned_amount": 1000.0,
                    "date_from": date(2033, 1, 1), "date_to": date(2033, 12, 31)}),
                Command.create({
                    "account_id": self.produit.id, "planned_amount": 20000.0,
                    "date_from": date(2033, 1, 1), "date_to": date(2033, 12, 31)}),
            ]})

    def test_un_budget_de_deux_natures_se_signale(self):
        budget = self._budget()
        self.assertTrue(
            budget.mixed_natures,
            "Un budget qui porte des produits et des charges doit le dire, "
            "sinon ses totaux se lisent comme s'ils avaient un sens")

    def test_l_effet_sur_le_resultat_porte_le_signe_de_la_nature(self):
        """Mille de charge en trop et sept mille de vente en moins pèsent
        tous deux sur le résultat."""
        self._ecrire(self.charge, 6500.0, 0.0)
        self._ecrire(self.produit, 0.0, 13000.0)
        budget = self._budget()
        self.assertAlmostEqual(
            budget.total_impact, -12500.0, places=2,
            msg="Une charge dépassée et un produit manqué se cumulent, ils ne "
                "se compensent pas")

    def test_un_budget_d_une_seule_nature_garde_ses_totaux(self):
        budget = self.env["expodo.budget"].create({
            "name": "Budget de charges", "company_id": self.societe.id,
            "date_from": date(2033, 1, 1), "date_to": date(2033, 12, 31),
            "line_ids": [Command.create({
                "account_id": self.charge.id, "planned_amount": 1000.0,
                "date_from": date(2033, 1, 1),
                "date_to": date(2033, 12, 31)})]})
        self.assertFalse(budget.mixed_natures)
        self.assertAlmostEqual(budget.total_planned, 1000.0, places=2)

    def test_la_nature_du_compte_est_lisible_sur_la_ligne(self):
        """Une décoration de liste n'évalue pas un chemin pointé.

        ``account_id.internal_group`` ne vaut rien côté navigateur : les
        lignes en dépassement ne se coloraient pas, et la seule lecture
        immédiate du tableau était perdue.
        """
        budget = self._budget()
        ligne = budget.line_ids[0]
        self.assertIn("account_internal_group", ligne._fields)
        self.assertEqual(
            ligne.account_internal_group, ligne.account_id.internal_group)


@tagged("post_install", "-at_install")
class TestVuesBudget(TransactionCase):
    """Ce que la vue déclare, et que seul un utilisateur voyait."""

    def _arch(self, xmlid):
        return self.env.ref("expodo_account_budget." + xmlid).arch

    def test_le_taux_de_realisation_n_est_pas_multiplie_deux_fois(self):
        """``achievement`` est déjà un pourcentage.

        Le widget ``percentage`` multiplie par cent ce qu'il reçoit : un
        réalisé de 127,5 % s'affichait 12 750 %.
        """
        self.assertNotIn(
            'name="achievement" widget="percentage"',
            self._arch("view_expodo_budget_line_list"),
            "Le champ vaut déjà des pour cent, le widget les multiplie encore")

    def test_les_decorations_n_empruntent_aucun_chemin_pointe(self):
        arch = self._arch("view_expodo_budget_line_list")
        for decoration in ("decoration-danger", "decoration-success"):
            debut = arch.find(decoration)
            if debut < 0:
                continue
            fin = arch.find('"', arch.find('"', debut) + 1)
            expression = arch[debut:fin]
            self.assertNotIn(
                "account_id.", expression,
                "Une décoration s'évalue côté navigateur, sur les seuls "
                "champs présents dans la vue")

    def test_les_totaux_de_deux_natures_sont_masques(self):
        arch = self._arch("view_expodo_budget_form")
        self.assertIn("mixed_natures", arch)
        self.assertIn("total_impact", arch)
