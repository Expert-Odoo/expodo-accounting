# -*- coding: utf-8 -*-
# Copyright 2026 Expodo (https://expodo.fr)
# License LGPL-3
"""La déclaration et son cycle de vie.

Une déclaration n'est pas un état
---------------------------------

Un état se consulte, se réédite, change quand les écritures changent. Une
déclaration est un **acte** : elle est déposée à une date, pour une période, et
engage celui qui la signe. Ce qu'elle contient ne doit plus bouger ensuite.

Odoo Community donne les états. Il ne donne rien pour suivre les dépôts, et
c'est là que les ennuis arrivent : personne ne sait quelle période a été
déclarée, ni si une écriture est venue s'ajouter après coup.

Les quatre états
----------------

    brouillon   la période est en cours ou vient de se clore
    contrôlée   les vérifications sont passées
    déposée     la déclaration est partie, la période est verrouillée
    payée       le règlement est fait

Le verrouillage à la déposition est le point important. Ce qui a été déclaré ne
doit plus bouger : une écriture comptabilisée après le dépôt fait diverger les
livres et la déclaration, et rien ne le signale — jusqu'au contrôle.

Les contrôles
-------------

Ils ne remplacent pas la relecture d'un comptable. Ils écartent les erreurs
mécaniques : écritures en brouillon dans la période, période déjà déclarée,
déclaration antérieure non déposée. Ce sont celles qui passent inaperçues
précisément parce qu'elles ne ressemblent pas à des erreurs.
"""

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError


