import {Component, useState} from "@odoo/owl";
import {Dialog} from "@web/core/dialog/dialog";
import {_t} from "@web/core/l10n/translation";

/**
 * Guided-task dialog: turns the "6 elements of a good request" into a form.
 * Task-type aware (new / modify / analyze), with KIND PRESETS that pre-fill the
 * hard-won best practices (sudo aggregation, standardActionServiceProps, real
 * filters, menu group-gating) so the user only supplies the business details.
 * On confirm it composes a precise prompt and hands it back to be sent.
 */

// Preset scaffolding for NEW modules — each carries the lessons so the user
// does not have to remember them. Fields left "" are for the user to fill.
export const NEW_MODULE_PRESETS = {
    dashboard: {
        label: "Dashboard / analytics",
        depends: "base, web",
        models: "(usually none new — aggregate existing records)",
        backend:
            "@api.model get_<name>_data(self, days=30): SUDO the read-only aggregation " +
            "(record rules/ACLs must not break the dashboard). Return everything the UI needs:\n" +
            "- kpis: the headline numbers\n" +
            "- by_<group>: [{label, value}] for a bar chart\n" +
            "- recent: last N rows, EACH carrying every field the UI filters/shows (e.g. state, category)",
        frontend:
            "OWL client action `<module>.dashboard`: component with " +
            "static props = {...standardActionServiceProps} (import from @web/webclient/actions/action_service); " +
            "useService('orm') + onWillStart to load the data; KPI row; a bar chart (width by value); " +
            "a recent-rows table with FILTER buttons that really filter via a getter; a Refresh button. " +
            "Proper OWL template (t-name = static template) + SCSS.",
        menu: "a menu opening the client action, groups=\"<the group whose data it reads>\" (gate it).",
    },
    crud: {
        label: "CRUD data module",
        depends: "base",
        models: "model.name: field1(Char required), field2(Selection ...), partner_id(Many2one res.partner) ...",
        backend: "state flow buttons / computed fields / constraints as needed (models.Constraint, not _sql_constraints).",
        frontend: "list + form + search views; (optional) a kanban. No JS needed unless specified.",
        menu: "root menu + child action; security groups + ir.model.access.csv for every model.",
    },
    wizard: {
        label: "Wizard (TransientModel)",
        depends: "base",
        models: "the.wizard (TransientModel): the fields the user picks; a target model to act on.",
        backend: "action_confirm() that does the work and returns {'type': 'ir.actions.act_window_close'}.",
        frontend: "a form with <footer> Confirm/Cancel buttons; act_window target='new' (opens as dialog).",
        menu: "opened from a menu or a server action; access csv line for the TransientModel.",
    },
    report: {
        label: "QWeb PDF report",
        depends: "base",
        models: "the model to report on (or extend an existing one).",
        backend: "ir.actions.report (report_type qweb-pdf), bound to the model (binding_model_id).",
        frontend: "a QWeb template using web.external_layout, t-foreach over docs, t-field for values.",
        menu: "print binding on the model (binding_type report).",
    },
    extend: {
        label: "Extend an existing model",
        depends: "the module that owns the model",
        models: "_inherit = 'existing.model' (NO _name): the new fields to add.",
        backend: "override methods with super(); computed fields with @api.depends.",
        frontend: "inherit the existing view with xpath in a NEW view file (prefer over editing the original).",
        menu: "(usually none — the fields show in the existing views).",
    },
    other: {label: "Other / freeform", depends: "base", models: "", backend: "", frontend: "", menu: ""},
};

export class GuidedTaskDialog extends Component {
    static template = "ai_code.GuidedTaskDialog";
    static components = {Dialog};
    static props = {
        close: Function,          // provided by the dialog service
        onConfirm: Function,      // (promptText) => sends it to the AI
        version: {type: String, optional: true},
        modules: {type: Array, optional: true},   // [{name, title}] in the folder
        folderLabel: {type: String, optional: true},
    };

