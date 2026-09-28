# -*- coding: utf-8 -*-
# Copyright 2026 Expodo (https://expodo.fr)
# License LGPL-3
{
    "name": "Tax Units for Community",
    "summary": "VAT groups in Odoo Community: declare several companies "
               "together under one VAT number",
    "description": """
Tax Units for Community
=======================

In many member states, related companies may file a single VAT return for the
whole group. One company is designated as representative; its VAT number appears
on the return, and the group's consolidated operations are declared on it.

The scheme goes by different names — *unité TVA* in Belgium, *groupe TVA* in
France since 2023, *Organschaft* in Germany — and eligibility rules differ. What
they share, and what this module provides, is the **notion of a scope**: a set of
companies whose operations are declared together.

What it does not do
-------------------

It does not check eligibility — capital, economic and organisational links.
Those are matters of national law: they are established, not computed.

It does not eliminate intra-group operations either. In several schemes they
fall outside the scope of VAT, but not in all, and the rule depends on the
country and on the nature of the operation. Eliminating them by default would
produce a wrong return wherever they must appear — and it would be invisible,
since the total would stay plausible.

Both limits are stated rather than guessed at: a group return commits the
representative on behalf of every company in the scope.

Checks
------

The representative must belong to the scope it represents, and a company may
belong to only one unit per country. Two overlapping units would declare the
same operations twice, each return looking complete.

Built and maintained by Expodo — https://expodo.fr
    """,
    "version": "20.0.1.0.0",
    "category": "Accounting/Accounting",
    "license": "LGPL-3",
    "author": "Expodo",
    "website": "https://expodo.fr",
    "support": "support@expodo.fr",
    "depends": ["account"],
    "data": [ "views/tax_unit_views.xml", 'security/ir.access.csv'],
    "installable": True,
    "application": False,
    "auto_install": False,
}
