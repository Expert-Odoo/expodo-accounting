# -*- coding: utf-8 -*-
# Copyright 2026 Expodo (https://expodo.fr)
# License LGPL-3
"""Assistant de clôture d'exercice."""

from dateutil.relativedelta import relativedelta

from odoo import _, api, fields, models
from odoo.exceptions import UserError


class ClotureExercice(models.TransientModel):
    _name = "expodo.year.closing"
    _description = "Close a fiscal year"

    company_id = fields.Many2one(
        "res.company", string="Company", required=True,
        default=lambda self: self.env.company,
    )
    date_from = fields.Date(string="From", required=True)
    date_to = fields.Date(string="To", required=True)
    journal_id = fields.Many2one(
        "account.journal", string="Journal", required=True,
        domain="[('type', '=', 'general'), ('company_id', '=', company_id)]",
        help="Journal receiving the closing and opening entries. A dedicated "
             "journal keeps them apart from day-to-day operations, which "
             "makes both the audit trail and the FEC easier to read.",
    )
    create_opening = fields.Boolean(
        string="Also create the opening entry", default=True,
        help="Carries the balance sheet accounts forward to the first day of "
             "the next year. Required for the entries file (FEC), where "
             "opening entries must head the file.",
    )

    closing_move_id = fields.Many2one(
        "account.move", string="Closing entry", readonly=True)
    opening_move_id = fields.Many2one(
        "account.move", string="Opening entry", readonly=True)
    result = fields.Monetary(
        string="Result for the period", readonly=True,
        currency_field="currency_id")
    currency_id = fields.Many2one(
        related="company_id.currency_id", readonly=True)
    warning = fields.Text(string="Before you post", readonly=True)

    # ------------------------------------------------------------------
    # Valeurs par défaut
    # ------------------------------------------------------------------

    @api.model
    def default_get(self, champs):
        valeurs = super().default_get(champs)
        societe = self.env.company
        bornes = societe.compute_fiscalyear_dates(fields.Date.context_today(self))
        valeurs.setdefault("date_from", bornes["date_from"])
        valeurs.setdefault("date_to", bornes["date_to"])
        journal = self.env["account.journal"].search([
            ("type", "=", "general"), ("company_id", "=", societe.id),
        ], limit=1)
        if journal:
            valeurs.setdefault("journal_id", journal.id)
        return valeurs

    @api.onchange("company_id")
    def _onchange_company(self):
        """Les bornes suivent l'exercice de la société choisie.

        Une société d'un groupe peut clôturer au 30 juin quand une autre
        clôture au 31 décembre ; garder les dates de la précédente produirait
        une clôture sur la mauvaise période.
        """
        for assistant in self:
            if not assistant.company_id:
                continue
            bornes = assistant.company_id.compute_fiscalyear_dates(
                fields.Date.context_today(assistant))
            assistant.date_from = bornes["date_from"]
            assistant.date_to = bornes["date_to"]

    # ------------------------------------------------------------------
    # Contrôle préalable
    # ------------------------------------------------------------------

    def action_check(self):
        """Montre ce qui sera fait, sans rien écrire.

        Une clôture est irréversible une fois comptabilisée. Pouvoir la
        regarder avant vaut mieux qu'un bouton unique.
        """
        self.ensure_one()
        self.warning = "\n".join(self._messages()) or False
        return self._revenir()

    def _messages(self):
        self.ensure_one()
        moteur = self.env["expodo.closing.engine"]
        messages = []

        for nature, detail in moteur.controler(
                self.company_id, self.date_from, self.date_to):
            if nature == "deja_cloture":
                messages.append(_(
                    "This period already carries closing entries (%(names)s). "
                    "Closing twice halves the result without warning: delete "
                    "or reverse them first.",
                    names=", ".join(detail)))
            elif nature == "brouillons":
                messages.append(_(
                    "%(count)s draft entries fall in this period. They are "
                    "excluded from the closing; post them first if they "
                    "belong to the year.", count=detail))
            elif nature == "compte_repli":
                messages.append(_(
                    "Unallocated earnings are carried to account %(code)s, a "
                    "class 8 or 9 fallback that Odoo creates when the "
                    "installed chart names none. In the French chart this "
                    "should be 110000. Class 8 and 9 accounts are also "
                    "excluded from the FEC, so this will surface twice.",
                    code=detail))

        if not self.company_id.account_fiscal_country_id:
            messages.append(_(
                "No fiscal country is set on the company. The result account "
                "is chosen from the account type rather than from the local "
                "chart's numbering."))

        return messages

    # ------------------------------------------------------------------
    # Production
    # ------------------------------------------------------------------

    def action_close(self):
        """Produit les écritures, en brouillon."""
        self.ensure_one()

        if self.date_from > self.date_to:
            raise UserError(_("The start date must precede the end date."))

        moteur = self.env["expodo.closing.engine"]
        deja = [n for n, _d in moteur.controler(
            self.company_id, self.date_from, self.date_to) if n == "deja_cloture"]
        if deja:
            raise UserError(_(
                "This period already carries closing entries. Closing twice "
                "would halve the result. Reverse the existing entries first."))

        cloture = moteur.ecriture_cloture(
            self.company_id, self.date_from, self.date_to, self.journal_id)
        if not cloture:
            raise UserError(_(
                "No posted profit-and-loss entry in this period: there is "
                "nothing to close."))

        # Le résultat se lit sur la ligne portée au compte de résultat.
        ligne_resultat = cloture.line_ids.filtered(
            lambda l: l.account_id.account_type == "equity_unaffected"
            or (l.account_id.code or "").startswith(("120", "129")))
        resultat = -sum(ligne_resultat.mapped("balance")) if ligne_resultat else 0.0

        ouverture = self.env["account.move"]
        if self.create_opening:
            # Les à-nouveaux se lisent APRÈS la clôture : le résultat doit
            # être au bilan pour être reporté. On comptabilise donc la clôture
            # d'abord — c'est le seul moment où le moteur écrit sans que
            # l'utilisateur ait relu, et c'est assumé : un à-nouveau calculé
            # sur une clôture en brouillon serait faux.
            cloture.action_post()
            ouverture = moteur.ecriture_a_nouveaux(
                self.company_id, self.date_from, self.date_to,
                self.journal_id, self.date_to + relativedelta(days=1))

        self.write({
            "closing_move_id": cloture.id,
            "opening_move_id": ouverture.id if ouverture else False,
            "result": resultat,
            "warning": "\n".join(self._messages()) or False,
        })
        return self._revenir()

    def action_register_opening(self):
        """Déclare l'écriture d'à-nouveaux comme celle de la société.

        C'est ce rattachement que lit l'export FEC pour placer les à-nouveaux
        en tête de fichier, comme le BOFIP l'exige. Sans lui, l'écriture
        existe mais rien ne la distingue d'une opération diverse.
        """
        self.ensure_one()
        if not self.opening_move_id:
            raise UserError(_("No opening entry has been generated."))
        self.company_id.account_opening_move_id = self.opening_move_id
        self.company_id.account_opening_date = self.opening_move_id.date
        return self._revenir()

    def _revenir(self):
        return {
            # Le titre est repris du menu. Sans lui, Odoo intitule la
            # fenêtre « Odoo » : l'utilisateur clique sur « Year-End Closing »,
            # la fenêtre s'ouvre avec ce titre, puis le perd dès la première
            # action et il ne sait plus dans quel assistant il se trouve.
            "name": _('Year-End Closing'),
            "type": "ir.actions.act_window",
            "res_model": self._name,
            "res_id": self.id,
            "view_mode": "form",
            "target": "new",
        }
