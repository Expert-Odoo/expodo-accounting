# -*- coding: utf-8 -*-
# Copyright 2026 Expodo (https://expodo.fr)
# License LGPL-3
"""Le cycle de base d'un comptable, de bout en bout.

Pourquoi ce fichier existe
--------------------------

Tous les autres tests partent d'écritures construites à la main : un débit, un
crédit, un compte choisi. C'est commode et c'est incomplet, parce que **aucune
donnée d'un client réel n'arrive par ce chemin**. Elle arrive par une facture,
qui porte une taxe, qui pose des grilles, puis par un règlement, qui lettre.

Entre les deux se trouvent tous les mécanismes qu'un état doit traverser
correctement : la répartition de taxe, les étiquettes de grille, le compte
d'attente d'encaissement, le lettrage. Un module peut satisfaire trois cents
tests sur des écritures brutes et se tromper à la première facture.

Ce que le cycle vérifie
-----------------------

À chaque étape, les états doivent se répondre entre eux :

    facture comptabilisée
        le résultat porte le hors-taxe
        le bilan porte la créance toutes taxes comprises
        la taxe collectée figure au passif
        la déclaration porte la base et la taxe dans leurs cases

    règlement encaissé
        la facture passe à « payée »
        la créance disparaît des états d'ancienneté
        le bilan reste équilibré

La dernière ligne compte autant que les autres : un état qui oublie de se
mettre à jour après un règlement est plus dangereux qu'un état faux dès le
départ, parce qu'il a été juste une fois.
"""

from datetime import date

from odoo import Command
from odoo.tests import TransactionCase, tagged


