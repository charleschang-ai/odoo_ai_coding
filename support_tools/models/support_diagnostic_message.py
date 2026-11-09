# -*- coding: utf-8 -*-
from odoo import fields, models


class SupportDiagnosticMessage(models.Model):
    _name = 'support.diagnostic.message'
    _description = "Diagnostic Conversation Turn"
    _order = 'date, id'

    session_id = fields.Many2one(
        'support.diagnostic.session', string="Session",
        required=True, ondelete='cascade', index=True)
    message_id = fields.Many2one(
        'mail.message', string="Source Message",
        ondelete='cascade', index=True,
        help="The discuss message this record mirrors.")
    role = fields.Selection(
        [('user', 'Support'), ('assistant', 'AI')],
        string="Role", required=True)
    author_name = fields.Char(string="Author")
    body = fields.Html(string="Message", sanitize=False, readonly=True)
    date = fields.Datetime(string="Date")
