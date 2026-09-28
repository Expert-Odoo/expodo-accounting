# -*- coding: utf-8 -*-
# Copyright 2026 Expodo (https://expodo.fr)
# License LGPL-3
"""Conformité du fichier des écritures comptables.

Ces tests ne vérifient pas que le module « marche » : ils vérifient qu'il
produit un fichier que l'administration acceptera. La nuance est la raison
d'être du module.

Un FEC défaillant ne lève aucune erreur. Il se produit, se télécharge, se
remet — et c'est le vérificateur qui découvre le défaut, avec cinq mille euros
d'amende par exercice à la clé. Les contrôles ci-dessous sont donc écrits du
point de vue de celui qui relira le fichier, pas de celui qui l'écrit.
"""

from datetime import date

from odoo import Command
from odoo.tests import TransactionCase, tagged

from ..models.fec_generator import CHAMPS_FEC, SEPARATEUR


@tagged("post_install", "-at_install")
class TestConformiteFec(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # Ces contrôles relisent le fichier entier, ligne à ligne. Adossés à la
        # société principale, ils ne jugeaient que les quelques écritures créées
        # ici tant que la base restait vide ; sur une comptabilité réelle, ils se
        # mettaient à régénérer tout l'historique à chaque test. Soixante mille
        # lignes font vingt et un fichiers de huit mégaoctets dans une seule
        # transaction, et la mémoire cède avant la fin. Une société dédiée rend
        # le fichier indépendant de ce que la base contient déjà.
        principale = cls.env.company
        cls.societe = cls.env["res.company"].create({
            "name": "Société du contrôle fiscal",
            "country_id": principale.country_id.id or cls.env.ref("base.fr").id,
        })
        cls.env["account.chart.template"].try_loading(
            principale.chart_template or "generic_coa",
            company=cls.societe, install_demo=False)
        cls.env.user.company_ids = [Command.link(cls.societe.id)]
        cls.env = cls.env(context=dict(
            cls.env.context, allowed_company_ids=[cls.societe.id]))

        cls.journal = cls.env["account.journal"].search(
            [("type", "=", "general"), ("company_id", "=", cls.societe.id)], limit=1)
        comptes = cls.env["account.account"].with_company(cls.societe)
        cls.compte_client = comptes.search(
            [("account_type", "=", "asset_receivable"),
             ("company_ids", "in", cls.societe.id)], limit=1)
        cls.compte_vente = comptes.search(
            [("account_type", "=", "income"),
             ("company_ids", "in", cls.societe.id)], limit=1)
        cls.partenaire = cls.env["res.partner"].create({
            "name": "Client du contrôle fiscal",
            "ref": "C0001",
        })

        cls.ecriture = cls.env["account.move"].create({
            "journal_id": cls.journal.id,
            "date": date(2026, 3, 15),
            "ref": "PJ-2026-0042",
            "line_ids": [
                Command.create({
                    "name": "Vente de prestations",
                    "account_id": cls.compte_client.id,
                    "partner_id": cls.partenaire.id,
                    "debit": 1200.0,
                    "credit": 0.0,
                }),
                Command.create({
                    "name": "Vente de prestations",
                    "account_id": cls.compte_vente.id,
                    "debit": 0.0,
                    "credit": 1200.0,
                }),
            ],
        })
        cls.ecriture.action_post()

        cls.generateur = cls.env["expodo.fec.generator"]

    # ------------------------------------------------------------------
    # Utilitaires
    # ------------------------------------------------------------------

    def _fichier(self, debut=date(2026, 1, 1), fin=date(2026, 12, 31)):
        return self.generateur.generer(self.societe, debut, fin)

    def _lignes(self, contenu=None):
        contenu = contenu if contenu is not None else self._fichier()
        return [l for l in contenu.split("\r\n") if l]

    def _colonnes(self, ligne):
        return ligne.split(SEPARATEUR)

    def _colonne(self, ligne, nom):
        return self._colonnes(ligne)[CHAMPS_FEC.index(nom)]

    def _ligne_de_notre_ecriture(self):
        for ligne in self._lignes()[1:]:
            if self._colonne(ligne, "EcritureNum") == self.ecriture.name:
                if self._colonne(ligne, "CompteNum") == self.compte_client.code:
                    return ligne
        self.fail("L'écriture de test est absente du fichier")

    # ------------------------------------------------------------------
    # Structure du fichier
    # ------------------------------------------------------------------

    def test_l_en_tete_est_presente_et_exacte(self):
        """Un fichier à plat sans en-tête est rejeté, fût-il exact.

        C'est le premier des sept contrôles que passe un vérificateur, et le
        plus bête à manquer.
        """
        premiere = self._lignes()[0]
        self.assertEqual(
            self._colonnes(premiere), list(CHAMPS_FEC),
            "L'en-tête doit porter les dix-huit intitulés, dans l'ordre de "
            "l'arrêté du 29 juillet 2013")

    def test_chaque_ligne_porte_exactement_dix_huit_champs(self):
        """Ni dix-sept, ni dix-neuf.

        Un champ manquant décale tous les suivants : l'administration lit
        alors un montant là où elle attend une date, sans que rien ne le
        signale.
        """
        for numero, ligne in enumerate(self._lignes(), start=1):
            self.assertEqual(
                len(self._colonnes(ligne)), 18,
                "Ligne %d : %d champs au lieu de 18" % (
                    numero, len(self._colonnes(ligne))))

    def test_aucun_separateur_parasite_dans_les_libelles(self):
        """Une tabulation dans un libellé décalerait la ligne entière."""
        ecriture = self.env["account.move"].create({
            "journal_id": self.journal.id,
            "date": date(2026, 4, 1),
            "line_ids": [
                Command.create({
                    "name": "Libellé\tavec\ttabulations\net saut de ligne",
                    "account_id": self.compte_client.id,
                    "partner_id": self.partenaire.id,
                    "debit": 50.0, "credit": 0.0,
                }),
                Command.create({
                    "name": "Contrepartie",
                    "account_id": self.compte_vente.id,
                    "debit": 0.0, "credit": 50.0,
                }),
            ],
        })
        ecriture.action_post()
        for ligne in self._lignes():
            self.assertEqual(len(self._colonnes(ligne)), 18)

    # ------------------------------------------------------------------
    # Format des champs
    # ------------------------------------------------------------------

    def test_les_dates_sont_au_format_aaaammjj(self):
        """Une date ISO se lit parfaitement et fait échouer le contrôle."""
        ligne = self._ligne_de_notre_ecriture()
        for nom in ("EcritureDate", "PieceDate", "ValidDate"):
            valeur = self._colonne(ligne, nom)
            self.assertRegex(
                valeur, r"^\d{8}$",
                "%s doit valoir huit chiffres (AAAAMMJJ), pas %r" % (nom, valeur))
        self.assertEqual(self._colonne(ligne, "EcritureDate"), "20260315")

    def test_les_montants_emploient_la_virgule_decimale(self):
        ligne = self._ligne_de_notre_ecriture()
        self.assertEqual(self._colonne(ligne, "Debit"), "1200,00")
        self.assertEqual(self._colonne(ligne, "Credit"), "0,00")

    def test_un_montant_nul_s_ecrit_et_ne_reste_pas_vide(self):
        """Debit et Credit sont obligatoires : un vide n'est pas un zéro."""
        for ligne in self._lignes()[1:]:
            for nom in ("Debit", "Credit"):
                self.assertTrue(
                    self._colonne(ligne, nom),
                    "%s ne doit jamais être vide" % nom)

    # ------------------------------------------------------------------
    # Contenu comptable
    # ------------------------------------------------------------------

    def test_le_compte_auxiliaire_ne_sert_que_pour_les_comptes_de_tiers(self):
        """Porter un auxiliaire sur un compte de produit est une faute de nature.

        L'administration y lit une ventilation par tiers, qui n'a de sens que
        sur les comptes collectifs.
        """
        for ligne in self._lignes()[1:]:
            if self._colonne(ligne, "CompteNum") == self.compte_vente.code:
                self.assertEqual(
                    self._colonne(ligne, "CompAuxNum"), "",
                    "Un compte de produit ne porte pas de compte auxiliaire")

        ligne_client = self._ligne_de_notre_ecriture()
        self.assertEqual(self._colonne(ligne_client, "CompAuxNum"), "C0001")
        self.assertEqual(
            self._colonne(ligne_client, "CompAuxLib"), "Client du contrôle fiscal")

    def test_les_ecritures_en_brouillon_sont_exclues(self):
        """Une écriture en brouillon n'est pas une écriture comptable.

        L'inclure exposerait à une comptabilité jugée non définitive, ce qui
        est un grief plus lourd qu'un champ mal rempli.
        """
        brouillon = self.env["account.move"].create({
            "journal_id": self.journal.id,
            "date": date(2026, 5, 1),
            "ref": "BROUILLON-NE-DOIT-PAS-APPARAITRE",
            "line_ids": [
                Command.create({
                    "name": "Écriture non validée",
                    "account_id": self.compte_client.id,
                    "debit": 999.0, "credit": 0.0,
                }),
                Command.create({
                    "name": "Contrepartie",
                    "account_id": self.compte_vente.id,
                    "debit": 0.0, "credit": 999.0,
                }),
            ],
        })
        self.assertEqual(brouillon.state, "draft")
        self.assertNotIn("BROUILLON-NE-DOIT-PAS-APPARAITRE", self._fichier())

    def test_le_fichier_est_equilibre(self):
        """Somme des débits égale somme des crédits.

        C'est le premier recalcul que fait un outil d'analyse automatisée. Un
        FEC déséquilibré ouvre la discussion sur la fiabilité de l'ensemble de
        la comptabilité, pas seulement sur le fichier.
        """
        debits = credits = 0.0
        for ligne in self._lignes()[1:]:
            debits += float(self._colonne(ligne, "Debit").replace(",", "."))
            credits += float(self._colonne(ligne, "Credit").replace(",", "."))
        self.assertAlmostEqual(
            debits, credits, places=2,
            msg="Le fichier doit être équilibré : %.2f au débit contre %.2f "
                "au crédit" % (debits, credits))

    def test_les_lignes_d_une_meme_ecriture_restent_contigues(self):
        """Une écriture éclatée est conforme mais illisible.

        Un vérificateur gêné cherche plus longtemps, et cherche ailleurs.
        """
        vues = []
        derniere = None
        for ligne in self._lignes()[1:]:
            numero = self._colonne(ligne, "EcritureNum")
            if numero != derniere:
                self.assertNotIn(
                    numero, vues,
                    "L'écriture %s réapparaît après avoir été interrompue" % numero)
                vues.append(numero)
                derniere = numero

    def test_le_fichier_suit_l_ordre_chronologique(self):
        dates = [self._colonne(l, "EcritureDate") for l in self._lignes()[1:]]
        self.assertEqual(
            dates, sorted(dates),
            "Les écritures doivent se suivre dans l'ordre chronologique")

    # ------------------------------------------------------------------
    # Nom du fichier
    # ------------------------------------------------------------------

    def test_le_nom_suit_la_nomenclature_reglementaire(self):
        # Odoo 20 valide le SIRET (cle de Luhn) a l'ecriture : le numero
        # d'exemple doit etre reel dans sa forme.
        self.societe.partner_id.additional_identifiers = {
            "FR_SIRET": "73282932000074"}
        nom = self.generateur.nom_fichier(self.societe, date(2026, 12, 31))
        self.assertEqual(nom, "732829320FEC20261231.txt")

    def test_le_siren_est_extrait_d_un_numero_de_tva(self):
        """Un numéro de TVA français porte le SIREN en fin de chaîne."""
        self.societe.partner_id.additional_identifiers = {}
        self.societe.vat = "FR40123456824"
        nom = self.generateur.nom_fichier(self.societe, date(2026, 12, 31))
        self.assertTrue(
            nom.startswith("123456824"),
            "Le SIREN doit être extrait du numéro de TVA, obtenu : %s" % nom)

    def test_sans_identifiant_le_fichier_est_quand_meme_produit(self):
        """Un nom imparfait se corrige en une seconde ; un export refusé, non.

        L'assistant signale le cas plutôt que de bloquer : une entreprise qui
        doit remettre son FEC sous quinze jours a besoin du fichier.
        """
        self.societe.partner_id.additional_identifiers = {}
        self.societe.vat = False
        nom = self.generateur.nom_fichier(self.societe, date(2026, 12, 31))
        self.assertEqual(nom, "000000000FEC20261231.txt")

    # ------------------------------------------------------------------
    # Assistant
    # ------------------------------------------------------------------

    def test_l_assistant_produit_un_fichier_telechargeable(self):
        assistant = self.env["expodo.fec.export"].create({
            "company_id": self.societe.id,
            "date_from": date(2026, 1, 1),
            "date_to": date(2026, 12, 31),
        })
        assistant.action_generate()
        self.assertTrue(assistant.file_data, "Le fichier doit être produit")
        self.assertTrue(assistant.file_name.endswith(".txt"))
        contenu = assistant.file_data.content.decode("utf-8")
        self.assertEqual(
            contenu.split("\r\n")[0].split(SEPARATEUR), list(CHAMPS_FEC))
        self.assertGreater(assistant.line_count, 0)

    def test_l_assistant_signale_les_brouillons_sans_bloquer(self):
        self.env["account.move"].create({
            "journal_id": self.journal.id,
            "date": date(2026, 6, 1),
            "line_ids": [
                Command.create({
                    "name": "En attente", "account_id": self.compte_client.id,
                    "debit": 10.0, "credit": 0.0}),
                Command.create({
                    "name": "En attente", "account_id": self.compte_vente.id,
                    "debit": 0.0, "credit": 10.0}),
            ],
        })
        assistant = self.env["expodo.fec.export"].create({
            "company_id": self.societe.id,
            "date_from": date(2026, 1, 1),
            "date_to": date(2026, 12, 31),
        })
        assistant.action_generate()
        self.assertTrue(assistant.file_data, "Le fichier doit être produit malgré tout")
        self.assertTrue(assistant.warning, "Les brouillons doivent être signalés")

    def test_la_periode_par_defaut_suit_l_exercice_de_la_societe(self):
        """L'exercice ne commence pas le 1er janvier partout.

        Un FEC couvrant la mauvaise période est refusé, et l'erreur est
        invisible sur le fichier lui-même.
        """
        self.societe.write({"fiscalyear_last_month": "6", "fiscalyear_last_day": 30})
        assistant = self.env["expodo.fec.export"].create({})
        self.assertEqual(
            (assistant.date_from.month, assistant.date_from.day), (7, 1),
            "Un exercice clos le 30 juin doit s'ouvrir le 1er juillet")

    # ------------------------------------------------------------------
    # Ordre réglementaire et anomalies de contenu
    # ------------------------------------------------------------------

    def test_les_a_nouveaux_figurent_en_tete_du_fichier(self):
        """Le BOFIP l'impose, et c'est une anomalie fréquemment relevée.

        Le piège est qu'un tri purement chronologique paraît correct : les
        à-nouveaux portent la date du premier jour de l'exercice et se
        retrouvent souvent en tête par accident. Dès qu'une autre écriture
        porte la même date, l'ordre devient fortuit.
        """
        journal = self.env["account.journal"].search(
            [("type", "=", "general"), ("company_id", "=", self.societe.id)], limit=1)

        # Une écriture ordinaire datée du même jour que les à-nouveaux, créée
        # AVANT eux : un tri naïf la placerait en premier.
        ordinaire = self.env["account.move"].create({
            "journal_id": journal.id,
            "date": date(2026, 1, 1),
            "ref": "ECRITURE-ORDINAIRE-DU-1ER-JANVIER",
            "line_ids": [
                Command.create({
                    "name": "Opération courante",
                    "account_id": self.compte_client.id,
                    "partner_id": self.partenaire.id,
                    "debit": 300.0, "credit": 0.0}),
                Command.create({
                    "name": "Opération courante",
                    "account_id": self.compte_vente.id,
                    "debit": 0.0, "credit": 300.0}),
            ],
        })
        ordinaire.action_post()

        ouverture = self.env["account.move"].create({
            "journal_id": journal.id,
            "date": date(2026, 1, 1),
            "ref": "A-NOUVEAUX",
            "line_ids": [
                Command.create({
                    "name": "Report des soldes",
                    "account_id": self.compte_client.id,
                    "partner_id": self.partenaire.id,
                    "debit": 500.0, "credit": 0.0}),
                Command.create({
                    "name": "Report des soldes",
                    "account_id": self.compte_vente.id,
                    "debit": 0.0, "credit": 500.0}),
            ],
        })
        ouverture.action_post()
        self.societe.account_opening_move_id = ouverture

        lignes = self._lignes()[1:]
        premieres = [self._colonne(l, "EcritureNum") for l in lignes[:2]]
        self.assertTrue(
            all(n == ouverture.name for n in premieres),
            "Les à-nouveaux doivent ouvrir le fichier, obtenu : %s" % premieres)

    def test_les_comptes_de_classe_8_et_9_sont_signales(self):
        """Le BOFIP les exclut du FEC, mais les retirer déséquilibrerait le fichier.

        Odoo crée des comptes 999xxx en repli pour les écarts de change. Le
        module les signale et les conserve : un FEC déséquilibré ouvre la
        discussion sur la fiabilité de toute la comptabilité, ce qui est un
        grief plus lourd qu'une classe de compte inattendue.
        """
        compte9 = self.env["account.account"].search(
            [("code", "=like", "9%"), ("company_ids", "in", self.societe.id)], limit=1)
        if not compte9:
            compte9 = self.env["account.account"].create({
                "code": "999900", "name": "Compte hors champ",
                "account_type": "expense"})

        ecriture = self.env["account.move"].create({
            "journal_id": self.journal.id,
            "date": date(2026, 7, 1),
            "line_ids": [
                Command.create({
                    "name": "Écart de change", "account_id": compte9.id,
                    "debit": 25.0, "credit": 0.0}),
                Command.create({
                    "name": "Contrepartie", "account_id": self.compte_vente.id,
                    "debit": 0.0, "credit": 25.0}),
            ],
        })
        ecriture.action_post()

        releves = self.env["expodo.fec.generator"].anomalies(
            self.societe, date(2026, 1, 1), date(2026, 12, 31))
        natures = [n for n, _details, _nombre in releves]
        self.assertIn(
            "classes_8_9", natures,
            "Un compte de classe 9 portant des écritures doit être signalé")

        # Et la ligne reste dans le fichier.
        self.assertIn(compte9.code, self._fichier())

    def test_la_date_de_validation_n_est_jamais_anterieure_a_l_ecriture(self):
        """Une ValidDate antérieure suggère une écriture antidatée.

        C'est l'une des anomalies qui peuvent conduire au rejet de la
        comptabilité, bien au-delà de l'amende pour fichier non conforme.
        """
        for ligne in self._lignes()[1:]:
            ecriture = self._colonne(ligne, "EcritureDate")
            validation = self._colonne(ligne, "ValidDate")
            self.assertGreaterEqual(
                validation, ecriture,
                "ValidDate (%s) ne peut pas précéder EcritureDate (%s)"
                % (validation, ecriture))

    def test_l_absence_d_a_nouveaux_est_signalee(self):
        self.societe.account_opening_move_id = False
        releves = self.env["expodo.fec.generator"].anomalies(
            self.societe, date(2026, 1, 1), date(2026, 12, 31))
        self.assertIn(
            "sans_a_nouveaux", [n for n, _d, _c in releves],
            "L'absence d'écriture d'à-nouveaux doit être signalée")
