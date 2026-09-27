# -*- coding: utf-8 -*-
# Tests d'intégration chargés par Odoo.
#
# `test_reports.py`  : comportements choisis et régressions nommées.
# `test_matrix.py`   : couverture systématique — chaque rapport croisé avec
#                      chaque commande. Les deux sont complémentaires : le
#                      premier dit pourquoi un cas compte, le second garantit
#                      qu'aucun rapport n'est oublié.
# `test_cycle.py`    : le chemin réel des données — facture, taxe, déclaration,
#                      encaissement, lettrage. Les autres fichiers partent
#                      d'écritures construites à la main ; aucune donnée d'un
#                      client n'arrive par ce chemin-là.
#
# Les tests du paquet `engine/` vivent dans `tests_offline/` : ils s'exécutent
# sans base ni serveur et ne doivent pas dépendre d'Odoo.
from . import test_reports
from . import test_matrix
from . import test_french_statements
from . import test_cycle
