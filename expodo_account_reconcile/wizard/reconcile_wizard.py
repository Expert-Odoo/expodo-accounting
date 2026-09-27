# -*- coding: utf-8 -*-
# Copyright 2026 Expodo (https://expodo.fr)
# License LGPL-3
"""Lettrage des écritures.

Ce que Community possède déjà
-----------------------------

Tout le moteur. `account.move.line.reconcile()`, les rapprochements partiels
et complets, les modèles de lettrage, la génération des écarts de change : rien
de cela n'est propre à l'édition Enterprise.

Ce qui manque est l'accès. Aucune action, aucun assistant, aucun bouton dans
les vues liste. L'édition Community livre même une action serveur « Annuler le
lettrage » — on peut donc défaire ce qu'on n'a aucun moyen de faire.

Ce module fournit l'accès, pas le moteur. Il n'appelle que `reconcile()`, la
méthode d'Odoo, et laisse le cœur gérer les écarts de change, les taxes sur
encaissement et les contrôles de cohérence. Réimplémenter ces règles serait à
la fois inutile et dangereux : elles changent avec les versions.
"""

from odoo import _, api, fields, models
from odoo.exceptions import UserError


class AssistantLettrage(models.TransientModel):
    """Lettrage manuel d'une sélection de lignes."""

    _name = "expodo.reconcile.wizard"
    _description = "Reconcile selected journal items"

    line_ids = fields.Many2many(
        "account.move.line", string="Journal items", required=True)
    account_id = fields.Many2one(
        "account.account", string="Account", readonly=True)
    partner_id = fields.Many2one(
        "res.partner", string="Partner", readonly=True)
    currency_id = fields.Many2one(
        "res.currency", string="Currency", readonly=True)

    total_debit = fields.Monetary(
        string="Total debit", readonly=True, currency_field="currency_id")
    total_credit = fields.Monetary(
        string="Total credit", readonly=True, currency_field="currency_id")
    difference = fields.Monetary(
        string="Difference", readonly=True, currency_field="currency_id",
        help="Debit minus credit. When it is not zero, the lines can only be "
             "partially reconciled unless the difference is written off.")

    write_off = fields.Boolean(
        string="Write off the difference",
        help="Posts an entry for the remaining difference so that the lines "
             "reconcile fully. Typical for a rounding gap or a discount "
             "granted after the fact.")
    write_off_account_id = fields.Many2one(
        "account.account", string="Write-off account",
        domain="[('active', '=', True)]")
    write_off_journal_id = fields.Many2one(
        "account.journal", string="Write-off journal",
        domain="[('type', '=', 'general')]")
    write_off_label = fields.Char(
        string="Write-off label", default=lambda self: _("Reconciliation difference"))

    # ------------------------------------------------------------------
    # Chargement depuis la sélection
    # ------------------------------------------------------------------

    @api.model
    def default_get(self, champs):
        valeurs = super().default_get(champs)
        ids = self.env.context.get("active_ids") or []
        if self.env.context.get("active_model") != "account.move.line" or not ids:
            return valeurs

        lignes = self.env["account.move.line"].browse(ids).exists()
        self._verifier(lignes)
        valeurs["line_ids"] = [(6, 0, lignes.ids)]
        valeurs["account_id"] = lignes[:1].account_id.id
        valeurs["currency_id"] = lignes[:1].company_currency_id.id

        partenaires = lignes.mapped("partner_id")
        if len(partenaires) == 1:
            valeurs["partner_id"] = partenaires.id

        debit = sum(lignes.mapped("debit"))
        credit = sum(lignes.mapped("credit"))
        valeurs["total_debit"] = debit
        valeurs["total_credit"] = credit
        valeurs["difference"] = debit - credit

        journal = self.env["account.journal"].search([
            ("type", "=", "general"),
            ("company_id", "=", lignes[:1].company_id.id),
        ], limit=1)
        if journal:
            valeurs["write_off_journal_id"] = journal.id
        return valeurs

    @api.model
    def _verifier(self, lignes):
        """Refuse une sélection que le lettrage ne peut pas traiter.

        Chaque refus est formulé avec ce qui ne va pas *et* ce qu'il faut
        faire. Un message qui dit seulement « impossible » oblige l'utilisateur
        à deviner, et il devine souvent qu'il s'agit d'un défaut du logiciel.
        """
        if len(lignes) < 2:
            raise UserError(_(
                "Select at least two journal items: reconciling means matching "
                "them against each other."))

        if any(ligne.parent_state != "posted" for ligne in lignes):
            raise UserError(_(
                "Only posted journal items can be reconciled. Post the draft "
                "entries first."))

        comptes = lignes.mapped("account_id")
        if len(comptes) > 1:
            raise UserError(_(
                "The selected items sit on several accounts (%(accounts)s). "
                "Reconciliation matches items within one account.",
                accounts=", ".join(comptes.mapped("code"))))

        if not comptes.reconcile:
            raise UserError(_(
                "Account %(code)s is not marked as reconcilable. Tick "
                "“Allow Reconciliation” on the account if items on it are "
                "meant to be matched.", code=comptes.code))

        if len(lignes.mapped("company_id")) > 1:
            raise UserError(_(
                "The selected items belong to several companies. Reconcile "
                "them company by company."))

        deja = lignes.filtered("reconciled")
        if len(deja) == len(lignes):
            raise UserError(_(
                "Every selected item is already fully reconciled."))

    # ------------------------------------------------------------------
    # Exécution
    # ------------------------------------------------------------------

    def action_reconcile(self):
        self.ensure_one()
        lignes = self.line_ids
        self._verifier(lignes)

        if self.write_off:
            if not self.write_off_account_id or not self.write_off_journal_id:
                raise UserError(_(
                    "Writing off the difference needs both an account and a "
                    "journal."))
            lignes |= self._ecrire_ecart()

        # On délègue entièrement au moteur d'Odoo : écarts de change, taxes
        # sur encaissement et contrôles de cohérence sont son affaire, et ses
        # règles évoluent d'une version à l'autre.
        lignes.reconcile()
        return {"type": "ir.actions.act_window_close"}

    def _ecrire_ecart(self):
        """Comptabilise l'écart et retourne la ligne à lettrer.

        L'écriture porte la date la plus récente de la sélection : antidater
        un écart de lettrage placerait une écriture dans une période close, et
        produirait une date de validation antérieure à l'écriture — l'anomalie
        que l'administration recherche en priorité dans un FEC.
        """
        self.ensure_one()
        ecart = self.difference
        if not ecart:
            raise UserError(_("There is no difference to write off."))

        date_ecriture = max(self.line_ids.mapped("date"))
        libelle = self.write_off_label or _("Reconciliation difference")

        ecriture = self.env["account.move"].create({
            "company_id": self.line_ids[:1].company_id.id,
            "journal_id": self.write_off_journal_id.id,
            "date": date_ecriture,
            "ref": libelle,
            "line_ids": [
                # La contrepartie sur le compte lettré annule l'écart, ce qui
                # permet aux lignes de se solder entre elles.
                (0, 0, {
                    "account_id": self.account_id.id,
                    "partner_id": self.partner_id.id or False,
                    "name": libelle,
                    "debit": 0.0 if ecart > 0 else -ecart,
                    "credit": ecart if ecart > 0 else 0.0,
                }),
                (0, 0, {
                    "account_id": self.write_off_account_id.id,
                    "partner_id": self.partner_id.id or False,
                    "name": libelle,
                    "debit": ecart if ecart > 0 else 0.0,
                    "credit": 0.0 if ecart > 0 else -ecart,
                }),
            ],
        })
        ecriture.action_post()
        return ecriture.line_ids.filtered(
            lambda l: l.account_id == self.account_id)


