# -*- coding: utf-8 -*-
# Copyright 2026 Expodo (https://expodo.fr)
# License LGPL-3
"""Lettrage manuel et automatique.

Un lettrage erroné est plus coûteux qu'un lettrage manquant : il se défait à la
main, écriture par écriture, et masque entre-temps une créance réellement
impayée. Ces tests portent donc autant sur ce que le module **refuse** de faire
que sur ce qu'il fait.
"""

from datetime import date

from odoo import Command
from odoo.exceptions import UserError
from odoo.tests import TransactionCase, tagged


@tagged("post_install", "-at_install")
class TestLettrage(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.societe = cls.env.company
        cls.journal = cls.env["account.journal"].search(
            [("type", "=", "general"), ("company_id", "=", cls.societe.id)], limit=1)
        comptes = cls.env["account.account"]
        cls.compte_client = comptes.search([
            ("account_type", "=", "asset_receivable"), ("reconcile", "=", True)], limit=1)
        cls.compte_vente = comptes.search([("account_type", "=", "income")], limit=1)
        cls.compte_charge = comptes.search([("account_type", "=", "expense")], limit=1)
        cls.partenaire = cls.env["res.partner"].create({"name": "Client à lettrer"})
        cls.autre = cls.env["res.partner"].create({"name": "Autre client"})

    def _mouvement(self, montant, partenaire=None, jour=date(2026, 3, 1)):
        """Une écriture portant `montant` au compte client (négatif = crédit)."""
        partenaire = partenaire or self.partenaire
        ecriture = self.env["account.move"].create({
            "journal_id": self.journal.id,
            "date": jour,
            "line_ids": [
                Command.create({
                    "name": "Mouvement",
                    "account_id": self.compte_client.id,
                    "partner_id": partenaire.id,
                    "debit": montant if montant > 0 else 0.0,
                    "credit": -montant if montant < 0 else 0.0}),
                Command.create({
                    "name": "Contrepartie",
                    "account_id": self.compte_vente.id,
                    "debit": -montant if montant < 0 else 0.0,
                    "credit": montant if montant > 0 else 0.0}),
            ],
        })
        ecriture.action_post()
        return ecriture.line_ids.filtered(
            lambda l: l.account_id == self.compte_client)

    def _assistant(self, lignes):
        return self.env["expodo.reconcile.wizard"].with_context(
            active_model="account.move.line", active_ids=lignes.ids).create({})

    # ------------------------------------------------------------------
    # Lettrage manuel
    # ------------------------------------------------------------------

    def test_deux_lignes_qui_se_compensent_se_lettrent(self):
        lignes = self._mouvement(1000.0) | self._mouvement(-1000.0)
        assistant = self._assistant(lignes)
        self.assertAlmostEqual(assistant.difference, 0.0, places=2)
        assistant.action_reconcile()
        self.assertTrue(
            all(lignes.mapped("reconciled")),
            "Deux lignes de sens opposé et de même montant doivent se solder")

    def test_l_ecart_est_affiche_avant_toute_action(self):
        """L'utilisateur doit voir l'écart avant de décider, pas après."""
        lignes = self._mouvement(1000.0) | self._mouvement(-940.0)
        assistant = self._assistant(lignes)
        self.assertAlmostEqual(assistant.difference, 60.0, places=2)
        self.assertAlmostEqual(assistant.total_debit, 1000.0, places=2)
        self.assertAlmostEqual(assistant.total_credit, 940.0, places=2)

    def test_l_ecart_peut_etre_passe_en_charge(self):
        lignes = self._mouvement(1000.0) | self._mouvement(-940.0)
        assistant = self._assistant(lignes)
        assistant.write({
            "write_off": True,
            "write_off_account_id": self.compte_charge.id,
            "write_off_journal_id": self.journal.id,
            "write_off_label": "Escompte accordé",
        })
        assistant.action_reconcile()
        self.assertTrue(
            all(lignes.mapped("reconciled")),
            "Après passation de l'écart, les lignes doivent être soldées")

    def test_l_ecriture_d_ecart_n_est_jamais_antidatee(self):
        """Antidater placerait une écriture dans une période close.

        Elle produirait aussi une date de validation antérieure à l'écriture,
        anomalie que l'administration recherche en priorité dans un FEC.
        """
        ancienne = self._mouvement(1000.0, jour=date(2026, 1, 15))
        recente = self._mouvement(-940.0, jour=date(2026, 6, 20))
        assistant = self._assistant(ancienne | recente)
        assistant.write({
            "write_off": True,
            "write_off_account_id": self.compte_charge.id,
            "write_off_journal_id": self.journal.id,
        })
        assistant.action_reconcile()
        ecart = self.env["account.move"].search([
            ("company_id", "=", self.societe.id),
            ("journal_id", "=", self.journal.id),
        ], order="id desc", limit=1)
        self.assertGreaterEqual(
            ecart.date, date(2026, 6, 20),
            "L'écriture d'écart doit porter la date la plus récente de la "
            "sélection, jamais une date antérieure")

    # ------------------------------------------------------------------
    # Refus
    # ------------------------------------------------------------------

    def test_une_seule_ligne_est_refusee(self):
        ligne = self._mouvement(500.0)
        with self.assertRaises(UserError):
            self._assistant(ligne)

    def test_des_comptes_differents_sont_refuses(self):
        """Le lettrage rapproche des lignes d'un même compte."""
        ligne = self._mouvement(500.0)
        ecriture = self.env["account.move"].create({
            "journal_id": self.journal.id,
            "date": date(2026, 3, 1),
            "line_ids": [
                Command.create({
                    "name": "Ailleurs", "account_id": self.compte_charge.id,
                    "debit": 500.0, "credit": 0.0}),
                Command.create({
                    "name": "Ailleurs", "account_id": self.compte_vente.id,
                    "debit": 0.0, "credit": 500.0}),
            ],
        })
        ecriture.action_post()
        autre = ecriture.line_ids.filtered(
            lambda l: l.account_id == self.compte_charge)
        with self.assertRaises(UserError):
            self._assistant(ligne | autre)

    def test_un_brouillon_est_refuse(self):
        lignes = self._mouvement(800.0) | self._mouvement(-800.0)
        brouillon = self.env["account.move"].create({
            "journal_id": self.journal.id,
            "date": date(2026, 3, 1),
            "line_ids": [
                Command.create({
                    "name": "Brouillon", "account_id": self.compte_client.id,
                    "partner_id": self.partenaire.id,
                    "debit": 0.0, "credit": 800.0}),
                Command.create({
                    "name": "Brouillon", "account_id": self.compte_vente.id,
                    "debit": 800.0, "credit": 0.0}),
            ],
        })
        ligne_brouillon = brouillon.line_ids.filtered(
            lambda l: l.account_id == self.compte_client)
        with self.assertRaises(UserError):
            self._assistant(lignes | ligne_brouillon)

    # ------------------------------------------------------------------
    # Lettrage automatique
    # ------------------------------------------------------------------

    def _auto(self):
        assistant = self.env["expodo.auto.reconcile.wizard"].create({
            "company_id": self.societe.id,
            "account_ids": [(6, 0, self.compte_client.ids)],
            "date_to": date(2026, 12, 31),
        })
        assistant.action_auto_reconcile()
        return assistant

    def test_un_solde_nul_par_tiers_se_lettre_tout_seul(self):
        lignes = (self._mouvement(2500.0) | self._mouvement(-1500.0)
                  | self._mouvement(-1000.0))
        self._auto()
        self.assertTrue(
            all(lignes.mapped("reconciled")),
            "Trois lignes qui se compensent exactement doivent se lettrer")

    def test_un_solde_non_nul_est_laisse_tel_quel(self):
        """Mieux vaut laisser du travail que cacher une créance impayée."""
        lignes = self._mouvement(2000.0) | self._mouvement(-1200.0)
        self._auto()
        self.assertFalse(
            any(lignes.mapped("reconciled")),
            "Un groupe dont le solde n'est pas nul ne doit pas être lettré")

    def test_deux_tiers_ne_sont_jamais_melanges(self):
        """Compenser un client par un autre masquerait deux anomalies à la fois.

        La créance impayée du premier disparaîtrait, et l'avoir du second
        aussi. C'est le lettrage automatique le plus tentant et le plus faux.
        """
        chez_lui = self._mouvement(700.0, partenaire=self.partenaire)
        chez_elle = self._mouvement(-700.0, partenaire=self.autre)
        self._auto()
        self.assertFalse(
            chez_lui.reconciled or chez_elle.reconciled,
            "Des lignes de tiers différents ne doivent jamais se compenser")

    def test_un_groupe_de_meme_sens_n_est_pas_lettre(self):
        """Deux débits ne se compensent pas, quel que soit leur total."""
        lignes = self._mouvement(300.0) | self._mouvement(400.0)
        self._auto()
        self.assertFalse(any(lignes.mapped("reconciled")))

    def test_le_compte_rendu_denombre_ce_qui_a_ete_fait(self):
        self._mouvement(900.0)
        self._mouvement(-900.0)
        assistant = self._auto()
        self.assertGreaterEqual(assistant.reconciled_count, 2)
        self.assertGreaterEqual(assistant.group_count, 1)
        self.assertTrue(assistant.message)
