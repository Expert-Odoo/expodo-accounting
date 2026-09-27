# -*- coding: utf-8 -*-
# Copyright 2026 Expodo (https://expodo.fr)
# License LGPL-3
"""Production du Fichier des Écritures Comptables (FEC).

Cadre légal
-----------

Le I de l'article L. 47 A du Livre des procédures fiscales impose à toute
entreprise tenant une comptabilité informatisée de remettre, en cas de
vérification, un fichier des écritures comptables. L'arrêté du 29 juillet
2013, codifié à l'article A. 47 A-1 du même livre, en fixe le format : dix-huit
champs, dans un ordre imposé, en fichier à plat.

Le défaut de présentation d'un fichier conforme est sanctionné par l'article
1729 D du code général des impôts — cinq mille euros par exercice, avant les
conséquences d'une comptabilité écartée.

Pourquoi un module
------------------

Odoo Community ne produit pas de FEC : l'export est resté dans l'édition
Enterprise. Une entreprise française qui tient sa comptabilité sous Community
ne peut donc pas répondre à une demande de l'administration.

Principe de construction
------------------------

Le fichier n'est pas *calculé* : il est *transcrit*. Chaque ligne du FEC
correspond à une ligne d'écriture comptabilisée, et chaque champ à une donnée
déjà présente en base. Aucune règle de gestion n'est inventée ici.

Ce choix est délibéré. Un FEC est relu par des outils d'analyse automatisée qui
recalculent les soldes et les rapprochent des déclarations déposées. Tout écart
entre le fichier et la comptabilité qui l'a produit se retourne contre
l'entreprise. Le rôle de ce module est donc de transcrire fidèlement, jamais
d'interpréter.
"""

from odoo import models


#: Les dix-huit champs, dans l'ordre imposé par l'arrêté du 29 juillet 2013.
#: L'ordre n'est pas indicatif : une inversion rend le fichier non conforme.
CHAMPS_FEC = (
    "JournalCode",
    "JournalLib",
    "EcritureNum",
    "EcritureDate",
    "CompteNum",
    "CompteLib",
    "CompAuxNum",
    "CompAuxLib",
    "PieceRef",
    "PieceDate",
    "EcritureLib",
    "Debit",
    "Credit",
    "EcritureLet",
    "DateLet",
    "ValidDate",
    "Montantdevise",
    "Idevise",
)

#: Séparateur de champs. L'arrêté admet la tabulation ou le caractère `|`.
#: La tabulation est retenue : c'est la valeur par défaut des outils de
#: contrôle de la DGFiP, et elle évite d'avoir à échapper les libellés
#: contenant une barre verticale.
SEPARATEUR = "\t"


