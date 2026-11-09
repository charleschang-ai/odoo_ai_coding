import {Component, markup, onWillStart, useExternalListener, useState} from "@odoo/owl";
import {registry} from "@web/core/registry";
import {useService} from "@web/core/utils/hooks";
import {user} from "@web/core/user";
import {browser} from "@web/core/browser/browser";
import {_t} from "@web/core/l10n/translation";
import {ConfirmationDialog} from "@web/core/confirmation_dialog/confirmation_dialog";
import {rpc} from "@web/core/network/rpc";

import {AiPlatformSidebar} from "./sidebar";
import {AiPlatformThread} from "./thread";
import {AiPlatformComposer} from "./composer";

const isMobile = () => window.innerWidth <= 768;

/**
 * AI Platform root component.
 *
 * Data layer is plain discuss: a conversation is a discuss.channel
 * (channel_type='ai_chat'), messages go through the mail.store Thread
 * record. The ai_chatbot_enhance patch on Thread.post triggers
 * /ai/get_ai_response after each user message; the AI answer is pushed
 * back over the bus and this UI re-renders reactively.
 */
export class AiPlatform extends Component {
    static template = "ai_chat_platform.AiPlatform";
    static components = {AiPlatformSidebar, AiPlatformThread, AiPlatformComposer};
    static props = {"*": true};

    setup() {
        this.store = useState(useService("mail.store"));
        this.orm = useService("orm");
        this.dialog = useService("dialog");
        this.notification = useService("notification");
        this.state = useState({
            conversations: [],
            agents: [],
            suggestions: [],
            agentId: false, // default agent, resolved server-side (no picker for now)
            activeId: false,
            collapsed: isMobile(),
            busy: false,
            loadingThread: false,
            usage: null,
            userName: "",
            agentMenuOpen: false,
            preview: null, // { id, name, mimetype } of the attachment shown in the drawer
            canViewAll: false, // Settings admin: may list every user's conversations
            showAll: false, // admin toggle "All users" (persisted per user)
        });
        useExternalListener(document, "click", (ev) => {
            this.state.agentMenuOpen = false;
            if (
                this.state.preview &&
                !ev.target.closest(".o_aip_drawer") &&
                !ev.target.closest(".o_aip_att")
            ) {
                this.closePreview();
            }

        });
        useExternalListener(document, "keydown", (ev) => {
            if (ev.key === "Escape" && this.state.preview) {
                this.closePreview();
            }
        });
        onWillStart(async () => {
            const data = await this.orm.call("discuss.channel", "ai_platform_get_data", []);
            Object.assign(this.state, {
                conversations: data.conversations,
                agents: data.agents,
                suggestions: data.suggestions,
                agentId: data.default_agent_id,
                userName: data.user_name,
            });
            await this.restoreShowAll(data.can_view_all);
            const saved = parseInt(browser.localStorage.getItem(this.lastConvKey), 10);
            if (saved && this.state.conversations.some((c) => c.id === saved)) {
                await this.openConversation(saved);
            }
        });
    }

    get lastConvKey() {
        return `ai_chat_platform.last_conv.${user.userId}`;
    }

    get showAllKey() {
        return `ai_chat_platform.show_all.${user.userId}`;
    }

    /**
     * Admin "All users" switch: apply the server-side permission and the
     * remembered choice, reloading the list when the switch is on.
     */
    async restoreShowAll(canViewAll) {
        this.state.canViewAll = !!canViewAll;
        this.state.showAll =
            this.state.canViewAll && browser.localStorage.getItem(this.showAllKey) === "1";
        if (this.state.showAll) {
            await this.refreshConversations();
        }
    }

    async toggleShowAll() {
        if (!this.state.canViewAll) {
            return;
        }
        this.state.showAll = !this.state.showAll;
        browser.localStorage.setItem(this.showAllKey, this.state.showAll ? "1" : "0");
        await this.refreshConversations();
        // a foreign conversation disappears from the list when the switch goes off
        if (this.state.activeId && !this.activeConversation) {
            this.newChat();
        }
    }

    /** The open conversation belongs to another user (admin read-only view). */
    get viewingOthers() {
        const conv = this.activeConversation;
        return !!conv && conv.is_mine === false;
    }

    get thread() {
        return this.state.activeId
            ? this.store.Thread.get({model: "discuss.channel", id: this.state.activeId})
            : undefined;
    }

    get activeConversation() {
        return this.state.conversations.find((c) => c.id === this.state.activeId);
    }

    get headerTitle() {
        return this.activeConversation?.name || _t("New conversation");
    }

