import { Thread } from "@mail/core/common/thread";
import { _t } from "@web/core/l10n/translation";
import { patch } from "@web/core/utils/patch";

patch(Thread.prototype, {
    get showStartMessage() {
        return super.showStartMessage || (this.props.thread.channel_type === "ai_chat");
    },
    get startMessageSubtitle() {
        if (this.props.thread.channel_type === "ai_chat") {
            return _t("Hello, Feel free to ask your AI assistant?");
        } else {
            return super.startMessageSubtitle;
        }
    },
    onClickPromptButton(button) {
        this.props.thread.post(button);
    },
    get showPromptButtons() {
        return this.props.thread.messages.length === 0;
    },
});
