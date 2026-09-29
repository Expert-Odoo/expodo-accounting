/** @odoo-module **/

/**
 * Client action des rapports comptables.
 *
 * Le composant ne connaît rien de la structure des rapports : il affiche une
 * liste plate de lignes que le serveur lui fournit déjà calculées et mises en
 * forme. Toute la logique comptable reste côté serveur, donc testable sans
 * navigateur.
 *
 * Les lignes dépliables ne chargent leurs enfants qu'au clic (CDC F-21) : sur
 * un grand livre de plusieurs milliers de comptes, un dépliage intégral au
 * premier rendu rendrait le rapport inutilisable.
 */

import { _t } from "@web/core/l10n/translation";
import { NotificationPlugin } from "@web/core/notifications/notification_plugin";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";
import { useSetupAction } from "@web/search/action_hook";
import { standardActionServiceProps } from "@web/webclient/actions/action_plugin";
import { Component, onWillStart, proxy, usePlugin, useProps } from "@odoo/owl";

export class ExpodoAccountReport extends Component {
    static template = "expodo_account_reports.ReportAction";
    // Odoo 20 / OWL 3 : les props se declarent par `useProps`, et
    // `standardActionServiceProps` vit dans action_plugin (action_service.js
    // n'existe plus). Le script officiel owl3-migration proposait un
    // `ActionManagerPlugin` absent du nightly : le service "action" reste
    // l'API employee par le coeur pour les actions client.
    props = useProps(standardActionServiceProps);

    setup() {
        this.orm = useService("orm");
        this.action = useService("action");
        this.notification = usePlugin(NotificationPlugin);

        this.reportId =
            this.props.action.context.report_id ||
            this.props.action.params?.report_id;
        // Une action peut désigner une *catégorie* plutôt qu'un rapport
        // précis : « la déclaration de taxes », résolue selon le pays de la
        // société. C'est ce qui rend le menu valable dans toutes les
        // localisations sans en coder aucune.
        this.reportKind = this.props.action.context.report_kind;

        this.state = proxy({
            notice: false,
            loading: true,
            report: {},
            options: {},
            columns: [],
            columnGroups: [],
            lines: [],
            search: "",
        });

        // État conservé au travers du fil d'Ariane. Ouvrir les écritures
        // d'une cellule puis revenir rechargeait l'état sur sa période par
        // défaut, dépliage perdu : un contrôle ligne à ligne obligeait à
        // ressaisir la période à chaque retour (constaté au port 20.0).
        useSetupAction({
            getLocalState: () => ({
                expodoReportId: this.reportId,
                expodoOptions: JSON.parse(JSON.stringify(this.state.options)),
                expodoUnfolded: this.state.lines
                    .filter((l) => l.unfolded && !l.parent_id)
                    .map((l) => l.id),
            }),
        });

        onWillStart(async () => {
            const restaure = this.props.state;
            if (restaure?.expodoOptions && restaure.expodoReportId) {
                this.reportId = restaure.expodoReportId;
                await this.load(restaure.expodoOptions, { keepUnfolded: false });
                for (const id of restaure.expodoUnfolded || []) {
                    const line = this.state.lines.find((l) => l.id === id);
                    if (line && line.unfoldable && !line.unfolded) {
                        await this.onToggleLine(line);
                    }
                }
                return;
            }
            if (!this.reportId && this.reportKind === "tax") {
                this.reportId = await this.orm.call(
                    "account.report", "expodo_resolve_tax_report", []
                );
            } else if (!this.reportId && this.reportKind) {
                // « balance_sheet » ou « profit_loss » : la présentation
                // nationale si le pays de la société en fournit une, l'état
                // universel sinon.
                this.reportId = await this.orm.call(
                    "account.report", "expodo_resolve_statutory_report",
                    [this.reportKind]
                );
            }
            // Une action peut imposer la période : la déclaration de TVA de
            // septembre ouvre l'état sur septembre. Les paramètres étaient
            // transmis mais jamais lus, et l'état s'ouvrait sur sa période
            // par défaut, le mois précédent pour une déclaration (constaté
            // au port 20.0).
            const params = this.props.action.params || {};
            const initiales = params.date_from && params.date_to
                ? { date: { date_from: params.date_from, date_to: params.date_to,
                            filter: "custom", mode: "range" } }
                : null;
            await this.load(initiales);
        });
    }

