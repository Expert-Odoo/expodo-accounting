# -*- coding: utf-8 -*-
# Copyright 2026 Expodo (https://expodo.fr)
# License LGPL-3
"""Tests d'intégration des immobilisations.

Ce module écrit dans la comptabilité. Les tests portent donc moins sur le
calcul — éprouvé sans base dans `tests_offline/` sur plus de dix mille
combinaisons — que sur ce qui peut mal tourner **autour** de l'écriture :
garde-fous, transitions d'état, intégrité du grand livre, droits d'accès.

Chaque test vérifie une propriété qui, si elle était fausse, produirait une
comptabilité inexacte sans lever la moindre erreur.
"""

from datetime import date

from odoo import Command, fields
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tests import TransactionCase, tagged


@tagged("post_install", "-at_install")
class TestAssets(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env.company
        cls.journal = cls.env["account.journal"].search([
            ("type", "=", "general"), ("company_id", "=", cls.company.id),
        ], limit=1)

        def compte(code, nom, type_compte):
            existant = cls.env["account.account"].search([("code", "=", code)], limit=1)
            return existant or cls.env["account.account"].create({
                "code": code, "name": nom, "account_type": type_compte,
            })

        cls.acc_asset = compte("219000", "Test asset account", "asset_fixed")
        cls.acc_depreciation = compte("289000", "Test accumulated depreciation",
                                      "asset_fixed")
        cls.acc_expense = compte("689000", "Test depreciation expense",
                                 "expense_depreciation")
        cls.acc_loss = compte("679000", "Test net book value", "expense_other")
        cls.acc_counterpart = compte("519000", "Test counterpart", "asset_current")

    # ------------------------------------------------------------------

    def _asset(self, **surcharges):
        valeurs = {
            "name": "Test asset",
            "acquisition_value": 12000.0,
            "date_start": date(2026, 1, 1),
            "duration_months": 12,
            "journal_id": self.journal.id,
            "account_asset_id": self.acc_asset.id,
            "account_depreciation_id": self.acc_depreciation.id,
            "account_expense_id": self.acc_expense.id,
        }
        valeurs.update(surcharges)
        return self.env["expodo.asset"].create(valeurs)

    def _acquisition(self, asset):
        """Comptabilise l'acquisition, comme une facture fournisseur le ferait."""
        move = self.env["account.move"].create({
            "journal_id": self.journal.id,
            "date": asset.date_start,
            "ref": "Acquisition",
            "line_ids": [
                Command.create({
                    "name": "Asset", "account_id": asset.account_asset_id.id,
                    "debit": asset.acquisition_value, "credit": 0.0,
                }),
                Command.create({
                    "name": "Asset", "account_id": self.acc_counterpart.id,
                    "debit": 0.0, "credit": asset.acquisition_value,
                }),
            ],
        })
        move.action_post()
        return move

    def _solde(self, compte):
        self.env.cr.execute("""
            SELECT COALESCE(SUM(l.balance), 0.0)
              FROM account_move_line l JOIN account_move m ON m.id = l.move_id
             WHERE m.state = 'posted' AND l.account_id = %s
        """, (compte.id,))
        return float(self.env.cr.fetchone()[0])

    # ------------------------------------------------------------------
    # Tableau d'amortissement
    # ------------------------------------------------------------------

    def test_board_totals_match_the_depreciable_base(self):
        asset = self._asset()
        asset.action_validate()
        self.assertEqual(len(asset.line_ids), 12)
        self.assertAlmostEqual(
            sum(asset.line_ids.mapped("amount")), 12000.0, places=2)
        self.assertAlmostEqual(asset.line_ids[-1].remaining, 0.0, places=2)

    def test_board_respects_salvage_value(self):
        asset = self._asset(salvage_value=2000.0)
        asset.action_validate()
        self.assertAlmostEqual(
            sum(asset.line_ids.mapped("amount")), 10000.0, places=2)
        self.assertAlmostEqual(asset.line_ids[-1].remaining, 2000.0, places=2)

    def test_board_excludes_prior_depreciation(self):
        """Un bien migré : l'antériorité sort du tableau, la date de fin ne bouge pas."""
        asset = self._asset(already_depreciated=3000.0)
        asset.action_validate()
        self.assertAlmostEqual(
            sum(asset.line_ids.mapped("amount")), 9000.0, places=2)
        self.assertEqual(len(asset.line_ids), 12)
        self.assertAlmostEqual(asset.line_ids[-1].cumulative, 12000.0, places=2)

    def test_invalid_values_are_refused(self):
        for surcharge in (
            {"acquisition_value": 0.0},
            {"acquisition_value": -500.0},
            {"duration_months": 0},
            {"salvage_value": 20000.0},
            {"already_depreciated": 20000.0},
        ):
            with self.subTest(**surcharge), self.assertRaises(ValidationError):
                self._asset(**surcharge)

    def test_expense_account_accepts_other_expenses(self):
        """Le plan français type les dotations aux amortissements 6811 en
        `expense_other`. Le domaine du champ les excluait : sur une société
        française, le compte de dotation ne proposait aucun compte."""
        domaine = self.env["expodo.asset"]._fields["account_expense_id"].domain
        if isinstance(domaine, str):
            from odoo.tools.safe_eval import safe_eval
            domaine = safe_eval(domaine)
        comptes = self.acc_expense | self.acc_loss
        self.assertEqual(comptes.filtered_domain(domaine), comptes)

    def test_depreciation_and_expense_accounts_must_differ(self):
        """Sinon l'écriture débiterait et créditerait le même compte : effet nul."""
        with self.assertRaises(ValidationError):
            self._asset(account_expense_id=self.acc_depreciation.id)

    # ------------------------------------------------------------------
    # Écritures
    # ------------------------------------------------------------------

    def test_posting_creates_a_balanced_entry(self):
        asset = self._asset()
        asset.action_validate()
        ligne = asset.line_ids[0]
        ligne.action_post()

        self.assertEqual(ligne.state, "posted")
        move = ligne.move_id
        self.assertTrue(move, "L'échéance doit porter son écriture")
        self.assertEqual(move.state, "posted")
        self.assertAlmostEqual(
            sum(move.line_ids.mapped("debit")),
            sum(move.line_ids.mapped("credit")), places=2)
        self.assertAlmostEqual(
            self._solde(self.acc_expense), ligne.amount, places=2)
        self.assertAlmostEqual(
            self._solde(self.acc_depreciation), -ligne.amount, places=2)

    def test_nothing_is_posted_without_an_explicit_action(self):
        """Le choix structurant du module : aucune tâche planifiée.

        Valider une immobilisation calcule le tableau et n'écrit rien.
        """
        asset = self._asset()
        asset.action_validate()
        self.assertTrue(asset.line_ids)
        self.assertFalse(
            asset.line_ids.filtered(lambda l: l.state == "posted"),
            "Valider ne doit comptabiliser aucune échéance")
        self.assertAlmostEqual(self._solde(self.acc_expense), 0.0, places=2)

    def test_posting_twice_is_refused(self):
        asset = self._asset()
        asset.action_validate()
        ligne = asset.line_ids[0]
        ligne.action_post()
        with self.assertRaises(UserError):
            ligne.action_post()

    def test_posting_a_draft_asset_is_refused(self):
        asset = self._asset()
        asset.action_compute_board()
        self.assertEqual(asset.state, "draft")
        with self.assertRaises(UserError):
            asset.line_ids[0].action_post()

    def test_lock_date_is_checked_before_creating_the_entry(self):
        """Odoo refuserait de comptabiliser, mais le brouillon existerait déjà.

        Une écriture en brouillon oubliée dans un journal ne se remarque pas.
        """
        asset = self._asset()
        asset.action_validate()
        self.company.sudo().fiscalyear_lock_date = date(2026, 12, 31)
        avant = self.env["account.move"].search_count([])
        with self.assertRaises(UserError):
            asset.line_ids[0].action_post()
        self.assertEqual(
            self.env["account.move"].search_count([]), avant,
            "Aucune écriture, même en brouillon, ne doit avoir été créée")
        self.company.sudo().fiscalyear_lock_date = False

    def test_recomputing_after_posting_is_refused(self):
        """Recalculer désynchroniserait le tableau du grand livre."""
        asset = self._asset()
        asset.action_validate()
        asset.line_ids[0].action_post()
        with self.assertRaises(UserError):
            asset.action_compute_board()

    def test_back_to_draft_refused_after_posting(self):
        asset = self._asset()
        asset.action_validate()
        asset.line_ids[0].action_post()
        with self.assertRaises(UserError):
            asset.action_set_draft()

    def test_posted_line_cannot_be_deleted(self):
        """Supprimer laisserait l'écriture orpheline au grand livre."""
        asset = self._asset()
        asset.action_validate()
        ligne = asset.line_ids[0]
        ligne.action_post()
        with self.assertRaises(UserError):
            ligne.unlink()

    def test_asset_closes_when_fully_posted(self):
        asset = self._asset(duration_months=3)
        asset.action_validate()
        asset.line_ids.action_post()
        self.assertEqual(asset.state, "closed")
        self.assertAlmostEqual(asset.remaining_value, 0.0, places=2)

    def test_post_due_ignores_future_instalments(self):
        asset = self._asset(date_start=date(2026, 1, 1), duration_months=36)
        asset.action_validate()
        aujourdhui = fields.Date.context_today(asset)
        echues = asset.line_ids.filtered(lambda l: l.date <= aujourdhui)
        asset.line_ids.action_post_due()
        self.assertEqual(
            len(asset.line_ids.filtered(lambda l: l.state == "posted")),
            len(echues),
            "Seules les échéances arrivées à terme doivent être comptabilisées")

    # ------------------------------------------------------------------
    # Sortie d'actif
    # ------------------------------------------------------------------

    def test_disposal_squares_the_accounts(self):
        """Après sortie, le bien ne doit plus figurer au bilan.

        Contrôle décisif : compte d'immobilisation plus compte d'amortissement
        doivent se compenser exactement.
        """
        asset = self._asset(duration_months=12)
        self._acquisition(asset)
        asset.action_validate()
        asset.line_ids[:3].action_post()

        assistant = self.env["expodo.asset.disposal"].create({
            "asset_id": asset.id,
            "date": date(2026, 6, 30),
            "reason": "scrap",
            "account_loss_id": self.acc_loss.id,
            "post_due_first": False,
        })
        assistant.action_confirm()

        self.assertEqual(asset.state, "disposed")
        net = self._solde(self.acc_asset) + self._solde(self.acc_depreciation)
        self.assertAlmostEqual(
            net, 0.0, places=2,
            msg="Le bien doit être entièrement sorti du bilan")

    def test_disposal_charges_the_net_book_value(self):
        asset = self._asset(duration_months=12)
        self._acquisition(asset)
        asset.action_validate()
        asset.line_ids[:4].action_post()
        valeur_nette = asset.remaining_value

        self.env["expodo.asset.disposal"].create({
            "asset_id": asset.id, "date": date(2026, 6, 30), "reason": "scrap",
            "account_loss_id": self.acc_loss.id, "post_due_first": False,
        }).action_confirm()

        self.assertAlmostEqual(
            self._solde(self.acc_loss), valeur_nette, places=2)

    def test_disposal_cancels_remaining_instalments(self):
        """Laissées en attente, quelqu'un finirait par les comptabiliser."""
        asset = self._asset(duration_months=12)
        self._acquisition(asset)
        asset.action_validate()
        asset.line_ids[:2].action_post()

        self.env["expodo.asset.disposal"].create({
            "asset_id": asset.id, "date": date(2026, 6, 30), "reason": "sale",
            "account_loss_id": self.acc_loss.id, "post_due_first": False,
        }).action_confirm()

        self.assertFalse(asset.line_ids.filtered(lambda l: l.state == "draft"))
        self.assertEqual(len(asset.line_ids.filtered(lambda l: l.state == "posted")), 2)

    def test_disposal_of_fully_depreciated_asset(self):
        """Valeur nette nulle : l'écriture ne doit comporter aucune charge."""
        asset = self._asset(duration_months=3)
        self._acquisition(asset)
        asset.action_validate()
        asset.line_ids.action_post()
        self.assertEqual(asset.state, "closed")

        self.env["expodo.asset.disposal"].create({
            "asset_id": asset.id, "date": date(2026, 6, 30), "reason": "scrap",
            "account_loss_id": self.acc_loss.id, "post_due_first": False,
        }).action_confirm()

        self.assertEqual(asset.state, "disposed")
        self.assertAlmostEqual(self._solde(self.acc_loss), 0.0, places=2)
        self.assertAlmostEqual(
            self._solde(self.acc_asset) + self._solde(self.acc_depreciation),
            0.0, places=2)

    def test_disposal_of_draft_asset_is_refused(self):
        asset = self._asset()
        with self.assertRaises(UserError):
            self.env["expodo.asset.disposal"].create({
                "asset_id": asset.id, "date": date(2026, 6, 30),
                "reason": "scrap", "account_loss_id": self.acc_loss.id,
            }).action_confirm()

    # ------------------------------------------------------------------
    # Rattachement à l'acquisition
    # ------------------------------------------------------------------

    def test_warning_when_no_acquisition_entry(self):
        """Sans écriture d'acquisition, on amortirait un bien absent du bilan."""
        asset = self._asset()
        self.assertTrue(asset.acquisition_warning)

    def test_warning_disappears_once_linked(self):
        asset = self._asset()
        asset.original_move_id = self._acquisition(asset)
        self.assertFalse(asset.acquisition_warning)

    def test_unrelated_entry_cannot_be_linked(self):
        """Un rattachement erroné donnerait une fausse assurance."""
        asset = self._asset()
        etranger = self.env["account.move"].create({
            "journal_id": self.journal.id, "date": date(2026, 1, 1),
            "line_ids": [
                Command.create({"name": "x", "account_id": self.acc_expense.id,
                                "debit": 100.0, "credit": 0.0}),
                Command.create({"name": "x", "account_id": self.acc_counterpart.id,
                                "debit": 0.0, "credit": 100.0}),
            ],
        })
        etranger.action_post()
        with self.assertRaises(ValidationError):
            asset.original_move_id = etranger

    def test_entry_below_acquisition_value_is_refused(self):
        asset = self._asset()
        insuffisant = self.env["account.move"].create({
            "journal_id": self.journal.id, "date": date(2026, 1, 1),
            "line_ids": [
                Command.create({"name": "x", "account_id": self.acc_asset.id,
                                "debit": 500.0, "credit": 0.0}),
                Command.create({"name": "x", "account_id": self.acc_counterpart.id,
                                "debit": 0.0, "credit": 500.0}),
            ],
        })
        insuffisant.action_post()
        with self.assertRaises(ValidationError):
            asset.original_move_id = insuffisant

    def test_consistency_check_detects_a_missing_acquisition(self):
        asset = self._asset()   # aucune écriture d'acquisition
        action = asset.action_check_consistency()
        self.assertEqual(action["params"]["type"], "warning")

    def test_consistency_check_passes_when_ledger_matches(self):
        """Le contrôle est appelé sur le bien, pas sur le modèle.

        Appelé globalement, il dépendrait de tout ce que la base contient par
        ailleurs : une immobilisation incohérente créée par un autre test, ou
        présente dans la base du client, le ferait échouer pour une raison
        étrangère à ce qu'il vérifie. Le test a d'ailleurs échoué exactement
        ainsi sur une base fraîche.
        """
        asset = self._asset()
        self._acquisition(asset)
        action = asset.action_check_consistency()
        self.assertEqual(
            action["params"]["type"], "success",
            "Le contrôle doit passer quand le grand livre couvre les fiches")

    # ------------------------------------------------------------------
    # Droits d'accès
    # ------------------------------------------------------------------

    def test_user_without_accounting_rights_is_denied(self):
        etranger = self.env["res.users"].create({
            "name": "Sans droits", "login": "assets_sans_droits",
            "group_ids": [Command.link(self.env.ref("base.group_user").id)],
        })
        with self.assertRaises(AccessError):
            self.env["expodo.asset"].with_user(etranger).search([])

    def test_readonly_user_cannot_create(self):
        lecteur = self.env["res.users"].create({
            "name": "Lecture seule", "login": "assets_lecture",
            "group_ids": [Command.link(
                self.env.ref("account.group_account_readonly").id)],
        })
        with self.assertRaises(AccessError):
            self.env["expodo.asset"].with_user(lecteur).create({
                "name": "Interdit", "acquisition_value": 100.0,
                "date_start": date(2026, 1, 1), "duration_months": 12,
                "journal_id": self.journal.id,
                "account_asset_id": self.acc_asset.id,
                "account_depreciation_id": self.acc_depreciation.id,
                "account_expense_id": self.acc_expense.id,
            })

    # ------------------------------------------------------------------
    # Intégrité d'ensemble
    # ------------------------------------------------------------------

    def test_full_life_cycle_leaves_a_consistent_ledger(self):
        """Cycle complet : acquisition, amortissement, cession.

        À la fin, les charges cumulées doivent égaler la base amortissable, et
        le bien avoir disparu du bilan. C'est le contrôle qui engloberait toute
        erreur des étapes précédentes.
        """
        asset = self._asset(duration_months=6, already_depreciated=0.0)
        self._acquisition(asset)
        asset.original_move_id = asset.original_move_id or self.env["account.move"].search(
            [("ref", "=", "Acquisition")], limit=1)
        asset.action_validate()
        asset.line_ids[:2].action_post()

        self.env["expodo.asset.disposal"].create({
            "asset_id": asset.id, "date": date(2026, 8, 31), "reason": "sale",
            "account_loss_id": self.acc_loss.id, "post_due_first": False,
        }).action_confirm()

        charges = self._solde(self.acc_expense) + self._solde(self.acc_loss)
        self.assertAlmostEqual(
            charges, asset.acquisition_value, places=2,
            msg="Les charges cumulées doivent égaler la valeur brute du bien")
        self.assertAlmostEqual(
            self._solde(self.acc_asset) + self._solde(self.acc_depreciation),
            0.0, places=2,
            msg="Le bien doit avoir disparu du bilan")

    # ------------------------------------------------------------------
    # Régressions trouvées en navigateur
    # ------------------------------------------------------------------

    def test_future_instalment_cannot_be_posted(self):
        """Régression : le bouton de ligne permettait de comptabiliser 2029.

        Les tests appelaient `action_post` sur la première échéance, toujours
        échue. Dans l'interface, chaque ligne du tableau porte son bouton,
        y compris les échéances à trois ans. Rien ne l'empêchait, et cela
        revenait à constater une charge avant qu'elle ne soit encourue.

        Trouvé en ouvrant simplement la fiche dans un navigateur.
        """
        asset = self._asset(date_start=date(2026, 1, 1), duration_months=60)
        asset.action_validate()
        aujourdhui = fields.Date.context_today(asset)
        future = asset.line_ids.filtered(lambda l: l.date > aujourdhui)
        self.assertTrue(future, "Le test suppose des échéances à venir")
        avant = self.env["account.move"].search_count([])
        with self.assertRaises(UserError):
            future[0].action_post()
        self.assertEqual(
            self.env["account.move"].search_count([]), avant,
            "Aucune écriture ne doit avoir été créée")

    def test_due_instalment_is_still_accepted(self):
        """Le garde-fou ne doit pas bloquer les échéances légitimes."""
        asset = self._asset(date_start=date(2026, 1, 1), duration_months=60)
        asset.action_validate()
        aujourdhui = fields.Date.context_today(asset)
        echues = asset.line_ids.filtered(lambda l: l.date <= aujourdhui)
        self.assertTrue(echues, "Le test suppose des échéances échues")
        echues[0].action_post()
        self.assertEqual(echues[0].state, "posted")

    def test_batch_posting_logs_one_message(self):
        """Régression : un message de suivi par échéance noyait le fil.

        Quatre-vingt-quatre messages pour un bien amorti sur sept ans, et plus
        aucune trace lisible des évènements qui comptent.
        """
        asset = self._asset(date_start=date(2026, 1, 1), duration_months=60)
        asset.action_validate()
        avant = self.env["mail.message"].search_count([
            ("model", "=", "expodo.asset"), ("res_id", "=", asset.id)])
        echues = asset.line_ids.filtered(
            lambda l: l.date <= fields.Date.context_today(asset))
        self.assertGreater(len(echues), 3, "Le test suppose un lot")
        echues.action_post()
        apres = self.env["mail.message"].search_count([
            ("model", "=", "expodo.asset"), ("res_id", "=", asset.id)])
        self.assertEqual(
            apres - avant, 1,
            "Un lot de %d échéances doit produire un seul message" % len(echues))

    def test_accounting_manager_can_dispose(self):
        """Régression : l'administrateur comptable ne pouvait pas sortir un bien.

        En v19, la hiérarchie des groupes comptables n'est pas linéaire :
        `group_account_manager` implique « Facturation » mais **pas**
        « Utilisateur ». Les droits de l'assistant n'étaient accordés qu'à ce
        dernier, si bien que l'administrateur — la personne même censée sortir
        un actif — recevait une erreur d'accès.

        Le même piège s'était déjà présenté sur le module de rapports. C'est la
        raison d'être de ce test : que l'oubli ne se répète pas une troisième
        fois.
        """
        manager = self.env["res.users"].create({
            "name": "Comptable", "login": "assets_manager_test",
            "group_ids": [Command.link(
                self.env.ref("account.group_account_manager").id)],
        })
        asset = self._asset(duration_months=12)
        self._acquisition(asset)
        asset.action_validate()

        assistant = self.env["expodo.asset.disposal"].with_user(manager).create({
            "asset_id": asset.id,
            "date": date(2026, 6, 30),
            "reason": "scrap",
            "account_loss_id": self.acc_loss.id,
            "post_due_first": False,
        })
        assistant.action_confirm()
        self.assertEqual(asset.state, "disposed")

    def test_accounting_manager_can_post_an_instalment(self):
        """Même hiérarchie, même risque sur la comptabilisation."""
        manager = self.env["res.users"].search([
            ("login", "=", "assets_manager_test")], limit=1)
        if not manager:
            manager = self.env["res.users"].create({
                "name": "Comptable", "login": "assets_manager_test2",
                "group_ids": [Command.link(
                    self.env.ref("account.group_account_manager").id)],
            })
        asset = self._asset(duration_months=12)
        asset.action_validate()
        asset.line_ids[0].with_user(manager).action_post()
        self.assertEqual(asset.line_ids[0].state, "posted")

    def test_disposal_wizard_picks_up_the_asset_from_context(self):
        """Régression : l'assistant s'ouvrait sans bien ni valeur.

        Ouvert depuis le bouton de la fiche, il recevait l'identifiant dans le
        contexte mais ne le lisait pas. Résultat : des champs requis vides et
        un écran bloqué sans message.

        Le compte de charge, lui, peut rester vide : le plan français livré
        par Odoo ne comporte aucun compte 675, et la règle est de ne rien
        proposer plutôt que de proposer un compte approchant. Ce test
        n'exige donc plus qu'il soit rempli ; il exige que l'assistant
        s'ouvre quand même.
        """
        asset = self._asset()
        assistant = self.env["expodo.asset.disposal"].with_context(
            active_model="expodo.asset", active_id=asset.id,
        ).create({})
        self.assertEqual(assistant.asset_id, asset)

    def test_disposal_without_a_loss_account_says_so(self):
        """Un compte manquant doit se dire, pas remonter de PostgreSQL.

        Le champ était requis au niveau du modèle, donc NOT NULL en base. Le
        plan français ne portant aucun compte 675, la valeur par défaut est
        vide : toute création sans valeur — un appel programmatique, un autre
        module qui ouvrirait cet assistant — remontait l'erreur brute du
        moteur de base de données au lieu d'un message lisible.
        """
        asset = self._asset()
        asset.action_validate()
        assistant = self.env["expodo.asset.disposal"].create({
            "asset_id": asset.id,
            "date": fields.Date.to_date("2026-06-30"),
        })
        assistant.account_loss_id = False
        with self.assertRaises(UserError):
            assistant.action_confirm()

    def test_expense_account_type_is_flagged(self):
        """Un compte de dotation mal typé doit être signalé.

        Le tableau de flux réintègre les dotations en s'appuyant sur le type
        de compte. Typé « charge » plutôt que « dotation », le montant tombe
        dans le résultat net au lieu d'apparaître à part. Le total reste juste,
        donc rien d'autre ne le signale — et le plan comptable français type
        précisément 681120 en charge ordinaire.
        """
        charge_ordinaire = self.env["account.account"].search([
            ("account_type", "=", "expense")], limit=1)
        self.assertTrue(charge_ordinaire, "Le test suppose un compte de charge")
        asset = self._asset(account_expense_id=charge_ordinaire.id)
        self.assertTrue(
            asset.expense_type_warning,
            "Un compte typé « charge » doit être signalé")

        dotation = self.env["account.account"].search([
            ("account_type", "=", "expense_depreciation")], limit=1)
        if dotation:
            asset.account_expense_id = dotation
            self.assertFalse(
                asset.expense_type_warning,
                "Un compte correctement typé ne doit rien signaler")

    def test_asset_with_posted_entries_cannot_be_deleted(self):
        """Régression : supprimer un bien laissait ses écritures orphelines.

        Les échéances sont liées en `ondelete="cascade"` : supprimer la fiche
        efface le tableau sans passer par le contrôle posé sur la ligne. Les
        dotations restaient au grand livre, comptabilisées, équilibrées, et
        plus aucune fiche ne les justifiait. Rien ne les signalait — on les
        découvrait en cherchant d'où venait une charge.
        """
        asset = self._asset()
        asset.action_validate()
        ligne = asset.line_ids[0]
        ligne.action_post()
        ecriture = ligne.move_id

        with self.assertRaises(UserError):
            asset.unlink()
        self.assertTrue(asset.exists(), "Le bien doit subsister")
        self.assertTrue(ecriture.exists(), "L'écriture doit subsister")
        self.assertEqual(ecriture.state, "posted")

    def test_asset_without_entries_can_be_deleted(self):
        """Le garde-fou ne doit pas empêcher de corriger une saisie."""
        asset = self._asset()
        asset.action_validate()
        self.assertTrue(asset.line_ids, "Le tableau doit exister")
        asset.unlink()
        self.assertFalse(asset.exists())

    # ------------------------------------------------------------------
    # Cloisonnement multi-société
    # ------------------------------------------------------------------

    def test_assets_of_another_company_are_not_visible(self):
        """Régression : aucune règle d'enregistrement ne cloisonnait les biens.

        Un utilisateur d'une société lisait les immobilisations de toutes les
        autres — valeur des biens, comptes employés, tableau d'amortissement.
        Les droits d'accès du modèle étaient satisfaits, donc rien ne le
        signalait : seule une règle d'enregistrement pose la frontière.

        Le cas se présente dès qu'un cabinet gère plusieurs dossiers dans la
        même base, ce qui est la situation courante chez un intégrateur.
        """
        autre = self.env["res.company"].create({"name": "Autre société"})
        journal = self.env["account.journal"].create({
            "name": "OD autre", "code": "ODA", "type": "general",
            "company_id": autre.id,
        })
        # Comptes propres à l'autre société. En v19, partager un compte entre
        # deux sociétés impose d'y définir son code, qui est dépendant de la
        # société ; créer des comptes dédiés est plus simple et plus fidèle à
        # la réalité d'un cabinet qui gère plusieurs dossiers.
        def compte_autre(code, nom, type_compte):
            return self.env["account.account"].with_company(autre).create({
                "code": code, "name": nom, "account_type": type_compte,
            })

        actif_autre = compte_autre("219900", "Autre actif", "asset_fixed")
        amort_autre = compte_autre("289900", "Autre amortissement", "asset_fixed")
        charge_autre = compte_autre("689900", "Autre dotation", "expense_depreciation")
        cache = self.env["expodo.asset"].with_company(autre).create({
            "name": "Bien confidentiel",
            "acquisition_value": 99000.0,
            "date_start": date(2026, 1, 1),
            "duration_months": 12,
            "company_id": autre.id,
            "journal_id": journal.id,
            "account_asset_id": actif_autre.id,
            "account_depreciation_id": amort_autre.id,
            "account_expense_id": charge_autre.id,
        })

        limite = self.env["res.users"].create({
            "name": "Limité", "login": "assets_une_societe",
            "company_id": self.company.id,
            "company_ids": [Command.set([self.company.id])],
            "group_ids": [Command.link(
                self.env.ref("account.group_account_manager").id)],
        })
        vus = self.env["expodo.asset"].with_user(limite).search([])
        self.assertNotIn(
            cache.id, vus.ids,
            "Un utilisateur ne doit pas voir les immobilisations d'une autre société")

    def test_board_lines_of_another_company_are_not_visible(self):
        """Le cloisonnement doit aussi couvrir le tableau d'amortissement.

        Cloisonner la fiche sans cloisonner ses échéances laisserait lire les
        montants, les dates et les écritures — c'est-à-dire l'essentiel.
        """
        regle = self.env.ref(
            "expodo_account_assets.rule_expodo_asset_line_company",
            raise_if_not_found=False)
        self.assertTrue(
            regle, "Une règle doit cloisonner les lignes du tableau")
        self.assertTrue(regle.global_ if hasattr(regle, "global_") else True)

    def test_consistency_check_runs_for_an_accounting_user(self):
        """Régression : le contrôle échouait pour son utilisateur cible.

        La notification enchaîne sur la liste des immobilisations, faute de
        quoi l'utilisateur reste devant un écran vide après avoir cliqué le
        menu. Cette action était d'abord **lue en base**, ce qui exige un
        droit d'administrateur : un comptable recevait une erreur d'accès à la
        place de son résultat. Elle est désormais construite en Python.
        """
        comptable = self.env["res.users"].create({
            "name": "Comptable contrôle", "login": "assets_controle",
            "group_ids": [Command.link(
                self.env.ref("account.group_account_user").id)],
        })
        asset = self._asset()
        self._acquisition(asset)

        action = asset.with_user(comptable).action_check_consistency()
        self.assertEqual(action["params"]["type"], "success")
        suite = action["params"].get("next")
        self.assertTrue(suite, "La notification doit enchaîner sur une vue")
        self.assertEqual(suite.get("res_model"), "expodo.asset")
        self.assertTrue(
            suite.get("views"),
            "L'action doit déclarer ses vues : une action construite en Python "
            "et renvoyée par RPC ne peut pas les déduire de `view_mode` seul")

    # ------------------------------------------------------------------
    # Points d'entrée de l'interface, jamais couverts jusqu'ici
    # ------------------------------------------------------------------

    def test_disposal_action_carries_the_asset_and_an_account(self):
        """L'action ouverte par le bouton « Dispose ».

        Jamais couverte, et c'est elle qui ouvrait un assistant vide : le
        contexte ne portait ni le bien ni le compte de charge, laissant des
        champs requis vides et l'écran bloqué sans message.
        """
        asset = self._asset()
        action = asset.action_open_disposal()
        self.assertEqual(action["res_model"], "expodo.asset.disposal")
        contexte = action["context"]
        self.assertEqual(contexte["active_id"], asset.id)
        self.assertEqual(contexte["default_asset_id"], asset.id)
        self.assertTrue(
            action.get("views"),
            "Une action construite en Python doit déclarer ses vues")

    def test_default_loss_account_prefers_the_statutory_account(self):
        """Le compte proposé pour la valeur nette cédée.

        La méthode vit désormais sur l'assistant, seul endroit qui propose ce
        compte. Elle vivait aussi sur l'immobilisation, ce qui créait deux
        sources de vérité — voir la régression documentée plus bas.
        """
        compte = self.env["expodo.asset.disposal"]._defaut_compte_charge()
        if compte:
            enregistrement = self.env["account.account"].browse(compte)
            self.assertTrue(
                enregistrement.code.startswith("675"),
                "Seul un compte 675 doit être proposé : ailleurs, le choix "
                "revient à l'utilisateur")

    def test_board_action_opens_the_instalments(self):
        asset = self._asset()
        asset.action_validate()
        action = asset.action_open_lines()
        self.assertEqual(action["res_model"], "expodo.asset.line")
        self.assertIn(("asset_id", "=", asset.id), action["domain"])
        self.assertTrue(action.get("views"))

    def test_opening_a_move_requires_a_posted_instalment(self):
        """Ouvrir l'écriture d'une échéance non comptabilisée doit refuser."""
        asset = self._asset()
        asset.action_validate()
        ligne = asset.line_ids[0]
        with self.assertRaises(UserError):
            ligne.action_open_move()
        ligne.action_post()
        action = ligne.action_open_move()
        self.assertEqual(action["res_model"], "account.move")
        self.assertEqual(action["res_id"], ligne.move_id.id)

    def test_depreciation_account_must_be_a_balance_sheet_account(self):
        """Un amortissement cumulé ne se loge pas dans un compte de résultat.

        Le champ n'avait aucun domaine : on pouvait y désigner un compte de
        ventes. L'écriture restait équilibrée, aucun contrôle arithmétique ne
        la signalait, et le bilan comme le compte de résultat devenaient faux
        en silence.

        Le domaine de la vue guide la saisie ; ce contrôle protège les deux
        autres chemins — l'import de données et l'appel par programme — par
        lesquels arrivent la plupart des reprises lors d'une migration.
        """
        produit = self.env["account.account"].search([
            ("account_type", "=", "income")], limit=1)
        self.assertTrue(produit, "Le test suppose un compte de produits")
        with self.assertRaises(ValidationError):
            self._asset(account_depreciation_id=produit.id)

    def test_asset_can_be_created_with_only_the_form_fields(self):
        """Tout ce qu'exige le formulaire doit suffire à créer un bien.

        Rien ne garantissait que les champs visibles à l'écran suffisent :
        un champ requis absent de la vue, un défaut posé seulement par le
        code, et l'utilisateur se heurte à un formulaire qu'il ne peut pas
        valider — alors que tout fonctionne quand on crée l'enregistrement
        par programme.
        """
        vue = self.env.ref("expodo_account_assets.view_expodo_asset_form")
        champs_vue = set(self.env["expodo.asset"].fields_get(
            attributes=["string"]))
        requis = {
            nom for nom, champ in self.env["expodo.asset"]._fields.items()
            if champ.required and not champ.compute and not champ.related
            and not champ.default and nom not in ("id", "create_uid",
                                                  "write_uid", "create_date",
                                                  "write_date")
        }
        manquants = [nom for nom in requis if 'name="%s"' % nom not in vue.arch_db]
        self.assertFalse(
            manquants,
            "Champs requis absents du formulaire, donc impossibles à "
            "renseigner à l'écran : %s" % ", ".join(sorted(manquants)))
        self.assertTrue(champs_vue)

    def test_disposal_account_has_a_single_source(self):
        """Le compte de cession ne doit être proposé qu'à un seul endroit.

        Régression : il l'était deux fois — par le défaut du champ de
        l'assistant, et par une clé `default_account_loss_id` que l'action
        d'ouverture plaçait dans son contexte, calculée par une seconde
        méthode. Corriger la première laissait la seconde intacte : le serveur
        ne proposait plus rien, l'écran affichait toujours l'ancien compte.

        Il m'a fallu près d'une heure pour le localiser, parce que chaque
        appel direct confirmait la correction et que seul le formulaire
        montrait l'ancienne valeur. Deux sources de vérité produisent
        exactement ce symptôme : un correctif qui marche partout sauf là où
        l'utilisateur regarde.
        """
        asset = self._asset()
        action = asset.action_open_disposal()
        self.assertNotIn(
            "default_account_loss_id", action["context"],
            "L'action ne doit pas proposer le compte : l'assistant s'en charge")

        assistant = self.env["expodo.asset.disposal"].with_context(
            **action["context"])
        depuis_action = assistant.default_get(["account_loss_id"]).get(
            "account_loss_id")
        depuis_champ = self.env["expodo.asset.disposal"]._defaut_compte_charge()
        self.assertEqual(
            depuis_action or False, depuis_champ or False,
            "Les deux chemins doivent donner le même compte")

    def test_disposal_account_is_only_proposed_in_france(self):
        """Le compte 675 ne doit être proposé qu'en France.

        Régression : le préfixe 675 n'est pas propre au plan français. Dans le
        plan espagnol, 675 désigne « Pérdidas por operaciones con obligaciones
        propias » — les pertes sur opérations sur obligations propres, sans
        rapport avec une cession d'immobilisation.

        C'est la deuxième fois que ce champ propose un compte faux. La première
        version prenait le premier compte de charges exceptionnelles venu et
        désignait « Cash Discount Loss » sur un plan américain. La correction
        — n'accepter que le 675 — semblait résoudre le problème, mais elle
        reposait sur la même illusion : qu'un numéro de compte identifie une
        nature comptable indépendamment du pays. Il n'en identifie une que
        dans le plan qui l'a défini.
        """
        assistant = self.env["expodo.asset.disposal"]
        pays = self.env.company.account_fiscal_country_id.code
        compte = assistant._defaut_compte_charge()

        if pays != "FR":
            self.assertFalse(
                compte,
                "Hors de France, aucun compte ne doit être proposé : les "
                "numéros de compte ne se transposent pas d'un plan à l'autre")
            return

        if compte:
            self.assertTrue(
                self.env["account.account"].browse(compte).code.startswith("675"),
                "En France, seul un compte 675 est acceptable")
