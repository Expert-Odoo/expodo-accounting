# -*- coding: utf-8 -*-
# Copyright 2026 Expodo (https://expodo.fr)
# License LGPL-3
"""Injection des annotations dans les lignes exportées."""

from odoo import models


class EtatFinancier(models.Model):
    _inherit = "account.report"

    def _expodo_annotations_par_ligne(self, options):
        """Les annotations en vigueur sur la période, par ligne d'état.

        Une explication périmée présentée comme actuelle est pire qu'une
        absence d'explication : la période des options fait foi, ici comme à
        l'export.
        """
        periode = (options or {}).get("date") or {}
        debut, fin = periode.get("date_from"), periode.get("date_to")
        if not (debut and fin):
            return {}
        return self.env["expodo.report.annotation"].pour_periode(
            self.env.company, debut, fin)

    def _expodo_serialize_report_lines(self, options, values_by_group):
        """Ajoute les annotations aux lignes envoyées au navigateur.

        Elles n'étaient posées que sur les lignes d'export : le PDF et le
        classeur les portaient, l'écran non. Or on annote pour expliquer un
        chiffre à celui qui le regarde, et c'est à l'écran qu'on le regarde.
        Personne n'exporte un état pour relire son propre commentaire.
        """
        lignes = super()._expodo_serialize_report_lines(options, values_by_group)
        par_ligne = self._expodo_annotations_par_ligne(options)
        if not par_ligne:
            return lignes
        for ligne in lignes:
            notes = par_ligne.get(ligne.get("line_id"))
            if notes:
                ligne["annotations"] = [n.text for n in notes]
        return lignes

    def _expodo_export_rows(self, options, limite=None):
        """Ajoute à chaque ligne les annotations qui la concernent.

        On enrichit le résultat du moteur plutôt que de modifier le moteur.
        Les annotations sont un commentaire posé à côté d'un chiffre : elles
        n'ont aucune raison d'entrer dans le calcul, et les y faire entrer
        créerait un chemin par lequel une note pourrait un jour changer un
        montant.
        """
        donnees, lignes = super()._expodo_export_rows(options, limite=limite)

        par_ligne = self._expodo_annotations_par_ligne(options)
        if not par_ligne:
            return donnees, lignes

        for ligne in lignes:
            notes = par_ligne.get(ligne.get("line_id"))
            if notes:
                ligne["annotations"] = [n.text for n in notes]

        return donnees, lignes
