# -*- coding: utf-8 -*-
# Copyright 2026 Expodo (https://expodo.fr)
# License LGPL-3
{
    "name": "Customer Follow-up for Community",
    "summary": "Chase overdue customers in Odoo Community: graduated levels, "
               "overdue amounts computed live, reminders sent by selection",
    "description": """
Customer Follow-up for Community
================================

A follow-up is not an isolated reminder: it is a gradation. The first letter is a
polite nudge, the last a formal notice, and in between is whatever the business
decides. What matters is that the gradation is **the same for every customer**:
chasing a good payer harshly over one forgotten invoice costs more than the
invoice.

Levels trigger on how late the oldest unpaid instalment is. Not on the amount,
not on how long the customer has been with you — late is late, and exceptions in
the rule are the surest way to end up chasing nobody.

What is computed, and what is decided
-------------------------------------

The module computes the factual part: how much is due, since when, which level
that lateness reaches. It does not decide to send. Sending stays a deliberate act:
a phone call yesterday, an invoice under dispute, an agreed payment plan — none of
that is in the database, and a reminder that goes out despite a verbal agreement
costs more than the invoice it chases.

Hence the shape: a list of customers to chase, sorted by urgency, with the level
reached and a way into the detail. Sending happens by selection.

Excluded items
--------------

Odoo Community already carries a ``no_followup`` field on journal items, inherited
from older versions and used by nothing. The module honours it rather than adding
another: an invoice marked as not to be chased is marked for a reason, and that
reason was recorded once.

Built and maintained by Expodo — https://expodo.fr
    """,
    "version": "20.0.1.0.0",
    "category": "Accounting/Accounting",
    "license": "LGPL-3",
    "author": "Expodo",
    "website": "https://expodo.fr",
    "support": "support@expodo.fr",
    "depends": ["account", "mail"],
    "data": ["security/ir.model.access.csv",
        "security/expodo_account_followup_rules.xml", "views/followup_views.xml"],
    "installable": True,
    "application": False,
    "auto_install": False,
}
