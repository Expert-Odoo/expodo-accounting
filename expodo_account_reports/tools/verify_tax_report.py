# -*- coding: utf-8 -*-
# Copyright 2026 Expodo (https://expodo.fr)
# License LGPL-3
"""Contrôle indépendant de la déclaration de TVA.

Ce script recalcule chaque case de la déclaration **sans utiliser le moteur de
rapports**. Il repart des écritures et des grilles de taxes, par un chemin
entièrement distinct, puis compare case à case.

Pourquoi c'est utile
====================

Le critère d'acceptation §8.2 du cahier des charges demande un rapprochement
manuel de la déclaration. Fait à la main, il est long, fastidieux, et lui-même
sujet à l'erreur. Fait ici, il est reproductible et s'exécute à chaque
modification du moteur.

Ce que ce contrôle prouve, et ce qu'il ne prouve pas
====================================================

**Il prouve** que le moteur calcule correctement la définition déclarée : si
les deux chemins convergent sur les 132 lignes, une erreur du moteur devrait
s'être produite deux fois de façon cohérente pour passer inaperçue.

**Il ne prouve pas** que la définition elle-même est juste — c'est-à-dire que
telle opération relève bien de telle case. Mais cette définition vient d'Odoo,
pas de nous : elle est livrée par `l10n_fr_account` et partagée par toutes les
bases françaises.

**Il ne prouve pas non plus** que les taxes sont correctement paramétrées dans
la base. C'est la première cause de déclaration fausse, et elle est
indépendante du module. La seconde partie du script la contrôle donc aussi.

Usage
=====

Depuis un shell Odoo :

    exec(open('tools/verify_tax_report.py').read())
    verifier(env, '2026-10-01', '2026-10-31')
"""


def _recalcul_independant(env, date_from, date_to, posted_only=True):
    """Somme des écritures par tag de TVA, sans passer par le moteur.

    Chemin volontairement différent de celui du moteur : requête directe,
    agrégation par nom de tag, signe déduit du sens de la formule comme le
    fait la localisation.
    """
    etat = "AND m.state = 'posted'" if posted_only else "AND m.state != 'cancel'"
    env.cr.execute("""
        SELECT t.name->>'en_US' AS tag, SUM(aml.balance) AS solde, COUNT(*) AS n
          FROM account_move_line aml
          JOIN account_account_tag_account_move_line_rel rel
            ON rel.account_move_line_id = aml.id
          JOIN account_account_tag t
            ON t.id = rel.account_account_tag_id
          JOIN account_move m ON m.id = aml.move_id
         WHERE aml.date BETWEEN %s AND %s
           {etat}
      GROUP BY t.name
    """.format(etat=etat), (date_from, date_to))
    return {row[0]: (float(row[1] or 0.0), row[2]) for row in env.cr.fetchall()}


