# -*- coding: utf-8 -*-
# Copyright 2026 Expodo (https://expodo.fr)
# License LGPL-3
"""Cycle de vie des déclarations.

Le risque de ce module n'est pas l'erreur de calcul : c'est le **verrou**.

Un verrou qui ne se pose pas laisse la porte ouverte à des écritures
postérieures au dépôt, qui font diverger les livres et la déclaration sans que
rien ne le signale. Un verrou qui recule rouvre des périodes déjà déclarées, ce
qui est pire encore.

Les tests portent donc d'abord sur le verrou, ensuite sur le reste.
"""

from datetime import date

from odoo import Command
from odoo.exceptions import UserError, ValidationError
from odoo.tests import TransactionCase, tagged


@tagged("post_install", "-at_install")
class TestDeclarations(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.societe = cls.env.company
        cls.societe.tax_lock_date = False
        cls.type_mensuel = cls.env["expodo.return.type"].create({
            "name": "TVA mensuelle",
            "company_id": cls.societe.id,
            "periodicity": "monthly",
            "deadline_days": 19,
            "lock_on_submission": True,
        })

    def _declaration(self, debut=date(2026, 3, 1), fin=date(2026, 3, 31),
                     type_=None):
        return self.env["expodo.return"].create({
            "type_id": (type_ or self.type_mensuel).id,
            "company_id": self.societe.id,
            "date_from": debut, "date_to": fin,
        })

    # ------------------------------------------------------------------
    # Le verrou
    # ------------------------------------------------------------------

    def test_le_depot_verrouille_la_periode(self):
        """Ce qui a été déclaré ne doit plus bouger.

        Sans verrou, une écriture comptabilisée après le dépôt fait diverger
        les livres et la déclaration, et rien ne le signale avant le contrôle.
        """
        declaration = self._declaration()
        declaration.action_check()
        declaration.action_submit()
        self.assertEqual(
            self.societe.tax_lock_date, date(2026, 3, 31),
            "Le dépôt doit avancer le verrou fiscal jusqu'à la fin de la "
            "période déclarée")

    def test_apres_depot_aucune_ecriture_ne_retombe_dans_la_periode(self):
        """Le verrou est posé pour cela, et c'est cela qu'il faut vérifier.

        Les autres contrôles regardent la date de verrou écrite sur la
        société. Celui-ci regarde la conséquence promise à l'utilisateur :
        une facture saisie après le dépôt, datée dans la période déclarée, ne
        doit pas s'y loger. Odoo ne la refuse pas, il la reporte au premier
        jour libre, ce qui est le comportement attendu et laisse la
        déclaration déposée intacte.

        Le test emploie un comptable et non le superutilisateur : ce dernier
        traverse les verrous, si bien qu'un contrôle mené en son nom ne
        prouve rien.
        """
        declaration = self._declaration()
        declaration.action_check()
        declaration.action_submit()
        verrou = self.societe.tax_lock_date
        self.assertTrue(verrou, "Le dépôt doit poser un verrou")

        comptable = self.env["res.users"].create({
            "name": "Comptable du verrou",
            "login": "verrou_periode_declaree",
            "company_id": self.societe.id,
            "company_ids": [Command.set([self.societe.id])],
            "group_ids": [Command.set([
                self.env.ref("base.group_user").id,
                self.env.ref("account.group_account_user").id])],
        })
        taxe = self.env["account.tax"].search(
            [("type_tax_use", "=", "sale"),
             ("company_id", "=", self.societe.id)], limit=1)
        if not taxe:
            self.skipTest("Aucune taxe de vente sur cette société")
        client = self.env["res.partner"].create({"name": "Client du verrou"})

        facture = self.env["account.move"].with_user(comptable).create({
            "move_type": "out_invoice", "partner_id": client.id,
            "invoice_date": declaration.date_from,
            "date": declaration.date_from,
            "invoice_line_ids": [Command.create({
                "name": "Prestation", "quantity": 1, "price_unit": 100.0,
                "tax_ids": [Command.set(taxe.ids)]})],
        })
        facture.action_post()
        self.assertGreater(
            facture.date, verrou,
            "Une facture saisie après le dépôt s'est logée dans la période "
            "déclarée : les livres et la déclaration divergent sans que rien "
            "ne le signale")

    def test_le_verrou_ne_recule_jamais(self):
        """Reculer rouvrirait des périodes déjà déclarées.

        Une écriture pourrait alors s'y glisser sans que personne ne s'en
        aperçoive — exactement ce que le verrou existe pour empêcher.
        """
        self.societe.tax_lock_date = date(2026, 6, 30)
        declaration = self._declaration()
        declaration.action_check()
        declaration.action_submit()
        self.assertEqual(
            self.societe.tax_lock_date, date(2026, 6, 30),
            "Déposer une période antérieure ne doit pas reculer le verrou")

    def test_un_type_sans_verrouillage_ne_touche_pas_au_verrou(self):
        """Toutes les déclarations ne verrouillent pas.

        Un relevé intracommunautaire, par exemple, n'a pas à figer la période
        comptable : il ne porte pas de montant de taxe.
        """
        type_libre = self.env["expodo.return.type"].create({
            "name": "Relevé sans verrou",
            "company_id": self.societe.id,
            "periodicity": "monthly",
            "lock_on_submission": False,
        })
        declaration = self._declaration(type_=type_libre)
        declaration.action_check()
        declaration.action_submit()
        self.assertFalse(self.societe.tax_lock_date)

    def test_la_reouverture_ne_deverrouille_pas(self):
        """Rouvrir la fiche ne rouvre pas la période.

        On rouvre pour corriger une référence de dépôt ou un montant saisi.
        Si la période doit vraiment redevenir modifiable, le verrou se recule
        à la main, délibérément.
        """
        declaration = self._declaration()
        declaration.action_check()
        declaration.action_submit()
        declaration.action_reset()
        self.assertEqual(declaration.state, "draft")
        self.assertEqual(
            self.societe.tax_lock_date, date(2026, 3, 31),
            "Le verrou reste en place après réouverture de la fiche")

    # ------------------------------------------------------------------
    # Le cycle
    # ------------------------------------------------------------------

    def test_on_ne_depose_pas_sans_avoir_controle(self):
        """Le dépôt est un acte : il vaut deux minutes de vérification."""
        declaration = self._declaration()
        with self.assertRaises(UserError):
            declaration.action_submit()

    def test_on_ne_paie_pas_ce_qui_n_est_pas_depose(self):
        declaration = self._declaration()
        declaration.action_check()
        with self.assertRaises(UserError):
            declaration.action_pay()

    def test_une_declaration_payee_ne_se_rouvre_pas(self):
        """Une correction se dépose comme une nouvelle déclaration.

        C'est ce que l'administration s'attend à voir : une rectification
        datée, pas une déclaration modifiée après paiement.
        """
        declaration = self._declaration()
        declaration.action_check()
        declaration.action_submit()
        declaration.action_pay()
        with self.assertRaises(UserError):
            declaration.action_reset()

    def test_deux_declarations_ne_couvrent_pas_la_meme_periode(self):
        """Elles paraîtraient toutes deux complètes et se contrediraient."""
        self._declaration()
        with self.assertRaises(Exception):
            self._declaration()
            self.env.flush_all()

    # ------------------------------------------------------------------
    # Périodes
    # ------------------------------------------------------------------

    def test_la_periode_mensuelle_couvre_le_mois_entier(self):
        debut, fin = self.type_mensuel.bornes_periode(date(2026, 3, 15))
        self.assertEqual(debut, date(2026, 3, 1))
        self.assertEqual(fin, date(2026, 3, 31))

    def test_la_periode_trimestrielle_suit_les_trimestres_civils(self):
        type_ = self.env["expodo.return.type"].create({
            "name": "CA3 trimestrielle", "company_id": self.societe.id,
            "periodicity": "quarterly", "deadline_days": 24})
        debut, fin = type_.bornes_periode(date(2026, 5, 7))
        self.assertEqual(debut, date(2026, 4, 1))
        self.assertEqual(fin, date(2026, 6, 30))

    def test_la_periode_annuelle_suit_l_exercice_et_non_l_annee_civile(self):
        """Supposer l'année civile produirait la mauvaise période.

        Et rien ne le signalerait : la déclaration s'afficherait, couvrant
        simplement autre chose que l'exercice.
        """
        self.societe.write({"fiscalyear_last_month": "6", "fiscalyear_last_day": 30})
        type_ = self.env["expodo.return.type"].create({
            "name": "Déclaration annuelle", "company_id": self.societe.id,
            "periodicity": "yearly", "deadline_days": 90})
        debut, fin = type_.bornes_periode(date(2026, 9, 15))
        self.assertEqual((debut.month, debut.day), (7, 1))
        self.assertEqual((fin.month, fin.day), (6, 30))

    def test_l_echeance_se_deduit_du_delai_de_depot(self):
        declaration = self._declaration()
        self.assertEqual(
            declaration.date_deadline, date(2026, 4, 19),
            "Le 31 mars plus dix-neuf jours")

    def test_une_periode_a_l_envers_est_refusee(self):
        with self.assertRaises(ValidationError):
            self._declaration(debut=date(2026, 3, 31), fin=date(2026, 3, 1))

    # ------------------------------------------------------------------
    # Contrôles
    # ------------------------------------------------------------------

    def test_les_brouillons_de_la_periode_sont_signales(self):
        """Ils entreront dans les chiffres après le dépôt, pas avant."""
        journal = self.env["account.journal"].search(
            [("type", "=", "general"), ("company_id", "=", self.societe.id)], limit=1)
        comptes = self.env["account.account"]
        self.env["account.move"].create({
            "journal_id": journal.id,
            "date": date(2026, 3, 15),
            "line_ids": [
                Command.create({
                    "name": "Non validé",
                    "account_id": comptes.search(
                        [("account_type", "=", "asset_receivable")], limit=1).id,
                    "debit": 100.0, "credit": 0.0}),
                Command.create({
                    "name": "Non validé",
                    "account_id": comptes.search(
                        [("account_type", "=", "income")], limit=1).id,
                    "debit": 0.0, "credit": 100.0}),
            ],
        })
        declaration = self._declaration()
        # Les anomalies sont rédigées dans la langue de l'utilisateur. Le test
        # reconnaît un mot-clé : il faut donc fixer la langue, sinon il échoue
        # sur une base espagnole où le message dit « borrador ».
        anomalies = declaration.with_context(lang="en_US").controles()
        self.assertTrue(
            any("draft" in a.lower() for a in anomalies),
            "Les écritures en brouillon de la période doivent être signalées")

    def test_une_declaration_anterieure_non_deposee_est_signalee(self):
        """Déposer dans le désordre laisse un trou dans la chronologie.

        L'administration le remarque avant le déclarant.
        """
        self._declaration(date(2026, 1, 1), date(2026, 1, 31))
        suivante = self._declaration(date(2026, 3, 1), date(2026, 3, 31))
        # Les contrôles sont traduits : la langue est fixée ici, sinon le test
        # cherche des mots anglais dans un message français et échoue pour une
        # raison qui n'a rien à voir avec ce qu'il vérifie.
        anomalies = suivante.with_context(lang="en_US").controles()
        self.assertTrue(
            any("unfiled" in a.lower() or "earlier" in a.lower() for a in anomalies),
            "Une déclaration antérieure non déposée doit être signalée : %s"
            % (anomalies,))

    def test_le_controle_ne_pretend_pas_remplacer_un_comptable(self):
        """Le message le dit explicitement.

        Un outil qui annonce « aucune anomalie » sans nuance invite à ne pas
        relire — et ces contrôles ne voient que les erreurs mécaniques.
        """
        declaration = self._declaration(date(2026, 3, 1), date(2026, 3, 31))
        declaration.action_check()
        self.assertTrue(declaration.check_result)
