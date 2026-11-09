# -*- coding: utf-8 -*-
from odoo import fields, models


class AiAgentBot(models.Model):
    _inherit = 'ai.agent.bot'

    # Marks agents whose tools query a REMOTE instance (this module's read-only
    # tools). Diagnostic sessions only allow such agents, so a generic agent —
    # whose tools would hit the LOCAL database — can never be selected by mistake.
    is_remote_support = fields.Boolean(
        string="Remote Support Agent", default=False,
        help="This agent diagnoses remote customer instances through the "
             "support_tools read-only tools instead of the local database.")
