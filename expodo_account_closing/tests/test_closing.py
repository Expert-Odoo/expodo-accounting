# -*- coding: utf-8 -*-
# Copyright 2026 Expodo (https://expodo.fr)
# License LGPL-3
"""Clôture d'exercice et à-nouveaux.

Une clôture fausse ne se voit pas tout de suite. Elle se voit un an plus tard,
quand le bilan d'ouverture ne correspond à rien et qu'il faut remonter douze
mois d'écritures pour comprendre. Ces tests portent donc sur les identités
comptables — équilibre, comptes soldés, report du résultat — plutôt que sur le
bon fonctionnement apparent de l'assistant.
"""

from datetime import date

from odoo import Command
from odoo.exceptions import UserError
from odoo.tests import TransactionCase, tagged


@tagged("post_install", "-at_install")
class TestClotureExercice(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.societe = cls.env.company
        cls.journal = cls.env["account.journal"].search(
            [("type", "=", "general"), ("company_id", "=", cls.societe.id)], limit=1)
        comptes = cls.env["account.account"]
        cls.compte_client = comptes.search(
            [("account_type", "=", "asset_receivable")], limit=1)
        cls.compte_vente = comptes.search([("account_type", "=", "income")], limit=1)
        cls.compte_achat = comptes.search([("account_type", "=", "expense")], limit=1)
        cls.moteur = cls.env["expodo.closing.engine"]

        cls.env["account.move"].create({
            "journal_id": cls.journal.id,
            "date": date(2026, 6, 30),
            "ref": "VENTE-EXERCICE",
            "line_ids": [
                Command.create({
                    "name": "Vente", "account_id": cls.compte_client.id,
                    "debit": 10000.0, "credit": 0.0}),
                Command.create({
                    "name": "Vente", "account_id": cls.compte_vente.id,
                    "debit": 0.0, "credit": 10000.0}),
            ],
        }).action_post()

        cls.env["account.move"].create({
            "journal_id": cls.journal.id,
            "date": date(2026, 7, 31),
            "ref": "ACHAT-EXERCICE",
            "line_ids": [
                Command.create({
                    "name": "Achat", "account_id": cls.compte_achat.id,
                    "debit": 4000.0, "credit": 0.0}),
                Command.create({
                    "name": "Achat", "account_id": cls.compte_client.id,
                    "debit": 0.0, "credit": 4000.0}),
            ],
        }).action_post()

    # ------------------------------------------------------------------
    # Utilitaires
    # ------------------------------------------------------------------

    def _solde_groupe(self, groupes, jusqu_au=date(2026, 12, 31)):
        total = 0.0
        for _compte, solde in self.env["account.move.line"]._read_group(
                domain=[
                    ("company_id", "=", self.societe.id),
                    ("parent_state", "=", "posted"),
                    ("date", "<=", jusqu_au),
                    ("account_id.internal_group", "in", groupes),
                ],
                groupby=["account_id"], aggregates=["balance:sum"]):
            total += solde
        return total

    def _cloturer(self):
        cloture = self.moteur.ecriture_cloture(
            self.societe, date(2026, 1, 1), date(2026, 12, 31), self.journal)
        cloture.action_post()
        return cloture

    # ------------------------------------------------------------------
    # Identités comptables
    # ------------------------------------------------------------------

    def test_l_ecriture_de_cloture_est_equilibree(self):
        cloture = self.moteur.ecriture_cloture(
            self.societe, date(2026, 1, 1), date(2026, 12, 31), self.journal)
        self.assertAlmostEqual(
            sum(cloture.line_ids.mapped("debit")),
            sum(cloture.line_ids.mapped("credit")), places=2,
            msg="Une écriture de clôture déséquilibrée fait plus de dégâts "
                "qu'une clôture non faite")

    def test_les_comptes_de_gestion_sont_a_zero_apres_cloture(self):
        """C'est la définition même de la clôture.

        Un compte de gestion non soldé se reporte silencieusement sur
        l'exercice suivant et gonfle son résultat.
        """
        self._cloturer()
        self.assertAlmostEqual(
            self._solde_groupe(["income", "expense"]), 0.0, places=2,
            msg="Les classes de gestion doivent être soldées")

    def test_aucun_type_de_compte_de_gestion_n_est_oublie(self):
        """Régression : `expense_other` avait été omis.

        La première version énumérait les types un par un et oubliait les
        charges exceptionnelles — celles des cessions d'immobilisation. La
        clôture les laissait non soldées et l'écriture d'à-nouveaux sortait
        déséquilibrée du montant exact de ces charges.

        Le moteur s'appuie désormais sur `internal_group`, la classification
        qu'Odoo tient lui-même. Ce test vérifie qu'aucun type de compte de
        gestion n'échappe à ce filet, y compris ceux qu'Odoo ajoutera.
        """
        types_gestion = [
            t for t, _lib in self.env["account.account"]._fields[
                "account_type"].selection
            if t.startswith(("income", "expense"))
        ]
        comptes = self.env["account.account"].search([
            ("account_type", "in", types_gestion)])
        groupes = set(comptes.mapped("internal_group"))
        oublies = groupes - set(self.moteur._groupes_gestion())
        self.assertFalse(
            oublies,
            "Ces groupes de comptes de gestion échapperaient à la clôture : %s"
            % oublies)

    def test_le_resultat_est_porte_au_bilan(self):
        """Le résultat quitte les comptes de gestion sans disparaître."""
        resultat = -self._solde_groupe(["income", "expense"])
        avant = self._solde_groupe(["asset", "liability", "equity"])
        self._cloturer()
        apres = self._solde_groupe(["asset", "liability", "equity"])
        self.assertAlmostEqual(
            apres - avant, -resultat, places=2,
            msg="Le résultat doit se retrouver au bilan, pas s'évaporer")

    def test_l_ecriture_d_a_nouveaux_est_equilibree(self):
        self._cloturer()
        ouverture = self.moteur.ecriture_a_nouveaux(
            self.societe, date(2026, 1, 1), date(2026, 12, 31),
            self.journal, date(2027, 1, 1))
        self.assertAlmostEqual(
            sum(ouverture.line_ids.mapped("debit")),
            sum(ouverture.line_ids.mapped("credit")), places=2)

    def test_les_a_nouveaux_reprennent_exactement_les_soldes_de_bilan(self):
        """Le bilan d'ouverture doit être celui de clôture, à l'identique.

        C'est la seule vérification qui compte vraiment : un à-nouveau qui
        « a l'air juste » mais diffère sur un compte suffit à fausser tout
        l'exercice suivant.
        """
        self._cloturer()
        soldes = {
            compte: solde
            for compte, solde in self.env["account.move.line"]._read_group(
                domain=[
                    ("company_id", "=", self.societe.id),
                    ("parent_state", "=", "posted"),
                    ("date", "<=", date(2026, 12, 31)),
                    ("account_id.internal_group", "in",
                     ["asset", "liability", "equity"]),
                ],
                groupby=["account_id"], aggregates=["balance:sum"])
            if solde
        }
        ouverture = self.moteur.ecriture_a_nouveaux(
            self.societe, date(2026, 1, 1), date(2026, 12, 31),
            self.journal, date(2027, 1, 1))
        repris = {ligne.account_id: ligne.balance for ligne in ouverture.line_ids}

        self.assertEqual(
            set(soldes), set(repris),
            "Les comptes repris doivent être exactement ceux qui ont un solde")
        for compte, solde in soldes.items():
            self.assertAlmostEqual(
                repris[compte], solde, places=2,
                msg="Le compte %s est repris pour %.2f au lieu de %.2f"
                    % (compte.code, repris[compte], solde))

    def test_les_a_nouveaux_portent_la_date_du_nouvel_exercice(self):
        self._cloturer()
        ouverture = self.moteur.ecriture_a_nouveaux(
            self.societe, date(2026, 1, 1), date(2026, 12, 31),
            self.journal, date(2027, 1, 1))
        self.assertEqual(ouverture.date, date(2027, 1, 1))

    # ------------------------------------------------------------------
    # Garde-fous
    # ------------------------------------------------------------------

    def test_on_ne_cloture_pas_deux_fois(self):
        """Une double clôture divise le résultat par deux, sans rien signaler.

        C'est l'erreur la plus coûteuse du lot, parce qu'elle ne produit ni
        déséquilibre ni message : le bilan reste équilibré, simplement faux.
        """
        self._cloturer()
        releves = [n for n, _d in self.moteur.controler(
            self.societe, date(2026, 1, 1), date(2026, 12, 31))]
        self.assertIn("deja_cloture", releves)

        assistant = self.env["expodo.year.closing"].create({
            "company_id": self.societe.id,
            "date_from": date(2026, 1, 1), "date_to": date(2026, 12, 31),
            "journal_id": self.journal.id,
        })
        with self.assertRaises(UserError):
            assistant.action_close()

    def test_les_brouillons_sont_signales(self):
        self.env["account.move"].create({
            "journal_id": self.journal.id,
            "date": date(2026, 8, 1),
            "line_ids": [
                Command.create({
                    "name": "Non validé", "account_id": self.compte_client.id,
                    "debit": 100.0, "credit": 0.0}),
                Command.create({
                    "name": "Non validé", "account_id": self.compte_vente.id,
                    "debit": 0.0, "credit": 100.0}),
            ],
        })
        releves = [n for n, _d in self.moteur.controler(
            self.societe, date(2026, 1, 1), date(2026, 12, 31))]
        self.assertIn("brouillons", releves)

    def test_une_periode_sans_ecriture_de_gestion_est_refusee(self):
        assistant = self.env["expodo.year.closing"].create({
            "company_id": self.societe.id,
            "date_from": date(2019, 1, 1), "date_to": date(2019, 12, 31),
            "journal_id": self.journal.id,
        })
        with self.assertRaises(UserError):
            assistant.action_close()

    # ------------------------------------------------------------------
    # Assistant
    # ------------------------------------------------------------------

    def test_l_assistant_produit_les_deux_ecritures(self):
        assistant = self.env["expodo.year.closing"].create({
            "company_id": self.societe.id,
            "date_from": date(2026, 1, 1), "date_to": date(2026, 12, 31),
            "journal_id": self.journal.id, "create_opening": True,
        })
        assistant.action_close()
        self.assertTrue(assistant.closing_move_id)
        self.assertTrue(assistant.opening_move_id)
        self.assertEqual(assistant.opening_move_id.date, date(2027, 1, 1))

    def test_l_ecriture_d_ouverture_peut_etre_declaree_sur_la_societe(self):
        """C'est ce rattachement que lit l'export FEC.

        Sans lui, l'écriture existe mais rien ne la distingue d'une opération
        diverse, et les à-nouveaux ne se placent pas en tête du fichier.
        """
        assistant = self.env["expodo.year.closing"].create({
            "company_id": self.societe.id,
            "date_from": date(2026, 1, 1), "date_to": date(2026, 12, 31),
            "journal_id": self.journal.id, "create_opening": True,
        })
        assistant.action_close()
        assistant.action_register_opening()
        self.assertEqual(
            self.societe.account_opening_move_id, assistant.opening_move_id)

    def test_les_bornes_par_defaut_suivent_l_exercice_de_la_societe(self):
        self.societe.write({"fiscalyear_last_month": "6", "fiscalyear_last_day": 30})
        assistant = self.env["expodo.year.closing"].create({})
        self.assertEqual(
            (assistant.date_from.month, assistant.date_from.day), (7, 1))
        self.assertEqual(
            (assistant.date_to.month, assistant.date_to.day), (6, 30))
