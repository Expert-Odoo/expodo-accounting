# -*- coding: utf-8 -*-
# Copyright 2026 Expodo (https://expodo.fr)
# License LGPL-3
"""Assistant de constatation des charges et produits d'avance."""

from odoo import _, api, fields, models
from odoo.exceptions import UserError


class AssistantReport(models.TransientModel):
    _name = "expodo.deferral.wizard"
    _description = "Post deferred expenses and revenues"

    company_id = fields.Many2one(
        "res.company", string="Company", required=True,
        default=lambda self: self.env.company)
    date_to = fields.Date(
        string="Closing date", required=True,
        default=lambda self: self._defaut_cloture(),
        help="Everything covering a period after this date is carried to the "
             "balance sheet.")
    journal_id = fields.Many2one(
        "account.journal", string="Journal", required=True,
        domain="[('type', '=', 'general'), ('company_id', '=', company_id)]")

    move_id = fields.Many2one("account.move", string="Deferral entry", readonly=True)
    line_count = fields.Integer(string="Lines concerned", readonly=True)
    total_deferred = fields.Monetary(
        string="Total deferred", readonly=True, currency_field="currency_id")
    currency_id = fields.Many2one(related="company_id.currency_id", readonly=True)
    preview = fields.Text(string="Detail", readonly=True)
    warning = fields.Text(string="Before you post", readonly=True)

    @api.model
    def _defaut_cloture(self):
        societe = self.env.company
        return societe.compute_fiscalyear_dates(
            fields.Date.context_today(self))["date_to"]

    @api.model
    def default_get(self, champs):
        valeurs = super().default_get(champs)
        journal = self.env["account.journal"].search([
            ("type", "=", "general"),
            ("company_id", "=", self.env.company.id),
        ], limit=1)
        if journal:
            valeurs.setdefault("journal_id", journal.id)
        return valeurs

    # ------------------------------------------------------------------
    # Aperçu
    # ------------------------------------------------------------------

    def action_preview(self):
        """Montre le détail sans rien écrire.

        Un report touche deux exercices à la fois. Le voir avant vaut mieux
        que le corriger après : la correction suppose de contre-passer dans
        l'un et dans l'autre.
        """
        self.ensure_one()
        moteur = self.env["expodo.deferral.engine"]
        details = moteur.calculer(self.company_id, self.date_to)

        lignes = []
        for detail in details:
            lignes.append("%s  %s → %s  %s %.2f" % (
                detail["account"].code,
                detail["start"], detail["end"],
                self.currency_id.symbol or "",
                detail["amount"]))

        self.write({
            "line_count": len(details),
            "total_deferred": sum(d["amount"] for d in details),
            "preview": "\n".join(lignes) or _("Nothing to defer at this date."),
            "warning": "\n".join(self._messages()) or False,
        })
        return self._revenir()

    def _messages(self):
        self.ensure_one()
        messages = []
        for nature, detail in self.env["expodo.deferral.engine"].controler(
                self.company_id, self.date_to):
            if nature == "deja_reporte":
                messages.append(_(
                    "A deferral entry already exists at this date (%(names)s). "
                    "Posting a second one would defer the same amounts twice.",
                    names=", ".join(detail)))
            elif nature == "dates_incompletes":
                messages.append(_(
                    "%(count)s lines carry a start date but no end date. They "
                    "cannot be spread and stay entirely in this year's result.",
                    count=detail))
        return messages

    # ------------------------------------------------------------------
    # Comptabilisation
    # ------------------------------------------------------------------

    def action_post_deferral(self):
        self.ensure_one()
        moteur = self.env["expodo.deferral.engine"]

        deja = [n for n, _d in moteur.controler(self.company_id, self.date_to)
                if n == "deja_reporte"]
        if deja:
            raise UserError(_(
                "A deferral entry already exists at this date. Posting a "
                "second one would defer the same amounts twice. Reverse the "
                "existing entry first."))

        ecriture = moteur.ecriture_de_report(
            self.company_id, self.date_to, self.journal_id)
        if not ecriture:
            raise UserError(_(
                "No line covers a period after %(date)s: there is nothing to "
                "defer.", date=self.date_to))

        self.write({
            "move_id": ecriture.id,
            "line_count": len(ecriture.line_ids) // 2,
            "total_deferred": sum(ecriture.line_ids.mapped("debit")),
            "warning": "\n".join(self._messages()) or False,
        })
        return self._revenir()

    def _revenir(self):
        return {
            # Le titre est repris du menu. Sans lui, Odoo intitule la
            # fenêtre « Odoo » : l'utilisateur clique sur « Deferred Expenses and Revenues »,
            # la fenêtre s'ouvre avec ce titre, puis le perd dès la première
            # action et il ne sait plus dans quel assistant il se trouve.
            "name": _('Deferred Expenses and Revenues'),
            "type": "ir.actions.act_window",
            "res_model": self._name,
            "res_id": self.id,
            "view_mode": "form",
            "target": "new",
        }
