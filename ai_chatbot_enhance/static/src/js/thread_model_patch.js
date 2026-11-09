/** @odoo-module **/
import { Thread } from "@mail/core/common/thread_model";
import { patch } from "@web/core/utils/patch";
import { rpc } from "@web/core/network/rpc";

patch(Thread.prototype, {
    /**
     * 当用户发送消息后，如果当前频道是 AI 频道，则触发后端调用 DeepSeek
     */
    async post(body, postData = {}, extraData = {}) {
        const message = await super.post(body, postData, extraData);

        const aiMember = this.channel_member_ids?.find(
            (member) => member.partner_id?.im_status == "agent"
        );
        // message could be undefined if it is a command, for example /help.
        if (message?.thread?.ai_en_agent_id) {
            // The rpc only STARTS the generation (background job) and returns
            // immediately; the response arrives later through the bus. The
            // typing indicator is driven by the server (typing_status events)
            // — set it locally for instant feedback, clear it only on error.
            if (aiMember) {
                aiMember.isTyping = true;
            }
            try {
                await rpc("/ai/get_ai_response", {
                    mail_message_id: message.id,
                    channel_id: this.id,
                    // current_view_info: await getCurrentViewInfo(this.store.env.bus),
                    // ai_session_identifier: session.ai_session_identifier,
                });
            } catch (e) {
                if (aiMember) {
                    aiMember.isTyping = false;
                }
                throw e;
            }
        }
        return message;
    },
});