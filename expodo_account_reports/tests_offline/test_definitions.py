# -*- coding: utf-8 -*-
# Copyright 2026 Expodo (https://expodo.fr)
# License LGPL-3
"""Contrôles structurels sur les définitions XML des rapports.

Ces tests ne touchent pas la base : ils lisent les fichiers de données comme
le ferait Odoo à l'installation. C'est leur intérêt. Un test qui interroge la
base de développement valide ce que cette base contient, or une base de
développement porte des valeurs posées à la main au fil des essais. Le
rapport d'ancienneté en est l'exemple : sa date d'ouverture avait été
corrigée dans la base et jamais dans le module, le test passait au vert, et
une installation neuve serait repartie avec l'ancien comportement.
"""

import ast
import io
import os
import re
from xml.etree import ElementTree
import unittest

RACINE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")

ENTETE_RAPPORT = re.compile(
    r'<record id="([a-z0-9_]+)" model="account\.report">')


def _fichiers():
    return [os.path.join(RACINE, f) for f in sorted(os.listdir(RACINE))
            if f.endswith(".xml")]


def _rapports():
    """Retourne ``[(fichier, identifiant, corps du record)]``."""
    trouves = []
    for chemin in _fichiers():
        with open(chemin, encoding="utf-8") as fichier:
            texte = fichier.read()
        positions = [(m.group(1), m.end()) for m in ENTETE_RAPPORT.finditer(texte)]
        for index, (nom, debut) in enumerate(positions):
            fin = positions[index + 1][1] if index + 1 < len(positions) else len(texte)
            trouves.append((os.path.basename(chemin), nom, texte[debut:fin]))
    return trouves