@tagged("post_install", "-at_install")
class TestCycleComptable(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.societe = cls.env.company
        cls.client_test = cls.env["res.partner"].create({"name": "Client du cycle"})
        cls.taxe = cls.env["account.tax"].search([
            ("type_tax_use", "=", "sale"),
            ("amount_type", "=", "percent"),
            ("amount", ">", 0),
            ("company_id", "=", cls.societe.id),
        ], limit=1)
        cls.compte_produit = cls.env["account.account"].search([
            ("account_type", "=", "income"),
            ("company_ids", "in", cls.societe.id),
        ], limit=1)

    # ------------------------------------------------------------------
    # Outils
    # ------------------------------------------------------------------

    def _facture(self, montant=10000.0, jour=date(2026, 3, 10)):
        """Une facture client, taxée, comptabilisée."""
        lignes = [Command.create({
            "name": "Prestation du cycle",
            "quantity": 1,
            "price_unit": montant,
            "account_id": self.compte_produit.id,
            "tax_ids": [Command.set(self.taxe.ids)] if self.taxe else False,
        })]
        facture = self.env["account.move"].create({
            "move_type": "out_invoice",
            "partner_id": self.client_test.id,
            "invoice_date": jour,
            "date": jour,
            "invoice_line_ids": lignes,
        })
        facture.action_post()
        return facture

    def _valeurs(self, nom_ou_xmlid, debut=date(2026, 1, 1), fin=date(2026, 12, 31),
                 colonne=0):
        """Valeurs d'un état, indexées par code de ligne.

        `colonne` désigne laquelle lire. Le défaut, la première, convient aux
        états à une seule colonne de montant. Une balance âgée en porte sept,
        et sa première est « non échu » : la lire donnerait zéro pour une
        créance échue, ce qui ressemble à un état vide alors qu'il est juste.
        """
        rapports = self.env["account.report"]
        rapport = rapports.search([("name", "=", nom_ou_xmlid)], limit=1)
        if not rapport:
            donnees = self.env["ir.model.data"]
            identifiant = donnees._xmlid_to_res_id(nom_ou_xmlid, raise_if_not_found=False)
            rapport = rapports.browse(identifiant) if identifiant else rapports
        if not rapport:
            return None
        # La période se passe **en entrée** de `_expodo_get_options`, jamais
        # en écrasant `options["date"]` après coup : le moteur lit la date
        # dans `options["column_groups"]`, qui est construit à ce moment-là.
        # Un remplacement ultérieur laisse les groupes sur la période par
        # défaut, et l'état répond alors sur une autre période que celle
        # demandée — sans rien signaler.
        options = rapport._expodo_get_options({
            "date": {"mode": "range", "filter": "custom",
                     "date_from": debut, "date_to": fin}})
        _d, lignes = rapport._expodo_export_rows(options, limite=100000)
        valeurs = {}
        for ligne in lignes:
            if not ligne.get("code"):
                continue
            colonnes = ligne.get("columns") or [{}]
            cellule = colonnes[colonne] if abs(colonne) < len(colonnes) else colonnes[0]
            brut = cellule.get("raw")
            valeurs[ligne["code"]] = (
                brut.get("main") if isinstance(brut, dict) else brut) or 0.0
        return valeurs

    # ------------------------------------------------------------------
    # Après comptabilisation
    # ------------------------------------------------------------------

    def test_la_facture_se_comptabilise_avec_sa_taxe(self):
        facture = self._facture()
        self.assertEqual(facture.state, "posted")
        self.assertAlmostEqual(facture.amount_untaxed, 10000.0, places=2)
        if self.taxe:
            self.assertGreater(
                facture.amount_tax, 0.0,
                "Une facture taxée doit porter un montant de taxe")

    def test_la_taxe_pose_ses_grilles(self):
        """Sans grilles, la déclaration reste vide alors que la facture existe.

        C'est le défaut le plus coûteux de la chaîne : tout paraît correct
        jusqu'au moment de déclarer.
        """
        if not self.taxe:
            self.skipTest("Aucune taxe de vente dans cette base")
        facture = self._facture()
        grilles = facture.line_ids.mapped("tax_tag_ids")
        self.assertTrue(
            grilles,
            "La facture doit porter des grilles de taxe, faute de quoi la "
            "déclaration ne la verra pas")

    def test_le_resultat_porte_le_hors_taxe(self):
        """Et non le toutes taxes comprises : la TVA n'est pas un produit."""
        avant = self._valeurs("Profit and Loss") or {}
        depart = avant.get("PL_NET", 0.0)
        self._facture()
        apres = self._valeurs("Profit and Loss") or {}
        self.assertAlmostEqual(
            apres.get("PL_NET", 0.0) - depart, 10000.0, places=2,
            msg="Le résultat doit augmenter du hors-taxe, pas du TTC")

    def test_le_bilan_porte_la_creance_toutes_taxes_comprises(self):
        """Le client doit le TTC, quel que soit le sort de la taxe."""
        facture = self._facture()
        creances = facture.line_ids.filtered(
            lambda l: l.account_id.account_type == "asset_receivable")
        self.assertAlmostEqual(
            sum(creances.mapped("debit")), facture.amount_total, places=2)

    def test_le_bilan_reste_equilibre_apres_facturation(self):
        self._facture()
        valeurs = self._valeurs("Balance Sheet") or {}
        if "BS_CHECK" not in valeurs:
            self.skipTest("Ligne de contrôle indisponible")
        self.assertAlmostEqual(
            valeurs["BS_CHECK"], 0.0, places=2,
            msg="Actif et passif doivent rester égaux après une facture")

    def test_la_declaration_porte_la_base_et_la_taxe(self):
        """Le contrôle qui compte vraiment pour un comptable français.

        Une facture qui n'atteint pas la déclaration se découvre le jour du
        dépôt, quand il est trop tard pour comprendre pourquoi.
        """
        if not self.taxe:
            self.skipTest("Aucune taxe de vente dans cette base")
        rapports = self.env["account.report"]
        identifiant = rapports.expodo_resolve_tax_report()
        declaration = rapports.browse(identifiant)

        options = declaration._expodo_get_options({})
        options["date"] = dict(
            options["date"], date_from=date(2026, 3, 1),
            date_to=date(2026, 3, 31), mode="range")
        _d, avant = declaration._expodo_export_rows(options, limite=100000)
        somme_avant = self._somme(avant)

        self._facture()

        _d, apres = declaration._expodo_export_rows(options, limite=100000)
        somme_apres = self._somme(apres)

        self.assertGreater(
            somme_apres, somme_avant,
            "La facture doit apparaître dans la déclaration de la période")

    @staticmethod
    def _somme(lignes):
        total = 0.0
        for ligne in lignes:
            for colonne in (ligne.get("columns") or []):
                brut = colonne.get("raw")
                valeur = brut.get("main") if isinstance(brut, dict) else brut
                if isinstance(valeur, (int, float)):
                    total += abs(valeur)
        return total

    # ------------------------------------------------------------------
    # Après règlement
    # ------------------------------------------------------------------

    def _encaisser(self, facture, jour=date(2026, 4, 15)):
        assistant = self.env["account.payment.register"].with_context(
            active_model="account.move", active_ids=facture.ids,
        ).create({"payment_date": jour})
        assistant.action_create_payments()
        facture.invalidate_recordset()
        return facture

    def test_le_reglement_solde_la_facture(self):
        facture = self._encaisser(self._facture())
        self.assertEqual(facture.payment_state, "paid")
        self.assertAlmostEqual(facture.amount_residual, 0.0, places=2)

    def test_le_reglement_lettre_la_ligne_client(self):
        facture = self._encaisser(self._facture())
        creances = facture.line_ids.filtered(
            lambda l: l.account_id.account_type == "asset_receivable")
        self.assertTrue(
            all(creances.mapped("reconciled")),
            "Le règlement doit lettrer la ligne client")

    def test_la_creance_disparait_des_etats_d_anciennete(self):
        """Un état qui garde une créance réglée fait relancer un bon payeur.

        La mesure porte sur l'**écart** avant et après, jamais sur le total :
        une base de test porte toujours d'autres écritures, et un test qui
        lit un total global mesure surtout le contenu de la base.

        Le défaut est d'autant plus sournois que l'état était juste avant le
        règlement : il a donc déjà gagné la confiance de son lecteur.
        """
        depart = (self._valeurs("Aged Receivable", colonne=-1) or {}).get("AGEDR", 0.0)
        facture = self._facture()
        avec = (self._valeurs("Aged Receivable", colonne=-1) or {}).get("AGEDR", 0.0)
        self.assertAlmostEqual(
            avec - depart, facture.amount_total, places=2,
            msg="La créance doit apparaître pour son montant toutes taxes")

        self._encaisser(facture)
        apres = (self._valeurs("Aged Receivable", colonne=-1) or {}).get("AGEDR", 0.0)
        self.assertAlmostEqual(
            apres, depart, places=2,
            msg="Une créance réglée doit quitter la balance âgée")

    def test_le_bilan_reste_equilibre_apres_reglement(self):
        self._encaisser(self._facture())
        valeurs = self._valeurs("Balance Sheet") or {}
        if "BS_CHECK" not in valeurs:
            self.skipTest("Ligne de contrôle indisponible")
        self.assertAlmostEqual(valeurs["BS_CHECK"], 0.0, places=2)

    def test_le_resultat_ne_bouge_pas_au_reglement(self):
        """Encaisser n'enrichit pas : le produit était acquis à la facture.

        Un module qui compterait le produit deux fois — à la facture puis à
        l'encaissement — doublerait le chiffre d'affaires sans déséquilibrer
        le bilan, puisque les deux écritures sont équilibrées chacune.
        """
        facture = self._facture()
        avant = (self._valeurs("Profit and Loss") or {}).get("PL_NET", 0.0)
        self._encaisser(facture)
        apres = (self._valeurs("Profit and Loss") or {}).get("PL_NET", 0.0)
        self.assertAlmostEqual(
            apres, avant, places=2,
            msg="Le résultat doit rester inchangé après un encaissement")
