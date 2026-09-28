# -*- coding: utf-8 -*-
# Copyright 2026 Expodo (https://expodo.fr)
# License LGPL-3
"""Immobilisations et tableau d'amortissement.

Principe de sûreté
==================

Ce module est le premier du lot à **écrire dans la comptabilité**. Il suit
donc trois règles que les rapports n'avaient pas à respecter.

**Aucune écriture n'est jamais posée automatiquement.** Le tableau
d'amortissement est calculé et affiché, mais chaque dotation exige une action
humaine. Les modules concurrents postent par tâche planifiée, et leurs avis
utilisateurs font état d'écritures déséquilibrées découvertes des mois plus
tard. Une dotation manquée se rattrape ; une écriture fausse dans un exercice
clôturé, non.

**Le tableau est contrôlé avant chaque écriture.** La somme doit égaler la
base amortissable, aucune dotation ne peut être négative, les échéances
doivent être croissantes. Un tableau incohérent n'atteint jamais le grand
livre.

**Les dates de verrouillage sont respectées.** Odoo les vérifie déjà au
moment du `action_post`, mais l'erreur arriverait alors après la création de
l'écriture. On vérifie avant, pour refuser proprement.
"""

from odoo import api, fields, models
from odoo.exceptions import UserError, ValidationError

from ..engine.depreciation import (
    AmortissementError,
    calculer_tableau,
    controler_tableau,
)


