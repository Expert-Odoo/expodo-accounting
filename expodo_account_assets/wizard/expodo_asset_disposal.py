# -*- coding: utf-8 -*-
# Copyright 2026 Expodo (https://expodo.fr)
# License LGPL-3
"""Sortie d'actif : cession ou mise au rebut.

Périmètre retenu
================

Cet assistant ne produit **que l'écriture de sortie d'actif** : il solde le
compte d'immobilisation et le compte d'amortissement, et constate la valeur
comptable résiduelle en charge.

Il ne comptabilise **pas** le produit de la vente. En cas de cession, le prix
se facture normalement, avec sa TVA et son compte de produits de cession. La
plus ou moins-value apparaît alors d'elle-même au compte de résultat, par
différence entre le produit de cession et la valeur comptable sortie.

C'est la pratique comptable française, et c'est aussi le choix le plus sûr :
mêler la sortie d'actif et la facturation obligerait à gérer la TVA, le compte
client et les conditions de règlement dans un assistant, pour un résultat que
la facturation d'Odoo fait déjà correctement. Un module qui écrit dans la
comptabilité doit écrire le moins possible.

L'écriture produite
===================

    Débit   Amortissements cumulés      montant déjà amorti
    Débit   Valeur comptable cédée      valeur nette restante
    Crédit  Immobilisation              valeur brute d'acquisition

Elle est équilibrée par construction, la valeur nette étant définie comme la
différence entre la valeur brute et le cumul amorti.
"""

from odoo import api, fields, models
from odoo.exceptions import UserError


