# -*- coding: utf-8 -*-
# Copyright 2026 Expodo (https://expodo.fr)
# License LGPL-3
"""Écarts de conversion.

Le sens de l'écart est tout l'enjeu. Une perte latente portée au compte des
gains produit une écriture équilibrée, un bilan équilibré, et un résultat faux
du double du montant. Rien ne le signale.

Les tests portent donc d'abord sur le sens, ensuite sur le montant.
"""

from datetime import date

from odoo import Command
from odoo.exceptions import UserError
from odoo.tests import TransactionCase, tagged


@tagged("post_install", "-at_install")
class TestEcartsDeConversion(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.societe = cls.env.company
        cls.journal = cls.env["account.journal"].search(
            [("type", "=", "general"), ("company_id", "=", cls.societe.id)], limit=1)
        comptes = cls.env["account.account"]
        cls.compte_client = comptes.search([
            ("account_type", "=", "asset_receivable"), ("reconcile", "=", True)], limit=1)
        cls.compte_fournisseur = comptes.search([
            ("account_type", "=", "liability_payable"), ("reconcile", "=", True)], limit=1)
        cls.compte_vente = comptes.search([("account_type", "=", "income")], limit=1)
        cls.compte_charge = comptes.search([("account_type", "=", "expense")], limit=1)
        cls.partenaire = cls.env["res.partner"].create({"name": "Partenaire en devise"})
        cls.moteur = cls.env["expodo.revaluation.engine"]

        cls.devise = cls.env["res.currency"].search([("name", "=", "USD")], limit=1)
        cls.devise.active = True
        # Un taux peut déjà exister à cette date : `res.currency.rate` porte
        # une contrainte d'unicité par jour et par société. On écrit plutôt
        # que de créer, sans quoi la préparation du test échoue selon l'état
        # de la base — c'est-à-dire de façon reproductible en développement et
        # pas en intégration continue.
        # Cours de clôture daté du 30/12 : en v20, la conversion au 31/12
        # retient le dernier cours daté strictement avant ce jour (voir
        # TestCoursDuJourDeCloture).
        for jour, taux in ((date(2026, 10, 1), 0.92), (date(2026, 12, 30), 0.95)):
            existant = cls.env["res.currency.rate"].search([
                ("currency_id", "=", cls.devise.id),
                ("name", "=", jour),
                ("company_id", "=", cls.societe.id),
            ], limit=1)
            if existant:
                existant.inverse_company_rate = taux
            else:
                cls.env["res.currency.rate"].create({
                    "currency_id": cls.devise.id, "name": jour,
                    "company_id": cls.societe.id, "inverse_company_rate": taux})

    def _creance(self, montant_devise=10000.0, valeur=9200.0):
        ecriture = self.env["account.move"].create({
            "journal_id": self.journal.id,
            "date": date(2026, 10, 1),
            "ref": "FACTURE DEVISE",
            "line_ids": [
                Command.create({
                    "name": "Vente", "account_id": self.compte_client.id,
                    "partner_id": self.partenaire.id,
                    "currency_id": self.devise.id,
                    "amount_currency": montant_devise,
                    "debit": valeur, "credit": 0.0}),
                Command.create({
                    "name": "Vente", "account_id": self.compte_vente.id,
                    "currency_id": self.devise.id,
                    "amount_currency": -montant_devise,
                    "debit": 0.0, "credit": valeur}),
            ],
        })
        ecriture.action_post()
        return ecriture

    def _dette(self, montant_devise=10000.0, valeur=9200.0):
        ecriture = self.env["account.move"].create({
            "journal_id": self.journal.id,
            "date": date(2026, 10, 1),
            "ref": "ACHAT DEVISE",
            "line_ids": [
                Command.create({
                    "name": "Achat", "account_id": self.compte_charge.id,
                    "currency_id": self.devise.id,
                    "amount_currency": montant_devise,
                    "debit": valeur, "credit": 0.0}),
                Command.create({
                    "name": "Achat", "account_id": self.compte_fournisseur.id,
                    "partner_id": self.partenaire.id,
                    "currency_id": self.devise.id,
                    "amount_currency": -montant_devise,
                    "debit": 0.0, "credit": valeur}),
            ],
        })
        ecriture.action_post()
        return ecriture

    # ------------------------------------------------------------------
    # Montant
    # ------------------------------------------------------------------

    def test_l_ecart_vaut_la_difference_entre_les_deux_cours(self):
        """Dix mille dollars inscrits à 9 200 valent 9 500 au cours de clôture."""
        self._creance()
        details = self.moteur.calculer(self.societe, date(2026, 12, 31))
        concerne = [d for d in details if d["account"] == self.compte_client]
        self.assertTrue(concerne, "La créance en devise doit être retenue")
        self.assertAlmostEqual(concerne[0]["gap"], 300.0, delta=1.0)

    def test_une_creance_dans_la_devise_de_la_societe_est_ignoree(self):
        """Il n'y a rien à convertir : le cours vaut un."""
        ecriture = self.env["account.move"].create({
            "journal_id": self.journal.id,
            "date": date(2026, 10, 1),
            "line_ids": [
                Command.create({
                    "name": "Vente locale", "account_id": self.compte_client.id,
                    "partner_id": self.partenaire.id,
                    "debit": 5000.0, "credit": 0.0}),
                Command.create({
                    "name": "Vente locale", "account_id": self.compte_vente.id,
                    "debit": 0.0, "credit": 5000.0}),
            ],
        })
        ecriture.action_post()
        for detail in self.moteur.calculer(self.societe, date(2026, 12, 31)):
            self.assertNotEqual(
                detail["currency"], self.societe.currency_id,
                "Un solde déjà dans la devise de la société n'a pas à être "
                "réévalué")

    # ------------------------------------------------------------------
    # Sens de l'écart
    # ------------------------------------------------------------------

    def test_une_creance_qui_s_apprecie_produit_un_gain(self):
        self._creance()
        details = [d for d in self.moteur.calculer(self.societe, date(2026, 12, 31))
                   if d["account"] == self.compte_client]
        self.assertGreater(
            details[0]["gap"], 0.0,
            "Une créance dont la devise monte est un gain latent")

    def test_une_dette_qui_s_alourdit_produit_une_perte(self):
        """Le cas où le signe se retourne, et où l'erreur est invisible.

        La dette a un solde créditeur. Si la devise monte, la dette coûte plus
        cher : c'est une perte. Une implémentation qui traiterait le sens sans
        y penser porterait ce montant en gain — écriture équilibrée, bilan
        équilibré, résultat faux du double.
        """
        self._dette()
        details = [d for d in self.moteur.calculer(self.societe, date(2026, 12, 31))
                   if d["account"] == self.compte_fournisseur]
        self.assertTrue(details, "La dette en devise doit être retenue")
        self.assertLess(
            details[0]["gap"], 0.0,
            "Une dette dont la devise monte est une perte latente")

    def test_un_gain_latent_va_au_compte_des_gains(self):
        """En plan français, 477 — et surtout pas 476."""
        self._creance()
        ecriture = self.moteur.ecriture_de_conversion(
            self.societe, date(2026, 12, 31), self.journal)
        codes = ecriture.line_ids.mapped("account_id.code")
        if self.societe.account_fiscal_country_id.code == "FR":
            self.assertTrue(
                any(c.startswith("477") for c in codes),
                "Un gain latent doit rejoindre le 477, obtenu : %s" % codes)
            self.assertFalse(
                any(c.startswith("476") for c in codes),
                "Le 476 est réservé aux pertes latentes")

    # ------------------------------------------------------------------
    # Écriture
    # ------------------------------------------------------------------

    def test_l_ecriture_est_equilibree(self):
        self._creance()
        self._dette()
        ecriture = self.moteur.ecriture_de_conversion(
            self.societe, date(2026, 12, 31), self.journal)
        self.assertAlmostEqual(
            sum(ecriture.line_ids.mapped("debit")),
            sum(ecriture.line_ids.mapped("credit")), places=2)

    def test_l_ecriture_reste_en_brouillon(self):
        self._creance()
        ecriture = self.moteur.ecriture_de_conversion(
            self.societe, date(2026, 12, 31), self.journal)
        self.assertEqual(ecriture.state, "draft")

    def test_la_contre_passation_est_creee_en_meme_temps(self):
        """Elle n'est pas remise à plus tard.

        Un écart de conversion laissé sans contrepartie fait double emploi
        avec l'écart réalisé au règlement. Et personne ne se souvient, en
        mars, qu'il fallait contre-passer en janvier.
        """
        self._creance()
        assistant = self.env["expodo.revaluation.wizard"].create({
            "company_id": self.societe.id,
            "date_to": date(2026, 12, 31),
            "journal_id": self.journal.id,
            "reverse_next_day": True,
        })
        assistant.action_revalue()
        self.assertTrue(assistant.reversal_move_id)
        self.assertEqual(
            assistant.reversal_move_id.date, date(2027, 1, 1),
            "La contre-passation porte le premier jour de l'exercice suivant")

    # ------------------------------------------------------------------
    # Garde-fous
    # ------------------------------------------------------------------

    def test_on_ne_reevalue_pas_deux_fois(self):
        self._creance()
        premier = self.env["expodo.revaluation.wizard"].create({
            "company_id": self.societe.id,
            "date_to": date(2026, 12, 31),
            "journal_id": self.journal.id,
        })
        premier.action_revalue()

        second = self.env["expodo.revaluation.wizard"].create({
            "company_id": self.societe.id,
            "date_to": date(2026, 12, 31),
            "journal_id": self.journal.id,
        })
        with self.assertRaises(UserError):
            second.action_revalue()

    def test_la_provision_sur_perte_latente_est_signalee(self):
        """Elle n'est pas créée : son montant relève d'une appréciation.

        Couverture de change, position globale, compensation entre devises :
        rien de cela n'est mécanisable. Créer d'office une provision au
        montant brut serait faux dans la plupart des cas.
        """
        self._dette()
        releves = [n for n, _d in self.moteur.controler(
            self.societe, date(2026, 12, 31))]
        self.assertIn(
            "provision", releves,
            "Une perte latente doit appeler l'attention sur la provision")

    def test_un_taux_perime_est_signale(self):
        """Une conversion sur un cours périmé paraît juste et ne l'est pas."""
        self._creance()
        releves = dict(
            (n, d) for n, d in self.moteur.controler(
                self.societe, date(2027, 6, 30)))
        self.assertIn(
            "taux_perime", releves,
            "Six mois après le dernier cours connu, la conversion doit être "
            "signalée comme reposant sur un taux périmé")


@tagged("post_install", "-at_install")
class TestCoursDuJourDeCloture(TransactionCase):
    """Hypothèse du module sur la conversion d'Odoo, fixée par un test.

    Jusqu'en v19, `res.currency._convert` retenait le dernier cours daté
    **au plus tard** du jour demandé. En v20 il retient le dernier cours daté
    **strictement avant** : un cours saisi au 31/12 ne s'applique qu'à partir
    du 01/01. L'édition Enterprise v20 fait de même (rapport de réévaluation
    observé sur l'instance témoin : au 31/12, il affiche le cours précédent).

    Le module suit le cœur. Si Odoo revient à l'ancienne règle, ce test
    tombe et signale que le contrôle de fraîcheur du cours est à revoir.
    """

    def test_un_cours_date_du_jour_ne_s_applique_qu_au_lendemain(self):
        societe = self.env.company
        devise = self.env["res.currency"].with_context(
            active_test=False).search([("name", "=", "USD")], limit=1)
        devise.active = True
        for jour, taux in ((date(2026, 10, 1), 0.92), (date(2026, 12, 31), 0.95)):
            existant = self.env["res.currency.rate"].search([
                ("currency_id", "=", devise.id), ("name", "=", jour),
                ("company_id", "=", societe.id)], limit=1)
            if existant:
                existant.inverse_company_rate = taux
            else:
                self.env["res.currency.rate"].create({
                    "currency_id": devise.id, "name": jour,
                    "company_id": societe.id, "inverse_company_rate": taux})
        convertir = lambda jour: devise._convert(  # noqa: E731
            10000.0, societe.currency_id, societe, jour)
        self.assertAlmostEqual(convertir(date(2026, 12, 31)), 9200.0, places=2)
        self.assertAlmostEqual(convertir(date(2027, 1, 1)), 9500.0, places=2)
