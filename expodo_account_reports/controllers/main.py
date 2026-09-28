# -*- coding: utf-8 -*-
# Copyright 2026 Expodo (https://expodo.fr)
# License LGPL-3
"""Téléchargement des exports."""

import json

from odoo import http
from odoo.http import request
from odoo.http.stream import content_disposition


class ExpodoAccountReportController(http.Controller):

    @http.route(
        "/expodo_account_reports/export/<string:export_format>/<int:report_id>",
        type="http", auth="user", methods=["GET"],
        # Pas de `readonly=True` : la génération PDF écrit en base — ne
        # serait-ce que pour mémoriser le format de papier — et Odoo répond
        # alors 503 après avoir réessayé sur un curseur en lecture seule.
        # Le symptôme est trompeur : la requête n'atteint jamais wkhtmltopdf
        # et rien n'apparaît dans le journal.
    )
    def export(self, export_format, report_id, options=None, **kwargs):
        """Génère et renvoie le fichier demandé.

        L'authentification `user` ne suffit pas : les rapports comptables sont
        réservés aux utilisateurs de la comptabilité (CDC F-40). Sans ce
        contrôle, n'importe quel utilisateur interne pourrait télécharger le
        bilan. La liste des groupes est partagée avec le modèle, afin qu'une
        divergence entre l'écran et l'export soit impossible.
        """
        groups = request.env["account.report"].EXPODO_REPORT_GROUPS
        if not any(request.env.user.has_group(g) for g in groups):
            return request.not_found()

        report = request.env["account.report"].browse(report_id).exists()
        if not report:
            return request.not_found()

        parsed = json.loads(options) if options else None

        if export_format == "xlsx":
            content = report._expodo_export_xlsx(parsed)
            mimetype = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            extension = "xlsx"
        elif export_format == "pdf":
            content = report._expodo_export_pdf(parsed)
            mimetype = "application/pdf"
            extension = "pdf"
        else:
            return request.not_found()

        filename = "%s.%s" % (
            (report.display_name or "rapport").replace("/", "-"), extension,
        )
        return request.make_response(content, headers=[
            ("Content-Type", mimetype),
            ("Content-Length", len(content)),
            ("Content-Disposition", content_disposition(filename)),
        ])