class TestDefinitionsRapports(unittest.TestCase):

    def test_des_rapports_sont_definis(self):
        """Garde-fou : sans rapport trouvé, les autres tests ne vérifient rien."""
        self.assertGreater(len(_rapports()), 15)

    def test_chaque_rapport_declare_sa_periode_d_ouverture(self):
        """Aucun rapport ne doit dépendre d'une valeur implicite.

        `default_opening_date_filter` n'a pas de valeur par défaut dans Odoo.
        Un rapport qui ne la déclare pas s'ouvre donc sur l'exercice en cours,
        ce qui est faux pour une balance âgée : ouverte en septembre, elle
        classait les créances au 31 décembre et tout paraissait non échu.
        """
        manquants = [
            "%s / %s" % (fichier, nom)
            for fichier, nom, corps in _rapports()
            if "default_opening_date_filter" not in corps
        ]
        self.assertEqual(
            manquants, [],
            "Ces rapports ne déclarent pas leur période d'ouverture : %s"
            % ", ".join(manquants))

    def test_aucune_date_de_debut_sans_effet(self):
        """Un état qui affiche une date de début doit s'en servir.

        Les portées `from_beginning`, `from_fiscalyear` et les portées de
        début de période ignorent `date_from` : seul `strict_range` la lit.
        Un état dont aucune expression ne lit sur la période affichait donc
        deux bornes dont la première ne commandait rien — on pouvait la
        déplacer d'un exercice entier sans qu'un chiffre bouge. C'est le cas
        qu'ont présenté le bilan, la balance âgée, les écritures ouvertes et
        le relevé client ; c'est aussi celui qu'ont présenté le livre de
        banque et le livre de caisse, qui cumulaient tout l'historique dans
        leurs colonnes d'entrées et de sorties.

        Deux écritures coexistent et comptent toutes les deux : l'expression
        déclarée en propre, et la formule abrégée posée sur la ligne
        (`domain_formula`, `aggregation_formula`…), qu'Odoo transforme en
        expression sur la période — sauf sur un état déclaré cumulatif, où
        le module la ramène à une lecture depuis l'origine.
        """
        abregees = re.compile(
            r'<field name="(?:domain|account_codes|aggregation|external|'
            r'tax_tags)_formula"')
        inertes = []
        for fichier, nom, corps in _rapports():
            if '<field name="filter_date_range" eval="True"/>' not in corps:
                continue
            cumulatif = '<field name="expodo_cumulative" eval="True"/>' in corps
            portees = set()
            for bloc in corps.split('model="account.report.expression">')[1:]:
                declaration = bloc.split("</record>")[0]
                trouvee = re.search(
                    r'<field name="date_scope">([a-z_]+)</field>', declaration)
                # Sans déclaration, le champ vaut `strict_range` dans Odoo.
                portees.add(trouvee.group(1) if trouvee else "strict_range")
            if abregees.search(corps):
                portees.add("from_beginning" if cumulatif else "strict_range")
            if portees and "strict_range" not in portees:
                inertes.append("%s / %s" % (fichier, nom))
        self.assertEqual(
            inertes, [],
            "Ces états affichent une date de début qu'aucune expression ne "
            "lit ; ils doivent déclarer filter_date_range à False ou lire "
            "sur la période : %s" % ", ".join(inertes))

    def test_aucun_etat_de_situation_ne_cumule_les_exercices(self):
        """Une ligne de gestion dans un bilan doit déclarer sa portée.

        Un bilan lit ses soldes depuis l'origine. C'est juste pour un compte
        de bilan, dont le solde est par nature cumulé, et faux pour un compte
        de gestion, qui recommence à chaque exercice : la ligne de résultat
        additionnait alors tous les exercices ouverts et affichait le cumul de
        plusieurs années sous le titre d'un seul.

        Rien ne le signalait. Le total des capitaux propres restait juste,
        donc le contrôle actif-passif restait à zéro et la suite passait au
        vert. Seule la répartition mentait, et c'est l'une des premières
        lignes que lit un expert-comptable.

        Deux drapeaux répondent à ce besoin : `expodo_period_scope` pour
        l'exercice en cours, `expodo_prior_scope` pour ce que les exercices
        antérieurs ont laissé sans affectation. Toute ligne d'un état cumulé
        qui lit une classe de gestion doit porter l'un des deux.
        """
        gestion = re.compile(r"(?<![0-9])[678][0-9]*")
        fautives = []

        def champs(record):
            return {f.get("name"): (f.text or f.get("eval") or "")
                    for f in record.findall("field")}

        def parcourir(record, fichier):
            if record.get("model") == "account.report.line":
                valeurs = champs(record)
                formule = valeurs.get("account_codes_formula", "")
                if formule and gestion.search(formule):
                    if not (valeurs.get("expodo_period_scope")
                            or valeurs.get("expodo_prior_scope")):
                        fautives.append("%s / %s (%s)" % (
                            fichier, record.get("id"), formule.strip()))
            for enfant in record.iter("record"):
                if enfant is not record:
                    parcourir(enfant, fichier)

        for chemin in _fichiers():
            racine = ElementTree.parse(chemin).getroot()
            for record in racine.iter("record"):
                if record.get("model") != "account.report":
                    continue
                if not any(f.get("name") == "expodo_cumulative"
                           for f in record.findall("field")):
                    continue
                for enfant in record.iter("record"):
                    parcourir(enfant, os.path.basename(chemin))

        self.assertEqual(
            fautives, [],
            "Ces lignes lisent des comptes de gestion dans un état cumulé "
            "sans déclarer leur portée : %s" % ", ".join(sorted(set(fautives))))

    def test_chaque_rapport_porte_un_nom_et_des_colonnes(self):
        """Un rapport sans colonne s'affiche vide sans rien expliquer."""
        pauvres = []
        for fichier, nom, corps in _rapports():
            if '<field name="name">' not in corps:
                pauvres.append("%s / %s : sans nom" % (fichier, nom))
            if '<field name="column_ids">' not in corps:
                pauvres.append("%s / %s : sans colonne" % (fichier, nom))
        self.assertEqual(pauvres, [], "; ".join(pauvres))


