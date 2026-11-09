# -*- coding: utf-8 -*-
import datetime
import pathlib

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError

RUNNING_TASK_STATES = ('running',)
WAITING_TASK_STATES = ('waiting_user',)


class DiscussChannel(models.Model):
    _inherit = 'discuss.channel'

    ai_code_working_dir = fields.Char(
        string='AI Code Working Directory',
        help="Coding sessions: whitelisted addons directory this conversation works in. "
             "Every task created from this conversation inherits it.")
    ai_code_task_ids = fields.One2many('ai.code.task', 'channel_id',
                                       string='AI Coding Tasks')

    @api.constrains('ai_code_working_dir')
    def _check_ai_code_working_dir(self):
        for channel in self:
            if channel.ai_code_working_dir:
                self.env['ai.code.workspace']._validate_working_dir(
                    channel.ai_code_working_dir)

    def unlink(self):
        """Deleting a coding conversation stops its tasks: running ones are
        aborted (the worker stops after the current step), waiting ones cancelled.
        The task records themselves survive (channel_id → set null) for audit."""
        for task in self.sudo().ai_code_task_ids:
            if task.state in RUNNING_TASK_STATES:
                task.abort_flag = True  # worker sees it after this request commits
            task.state = 'cancelled'
        return super().unlink()

    # ══════════════════════════════════════════════════════════════════
    # AI Platform page integration: the coding agent lives INSIDE the
    # normal chat page (ai_chat_platform). Its bootstrap/conversation RPCs
    # are extended so the frontend can show the folder picker, task strip,
    # todo progress and diff review for coding conversations.
    # ══════════════════════════════════════════════════════════════════

    @api.model
    def ai_code_workspace_data(self):
        """Bootstrap payload for the dedicated coding workspace page: the
        coding agent, the (auto-detected or configured) folders, and this
        user's coding conversations with live task state. Kept separate from
        the platform's own ai_platform_get_data so the normal chat page is
        untouched."""
        agent = self.env.ref('ai_code.coding_agent', raise_if_not_found=False)
        if agent and agent.sudo().allowed_group_ids and not (
                self.env.user.all_group_ids & agent.sudo().allowed_group_ids):
            raise UserError(_("You are not allowed to use the coding agent."))
        Ws = self.env['ai.code.workspace']
        dirs = Ws._get_workspace_dirs()
        return {
            "coding_agent_id": agent.id if agent else False,
            "coding_agent_name": agent.name if agent else _("Odoo Coding Agent"),
            "workspaces": [self._ai_code_ws_entry(Ws, d) for d in dirs],
            "recent": [self._ai_code_ws_entry(Ws, pathlib.Path(p))
                       for p in Ws.get_recent_dirs()],
            "conversations": self.ai_code_get_conversations(),
            "user_name": self.env.user.name,
            "can_view_all": self._ai_platform_can_view_all(),
            "dev_reload": Ws.server_dev_reload(),
            "models": self.ai_code_available_models(),
            "current_model": agent._coding_main_model() if agent else "",
        }

    # ── model picker ───────────────────────────────────────────────────
    @api.model
    def ai_code_available_models(self):
        """Models of every provider that has an API key configured, for the
        workspace model picker: [{id, label, provider, provider_label}]."""
        from odoo.addons.ai_chatbot_enhance.utils.llm_providers import PROVIDERS
        from odoo.addons.ai_chatbot_enhance.utils.llm_api_service import LLMApiService
        out = []
        for provider in PROVIDERS:
            try:
                token = LLMApiService(self.env, provider.name)._get_api_token(raise_if_missing=False)
            except Exception:  # noqa: BLE001 — a misconfigured provider is just absent
                token = None
            if not token:
                continue
            for model_id, label in provider.llms:
                out.append({'id': model_id, 'label': label, 'provider': provider.name,
                            'provider_label': provider.display_name})
        return out

    def ai_code_set_model(self, model):
        """Pick the model for THIS conversation (its session task) and remember
        it as the user's default for new coding conversations."""
        self.ensure_one()
        self._ai_platform_check_member()
        model = (model or '').strip()
        allowed = {m['id'] for m in self.ai_code_available_models()}
        if model not in allowed:
            raise UserError(_("Model %s is not available (no API key for its provider).", model))
        self.env['ir.config_parameter'].sudo().set_param(f'ai_code.model.{self.env.uid}', model)
        task = self.env['ai.code.runner'].get_session_task(self)
        if task:
            task.sudo().write({'main_model': model})
        return model

    @api.model
    def ai_code_set_default_model(self, model):
        """Remember the user's model for the NEXT new conversation (no channel yet)."""
        model = (model or '').strip()
        allowed = {m['id'] for m in self.ai_code_available_models()}
        if model not in allowed:
            raise UserError(_("Model %s is not available (no API key for its provider).", model))
        self.env['ir.config_parameter'].sudo().set_param(f'ai_code.model.{self.env.uid}', model)
        return model

    @staticmethod
    def _ai_code_ws_entry(Ws, d):
        d = pathlib.Path(d)
        return {
            "path": str(d),
            "label": d.name or str(d),
            "is_git": Ws._dir_is_git(d),
            "module_count": Ws.count_modules(d),
            "writable": Ws.dir_writable(d),
        }

    @api.model
    def ai_code_running_tasks(self):
        """Background-tasks widget data: the current user's active coding tasks
        across ALL conversations — running (with live elapsed time) and waiting
        for review — plus the last few finished within 30 minutes."""
        # self-heal: any run whose worker died (server restart/crash) is failed
        # here so it never spins forever in the widget
        self.env['ai.code.runner'].reap_stale_tasks()
        now = fields.Datetime.now()
        recent_cut = now - datetime.timedelta(minutes=30)
        tasks = self.env['ai.code.task'].sudo().search([
            ('user_id', '=', self.env.uid),
            '|',
            ('state', 'in', RUNNING_TASK_STATES + WAITING_TASK_STATES),
            '&', ('state', 'in', ('idle', 'failed')),
                 ('write_date', '>=', fields.Datetime.to_string(recent_cut)),
        ], order='id desc', limit=20)
        return [self._ai_code_task_row(task, now) for task in tasks]

    @api.model
    def ai_code_task_history(self, limit=40):
        """History tab: this user's finished coding tasks (done/failed/cancelled),
        newest first, NOT time-boxed — so past runs stay visible for audit."""
        tasks = self.env['ai.code.task'].sudo().search([
            ('user_id', '=', self.env.uid),
            ('state', 'in', ('idle', 'failed', 'cancelled', 'done')),
        ], order='id desc', limit=min(int(limit or 40), 100))
        now = fields.Datetime.now()
        return [self._ai_code_task_row(task, now) for task in tasks]

    def _ai_code_task_row(self, task, now=None):
        """One task-manager row (shared by the active-tasks widget and the
        history tab)."""
        now = now or fields.Datetime.now()
        running = task.state in RUNNING_TASK_STATES
        start = task.started_at or task.create_date
        elapsed = int((now - start).total_seconds()) if start and running else 0
        todos = task.todo_json or []
        done = sum(1 for t in todos if t.get('status') == 'completed')
        return {
            'task_id': task.id,
            'channel_id': task.channel_id.id or False,
            'label': task.module_name or (task.requirement or '')[:40] or f"#{task.id}",
            'folder': pathlib.Path(task.target_dir).name if task.target_dir else '',
            'state': task.state,
            'running': running,
            'waiting': task.state in WAITING_TASK_STATES,
            'elapsed': elapsed,
            'duration': round(task.duration or 0),
            'progress': f"{done}/{len(todos)}" if todos else '',
            'turns': task.turn_count,
            'cost': round(task.cost or 0, 3),
            'error': (task.error_message or '')[:200],
        }

    @api.model
    def ai_code_stop_task(self, task_id):
        """Stop button in the background-tasks widget: abort a running task
        (graceful — checked between rounds) or cancel a waiting one."""
        task = self.env['ai.code.task'].sudo().browse(int(task_id)).exists()
        if not task:
            raise UserError(_("Task not found."))
        if task.user_id.id != self.env.uid and \
                not self.env.user.has_group('ai_chatbot_enhance.group_ai_manager'):
            raise AccessError(_("You may only stop your own tasks."))
        if task.state in RUNNING_TASK_STATES:
            task.abort_flag = True
            if task.channel_id:
                task.channel_id.sudo().write({'ai_stop_request_dt': fields.Datetime.now()})
            return {'action': 'aborting'}
        if task.state in WAITING_TASK_STATES:
            task.write({'state': 'idle', 'pending_call_id': False})
            return {'action': 'cancelled'}
        return {'action': 'noop'}

    @api.model
    def ai_code_scan_workspace(self, working_dir):
        """List existing modules in a folder (for the workspace card): empty →
        new project, non-empty → pick a module to modify or describe a new one."""
        return self.env['ai.code.workspace'].scan_workspace(working_dir)

    @api.model
    def ai_code_browse_dir(self, path=''):
        """Server-side folder browser (the web-safe 'open folder'): list the
        browsable sub-directories of `path`, bounded to the project area."""
        return self.env['ai.code.workspace'].browse_dir(path or '')

    @api.model
    def ai_code_adopt_workspace(self, path):
        """Adopt a browsed folder as a usable working directory (whitelist +
        recent), returning the refreshed picker data with it selected."""
        if not self.env.user.has_group('ai_chatbot_enhance.group_ai_manager'):
            raise AccessError(_("Only AI managers may add a coding folder."))
        Ws = self.env['ai.code.workspace']
        adopted = Ws.adopt_working_dir(path)
        return {
            "adopted": adopted,
            "workspaces": [self._ai_code_ws_entry(Ws, d) for d in Ws._get_workspace_dirs()],
            "recent": [self._ai_code_ws_entry(Ws, pathlib.Path(p)) for p in Ws.get_recent_dirs()],
        }

    @api.model
    def ai_code_get_conversations(self, all_users=False):
        agent = self.env.ref('ai_code.coding_agent', raise_if_not_found=False)
        if not agent:
            return []
        # admins may list every user's coding conversation (same gate as the
        # platform chat page: _ai_platform_conv_domain)
        channels = self.search(
            self._ai_platform_conv_domain(all_users) + [("ai_en_agent_id", "=", agent.id)],
            order="last_interest_dt DESC, id DESC", limit=200)
        result = []
        for channel in channels:
            task = channel.sudo().ai_code_task_ids.sorted('id')[-1:]
            owner_name, is_mine = channel._ai_platform_conv_owner()
            result.append({
                "id": channel.id,
                "name": channel.name or "",
                "agent_id": agent.id,
                "agent_name": agent.name or "",
                "agent_model": (task.main_model if task else "") or agent.llm_model or "",
                "owner_name": owner_name,
                "is_mine": is_mine,
                "working_dir": channel.sudo().ai_code_working_dir or "",
                "task_state": task.state if task else "",
                "task_id": task.id if task else False,
                "last_interest_dt": fields.Datetime.to_string(channel.last_interest_dt)
                if channel.last_interest_dt else "",
            })
        return result

    @api.model
    def ai_code_new_conversation(self, working_dir, name=None):
        """Create a coding conversation bound to a working directory. The dir is
        adopted (whitelisted + recorded as recent) then validated."""
        Ws = self.env['ai.code.workspace']
        working_dir = Ws.adopt_working_dir(working_dir)
        Ws._validate_working_dir(working_dir)
        if not Ws.dir_writable(working_dir):
            raise UserError(_(
                "%s is mounted read-only (deployed code). Pick a writable folder — on "
                "OdooshCN use the AI workspace (/mnt/ai-workspace).", working_dir))
        agent = self.env.ref('ai_code.coding_agent', raise_if_not_found=False)
        if not agent:
            raise UserError(_("The coding agent is not installed."))
        from odoo.addons.mail.tools.discuss import Store
        # fold the folder name into the conversation title so the sidebar shows
        # which project each conversation belongs to (badge without a custom sidebar)
        folder = pathlib.Path(working_dir).name or working_dir
        title = (name or "").strip()[:100] or _("New session")
        channel_name = f"[{folder}] {title}"
        channel = agent._create_ai_chat_channel(channel_name=channel_name[:120])
        channel.sudo().ai_code_working_dir = working_dir
        return {
            "channel_id": channel.id,
            "working_dir": working_dir,
            "store_data": Store().add(channel).get_result(),
        }

    def ai_code_set_working_dir(self, working_dir):
        self.ensure_one()
        self._ai_platform_check_member()
        self.env['ai.code.workspace']._validate_working_dir(working_dir)
        self.sudo().ai_code_working_dir = working_dir
        return working_dir

    # ── live task progress for the chat page ───────────────────────────
    def ai_code_task_progress(self, limit=30):
        """Latest task of this conversation: phase, todo progress, change list
        (no diff bodies — fetched per change on click), usage stats."""
        self.ensure_one()
        self._ai_platform_check_member()
        task = self.sudo().ai_code_task_ids.sorted('id')[-1:]
        if not task:
            return {}
        todos = task.todo_json or []
        done_count = sum(1 for t in todos if t.get('status') == 'completed')
        running = task.state in RUNNING_TASK_STATES
        if running and self.env['ai.code.runner']._is_stale(task):
            self.env['ai.code.runner'].reap_stale_tasks()
            task.invalidate_recordset(['state'])
            running = task.state in RUNNING_TASK_STATES
        Event = self.env['ai.code.event'].sudo()
        events = Event.search([('task_id', '=', task.id),
                               ('event_type', 'in', ('tool_call', 'phase_change'))],
                              order='seq desc', limit=int(limit or 30))
        activity = [{
            'id': e.id, 'seq': e.seq, 'kind': e.event_type, 'name': e.tool_name or '',
            'summary': (e.content or '')[:160], 'status': e.status or '',
            'ms': e.duration_ms or 0, 'turn': e.turn or 0,
        } for e in reversed(events)]
        current = next((a for a in reversed(activity)
                        if a['kind'] == 'tool_call' and a['status'] == 'running'), None)
        return {
            "task_id": task.id,
            "state": task.state,
            "running": running,
            "waiting_user": task.state in WAITING_TASK_STATES,
            "followup_active": False,
            "module_name": task.module_name or "",
            "working_dir": task.target_dir or "",
            "branch": task.branch_name or "",
            "error": (task.error_message or "")[:500],
            "error_ack": task.error_ack,
            "turn": task.turn_count,
            "model": task.main_model or "",
            "current": current,
            "activity": activity,
            "todo": {
                "total": len(todos),
                "done_count": done_count,
                "items": [{
                    "index": i,
                    "content": t.get('content', ''),
                    "status": t.get('status', 'pending'),
                } for i, t in enumerate(todos, 1)],
            },
            "changes": [{
                "id": c.id,
                "file_path": c.file_path,
                "change_type": c.change_type,
                "git_sha": (c.git_sha or "")[:7],
                "reverted": c.reverted,
            } for c in task.change_ids],
            "stats": {
                "api_calls": task.api_calls,
                "tokens": task.input_tokens + task.output_tokens,
                "cached": task.cached_tokens,
                "cost": round(task.cost, 3),
            },
        }

    # ── conversation documents (spec-by-example uploads) ───────────────
    def ai_code_upload_attachment(self, name, datas_b64, mimetype=None):
        """Attach a document to this coding conversation (quotation, invoice,
        price list...) for the agent to analyze. Member-gated; sudo create so
        plain users need no ir.attachment ACL gymnastics."""
        self.ensure_one()
        self._ai_platform_check_member()
        if not datas_b64:
            raise UserError(_("Empty file."))
        # ~15MB binary ceiling (b64 is ≈4/3 of raw)
        if len(datas_b64) > 20 * 1024 * 1024:
            raise UserError(_("File too large (max 15 MB)."))
        import mimetypes
        att = self.env['ir.attachment'].sudo().create({
            'name': (name or 'document')[:180],
            'datas': datas_b64,
            'mimetype': mimetype or mimetypes.guess_type(name or '')[0]
            or 'application/octet-stream',
            'res_model': 'discuss.channel',
            'res_id': self.id,
        })
        return {'id': att.id, 'name': att.name, 'mimetype': att.mimetype,
                'size': att.file_size}

    def ai_code_list_attachments(self):
        self.ensure_one()
        self._ai_platform_check_member()
        atts = self.env['ir.attachment'].sudo().search([
            ('res_model', '=', 'discuss.channel'), ('res_id', '=', self.id),
        ], order='id')
        return [{'id': a.id, 'name': a.name, 'mimetype': a.mimetype,
                 'size': a.file_size} for a in atts]

    def ai_code_delete_attachment(self, attachment_id):
        self.ensure_one()
        self._ai_platform_check_member()
        att = self.env['ir.attachment'].sudo().browse(int(attachment_id)).exists()
        if not att or att.res_model != 'discuss.channel' or att.res_id != self.id:
            raise UserError(_("Attachment not found in this conversation."))
        att.unlink()
        return True

    def ai_code_ack_error(self, task_id):
        """Dismiss the failure banner: the failure stays recorded on the task
        (audit), it just stops being shown in the chat."""
        self.ensure_one()
        self._ai_platform_check_member()
        task = self.env['ai.code.task'].sudo().browse(int(task_id)).exists()
        if not task or task.channel_id.id != self.id:
            raise UserError(_("Task not found in this conversation."))
        task.error_ack = True
        return True

    def ai_code_get_change_diff(self, change_id):
        self.ensure_one()
        self._ai_platform_check_member()
        change = self.env['ai.code.change'].sudo().browse(int(change_id)).exists()
        if not change or change.task_id.channel_id.id != self.id:
            raise UserError(_("Change not found in this conversation."))
        return {
            "id": change.id,
            "file_path": change.file_path,
            "change_type": change.change_type,
            "git_sha": (change.git_sha or "")[:7],
            "reverted": change.reverted,
            "diff": (change.diff or _("(no diff recorded)"))[:60000],
        }

    def ai_code_get_change_content(self, change_id):
        """Current full content of a changed file (for the 'copy file' button)."""
        self.ensure_one()
        self._ai_platform_check_member()
        change = self.env['ai.code.change'].sudo().browse(int(change_id)).exists()
        if not change or change.task_id.channel_id.id != self.id:
            raise UserError(_("Change not found in this conversation."))
        task = change.task_id
        content = self.env['ai.code.workspace'].tool_read_file_raw(
            task.target_dir, change.file_path)
        return {"file_path": change.file_path, "content": content}

    def ai_code_revert_change(self, change_id):
        """Revert one step from the chat diff drawer. Member + AI manager gated;
        executes with sudo because the git/file work is server-side anyway."""
        self.ensure_one()
        self._ai_platform_check_member()
        if not self.env.user.has_group('ai_chatbot_enhance.group_ai_manager'):
            raise AccessError(_("Only AI managers may revert coding steps."))
        change = self.env['ai.code.change'].sudo().browse(int(change_id)).exists()
        if not change or change.task_id.channel_id.id != self.id:
            raise UserError(_("Change not found in this conversation."))
        self.env['ai.code.workspace'].sudo()._revert_change(change)
        return True
