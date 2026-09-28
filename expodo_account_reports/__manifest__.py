# -*- coding: utf-8 -*-
# Copyright 2026 Expodo (https://expodo.fr)
# License LGPL-3
{
    # 24 caractères, sous la limite de 25 des règles vendeur d'Odoo, et
    # exactement la formule que les utilisateurs recherchent.
    "name": "Accounting for Community: Financial Reports",
    "summary": "Financial reports for Odoo Community: Balance Sheet, Profit and Loss, "
               "General Ledger, Trial Balance, Aged Balance, Partner Ledger, "
               "Journals, Day Book, Open Items, Cash Flow Statement, Tax Report, "
               "VAT Declaration, SYSCOHADA, PDF and "
               "Excel export",
    "description": """
Accounting for Community
========================

Interactive financial reports for Odoo Community: trial balance, general
ledger, partner ledger, open items, aged balances, journals, bank and cash
books, day book, cash flow statement, and **your country's tax return**.

The problem
-----------

Odoo Community already ships the *definition* of the accounting reports.
The ``account.report`` model and its satellites are part of the ``account``
module, and every localisation declares its own tax return there — the French
one alone is 132 lines and 349 expressions, present in every French database.

What Community does not ship is the layer that *executes* those definitions.
It stayed in the Enterprise edition. The result: your tax return is physically
in your database, and unusable.

The approach
------------

This module provides the missing layer. It does not redefine a single report
in Python: it runs the declarative definitions already present.

* **Every localisation works with no additional code.** The engine is
  validated against the real tax returns of ten countries — France, Belgium,
  the Netherlands, Switzerland, Austria, Germany, Spain, the United States,
  the United Arab Emirates and Senegal — covering thirty-five statutory
  returns, the eight Spanish models included.
* **No localisation dependency.** Installing this module imposes no chart of
  accounts.
* **Version upgrades are mechanical**, since the data model is maintained by
  Odoo itself.

Balance sheet and profit and loss work everywhere too: they group by account
type, a classification Odoo assigns identically in every localisation. Optional
localisation packs add the statutory presentation your accountant expects — the
French pack follows the *plan comptable général*, and a SYSCOHADA pack covers
the seventeen OHADA member states with their own balance sheet and their
normalised cascade of intermediate management balances — but the reports are
correct and usable without them.

Languages
---------

Interface and statement headings are translated into French, Spanish, German,
Dutch, Italian and Brazilian Portuguese. Right-to-left languages are supported:
PDF exports follow the reading direction of the rendering language, so an
Arabic statement reads right to left rather than in Western column order.

Note on adding a language to an existing database: activate it, load its
translations, **then restart the server**. Skipping either step leaves parts of
the interface in English with no error message.

Built-in checks
---------------

The balance sheet displays its own assets-equals-liabilities gap; the profit
and loss statement displays the gap between its rebuilt total and the global
balance of expense and revenue accounts. Both must be zero.

Those lines exist because a forgotten section in a financial statement raises
no error: it produces a wrong, plausible figure that nobody notices before the
audit.

Quality
-------

* 80 unit tests on the formula grammar, runnable without a database.
* 124 integration tests, including the accounting identities, a check that the
  SQL query count does not follow report size, and a test that every tax return
  of the installed localisation renders — not merely the first one.
* An independent verification tool that recomputes each tax return box without
  using the engine, and cross-checks tax grid configuration.

Known limits
------------

Stated plainly, because a limit discovered after installation costs more than a
limit read beforehand:

* The statutory groupings of the French and SYSCOHADA packs were built from the
  published standards. They have not yet been reviewed by a chartered
  accountant of the relevant jurisdiction.
* Figures have been reconciled against generated datasets, not yet against a
  filed return.
* Zakat and Hijri-calendar reporting are out of scope.
* SYSCOHADA statements are listed alongside the universal ones rather than
  replacing them automatically, because Odoo binds a report to a single country
  and SYSCOHADA spans seventeen.

Built and maintained by Expodo — https://expodo.fr
    """,

    "version": "19.0.1.0.7",
    "category": "Accounting/Accounting",
    "license": "LGPL-3",
    "author": "Expodo",
    "website": "https://expodo.fr",
    "support": "support@expodo.fr",

    # Aucune dépendance de localisation. Le module exécute la déclaration de
    # taxes du pays installé, quel qu'il soit : dépendre de `l10n_fr_account`
    # imposerait le plan comptable français à tous les utilisateurs, ce qui
    # rendrait le module inutilisable partout ailleurs.
    "depends": [
        "account",
    ],

    "data": [
        "security/ir.model.access.csv",
        "data/reports_universal.xml",
        "data/report_cash_flow.xml",
        "data/report_cash_flow_direct.xml",
        "data/report_day_book.xml",
        # États nationaux : aucune dépendance de localisation, ils
        # n'emploient que des préfixes de comptes. Résolus à l'exécution
        # selon le pays de la société.
        "data/report_bilan_fr.xml",
        "data/report_resultat_fr.xml",
        "data/report_bilan_ohada.xml",
        "data/report_resultat_ohada.xml",
        "data/report_sig_fr.xml",
        "data/report_executive_summary.xml",
        "data/report_customer_statement.xml",
        "data/report_ec_sales_list.xml",
        "data/reports_groupes.xml",
        "data/reports_aged.xml",
        "report/report_pdf_templates.xml",
        "views/menus.xml",
    ],

    "assets": {
        "web.assets_backend": [
            "expodo_account_reports/static/src/scss/report.scss",
            "expodo_account_reports/static/src/js/report_action.js",
            "expodo_account_reports/static/src/xml/report_action.xml",
        ],
        # Parcours navigateur, chargé uniquement pendant les tests.
        "web.assets_tests": [
            "expodo_account_reports/static/tests/tour_report.js",
        ],
    },

    # Une seule fiche sur l'App Store : seul le module qui embarque la
    # suite porte `application`. Les autres sont des dépendances, livrées
    # dans le même téléchargement et invisibles comme applications.
    # C'est le schéma des kits comptables concurrents.
    "application": False,
    "installable": True,
    "auto_install": False,

    "images": ["static/description/banner.png"],

}