class ExpodoAsset(models.Model):
    _name = "expodo.asset"
    _description = "Fixed Asset"
    _inherit = ["mail.thread", "mail.activity.mixin"]
    _order = "date_start desc, id desc"
    _check_company_auto = True

    name = fields.Char(string="Name", required=True, tracking=True)
    company_id = fields.Many2one(
        "res.company", string="Company", required=True,
        default=lambda self: self.env.company,
    )
    currency_id = fields.Many2one(related="company_id.currency_id")
    partner_id = fields.Many2one("res.partner", string="Vendor", tracking=True)

    state = fields.Selection(
        [("draft", "Draft"), ("running", "Running"),
         ("closed", "Fully depreciated"), ("disposed", "Disposed")],
        string="Status", default="draft", required=True, tracking=True, copy=False,
    )

    # --- Valeurs ---
    acquisition_value = fields.Monetary(
        string="Acquisition value", required=True, tracking=True,
    )
    salvage_value = fields.Monetary(
        string="Salvage value", default=0.0, tracking=True,
        help="Portion of the value that is never depreciated.",
    )
    already_depreciated = fields.Monetary(
        string="Already depreciated", default=0.0, tracking=True,
        help="Depreciation already recognised before this asset was recorded "
             "here. Used when migrating an existing asset: the amount is "
             "excluded from the board without changing the end date.",
    )
    depreciated_value = fields.Monetary(
        string="Depreciated", compute="_compute_totals", store=True,
    )
    remaining_value = fields.Monetary(
        string="Remaining", compute="_compute_totals", store=True,
    )

    # --- Paramètres d'amortissement ---
    date_start = fields.Date(
        string="In service date", required=True, tracking=True,
        default=fields.Date.context_today,
    )
    duration_months = fields.Integer(
        string="Duration (months)", required=True, default=60, tracking=True,
    )
    periodicity = fields.Selection(
        [("monthly", "Monthly"), ("quarterly", "Quarterly"), ("yearly", "Yearly")],
        string="Periodicity", default="monthly", required=True, tracking=True,
    )
    prorata = fields.Boolean(
        string="Prorata temporis", default=True,
        help="Reduce the first instalment to the number of days actually "
             "elapsed. An asset put in service on the 15th is not depreciated "
             "for the whole month.",
    )

    # --- Comptes ---
    journal_id = fields.Many2one(
        "account.journal", string="Journal", required=True,
        domain="[('type', '=', 'general'), ('company_id', '=', company_id)]",
        check_company=True,
    )
    account_asset_id = fields.Many2one(
        "account.account", string="Asset account", required=True,
        domain="[('account_type', 'in', ('asset_fixed', 'asset_non_current'))]",
        check_company=True,
    )
    account_depreciation_id = fields.Many2one(
        "account.account", string="Depreciation account", required=True,
        check_company=True,
        # Les comptes de charges et de produits sont écartés : un
        # amortissement cumulé est toujours un compte de bilan. Sans ce
        # filtre, le champ acceptait n'importe quel compte — y compris un
        # compte de ventes — et produisait des écritures équilibrées mais
        # absurdes, qu'aucun contrôle arithmétique ne signale.
        domain="[('account_type', 'not in', ("
               "'income', 'income_other', 'expense', 'expense_other',"
               "'expense_depreciation', 'expense_direct_cost', 'off_balance'))]",
        # L'aide décrit ce que le compte doit être, pas son numéro dans un
        # plan comptable donné. Le module est universel : citer le seul plan
        # français y ferait passer une convention nationale pour la règle.
        help="Accumulated depreciation, credited by each instalment. This is "
             "a balance sheet account, never a profit and loss one "
             "(28x in the French chart, 281x in the Spanish one).",
    )
    account_expense_id = fields.Many2one(
        "account.account", string="Expense account", required=True,
        # `expense_other` compris : le plan français type les dotations 6811
        # en « Other Expenses ». Sans lui, aucun compte de dotation n'était
        # proposé sur une société française (constaté au port 20.0).
        domain="[('account_type', 'in', ('expense', 'expense_other', 'expense_depreciation'))]",
        check_company=True,
    )

    # --- Rattachement à l'écriture d'acquisition ---
    #
    # Rien n'oblige à enregistrer un bien qui existe en comptabilité : on peut
    # créer une fiche sans qu'aucune écriture ne lui corresponde, et amortir un
    # bien qui n'est pas au bilan. Le tableau serait juste, la comptabilité
    # fausse, et aucun contrôle arithmétique ne le verrait.
    original_move_id = fields.Many2one(
        "account.move", string="Acquisition entry", check_company=True,
        domain="[('state', '=', 'posted'), ('company_id', '=', company_id)]",
        help="Journal entry that brought this asset into the books. Optional, "
             "but without it nothing guarantees the asset actually exists in "
             "the ledger.",
    )
    acquisition_warning = fields.Char(
        compute="_compute_acquisition_warning",
        help="Set when the asset is not backed by a posted acquisition entry.",
    )

    line_ids = fields.One2many(
        "expodo.asset.line", "asset_id", string="Depreciation board", copy=False,
    )
    posted_count = fields.Integer(compute="_compute_totals", store=True)
    pending_count = fields.Integer(compute="_compute_totals", store=True)

    # ------------------------------------------------------------------
    # Contrôles
    # ------------------------------------------------------------------

    @api.constrains("acquisition_value", "salvage_value", "already_depreciated",
                    "duration_months")
    def _check_values(self):
        for asset in self:
            if asset.acquisition_value <= 0:
                raise ValidationError(
                    self.env._("The acquisition value must be positive."))
            if asset.salvage_value < 0:
                raise ValidationError(
                    self.env._("The salvage value cannot be negative."))
            if asset.salvage_value >= asset.acquisition_value:
                raise ValidationError(self.env._(
                    "The salvage value must be lower than the acquisition value."))
            if asset.duration_months <= 0:
                raise ValidationError(
                    self.env._("The duration must be at least one month."))
            base = asset.acquisition_value - asset.salvage_value
            if asset.already_depreciated > base:
                raise ValidationError(self.env._(
                    "The amount already depreciated (%(done).2f) exceeds the "
                    "depreciable base (%(base).2f).",
                    done=asset.already_depreciated, base=base))

    @api.constrains("account_depreciation_id")
    def _check_depreciation_account_type(self):
        """Le compte d'amortissement doit être un compte de bilan.

        Le domaine de la vue guide la saisie, il ne protège pas d'un import
        de données ni d'un appel par programme — deux chemins par lesquels
        arrivent la plupart des reprises de données lors d'une migration.
        """
        interdits = (
            "income", "income_other", "expense", "expense_other",
            "expense_depreciation", "expense_direct_cost", "off_balance",
        )
        for asset in self:
            compte = asset.account_depreciation_id
            if compte and compte.account_type in interdits:
                raise ValidationError(self.env._(
                    "%(account)s is a profit and loss account. Accumulated "
                    "depreciation must be posted to a balance sheet account.",
                    account=compte.display_name))

    @api.constrains("account_depreciation_id", "account_expense_id")
    def _check_accounts_differ(self):
        for asset in self:
            if asset.account_depreciation_id == asset.account_expense_id:
                raise ValidationError(self.env._(
                    "The depreciation and expense accounts must be different: "
                    "an instalment debits one and credits the other."))

    @api.constrains("original_move_id", "account_asset_id", "acquisition_value")
    def _check_original_move(self):
        """L'écriture rattachée doit réellement porter le bien.

        Rattacher une écriture qui ne touche pas le compte d'immobilisation
        donnerait une fausse assurance — pire que pas de rattachement du tout.
        """
        for asset in self.filtered("original_move_id"):
            lignes = asset.original_move_id.line_ids.filtered(
                lambda l: l.account_id == asset.account_asset_id)
            if not lignes:
                raise ValidationError(self.env._(
                    "Entry %(move)s has no line on the asset account "
                    "%(account)s. It cannot be the acquisition entry of "
                    "“%(asset)s”.",
                    move=asset.original_move_id.name,
                    account=asset.account_asset_id.display_name,
                    asset=asset.display_name))
            debit = sum(lignes.mapped("debit"))
            if debit < asset.acquisition_value - 0.01:
                raise ValidationError(self.env._(
                    "Entry %(move)s debits only %(debit).2f on the asset "
                    "account, less than the acquisition value %(value).2f of "
                    "“%(asset)s”.",
                    move=asset.original_move_id.name, debit=debit,
                    value=asset.acquisition_value, asset=asset.display_name))

    expense_type_warning = fields.Char(
        compute="_compute_expense_type_warning",
        help="Set when the expense account is not typed as depreciation.",
    )

    @api.depends("account_expense_id", "account_depreciation_id")
    def _compute_expense_type_warning(self):
        """Signale une dotation que le tableau de flux ne saurait pas isoler.

        Le tableau de flux réintègre comme charge sans effet de trésorerie
        toute charge typée « dotation », et toute charge dont l'écriture
        mouvemente une immobilisation (`asset_fixed`) : c'est le cas des
        échéances d'un bien dont le compte d'amortissement est une
        immobilisation, 2818 en France, quel que soit le type du 6811.

        Le message s'affichait dès que le compte de charge n'était pas typé
        « dotation », donc sur toute société française, alors que le tableau
        de flux réintègre ces dotations depuis le port 20.0. Il ne reste utile
        que si ni le compte de charge ni le compte d'amortissement ne
        permettent de reconnaître la dotation.
        """
        for asset in self:
            compte = asset.account_expense_id
            asset.expense_type_warning = False
            if (compte and compte.account_type != "expense_depreciation"
                    and asset.account_depreciation_id
                    and asset.account_depreciation_id.account_type != "asset_fixed"):
                asset.expense_type_warning = self.env._(
                    "Neither the expense account %(account)s (typed “%(type)s”) "
                    "nor the depreciation account is typed as depreciation or "
                    "fixed asset. Depreciation will still be posted correctly, "
                    "but the cash flow statement will report it inside the net "
                    "result instead of adding it back separately.",
                    account=compte.display_name,
                    type=dict(compte._fields["account_type"].selection).get(
                        compte.account_type, compte.account_type),
                )

    @api.depends("original_move_id")
    def _compute_acquisition_warning(self):
        for asset in self:
            asset.acquisition_warning = False if asset.original_move_id else self.env._(
                "No acquisition entry is linked. Nothing guarantees this asset "
                "exists in the ledger: it could be depreciated while absent "
                "from the balance sheet.")

    def action_check_consistency(self):
        """Compare, par compte d'immobilisation, les fiches et le grand livre.

        Un écart signale soit un bien enregistré sans écriture d'acquisition,
        soit une immobilisation comptabilisée que personne n'amortit. Les deux
        passent inaperçus autrement : la comptabilité reste équilibrée dans les
        deux cas.

        Appelée sur un ensemble d'immobilisations, la méthode ne contrôle que
        les comptes que celles-ci utilisent ; appelée sur le modèle, elle
        contrôle tout. Le second usage sert le menu, le premier permet de
        vérifier une sélection — et rend le contrôle utilisable dans un test,
        qui ne maîtrise pas ce que le reste de la base contient.
        """
        if self:
            comptes_vises = self.mapped("account_asset_id")
            actifs = self.search([
                ("state", "in", ("draft", "running")),
                ("account_asset_id", "in", comptes_vises.ids),
            ])
        else:
            actifs = self.search([("state", "in", ("draft", "running"))])
        ecarts = []
        for compte in actifs.mapped("account_asset_id"):
            fiches = actifs.filtered(lambda a: a.account_asset_id == compte)
            valeur_fiches = sum(fiches.mapped("acquisition_value"))
            self.env.cr.execute("""
                SELECT COALESCE(SUM(l.balance), 0.0)
                  FROM account_move_line l
                  JOIN account_move m ON m.id = l.move_id
                 WHERE m.state = 'posted' AND l.account_id = %s
            """, (compte.id,))
            solde = float(self.env.cr.fetchone()[0])
            if abs(valeur_fiches - solde) > 0.01:
                ecarts.append(self.env._(
                    "%(account)s: %(cards).2f recorded on asset cards, "
                    "%(ledger).2f in the ledger (difference %(gap).2f)",
                    account=compte.display_name, cards=valeur_fiches,
                    ledger=solde, gap=valeur_fiches - solde))

        if not ecarts:
            message = self.env._(
                "Every asset account matches the asset cards recorded against it.")
            type_message = "success"
        else:
            message = self.env._(
                "%(count)d asset account(s) do not match:\n\n%(details)s",
                count=len(ecarts), details="\n".join(ecarts))
            type_message = "warning"
        # La notification est suivie de la liste des immobilisations : lancée
        # depuis le menu, elle laissait sinon l'utilisateur devant un écran
        # vide, sans rien à faire du résultat qu'il venait de demander.
        #
        # L'action est construite ici plutôt que lue en base : lire un
        # enregistrement `ir.actions.act_window` exige un droit
        # d'administrateur, et un comptable recevait une erreur d'accès à la
        # place de son résultat.
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": self.env._("Asset consistency"),
                "message": message,
                "type": type_message,
                "sticky": bool(ecarts),
                "next": {
                    "type": "ir.actions.act_window",
                    "name": self.env._("Assets"),
                    "res_model": "expodo.asset",
                    "view_mode": "list,form",
                    "views": [(False, "list"), (False, "form")],
                    "target": "main",
                },
            },
        }

    @api.depends("line_ids.amount", "line_ids.state", "already_depreciated",
                 "acquisition_value")
    def _compute_totals(self):
        for asset in self:
            postees = asset.line_ids.filtered(lambda l: l.state == "posted")
            asset.depreciated_value = asset.already_depreciated + sum(postees.mapped("amount"))
            asset.remaining_value = asset.acquisition_value - asset.depreciated_value
            asset.posted_count = len(postees)
            asset.pending_count = len(asset.line_ids) - len(postees)

    # ------------------------------------------------------------------
    # Tableau d'amortissement
    # ------------------------------------------------------------------

    def _build_board(self):
        """Calcule le tableau et le contrôle, sans rien écrire en comptabilité."""
        self.ensure_one()
        try:
            # La précision vient de la devise du bien, pas d'un principe.
            #
            # Calculé au centime en yen, le tableau annonçait 10 000 pour une
            # somme d'échéances de 9 996 une fois enregistrée : la monnaie
            # japonaise n'a pas de subdivision, et chaque dotation perdait
            # ses décimales à l'écriture. Quatre yens restaient au compte
            # d'immobilisation sans que rien les explique.
            decimales = self.currency_id.decimal_places
            lignes = calculer_tableau(
                valeur_acquisition=self.acquisition_value,
                date_mise_en_service=self.date_start,
                duree_mois=self.duration_months,
                periodicite=self.periodicity,
                valeur_residuelle=self.salvage_value,
                deja_amorti=self.already_depreciated,
                prorata=self.prorata,
                decimales=decimales,
            )
            controler_tableau(
                lignes, self.acquisition_value, self.salvage_value,
                self.already_depreciated, decimales=decimales,
            )
        except AmortissementError as error:
            raise UserError(self.env._(
                "Depreciation board for “%(asset)s”: %(error)s",
                asset=self.display_name, error=str(error))) from error
        return lignes

    def action_compute_board(self):
        """Recalcule le tableau. Les échéances déjà comptabilisées sont gardées."""
        for asset in self:
            postees = asset.line_ids.filtered(lambda l: l.state == "posted")
            if postees:
                raise UserError(self.env._(
                    "“%(asset)s” already has %(count)d posted instalment(s). "
                    "Recomputing the board would desynchronise it from the "
                    "ledger. Dispose of the asset and record a new one instead.",
                    asset=asset.display_name, count=len(postees)))
            asset.line_ids.unlink()
            asset.line_ids = [
                (0, 0, {
                    "sequence": l.numero,
                    "date": l.date_echeance,
                    "amount": l.dotation,
                    "cumulative": l.cumul,
                    "remaining": l.valeur_residuelle,
                })
                for l in asset._build_board()
            ]
        return True

    def action_validate(self):
        """Passe en cours : le tableau est figé, les écritures restent à valider."""
        for asset in self:
            if asset.state != "draft":
                raise UserError(self.env._(
                    "Only a draft asset can be validated."))
            if not asset.line_ids:
                asset.action_compute_board()
            asset.state = "running"
            asset.message_post(body=self.env._(
                "Asset validated. %(count)d instalment(s) to be posted, each "
                "requiring confirmation.", count=len(asset.line_ids)))
        return True

    def action_set_draft(self):
        for asset in self:
            if asset.line_ids.filtered(lambda l: l.state == "posted"):
                raise UserError(self.env._(
                    "An asset with posted instalments cannot be set back to draft."))
            asset.state = "draft"
        return True

    def unlink(self):
        """Interdit la suppression d'un bien dont des écritures existent.

        Les échéances sont liées en `ondelete="cascade"` : supprimer la fiche
        efface le tableau sans passer par le contrôle posé sur la ligne, et
        laisse au grand livre des dotations comptabilisées que plus aucune
        fiche ne justifie. Elles restent équilibrées, donc rien ne les
        signale — on les découvre en cherchant d'où vient une charge.

        Un bien qui ne doit plus être amorti se sort par l'assistant de
        cession : le grand livre garde alors la trace de ce qui s'est passé.
        """
        comptabilises = self.filtered(
            lambda a: a.line_ids.filtered(lambda l: l.state == "posted"))
        if comptabilises:
            raise UserError(self.env._(
                "%(assets)s cannot be deleted: %(count)d depreciation "
                "entries have already been posted, and deleting the asset "
                "would leave them in the ledger with nothing to justify them. "
                "Dispose of the asset instead — the ledger then keeps a trace "
                "of what happened.",
                assets=", ".join(comptabilises.mapped("display_name")),
                count=len(comptabilises.line_ids.filtered(
                    lambda l: l.state == "posted"))))
        return super().unlink()

    def action_open_disposal(self):
        """Ouvre l'assistant de sortie, contexte compris.

        Un bouton d'en-tête de type `action` ne transmet pas toujours
        `active_id` à l'assistant : ouvert ainsi, il s'affichait sans bien, sans
        valeur nette et sans compte — inutilisable. Construire l'action ici rend
        le passage explicite et ne dépend d'aucun comportement implicite de la
        vue.
        """
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": self.env._("Dispose of asset"),
            "res_model": "expodo.asset.disposal",
            "view_mode": "form",
            "views": [(False, "form")],
            "target": "new",
            "context": {
                "active_model": "expodo.asset",
                "active_id": self.id,
                "active_ids": self.ids,
                "default_asset_id": self.id,
                # Le compte de charge n'est **pas** transmis ici : l'assistant
                # le calcule lui-même. Le proposer des deux côtés créait deux
                # sources de vérité, et la correction apportée à l'une laissait
                # l'autre intacte — l'écran continuait d'afficher un compte que
                # le serveur ne proposait plus, sans que rien ne l'explique.
            },
        }

    def action_open_lines(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": self.env._("Depreciation board"),
            "res_model": "expodo.asset.line",
            "view_mode": "list,form",
            "views": [(False, "list"), (False, "form")],
            "domain": [("asset_id", "=", self.id)],
            "context": {"default_asset_id": self.id, "create": False},
        }
