# -*- coding: utf-8 -*-
# Copyright 2026 Expodo (https://expodo.fr)
# License LGPL-3
"""Tests du calcul d'amortissement, exécutables sans Odoo.

    python3 -m unittest discover -s tests_offline -t .

Ce calcul finit en écritures comptables : c'est la partie du module où une
erreur est la plus coûteuse, et la seule qu'on puisse éprouver exhaustivement
sans base de données.
"""

import sys
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine.depreciation import (
    AmortissementError,
    calculer_tableau,
    controler_tableau,
)

DELTA = 0.005


class TestTableauLineaire(unittest.TestCase):

    def test_cas_simple_sans_prorata(self):
        """12 000 sur 12 mois à compter du 1er janvier : 1 000 par mois."""
        t = calculer_tableau(12000.0, date(2026, 1, 1), 12)
        self.assertEqual(len(t), 12)
        for ligne in t:
            self.assertAlmostEqual(ligne.dotation, 1000.0, delta=DELTA)
        self.assertAlmostEqual(t[-1].cumul, 12000.0, delta=DELTA)
        self.assertAlmostEqual(t[-1].valeur_residuelle, 0.0, delta=DELTA)

    def test_la_somme_egale_toujours_la_base(self):
        """Invariant central, éprouvé sur des valeurs qui tombent mal.

        Un tableau dont la somme s'écarte de la base laisse une valeur
        résiduelle parasite au bilan, invisible jusqu'à la cession.
        """
        cas = [
            (10000.0, 36), (7333.33, 60), (1.0, 12), (999.99, 7),
            (123456.78, 84), (50.0, 5), (1000.0, 3), (2500.0, 18),
        ]
        for valeur, duree in cas:
            with self.subTest(valeur=valeur, duree=duree):
                t = calculer_tableau(valeur, date(2026, 3, 17), duree)
                total = round(sum(l.dotation for l in t), 2)
                self.assertAlmostEqual(total, round(valeur, 2), delta=DELTA)

    def test_prorata_de_la_premiere_periode(self):
        """Un bien mis en service en cours de mois n'est pas amorti du mois entier."""
        entier = calculer_tableau(12000.0, date(2026, 1, 1), 12)
        milieu = calculer_tableau(12000.0, date(2026, 1, 16), 12)
        self.assertAlmostEqual(entier[0].dotation, 1000.0, delta=DELTA)
        self.assertLess(
            milieu[0].dotation, entier[0].dotation,
            "La première dotation doit être réduite au prorata des jours",
        )
        self.assertAlmostEqual(
            sum(l.dotation for l in milieu), 12000.0, delta=DELTA,
            msg="Le prorata ne doit pas modifier le total amorti",
        )

    def test_sans_prorata_sur_demande(self):
        t = calculer_tableau(12000.0, date(2026, 1, 16), 12, prorata=False)
        self.assertAlmostEqual(t[0].dotation, 1000.0, delta=DELTA)

    def test_valeur_residuelle(self):
        """La valeur résiduelle n'est jamais amortie."""
        t = calculer_tableau(10000.0, date(2026, 1, 1), 10, valeur_residuelle=2000.0)
        self.assertAlmostEqual(sum(l.dotation for l in t), 8000.0, delta=DELTA)
        self.assertAlmostEqual(t[-1].valeur_residuelle, 2000.0, delta=DELTA)

    def test_periodicites(self):
        for periodicite, attendu in (("monthly", 12), ("quarterly", 4), ("yearly", 1)):
            with self.subTest(periodicite=periodicite):
                t = calculer_tableau(12000.0, date(2026, 1, 1), 12,
                                     periodicite=periodicite)
                self.assertEqual(len(t), attendu)
                self.assertAlmostEqual(sum(l.dotation for l in t), 12000.0, delta=DELTA)

    def test_echeances_en_fin_de_periode(self):
        t = calculer_tableau(12000.0, date(2026, 1, 1), 3)
        self.assertEqual(
            [l.date_echeance for l in t],
            [date(2026, 1, 31), date(2026, 2, 28), date(2026, 3, 31)],
        )

    def test_annee_bissextile(self):
        t = calculer_tableau(1200.0, date(2028, 2, 1), 1)
        self.assertEqual(t[0].date_echeance, date(2028, 2, 29))

    def test_echeances_strictement_croissantes(self):
        t = calculer_tableau(50000.0, date(2026, 11, 20), 40)
        for a, b in zip(t, t[1:]):
            self.assertLess(a.date_echeance, b.date_echeance)


