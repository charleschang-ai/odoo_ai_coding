/* global d3 */
import { Component, onMounted, onWillUnmount, useRef, useState } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";
import { _t } from "@web/core/l10n/translation";

const LAYER_COLORS = {
    python: "#7F77DD",
    xml: "#F0997B",
    js: "#5DCAA5",
    module: "#8A8A83",
};
// Edges expressing "a module touches something" render dashed
const DASHED_EDGES = new Set(["model_extend", "js_patches", "method_override", "js_registers"]);

export const EDGE_LABELS = {
    module_depends: _t("depends on"),
    model_inherit: _t("inherits model"),
    model_extend: _t("extends model"),
    model_delegate: _t("delegates to (_inherits)"),
    field_of: _t("field of"),
    field_comodel: _t("targets model"),
    compute_depends: _t("compute depends on"),
    method_of: _t("method of"),
    method_override: _t("overrides method"),
    view_of_model: _t("view of"),
    view_inherits: _t("inherits view"),
    view_uses_field: _t("displays field"),
    action_targets_model: _t("opens model"),
    menu_opens_action: _t("triggers action"),
    template_inherits: _t("inherits template"),
    js_patches: _t("patches"),
    js_registers: _t("registers"),
    component_uses_template: _t("renders template"),
    js_component_extends: _t("extends component"),
};

export const NODE_TYPE_LABELS = {
    module: _t("Module"),
    model: _t("Model"),
    field: _t("Field"),
    method: _t("Method"),
    view: _t("View"),
    qweb_template: _t("QWeb Template"),
    action: _t("Action"),
    menu: _t("Menu"),
    js_component: _t("JS Component"),
    js_service: _t("JS Service"),
};

export class CodeGraphAction extends Component {
    static template = "ai_code_graph.CodeGraphAction";
    static props = { "*": true };

    setup() {
        this.orm = useService("orm");
        this.actionService = useService("action");
        this.notification = useService("notification");
        this.svgRef = useRef("svg");
        this.simulation = null;
        this.searchTimer = null;
        const params = this.props.action?.params || {};
        this.state = useState({
            mode: ["structure", "calls"].includes(params.mode) ? params.mode : "graph",
            calls: null,
            methodFilter: "",
            stale: false,
            customModules: [],
            focusKey: params.focus || false,
            depth: 2,
            layers: { python: true, xml: true, js: true },
            searchTerm: "",
            searchResults: [],
            selected: null,
            structure: null,
            loading: false,
            rebuilding: false,
            empty: false,
            truncated: false,
            nodeCount: 0,
            edgeCount: 0,
            error: "",
        });
        this.edgeLabels = EDGE_LABELS;
        this.typeLabels = NODE_TYPE_LABELS;
        this.layerColors = LAYER_COLORS;
        onMounted(() => this.reload());
        onWillUnmount(() => {
            if (this.simulation) {
                this.simulation.stop();
            }
            clearTimeout(this.searchTimer);
        });
    }

    get activeLayers() {
        return Object.keys(this.state.layers).filter((l) => this.state.layers[l]);
    }

    isCustomModule(moduleName) {
        return this.state.customModules.includes(moduleName);
    }

    typeColor(nodeType) {
        return {
            model: "#7F77DD",
            method: "#378ADD",
            js_component: "#1D9E75",
            js_service: "#1D9E75",
            js_function: "#1D9E75",
            qweb_template: "#D85A30",
            view: "#D85A30",
            server_action: "#D4537E",
            action: "#BA7517",
            menu: "#BA7517",
            module: "#5F5E5A",
            field: "#888780",
        }[nodeType] || "#888780";
    }

    get filteredMethods() {
        const structure = this.state.structure;
        if (!structure || !structure.methods) {
            return [];
        }
        const needle = this.state.methodFilter.trim().toLowerCase();
        if (!needle) {
            return structure.methods;
        }
        return structure.methods.filter(
            (m) => m.name.toLowerCase().includes(needle)
                || m.modules.some((mod) => mod.toLowerCase().includes(needle)));
    }

    reload() {
        if (this.state.mode === "structure") {
            this.state.structure = null;
            return this.state.focusKey ? this.loadStructure() : Promise.resolve();
        }
        if (this.state.mode === "calls") {
            this.state.calls = null;
            return this.state.focusKey ? this.loadCalls() : Promise.resolve();
        }
        return this.loadGraph();
    }

    async loadCalls() {
        this.state.loading = true;
        this.state.error = "";
        try {
            const data = await this.orm.call(
                "ai.code.node", "get_call_hierarchy", [this.state.focusKey]);
            this.state.stale = Boolean(data.stale);
            this.state.customModules = data.custom_modules || this.state.customModules;
            if (data.error === "not_found") {
                this.state.calls = null;
                this.state.error = _t("Method '%s' is not in the index — only custom addons are deep-indexed.",
                    this.state.focusKey);
                return;
            }
            if (data.needs_rebuild) {
                this.state.calls = null;
                this.state.error = _t("The index was built by an older version and contains no call data — click Rebuild Index once.");
                return;
            }
            this.state.calls = data;
        } finally {
            this.state.loading = false;
        }
    }

