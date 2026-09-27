# -*- coding: utf-8 -*-
# Copyright 2026 Expodo (https://expodo.fr)
# License LGPL-3
"""Sérialisation d'un rapport pour l'interface interactive.

Le composant OWL ne connaît rien de la structure des rapports : il reçoit une
liste plate de lignes déjà calculées, chacune portant son niveau hiérarchique
et son état de dépliage. Toute la logique reste côté serveur, ce qui permet de
la tester sans navigateur.

Les lignes à groupement dynamique ne sont pas développées au premier rendu :
elles sont renvoyées comme dépliables, et leurs enfants ne sont calculés qu'à
la demande (CDC F-21).
"""

from odoo import api, models
from odoo.exceptions import AccessError, UserError
from odoo.tools.misc import format_amount, format_date


class AccountReport(models.Model):
    _inherit = "account.report"

    #: Groupes autorisés à consulter les rapports.
    #:
    #: En v19, la hiérarchie des groupes comptables n'est pas linéaire :
    #: `group_account_manager` implique « Facturation » mais **pas**
    #: « Lecture seule », qui n'est impliquée que par `group_account_user`.
    #: Ne tester que `group_account_readonly` rejetterait donc
    #: l'administrateur comptable — précisément la personne censée lire les
    #: rapports. Les quatre groupes sont donc testés explicitement.
    #: Groupes autorises a ouvrir un rapport.
    #:
    #: La liste reproduit exactement celle que le coeur d'Odoo applique au
    #: modele `account.report` : administrateur, lecture seule et basique.
    #: Elle portait `group_account_invoice`, que le coeur refuse : un
    #: utilisateur de facturation passait notre controle puis se heurtait a
    #: l'erreur brute de l'ORM, laquelle enumere des noms de groupes internes
    #: au lieu de dire ce qui manque.
    EXPODO_REPORT_GROUPS = (
        "account.group_account_readonly",
        "account.group_account_basic",
        "account.group_account_user",
        "account.group_account_manager",
    )

    def _expodo_check_access(self):
        """Réserve les rapports aux utilisateurs de la comptabilité (CDC F-40).

        Les quatre points d'entrée de l'interface sont publics car appelables
        par RPC ; sans ce contrôle, tout utilisateur interne pourrait lire le
        bilan de la société.
        """
        if not any(self.env.user.has_group(g) for g in self.EXPODO_REPORT_GROUPS):
            raise AccessError(
                self.env._("Accounting reports require an accounting access right.")
            )

    # ------------------------------------------------------------------
    # Sérialisation
    # ------------------------------------------------------------------

    def _expodo_column_defs(self):
        """Définition des colonnes, ordonnées."""
        self.ensure_one()
        return [
            {
                "name": column.name,
                "label": column.expression_label,
                "figure_type": column.figure_type,
                "blank_if_zero": column.blank_if_zero,
            }
            for column in self.column_ids.sorted("sequence")
        ]

    def _expodo_format_value(self, value, figure_type, blank_if_zero=False):
        """Met en forme une valeur selon le type d'affichage de sa colonne."""
        self.ensure_one()
        if value is None:
            return ""
        if blank_if_zero and not value:
            return ""
        if figure_type == "integer":
            return "%d" % round(value)
        if figure_type == "percentage":
            return "%.2f %%" % value
        if figure_type == "float":
            return "%.2f" % value
        if figure_type == "date":
            # `format_date` respecte la locale de l'utilisateur : un état lu
            # en allemand doit afficher ses dates à l'allemande, sinon le
            # lecteur inverse le jour et le mois sans s'en rendre compte.
            return format_date(self.env, value)
        if figure_type == "string":
            # Un identifiant de tiers devient son nom. Afficher le numéro
            # laisserait le lecteur devant une colonne de chiffres sans
            # signification, là où il attend un nom.
            if isinstance(value, (int, float)):
                partenaire = self.env["res.partner"].browse(int(value)).exists()
                return partenaire.display_name if partenaire else ""
            return str(value)
        # `format_amount` produit une chaîne simple, contrairement au champ
        # QWeb monétaire qui renvoie du balisage HTML : celui-ci ne peut pas
        # transiter tel quel dans une réponse JSON.
        return format_amount(self.env, value, self.env.company.currency_id)

    @api.model
    def _expodo_line_id(self, *parts):
        """Identifiant stable d'une ligne, y compris pour les sous-groupes."""
        return "|".join(str(part) for part in parts)

    def _expodo_serialize_report_lines(self, options, values_by_group):
        """Lignes statiques du rapport, à plat, dans l'ordre d'affichage."""
        self.ensure_one()
        columns = self._expodo_column_defs()
        serialized = []

        def walk(lines):
            for line in lines.sorted("sequence"):
                has_groupby = bool(line._expodo_groupby_chain(options))
                cells = []
                for column in columns:
                    cell_values = {}
                    for group_key, values in values_by_group.items():
                        raw = values.get((line.code or "", column["label"]))
                        cell_values[group_key] = raw
                    cells.append({
                        "label": column["label"],
                        "values": {
                            key: self._expodo_format_value(
                                raw, column["figure_type"], column["blank_if_zero"]
                            )
                            for key, raw in cell_values.items()
                        },
                        "raw": cell_values,
                        # Seule une expression terminale est auditable : une
                        # agrégation n'a pas d'écritures sous-jacentes (F-22).
                        "auditable": line._expodo_is_auditable(column["label"]),
                    })

                serialized.append({
                    "id": self._expodo_line_id("line", line.id),
                    "line_id": line.id,
                    "name": line.name,
                    "level": line.hierarchy_level or 0,
                    "unfoldable": has_groupby,
                    "unfolded": False,
                    "columns": cells,
                    "code": line.code or "",
                })
                walk(line.children_ids)

        walk(self.line_ids.filtered(lambda l: not l.parent_id))
        return serialized

    @api.model
    def expodo_resolve_tax_report(self):
        """Rapport de taxes applicable à la société courante.

        Cherche la déclaration déclarée pour le pays de la société, et retombe
        sur le rapport de taxes générique d'Odoo si le pays n'en fournit
        aucune. C'est ce qui permet au module de servir toutes les
        localisations sans en dépendre d'aucune.
        """
        self.env["account.report"].check_access("read")
        country = self.env.company.account_fiscal_country_id or self.env.company.country_id
        generic = self.env.ref("account.generic_tax_report", raise_if_not_found=False)

        # Deux tentatives, de la plus précise à la plus large. Toutes les
        # localisations ne rattachent pas leur déclaration au rapport
        # générique : s'en tenir à ce seul critère ferait retomber sur le
        # rapport générique des pays qui fournissent pourtant le leur.
        candidates = self.search([("country_id", "=", country.id)])
        rattachees = candidates.filtered(
            lambda r: generic and r.root_report_id == generic)
        localised = self._expodo_declaration_principale(rattachees) \
            or candidates.filtered(lambda r: not r.root_report_id)[:1] \
            or candidates[:1]

        report = localised or generic
        if not report:
            raise UserError(self.env._(
                "No tax return is available for %(country)s. Install the "
                "accounting localisation of your country.",
                country=country.display_name or "—",
            ))
        return report.id

    @api.model
    def _expodo_declaration_principale(self, candidates):
        """Parmi les déclarations d'un pays, celle que ses taxes alimentent.

        Plusieurs localisations en fournissent plusieurs : l'Espagne en livre
        six, le Mod 303 pour la TVA, le Mod 111 pour les retenues sur
        salaires, le Mod 115 pour les loyers. Retenir la première venue
        ouvrait le menu sur le Mod 111 — une déclaration de retenues à la
        source, là où le comptable qui clique « Déclaration de taxes » attend
        la TVA. Les autres restent atteignables par le sélecteur de
        présentation, mais le défaut ouvrait sur la mauvaise.

        Le départage se fait sur les faits plutôt que sur le nom : on compare
        les étiquettes de taxes que chaque déclaration lit à celles que
        portent réellement les taxes de vente et d'achat de la société. La
        déclaration de TVA en recouvre l'essentiel, les autres une poignée.
        Mesuré en Espagne : 48 pour le Mod 303, 8 pour le Mod 111.

        Aucun nom, aucun numéro de formulaire n'est codé en dur : la règle
        vaut pour les localisations qu'on n'a pas sous la main.
        """
        if len(candidates) < 2:
            return candidates[:1]

        taxes = self.env["account.tax"].search([
            ("type_tax_use", "in", ("sale", "purchase")),
        ])
        portees = set(taxes.repartition_line_ids.tag_ids.ids)
        if not portees:
            return candidates[:1]

        def recouvrement(rapport):
            expressions = rapport.line_ids.expression_ids.filtered(
                lambda e: e.engine == "tax_tags")
            if not expressions:
                return 0
            try:
                return len(set(expressions._get_matching_tags().ids) & portees)
            except Exception:
                # Une localisation peut nommer une étiquette qui n'existe pas
                # dans la base : on la traite comme sans recouvrement plutôt
                # que de faire échouer l'ouverture du menu.
                return 0

        # À recouvrement égal, le plus petit identifiant : l'ordre
        # d'installation de la localisation, stable d'une base à l'autre.
        return candidates.sorted(lambda r: (-recouvrement(r), r.id))[:1]

    @api.model
    def expodo_resolve_statutory_report(self, kind):
        """État financier applicable à la société, selon son pays.

        Même mécanisme que pour la déclaration de taxes : un état universel
        sert de racine, et les présentations nationales s'y rattachent par
        `root_report_id`. Si le pays de la société en fournit une, elle est
        retenue ; sinon l'état universel s'applique.

        L'utilisateur voit donc un seul menu « Balance Sheet », qui affiche le
        bilan au format de son référentiel s'il existe. Rien à chercher, rien
        à configurer, et aucun menu étranger dans la liste.

        :param kind: ``balance_sheet`` ou ``profit_loss``.
        """
        self.env["account.report"].check_access("read")
        racines = {
            "balance_sheet": "expodo_account_reports.report_balance_sheet",
            "profit_loss": "expodo_account_reports.report_profit_loss",
        }
        if kind not in racines:
            raise UserError(self.env._("Unknown report kind: %(kind)s", kind=kind))

        universel = self.env.ref(racines[kind], raise_if_not_found=False)
        if not universel:
            raise UserError(self.env._(
                "The universal report %(xmlid)s is missing.", xmlid=racines[kind]))

        pays = self.env.company.account_fiscal_country_id or self.env.company.country_id
        national = self.search([
            ("root_report_id", "=", universel.id),
            ("country_id", "=", pays.id),
        ], limit=1)
        if national:
            return national.id

        # Référentiels supranationaux.
        #
        # Le SYSCOHADA est commun à dix-sept États, et Odoo n'accepte qu'un
        # pays par état : ses présentations ne portent donc aucun pays et ne
        # pouvaient être retenues par la recherche ci-dessus. Résultat, elles
        # existaient sans qu'aucun utilisateur puisse les atteindre par le
        # menu — un état inatteignable est un état qui n'existe pas.
        #
        # On les retient sur la reconnaissance du plan comptable plutôt que
        # sur le pays : chaque présentation déclare un préfixe de compte qui
        # n'existe que dans son référentiel.
        supranational = self.search([
            ("root_report_id", "=", universel.id),
            ("country_id", "=", False),
            ("expodo_chart_prefix", "!=", False),
        ])
        for candidat in supranational:
            if candidat._expodo_chart_matches():
                return candidat.id

        return universel.id

    def expodo_get_report_data(self, previous_options=None):
        """Point d'entrée de l'interface : options, colonnes et lignes.

        Méthode **publique** : elle est appelée par RPC depuis le navigateur,
        et Odoo interdit d'exposer ainsi une méthode préfixée par un tiret bas.
        Le contrôle d'accès doit donc être explicite ici — l'ORM ne le fait pas
        à notre place sur un appel de méthode (CDC F-40).
        """
        self.ensure_one()
        self._expodo_check_access()
        options = self._expodo_get_options(previous_options)

        values_by_group = {
            group_key: self._expodo_compute_values(options, group_key)
            for group_key in options["column_groups"]
        }

        return {
            "report": {
                "id": self.id,
                "name": self.display_name,
                "variants": self._expodo_available_variants(),
                "filter_date_range": self.filter_date_range,
                "filter_journals": self.filter_journals,
                "journals": self._expodo_journal_choices(options),
                "filter_partner": self.filter_partner,
                "filter_show_draft": self.filter_show_draft,
                "filter_unfold_all": self.filter_unfold_all,
                "search_bar": self.search_bar,
            },
            "options": self._expodo_serialize_options(options),
            "column_groups": [
                {
                    "key": key,
                    "label": self._expodo_column_group_label(key, group),
                }
                for key, group in options["column_groups"].items()
            ],
            "columns": self._expodo_column_defs(),
            "lines": self._expodo_serialize_report_lines(options, values_by_group),
            # Un rapport sans aucune ligne définie n'est pas un rapport vide de
            # données : c'est un rapport que la localisation du pays n'a pas
            # rempli. Le cas se présente hors de France — la déclaration
            # américaine, par exemple, existe sans aucune ligne. L'utilisateur
            # ouvrait alors un tableau vide, sans rien pour comprendre s'il
            # n'avait pas d'écritures ou si le rapport n'existait pas chez lui.
            "notice": self.env._(
                "This report has no lines defined for %(country)s. The "
                "localisation for this country does not provide them; the "
                "report will stay empty until it does.",
                country=(self.country_id or self.env.company.account_fiscal_country_id
                         or self.env.company.country_id).display_name or "—",
            ) if not self.line_ids else False,
        }

    def _expodo_journal_choices(self, options):
        """Journaux proposés au filtre, pour les états qui le déclarent.

        Les rapports déclarent `filter_journals` et le moteur lit bien
        `journal_ids`, mais l'en-tête n'offrait aucun moyen de le renseigner :
        le filtre existait des deux côtés et n'avait pas de commande. Or
        n'ouvrir qu'un journal est un geste quotidien sur un grand livre.

        La liste suit le périmètre de sociétés retenu, sinon on proposerait
        des journaux dont l'état ne lira jamais les écritures.
        """
        self.ensure_one()
        if not self.filter_journals:
            return []
        journaux = self.env["account.journal"].search([
            ("company_id", "in", options.get("company_ids") or self.env.companies.ids),
        ], order="company_id, code")
        plusieurs = len(set(journaux.mapped("company_id"))) > 1
        return [
            {
                "id": journal.id,
                "name": "%s — %s" % (journal.company_id.name, journal.name)
                        if plusieurs else journal.name,
                "code": journal.code or "",
            }
            for journal in journaux
        ]

    def _expodo_available_variants(self):
        """Présentations que la société peut légitimement ouvrir ici.

        Un même état existe souvent en plusieurs présentations rattachées à un
        état racine par `root_report_id` : l'état universel, celle du plan
        comptable national, et parfois une seconde lecture du même exercice —
        les soldes intermédiaires de gestion français, le tableau de flux en
        méthode directe.

        Le menu n'en désigne qu'une. Sans cette liste, les autres existaient
        sans qu'aucun utilisateur puisse les atteindre : le SIG et la méthode
        directe étaient livrés, testés, traduits, et invisibles. L'édition
        Enterprise offre le même choix par un sélecteur en en-tête.

        Trois règles d'exclusion, dans cet ordre :
        - une présentation d'un autre pays n'est pas proposée ;
        - une présentation qui déclare un plan comptable que la société n'a
          pas ne l'est pas non plus — c'est ce qui écarte le SYSCOHADA d'une
          société française ;
        - une présentation sans aucune ligne ne l'est pas : la proposer
          reviendrait à offrir un tableau vide.
        """
        self.ensure_one()
        racine = self.root_report_id or self
        candidats = racine | self.search([("root_report_id", "=", racine.id)])
        pays = self.env.company.account_fiscal_country_id or self.env.company.country_id

        retenus = self.browse()
        for candidat in candidats:
            if not candidat.line_ids:
                continue
            if candidat.country_id and candidat.country_id != pays:
                continue
            if candidat.expodo_chart_prefix and not candidat._expodo_chart_matches():
                continue
            retenus |= candidat

        if len(retenus) < 2:
            # Une seule présentation : le sélecteur n'aurait rien à choisir.
            return []

        return [
            {"id": rapport.id, "name": rapport.display_name}
            for rapport in retenus.sorted(
                key=lambda r: (bool(r.root_report_id), r.display_name))
        ]

    def expodo_expand_line(self, line_id, options, parent_group=None, level=0):
        """Développe une ligne groupée, à la demande de l'interface."""
        self.ensure_one()
        self._expodo_check_access()
        line = self.env["account.report.line"].browse(line_id)
        options = self._expodo_deserialize_options(options)
        columns = self._expodo_column_defs()

        parent_domain = None
        if parent_group:
            parent_domain = line._expodo_group_domain(
                parent_group["field"], parent_group["id"]
            )

        # Une seule ligne par groupement, portant les valeurs de **tous** les
        # groupes de colonnes.
        #
        # La boucle ajoutait auparavant une ligne par groupe : avec une
        # comparaison active, chaque compte apparaissait deux fois, chacun ne
        # portant que la moitié des chiffres. À l'écran les doublons se
        # confondaient, mais l'export les rendait visibles — des lignes à
        # quatre colonnes au milieu de lignes à sept. Découvert en ouvrant un
        # classeur exporté.
        fusionnees = {}
        ordre = []
        for group_key in options["column_groups"]:
            rows = line._expodo_expand(
                self, options, group_key, level=level, parent_domain=parent_domain
            )
            for row in rows:
                cle = (row["group_field"], row["group_id"])
                if cle not in fusionnees:
                    ordre.append(cle)
                    fusionnees[cle] = {
                        "id": self._expodo_line_id(
                            "grp", line.id, level, row["group_field"], row["group_id"]
                        ),
                        "line_id": line.id,
                        "name": row["name"],
                        "level": (line.hierarchy_level or 0) + level + 1,
                        "unfoldable": row["expandable"],
                        "unfolded": False,
                        "group": {"field": row["group_field"], "id": row["group_id"]},
                        "next_level": level + 1,
                        "columns": [
                            {"label": column["label"], "values": {}, "raw": {},
                             "auditable": True}
                            for column in columns
                        ],
                    }
                for index, column in enumerate(columns):
                    cellule = fusionnees[cle]["columns"][index]
                    valeur = row["values"].get(column["label"])
                    cellule["values"][group_key] = self._expodo_format_value(
                        valeur, column["figure_type"], column["blank_if_zero"]
                    )
                    cellule["raw"][group_key] = valeur

        # Un groupe absent d'une ligne vaut zéro, et non « rien ».
        #
        # Un compte présent sur l'exercice mais sans mouvement sur la période
        # comparée n'apparaît pas dans le groupement de cette période. Laisser
        # la cellule vide oblige le lecteur à deviner : le compte n'existait-il
        # pas, ou n'a-t-il simplement rien enregistré ? Sur une comparaison
        # d'exercices, c'est la seconde réponse qui est vraie, et zéro la dit.
        for ligne in fusionnees.values():
            for index, cellule in enumerate(ligne["columns"]):
                colonne = columns[index]
                for group_key in options["column_groups"]:
                    if group_key in cellule["raw"]:
                        continue
                    cellule["raw"][group_key] = 0.0
                    cellule["values"][group_key] = self._expodo_format_value(
                        0.0, colonne["figure_type"], colonne["blank_if_zero"]
                    )

        return [fusionnees[cle] for cle in ordre]

    # ------------------------------------------------------------------
    # Audit — accès aux écritures sous-jacentes (F-20)
    # ------------------------------------------------------------------

    def expodo_action_audit(self, line_id, options, group=None, column_group_key="main",
                            expression_label=None):
        """Ouvre les écritures qui composent une valeur.

        Le domaine reproduit exactement celui qui a servi au calcul : c'est la
        condition pour que la somme des écritures affichées égale le montant
        cliqué. Un domaine approché donnerait une liste plausible mais fausse.
        """
        self.ensure_one()
        self._expodo_check_access()
        options = self._expodo_deserialize_options(options)
        line = self.env["account.report.line"].browse(line_id)

        # Tous les moteurs terminaux, pas seulement `domain`.
        #
        # Le bilan est bâti sur des préfixes de comptes : aucune de ses lignes
        # ne portait d'expression `domain`, la recherche revenait vide et le
        # domaine d'audit retombait sur la période entière, sans même la
        # restriction de comptes. Cliquer « Banque 163 220 » ouvrait six
        # écritures totalisant −24 720, et cliquer « Créances » ouvrait le
        # grand livre complet.
        expressions = line.expression_ids.filtered(
            lambda e: e.engine in ("domain", "account_codes", "tax_tags"))

        # La portée de date est celle de **la colonne cliquée**, pas celle de
        # la première expression de la ligne.
        #
        # Tant que toutes les colonnes d'un état partageaient la même portée,
        # prendre la première donnait le bon résultat. L'ajout d'un solde
        # initial et d'un solde final cumulé à la balance générale a rompu
        # cette hypothèse : cliquer un solde cumulé de 149 750 ouvrait les
        # écritures de la seule période, qui en totalisaient 11 250.
        #
        # Une cellule dont le détail ne se recoupe pas avec la valeur est le
        # défaut le plus grave que puisse avoir un état d'audit : il fait
        # douter de tous les autres chiffres, y compris les justes.
        visee = expressions.filtered(lambda e: e.label == expression_label)

        # Sans colonne désignée, on retient la dernière que l'état présente.
        #
        # Le repli allait jusque-là sur la première expression de la ligne.
        # Sur la balance générale, c'est le solde initial, de portée « début
        # de période » : un appel sans colonne ouvrait les écritures
        # antérieures à l'exercice, c'est-à-dire aucune sur une base neuve,
        # alors que la ligne affiche un solde de 1 200. Le défaut ne se voyait
        # pas sur une base de développement, dont les exercices antérieurs
        # portent des écritures.
        #
        # La dernière colonne est le solde de clôture sur tous les états de la
        # suite : c'est ce qu'un utilisateur désigne quand il clique une ligne
        # sans viser de colonne.
        if not visee:
            for etiquette in reversed(
                    self.column_ids.sorted("sequence").mapped("expression_label")):
                visee = expressions.filtered(lambda e: e.label == etiquette)
                if visee:
                    break

        retenue = visee[:1] or expressions[:1]
        date_scope = retenue.date_scope or "strict_range"
        domain = self._expodo_base_domain(options, column_group_key, date_scope)

        if retenue:
            domain = domain + retenue._expodo_audit_domain(
                self, options, column_group_key)
        if group:
            domain = domain + line._expodo_group_domain(group["field"], group["id"])

        # Le titre doit porter la ligne réellement cliquée, pas son parent.
        # Sur un grand livre, cliquer le compte « 512001 Bank » ouvrait une
        # fenêtre intitulée « Comptes » : dans un contexte d'audit, où l'on
        # vérifie précisément d'où vient un montant, un intitulé erroné est
        # pire qu'absent.
        intitule = line.name
        if group:
            libelle = line._expodo_group_label(group["field"], group["id"])
            if libelle:
                intitule = libelle

        return {
            "type": "ir.actions.act_window",
            "name": self.env._("Journal items — %(line)s", line=intitule),
            "res_model": "account.move.line",
            "view_mode": "list,form",
            # `views` doit être fourni explicitement : une action construite en
            # Python et renvoyée par RPC ne passe pas par `ir.actions.act_window`,
            # donc le client ne peut pas déduire la liste des vues de
            # `view_mode` seul et échoue dans `_preprocessAction`.
            "views": [(False, "list"), (False, "form")],
            "domain": domain,
            "context": {"search_default_group_by_move": 1, "create": False},
            "target": "current",
        }

    # ------------------------------------------------------------------
    # Sérialisation des options
    # ------------------------------------------------------------------

    def _expodo_serialize_options(self, options):
        """Rend les options transmissibles au navigateur (dates en chaînes)."""
        serialized = dict(options)
        serialized["date"] = {
            **options["date"],
            "date_from": options["date"]["date_from"].isoformat(),
            "date_to": options["date"]["date_to"].isoformat(),
        }
        # Le groupe est recopié **en entier**, pas réduit à sa période.
        #
        # Seules les dates ont besoin d'être converties ; tout le reste doit
        # survivre à l'aller-retour vers le navigateur. La ventilation
        # horizontale y range le domaine de sa colonne : réduit à sa période,
        # le groupe perdait ce domaine au premier dépliage. La ligne mère
        # affichait 30 195,50 pour trois journaux et son détail 90 957,21 pour
        # tous, sans que rien ne signale la contradiction. Le clic sur un
        # montant et l'export, qui repassent par les mêmes options, la
        # perdaient aussi.
        serialized["column_groups"] = {
            key: {
                **group,
                "date": {
                    **group["date"],
                    "date_from": group["date"]["date_from"].isoformat(),
                    "date_to": group["date"]["date_to"].isoformat(),
                },
            }
            for key, group in options["column_groups"].items()
        }
        return serialized

    def _expodo_deserialize_options(self, options):
        """Opération inverse : les dates reviennent du navigateur en chaînes."""
        if not options:
            return self._expodo_get_options()
        return self._expodo_normalize_options(options)

    def _expodo_column_group_label(self, key, group):
        """Libellé d'un groupe de colonnes : la période qu'il couvre.

        Deux défauts tenaient dans cette ligne.

        Les dates y étaient interpolées telles quelles, donc en ISO : « Du
        2026-07-01 au 2026-09-27 » en tête d'un bilan français, quand tout le
        reste de la page est en français. L'en-tête des exports avait été
        corrigé ; l'écran dont ils sortent ne l'était pas.

        Et un état arrêté à une date s'annonçait comme un intervalle. Sa borne
        d'ouverture n'existe que pour donner son amplitude à la colonne de
        comparaison : l'afficher laisse croire qu'on peut la déplacer, alors
        qu'elle ne change aucun montant. La règle qui tranche est celle de
        l'export, et c'est désormais la même méthode qui la porte.
        """
        dates = group["date"]
        arrete = self._expodo_est_un_arrete()
        if key == "main":
            if arrete:
                return self.env._(
                    "As of %(date)s",
                    date=format_date(self.env, dates["date_to"]))
            return self.env._(
                "From %(from)s to %(to)s",
                **{"from": format_date(self.env, dates["date_from"]),
                   "to": format_date(self.env, dates["date_to"])}
            )
        if arrete:
            return self.env._(
                "Compared: as of %(date)s",
                date=format_date(self.env, dates["date_to"]))
        return self.env._(
            "Compared: from %(from)s to %(to)s",
            **{"from": format_date(self.env, dates["date_from"]),
               "to": format_date(self.env, dates["date_to"])}
        )


class AccountReportLine(models.Model):
    _inherit = "account.report.line"

    def _expodo_is_auditable(self, label):
        """Une valeur est auditable si elle provient d'écritures réelles.

        Les agrégations n'ont pas d'écritures sous-jacentes : les rendre
        cliquables produirait une liste vide ou, pire, une liste plausible
        mais sans rapport avec le montant affiché (CDC F-22).
        """
        self.ensure_one()
        expression = self.expression_ids.filtered(lambda e: e.label == label)
        return bool(expression) and expression[0].engine in (
            "domain", "account_codes", "tax_tags",
        )
