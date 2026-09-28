# -*- coding: utf-8 -*-
# Copyright 2026 Expodo (https://expodo.fr)
# License LGPL-3
"""Assistant de production du FEC."""

from odoo import _, api, fields, models
from odoo.exceptions import UserError
from odoo.tools.binary import BinaryBytes


class ExportFec(models.TransientModel):
    _name = "expodo.fec.export"
    _description = "Export the French accounting entries file (FEC)"

    company_id = fields.Many2one(
        "res.company", string="Company", required=True,
        default=lambda self: self.env.company,
    )
    date_from = fields.Date(
        string="From", required=True,
        default=lambda self: self._defaut_debut_exercice(),
        help="First day of the period covered by the file. Defaults to the "
             "start of the current fiscal year, as configured on the company.",
    )
    date_to = fields.Date(
        string="To", required=True,
        default=lambda self: self._defaut_fin_exercice(),
        help="Last day of the period. The file name is built from this date, "
             "as required by the 29 July 2013 order.",
    )
    file_name = fields.Char(string="File name", readonly=True)
    file_data = fields.Binary(string="File", readonly=True)
    line_count = fields.Integer(string="Entry lines", readonly=True)
    warning = fields.Text(string="Warning", readonly=True)

    # ------------------------------------------------------------------
    # Valeurs par défaut
    # ------------------------------------------------------------------

    @api.model
    def _bornes_exercice(self):
        """Bornes de l'exercice en cours, selon le réglage de la société.

        On délègue à `compute_fiscalyear_dates` plutôt que de supposer
        l'année civile : l'exercice ne commence pas le 1er janvier partout,
        et un FEC couvrant la mauvaise période est refusé.
        """
        societe = self.env.company
        return societe.compute_fiscalyear_dates(fields.Date.context_today(self))

    @api.model
    def _defaut_debut_exercice(self):
        return self._bornes_exercice()["date_from"]

    @api.model
    def _defaut_fin_exercice(self):
        return self._bornes_exercice()["date_to"]

    # ------------------------------------------------------------------
    # Production
    # ------------------------------------------------------------------

    def action_generate(self):
        """Produit le fichier et le rend téléchargeable."""
        self.ensure_one()

        if self.date_from > self.date_to:
            raise UserError(_("The start date must precede the end date."))

        generateur = self.env["expodo.fec.generator"]
        contenu = generateur.generer(self.company_id, self.date_from, self.date_to)
        nom = generateur.nom_fichier(self.company_id, self.date_to)

        # L'encodage est explicite. Un FEC en UTF-8 est admis par l'arrêté,
        # mais le déclarer ici évite qu'un changement d'environnement ne
        # produise silencieusement du Latin-1 sur une machine et de l'UTF-8
        # sur une autre — deux fichiers différents pour la même comptabilité.
        octets = contenu.encode("utf-8")

        avertissements = self._controler(nom)

        self.write({
            "file_name": nom,
            # Odoo 20 : un champ binaire recoit le contenu brut enveloppe
            # dans `BinaryBytes`, et non plus du base64 en octets.
            "file_data": BinaryBytes(octets, filename=nom),
            # Le nombre de lignes exclut l'en-tête.
            "line_count": contenu.count("\r\n") - 1,
            "warning": "\n".join(avertissements) or False,
        })

        return {
            # Le titre est repris du menu. Sans lui, Odoo intitule la
            # fenêtre « Odoo » : l'utilisateur clique sur « FEC Export »,
            # la fenêtre s'ouvre avec ce titre, puis le perd dès la première
            # action et il ne sait plus dans quel assistant il se trouve.
            "name": _('FEC Export'),
            "type": "ir.actions.act_window",
            "res_model": self._name,
            "res_id": self.id,
            "view_mode": "form",
            "target": "new",
        }

    def _controler(self, nom_fichier):
        """Contrôles de bon sens, signalés sans bloquer.

        Aucun de ces points ne rend le fichier inexploitable, et bloquer la
        production serait pire que la laisser aboutir : une entreprise qui
        doit remettre son FEC sous quinze jours préfère un fichier imparfait
        qu'elle corrigera à un refus de l'outil.
        """
        self.ensure_one()
        avertissements = []

        if nom_fichier.startswith("000000000"):
            avertissements.append(_(
                "No company registration number found: the file is named with "
                "a placeholder SIREN. Set the company registry (SIRET) before "
                "handing the file over."))

        if self.company_id.account_fiscal_country_id.code != "FR":
            avertissements.append(_(
                "The company's fiscal country is not France. The FEC is a "
                "French requirement; check that this is what you intend."))

        brouillons = self.env["account.move"].search_count([
            ("company_id", "=", self.company_id.id),
            ("state", "=", "draft"),
            ("date", ">=", self.date_from),
            ("date", "<=", self.date_to),
        ])
        if brouillons:
            avertissements.append(_(
                "%(count)s draft entries in this period are excluded from the "
                "file, as required. Post them first if they belong to the "
                "period.", count=brouillons))

        avertissements += self._anomalies_comptables()
        return avertissements

    def _anomalies_comptables(self):
        """Traduit en messages les anomalies relevées sur les données.

        Chacune est formulée avec ce qu'il faut faire, pas seulement ce qui
        ne va pas. Un avertissement qu'on ne sait pas traiter est un
        avertissement qu'on ignore.
        """
        self.ensure_one()
        messages = []
        releves = self.env["expodo.fec.generator"].anomalies(
            self.company_id, self.date_from, self.date_to)

        for nature, details, nombre in releves:
            if nature == "classes_8_9":
                messages.append(_(
                    "%(count)s entry lines use class 8 or 9 accounts "
                    "(%(codes)s), which the tax authority excludes from the "
                    "FEC. Odoo creates 999xxx accounts as a fallback for "
                    "exchange differences when the installed chart names none. "
                    "Remap them to their French chart equivalents — 666 and "
                    "766 for exchange differences — and repost. The lines are "
                    "kept in the file: removing them would unbalance it, which "
                    "is a heavier objection than the account class.",
                    count=nombre, codes=", ".join(details)))
            elif nature == "hors_periode":
                messages.append(_(
                    "%(count)s entry lines are dated before the start of the "
                    "period. Check the period bounds.", count=nombre))
            elif nature == "sans_a_nouveaux":
                messages.append(_(
                    "No opening entry is recorded on the company. Opening "
                    "entries must head the file and carry forward the previous "
                    "year's balances; their absence is one of the anomalies the "
                    "tax authority identifies immediately."))

        messages.append(_(
            "Validation dates are set to the accounting date, as Odoo keeps no "
            "separate posting timestamp. Produce the file at the time of the "
            "year-end close, not months later, so that these dates stay "
            "consistent with the return you filed."))
        return messages
