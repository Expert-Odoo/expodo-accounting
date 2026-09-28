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

    def test_un_budget_mixte_se_lit_par_son_effet_sur_le_resultat(self):
        """Ventes sous le prévu et charges au-dessus sont deux écarts
        défavorables : ils s'additionnent, ils ne se compensent pas."""
        self._mouvement(self.compte_charge, 6500.0)
        self._mouvement(self.compte_vente, -13000.0)
        self._ligne(self.compte_charge, 1000.0)
        self._ligne(self.compte_vente, 20000.0)
        self.assertTrue(self.budget.mixed_natures)
        # Charges : 5 500 au-dessus du prévu ; ventes : 7 000 en dessous.
        self.assertAlmostEqual(self.budget.total_impact, -12500.0, places=2)

    def test_un_budget_homogene_garde_ses_totaux(self):
        self._ligne(self.compte_charge, 10000.0)
        self.assertFalse(self.budget.mixed_natures)

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