class TestRepriseAnteriorite(unittest.TestCase):
    """Reprise d'un bien déjà partiellement amorti.

    Sans cette reprise, le module est inutilisable pour une entreprise qui
    migre — c'est-à-dire pour la population visée.
    """

    def test_le_deja_amorti_est_retire_du_tableau(self):
        t = calculer_tableau(12000.0, date(2026, 1, 1), 12, deja_amorti=3000.0)
        self.assertAlmostEqual(sum(l.dotation for l in t), 9000.0, delta=DELTA)
        self.assertAlmostEqual(t[-1].cumul, 12000.0, delta=DELTA)
        self.assertAlmostEqual(t[-1].valeur_residuelle, 0.0, delta=DELTA)

    def test_le_cumul_part_du_deja_amorti(self):
        t = calculer_tableau(12000.0, date(2026, 1, 1), 12, deja_amorti=3000.0)
        self.assertGreater(t[0].cumul, 3000.0)

    def test_bien_entierement_amorti(self):
        """Un bien déjà soldé ne produit aucune échéance, et ne lève pas."""
        t = calculer_tableau(12000.0, date(2026, 1, 1), 12, deja_amorti=12000.0)
        self.assertEqual(t, [])

    def test_deja_amorti_excessif_rejete(self):
        with self.assertRaises(AmortissementError):
            calculer_tableau(12000.0, date(2026, 1, 1), 12, deja_amorti=15000.0)


class TestParametresInvalides(unittest.TestCase):

    def test_valeur_nulle_ou_negative(self):
        for valeur in (0.0, -100.0):
            with self.subTest(valeur=valeur), self.assertRaises(AmortissementError):
                calculer_tableau(valeur, date(2026, 1, 1), 12)

    def test_duree_nulle(self):
        with self.assertRaises(AmortissementError):
            calculer_tableau(1000.0, date(2026, 1, 1), 0)

    def test_periodicite_inconnue(self):
        with self.assertRaises(AmortissementError):
            calculer_tableau(1000.0, date(2026, 1, 1), 12, periodicite="weekly")

    def test_residuelle_superieure_a_la_valeur(self):
        with self.assertRaises(AmortissementError):
            calculer_tableau(1000.0, date(2026, 1, 1), 12, valeur_residuelle=1500.0)


class TestControles(unittest.TestCase):
    """Le contrôle qui précède toute écriture comptable."""

    def test_tableau_valide_accepte(self):
        t = calculer_tableau(9876.54, date(2026, 5, 9), 27)
        controler_tableau(t, 9876.54)

    def test_somme_incoherente_rejetee(self):
        t = calculer_tableau(10000.0, date(2026, 1, 1), 10)
        altere = list(t)
        altere[0] = type(t[0])(
            numero=t[0].numero, date_echeance=t[0].date_echeance,
            dotation=t[0].dotation + 50.0, cumul=t[0].cumul,
            valeur_residuelle=t[0].valeur_residuelle,
        )
        with self.assertRaises(AmortissementError):
            controler_tableau(altere, 10000.0)

    def test_tableau_vide_accepte(self):
        controler_tableau([], 10000.0, deja_amorti=10000.0)

    def test_controle_avec_residuelle_et_reprise(self):
        t = calculer_tableau(20000.0, date(2026, 2, 14), 48,
                             valeur_residuelle=2000.0, deja_amorti=5000.0)
        controler_tableau(t, 20000.0, valeur_residuelle=2000.0, deja_amorti=5000.0)


