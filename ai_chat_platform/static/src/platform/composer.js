import { Component, useExternalListener, useRef, useState } from "@odoo/owl";
import { useService } from "@web/core/utils/hooks";
import { _t } from "@web/core/l10n/translation";
import { useVoiceRecorder } from "@mail/discuss/voice_message/common/voice_recorder";

const MAX_FILE_SIZE = 20 * 1024 * 1024;

/**
 * Input area: attachment chips, plus-menu (upload / export), voice
 * recorder (reuses Odoo's mail useVoiceRecorder hook with our own button
 * look), textarea (Enter to send, Shift+Enter for newline), send button
 * and the session token-usage footnote.
 *
 * Attachments are uploaded through the standard mail.attachment_upload
 * service (pending on thread.composer) and sent with the next message;
 * the backend AI extracts and reads their content.
 */
export class AiPlatformComposer extends Component {
    static template = "ai_chat_platform.Composer";
    static props = {
        thread: { optional: true },
        busy: Boolean,
        onSend: Function,
        requestThread: Function,
        onExport: Function,
    };

    setup() {
        this.state = useState({ text: "", menuOpen: false, uploadingCount: 0 });
        this.attachmentUpload = useService("mail.attachment_upload");
        this.notification = useService("notification");
        this.fileInputRef = useRef("fileInput");
        this.textareaRef = useRef("textarea");
        // Odoo's voice recorder hook (mail): records mic input, encodes
        // mp3 and calls this.attachmentUploader.uploadFile() when done
        this.voiceRecorder = useVoiceRecorder();
        this.attachmentUploader = {
            uploadFile: async (file) => {
                let thread;
                try {
                    thread = await this.props.requestThread();
                } catch {
                    return;
                }
                this.state.uploadingCount++;
                try {
                    await this.attachmentUpload.upload(thread, thread.composer, file);
                } catch (e) {
                    this.notification.add(
                        _t("Upload failed: %s", e.message || file.name), { type: "danger" });
                } finally {
                    this.state.uploadingCount--;
                }
            },
        };
        useExternalListener(document, "click", () => {
            this.state.menuOpen = false;
        });
    }

    get attachments() {
        return this.props.thread?.composer?.attachments || [];
    }

    get canSend() {
        return !this.props.busy &&
            this.state.uploadingCount === 0 &&
            !this.voiceRecorder.recording &&
            (this.state.text.trim() || this.attachments.length);
    }

    onKeydown(ev) {
        if (ev.key === "Enter" && !ev.shiftKey) {
            ev.preventDefault();
            this.send();
        }
    }

    async send() {
        if (!this.canSend) {
            return;
        }
        const text = this.state.text;
        this.state.text = "";
        const sent = await this.props.onSend(text);
        if (!sent) {
            this.state.text = text;
        }
        this.textareaRef.el?.focus();
    }

    toggleMenu(ev) {
        ev.stopPropagation();
        this.state.menuOpen = !this.state.menuOpen;
    }

    pickFile() {
        this.state.menuOpen = false;
        this.fileInputRef.el?.click();
    }

    exportChat() {
        this.state.menuOpen = false;
        this.props.onExport();
    }

    async onVoiceClick() {
        // make sure the channel exists before recording starts, so the
        // upload on stop has a thread to attach to
        if (!this.voiceRecorder.recording) {
            try {
                await this.props.requestThread();
            } catch {
                return;
            }
        }
        this.voiceRecorder.onClick();
    }

    async onFileChange(ev) {
        const files = [...(ev.target.files || [])];
        ev.target.value = "";
        if (!files.length) {
            return;
        }
        for (const file of files) {
            if (file.size > MAX_FILE_SIZE) {
                this.notification.add(
                    _t("File exceeds the 20MB limit: %s", file.name), { type: "warning" });
                continue;
            }
            this.attachmentUploader.uploadFile(file);
        }
    }

    removeAttachment(att) {
        this.attachmentUpload.unlink(att);
    }
}
