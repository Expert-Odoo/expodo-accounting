# -*- coding: utf-8 -*-
# Copyright 2026 Expodo (https://expodo.fr)
# License LGPL-3
"""Clôture d'exercice et écritures d'à-nouveaux.

Deux opérations distinctes, souvent confondues
----------------------------------------------

**La clôture** solde les comptes de gestion — charges et produits — et porte
leur différence au compte de résultat de l'exercice. Après elle, les classes 6
et 7 sont à zéro et le bénéfice ou la perte figure au bilan.

**Les à-nouveaux** reprennent, au premier jour de l'exercice suivant, les
soldes des comptes de bilan. Sans eux, le nouvel exercice démarre sur une
comptabilité vide et le bilan d'ouverture ne correspond à rien.

Odoo ne fait ni l'une ni l'autre. Son bilan calcule le résultat de l'exercice
à la volée, ce qui suffit à l'affichage mais ne produit aucune écriture. Cela
convient à une lecture de gestion ; cela ne convient pas à une comptabilité
française, où la clôture est une opération comptable en bonne et due forme, et
où les à-nouveaux doivent figurer en tête du fichier des écritures comptables.

Principes retenus
-----------------

*Rien n'est écrit sans être équilibré.* Chaque écriture produite est vérifiée
avant d'être comptabilisée : une écriture de clôture déséquilibrée ferait plus
de dégâts qu'une clôture non faite.

*L'opération est réversible tant qu'elle n'est pas comptabilisée.* Les
écritures sont créées en brouillon et l'utilisateur les valide après lecture.
Une clôture est irréversible une fois comptabilisée ; la faire en un clic
serait un mauvais service.

*On ne clôture jamais deux fois.* Le moteur détecte ses propres écritures et
refuse de les doubler, parce qu'une double clôture divise le résultat par deux
sans rien signaler.
"""

from odoo import _, models
from odoo.exceptions import UserError


