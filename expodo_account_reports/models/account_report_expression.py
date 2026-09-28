# -*- coding: utf-8 -*-
# Copyright 2026 Expodo (https://expodo.fr)
# License LGPL-3
"""Moteurs terminaux : les expressions qui lisent la base.

Quatre moteurs produisent des nombres à partir des écritures :
``domain``, ``account_codes``, ``tax_tags`` et ``external``. Le cinquième,
``aggregation``, ne fait que de l'arithmétique sur leurs résultats et vit
dans le paquet ``engine``, sans dépendance Odoo.

**Contrainte structurante (CDC T-07b)** : chaque méthode ci-dessous reçoit
*toutes* les expressions de son moteur d'un coup et n'émet qu'un aller-retour
SQL par portée de date, quel que soit le nombre de lignes du rapport. Une
implémentation par ligne rendrait un Bilan en une centaine de requêtes et
s'effondrerait sur une base volumineuse.
"""

import ast
from collections import defaultdict

from datetime import date

from odoo import api, fields, models
from odoo.exceptions import ValidationError
from odoo.tools import SQL

from ..engine.accounts import account_coefficients, sum_account_codes
from ..engine.formula import AGED_RE, parse_account_codes_formula


class AccountReportExpression(models.Model):
    _inherit = "account.report.expression"

    #: Deux portées propres à la balance générale.
    #
    #: Un compte de gestion est soldé à chaque clôture : son ouverture repart
    #: du début de l'exercice, alors qu'un compte de bilan se cumule depuis
    #: l'origine. Les portées natives ne savent pas faire cette distinction —
    #: elles s'appliquent à toutes les lignes sans regarder la nature du
    #: compte — d'où ces deux-là, qui ne diffèrent des portées `*_period` que
    #: par ce traitement des classes de gestion.
    date_scope = fields.Selection(
        selection_add=[
            ("expodo_opening",
             "Opening balance (income and expenses from the fiscal year start)"),
            ("expodo_closing",
             "Closing balance (income and expenses from the fiscal year start)"),
        ],
        ondelete={"expodo_opening": "set default",
                  "expodo_closing": "set default"},
    )

    # ------------------------------------------------------------------
    # Utilitaires de requêtage
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Jetons de domaine
    # ------------------------------------------------------------------
    #
    #: Une formule de domaine est une constante : elle est lue par
    #: `literal_eval`, qui refuse tout appel de fonction — et c'est voulu, un
    #: domaine ne doit pas pouvoir exécuter de code. Elle ne peut donc pas
    #: désigner le pays de la société ni les comptes de ses journaux, qui ne
    #: sont connus qu'à l'exécution.
    #:
    #: Ces jetons comblent le manque sans rouvrir la porte : ce sont des
    #: chaînes littérales, remplacées par des données après la lecture du
    #: domaine. Aucun code utilisateur n'est jamais évalué.
    EXPODO_DOMAIN_TOKENS = (
        "__company_fiscal_country__",
        "__bank_journal_accounts__",
        "__cash_journal_accounts__",
        "__date_to__",
    )

    @api.model
    def _expodo_token_values(self):
        """Valeur de chaque jeton pour la société active."""
        societe = self.env.company
        pays = societe.account_fiscal_country_id or societe.country_id

        def comptes_de(type_journal):
            journaux = self.env["account.journal"].search([
                ("type", "=", type_journal),
                ("company_id", "=", societe.id),
            ])
            return journaux.default_account_id.ids

        return {
            "__company_fiscal_country__": pays.id,
            "__bank_journal_accounts__": comptes_de("bank"),
            "__cash_journal_accounts__": comptes_de("cash"),
            # Repli volontairement inatteignable : sans borne connue, une
            # condition « lettré après cette date » ne retient rien et le
            # domaine se comporte comme avant l'ajout du jeton. Le repli
            # inverse ferait ressortir des lignes soldées depuis des années.
            "__date_to__": date.max,
        }

    @api.constrains("formula")
    def _check_formula(self):
        """Valide les domaines **après** résolution des jetons.

        Le contrôle du cœur exécute la recherche sur la formule brute. Un
        jeton y passe tant qu'il se compare à un champ relationnel, où Odoo
        accepte un nom, mais il est rejeté dès qu'il se compare à une date :
        « __date_to__ » n'en est pas une. Le module refusait alors de
        s'installer, ce qui est la façon la plus bruyante possible de
        découvrir qu'une validation ignore un mécanisme du module.
        """
        import ast

        avec_jetons = self.filtered(
            lambda e: e.engine == "domain" and any(
                jeton in (e.formula or "") for jeton in self.EXPODO_DOMAIN_TOKENS))
        for expression in avec_jetons:
            try:
                domaine = self._expodo_resolve_tokens(
                    ast.literal_eval(expression.formula))
                self.env["account.move.line"]._search(domaine)
            except Exception as erreur:
                raise ValidationError(self.env._(
                    "Invalid formula for expression '%(label)s' of line "
                    "'%(line)s': %(formula)s",
                    label=expression.label,
                    line=expression.report_line_name,
                    formula=expression.formula)) from erreur
        return super(AccountReportExpression, self - avec_jetons)._check_formula()

    @api.model
    def _expodo_tokens_a_la_date(self, report, options, column_group_key,
                                 date_scope, base=None):
        """Les jetons, complétés par la date d'arrêté de la portée demandée.

        Un état arrêté à une date doit dire ce qui était vrai **à cette
        date**. Le lettrage, lui, arrive plus tard : une facture de décembre
        réglée en mars n'était pas soldée au 31 décembre. L'état qui justifie
        le poste client du bilan doit donc la porter, sans quoi les deux
        écrans se contredisent d'autant plus que les règlements rentrent.
        """
        valeurs = dict(base or self._expodo_token_values())
        try:
            _debut, fin = report._expodo_get_date_bounds(
                options, column_group_key, date_scope)
        except Exception:
            fin = None
        valeurs["__date_to__"] = fin or date.max
        return valeurs

    @api.model
    def _expodo_resolve_tokens(self, domaine, valeurs=None):
        """Remplace les jetons d'un domaine déjà lu par leurs valeurs.

        Le domaine arrive sous forme de listes et de tuples imbriqués : on le
        parcourt à l'identique, en ne touchant qu'aux chaînes reconnues.
        """
        if valeurs is None:
            valeurs = self._expodo_token_values()

        def remplacer(valeur):
            if isinstance(valeur, str):
                return valeurs.get(valeur, valeur)
            if isinstance(valeur, tuple):
                return tuple(remplacer(x) for x in valeur)
            if isinstance(valeur, list):
                return [remplacer(x) for x in valeur]
            return valeur

        return remplacer(domaine)

    def _expodo_group_by_date_scope(self):
        """Regroupe les expressions par portée de date.

        Deux expressions de portées différentes ne peuvent pas partager la
        même requête. Le nombre de portées est borné à six par le modèle
        natif : le nombre de requêtes reste donc indépendant du nombre de
        lignes du rapport.
        """
        grouped = defaultdict(lambda: self.browse())
        for expression in self:
            grouped[expression.date_scope] += expression
        return grouped

    def _expodo_aml_query(self, report, options, column_group_key, date_scope, extra_domain=None):
        """Construit la requête des ``account.move.line`` du périmètre."""
        domain = report._expodo_base_domain(options, column_group_key, date_scope)
        if extra_domain:
            domain = domain + extra_domain
        return self.env["account.move.line"]._search(domain)

    # ------------------------------------------------------------------
    # Moteur `tax_tags`
    # ------------------------------------------------------------------

    def _expodo_engine_tax_tags(self, report, options, column_group_key):
        """Somme les écritures portant les tags de TVA visés.

        Le signe provient du préfixe ``-`` de la formule : en v19, le core
        expose cette convention via ``account.account.tag.balance_negate``,
        calculé depuis la formule de l'expression correspondante.
        """
        results = {}
        aml_model = self.env["account.move.line"]
        relation = aml_model._fields["tax_tag_ids"].relation
        column_aml = aml_model._fields["tax_tag_ids"].column1
        column_tag = aml_model._fields["tax_tag_ids"].column2

        for date_scope, expressions in self._expodo_group_by_date_scope().items():
            # `_get_matching_tags` est conçue pour opérer sur un recordset
            # entier et n'émettre qu'une seule requête. L'appeler expression
            # par expression produirait une requête par ligne du rapport —
            # 99 pour la seule CA3 française (cf. CDC T-07b).
            all_tags = expressions._get_matching_tags()
            tags_by_name = {}
            for tag in all_tags:
                tags_by_name.setdefault(
                    (tag.country_id.id, tag.name), self.env["account.account.tag"]
                )
                tags_by_name[(tag.country_id.id, tag.name)] |= tag

            tags_by_expression = {}
            for expression in expressions:
                country = expression.report_line_id.report_id.country_id
                key = (country.id, expression.formula.lstrip("-"))
                tags_by_expression[expression.id] = tags_by_name.get(
                    key, self.env["account.account.tag"]
                )

            all_tag_ids = {tag.id for tag in all_tags}
            if not all_tag_ids:
                results.update({expression.id: 0.0 for expression in expressions})
                continue

            query = self._expodo_aml_query(report, options, column_group_key, date_scope)

            # Un seul aller-retour : agrégation par tag, répartition en Python.
            rows = self.env.execute_query(SQL(
                """
                  SELECT rel.%(column_tag)s AS tag_id,
                         SUM(account_move_line.balance) AS balance
                    FROM %(from_clause)s
                    JOIN %(relation)s AS rel
                      ON rel.%(column_aml)s = account_move_line.id
                   WHERE %(where_clause)s
                     AND rel.%(column_tag)s IN %(tag_ids)s
                GROUP BY rel.%(column_tag)s
                """,
                column_tag=SQL.identifier(column_tag),
                column_aml=SQL.identifier(column_aml),
                relation=SQL.identifier(relation),
                from_clause=query.from_clause,
                where_clause=query.where_clause or SQL("TRUE"),
                tag_ids=tuple(all_tag_ids),
            ))
            balance_by_tag = {row[0]: row[1] or 0.0 for row in rows}

            for expression in expressions:
                total = sum(
                    balance_by_tag.get(tag.id, 0.0)
                    for tag in tags_by_expression[expression.id]
                )
                # `balance_negate` vaut vrai lorsque la formule commence par '-'.
                if expression.formula.startswith("-"):
                    total = -total
                results[expression.id] = total

        return results

    # ------------------------------------------------------------------
    # Moteur `account_codes`
    # ------------------------------------------------------------------

    def _expodo_engine_account_codes(self, report, options, column_group_key):
        """Somme par préfixe de code de compte.

        Sémantique des suffixes ``D`` et ``C``, conforme à la documentation
        publique d'Odoo : un compte n'est retenu que si son préfixe
        correspond **et** si le solde total de ses écritures sur la période
        est du sens demandé. C'est un filtre sur le compte, pas une sélection
        de la colonne débit ou crédit.

        Exemple de la documentation : le compte 210001 a un solde de -42 et le
        compte 210002 un solde de 25 ; la formule ``21D`` ne retient que
        210002 et renvoie 25.
        """
        results = {}

        for date_scope, expressions in self._expodo_group_by_date_scope().items():
            query = self._expodo_aml_query(report, options, column_group_key, date_scope)

            # Un seul aller-retour, agrégé par compte. Le nombre de lignes
            # retournées est borné par la taille du plan comptable, jamais par
            # le volume d'écritures ni par le nombre de lignes du rapport.
            rows = self.env.execute_query(SQL(
                """
                  SELECT account_move_line.account_id AS account_id,
                         SUM(account_move_line.balance) AS balance
                    FROM %(from_clause)s
                   WHERE %(where_clause)s
                GROUP BY account_move_line.account_id
                """,
                from_clause=query.from_clause,
                where_clause=query.where_clause or SQL("TRUE"),
            ))
            balance_by_account = {row[0]: row[1] or 0.0 for row in rows}
            if not balance_by_account:
                results.update({expression.id: 0.0 for expression in expressions})
                continue

            accounts = self.env["account.account"].browse(balance_by_account).exists()
            account_info = report._expodo_account_info(accounts)

            for expression in expressions:
                results[expression.id] = self._expodo_sum_account_codes(
                    expression, balance_by_account, account_info
                )

        return results

    def _expodo_sum_account_codes(self, expression, balance_by_account, account_info):
        """Applique une formule ``account_codes`` à des soldes déjà agrégés.

        La décision d'inclusion est déléguée à ``engine.accounts``, qui la
        traite sans dépendance Odoo et sous test unitaire.
        """
        return sum_account_codes(
            parse_account_codes_formula(expression.formula),
            balance_by_account,
            account_info,
            tag_resolver=self._expodo_resolve_tag_reference,
        )

    def _expodo_audit_domain(self, report, options, column_group_key):
        """Restriction qui isole les écritures composant cette expression.

        Le domaine d'audit doit reproduire la sélection du calcul, sinon la
        fenêtre ouverte par un clic contredit le montant cliqué — le pire
        défaut possible pour un état d'audit : il fait douter de tous les
        autres chiffres, y compris des justes.

        Chaque moteur sélectionne à sa façon : un domaine le dit lui-même, des
        préfixes de comptes désignent des comptes, des grilles de TVA
        désignent des étiquettes. Seul le premier était traité, si bien que
        tout le bilan — bâti sur les préfixes — ouvrait les écritures de la
        période entière, quel que soit le poste cliqué.
        """
        self.ensure_one()
        if self.engine == "domain":
            try:
                return self._expodo_resolve_tokens(
                    ast.literal_eval(self.formula or "[]"),
                    self._expodo_tokens_a_la_date(
                        report, options, column_group_key, self.date_scope))
            except (ValueError, SyntaxError) as erreur:
                raise ValidationError(
                    self.env._(
                        "Invalid domain on line %(line)s: %(formula)s",
                        line=self.report_line_id.display_name, formula=self.formula,
                    )
                ) from erreur

        if self.engine == "tax_tags":
            tags = self._get_matching_tags()
            return [("tax_tag_ids", "in", tags.ids)] if tags else [(0, "=", 1)]

        if self.engine == "account_codes":
            # Les comptes retenus sont ceux que le calcul a retenus : on
            # refait la même sélection, sur la même portée, avec le même code.
            query = self._expodo_aml_query(
                report, options, column_group_key, self.date_scope)
            lignes = self.env.execute_query(SQL(
                """
                  SELECT account_move_line.account_id AS account_id,
                         SUM(account_move_line.balance) AS solde
                    FROM %(from_clause)s
                   WHERE %(where_clause)s
                GROUP BY account_move_line.account_id
                """,
                from_clause=query.from_clause,
                where_clause=query.where_clause or SQL("TRUE"),
            ))
            soldes = {ligne[0]: ligne[1] or 0.0 for ligne in lignes}
            if not soldes:
                return [(0, "=", 1)]
            comptes = self.env["account.account"].browse(soldes).exists()
            retenus = self._expodo_account_coefficients(
                soldes, report._expodo_account_info(comptes))
            # Un compte dont le coefficient s'annule ne contribue pas au
            # montant : une formule comme `60 - 607` retient la classe 60 puis
            # en retranche le compte 607, qui pèse zéro. L'ouvrir à l'audit
            # ajoutait 4 900 à une consommation de 1 280.
            vises = [compte for compte, coef in retenus.items() if coef]
            return [("account_id", "in", vises)] if vises else [(0, "=", 1)]

        # Une agrégation n'a pas d'écritures propres : elle n'est pas auditable
        # et ne devrait jamais parvenir ici.
        return []

    def _expodo_account_coefficients(self, balance_by_account, account_info):
        """Comptes retenus par cette expression, et leur coefficient.

        Sert au dépliage d'une ligne `account_codes` : la sélection des
        comptes est faite une fois pour toutes ici, puis le solde de chaque
        compte est ventilé par groupe. La sélection est donc rigoureusement la
        même que celle du calcul non déplié — c'est le même code qui la fait.
        """
        self.ensure_one()
        return account_coefficients(
            parse_account_codes_formula(self.formula),
            balance_by_account,
            account_info,
            tag_resolver=self._expodo_resolve_tag_reference,
        )

    def _expodo_resolve_tag_reference(self, reference):
        """``tag(25)`` ou ``tag(module.xmlid)`` -> ensemble d'identifiants."""
        if reference.isdigit():
            return {int(reference)}
        record = self.env.ref(reference, raise_if_not_found=False)
        if not record or record._name != "account.account.tag":
            raise ValidationError(
                self.env._(
                    "Tag reference not found or of wrong type: %(ref)s",
                    ref=reference,
                )
            )
        return {record.id}

    # ------------------------------------------------------------------
    # Moteur `domain`
    # ------------------------------------------------------------------

    def _expodo_engine_domain(self, report, options, column_group_key):
        """Évalue un domaine Odoo arbitraire sur les écritures.

        Chaque expression porte son propre domaine, donc aucune agrégation
        commune n'est possible. Les sous-requêtes sont néanmoins assemblées en
        un ``UNION ALL`` afin de ne faire qu'un aller-retour par portée de
        date — l'exigence T-07b porte sur le nombre d'allers-retours, pas sur
        le nombre de sous-requêtes.
        """
        import ast

        results = {}
        jetons = self._expodo_token_values()

        for date_scope, expressions in self._expodo_group_by_date_scope().items():
            subqueries = []
            for expression in expressions:
                try:
                    domain = self._expodo_resolve_tokens(
                        ast.literal_eval(expression.formula),
                        self._expodo_tokens_a_la_date(
                            report, options, column_group_key, date_scope,
                            base=jetons))
                except (ValueError, SyntaxError) as error:
                    raise ValidationError(
                        self.env._(
                            "Invalid domain on expression %(label)s of line "
                            "%(line)s: %(formula)s",
                            label=expression.label,
                            line=expression.report_line_name,
                            formula=expression.formula,
                        )
                    ) from error

                query = self._expodo_aml_query(
                    report, options, column_group_key, date_scope, extra_domain=domain
                )
                aged = AGED_RE.match(expression.subformula or "")
                if aged:
                    aggregate = report.env["account.report.line"]._expodo_aged_sql(
                        aged, options, column_group_key, report
                    )
                else:
                    aggregate = self._expodo_domain_aggregate(expression.subformula)
                subqueries.append(SQL(
                    """
                    SELECT %(expression_id)s AS expression_id, %(aggregate)s AS value
                      FROM %(from_clause)s
                     WHERE %(where_clause)s
                    """,
                    expression_id=expression.id,
                    aggregate=aggregate,
                    from_clause=query.from_clause,
                    where_clause=query.where_clause or SQL("TRUE"),
                ))

            if not subqueries:
                continue

            rows = self.env.execute_query(
                SQL(" UNION ALL ").join(subqueries)
            )
            for expression_id, value in rows:
                results[expression_id] = value or 0.0

            for expression in expressions:
                results.setdefault(expression.id, 0.0)
                if expression.subformula == "-sum":
                    results[expression.id] = -results[expression.id]

        return results

    def _expodo_domain_aggregate(self, subformula):
        """Traduit la sous-formule du moteur ``domain`` en agrégat SQL."""
        if subformula in ("sum", "-sum", None, ""):
            # L'inversion de `-sum` est appliquée après la requête, afin que
            # l'agrégat SQL reste identique et la requête mutualisable.
            return SQL("COALESCE(SUM(account_move_line.balance), 0.0)")
        if subformula == "sum_if_pos":
            return SQL(
                "GREATEST(COALESCE(SUM(account_move_line.balance), 0.0), 0.0)"
            )
        if subformula == "sum_if_neg":
            return SQL(
                "LEAST(COALESCE(SUM(account_move_line.balance), 0.0), 0.0)"
            )
        if subformula == "count_rows":
            return SQL("COUNT(account_move_line.id)::numeric")
        if subformula == "count_moves":
            return SQL("COUNT(DISTINCT account_move_line.move_id)::numeric")
        # Extensions Expodo : Community ne définit aucun agrégat pour les
        # colonnes débit et crédit, indispensables à une balance générale.
        if subformula == "sum_debit":
            return SQL("COALESCE(SUM(account_move_line.debit), 0.0)")
        if subformula == "sum_credit":
            return SQL("COALESCE(SUM(account_move_line.credit), 0.0)")
        if subformula in ("line_date", "line_partner"):
            # Colonne de détail : elle n'a de valeur qu'une fois la ligne
            # dépliée, sur une écriture précise.
            #
            # Au niveau agrégé, « la date du compte 401 » ne veut rien dire.
            # On renvoie NULL plutôt que de lever : un état dont une colonne
            # n'a pas de sens au total doit afficher une case vide à cet
            # endroit, pas refuser de s'ouvrir.
            return SQL("NULL::numeric")
        raise ValidationError(
            self.env._("Unknown domain subformula: %(sub)s", sub=subformula)
        )

    # ------------------------------------------------------------------
    # Moteur `external`
    # ------------------------------------------------------------------

    def _expodo_engine_external(self, report, options, column_group_key):
        """Lit les valeurs saisies manuellement.

        ``sum`` additionne toutes les valeurs de la période ; ``most_recent``
        ne retient que la plus récente. Ces valeurs sont celles des cellules
        éditables de la déclaration de TVA et les reports de crédit d'une
        période à l'autre.
        """
        results = {expression.id: 0.0 for expression in self}

        for date_scope, expressions in self._expodo_group_by_date_scope().items():
            date_from, date_to = report._expodo_get_date_bounds(
                options, column_group_key, date_scope
            )
            domain = [
                ("target_report_expression_id", "in", expressions.ids),
                ("company_id", "in", options["company_ids"]),
                ("date", "<=", date_to),
            ]
            if date_from is not None:
                domain.append(("date", ">=", date_from))

            values = self.env["account.report.external.value"].search_read(
                domain,
                ["target_report_expression_id", "value", "date"],
                order="date asc, id asc",
            )

            most_recent = {}
            for record in values:
                expression_id = record["target_report_expression_id"][0]
                results[expression_id] = results.get(expression_id, 0.0) + record["value"]
                most_recent[expression_id] = record["value"]

            for expression in expressions:
                if expression.subformula and "most_recent" in expression.subformula:
                    results[expression.id] = most_recent.get(expression.id, 0.0)

        return results
