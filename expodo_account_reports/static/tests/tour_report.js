/** @odoo-module **/

/**
 * Parcours automatisé du rapport, exécuté dans un vrai navigateur.
 *
 * Ce fichier existe parce que les tests Python ne voient pas ce qui se passe
 * dans le navigateur. Toute une série de défauts — options renvoyées telles
 * changement de filtre, recherche opérant sur des lignes non chargées — sont
 * passés au travers d'une suite serveur complète et verte.
 *
 * Le parcours suit l'usage réel : ouvrir, déplier, filtrer, chercher, auditer.
 *
 * ---------------------------------------------------------------------------
 * Comment le lancer
 * ---------------------------------------------------------------------------
 *
 * Ce fichier vit dans `web.assets_tests`, un lot que le client d'une instance
 * ordinaire ne charge jamais. Le parcours ne tourne donc que là où Odoo est
 * démarré en mode test, avec un Chrome sans interface disponible. C'est le
 * bon réglage pour la livraison — du code de test n'a rien à faire chez
 * l'utilisateur — mais il a un coût : sur une instance de développement sans
 * Chrome installé, le parcours ne tourne jamais et la classe `TestReportTour`
 * se saute en silence.
 *
 * Pour le jouer dans un navigateur ordinaire, contre l'instance qui tourne,
 * ouvrir la balance générale puis, dans la console :
 *
 *     const meta = await (await fetch('/web/bundle/web.assets_tests')).json();
 *     const code = await (await fetch(meta.find(m => m.type === 'script').src,
 *                                     { cache: 'reload' })).text();
 *     const blob = URL.createObjectURL(new Blob([code], { type: 'text/javascript' }));
 *     await new Promise((ok, ko) => {
 *         const s = document.createElement('script');
 *         s.src = blob; s.onload = ok; s.onerror = ko;
 *         document.head.appendChild(s);
 *     });
 *     await odoo.startTour('expodo_account_report_tour',
 *                          { mode: 'auto', redirect: false, stepDelay: 120 });
 *
 * `cache: 'reload'` n'est pas une précaution de confort : l'adresse d'un lot
 * ne change qu'avec la date de modification des fichiers qui le composent.
 * Un déploiement qui les horodate à zéro laisse l'adresse inchangée, et le
 * navigateur ressert sa copie — on relit alors l'ancien parcours en croyant
 * avoir corrigé le nouveau.
 *
 * `redirect: false` évite le rechargement de page que `startTour` provoque
 * autrement : après un rechargement, le lot injecté à la main a disparu et le
 * parcours reste en attente, indéfiniment.
 *
 * Le parcours a besoin d'une écriture comptabilisée **et** d'un brouillon
 * dans la période affichée, sans quoi l'étape du filtre brouillons n'a rien
 * à prouver.
 */

import { registry } from "@web/core/registry";

/**
 * Empreinte des montants de la première ligne.
 *
 * Ce parcours lisait `td:nth-child(2)` en croyant y trouver le débit. C'est
 * la première colonne de montants, qui sur une balance générale porte le
 * solde initial — lequel ne bouge évidemment pas quand on inclut les
 * brouillons de la période. L'étape échouait donc sur un rapport qui
 * fonctionne. Personne ne l'a vu parce que le parcours n'avait jamais tourné :
 * il vit dans `web.assets_tests`, et aucune exécution n'avait de navigateur.
 *
 * Lire une colonne par sa position suppose de connaître le jeu de colonnes ;
 * la lire par son intitulé suppose de connaître la langue. On compare donc
 * toute la ligne, ce qui exprime mieux ce que l'étape veut prouver : inclure
 * les brouillons change les chiffres.
 */
function firstRowAmounts() {
    const row = document.querySelector(".o_expodo_report_table tbody tr");
    if (!row) {
        return null;
    }
    return [...row.children]
        .slice(1)
        .map((cell) => cell.textContent.trim())
        .join(" | ");
}

function bodyRowCount() {
    return document.querySelectorAll(".o_expodo_report_table tbody tr").length;
}

registry.category("web_tour.tours").add("expodo_account_report_tour", {
    url: "/odoo/action-expodo_account_reports.action_balance",
    steps: () => [
        {
            content: "Le rapport s'affiche avec sa ligne racine",
            trigger: ".o_expodo_report_table tbody tr td:first-child",
            run: () => {
                window.__expodo = { initialAmounts: firstRowAmounts() };
            },
        },
        {
            content: "Déplier la ligne racine",
            trigger: ".o_expodo_report_table .o_expodo_caret",
            run: "click",
        },
        {
            content: "Des sous-lignes apparaissent",
            trigger: ".o_expodo_report_table tbody tr:nth-child(3)",
            run: () => {
                window.__expodo.unfoldedCount = bodyRowCount();
                if (window.__expodo.unfoldedCount < 3) {
                    throw new Error("Le dépliage n'a produit aucune sous-ligne");
                }
            },
        },
        {
            content: "Activer l'inclusion des brouillons",
            trigger: ".o_expodo_report_controls input[type=checkbox]",
            run: "click",
        },
        {
            content: "Le total change et le dépliage est conservé",
            trigger: ".o_expodo_report_table tbody tr:nth-child(2)",
            // Le rechargement après le clic est asynchrone, et le déclencheur
            // est satisfait par les lignes déjà affichées : l'étape lisait la
            // ligne racine avant la réponse du serveur. On attend donc que
            // les montants changent, dans une limite de cinq secondes.
            run: async () => {
                for (let i = 0; i < 50 && firstRowAmounts() === window.__expodo.initialAmounts; i++) {
                    await new Promise((resolve) => setTimeout(resolve, 100));
                }
                if (firstRowAmounts() === window.__expodo.initialAmounts) {
                    throw new Error(
                        "Le filtre brouillons n'a modifié aucun montant de la " +
                            "ligne racine : " + window.__expodo.initialAmounts
                    );
                }
                if (bodyRowCount() < 3) {
                    throw new Error(
                        "Le dépliage a été perdu au changement de filtre"
                    );
                }
            },
        },
        {
            content: "Rechercher un terme présent dans les sous-lignes",
            trigger: ".o_expodo_report_controls input[type=search]",
            run: "edit 4",
        },
        {
            content: "La recherche ne produit aucun doublon",
            trigger: ".o_expodo_report_table tbody tr",
            run: () => {
                const names = [
                    ...document.querySelectorAll(
                        ".o_expodo_report_table tbody tr td:first-child"
                    ),
                ].map((cell) => cell.textContent.trim());
                const unique = new Set(names);
                if (unique.size !== names.length) {
                    throw new Error(
                        "Lignes dupliquées après recherche : " +
                            (names.length - unique.size) + " doublon(s)"
                    );
                }
            },
        },
        {
            content: "Vider la recherche",
            trigger: ".o_expodo_report_controls input[type=search]",
            run: "edit ",
        },
        {
            content: "Cliquer un montant auditable ouvre les écritures",
            trigger: ".o_expodo_report_table tbody tr:nth-child(2) .o_expodo_auditable",
            run: "click",
        },
        {
            content: "La liste des écritures est affichée",
            trigger: ".o_list_view",
        },
    ],
});
