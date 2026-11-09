import {onMounted, onWillStart, onWillUnmount, useExternalListener, useRef, useState} from "@odoo/owl";
import {registry} from "@web/core/registry";
import {useService} from "@web/core/utils/hooks";
import {user} from "@web/core/user";
import {browser} from "@web/core/browser/browser";
import {_t} from "@web/core/l10n/translation";
import {ConfirmationDialog} from "@web/core/confirmation_dialog/confirmation_dialog";

import {AiPlatform} from "@ai_chat_platform/platform/ai_platform";
import {GuidedTaskDialog} from "./guided_task";

/**
 * Coding workspace — a dedicated full-page chat that EXTENDS the AI Platform
 * page (reuses its sidebar / thread / composer and all conversation/send
 * logic) without modifying ai_chat_platform. It is its own client action, so
 * the normal Chat page is completely untouched.
 *
 * Additions over the platform chat:
 *  - a working-DIRECTORY picker (auto-detected addons folders, no config);
 *  - a live task strip: state chip, todo list, activity feed (tool calls as
 *    they happen), Stop button, cost;
 *  - a diff-review drawer with per-step git revert.
 *
 * Live updates: the runner pushes `ai_code/activity` bus events after every
 * tool call / round; polling (2.5s while working) is the fallback.
 */
const isMobile = () => window.innerWidth <= 768;
const RUNNING = new Set(["running"]);
const WAITING = new Set(["waiting_user"]);
const POLL_MS = 2500;

export class AiCodeWorkspace extends AiPlatform {
    static template = "ai_code.Workspace";

    setup() {
        this.store = useState(useService("mail.store"));
        this.orm = useService("orm");
        this.dialog = useService("dialog");
        this.notification = useService("notification");
        this.busService = useService("bus_service");
        this.state = useState({
            conversations: [],
            agents: [],
            suggestions: [],
            agentId: false,
            activeId: false,
            collapsed: isMobile(),
            busy: false,
            loadingThread: false,
            usage: null,
            userName: "",
            agentMenuOpen: false,
            preview: null,
            canViewAll: false, // Settings admin: may list every user's conversations
            showAll: false, // admin toggle "All users" (persisted per user)
        });
        this.codeState = useState({
            workspaces: [],
            workingDir: "",
            dirMenuOpen: false,
            recent: [],           // per-user recent folders
            browse: null,         // {current, parent, entries, selectable, current_is_git} or null
            browseLoading: false,
            scan: null,           // {is_empty, modules:[{name,title}]} of the current folder
            intent: null,         // {type:'modify', module:'x'} from a chip click
            attachments: [],      // documents uploaded to this conversation
            uploading: false,
            devReload: false,
            devWarnDismissed: false,
            bgTasks: [],          // background-tasks widget rows (active)
            bgOpen: false,
            bgTab: "active",      // 'active' | 'history'
            history: [],          // finished tasks (History tab)
            historyLoading: false,
            refreshing: false,    // manual-refresh spinner
            task: null,
            todoOpen: false,
            activityOpen: false,
            changesOpen: false,
            selectedChange: null,
            diffLoading: false,
            stopping: false,
            draft: "",            // streamed text of the answer being generated
            models: [],           // [{id, label, provider, provider_label}] with a key configured
            currentModel: "",     // model of the active session (or the user's default)
            modelMenuOpen: false,
        });
        this._aicPollTimer = null;
        this._aicBgTimer = null;
        this._aicPendingSince = null;   // "message sent, AI not yet observed working"
        this._aicRefreshQueued = false;
        this.aicFileInput = useRef("aicFileInput");
        useExternalListener(document, "click", (ev) => this._aicDismissOutside(ev));
        useExternalListener(document, "keydown", (ev) => {
            if (ev.key === "Escape") {
                this._aicCloseAllPopovers();
            }
        });
        const growIfComposer = (el) => {
            if (el && el.tagName === "TEXTAREA" && el.closest(".o_aip_inputbar")) {
                this._aicAutoGrow(el);
            }
        };
        useExternalListener(document, "input", (ev) => growIfComposer(ev.target));
        useExternalListener(document, "paste", (ev) => {
            const el = ev.target;
            browser.setTimeout(() => growIfComposer(el), 0);
        });
        // live progress push from the runner (tool calls, rounds, todo updates)
        this._aicOnBus = (payload) => this._aicOnActivity(payload);
        this.busService.subscribe("ai_code/activity", this._aicOnBus);
        // token streaming of the answer being generated
        this._aicOnStream = (payload) => this._aicOnStreamDelta(payload);
        this.busService.subscribe("ai_code/stream", this._aicOnStream);
        this._aicStream = {id: 0, seq: 0};
        onWillStart(async () => {
            const data = await this.orm.call("discuss.channel", "ai_code_workspace_data", []);
            Object.assign(this.state, {
                conversations: data.conversations,
                agents: [{
                    id: data.coding_agent_id,
                    name: data.coding_agent_name,
                    subtitle: "",
                    model: "",
                    is_coding_agent: true,
                }],
                agentId: data.coding_agent_id,
                userName: data.user_name,
            });
            this.codeState.workspaces = data.workspaces || [];
            this.codeState.recent = data.recent || [];
            this.codeState.models = data.models || [];
            this.codeState.currentModel = data.current_model || "";
            this.codeState.devReload = !!data.dev_reload;
            if (data.dev_reload) {
                this.codeState.devWarnDismissed =
                    browser.localStorage.getItem("ai_code.devwarn.dismissed") === "1";
            } else {
                browser.localStorage.removeItem("ai_code.devwarn.dismissed");
            }
            const pref = this.codeState.recent[0] || (
                this.codeState.workspaces.length === 1 ? this.codeState.workspaces[0] : null);
            if (pref) {
                this.codeState.workingDir = pref.path;
                this.aicScanFolder(pref.path);
            }
            await this.restoreShowAll(data.can_view_all);
            const saved = parseInt(browser.localStorage.getItem(this.lastConvKey), 10);
            if (saved && this.state.conversations.some((c) => c.id === saved)) {
                await this.openConversation(saved);
            }
        });
        onWillUnmount(() => {
            this._aicStopPolling();
            this._aicStopBgPolling();
            this.busService.unsubscribe("ai_code/activity", this._aicOnBus);
            this.busService.unsubscribe("ai_code/stream", this._aicOnStream);
            if (this._aicCodeObserver) {
                this._aicCodeObserver.disconnect();
            }
        });
        this.aicFetchBgTasks();
        onMounted(() => this._aicSetupCodeCopy());
    }

