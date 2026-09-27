# -*- coding: utf-8 -*-
# Copyright 2026 Expodo (https://expodo.fr)
# License LGPL-3
{
    "name": "Deferred Expenses and Revenues for Community",
    "summary": "Prepaid expenses and deferred income for Odoo Community: "
               "spread by exact days, carried to the balance sheet at closing",
    "description": """
Deferred Expenses and Revenues for Community
============================================

An insurance premium paid on 1 October for twelve months is booked as an expense
that day. Nine tenths of it belong to the following year. Left alone, it gets two
results wrong at once: it weighs down the current year and lightens the next.

The principle of matching therefore requires the future part to leave the
profit-and-loss account for the balance sheet — 486 and 487 in the French chart —
and to come back at the opening of the next year.

Odoo Community has nothing for this. The feature stayed in the Enterprise
edition.

How it works
------------

Date a journal item — “this expense covers 1 October to 30 September” — and the
wizard computes, at a given closing date, the part belonging to the future and
produces the entry.

Pro rata by exact days
----------------------

Not by whole months. A monthly pro rata is simpler and defensible in some
frameworks, but it drifts on every contract that does not start on the first of
a month — which is most of them. A premium starting on 17 October would be
deferred as if it started on the 1st: sixteen days nobody could explain a year
later.

Both bounds count. A service running 1 to 31 January covers thirty-one days, not
thirty. The difference looks negligible over one month; across a multi-year
contract split into twelve deferrals, it shows.

Checks
------

Entries are created as drafts and previewed before posting. A deferral touches
two years at once: seeing it beforehand beats correcting it afterwards, since
correcting means reversing in both.

The wizard refuses to defer twice at the same date, and reports lines carrying a
start date but no end date — those cannot be spread and stay entirely in the
current year's result, silently.

Built and maintained by Expodo — https://expodo.fr
    """,
    "version": "19.0.1.0.0",
    "category": "Accounting/Accounting",
    "license": "LGPL-3",
    "author": "Expodo",
    "website": "https://expodo.fr",
    "support": "support@expodo.fr",
    "depends": ["account"],
    "data": [
        "security/ir.model.access.csv",
        "views/deferral_views.xml",
    ],
    "installable": True,
    "application": False,
    "auto_install": False,
}
