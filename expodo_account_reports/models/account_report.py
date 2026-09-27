# -*- coding: utf-8 -*-
# Copyright 2026 Expodo (https://expodo.fr)
# License LGPL-3
"""Orchestration du rendu d'un ``account.report``.

Responsabilités de ce fichier :

* construire le dictionnaire d'options (période, comparaisons, journaux,
  état des écritures, sociétés) ;
* assembler le domaine de base des ``account.move.line`` correspondant ;
* appliquer les portées de date propres à chaque expression ;
* déléguer l'évaluation au paquet ``engine`` et retourner les valeurs.

Le rendu visuel (client action OWL) relève du lot L3 et n'est pas ici.
"""

from collections import defaultdict
from dateutil.relativedelta import relativedelta

from odoo import api, fields, models
from odoo.exceptions import ValidationError
from odoo.tools import date_utils

from ..engine.resolver import (
    ExpressionSpec,
    compile_expressions,
    evaluate_all,
)
from ..engine.formula import FormulaError

#: Portées de date qui ignorent la borne basse de la période.
CUMULATIVE_SCOPES = ("from_beginning", "from_fiscalyear")

#: Portées de date qui produisent un solde à une date, sans borne basse.
POINT_IN_TIME_SCOPES = ("to_beginning_of_period", "to_beginning_of_fiscalyear")