    /** Streaming deltas of the answer being generated for the active
     *  conversation: shown in a live draft bubble until the real message is
     *  posted (kind "end"). Out-of-order/late chunks of an older stream are ignored. */
    _aicOnStreamDelta(payload) {
        if (!payload || payload.channel_id !== this.state.activeId) {
            return;
        }
        const st = this._aicStream;
        if (payload.kind === "end") {
            if (payload.stream >= st.id) {
                this.codeState.draft = "";
                st.id = payload.stream;
                st.seq = 0;
            }
            return;
        }
        if (payload.stream < st.id) {
            return;
        }
        if (payload.stream > st.id) {
            st.id = payload.stream;
            st.seq = 0;
            this.codeState.draft = "";
        }
        if (payload.seq <= st.seq) {
            return;
        }
        st.seq = payload.seq;
        this.codeState.draft += payload.text || "";
        browser.setTimeout(() => {
            const el = document.querySelector(".o_aic_draft_body");
            if (el) {
                el.scrollTop = el.scrollHeight;
            }
        }, 0);
    }

    /** Bus push from the runner: refresh the strip of the affected conversation
     *  at once (debounced — tool calls can come several per second). */
    _aicOnActivity(payload) {
        if (!payload || payload.channel_id !== this.state.activeId) {
            if (payload && payload.kind === "turn_end") {
                this.aicFetchBgTasks();
            }
            return;
        }
        if (this._aicRefreshQueued) {
            return;
        }
        this._aicRefreshQueued = true;
        browser.setTimeout(async () => {
            this._aicRefreshQueued = false;
            await this.aicRefreshTask();
            if (payload.kind === "turn_end") {
                this.aicFetchBgTasks();
                if (this.aicWorkingDir) {
                    this.aicScanFolder(this.aicWorkingDir);
                }
            }
        }, 250);
    }

    // ------------------------------------------------------------------
    // Copy-to-clipboard for code (chat code blocks + diff cards)
    // ------------------------------------------------------------------
    async aicCopy(text, label) {
        const ok = await this._aicWriteClipboard(text || "");
        this.notification.add(
            ok ? (label || _t("Copied")) : _t("Copy failed — select and copy manually."),
            {type: ok ? "success" : "warning"});
    }

    async _aicWriteClipboard(text) {
        const clip = browser.navigator && browser.navigator.clipboard;
        if (clip && window.isSecureContext) {
            try {
                await clip.writeText(text);
                return true;
            } catch {
                // fall through to the legacy path
            }
        }
        try {
            const ta = document.createElement("textarea");
            ta.value = text;
            ta.setAttribute("readonly", "");
            ta.style.position = "fixed";
            ta.style.top = "-1000px";
            ta.style.opacity = "0";
            document.body.appendChild(ta);
            ta.select();
            ta.setSelectionRange(0, text.length);
            const ok = document.execCommand("copy");
            document.body.removeChild(ta);
            return ok;
        } catch {
            return false;
        }
    }

