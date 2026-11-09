import { Composer } from "@mail/core/common/composer";
import { patch } from "@web/core/utils/patch";
import { useService } from "@web/core/utils/hooks";
import { useState, useEffect, onWillStart, onWillUpdateProps } from "@odoo/owl";

patch(Composer.prototype, {
    setup() {
        super.setup();

        this.orm = useService("orm");

        this.agentState = useState({
            currentAgent: null,
            sessionUsage: null,
        });

        this.lastFetchedAgentId = null;

        onWillStart(async () => {
            await this.fetchAgentViaOrm();
        });

        onWillUpdateProps(async (nextProps) => {
            await this.fetchAgentViaOrm(nextProps);
        });

        // Refresh the session token counter whenever a message lands
        useEffect(
            () => {
                this.fetchSessionUsage();
            },
            () => [
                this.props.composer?.thread?.id,
                this.props.composer?.thread?.messages?.length,
            ]
        );
    },

    async fetchSessionUsage() {
        const thread = this.props.composer?.thread;
        if (!thread || thread.channel_type !== "ai_chat" || !thread.id || thread.id < 1) {
            this.agentState.sessionUsage = null;
            return;
        }
        try {
            this.agentState.sessionUsage = await this.orm.call(
                "discuss.channel", "ai_get_session_usage", [[thread.id]]
            );
        } catch {
            this.agentState.sessionUsage = null;
        }
    },

    get sessionUsageLabel() {
        const usage = this.agentState.sessionUsage;
        if (!usage || !usage.turns) {
            return "";
        }
        const total = usage.total_tokens || 0;
        const tokens = total >= 1000 ? (total / 1000).toFixed(1) + "k" : String(total);
        let label = tokens + " tok";
        if (usage.cost) {
            label += " · $" + usage.cost.toFixed(4);
        }
        return label;
    },

    /**
     * 纯净的 ORM 查询方法
     */
    async fetchAgentViaOrm(nextProps) {
        const composer = nextProps?.composer || this.props.composer;
        const thread = composer?.thread;

        if (thread && thread.channel_type === 'ai_chat' && thread.ai_en_agent_id) {

            // 兼容处理 Odoo 各种奇葩的 Many2one 格式（防止传过来的是数组 [id, name] 或代理对象）
            let currentId = 0;
            if (Array.isArray(thread.ai_en_agent_id)) {
                currentId = thread.ai_en_agent_id[0];
            } else if (typeof thread.ai_en_agent_id === 'object') {
                currentId = thread.ai_en_agent_id.id;
            } else {
                currentId = Number(thread.ai_en_agent_id);
            }

            if (currentId && currentId === this.lastFetchedAgentId) {
                return;
            }

            this.lastFetchedAgentId = currentId;

            try {
                const [agentData] = await this.orm.searchRead(
                    "ai.agent.bot",
                    [["id", "=", currentId]],
                    ["id", "name", "llm_model"]
                );

                if (agentData) {
                    this.agentState.currentAgent = agentData;
                }
            } catch (error) {
                console.error("Composer orm search Agent failed:", error);
                this.agentState.currentAgent = null;
            }
        } else {
            this.agentState.currentAgent = null;
            this.lastFetchedAgentId = null;
        }
    },
    get currentAgentInfo() {
        return this.agentState.currentAgent;
    },

    /** The AI agent member currently typing (i.e. a generation is running). */
    get aiTypingMember() {
        const thread = this.props.composer?.thread;
        if (!thread || thread.channel_type !== "ai_chat") {
            return null;
        }
        return thread.channel_member_ids?.find(
            (member) => member.partner_id?.im_status === "agent" && member.isTyping
        );
    },

    get isAiGenerating() {
        return Boolean(this.aiTypingMember);
    },

    /** Stop the in-progress AI generation for this channel. */
    async stopAiGeneration() {
        const thread = this.props.composer?.thread;
        if (!thread) {
            return;
        }
        const member = this.aiTypingMember;
        try {
            await this.orm.call("discuss.channel", "ai_request_stop", [[thread.id]]);
        } catch (error) {
            console.error("AI stop request failed:", error);
            return;
        }
        // instant feedback; the server also sends typing=false when the
        // loop actually aborts
        if (member) {
            member.isTyping = false;
        }
    },
});