class AccountReport(models.Model):
    _inherit = "account.report"

    expodo_cumulative = fields.Boolean(
        string="Cumulative statement",
        default=False,
        help="Read balances from the very beginning rather than over the "
             "displayed period. Set it on statements of position — balance "
             "sheet, aged balances — and leave it off on statements of "
             "activity such as the profit and loss.",
    )

    expodo_chart_prefix = fields.Char(
        string="Chart prefix",
        help="Account code prefix that exists only in the accounting "
             "framework this presentation targets. Used to offer a "
             "supranational statement — SYSCOHADA covers seventeen states, "
             "and Odoo binds a report to a single country.",
    )

    def _expodo_chart_matches(self):
        """Vrai si le plan comptable de la société porte le préfixe déclaré.

        La reconnaissance se fait sur l'existence d'un compte, pas sur son
        mouvement : un plan SYSCOHADA fraîchement installé n'a encore aucune
        écriture en classe 8, et il est pourtant bien un plan SYSCOHADA.
        """
        self.ensure_one()
        if not self.expodo_chart_prefix:
            return False
        return bool(self.env["account.account"].search_count([
            ("company_ids", "in", self.env.company.id),
            ("code", "=like", self.expodo_chart_prefix + "%"),
        ]))

    # ------------------------------------------------------------------
    # Options
    # ------------------------------------------------------------------

    #: Correspondance entre le champ natif `default_opening_date_filter` et
    #: les périodes que ce moteur sait construire.
    EXPODO_OPENING_PERIODS = {
        "this_year": "fiscalyear",
        "this_quarter": "quarter",
        "this_month": "month",
        "previous_month": "previous_month",
        "today": "to_today",
        "previous_quarter": "previous_quarter",
        "previous_year": "previous_year",
        "this_return_period": "return_period",
        "previous_return_period": "previous_return_period",
    }


    def _expodo_return_periodicity(self):
        """Périodicité de dépôt applicable à ce rapport.

        Les déclarations de taxes déclarent leur période d'ouverture en
        `this_return_period` ou `previous_return_period` : la période de dépôt
        en cours, ou la précédente. Ces deux valeurs n'étaient pas reconnues et
        retombaient sur l'exercice : une société française ouvrait sa CA3 sur
        les douze mois de l'année au lieu du mois qu'elle doit déclarer, et le
        total à payer affiché n'était celui d'aucune déclaration.

        La périodicité vient du type de déclaration rattaché au rapport, que
        le module des déclarations porte. À défaut, le mois : c'est le régime
        de dépôt le plus répandu, et celui qu'Odoo retient lui aussi par
        défaut.
        """
        self.ensure_one()
        if "expodo.return.type" in self.env:
            racine = self.root_report_id or self
            type_retour = self.env["expodo.return.type"].sudo().search([
                ("report_id", "in", list({self.id, racine.id})),
                ("company_id", "in", (False, self.env.company.id)),
            ], limit=1)
            if type_retour.periodicity:
                return type_retour.periodicity
        return "monthly"

    def _expodo_default_date_options(self, period=None, anchor=None):
        """Période par défaut à l'ouverture d'un rapport.

        Le parcours utilisateur (CDC §0.1) exige qu'un rapport s'affiche sans
        paramétrage préalable. La période retenue est celle que le rapport
        déclare dans `default_opening_date_filter`, un champ natif
        d'`account.report` ; à défaut, l'exercice en cours.

        La méthode n'est plus `@api.model` : elle a besoin de l'enregistrement
        pour lire ce champ. Ignoré, il ouvrait le livre-journal sur l'exercice
        entier alors qu'il déclare le mois — des dizaines de milliers de lignes
        sur une base réelle, là où l'utilisateur en attend quelques centaines.
        """
        if period is None:
            declare = self.default_opening_date_filter if len(self) == 1 else None
            period = self.EXPODO_OPENING_PERIODS.get(declare, "fiscalyear")
        anchor = anchor or fields.Date.context_today(self)
        company = self.env.company

        if period == "fiscalyear":
            dates = company.compute_fiscalyear_dates(anchor)
            date_from, date_to = dates["date_from"], dates["date_to"]
        elif period == "month":
            date_from = date_utils.start_of(anchor, "month")
            date_to = date_utils.end_of(anchor, "month")
        elif period == "previous_month":
            fin_precedent = date_utils.start_of(anchor, "month") - relativedelta(days=1)
            date_from = date_utils.start_of(fin_precedent, "month")
            date_to = fin_precedent
        elif period == "to_today":
            # De l'ouverture de l'exercice à **aujourd'hui**.
            #
            # C'est la période d'une balance âgée : l'ancienneté d'une créance
            # se compte depuis son échéance jusqu'au jour où on la lit. Arrêtée
            # à la fin d'un mois écoulé, une facture échue depuis s'affichait
            # comme non échue, et le lecteur ne relançait pas ceux qu'il
            # fallait. Arrêtée à la fin de l'exercice, elle paraîtrait plus
            # ancienne qu'elle n'est.
            dates = company.compute_fiscalyear_dates(anchor)
            date_from = dates["date_from"]
            date_to = anchor
        elif period == "quarter":
            date_from = date_utils.start_of(anchor, "quarter")
            date_to = date_utils.end_of(anchor, "quarter")
        elif period == "previous_quarter":
            fin_precedent = date_utils.start_of(anchor, "quarter") - relativedelta(days=1)
            date_from = date_utils.start_of(fin_precedent, "quarter")
            date_to = fin_precedent
        elif period == "previous_year":
            dates = company.compute_fiscalyear_dates(anchor)
            dates = company.compute_fiscalyear_dates(
                dates["date_from"] - relativedelta(days=1))
            date_from, date_to = dates["date_from"], dates["date_to"]
        elif period in ("return_period", "previous_return_period"):
            # Une déclaration se dépose pour une période close : en septembre,
            # on déclare août. D'où la période précédente par défaut sur la
            # plupart des déclarations nationales.
            precedente = period == "previous_return_period"
            periodicite = self._expodo_return_periodicity()
            if periodicite == "quarterly":
                debut = date_utils.start_of(anchor, "quarter")
                if precedente:
                    debut = date_utils.start_of(debut - relativedelta(days=1), "quarter")
                date_from, date_to = debut, date_utils.end_of(debut, "quarter")
            elif periodicite == "yearly":
                dates = company.compute_fiscalyear_dates(anchor)
                if precedente:
                    dates = company.compute_fiscalyear_dates(
                        dates["date_from"] - relativedelta(days=1))
                date_from, date_to = dates["date_from"], dates["date_to"]
            else:
                debut = date_utils.start_of(anchor, "month")
                if precedente:
                    debut = date_utils.start_of(debut - relativedelta(days=1), "month")
                date_from, date_to = debut, date_utils.end_of(debut, "month")
        else:
            raise ValidationError(
                self.env._("Unknown period: %(period)s", period=period)
            )

        return {
            "mode": "range",
            "filter": period,
            "date_from": date_from,
            "date_to": date_to,
        }

    def _expodo_anchor_date_options(self, date_options):
        """Ramène la borne d'ouverture d'un état arrêté à une date.

        Un bilan, une balance âgée, un relevé de compte n'ont pas de date de
        début : ils décrivent une situation à une date. Leurs expressions
        lisent depuis l'origine et ignorent donc `date_from`. L'interface
        n'affiche qu'une borne pour ces états — mais `date_from` doit rester
        peuplée, car c'est elle qui donne son amplitude à la colonne de
        comparaison : sans exercice de référence, « période précédente » ne
        reculerait plus d'un exercice mais d'un jour.

        La borne est donc ramenée au début de l'exercice de la date de
        clôture, ce qui laisse la comparaison reculer d'exercice en exercice.
        """
        if self.filter_date_range:
            return date_options
        debut = self.env.company.compute_fiscalyear_dates(
            date_options["date_to"])["date_from"]
        return {**date_options, "date_from": debut}

    @api.model
    def _expodo_build_comparison_periods(self, date_options, comparison):
        """Développe une demande de comparaison en périodes datées.

        ``comparison`` : ``{'filter': 'previous_period'|'same_last_year'|'no_comparison',
        'number_period': int}``.
        """
        if not comparison or comparison.get("filter") in (None, "no_comparison"):
            return []

        periods = []
        date_from = date_options["date_from"]
        date_to = date_options["date_to"]
        count = max(1, int(comparison.get("number_period") or 1))

        for index in range(1, count + 1):
            if comparison["filter"] == "same_last_year":
                shift = relativedelta(years=index)
                periods.append({
                    "date_from": date_from - shift,
                    "date_to": date_to - shift,
                    "mode": date_options["mode"],
                })
            elif comparison["filter"] == "previous_period":
                if date_options.get("filter") == "month":
                    shift = relativedelta(months=index)
                elif date_options.get("filter") == "quarter":
                    shift = relativedelta(months=3 * index)
                else:
                    shift = relativedelta(years=index)
                shifted_from = date_from - shift
                periods.append({
                    "date_from": shifted_from,
                    "date_to": date_utils.end_of(
                        date_to - shift, "month"
                    ) if date_options.get("filter") in ("month", "quarter") else date_to - shift,
                    "mode": date_options["mode"],
                })
            else:
                raise ValidationError(
                    self.env._(
                        "Unknown comparison mode: %(mode)s",
                        mode=comparison["filter"],
                    )
                )
        return periods

    def _expodo_get_options(self, previous_options=None):
        """Construit le jeu d'options complet du rapport.

        Chaque période — la principale et chaque comparaison — devient un
        *groupe de colonnes*. Les moteurs terminaux sont évalués une fois par
        groupe de colonnes, jamais une fois par ligne (CDC T-07b).
        """
        self.ensure_one()
        # Les options qui reviennent du navigateur portent des dates en
        # chaînes : elles ont fait l'aller-retour par JSON. Les réutiliser
        # telles quelles casse au premier changement de filtre, et le défaut
        # ne se voit qu'à l'usage — un test qui passe des objets `date` ne
        # l'atteint jamais.
        previous_options = self._expodo_normalize_options(previous_options)

        date_options = previous_options.get("date") or self._expodo_default_date_options()
        # Une borne absente ramène la période par défaut plutôt que de laisser
        # un `None` se propager jusqu'au formatage.
        if not date_options.get("date_from") or not date_options.get("date_to"):
            date_options = self._expodo_default_date_options()
        date_options = self._expodo_anchor_date_options(date_options)
        if date_options["date_from"] > date_options["date_to"]:
            raise ValidationError(
                self.env._("The start date is later than the end date.")
            )
        comparison = previous_options.get("comparison") or {"filter": "no_comparison"}

        options = {
            "report_id": self.id,
            "date": date_options,
            "comparison": comparison,
            "all_entries": previous_options.get("all_entries", False),
            "journal_ids": previous_options.get("journal_ids", []),
            "partner_ids": previous_options.get("partner_ids", []),
            "company_ids": previous_options.get("company_ids")
                           or self.env.companies.ids,
            "unfolded_lines": previous_options.get("unfolded_lines", []),
            "column_groups": {},
        }

        column_groups = {"main": {"date": date_options}}
        for index, period in enumerate(
            self._expodo_build_comparison_periods(date_options, comparison), start=1
        ):
            column_groups["comparison_%d" % index] = {"date": period}
        options["column_groups"] = column_groups

        return options

    @api.model
    def _expodo_normalize_options(self, options):
        """Reconvertit en dates toute chaîne de date présente dans les options.

        Tolérant par construction : les options peuvent venir du navigateur
        (dates en chaînes, structure complète), d'un appel Python (objets
        `date`, structure partielle) ou être absentes.
        """
        if not options:
            return {}

        def convert(value):
            if not isinstance(value, str):
                return value
            try:
                return fields.Date.to_date(value)
            except (ValueError, TypeError) as error:
                # Ne pas laisser passer la chaîne : elle exploserait plus loin
                # avec un message incompréhensible du type « 'str' object has
                # no attribute 'isoformat' ». Une date invalide doit être
                # signalée là où elle est reçue.
                raise ValidationError(
                    self.env._("Invalid date: %(value)s", value=value)
                ) from error

        normalized = dict(options)
        if isinstance(normalized.get("date"), dict):
            normalized["date"] = {
                key: convert(value) if key in ("date_from", "date_to") else value
                for key, value in normalized["date"].items()
            }
        if isinstance(normalized.get("column_groups"), dict):
            normalized["column_groups"] = {
                group_key: {
                    **group,
                    "date": {
                        key: convert(value) if key in ("date_from", "date_to") else value
                        for key, value in (group.get("date") or {}).items()
                    },
                }
                for group_key, group in normalized["column_groups"].items()
            }
        return normalized

    # ------------------------------------------------------------------
    # Domaine de base
    # ------------------------------------------------------------------

    def _expodo_get_date_bounds(self, options, column_group_key, date_scope):
        """Bornes de date effectives d'une expression, selon sa portée.

        Retourne ``(date_from, date_to)``, ``date_from`` pouvant être ``None``
        pour les portées cumulatives depuis l'origine.
        """
        self.ensure_one()
        group_dates = options["column_groups"][column_group_key]["date"]
        date_from, date_to = group_dates["date_from"], group_dates["date_to"]
        company = self.env.company

        if date_scope == "strict_range":
            return date_from, date_to
        if date_scope == "from_beginning":
            return None, date_to
        if date_scope == "from_fiscalyear":
            return company.compute_fiscalyear_dates(date_to)["date_from"], date_to
        if date_scope == "to_beginning_of_period":
            return None, date_from - relativedelta(days=1)
        if date_scope == "to_beginning_of_fiscalyear":
            fiscalyear_start = company.compute_fiscalyear_dates(date_to)["date_from"]
            return None, fiscalyear_start - relativedelta(days=1)
        if date_scope == "previous_return_period":
            # La période de déclaration précédente dépend de la périodicité de
            # TVA paramétrée sur la société.
            return self._expodo_previous_return_period(date_from)

        raise ValidationError(
            self.env._("Unsupported date scope: %(scope)s", scope=date_scope)
        )

    def _expodo_previous_return_period(self, date_from):
        """Bornes de la période de déclaration précédant ``date_from``.

        La périodicité est portée par ``res.company.expodo_tax_periodicity``,
        que ce module ajoute : Community n'en possède aucune, le champ
        ``account_tax_periodicity`` étant propre à l'édition Enterprise.
        """
        months = self.env.company._expodo_periodicity_months()
        previous_end = date_from - relativedelta(days=1)
        previous_start = date_utils.start_of(
            previous_end - relativedelta(months=months - 1), "month"
        )
        return previous_start, previous_end

    def _expodo_base_domain(self, options, column_group_key, date_scope):
        """Domaine ``account.move.line`` commun à tous les moteurs terminaux."""
        self.ensure_one()
        date_from, date_to = self._expodo_get_date_bounds(
            options, column_group_key, date_scope
        )

        domain = [
            ("company_id", "in", options["company_ids"]),
            ("date", "<=", date_to),
        ]
        if date_from is not None:
            domain.append(("date", ">=", date_from))

        if not options.get("all_entries"):
            domain.append(("parent_state", "=", "posted"))
        else:
            domain.append(("parent_state", "!=", "cancel"))

        if options.get("journal_ids"):
            domain.append(("journal_id", "in", options["journal_ids"]))
        if options.get("partner_ids"):
            domain.append(("partner_id", "in", options["partner_ids"]))

        return domain

    @api.model
    def _expodo_account_info(self, accounts):
        """Code et étiquettes de chaque compte, pour le moteur `account_codes`.

        Le code d'un compte dépend de la société : `account.code` est calculé
        pour la société active. Dans un périmètre multi-société, un compte
        codé chez une filiale mais pas chez la société active renvoie donc un
        code vide — et un code vide ne correspond à aucun préfixe. Le compte
        et tout ce qu'il porte disparaissaient alors du bilan et du compte de
        résultat, sans ligne d'écart pour le signaler : les deux états
        restaient équilibrés entre eux, simplement amputés.

        On retombe donc sur le code que le compte porte dans l'une de ses
        propres sociétés, exactement comme le fait l'édition Enterprise.
        """
        infos = {}
        for compte in accounts:
            code = compte.code
            if not code:
                for societe in compte.company_ids:
                    code = compte.with_company(societe).code
                    if code:
                        break
            infos[compte.id] = (code or "", set(compte.tag_ids.ids))
        return infos

    # ------------------------------------------------------------------
    # Rendu
    # ------------------------------------------------------------------

    def _expodo_collect_specs(self):
        """Convertit les expressions du rapport en spécifications de moteur."""
        self.ensure_one()
        specs = []
        for line in self.line_ids:
            children_codes = [child.code for child in line.children_ids if child.code]
            for expression in line.expression_ids:
                if not line.code:
                    # Une ligne sans code ne peut être ni référencée ni agrégée.
                    continue
                specs.append(
                    ExpressionSpec(
                        line_code=line.code,
                        label=expression.label,
                        engine=expression.engine,
                        formula=expression.formula,
                        subformula=expression.subformula,
                        children_codes=children_codes,
                    )
                )
        return specs

    def _expodo_compute_values(self, options, column_group_key):
        """Évalue toutes les expressions du rapport pour un groupe de colonnes.

        Retourne ``{(code_ligne, libelle): valeur}``.
        """
        self.ensure_one()

        specs = self._expodo_collect_specs()
        try:
            compiled = compile_expressions(specs)
        except FormulaError as error:
            raise ValidationError(
                self.env._(
                    "Report %(report)s: %(error)s",
                    report=self.display_name, error=str(error),
                )
            ) from error

        # Les feuilles sont regroupées par moteur, afin de n'émettre qu'un
        # aller-retour SQL par moteur (CDC T-07b).
        expressions_by_key = {
            (expression.report_line_id.code, expression.label): expression
            for line in self.line_ids
            for expression in line.expression_ids
            if line.code
        }
        leaves_by_engine = defaultdict(list)
        for key, compiled_expression in compiled.items():
            if compiled_expression.is_leaf and key in expressions_by_key:
                leaves_by_engine[compiled_expression.spec.engine].append(
                    expressions_by_key[key]
                )

        leaf_values = {}
        expression_model = self.env["account.report.expression"]
        for engine, expressions in leaves_by_engine.items():
            records = expression_model.browse([e.id for e in expressions])
            # La méthode est résolue sur le recordset lui-même : elle est donc
            # déjà liée, et ne prend que les arguments explicites.
            handler = getattr(records, "_expodo_engine_%s" % engine, None)
            if handler is None:
                # Un moteur inconnu ne doit pas produire un zéro silencieux.
                raise ValidationError(
                    self.env._("Unsupported expression engine: %(engine)s", engine=engine)
                )
            by_id = handler(self, options, column_group_key)
            for expression in expressions:
                leaf_values[
                    (expression.report_line_id.code, expression.label)
                ] = by_id.get(expression.id, 0.0)

        try:
            # `integer_rounding` est un champ natif d'`account.report` : la
            # CA3 française y déclare HALF-UP. L'ignorer revient à appliquer
            # l'arrondi bancaire de Python et à sous-déclarer.
            return evaluate_all(
                compiled, leaf_values,
                rounding=self.integer_rounding or "HALF-UP",
            )
        except FormulaError as error:
            raise ValidationError(
                self.env._(
                    "Report %(report)s: %(error)s",
                    report=self.display_name, error=str(error),
                )
            ) from error