    // ------------------------------------------------------------------
    // Chargement
    // ------------------------------------------------------------------

    async load(previousOptions = null, { keepUnfolded = true } = {}) {
        // L'état de dépliage est conservé au travers d'un changement de
        // filtre : sans cela, l'utilisateur qui déplie un compte puis change
        // la période perd son dépliage à chaque fois.
        const previouslyUnfolded = keepUnfolded
            ? this.state.lines.filter((l) => l.unfolded && !l.parent_id).map((l) => l.id)
            : [];
        // Numéro de chargement : un champ date émet plusieurs changements
        // valides pendant la frappe (le jour, puis le mois, puis l'année).
        // Les chargements se chevauchent alors, et chacun re-déplie les
        // lignes : les enfants d'un chargement périmé s'inséraient dans la
        // liste du suivant, et le grand livre affichait ses comptes en
        // double ou en triple. Seul le dernier chargement a le droit
        // d'écrire dans l'état.
        const seq = (this.loadSeq = (this.loadSeq || 0) + 1);
        this.state.loading = true;
        try {
            const data = await this.orm.call(
                "account.report",
                "expodo_get_report_data",
                [[this.reportId], previousOptions]
            );
            if (seq !== this.loadSeq) {
                return;
            }
            Object.assign(this.state, {
                report: data.report,
                options: data.options,
                columns: data.columns,
                columnGroups: data.column_groups,
                // Message affiché lorsque la localisation du pays ne
                // fournit aucune ligne pour ce rapport.
                notice: data.notice || false,
                lines: data.lines,
            });
        } catch (error) {
            // Une erreur de définition de rapport doit être lisible, pas
            // avalée : elle signale une formule fausse, pas un incident.
            this.notification.add(error.data?.message || error.message, {
                type: "danger",
                sticky: true,
            });
            throw error;
        } finally {
            if (seq === this.loadSeq) {
                this.state.loading = false;
            }
        }

        for (const id of previouslyUnfolded) {
            if (seq !== this.loadSeq) {
                return;
            }
            const line = this.state.lines.find((l) => l.id === id);
            if (line && line.unfoldable) {
                await this.onToggleLine(line);
            }
        }
    }

    // ------------------------------------------------------------------
    // Filtres
    // ------------------------------------------------------------------

    /**
     * Change de présentation sans quitter l'écran.
     *
     * Un même état se lit parfois de plusieurs façons — le compte de résultat
     * français et ses soldes intermédiaires de gestion, le tableau de flux en
     * méthode indirecte et en méthode directe. Le menu n'en désigne qu'une ;
     * les autres ne se rejoignaient par aucun chemin.
     *
     * La période en cours est conservée : changer de présentation ne doit pas
     * renvoyer l'utilisateur à l'exercice par défaut au moment précis où il
     * compare deux lectures du même exercice.
     */
    async onVariantChange(ev) {
        const id = parseInt(ev.target.value, 10);
        if (!id || id === this.reportId) {
            return;
        }
        this.reportId = id;
        await this.load(this.state.options, { keepUnfolded: false });
    }

    /**
     * Restreint l'état à un journal.
     *
     * Les rapports déclarent le filtre et le serveur le lit depuis toujours ;
     * il manquait la commande qui le renseigne. Un grand livre ouvert sur le
     * seul journal des achats est la lecture la plus courante qu'on en fasse.
     */
    async onJournalChange(ev) {
        const id = parseInt(ev.target.value, 10);
        const options = JSON.parse(JSON.stringify(this.state.options));
        options.journal_ids = id ? [id] : [];
        await this.load(options);
    }

    async onDateChange(field, ev) {
        const value = ev.target.value;

        // Un champ `type="date"` émet un évènement à chaque état intermédiaire
        // de la saisie au clavier. Recharger sur chacun envoie des dates
        // absurdes au serveur — « 102202-01-01 » — et fait clignoter le
        // rapport. On n'accepte qu'une date complète et plausible.
        if (!/^\d{4}-\d{2}-\d{2}$/.test(value)) {
            return;
        }
        const parsed = new Date(value);
        if (Number.isNaN(parsed.getTime()) || parsed.getFullYear() < 1900) {
            return;
        }

        const options = JSON.parse(JSON.stringify(this.state.options));
        options.date[field] = value;
        options.date.filter = "custom";

        // Un état arrêté à une date n'offre que sa clôture. La borne
        // d'ouverture suit, sinon déplacer la clôture sur un exercice
        // antérieur la ferait passer derrière l'ouverture et le changement
        // serait refusé. Le serveur la ramène ensuite au début de l'exercice.
        if (!this.state.report.filter_date_range) {
            options.date.date_from = value;
        }

        if (options.date.date_from > options.date.date_to) {
            this.notification.add(
                _t("The start date is later than the end date."),
                { type: "warning" }
            );
            return;
        }
        await this.load(options);
    }

