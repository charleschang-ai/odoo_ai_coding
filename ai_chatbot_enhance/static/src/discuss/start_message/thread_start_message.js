/** @odoo-module **/

import {Thread} from "@mail/core/common/thread";
import {patch} from "@web/core/utils/patch";
import {_t} from "@web/core/l10n/translation";
import {useState, useEffect} from "@odoo/owl";
import {useService} from "@web/core/utils/hooks";

patch(Thread.prototype, {
    // 是否显示开始消息（欢迎区域）
    setup() {
        super.setup();
        this.aiState = useState({
            promptButtons: [],
        });
        this.orm = useService("orm");
        this.onClickPromptButton = this.onClickPromptButton.bind(this);


        const loadPromptButtons = async () => {
            const thread = this.props.thread;
            if (thread.channel_type === "ai_chat" && thread.ai_en_agent_id) {
                try {
                    const prompts = await this.orm.call(
                        "ai.en.composer",
                        "get_prompts_by_agent",
                        [thread.ai_en_agent_id]
                    );
                    this.aiState.promptButtons = prompts || [];
                } catch (error) {
                    console.error("Failed to load prompts:", error);
                    this.aiState.promptButtons = [];
                }
            } else {
                this.aiState.promptButtons = [];
            }
        };

        loadPromptButtons();
        useEffect(() => {
            loadPromptButtons();
        }, () => [this.props.thread.id, this.props.thread.ai_en_agent_id]);
    },


    get showStartMessage() {
        return this.props.thread.channel_type === "ai_chat";
    },

    get startMessageSubtitle() {
        if (this.props.thread.channel_type === "ai_chat") {
            return _t("Hello, I'm an AI assistant. How can I help you?");
        }
        return "";
    },

    get startMessageTitle() {
        return this.props.thread.displayName || _t("AI Chat");
    },

    get promptButtons() {
        return this.aiState?.promptButtons || [];
    },

    get showPromptButtons() {
        return this.aiState?.promptButtons?.length > 0;
    },

onClickPromptButton(button) {
    const messageText = String(button.text); // 强制转换
    this.props.thread.post({ body: messageText });
}
});