    aicCopyDiff() {
        this.aicCopy(this.codeState.selectedChange?.diff || "", _t("Diff copied"));
    }

    async aicCopyFile(change) {
        const c = change || this.codeState.selectedChange;
        if (!c) {
            return;
        }
        try {
            const res = await this.orm.call("discuss.channel", "ai_code_get_change_content",
                [[this.state.activeId], c.id]);
            await this.aicCopy(res.content || "", _t("File copied"));
        } catch (e) {
            this.notification.add(e.data?.message || String(e), {type: "danger"});
        }
    }

    _aicSetupCodeCopy() {
        const root = document.querySelector(".o_ai_platform") || document.body;
        this._aicEnhanceCodeBlocks(root);
        this._aicCodeObserver = new MutationObserver((mutations) => {
            for (const m of mutations) {
                for (const node of m.addedNodes) {
                    if (node.nodeType === 1) {
                        this._aicEnhanceCodeBlocks(node);
                    }
                }
            }
        });
        this._aicCodeObserver.observe(root, {childList: true, subtree: true});
    }

    _aicEnhanceCodeBlocks(scope) {
        const pres = scope.matches?.("pre") ? [scope] : scope.querySelectorAll?.("pre") || [];
        for (const pre of pres) {
            if (pre.dataset.aicCopy || !pre.querySelector("code")) {
                continue;
            }
            pre.dataset.aicCopy = "1";
            pre.classList.add("o_aic_codeblock");
            const code = pre.querySelector("code");
            const lang = (Array.from(code.classList).find((c) => c.startsWith("language-")) || "")
                .replace("language-", "");
            const bar = document.createElement("div");
            bar.className = "o_aic_codebar";
            const label = document.createElement("span");
            label.className = "o_aic_codelang";
            label.textContent = lang || "code";
            const btn = document.createElement("button");
            btn.className = "o_aic_codecopy";
            btn.type = "button";
            btn.innerHTML = '<i class="fa fa-clipboard"></i> ' + _t("Copy");
            btn.addEventListener("click", (ev) => {
                ev.stopPropagation();
                this.aicCopy(code.textContent, _t("Code copied"));
                btn.innerHTML = '<i class="fa fa-check"></i> ' + _t("Copied");
                browser.setTimeout(() => {
                    btn.innerHTML = '<i class="fa fa-clipboard"></i> ' + _t("Copy");
                }, 1500);
            });
            bar.appendChild(label);
            bar.appendChild(btn);
            pre.prepend(bar);
        }
    }

    // ------------------------------------------------------------------
    // Background-tasks widget (all conversations, like a task manager)
    // ------------------------------------------------------------------
    get aicBgRunningCount() {
        return this.codeState.bgTasks.filter((t) => t.running).length;
    }

    get aicBgWaitingCount() {
        return this.codeState.bgTasks.filter((t) => t.waiting).length;
    }

    aicToggleBgPanel() {
        this.codeState.bgOpen = !this.codeState.bgOpen;
        if (this.codeState.bgOpen) {
            this.aicFetchBgTasks();
            if (this.codeState.bgTab === "history") {
                this.aicFetchHistory();
            }
        }
    }

    async aicSetBgTab(tab) {
        this.codeState.bgTab = tab;
        if (tab === "history") {
            await this.aicFetchHistory();
        } else {
            await this.aicFetchBgTasks();
        }
    }

    async aicFetchHistory() {
        this.codeState.historyLoading = true;
        try {
            this.codeState.history = await this.orm.call(
                "discuss.channel", "ai_code_task_history", []);
        } catch {
            this.codeState.history = [];
        } finally {
            this.codeState.historyLoading = false;
        }
    }

    async aicManualRefresh() {
        if (this.codeState.refreshing) {
            return;
        }
        this.codeState.refreshing = true;
        try {
            await this.aicRefreshTask();
            await this.aicFetchBgTasks();
            if (this.codeState.bgTab === "history") {
                await this.aicFetchHistory();
            }
            if (this.aicWorkingDir) {
                await this.aicScanFolder(this.aicWorkingDir);
            }
            await this.refreshConversations();
        } finally {
            this.codeState.refreshing = false;
        }
    }

    async aicFetchBgTasks() {
        try {
            this.codeState.bgTasks = await this.orm.call(
                "discuss.channel", "ai_code_running_tasks", []);
        } catch {
            this.codeState.bgTasks = [];
        }
        this._aicManageBgPolling();
    }

