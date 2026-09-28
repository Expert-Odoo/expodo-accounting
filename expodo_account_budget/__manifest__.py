# -*- coding: utf-8 -*-
# Copyright 2026 Expodo (https://expodo.fr)
# License LGPL-3
{
    "name": "Budgets for Community",
    "summary": "Budgets in Odoo Community: planned amounts per account, actual "
               "computed live, variance and time-prorated comparison",
    "description": """
Budgets for Community
=====================

A planned amount on its own says nothing. What is looked at is the gap: how much
has been spent against how much was planned, and how far into the period.

Each line carries an account, a period and a planned amount. The actual is
computed live from posted entries, and the variance with it. Nothing is frozen:
an entry posted today changes yesterday's actual, which is what a budget follow-up
is expected to do.

Why the actual is not stored
----------------------------

Storing it would mean recomputing on every entry, every cancellation, every date
change. One missed recomputation would produce a wrong and *stable* variance —
the worst of both worlds: a figure that looks reliable because it no longer moves.

Prorated comparison
-------------------

Comparing three months of spending with a yearly budget teaches nothing: the gap
always looks favourable. Each line therefore also shows the planned amount in
proportion to the elapsed part of the period, which is the only useful comparison
while the period is running.

Signs
-----

Planned amounts are entered as positive figures for both income and expense.
Asking the user to type a negative number for revenue is a reliable way of
collecting sign errors; the module handles the credit balance of income accounts
itself.

Drill-down
----------

Every line opens the entries behind its actual. A variance that cannot be opened
gets discussed indefinitely; being able to reach the entries ends the discussion
in thirty seconds.

Built and maintained by Expodo — https://expodo.fr
    """,
    "version": "19.0.1.0.1",
    "category": "Accounting/Accounting",
    "license": "LGPL-3",
    "author": "Expodo",
    "website": "https://expodo.fr",
    "support": "support@expodo.fr",
    "depends": ["account"],
    "data": ["security/ir.model.access.csv",
        "security/expodo_account_budget_rules.xml", "views/budget_views.xml"],
    "installable": True,
    "application": False,
    "auto_install": False,
}
