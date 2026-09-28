# -*- coding: utf-8 -*-
# Copyright 2026 Expodo (https://expodo.fr)
# License LGPL-3
{
    "name": "Reconciliation for Community",
    "summary": "Reconcile journal items in Odoo Community: manual reconciliation "
               "with write-off, and automatic matching by partner balance",
    "description": """
Reconciliation for Community
============================

Odoo Community already carries the whole reconciliation engine: ``reconcile()``,
partial and full matches, reconciliation models, exchange-difference handling.
None of that is specific to the Enterprise edition.

What is missing is the way in. There is no action, no wizard, no button in the
list views. Community even ships a server action called *Undo Reconciliation* —
so you can undo what you have no means of doing.

This module supplies the access, not the engine. It calls Odoo's own
``reconcile()`` and leaves exchange differences, cash-basis taxes and
consistency checks to the core, whose rules change from version to version.

Manual reconciliation
---------------------

Select journal items, then *Reconcile* from the action menu. The wizard shows
the totals and the remaining difference before doing anything.

When the items do not net to zero they are partially reconciled, or the
difference is written off to an account you choose. The write-off entry is dated
on the latest item of the selection: back-dating it would drop an entry into a
closed period and produce a validation date earlier than the entry — the first
anomaly a tax auditor looks for.

Automatic reconciliation
------------------------

Matches items that net to exactly zero on the same account and the same partner.

The rule is deliberately strict. Matching approximately — within a few cents, or
on label similarity — would reconcile more items and reconcile wrong ones. A
wrong reconciliation has to be undone item by item, and hides a genuinely unpaid
receivable in the meantime. Better to leave some work than to conceal some.

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
        "wizard/reconcile_wizard_views.xml",
    ],
    "installable": True,
    "application": False,
    "auto_install": False,
}