class TestGabaritPdf(unittest.TestCase):
    """Le document PDF est celui que le client transmet à son comptable."""

    @staticmethod
    def _gabarit(sans_commentaires=False):
        chemin = os.path.join(
            os.path.dirname(RACINE), "report", "report_pdf_templates.xml")
        with open(chemin, encoding="utf-8") as fichier:
            texte = fichier.read()
        if sans_commentaires:
            # Les commentaires du gabarit citent précisément ce qu'il ne faut
            # pas faire. Les lire comme du balisage ferait échouer le contrôle
            # sur l'explication de la règle qu'il vérifie.
            texte = re.sub(r"<!--.*?-->", "", texte, flags=re.S)
        return texte

    def test_la_hauteur_de_ligne_est_posee(self):
        """Sans hauteur fixe, le tableau ondule.

        La hauteur d'une ligne dépendait alors des métriques des glyphes
        qu'elle contient. Sur un bilan d'une centaine de lignes, l'une d'elles
        se retrouvait plus haute que ses voisines et son libellé descendait de
        trois points sous son propre montant. Le défaut ne se voit ni dans les
        données, ni dans le HTML, ni à l'écran : seulement dans le PDF, et
        seulement si on le regarde.
        """
        style = self._gabarit()
        self.assertRegex(
            style, r"td\s*\{[^}]*line-height:\s*\d+px",
            "Les cellules doivent porter une hauteur de ligne fixe")
        self.assertRegex(
            style, r"td\s*\{[^}]*vertical-align:\s*top",
            "Libellé et montant doivent partir du même bord")

    def test_le_document_ne_charge_aucune_ressource_externe(self):
        """wkhtmltopdf n'a pas accès au réseau : une URL et le rendu échoue.

        Le défaut ne se voit pas en shell, où la résolution d'URL diffère. Il
        n'apparaît qu'en requête HTTP réelle, c'est-à-dire chez le client.
        """
        balisage = self._gabarit(sans_commentaires=True)
        for motif in ("http://", "https://", "web.html_container",
                      "<link", "url("):
            self.assertNotIn(
                motif, balisage,
                "Le gabarit PDF doit rester autonome : %r le rend dépendant "
                "d'une ressource externe" % motif)


