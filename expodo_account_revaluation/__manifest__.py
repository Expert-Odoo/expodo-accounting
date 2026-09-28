# -*- coding: utf-8 -*-
# Copyright 2026 Expodo (https://expodo.fr)
# License LGPL-3
{
    "name": "Currency Revaluation for Community",
    "summary": "Revalue foreign currency receivables and payables at closing "
               "in Odoo Community, with exchange difference entries",
    "description": """
Currency Revaluation for Community
==================================

A ten-thousand-dollar receivable booked at 0.92 is worth 9,200 euros on the
balance sheet. If the dollar closes at 0.95, the same receivable is worth 9,500.
The 300-euro difference exists; it is simply not realised yet, because the
customer has not paid.

Ignoring it means presenting a balance sheet at a stale rate.

What Community already does
---------------------------

**Realised** differences, at reconciliation: the invoice is settled and the gap
between the original rate and the settlement rate goes to exchange gain or loss.
The accounts sit on the company and the mechanism is complete.

What is missing is the **unrealised** revaluation of balances still open at
closing. That stayed in the Enterprise edition.

A French particularity
----------------------

French GAAP does not treat the two directions symmetrically.

An **unrealised loss** — a receivable losing value, a payable gaining — goes to
476 and additionally calls for a provision for risks (1515). An **unrealised
gain** goes to 477 and is *not* taken to profit: prudence forbids recognising a
profit that is not realised.

This module produces the conversion entry. It **reports** the provision without
creating it: the amount depends on hedging, on the overall position and on
offsetting between currencies — judgement that cannot be mechanised. Creating a
provision for the gross amount would be wrong in most cases.

Reversal
--------

The entry is reversed at the opening of the next period, and the reversal is
created at the same time rather than left for later. An exchange difference is a
snapshot at one date, not a gain or a loss; left standing it would double-count
with the realised difference recorded at settlement — and nobody remembers in
March that something had to be reversed in January.

Checks
------

The wizard reports a revaluation already posted at that date, unrealised losses
calling for a provision, and currencies with no recent rate — where the
conversion silently falls back on the last known rate, which looks correct and
is stale.

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
        "views/revaluation_views.xml",
    ],
    "installable": True,
    "application": False,
    "auto_install": False,
}
