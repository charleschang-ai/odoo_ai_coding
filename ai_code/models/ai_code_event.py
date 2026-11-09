# -*- coding: utf-8 -*-
from odoo import fields, models

EVENT_TYPES = [
    ('message', 'LLM Message'),
    ('tool_call', 'Tool Call'),
    ('observation', 'Observation'),
    ('summary', 'Condensed Summary'),
    ('phase_change', 'Phase Change'),
]


class AICodeEvent(models.Model):
    """Event-sourced journal of a coding session.

    Every user/assistant message, tool call and tool result is one row: the
    audit trail, the live activity feed of the workspace UI, and the store of
    full tool outputs (the LLM context only keeps stubs of old ones).
    """
    _name = 'ai.code.event'
    _description = 'AI Coding Task Event'
    _order = 'task_id, seq'

    _seq_uniq = models.Constraint(
        'UNIQUE(task_id, seq)',
        "Event sequence numbers must be unique per task.")

    task_id = fields.Many2one('ai.code.task', string='Task', required=True,
                              ondelete='cascade', index=True)
    seq = fields.Integer(string='Seq', required=True)
    turn = fields.Integer(string='Turn', help="User turn this event belongs to.")
    phase = fields.Char(string='Phase', help="Task stage when the event happened.")
    role = fields.Selection([
        ('system', 'System'),
        ('user', 'User'),
        ('assistant', 'Assistant'),
        ('tool', 'Tool'),
    ], string='Role')
    event_type = fields.Selection(EVENT_TYPES, string='Type', required=True)
    tool_name = fields.Char(string='Tool')
    status = fields.Selection([('ok', 'OK'), ('error', 'Error'), ('running', 'Running')],
                              string='Status')
    duration_ms = fields.Integer(string='Duration (ms)')
    content = fields.Text(
        string='Content',
        help="What enters the LLM context (possibly truncated with a retrieval handle).")
    content_full = fields.Text(
        string='Full Content',
        help="Untruncated payload when 'content' is a stub; fetched on demand "
             "via the get_full_observation tool.")
    token_est = fields.Integer(string='Est. Tokens')
    pinned = fields.Boolean(string='Pinned', default=False)

    def tool_get_full_observation(self, event_id):
        """Fetch the untruncated payload of a stubbed observation.
        Only events of the current task (context ai_code_task_id) are served."""
        task_id = self.env.context.get('ai_code_task_id')
        try:
            event = self.sudo().browse(int(event_id)).exists()
        except (TypeError, ValueError):
            return "ERROR: event_id must be an integer."
        if not event or (task_id and event.task_id.id != task_id):
            return f"ERROR: no such observation event {event_id} in this task."
        payload = event.content_full or event.content
        if not payload:
            return f"Observation {event_id} has no stored payload."
        return payload[:24000] + ("\n... [still truncated at 24k chars]" if len(payload) > 24000 else "")