class TestTraductions(unittest.TestCase):
    """Chaque chaîne traduisible du code doit exister dans les six langues.

    Le module est livré en sept langues, ce qui est un argument de sa fiche.
    Une chaîne oubliée ne casse rien : elle s'affiche simplement en anglais au
    milieu d'un rapport français. L'en-tête « As of 2026-12-31 » figurait ainsi
    sur tous les bilans exportés en français, et personne ne l'avait vu.
    """

    LANGUES = ("fr", "es", "de", "nl", "it", "pt_BR")

    @staticmethod
    def _modules():
        """Tous les modules de la suite, voisins de celui-ci.

        Les quinze modules se publient ensemble : une chaîne oubliée dans
        l'assistant de clôture se voit autant qu'une oubliée dans un rapport.
        Quarante-cinq l'étaient, réparties sur dix modules.
        """
        racine = os.path.dirname(os.path.dirname(
            os.path.dirname(os.path.abspath(__file__))))
        return [
            os.path.join(racine, nom) for nom in sorted(os.listdir(racine))
            if nom.startswith("expodo_")
            and os.path.isfile(os.path.join(racine, nom, "__manifest__.py"))
        ]

    @staticmethod
    def _chaines_du_code(base=None):
        import ast
        base = base or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        trouvees = set()
        for racine, _, fichiers in os.walk(base):
            if "tests" in racine or "__pycache__" in racine or "i18n" in racine:
                continue
            for nom in fichiers:
                chemin = os.path.join(racine, nom)
                if nom.endswith(".py"):
                    with open(chemin, encoding="utf-8") as fichier:
                        arbre = ast.parse(fichier.read())
                    for noeud in ast.walk(arbre):
                        if not isinstance(noeud, ast.Call) or not noeud.args:
                            continue
                        appel = getattr(noeud.func, "attr", None) or getattr(
                            noeud.func, "id", None)
                        premier = noeud.args[0]
                        if appel == "_" and isinstance(premier, ast.Constant) \
                                and isinstance(premier.value, str):
                            trouvees.add(premier.value)
                elif nom.endswith(".js"):
                    with open(chemin, encoding="utf-8") as fichier:
                        texte = fichier.read()
                    trouvees.update(
                        re.findall(r'_t\(\s*["\']([^"\']+)["\']\s*\)', texte))
        return trouvees

    @staticmethod
    def _msgids(chemin):
        identifiants = set()
        courant = None
        dans = False
        with open(chemin, encoding="utf-8") as fichier:
            for ligne in fichier:
                ligne = ligne.rstrip("\n")
                if ligne.startswith("msgid "):
                    if courant is not None:
                        identifiants.add(courant)
                    courant, dans = ligne[7:-1], True
                elif dans and ligne.startswith('"'):
                    courant += ligne[1:-1]
                else:
                    if dans and courant is not None:
                        identifiants.add(courant)
                    dans = False
        if courant is not None:
            identifiants.add(courant)
        return {i.replace("\\n", "\n").replace('\\"', '"') for i in identifiants}

    ENTREE = re.compile(
        r'\nmsgid ((?:"(?:[^"\\\\]|\\\\.)*"\n)+)'
        r'msgstr ((?:"(?:[^"\\\\]|\\\\.)*"\n?)+)')

    @staticmethod
    def _texte_po(bloc):
        return "".join(re.findall(r'"((?:[^"\\\\]|\\\\.)*)"', bloc))

    def test_aucune_traduction_vide(self):
        """Une entrée présente mais vide ne traduit rien.

        Le contrôle de couverture vérifiait que le terme figurait dans le
        fichier. Il y figurait : avec un `msgstr` vide. Deux cent
        cinquante-trois entrées du module des immobilisations étaient dans ce
        cas, dont les champs hérités du fil de discussion et des activités,
        plus cinq des nôtres. Elles s'affichaient en anglais dans les filtres,
        les regroupements et les infobulles d'une interface par ailleurs
        traduite, et le contrôle les comptait pour traduites.
        """
        vides = []
        for base in self._modules():
            dossier = os.path.join(base, "i18n")
            if not os.path.isdir(dossier):
                continue
            module = os.path.basename(base)
            for langue in self.LANGUES:
                chemin = os.path.join(dossier, "%s.po" % langue)
                if not os.path.exists(chemin):
                    continue
                with open(chemin, encoding="utf-8") as fichier:
                    texte = fichier.read()
                for entree in self.ENTREE.finditer(texte):
                    source = self._texte_po(entree.group(1))
                    cible = self._texte_po(entree.group(2))
                    if source and not cible.strip():
                        vides.append("%s [%s] %s" % (module, langue, source[:45]))
        self.assertEqual(
            vides, [],
            "Ces entrées de traduction sont présentes mais vides ; le terme "
            "restera en anglais : %s" % ", ".join(vides[:25]))

    MENU = re.compile(r'<menuitem\b[^>]*?\bname="([^"]+)"', re.S)

    @classmethod
    def _noms_de_menus(cls, base):
        """Intitulés déclarés par les `<menuitem>` d'un module."""
        trouves = set()
        for racine, _dossiers, fichiers in os.walk(base):
            if "__pycache__" in racine or "/i18n" in racine:
                continue
            for nom in fichiers:
                if not nom.endswith(".xml"):
                    continue
                with open(os.path.join(racine, nom), encoding="utf-8") as f:
                    trouves.update(cls.MENU.findall(f.read()))
        return trouves

    def test_chaque_menu_est_traduit(self):
        """Un menu en anglais dans une liste française se voit immédiatement.

        L'intitulé d'un `<menuitem>` ne se traduit pas par un attribut
        `name@fr` : il passe par le fichier de traduction, comme tout terme de
        modèle. Trois menus ajoutés après coup — résumé général, relevé de
        compte client, relevé intracommunautaire — n'y figuraient pas. Ils
        s'affichaient en anglais au milieu de quatorze menus français, dans
        les six langues, alors même que la page qu'ils ouvrent portait un
        titre traduit.

        Aucun contrôle ne regardait les menus : celui de couverture ne lit
        que les chaînes du code Python et JavaScript.
        """
        manquants = []
        for base in self._modules():
            noms = self._noms_de_menus(base)
            if not noms:
                continue
            module = os.path.basename(base)
            for langue in self.LANGUES:
                chemin = os.path.join(base, "i18n", "%s.po" % langue)
                if not os.path.exists(chemin):
                    continue
                connus = self._msgids(chemin)
                for nom in sorted(noms):
                    if nom not in connus:
                        manquants.append("%s [%s] %s" % (module, langue, nom))
        self.assertEqual(
            manquants, [],
            "Ces intitulés de menu n'ont pas d'entrée de traduction et "
            "resteront en anglais : %s" % ", ".join(manquants))

    @staticmethod
    def _chaines_js(base):
        """Chaînes tirées des fichiers `.js` d'un module."""
        trouvees = set()
        for racine, _dossiers, fichiers in os.walk(base):
            if "__pycache__" in racine or "/i18n" in racine:
                continue
            for nom in fichiers:
                if not nom.endswith(".js"):
                    continue
                with open(os.path.join(racine, nom), encoding="utf-8") as f:
                    trouvees.update(
                        re.findall(r'_t\(\s*["\']([^"\']+)["\']\s*\)', f.read()))
        return trouvees

    def test_chaque_chaine_javascript_parvient_au_navigateur(self):
        """Traduire ne suffit pas : encore faut-il que la traduction arrive.

        Odoo ne sert au navigateur que les entrées dont la référence source
        désigne un fichier `.js`. Deux chaînes du bandeau — « All journals »
        et « Unfold all » — avaient été écrites avec une référence Python :
        elles figuraient dans les six fichiers de traduction, le contrôle de
        couverture les voyait, et elles restaient pourtant en anglais au
        milieu d'une interface française. Le paquet de traduction du module
        en portait quinze au lieu de dix-sept.

        Le contrôle porte donc sur la référence et non sur la seule présence
        du terme.
        """
        egarees = []
        for base in self._modules():
            chaines = self._chaines_js(base)
            if not chaines:
                continue
            module = os.path.basename(base)
            for langue in self.LANGUES:
                chemin = os.path.join(base, "i18n", "%s.po" % langue)
                if not os.path.exists(chemin):
                    continue
                with open(chemin, encoding="utf-8") as fichier:
                    texte = fichier.read()
                for chaine in sorted(chaines):
                    marque = 'msgid "%s"\n' % chaine.replace('"', '\\"')
                    position = texte.find(marque)
                    if position < 0:
                        continue  # absence traitée par le contrôle précédent
                    # L'en-tête de l'entrée : les lignes de commentaire qui
                    # précèdent immédiatement le msgid.
                    debut = texte.rfind("\n\n", 0, position)
                    entete = texte[debut:position]
                    if ".js" not in entete:
                        egarees.append("%s [%s] %s" % (module, langue, chaine))
        self.assertEqual(
            egarees, [],
            "Ces chaînes viennent d'un fichier JavaScript mais leur entrée de "
            "traduction désigne une source Python ; Odoo ne les sert pas au "
            "navigateur et elles restent en anglais : %s" % ", ".join(egarees))

    def test_aucune_chaine_oubliee(self):
        total = 0
        for base in self._modules():
            chaines = self._chaines_du_code(base)
            total += len(chaines)
            if not chaines:
                continue
            dossier = os.path.join(base, "i18n")
            module = os.path.basename(base)
            self.assertTrue(
                os.path.isdir(dossier),
                "%s porte %d chaînes traduisibles et aucun dossier i18n"
                % (module, len(chaines)))
            for langue in self.LANGUES:
                chemin = os.path.join(dossier, "%s.po" % langue)
                self.assertTrue(
                    os.path.exists(chemin),
                    "Traduction absente : %s / %s" % (module, langue))
                manquantes = sorted(
                    c for c in chaines if c not in self._msgids(chemin))
                self.assertEqual(
                    manquantes, [],
                    "Chaînes non traduites en %s pour %s : %s"
                    % (langue, module, manquantes))
        self.assertGreater(
            total, 100, "Aucune chaîne trouvée : le balayage est cassé")


