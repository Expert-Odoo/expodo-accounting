# -*- coding: utf-8 -*-
# Copyright 2026 Expodo (https://expodo.fr)
# License LGPL-3
"""Dates de couverture sur les lignes d'écriture."""

from odoo import _, api, fields, models
from odoo.exceptions import ValidationError


class LigneEcriture(models.Model):
    _inherit = "account.move.line"

    deferred_start_date = fields.Date(
        string="Covers from",
        help="First day of the period this line actually covers. Set it when "
             "the expense or income spans a period different from its "
             "accounting date — an insurance premium, a yearly subscription, "
             "a maintenance contract.",
        index="btree_not_null",
    )
    deferred_end_date = fields.Date(
        string="Covers to",
        help="Last day of the covered period, included.",
    )
    deferred_amount = fields.Monetary(
        string="Deferred portion", compute="_compute_deferred_amount",
        currency_field="company_currency_id",
        help="Part of this line belonging to periods after the accounting "
             "date, computed pro rata by exact days. Shown for information; "
             "the deferral entry is produced by the wizard.",
    )

    @api.depends("balance", "deferred_start_date", "deferred_end_date", "date")
    def _compute_deferred_amount(self):
        moteur = self.env["expodo.deferral.engine"]
        for ligne in self:
            ligne.deferred_amount = moteur.part_reportee(
                ligne.balance, ligne.deferred_start_date,
                ligne.deferred_end_date, ligne.date) if ligne.date else 0.0

    @api.constrains("deferred_start_date", "deferred_end_date")
    def _verifier_periode(self):
        """Une période à l'envers produirait un report négatif plausible.

        Le montant obtenu resterait dans les ordres de grandeur attendus, et
        l'écriture s'équilibrerait : rien ne signalerait l'erreur avant la
        revue des comptes.
        """
        for ligne in self:
            if not (ligne.deferred_start_date and ligne.deferred_end_date):
                continue
            if ligne.deferred_start_date > ligne.deferred_end_date:
                raise ValidationError(_(
                    "The covered period of “%(line)s” ends before it starts.",
                    line=ligne.name or ligne.move_id.name))