    _aicManageBgPolling() {
        const active = this.aicBgRunningCount > 0 || this.codeState.bgOpen;
        if (active && !this._aicBgTimer) {
            this._aicBgTimer = setInterval(() => this.aicFetchBgTasks(), 5000);
        } else if (!active && this._aicBgTimer) {
            this._aicStopBgPolling();
        }
    }

    _aicStopBgPolling() {
        if (this._aicBgTimer) {
            clearInterval(this._aicBgTimer);
            this._aicBgTimer = null;
        }
    }

    aicFmtElapsed(seconds) {
        const m = Math.floor(seconds / 60);
        const s = seconds % 60;
        return m ? `${m}m ${String(s).padStart(2, "0")}s` : `${s}s`;
    }

    aicBgStateLabel(t) {
        return this.aicPhaseLabelFor(t.state);
    }

    aicPhaseLabelFor(state) {
        return {
            draft: _t("Draft"),
            idle: _t("Ready"),
            running: _t("Working"),
            waiting_user: _t("Waiting for your answer"),
            failed: _t("Failed"),
            cancelled: _t("Cancelled"),
            // legacy pipeline states (old records)
            analyzing: _t("Analyzing"),
            spec_review: _t("Awaiting spec approval"),
            studying: _t("Studying"),
            planning: _t("Planning"),
            plan_review: _t("Awaiting plan approval"),
            coding: _t("Coding"),
            testing: _t("Sandbox testing"),
            done: _t("Done"),
        }[state] || state;
    }

    async aicStopBgTask(t) {
        try {
            const res = await this.orm.call("discuss.channel", "ai_code_stop_task", [t.task_id]);
            this.notification.add(
                res.action === "aborting"
                    ? _t("Stopping after the current step…")
                    : _t("Question dismissed — the session is ready for a new message."),
                {type: "info"});
            await this.aicFetchBgTasks();
            if (t.channel_id === this.state.activeId) {
                this.aicRefreshTask();
            }
        } catch (e) {
            this.notification.add(e.data?.message || String(e), {type: "danger"});
        }
    }

    /** Stop button in the header for the CURRENT conversation. */
    async aicStopCurrent() {
        const t = this.aicTask;
        if (!t || this.codeState.stopping) {
            return;
        }
        this.codeState.stopping = true;
        try {
            await this.aicStopBgTask({task_id: t.task_id, channel_id: this.state.activeId});
        } finally {
            browser.setTimeout(() => {
                this.codeState.stopping = false;
            }, 3000);
        }
    }

    async aicOpenBgTask(t) {
        this.codeState.bgOpen = false;
        if (t.channel_id && t.channel_id !== this.state.activeId) {
            await this.openConversation(t.channel_id);
        }
    }

    get lastConvKey() {
        return `ai_code.last_conv.${user.userId}`;
    }

    // ------------------------------------------------------------------
    // Model picker
    // ------------------------------------------------------------------
    get aicModelLabel() {
        const id = this.aicTask?.model || this.codeState.currentModel;
        if (!id) {
            return _t("Model");
        }
        const m = this.codeState.models.find((x) => x.id === id);
        return m ? m.label.split(" (")[0] : id;
    }

    get aicCurrentModelId() {
        return this.aicTask?.model || this.codeState.currentModel || "";
    }

    /** Models grouped by provider for the dropdown. */
    get aicModelGroups() {
        const groups = [];
        for (const m of this.codeState.models) {
            let g = groups.find((x) => x.provider === m.provider);
            if (!g) {
                g = {provider: m.provider, label: m.provider_label, models: []};
                groups.push(g);
            }
            g.models.push(m);
        }
        return groups;
    }

    aicToggleModelMenu(ev) {
        ev.stopPropagation();
        this.codeState.modelMenuOpen = !this.codeState.modelMenuOpen;
    }

    async aicPickModel(modelId) {
        this.codeState.modelMenuOpen = false;
        try {
            if (this.state.activeId) {
                await this.orm.call("discuss.channel", "ai_code_set_model",
                    [[this.state.activeId], modelId]);
            } else {
                await this.orm.call("discuss.channel", "ai_code_set_default_model", [modelId]);
            }
            this.codeState.currentModel = modelId;
            if (this.codeState.task) {
                this.codeState.task.model = modelId;
            }
            const conv = this.activeConversation;
            if (conv) {
                conv.agent_model = modelId;
            }
            this.notification.add(_t("Model set to %s. It applies from the next message.",
                this.aicModelLabel), {type: "success"});
        } catch (e) {
            this.notification.add(e.data?.message || String(e), {type: "danger"});
        }
    }

