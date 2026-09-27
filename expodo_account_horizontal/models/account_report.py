# -*- coding: utf-8 -*-
# Copyright 2026 Expodo (https://expodo.fr)
# License LGPL-3
"""Branchement des groupes horizontaux sur le moteur d'états."""

from odoo import models


class EtatFinancier(models.Model):
    _inherit = "account.report"

    def _expodo_get_options(self, previous_options=None):
        """Multiplie les jeux de colonnes par les règles du groupe choisi.

        Chaque jeu existant — la période principale, la période comparée —
        donne autant de jeux qu'il y a de colonnes dans le groupe, chacun
        portant le filtre de sa règle.

        La multiplication se fait ici plutôt que dans le calcul : le moteur
        sait déjà traiter plusieurs jeux de colonnes, il n'a pas à savoir
        pourquoi il y en a plusieurs.
        """
        options = super()._expodo_get_options(previous_options)

        groupe_id = (previous_options or {}).get("horizontal_group_id")
        options["horizontal_group_id"] = groupe_id
        if not groupe_id:
            return options

        groupe = self.env["expodo.report.horizontal.group"].browse(groupe_id)
        if not groupe.exists() or not groupe.rule_ids:
            options["horizontal_group_id"] = False
            return options

        multiplies = {}
        for cle, jeu in options["column_groups"].items():
            for regle in groupe.rule_ids:
                nouvelle_cle = "%s|h%s" % (cle, regle.id)
                nouveau = dict(jeu)
                nouveau["horizontal_rule_id"] = regle.id
                nouveau["horizontal_label"] = regle.name
                nouveau["horizontal_domain"] = regle.domaine()
                multiplies[nouvelle_cle] = nouveau
        options["column_groups"] = multiplies
        return options

    def _expodo_base_domain(self, options, column_group_key, date_scope):
        """Ajoute le filtre de la colonne horizontale, s'il y en a un.

        Le filtre s'ajoute au domaine commun plutôt que de le remplacer : une
        colonne ventilée reste soumise aux mêmes bornes de date, au même
        filtre de journaux et au même état des écritures que le reste de
        l'état. Sans quoi deux colonnes de la même page ne couvriraient pas la
        même chose.
        """
        domaine = super()._expodo_base_domain(
            options, column_group_key, date_scope)
        jeu = (options.get("column_groups") or {}).get(column_group_key) or {}
        supplement = jeu.get("horizontal_domain")
        if supplement:
            domaine = domaine + list(supplement)
        return domaine

    def _expodo_column_group_label(self, key, group):
        """Préfixe le libellé de période par celui de la colonne."""
        base = super()._expodo_column_group_label(key, group)
        etiquette = group.get("horizontal_label")
        return "%s — %s" % (etiquette, base) if etiquette else base
