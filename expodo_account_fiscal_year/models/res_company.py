# -*- coding: utf-8 -*-
# Copyright 2026 Expodo (https://expodo.fr)
# License LGPL-3
"""Branchement des exercices déclarés sur le calcul natif."""

from odoo import models


class Societe(models.Model):
    _inherit = "res.company"

    def compute_fiscalyear_dates(self, current_date):
        """Les bornes de l'exercice contenant `current_date`.

        Un exercice explicitement déclaré prime sur le motif répétitif de la
        société. En l'absence de déclaration, le comportement d'Odoo est
        conservé à l'identique.

        Cette surcharge est le cœur du module. Elle évite d'avoir à modifier
        chacun des endroits qui lisent un exercice — période par défaut des
        états, clôture, fichier des écritures comptables — et garantit que
        tous répondent la même chose. Des calculs d'exercice divergents selon
        le module seraient impossibles à diagnostiquer pour l'utilisateur, qui
        verrait simplement deux écrans se contredire.
        """
        self.ensure_one()
        exercice = self.env["expodo.fiscal.year"].exercice_a_la_date(
            self, current_date)
        if exercice:
            return {"date_from": exercice.date_from, "date_to": exercice.date_to}
        return super().compute_fiscalyear_dates(current_date)
