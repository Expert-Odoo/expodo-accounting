# -*- coding: utf-8 -*-
# Copyright 2026 Expodo (https://expodo.fr)
# License LGPL-3
"""Budgets.

Un budget n'a d'intérêt que comparé
-----------------------------------

Un montant prévu, seul, ne dit rien. Ce qui se regarde, c'est l'écart : combien
a été dépensé face à combien était prévu, et à quel moment de la période.

D'où la forme retenue. Chaque ligne porte un compte, une période et un montant
prévu ; le réalisé se calcule à la volée sur les écritures comptabilisées, et
l'écart avec lui. Rien n'est figé : une écriture passée aujourd'hui change le
réalisé d'hier, ce qui est le comportement attendu d'un suivi budgétaire.

Pourquoi le réalisé n'est pas stocké
------------------------------------

Le stocker imposerait de le recalculer à chaque écriture, à chaque annulation,
à chaque changement de date. Un recalcul manqué produirait un écart faux et
stable — le pire des deux mondes : une valeur qui paraît fiable parce qu'elle
ne bouge plus.

Le prorata temporel
-------------------

Comparer le réalisé de trois mois à un budget annuel n'apprend rien : l'écart
paraît toujours favorable. La ligne expose donc aussi le **budget au prorata**
de la période écoulée, qui est le seul terme de comparaison utile en cours
d'exercice.
"""

from odoo import _, api, fields, models
from odoo.exceptions import ValidationError


class Budget(models.Model):
    _name = "expodo.budget"
    _description = "Budget"
    _order = "date_from desc, id desc"

    name = fields.Char(string="Name", required=True)
    company_id = fields.Many2one(
        "res.company", string="Company", required=True,
        default=lambda self: self.env.company, index=True)
    date_from = fields.Date(string="From", required=True)
    date_to = fields.Date(string="To", required=True)
    state = fields.Selection(
        [("draft", "Draft"), ("confirmed", "Confirmed"), ("done", "Closed")],
        string="Status", default="draft", required=True)
    line_ids = fields.One2many(
        "expodo.budget.line", "budget_id", string="Lines")
    currency_id = fields.Many2one(
        related="company_id.currency_id", readonly=True)

    total_planned = fields.Monetary(
        string="Planned", compute="_compute_totals", currency_field="currency_id")
    total_actual = fields.Monetary(
        string="Actual", compute="_compute_totals", currency_field="currency_id")
    total_variance = fields.Monetary(
        string="Variance", compute="_compute_totals", currency_field="currency_id")
    mixed_natures = fields.Boolean(
        string="Mixed natures", compute="_compute_totals",
        help="True when the budget holds both income and expense accounts. "
             "Their totals cannot be added: both are entered as positive "
             "figures, so the sum adds money earned to money spent.")
    total_impact = fields.Monetary(
        string="Impact on profit", compute="_compute_totals",
        currency_field="currency_id",
        help="What the variances do to the result: an expense over its plan "
             "and income under its plan both weigh negatively.")

    @api.depends("line_ids.planned_amount", "line_ids.actual_amount",
                 "line_ids.variance", "line_ids.account_internal_group")
    def _compute_totals(self):
        """Les trois totaux, et ce qui les remplace quand ils n'ont pas de sens.

        Produits et charges se saisissent l'un et l'autre en positif : ne pas
        demander un nombre négatif à qui prévoit vingt mille de ventes évite
        une classe entière d'erreurs de signe. Mais les additionner revient à
        ajouter des euros gagnés à des euros dépensés. Le total obtenu ne veut
        rien dire, et il est d'autant plus trompeur qu'il a l'air juste.

        L'effet sur le résultat, lui, s'additionne toujours : une charge
        dépassée et un produit manqué pèsent dans le même sens.
        """
        for budget in self:
            budget.total_planned = sum(budget.line_ids.mapped("planned_amount"))
            budget.total_actual = sum(budget.line_ids.mapped("actual_amount"))
            budget.total_variance = budget.total_actual - budget.total_planned
            natures = set(budget.line_ids.mapped("account_internal_group"))
            budget.mixed_natures = (
                "income" in natures and "expense" in natures)
            budget.total_impact = sum(
                ligne.variance if ligne.account_internal_group == "income"
                else -ligne.variance
                for ligne in budget.line_ids)

    @api.constrains("date_from", "date_to")
    def _verifier_periode(self):
        for budget in self:
            if budget.date_from > budget.date_to:
                raise ValidationError(_(
                    "“%(name)s” ends before it starts.", name=budget.name))

    def action_confirm(self):
        self.state = "confirmed"

    def action_close(self):
        self.state = "done"

    def action_reset(self):
        self.state = "draft"


