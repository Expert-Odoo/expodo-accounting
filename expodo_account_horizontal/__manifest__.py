# -*- coding: utf-8 -*-
# Copyright 2026 Expodo (https://expodo.fr)
# License LGPL-3
{
    "name": "Horizontal Groups for Community",
    "summary": "Split financial statement columns by a second dimension in "
               "Odoo Community: establishment, activity, journal",
    "description": """
Horizontal Groups for Community
===============================

A financial statement reads time left to right: one period, sometimes two when
comparing. A horizontal group adds a second dimension — by establishment, by
activity, by journal — and answers a question the statement alone cannot ask:
*where does this total come from?*

A consolidated profit and loss showing two hundred thousand euros of staff costs
does not say whether they come from the workshop or from head office. Split by
establishment, it says so on the same page.

How it is built
---------------

The report engine already computes several sets of columns — that is how period
comparison works. A horizontal group simply multiplies those sets by its own
rules, each carrying a filter.

None of the computation is rewritten. That was the only acceptable option:
duplicating the aggregation logic to add a dimension would have produced two
calculation paths, which would eventually diverge — and a statement whose two
columns disagree is worse than a statement with no columns.

The filter adds to the common domain rather than replacing it. A split column is
still bound by the same dates, the same journal filter and the same entry states
as the rest of the statement; otherwise two columns on one page would not cover
the same thing.

Built and maintained by Expodo — https://expodo.fr
    """,
    "version": "20.0.1.0.0",
    "category": "Accounting/Accounting",
    "license": "LGPL-3",
    "author": "Expodo",
    "website": "https://expodo.fr",
    "support": "support@expodo.fr",
    "depends": ["expodo_account_reports"],
    "data": ["security/ir.model.access.csv",
        "security/expodo_account_horizontal_rules.xml", "views/horizontal_group_views.xml"],
    "installable": True,
    "application": False,
    "auto_install": False,
}
