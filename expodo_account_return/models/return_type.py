# -*- coding: utf-8 -*-
# Copyright 2026 Expodo (https://expodo.fr)
# License LGPL-3
"""Types de déclaration.

Un type décrit une obligation récurrente : la déclaration de TVA mensuelle, la
CA3 trimestrielle, le relevé intracommunautaire. Il porte la périodicité, le
délai de dépôt et l'état sur lequel la déclaration s'appuie.

Séparer le type de la déclaration permet de créer les échéances d'une année
d'avance et de voir venir les dépôts, plutôt que de les découvrir à l'échéance.
"""

from dateutil.relativedelta import relativedelta

from odoo import _, api, fields, models
from odoo.exceptions import ValidationError


class TypeDeclaration(models.Model):
    _name = "expodo.return.type"
    _description = "Tax return type"
    _order = "name"

    name = fields.Char(string="Name", required=True, translate=True)
    company_id = fields.Many2one(
        "res.company", string="Company", required=True,
        default=lambda self: self.env.company, index=True)
    active = fields.Boolean(string="Active", default=True)

    periodicity = fields.Selection(
        [("monthly", "Monthly"),
         ("quarterly", "Quarterly"),
         ("yearly", "Yearly")],
        string="Periodicity", required=True, default="monthly")

    deadline_days = fields.Integer(
        string="Days to file", default=15,
        help="Number of days after the end of the period within which the "
             "return must be filed. Used to compute the due date.")

    report_id = fields.Many2one(
        "account.report", string="Report",
        help="The report this return is based on. Leave empty if the return "
             "is prepared outside Odoo; the lifecycle still applies.")

    lock_on_submission = fields.Boolean(
        string="Lock the period when filed", default=True,
        help="Moves the company's tax lock date to the end of the period once "
             "the return is filed. What has been declared should no longer "
             "move: an entry posted after filing makes the books and the "
             "return disagree, and nothing signals it.")

    @api.constrains("deadline_days")
    def _verifier_delai(self):
        for type_ in self:
            if type_.deadline_days < 0:
                raise ValidationError(_(
                    "A filing deadline cannot be negative."))

    # ------------------------------------------------------------------
    # Calcul des périodes
    # ------------------------------------------------------------------

    def bornes_periode(self, date_dans_periode):
        """Bornes de la période contenant cette date, selon la périodicité."""
        self.ensure_one()
        jour = date_dans_periode

        if self.periodicity == "monthly":
            debut = jour.replace(day=1)
            fin = debut + relativedelta(months=1, days=-1)
        elif self.periodicity == "quarterly":
            premier_mois = 3 * ((jour.month - 1) // 3) + 1
            debut = jour.replace(month=premier_mois, day=1)
            fin = debut + relativedelta(months=3, days=-1)
        else:
            # Annuel : on suit l'exercice de la société, qui ne commence pas
            # nécessairement le 1er janvier. Supposer l'année civile
            # produirait des déclarations couvrant la mauvaise période, sans
            # que rien ne le signale.
            bornes = self.company_id.compute_fiscalyear_dates(jour)
            debut, fin = bornes["date_from"], bornes["date_to"]

        return debut, fin

    def date_limite(self, fin_periode):
        """Date de dépôt au plus tard."""
        self.ensure_one()
        return fin_periode + relativedelta(days=self.deadline_days)
