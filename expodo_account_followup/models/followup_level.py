# -*- coding: utf-8 -*-
# Copyright 2026 Expodo (https://expodo.fr)
# License LGPL-3
"""Niveaux de relance.

Une relance n'est pas un rappel isolé : c'est une gradation. Le premier courrier
est un rappel aimable, le dernier une mise en demeure, et entre les deux il y a
ce que l'entreprise décide. Ce qui compte est que la gradation soit **la même
pour tous les clients** : relancer durement un bon payeur qui a oublié une
facture coûte plus cher que la facture.

Les niveaux se déclenchent sur le retard de la plus ancienne échéance impayée.
Pas sur le montant, ni sur l'ancienneté du client : un retard est un retard, et
introduire des exceptions dans la règle est le meilleur moyen de ne plus relancer
personne.
"""

from odoo import _, api, fields, models
from odoo.exceptions import ValidationError


class NiveauRelance(models.Model):
    _name = "expodo.followup.level"
    _description = "Follow-up level"
    _order = "delay_days, id"

    name = fields.Char(string="Name", required=True, translate=True)
    company_id = fields.Many2one(
        "res.company", string="Company", required=True,
        default=lambda self: self.env.company, index=True)
    active = fields.Boolean(string="Active", default=True)

    delay_days = fields.Integer(
        string="Days overdue", required=True,
        help="Level reached once the oldest unpaid instalment is this many "
             "days past due. A negative value chases before the due date, "
             "which is a courtesy reminder rather than a follow-up.")

    subject = fields.Char(
        string="Email subject", translate=True,
        default=lambda self: _("Outstanding balance"))
    body = fields.Html(
        string="Message", translate=True,
        help="Body of the reminder. The outstanding statement is attached "
             "separately: a figure quoted inside the message and a statement "
             "showing another is the fastest way to get the whole thing "
             "disputed.")

    send_email = fields.Boolean(string="Send an email", default=True)
    print_letter = fields.Boolean(
        string="Print a letter", default=False,
        help="For levels where a trace on paper matters — a formal notice "
             "usually does.")

    @api.constrains("delay_days", "company_id")
    def _verifier_unicite_du_delai(self):
        """Deux niveaux au même délai rendraient la gradation arbitraire.

        Selon l'ordre de lecture, un client recevrait le rappel aimable ou la
        mise en demeure. Une règle qui dépend de l'ordre de lecture n'est pas
        une règle.
        """
        for niveau in self:
            jumeau = self.search([
                ("id", "!=", niveau.id),
                ("company_id", "=", niveau.company_id.id),
                ("delay_days", "=", niveau.delay_days),
            ], limit=1)
            if jumeau:
                raise ValidationError(_(
                    "“%(other)s” already triggers at %(days)s days. Two levels "
                    "at the same delay would make the sequence arbitrary.",
                    other=jumeau.name, days=niveau.delay_days))

    @api.model
    def niveau_pour_retard(self, societe, jours_de_retard):
        """Le niveau le plus élevé atteint par ce retard.

        On retient le dernier franchi, pas le premier : un client en retard de
        quatre-vingt-dix jours doit recevoir la mise en demeure, pas le rappel
        aimable qu'il a déjà eu deux fois.
        """
        return self.search([
            ("company_id", "=", societe.id),
            ("delay_days", "<=", jours_de_retard),
        ], order="delay_days desc", limit=1)