class TestFenetresNommees(unittest.TestCase):
    """Toute fenêtre ouverte par le code porte un titre.

    Odoo n'invente pas de titre : une action `act_window` renvoyée sans
    `name` s'affiche « Odoo ». Cinq assistants le faisaient — clôture
    annuelle, charges constatées d'avance, export FEC, lettrage automatique,
    réévaluation des devises. L'utilisateur ouvrait « Clôture annuelle »,
    cliquait sur « Vérifier », et la fenêtre reprenait sous le titre
    « Odoo » : à cet instant, plus rien à l'écran ne dit dans quel assistant
    il se trouve ni ce que le bouton vient de faire.
    """

    def test_aucune_action_de_fenetre_sans_titre(self):
        import ast

        anonymes = []
        for base in TestTraductions._modules():
            for racine, _dossiers, fichiers in os.walk(base):
                if "__pycache__" in racine:
                    continue
                for nom in fichiers:
                    if not nom.endswith(".py"):
                        continue
                    chemin = os.path.join(racine, nom)
                    with open(chemin, encoding="utf-8") as fichier:
                        arbre = ast.parse(fichier.read())
                    for noeud in ast.walk(arbre):
                        if not isinstance(noeud, ast.Dict):
                            continue
                        clefs = {
                            c.value for c in noeud.keys
                            if isinstance(c, ast.Constant)
                            and isinstance(c.value, str)
                        }
                        valeurs = {
                            v.value for v in noeud.values
                            if isinstance(v, ast.Constant)
                            and isinstance(v.value, str)
                        }
                        # `act_window_close` ferme la fenêtre courante : elle
                        # n'en ouvre aucune et n'a donc pas de titre.
                        if "ir.actions.act_window" not in valeurs:
                            continue
                        if "name" not in clefs:
                            anonymes.append(
                                "%s/%s:%s"
                                % (os.path.basename(base), nom, noeud.lineno))
        self.assertEqual(
            anonymes, [],
            "Ces actions ouvrent une fenêtre intitulée « Odoo » : %s"
            % ", ".join(anonymes))


