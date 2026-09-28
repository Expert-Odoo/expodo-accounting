# -*- coding: utf-8 -*-
# Copyright 2026 Expodo (https://expodo.fr)
# License LGPL-3
{
    "name": "Accounting for Community",
    "summary": "Accounting Community: financial statements, tax returns, "
               "year-end closing, assets, reconciliation, budgets and "
               "follow-up. Balance sheet, profit and loss, general ledger, "
               "trial balance and aged balance for Odoo Community.",
    "description": """
Accounting for Community
========================

Install this module to get the complete suite.

It carries no code of its own: it declares the modules below as dependencies,
so a single installation brings them all in. Each one remains a separate module
afterwards, and can be removed on its own if you do not need it.

Financial reporting
-------------------
Balance sheet, profit and loss, cash flow, trial balance, general and partner
ledgers, aged balances, day book, journals, and your country's tax return —
interactive, with one-click access to the entries behind every figure.

Year-end
--------
Closing entry and opening balances, prepaid expenses and deferred income,
currency revaluation, irregular fiscal years, and the French entries file (FEC).

Day-to-day
----------
Manual and automatic reconciliation, fixed assets and depreciation, budgets
against actuals, customer follow-up, tax return tracking.

Analysis
--------
Statement annotations, horizontal groups to split columns by establishment or
activity, VAT groups across companies.

Built and maintained by Expodo — https://expodo.fr
    """,
    "version": "19.0.1.0.11",
    "category": "Accounting/Accounting",
    # Vignette de la fiche App Store. Sans cette clé, le module
    # est le seul de la suite à n'en avoir aucune.
    "images": ["static/description/banner.png"],
    "license": "LGPL-3",
    "author": "Expodo",
    "website": "https://expodo.fr",
    "support": "support@expodo.fr",
    "depends": [
        "expodo_account_reports",
        "expodo_account_assets",
        "expodo_account_fec",
        "expodo_account_closing",
        "expodo_account_reconcile",
        "expodo_account_fiscal_year",
        "expodo_account_deferred",
        "expodo_account_revaluation",
        "expodo_account_return",
        "expodo_account_tax_unit",
        "expodo_account_budget",
        "expodo_account_followup",
        "expodo_account_annotation",
        "expodo_account_horizontal",
    ],
    "data": [],
    "installable": True,
    "application": True,
    "auto_install": False,
}
