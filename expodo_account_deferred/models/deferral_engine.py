# -*- coding: utf-8 -*-
# Copyright 2026 Expodo (https://expodo.fr)
# License LGPL-3
"""Charges et produits constatés d'avance.

Le problème
-----------

Une prime d'assurance payée le 1er octobre pour douze mois est enregistrée en
charge à cette date. Or les neuf dixièmes de cette charge concernent l'exercice
suivant. La laisser telle quelle fausse deux résultats à la fois : elle alourdit
celui de l'exercice en cours et allège celui du suivant.

Le principe d'indépendance des exercices impose donc de sortir la part future du
compte de charge et de la porter au bilan — 486 en plan français — puis de l'y
reprendre à l'ouverture. Symétriquement pour les produits encaissés d'avance,
en 487.

Ce que fait ce module
---------------------

Il permet de dater une ligne d'écriture — « cette charge couvre du 1er octobre
au 30 septembre » — puis calcule, à une date de clôture donnée, la part qui
appartient au futur et produit l'écriture de report.

Méthode de calcul
-----------------

Le prorata se fait **au jour exact**, et non au mois entier.

Un prorata mensuel est plus simple et se défend dans certains référentiels,
mais il produit des écarts visibles sur les contrats qui ne commencent pas un
premier du mois — c'est-à-dire la plupart. Une prime commençant le 17 octobre
serait reportée comme si elle commençait le 1er, soit seize jours d'écart que
personne ne saurait expliquer un an plus tard.

Rien n'est écrit sans être équilibré, et rien n'est comptabilisé sans que
l'utilisateur ait relu : les écritures sont produites en brouillon.
"""

from odoo import _, models
from odoo.exceptions import UserError


