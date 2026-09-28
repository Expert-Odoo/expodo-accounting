# -*- coding: utf-8 -*-
# Copyright 2026 Expodo (https://expodo.fr)
# License LGPL-3
"""Injection des annotations dans les lignes exportées."""

from odoo import models


class EtatFinancier(models.Model):
    _inherit = "account.report"

    def _expodo_export_rows(self, options, limite=None):
        """Ajoute à chaque ligne les annotations qui la concernent.

        On enrichit le résultat du moteur plutôt que de modifier le moteur.
        Les annotations sont un commentaire posé à côté d'un chiffre : elles
        n'ont aucune raison d'entrer dans le calcul, et les y faire entrer
        créerait un chemin par lequel une note pourrait un jour changer un
        montant.
        """
        donnees, lignes = super()._expodo_export_rows(options, limite=limite)
        self._expodo_annoter(options, lignes)
        return donnees, lignes

    def _expodo_annoter(self, options, lignes):
        """Lecture unique de la période, partagée par l'écran et l'export :
        les deux ne peuvent plus afficher des notes différentes."""
        periode = (options or {}).get("date") or {}
        debut, fin = periode.get("date_from"), periode.get("date_to")
        if not (debut and fin):
            return
        par_ligne = self.env["expodo.report.annotation"].pour_periode(
            self.env.company, debut, fin)
        for ligne in lignes:
            notes = par_ligne.get(ligne.get("line_id")) if par_ligne else None
            if notes:
                ligne["annotations"] = [n.text for n in notes]

    def _expodo_serialize_report_lines(self, options, values_by_group):
        """Mêmes annotations à l'écran qu'à l'export.

        Elles n'étaient ajoutées qu'aux lignes exportées : la note saisie
        s'imprimait dans le PDF mais restait invisible dans l'état ouvert à
        l'écran, là où on la cherche d'abord (constaté au port 20.0).
        """
        lignes = super()._expodo_serialize_report_lines(options, values_by_group)
        self._expodo_annoter(options, lignes)
        return lignes
