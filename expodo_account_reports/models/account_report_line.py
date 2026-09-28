# -*- coding: utf-8 -*-
# Copyright 2026 Expodo (https://expodo.fr)
# License LGPL-3
"""Expansion des lignes à groupement dynamique.

Le modèle `account.report.line` de Community porte les champs `groupby` et
`user_groupby`, mais **aucune logique d'expansion** : celle-ci est restée
dans l'édition Enterprise. Sans elle, une balance générale ou un grand livre
sont impossibles à exprimer de façon déclarative.

Ce fichier fournit cette logique. Une ligne dont `groupby` vaut par exemple
``account_id`` se développe en une sous-ligne par compte mouvementé ; une
chaîne ``account_id,partner_id`` se développe sur deux niveaux, le second
n'étant calculé qu'au dépliage du premier.

Contrainte de performance (CDC T-07b) : une ligne groupée produit **une seule
requête par portée de date**, quel que soit le nombre de groupes obtenus. Les
agrégats des différentes expressions de la ligne sont calculés dans la même
passe.
"""

import ast
from collections import defaultdict
from datetime import timedelta

from odoo import fields, models
from odoo.exceptions import ValidationError
from odoo.tools import SQL

from ..engine.formula import AGED_RE

#: Agrégats calculés en une passe pour toute ligne groupée.
#: Clé = sous-formule de l'expression, valeur = expression SQL.
GROUP_AGGREGATES = {
    "sum": SQL("COALESCE(SUM(account_move_line.balance), 0.0)"),
    "-sum": SQL("-COALESCE(SUM(account_move_line.balance), 0.0)"),
    "sum_debit": SQL("COALESCE(SUM(account_move_line.debit), 0.0)"),
    "sum_credit": SQL("COALESCE(SUM(account_move_line.credit), 0.0)"),
    "count_rows": SQL("COUNT(account_move_line.id)::numeric"),
    "count_moves": SQL(
        "COUNT(DISTINCT account_move_line.move_id)::numeric AS %s",
        SQL.identifier("count_moves")),
}

#: Colonnes qui ne sont pas des agrégats mais des attributs de l'écriture.
#:
#: Elles complètent les montants d'un grand livre ou d'un livre-journal : la
#: date, le tiers, la pièce. Les produire dans la même requête que les
#: agrégats évite un aller-retour par ligne affichée — un grand livre de mille
#: écritures en ferait mille.
#: Sur une ligne qui regroupe plusieurs écritures, ces colonnes ne valent que
#: si toutes les écritures du groupe portent la **même** valeur.
#:
#: Un `MIN()` renvoyait sinon la plus petite : le compte 401100, qui porte
#: trois fournisseurs, s'affichait au nom de l'un d'eux. Le lecteur en
#: concluait que le compte lui appartenait — et un compte collectif lu comme
#: un compte individuel fausse tout ce qu'on en déduit.
#:
#: La règle retenue est celle de l'aveu d'ignorance : afficher la valeur quand
#: elle est certaine, laisser vide quand elle varie. Le dépliage montre alors
#: les écritures, chacune avec la sienne. L'édition de référence laisse ces
#: colonnes vides sur toute ligne agrégée ; les remplir quand le groupe est
#: homogène en dit un peu plus sans jamais rien affirmer de faux.
DETAIL_COLUMNS = {
    "line_date": SQL(
        "CASE WHEN COUNT(DISTINCT account_move_line.date) = 1 "
        "THEN MIN(account_move_line.date) END AS %s",
        SQL.identifier("line_date")),
    # Le tiers exige en plus qu'aucune écriture du groupe n'en soit dépourvue :
    # un groupe mêlant des lignes sans tiers et des lignes d'un seul tiers
    # n'appartient pas à ce tiers.
    "line_partner": SQL(
        "CASE WHEN COUNT(DISTINCT account_move_line.partner_id) = 1 "
        "AND COUNT(account_move_line.partner_id) = COUNT(*) "
        "THEN MIN(account_move_line.partner_id) END AS %s",
        SQL.identifier("line_partner")),
}

