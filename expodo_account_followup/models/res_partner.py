# -*- coding: utf-8 -*-
# Copyright 2026 Expodo (https://expodo.fr)
# License LGPL-3
"""Suivi de relance par client.

Ce qui se calcule, et ce qui se décide
--------------------------------------

Le module calcule ce qui est factuel : combien est dû, depuis quand, quel
niveau de relance ce retard atteint. Il ne décide pas d'envoyer. L'envoi reste
un geste : un client au téléphone la veille, une facture en litige, un accord
d'étalement — rien de cela n'est dans la base, et une relance partie malgré un
accord verbal coûte plus que la facture qu'elle réclame.

D'où la forme : une liste de clients à relancer, triée par urgence, avec le
niveau atteint et de quoi voir le détail. L'envoi se fait par sélection.

Les lignes exclues
------------------

Odoo Community porte déjà un champ `no_followup` sur les lignes d'écriture,
hérité de versions plus anciennes et que plus rien n'utilise. Le module s'en
sert plutôt que d'en créer un autre : une facture marquée comme non relançable
l'est sans doute pour une bonne raison, et cette raison a été saisie une fois.
"""

from odoo import _, api, fields, models


class Partenaire(models.Model):
    _inherit = "res.partner"

    followup_amount_due = fields.Monetary(
        string="Overdue", compute="_compute_followup",
        currency_field="currency_id", search="_search_followup_amount")
    followup_oldest_due = fields.Date(
        string="Oldest due date", compute="_compute_followup")
    followup_days_overdue = fields.Integer(
        string="Days overdue", compute="_compute_followup")
    followup_level_id = fields.Many2one(
        "expodo.followup.level", string="Level reached",
        compute="_compute_followup")
    followup_last_date = fields.Date(
        string="Last chased on", readonly=True,
        help="Recorded when a reminder is actually sent. Chasing the same "
             "customer twice in a week is how a relationship gets damaged.")
    followup_note = fields.Text(
        string="Follow-up note",
        help="What was agreed with this customer. A payment plan or a dispute "
             "lives here, not in someone's memory.")

    def _lignes_echues(self):
        """Lignes clientes échues, non lettrées, non exclues."""
        self.ensure_one()
        aujourd_hui = fields.Date.context_today(self)
        return self.env["account.move.line"].search([
            ("partner_id", "=", self.id),
            ("company_id", "=", self.env.company.id),
            ("parent_state", "=", "posted"),
            ("account_id.account_type", "=", "asset_receivable"),
            ("full_reconcile_id", "=", False),
            ("no_followup", "=", False),
            # Un avoir ou un règlement non lettré vient en déduction, quelle
            # que soit son échéance : un règlement saisi sans échéance était
            # ignoré, et le client qui avait payé restait relancé du montant
            # de sa facture (constaté au port 20.0).
            "|", ("date_maturity", "<", aujourd_hui), ("amount_residual", "<", 0.0),
            ("amount_residual", "!=", 0.0),
        ])

    @api.depends_context("company")
    def _compute_followup(self):
        aujourd_hui = fields.Date.context_today(self)
        societe = self.env.company
        niveaux = self.env["expodo.followup.level"]

        for partenaire in self:
            lignes = partenaire._lignes_echues()
            echues = lignes.filtered(lambda l: l.amount_residual > 0)
            if not echues or sum(lignes.mapped("amount_residual")) <= 0:
                partenaire.followup_amount_due = 0.0
                partenaire.followup_oldest_due = False
                partenaire.followup_days_overdue = 0
                partenaire.followup_level_id = False
                continue

            partenaire.followup_amount_due = sum(lignes.mapped("amount_residual"))
            plus_ancienne = min(echues.mapped("date_maturity"))
            partenaire.followup_oldest_due = plus_ancienne
            retard = (aujourd_hui - plus_ancienne).days
            partenaire.followup_days_overdue = retard
            partenaire.followup_level_id = niveaux.niveau_pour_retard(
                societe, retard)

    def _search_followup_amount(self, operateur, valeur):
        """Rend le montant échu filtrable depuis la vue liste.

        Un champ calculé non stocké n'est pas cherchable par défaut. Sans
        cela, la liste des clients à relancer ne pourrait pas être filtrée, ce
        qui est précisément ce qu'on veut en faire.
        """
        aujourd_hui = fields.Date.context_today(self)
        lignes = self.env["account.move.line"].search([
            ("company_id", "=", self.env.company.id),
            ("parent_state", "=", "posted"),
            ("account_id.account_type", "=", "asset_receivable"),
            ("full_reconcile_id", "=", False),
            ("no_followup", "=", False),
            # Un avoir ou un règlement non lettré vient en déduction, quelle
            # que soit son échéance : un règlement saisi sans échéance était
            # ignoré, et le client qui avait payé restait relancé du montant
            # de sa facture (constaté au port 20.0).
            "|", ("date_maturity", "<", aujourd_hui), ("amount_residual", "<", 0.0),
            ("amount_residual", "!=", 0.0),
        ])
        totaux = {}
        for ligne in lignes:
            totaux.setdefault(ligne.partner_id.id, 0.0)
            totaux[ligne.partner_id.id] += ligne.amount_residual

        import operator
        comparaisons = {
            ">": operator.gt, ">=": operator.ge,
            "<": operator.lt, "<=": operator.le,
            "=": operator.eq, "!=": operator.ne,
        }
        test = comparaisons.get(operateur)
        if not test:
            return [("id", "in", list(totaux))]
        return [("id", "in", [
            pid for pid, total in totaux.items() if test(total, valeur)])]

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def action_open_overdue(self):
        """Les écritures derrière le montant échu."""
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": _("Overdue items — %(partner)s", partner=self.display_name),
            "res_model": "account.move.line",
            "view_mode": "list,form",
            "domain": [("id", "in", self._lignes_echues().ids)],
        }

    def action_send_followup(self):
        """Envoie la relance du niveau atteint et note la date.

        Les clients sans niveau atteint, sans courriel, ou dont le niveau
        n'envoie pas de courriel sont ignorés sans bruit : sélectionner large
        puis laisser le module trier est plus sûr que demander à l'utilisateur
        de trier lui-même.
        """
        envoyees = self.env["res.partner"]
        for partenaire in self:
            niveau = partenaire.followup_level_id
            if not niveau or not niveau.send_email or not partenaire.email:
                continue

            partenaire.message_post(
                body=niveau.body or _(
                    "Reminder: %(amount)s is outstanding.",
                    amount=partenaire.followup_amount_due),
                subject=niveau.subject or _("Outstanding balance"),
                partner_ids=partenaire.ids,
                message_type="email",
                subtype_xmlid="mail.mt_comment",
            )
            partenaire.followup_last_date = fields.Date.context_today(partenaire)
            envoyees |= partenaire

        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "type": "success" if envoyees else "warning",
                "message": _(
                    "%(sent)s reminders sent out of %(total)s selected. "
                    "Customers with no level reached, no email address, or a "
                    "level that does not send email were skipped.",
                    sent=len(envoyees), total=len(self)),
                "next": {"type": "ir.actions.act_window_close"},
            },
        }
