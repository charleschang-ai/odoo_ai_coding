# -*- coding: utf-8 -*-
from odoo import fields, models

from .ai_code_task import _llm_model_selection


class ResConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'

    ai_code_workspace_dirs = fields.Char(
        string='Workspace Directories',
        config_parameter='ai_code.workspace_dirs',
        help="Whitelisted addons directories the coding agent may work in "
             "(comma or newline separated absolute paths).")
    ai_code_git_user_name = fields.Char(
        string='Git Author Name',
        config_parameter='ai_code.git_user_name', default='AI Code Agent')
    ai_code_git_user_email = fields.Char(
        string='Git Author Email',
        config_parameter='ai_code.git_user_email', default='ai-code@localhost')
    ai_code_default_main_model = fields.Selection(
        selection=lambda self: _llm_model_selection(),
        string='Default Main Model',
        config_parameter='ai_code.default_main_model')
    ai_code_default_cheap_model = fields.Selection(
        selection=lambda self: _llm_model_selection(),
        string='Default Cheap Model',
        config_parameter='ai_code.default_cheap_model')
    ai_code_default_token_budget = fields.Integer(
        string='Default Token Budget per Session',
        config_parameter='ai_code.default_token_budget', default=0,
        help="0 = unlimited. A per-session hard cap on total tokens.")
    ai_code_max_rounds = fields.Integer(
        string='Max Rounds per Turn',
        config_parameter='ai_code.max_rounds', default=60,
        help="Safety cap on LLM rounds (tool-call cycles) for one user message.")
    ai_code_executor = fields.Selection([
        ('thread', 'Background thread in the HTTP worker'),
        ('process', 'Detached odoo-bin process (survives worker recycling)'),
    ], string='Turn Executor', config_parameter='ai_code.executor', default='thread',
        help="Thread: fastest start, but a worker recycled by limit_time/limit_memory kills "
             "the run. Process: each turn runs in its own odoo-bin shell process (~5-10s "
             "start-up, ~300 MB), immune to worker recycling.")
    ai_code_streaming = fields.Boolean(
        string='Stream Answers Token by Token',
        config_parameter='ai_code.streaming', default=True,
        help="Show the assistant's text live while it is generated (chat-completions "
             "providers: DeepSeek, Qwen, Kimi, OpenAI-compatible).")
    ai_code_shell_enabled = fields.Boolean(
        string='Shell Tool Enabled',
        config_parameter='ai_code.shell_enabled', default=True,
        help="Let the agent run commands inside the workspace. Read-only commands run "
             "at once; state-changing ones pause on an approval card in the chat.")
    ai_code_shell_auto_approve = fields.Boolean(
        string='Auto-approve Shell Commands',
        config_parameter='ai_code.shell_auto_approve', default=False,
        help="Skip the approval card for state-changing commands (catastrophic commands "
             "and git mutations stay refused). Only for trusted, isolated servers.")
    ai_code_sandbox_enabled = fields.Boolean(
        string='Sandbox Testing Enabled',
        config_parameter='ai_code.sandbox_enabled', default=True,
        help="Let the agent install-test modules in a disposable database "
             "(sandbox_test tool). Off = the user verifies manually.")
    ai_code_sandbox_db = fields.Char(
        string='Sandbox Database',
        config_parameter='ai_code.sandbox_db', default='aicode-sandbox',
        help="Disposable database used to install-test generated modules. "
             "Created automatically on first use.")
    ai_code_sandbox_modules = fields.Char(
        string='Sandbox Preinstalled Modules',
        config_parameter='ai_code.sandbox_modules', default='base',
        help="Comma-separated modules installed when the sandbox database is "
             "bootstrapped (e.g. base,mail,sale for tasks that extend them).")