class TestMessagesDAccueil(unittest.TestCase):
    """Le message d'une vue vide doit être traduit en entier.

    Odoo découpe le champ `help` en un terme par paragraphe. Seuls les titres
    figuraient dans les fichiers de traduction : un utilisateur français
    ouvrait une liste vide sur un titre français suivi d'un paragraphe
    anglais, et cela sur huit écrans.

    Chaque paragraphe tient sur une seule ligne du XML. Réparti sur
    plusieurs, le terme extrait porte les retours à la ligne et l'indentation
    du fichier : il devient impossible à recopier sans faute dans un `.po`,
    et la moindre reprise de mise en forme le rend introuvable.
    """

    PARAGRAPHE = re.compile(r"<p[^>]*>(.*?)</p>", re.S)
    AIDE = re.compile(r'<field name="help"[^>]*>(.*?)</field>', re.S)

    @staticmethod
    def _aides(base):
        """Retourne les paragraphes de tous les messages d'accueil du module."""
        trouves = []
        for racine, _dossiers, fichiers in os.walk(base):
            if "__pycache__" in racine or "/i18n" in racine:
                continue
            for nom in sorted(fichiers):
                if not nom.endswith(".xml"):
                    continue
                chemin = os.path.join(racine, nom)
                with open(chemin, encoding="utf-8") as fichier:
                    texte = fichier.read()
                for bloc in TestMessagesDAccueil.AIDE.findall(texte):
                    for para in TestMessagesDAccueil.PARAGRAPHE.findall(bloc):
                        trouves.append((nom, para))
        return trouves

    def test_chaque_paragraphe_tient_sur_une_ligne(self):
        replies = []
        for base in TestTraductions._modules():
            for nom, para in self._aides(base):
                if "\n" in para.strip():
                    replies.append("%s/%s : %s"
                                   % (os.path.basename(base), nom,
                                      " ".join(para.split())[:50]))
        self.assertEqual(
            replies, [],
            "Ces paragraphes sont répartis sur plusieurs lignes ; leur terme "
            "de traduction devient impossible à recopier : %s"
            % ", ".join(replies))

    def test_chaque_paragraphe_est_traduit(self):
        manquants = []
        for base in TestTraductions._modules():
            paragraphes = {p.strip() for _n, p in self._aides(base)}
            if not paragraphes:
                continue
            module = os.path.basename(base)
            for langue in TestTraductions.LANGUES:
                chemin = os.path.join(base, "i18n", "%s.po" % langue)
                self.assertTrue(
                    os.path.exists(chemin),
                    "Traduction absente : %s / %s" % (module, langue))
                connus = TestTraductions._msgids(chemin)
                for para in sorted(paragraphes):
                    if para not in connus:
                        manquants.append("%s [%s] %s"
                                         % (module, langue, para[:45]))
        self.assertEqual(
            manquants, [],
            "Paragraphes d'accueil non traduits : %s" % ", ".join(manquants))