class GenerateurFec(models.AbstractModel):
    """Transcription des écritures d'un exercice au format réglementaire."""

    _name = "expodo.fec.generator"
    _description = "FEC file generator"

    # ------------------------------------------------------------------
    # Mise en forme
    # ------------------------------------------------------------------

    @staticmethod
    def _date_fec(valeur):
        """Date au format AAAAMMJJ, chaîne vide si absente.

        Le format est imposé. Une date ISO (AAAA-MM-JJ) fait échouer le
        contrôle de conformité, alors qu'elle se lit parfaitement à l'œil —
        c'est le genre d'écart qu'on ne voit qu'au moment du contrôle.
        """
        return valeur.strftime("%Y%m%d") if valeur else ""

    @staticmethod
    def _montant_fec(valeur):
        """Montant à deux décimales, virgule décimale.

        L'arrêté admet le point ou la virgule ; la virgule est la convention
        française et celle qu'attendent les outils de la DGFiP. Un montant nul
        s'écrit « 0,00 » et non une chaîne vide : le champ est obligatoire.
        """
        return ("%.2f" % (valeur or 0.0)).replace(".", ",")

    @staticmethod
    def _texte_fec(valeur):
        """Texte assaini pour un fichier à plat.

        Une tabulation ou un retour à la ligne dans un libellé décalerait
        toutes les colonnes de la ligne, et le fichier deviendrait illisible à
        partir de cette écriture — sans qu'aucune erreur ne soit levée à
        l'écriture. On les remplace donc par une espace.
        """
        if not valeur:
            return ""
        texte = str(valeur)
        for caractere in ("\t", "\r\n", "\r", "\n"):
            texte = texte.replace(caractere, " ")
        return texte.strip()

    # ------------------------------------------------------------------
    # Sélection des écritures
    # ------------------------------------------------------------------

    def _domaine_ecritures(self, societe, date_debut, date_fin):
        """Écritures à transcrire.

        Seules les écritures comptabilisées entrent dans le fichier : une
        écriture en brouillon n'est pas une écriture comptable, et l'inclure
        exposerait à une comptabilité jugée non définitive.
        """
        return [
            ("company_id", "=", societe.id),
            ("parent_state", "=", "posted"),
            ("date", ">=", date_debut),
            ("date", "<=", date_fin),
            ("display_type", "not in", ("line_section", "line_note")),
        ]

    def _ecritures(self, societe, date_debut, date_fin):
        """Écritures dans l'ordre attendu par l'administration.

        Le BOFIP (BOI-CF-IOR-60-40-20) impose un ordre précis : les écritures
        d'à-nouveaux figurent **en tête du fichier**, suivies de toutes les
        autres par ordre chronologique, sans rupture de séquence.

        Les à-nouveaux reportent les soldes de l'exercice précédent. Les
        placer au milieu du fichier parce qu'ils portent la date du premier
        jour de l'exercice est l'une des anomalies que l'administration relève
        le plus souvent — le fichier paraît pourtant trié.

        Au sein d'un même rang, le tri est chronologique puis par numéro
        d'écriture, afin que les lignes d'une même écriture restent contiguës.
        Une écriture éclatée est formellement conforme mais illisible, et un
        vérificateur gêné cherche plus longtemps.
        """
        lignes = self.env["account.move.line"].search(
            self._domaine_ecritures(societe, date_debut, date_fin),
            order="date asc, move_name asc, id asc",
        )
        ouverture = societe.account_opening_move_id
        if not ouverture:
            return lignes
        a_nouveaux = lignes.filtered(lambda l: l.move_id == ouverture)
        if not a_nouveaux:
            return lignes
        return a_nouveaux + (lignes - a_nouveaux)

    def anomalies(self, societe, date_debut, date_fin):
        """Points qui rendraient le fichier contestable, relevés sur les données.

        Ces contrôles ne portent pas sur la mise en forme — celle-ci est
        garantie par le code ci-dessus — mais sur le contenu comptable, que
        le module ne peut pas corriger seul.

        Les signaler est le seul comportement défendable. Les taire livrerait
        un fichier non conforme sans que personne ne le sache ; retirer les
        lignes fautives déséquilibrerait le fichier, et un FEC déséquilibré
        ouvre la discussion sur la fiabilité de toute la comptabilité, pas
        seulement sur un champ.
        """
        releves = []
        lignes = self._ecritures(societe, date_debut, date_fin)

        # Classes 8 et 9 : le BOFIP les exclut du fichier des écritures
        # comptables. Odoo crée pourtant des comptes 999xxx en repli pour les
        # écarts de change lorsque le plan installé n'en désigne aucun — ils
        # portent alors de vraies écritures.
        hors_champ = lignes.filtered(
            lambda l: (l.account_id.code or "").startswith(("8", "9")))
        if hors_champ:
            codes = sorted(set(hors_champ.mapped("account_id.code")))
            releves.append((
                "classes_8_9",
                codes,
                len(hors_champ),
            ))

        # Une date de validation antérieure à la date d'écriture, ou
        # antérieure à l'exercice, est relevée comme anomalie grave : elle
        # suggère une écriture antidatée.
        anterieures = lignes.filtered(lambda l: l.date < date_debut)
        if anterieures:
            releves.append(("hors_periode", [], len(anterieures)))

        if not societe.account_opening_move_id:
            releves.append(("sans_a_nouveaux", [], 0))

        return releves

    # ------------------------------------------------------------------
    # Transcription d'une ligne
    # ------------------------------------------------------------------

    def _ligne_fec(self, ligne):
        """Les dix-huit champs d'une ligne d'écriture, sous forme de liste."""
        ecriture = ligne.move_id
        compte = ligne.account_id
        partenaire = ligne.partner_id

        # Le compte auxiliaire n'est renseigné que pour les comptes de tiers.
        # Le porter sur un compte de charge ou de produit serait une erreur de
        # nature : l'administration y lit une ventilation par tiers qui n'a de
        # sens que sur les comptes collectifs.
        auxiliaire = compte.account_type in ("asset_receivable", "liability_payable")
        compte_aux_num = self._texte_fec(partenaire.ref or partenaire.id) if (auxiliaire and partenaire) else ""
        compte_aux_lib = self._texte_fec(partenaire.name) if (auxiliaire and partenaire) else ""

        # Date de pièce : celle du document justificatif lorsqu'elle existe,
        # sinon la date de comptabilisation. Une facture saisie en retard porte
        # deux dates différentes, et c'est celle de la facture qui fait foi.
        date_piece = ecriture.invoice_date or ligne.date

        # ValidDate : date à laquelle l'écriture est devenue définitive.
        #
        # Odoo ne conserve aucun horodatage de validation : `write_date` bouge
        # à chaque modification ultérieure et `create_date` peut précéder la
        # date d'écriture. Ni l'un ni l'autre ne convient.
        #
        # On retient donc la date de comptabilisation. Ce choix est le seul
        # qui évite l'anomalie grave que l'administration recherche : une
        # ValidDate antérieure à l'écriture, ou antérieure au début de
        # l'exercice, suggère une écriture antidatée et peut conduire au rejet
        # de la comptabilité (BOI-CF-IOR-60-40-20).
        #
        # Conséquence pratique, signalée à l'utilisateur : le FEC doit être
        # produit au moment de l'arrêté fiscal, pas des mois plus tard. C'est
        # la seule façon d'avoir des dates de validation cohérentes avec la
        # liasse déposée.
        date_validation = ligne.date

        # Lettrage : Odoo expose `matching_number`, qui vaut quelque chose comme
        # « A00042 » pour les lignes lettrées et commence par « P » pour les
        # rapprochements partiels. Les partiels ne sont pas un lettrage au sens
        # comptable et sont donc écartés.
        lettrage = ligne.matching_number or ""
        if lettrage.startswith("P"):
            lettrage = ""
        date_lettrage = ""
        if lettrage and ligne.full_reconcile_id:
            dates = ligne.full_reconcile_id.reconciled_line_ids.mapped("date")
            if dates:
                date_lettrage = self._date_fec(max(dates))

        # Montant en devise : renseigné uniquement si l'écriture est
        # effectivement libellée dans une autre devise que celle de la société.
        montant_devise = ""
        identifiant_devise = ""
        if ligne.currency_id and ligne.currency_id != ligne.company_currency_id:
            montant_devise = self._montant_fec(ligne.amount_currency)
            identifiant_devise = self._texte_fec(ligne.currency_id.name)

        return [
            self._texte_fec(ligne.journal_id.code),
            self._texte_fec(ligne.journal_id.name),
            self._texte_fec(ecriture.name),
            self._date_fec(ligne.date),
            self._texte_fec(compte.code),
            self._texte_fec(compte.name),
            compte_aux_num,
            compte_aux_lib,
            self._texte_fec(ecriture.ref or ecriture.name),
            self._date_fec(date_piece),
            self._texte_fec(ligne.name or ecriture.ref or ecriture.name),
            self._montant_fec(ligne.debit),
            self._montant_fec(ligne.credit),
            self._texte_fec(lettrage),
            date_lettrage,
            self._date_fec(date_validation),
            montant_devise,
            identifiant_devise,
        ]

    # ------------------------------------------------------------------
    # Production du fichier
    # ------------------------------------------------------------------

    def generer(self, societe, date_debut, date_fin):
        """Contenu complet du fichier, en texte.

        La première ligne porte les intitulés des colonnes. Elle est
        obligatoire : un fichier à plat sans en-tête est rejeté, alors même
        que les données qu'il contient seraient exactes.
        """
        lignes = [SEPARATEUR.join(CHAMPS_FEC)]
        for ligne in self._ecritures(societe, date_debut, date_fin):
            lignes.append(SEPARATEUR.join(self._ligne_fec(ligne)))
        return "\r\n".join(lignes) + "\r\n"

    def nom_fichier(self, societe, date_fin):
        """Nom réglementaire : ``<SIREN>FEC<AAAAMMJJ>.txt``.

        Le SIREN est lu sur le numéro d'identification de la société. À
        défaut, on retient « 000000000 » plutôt que d'échouer : un fichier
        au nom imparfait reste exploitable et se renomme en une seconde,
        alors qu'un export refusé la veille d'une remise ne sert à rien.
        L'assistant signale le cas à l'utilisateur.
        """
        registre = (societe.company_registry or societe.vat or "").strip()
        chiffres = "".join(c for c in registre if c.isdigit())
        # Le SIRET comporte quatorze chiffres dont les neuf premiers forment
        # le SIREN ; un numéro de TVA français porte le SIREN en fin de chaîne.
        if len(chiffres) >= 14:
            siren = chiffres[:9]
        elif len(chiffres) >= 9:
            siren = chiffres[-9:]
        else:
            siren = "000000000"
        return "%sFEC%s.txt" % (siren, self._date_fec(date_fin))
