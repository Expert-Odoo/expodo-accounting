# -*- coding: utf-8 -*-
# Copyright 2026 Expodo (https://expodo.fr)
# License LGPL-3
"""Calcul du tableau d'amortissement.

Aucune dépendance Odoo : ce module ne manipule que des dates et des nombres,
ce qui le rend testable sans base ni serveur. C'est la même discipline que
pour le moteur de rapports, et pour la même raison — c'est ici qu'une erreur
coûte le plus cher, puisqu'elle finit en écritures comptables.

Règles retenues
===============

**Linéaire uniquement.** Le dégressif est un régime fiscal national, aux
coefficients variables selon la durée et le pays. Le linéaire couvre
l'essentiel des cas partout, et une règle fausse vaut moins que pas de règle.

**Prorata temporis à la journée.** Un bien acquis le 15 du mois n'est pas
amorti d'un mois entier. La première période est calculée au nombre de jours,
et les jours qu'on lui retire sont amortis au-delà de la dernière échéance
prévue : une durée de douze mois entamée le 28 septembre court jusqu'au
30 septembre de l'année suivante. Proratiser la seule première période
reviendrait à raccourcir la durée du bien.

**La somme du tableau égale exactement la base amortissable.** Les arrondis
sont absorbés par la dernière échéance, jamais répartis. Un tableau dont la
somme s'écarte de la base laisse une valeur résiduelle parasite au bilan, que
personne ne remarque avant la cession.

**Reprise d'antériorité.** Un bien migré est déjà partiellement amorti. Le
montant déjà constaté est retiré du tableau, sans altérer les échéances
restantes ni la date de fin.
"""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from typing import List, Optional

#: Périodicités admises, exprimées en mois.
PERIODICITES = {"monthly": 1, "quarterly": 3, "yearly": 12}


class AmortissementError(ValueError):
    """Paramètres d'amortissement incohérents."""


@dataclass(frozen=True)
class Echeance:
    """Une ligne du tableau d'amortissement."""

    numero: int
    date_echeance: date
    dotation: float
    cumul: float
    valeur_residuelle: float


def _arrondi(valeur: float, decimales: int = 2) -> float:
    """Arrondi commercial, demi vers le haut.

    ``round()`` de Python applique l'arrondi bancaire : ``round(0.125, 2)``
    vaut 0.12. Sur des dotations mensuelles, l'écart se cumule sur toute la
    durée du bien.
    """
    quantum = Decimal(1).scaleb(-decimales)
    return float(Decimal(str(valeur)).quantize(quantum, rounding=ROUND_HALF_UP))


def _fin_de_mois(annee: int, mois: int) -> date:
    if mois == 12:
        return date(annee, 12, 31)
    premier_du_suivant = date(annee, mois + 1, 1)
    return date.fromordinal(premier_du_suivant.toordinal() - 1)


def _ajouter_mois(origine: date, mois: int) -> date:
    total = origine.month - 1 + mois
    annee = origine.year + total // 12
    m = total % 12 + 1
    return _fin_de_mois(annee, m)