    setup() {
        this.presets = NEW_MODULE_PRESETS;
        this.form = useState({
            taskType: "new_module",
            kind: "dashboard",
            version: this.props.version || "19",   // detected from the folder; user may override
            moduleName: "",
            title: "",
            goal: "",
            depends: "",
            models: "",
            backend: "",
            frontend: "",
            menu: "",
            reference: "",
            // modify
            targetModule: (this.props.modules || [])[0]?.name || "",
            changes: "",
            constraints: "",
            // analyze
            analyzeTarget: "",
            question: "",
            // preview
            preview: "",
            previewDirty: false,
        });
        this.applyPreset(this.form.kind);   // seed dashboard defaults
    }

    get version() {
        return (this.form && this.form.version) || this.props.version || "19";
    }

    get versionOptions() {
        const vs = ["20", "19", "18", "17", "16", "15"];
        const cur = this.props.version;
        if (cur && !vs.includes(cur)) {
            vs.unshift(cur);   // keep an unusual detected version selectable
        }
        return vs;
    }

    get modules() {
        return this.props.modules || [];
    }

    // ---- field / preset handling ----
    applyPreset(kind) {
        const p = this.presets[kind];
        if (!p) {
            return;
        }
        this.form.kind = kind;
        this.form.depends = p.depends;
        this.form.models = p.models;
        this.form.backend = p.backend;
        this.form.frontend = p.frontend;
        this.form.menu = p.menu;
        this.refreshPreview();
    }

    onField() {
        // any field edit (except the preview itself) re-syncs the preview
        // unless the user has taken it over manually
        if (!this.form.previewDirty) {
            this.refreshPreview();
        }
    }

    onPreviewInput(ev) {
        this.form.preview = ev.target.value;
        this.form.previewDirty = true;
    }

    resetPreview() {
        this.form.previewDirty = false;
        this.refreshPreview();
    }

    refreshPreview() {
        this.form.preview = this.composePrompt();
    }

    // ---- prompt composition ----
    _section(label, value) {
        const v = (value || "").trim();
        return v ? `${label}: ${v}\n` : "";
    }

    composePrompt() {
        const f = this.form;
        const v = this.version;
        if (f.taskType === "analyze") {
            let t = _t("Analyze %s in this codebase. Read the real source and use the code graph — do not guess.",
                f.analyzeTarget || _t("this module")) + "\n";
            t += this._section(_t("Question"), f.question);
            t += _t("Answer with concrete file:line references. Do NOT modify any code.");
            return t;
        }
        if (f.taskType === "modify_module") {
            let t = _t("Modify the EXISTING module `%s` in this working directory (read its files first; extend rather than rewrite; prefer inheritance over editing originals).",
                f.targetModule) + "\n";
            t += this._section(_t("Change"), f.changes);
            t += this._section(_t("Keep working (do not break)"), f.constraints);
            t += this._section(_t("Reference modules to study first (use their REAL field names)"), f.reference);
            t += _t("Target Odoo %s idioms. Upgrade cleanly (-u). Proceed without asking; this spec is complete.", v);
            return t;
        }
        // new_module
        const name = f.moduleName || _t("(propose a distinctive snake_case name — avoid core names)");
        let t = _t("Build a new Odoo %s module `%s`", v, name);
        t += f.depends ? " " + _t("(depends on %s)", f.depends) : "";
        t += ".\n";
        t += this._section(_t("Goal"), f.goal);
        t += this._section(_t("Study first (read the REAL source, use real field names — do not invent)"),
            f.reference);
        t += this._section(_t("Models & fields"), f.models);
        t += this._section(_t("Backend (methods & EXACT return structure)"), f.backend);
        t += this._section(_t("Frontend"), f.frontend);
        t += this._section(_t("Menu & access"), f.menu);
        t += _t("Target Odoo %s idioms throughout. Install cleanly", v) +
            (f.frontend ? " " + _t("AND the OWL frontend must render + interactions (filters) must work") : "") +
            ". " + _t("Proceed without asking; this spec is complete.");
        return t;
    }

    // ---- actions ----
    confirm() {
        const text = (this.form.preview || "").trim();
        if (!text) {
            return;
        }
        this.props.onConfirm(text);
        this.props.close();
    }

    cancel() {
        this.props.close();
    }
}