class AssistantLettrageAutomatique(models.TransientModel):
    """Lettrage automatique par rapprochement de montants."""

    _name = "expodo.auto.reconcile.wizard"
    _description = "Reconcile journal items automatically"

    company_id = fields.Many2one(
        "res.company", string="Company", required=True,
        default=lambda self: self.env.company)
    account_ids = fields.Many2many(
        "account.account", string="Accounts",
        domain="[('reconcile', '=', True), ('active', '=', True)]",
        help="Leave empty to cover every reconcilable account.")
    partner_ids = fields.Many2many(
        "res.partner", string="Partners",
        help="Leave empty to cover every partner.")
    date_to = fields.Date(
        string="Up to", default=fields.Date.context_today, required=True)

    reconciled_count = fields.Integer(string="Items reconciled", readonly=True)
    group_count = fields.Integer(string="Groups matched", readonly=True)
    message = fields.Text(string="Result", readonly=True)

    def action_auto_reconcile(self):
        """Lettre les groupes dont le solde est exactement nul.

        La règle retenue est volontairement stricte : même compte, même tiers,
        et solde nul au centime. Elle ne lettre donc que ce qui ne prête à
        aucune discussion.

        Un rapprochement approximatif — à quelques centimes près, ou par
        rapprochement de libellés — lettrerait davantage de lignes et en
        lettrerait de fausses. Or un lettrage erroné se défait à la main,
        écriture par écriture, et masque entre-temps une créance réellement
        impayée. Mieux vaut en laisser à l'utilisateur que lui en cacher.
        """
        self.ensure_one()

        domaine = [
            ("company_id", "=", self.company_id.id),
            ("parent_state", "=", "posted"),
            ("reconciled", "=", False),
            ("account_id.reconcile", "=", True),
            ("date", "<=", self.date_to),
            ("balance", "!=", 0.0),
        ]
        if self.account_ids:
            domaine.append(("account_id", "in", self.account_ids.ids))
        if self.partner_ids:
            domaine.append(("partner_id", "in", self.partner_ids.ids))

        lignes = self.env["account.move.line"].search(domaine)

        groupes = {}
        for ligne in lignes:
            groupes.setdefault(
                (ligne.account_id.id, ligne.partner_id.id), self.env["account.move.line"]
            )
            groupes[(ligne.account_id.id, ligne.partner_id.id)] |= ligne

        lettrees = 0
        groupes_traites = 0
        for cle, groupe in groupes.items():
            if len(groupe) < 2:
                continue
            solde = sum(groupe.mapped("balance"))
            if abs(solde) > 0.005:
                continue
            # Un groupe entièrement au débit ou entièrement au crédit ne se
            # compense pas : son solde nul signifierait qu'il est vide.
            if not (any(l.balance > 0 for l in groupe)
                    and any(l.balance < 0 for l in groupe)):
                continue
            try:
                groupe.reconcile()
            except UserError:
                # Un groupe refusé par le moteur n'interrompt pas les autres :
                # l'utilisateur préfère que les cas nets passent et que les
                # cas litigieux lui restent.
                continue
            lettrees += len(groupe)
            groupes_traites += 1

        self.write({
            "reconciled_count": lettrees,
            "group_count": groupes_traites,
            "message": _(
                "%(lines)s items reconciled across %(groups)s partner "
                "balances. Items left unreconciled either do not net to zero "
                "or have no counterpart on the same account and partner.",
                lines=lettrees, groups=groupes_traites),
        })
        return {
            # Le titre est repris du menu. Sans lui, Odoo intitule la
            # fenêtre « Odoo » : l'utilisateur clique sur « Automatic Reconciliation »,
            # la fenêtre s'ouvre avec ce titre, puis le perd dès la première
            # action et il ne sait plus dans quel assistant il se trouve.
            "name": _('Automatic Reconciliation'),
            "type": "ir.actions.act_window",
            "res_model": self._name,
            "res_id": self.id,
            "view_mode": "form",
            "target": "new",
        }
