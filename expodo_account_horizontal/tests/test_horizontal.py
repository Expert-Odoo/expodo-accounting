# -*- coding: utf-8 -*-
# Copyright 2026 Expodo (https://expodo.fr)
# License LGPL-3
"""Groupes horizontaux.

Le risque de ce module est la colonne qui ment. Une ventilation dont les
colonnes ne somment pas au total, ou dont une colonne affiche le total général
sous une étiquette qui promet un sous-ensemble, est pire que pas de ventilation
du tout : elle a l'air d'une analyse.
"""

from odoo.exceptions import ValidationError
from odoo.tests import TransactionCase, tagged


@tagged("post_install", "-at_install")
class TestGroupesHorizontaux(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.societe = cls.env.company
        donnees = cls.env["ir.model.data"]
        cls.rapport = cls.env["account.report"].browse(
            donnees._xmlid_to_res_id("expodo_account_reports.report_balance_sheet"))

        cls.journaux = cls.env["account.journal"].search(
            [("company_id", "=", cls.societe.id)], limit=3)

        cls.groupe = cls.env["expodo.report.horizontal.group"].create({
            "name": "Par journal",
            "company_id": cls.societe.id,
            "rule_ids": [
                (0, 0, {"name": cls.journaux[0].name, "field_name": "journal_id",
                        "value_ids_text": str(cls.journaux[0].id), "sequence": 1}),
                (0, 0, {"name": "Les autres", "field_name": "journal_id",
                        "value_ids_text": ",".join(
                            str(j.id) for j in cls.journaux[1:]), "sequence": 2}),
            ],
        })

    def _options(self, avec_groupe=True):
        precedentes = {"horizontal_group_id": self.groupe.id} if avec_groupe else {}
        return self.rapport._expodo_get_options(precedentes)

    # ------------------------------------------------------------------
    # Multiplication des colonnes
    # ------------------------------------------------------------------

    def test_la_ventilation_survit_a_l_aller_retour_vers_le_navigateur(self):
        """Le détail d'une colonne ventilée doit valoir cette colonne.

        Les options repartent au navigateur puis reviennent à chaque dépliage,
        chaque clic sur un montant et chaque export. La sérialisation réduisait
        chaque groupe de colonnes à sa seule période et jetait le domaine de la
        ventilation : la ligne mère montrait les trois journaux retenus, son
        détail les montrait tous, et rien ne signalait la contradiction.

        Le contrôle passe donc par `expodo_get_report_data`, qui rend les
        options sérialisées, et non par `_expodo_get_options`, qui les rend
        telles quelles.
        """
        donnees = self.rapport.expodo_get_report_data(
            {"horizontal_group_id": self.groupe.id})
        options = donnees["options"]
        cles = list(options["column_groups"])
        self.assertGreater(
            len(cles), 1, "La ventilation doit multiplier les colonnes")

        controles = 0
        for ligne in donnees["lines"]:
            if not ligne.get("unfoldable"):
                continue
            sous_lignes = self.rapport.expodo_expand_line(ligne["line_id"], options)
            for rang, colonne in enumerate(ligne["columns"]):
                for cle in cles:
                    total = colonne["raw"].get(cle)
                    if not isinstance(total, (int, float)):
                        continue
                    detail = sum(
                        (sous["columns"][rang]["raw"].get(cle) or 0.0)
                        for sous in sous_lignes
                        if isinstance(sous["columns"][rang]["raw"].get(cle), (int, float)))
                    controles += 1
                    self.assertAlmostEqual(
                        detail, total, places=2,
                        msg="Colonne %s de la ligne %s : la ligne annonce %s, "
                            "son détail %s" % (cle, ligne["name"], total, detail))
        self.assertGreater(
            controles, 0,
            "Le balayage doit rencontrer au moins une ligne dépliable")

    def test_sans_groupe_les_colonnes_sont_inchangees(self):
        """Le module ne doit rien changer tant qu'on ne l'emploie pas."""
        options = self._options(avec_groupe=False)
        self.assertEqual(list(options["column_groups"]), ["main"])

    def test_le_groupe_multiplie_les_jeux_de_colonnes(self):
        options = self._options()
        self.assertEqual(
            len(options["column_groups"]), 2,
            "Deux règles doivent produire deux jeux de colonnes")

    def test_chaque_jeu_porte_son_propre_filtre(self):
        options = self._options()
        filtres = [
            jeu.get("horizontal_domain") for jeu in options["column_groups"].values()]
        self.assertEqual(len(filtres), 2)
        self.assertNotEqual(
            filtres[0], filtres[1],
            "Deux colonnes portant le même filtre afficheraient deux fois la "
            "même chose sous deux étiquettes différentes")

    def test_le_libelle_porte_l_etiquette_de_la_colonne(self):
        options = self._options()
        libelles = [
            self.rapport._expodo_column_group_label(cle, jeu)
            for cle, jeu in options["column_groups"].items()]
        self.assertTrue(
            any(self.journaux[0].name in l for l in libelles),
            "L'étiquette de la règle doit apparaître dans l'en-tête")

    # ------------------------------------------------------------------
    # Le domaine
    # ------------------------------------------------------------------

    def test_le_filtre_s_ajoute_au_domaine_commun(self):
        """Une colonne ventilée reste soumise aux mêmes bornes.

        Remplacer le domaine au lieu d'y ajouter ferait qu'une colonne
        couvrirait une autre période, un autre état d'écritures, une autre
        société que le reste de la page.
        """
        options = self._options()
        cle = list(options["column_groups"])[0]
        domaine = self.rapport._expodo_base_domain(options, cle, "strict_range")
        champs = [c[0] for c in domaine if isinstance(c, (list, tuple))]
        self.assertIn("company_id", champs)
        self.assertIn("date", champs)
        self.assertIn("journal_id", champs)

    def test_une_regle_sans_identifiant_ne_laisse_rien_passer(self):
        """Sinon la colonne afficherait le total général sous une étiquette
        qui promet un sous-ensemble — le mensonge le plus difficile à voir."""
        regle = self.env["expodo.report.horizontal.group.rule"].create({
            "group_id": self.groupe.id,
            "name": "Règle vide",
            "field_name": "journal_id",
            "value_ids_text": "rien de numérique",
        })
        self.assertEqual(regle.domaine(), [("id", "=", False)])

    def test_une_regle_peut_couvrir_plusieurs_valeurs(self):
        """« Paris et Lyon » comme un seul établissement."""
        regle = self.groupe.rule_ids.filtered(lambda r: r.name == "Les autres")
        domaine = regle.domaine()
        self.assertEqual(domaine[0][0], "journal_id")
        self.assertEqual(domaine[0][1], "in")
        self.assertGreaterEqual(len(domaine[0][2]), 1)

    # ------------------------------------------------------------------
    # Contrôles
    # ------------------------------------------------------------------

    def test_un_groupe_sans_colonne_est_refuse(self):
        """Il masquerait simplement l'état."""
        with self.assertRaises(ValidationError):
            self.env["expodo.report.horizontal.group"].create({
                "name": "Groupe vide", "company_id": self.societe.id})

    def test_un_groupe_supprime_n_empeche_pas_l_etat_de_s_afficher(self):
        """Une option qui pointe sur un enregistrement disparu ne doit pas
        casser l'état : elle doit simplement ne plus s'appliquer."""
        options = self.rapport._expodo_get_options(
            {"horizontal_group_id": 999999999})
        self.assertFalse(options["horizontal_group_id"])
        self.assertEqual(list(options["column_groups"]), ["main"])
