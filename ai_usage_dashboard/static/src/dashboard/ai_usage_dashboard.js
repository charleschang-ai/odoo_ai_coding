/** @odoo-module **/

import { Component, useState, onWillStart } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";
import { standardActionServiceProps } from "@web/webclient/actions/action_service";

export class AiUsageDashboard extends Component {
    static template = "ai_usage_dashboard.AiUsageDashboard";
    static props = { ...standardActionServiceProps };

    setup() {
        this.orm = useService("orm");

        this.state = useState({
            kpis: {},
            byModel: [],
            byAgent: [],
            recent: [],
            days: 30,
            providerFilter: "all",
            loading: false,
        });

        onWillStart(() => this.load());
    }

    // ── Data loading ──────────────────────────────────────────────

    async load() {
        this.state.loading = true;
        try {
            const data = await this.orm.call(
                "ai.token.usage",
                "get_usage_dashboard_data",
                [this.state.days]
            );
            this.state.kpis = data.kpis || {};
            this.state.byModel = data.by_model || [];
            this.state.byAgent = data.by_agent || [];
            this.state.recent = data.recent || [];
        } finally {
            this.state.loading = false;
        }
    }

    // ── Reactive getters ──────────────────────────────────────────

    get filteredRecent() {
        if (this.state.providerFilter === "all") {
            return this.state.recent;
        }
        return this.state.recent.filter(
            (r) => r.provider === this.state.providerFilter
        );
    }

    /** Unique provider names present in `recent`, for filter buttons. */
    get providers() {
        const seen = new Set();
        for (const r of this.state.recent) {
            seen.add(r.provider);
        }
        return [...seen].sort();
    }

    /** Maximum cost among byModel entries, for bar width calculation. */
    get maxModelCost() {
        if (!this.state.byModel.length) return 1;
        return this.state.byModel[0].cost || 1;
    }

    // ── Event handlers ────────────────────────────────────────────

    setDays(days) {
        if (this.state.days !== days) {
            this.state.days = days;
            this.load();
        }
    }

    setProviderFilter(provider) {
        this.state.providerFilter = provider;
    }

    refresh() {
        this.load();
    }

    // ── Helpers (used in template) ────────────────────────────────

    formatCost(cost) {
        return cost != null ? "$" + Number(cost).toFixed(4) : "$0.0000";
    }

    formatTokens(tokens) {
        if (tokens == null) return "0";
        if (tokens >= 1000000) return (tokens / 1000000).toFixed(1) + "M";
        if (tokens >= 1000) return (tokens / 1000).toFixed(1) + "k";
        return String(tokens);
    }

    formatDate(dateStr) {
        if (!dateStr) return "";
        return new Date(dateStr).toLocaleString();
    }

    barWidthPercent(cost) {
        const max = this.maxModelCost;
        return max ? Math.round((cost / max) * 100) : 0;
    }
}

registry
    .category("actions")
    .add("ai_usage_dashboard.dashboard", AiUsageDashboard);