    // ------------------------------------------------------------------
    // Working directory
    // ------------------------------------------------------------------
    get aicWorkingDir() {
        return this.activeConversation?.working_dir || this.codeState.workingDir || "";
    }

    get aicWorkingDirLabel() {
        const dir = this.aicWorkingDir;
        if (!dir) {
            return _t("Choose a folder");
        }
        const ws = this.codeState.workspaces.find((w) => dir.startsWith(w.path));
        return ws ? ws.label : dir.split(/[\\/]/).filter(Boolean).pop();
    }

    get aicFolderLocked() {
        return !!this.state.activeId;
    }

    _aicAutoGrow(el) {
        el.style.height = "auto";
        el.style.height = Math.min(el.scrollHeight, 220) + "px";
    }

    aicOpenGuided() {
        if (!this.aicWorkingDir) {
            this.notification.add(
                _t("Choose a working directory first."), {type: "warning"});
            return;
        }
        this.dialog.add(GuidedTaskDialog, {
            version: this.codeState.scan?.version || "19",
            modules: this.aicFolderModules,
            folderLabel: this.aicWorkingDirLabel,
            onConfirm: (text) => this.sendMessage(text),
        });
    }

    aicToggleDirMenu() {
        this.codeState.dirMenuOpen = !this.codeState.dirMenuOpen;
        if (!this.codeState.dirMenuOpen) {
            this.codeState.browse = null;
        }
    }

    get aicGit() {
        return this.codeState.scan && this.codeState.scan.git
            ? this.codeState.scan.git : null;
    }

    get aicGitTitle() {
        const g = this.aicGit;
        if (!g || !g.is_git) {
            return "";
        }
        const state = g.dirty
            ? _t("%s uncommitted change(s)", g.dirty)
            : _t("clean");
        return _t("Git · branch %s · %s. Ask the AI for git diff / log / status.",
            g.branch, state);
    }

    async aicOpenBrowse(path) {
        this.codeState.browseLoading = true;
        try {
            this.codeState.browse = await this.orm.call(
                "discuss.channel", "ai_code_browse_dir", [path || ""]);
        } catch (e) {
            this.notification.add(e.data?.message || String(e), {type: "danger"});
        } finally {
            this.codeState.browseLoading = false;
        }
    }

    aicCloseBrowse() {
        this.codeState.browse = null;
    }

    async aicUseBrowsedDir() {
        const path = this.codeState.browse?.current;
        if (!path) {
            return;
        }
        try {
            const res = await this.orm.call(
                "discuss.channel", "ai_code_adopt_workspace", [path]);
            this.codeState.workspaces = res.workspaces || [];
            this.codeState.recent = res.recent || [];
            this.codeState.browse = null;
            this.codeState.dirMenuOpen = false;
            await this.aicPickWorkingDir(res.adopted || path);
        } catch (e) {
            this.notification.add(e.data?.message || String(e), {type: "danger"});
        }
    }

    async aicPickWorkingDir(path) {
        this.codeState.dirMenuOpen = false;
        const conv = this.activeConversation;
        if (conv && conv.working_dir && conv.working_dir !== path) {
            this.newChat();
        }
        this.codeState.workingDir = path;
        this.aicScanFolder(path);
    }

    async aicScanFolder(path) {
        this.codeState.scan = null;
        this.codeState.intent = null;
        if (!path) {
            return;
        }
        try {
            this.codeState.scan = await this.orm.call(
                "discuss.channel", "ai_code_scan_workspace", [path]);
        } catch {
            this.codeState.scan = null;
        }
    }

    get aicFolderModules() {
        return this.codeState.scan?.modules || [];
    }

    aicPickModule(moduleName) {
        this.codeState.intent = {type: "modify", module: moduleName};
    }

    aicClearIntent() {
        this.codeState.intent = null;
    }

    // ------------------------------------------------------------------
    // Task strip getters
    // ------------------------------------------------------------------
    get aicTask() {
        return this.codeState.task;
    }

    get aicPhaseLabel() {
        const t = this.aicTask;
        if (!t) {
            return "";
        }
        if (t.running && t.current) {
            return _t("Working");
        }
        return this.aicPhaseLabelFor(t.state);
    }

    get aicPhaseClass() {
        const s = this.aicTask?.state;
        if (s === "idle" || s === "done") {
            return "o_aic_chip--done";
        }
        if (s === "failed" || s === "cancelled") {
            return "o_aic_chip--fail";
        }
        if (WAITING.has(s)) {
            return "o_aic_chip--review";
        }
        return RUNNING.has(s) ? "o_aic_chip--run" : "";
    }

    get aicTodoPercent() {
        const todo = this.aicTask?.todo;
        return todo && todo.total ? Math.round((todo.done_count / todo.total) * 100) : 0;
    }

