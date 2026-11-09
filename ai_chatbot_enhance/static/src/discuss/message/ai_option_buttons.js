/** @odoo-module **/

import { markup } from "@odoo/owl";
import { Thread } from "@mail/core/common/thread";
import { patch } from "@web/core/utils/patch";

// Local per-session record of clicked options (messageId -> text), so the
// footnote appears instantly without waiting for the body round-trip
const chosenByMessageId = new Map();
// Form input values (messageId -> {label: value}): the thread re-renders on
// every store update, which recreates the DOM inputs — values must live here,
// not in the DOM, or they get wiped while the user is typing
const formValuesByMessageId = new Map();

const CONFIRM_RE = /^(✅|✔|确认|确定|是的|好的|同意|执行|confirm|yes|ok)/i;
const CANCEL_RE = /^(❌|✖|取消|放弃|不用|否|cancel|no)/i;
const MODIFY_RE = /^(✏|✎|📝|修改|调整|更改|edit|modify|change)/i;

/**
 * Quick-reply option cards.
 *
 * The AI backend appends a hidden `<div class="o_ai_option_data">[...json...]</div>`
 * node to the message body when the agent offers choices (missing parameter,
 * ambiguous match, mutation confirmation). We render the options of the NEWEST
 * message as clickable cards at the bottom of the thread; clicking posts the
 * option text as the user's message and persists the choice as a small
 * footnote on the AI message ("Selected: ...").
 */
patch(Thread.prototype, {
    /**
     * The AI message whose options/form are still actionable: the newest
     * message carrying option/form data SINCE the user's last message.
     * (One AI turn may span several messages — the marker is not always in
     * the last one, so scanning only newestPersistentMessage loses buttons.)
     */
    _aiActionableMessage() {
        const thread = this.props.thread;
        if (!thread || thread.channel_type !== "ai_chat") {
            return null;
        }
        const messages = thread.messages || [];
        for (let i = messages.length - 1; i >= 0; i--) {
            const msg = messages[i];
            if (!msg) {
                continue;
            }
            if (msg.isSelfAuthored) {
                // the user already replied: anything above is stale
                return null;
            }
            const body = msg.body?.toString() || "";
            if (body.includes("o_ai_option_data") || body.includes("o_ai_form_data")) {
                if (chosenByMessageId.has(msg.id) || body.includes("o_ai_option_chosen")) {
                    return null;
                }
                return msg;
            }
        }
        return null;
    },

    get aiQuickOptions() {
        const message = this._aiActionableMessage();
        const body = message?.body?.toString() || "";
        if (!body.includes("o_ai_option_data")) {
            return [];
        }
        try {
            const doc = new DOMParser().parseFromString(body, "text/html");
            const el = doc.querySelector(".o_ai_option_data");
            if (!el) {
                return [];
            }
            const data = JSON.parse(el.textContent);
            if (!Array.isArray(data)) {
                return [];
            }
            return data
                .filter((option) => typeof option === "string" && option.trim())
                .slice(0, 6)
                .map((text) => ({
                    text,
                    kind: CONFIRM_RE.test(text.trim())
                        ? "confirm"
                        : CANCEL_RE.test(text.trim())
                        ? "cancel"
                        : MODIFY_RE.test(text.trim())
                        ? "modify"
                        : "normal",
                }));
        } catch {
            return [];
        }
    },

    get aiQuickForm() {
        const message = this._aiActionableMessage();
        const body = message?.body?.toString() || "";
        if (!body.includes("o_ai_form_data")) {
            return null;
        }
        try {
            const doc = new DOMParser().parseFromString(body, "text/html");
            const el = doc.querySelector(".o_ai_form_data");
            if (!el) {
                return null;
            }
            const data = JSON.parse(el.textContent);
            if (!data || !Array.isArray(data.fields) || !data.fields.length) {
                return null;
            }
            return data;
        } catch {
            return null;
        }
    },

    /** Chosen-option footnote text for a given message (empty when none). */
    aiChosenNote(msg) {
        if (this.props.thread?.channel_type !== "ai_chat") {
            return "";
        }
        if (msg && chosenByMessageId.has(msg.id)) {
            return chosenByMessageId.get(msg.id);
        }
        const body = msg?.body?.toString() || "";
        if (!body.includes("o_ai_option_chosen")) {
            return "";
        }
        try {
            const doc = new DOMParser().parseFromString(body, "text/html");
            return doc.querySelector(".o_ai_option_chosen")?.textContent?.trim() || "";
        } catch {
            return "";
        }
    },

    async onClickAiQuickOption(option) {
        const text = String(option.text || option);
        await this._aiPostAndRemember(text);
    },

    /** Keep typed values across re-renders (the DOM inputs are recreated). */
    onAiFormInput(ev) {
        const message = this._aiActionableMessage();
        const label = ev.target.dataset.aiFormLabel;
        if (!message || !label) {
            return;
        }
        let values = formValuesByMessageId.get(message.id);
        if (!values) {
            values = {};
            formValuesByMessageId.set(message.id, values);
        }
        values[label] = ev.target.value;
    },

    /** Current value of a form field: typed value first, then the AI default. */
    aiFormValue(field) {
        const message = this._aiActionableMessage();
        const values = message && formValuesByMessageId.get(message.id);
        if (values && field.label in values) {
            return values[field.label];
        }
        return field.value || "";
    },

    async onSubmitAiForm() {
        const form = this.aiQuickForm;
        if (!form) {
            return;
        }
        const parts = [];
        for (const field of form.fields) {
            const value = String(this.aiFormValue(field) || "").trim();
            if (value) {
                parts.push(`${field.label}: ${value}`);
            }
        }
        if (!parts.length) {
            return; // nothing filled at all
        }
        await this._aiPostAndRemember(parts.join("; "));
    },

    async _aiPostAndRemember(text) {
        const thread = this.props.thread;
        const message = this._aiActionableMessage();
        thread.post(text);
        if (!message) {
            return;
        }
        // Instant footnote + hide the buttons for this message
        chosenByMessageId.set(message.id, text);
        // Persist the choice server-side (footnote survives reload)
        try {
            const newBody = await this.env.services.orm.call(
                "discuss.channel",
                "ai_set_option_choice",
                [[thread.id], message.id, text]
            );
            if (newBody) {
                // markup() is critical: assigning a plain string would make
                // OWL escape the whole body (tags rendered as literal text)
                message.body = markup(newBody);
            }
        } catch {
            // cosmetic only: never block the conversation
        }
    },
});
