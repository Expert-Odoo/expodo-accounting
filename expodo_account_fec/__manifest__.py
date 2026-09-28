# -*- coding: utf-8 -*-
# Copyright 2026 Expodo (https://expodo.fr)
# License LGPL-3
{
    "name": "FEC Export for Community",
    "summary": "French accounting entries file (FEC) export for Odoo Community: "
               "statutory tax audit file, 18 fields, arrêté du 29 juillet 2013",
    "description": """
FEC Export for Community
========================

Produces the **Fichier des Écritures Comptables** required from every French
company under a tax audit.

The obligation
--------------

Article L. 47 A-I of the *Livre des procédures fiscales* requires any company
keeping computerised accounts to hand over a file of its accounting entries
when audited. The 29 July 2013 order fixes the format: eighteen fields, in a
prescribed order, as a flat file.

Failing to produce a compliant file is penalised under article 1729 D of the
tax code — €5,000 per financial year, before the consequences of accounts being
set aside.

The gap
-------

Odoo Community does not produce a FEC. The export stayed in the Enterprise
edition. A French company running Community therefore cannot answer a request
from the tax authority.

What this module does
---------------------

* The eighteen fields of the order, in the prescribed order, with the header
  line — a flat file without one is rejected whatever it contains.
* Dates as AAAAMMJJ, amounts with a decimal comma, tab-separated.
* File named ``<SIREN>FEC<AAAAMMJJ>.txt`` from the company registry.
* Posted entries only: a draft entry is not an accounting entry.
* Subsidiary account columns filled only for receivable and payable accounts.
* Foreign-currency columns filled only where the entry really is in another
  currency.
* The period defaults to the current fiscal year as configured on the company,
  which is not necessarily the calendar year.

The file is transcribed, never recomputed. Every field comes from data already
in the database. Automated audit tools recompute balances from the file and
compare them with the returns filed; any divergence between the file and the
accounts that produced it counts against the company.

Checks
------

The wizard reports, without blocking, what would weaken the file: a missing
company registration number, draft entries in the period, a company whose
fiscal country is not France.

Blocking would be worse than warning. A company that must hand over its file
within days is better served by an imperfect file it can fix than by a tool
that refuses.

Built and maintained by Expodo — https://expodo.fr
    """,
    "version": "20.0.1.0.0",
    "category": "Accounting/Accounting",
    "license": "LGPL-3",
    "author": "Expodo",
    "website": "https://expodo.fr",
    "support": "support@expodo.fr",

    # Aucune dépendance de localisation : le FEC se transcrit depuis les
    # écritures, qui existent quel que soit le plan comptable installé.
    # Dépendre de `l10n_fr_account` imposerait le plan français à une filiale
    # étrangère d'un groupe qui doit pourtant produire un FEC.
    "depends": ["account"],

    "data": [
        "security/ir.model.access.csv",
        "wizard/fec_export_views.xml",
    ],

    "installable": True,
    "application": False,
    "auto_install": False,
}
