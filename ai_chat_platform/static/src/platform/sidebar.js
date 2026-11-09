import { Component, useEffect, useRef, useState } from "@odoo/owl";
import { _t } from "@web/core/l10n/translation";

const { DateTime } = luxon;

/**
 * Left sidebar: brand, new chat, search, conversation history grouped by
 * date (Today / Yesterday / Previous 7 days / ...), user bar.
 */
export class AiPlatformSidebar extends Component {
    static template = "ai_chat_platform.Sidebar";
    static props = {
        conversations: Array,
        activeId: [Number, Boolean],
        userName: String,
        canViewAll: { type: Boolean, optional: true },
        showAll: { type: Boolean, optional: true },
        onToggleShowAll: { type: Function, optional: true },
        onSelect: Function,
        onNew: Function,
        onRename: Function,
        onDelete: Function,
    };

    setup() {
        this.state = useState({ renamingId: false, renameText: "", search: "" });
        this.renameInputRef = useRef("renameInput");
        useEffect(
            () => {
                this.renameInputRef.el?.focus();
                this.renameInputRef.el?.select();
            },
            () => [this.state.renamingId],
        );
    }

    /** Conversations filtered by the search box, grouped by recency. */
    get groups() {
        const term = this.state.search.trim().toLowerCase();
        const convs = term
            ? this.props.conversations.filter((c) => (c.name || "").toLowerCase().includes(term))
            : this.props.conversations;
        const now = DateTime.local();
        const today = now.startOf("day");
        const buckets = [
            { label: _t("Today"), items: [] },
            { label: _t("Yesterday"), items: [] },
            { label: _t("Previous 7 days"), items: [] },
            { label: _t("Previous 30 days"), items: [] },
            { label: _t("Older"), items: [] },
        ];
        for (const conv of convs) {
            const dt = this._parseDt(conv.last_interest_dt);
            let idx = 4;
            if (dt) {
                const days = today.diff(dt.startOf("day"), "days").days;
                idx = days <= 0 ? 0 : days === 1 ? 1 : days <= 7 ? 2 : days <= 30 ? 3 : 4;
            }
            buckets[idx].items.push(conv);
        }
        return buckets.filter((b) => b.items.length);
    }

    _parseDt(value) {
        if (!value) {
            return null;
        }
        const dt = DateTime.fromSQL(value, { zone: "utc" }).toLocal();
        return dt.isValid ? dt : null;
    }

    /** Short timestamp for a conversation row. */
    stamp(conv) {
        const dt = this._parseDt(conv.last_interest_dt);
        if (!dt) {
            return "";
        }
        const now = DateTime.local();
        if (dt.hasSame(now, "day")) {
            return dt.toFormat("HH:mm");
        }
        if (dt.hasSame(now, "year")) {
            return dt.toFormat("MM-dd");
        }
        return dt.toFormat("yyyy-MM-dd");
    }

    onConvClick(conv) {
        if (this.state.renamingId === conv.id) {
            return;
        }
        this.props.onSelect(conv.id);
    }

    startRename(conv, ev) {
        ev.stopPropagation();
        this.state.renamingId = conv.id;
        this.state.renameText = conv.name;
    }

    async commitRename(conv) {
        const name = (this.state.renameText || "").trim();
        this.state.renamingId = false;
        if (name && name !== conv.name) {
            await this.props.onRename(conv, name);
        }
    }

    onRenameKeydown(conv, ev) {
        if (ev.key === "Enter") {
            ev.preventDefault();
            this.commitRename(conv);
        } else if (ev.key === "Escape") {
            this.state.renamingId = false;
        }
    }

    onDeleteClick(conv, ev) {
        ev.stopPropagation();
        this.props.onDelete(conv);
    }
}
