# -*- coding: utf-8 -*-
from odoo import fields, models


class AICodeChange(models.Model):
    """One file-level change made by the agent (one git commit per change)."""
    _name = 'ai.code.change'
    _description = 'AI Coding File Change'
    _order = 'id'

    task_id = fields.Many2one('ai.code.task', string='Task', required=True,
                              ondelete='cascade', index=True)
    file_path = fields.Char(string='File', required=True,
                            help="Path relative to the working directory.")
    change_type = fields.Selection([
        ('create', 'Create'),
        ('modify', 'Modify'),
        ('delete', 'Delete'),
    ], string='Change', required=True)
    diff = fields.Text(string='Unified Diff')
    git_sha = fields.Char(string='Commit', readonly=True,
                          help="Commit created for this step; revert targets it.")
    reverted = fields.Boolean(string='Reverted', default=False)

    def action_revert(self):
        """Revert this step's commit in the working directory (button + tool)."""
        self.ensure_one()
        self.env['ai.code.workspace']._revert_change(self)
        return True
