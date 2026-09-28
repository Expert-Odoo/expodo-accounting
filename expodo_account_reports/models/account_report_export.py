# -*- coding: utf-8 -*-
# Copyright 2026 Expodo (https://expodo.fr)
# License LGPL-3
"""Exports PDF et XLSX.

Les deux exports partent de la **même sérialisation** que l'affichage écran
(`_expodo_get_report_data`). C'est délibéré : un export qui recalculerait le
rapport par un autre chemin pourrait diverger de ce que l'utilisateur a sous
les yeux, et un écart entre l'écran et le PDF transmis à la banque est le
genre d'incident qui coûte un client.

L'en-tête porte société, période et filtres actifs (CDC F-32) : un état
comptable sans son périmètre n'est pas exploitable.
"""

import io
import json

from odoo import models
from odoo.exceptions import UserError
from odoo.tools.misc import format_date


class AccountReport(models.Model):
    _inherit = "account.report"

    # ------------------------------------------------------------------
    # Point d'entrée
    # ------------------------------------------------------------------

    def expodo_action_export(self, options, export_format):
        """Déclenche le téléchargement du rapport dans le format demandé."""
        self.ensure_one()
        self._expodo_check_access()
        if export_format not in ("pdf", "xlsx"):
            raise UserError(
                self.env._("Unsupported export format: %(fmt)s", fmt=export_format)
            )
        return {
            "type": "ir.actions.act_url",
            "url": "/expodo_account_reports/export/%s/%s?options=%s" % (
                export_format, self.id, json.dumps(options),
            ),
            "target": "self",
        }

    # ------------------------------------------------------------------
    # Contexte commun aux deux formats
    # ------------------------------------------------------------------

    def _expodo_export_period_label(self, options):
        """Libellé de période, selon ce que l'état lit réellement.

        Une balance âgée ne lit pas un intervalle : ses expressions partent de
        l'origine et seule la date de fin compte, puisque c'est à ce jour-là
        que l'ancienneté se calcule. Annoncer « du 1er janvier au 20 septembre »
        laisserait croire que déplacer la date de début change les montants,
        alors qu'elle n'a aucun effet.

        Le critère est donc la portée des expressions, pas le nom de l'état :
        tout état qui lit depuis l'origine s'annonce comme un arrêté. Un état
        qui n'offre pas d'intervalle à l'écran s'annonce de même, pour que le
        fichier transmis dise la même période que la page dont il sort.

        Les dates suivent la langue du destinataire. Interpolées telles
        quelles, elles sortaient en ISO — « Au 2026-09-26 » sur un bilan
        français, quand tout le reste du document est en français et que le
        lecteur attend 26/09/2026.
        """
        self.ensure_one()
        dates = options["date"]
        if self._expodo_est_un_arrete():
            return self.env._(
                "As of %(date)s",
                date=format_date(self.env, dates["date_to"]))
        return self.env._(
            "From %(from)s to %(to)s",
            **{"from": format_date(self.env, dates["date_from"]),
               "to": format_date(self.env, dates["date_to"])})

    def _expodo_est_un_arrete(self):
        """L'état porte-t-il des soldes arrêtés à une date ?

        Le critère est la portée des expressions, pas le nom de l'état : tout
        état qui lit depuis l'origine est un arrêté. Un état qui n'offre pas
        d'intervalle à l'écran l'est aussi.

        Extrait du libellé de l'export pour que l'en-tête des colonnes à
        l'écran tranche de la même façon : le fichier transmis et la page dont
        il sort doivent annoncer la même période.
        """
        self.ensure_one()
        portees = {
            expression.date_scope
            for ligne in self.line_ids
            for expression in ligne.expression_ids
        }
        return bool(not self.filter_date_range or (
            portees and portees == {"from_beginning"}))

    def _expodo_export_header(self, options):
        """Société, période et filtres actifs (CDC F-32)."""
        self.ensure_one()
        dates = options["date"]
        companies = self.env["res.company"].browse(options.get("company_ids") or [])

        filters = []
        if options.get("all_entries"):
            filters.append(self.env._("All entries, including drafts"))
        else:
            filters.append(self.env._("Posted entries only"))
        if options.get("journal_ids"):
            journals = self.env["account.journal"].browse(options["journal_ids"])
            filters.append(
                self.env._("Journals: %(names)s",
                           names=", ".join(journals.mapped("name")))
            )
        if options.get("partner_ids"):
            partners = self.env["res.partner"].browse(options["partner_ids"])
            filters.append(
                self.env._("Partners: %(names)s",
                           names=", ".join(partners.mapped("display_name")))
            )

        return {
            "report_name": self.display_name,
            "company_name": ", ".join(companies.mapped("name")) or self.env.company.name,
            "date_from": dates["date_from"],
            "date_to": dates["date_to"],
            # Période rédigée et traduite, plutôt que recomposée dans le
            # gabarit : « Du … au … » y était écrit en dur, si bien qu'un
            # utilisateur anglophone recevait un document bilingue.
            "period": self._expodo_export_period_label(options),
            "label_column": self.env._("Label"),
            "filters": filters,
            "generated_on": self.env["ir.qweb.field.datetime"].value_to_html(
                self.env.cr.now(), {}
            ) if False else None,
        }

    #: Nombre maximal de lignes écrites dans un export **tableur**.
    #:
    #: Un grand livre d'un grand compte peut compter des centaines de milliers
    #: d'écritures : sans borne, l'export épuiserait la mémoire du serveur et
    #: produirait un fichier qu'aucun tableur n'ouvre. La borne est haute —
    #: au-delà, c'est un extracteur de données qu'il faut, pas un rapport.
    EXPODO_EXPORT_MAX_ROWS = 100000

    #: Borne propre au **PDF**, beaucoup plus basse.
    #:
    #: Le coût du rendu n'est pas linéaire. Mesuré sur un grand livre de
    #: vingt-quatre mille lignes :
    #:
    #:     1 000 lignes →  5,1 s →   176 Ko
    #:     5 000 lignes →  6,5 s →   800 Ko
    #:    20 000 lignes → 21,5 s → 3 039 Ko
    #:
    #: Entre cinq et vingt mille, le temps triple pour un document que
    #: personne n'ouvre : quatre cents pages ne se lisent pas, elles
    #: s'extraient. La borne est donc posée là où le pire cas reste sous sept
    #: secondes, et le message de troncature renvoie au tableur, qui tient
    #: vingt fois plus et se filtre.
    #:
    #: Le tableur garde sa borne haute parce qu'un classeur de vingt mille
    #: lignes est réellement exploitable — on y trie, on y somme, on y
    #: recherche. Un PDF ne permet rien de tout cela.
    EXPODO_EXPORT_MAX_ROWS_PDF = 5000

    def _expodo_export_rows(self, options, limite=None):
        """Lignes à exporter, **tous niveaux de dépliage développés**.

        Un export ne peut pas être « replié » : l'utilisateur qui exporte veut
        le détail, et c'est même la seule raison d'exporter.

        Le dépliage était limité à un niveau, ce qui produisait un grand livre
        contenant ses comptes mais aucune écriture — c'est-à-dire une balance.
        Le fichier s'ouvrait, les totaux étaient justes, et il manquait
        précisément ce qu'on venait y chercher. Le défaut ne se voit qu'en
        comparant le nombre de lignes du fichier à celui de la base.
        """
        self.ensure_one()
        plafond = limite or self.EXPODO_EXPORT_MAX_ROWS
        data = self.expodo_get_report_data(options)
        rows = []
        tronque = False

        def developper(ligne, groupe=None, niveau=0):
            """Ajoute la ligne puis, récursivement, tout ce qu'elle contient."""
            nonlocal tronque
            rows.append(ligne)
            if not ligne.get("unfoldable") or tronque:
                return
            enfants = self.expodo_expand_line(
                ligne["line_id"], data["options"],
                parent_group=ligne.get("group"), level=niveau,
            )
            for enfant in enfants:
                if len(rows) >= plafond:
                    tronque = True
                    return
                developper(enfant, enfant.get("group"), niveau + 1)

        for line in data["lines"]:
            # La borne vaut aussi pour les lignes de premier niveau : une fois
            # l'export tronqué, les lignes suivantes (résultat antérieur d'un
            # grand livre, par exemple) la dépassaient.
            if tronque or len(rows) >= plafond:
                tronque = True
                break
            developper(line)

        # Le message expliquant qu'un rapport n'a aucune ligne définie doit
        # figurer aussi dans le fichier. Il n'était posé qu'à l'écran : un
        # utilisateur qui exportait la déclaration américaine — que la
        # localisation livre vide — obtenait un document sans la moindre
        # explication, encore plus déroutant qu'un écran vide puisqu'il peut
        # le transmettre à un tiers.
        if not rows and data.get("notice"):
            rows.append({
                "id": "expodo_notice",
                "name": data["notice"],
                "level": 0,
                # La clé est `values`, au pluriel : c'est celle que lisent le
                # gabarit PDF et le classeur. Au singulier, le rendu lève une
                # `KeyError` — et cette ligne n'apparaît que dans les cas rares
                # qu'elle est censée expliquer, donc le défaut serait sorti
                # chez un client, jamais ici.
                "columns": [{"label": c.get("label") or c.get("name") or "",
                             "values": {}, "raw": {}, "auditable": False}
                            for c in data["columns"]],
            })

        if tronque:
            rows.append({
                "id": "expodo_truncated",
                "name": self.env._(
                    "Export truncated at %(count)d rows. Narrow the period, "
                    "filter by journal, or use the Excel export, which holds "
                    "far more.",
                    count=plafond),
                "level": 0,
                # Les colonnes sont reprises telles qu'elles existent, sans
                # présumer du nom de leurs clés : une ligne d'avertissement
                # qui casse l'export vaut moins que pas d'avertissement.
                # La clé est `values`, au pluriel : c'est celle que lisent le
                # gabarit PDF et le classeur. Au singulier, le rendu lève une
                # `KeyError` — et cette ligne n'apparaît que dans les cas rares
                # qu'elle est censée expliquer, donc le défaut serait sorti
                # chez un client, jamais ici.
                "columns": [{"label": c.get("label") or c.get("name") or "",
                             "values": {}, "raw": {}, "auditable": False}
                            for c in data["columns"]],
            })
        return data, rows

    # ------------------------------------------------------------------
    # XLSX
    # ------------------------------------------------------------------

    def _expodo_export_xlsx(self, options):
        """Classeur Excel.

        Les montants sont écrits en **numérique**, pas en texte : un export
        dont les chiffres arrivent en chaînes est inutilisable pour la
        révision comptable, qui consiste précisément à les recalculer
        (CDC T22).
        """
        precision = self.env.company.currency_id.decimal_places or 2

        self.ensure_one()
        try:
            import xlsxwriter
        except ImportError as error:
            raise UserError(
                self.env._("The xlsxwriter library is required for XLSX export.")
            ) from error

        options = self._expodo_deserialize_options(options)
        data, rows = self._expodo_export_rows(options)
        header = self._expodo_export_header(options)

        stream = io.BytesIO()
        book = xlsxwriter.Workbook(stream, {"in_memory": True})
        sheet = book.add_worksheet(self.display_name[:31])

        title = book.add_format({"bold": True, "font_size": 14})
        meta = book.add_format({"font_size": 9, "font_color": "#666666"})
        # Retour à la ligne obligatoire sur l'en-tête.
        #
        # Le nom d'une colonne comparée fait une cinquantaine de caractères
        # — « Credit (Compared: from 2025-01-01 to 2025-12-31) » — dans une
        # colonne de treize. Sans repli, et la cellule voisine étant occupée,
        # le tableur tronque : l'utilisateur lit « Credit (Compared: from… »
        # sur les six colonnes et ne distingue plus les deux périodes. Le
        # défaut porte précisément sur la fonction de comparaison.
        head = book.add_format({
            "bold": True, "bg_color": "#F2F2F2", "border": 1,
            "text_wrap": True, "valign": "top",
        })
        money = book.add_format({"num_format": "#,##0.00"})
        money_bold = book.add_format({"num_format": "#,##0.00", "bold": True})
        bold = book.add_format({"bold": True})
        note = book.add_format({"font_size": 9, "italic": True,
                                "font_color": "#555555", "text_wrap": True,
                                "valign": "top"})

        sheet.write(0, 0, header["report_name"], title)
        sheet.write(1, 0, header["company_name"], meta)
        # Traduit, comme tout le reste : ces deux chaînes étaient restées en
        # français dans le classeur et le PDF, si bien qu'un utilisateur
        # anglophone recevait un fichier bilingue — « Libellé » et « Du … au … »
        # au milieu de colonnes anglaises.
        sheet.write(2, 0, self._expodo_export_period_label(options), meta)
        sheet.write(3, 0, " — ".join(header["filters"]), meta)

        # Colonne de notes, ajoutée seulement si des annotations existent.
        #
        # Les annotations étaient attachées aux lignes exportées et aucun des
        # deux formats ne les écrivait : on pouvait poser une note sur un
        # poste et personne ne pouvait la lire.
        avec_notes = any(ligne.get("annotations") for ligne in rows)

        row = 5
        sheet.write(row, 0, self.env._("Label"), head)
        # Hauteur suffisante pour trois lignes de texte replié.
        sheet.set_row(row, 48)
        column_index = 1
        for group in data["column_groups"]:
            for column in data["columns"]:
                label = column["name"]
                if len(data["column_groups"]) > 1:
                    label = "%s (%s)" % (column["name"], group["label"])
                sheet.write(row, column_index, label, head)
                column_index += 1
        if avec_notes:
            sheet.write(row, column_index, self.env._("Notes"), head)

        sheet.set_column(0, 0, 55)
        sheet.set_column(1, column_index, 16)
        if avec_notes:
            sheet.set_column(column_index, column_index, 60)

        for line in rows:
            row += 1
            is_total = (line.get("level") or 0) == 0
            sheet.write(
                row, 0,
                "    " * (line.get("level") or 0) + (line.get("name") or ""),
                bold if is_total else None,
            )
            column_index = 1
            for group in data["column_groups"]:
                for cell in line["columns"]:
                    raw = (cell.get("raw") or {}).get(group["key"])
                    if raw is None:
                        sheet.write_blank(row, column_index, None)
                    elif not isinstance(raw, (int, float)):
                        # Colonne de détail : une date ou un nom de tiers.
                        #
                        # Le classeur reçoit la valeur **déjà formatée**, celle
                        # que l'écran affiche. Écrire l'identifiant brut d'un
                        # tiers donnerait une colonne de nombres sans
                        # signification, et une date écrite comme un nombre
                        # oblige le lecteur à la reformater à la main.
                        sheet.write_string(
                            row, column_index,
                            (cell.get("values") or {}).get(group["key"]) or "",
                            bold if is_total else None,
                        )
                    else:
                        # Arrondi à la précision de la devise avant écriture.
                        #
                        # La somme de flottants laisse des résidus :
                        # 78 757,21 s'écrivait « 78757.21000000001 » dans le
                        # classeur. La cellule est formatée, donc l'écran reste
                        # juste — mais la valeur réelle porte le bruit, et il
                        # ressort dès qu'on recalcule ou qu'on compare deux
                        # colonnes. Or recalculer est précisément ce qu'on fait
                        # d'un export comptable.
                        sheet.write_number(
                            row, column_index,
                            round(float(raw), precision),
                            money_bold if is_total else money,
                        )
                    column_index += 1
            if avec_notes:
                notes = line.get("annotations") or []
                if notes:
                    sheet.write_string(row, column_index, "\n".join(notes), note)

        sheet.freeze_panes(6, 1)
        book.close()
        return stream.getvalue()

    # ------------------------------------------------------------------
    # PDF
    # ------------------------------------------------------------------

    def _expodo_sens_lecture(self):
        """« rtl » ou « ltr », selon la langue dans laquelle l'état est rendu.

        Odoo porte l'information sur `res.lang.direction`. On la lit plutôt
        que de tenir une liste de langues : la liste se périmerait au premier
        ajout de langue, alors que le champ est renseigné par Odoo lui-même.
        """
        code = self.env.context.get("lang") or self.env.user.lang or "en_US"
        langue = self.env["res.lang"].with_context(active_test=False).search(
            [("code", "=", code)], limit=1)
        return langue.direction or "ltr"

    def _expodo_export_pdf(self, options):
        """Document PDF, via le moteur de rendu QWeb d'Odoo."""
        self.ensure_one()
        options = self._expodo_deserialize_options(options)
        data, rows = self._expodo_export_rows(
            options, limite=self.EXPODO_EXPORT_MAX_ROWS_PDF
        )
        header = self._expodo_export_header(options)

        html = self.env["ir.qweb"]._render(
            "expodo_account_reports.report_pdf_document",
            {
                "header": header,
                "labels": {"label": header["label_column"]},
                "column_groups": data["column_groups"],
                "columns": data["columns"],
                "lines": rows,
                # Le sens de lecture de la langue du rendu. Il pilote
                # l'attribut `dir` de la page et l'alignement des montants :
                # une écriture arabe ou hébraïque ne se lit pas dans l'ordre
                # de colonnes occidental.
                "sens_lecture": self._expodo_sens_lecture(),
            },
        )
        # Odoo 20 : le moteur PDF est enfichable (wkhtmltopdf n'est plus
        # qu'une implémentation parmi d'autres, dans base_report_wkhtmltox).
        # On passe par le moteur configuré sur la base plutôt que de le nommer.
        rapport = self.env["ir.actions.report"]
        return rapport._run_pdf_engine_without_processing(
            rapport._get_pdf_engine(),
            [html],
            landscape=len(data["columns"]) * len(data["column_groups"]) > 4,
            specific_paperformat_args={
                "data-report-margin-top": 10,
                "data-report-header-spacing": 10,
                "data-report-margin-bottom": 10,
                # Les marges gauche et droite ne se règlent pas ici : Odoo ne
                # lit de ce dictionnaire que `margin-top`, `margin-bottom`,
                # `header-spacing` et `dpi`. Les marges latérales viennent du
                # format de papier, qu'on ne fournit pas — elles sont donc
                # posées dans la feuille de style du gabarit.
                # Le document est autonome : aucune ressource externe à
                # charger. Couper l'accès réseau évite que wkhtmltopdf ne
                # tente une résolution d'URL et n'échoue en
                # `ProtocolUnknownError`.
                "data-report-margin-bottom": 10,
            },
        )