def verifier(env, date_from, date_to, posted_only=True, tolerance=0.01):
    """Compare le moteur et le recalcul indépendant, case par case."""
    from datetime import date as _date

    def _d(v):
        if isinstance(v, str):
            a, b, c = v.split("-")
            return _date(int(a), int(b), int(c))
        return v

    date_from, date_to = _d(date_from), _d(date_to)

    report = env["account.report"].browse(
        env["account.report"].expodo_resolve_tax_report()
    )
    options = report._expodo_get_options({"date": {
        "mode": "range", "filter": "custom",
        "date_from": date_from, "date_to": date_to,
        }, "all_entries": not posted_only})
    moteur = report._expodo_compute_values(options, "main")

    brut = _recalcul_independant(env, date_from, date_to, posted_only)

    print("=" * 78)
    print("CONTROLE DE LA DECLARATION — %s  du %s au %s"
          % (report.display_name, date_from, date_to))
    print("=" * 78)

    # --- 1. Les expressions terminales : moteur contre somme brute ---
    ecarts = []
    verifiees = 0
    for line in report.line_ids:
        for expr in line.expression_ids:
            if expr.engine != "tax_tags" or not line.code:
                continue
            nom = expr.formula.lstrip("-")
            solde, _ = brut.get(nom, (0.0, 0))
            attendu = -solde if expr.formula.startswith("-") else solde
            obtenu = moteur.get((line.code, expr.label))
            if obtenu is None:
                continue
            verifiees += 1
            if abs(obtenu - attendu) > tolerance:
                ecarts.append((line.name, expr.label, attendu, obtenu))

    print("\n1. EXPRESSIONS TERMINALES (moteur contre somme directe des tags)")
    print("   %d expressions contrôlées, %d écart(s)" % (verifiees, len(ecarts)))
    for nom, label, att, obt in ecarts[:15]:
        print("   ECART  %-44s %-14s attendu %10.2f  obtenu %10.2f"
              % (nom[:44], label, att, obt))

    # --- 2. Les lignes agrégées : recomposition arithmétique ---
    print("\n2. LIGNES AGREGEES (recomposition des totaux)")
    controles = [
        ("16 - TVA brute due", "box_16", ["box_08_taxe", "box_09_taxe",
                                          "box_9B_taxe", "box_10_taxe",
                                          "box_11_taxe", "box_13", "box_15"]),
        ("23 - TVA deductible", "box_23", ["box_19", "box_20", "box_21",
                                           "box_22", "box_2C"]),
    ]
    for libelle, total_code, composantes in controles:
        total = moteur.get((total_code, "balance"))
        if total is None:
            continue
        somme = sum(moteur.get((c, "balance"), 0.0) for c in composantes)
        etat = "OK" if abs(total - somme) <= tolerance else "*** ECART ***"
        print("   %-24s total %10.2f   somme des composantes %10.2f   %s"
              % (libelle, total, somme, etat))

    net = moteur.get(("box_28", "balance"), 0.0)
    due = moteur.get(("box_16", "balance"), 0.0)
    ded = moteur.get(("box_23", "balance"), 0.0)
    print("   %-24s %10.2f - %10.2f = %10.2f   %s"
          % ("28 - TVA nette due", due, ded, due - ded,
             "OK" if abs(net - (due - ded)) <= 1.0 else "verifier (arrondis/reports)"))

    # --- 3. Concordance avec les comptes de TVA du bilan ---
    print("\n3. CONCORDANCE AVEC LES COMPTES DE TVA")
    env.cr.execute("""
        SELECT a.id, SUM(aml.balance)
          FROM account_move_line aml
          JOIN account_account a ON a.id = aml.account_id
          JOIN account_move m ON m.id = aml.move_id
         WHERE aml.date BETWEEN %s AND %s AND m.state = 'posted'
      GROUP BY a.id
    """, (date_from, date_to))
    collectee = deductible = 0.0
    for aid, solde in env.cr.fetchall():
        code = env["account.account"].browse(aid).code or ""
        if code.startswith(("4457", "4452")):
            collectee += -float(solde or 0)
        elif code.startswith("4456"):
            deductible += float(solde or 0)
    print("   TVA collectee aux comptes 4457x/4452 : %10.2f" % collectee)
    print("   TVA deductible aux comptes 4456x     : %10.2f" % deductible)
    print("   solde des comptes                    : %10.2f" % (collectee - deductible))
    print("   ligne 28 de la declaration           : %10.2f" % net)
    ecart_bilan = abs((collectee - deductible) - net)
    print("   ecart                                : %10.2f   %s"
          % (ecart_bilan, "OK" if ecart_bilan <= 1.0 else
             "A JUSTIFIER (TVA sur encaissements, reports, arrondis)"))

    print("\n" + "=" * 78)
    verdict = not ecarts and ecart_bilan <= 1.0
    print("VERDICT : %s" % ("declaration coherente" if verdict
                            else "ecarts a examiner ci-dessus"))
    print("=" * 78)
    return verdict


