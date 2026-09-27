# Tests hors instance

Ces tests portent sur le paquet `engine/`, qui n'importe aucun module Odoo.
Ils s'exécutent sans base de données, sans serveur et en quelques
millisecondes :

    python3 -m unittest discover -s tests_offline -t .

Ils sont volontairement **hors du dossier `tests/`** : Odoo importe ce dernier
lors de ses propres exécutions, et un import d'`odoo` dans `tests/__init__.py`
rendrait cette suite inexécutable en dehors d'une instance. Les deux suites
sont complémentaires et ne doivent pas se gêner.

Ce qui est couvert ici : grammaire des formules et sous-formules, graphe de
dépendances, sélection des comptes par préfixe et sens de solde, tranches
d'ancienneté. Ce qui relève de `tests/` : requêtes SQL, droits d'accès,
sérialisation, identités comptables.