class MoteurCloture(models.AbstractModel):
    """Produit les écritures de clôture et de réouverture."""

    _name = "expodo.closing.engine"
    _description = "Year-end closing engine"

    #: Marqueur porté par la référence des écritures produites. Il sert à les
    #: reconnaître pour éviter une double clôture ; le libellé visible reste
    #: lisible par un humain.
    MARQUEUR_CLOTURE = "EXPODO-CLOSING"
    MARQUEUR_OUVERTURE = "EXPODO-OPENING"

    # ------------------------------------------------------------------
    # Lecture des soldes
    # ------------------------------------------------------------------

    def _soldes(self, societe, date_fin, groupes):
        """Solde **cumulé** par compte à la date donnée.

        Le cumul, et non le mouvement de la période. La distinction est la
        source du défaut le plus grave qu'ait connu ce moteur :

        — Pour les à-nouveaux, ne reprendre que les mouvements de l'exercice
          ferait disparaître du bilan d'ouverture tout ce qui le précède. Le
          capital souscrit il y a cinq ans, les immobilisations acquises
          avant, les amortissements accumulés : tout s'évanouirait, et le
          bilan resterait équilibré — simplement faux.

        — Pour la clôture, solder le cumul est aussi plus juste. Un compte de
          gestion démarre l'exercice à zéro *si* l'exercice précédent a été
          clôturé ; son cumul vaut alors le mouvement de l'année. Si le
          précédent ne l'a pas été, solder le cumul rattrape l'oubli au lieu
          de le perpétuer.

        Une seule requête, groupée par compte. Une boucle de `read_group` par
        compte produirait autant d'allers-retours que le plan comporte de
        comptes mouvementés — plusieurs centaines sur une base réelle.
        """
        lignes = self.env["account.move.line"]._read_group(
            domain=[
                ("company_id", "=", societe.id),
                ("parent_state", "=", "posted"),
                ("date", "<=", date_fin),
                ("account_id.internal_group", "in", groupes),
            ],
            groupby=["account_id"],
            aggregates=["balance:sum"],
        )
        return {compte: solde for compte, solde in lignes if solde}

    def _groupes_gestion(self):
        """Comptes de charges et de produits, soldés à la clôture.

        On s'appuie sur `internal_group`, la classification qu'Odoo tient
        lui-même, plutôt que sur une liste de types.

        La première version énumérait les types un à un et oubliait
        `expense_other` — celui des charges exceptionnelles, dont les cessions
        d'immobilisation. Conséquence : la clôture laissait ces comptes non
        soldés, et l'écriture d'à-nouveaux se retrouvait déséquilibrée du
        montant exact de ces charges.

        Une liste écrite à la main se périme au premier type qu'Odoo ajoute.
        La classification d'Odoo, elle, suit ses propres évolutions.
        """
        return ["income", "expense"]

    def _groupes_bilan(self):
        """Comptes de bilan, repris en à-nouveaux.

        Les comptes hors bilan (`off`) sont volontairement exclus : ce sont
        des engagements, pas des soldes à reporter.
        """
        return ["asset", "liability", "equity"]

    # ------------------------------------------------------------------
    # Comptes de destination
    # ------------------------------------------------------------------

    def _compte_resultat(self, societe, benefice):
        """Compte recevant le résultat de l'exercice.

        En plan comptable français, 120000 pour un bénéfice et 129000 pour une
        perte. Ailleurs, on retient le compte typé `equity_unaffected`, qui est
        la notion équivalente dans le modèle d'Odoo.

        Le code français n'est cherché que pour une société dont le pays
        fiscal est la France : ailleurs, « 120 » désigne autre chose, et c'est
        exactement l'erreur qui nous avait fait proposer un compte espagnol
        d'obligations propres pour une cession d'immobilisation.
        """
        comptes = self.env["account.account"]
        if societe.account_fiscal_country_id.code == "FR":
            code = "120" if benefice else "129"
            trouve = comptes.search([
                ("company_ids", "in", societe.id),
                ("code", "=like", code + "%"),
            ], limit=1)
            if trouve:
                return trouve

        trouve = comptes.search([
            ("company_ids", "in", societe.id),
            ("account_type", "=", "equity_unaffected"),
        ], limit=1)
        if not trouve:
            raise UserError(_(
                "No account is typed “Unallocated Earnings” for %(company)s. "
                "The closing entry has nowhere to carry the result.",
                company=societe.display_name))
        return trouve

    # ------------------------------------------------------------------
    # Contrôles préalables
    # ------------------------------------------------------------------

    def _ecritures_existantes(self, societe, marqueur, date_debut, date_fin):
        return self.env["account.move"].search([
            ("company_id", "=", societe.id),
            ("ref", "like", marqueur),
            ("date", ">=", date_debut),
            ("date", "<=", date_fin),
        ])

    def controler(self, societe, date_debut, date_fin):
        """Points à signaler avant de produire quoi que ce soit."""
        releves = []

        deja = self._ecritures_existantes(
            societe, self.MARQUEUR_CLOTURE, date_debut, date_fin)
        if deja:
            releves.append(("deja_cloture", deja.mapped("name")))

        brouillons = self.env["account.move"].search_count([
            ("company_id", "=", societe.id),
            ("state", "=", "draft"),
            ("date", ">=", date_debut),
            ("date", "<=", date_fin),
        ])
        if brouillons:
            releves.append(("brouillons", brouillons))

        # Le compte de résultat non affecté sur un compte de classe 9 est le
        # repli d'Odoo lorsque le plan installé n'en désigne aucun. Il passe
        # inaperçu jusqu'au jour où il apparaît au bilan — ou dans le FEC,
        # dont les classes 8 et 9 sont exclues.
        non_affecte = self.env["account.account"].search([
            ("company_ids", "in", societe.id),
            ("account_type", "=", "equity_unaffected"),
        ], limit=1)
        if non_affecte and (non_affecte.code or "").startswith(("8", "9")):
            releves.append(("compte_repli", non_affecte.code))

        return releves

    # ------------------------------------------------------------------
    # Écriture de clôture
    # ------------------------------------------------------------------

    def ecriture_cloture(self, societe, date_debut, date_fin, journal):
        """Solde les comptes de gestion et porte le résultat au bilan.

        L'écriture est datée du dernier jour de l'exercice et créée en
        brouillon : l'utilisateur la relit avant de la comptabiliser.
        """
        soldes = self._soldes(societe, date_fin, self._groupes_gestion())
        if not soldes:
            return self.env["account.move"]

        lignes = []
        resultat = 0.0
        for compte, solde in soldes.items():
            # Solder un compte, c'est passer l'inverse de son solde.
            lignes.append((0, 0, {
                "account_id": compte.id,
                "name": _("Closing of the period"),
                "debit": -solde if solde < 0 else 0.0,
                "credit": solde if solde > 0 else 0.0,
            }))
            resultat -= solde

        # `resultat` est positif pour un bénéfice : les produits ont un solde
        # créditeur, donc négatif dans la convention signée d'Odoo.
        compte_resultat = self._compte_resultat(societe, benefice=resultat > 0)
        lignes.append((0, 0, {
            "account_id": compte_resultat.id,
            "name": _("Result for the period"),
            "debit": 0.0 if resultat > 0 else -resultat,
            "credit": resultat if resultat > 0 else 0.0,
        }))

        ecriture = self.env["account.move"].create({
            "company_id": societe.id,
            "journal_id": journal.id,
            "date": date_fin,
            "ref": "%s %s" % (self.MARQUEUR_CLOTURE, date_fin),
            "line_ids": lignes,
        })
        self._verifier_equilibre(ecriture)
        return ecriture

    # ------------------------------------------------------------------
    # Écriture d'à-nouveaux
    # ------------------------------------------------------------------

    def ecriture_a_nouveaux(self, societe, date_debut, date_fin, journal,
                            date_ouverture):
        """Reprend les soldes de bilan au premier jour du nouvel exercice.

        Les soldes sont lus **après** la clôture : le compte de résultat porte
        alors le bénéfice ou la perte, qui se reporte donc naturellement. Lire
        avant produirait un à-nouveau sans résultat, et un bilan d'ouverture
        déséquilibré du montant exact du résultat — un écart assez gros pour
        être vu, mais qu'on attribue souvent à une erreur de saisie.
        """
        soldes = self._soldes(societe, date_fin, self._groupes_bilan())
        soldes = {c: s for c, s in soldes.items() if s}
        if not soldes:
            return self.env["account.move"]

        lignes = []
        for compte, solde in soldes.items():
            lignes.append((0, 0, {
                "account_id": compte.id,
                "name": _("Opening balance"),
                "debit": solde if solde > 0 else 0.0,
                "credit": -solde if solde < 0 else 0.0,
            }))

        ecriture = self.env["account.move"].create({
            "company_id": societe.id,
            "journal_id": journal.id,
            "date": date_ouverture,
            "ref": "%s %s" % (self.MARQUEUR_OUVERTURE, date_ouverture),
            "line_ids": lignes,
        })
        self._verifier_equilibre(ecriture)
        return ecriture

    # ------------------------------------------------------------------
    # Garde-fou
    # ------------------------------------------------------------------

    def _verifier_equilibre(self, ecriture):
        """Refuse une écriture déséquilibrée plutôt que de la laisser passer.

        Odoo refuserait lui-même la comptabilisation, mais bien plus tard et
        avec un message qui ne dit pas d'où vient l'écart. Contrôler ici
        permet de nommer l'opération fautive.
        """
        if not ecriture:
            return
        ecart = sum(ecriture.line_ids.mapped("debit")) - sum(
            ecriture.line_ids.mapped("credit"))
        if abs(ecart) > 0.01:
            raise UserError(_(
                "The generated entry %(name)s is out of balance by "
                "%(gap).2f. Nothing has been posted.",
                name=ecriture.name or ecriture.ref, gap=ecart))
