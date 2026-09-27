=====================================
Accounting Reports for Odoo Community
=====================================

.. |badge1| image:: https://img.shields.io/badge/licence-LGPL--3-blue.png
.. |badge2| image:: https://img.shields.io/badge/Odoo-19.0-875A7B.png

|badge1| |badge2|

Rapports financiers interactifs pour Odoo 19 Community : balance, grand livre,
balances âgées, journaux, écritures ouvertes et **la déclaration de taxes de
votre pays**.

Le module ne dépend d'aucune localisation. Il exécute la déclaration déclarée
par la localisation installée, quelle qu'elle soit.

Le problème
===========

Odoo 19 Community contient déjà **la définition** des rapports comptables.
Le modèle ``account.report`` et ses satellites sont dans le module ``account``,
et chaque localisation y déclare sa déclaration de taxes — la CA3 française
compte 132 lignes et 349 expressions, présentes dans toute base française.

Ce qui manque en Community, c'est **la couche qui exécute ces définitions**.
Elle est restée dans l'édition Enterprise. Résultat : votre déclaration de
taxes est physiquement dans votre base, et totalement inexploitable.

C'est vrai quel que soit votre pays. La déclaration française compte 132
lignes, la belge, la néerlandaise, la suisse, l'autrichienne et l'émirienne
sont déclarées de la même façon — et toutes sont inertes sans moteur.

L'approche
==========

Ce module fournit la couche manquante. Il ne redéfinit aucun rapport en
Python : il exécute les définitions déclaratives déjà présentes.

Trois conséquences directes :

* **Toute localisation fonctionne sans code supplémentaire.** Validé sur la
  France, la Belgique, les Pays-Bas, la Suisse, l'Autriche et les Émirats.
* **Les montées de version sont mécaniques**, le modèle de données étant
  maintenu par Odoo lui-même.
* **Ajouter un rapport ne demande pas de développement**, seulement une
  définition XML.

Rapports fournis
================

========================================  =============================================
Rapport                                   Particularité
========================================  =============================================
Bilan                                     Au référentiel du pays de la société
Compte de résultat                        Recomposition contrôlée par une ligne d'écart
Balance générale                          Débit, crédit, solde, dépliable par compte
Grand livre                               Deux niveaux : compte puis écriture
Grand livre auxiliaire                    Par partenaire
Écritures ouvertes                        Non lettrées uniquement
Balance âgée clients                      Six tranches d'ancienneté
Balance âgée fournisseurs                 Six tranches d'ancienneté
Journaux                                  Par journal puis par pièce
Livre de banque / Livre de caisse         Filtrés par type de journal
Déclaration de taxes                      Celle de votre localisation, résolue selon le pays de la société
========================================  =============================================

États financiers par pays
=========================

Le bilan et le compte de résultat dépendent du référentiel comptable national :
leurs regroupements n'ont pas d'équivalent universel. Le module en fournit
plusieurs présentations et retient celle du pays de la société, sans
configuration.

=================================  ====================================
Référentiel                        Présentations
=================================  ====================================
Universel                          Bilan, compte de résultat
France                             Bilan, compte de résultat et soldes
                                   intermédiaires de gestion au format du
                                   plan comptable général
SYSCOHADA                          Bilan et compte de résultat, reconnus
                                   sur le plan comptable de la société
=================================  ====================================

Lorsqu'un état existe en plusieurs présentations, un sélecteur en en-tête
permet de passer de l'une à l'autre sans quitter l'écran.

D'autres référentiels suivront. En ajouter un ne demande aucun développement :
uniquement une définition XML de ses regroupements. Les contributions sont
bienvenues.

Fonctionnalités
===============

* Rapports **interactifs** : dépliage à la demande, recherche, filtres de
  période, comparaison avec la période ou l'exercice précédent.
* **Chaque ligne se déplie sur les comptes qui la composent**, du bilan au
  tableau de flux. Un test automatisé vérifie sur l'ensemble des états que le
  détail d'une ligne somme exactement à cette ligne.
* **Drill-down** : un clic sur un montant ouvre les écritures qui le
  composent. La somme des écritures affichées égale exactement le montant
  cliqué — c'est vérifié par un test automatisé.
* **Exports PDF et XLSX**, montants écrits en numérique et non en texte.
* Filtres par journal, par partenaire, écritures comptabilisées ou toutes,
  multi-société.

Contrôles intégrés
==================

Le bilan et le compte de résultat affichent leur propre ligne de contrôle :

* **Bilan** : écart actif / passif, qui doit être nul.
* **Compte de résultat** : écart entre le total recomposé par rubrique et le
  solde global des classes 6 et 7.

Ces lignes existent parce qu'une rubrique oubliée dans un état financier ne
produit aucune erreur : elle donne un chiffre faux, plausible, que personne ne
remarque avant le contrôle. Rendre l'écart visible dans le rapport lui-même est
le seul moyen fiable de s'en apercevoir.

Installation
============

Community uniquement. Le module ne requiert aucune bibliothèque externe autre
que ``xlsxwriter``, déjà présent dans une installation Odoo standard, et
**ne dépend d'aucune localisation** : l'installer n'impose aucun plan
comptable.

::

    Applications → Mettre à jour la liste → « Accounting Reports for Odoo Community »

Les rapports apparaissent sous **Comptabilité → Rapports Expodo**.

Droits d'accès
==============

L'accès est réservé aux utilisateurs disposant d'un droit en comptabilité
(lecture seule, facturation, utilisateur ou administrateur). Cela vaut aussi
pour le téléchargement des exports.

Qualité
=======

* 69 tests unitaires sur la grammaire des formules, exécutables sans base de
  données.
* 16 tests d'intégration Odoo, dont les identités comptables et le contrôle
  que le nombre de requêtes SQL ne suit pas la taille du rapport.
* Moteur validé contre les déclarations de taxes réelles de six localisations.

Limites connues
===============

* Les cases Cerfa de la liasse fiscale 2050-2059 ne sont pas fournies. Le
  bilan et le compte de résultat au format PCG en constituent la matière
  première, mais un formulaire fiscal révisé chaque année n'a pas sa place
  dans un module gratuit : non maintenu, il ne serait pas incomplet, il serait
  faux.
* Immobilisations, budgets, rapprochement bancaire et relances ne sont pas
  couverts ; ils relèvent de versions ultérieures.
* La consolidation multi-société avec devises de consolidation n'est pas gérée.
* Le bilan et le compte de résultat ne sont fournis que pour la France à ce
  jour. Les autres rapports, eux, fonctionnent partout.
* La localisation française d'Odoo ne fournit pas de taxes de vente à taux
  réduit : seuls 20 % et les exonérations sont livrés. Les entreprises
  concernées doivent créer leurs taxes et leur affecter les bonnes grilles de
  déclaration.

Crédits
=======

Auteur et mainteneur : `Expodo <https://expodo.fr>`_

Contact : support@expodo.fr

Ce module est distribué sous licence LGPL-3. Il est développé à partir de
sources publiques exclusivement : le modèle ``account.report`` de l'édition
Community, les définitions XML des localisations, et la documentation
développeur publique d'Odoo.