    async onPeriodPreset(preset) {
        const options = JSON.parse(JSON.stringify(this.state.options));
        options.date = { ...options.date, filter: preset, preset: preset };
        delete options.date.date_from;
        delete options.date.date_to;
        await this.load({ ...options, date_preset: preset });
    }

    async onToggleHideZero() {
        const options = JSON.parse(JSON.stringify(this.state.options));
        options.hide_0_lines = !options.hide_0_lines;
        await this.load(options);
    }

    async onToggleDraft() {
        const options = JSON.parse(JSON.stringify(this.state.options));
        options.all_entries = !options.all_entries;
        await this.load(options);
    }

    async onComparisonChange(ev) {
        const options = JSON.parse(JSON.stringify(this.state.options));
        options.comparison = {
            filter: ev.target.value,
            number_period: 1,
        };
        await this.load(options);
    }

    // ------------------------------------------------------------------
    // Dépliage
    // ------------------------------------------------------------------

    async onToggleLine(line) {
        if (!line.unfoldable) {
            return;
        }
        if (line.unfolded) {
            this.collapse(line);
            return;
        }
        // Marquage **synchrone**, avant l'appel réseau. Sans cela, deux
        // déclenchements rapprochés — une frappe au clavier dans la recherche,
        // un double-clic — voient tous deux la ligne comme repliée et
        // insèrent chacun leur jeu d'enfants. Le rapport affiche alors les
        // mêmes comptes plusieurs fois, et les totaux paraissent faux.
        if (line.loading) {
            return;
        }
        line.loading = true;
        const seq = this.loadSeq;

        let children;
        try {
            children = await this.orm.call(
            "account.report",
            "expodo_expand_line",
                [
                    [this.reportId],
                    line.line_id,
                    this.state.options,
                    line.group || null,
                    line.next_level || 0,
                ]
            );
        } finally {
            line.loading = false;
        }

        // Nouvelle vérification après l'attente : l'état a pu changer, ou
        // un rechargement a remplacé les lignes entre-temps. Les enfants
        // calculés sur les anciennes options n'ont alors plus leur place.
        if (line.unfolded || seq !== this.loadSeq) {
            return;
        }
        const index = this.state.lines.findIndex((l) => l.id === line.id);
        if (index < 0) {
            return;
        }
        for (const child of children) {
            child.parent_id = line.id;
        }
        this.state.lines.splice(index + 1, 0, ...children);
        line.unfolded = true;
    }

    collapse(line) {
        // Retire récursivement tous les descendants : replier une ligne dont
        // un enfant est lui-même déplié doit tout refermer.
        const toRemove = new Set([line.id]);
        this.state.lines = this.state.lines.filter((candidate) => {
            if (candidate.parent_id && toRemove.has(candidate.parent_id)) {
                toRemove.add(candidate.id);
                return false;
            }
            return true;
        });
        line.unfolded = false;
    }

    // ------------------------------------------------------------------
    // Audit
    // ------------------------------------------------------------------

    async onCellClick(line, column) {
        if (!column.auditable) {
            return;
        }
        const action = await this.orm.call(
            "account.report",
            "expodo_action_audit",
            // La colonne cliquée est transmise : sa portée de date détermine
            // les écritures à ouvrir. Sans elle, un solde cumulé ouvrirait
            // les écritures de la seule période.
            [[this.reportId], line.line_id, this.state.options, line.group || null,
             "main", column?.label || null]
        );
        this.action.doAction(action);
    }

    // ------------------------------------------------------------------
    // Exports
    // ------------------------------------------------------------------

