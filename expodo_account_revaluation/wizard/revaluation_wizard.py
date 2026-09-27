# -*- coding: utf-8 -*-
# Copyright 2026 Expodo (https://expodo.fr)
# License LGPL-3
"""Assistant de réévaluation des soldes en devises."""

from dateutil.relativedelta import relativedelta

from odoo import _, api, fields, models
from odoo.exceptions import UserError


class AssistantReevaluation(models.TransientModel):
    _name = "expodo.revaluation.wizard"
    _description = "Revalue foreign currency balances"

    company_id = fields.Many2one(
        "res.company", string="Company", required=True,
        default=lambda self: self.env.company)
    date_to = fields.Date(
        string="Closing date", required=True,
        default=lambda self: self._defaut_cloture())
    journal_id = fields.Many2one(
        "account.journal", string="Journal", required=True,
        domain="[('type', '=', 'general'), ('company_id', '=', company_id)]")
    reverse_next_day = fields.Boolean(
        string="Reverse at the opening of the next period", default=True,
        help="An exchange difference is a snapshot at one date, not a gain or "
             "a loss. Leaving it standing would double-count with the realised "
             "difference recorded when the invoice is settled.")

    move_id = fields.Many2one("account.move", string="Revaluation entry", readonly=True)
    reversal_move_id = fields.Many2one("account.move", string="Reversal", readonly=True)
    currency_id = fields.Many2one(related="company_id.currency_id", readonly=True)
    total_gain = fields.Monetary(
        string="Unrealised gains", readonly=True, currency_field="currency_id")
    total_loss = fields.Monetary(
        string="Unrealised losses", readonly=True, currency_field="currency_id")
    preview = fields.Text(string="Detail", readonly=True)
    warning = fields.Text(string="Before you post", readonly=True)

    @api.model
    def _defaut_cloture(self):
        return self.env.company.compute_fiscalyear_dates(
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
        self.ensure_one()
        details = self.env["expodo.revaluation.engine"].calculer(
            self.company_id, self.date_to)

        lignes = []
        for detail in details:
            lignes.append("%s  %s  %s %.2f → %s %.2f  (%+.2f)" % (
                detail["account"].code, detail["currency"].name,
                detail["currency"].symbol or "", detail["amount_currency"],
                self.currency_id.symbol or "", detail["at_closing"],
                detail["gap"]))

        self.write({
            "total_gain": sum(d["gap"] for d in details if d["gap"] > 0),
            "total_loss": -sum(d["gap"] for d in details if d["gap"] < 0),
            "preview": "\n".join(lignes) or _(
                "No open foreign currency balance at this date."),
            "warning": "\n".join(self._messages()) or False,
        })
        return self._revenir()

    def _messages(self):
        self.ensure_one()
        messages = []
        for nature, detail in self.env["expodo.revaluation.engine"].controler(
                self.company_id, self.date_to):
            if nature == "deja_reevalue":
                messages.append(_(
                    "A revaluation already exists at this date (%(names)s). "
                    "Posting a second one would count the difference twice.",
                    names=", ".join(detail)))
            elif nature == "provision":
                messages.append(_(
                    "Unrealised losses of %(amount).2f. Under French GAAP "
                    "these call for a provision for risks (1515), which this "
                    "module does not create: the amount depends on hedging "
                    "and on the overall position, which cannot be derived "
                    "mechanically. Unrealised gains are not taken to profit.",
                    amount=detail))
            elif nature == "taux_perime":
                messages.append(_(
                    "No recent exchange rate for %(currencies)s. The "
                    "conversion falls back on the last known rate, which looks "
                    "correct and is stale.",
                    currencies=", ".join(detail)))
        return messages

    # ------------------------------------------------------------------
    # Comptabilisation
    # ------------------------------------------------------------------

    def action_revalue(self):
        self.ensure_one()
        moteur = self.env["expodo.revaluation.engine"]

        deja = [n for n, _d in moteur.controler(self.company_id, self.date_to)
                if n == "deja_reevalue"]
        if deja:
            raise UserError(_(
                "A revaluation already exists at this date. Posting a second "
                "one would count the difference twice."))

        ecriture = moteur.ecriture_de_conversion(
            self.company_id, self.date_to, self.journal_id)
        if not ecriture:
            raise UserError(_(
                "No open foreign currency balance at %(date)s: there is "
                "nothing to revalue.", date=self.date_to))

        contre_passation = self.env["account.move"]
        if self.reverse_next_day:
            # La contre-passation se crée en même temps que l'écriture, et non
            # « plus tard ». Une réévaluation laissée sans contrepartie ferait
            # double emploi avec l'écart réalisé au règlement — et personne ne
            # se souvient, en mars, qu'il fallait contre-passer en janvier.
            contre_passation = ecriture._reverse_moves([{
                "date": self.date_to + relativedelta(days=1),
                "ref": _("Reversal of %(ref)s", ref=ecriture.ref),
            }])

        self.write({
            "move_id": ecriture.id,
            "reversal_move_id": contre_passation.id if contre_passation else False,
            "total_gain": sum(ecriture.line_ids.mapped("debit")),
            "warning": "\n".join(self._messages()) or False,
        })
        return self._revenir()

    def _revenir(self):
        return {
            # Le titre est repris du menu. Sans lui, Odoo intitule la
            # fenêtre « Odoo » : l'utilisateur clique sur « Currency Revaluation »,
            # la fenêtre s'ouvre avec ce titre, puis le perd dès la première
            # action et il ne sait plus dans quel assistant il se trouve.
            "name": _('Currency Revaluation'),
            "type": "ir.actions.act_window",
            "res_model": self._name,
            "res_id": self.id,
            "view_mode": "form",
            "target": "new",
        }
