# -*- coding: utf-8 -*-
# Copyright 2026 Expodo (https://expodo.fr)
# License LGPL-3
{
    "name": "Report Annotations for Community",
    "summary": "Annotate financial statement lines in Odoo Community: dated "
               "explanations attached to a line and a period",
    "description": """
Report Annotations for Community
================================

A financial statement answers “how much” and never “why”. Yet “why” is what gets
asked at the year-end review: why purchases doubled in March, why that provision,
why that gap with last year.

With nowhere to write it, the answer lives in an email, a notebook, or the memory
of whoever made the entry. It is lost at exactly the moment it is needed — a year
later, in front of somebody else.

Dated annotations
-----------------

An annotation applies to a line **and to a period**. An explanation true at 31
December 2025 is no longer true at 31 December 2026, and showing it on the new
year would be worse than showing nothing: the reader would believe the explanation
is current.

Annotations show on statements whose period *overlaps* theirs, not only on an
exact match. A note written for the first quarter belongs on the annual statement
too, because it explains part of what that statement shows. Requiring identical
dates would make the note vanish as soon as the view changes — which is when it is
most wanted.

Design
------

The annotations enrich the engine's output; they do not enter the engine. A note
sits beside a figure and has no business in its computation. Letting it in would
create a path by which a comment could one day change an amount.

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
        "security/expodo_account_annotation_rules.xml", "views/annotation_views.xml"],
    "installable": True,
    "application": False,
    "auto_install": False,
}
