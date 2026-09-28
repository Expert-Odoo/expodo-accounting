# -*- coding: utf-8 -*-
# Copyright 2026 Expodo (https://expodo.fr)
# License LGPL-3
"""Tests du client OWL, exécutés sous Node sans navigateur.

Le composant des états est du JavaScript : ni la suite Python ni le parcours
de navigateur ne le couvrent utilement. Le parcours ne tourne que sur une
image portant Chrome, et il exerce l'écran, pas l'enchaînement des
chargements.

Ce qui est vérifié ici tient à l'asynchronisme, et se prête donc mal à un
test d'écran : deux chargements lancés coup sur coup se chevauchent, chacun
attend le réseau, et l'ordre des réponses n'est pas celui des appels.

Le fichier de production est chargé tel quel. Seules ses lignes ``import``
et son enregistrement dans le registre sont retirés : ce qui est exercé est
bien le code livré, et un changement de logique casse ce test.
"""

import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest

RACINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SOURCE = os.path.join(RACINE, "static", "src", "js", "report_action.js")

# Remplacent les modules du navigateur, absents sous Node. Aucun n'est
# exercé par le scénario : le composant n'est ni monté ni rendu.
PREAMBULE = """
const _t = (texte) => texte;
const registry = { category: () => ({ add: () => {} }) };
const useService = () => ({});
const standardActionServiceProps = {};
class Component {}
const onWillStart = () => {};
const useState = (etat) => etat;
"""


def source_javascript():
    """Le fichier de production, rendu exécutable hors navigateur."""
    with open(SOURCE, encoding="utf-8") as fichier:
        texte = fichier.read()
    texte = re.sub(r"^import .*?;\n", "", texte, flags=re.M | re.S)
    texte = re.sub(r"^registry\.category.*$", "", texte, flags=re.M)
    return PREAMBULE + texte


SCENARIO = r"""
const attendre = (ms) => new Promise((resolus) => setTimeout(resolus, ms));

// Deux lignes de premier niveau, dont une dépliable, étiquetées par la
// période : une ligne d'un chargement abandonné se reconnaît à son nom.
const lignes = (periode) => ([
    {id: "line|1", line_id: 1, name: "Comptes " + periode,
     unfoldable: true, unfolded: false, level: 0, columns: []},
    {id: "line|2", line_id: 2, name: "Total " + periode,
     unfoldable: false, unfolded: false, level: 0, columns: []},
]);

// L'identifiant d'un groupe ne porte pas la période : le serveur le
// compose de la ligne, du niveau et du groupe. Deux chargements ramènent
// donc les mêmes identifiants, et un doublon est bien un doublon.
const enfants = (periode) => ([
    {id: "grp|1|0|account_id|10", line_id: 1, name: "600000 " + periode,
     unfoldable: false, columns: []},
    {id: "grp|1|0|account_id|11", line_id: 1, name: "700000 " + periode,
     unfoldable: false, columns: []},
]);

// Le second chargement répond plus vite que le premier, et son dépliage plus
// vite encore. C'est le cas qui fait entrer les enfants du chargement dépassé
// dans la liste du suivant.
const retards = {A: {donnees: 10, depliage: 60}, B: {donnees: 25, depliage: 5}};

const orm = {
    async call(modele, methode, args) {
        if (methode === "expodo_get_report_data") {
            const periode = args[1].periode;
            await attendre(retards[periode].donnees);
            return {report: {}, options: args[1], columns: [],
                    column_groups: [], notice: false, lines: lignes(periode)};
        }
        if (methode === "expodo_expand_line") {
            const periode = args[2].periode;
            await attendre(retards[periode].depliage);
            return enfants(periode);
        }
        throw new Error("appel inattendu : " + methode);
    },
};

const vue = Object.create(ExpodoAccountReport.prototype);
vue.orm = orm;
vue.notification = {add() {}};
vue.reportId = 1;

// L'écran de départ : une ligne déjà dépliée par l'utilisateur. C'est elle
// que chaque chargement va vouloir redéplier.
const depart = lignes("0");
depart[0].unfolded = true;
const premiers = enfants("0");
for (const enfant of premiers) { enfant.parent_id = "line|1"; }
depart.splice(1, 0, ...premiers);
vue.state = {loading: false, notice: false, report: {}, options: {periode: "0"},
             columns: [], columnGroups: [], lines: depart, search: ""};

// Deux chargements coup sur coup, sans attendre le premier : c'est ce que
// produit la saisie d'une date au clavier, où le champ émet plusieurs
// valeurs complètes et valides.
Promise.all([vue.load({periode: "A"}), vue.load({periode: "B"})]).then(async () => {
    await attendre(120);
    console.log(JSON.stringify(vue.state.lines.map((l) => ({id: l.id, name: l.name}))));
});
"""


@unittest.skipUnless(shutil.which("node"), "Node absent de cette image")
class TestChargementsConcurrents(unittest.TestCase):
    """Deux chargements qui se chevauchent ne doivent en laisser qu'un.

    L'écran affichait les comptes deux ou trois fois, mêlant ceux d'une
    période abandonnée aux nouveaux. Chaque chargement rétablit le dépliage
    précédent, et les enfants d'un chargement dépassé s'inséraient dans la
    liste du suivant. Les totaux, eux, restaient justes : l'utilisateur
    voyait donc un état qui se contredisait lui-même.
    """

    def _executer(self):
        with tempfile.TemporaryDirectory() as dossier:
            chemin = os.path.join(dossier, "scenario.mjs")
            with open(chemin, "w", encoding="utf-8") as fichier:
                fichier.write(source_javascript() + SCENARIO)
            resultat = subprocess.run(
                [shutil.which("node"), chemin],
                capture_output=True, text=True, timeout=60)
        self.assertEqual(resultat.returncode, 0, resultat.stderr[-800:])
        return json.loads(resultat.stdout.strip().splitlines()[-1])

    def test_le_chargement_depasse_n_ecrit_pas_dans_l_etat(self):
        lignes = self._executer()
        noms = [ligne["name"] for ligne in lignes]
        self.assertFalse(
            [nom for nom in noms if nom.endswith(" A") or nom.endswith(" 0")],
            "Seul le dernier chargement demandé doit écrire dans l'état ; "
            "ici il reste des lignes d'une période abandonnée : %s" % noms)

    def test_aucune_ligne_n_apparait_deux_fois(self):
        lignes = self._executer()
        identifiants = [ligne["id"] for ligne in lignes]
        doublons = sorted({i for i in identifiants
                           if identifiants.count(i) > 1})
        self.assertFalse(
            doublons,
            "Les mêmes comptes apparaissent plusieurs fois : %s" % doublons)

    def test_le_dernier_chargement_est_complet(self):
        """Abandonner les chargements dépassés ne doit rien perdre.

        Une correction qui se contenterait d'ignorer le second chargement
        ferait passer les deux tests précédents en laissant l'écran sur la
        période précédente.
        """
        lignes = self._executer()
        noms = sorted(ligne["name"] for ligne in lignes)
        self.assertEqual(
            noms, ["600000 B", "700000 B", "Comptes B", "Total B"],
            "L'écran doit montrer la dernière période demandée, dépliage "
            "compris")


if __name__ == "__main__":
    unittest.main()