    get aicTodoItems() {
        return this.aicTask?.todo?.items || [];
    }

    get aicActivity() {
        return this.aicTask?.activity || [];
    }

    /** Human label of one activity row (tool name + short argument). */
    aicActivityLabel(a) {
        if (a.kind === "phase_change") {
            return a.summary;
        }
        const names = {
            read_file: _t("Read"), grep: _t("Search"), glob: _t("List files"),
            tree: _t("Tree"), outline: _t("Outline"), run_code: _t("Explore (program)"),
            write_file: _t("Write"), edit_file: _t("Edit"), multi_edit: _t("Edit"),
            scaffold_module: _t("Scaffold module"), sandbox_test: _t("Sandbox test"),
            todo_write: _t("Plan"), ask_user_question: _t("Ask you"),
            odoo_docs: _t("Idioms"), read_module_source: _t("Read Odoo source"),
            grep_module_source: _t("Search Odoo source"), code_graph_model_map: _t("Model map"),
            code_graph_blast_radius: _t("Blast radius"), find_symbol: _t("Find symbol"),
            repo_map: _t("Repo map"), mem_search: _t("Memory"), mem_get: _t("Memory"),
            list_attachments: _t("Attachments"), read_attachment: _t("Read document"),
            git_status: _t("Git status"), git_log: _t("Git log"), git_diff: _t("Git diff"),
        };
        const label = names[a.name] || a.name;
        const arg = (a.summary || "").replace(/^[a-z_]+\(/, "").replace(/\)$/, "");
        return arg ? `${label} · ${arg}` : label;
    }

    aicActivityIcon(a) {
        if (a.kind === "phase_change") {
            return "fa-flag-o";
        }
        if (a.status === "running") {
            return "fa-circle-o-notch fa-spin";
        }
        if (a.status === "error") {
            return "fa-exclamation-circle o_aic_act--err";
        }
        if (["write_file", "edit_file", "multi_edit", "scaffold_module"].includes(a.name)) {
            return "fa-pencil";
        }
        if (a.name === "sandbox_test") {
            return "fa-flask";
        }
        if (a.name === "todo_write") {
            return "fa-list-ul";
        }
        return "fa-search";
    }

    get aicActiveChanges() {
        return (this.aicTask?.changes || []).filter((c) => !c.reverted);
    }

    get aicDiffLines() {
        const diff = this.codeState.selectedChange?.diff || "";
        return diff.split("\n").map((text, i) => {
            let cls = "";
            if (text.startsWith("+") && !text.startsWith("+++")) {
                cls = "o_aic_diff_add";
            } else if (text.startsWith("-") && !text.startsWith("---")) {
                cls = "o_aic_diff_del";
            } else if (text.startsWith("@@")) {
                cls = "o_aic_diff_hunk";
            }
            return {id: i, text, cls};
        });
    }

    // ------------------------------------------------------------------
    // Overrides: bind conversations to the folder + coding RPCs
    // ------------------------------------------------------------------
    async ensureConversation(title) {
        if (this.state.activeId && this.thread) {
            return this.thread;
        }
        const dir = this.codeState.workingDir;
        if (!dir) {
            this.notification.add(
                _t("Choose a working directory first (folder picker in the header)."),
                {type: "warning"});
            throw new Error("No working directory selected");
        }
        const {channel_id, store_data} = await this.orm.call(
            "discuss.channel", "ai_code_new_conversation", [dir, title || false]);
        this.store.insert(store_data);
        this.state.activeId = channel_id;
        browser.localStorage.setItem(this.lastConvKey, channel_id);
        this.refreshConversations();
        return this.store.Thread.getOrFetch({model: "discuss.channel", id: channel_id});
    }

    async refreshConversations() {
        this.state.conversations = await this.orm.call(
            "discuss.channel", "ai_code_get_conversations", [this.state.showAll]);
    }

    async openConversation(id) {
        await super.openConversation(...arguments);
        this.codeState.task = null;
        this.codeState.draft = "";
        this.codeState.selectedChange = null;
        this.codeState.changesOpen = false;
        await this.aicRefreshTask();
        this.aicLoadAttachments();
        if (this.aicWorkingDir) {
            this.aicScanFolder(this.aicWorkingDir);
        }
    }

    newChat() {
        super.newChat(...arguments);
        this.codeState.task = null;
        this.codeState.selectedChange = null;
        this.codeState.changesOpen = false;
        this.codeState.attachments = [];
        this._aicStopPolling();
        if (this.codeState.workingDir) {
            this.aicScanFolder(this.codeState.workingDir);
        }
    }

