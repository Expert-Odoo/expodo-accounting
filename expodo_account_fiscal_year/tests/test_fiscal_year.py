# -*- coding: utf-8 -*-
# Copyright 2026 Expodo (https://expodo.fr)
# License LGPL-3
"""Exercices fiscaux déclarés.

Le risque de ce module n'est pas qu'il échoue : c'est qu'il réussisse trop.
Une surcharge de `compute_fiscalyear_dates` touche tout ce qui lit un exercice.
Si elle se déclenche à tort, elle décale silencieusement la période de tous les
états, de la clôture et du FEC — sans qu'aucun message ne soit levé.

Ces tests vérifient donc autant qu'elle s'applique quand il le faut que
**qu'elle se retire quand il ne le faut pas**.
"""

from datetime import date

from dateutil.relativedelta import relativedelta

from odoo import fields

from odoo.exceptions import ValidationError
from odoo.tests import TransactionCase, tagged


@tagged("post_install", "-at_install")
class TestExercicesFiscaux(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.societe = cls.env.company
        cls.societe.write({
            "fiscalyear_last_month": "12", "fiscalyear_last_day": 31})
        cls.modele = cls.env["expodo.fiscal.year"]

    # ------------------------------------------------------------------
    # Comportement par défaut préservé
    # ------------------------------------------------------------------

    def test_sans_declaration_le_comportement_d_odoo_est_intact(self):
        """C'est le test le plus important du module.

        Une surcharge qui modifierait le calcul par défaut décalerait la
        période de tous les états sans rien signaler.
        """
        bornes = self.societe.compute_fiscalyear_dates(date(2026, 6, 15))
        self.assertEqual(bornes["date_from"], date(2026, 1, 1))
        self.assertEqual(bornes["date_to"], date(2026, 12, 31))

    def test_une_date_hors_de_tout_exercice_declare_retombe_sur_le_motif(self):
        self.modele.create({
            "name": "Exercice déclaré 2024",
            "company_id": self.societe.id,
            "date_from": date(2024, 1, 1), "date_to": date(2024, 12, 31),
        })
        bornes = self.societe.compute_fiscalyear_dates(date(2026, 6, 15))
        self.assertEqual(
            bornes["date_from"], date(2026, 1, 1),
            "Une date hors des exercices déclarés doit suivre le motif de la "
            "société, pas le dernier exercice déclaré")

    # ------------------------------------------------------------------
    # Exercices irréguliers
    # ------------------------------------------------------------------

    def test_un_premier_exercice_de_quinze_mois(self):
        """Le cas le plus courant : constitution en cours d'année.

        Sans déclaration, un bilan de quinze mois s'afficherait comme un bilan
        de douze, et rien ne le signalerait.
        """
        exercice = self.modele.create({
            "name": "Premier exercice",
            "company_id": self.societe.id,
            "date_from": date(2025, 10, 1), "date_to": date(2026, 12, 31),
        })
        self.assertEqual(exercice.duration_months, 15)

        for jour in (date(2025, 11, 5), date(2026, 3, 20), date(2026, 12, 30)):
            bornes = self.societe.compute_fiscalyear_dates(jour)
            self.assertEqual(
                bornes["date_from"], date(2025, 10, 1),
                "Le %s appartient au premier exercice" % jour)
            self.assertEqual(bornes["date_to"], date(2026, 12, 31))

    def test_un_exercice_de_transition_plus_court(self):
        """Changement de date de clôture pour s'aligner sur un groupe."""
        self.modele.create({
            "name": "Transition",
            "company_id": self.societe.id,
            "date_from": date(2026, 1, 1), "date_to": date(2026, 6, 30),
        })
        bornes = self.societe.compute_fiscalyear_dates(date(2026, 4, 1))
        self.assertEqual(bornes["date_to"], date(2026, 6, 30))

    # ------------------------------------------------------------------
    # Contrôles
    # ------------------------------------------------------------------

    def test_un_exercice_a_l_envers_est_refuse(self):
        with self.assertRaises(ValidationError):
            self.modele.create({
                "name": "À l'envers",
                "company_id": self.societe.id,
                "date_from": date(2026, 12, 31), "date_to": date(2026, 1, 1),
            })

    def test_deux_exercices_ne_peuvent_pas_se_chevaucher(self):
        """Un chevauchement rendrait la période arbitraire.

        Deux écrans se contrediraient selon l'ordre de lecture, et rien ne
        permettrait à l'utilisateur de comprendre pourquoi.
        """
        self.modele.create({
            "name": "Premier",
            "company_id": self.societe.id,
            "date_from": date(2026, 1, 1), "date_to": date(2026, 12, 31),
        })
        with self.assertRaises(ValidationError):
            self.modele.create({
                "name": "Chevauchant",
                "company_id": self.societe.id,
                "date_from": date(2026, 7, 1), "date_to": date(2027, 6, 30),
            })

    def test_deux_exercices_consecutifs_sont_acceptes(self):
        """Se suivre n'est pas se chevaucher."""
        self.modele.create({
            "name": "Premier",
            "company_id": self.societe.id,
            "date_from": date(2025, 1, 1), "date_to": date(2025, 12, 31),
        })
        suivant = self.modele.create({
            "name": "Second",
            "company_id": self.societe.id,
            "date_from": date(2026, 1, 1), "date_to": date(2026, 12, 31),
        })
        self.assertTrue(suivant.id)

    def test_deux_societes_peuvent_avoir_des_exercices_superposes(self):
        """Le chevauchement n'a de sens qu'au sein d'une même société."""
        autre = self.env["res.company"].create({"name": "Société voisine"})
        self.modele.create({
            "name": "Chez l'une",
            "company_id": self.societe.id,
            "date_from": date(2026, 1, 1), "date_to": date(2026, 12, 31),
        })
        chez_elle = self.modele.create({
            "name": "Chez l'autre",
            "company_id": autre.id,
            "date_from": date(2026, 1, 1), "date_to": date(2026, 12, 31),
        })
        self.assertTrue(chez_elle.id)

    # ------------------------------------------------------------------
    # Effet sur les autres modules
    # ------------------------------------------------------------------

    def test_la_cloture_suit_l_exercice_declare(self):
        """Tout ce qui lit l'exercice doit en bénéficier sans le savoir.

        L'exercice déclaré doit contenir la date du jour : les assistants
        proposent l'exercice *en cours*, et un exercice déjà clos n'a pas à
        être proposé. Ma première version de ce test déclarait un exercice
        achevé et s'étonnait du repli sur le motif — le code avait raison.
        """
        if "expodo.year.closing" not in self.env.registry.models:
            self.skipTest("Module de clôture non installé")
        aujourd_hui = fields.Date.context_today(self.env.user)
        self.modele.create({
            "name": "Exercice en cours, irrégulier",
            "company_id": self.societe.id,
            "date_from": aujourd_hui - relativedelta(months=8),
            "date_to": aujourd_hui + relativedelta(months=2),
        })
        assistant = self.env["expodo.year.closing"].create({})
        self.assertEqual(
            assistant.date_to, aujourd_hui + relativedelta(months=2),
            "La clôture doit proposer les bornes de l'exercice déclaré")

    def test_l_export_fec_suit_aussi_l_exercice_declare(self):
        if "expodo.fec.export" not in self.env.registry.models:
            self.skipTest("Module FEC non installé")
        aujourd_hui = fields.Date.context_today(self.env.user)
        self.modele.create({
            "name": "Exercice en cours, irrégulier",
            "company_id": self.societe.id,
            "date_from": aujourd_hui - relativedelta(months=4),
            "date_to": aujourd_hui + relativedelta(months=5),
        })
        assistant = self.env["expodo.fec.export"].create({})
        self.assertEqual(
            assistant.date_from, aujourd_hui - relativedelta(months=4),
            "Le FEC doit couvrir l'exercice déclaré, pas l'année civile")
