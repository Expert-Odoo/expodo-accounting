# -*- coding: utf-8 -*-
# Copyright 2026 Expodo (https://expodo.fr)
# License LGPL-3
"""Renommage de la ligne « Position nette de trésorerie ».

Elle retranchait le besoin en fonds de roulement des disponibilités, alors
que ce besoin est déjà un solde net : une facture fournisseur impayée
améliorait la position, une créance client la dégradait. Corrigée, la ligne
additionne, et ce qu'elle donne est un fonds de roulement net, pas une
position de trésorerie. Le libellé suit.

Une mise à jour de module ne réécrit pas une traduction déjà en base.

Signature ``(cr, version)`` : Odoo 19 refuse toute autre et interrompt le
chargement du registre.
"""

import json

LIBELLES = {
    "en_US": "Net working capital",
    "fr_FR": "Fonds de roulement net",
    "fr_BE": "Fonds de roulement net",
    "fr_CA": "Fonds de roulement net",
    "fr_CH": "Fonds de roulement net",
    "es_ES": "Fondo de maniobra neto",
    "de_DE": "Nettoumlaufvermögen",
    "nl_NL": "Netto werkkapitaal",
    "it_IT": "Capitale circolante netto",
    "pt_BR": "Capital de giro líquido",
}

# Les libellés livrés jusqu'à la 19.0.1.0.6. Un texte absent de cette liste a
# été écrit par l'utilisateur : il est conservé tel quel.
ANCIENS = {"Net cash position", "Position nette de trésorerie",
           "Posición neta de tesorería", "Nettoliquidität",
           "Netto liquiditeitspositie", "Posizione finanziaria netta",
           "Posição líquida de caixa"}


def migrate(cr, version):
    cr.execute("""
        SELECT res_id FROM ir_model_data
         WHERE module = 'expodo_account_reports'
           AND name = 'exec_summary_position_nette'
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