def calculer_tableau(
    valeur_acquisition: float,
    date_mise_en_service: date,
    duree_mois: int,
    periodicite: str = "monthly",
    valeur_residuelle: float = 0.0,
    deja_amorti: float = 0.0,
    prorata: bool = True,
    decimales: int = 2,
) -> List[Echeance]:
    """Construit le tableau d'amortissement linéaire.

    :param valeur_acquisition: valeur brute du bien.
    :param date_mise_en_service: point de départ de l'amortissement.
    :param duree_mois: durée d'amortissement en mois.
    :param periodicite: ``monthly``, ``quarterly`` ou ``yearly``.
    :param valeur_residuelle: valeur non amortissable, souvent nulle.
    :param deja_amorti: amortissement déjà constaté, pour un bien repris.
    :param prorata: calcul de la première période au nombre de jours, le
        reliquat portant une échéance supplémentaire en fin de tableau.
    :param decimales: précision de la devise du bien.

    Les décimales sont celles de la devise, pas deux par principe.

    Le tableau était calculé au centime quelle que soit la monnaie. En yen,
    qui n'a pas de subdivision, douze dotations de 833,33 étaient ramenées à
    833 au moment d'être enregistrées : le tableau annonçait 10 000, la somme
    des échéances valait 9 996, et quatre yens restaient indéfiniment au
    compte d'immobilisation. Le même écart se produit en sens inverse sur les
    monnaies à trois décimales, dinar tunisien ou koweïtien.
    """
    if valeur_acquisition <= 0:
        raise AmortissementError("La valeur d'acquisition doit être positive.")
    if duree_mois <= 0:
        raise AmortissementError("La durée doit être d'au moins un mois.")
    if periodicite not in PERIODICITES:
        raise AmortissementError("Périodicité inconnue : %s" % periodicite)
    if valeur_residuelle < 0:
        raise AmortissementError("La valeur résiduelle ne peut pas être négative.")
    if valeur_residuelle >= valeur_acquisition:
        raise AmortissementError(
            "La valeur résiduelle doit être inférieure à la valeur d'acquisition."
        )

    base = _arrondi(valeur_acquisition - valeur_residuelle, decimales)
    if deja_amorti < 0:
        raise AmortissementError("L'amortissement déjà constaté ne peut pas être négatif.")
    if deja_amorti > base:
        raise AmortissementError(
            "L'amortissement déjà constaté (%.2f) dépasse la base amortissable (%.2f)."
            % (deja_amorti, base)
        )

    pas = PERIODICITES[periodicite]
    reste = _arrondi(base - deja_amorti, decimales)
    if not reste:
        return []

    # Dates d'échéance : fin de chaque période, à partir de la mise en service.
    dates: List[date] = []
    curseur = 0
    while curseur < duree_mois:
        curseur = min(curseur + pas, duree_mois)
        dates.append(_ajouter_mois(date_mise_en_service, curseur - 1))

    # Prorata de la première période : un bien mis en service le 15 n'est pas
    # amorti du mois entier.
    poids: List[float] = []
    for index, echeance in enumerate(dates):
        mois_periode = min(pas, duree_mois - index * pas)
        if index == 0 and prorata:
            debut_mois = date(date_mise_en_service.year, date_mise_en_service.month, 1)
            fin_premier_mois = _fin_de_mois(debut_mois.year, debut_mois.month)
            jours_mois = fin_premier_mois.day
            jours_courus = jours_mois - date_mise_en_service.day + 1
            poids.append(mois_periode - 1 + jours_courus / jours_mois)
        else:
            poids.append(float(mois_periode))

    # Le prorata décale la fin, il ne raccourcit pas la durée.
    #
    # Les jours retirés à la première période doivent être amortis, et ils le
    # sont après la dernière échéance prévue. Sans cette échéance de
    # reliquat, la base entière était répartie sur le nombre de périodes
    # initial : les jours retirés au premier mois étaient réinjectés dans les
    # suivants, chaque dotation majorée, et le bien soldé un mois trop tôt.
    # Sur 1 200 à douze mois mis en service le 28 septembre, onze dotations
    # de 108,11 au lieu de 100,00, et une fin au 31 août au lieu du
    # 30 septembre suivant.
    #
    # La charge de chaque exercice s'en trouvait majorée, et la durée
    # effective ne correspondait plus à celle inscrite sur la fiche du bien.
    total_poids = sum(poids)
    reliquat = duree_mois - total_poids
    if reliquat > 1e-9:
        dates.append(_ajouter_mois(date_mise_en_service, duree_mois))
        poids.append(reliquat)
        total_poids = float(duree_mois)

    lignes: List[Echeance] = []
    cumul = _arrondi(deja_amorti, decimales)

    # Arrondi par cumul, et non échéance par échéance.
    #
    # Chaque dotation est la différence entre deux cumuls arrondis. Cela
    # garantit deux propriétés à la fois : la somme égale exactement la base,
    # et aucune dotation n'est négative — le cumul théorique étant croissant,
    # sa version arrondie l'est aussi.
    #
    # La méthode naïve, qui arrondit chaque dotation puis fait absorber le
    # résidu par la dernière, échoue dès que le résidu dépasse une dotation :
    # 1,00 € sur 18 mois donne 17 fois 0,06 puis une dernière à -0,02. Éprouvé
    # sur 10 584 combinaisons, ce cas se présentait 274 fois.
    poids_cumule = 0.0
    cumul_arrondi_precedent = 0.0

    for index, (echeance, poids_periode) in enumerate(zip(dates, poids)):
        poids_cumule += poids_periode
        if index == len(dates) - 1:
            cumul_arrondi = reste
        else:
            cumul_arrondi = _arrondi(reste * poids_cumule / total_poids, decimales)
        dotation = _arrondi(cumul_arrondi - cumul_arrondi_precedent, decimales)
        cumul_arrondi_precedent = cumul_arrondi
        cumul = _arrondi(cumul + dotation, decimales)
        lignes.append(Echeance(
            numero=index + 1,
            date_echeance=echeance,
            dotation=dotation,
            cumul=cumul,
            valeur_residuelle=_arrondi(valeur_acquisition - cumul, decimales),
        ))

    # Les échéances sans dotation n'ont pas lieu d'être : elles produiraient
    # une écriture comptable vide.
    return [l for l in lignes if l.dotation > 0]


def controler_tableau(
    lignes: List[Echeance],
    valeur_acquisition: float,
    valeur_residuelle: float = 0.0,
    deja_amorti: float = 0.0,
    decimales: int = 2,
) -> None:
    """Vérifie les invariants du tableau. Lève si l'un est rompu.

    Appelé avant toute écriture comptable : un tableau incohérent ne doit
    jamais atteindre le grand livre.

    La tolérance suit la devise : une demi-unité de la dernière décimale.
    Fixée au demi-centime, elle laissait passer un écart d'un yen entier et
    refusait un tableau juste en dinar.
    """
    if not lignes:
        return

    tolerance = 0.5 * 10 ** -decimales
    base = _arrondi(valeur_acquisition - valeur_residuelle, decimales)
    total = _arrondi(sum(l.dotation for l in lignes), decimales)
    attendu = _arrondi(base - deja_amorti, decimales)
    if abs(total - attendu) > tolerance:
        raise AmortissementError(
            "La somme des dotations (%.2f) n'égale pas la base restant à amortir "
            "(%.2f). Un écart laisserait une valeur résiduelle parasite au bilan."
            % (total, attendu)
        )

    if any(l.dotation < 0 for l in lignes):
        raise AmortissementError("Une dotation négative figure au tableau.")

    derniere = lignes[-1]
    if abs(derniere.valeur_residuelle - valeur_residuelle) > tolerance:
        raise AmortissementError(
            "La valeur résiduelle finale (%.2f) ne correspond pas à celle attendue "
            "(%.2f)." % (derniere.valeur_residuelle, valeur_residuelle)
        )

    for precedente, suivante in zip(lignes, lignes[1:]):
        if suivante.date_echeance <= precedente.date_echeance:
            raise AmortissementError(
                "Les échéances ne sont pas strictement croissantes : %s puis %s."
                % (precedente.date_echeance, suivante.date_echeance)
            )
