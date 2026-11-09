# -*- coding: utf-8 -*-
import re

from odoo import api, fields, models, _
from odoo.exceptions import UserError

from odoo.addons.ai_chatbot_enhance.utils.llm_providers import PROVIDERS

TASK_STATES = [
    ('draft', 'Draft'),
    ('idle', 'Ready'),
    ('running', 'Working'),
    ('waiting_user', 'Waiting for your answer'),
    ('failed', 'Last turn failed'),
    ('cancelled', 'Cancelled'),
    # legacy states of the gated pipeline (kept so old records still load)
    ('analyzing', 'Analyzing (legacy)'),
    ('spec_review', 'Spec Review (legacy)'),
    ('studying', 'Studying (legacy)'),
    ('planning', 'Planning (legacy)'),
    ('plan_review', 'Plan Review (legacy)'),
    ('coding', 'Coding (legacy)'),
    ('testing', 'Testing (legacy)'),
    ('done', 'Done (legacy)'),
]

# states in which the background runner owns the task (no manual edits)
RUNNING_STATES = ('running',)


def _llm_model_selection():
    return [(model, f"{provider.display_name}: {label}")
            for provider in PROVIDERS
            for model, label in provider.llms]


class AICodeTask(models.Model):
    """One coding SESSION: a conversation bound to a working directory. Each
    user message is one turn of the agent loop; the chat history the model
    sees is persisted on the task, so turns build on each other."""
    _name = 'ai.code.task'
    _description = 'AI Coding Task'
    _order = 'id desc'

    # ── identity / parameters ────────────────────────────────────────────
    task_type = fields.Selection([
        ('new_module', 'New Module'),
        ('modify_module', 'Modify Module'),
    ], string='Type', required=True, default='new_module')
    module_name = fields.Char(
        string='Module (technical name)',
        help="Module most recently written by the agent (used as the default for "
             "sandbox tests and labels).")
    module_title = fields.Char(string='Module Title')
    target_dir = fields.Char(
        string='Working Directory', required=True,
        help="Absolute path of the addons directory to work in. "
             "Must be inside the configured whitelist (Settings > AI Chatbot > AI Code).")
    target_dir_pick = fields.Selection(
        selection='_get_workspace_dir_selection', string='Pick a Folder', store=False,
        help="Choose one of the whitelisted addons directories; it fills the "
             "working directory below.")
    requirement = fields.Text(string='Requirement', required=True,
                              help="The first request of the session.")
    reference_modules = fields.Char(string='Reference Modules')
    odoo_version = fields.Char(
        string='Odoo Version', help="Detected target series (e.g. 18.0). Drives which "
             "idiom snippets and lint rules apply.")
    channel_id = fields.Many2one('discuss.channel', string='Conversation',
                                 ondelete='set null', index=True)
    user_id = fields.Many2one('res.users', string='Requested By',
                              default=lambda self: self.env.user, readonly=True)

    # ── model / budget configuration ─────────────────────────────────────
    main_model = fields.Selection(
        selection=lambda self: _llm_model_selection(), string='Main Model',
        default=lambda self: self.env['ir.config_parameter'].sudo().get_param(
            'ai_code.default_main_model') or False,
        help="Strong model used for the agent loop.")
    cheap_model = fields.Selection(
        selection=lambda self: _llm_model_selection(), string='Cheap Model',
        default=lambda self: self.env['ir.config_parameter'].sudo().get_param(
            'ai_code.default_cheap_model') or False,
        help="Cheaper model used for memory distillation. Falls back to the main model.")
    token_budget = fields.Integer(
        string='Token Budget', default=lambda self: int(
            self.env['ir.config_parameter'].sudo().get_param(
                'ai_code.default_token_budget', '0') or 0),
        help="Hard cap of total tokens for this session. 0 = unlimited.")
    auto_test = fields.Boolean(string='Auto Sandbox Test', default=False,
                               help="Legacy flag (the agent now decides when to run sandbox_test).")

    # ── state machine ────────────────────────────────────────────────────
    state = fields.Selection(TASK_STATES, string='Stage', default='idle',
                             required=True, index=True)
    turn_count = fields.Integer(string='Turns', readonly=True, default=0)
    started_at = fields.Datetime(string='Started At', readonly=True)
    heartbeat = fields.Datetime(string='Heartbeat', readonly=True,
                                help="Bumped by the live runner; a stale heartbeat on a "
                                     "running task means the worker died.")
    abort_flag = fields.Boolean(string='Abort Requested', default=False)
    error_message = fields.Text(string='Failure Reason', readonly=True)
    error_ack = fields.Boolean(string='Failure Acknowledged', default=False)
    followup_active = fields.Boolean(string='Follow-up Running (legacy)', default=False)
    pending_user_text = fields.Text(
        string='Queued User Message',
        help="Message(s) the user sent while a turn was running; injected before "
             "the next LLM call.")
    pending_call_id = fields.Char(
        string='Pending Question Call',
        help="Tool-call id of the ask_user_question / shell approval waiting for the user.")
    pending_action_json = fields.Json(
        string='Pending Action',
        help="What the paused tool call is waiting for: {'kind': 'question'|'shell', ...}.")

    # ── persisted agent state ────────────────────────────────────────────
    history_json = fields.Json(string='LLM History', readonly=True,
                               help="Chat-format history the model sees (compacted).")
    todo_json = fields.Json(string='Todo List', readonly=True)
    todo_md = fields.Text(string='Todo', compute='_compute_todo_md')
    # legacy artifacts of the gated pipeline (read-only, hidden in new views)
    spec_md = fields.Text(string='Spec (legacy)', readonly=True)
    spec_json = fields.Json(string='Spec JSON (legacy)', readonly=True)
    study_notes = fields.Text(string='Study Notes (legacy)', readonly=True)
    plan_md = fields.Text(string='File Plan (legacy)', readonly=True)
    plan_json = fields.Json(string='Plan JSON (legacy)', readonly=True)
    result_summary = fields.Text(string='Result Summary', readonly=True)

    # ── git ──────────────────────────────────────────────────────────────
    branch_name = fields.Char(string='Git Branch', readonly=True)
    branch_base_sha = fields.Char(string='Branch Base', readonly=True)

    # ── relations ────────────────────────────────────────────────────────
    event_ids = fields.One2many('ai.code.event', 'task_id', string='Events')
    change_ids = fields.One2many('ai.code.change', 'task_id', string='Changes')
    event_count = fields.Integer(compute='_compute_counts')
    change_count = fields.Integer(compute='_compute_counts')

    # ── usage statistics (accumulated by the runner) ─────────────────────
    input_tokens = fields.Integer(string='Input Tokens', readonly=True)
    cached_tokens = fields.Integer(string='Cached Tokens', readonly=True)
    output_tokens = fields.Integer(string='Output Tokens', readonly=True)
    api_calls = fields.Integer(string='API Calls', readonly=True)
    tool_calls = fields.Integer(string='Tool Calls', readonly=True)
    cost = fields.Float(string='Cost', readonly=True, digits=(12, 4))
    duration = fields.Float(string='Duration (s)', readonly=True)

    @api.depends('event_ids', 'change_ids')
    def _compute_counts(self):
        for task in self:
            task.event_count = len(task.event_ids)
            task.change_count = len(task.change_ids)

    @api.depends('todo_json')
    def _compute_todo_md(self):
        marks = {'completed': '[x]', 'in_progress': '[>]', 'pending': '[ ]'}
        for task in self:
            todos = task.todo_json or []
            task.todo_md = "\n".join(
                f"{marks.get(t.get('status'), '[ ]')} {t.get('content', '')}" for t in todos)

    @api.model
    def _get_workspace_dir_selection(self):
        dirs = self.env['ai.code.workspace']._get_workspace_dirs()
        return [(str(d), str(d)) for d in dirs]

    @api.onchange('target_dir_pick')
    def _onchange_target_dir_pick(self):
        if self.target_dir_pick:
            self.target_dir = self.target_dir_pick

    @api.depends('module_name', 'channel_id', 'requirement')
    def _compute_display_name(self):
        for task in self:
            label = task.module_name or (task.requirement or '').strip()[:40] or _("(session)")
            task.display_name = f"{label} #{task.id or ''}"

    # ── validation ───────────────────────────────────────────────────────
    @api.constrains('module_name')
    def _check_module_name(self):
        for task in self:
            if task.module_name and not re.fullmatch(r'[a-z][a-z0-9_]*', task.module_name):
                raise UserError(_(
                    "Invalid technical module name %r: use snake_case "
                    "(lowercase letters, digits, underscores).", task.module_name))

    @api.constrains('target_dir')
    def _check_target_dir(self):
        for task in self:
            if task.target_dir:
                self.env['ai.code.workspace']._validate_working_dir(task.target_dir)

    # ── buttons ──────────────────────────────────────────────────────────
    def action_abort(self):
        for task in self:
            if task.state in RUNNING_STATES:
                task.abort_flag = True

    def action_cancel(self):
        for task in self:
            task.write({'state': 'cancelled', 'abort_flag': False})

    def action_reset_draft(self):
        """Legacy button name: bring a stuck/failed session back to Ready."""
        for task in self:
            task.write({'state': 'idle', 'abort_flag': False, 'error_message': False,
                        'error_ack': False, 'followup_active': False,
                        'pending_call_id': False, 'pending_action_json': False})

    def action_open_channel(self):
        self.ensure_one()
        if not self.channel_id:
            raise UserError(_("This task has no conversation attached."))
        return {
            'type': 'ir.actions.client',
            'tag': 'mail.action_discuss',
            'params': {'active_id': self.channel_id.id},
        }