    /**
     * Agent shown in the composer pill: the conversation's bound agent
     * when a conversation is open, otherwise the agent selected for the
     * next new chat.
     */
    get currentAgent() {
        const conv = this.activeConversation;
        if (conv) {
            const known = this.state.agents.find((a) => a.id === conv.agent_id);
            return known || {
                id: conv.agent_id,
                name: conv.agent_name,
                model: conv.agent_model || "",
                subtitle: "",
            };
        }
        return this.state.agents.find((a) => a.id === this.state.agentId) || null;
    }

    get activeAgentName() {
        return this.currentAgent?.name || _t("AI Assistant");
    }

    /**
     * Pick an agent. The model is bound to the agent (agent.llm_model is
     * what the backend calls), so choosing an agent chooses the model.
     * A conversation is permanently tied to its agent — picking a
     * different one while a conversation is open starts a new chat.
     */
    pickAgent(agentId) {
        this.state.agentMenuOpen = false;
        this.state.agentId = agentId;
        if (this.state.activeId && this.activeConversation?.agent_id !== agentId) {
            this.newChat();
        }
    }

    toggleAgentMenu(ev) {
        ev.stopPropagation();
        this.state.agentMenuOpen = !this.state.agentMenuOpen;
    }

    agentAvatarUrl(agent) {
        return `/web/image?model=ai.agent.bot&id=${agent.id}&field=avatar_128`;
    }

    // ------------------------------------------------------------------
    // Attachment preview drawer
    // ------------------------------------------------------------------
    openPreview(att) {
        this.state.preview = {id: att.id, name: att.name, mimetype: att.mimetype || ""};
    }

    closePreview() {
        this.state.preview = null;
    }

    get previewIsPdf() {
        return (this.state.preview?.mimetype || "").includes("pdf");
    }

    get previewIsImage() {
        return (this.state.preview?.mimetype || "").startsWith("image");
    }

    get previewInlineUrl() {
        return this.state.preview ? `/web/content/${this.state.preview.id}` : "";
    }

    get previewDownloadUrl() {
        return this.state.preview ? `/web/content/${this.state.preview.id}?download=true` : "";
    }

    get previewIcon() {
        if (this.previewIsPdf) {
            return "fa-file-pdf-o";
        }
        if (this.previewIsImage) {
            return "fa-file-image-o";
        }
        return "fa-file-o";
    }

    toggleSidebar() {
        this.state.collapsed = !this.state.collapsed;
    }

    // ------------------------------------------------------------------
    // Conversation list
    // ------------------------------------------------------------------
    async refreshConversations() {
        this.state.conversations = await this.orm.call(
            "discuss.channel", "ai_platform_get_conversations", [this.state.showAll]);
    }

    async openConversation(id) {
        this.state.activeId = id;
        this.state.usage = null;
        browser.localStorage.setItem(this.lastConvKey, id);
        if (isMobile()) {
            this.state.collapsed = true;
        }
        this.state.loadingThread = true;
        try {
            const thread = await this.store.Thread.getOrFetch({model: "discuss.channel", id});
            if (!thread) {
                this.newChat();
                await this.refreshConversations();
                return;
            }
            await thread.fetchNewMessages();
        } finally {
            this.state.loadingThread = false;
        }
        this.refreshUsage();
    }

    newChat() {
        this.state.activeId = false;
        this.state.usage = null;
        browser.localStorage.removeItem(this.lastConvKey);
        if (isMobile()) {
            this.state.collapsed = true;
        }
    }

    /** Lazily create the channel on the first message/attachment. */
    async ensureConversation(title) {
        if (this.state.activeId && this.thread) {
            return this.thread;
        }
        if (!this.state.agentId) {
            this.notification.add(
                _t("No AI agent is configured. Please contact your administrator."),
                {type: "warning"});
            throw new Error("No AI agent available");
        }
        const {channel_id, store_data} = await this.orm.call(
            "discuss.channel", "ai_platform_new_conversation",
            [this.state.agentId, title || false]);
        this.store.insert(store_data);
        this.state.activeId = channel_id;
        browser.localStorage.setItem(this.lastConvKey, channel_id);
        this.refreshConversations();
        return this.store.Thread.getOrFetch({model: "discuss.channel", id: channel_id});
    }

    async renameConversation(conv, name) {
        const finalName = await this.orm.call(
            "discuss.channel", "ai_platform_rename", [[conv.id], name]);
        if (finalName) {
            conv.name = finalName;
            const thread = this.store.Thread.get({model: "discuss.channel", id: conv.id});
            if (thread) {
                thread.name = finalName;
            }
        }
    }