    async expandCall(item, direction) {
        if (!item.has_more) {
            item.children = [];
            item.has_more = true;
            return;
        }
        const data = await this.orm.call(
            "ai.code.node", "get_call_hierarchy", [item.key],
            { down_depth: 2, up_depth: 2 });
        if (!data.error) {
            item.children = direction === "down" ? data.down : data.up;
            item.has_more = false;
        }
    }

    setMode(mode) {
        if (this.state.mode === mode) {
            return;
        }
        this.state.mode = mode;
        this.state.error = "";
        this.reload();
    }

    async loadStructure() {
        this.state.loading = true;
        this.state.error = "";
        try {
            const data = await this.orm.call(
                "ai.code.node", "get_structure_data", [this.state.focusKey]);
            this.state.stale = Boolean(data.stale);
            this.state.customModules = data.custom_modules || this.state.customModules;
            if (data.error === "not_found") {
                this.state.structure = null;
                this.state.error = _t("No node found for '%s'. Try a rebuild or another search.",
                    this.state.focusKey);
                return;
            }
            this.state.structure = data;
        } finally {
            this.state.loading = false;
        }
    }

    async loadGraph() {
        this.state.loading = true;
        this.state.error = "";
        try {
            const data = await this.orm.call("ai.code.node", "get_graph_data", [], {
                focus_key: this.state.focusKey,
                depth: this.state.depth,
                layers: this.activeLayers,
                limit: 300,
            });
            this.state.stale = Boolean(data.stale);
            if (data.error === "not_found") {
                this.state.error = _t("No node found for '%s'. Try a rebuild or another search.",
                    this.state.focusKey);
                this.state.loading = false;
                return;
            }
            this.state.empty = !this.state.focusKey && data.nodes.length === 0;
            this.state.truncated = data.truncated;
            this.state.nodeCount = data.nodes.length;
            this.state.edgeCount = data.edges.length;
            this.renderGraph(data);
        } finally {
            this.state.loading = false;
        }
    }

    renderGraph(data) {
        const svgEl = this.svgRef.el;
        if (!svgEl || typeof d3 === "undefined") {
            return;
        }
        const width = svgEl.clientWidth || 900;
        const height = svgEl.clientHeight || 600;
        const svg = d3.select(svgEl);
        svg.selectAll("*").remove();
        if (this.simulation) {
            this.simulation.stop();
        }
        if (!data.nodes.length) {
            return;
        }

        const root = svg.append("g");
        svg.call(
            d3.zoom()
                .scaleExtent([0.15, 4])
                .on("zoom", (event) => root.attr("transform", event.transform))
        );

        const nodes = data.nodes.map((n) => ({ ...n }));
        const links = data.edges.map((e) => ({ ...e }));
        const focusId = data.focus_id;
        const radius = (n) =>
            (n.id === focusId ? 6 : 0) + Math.min(26, 6 + Math.sqrt(n.degree || 0) * 1.7);
        const color = (n) =>
            n.type === "module" ? LAYER_COLORS.module : (LAYER_COLORS[n.layer] || "#888");

        this.simulation = d3.forceSimulation(nodes)
            .force("link", d3.forceLink(links).id((d) => d.id)
                .distance((l) => 40 + radius(l.source) + radius(l.target))
                .strength(0.35))
            .force("charge", d3.forceManyBody().strength(-220))
            .force("center", d3.forceCenter(width / 2, height / 2))
            .force("collide", d3.forceCollide().radius((d) => radius(d) + 4));

        const link = root.append("g")
            .selectAll("line")
            .data(links)
            .join("line")
            .attr("class", "o_cg_link")
            .attr("stroke-dasharray", (l) => (DASHED_EDGES.has(l.type) ? "4,3" : null));
        link.append("title").text((l) => this.edgeLabels[l.type] || l.type);

        const node = root.append("g")
            .selectAll("g")
            .data(nodes)
            .join("g")
            .attr("class", "o_cg_node")
            .style("cursor", "pointer")
            .call(
                d3.drag()
                    .on("start", (event, d) => {
                        if (!event.active) {
                            this.simulation.alphaTarget(0.25).restart();
                        }
                        d.fx = d.x;
                        d.fy = d.y;
                    })
                    .on("drag", (event, d) => {
                        d.fx = event.x;
                        d.fy = event.y;
                    })
                    .on("end", (event, d) => {
                        if (!event.active) {
                            this.simulation.alphaTarget(0);
                        }
                        d.fx = null;
                        d.fy = null;
                    })
            );

        node.append("circle")
            .attr("r", radius)
            .attr("fill", color)
            .attr("fill-opacity", (n) => (n.skeleton ? 0.35 : 0.85))
            .attr("stroke", (n) => (n.id === focusId ? "#1D9E75" : "#ffffff"))
            .attr("stroke-width", (n) => (n.id === focusId ? 3 : 1.2));

        node.append("title")
            .text((n) => `${this.typeLabels[n.type] || n.type}: ${n.key}`);

        node.append("text")
            .attr("class", "o_cg_label")
            .attr("dy", (n) => radius(n) + 12)
            .attr("text-anchor", "middle")
            .style("display", (n) =>
                n.id === focusId || radius(n) >= 11 || nodes.length <= 40 ? null : "none")
            .text((n) => (n.name.length > 28 ? n.name.slice(0, 26) + "…" : n.name));

        node.on("click", (event, n) => this.selectNode(n.id));
        node.on("dblclick", (event, n) => {
            event.preventDefault();
            this.focusOn(n.key, n.type);
        });

        this.simulation.on("tick", () => {
            link.attr("x1", (l) => l.source.x)
                .attr("y1", (l) => l.source.y)
                .attr("x2", (l) => l.target.x)
                .attr("y2", (l) => l.target.y);
            node.attr("transform", (n) => `translate(${n.x},${n.y})`);
        });
    }

