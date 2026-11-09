import { Component, useEffect, useRef, useState } from "@odoo/owl";
import { _t } from "@web/core/l10n/translation";
import { browser } from "@web/core/browser/browser";

const { DateTime } = luxon;

const CONFIRM_RE = /^(✅|✔|确认|确定|是的|好的|同意|执行|confirm|yes|ok)/i;
const CANCEL_RE = /^(❌|✖|取消|放弃|不用|否|cancel|no)/i;

/**
 * Message area: user/assistant bubbles with day separators, empty-state
 * suggestion chips, AI quick-reply options & forms, typing indicator,
 * scroll-to-bottom helper.
 *
 * AI answers arrive as server-rendered safe HTML (markdown converted in
 * the backend) and are rendered with t-out; hidden o_ai_option_data /
 * o_ai_form_data JSON nodes are parsed into clickable cards, matching the
 * behavior of the ai_chatbot_enhance chat windows.
 */
export class AiPlatformThread extends Component {
    static template = "ai_chat_platform.Thread";
    static props = {
        thread: { optional: true },
        busy: Boolean,
        loading: Boolean,
        suggestions: Array,
        agentName: String,
        onSuggestion: Function,
        onRegenerate: Function,
        onOption: Function,
        onPreview: Function,
    };

    setup() {
        this.scrollerRef = useRef("scroller");
        // options already clicked in this session (message id -> text)
        this.chosen = useState(new Map());
        this.state = useState({ copiedId: false, formValues: {}, showJump: false });
        useEffect(
            () => {
                const el = this.scrollerRef.el;
                if (el) {
                    el.scrollTop = el.scrollHeight;
                }
            },
            () => [this.messages.length, this.props.busy, this.props.thread?.id],
        );
    }

    get messages() {
        const thread = this.props.thread;
        if (!thread) {
            return [];
        }
        return thread.messages.filter((m) => m.message_type === "comment");
    }

    /** Messages annotated with day-separator flags. */
    get timeline() {
        const items = [];
        let lastDay = null;
        for (const msg of this.messages) {
            const day = msg.datetime?.toISODate?.() || null;
            if (day && day !== lastDay) {
                lastDay = day;
                items.push({ type: "day", key: "day-" + day, label: this._dayLabel(msg.datetime) });
            }
            items.push({ type: "msg", key: "msg-" + msg.id, msg });
        }
        return items;
    }

    _dayLabel(dt) {
        const now = DateTime.local();
        if (dt.hasSame(now, "day")) {
            return _t("Today");
        }
        if (dt.hasSame(now.minus({ days: 1 }), "day")) {
            return _t("Yesterday");
        }
        if (dt.hasSame(now, "year")) {
            return dt.toFormat("MMM dd");
        }
        return dt.toFormat("yyyy-MM-dd");
    }

    msgTime(msg) {
        return msg.datetime?.toFormat?.("HH:mm") || "";
    }

    authorName(msg) {
        return msg.isSelfAuthored ? _t("Me") : (msg.author?.name || this.props.agentName);
    }

    isLastAssistant(msg) {
        const assistants = this.messages.filter((m) => !m.isSelfAuthored);
        return assistants.length > 0 && assistants[assistants.length - 1].id === msg.id;
    }

    attachmentUrl(att) {
        return `/web/content/${att.id}?download=true`;
    }

    isAudio(att) {
        return (att.mimetype || "").startsWith("audio");
    }

    audioUrl(att) {
        return `/web/content/${att.id}`;
    }

    // ------------------------------------------------------------------
    // Scroll helpers
    // ------------------------------------------------------------------
    onScroll() {
        const el = this.scrollerRef.el;
        if (!el) {
            return;
        }
        this.state.showJump = el.scrollHeight - el.scrollTop - el.clientHeight > 300;
    }

    jumpToBottom() {
        const el = this.scrollerRef.el;
        if (el) {
            el.scrollTo({ top: el.scrollHeight, behavior: "smooth" });
        }
    }

    // ------------------------------------------------------------------
    // Copy
    // ------------------------------------------------------------------
    async copyMessage(msg) {
        const div = document.createElement("div");
        div.innerHTML = msg.body?.toString() || "";
        const text = div.innerText.trim();
        try {
            await navigator.clipboard.writeText(text);
        } catch {
            const ta = document.createElement("textarea");
            ta.value = text;
            ta.style.cssText = "position:absolute;left:-9999px";
            document.body.appendChild(ta);
            ta.select();
            document.execCommand("copy");
            ta.remove();
        }
        this.state.copiedId = msg.id;
        browser.setTimeout(() => {
            if (this.state.copiedId === msg.id) {
                this.state.copiedId = false;
            }
        }, 1500);
    }

    // ------------------------------------------------------------------
    // AI quick options / forms (same logic as ai_chatbot_enhance)
    // ------------------------------------------------------------------
    /** Newest un-answered AI message carrying option/form markers. */
    get actionableMessage() {
        const msgs = this.messages;
        for (let i = msgs.length - 1; i >= 0; i--) {
            const msg = msgs[i];
            if (msg.isSelfAuthored) {
                return null;
            }
            const body = msg.body?.toString() || "";
            if (body.includes("o_ai_option_data") || body.includes("o_ai_form_data")) {
                if (body.includes("o_ai_option_chosen") || this.chosen.has(msg.id)) {
                    return null;
                }
                return msg;
            }
        }
        return null;
    }

    _parseHiddenJson(message, selector) {
        const body = message?.body?.toString() || "";
        try {
            const doc = new DOMParser().parseFromString(body, "text/html");
            const el = doc.querySelector(selector);
            return el ? JSON.parse(el.textContent) : null;
        } catch {
            return null;
        }
    }

    get quickOptions() {
        const message = this.actionableMessage;
        if (!message) {
            return [];
        }
        const data = this._parseHiddenJson(message, ".o_ai_option_data");
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
                    : "normal",
            }));
    }

    get quickForm() {
        const message = this.actionableMessage;
        if (!message) {
            return null;
        }
        const data = this._parseHiddenJson(message, ".o_ai_form_data");
        if (!data || !Array.isArray(data.fields) || !data.fields.length) {
            return null;
        }
        return data;
    }

    onOptionClick(option) {
        const message = this.actionableMessage;
        if (message) {
            this.chosen.set(message.id, option.text);
        }
        this.props.onOption(option.text, message?.id);
    }

    formKey(field) {
        return `${this.actionableMessage?.id || 0}:${field.label}`;
    }

    formValue(field) {
        const key = this.formKey(field);
        return key in this.state.formValues ? this.state.formValues[key] : (field.value || "");
    }

    onFormInput(field, ev) {
        this.state.formValues[this.formKey(field)] = ev.target.value;
    }

    submitForm() {
        const form = this.quickForm;
        const message = this.actionableMessage;
        if (!form) {
            return;
        }
        const parts = [];
        for (const field of form.fields) {
            const value = String(this.formValue(field) || "").trim();
            if (value) {
                parts.push(`${field.label}: ${value}`);
            }
        }
        if (!parts.length) {
            return;
        }
        if (message) {
            this.chosen.set(message.id, parts.join("; "));
        }
        this.props.onOption(parts.join("; "), message?.id);
    }
}