class Declaration(models.Model):
    _name = "expodo.return"
    _description = "Tax return"
    _order = "date_to desc, id desc"
    _inherit = ["mail.thread"]

    name = fields.Char(
        string="Reference", compute="_compute_name", store=True)
    type_id = fields.Many2one(
        "expodo.return.type", string="Type", required=True,
        ondelete="restrict", index=True)
    company_id = fields.Many2one(
        "res.company", string="Company", required=True,
        default=lambda self: self.env.company, index=True)

    date_from = fields.Date(string="From", required=True)
    date_to = fields.Date(string="To", required=True)
    date_deadline = fields.Date(
        string="Due date", compute="_compute_deadline", store=True)

    state = fields.Selection(
        [("draft", "Draft"),
         ("checked", "Checked"),
         ("submitted", "Filed"),
         ("paid", "Paid")],
        string="Status", default="draft", required=True, tracking=True)

    date_submitted = fields.Date(string="Filed on", readonly=True, tracking=True)
    date_paid = fields.Date(string="Paid on", readonly=True, tracking=True)
    amount = fields.Monetary(
        string="Amount", currency_field="currency_id", tracking=True,
        help="Amount due or refundable, as filed. Entered by hand: the figure "
             "that matters is the one actually declared, which may differ from "
             "what the report shows today.")
    currency_id = fields.Many2one(
        related="company_id.currency_id", readonly=True)
    reference = fields.Char(
        string="Filing reference",
        help="Acknowledgement number returned by the tax portal.")

    check_result = fields.Text(string="Checks", readonly=True)
    note = fields.Text(string="Notes")

    # Odoo 19 déclare les contraintes SQL en attribut de classe, via
    # `models.Constraint`. L'ancien `_sql_constraints` est encore accepté par
    # l'ORM — l'attribut se lit — mais la contrainte n'est plus créée en base.
    # Elle passait donc totalement inaperçue : aucune erreur, aucun message,
    # simplement rien. C'est le test d'unicité qui l'a révélé.
    _periode_unique = models.Constraint(
        "unique (type_id, company_id, date_from, date_to)",
        "A return of this type already exists for this period. Two returns "
        "for the same period would each look complete and disagree.",
    )

    # ------------------------------------------------------------------
    # Calculs
    # ------------------------------------------------------------------

    @api.depends("type_id", "date_from", "date_to")
    def _compute_name(self):
        for declaration in self:
            if declaration.type_id and declaration.date_to:
                declaration.name = "%s — %s" % (
                    declaration.type_id.name, declaration.date_to)
            else:
                declaration.name = _("New return")

    @api.depends("type_id", "date_to")
    def _compute_deadline(self):
        for declaration in self:
            if declaration.type_id and declaration.date_to:
                declaration.date_deadline = declaration.type_id.date_limite(
                    declaration.date_to)
            else:
                declaration.date_deadline = False

    @api.constrains("date_from", "date_to")
    def _verifier_periode(self):
        for declaration in self:
            if declaration.date_from > declaration.date_to:
                raise ValidationError(_(
                    "The period of “%(name)s” ends before it starts.",
                    name=declaration.name))

    @api.onchange("type_id", "date_to")
    def _onchange_type(self):
        """Les bornes suivent la périodicité du type choisi."""
        for declaration in self:
            if not declaration.type_id:
                continue
            reference = declaration.date_to or fields.Date.context_today(
                declaration)
            debut, fin = declaration.type_id.bornes_periode(reference)
            declaration.date_from = debut
            declaration.date_to = fin

    # ------------------------------------------------------------------
    # Contrôles
    # ------------------------------------------------------------------

    def controles(self):
        """Vérifications mécaniques, celles qui passent inaperçues."""
        self.ensure_one()
        anomalies = []

        brouillons = self.env["account.move"].search_count([
            ("company_id", "=", self.company_id.id),
            ("state", "=", "draft"),
            ("date", ">=", self.date_from),
            ("date", "<=", self.date_to),
        ])
        if brouillons:
            anomalies.append(_(
                "%(count)s draft entries fall in this period. They are not in "
                "the figures and will be once posted — after the return is "
                "filed.", count=brouillons))

        # Une déclaration antérieure non déposée signale presque toujours un
        # oubli. Déposer celle-ci d'abord laisserait un trou dans la
        # chronologie, que l'administration remarque avant le déclarant.
        anterieure = self.search([
            ("company_id", "=", self.company_id.id),
            ("type_id", "=", self.type_id.id),
            ("date_to", "<", self.date_from),
            ("state", "in", ("draft", "checked")),
        ], order="date_to desc", limit=1)
        if anterieure:
            anomalies.append(_(
                "An earlier return of the same type is still unfiled "
                "(%(name)s). Filing out of order leaves a gap the tax "
                "authority notices before you do.", name=anterieure.name))

        if self.date_deadline and self.date_deadline < fields.Date.context_today(self):
            anomalies.append(_(
                "The filing deadline (%(date)s) has passed.",
                date=self.date_deadline))

        verrou = self.company_id.tax_lock_date
        if verrou and verrou >= self.date_to:
            anomalies.append(_(
                "The tax lock date (%(date)s) already covers this period, "
                "which suggests it has been filed already.", date=verrou))

        return anomalies

    def action_check(self):
        for declaration in self:
            if declaration.state not in ("draft", "checked"):
                raise UserError(_(
                    "Only a draft return can be checked."))
            anomalies = declaration.controles()
            declaration.check_result = "\n".join(anomalies) or _(
                "No mechanical anomaly found. This does not replace a review "
                "by an accountant.")
            declaration.state = "checked"
        return True

    # ------------------------------------------------------------------
    # Cycle de vie
    # ------------------------------------------------------------------

    def action_submit(self):
        """Marque la déclaration comme déposée et verrouille la période."""
        for declaration in self:
            if declaration.state not in ("checked",):
                raise UserError(_(
                    "Run the checks before filing “%(name)s”. Filing is an "
                    "act: it is worth two minutes of verification.",
                    name=declaration.name))

            declaration.write({
                "state": "submitted",
                "date_submitted": fields.Date.context_today(declaration),
            })
            declaration.message_post(body=_(
                "Return filed for %(start)s – %(end)s.",
                start=declaration.date_from, end=declaration.date_to))

            if declaration.type_id.lock_on_submission:
                declaration._verrouiller()
        return True

    def _verrouiller(self):
        """Avance la date de verrouillage fiscal jusqu'à la fin de la période.

        Jamais en arrière. Reculer un verrou rouvrirait des périodes déjà
        déclarées, et une écriture pourrait alors s'y glisser sans que
        personne ne s'en aperçoive.
        """
        self.ensure_one()
        actuel = self.company_id.tax_lock_date
        if not actuel or actuel < self.date_to:
            self.company_id.sudo().tax_lock_date = self.date_to

    def action_pay(self):
        for declaration in self:
            if declaration.state != "submitted":
                raise UserError(_(
                    "Only a filed return can be marked as paid."))
            declaration.write({
                "state": "paid",
                "date_paid": fields.Date.context_today(declaration),
            })
        return True

    def action_reset(self):
        """Ramène au brouillon, sans toucher au verrou.

        Le verrou reste en place volontairement. Rouvrir une déclaration sert
        à corriger une référence de dépôt ou un montant saisi ; cela ne veut
        pas dire que la période redevient modifiable. Si c'est vraiment le
        cas, le verrou se recule à la main, délibérément.
        """
        for declaration in self:
            if declaration.state == "paid":
                raise UserError(_(
                    "A paid return cannot be reopened. Record the correction "
                    "as a new return instead, which is what the tax authority "
                    "expects to see."))
            declaration.state = "draft"
        return True

    # ------------------------------------------------------------------
    # Ouverture de l'état
    # ------------------------------------------------------------------

    def action_open_report(self):
        """Ouvre l'état sous-jacent sur la période de la déclaration."""
        self.ensure_one()
        if not self.type_id.report_id:
            raise UserError(_(
                "No report is attached to this return type."))
        return {
            "type": "ir.actions.client",
            "tag": "expodo_account_report",
            "params": {
                "report_id": self.type_id.report_id.id,
                "date_from": str(self.date_from),
                "date_to": str(self.date_to),
            },
        }
