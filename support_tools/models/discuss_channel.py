# -*- coding: utf-8 -*-
import logging

from odoo import fields, models

_logger = logging.getLogger(__name__)


class DiscussChannel(models.Model):
    _inherit = 'discuss.channel'

    # Set when a diagnostic session opens its chat. Lets the AI tools know which
    # remote instance to query, and lets us mirror the conversation back.
    support_session_id = fields.Many2one(
        'support.diagnostic.session', string="Diagnostic Session",
        index=True, ondelete='set null')
    support_instance_id = fields.Many2one(
        'support.remote.instance', string="Remote Instance",
        index=True, ondelete='set null')

    def _message_post_after_hook(self, message, msg_vals):
        res = super()._message_post_after_hook(message, msg_vals)
        for channel in self:
            session = channel.support_session_id
            # Mirror only real conversation (composer/agent), not system notes.
            if not session or message.message_type != 'comment':
                continue
            Mirror = self.env['support.diagnostic.message'].sudo()
            if Mirror.search_count([('message_id', '=', message.id)], limit=1):
                continue  # already mirrored (defensive against re-entrancy)
            author = message.sudo().author_id
            Mirror.create({
                'session_id': session.id,
                'message_id': message.id,
                'role': 'assistant' if author.agent_ids else 'user',
                'author_name': author.display_name,
                'body': message.body,
                'date': message.date,
            })
        return res