class MoteurReport(models.AbstractModel):
    """Calcul et production des écritures de report."""

    _name = "expodo.deferral.engine"
    _description = "Deferred expense and revenue engine"

    MARQUEUR = "EXPODO-DEFERRAL"

    # ------------------------------------------------------------------
    # Calcul
    # ------------------------------------------------------------------

    @staticmethod
    def part_reportee(montant, debut, fin, date_cloture):
        """Part de `montant` appartenant aux exercices postérieurs à la clôture.

        Le calcul est linéaire au jour exact :

            part = montant × (jours après clôture) ÷ (jours totaux)

        Les deux bornes sont incluses : une prestation du 1er au 31 janvier
        couvre trente et un jours, pas trente. L'écart paraît négligeable sur
        un mois ; sur un contrat pluriannuel découpé en douze reports, il
        finit par se voir.

        Trois cas ne donnent rien à reporter :
        — la période est entièrement passée à la date de clôture ;
        — elle n'a pas encore commencé, et la charge n'a rien à y faire ;
        — les dates manquent.
        """
        if not debut or not fin or debut > fin:
            return 0.0
        if date_cloture >= fin:
            return 0.0
        if date_cloture < debut:
            # La période entière est postérieure : tout est à reporter.
            return montant

        jours_totaux = (fin - debut).days + 1
        jours_futurs = (fin - date_cloture).days
        if jours_totaux <= 0:
            return 0.0
        return montant * jours_futurs / jours_totaux

    # ------------------------------------------------------------------
    # Sélection
    # ------------------------------------------------------------------

    def _lignes_a_reporter(self, societe, date_cloture):
        """Lignes datées dont une part appartient au futur."""
        return self.env["account.move.line"].search([
            ("company_id", "=", societe.id),
            ("parent_state", "=", "posted"),
            ("deferred_start_date", "!=", False),
            ("deferred_end_date", ">", date_cloture),
            ("date", "<=", date_cloture),
            ("account_id.internal_group", "in", ("income", "expense")),
        ])

    def _compte_de_report(self, societe, ligne):
        """Compte de bilan recevant la part reportée.

        486 pour une charge, 487 pour un produit, en plan français. Ailleurs,
        on retient un compte d'actif ou de passif circulant, notion
        équivalente dans le modèle d'Odoo.

        Les codes français ne sont cherchés que pour une société dont le pays
        fiscal est la France. Ailleurs, « 486 » désigne autre chose — c'est
        l'erreur qui nous avait fait proposer un compte espagnol d'obligations
        propres pour une cession d'immobilisation.
        """
        comptes = self.env["account.account"]
        charge = ligne.account_id.internal_group == "expense"

        if societe.account_fiscal_country_id.code == "FR":
            code = "486" if charge else "487"
            trouve = comptes.search([
                ("company_ids", "in", societe.id),
                ("code", "=like", code + "%"),
            ], limit=1)
            if trouve:
                return trouve

        type_cible = "asset_current" if charge else "liability_current"
        trouve = comptes.search([
            ("company_ids", "in", societe.id),
            ("account_type", "=", type_cible),
        ], limit=1)
        if not trouve:
            raise UserError(_(
                "No balance sheet account is available to carry the deferred "
                "portion of %(line)s. Create an account typed “%(type)s”, or "
                "486/487 in the French chart.",
                line=ligne.display_name, type=type_cible))
        return trouve

    # ------------------------------------------------------------------
    # Production
    # ------------------------------------------------------------------

    def calculer(self, societe, date_cloture):
        """Détail de ce qui serait reporté, sans rien écrire.

        Retourne une liste de dictionnaires, un par ligne concernée. Séparer
        le calcul de l'écriture permet de montrer le détail avant de
        comptabiliser — sur une opération qui touche deux exercices, voir
        avant vaut mieux que corriger après.
        """
        details = []
        for ligne in self._lignes_a_reporter(societe, date_cloture):
            montant = self.part_reportee(
                ligne.balance, ligne.deferred_start_date,
                ligne.deferred_end_date, date_cloture)
            if societe.currency_id.is_zero(montant):
                continue
            details.append({
                "line": ligne,
                "account": ligne.account_id,
                "deferral_account": self._compte_de_report(societe, ligne),
                "amount": montant,
                "start": ligne.deferred_start_date,
                "end": ligne.deferred_end_date,
            })
        return details

    def ecriture_de_report(self, societe, date_cloture, journal):
        """Sort du compte de gestion la part appartenant au futur."""
        details = self.calculer(societe, date_cloture)
        if not details:
            return self.env["account.move"]

        lignes = []
        for detail in details:
            montant = detail["amount"]
            libelle = _(
                "Deferred %(start)s – %(end)s",
                start=detail["start"], end=detail["end"])
            # On retire du compte d'origine et on porte au bilan.
            lignes.append((0, 0, {
                "account_id": detail["account"].id,
                "partner_id": detail["line"].partner_id.id or False,
                "name": libelle,
                "debit": 0.0 if montant > 0 else -montant,
                "credit": montant if montant > 0 else 0.0,
            }))
            lignes.append((0, 0, {
                "account_id": detail["deferral_account"].id,
                "partner_id": detail["line"].partner_id.id or False,
                "name": libelle,
                "debit": montant if montant > 0 else 0.0,
                "credit": 0.0 if montant > 0 else -montant,
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
                "The deferral entry is out of balance by %(gap).2f. Nothing "
                "has been posted.", gap=ecart))

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
            # Une écriture en brouillon n'a pas encore de numéro : `name` y
            # vaut False. Assembler ces valeurs directement produisait une
            # erreur de type au moment précis où l'on venait de créer
            # l'écriture — c'est-à-dire systématiquement.
            noms = [m.name or m.ref or _("draft entry") for m in deja]
            releves.append(("deja_reporte", noms))

        # Une ligne datée sans date de fin ne peut pas être répartie ; elle
        # resterait silencieusement en charge de l'exercice.
        incompletes = self.env["account.move.line"].search_count([
            ("company_id", "=", societe.id),
            ("parent_state", "=", "posted"),
            ("deferred_start_date", "!=", False),
            ("deferred_end_date", "=", False),
        ])
        if incompletes:
            releves.append(("dates_incompletes", incompletes))

        return releves
