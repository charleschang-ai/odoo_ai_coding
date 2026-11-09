/** @odoo-module **/

import { registry } from "@web/core/registry";

/**
 * Silent live refresh.
 *
 * When an AI chat tool mutates records, the backend pushes a
 * `ai_chatbot_enhance.data_refresh` bus notification ({model, res_id}).
 * If the view currently open in this tab displays that model (or a closely
 * related one, e.g. sale.order.line vs sale.order), the controller is
 * soft-reloaded in place: the list/form/kanban data visibly updates without
 * a full browser reload.
 */
export const aiDataRefreshService = {
    dependencies: ["bus_service", "action"],
    start(env, { bus_service, action }) {
        let pending = new Set();
        let timer = null;

        // sale.order <-> sale.order.line, account.move <-> account.move.line...
        const related = (a, b) =>
            !!a && !!b && (a === b || a.startsWith(b + ".") || b.startsWith(a + "."));

        const flush = () => {
            timer = null;
            const models = [...pending];
            pending = new Set();
            const resModel = action.currentController?.action?.res_model;
            if (!resModel || !models.some((m) => related(m, resModel))) {
                return;
            }
            // official client action: restores the current controller,
            // reloading the interface without a full browser reload
            action.doAction({ type: "ir.actions.client", tag: "soft_reload" });
        };

        bus_service.subscribe("ai_chatbot_enhance.data_refresh", (payload) => {
            if (payload?.model) {
                pending.add(payload.model);
                clearTimeout(timer);
                // debounce: several tool calls in one AI turn -> one reload
                timer = setTimeout(flush, 800);
            }
        });
    },
};

registry.category("services").add("ai_data_refresh", aiDataRefreshService);
