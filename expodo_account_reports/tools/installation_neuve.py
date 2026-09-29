# -*- coding: utf-8 -*-
# Copyright 2026 Expodo (https://expodo.fr)
# License LGPL-3
"""Installation neuve de la suite, sur une base vierge.

Usage, depuis le conteneur Odoo :

    python3 tools/installation_neuve.py

Pourquoi ce contrôle existe
===========================

La suite de tests s'exécute sur une base où les modules sont déjà installés
et mis à jour. L'installation est un chemin distinct : l'ordre de chargement
des données et les contraintes évaluées à la création n'y jouent pas de la
même façon. Un domaine parfaitement valide à l'usage a déjà empêché le
module de s'installer, parce qu'une contrainte du coeur le vérifiait avant
que le module n'ait pu résoudre ses jetons. La suite était verte, et une
installation neuve échouait.

C'est aussi le seul chemin que vit celui qui télécharge le module : pour
lui, l'installation **est** le premier contact.

Ce qui est contrôlé
===================

1. L'installation aboutit, code de retour nul.
2. Le journal ne porte aucune erreur imputable à la suite.
3. Chaque module installé est bien à l'état ``installed``.
4. Les états s'ouvrent sur une base sans la moindre écriture, cas qu'aucun
   test ne couvre puisque la base de test en porte des milliers.

Le bruit connu
==============

Odoo produit deux messages reStructuredText à l'installation, « Unexpected
indentation » et « Block quote ends without a blank line ». Ils viennent de
la description du module ``mail`` d'Odoo, aux lignes 38 et 43, et non de la
suite. Ils sont filtrés nommément plutôt qu'ignorés en bloc : un filtre trop
large finit par masquer ce qu'il devait montrer.
"""

import os
import subprocess
import sys

BASE = "db_installation_neuve"
MODULES = "expodo_accounting,l10n_fr_account"
MOTIFS = ("ERROR", "CRITICAL", "Traceback")
IGNORES = (
    "option --without-demo",
    "missing --http-interface",
    "Unexpected indentation",
    "Block quote ends without a blank line",
)
ETATS = ("report_bilan_fr", "report_balance_sheet", "report_grand_livre_fr",
         "report_balance_fr", "report_executive_summary")


def psql(commande):
    subprocess.run(
        ["psql", "-h", "db", "-U", "odoo", "-d", "postgres", "-c", commande],
        capture_output=True, text=True, env=dict(os.environ, PGPASSWORD="odoo"))


CONTROLE = """
import odoo
from odoo.api import Environment
odoo.tools.config.parse_config(["-c", "/etc/odoo/odoo.conf"])
reg = odoo.modules.registry.Registry(%(base)r)
with reg.cursor() as cr:
    env = Environment(cr, 1, {})
    modules = env["ir.module.module"].search([("name", "=like", "expodo_%%")])
    non_installes = [m.name for m in modules if m.state != "installed"]
    print("modules:%%s" %% len(modules))
    print("non_installes:%%s" %% non_installes)
    for xmlid in %(etats)r:
        rapport = env.ref("expodo_account_reports." + xmlid, raise_if_not_found=False)
        if not rapport:
            print("manquant:%%s" %% xmlid)
            continue
        donnees = rapport.expodo_get_report_data({})
        print("etat:%%s:%%s" %% (xmlid, len(donnees["lines"])))
    cr.rollback()
"""


def main():
    psql("DROP DATABASE IF EXISTS %s;" % BASE)
    psql("CREATE DATABASE %s;" % BASE)
    installation = subprocess.run(
        ["odoo", "-c", "/etc/odoo/odoo.conf", "-d", BASE, "-i", MODULES,
         "--stop-after-init", "--log-level=warn",
         # Ports distincts : le serveur de développement tourne dans le même
         # conteneur et occupe les ports par défaut.
         "--http-port", "8077", "--gevent-port", "8076"],
        capture_output=True, text=True, timeout=3600)
    journal = (installation.stdout or "") + (installation.stderr or "")
    anomalies = [
        ligne for ligne in journal.splitlines()
        if any(motif in ligne for motif in MOTIFS)
        and not any(bruit in ligne for bruit in IGNORES)
    ]

    print("installation : code %s" % installation.returncode)
    for ligne in anomalies[:20]:
        print("   ", ligne[:190])

    controle = subprocess.run(
        ["python3", "-c", CONTROLE % {"base": BASE, "etats": ETATS}],
        capture_output=True, text=True, cwd="/tmp", timeout=900)
    for ligne in (controle.stdout or "").splitlines():
        if ligne.startswith(("modules:", "non_installes:", "etat:", "manquant:")):
            print("   ", ligne)

    echec = (installation.returncode != 0 or anomalies
             or "non_installes:[]" not in controle.stdout
             or "manquant:" in controle.stdout)
    print("resultat :", "ECHEC" if echec else "OK")
    psql("DROP DATABASE IF EXISTS %s;" % BASE)
    return 1 if echec else 0


if __name__ == "__main__":
    sys.exit(main())
