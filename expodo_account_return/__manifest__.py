# -*- coding: utf-8 -*-
# Copyright 2026 Expodo (https://expodo.fr)
# License LGPL-3
{
    "name": "Tax Returns for Community",
    "summary": "Track tax filings in Odoo Community: periods, checks, filing, "
               "payment, and period locking",
    "description": """
Tax Returns for Community
=========================

A report is consulted, re-run, and changes when the entries change. A return is
an **act**: filed on a date, for a period, engaging whoever signs it. What it
contains should no longer move afterwards.

Odoo Community gives the reports. It gives nothing to track the filings, and that
is where the trouble starts: nobody knows which period has been declared, nor
whether an entry crept in afterwards.

The lifecycle
-------------

    Draft     the period is running or has just closed
    Checked   the mechanical verifications have run
    Filed     the return has gone out, the period is locked
    Paid      the settlement is done

Locking on filing
-----------------

The important part. What has been declared should no longer move: an entry posted
after filing makes the books and the return disagree, and nothing signals it —
until the audit.

The lock only ever moves forward. Pushing it back would reopen periods already
declared, and an entry could then slip in unnoticed.

Checks
------

They do not replace an accountant's review. They catch the mechanical mistakes:
draft entries inside the period, an earlier return still unfiled, a deadline
already passed, a period the lock date already covers.

Those are the ones that go unnoticed precisely because they do not look like
mistakes.

Reopening
---------

A filed return can go back to draft to correct a filing reference or an amount,
and the lock stays where it is: reopening the record does not mean reopening the
period. A paid return cannot be reopened at all — a correction is filed as a new
return, which is what the tax authority expects to see.

Built and maintained by Expodo — https://expodo.fr
    """,
    "version": "20.0.1.0.0",
    "category": "Accounting/Accounting",
    "license": "LGPL-3",
    "author": "Expodo",
    "website": "https://expodo.fr",
    "support": "support@expodo.fr",
    "depends": ["account", "mail"],
    "data": [
        "security/ir.model.access.csv",
        "security/expodo_account_return_rules.xml",
        "views/return_views.xml",
    ],
    "installable": True,
    "application": False,
    "auto_install": False,
}