    async selectNode(nodeId) {
        this.state.selected = await this.orm.call(
            "ai.code.node", "get_node_details", [[nodeId]]);
    }

    async selectNodeAndFocus(nodeId) {
        const details = await this.orm.call(
            "ai.code.node", "get_node_details", [[nodeId]]);
        this.focusOn(details.key, details.type);
    }

    focusOn(key, nodeType = false) {
        this.state.focusKey = key;
        this.state.selected = null;
        this.state.searchResults = [];
        this.state.searchTerm = "";
        this.state.methodFilter = "";
        // models/modules/JS/templates read best in Structure, methods in Calls
        const structureTypes = ["model", "module", "js_component", "js_service",
            "js_function", "qweb_template", "view", "action", "menu", "field",
            "server_action"];
        if (structureTypes.includes(nodeType)) {
            this.state.mode = "structure";
        } else if (nodeType === "method") {
            this.state.mode = "calls";
        } else if (nodeType) {
            this.state.mode = "graph";
        }
        this.reload();
    }

    backToOverview() {
        this.state.focusKey = false;
        this.state.selected = null;
        this.state.structure = null;
        this.state.mode = "graph";
        this.loadGraph();
    }

    toggleLayer(layer) {
        this.state.layers[layer] = !this.state.layers[layer];
        if (this.state.mode === "graph") {
            this.loadGraph();
        }
    }

    onDepthChange(ev) {
        this.state.depth = parseInt(ev.target.value, 10) || 2;
        if (this.state.mode === "graph") {
            this.loadGraph();
        }
    }

    onSearchInput(ev) {
        const term = ev.target.value;
        this.state.searchTerm = term;
        clearTimeout(this.searchTimer);
        if (!term || term.length < 2) {
            this.state.searchResults = [];
            return;
        }
        this.searchTimer = setTimeout(async () => {
            this.state.searchResults = await this.orm.call(
                "ai.code.node", "search_nodes", [term], { limit: 8 });
        }, 250);
    }

    onSearchKeydown(ev) {
        if (ev.key === "Enter" && this.state.searchTerm) {
            this.focusOn(this.state.searchTerm.trim(), "model");
        } else if (ev.key === "Escape") {
            this.state.searchResults = [];
        }
    }

    async rebuildIndex() {
        this.state.rebuilding = true;
        try {
            const result = await this.orm.call("ai.code.graph.builder", "action_rebuild", []);
            if (result) {
                await this.actionService.doAction(result);
            }
            this.state.empty = false;
            await this.reload();
        } finally {
            this.state.rebuilding = false;
        }
    }

    async openRecord(refModel, refId) {
        if (!refModel || !refId) {
            return;
        }
        await this.actionService.doAction({
            type: "ir.actions.act_window",
            res_model: refModel,
            res_id: refId,
            views: [[false, "form"]],
            target: "current",
        });
    }

    async openNodeRecord(nodeId) {
        const details = await this.orm.call(
            "ai.code.node", "get_node_details", [[nodeId]]);
        await this.openRecord(details.ref_model, details.ref_id);
    }

    async openBackingRecord() {
        const selected = this.state.selected;
        if (selected) {
            await this.openRecord(selected.ref_model, selected.ref_id);
        }
    }
}

registry.category("actions").add("ai_code_graph_action", CodeGraphAction);
