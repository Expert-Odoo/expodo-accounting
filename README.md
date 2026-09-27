# Accounting for Community

Financial statements and accounting tools for **Odoo 19 Community**.

Odoo Community ships the accounting engine but keeps the report layer in
Enterprise. This suite fills that gap: balance sheet, profit and loss, trial
balance, general ledger, aged balances, tax return, and the surrounding tools
a bookkeeper needs to close a year.

Free, LGPL-3, no external service, no API key, no account to create.

## What you get

| Report | |
|---|---|
| Balance sheet | In your country's statutory layout where one exists |
| Profit and loss | Same |
| Cash flow statement | Indirect and direct method |
| Trial balance | Opening balance, movements, closing balance |
| General ledger | Account, then every entry, with date and partner |
| Partner ledger | Same, by partner |
| Aged receivable and payable | Six buckets, as of a chosen date |
| Open items | Unreconciled entries as of a date |
| Customer statement | One partner, every movement |
| Day book, journals, bank and cash books | |
| EC sales list | Intra-community sales |
| Tax return | The one your localisation declares |
| Executive summary | |

Every report unfolds by account or partner, every figure opens the entries
behind it, and everything exports to PDF and XLSX.

## Beyond the reports

Year-end closing and opening entries, fiscal years that are not calendar
years, assets and depreciation, deferred expenses and revenues, budgets,
customer follow-up levels, manual and automatic reconciliation, currency
revaluation, tax returns and tax units, report annotations, horizontal
groups, and the French FEC export.

## Presentations

A French company reads its balance sheet in the *plan comptable général*
layout. A West African one opens the same menu and reads a SYSCOHADA
presentation, in CFA francs. Neither installed anything extra: a selector in
the report header offers the presentations that the company's chart of
accounts makes meaningful.

## Install

Install **Accounting for Community** (`expodo_accounting`). It pulls in the
fourteen modules of the suite. Each one also installs on its own if you only
need part of it.

Requires Odoo 19 Community with the `account` module.

## Quality

The suite carries 461 automated tests: accounting identities, a check that
the SQL query count does not follow report size, every tax return of the
installed localisation rendered rather than merely the first, a browser
walkthrough of the report interface, and structural guards that read the XML
definitions as Odoo does at install time.

## Support

Questions, bug reports and custom work: [expodo.fr](https://expodo.fr)

## Licence

LGPL-3.