class TestArrondi(unittest.TestCase):

    def test_arrondi_commercial_et_non_bancaire(self):
        """Régression : `round()` de Python arrondit le demi vers le pair.

        Sur des dotations mensuelles, l'écart se cumule sur toute la durée.
        """
        from engine.depreciation import _arrondi
        self.assertEqual(_arrondi(0.125), 0.13)
        self.assertEqual(round(0.125, 2), 0.12)

    def test_aucune_dotation_negative(self):
        for valeur, duree, deja in ((1000.0, 12, 999.0), (5000.0, 60, 4999.99)):
            with self.subTest(valeur=valeur):
                t = calculer_tableau(valeur, date(2026, 1, 1), duree, deja_amorti=deja)
                self.assertTrue(all(l.dotation >= 0 for l in t))


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestArrondiParCumul(unittest.TestCase):
    """Régression : la méthode d'arrondi naïve produisait des dotations négatives.

    Arrondir chaque dotation séparément puis faire absorber le résidu par la
    dernière échéance échoue dès que le résidu dépasse une dotation. Cas type :
    1,00 € sur 18 mois donne dix-sept fois 0,06 puis une dernière à −0,02.

    Une dotation négative deviendrait une écriture comptable inversée.

    La correction arrondit le **cumul**, chaque dotation étant la différence de
    deux cumuls arrondis : le cumul théorique étant croissant, sa version
    arrondie l'est aussi, donc aucune différence ne peut être négative.
    """

    def test_petite_valeur_longue_duree(self):
        t = calculer_tableau(1.0, date(2026, 1, 1), 18)
        self.assertTrue(all(l.dotation > 0 for l in t),
                        "Aucune dotation ne doit être négative ou nulle")
        self.assertAlmostEqual(sum(l.dotation for l in t), 1.0, delta=DELTA)

    def test_balayage_exhaustif(self):
        """Toutes les combinaisons plausibles, sans exception.

        10 584 combinaisons de valeur, durée, périodicité, date de mise en
        service, valeur résiduelle et reprise d'antériorité.
        """
        valeurs = [1.0, 50.0, 999.99, 1234.56, 10000.0, 87654.32, 1000000.0]
        durees = [1, 3, 5, 12, 18, 36, 60, 84, 120]
        rompus = []
        for valeur in valeurs:
            for duree in durees:
                for periodicite in ("monthly", "quarterly", "yearly"):
                    for mois in (1, 2, 4, 12):
                        for jour in (1, 15, 28):
                            debut = date(2026, mois, jour)
                            for residuelle in (0.0, round(valeur * 0.1, 2)):
                                for deja in (0.0, round((valeur - residuelle) * 0.3, 2)):
                                    tableau = calculer_tableau(
                                        valeur, debut, duree, periodicite,
                                        residuelle, deja)
                                    try:
                                        controler_tableau(tableau, valeur,
                                                          residuelle, deja)
                                    except AmortissementError as erreur:
                                        rompus.append(
                                            "%.2f / %d mois / %s / %s : %s"
                                            % (valeur, duree, periodicite,
                                               debut, erreur))
        self.assertFalse(
            rompus,
            "%d combinaisons rompues, dont :\n  %s"
            % (len(rompus), "\n  ".join(rompus[:5])),
        )

    def test_aucune_echeance_vide(self):
        """Une échéance à zéro produirait une écriture comptable vide."""
        for valeur, duree in ((1.0, 24), (0.5, 12), (3.0, 60)):
            with self.subTest(valeur=valeur, duree=duree):
                t = calculer_tableau(valeur, date(2026, 1, 1), duree)
                self.assertTrue(all(l.dotation > 0 for l in t))


class TestPrecisionDeLaDevise(unittest.TestCase):
    """Le tableau doit tomber juste dans la monnaie du bien.

    Il était calculé au centime quelle que soit la devise. En yen, qui n'a
    pas de subdivision, douze dotations de 833,33 étaient ramenées à 833 au
    moment d'être enregistrées : le tableau annonçait 10 000, les échéances
    totalisaient 9 996, et quatre yens restaient indéfiniment au compte
    d'immobilisation, sans ligne pour les expliquer.

    Le cas se présente sur toutes les monnaies sans décimale — yen, won,
    peso chilien, couronne islandaise, dong — et en sens inverse sur celles
    qui en ont trois, dinar tunisien, koweïtien, bahreïni.
    """

    BASES = (12000.0, 10000.0, 1.0, 999.99, 24000.0, 7.0, 333.33)
    DUREES = (1, 7, 12, 18, 60)

    def test_la_somme_tombe_juste_dans_chaque_precision(self):
        for decimales in (0, 2, 3):
            unite = 0.5 * 10 ** -decimales
            for base in self.BASES:
                for duree in self.DUREES:
                    with self.subTest(decimales=decimales, base=base, duree=duree):
                        lignes = calculer_tableau(
                            valeur_acquisition=base,
                            date_mise_en_service=date(2026, 3, 15),
                            duree_mois=duree, periodicite="monthly",
                            decimales=decimales)
                        total = sum(l.dotation for l in lignes)
                        self.assertLessEqual(
                            abs(total - round(base, decimales)), unite,
                            "%s sur %s mois en %s décimales : somme %s"
                            % (base, duree, decimales, total))

    def test_aucune_dotation_negative_dans_aucune_precision(self):
        for decimales in (0, 2, 3):
            for base in self.BASES:
                for duree in self.DUREES:
                    lignes = calculer_tableau(
                        valeur_acquisition=base,
                        date_mise_en_service=date(2026, 3, 15),
                        duree_mois=duree, periodicite="monthly",
                        decimales=decimales)
                    negatives = [l.dotation for l in lignes if l.dotation < 0]
                    self.assertEqual(
                        negatives, [],
                        "Dotation négative pour %s sur %s mois en %s décimales"
                        % (base, duree, decimales))

    def test_le_controle_accepte_le_tableau_de_sa_devise(self):
        """Le contrôle doit tolérer une demi-unité, pas un demi-centime.

        Sa tolérance était fixée à 0,005 : elle laissait passer un écart d'un
        yen entier et refusait un tableau juste en dinar.
        """
        for decimales in (0, 2, 3):
            lignes = calculer_tableau(
                valeur_acquisition=10000.0,
                date_mise_en_service=date(2026, 3, 15),
                duree_mois=12, periodicite="monthly", decimales=decimales)
            controler_tableau(lignes, 10000.0, decimales=decimales)
