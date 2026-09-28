# -*- coding: utf-8 -*-
# Copyright 2026 Expodo (https://expodo.fr)
# License LGPL-3
"""Écarts de conversion sur les soldes en devises.

Le problème
-----------

Une créance de dix mille dollars enregistrée à 0,92 vaut 9 200 euros au bilan.
Si le dollar cote 0,95 à la clôture, la même créance en vaut 9 500. L'écart de
300 euros existe, il n'est simplement pas encore réalisé : le client n'a pas
payé.

Le laisser de côté revient à présenter un bilan à un cours périmé. Les
créances et dettes en devises doivent donc être converties au cours de clôture,
et la différence portée en écart de conversion.

Ce que Community sait déjà faire
--------------------------------

Les écarts **réalisés**, au moment du lettrage : la facture est encaissée, la
différence entre le cours d'origine et celui du règlement passe en gain ou en
perte de change. Les comptes sont réglés sur la société et le mécanisme est
complet.

Ce qui manque est la réévaluation **latente** des soldes encore ouverts à la
clôture. Elle est restée dans l'édition Enterprise.

Une particularité française
---------------------------

Le plan comptable général ne traite pas les deux sens symétriquement.

Une **perte latente** — créance qui se déprécie, dette qui s'alourdit — va au
476 et doit en outre faire l'objet d'une **provision pour risques** (1515). Un
**gain latent** va au 477 et n'est *pas* porté au résultat : le principe de
prudence interdit de constater un profit qui n'est pas réalisé.

Le module produit l'écriture de conversion. Il **signale** la provision sans la
créer : son montant relève d'une appréciation — couverture de change, position
globale, compensation entre devises — qui n'est pas mécanisable. Créer d'office
une provision au montant brut serait faux dans la plupart des cas.

Réversibilité
-------------

L'écriture est contre-passée à l'ouverture de l'exercice suivant. Un écart de
conversion n'est pas un gain ou une perte : c'est une photographie à une date.
La laisser courir ferait double emploi avec l'écart réalisé au moment du
règlement.
"""

from odoo import _, models
from odoo.exceptions import UserError


