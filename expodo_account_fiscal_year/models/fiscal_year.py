# -*- coding: utf-8 -*-
# Copyright 2026 Expodo (https://expodo.fr)
# License LGPL-3
"""Exercices fiscaux déclarés explicitement.

Ce que Community sait faire
---------------------------

`res.company` porte `fiscalyear_last_day` et `fiscalyear_last_month`, et
`compute_fiscalyear_dates` en déduit les bornes de l'exercice contenant une
date donnée. Cela exprime un **motif répétitif** : tous les exercices durent
douze mois et se closent le même jour.

Ce que cela ne sait pas exprimer
--------------------------------

Un exercice irrégulier. Or il y en a dans la vie de presque toute société :

* le **premier exercice**, qui court de la constitution à la première clôture
  et dure rarement douze mois — souvent quinze, parfois trois ;
* l'**exercice de transition**, quand une société change de date de clôture,
  par exemple pour s'aligner sur son groupe ;
* le **dernier exercice** d'une société en liquidation.

Sans moyen de les déclarer, tous les calculs qui s'appuient sur l'exercice
retombent sur le motif répétitif et se trompent de période. Le défaut est
silencieux : les états s'affichent, ils couvrent simplement les mauvaises
dates. Un bilan de quinze mois présenté comme un bilan de douze n'a rien qui
le signale.

Ce module fournit la déclaration explicite et branche
`compute_fiscalyear_dates` dessus, de sorte que **tout ce qui lit l'exercice en
bénéficie sans le savoir** : la période par défaut des états, la clôture, la
période du fichier des écritures comptables.
"""

from odoo import _, api, fields, models
from odoo.exceptions import ValidationError


class ExerciceFiscal(models.Model):
    _name = "expodo.fiscal.year"
    _description = "Fiscal year"
    _order = "date_from desc, id desc"

    name = fields.Char(string="Name", required=True)
    company_id = fields.Many2one(
        "res.company", string="Company", required=True,
        default=lambda self: self.env.company, index=True)
    date_from = fields.Date(string="Start date", required=True)
    date_to = fields.Date(string="End date", required=True)

    duration_months = fields.Integer(
        string="Duration (months)", compute="_compute_duration",
        help="Shown so that an unintended length is visible at a glance. A "
             "fiscal year of eleven or thirteen months is usually legitimate; "
             "one of one or of twenty-four is usually a typing mistake.")

    @api.depends("date_from", "date_to")
    def _compute_duration(self):
        for exercice in self:
            if exercice.date_from and exercice.date_to:
                mois = ((exercice.date_to.year - exercice.date_from.year) * 12
                        + exercice.date_to.month - exercice.date_from.month)
                exercice.duration_months = mois + 1
            else:
                exercice.duration_months = 0

    # ------------------------------------------------------------------
    # Contrôles
    # ------------------------------------------------------------------

    @api.constrains("date_from", "date_to")
    def _verifier_ordre(self):
        for exercice in self:
            if exercice.date_from > exercice.date_to:
                raise ValidationError(_(
                    "“%(name)s” ends before it starts.", name=exercice.name))

    @api.constrains("date_from", "date_to", "company_id")
    def _verifier_absence_de_chevauchement(self):
        """Deux exercices ne peuvent pas se recouvrir.

        Un chevauchement rendrait la réponse à « quel est l'exercice du 15
        mars ? » arbitraire : selon l'ordre de lecture, les états couvriraient
        une période ou l'autre. Une donnée qui change de sens selon l'ordre de
        lecture est pire qu'une donnée absente, parce qu'elle paraît juste.
        """
        for exercice in self:
            voisin = self.search([
                ("id", "!=", exercice.id),
                ("company_id", "=", exercice.company_id.id),
                ("date_from", "<=", exercice.date_to),
                ("date_to", ">=", exercice.date_from),
            ], limit=1)
            if voisin:
                raise ValidationError(_(
                    "“%(name)s” overlaps “%(other)s” (%(start)s – %(end)s). "
                    "Fiscal years must follow one another without overlapping.",
                    name=exercice.name, other=voisin.name,
                    start=voisin.date_from, end=voisin.date_to))

    # ------------------------------------------------------------------
    # Résolution
    # ------------------------------------------------------------------

    @api.model
    def exercice_a_la_date(self, societe, jour):
        """L'exercice déclaré contenant `jour`, s'il en existe un."""
        return self.search([
            ("company_id", "=", societe.id),
            ("date_from", "<=", jour),
            ("date_to", ">=", jour),
        ], limit=1)
