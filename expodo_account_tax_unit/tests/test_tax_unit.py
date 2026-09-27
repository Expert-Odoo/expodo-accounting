# -*- coding: utf-8 -*-
# Copyright 2026 Expodo (https://expodo.fr)
# License LGPL-3
"""Unités fiscales.

Les deux erreurs que ce module doit rendre impossibles produisent toutes deux
des déclarations qui paraissent complètes : un représentant extérieur au
périmètre, et deux unités qui se recouvrent. Rien dans les totaux ne les
signale.
"""

from odoo.exceptions import ValidationError
from odoo.tests import TransactionCase, tagged


@tagged("post_install", "-at_install")
class TestUnitesFiscales(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.france = cls.env.ref("base.fr")
        cls.belgique = cls.env.ref("base.be")
        cls.mere = cls.env["res.company"].create({"name": "Groupe Dupont SA"})
        cls.fille = cls.env["res.company"].create({"name": "Dupont Services SARL"})
        cls.tierce = cls.env["res.company"].create({"name": "Société tierce"})
        cls.modele = cls.env["expodo.tax.unit"]

    def _unite(self, nom="Groupe TVA Dupont", societes=None, representant=None,
               pays=None):
        societes = societes or (self.mere | self.fille)
        return self.modele.create({
            "name": nom,
            "country_id": (pays or self.france).id,
            "vat": "FR12345678901",
            "company_ids": [(6, 0, societes.ids)],
            "main_company_id": (representant or self.mere).id,
        })

    def test_une_unite_se_cree_avec_son_perimetre(self):
        unite = self._unite()
        self.assertEqual(len(unite.company_ids), 2)
        self.assertIn(unite.main_company_id, unite.company_ids)

    def test_le_representant_doit_appartenir_au_perimetre(self):
        """Une société extérieure produirait deux erreurs à la fois.

        La déclaration porterait son numéro sans couvrir ses opérations, et
        couvrirait celles de sociétés qu'elle ne représente pas. Aucun total
        ne le montrerait.
        """
        with self.assertRaises(ValidationError):
            self._unite(representant=self.tierce)

    def test_une_societe_n_appartient_qu_a_une_unite_par_pays(self):
        """Deux unités qui se recouvrent déclarent deux fois les mêmes
        opérations, chacune paraissant complète."""
        self._unite()
        with self.assertRaises(ValidationError):
            self._unite(nom="Seconde unité", societes=self.fille | self.tierce,
                        representant=self.fille)

    def test_une_societe_peut_relever_d_unites_de_pays_differents(self):
        """Un groupe transfrontalier relève d'un régime par pays.

        L'appartenance à l'unité belge n'empêche pas l'appartenance à
        l'unité française : ce sont deux obligations distinctes, devant deux
        administrations.
        """
        self._unite()
        belge = self.modele.create({
            "name": "BTW-eenheid Dupont",
            "country_id": self.belgique.id,
            "vat": "BE0477472701",
            "company_ids": [(6, 0, (self.mere | self.fille).ids)],
            "main_company_id": self.mere.id,
        })
        self.assertTrue(belge.id)

    def test_l_ouverture_des_etats_porte_sur_le_perimetre(self):
        """C'est toute l'utilité : ne pas resélectionner les sociétés.

        Et surtout ne pas se tromper d'une société sans s'en apercevoir.
        """
        unite = self._unite()
        action = unite.action_open_reports()
        self.assertEqual(
            sorted(action["context"]["allowed_company_ids"]),
            sorted(unite.company_ids.ids))
