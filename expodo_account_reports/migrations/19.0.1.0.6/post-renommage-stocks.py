# -*- coding: utf-8 -*-
# Copyright 2026 Expodo (https://expodo.fr)
# License LGPL-3
"""Renommage de la ligne « Stocks » du résumé général.

La ligne lit tout l'actif circulant — taxe déductible, charges constatées
d'avance, autres créances — et non les seuls stocks. Le domaine est le bon
pour un besoin en fonds de roulement ; c'était le libellé qui mentait.

Une mise à jour de module ne réécrit pas une traduction déjà présente en
base : sans ce script, les installations existantes garderaient l'ancien
libellé sur une ligne dont le sens a changé, c'est-à-dire exactement le
défaut corrigé.

La signature attendue est ``(cr, version)`` : Odoo 19 refuse ``(env, ...)``
et interrompt le chargement du registre.
"""

import json

LIBELLES = {
    "en_US": "Inventory and other current assets",
    "fr_FR": "Stocks et autres créances",
    "fr_BE": "Stocks et autres créances",
    "fr_CA": "Stocks et autres créances",
    "fr_CH": "Stocks et autres créances",
    "es_ES": "Existencias y otros activos corrientes",
    "de_DE": "Vorräte und sonstige Forderungen",
    "nl_NL": "Voorraden en overige vorderingen",
    "it_IT": "Rimanenze e altri crediti",
    "pt_BR": "Estoques e outros ativos circulantes",
}

# Les libellés livrés jusqu'à la 19.0.1.0.5. Un texte absent de cette liste a
# été écrit par l'utilisateur : il est conservé tel quel.
ANCIENS = {"Inventory", "Stocks", "Existencias", "Vorräte", "Voorraden",
           "Rimanenze", "Estoques"}


def migrate(cr, version):
    cr.execute("""
        SELECT res_id FROM ir_model_data
         WHERE module = 'expodo_account_reports'
           AND name = 'exec_summary_stocks'
           AND model = 'account.report.line'
    """)
    trouve = cr.fetchone()
    if not trouve:
        return
    cr.execute("SELECT name FROM account_report_line WHERE id = %s", (trouve[0],))
    ligne = cr.fetchone()
    if not ligne:
        return
    courant = ligne[0]
    if not isinstance(courant, dict):
        courant = {"en_US": courant or ""}
    nouveau = {
        langue: (LIBELLES[langue]
                 if texte in ANCIENS and langue in LIBELLES else texte)
        for langue, texte in courant.items()
    }
    if nouveau == courant:
        return
    cr.execute("UPDATE account_report_line SET name = %s WHERE id = %s",
               (json.dumps(nouveau), trouve[0]))