class ExpodoAssetDisposal(models.TransientModel):
    _name = "expodo.asset.disposal"
    _description = "Asset disposal"

    # Pas de `readonly` sur le modèle : le client appelle `onchange`, qui
    # n'alimente pas un champ déclaré en lecture seule à ce niveau. La valeur
    # arrivait donc vide et l'assistant s'ouvrait inutilisable. Le verrouillage
    # est posé dans la vue, où il ne gêne pas la valeur par défaut.
    asset_id = fields.Many2one(
        "expodo.asset", string="Asset", required=True,
        default=lambda self: self.env.context.get("active_id"),
    )
    company_id = fields.Many2one(related="asset_id.company_id")
    currency_id = fields.Many2one(related="asset_id.currency_id")

    date = fields.Date(
        string="Disposal date", required=True, default=fields.Date.context_today,
    )
    reason = fields.Selection(
        [("sale", "Sold"), ("scrap", "Scrapped")],
        string="Reason", default="sale", required=True,
    )
    def _defaut_compte_charge(self):
        """Compte de charge proposé pour la valeur nette cédée.

        Alimenté par un `default=` plutôt que par une clé `default_*` du
        contexte : cette dernière ne parvient pas au client lorsque
        l'assistant s'ouvre en fenêtre modale, et le champ requis restait
        vide — bloquant l'écran sans message.

        Ne propose que le compte 675, celui que le plan comptable français
        réserve à la valeur comptable des éléments d'actif cédés. Hors de
        France, aucune proposition n'est faite.

        Mesuré sur une base française neuve : le plan livré par Odoo ne
        comporte aucun compte 675, seulement 672000 et 678000. La proposition
        est donc vide sur une installation française ordinaire, et c'est
        assumé. Un repli sur le 678 « autres charges exceptionnelles » a été
        essayé puis retiré : il reste une imprécision de codification que le
        comptable devra reprendre, et un champ pré-rempli se relit moins
        qu'un champ vide.

        Ce que le vide ne doit pas coûter, c'est un écran bloqué sans
        message : voir plus bas pourquoi le champ n'est plus requis au niveau
        du modèle, et le contrôle explicite posé à la validation.

        Le repli sur « n'importe quel compte de charges exceptionnelles » a
        été retiré : sur un plan américain, il désignait « Cash Discount
        Loss », un compte d'escompte sémantiquement faux pour une cession.
        Un utilisateur presse accepte ce qu'on lui propose, et l'écriture
        part sur le mauvais compte sans que rien ne le signale.

        Laisser le champ vide coûte un clic ; proposer un compte faux coûte
        une écriture à reprendre. Le domaine de la vue guide le choix.
        """
        # Le préfixe 675 n'est pas propre à la France : dans le plan espagnol,
        # 675 désigne « Pérdidas por operaciones con obligaciones propias »,
        # les pertes sur opérations sur obligations propres — rien à voir
        # avec une cession. Sans le filtre de pays, la proposition était
        # aussi fausse qu'avant, simplement moins visible.
        if self.env.company.account_fiscal_country_id.code != "FR":
            return False
        compte = self.env["account.account"].search(
            [("code", "=like", "675%"),
             ("account_type", "in", ("expense", "expense_other"))], limit=1)
        return compte.id if compte else False

    # Requis dans la vue, pas dans le modèle.
    #
    # `required=True` pose une contrainte NOT NULL en base. Le plan français
    # livré par Odoo ne comportant aucun compte 675, la valeur par défaut est
    # vide, et une création sans valeur — ce que fait tout appel programmatique,
    # et ce que ferait un autre module appelant cet assistant — remontait
    # l'erreur brute de PostgreSQL au lieu d'un message lisible.
    account_loss_id = fields.Many2one(
        "account.account", string="Net book value account",
        default=lambda self: self._defaut_compte_charge(),
        domain="[('account_type', 'in', ('expense', 'expense_other'))]",
        # Même principe que pour le compte d'amortissement : on décrit la
        # nature du compte, le numéro français n'étant qu'un exemple.
        help="Account charged with the remaining net book value. An expense "
             "account, distinct from the ongoing depreciation charge "
             "(675 in the French chart).",
    )
    post_due_first = fields.Boolean(
        string="Post instalments due first", default=True,
        help="Post the instalments falling on or before the disposal date, so "
             "that depreciation is recognised up to the day the asset leaves "
             "the books.",
    )

    depreciated_value = fields.Monetary(
        related="asset_id.depreciated_value", string="Depreciated",
    )
    remaining_value = fields.Monetary(
        related="asset_id.remaining_value", string="Net book value",
    )

    # ------------------------------------------------------------------

    @api.model
    def default_get(self, champs):
        """Reprend l'immobilisation depuis laquelle l'assistant a été ouvert.

        Le bouton de la fiche transmet l'identifiant dans `active_id`. Sans
        cette reprise, l'assistant s'ouvre vide : ni bien, ni valeur nette, et
        le comptable ne peut rien en faire. Les tests appelaient `create()` en
        passant `asset_id` explicitement, ce qui masquait le défaut.

        Pas de retour anticipé lorsque le bien est déjà connu : il faisait
        dépendre le reste de la méthode du chemin d'appel. Selon que
        `asset_id` figurait ou non dans les champs demandés, on sortait avant
        ou après le bloc suivant, et le compte de charge proposé différait —
        vide par appel direct, rempli par le formulaire. Un défaut invisible
        tant qu'on ne compare pas les deux chemins.
        """
        valeurs = super().default_get(champs)
        if not valeurs.get("asset_id") and \
                self.env.context.get("active_model") == "expodo.asset":
            actif = self.env.context.get("active_id")
            if actif:
                valeurs["asset_id"] = actif
        return valeurs

    def action_confirm(self):
        self.ensure_one()
        asset = self.asset_id

        if asset.state not in ("running", "closed"):
            raise UserError(self.env._(
                "Only a running or fully depreciated asset can be disposed of."))

        if not self.account_loss_id:
            raise UserError(self.env._(
                "Choose the account that carries the remaining net book "
                "value. It is an expense account, separate from the ongoing "
                "depreciation charge."))

        if self.post_due_first:
            a_poster = asset.line_ids.filtered(
                lambda l: l.state == "draft" and l.date <= self.date)
            if a_poster:
                a_poster.action_post()

        restantes = asset.line_ids.filtered(lambda l: l.state == "draft")
        cumul = asset.depreciated_value
        brute = asset.acquisition_value
        nette = brute - cumul

        lignes = []
        libelle = self.env._("%(asset)s — disposal", asset=asset.name)
        if cumul:
            lignes.append((0, 0, {
                "name": libelle,
                "account_id": asset.account_depreciation_id.id,
                "debit": cumul, "credit": 0.0,
            }))
        if nette:
            lignes.append((0, 0, {
                "name": libelle,
                "account_id": self.account_loss_id.id,
                "debit": nette, "credit": 0.0,
            }))
        lignes.append((0, 0, {
            "name": libelle,
            "account_id": asset.account_asset_id.id,
            "debit": 0.0, "credit": brute,
        }))

        move = self.env["account.move"].create({
            "journal_id": asset.journal_id.id,
            "date": self.date,
            "ref": libelle,
            "company_id": asset.company_id.id,
            "line_ids": lignes,
        })
        move.action_post()

        # Les échéances postérieures à la sortie n'ont plus lieu d'être : le
        # bien n'est plus à l'actif. On les supprime plutôt que de les laisser
        # en attente, où quelqu'un finirait par les comptabiliser.
        restantes.unlink()
        asset.write({"state": "disposed"})
        asset.message_post(body=self.env._(
            "Asset disposed of on %(date)s (%(reason)s). Net book value "
            "%(value)s charged to %(account)s. Entry %(move)s. "
            "%(cancelled)d remaining instalment(s) cancelled.",
            date=self.date,
            reason=dict(self._fields["reason"].selection)[self.reason],
            value=nette, account=self.account_loss_id.display_name,
            move=move.name, cancelled=len(restantes)))

        if self.reason == "sale":
            asset.message_post(body=self.env._(
                "Reminder: the sale proceeds are not recorded here. Issue the "
                "customer invoice as usual — the gain or loss on disposal will "
                "then appear in the profit and loss statement, as the "
                "difference between the proceeds and the net book value just "
                "charged."))

        return {
            "type": "ir.actions.act_window",
            "name": self.env._("Disposal entry"),
            "res_model": "account.move",
            "res_id": move.id,
            "view_mode": "form",
            "views": [(False, "form")],
        }
