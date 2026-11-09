# Part of AI Code Graph.
from odoo import models


class IrModel(models.Model):
    _inherit = 'ir.model'

    def action_open_code_graph(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.client',
            'tag': 'ai_code_graph_action',
            'name': f"Code Graph: {self.model}",
            'params': {'focus': self.model, 'mode': 'structure'},
        }
