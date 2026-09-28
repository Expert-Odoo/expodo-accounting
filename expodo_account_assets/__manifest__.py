# -*- coding: utf-8 -*-
# Copyright 2026 Expodo (https://expodo.fr)
# License LGPL-3
{
    "name": "Accounting for Community: Assets",
    "summary": "Fixed asset management and depreciation for Odoo Community — "
               "depreciation board, prior depreciation on migrated assets, "
               "journal entries posted only on confirmation",

    "description": """
Accounting for Community: Assets
================================

Fixed asset management for Odoo Community: depreciation board, journal
entries, disposal.

Depreciation is a legal obligation for any company holding fixed assets, and
Odoo Community ships the accounts but nothing to depreciate them. This module
fills that gap.

What makes it different
-----------------------

**Nothing is ever posted automatically.** The board is computed and displayed,
but every instalment requires a human action. A missed instalment is caught up
in minutes; a wrong entry in a closed period is not.

**The board is verified before every entry.** Its total must equal the
depreciable base, no instalment can be negative, due dates must be strictly
increasing. An inconsistent board never reaches the ledger.

**Lock dates are checked before the entry is created**, not after. Odoo would
refuse to post, but the draft entry would already sit in the journal.

**Migrated assets are supported from the start.** A company moving to Odoo
already owns partly depreciated assets. The amount already recognised is
excluded from the board without changing the end date.

Rounding
--------

Instalments are derived from a rounded cumulative rather than rounded
individually. This guarantees both that the board sums exactly to the
depreciable base and that no instalment can be negative — a property verified
on 10,584 combinations of value, duration, periodicity, in-service date,
salvage value and prior depreciation.

Scope
-----

Straight-line depreciation only. Declining-balance is a national tax regime
with country-specific coefficients; a wrong rule is worse than no rule.

Companion module
----------------

**Accounting for Community** adds fifteen financial reports to Odoo Community:
balance sheet, profit and loss, cash flow statement, ledgers, aged balances and
your country's tax return. The two modules install independently.

Built and maintained by Expodo — https://expodo.fr
    """,

    "version": "20.0.1.0.0",
    "category": "Accounting/Accounting",
    "license": "LGPL-3",
    "author": "Expodo",
    "website": "https://expodo.fr",
    "support": "support@expodo.fr",

    "depends": [
        "account",
    ],

    "data": [
        "security/ir.model.access.csv",
        "security/expodo_asset_rules.xml",
        # L'assistant en premier : la vue formulaire des immobilisations
        # référence son action par `%(xmlid)d`, qui doit donc déjà exister.
        "wizard/expodo_asset_disposal_views.xml",
        "views/expodo_asset_views.xml",
        "views/menus.xml",
    ],

    # Une seule fiche sur l'App Store : seul le module qui embarque la
    # suite porte `application`. Les autres sont des dépendances, livrées
    # dans le même téléchargement et invisibles comme applications.
    # C'est le schéma des kits comptables concurrents.
    "application": False,
    "installable": True,
    "auto_install": False,
    "images": ["static/description/banner.png"],
}