class MoteurReevaluation(models.AbstractModel):
    """Calcul et production des écarts de conversion."""

    _name = "expodo.revaluation.engine"
    _description = "Currency revaluation engine"

    MARQUEUR = "EXPODO-REVALUATION"

    # ------------------------------------------------------------------
    # Sélection
    # ------------------------------------------------------------------

    def _lignes_en_devise(self, societe, date_cloture):
        """Créances et dettes en devise, non soldées à la date de clôture.

        Seuls les comptes lettrables sont retenus : ce sont ceux dont le solde
        représente une somme à recevoir ou à payer, donc exposée au change.
        Une charge en devise est déjà définitivement convertie à sa date ;
        la réévaluer n'aurait pas de sens.
        """
        return self.env["account.move.line"].search([
            ("company_id", "=", societe.id),
            ("parent_state", "=", "posted"),
            ("date", "<=", date_cloture),
            ("currency_id", "!=", societe.currency_id.id),
            ("account_id.reconcile", "=", True),
            ("full_reconcile_id", "=", False),
            ("amount_residual_currency", "!=", 0.0),
        ])

    # ------------------------------------------------------------------
    # Calcul
    # ------------------------------------------------------------------

    def calculer(self, societe, date_cloture):
        """Écart par compte et par devise, sans rien écrire.

        L'écart d'une ligne vaut :

            valeur au cours de clôture − valeur inscrite aux livres

        Un écart positif sur une créance est un gain latent ; sur une dette,
        une perte. Le signe du solde s'en charge : une dette a un solde
        créditeur, donc négatif, et l'écart calculé de la même façon porte
        naturellement le bon sens.
        """
        details = {}
        for ligne in self._lignes_en_devise(societe, date_cloture):
            devise = ligne.currency_id
            valeur_cloture = devise._convert(
                ligne.amount_residual_currency, societe.currency_id,
                societe, date_cloture)
            ecart = valeur_cloture - ligne.amount_residual
            if societe.currency_id.is_zero(ecart):
                continue
            cle = (ligne.account_id, devise)
            if cle not in details:
                details[cle] = {
                    "account": ligne.account_id,
                    "currency": devise,
                    "amount_currency": 0.0,
                    "booked": 0.0,
                    "at_closing": 0.0,
                    "gap": 0.0,
                    "lines": self.env["account.move.line"],
                }
            entree = details[cle]
            entree["amount_currency"] += ligne.amount_residual_currency
            entree["booked"] += ligne.amount_residual
            entree["at_closing"] += valeur_cloture
            entree["gap"] += ecart
            entree["lines"] |= ligne
        return list(details.values())

    # ------------------------------------------------------------------
    # Comptes de destination
    # ------------------------------------------------------------------

    def _compte_ecart(self, societe, perte):
        """Compte d'écart de conversion.

        En plan français, 476 pour une perte latente et 477 pour un gain. Les
        codes ne sont cherchés que pour une société dont le pays fiscal est la
        France : ailleurs, ces numéros désignent autre chose.

        À défaut, on retombe sur les comptes de change déjà réglés sur la
        société pour les écarts réalisés. Ce n'est pas la présentation
        française, mais c'est comptablement tenable et cela évite de bloquer
        l'utilisateur.
        """
        comptes = self.env["account.account"]
        if societe.account_fiscal_country_id.code == "FR":
            code = "476" if perte else "477"
            trouve = comptes.search([
                ("company_ids", "in", societe.id),
                ("code", "=like", code + "%"),
            ], limit=1)
            if trouve:
                return trouve

        repli = (societe.expense_currency_exchange_account_id if perte
                 else societe.income_currency_exchange_account_id)
        if not repli:
            raise UserError(_(
                "No account is available for unrealised exchange differences. "
                "Set the exchange gain and loss accounts on the company, or "
                "create 476/477 accounts in the French chart."))
        return repli

    # ------------------------------------------------------------------
    # Écriture
    # ------------------------------------------------------------------

    def ecriture_de_conversion(self, societe, date_cloture, journal):
        """Porte les écarts latents au bilan, en brouillon."""
        details = self.calculer(societe, date_cloture)
        if not details:
            return self.env["account.move"]

        lignes = []
        for detail in details:
            ecart = detail["gap"]
            libelle = _(
                "Revaluation %(currency)s at %(date)s",
                currency=detail["currency"].name, date=date_cloture)
            compte_ecart = self._compte_ecart(societe, perte=ecart < 0)

            lignes.append((0, 0, {
                "account_id": detail["account"].id,
                "name": libelle,
                "debit": ecart if ecart > 0 else 0.0,
                "credit": -ecart if ecart < 0 else 0.0,
            }))
            lignes.append((0, 0, {
                "account_id": compte_ecart.id,
                "name": libelle,
                "debit": -ecart if ecart < 0 else 0.0,
                "credit": ecart if ecart > 0 else 0.0,
            }))

        ecriture = self.env["account.move"].create({
            "company_id": societe.id,
            "journal_id": journal.id,
            "date": date_cloture,
            "ref": "%s %s" % (self.MARQUEUR, date_cloture),
            "line_ids": lignes,
        })
        self._verifier_equilibre(ecriture)
        return ecriture

    def _verifier_equilibre(self, ecriture):
        if not ecriture:
            return
        ecart = (sum(ecriture.line_ids.mapped("debit"))
                 - sum(ecriture.line_ids.mapped("credit")))
        if abs(ecart) > 0.01:
            raise UserError(_(
                "The revaluation entry is out of balance by %(gap).2f. "
                "Nothing has been posted.", gap=ecart))

    # ------------------------------------------------------------------
    # Contrôles
    # ------------------------------------------------------------------

    def controler(self, societe, date_cloture):
        releves = []

        deja = self.env["account.move"].search([
            ("company_id", "=", societe.id),
            ("ref", "like", self.MARQUEUR),
            ("date", "=", date_cloture),
        ])
        if deja:
            noms = [m.name or m.ref or _("draft entry") for m in deja]
            releves.append(("deja_reevalue", noms))

        details = self.calculer(societe, date_cloture)
        pertes = sum(d["gap"] for d in details if d["gap"] < 0)
        if pertes:
            releves.append(("provision", -pertes))

        # Un taux de change absent à la date de clôture fait retomber la
        # conversion sur le dernier taux connu, sans rien signaler. Le
        # résultat paraît juste et repose sur un cours périmé.
        #
        # Odoo 20 : la conversion retient le dernier cours daté strictement
        # avant la date demandée (v19 : au plus tard ce jour-là). Le contrôle
        # porte sur le cours réellement utilisé, d'où l'inégalité stricte.
        devises = {d["currency"] for d in details}
        sans_taux = []
        for devise in devises:
            taux = self.env["res.currency.rate"].search([
                ("currency_id", "=", devise.id),
                ("company_id", "in", (societe.id, False)),
                ("name", "<", date_cloture),
            ], order="name desc", limit=1)
            if not taux or (date_cloture - taux.name).days > 31:
                sans_taux.append(devise.name)
        if sans_taux:
            releves.append(("taux_perime", sans_taux))

        return releves
