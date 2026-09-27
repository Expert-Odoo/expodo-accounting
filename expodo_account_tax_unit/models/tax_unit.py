# -*- coding: utf-8 -*-
# Copyright 2026 Expodo (https://expodo.fr)
# License LGPL-3
"""Unités fiscales.

Plusieurs sociétés liées peuvent, dans nombre d'États membres, ne déposer
qu'une seule déclaration de TVA pour l'ensemble du groupe. Une société est
désignée représentante ; c'est son numéro de TVA qui figure sur la déclaration,
et ce sont les opérations consolidées du groupe qui y sont portées.

Le régime porte des noms différents selon les pays — unité TVA en Belgique,
groupe TVA en France depuis 2023, *Organschaft* en Allemagne — et les règles
d'éligibilité varient. Ce que les régimes ont en commun, et ce que ce module
fournit, c'est la **notion de périmètre** : un ensemble de sociétés dont les
opérations se déclarent ensemble.

Ce que le module ne fait pas
----------------------------

Il ne vérifie pas l'éligibilité au régime — liens capitalistiques, économiques,
organisationnels — parce qu'elle relève du droit de chaque État et se constate,
elle ne se calcule pas.

Il n'élimine pas non plus les opérations internes au groupe. Dans plusieurs
régimes elles sortent du champ de la TVA, mais pas dans tous, et la règle
dépend du pays et de la nature de l'opération. Les éliminer d'office produirait
une déclaration fausse dans les cas où elles doivent y figurer — et ce serait
invisible, puisque le total resterait plausible.

Ces deux limites sont dites plutôt que devinées : une déclaration de groupe
engage le représentant pour toutes les sociétés du périmètre.
"""

from odoo import _, api, fields, models
from odoo.exceptions import ValidationError


class UniteFiscale(models.Model):
    _name = "expodo.tax.unit"
    _description = "Tax unit"
    _order = "name"

    name = fields.Char(string="Name", required=True)
    active = fields.Boolean(string="Active", default=True)
    country_id = fields.Many2one(
        "res.country", string="Country", required=True,
        help="Country whose VAT grouping regime applies. The scheme, and who "
             "may join it, is a matter of national law.")
    vat = fields.Char(
        string="VAT number", required=True,
        help="Number under which the group files. It is the representative's "
             "number that appears on the return.")

    company_ids = fields.Many2many(
        "res.company", string="Companies", required=True,
        help="Companies whose operations are declared together.")
    main_company_id = fields.Many2one(
        "res.company", string="Representative", required=True,
        help="Company filing on behalf of the group. It carries the "
             "obligation for every company in the scope.")

    @api.constrains("main_company_id", "company_ids")
    def _verifier_representant(self):
        """Le représentant appartient au périmètre qu'il représente.

        Sans ce contrôle, on pourrait désigner une société extérieure au
        groupe : la déclaration porterait son numéro sans couvrir ses
        opérations, et couvrirait celles de sociétés qu'elle ne représente
        pas. Les deux erreurs à la fois, sans qu'aucun total ne le montre.
        """
        for unite in self:
            if unite.main_company_id not in unite.company_ids:
                raise ValidationError(_(
                    "The representative of “%(name)s” must be one of the "
                    "companies in the unit.", name=unite.name))

    @api.constrains("company_ids")
    def _verifier_unicite_d_appartenance(self):
        """Une société n'appartient qu'à une unité par pays.

        Deux unités se recouvrant déclareraient deux fois les mêmes
        opérations, chacune paraissant complète.
        """
        for unite in self:
            for societe in unite.company_ids:
                autre = self.search([
                    ("id", "!=", unite.id),
                    ("country_id", "=", unite.country_id.id),
                    ("company_ids", "in", societe.id),
                ], limit=1)
                if autre:
                    raise ValidationError(_(
                        "%(company)s already belongs to the unit “%(other)s” "
                        "for %(country)s. Declaring the same operations twice "
                        "would make both returns look complete.",
                        company=societe.display_name, other=autre.name,
                        country=unite.country_id.name))

    def action_open_reports(self):
        """Ouvre les états sur le périmètre de l'unité.

        Le filtre multi-société du moteur d'états fait le reste : il agrège
        déjà plusieurs sociétés. L'unité ne fait que nommer le périmètre, ce
        qui évite de le resélectionner à chaque déclaration — et évite surtout
        de se tromper d'une société sans s'en apercevoir.
        """
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": _("Reports for %(name)s", name=self.name),
            "res_model": "account.report",
            "view_mode": "list,form",
            "domain": [("country_id", "in", (self.country_id.id, False))],
            "context": {"allowed_company_ids": self.company_ids.ids},
        }
