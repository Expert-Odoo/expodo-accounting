# -*- coding: utf-8 -*-
# Copyright 2026 Expodo (https://expodo.fr)
# License LGPL-3
"""Charges et produits constatés d'avance.

Un report mal calculé fausse deux exercices à la fois, dans des sens opposés.
L'erreur est particulièrement difficile à retrouver : chaque exercice paraît
plausible pris isolément, et c'est leur comparaison qui ne tient pas.

Les tests portent donc d'abord sur l'arithmétique du prorata, ensuite sur les
cas limites — et ce sont les cas limites qui produisent les erreurs, pas le cas
courant.
"""

from datetime import date

from odoo import Command
from odoo.exceptions import UserError, ValidationError
from odoo.tests import TransactionCase, tagged


@tagged("post_install", "-at_install")
class TestReportsDAvance(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.societe = cls.env.company
        cls.journal = cls.env["account.journal"].search(
            [("type", "=", "general"), ("company_id", "=", cls.societe.id)], limit=1)
        comptes = cls.env["account.account"]
        cls.compte_charge = comptes.search([("account_type", "=", "expense")], limit=1)
        cls.compte_produit = comptes.search([("account_type", "=", "income")], limit=1)
        cls.compte_banque = comptes.search([("account_type", "=", "asset_cash")], limit=1)
        cls.moteur = cls.env["expodo.deferral.engine"]

    # ------------------------------------------------------------------
    # Arithmétique du prorata
    # ------------------------------------------------------------------

    def test_le_prorata_se_calcule_au_jour_exact(self):
        """Une prime annuelle souscrite au 1er octobre.

        Du 1er octobre 2026 au 30 septembre 2027 : 365 jours. Après le 31
        décembre il en reste 273. La part reportée vaut donc 12 000 × 273/365.
        """
        part = self.moteur.part_reportee(
            12000.0, date(2026, 10, 1), date(2027, 9, 30), date(2026, 12, 31))
        self.assertAlmostEqual(part, 12000.0 * 273 / 365, places=2)

    def test_les_deux_bornes_sont_incluses(self):
        """Janvier couvre trente et un jours, pas trente.

        L'écart paraît négligeable sur un mois. Sur un contrat pluriannuel
        découpé en douze reports, il finit par se voir — et personne ne sait
        d'où il vient.
        """
        part = self.moteur.part_reportee(
            310.0, date(2026, 1, 1), date(2026, 1, 31), date(2026, 1, 15))
        # Seize jours restants sur trente et un.
        self.assertAlmostEqual(part, 310.0 * 16 / 31, places=2)

    def test_une_periode_deja_echue_ne_reporte_rien(self):
        part = self.moteur.part_reportee(
            1000.0, date(2026, 1, 1), date(2026, 6, 30), date(2026, 12, 31))
        self.assertAlmostEqual(part, 0.0, places=2)

    def test_une_periode_entierement_future_se_reporte_en_totalite(self):
        """Une charge payée d'avance pour l'an prochain n'appartient pas du
        tout à l'exercice en cours."""
        part = self.moteur.part_reportee(
            1000.0, date(2027, 1, 1), date(2027, 12, 31), date(2026, 12, 31))
        self.assertAlmostEqual(part, 1000.0, places=2)

    def test_un_debut_en_cours_de_mois_n_est_pas_arrondi(self):
        """Le cas qui condamne le prorata mensuel.

        Un contrat démarrant le 17 octobre serait reporté comme s'il démarrait
        le 1er : seize jours d'écart, que personne ne saurait expliquer un an
        plus tard.
        """
        part = self.moteur.part_reportee(
            1200.0, date(2026, 10, 17), date(2027, 10, 16), date(2026, 12, 31))
        jours_totaux = (date(2027, 10, 16) - date(2026, 10, 17)).days + 1
        jours_futurs = (date(2027, 10, 16) - date(2026, 12, 31)).days
        self.assertAlmostEqual(
            part, 1200.0 * jours_futurs / jours_totaux, places=2)
        self.assertNotAlmostEqual(
            part, 1200.0 * 9 / 12, places=2,
            msg="Un prorata mensuel donnerait neuf douzièmes : c'est "
                "précisément ce qu'il faut éviter")

    def test_l_absence_de_dates_ne_reporte_rien(self):
        self.assertAlmostEqual(
            self.moteur.part_reportee(1000.0, None, None, date(2026, 12, 31)),
            0.0, places=2)

    # ------------------------------------------------------------------
    # Écritures produites
    # ------------------------------------------------------------------

    def _charge_datee(self, montant=12000.0):
        ecriture = self.env["account.move"].create({
            "journal_id": self.journal.id,
            "date": date(2026, 10, 1),
            "ref": "PRIME",
            "line_ids": [
                Command.create({
                    "name": "Prime", "account_id": self.compte_charge.id,
                    "debit": montant, "credit": 0.0,
                    "deferred_start_date": date(2026, 10, 1),
                    "deferred_end_date": date(2027, 9, 30)}),
                Command.create({
                    "name": "Prime", "account_id": self.compte_banque.id,
                    "debit": 0.0, "credit": montant}),
            ],
        })
        ecriture.action_post()
        return ecriture

    def _produit_date(self, montant=6000.0):
        ecriture = self.env["account.move"].create({
            "journal_id": self.journal.id,
            "date": date(2026, 11, 1),
            "ref": "ABONNEMENT",
            "line_ids": [
                Command.create({
                    "name": "Abonnement", "account_id": self.compte_banque.id,
                    "debit": montant, "credit": 0.0}),
                Command.create({
                    "name": "Abonnement", "account_id": self.compte_produit.id,
                    "debit": 0.0, "credit": montant,
                    "deferred_start_date": date(2026, 11, 1),
                    "deferred_end_date": date(2027, 4, 30)}),
            ],
        })
        ecriture.action_post()
        return ecriture

    def test_l_ecriture_de_report_est_equilibree(self):
        self._charge_datee()
        self._produit_date()
        ecriture = self.moteur.ecriture_de_report(
            self.societe, date(2026, 12, 31), self.journal)
        self.assertAlmostEqual(
            sum(ecriture.line_ids.mapped("debit")),
            sum(ecriture.line_ids.mapped("credit")), places=2)

    def test_une_charge_quitte_le_compte_de_gestion_pour_le_bilan(self):
        self._charge_datee()
        ecriture = self.moteur.ecriture_de_report(
            self.societe, date(2026, 12, 31), self.journal)
        groupes = ecriture.line_ids.mapped("account_id.internal_group")
        self.assertIn(
            "expense", groupes, "Le compte de charge doit être mouvementé")
        self.assertIn(
            "asset", groupes,
            "La part reportée doit rejoindre un compte de bilan")

    def test_un_produit_va_au_passif_et_non_a_l_actif(self):
        """Le sens compte : un produit constaté d'avance est une dette.

        L'entreprise a encaissé une somme au titre d'un service qu'elle n'a
        pas encore rendu. La porter à l'actif inverserait la lecture du bilan.
        """
        self._produit_date()
        ecriture = self.moteur.ecriture_de_report(
            self.societe, date(2026, 12, 31), self.journal)
        bilan = ecriture.line_ids.filtered(
            lambda l: l.account_id.internal_group in ("asset", "liability"))
        self.assertTrue(bilan)
        self.assertEqual(
            set(bilan.mapped("account_id.internal_group")), {"liability"},
            "Un produit constaté d'avance est une dette, pas une créance")

    def test_l_ecriture_reste_en_brouillon(self):
        """Un report touche deux exercices : il se relit avant de se poser."""
        self._charge_datee()
        ecriture = self.moteur.ecriture_de_report(
            self.societe, date(2026, 12, 31), self.journal)
        self.assertEqual(ecriture.state, "draft")

    # ------------------------------------------------------------------
    # Garde-fous
    # ------------------------------------------------------------------

    def test_une_periode_a_l_envers_est_refusee(self):
        """Elle produirait un report négatif parfaitement plausible.

        Le montant resterait dans les ordres de grandeur attendus et
        l'écriture s'équilibrerait : rien ne signalerait l'erreur avant la
        revue des comptes.
        """
        with self.assertRaises(ValidationError):
            self.env["account.move"].create({
                "journal_id": self.journal.id,
                "date": date(2026, 10, 1),
                "line_ids": [
                    Command.create({
                        "name": "À l'envers", "account_id": self.compte_charge.id,
                        "debit": 1000.0, "credit": 0.0,
                        "deferred_start_date": date(2027, 9, 30),
                        "deferred_end_date": date(2026, 10, 1)}),
                    Command.create({
                        "name": "À l'envers", "account_id": self.compte_banque.id,
                        "debit": 0.0, "credit": 1000.0}),
                ],
            })

    def test_on_ne_reporte_pas_deux_fois_a_la_meme_date(self):
        """Un second report doublerait la part sortie du résultat."""
        self._charge_datee()
        assistant = self.env["expodo.deferral.wizard"].create({
            "company_id": self.societe.id,
            "date_to": date(2026, 12, 31),
            "journal_id": self.journal.id,
        })
        assistant.action_post_deferral()

        second = self.env["expodo.deferral.wizard"].create({
            "company_id": self.societe.id,
            "date_to": date(2026, 12, 31),
            "journal_id": self.journal.id,
        })
        with self.assertRaises(UserError):
            second.action_post_deferral()

    def test_une_date_de_fin_manquante_est_signalee(self):
        """Sans elle, la ligne reste entièrement dans l'exercice, en silence."""
        ecriture = self.env["account.move"].create({
            "journal_id": self.journal.id,
            "date": date(2026, 10, 1),
            "line_ids": [
                Command.create({
                    "name": "Sans fin", "account_id": self.compte_charge.id,
                    "debit": 1000.0, "credit": 0.0,
                    "deferred_start_date": date(2026, 10, 1)}),
                Command.create({
                    "name": "Sans fin", "account_id": self.compte_banque.id,
                    "debit": 0.0, "credit": 1000.0}),
            ],
        })
        ecriture.action_post()
        releves = [n for n, _d in self.moteur.controler(
            self.societe, date(2026, 12, 31))]
        self.assertIn("dates_incompletes", releves)

    def test_l_apercu_n_ecrit_rien(self):
        """Voir avant d'écrire : c'est toute la raison d'être du bouton."""
        self._charge_datee()
        avant = self.env["account.move"].search_count([
            ("company_id", "=", self.societe.id)])
        assistant = self.env["expodo.deferral.wizard"].create({
            "company_id": self.societe.id,
            "date_to": date(2026, 12, 31),
            "journal_id": self.journal.id,
        })
        assistant.action_preview()
        self.assertTrue(assistant.preview)
        self.assertEqual(
            self.env["account.move"].search_count([
                ("company_id", "=", self.societe.id)]),
            avant, "L'aperçu ne doit créer aucune écriture")
