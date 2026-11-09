import { Thread } from "@mail/core/common/thread_model";
import { patch } from "@web/core/utils/patch";
import { fields } from "@mail/core/common/record";
import { browser } from "@web/core/browser/browser";

const AI_EN_PROMPT_BUTTONS = "ai.thread.prompt_buttons.";

patch(Thread.prototype, {
    setup() {
        super.setup();
        this.ai_en_prompt_buttons = fields.Many("ai.en.prompt.button", {
            inverse: "thread_id",
            compute() {
                return JSON.parse(browser.localStorage.getItem(AI_EN_PROMPT_BUTTONS.concat(this.id)));
            },
        });
    },
    async closeChatWindow(options = {}) {
        await super.closeChatWindow(options);
        browser.localStorage.removeItem(AI_EN_PROMPT_BUTTONS.concat(this.id));
    },
    get avatarUrl() {
        if (this.channel_type === "ai_chat" && this.correspondent) {
            return this.correspondent.avatarUrl;
        }

        return super.avatarUrl;
    },
    computeCorrespondent() {
        const correspondent = super.computeCorrespondent();
        if (
            ["ai_composer", "ai_chat"].includes(this.channel_type) &&
            correspondent?.persona?.eq(this.store.self)
        ) {
            return undefined;
        }
        return correspondent;
    },
    _computeDiscussAppCategory() {
        if (this.parent_channel_id) {
            return;
        }
        if (["group", "chat", "ai_chat"].includes(this.channel_type)) {
            return this.store.discuss.chats;
        }
        if (this.channel_type === "channel") {
            return this.store.discuss.channels;
        }
    },
});