class TestScriptsDeMigration(unittest.TestCase):
    """Un script de migration mal formé empêche le module de se charger.

    Odoo exécute ces fichiers pendant la construction du registre et vérifie
    la signature de ``migrate``. Une signature ``(env, version)`` — celle que
    suggère le reste du code d'Odoo 19 — fait échouer le chargement de la
    base entière, pas seulement du module. L'erreur ne se voit qu'à la mise à
    jour d'une installation existante, jamais sur une base neuve, donc jamais
    en développement.
    """

    def _scripts(self):
        racine = os.path.join(os.path.dirname(RACINE), "migrations")
        if not os.path.isdir(racine):
            return []
        trouves = []
        for version in sorted(os.listdir(racine)):
            dossier = os.path.join(racine, version)
            if not os.path.isdir(dossier):
                continue
            for nom in sorted(os.listdir(dossier)):
                if nom.endswith(".py"):
                    trouves.append(os.path.join(dossier, nom))
        return trouves

    def test_chaque_script_expose_migrate_cr_version(self):
        for chemin in self._scripts():
            with open(chemin, encoding="utf-8") as fichier:
                texte = fichier.read()
            with self.subTest(script=os.path.basename(chemin)):
                self.assertIn(
                    "def migrate(cr, version)", texte,
                    "Odoo attend la signature (cr, version) ; toute autre "
                    "interrompt le chargement du registre")

    def test_chaque_script_est_compilable(self):
        for chemin in self._scripts():
            with open(chemin, encoding="utf-8") as fichier:
                texte = fichier.read()
            with self.subTest(script=os.path.basename(chemin)):
                compile(texte, chemin, "exec")

    def test_chaque_version_de_migration_existe_au_manifeste(self):
        """Un dossier dont la version dépasse celle du module ne tourne jamais."""
        manifeste = os.path.join(
            os.path.dirname(RACINE), "__manifest__.py")
        with open(manifeste, encoding="utf-8") as fichier:
            version = re.search(r'"version"\s*:\s*"([^"]+)"',
                                fichier.read()).group(1)
        racine = os.path.join(os.path.dirname(RACINE), "migrations")
        if not os.path.isdir(racine):
            return
        for dossier in sorted(os.listdir(racine)):
            if not os.path.isdir(os.path.join(racine, dossier)):
                continue
            with self.subTest(migration=dossier):
                self.assertLessEqual(
                    [int(x) for x in dossier.split(".")],
                    [int(x) for x in version.split(".")],
                    "Le module doit porter au moins la version du script, "
                    "sinon Odoo ne l'exécute pas")


