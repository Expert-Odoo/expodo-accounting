# -*- coding: utf-8 -*-
# Copyright 2026 Expodo (https://expodo.fr)
# License LGPL-3
{
    "name": "Year-End Closing for Community",
    "summary": "Year-end closing and opening entries for Odoo Community: "
               "zero profit-and-loss accounts, carry the result, bring balances forward",
    "description": """
Year-End Closing for Community
==============================

Two operations Odoo does not perform, and that are often confused.

**Closing** zeroes the profit-and-loss accounts and carries their difference to
the result account. Afterwards, classes 6 and 7 are empty and the profit or loss
sits on the balance sheet.

**Opening entries** bring the balance sheet balances forward to the first day of
the next year. Without them the new year starts on empty books.

Odoo computes the current year's result on the fly for display. That is enough
to read a balance sheet; it produces no accounting entry. French practice
requires a real closing, and the entries file (FEC) requires opening entries at
the head of the file.

How it works
------------

* Closing and opening entries are created **as drafts** and reviewed before
  posting. A posted closing cannot be undone; doing it in one click would be a
  disservice.
* Every generated entry is checked for balance before anything is written. An
  out-of-balance closing does more damage than no closing at all.
* The engine recognises its own entries and refuses to close twice — a double
  closing halves the result with no warning at all.
* Opening balances are read **after** the closing is posted, so that the result
  carries forward. Reading before would leave the opening balance sheet short by
  exactly the result — a gap large enough to see, and usually blamed on a typo.
* The result account follows the local chart: 120000 or 129000 for a French
  company, otherwise the account typed “Unallocated Earnings”. The French codes
  are only looked up for a company whose fiscal country is France, because the
  same numbers mean something else elsewhere.
* The opening entry can be registered on the company, which is what the FEC
  export reads to place opening entries at the head of the file.

Checks before posting
---------------------

The wizard reports draft entries in the period, entries already closed, and a
result account sitting on a class 8 or 9 fallback — which Odoo creates when the
installed chart names none, and which is also excluded from the FEC.

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
        "wizard/year_closing_views.xml",
        'security/ir.access.csv',
    ],
    "installable": True,
    "application": False,
    "auto_install": False,
}
