# -*- coding: utf-8 -*-
# Copyright 2026 Expodo (https://expodo.fr)
# License LGPL-3
"""Échéances du tableau d'amortissement et écritures correspondantes.

Chaque écriture est créée puis comptabilisée **sur action explicite**. Rien
n'est posté par tâche planifiée : c'est le choix structurant de ce module.
"""

from odoo import api, fields, models
from odoo.exceptions import UserError


class ExpodoAssetLine(models.Model):
    _name = "expodo.asset.line"
    _description = "Depreciation board line"
    _order = "asset_id, sequence, date"
    _check_company_auto = True

    asset_id = fields.Many2one(
        "expodo.asset", string="Asset", required=True, ondelete="cascade",
        index=True,
    )
    company_id = fields.Many2one(related="asset_id.company_id", store=True)
    currency_id = fields.Many2one(related="asset_id.currency_id")

    sequence = fields.Integer(string="No.", required=True)
    date = fields.Date(string="Due date", required=True, index=True)
    amount = fields.Monetary(string="Depreciation", required=True)
    cumulative = fields.Monetary(string="Cumulative")
    remaining = fields.Monetary(string="Remaining value")

    move_id = fields.Many2one(
        "account.move", string="Journal entry", readonly=True, copy=False,
        check_company=True,
    )
    state = fields.Selection(
        [("draft", "To post"), ("posted", "Posted")],
        string="Status", default="draft", required=True, copy=False,
    )

    # `_sql_constraints` est déprécié en v19 au profit de `models.Constraint`.
    # L'ancienne forme fonctionne encore mais journalise un avertissement à
    # chaque chargement du registre.
    _amount_positive = models.Constraint(
        "CHECK(amount > 0)",
        "A depreciation instalment must be strictly positive.",
    )

    # ------------------------------------------------------------------
    # Écritures
    # ------------------------------------------------------------------

    def _check_can_post(self):
        """Refuse tout ce qui rendrait l'écriture invalide ou tardive."""
        for line in self:
            if line.state == "posted":
                raise UserError(self.env._(
                    "Instalment %(no)d of “%(asset)s” is already posted.",
                    no=line.sequence, asset=line.asset_id.display_name))
            if line.asset_id.state != "running":
                raise UserError(self.env._(
                    "“%(asset)s” is not running: validate the asset first.",
                    asset=line.asset_id.display_name))

            # Une échéance future ne doit pas être comptabilisée : ce serait
            # constater une charge avant qu'elle ne soit encourue. Le bouton
            # de la ligne appelait `action_post` sans ce contrôle, si bien
            # qu'on pouvait poster une dotation de 2029 en 2026 — découvert en
            # ouvrant simplement la fiche dans un navigateur.
            aujourdhui = fields.Date.context_today(line)
            if line.date > aujourdhui:
                raise UserError(self.env._(
                    "Instalment %(no)d falls due on %(date)s, which is in the "
                    "future. Depreciation is recognised as it is incurred, not "
                    "in advance.",
                    no=line.sequence, date=line.date))

            # Les dates de verrouillage sont vérifiées **avant** la création de
            # l'écriture. Odoo les contrôlerait au moment de comptabiliser,
            # mais l'écriture existerait déjà et resterait en brouillon dans le
            # journal, ce que personne ne remarque.
            company = line.company_id
            verrous = [
                (company.fiscalyear_lock_date, self.env._("fiscal year lock date")),
                (company.hard_lock_date, self.env._("hard lock date")),
            ]
            for verrou, libelle in verrous:
                if verrou and line.date <= verrou:
                    raise UserError(self.env._(
                        "Instalment %(no)d falls on %(date)s, which is covered "
                        "by the %(lock)s (%(lock_date)s). Adjust the lock date "
                        "or the asset's in-service date.",
                        no=line.sequence, date=line.date,
                        lock=libelle, lock_date=verrou))

    def _prepare_move_vals(self):
        self.ensure_one()
        asset = self.asset_id
        libelle = self.env._(
            "%(asset)s — depreciation %(no)d/%(total)d",
            asset=asset.name, no=self.sequence, total=len(asset.line_ids))
        return {
            "journal_id": asset.journal_id.id,
            "date": self.date,
            "ref": libelle,
            "company_id": asset.company_id.id,
            "line_ids": [
                (0, 0, {
                    "name": libelle,
                    "account_id": asset.account_expense_id.id,
                    "debit": self.amount,
                    "credit": 0.0,
                    "partner_id": asset.partner_id.id,
                }),
                (0, 0, {
                    "name": libelle,
                    "account_id": asset.account_depreciation_id.id,
                    "debit": 0.0,
                    "credit": self.amount,
                    "partner_id": asset.partner_id.id,
                }),
            ],
        }

    def action_post(self):
        """Crée puis comptabilise l'écriture de dotation.

        Le tableau complet est recontrôlé avant toute écriture : une
        incohérence introduite entre-temps, par import ou par modification
        manuelle, ne doit pas atteindre le grand livre.
        """
        self._check_can_post()
        for asset in self.mapped("asset_id"):
            asset._build_board()   # lève si le tableau est devenu incohérent

        for line in self:
            move = self.env["account.move"].create(line._prepare_move_vals())
            move.action_post()
            line.write({"move_id": move.id, "state": "posted"})

        # Un seul message par lot et par immobilisation. Un message par
        # échéance noyait le fil de discussion : quatre-vingt-quatre messages
        # pour un bien amorti sur sept ans, et plus aucune trace lisible des
        # évènements qui comptent.
        for asset in self.mapped("asset_id"):
            lignes = self.filtered(lambda l: l.asset_id == asset).sorted("sequence")
            if len(lignes) == 1:
                corps = self.env._(
                    "Instalment %(no)d posted: %(amount)s on %(date)s (%(move)s).",
                    no=lignes.sequence, amount=lignes.amount, date=lignes.date,
                    move=lignes.move_id.name)
            else:
                corps = self.env._(
                    "%(count)d instalments posted, from %(first)s to %(last)s, "
                    "totalling %(total)s.",
                    count=len(lignes), first=lignes[0].date, last=lignes[-1].date,
                    total=sum(lignes.mapped("amount")))
            asset.message_post(body=corps)

        for asset in self.mapped("asset_id"):
            if all(l.state == "posted" for l in asset.line_ids):
                asset.state = "closed"
                asset.message_post(body=self.env._("Asset fully depreciated."))
        return True

    def action_post_due(self):
        """Comptabilise les échéances arrivées à terme, et elles seules.

        Le filtrage reste utile ici : il évite de lever une erreur sur un lot
        contenant des échéances futures, là où `action_post` refuserait tout.
        """
        aujourdhui = fields.Date.context_today(self)
        echues = self.filtered(
            lambda l: l.state == "draft" and l.date <= aujourdhui)
        if not echues:
            raise UserError(self.env._(
                "No instalment is due yet. Future instalments are posted when "
                "their due date is reached."))
        return echues.action_post()

    def action_open_move(self):
        self.ensure_one()
        if not self.move_id:
            raise UserError(self.env._("This instalment has not been posted yet."))
        return {
            "type": "ir.actions.act_window",
            "name": self.env._("Journal entry"),
            "res_model": "account.move",
            "res_id": self.move_id.id,
            "view_mode": "form",
            "views": [(False, "form")],
        }

    def unlink(self):
        if self.filtered(lambda l: l.state == "posted"):
            raise UserError(self.env._(
                "A posted instalment cannot be deleted. Reverse its journal "
                "entry instead, so the ledger keeps a trace."))
        return super().unlink()