    async onExport(format) {
        // Les lignes dépliées ne sont pas transmises : l'export développe de
        // toute façon tous les niveaux, et cette liste ne sert donc à rien.
        //
        // Elle coûte en revanche cher : les options voyagent dans la chaîne
        // de requête de l'URL de téléchargement, et sur un plan comptable
        // réel — 1 300 comptes ici — déplier puis exporter produisait une URL
        // de plus de 24 000 caractères. Le serveur la rejette bien avant, et
        // l'utilisateur ne voit qu'un téléchargement qui échoue.
        const options = { ...this.state.options, unfolded_lines: [] };
        const action = await this.orm.call(
            "account.report",
            "expodo_action_export",
            [[this.reportId], options, format]
        );
        this.action.doAction(action);
    }

    // ------------------------------------------------------------------
    // Affichage
    // ------------------------------------------------------------------

    /**
     * Déplie ce qui est nécessaire avant de filtrer.
     *
     * Sans cela, chercher « TVA » sur un rapport replié ne renvoie rien :
     * les comptes ne sont pas encore chargés. L'utilisateur en conclut qu'il
     * n'existe aucun compte de TVA — une réponse fausse, pas seulement
     * inutile.
     */
    /**
     * Déplie tout l'état d'un coup.
     *
     * N'est proposé que sur les états qui le déclarent, c'est-à-dire les
     * présentations nationales, qui comptent une vingtaine de lignes. Le
     * proposer sur un grand livre reviendrait à charger d'un clic les
     * écritures de tous les comptes, ce que le dépliage à la demande existe
     * précisément pour éviter.
     */
    async onUnfoldAll() {
        if (this.expanding) {
            return;
        }
        this.expanding = true;
        try {
            const tout = this.state.lines.every((l) => !l.unfoldable || l.unfolded);
            for (const line of [...this.state.lines]) {
                if (!line.unfoldable) {
                    continue;
                }
                if (tout) {
                    if (line.unfolded) {
                        this.collapse(line);
                    }
                } else if (!line.unfolded) {
                    await this.onToggleLine(line);
                }
            }
        } finally {
            this.expanding = false;
        }
    }

    async onSearchInput(ev) {
        this.state.search = ev.target.value;
        if (!this.state.search.trim() || this.expanding) {
            return;
        }
        // Une seule campagne d'expansion à la fois : la recherche se saisit
        // lettre par lettre, et relancer l'expansion à chaque frappe n'apporte
        // rien qu'un risque de doublons.
        this.expanding = true;
        try {
            for (const line of [...this.state.lines]) {
                if (line.unfoldable && !line.unfolded) {
                    await this.onToggleLine(line);
                }
            }
        } finally {
            this.expanding = false;
        }
    }

    get visibleLines() {
        const needle = this.state.search.trim().toLowerCase();
        if (!needle) {
            return this.state.lines;
        }
        // Une ligne dépliable est conservée dès qu'un de ses enfants
        // correspond, faute de quoi les résultats apparaîtraient sans leur
        // en-tête et le rapport deviendrait illisible.
        const matching = this.state.lines.filter((line) =>
            (line.name || "").toLowerCase().includes(needle)
        );
        const parents = new Set(matching.map((line) => line.parent_id).filter(Boolean));
        return this.state.lines.filter(
            (line) => matching.includes(line) || parents.has(line.id)
        );
    }

    /**
     * Libellés de la barre d'outils.
     *
     * Le texte brut d'un template OWL n'est pas extrait pour traduction : il
     * faut le faire passer par `_t()` en JavaScript. Sans cela l'interface
     * reste en anglais quelle que soit la langue de l'utilisateur, alors même
     * que les rapports et les menus sont traduits — un module à moitié traduit
     * paraît moins fini qu'un module entièrement en anglais.
     */
    get labels() {
        return {
            from: _t("From"),
            to: _t("to"),
            asOf: _t("As of"),
            noComparison: _t("No comparison"),
            previousPeriod: _t("Previous period"),
            previousYear: _t("Previous fiscal year"),
            includeDrafts: _t("Include drafts"),
            hideZero: _t("Hide lines at zero"),
            search: _t("Search..."),
            computing: _t("Computing..."),
            label: _t("Label"),
            variant: _t("Presentation"),
            allJournals: _t("All journals"),
            unfoldAll: _t("Unfold all"),
        };
    }

    indentStyle(line) {
        return `padding-left: ${8 + (line.level || 0) * 18}px;`;
    }
}

registry.category("actions").add("expodo_account_report", ExpodoAccountReport);
