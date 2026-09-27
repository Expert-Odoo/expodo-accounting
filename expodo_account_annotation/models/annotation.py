# -*- coding: utf-8 -*-
# Copyright 2026 Expodo (https://expodo.fr)
# License LGPL-3
"""Annotations sur les lignes d'états.

Un état financier répond à « combien » et jamais à « pourquoi ». Or c'est
« pourquoi » qu'on demande à l'arrêté des comptes : pourquoi les achats ont
doublé en mars, pourquoi cette provision, pourquoi cet écart avec l'an dernier.

Sans endroit où l'écrire, la réponse vit dans un courriel, un carnet, ou la
mémoire de celui qui a fait l'écriture. Elle est perdue au moment précis où on
en a besoin — c'est-à-dire un an plus tard, devant quelqu'un d'autre.

Une annotation est datée
------------------------

Elle porte sur une ligne **et sur une période**. Une explication vraie au 31
décembre 2025 ne l'est plus au 31 décembre 2026, et l'afficher sur le nouvel
exercice serait pire que ne rien afficher : le lecteur croirait avoir une
explication à jour.
"""

from odoo import _, api, fields, models
from odoo.exceptions import ValidationError


class Annotation(models.Model):
    _name = "expodo.report.annotation"
    _description = "Report line annotation"
    _order = "date_to desc, id desc"

    report_line_id = fields.Many2one(
        "account.report.line", string="Report line", required=True,
        ondelete="cascade", index=True)
    report_id = fields.Many2one(
        related="report_line_id.report_id", store=True, index=True)
    company_id = fields.Many2one(
        "res.company", string="Company", required=True,
        default=lambda self: self.env.company, index=True)

    date_from = fields.Date(string="From", required=True)
    date_to = fields.Date(string="To", required=True)

    text = fields.Text(
        string="Annotation", required=True,
        help="Why the figure is what it is. Written for whoever reads the "
             "statement next, who will not be you.")

    @api.constrains("date_from", "date_to")
    def _verifier_periode(self):
        for annotation in self:
            if annotation.date_from > annotation.date_to:
                raise ValidationError(_(
                    "The annotation period ends before it starts."))

    @api.model
    def pour_periode(self, societe, date_from, date_to):
        """Annotations dont la période recouvre celle demandée.

        Le recouvrement, et non l'égalité stricte : une note écrite pour le
        premier trimestre doit apparaître sur un état annuel, parce qu'elle
        explique une partie de ce qu'il montre. Exiger des dates identiques
        ferait disparaître l'annotation dès qu'on change de vue, ce qui est le
        moment où l'on en a le plus besoin.
        """
        annotations = self.search([
            ("company_id", "=", societe.id),
            ("date_from", "<=", date_to),
            ("date_to", ">=", date_from),
        ])
        par_ligne = {}
        for annotation in annotations:
            par_ligne.setdefault(annotation.report_line_id.id, [])
            par_ligne[annotation.report_line_id.id].append(annotation)
        return par_ligne
