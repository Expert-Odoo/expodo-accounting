# -*- coding: utf-8 -*-
# Copyright 2026 Expodo (https://expodo.fr)
# License LGPL-3
"""Périodicité de déclaration de TVA.

Odoo Community ne porte aucune notion de périodicité de déclaration : le
champ ``account_tax_periodicity`` appartient à l'édition Enterprise. La
portée de date ``previous_return_period``, utilisée par les reports de crédit
de TVA d'une déclaration à la suivante, en dépend pourtant directement.

Ce module fournit donc le paramètre manquant. C'est la seule extension de
modèle qu'il introduit en dehors des modèles de rapport eux-mêmes ; elle est
justifiée par une absence dans Community, non par un choix de conception.
"""

from odoo import fields, models

#: Nombre de mois couverts par une déclaration, selon la périodicité.
PERIODICITY_MONTHS = {
    "monthly": 1,
    "trimester": 3,
    "4_months": 4,
    "semester": 6,
    "year": 12,
}


class ResCompany(models.Model):
    _inherit = "res.company"

    expodo_tax_periodicity = fields.Selection(
        selection=[
            ("monthly", "Mensuelle"),
            ("trimester", "Trimestrielle"),
            ("4_months", "Quadrimestrielle"),
            ("semester", "Semestrielle"),
            ("year", "Annuelle"),
        ],
        string="Périodicité de déclaration de TVA",
        default="monthly",
        required=True,
        help="Détermine la période couverte par une déclaration de TVA. "
             "Utilisée pour imputer un crédit de TVA d'une déclaration sur la "
             "suivante. En France, le régime réel normal est mensuel, le "
             "régime simplifié annuel.",
    )

    def _expodo_periodicity_months(self):
        """Nombre de mois couverts par une déclaration de cette société."""
        self.ensure_one()
        return PERIODICITY_MONTHS[self.expodo_tax_periodicity]
