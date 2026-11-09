import logging

from dateutil.relativedelta import relativedelta

from odoo import api, fields, models
from odoo.tools import html_sanitize

try:
    from markdown2 import markdown
except ImportError:
    markdown = None

_logger = logging.getLogger(__name__)


class AiDigest(models.Model):
    """Scheduled AI reports: an agent runs a prompt on a schedule and the
    answer is posted to a Discuss channel and/or emailed to recipients.

    Intended for READ/ANALYSIS prompts ("summarize last week's sales") —
    mutation tools stay behind their usual guide/confirmation gates, and
    there is no human in the loop here to confirm anything.
    """
    _name = 'ai.digest'
    _description = 'Scheduled AI Report'
    _order = 'next_run'

    name = fields.Char(string='Title', required=True)
    active = fields.Boolean(default=True)
    agent_id = fields.Many2one('ai.agent.bot', string='AI Agent', required=True, ondelete='cascade')
    prompt = fields.Text(
        string='Prompt', required=True,
        help="What to ask the agent on each run, e.g. '总结上周的销售情况：订单数、金额、TOP客户，"
             "并给出本周建议'. Use READ/ANALYSIS prompts — there is nobody here to confirm changes.")
    interval_type = fields.Selection([
        ('daily', 'Daily'),
        ('weekly', 'Weekly'),
        ('monthly', 'Monthly'),
    ], string='Frequency', required=True, default='weekly')
    next_run = fields.Datetime(string='Next Run', required=True, default=fields.Datetime.now)
    channel_id = fields.Many2one(
        'discuss.channel', string='Post to Channel',
        help="The report is posted in this Discuss channel (as the agent).")
    recipient_ids = fields.Many2many(
        'res.partner', string='Email Recipients',
        help="The report is also emailed to these contacts.")
    last_run = fields.Datetime(string='Last Run', readonly=True)
    last_error = fields.Char(string='Last Error', readonly=True)

    def action_run_now(self):
        for digest in self:
            digest._run_digest()
        return True

    @api.model
    def _cron_run_digests(self):
        now = fields.Datetime.now()
        for digest in self.search([('next_run', '<=', now)]):
            try:
                with self.env.cr.savepoint():
                    digest._run_digest()
            except Exception as e:  # noqa: BLE001 - one broken digest must not stop the others
                _logger.exception("AI digest '%s' failed", digest.name)
                digest.last_error = str(e)[:500]
            digest._schedule_next()

    def _run_digest(self):
        self.ensure_one()
        agent = self.agent_id
        responses = agent._get_response(prompt=self.prompt, chat_history=[])
        text = "\n\n".join(responses or [])
        if not text.strip():
            raise ValueError("The agent returned an empty report.")

        if self.channel_id:
            agent._post_ai_response(self.channel_id, f"**{self.name}**\n\n{text}")

        if self.recipient_ids:
            if markdown:
                body_html = html_sanitize(markdown(text, extras=['fenced-code-blocks', 'tables', 'strike']))
            else:
                body_html = html_sanitize(text)
            self.env['mail.mail'].sudo().create({
                'subject': self.name,
                'body_html': body_html,
                'recipient_ids': [(6, 0, self.recipient_ids.ids)],
                'author_id': agent.partner_id.id,
            }).send()

        self.write({'last_run': fields.Datetime.now(), 'last_error': False})

    def _schedule_next(self):
        deltas = {
            'daily': relativedelta(days=1),
            'weekly': relativedelta(weeks=1),
            'monthly': relativedelta(months=1),
        }
        for digest in self:
            base = digest.next_run or fields.Datetime.now()
            next_run = base + deltas[digest.interval_type]
            # catch up if the schedule fell far behind (e.g. server was down)
            now = fields.Datetime.now()
            while next_run <= now:
                next_run += deltas[digest.interval_type]
            digest.next_run = next_run
