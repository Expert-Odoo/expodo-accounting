# -*- coding: utf-8 -*-
# Copyright 2026 Expodo (https://expodo.fr)
# License LGPL-3
"""Groupes horizontaux.

Un état financier lit le temps de gauche à droite : une période, parfois deux
quand on compare. Le groupe horizontal ajoute une seconde dimension — par
établissement, par activité, par journal, par plan analytique — et répond à une
question que l'état seul ne sait pas poser : *d'où vient ce total ?*

Un compte de résultat consolidé qui montre deux cent mille euros de charges de
personnel ne dit pas si elles viennent de l'atelier ou du siège. Reventilé par
établissement, il le dit sur la même page.

Comment c'est construit
-----------------------

Le moteur d'états sait déjà calculer plusieurs jeux de colonnes : c'est ainsi
que fonctionne la comparaison de périodes. Un groupe horizontal ne fait que
multiplier ces jeux par ses propres règles, chacune portant un filtre.

Rien du calcul n'est réécrit. C'était la seule façon acceptable : dupliquer la
logique d'agrégation pour y ajouter une dimension aurait produit deux chemins
de calcul, qui auraient fini par diverger — et un état dont deux colonnes se
contredisent est pire qu'un état sans colonnes.
"""

from odoo import _, api, fields, models
from odoo.exceptions import ValidationError

#: Champs sur lesquels une ventilation a un sens comptable. La liste est
#: volontairement courte : ouvrir tous les champs de la ligne d'écriture
#: laisserait ventiler par identifiant ou par date de création, ce qui
#: produirait des colonnes sans signification et lentes à calculer.
CHAMPS_AUTORISES = [
    ("journal_id", "Journal"),
    ("partner_id", "Partner"),
    ("account_id", "Account"),
    ("company_id", "Company"),
    ("analytic_distribution", "Analytic"),
]


class GroupeHorizontal(models.Model):
    _name = "expodo.report.horizontal.group"
    _description = "Horizontal group"
    _order = "name"

    name = fields.Char(string="Name", required=True, translate=True)
    company_id = fields.Many2one(
        "res.company", string="Company", required=True,
        default=lambda self: self.env.company, index=True)
    active = fields.Boolean(string="Active", default=True)
    rule_ids = fields.One2many(
        "expodo.report.horizontal.group.rule", "group_id",
        string="Columns", required=True)

    @api.constrains("rule_ids")
    def _verifier_les_regles(self):
        for groupe in self:
            if not groupe.rule_ids:
                raise ValidationError(_(
                    "“%(name)s” has no column. A horizontal group with no rule "
                    "would simply hide the report.", name=groupe.name))

    @api.model_create_multi
    def create(self, vals_list):
        """Vérifie aussi à la création, y compris sans règle fournie.

        Une contrainte `@api.constrains` sur un champ un-à-plusieurs ne
        s'évalue que si le champ figure dans les valeurs. Créer un groupe sans
        mentionner `rule_ids` passait donc au travers, et produisait exactement
        l'objet que la contrainte existe pour interdire.
        """
        groupes = super().create(vals_list)
        groupes._verifier_les_regles()
        return groupes


class RegleGroupeHorizontal(models.Model):
    _name = "expodo.report.horizontal.group.rule"
    _description = "Horizontal group column"
    _order = "sequence, id"

    group_id = fields.Many2one(
        "expodo.report.horizontal.group", string="Group", required=True,
        ondelete="cascade", index=True)
    sequence = fields.Integer(string="Sequence", default=10)
    name = fields.Char(
        string="Column label", required=True, translate=True,
        help="Heading shown above this column.")

    field_name = fields.Selection(
        CHAMPS_AUTORISES, string="Field", required=True, default="journal_id")
    value_ids_text = fields.Char(
        string="Values", required=True,
        help="Comma-separated database identifiers the column keeps. Filling "
             "this by hand is unpleasant; it is also the only way to express "
             "a column covering several values at once — “Paris and Lyon” as "
             "one establishment, for instance.")

    def domaine(self):
        """Filtre appliqué aux écritures pour cette colonne."""
        self.ensure_one()
        identifiants = []
        for morceau in (self.value_ids_text or "").split(","):
            morceau = morceau.strip()
            if morceau.isdigit():
                identifiants.append(int(morceau))
        if not identifiants:
            # Une règle sans identifiant valide ne doit rien laisser passer.
            # Retourner un domaine vide la rendrait équivalente à « tout »,
            # et la colonne afficherait le total général sous une étiquette
            # qui promet une ventilation.
            return [("id", "=", False)]
        return [(self.field_name, "in", identifiants)]