    async sendMessage(text) {
        let body = text;
        if (this.codeState.intent?.module && (text || "").trim()) {
            body = _t("Modify the %s module: %s", this.codeState.intent.module, text);
        }
        const sent = await super.sendMessage(body);
        if (sent) {
            this.codeState.intent = null;
            const ta = document.querySelector(".o_aip_inputbar textarea");
            if (ta) {
                ta.style.height = "auto";
            }
            this._aicPendingSince = Date.now();
            await this.aicRefreshTask();
            this.aicFetchBgTasks();
        }
        return sent;
    }

    // ------------------------------------------------------------------
    // Task progress polling
    // ------------------------------------------------------------------
    async aicRefreshTask() {
        const channelId = this.state.activeId;
        if (!channelId) {
            this._aicStopPolling();
            return;
        }
        try {
            const task = await this.orm.call(
                "discuss.channel", "ai_code_task_progress", [[channelId]]);
            if (this.state.activeId === channelId) {
                this.codeState.task = task && task.task_id ? task : null;
                const conv = this.activeConversation;
                if (conv && task && task.task_id) {
                    conv.task_state = task.state;
                }
                if (task && RUNNING.has(task.state)) {
                    this._aicPendingSince = null;
                }
            }
        } catch {
            this.codeState.task = null;
        }
        this._aicManagePolling();
    }

    _aicPendingFresh() {
        return !!(this._aicPendingSince && Date.now() - this._aicPendingSince < 30000);
    }

    get aicWorking() {
        const t = this.aicTask;
        if (t && RUNNING.has(t.state)) {
            return true;
        }
        return this._aicPendingFresh();
    }

    get aicWorkingLabel() {
        const t = this.aicTask;
        if (t && RUNNING.has(t.state)) {
            if (t.current) {
                return this.aicActivityLabel(t.current);
            }
            return _t("Thinking");
        }
        return _t("Thinking");
    }

    _aicManagePolling() {
        const t = this.aicTask;
        const active = (t && RUNNING.has(t.state)) || this._aicPendingFresh();
        if (active && !this._aicPollTimer) {
            this._aicPollTimer = setInterval(() => this.aicRefreshTask(), POLL_MS);
        } else if (!active && this._aicPollTimer) {
            this._aicStopPolling();
        }
    }

    _aicStopPolling() {
        if (this._aicPollTimer) {
            clearInterval(this._aicPollTimer);
            this._aicPollTimer = null;
        }
    }

    // ------------------------------------------------------------------
    // Todo / activity / diff drawer
    // ------------------------------------------------------------------
    aicToggleTodo() {
        this.codeState.todoOpen = !this.codeState.todoOpen;
        this.codeState.activityOpen = false;
    }

    aicToggleActivity() {
        this.codeState.activityOpen = !this.codeState.activityOpen;
        this.codeState.todoOpen = false;
        if (this.codeState.activityOpen) {
            this.aicRefreshTask();
        }
    }

    _aicCloseAllPopovers() {
        this.state.agentMenuOpen = false;
        this.codeState.dirMenuOpen = false;
        this.codeState.bgOpen = false;
        this.codeState.todoOpen = false;
        this.codeState.activityOpen = false;
        this.codeState.changesOpen = false;
        this.codeState.modelMenuOpen = false;
    }

    _aicDismissOutside(ev) {
        const t = ev.target;
        if (!t || !t.closest) {
            return;
        }
        const cs = this.codeState;
        if (cs.dirMenuOpen && !t.closest(".o_aic_dirpicker")) {
            cs.dirMenuOpen = false;
        }
        if (cs.bgOpen && !t.closest(".o_aic_bg")) {
            cs.bgOpen = false;
        }
        if (cs.todoOpen && !t.closest(".o_aic_todo")) {
            cs.todoOpen = false;
        }
        if (cs.activityOpen && !t.closest(".o_aic_activity")) {
            cs.activityOpen = false;
        }
        if (cs.modelMenuOpen && !t.closest(".o_aic_modelpicker")) {
            cs.modelMenuOpen = false;
        }
        if (this.state.agentMenuOpen) {
            this.state.agentMenuOpen = false;
        }
        if (cs.changesOpen &&
            !t.closest(".o_aic_drawer") && !t.closest(".o_aic_changes_btn")) {
            cs.changesOpen = false;
        }
    }

    aicToggleChanges() {
        this.codeState.changesOpen = !this.codeState.changesOpen;
        if (this.codeState.changesOpen) {
            this.codeState.selectedChange = null;
            this.aicRefreshTask();
        }
    }