class TestChampsPosesEtJamaisLus(unittest.TestCase):
    """Un champ posé dans les données et que rien ne lit ne fait rien.

    C'est le défaut le plus fréquent de ce module, et le plus silencieux :
    la définition annonce un comportement, le moteur l'ignore, et l'écran
    paraît normal. ``hide_if_zero`` a vécu ainsi jusqu'à ce qu'un contrôle à
    l'écran le révèle, et les bornes de date d'une déclaration de taxes
    voyageaient de la même façon sans être lues à l'arrivée.

    Ce test le dit mécaniquement : tout champ renseigné dans les définitions
    doit apparaître ailleurs dans le module, ou figurer dans la liste
    ci-dessous avec sa raison.
    """

    #: Champs du cœur d'Odoo qu'aucun code du module ne lit, sciemment.
    #: Une entrée ici est une décision, pas un oubli : elle se justifie.
    ADMIS = {
        # Les chiffres suivent les sociétés actives du sélecteur d'Odoo, qui
        # remplit `company_ids` dans les options. Un second sélecteur propre à
        # l'état ferait double emploi, et deux filtres qui se contredisent
        # valent moins qu'un seul.
        "filter_multi_company",
        # Le cœur s'en sert pour déplier une ligne d'emblée. Ici tout est
        # replié au premier rendu et se déplie au clic : sur un grand livre
        # de plusieurs milliers de comptes, un dépliage intégral rendrait
        # l'état inutilisable. Le champ est donc honoré par construction.
        "foldable",
    }

    def test_aucun_champ_pose_n_est_ignore(self):
        racine = os.path.dirname(RACINE)
        poses = set()
        for nom in sorted(os.listdir(RACINE)):
            if not nom.endswith(".xml"):
                continue
            with open(os.path.join(RACINE, nom), encoding="utf-8") as fichier:
                poses |= set(re.findall(r'<field name="([a-z0-9_]+)"', fichier.read()))

        code = []
        for dossier, _sous, fichiers in os.walk(racine):
            if "__pycache__" in dossier or dossier.endswith("data"):
                continue
            if os.sep + "i18n" in dossier:
                continue
            for nom in fichiers:
                if nom.endswith((".py", ".js", ".xml")):
                    with open(os.path.join(dossier, nom), encoding="utf-8",
                              errors="ignore") as fichier:
                        code.append(fichier.read())
        code = "\n".join(code)

        ignores = sorted(
            champ for champ in poses
            if champ not in self.ADMIS
            and not re.search(r"\b%s\b" % re.escape(champ), code))
        self.assertFalse(
            ignores,
            "Ces champs sont renseignés dans les définitions et lus nulle "
            "part : %s. Soit le moteur doit les honorer, soit ils n'ont rien "
            "à faire dans les données." % ignores)


class TestDescriptionsDesManifestes(unittest.TestCase):
    """La description d'un module est sa fiche de vente.

    Odoo la rend en reStructuredText sur l'App Store et dans l'écran
    Applications. Une indentation inattendue ou un bloc de citation mal
    fermé passe sans bruit à l'installation, puis s'affiche de travers à
    celui qui décide de télécharger. C'est la première chose qu'il voit du
    module, et la seule avant qu'il ne l'installe.
    """

    def _modules(self):
        racine = os.path.dirname(os.path.dirname(RACINE))
        for nom in sorted(os.listdir(racine)):
            manifeste = os.path.join(racine, nom, "__manifest__.py")
            if nom.startswith("expodo_") and os.path.isfile(manifeste):
                with open(manifeste, encoding="utf-8") as fichier:
                    yield nom, ast.literal_eval(fichier.read())

    def test_chaque_description_se_rend_sans_avertissement(self):
        try:
            from docutils.core import publish_string
        except ImportError:
            self.skipTest("docutils absent de cette image")

        for nom, manifeste in self._modules():
            description = (manifeste.get("description") or "").strip()
            if not description:
                continue
            journal = io.StringIO()
            with self.subTest(module=nom):
                publish_string(
                    source=description, writer_name="html",
                    settings_overrides={"report_level": 1, "halt_level": 6,
                                        "warning_stream": journal,
                                        "input_encoding": "unicode"})
                self.assertFalse(
                    journal.getvalue().strip(),
                    "La description ne se rend pas proprement :\n%s"
                    % journal.getvalue()[:400])

    def test_chaque_module_porte_une_description(self):
        """Un module sans description s'affiche vide sur sa fiche."""
        for nom, manifeste in self._modules():
            with self.subTest(module=nom):
                self.assertTrue(
                    (manifeste.get("description") or "").strip()
                    or (manifeste.get("summary") or "").strip(),
                    "Ni description ni résumé : la fiche du module est muette")