    deleteConversation(conv) {
        this.dialog.add(ConfirmationDialog, {
            title: _t("Delete conversation"),
            body: _t('Delete "%s"? This cannot be undone.', conv.name),
            confirmLabel: _t("Delete"),
            cancelLabel: _t("Cancel"),
            confirm: async () => {
                await this.orm.call("discuss.channel", "ai_platform_delete", [[conv.id]]);
                if (this.state.activeId === conv.id) {
                    this.newChat();
                }
                await this.refreshConversations();
            },
            cancel: () => {
            },
        });
    }

    // ------------------------------------------------------------------
    // Send / regenerate / quick options
    // ------------------------------------------------------------------
    /**
     * @param {string} text
     * @returns {Promise<boolean>} whether a message was actually sent
     */
    async sendMessage(text) {
        if (this.state.busy) {
            return false;
        }
        if (this.viewingOthers) {
            this.notification.add(
                _t("This conversation belongs to another user and is read-only."),
                {type: "warning"});
            return false;
        }
        const body = (text || "").trim();
        const pendingAttachments = this.thread?.composer?.attachments?.length || 0;
        if (!body && !pendingAttachments) {
            return false;
        }
        this.state.busy = true;
        try {
            const thread = await this.ensureConversation(body ? body.slice(0, 40) : undefined);
            const attachments = [...(thread.composer?.attachments || [])];
            // ai_chatbot_enhance patch: post() internally awaits
            // /ai/get_ai_response — when it resolves the AI answer exists
            await thread.post(body || _t("Please analyze the attached file(s)"), {attachments});
            thread.composer?.clear();
        } catch (e) {
            this.notification.add(_t("Failed to send: %s", e.message || e), {type: "danger"});
            return false;
        } finally {
            this.state.busy = false;
        }
        this.refreshUsage();
        this.refreshConversations();
        return true;
    }

    /** Regenerate: re-run the AI on the last user message (answer is appended). */
    async regenerate() {
        const thread = this.thread;
        if (!thread || this.state.busy || this.viewingOthers) {
            return;
        }
        const lastUserMessage = [...thread.messages]
            .reverse()
            .find((m) => m.message_type === "comment" && m.isSelfAuthored);
        if (!lastUserMessage) {
            return;
        }
        this.state.busy = true;
        try {
            await rpc("/ai/get_ai_response", {
                mail_message_id: lastUserMessage.id,
                channel_id: thread.id,
            });
        } finally {
            this.state.busy = false;
        }
        this.refreshUsage();
    }

    /** A quick-reply option (o_ai_option_data) of an AI message was clicked. */
    async onQuickOption(text, messageId) {
        const channelId = this.state.activeId;
        // Same strategy as ai_chatbot_enhance: persist the choice in
        // parallel, then post the option text as the user's message
        if (channelId && messageId) {
            this.orm
                .call("discuss.channel", "ai_set_option_choice", [[channelId], messageId, text])
                .then((newBody) => {
                    if (newBody) {
                        const msg = this.store["mail.message"].get(messageId);
                        if (msg) {
                            msg.body = markup(newBody);
                        }
                    }
                })
                .catch(() => {
                });
        }
        await this.sendMessage(text);
    }

    // ------------------------------------------------------------------
    // Usage & export
    // ------------------------------------------------------------------
    async refreshUsage() {
        if (!this.state.activeId) {
            this.state.usage = null;
            return;
        }
        try {
            const usage = await this.orm.call(
                "discuss.channel", "ai_get_session_usage", [[this.state.activeId]]);
            this.state.usage = usage && usage.turns ? usage : null;
        } catch {
            this.state.usage = null;
        }
    }

    exportConversation() {
        const thread = this.thread;
        if (!thread) {
            this.notification.add(_t("No active conversation to export."), {type: "warning"});
            return;
        }
        const lines = [];
        for (const m of thread.messages.filter((m) => m.message_type === "comment")) {
            const div = document.createElement("div");
            div.innerHTML = m.body?.toString() || "";
            const who = m.isSelfAuthored ? _t("Me") : (m.author?.name || _t("AI Assistant"));
            lines.push(`[${who}]\n${div.innerText.trim()}\n`);
        }
        const blob = new Blob([lines.join("\n")], {type: "text/plain;charset=utf-8"});
        const link = document.createElement("a");
        link.href = URL.createObjectURL(blob);
        link.download = `${thread.name || _t("AI conversation")}.txt`;
        link.click();
        URL.revokeObjectURL(link.href);
    }
}

registry.category("actions").add("ai_chat_platform.platform", AiPlatform);