    async aicOpenChange(change) {
        this.codeState.diffLoading = true;
        try {
            this.codeState.selectedChange = await this.orm.call(
                "discuss.channel", "ai_code_get_change_diff",
                [[this.state.activeId], change.id]);
        } catch (e) {
            this.notification.add(e.data?.message || String(e), {type: "danger"});
        } finally {
            this.codeState.diffLoading = false;
        }
    }

    // ------------------------------------------------------------------
    // Conversation documents (spec-by-example uploads)
    // ------------------------------------------------------------------
    async aicLoadAttachments() {
        if (!this.state.activeId) {
            this.codeState.attachments = [];
            return;
        }
        try {
            this.codeState.attachments = await this.orm.call(
                "discuss.channel", "ai_code_list_attachments", [[this.state.activeId]]);
        } catch {
            this.codeState.attachments = [];
        }
    }

    async aicClickAttach() {
        if (!this.state.activeId) {
            try {
                await this.ensureConversation();
                await this.refreshConversations();
            } catch {
                return;
            }
        }
        this.aicFileInput.el?.click();
    }

    async aicFilesChosen(ev) {
        const files = Array.from(ev.target.files || []);
        ev.target.value = "";
        if (!files.length || !this.state.activeId) {
            return;
        }
        this.codeState.uploading = true;
        try {
            for (const file of files) {
                if (file.size > 15 * 1024 * 1024) {
                    this.notification.add(
                        _t("%s is larger than 15 MB — skipped.", file.name),
                        {type: "warning"});
                    continue;
                }
                const b64 = await new Promise((resolve, reject) => {
                    const reader = new FileReader();
                    reader.onload = () => resolve(String(reader.result).split(",")[1] || "");
                    reader.onerror = reject;
                    reader.readAsDataURL(file);
                });
                await this.orm.call("discuss.channel", "ai_code_upload_attachment",
                    [[this.state.activeId], file.name, b64, file.type || false]);
            }
            await this.aicLoadAttachments();
            this.notification.add(
                _t("Uploaded. Now describe what to build from these documents — " +
                   "the AI reads them while working."), {type: "success"});
        } catch (e) {
            this.notification.add(e.data?.message || String(e), {type: "danger"});
        } finally {
            this.codeState.uploading = false;
        }
    }

    async aicDeleteAttachment(att) {
        try {
            await this.orm.call("discuss.channel", "ai_code_delete_attachment",
                [[this.state.activeId], att.id]);
            await this.aicLoadAttachments();
        } catch (e) {
            this.notification.add(e.data?.message || String(e), {type: "danger"});
        }
    }

    aicFmtSize(bytes) {
        if (!bytes) {
            return "";
        }
        return bytes >= 1048576
            ? (bytes / 1048576).toFixed(1) + " MB"
            : Math.max(1, Math.round(bytes / 1024)) + " KB";
    }

    aicAttIcon(att) {
        const n = (att.name || "").toLowerCase();
        if (n.endsWith(".pdf")) {
            return "fa-file-pdf-o";
        }
        if (/\.(xlsx?|xlsm|csv)$/.test(n)) {
            return "fa-file-excel-o";
        }
        if (/\.(docx?|txt|md)$/.test(n)) {
            return "fa-file-text-o";
        }
        if (/\.(png|jpe?g|gif|webp)$/.test(n)) {
            return "fa-file-image-o";
        }
        return "fa-file-o";
    }

    aicDismissDevWarn() {
        this.codeState.devWarnDismissed = true;
        browser.localStorage.setItem("ai_code.devwarn.dismissed", "1");
    }

    async aicAckError() {
        const t = this.aicTask;
        if (!t) {
            return;
        }
        try {
            await this.orm.call("discuss.channel", "ai_code_ack_error",
                [[this.state.activeId], t.task_id]);
            t.error_ack = true;
        } catch (e) {
            this.notification.add(e.data?.message || String(e), {type: "danger"});
        }
    }

    aicRevertChange(change) {
        this.dialog.add(ConfirmationDialog, {
            title: _t("Revert this step"),
            body: _t('Revert the change on "%s" (git revert %s)? Later steps touching the same lines may conflict.',
                change.file_path, change.git_sha),
            confirmLabel: _t("Revert"),
            cancelLabel: _t("Cancel"),
            confirm: async () => {
                try {
                    await this.orm.call("discuss.channel", "ai_code_revert_change",
                        [[this.state.activeId], change.id]);
                    this.notification.add(_t("Step reverted."), {type: "success"});
                    this.codeState.selectedChange = null;
                    await this.aicRefreshTask();
                } catch (e) {
                    this.notification.add(e.data?.message || String(e), {type: "danger"});
                }
            },
            cancel: () => {},
        });
    }
}

registry.category("actions").add("ai_code.workspace", AiCodeWorkspace);
