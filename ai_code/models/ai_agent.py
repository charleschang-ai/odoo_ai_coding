# -*- coding: utf-8 -*-
"""Coding agent = a normal ai.agent.bot whose channel messages are routed to
the ai.code.runner single-loop engine instead of the generic chat loop.

Reusing ai.agent.bot means the whole platform plumbing comes for free: the
/ai/get_ai_response controller, the BACKGROUND response thread (with typing
indicator, Stop button and failure fallback), and message rendering with
[AI_OPTIONS]/[AI_FORM] cards.
"""
import logging
import re

from odoo import api, fields, models, _

_logger = logging.getLogger(__name__)


class AIAgentBot(models.Model):
    _inherit = 'ai.agent.bot'

    is_coding_agent = fields.Boolean(
        string='Coding Agent',
        help="Route this agent's chat to the AI Code runner (single ReAct loop with "
             "todo list, tools and sandbox) instead of the standard assistant loop.")

    def _ai_spawn_response_job(self, mail_message, channel):
        """While a coding turn is running, a new user message must not be
        dropped by the platform's one-job-per-channel gate: queue it for the
        running loop (steering) and acknowledge instantly."""
        if self.sudo().is_coding_agent:
            runner = self.env['ai.code.runner']
            task = runner.get_session_task(channel)
            if task and task.state == 'running' and not runner._is_stale(task):
                text = self._html_to_text(mail_message.body)
                if text:
                    runner._queue_text(task, text)
                return True
            executor = self.env['ir.config_parameter'].sudo().get_param('ai_code.executor', 'thread')
            if executor == 'process' and channel.sudo().ai_code_working_dir \
                    and runner.spawn_process_turn(self, channel, mail_message):
                return True
        return super()._ai_spawn_response_job(mail_message, channel)

    def _get_response_for_channel(self, mail_message, channel):
        if not self.sudo().is_coding_agent:
            return super()._get_response_for_channel(mail_message, channel)
        text = self._html_to_text(mail_message.body)
        if not text:
            return
        return self.env['ai.code.runner'].handle_message(self, channel, text, mail_message)

    # ── helpers ────────────────────────────────────────────────────────
    @api.model
    def _html_to_text(self, body):
        from odoo.tools.mail import html_to_inner_content
        try:
            return (html_to_inner_content(body or '') or '').strip()
        except Exception:  # noqa: BLE001
            return re.sub(r'<[^>]+>', '', body or '').strip()

    def _coding_main_model(self):
        """The user's own pick (workspace model picker) wins, then the global
        default from Settings, then the agent's bound model."""
        params = self.env['ir.config_parameter'].sudo()
        return (params.get_param(f'ai_code.model.{self.env.uid}')
                or params.get_param('ai_code.default_main_model')
                or self.sudo().llm_model)

    def open_agent_chat(self):
        """Agents kanban card click: the coding agent opens its dedicated
        coding workspace (folder picker, task strip, diff review) instead of
        the plain chat window used by the other agents."""
        self.ensure_one()
        if self.sudo().is_coding_agent:
            action = self.env.ref('ai_code.action_ai_code_workspace', raise_if_not_found=False)
            if action:
                return action.read()[0]
        return super().open_agent_chat()

    def open_coding_workspace(self, working_dir=None):
        """Get/create this coding agent's channel and (optionally) bind a dir."""
        self.ensure_one()
        channel = self._get_or_create_ai_chat(self.sudo().name)
        if working_dir:
            self.env['ai.code.workspace']._validate_working_dir(working_dir)
            channel.sudo().ai_code_working_dir = working_dir
        return channel