class LigneBudgetaire(models.Model):
    _name = "expodo.budget.line"
    _description = "Budget line"
    _order = "account_id, date_from"

    budget_id = fields.Many2one(
        "expodo.budget", string="Budget", required=True,
        ondelete="cascade", index=True)
    company_id = fields.Many2one(
        related="budget_id.company_id", store=True, index=True)
    currency_id = fields.Many2one(
        related="budget_id.currency_id", readonly=True)

    account_id = fields.Many2one(
        "account.account", string="Account", required=True,
        help="Account whose movements are compared with the planned amount.")
    # Une décoration de liste s'évalue dans le navigateur, sur les seuls
    # champs présents dans la vue : `account_id.internal_group` n'y vaut rien,
    # et les lignes en dépassement ne se coloraient pas. Le champ lié rend la
    # nature lisible depuis la vue, en colonne invisible.
    account_internal_group = fields.Selection(
        related="account_id.internal_group", string="Account nature",
        readonly=True)
    date_from = fields.Date(
        string="From", required=True,
        default=lambda self: self.env.context.get("default_date_from"))
    date_to = fields.Date(
        string="To", required=True,
        default=lambda self: self.env.context.get("default_date_to"))

    planned_amount = fields.Monetary(
        string="Planned", required=True, currency_field="currency_id",
        help="Amount expected over the period, as a positive figure for both "
             "income and expense. Asking the user to enter a negative number "
             "for revenue is a reliable way of collecting sign errors.")

    actual_amount = fields.Monetary(
        string="Actual", compute="_compute_actual",
        currency_field="currency_id")
    variance = fields.Monetary(
        string="Variance", compute="_compute_actual",
        currency_field="currency_id",
        help="Actual minus planned. Positive means over the planned amount — "
             "which is bad news for an expense and good news for income.")
    achievement = fields.Float(
        string="Achieved (%)", compute="_compute_actual",
        help="Actual as a percentage of the planned amount.")
    prorated_amount = fields.Monetary(
        string="Planned to date", compute="_compute_actual",
        currency_field="currency_id",
        help="Planned amount in proportion to the elapsed part of the period. "
             "Comparing three months of spending with a yearly budget always "
             "looks favourable; this is the figure to compare against while "
             "the period is running.")

    @api.constrains("date_from", "date_to")
    def _verifier_periode(self):
        for ligne in self:
            if ligne.date_from > ligne.date_to:
                raise ValidationError(_(
                    "The period of a budget line ends before it starts."))
            budget = ligne.budget_id
            if ligne.date_from < budget.date_from or ligne.date_to > budget.date_to:
                raise ValidationError(_(
                    "The line period (%(start)s – %(end)s) falls outside the "
                    "budget (%(bstart)s – %(bend)s). A line reaching beyond "
                    "its budget would be counted in no total.",
                    start=ligne.date_from, end=ligne.date_to,
                    bstart=budget.date_from, bend=budget.date_to))

    @api.depends("account_id", "date_from", "date_to", "planned_amount")
    def _compute_actual(self):
        """Réalisé, écart, réalisation et budget au prorata.

        Une seule requête groupée pour l'ensemble des lignes calculées. Une
        requête par ligne produirait autant d'allers-retours que le budget
        compte de postes — plusieurs centaines sur un budget détaillé, et le
        calcul se déclenche à chaque ouverture de la vue.
        """
        aujourd_hui = fields.Date.context_today(self)

        # Le regroupement porte sur (compte, période) : deux lignes peuvent
        # viser le même compte sur des mois différents.
        par_cle = {}
        for ligne in self:
            if not (ligne.account_id and ligne.date_from and ligne.date_to):
                continue
            par_cle.setdefault(
                (ligne.company_id.id, ligne.date_from, ligne.date_to),
                self.env["account.account"]
            )
            par_cle[(ligne.company_id.id, ligne.date_from, ligne.date_to)] |= ligne.account_id

        soldes = {}
        for (societe_id, debut, fin), comptes in par_cle.items():
            for compte, solde in self.env["account.move.line"]._read_group(
                    domain=[
                        ("company_id", "=", societe_id),
                        ("parent_state", "=", "posted"),
                        ("date", ">=", debut),
                        ("date", "<=", fin),
                        ("account_id", "in", comptes.ids),
                    ],
                    groupby=["account_id"], aggregates=["balance:sum"]):
                soldes[(societe_id, debut, fin, compte.id)] = solde

        for ligne in self:
            if not (ligne.account_id and ligne.date_from and ligne.date_to):
                ligne.actual_amount = 0.0
                ligne.variance = 0.0
                ligne.achievement = 0.0
                ligne.prorated_amount = 0.0
                continue

            solde = soldes.get(
                (ligne.company_id.id, ligne.date_from, ligne.date_to,
                 ligne.account_id.id), 0.0)

            # Les produits ont un solde créditeur, donc négatif. On présente
            # les deux natures en positif : un budget de vente de cent mille
            # se saisit à cent mille, pas à moins cent mille.
            if ligne.account_id.internal_group == "income":
                solde = -solde
            ligne.actual_amount = solde
            ligne.variance = solde - ligne.planned_amount
            ligne.achievement = (
                100.0 * solde / ligne.planned_amount
                if ligne.planned_amount else 0.0)

            jours_totaux = (ligne.date_to - ligne.date_from).days + 1
            if aujourd_hui >= ligne.date_to:
                ecoules = jours_totaux
            elif aujourd_hui < ligne.date_from:
                ecoules = 0
            else:
                ecoules = (aujourd_hui - ligne.date_from).days + 1
            ligne.prorated_amount = (
                ligne.planned_amount * ecoules / jours_totaux
                if jours_totaux else 0.0)

    def action_open_entries(self):
        """Ouvre les écritures derrière le réalisé.

        Un écart qu'on ne peut pas ouvrir se discute indéfiniment. Pouvoir
        descendre aux écritures termine la discussion en trente secondes.
        """
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": _("Entries behind %(account)s",
                      account=self.account_id.display_name),
            "res_model": "account.move.line",
            "view_mode": "list,form",
            "domain": [
                ("company_id", "=", self.company_id.id),
                ("parent_state", "=", "posted"),
                ("account_id", "=", self.account_id.id),
                ("date", ">=", self.date_from),
                ("date", "<=", self.date_to),
            ],
        }
