# -*- coding: utf-8 -*-
import logging

from odoo import _, api, fields, models
from odoo.exceptions import UserError
from odoo.fields import Command

_logger = logging.getLogger(__name__)


class SupportDiagnosticSession(models.Model):
    _name = 'support.diagnostic.session'
    _description = "Remote Support Diagnostic Session"
    _order = 'create_date desc'

    name = fields.Char(string="Reference", required=True, copy=False,
                       readonly=True, default=lambda self: _('New'))
    instance_id = fields.Many2one(
        'support.remote.instance', string="Remote Instance",
        required=True, ondelete='cascade')
    agent_id = fields.Many2one(
        'ai.agent.bot', string="AI Agent",
        domain=[('is_remote_support', '=', True)],
        default=lambda self: self._default_agent(),
        help="AI agent that runs the diagnosis. Only remote-support agents "
             "(whose tools query the remote instance) are allowed, so a generic "
             "agent that would query the local database can't be picked.")
    question = fields.Text(
        string="Customer Question", required=True,
        help="The problem to diagnose, e.g. 'Invoice INV/2026/0087 cannot be "
             "posted'. Sent as the first chat message when you open the chat.")

    state = fields.Selection(
        [('draft', 'Draft'), ('active', 'In Progress')],
        string="Status", default='draft', copy=False)
    channel_id = fields.Many2one(
        'discuss.channel', string="Chat", copy=False, readonly=True,
        ondelete='set null')
    chat_seeded = fields.Boolean(copy=False, readonly=True)
    message_ids = fields.One2many(
        'support.diagnostic.message', 'session_id',
        string="Conversation", copy=False, readonly=True)
    message_count = fields.Integer(compute='_compute_message_count')

    user_id = fields.Many2one(
        'res.users', string="Opened by",
        default=lambda self: self.env.user, readonly=True)

    @api.depends('message_ids')
    def _compute_message_count(self):
        for record in self:
            record.message_count = len(record.message_ids)

    @api.model
    def _default_agent(self):
        return self.env.ref('support_tools.ai_agent_remote_support',
                            raise_if_not_found=False)

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if not vals.get('name') or vals['name'] == _('New'):
                vals['name'] = self.env['ir.sequence'].next_by_code(
                    'support.diagnostic.session') or _('New')
        return super().create(vals_list)

    def _resolve_agent(self):
        self.ensure_one()
        agent = self.agent_id or self._default_agent()
        if not agent:
            raise UserError(_(
                "No Remote Support AI agent is available. Please select one."))
        if not agent.is_remote_support:
            raise UserError(_(
                "Agent '%s' is not a Remote Support agent: its tools would query "
                "the LOCAL database, not the customer's remote instance. Pick the "
                "Remote Support Diagnostician agent.", agent.display_name))
        return agent

    def _ensure_channel(self):
        """Create (once) a dedicated ai_chat channel bound to this session and
        its remote instance, so the AI's tools query the right customer."""
        self.ensure_one()
        if self.channel_id:
            return self.channel_id
        if not self.instance_id.sudo().api_key:
            raise UserError(_(
                "The selected remote instance has no API key configured."))
        agent = self._resolve_agent()
        channel = self.env['discuss.channel'].sudo().create({
            'name': _("Diagnosis %(ref)s — %(inst)s",
                      ref=self.name, inst=self.instance_id.display_name),
            'channel_type': 'ai_chat',
            'ai_en_agent_id': agent.id,
            'support_session_id': self.id,
            'support_instance_id': self.instance_id.id,
            'channel_member_ids': [
                Command.create({'partner_id': self.env.user.partner_id.id}),
                Command.create({'partner_id': agent.partner_id.id}),
            ],
        })
        self.channel_id = channel.id
        self.state = 'active'
        return channel

    def action_open_chat(self):
        """Open the AI chat window for this diagnosis. On first open, the
        customer question is auto-sent so the AI starts diagnosing."""
        self.ensure_one()
        if not self.question:
            raise UserError(_("Please describe the problem to diagnose first."))
        channel = self._ensure_channel()
        params = {'channelId': channel.id}
        if not self.chat_seeded:
            params['user_prompt'] = self.question
            self.chat_seeded = True
        return {
            'type': 'ir.actions.client',
            'tag': 'ai_agent_chat_action',
            'params': params,
        }

    def action_open_discuss(self):
        """Fallback: open the full Discuss app focused on this channel."""
        self.ensure_one()
        if not self.channel_id:
            raise UserError(_("Open the chat first."))
        return {
            'type': 'ir.actions.act_url',
            'url': '/odoo/action-mail.action_discuss?active_id=%s' % self.channel_id.id,
            'target': 'self',
        }