#: Champs de groupement autorisés, avec leur modèle cible.
#: Restreindre explicitement évite qu'une définition XML erronée n'expose
#: un champ arbitraire dans une requête.
ALLOWED_GROUPBY = {
    "account_id": "account.account",
    "partner_id": "res.partner",
    "journal_id": "account.journal",
    "move_id": "account.move",
    "analytic_distribution": None,
    "id": "account.move.line",
    "currency_id": "res.currency",
    "date": None,
    "tax_line_id": "account.tax",
}


class AccountReportLine(models.Model):
    _inherit = "account.report.line"

    # ------------------------------------------------------------------
    # Groupement
    # ------------------------------------------------------------------

    expodo_prior_scope = fields.Boolean(
        string="Read up to the start of the fiscal year",
        default=False,
        help="Reads income and expense accounts from the very beginning up to "
             "the day before the current fiscal year starts. That is what "
             "earlier years left unallocated: the result of a closed year "
             "still awaiting the shareholders' decision, and, on books that "
             "were never closed, every earlier year at once.",
    )

    expodo_period_scope = fields.Boolean(
        string="Read over the fiscal year",
        default=False,
        help="Exception to the cumulative reading of a statement of position. "
             "The result line of a balance sheet reads income and expense "
             "accounts: it covers the current fiscal year, since earlier "
             "years' results have already joined retained earnings.",
    )

    def _create_report_expression(self, engine):
        """Poser la portée de lecture sur les expressions des raccourcis.

        Les champs raccourcis — `account_codes_formula`, `domain_formula` — ne
        déclarent aucune portée : les expressions qu'ils créent héritent donc
        du défaut, qui lit sur la période affichée.

        Pour un compte de résultat c'est juste : il mesure une activité entre
        deux dates. Pour un bilan c'est faux : il porte des soldes arrêtés à
        une date, cumulés depuis l'origine. Un bilan ouvert du 1er juillet au
        31 décembre ne montrait que les mouvements du second semestre, et
        affichait une trésorerie négative là où la banque était créditrice.

        Le défaut était invisible tant que la période commençait au début des
        données, et le contrôle actif-passif ne le voyait pas non plus : les
        deux côtés étaient faux de la même manière, donc leur écart restait
        nul. C'est précisément la famille d'erreurs que ce module existe pour
        attraper.

        La ligne de résultat fait exception : elle lit des comptes de produits
        et de charges, et porte l'**exercice en cours**. Cumulée depuis
        l'origine, elle agrégerait tous les exercices, alors que les précédents
        ont déjà rejoint le report à nouveau — le bilan compterait deux fois
        les mêmes bénéfices. Restreinte à la plage affichée, elle ne
        s'accorderait plus avec un actif cumulé : un bilan ouvert sur un
        semestre porterait un actif complet et un résultat de six mois.

        L'exercice est la seule portée qui tienne les deux bouts, et c'est
        celle qu'emploie l'édition de référence. Ces lignes portent
        `expodo_period_scope`.
        """
        super()._create_report_expression(engine)
        for ligne in self:
            if not ligne.report_id.expodo_cumulative:
                continue
            # La portée est posée dans les deux sens : une ligne qui reprend
            # l'exception doit retrouver la lecture sur la période, faute de
            # quoi une correction de déclaration resterait sans effet sur une
            # base déjà installée.
            if ligne.expodo_period_scope:
                voulue = "from_fiscalyear"
            elif ligne.expodo_prior_scope:
                voulue = "to_beginning_of_fiscalyear"
            else:
                voulue = "from_beginning"
            # Seule l'expression que le raccourci vient d'écrire est concernée.
            # La boucle réglait auparavant *toutes* les expressions de la
            # ligne : une expression déclarée en XML avec sa propre portée se
            # faisait réécrire par le premier raccourci posé sur la même
            # ligne, sans rien signaler. Une ligne qui a besoin de deux
            # lectures — le report à nouveau, qui lit un compte depuis
            # l'origine et les comptes de gestion jusqu'au début de l'exercice
            # — devenait alors impossible à écrire.
            aregler = ligne.expression_ids.filtered(
                lambda e: e.label == "balance" and e.date_scope != voulue)
            if aregler:
                aregler.date_scope = voulue

    def _expodo_groupby_chain(self, options=None):
        """Chaîne de groupement effective de la ligne.

        Le groupement choisi par l'utilisateur (``user_groupby``) prime sur
        celui défini par le rapport (``groupby``), comme dans le modèle natif.
        """
        self.ensure_one()
        raw = (options or {}).get("user_groupby") or self.user_groupby or self.groupby or ""
        chain = [part.strip() for part in raw.split(",") if part.strip()]
        for field_name in chain:
            if field_name not in ALLOWED_GROUPBY:
                raise ValidationError(
                    self.env._(
                        "Group-by not allowed on line %(line)s: %(field)s",
                        line=self.display_name, field=field_name,
                    )
                )
        return chain

    def _expodo_expand(self, report, options, column_group_key, level=0, parent_domain=None):
        """Développe la ligne en une sous-ligne par groupe.

        :param level: niveau de la chaîne de groupement à développer.
        :param parent_domain: restriction héritée des niveaux supérieurs.
        :return: liste de dictionnaires ``{id, name, values, expandable}``.
        """
        self.ensure_one()
        chain = self._expodo_groupby_chain(options)
        if level >= len(chain):
            return []

        field_name = chain[level]
        # Deux moteurs savent se grouper.
        #
        # `domain` était le seul reconnu à l'origine, ce qui rendait
        # inutilisables 73 lignes de la localisation espagnole : les
        # déclarations 111, 115, 303 et 420 y groupent par compte des montants
        # issus de grilles de TVA, donc du moteur `tax_tags`. Cinq déclarations
        # sur huit refusaient de s'afficher.
        #
        # La leçon vaut au-delà de l'Espagne : rien n'impose qu'une ligne
        # groupée tire ses montants d'un domaine. Le groupement est une
        # opération sur le résultat, indépendante du moteur qui le produit.
        par_domaine = self.expression_ids.filtered(lambda e: e.engine == "domain")
        par_grilles = self.expression_ids.filtered(lambda e: e.engine == "tax_tags")
        par_comptes = self.expression_ids.filtered(lambda e: e.engine == "account_codes")
        if not par_domaine and not par_grilles and not par_comptes:
            raise ValidationError(
                self.env._(
                    "Grouped line %(line)s must carry at least one `domain`, "
                    "`account_codes` or `tax_tags` engine expression.",
                    line=self.display_name,
                )
            )

        rows_by_scope = defaultdict(dict)
        for date_scope, scoped in self._expodo_group_expressions(par_domaine):
            rows_by_scope[date_scope].update(self._expodo_group_query(
                report, options, column_group_key, date_scope,
                field_name, scoped, parent_domain,
            ))
        for date_scope, scoped in self._expodo_group_expressions(par_grilles):
            lignes = self._expodo_group_query_tax_tags(
                report, options, column_group_key, date_scope,
                field_name, scoped, parent_domain,
            )
            for cle, valeurs in lignes.items():
                rows_by_scope[date_scope].setdefault(cle, {}).update(valeurs)
        for date_scope, scoped in self._expodo_group_expressions(par_comptes):
            lignes = self._expodo_group_query_account_codes(
                report, options, column_group_key, date_scope,
                field_name, scoped, parent_domain,
            )
            for cle, valeurs in lignes.items():
                rows_by_scope[date_scope].setdefault(cle, {}).update(valeurs)

        # Assemblage : un groupe peut n'exister que dans certaines portées.
        #
        # Au niveau de l'écriture (`id`), seules les portées de la période
        # désignent les lignes à afficher. Les portées cumulées (solde
        # d'ouverture, solde final) ne font que compléter les valeurs : sans
        # cette règle, le détail d'un compte listerait toutes ses écritures
        # depuis l'origine dès que l'état porte un solde d'ouverture.
        sources = rows_by_scope
        if field_name == "id" and "strict_range" in rows_by_scope:
            sources = {"strict_range": rows_by_scope["strict_range"]}
        keys = []
        for rows in sources.values():
            for key in rows:
                if key not in keys:
                    keys.append(key)

        # Les libellés sont résolus en une passe : un `browse().display_name`
        # par groupe produirait une requête par ligne affichée, soit 58 pour
        # un grand livre de 17 comptes (CDC T-07b).
        labels = self._expodo_group_labels(field_name, keys)

        # Chaque groupe porte **toutes** les colonnes de la ligne, même celles
        # où il n'a aucun mouvement.
        #
        # La requête groupée ne renvoie que les comptes qui ont bougé dans la
        # portée interrogée. Un compte mouvementé sur la période mais pas
        # avant n'apparaissait donc pas dans le solde initial, et sa cellule
        # restait vide. Vide ne se lit pas : le lecteur ne peut pas distinguer
        # « ce compte n'a rien enregistré » de « ce compte n'existait pas ».
        # Zéro le dit.
        etiquettes = set(self.expression_ids.mapped("label"))

        results = []
        for key in keys:
            values = {etiquette: 0.0 for etiquette in etiquettes}
            for date_scope, rows in rows_by_scope.items():
                values.update(rows.get(key, {}))
            results.append({
                "group_field": field_name,
                "group_id": key,
                "name": labels.get(key) or str(key),
                "values": values,
                "expandable": level + 1 < len(chain),
                "level": level,
            })

        results.sort(key=lambda row: (row["name"] or "").lower())
        return results

    @staticmethod
    def _expodo_group_expressions(expressions):
        """Regroupe les expressions d'une ligne par portée de date."""
        grouped = defaultdict(lambda: expressions.browse())
        for expression in expressions:
            grouped[expression.date_scope] |= expression
        return list(grouped.items())

    def _expodo_group_query(self, report, options, column_group_key, date_scope,
                            field_name, expressions, parent_domain=None):
        """Une requête, tous les agrégats de la ligne, groupés.

        Les expressions d'une même ligne partagent presque toujours le même
        domaine — c'est ce qui permet de tout calculer en une passe. Si elles
        divergent, on retombe sur une requête par domaine distinct, ce qui
        reste borné par le nombre d'expressions de la ligne et non par le
        nombre de groupes.
        """
        self.ensure_one()
        by_domain = defaultdict(list)
        for expression in expressions:
            by_domain[expression.formula].append(expression)

        rows_by_key = defaultdict(dict)
        detail_labels = {
            expression.label for expression in expressions
            if (expression.subformula or "") in DETAIL_COLUMNS
        }

        for formula, group in by_domain.items():
            try:
                domain = ast.literal_eval(formula) if formula.strip() else []
            except (ValueError, SyntaxError) as error:
                raise ValidationError(
                    self.env._(
                        "Invalid domain on line %(line)s: %(formula)s",
                        line=self.display_name, formula=formula,
                    )
                ) from error

            full_domain = report._expodo_base_domain(options, column_group_key, date_scope)
            full_domain += domain + list(parent_domain or [])
            query = self.env["account.move.line"]._search(full_domain)

            selects = [SQL(
                "%s AS group_key",
                SQL.identifier("account_move_line", field_name),
            )]
            labels = []
            for expression in group:
                subformula = expression.subformula or "sum"
                aged = AGED_RE.match(subformula)
                if aged:
                    # Une tranche d'ancienneté n'est pas un agrégat différent :
                    # c'est la même somme, restreinte par un CASE sur la date
                    # d'échéance. La restriction est exprimée dans le SELECT
                    # plutôt que dans le WHERE, afin que toutes les tranches
                    # tiennent dans la même requête.
                    selects.append(self._expodo_aged_sql(aged, options, column_group_key, report))
                    labels.append(expression.label)
                    continue
                if subformula in DETAIL_COLUMNS:
                    # Colonne de détail : la date ou le tiers de l'écriture,
                    # et non un agrégat.
                    #
                    # Un grand livre sans date n'est pas un grand livre : on y
                    # cherche *quand* un compte a bougé autant que de combien.
                    # `MIN` sur un groupement par ligne d'écriture retourne la
                    # valeur de la ligne elle-même ; sur un groupement plus
                    # large il retourne la plus ancienne, ce qui reste la
                    # lecture attendue.
                    selects.append(DETAIL_COLUMNS[subformula])
                    labels.append(expression.label)
                    continue
                aggregate = GROUP_AGGREGATES.get(subformula)
                if aggregate is None:
                    raise ValidationError(
                        self.env._(
                            "Unknown group aggregate: %(sub)s",
                            sub=expression.subformula,
                        )
                    )
                selects.append(aggregate)
                labels.append(expression.label)

            rows = self.env.execute_query(SQL(
                """
                  SELECT %(selects)s
                    FROM %(from_clause)s
                   WHERE %(where_clause)s
                GROUP BY %(group_field)s
                """,
                selects=SQL(", ").join(selects),
                from_clause=query.from_clause,
                where_clause=query.where_clause or SQL("TRUE"),
                group_field=SQL.identifier("account_move_line", field_name),
            ))
            for row in rows:
                key = row[0]
                for label, value in zip(labels, row[1:]):
                    if label in detail_labels:
                        # Une colonne de détail porte une date ou un
                        # identifiant de tiers, pas un montant. La convertir en
                        # flottant produisait une erreur de type sur la
                        # première ligne dépliée — le rapport refusait de
                        # s'ouvrir, ce qui est au moins franc.
                        rows_by_key[key][label] = value
                    else:
                        rows_by_key[key][label] = float(value or 0.0)

        return rows_by_key

    def _expodo_group_query_tax_tags(self, report, options, column_group_key,
                                     date_scope, field_name, expressions,
                                     parent_domain=None):
        """Montants d'une ligne à grilles de TVA, ventilés par groupe.

        Même principe que `_expodo_group_query`, mais la sélection des
        écritures ne vient pas d'un domaine : elle vient des étiquettes de
        taxe visées par la formule. On joint donc la table de relation
        `tax_tag_ids` et on agrège par (groupe, étiquette), la répartition
        entre expressions se faisant ensuite en Python.

        Une seule requête par portée de date, comme partout ailleurs dans ce
        module : le nombre de groupes obtenus ne doit jamais se traduire en
        nombre d'allers-retours SQL.

        Le signe suit la même convention que le moteur non groupé : une
        formule préfixée de `-` inverse le solde. Cette convention est celle
        du cœur d'Odoo, exposée par `account.account.tag.balance_negate` ;
        la reproduire ici plutôt que de la réinventer garantit qu'un total
        groupé et son total de ligne ne puissent pas diverger de signe.
        """
        self.ensure_one()
        modele_ligne = self.env["account.move.line"]
        relation = modele_ligne._fields["tax_tag_ids"].relation
        colonne_ligne = modele_ligne._fields["tax_tag_ids"].column1
        colonne_tag = modele_ligne._fields["tax_tag_ids"].column2

        toutes = expressions._get_matching_tags()
        par_nom = defaultdict(lambda: self.env["account.account.tag"])
        for tag in toutes:
            par_nom[(tag.country_id.id, tag.name)] |= tag

        tags_par_expression = {}
        for expression in expressions:
            pays = expression.report_line_id.report_id.country_id
            cle = (pays.id, expression.formula.lstrip("-"))
            tags_par_expression[expression.id] = par_nom.get(
                cle, self.env["account.account.tag"])

        identifiants = {tag.id for tag in toutes}
        if not identifiants:
            return {}

        domaine = report._expodo_base_domain(options, column_group_key, date_scope)
        domaine += list(parent_domain or [])
        requete = modele_ligne._search(domaine)

        lignes = self.env.execute_query(SQL(
            """
              SELECT %(group_field)s AS group_key,
                     rel.%(colonne_tag)s AS tag_id,
                     COALESCE(SUM(account_move_line.balance), 0.0) AS solde
                FROM %(from_clause)s
                JOIN %(relation)s AS rel
                  ON rel.%(colonne_ligne)s = account_move_line.id
               WHERE %(where_clause)s
                 AND rel.%(colonne_tag)s IN %(tag_ids)s
            GROUP BY %(group_field)s, rel.%(colonne_tag)s
            """,
            group_field=SQL.identifier("account_move_line", field_name),
            colonne_tag=SQL.identifier(colonne_tag),
            colonne_ligne=SQL.identifier(colonne_ligne),
            relation=SQL.identifier(relation),
            from_clause=requete.from_clause,
            where_clause=requete.where_clause or SQL("TRUE"),
            tag_ids=tuple(identifiants),
        ))

        soldes = defaultdict(dict)
        for groupe, tag_id, solde in lignes:
            soldes[groupe][tag_id] = float(solde or 0.0)

        resultat = defaultdict(dict)
        for groupe, par_tag in soldes.items():
            for expression in expressions:
                total = sum(
                    par_tag.get(tag.id, 0.0)
                    for tag in tags_par_expression[expression.id]
                )
                if expression.formula.startswith("-"):
                    total = -total
                resultat[groupe][expression.label] = total
        return resultat

    def _expodo_group_query_account_codes(self, report, options, column_group_key,
                                          date_scope, field_name, expressions,
                                          parent_domain=None):
        """Montants d'une ligne à préfixes de comptes, ventilés par groupe.

        Le moteur `account_codes` choisit des comptes ; le dépliage demande de
        savoir où leurs écritures se rangent. Une seule requête agrège donc les
        soldes par (compte, groupe) — une par portée de date, comme partout
        ailleurs, quel que soit le nombre de groupes obtenus.

        Le solde total du compte, somme de ses groupes, sert à trancher les
        suffixes `D` et `C`, exactement comme dans le calcul non déplié. Juger
        le sens groupe par groupe ferait entrer un compte globalement créditeur
        au titre d'un groupe débiteur : le détail ne sommerait plus à la ligne,
        et un bilan dont l'actif ne vaut plus la somme de ses comptes ne vaut
        plus rien.

        Sans cette méthode, les dix états structurés du module — bilans,
        comptes de résultat, SIG — restaient plats : on lisait « Créances
        76 500 » sans jamais savoir de quels comptes elles venaient, là où
        l'édition Enterprise déplie chacune de ses lignes.
        """
        self.ensure_one()
        domaine = report._expodo_base_domain(options, column_group_key, date_scope)
        domaine += list(parent_domain or [])
        requete = self.env["account.move.line"]._search(domaine)

        lignes = self.env.execute_query(SQL(
            """
              SELECT account_move_line.account_id AS account_id,
                     %(group_field)s AS group_key,
                     COALESCE(SUM(account_move_line.balance), 0.0) AS solde
                FROM %(from_clause)s
               WHERE %(where_clause)s
            GROUP BY account_move_line.account_id, %(group_field)s
            """,
            group_field=SQL.identifier("account_move_line", field_name),
            from_clause=requete.from_clause,
            where_clause=requete.where_clause or SQL("TRUE"),
        ))

        soldes = defaultdict(dict)
        totaux = defaultdict(float)
        for compte, groupe, solde in lignes:
            solde = float(solde or 0.0)
            soldes[compte][groupe] = soldes[compte].get(groupe, 0.0) + solde
            totaux[compte] += solde
        if not totaux:
            return {}

        comptes = self.env["account.account"].browse(list(totaux)).exists()
        infos = report._expodo_account_info(comptes)

        resultat = defaultdict(dict)
        for expression in expressions:
            coefficients = expression._expodo_account_coefficients(totaux, infos)
            par_groupe = defaultdict(float)
            for compte, coefficient in coefficients.items():
                for groupe, solde in soldes[compte].items():
                    par_groupe[groupe] += coefficient * solde
            for groupe, montant in par_groupe.items():
                resultat[groupe][expression.label] = montant
        return resultat

    def _expodo_group_labels(self, field_name, keys):
        """Libellés de tous les groupes, résolus en une seule passe.

        Retourne ``{clé: libellé}``. Le coût est d'un `browse` unique par
        niveau de groupement, indépendant du nombre de groupes.
        """
        unset = self.env._("Not set")
        labels = {key: unset for key in keys if key is None}

        model_name = ALLOWED_GROUPBY.get(field_name)
        real_keys = [key for key in keys if key is not None]
        if not model_name:
            labels.update({key: str(key) for key in real_keys})
            return labels

        records = self.env[model_name].browse(real_keys).exists()
        if model_name == "account.account":
            # Le code prime sur le nom : c'est ainsi qu'une balance se lit.
            found = {r.id: "%s %s" % (r.code or "", r.name or "") for r in records}
        else:
            found = {r.id: r.display_name for r in records}

        for key in real_keys:
            labels[key] = found.get(key, str(key))
        return labels

    def _expodo_group_domain(self, field_name, key):
        """Restriction à appliquer pour développer le niveau suivant."""
        return [(field_name, "=", key)]

    def _expodo_group_label(self, field_name, key):
        """Libellé d'une clé de groupement, identique à celui de la ligne.

        Sert à intituler la fenêtre de drill-down avec ce qui a réellement été
        cliqué : « 101100 Capital souscrit » plutôt que « Comptes ». Le
        libellé est produit par la même méthode que les lignes du rapport, de
        sorte que le titre et la ligne ne puissent pas diverger — un compte
        affiché avec son code dans le rapport et sans lui dans le titre
        sèmerait le doute au moment précis où l'on vérifie.
        """
        if not key:
            return ""
        return self._expodo_group_labels(field_name, [key]).get(key) or str(key)

    def _expodo_aged_sql(self, match, options, column_group_key, report):
        """Somme restreinte à une tranche d'ancienneté.

        Les bornes sont exprimées en jours révolus depuis la date d'échéance,
        comptés à partir de la date de fin de la période affichée. Une ligne
        sans date d'échéance est rattachée à sa date comptable, comme le veut
        l'usage : une écriture diverse non échue reste exigible.
        """
        reference = options["column_groups"][column_group_key]["date"]["date_to"]
        minimum = int(match.group("min"))
        maximum = match.group("max")

        # `aged(0, 30)` couvre les créances échues depuis 0 à 30 jours ;
        # `aged(-999, -1)` couvre le non échu.
        upper_date = reference - timedelta(days=minimum)
        conditions = [SQL(
            "COALESCE(account_move_line.date_maturity, account_move_line.date) <= %s",
            upper_date,
        )]
        if maximum != "*":
            lower_date = reference - timedelta(days=int(maximum))
            # Borne basse **inclusive**. Avec une borne stricte, une échéance
            # tombant exactement à la date de référence n'appartient à aucune
            # tranche : « non échu » exige une échéance postérieure, et
            # « 1 à 30 jours » une échéance antérieure d'au moins un jour.
            # Le jour de référence resterait orphelin — or c'est le cas de
            # toute facture échue le jour même. Avec `>=`, les tranches sont
            # contiguës et leur somme égale le total (cf. T30).
            conditions.append(SQL(
                "COALESCE(account_move_line.date_maturity, account_move_line.date) >= %s",
                lower_date,
            ))

        return SQL(
            "COALESCE(SUM(CASE WHEN %(cond)s THEN account_move_line.balance ELSE 0 END), 0.0)",
            cond=SQL(" AND ").join(conditions),
        )
