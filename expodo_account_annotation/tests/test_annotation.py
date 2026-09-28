# -*- coding: utf-8 -*-
# Copyright 2026 Expodo (https://expodo.fr)
# License LGPL-3
"""Annotations sur les lignes d'états.

Le risque de ce module est l'annotation qui s'affiche au mauvais moment. Une
explication périmée présentée comme actuelle est pire qu'une absence
d'explication : le lecteur croit savoir.
"""

from datetime import date

from odoo.exceptions import ValidationError
from odoo.tests import TransactionCase, tagged


@tagged("post_install", "-at_install")
class TestAnnotations(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.societe = cls.env.company
        donnees = cls.env["ir.model.data"]
        cls.rapport = cls.env["account.report"].browse(
            donnees._xmlid_to_res_id("expodo_account_reports.report_balance_sheet"))
        cls.ligne = cls.rapport.line_ids[:1]
        cls.modele = cls.env["expodo.report.annotation"]

    def _annoter(self, texte="Explication", debut=date(2026, 1, 1),
                 fin=date(2026, 12, 31)):
        return self.modele.create({
            "report_line_id": self.ligne.id,
            "company_id": self.societe.id,
            "date_from": debut, "date_to": fin,
            "text": texte,
        })

    def _lignes(self, debut=date(2026, 1, 1), fin=date(2026, 12, 31)):
        options = self.rapport._expodo_get_options({})
        options["date"] = dict(
            options["date"], date_from=debut, date_to=fin, mode="range")
        _d, lignes = self.rapport._expodo_export_rows(options, limite=100000)
        return lignes

    def _annotations_de_la_ligne(self, lignes):
        for ligne in lignes:
            if ligne.get("line_id") == self.ligne.id:
                return ligne.get("annotations") or []
        return []

    # ------------------------------------------------------------------
    # Affichage
    # ------------------------------------------------------------------

    def test_une_note_arrive_dans_les_deux_exports(self):
        """Une note que personne ne peut lire n'est pas une note.

        Les annotations étaient bien attachées aux lignes exportées, et ni le
        classeur ni le PDF n'écrivaient la clé : on pouvait commenter un poste
        du bilan sans qu'aucun destinataire du fichier ne voie jamais le
        commentaire. Le module entier restait sans effet visible.
        """
        self._annoter("ZZ note de contrôle ZZ")
        options = self.rapport._expodo_get_options({})
        options["date"] = dict(
            options["date"], date_from=date(2026, 1, 1),
            date_to=date(2026, 12, 31), mode="range")

        import io
        import zipfile
        classeur = self.rapport._expodo_export_xlsx(options)
        archive = zipfile.ZipFile(io.BytesIO(classeur))
        self.assertTrue(
            any(b"ZZ note de contr" in archive.read(nom)
                for nom in archive.namelist()),
            "Le classeur doit porter la note")

        _donnees, lignes = self.rapport._expodo_export_rows(options, limite=100000)
        html = self.env["ir.qweb"]._render(
            "expodo_account_reports.report_pdf_document",
            {
                "header": self.rapport._expodo_export_header(options),
                "labels": {"label": "Label"},
                "column_groups": _donnees["column_groups"],
                "columns": _donnees["columns"],
                "lines": lignes,
                "sens_lecture": self.rapport._expodo_sens_lecture(),
            })
        texte = html if isinstance(html, str) else html.decode()
        self.assertIn(
            "ZZ note de contr", texte,
            "Le document PDF doit porter la note")

    def test_une_note_s_affiche_aussi_a_l_ecran(self):
        """La note s'imprimait dans le PDF et restait invisible dans l'état
        ouvert à l'écran, là où on la cherche d'abord."""
        self._annoter("Visible à l'écran")
        donnees = self.rapport.expodo_get_report_data({"date": {
            "date_from": "2026-01-01", "date_to": "2026-12-31",
            "filter": "custom", "mode": "range"}})
        self.assertEqual(self._annotations_de_la_ligne(donnees["lines"]),
                         ["Visible à l'écran"])

    def test_sans_note_le_classeur_ne_porte_pas_de_colonne_vide(self):
        """La colonne de notes n'apparaît que lorsqu'il y a des notes."""
        import io
        import zipfile
        options = self.rapport._expodo_get_options({})
        archive = zipfile.ZipFile(
            io.BytesIO(self.rapport._expodo_export_xlsx(options)))
        contenu = b"".join(archive.read(nom) for nom in archive.namelist())
        self.assertNotIn(
            b"Notes", contenu,
            "Sans annotation, le classeur ne doit pas porter de colonne de notes")

    def test_une_annotation_apparait_sur_sa_periode(self):
        self._annoter("Reprise de provision exceptionnelle")
        self.assertIn(
            "Reprise de provision exceptionnelle",
            self._annotations_de_la_ligne(self._lignes()))

    def test_une_annotation_d_un_autre_exercice_n_apparait_pas(self):
        """Une explication périmée présentée comme actuelle fait croire au
        lecteur qu'il sait."""
        self._annoter("Vrai en 2024 seulement",
                      debut=date(2024, 1, 1), fin=date(2024, 12, 31))
        self.assertNotIn(
            "Vrai en 2024 seulement",
            self._annotations_de_la_ligne(self._lignes()))

    def test_une_annotation_trimestrielle_apparait_sur_l_annuel(self):
        """Elle explique une partie de ce que l'état annuel montre.

        Exiger des dates identiques la ferait disparaître dès qu'on change de
        vue, c'est-à-dire au moment où l'on en a le plus besoin.
        """
        self._annoter("Litige résolu au premier trimestre",
                      debut=date(2026, 1, 1), fin=date(2026, 3, 31))
        self.assertIn(
            "Litige résolu au premier trimestre",
            self._annotations_de_la_ligne(self._lignes()))

    def test_plusieurs_annotations_coexistent_sur_une_ligne(self):
        self._annoter("Première explication")
        self._annoter("Seconde explication")
        notes = self._annotations_de_la_ligne(self._lignes())
        self.assertEqual(len(notes), 2)

    def test_une_ligne_sans_annotation_n_en_porte_aucune(self):
        """La clé ne doit pas apparaître à vide : un état sans annotation doit
        produire exactement les mêmes lignes qu'avant l'installation."""
        lignes = self._lignes()
        for ligne in lignes:
            if ligne.get("line_id") != self.ligne.id:
                self.assertNotIn("annotations", ligne)

    # ------------------------------------------------------------------
    # Contrôles
    # ------------------------------------------------------------------

    def test_une_periode_a_l_envers_est_refusee(self):
        with self.assertRaises(ValidationError):
            self._annoter(debut=date(2026, 12, 31), fin=date(2026, 1, 1))

    def test_l_etat_est_deduit_de_la_ligne(self):
        """Saisir les deux permettrait de les rendre incohérents."""
        annotation = self._annoter()
        self.assertEqual(annotation.report_id, self.rapport)

    def test_les_annotations_n_entrent_pas_dans_le_calcul(self):
        """Une note se pose à côté d'un chiffre, jamais dedans."""
        avant = self._lignes()
        valeurs_avant = [
            (l.get("line_id"), str(l.get("columns"))) for l in avant]
        self._annoter("Une note qui ne doit rien changer")
        apres = self._lignes()
        valeurs_apres = [
            (l.get("line_id"), str(l.get("columns"))) for l in apres]
        self.assertEqual(
            valeurs_avant, valeurs_apres,
            "Ajouter une annotation ne doit modifier aucun montant")
