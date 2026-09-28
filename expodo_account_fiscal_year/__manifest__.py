# -*- coding: utf-8 -*-
# Copyright 2026 Expodo (https://expodo.fr)
# License LGPL-3
{
    "name": "Fiscal Years for Community",
    "summary": "Declare irregular fiscal years in Odoo Community: first year, "
               "transition year, final year",
    "description": """
Fiscal Years for Community
==========================

Odoo derives fiscal years from a repeating pattern held on the company: a
closing day and month, twelve months each time. That covers ordinary years and
nothing else.

It cannot express:

* a **first year**, running from incorporation to the first close — rarely
  twelve months, often fifteen;
* a **transition year**, when a company changes its closing date to align with
  its group;
* a **final year** of a company being wound up.

Without a way to declare these, everything that relies on the fiscal year falls
back on the repeating pattern and covers the wrong dates. The failure is silent:
the reports render, they simply span the wrong period. A fifteen-month balance
sheet presented as a twelve-month one carries nothing to say so.

What this module does
---------------------

It adds explicit fiscal years and plugs them into ``compute_fiscalyear_dates``,
so that **everything reading a fiscal year benefits without knowing about it**:
default report periods, year-end closing, the period of the entries file (FEC).

Declared years take precedence over the pattern. Regular years need no record —
where nothing is declared, Odoo's own behaviour is untouched.

Overlapping years are refused. An overlap would make “which year contains 15
March?” depend on reading order, and data that changes meaning with reading
order is worse than missing data, because it looks right.

Built and maintained by Expodo — https://expodo.fr
    """,
    "version": "20.0.1.0.0",
    "category": "Accounting/Accounting",
    "license": "LGPL-3",
    "author": "Expodo",
    "website": "https://expodo.fr",
    "support": "support@expodo.fr",
    "depends": ["account"],
    "data": [
        "security/ir.model.access.csv",
        "security/expodo_account_fiscal_year_rules.xml",
        "views/fiscal_year_views.xml",
    ],
    "installable": True,
    "application": False,
    "auto_install": False,
}
