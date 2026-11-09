import { rpc } from "@web/core/network/rpc";
import { patch } from "@web/core/utils/patch";
import { ChatWindow } from "@mail/core/common/chat_window_model";

patch(ChatWindow.prototype, {
    computeCanShow() {
        if (this.store.aiInsertButtonTarget && this.store.discuss.isActive) {
            return this.thread?.channel_type === "ai_chat";
        }
        return super.computeCanShow();
    },
    async _onClose() {
        const thread = this.thread;
        if (thread?.ai_en_agent_id) {
            await rpc("/ai/close_chat_ai", { channel_id: thread.id });
        }
        await super._onClose(...arguments);
    },
    get avatarUrl() {
        if (this.channel_type === "ai_chat" && this.correspondent) {
            return this.correspondent.avatarUrl;
        }

        return super.avatarUrl;
    },

});
