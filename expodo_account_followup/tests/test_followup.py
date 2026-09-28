# -*- coding: utf-8 -*-
# Copyright 2026 Expodo (https://expodo.fr)
# License LGPL-3
"""Relances clients.

Deux erreurs coûtent plus cher que les factures qu'elles réclament : relancer
un client qui a payé, et relancer au mauvais niveau. Les tests portent d'abord
sur ce que le module **ne relance pas**.
"""

from datetime import date

from dateutil.relativedelta import relativedelta

from odoo import Command, fields
from odoo.exceptions import ValidationError
from odoo.tests import TransactionCase, tagged


@tagged("post_install", "-at_install")
class TestRelances(TransactionCase):

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

        cls.client = cls.env["res.partner"].create({
            "name": "Client retardataire", "email": "client@exemple.fr"})

        cls.niveaux = cls.env["expodo.followup.level"]
        for jours, nom in ((7, "Rappel aimable"), (30, "Relance"),
                           (60, "Mise en demeure")):
            cls.niveaux |= cls.env["expodo.followup.level"].create({
                "name": nom, "company_id": cls.societe.id,
                "delay_days": jours, "send_email": True,
                "subject": nom, "body": "<p>%s</p>" % nom})

    def _facture(self, montant=1000.0, jours_de_retard=45, partenaire=None,
                 exclue=False):
        partenaire = partenaire or self.client
        echeance = fields.Date.context_today(self.env.user) - relativedelta(
            days=jours_de_retard)
        ecriture = self.env["account.move"].create({
            "journal_id": self.journal.id,
            "date": echeance,
            "line_ids": [
                Command.create({
                    "name": "Facture", "account_id": self.compte_client.id,
                    "partner_id": partenaire.id,
                    "date_maturity": echeance,
                    "no_followup": exclue,
                    "debit": montant, "credit": 0.0}),
                Command.create({
                    "name": "Facture", "account_id": self.compte_vente.id,
                    "debit": 0.0, "credit": montant}),
            ],
        })
        ecriture.action_post()
        return ecriture

    # ------------------------------------------------------------------
    # Ce qui n'est pas relancé
    # ------------------------------------------------------------------

    def test_une_facture_non_echue_ne_declenche_rien(self):
        """Relancer avant l'échéance abîme la relation sans rien rapporter."""
        self._facture(jours_de_retard=-15)
        self.client.invalidate_recordset()
        self.assertAlmostEqual(self.client.followup_amount_due, 0.0, places=2)
        self.assertFalse(self.client.followup_level_id)

    def test_une_facture_lettree_ne_declenche_rien(self):
        """Réclamer une facture payée est l'erreur la plus coûteuse du lot."""
        facture = self._facture()
        reglement = self.env["account.move"].create({
            "journal_id": self.journal.id,
            "date": fields.Date.context_today(self.env.user),
            "line_ids": [
                Command.create({
                    "name": "Règlement", "account_id": self.compte_client.id,
                    "partner_id": self.client.id,
                    "debit": 0.0, "credit": 1000.0}),
                Command.create({
                    "name": "Règlement", "account_id": self.compte_vente.id,
                    "debit": 1000.0, "credit": 0.0}),
            ],
        })
        reglement.action_post()
        lignes = (facture.line_ids | reglement.line_ids).filtered(
            lambda l: l.account_id == self.compte_client)
        lignes.reconcile()

        self.client.invalidate_recordset()
        self.assertAlmostEqual(self.client.followup_amount_due, 0.0, places=2)

    def test_un_reglement_non_lettre_sans_echeance_vient_en_deduction(self):
        """Un règlement saisi sans échéance et pas encore lettré était ignoré :
        le client qui avait payé restait relancé du montant de sa facture."""
        self._facture(montant=1000.0)
        # Journal de banque : en 20.0, les lignes d'un journal d'opérations
        # diverses naissent « sans relance » (no_followup).
        banque = self.env["account.journal"].search(
            [("type", "=", "bank"), ("company_id", "=", self.societe.id)], limit=1)
        reglement = self.env["account.move"].create({
            "journal_id": banque.id,
            "date": fields.Date.context_today(self.env.user),
            "line_ids": [
                Command.create({
                    "name": "Règlement", "account_id": self.compte_client.id,
                    "partner_id": self.client.id, "date_maturity": False,
                    "debit": 0.0, "credit": 1000.0}),
                Command.create({
                    "name": "Règlement", "account_id": self.compte_vente.id,
                    "debit": 1000.0, "credit": 0.0}),
            ],
        })
        reglement.action_post()
        self.client.invalidate_recordset()
        self.assertAlmostEqual(self.client.followup_amount_due, 0.0, places=2)
        self.assertFalse(self.client.followup_level_id)
        self.assertNotIn(self.client, self.env["res.partner"].search(
            [("followup_amount_due", ">", 0)]))

    def test_un_acompte_partiel_reduit_le_montant_relance(self):
        self._facture(montant=1000.0)
        banque = self.env["account.journal"].search(
            [("type", "=", "bank"), ("company_id", "=", self.societe.id)], limit=1)
        acompte = self.env["account.move"].create({
            "journal_id": banque.id,
            "date": fields.Date.context_today(self.env.user),
            "line_ids": [
                Command.create({
                    "name": "Acompte", "account_id": self.compte_client.id,
                    "partner_id": self.client.id,
                    "debit": 0.0, "credit": 400.0}),
                Command.create({
                    "name": "Acompte", "account_id": self.compte_vente.id,
                    "debit": 400.0, "credit": 0.0}),
            ],
        })
        acompte.action_post()
        self.client.invalidate_recordset()
        self.assertAlmostEqual(self.client.followup_amount_due, 600.0, places=2)
        self.assertTrue(self.client.followup_level_id)

    def test_une_ligne_marquee_non_relancable_est_respectee(self):
        """Elle a été marquée pour une raison, saisie une fois."""
        self._facture(exclue=True)
        self.client.invalidate_recordset()
        self.assertAlmostEqual(self.client.followup_amount_due, 0.0, places=2)

    # ------------------------------------------------------------------
    # Le niveau atteint
    # ------------------------------------------------------------------

    def test_le_niveau_est_le_dernier_franchi_et_non_le_premier(self):
        """Un client en retard de quatre-vingt-dix jours doit recevoir la mise
        en demeure, pas le rappel aimable qu'il a déjà eu deux fois."""
        self._facture(jours_de_retard=90)
        self.client.invalidate_recordset()
        self.assertEqual(self.client.followup_level_id.delay_days, 60)

    def test_un_retard_intermediaire_donne_le_niveau_intermediaire(self):
        self._facture(jours_de_retard=45)
        self.client.invalidate_recordset()
        self.assertEqual(self.client.followup_level_id.delay_days, 30)

    def test_un_retard_inferieur_au_premier_seuil_ne_donne_aucun_niveau(self):
        self._facture(jours_de_retard=3)
        self.client.invalidate_recordset()
        self.assertFalse(self.client.followup_level_id)

    def test_le_retard_se_compte_sur_la_plus_ancienne_echeance(self):
        """Une facture récente ne doit pas masquer une créance ancienne."""
        self._facture(montant=500.0, jours_de_retard=90)
        self._facture(montant=800.0, jours_de_retard=10)
        self.client.invalidate_recordset()
        self.assertEqual(self.client.followup_days_overdue, 90)
        self.assertAlmostEqual(self.client.followup_amount_due, 1300.0, places=2)

    # ------------------------------------------------------------------
    # Configuration
    # ------------------------------------------------------------------

    def test_deux_niveaux_au_meme_delai_sont_refuses(self):
        """La gradation deviendrait arbitraire : selon l'ordre de lecture, le
        client recevrait le rappel aimable ou la mise en demeure."""
        with self.assertRaises(ValidationError):
            self.env["expodo.followup.level"].create({
                "name": "Doublon", "company_id": self.societe.id,
                "delay_days": 30})

    # ------------------------------------------------------------------
    # Envoi
    # ------------------------------------------------------------------

    def test_l_envoi_note_la_date(self):
        """Relancer deux fois la même semaine abîme la relation."""
        self._facture(jours_de_retard=45)
        self.client.invalidate_recordset()
        self.client.action_send_followup()
        self.assertEqual(
            self.client.followup_last_date,
            fields.Date.context_today(self.env.user))

    def test_un_client_sans_courriel_est_ignore_sans_bruit(self):
        """Sélectionner large et laisser le module trier est plus sûr que
        demander à l'utilisateur de trier lui-même."""
        muet = self.env["res.partner"].create({"name": "Client sans courriel"})
        self._facture(jours_de_retard=45, partenaire=muet)
        muet.invalidate_recordset()
        muet.action_send_followup()
        self.assertFalse(muet.followup_last_date)

    def test_les_ecritures_echues_sont_ouvrables(self):
        """Un montant réclamé qu'on ne peut pas ouvrir se conteste."""
        self._facture(montant=1000.0, jours_de_retard=45)
        self.client.invalidate_recordset()
        action = self.client.action_open_overdue()
        lignes = self.env["account.move.line"].search(action["domain"])
        self.assertAlmostEqual(
            sum(lignes.mapped("amount_residual")),
            self.client.followup_amount_due, places=2,
            msg="Les écritures ouvertes doivent être exactement celles du "
                "montant réclamé")