def controler_parametrage_taxes(env):
    """Contrôle du paramétrage des taxes — première cause de déclaration fausse.

    Une taxe dont les grilles sont mal affectées produit une déclaration
    plausible et fausse : les montants existent, ils tombent dans la mauvaise
    case. Aucun contrôle arithmétique ne peut le détecter, puisque tout
    s'équilibre.

    Ce que ce contrôle **ne** signale **pas**, et pourquoi :

    * Une ligne de base sans grille n'est pas une anomalie. En France, un achat
      ordinaire ne déclare aucune base : seule la taxe remonte en case 20.
    * Une fraction de taxe sans grille non plus. Sur une taxe à déductibilité
      partielle, la part non déductible ne doit justement figurer nulle part.

    Une première version signalait ces deux cas et produisait 22 fausses
    alertes sur 29 taxes. Un contrôleur qui crie au loup n'est pas lu.
    """
    print("=" * 78)
    print("CONTROLE DU PARAMETRAGE DES TAXES")
    print("=" * 78)

    taxes = env["account.tax"].search([
        ("type_tax_use", "in", ("sale", "purchase")),
        ("company_id", "=", env.company.id),
    ])

    env.cr.execute("""
        SELECT aml.tax_line_id, COUNT(*) FROM account_move_line aml
         WHERE aml.tax_line_id IS NOT NULL GROUP BY aml.tax_line_id
    """)
    utilisees = dict(env.cr.fetchall())

    muettes, asymetriques = [], []
    for tax in taxes:
        lignes = tax.repartition_line_ids
        taxe_facture = lignes.filtered(
            lambda l: l.document_type == "invoice" and l.repartition_type == "tax")
        taxe_avoir = lignes.filtered(
            lambda l: l.document_type == "refund" and l.repartition_type == "tax")

        # Anomalie 1 : aucune grille nulle part. Les montants de cette taxe
        # n'apparaîtront dans aucune case de la déclaration.
        if not lignes.filtered(lambda l: l.tag_ids):
            muettes.append(tax)
            continue

        # Anomalie 2 : facture et avoir tagués différemment. Un avoir ne
        # viendrait alors pas annuler la facture dans la déclaration — l'erreur
        # est invisible tant qu'aucun avoir n'est émis, puis permanente.
        tags_facture = set(taxe_facture.tag_ids.mapped("name"))
        tags_avoir = set(taxe_avoir.tag_ids.mapped("name"))
        if tags_facture != tags_avoir:
            asymetriques.append((tax, sorted(tags_facture), sorted(tags_avoir)))

    print("\n1. TAXES SANS AUCUNE GRILLE (%d)" % len(muettes))
    print("   Leurs montants n'apparaitront dans aucune case.")
    critiques = []
    for tax in muettes:
        n = utilisees.get(tax.id, 0)
        if n:
            critiques.append(tax)
        print("   %-34s %5s%% %-9s %s" % (
            tax.name[:34], tax.amount, tax.type_tax_use,
            "<-- UTILISEE SUR %d ECRITURES" % n if n else "(inutilisee)"))

    print("\n2. FACTURE ET AVOIR TAGUES DIFFEREMMENT (%d)" % len(asymetriques))
    print("   Un avoir n'annulerait pas la facture dans la declaration.")
    for tax, f, a in asymetriques[:15]:
        print("   %-30s facture=%s  avoir=%s" % (tax.name[:30], f or "-", a or "-"))

    print("\n3. TAXES EFFECTIVEMENT UTILISEES : %d" % len(utilisees))

    probleme = bool(critiques or asymetriques)
    print("\nVERDICT : %s" % (
        "%d taxe(s) utilisee(s) sans grille, %d asymetrie(s) — a corriger"
        % (len(critiques), len(asymetriques)) if probleme
        else "parametrage coherent"))
    print("=" * 78)
    return not probleme
