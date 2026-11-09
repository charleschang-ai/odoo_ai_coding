import logging

from odoo import api, fields, models

_logger = logging.getLogger(__name__)


class AiLlmPrice(models.Model):
    """Per-model token pricing (USD per 1M tokens) used to estimate costs.

    Shipped defaults are demo values — adjust them to your actual contracts.
    """
    _name = 'ai.llm.price'
    _description = 'AI Model Token Prices'
    _order = 'llm_model'

    llm_model = fields.Char(string='LLM Model', required=True, index=True)
    input_price_per_m = fields.Float(string='Input $/1M', digits=(12, 4))
    output_price_per_m = fields.Float(string='Output $/1M', digits=(12, 4))
    cached_price_per_m = fields.Float(
        string='Cached Input $/1M', digits=(12, 4),
        help="Price for cached input tokens. 0 = same as normal input price.")

    _llm_model_uniq = models.Constraint(
        'UNIQUE (llm_model)', 'A price line already exists for this model.')


class AiTokenUsage(models.Model):
    """One row per AI response generation (all API calls of one turn aggregated).

    Written with sudo from the LLM service; AI managers can only read.
    """
    _name = 'ai.token.usage'
    _description = 'AI Token Usage'
    _order = 'id desc'

    user_id = fields.Many2one('res.users', string='User', required=True, index=True, readonly=True)
    agent_id = fields.Many2one('ai.agent.bot', string='AI Agent', index=True, readonly=True, ondelete='set null')
    channel_id = fields.Many2one('discuss.channel', string='Chat Channel', readonly=True, ondelete='set null')
    provider = fields.Char(string='Provider', index=True, readonly=True)
    llm_model = fields.Char(string='LLM Model', index=True, readonly=True)
    api_calls = fields.Integer(string='API Calls', readonly=True)
    tool_calls = fields.Integer(string='Tool Calls', readonly=True)
    input_tokens = fields.Integer(string='Input Tokens', readonly=True)
    cached_tokens = fields.Integer(string='Cached Tokens', readonly=True)
    output_tokens = fields.Integer(string='Output Tokens', readonly=True)
    total_tokens = fields.Integer(string='Total Tokens', compute='_compute_total_tokens', store=True)
    is_estimated = fields.Boolean(
        string='Estimated', readonly=True,
        help="The provider did not report exact usage; tokens were estimated (~4 chars/token).")
    duration = fields.Float(string='Duration (s)', digits=(10, 2), readonly=True, aggregator='avg',
                            help="Wall-clock time of the whole AI turn (all API calls + tools).")
    cost = fields.Float(string='Cost (USD)', digits=(12, 6), readonly=True,
                        help="Estimated from the Model Token Prices table at logging time.")

    @api.depends('input_tokens', 'output_tokens')
    def _compute_total_tokens(self):
        for rec in self:
            rec.total_tokens = rec.input_tokens + rec.output_tokens

    # ══════════════════════════════════════════════
    # Budget management
    # ══════════════════════════════════════════════

    @api.model
    def _get_month_usage(self, user=None):
        """Aggregated usage since the 1st of the current month."""
        start = fields.Datetime.now().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        domain = [('create_date', '>=', start)]
        if user is not None:
            domain.append(('user_id', '=', user.id))
        rows = self.sudo().search_read(domain, ['total_tokens', 'cost'])
        return {
            'tokens': sum(r['total_tokens'] for r in rows),
            'cost': round(sum(r['cost'] for r in rows), 4),
        }

    @api.model
    def _check_budget(self, user):
        """Budget gate, called before every AI turn.

        Config parameters:
        - ai_chatbot_enhance.budget_monthly_tokens: company monthly budget (0 = unlimited)
        - ai_chatbot_enhance.budget_user_monthly_tokens: per-user monthly limit (0 = unlimited)
        - ai_chatbot_enhance.budget_action: 'warn' (notify managers only) | 'block'

        Managers are notified once when 80% and once when 100% of the company
        budget is reached. Returns (blocked, user_facing_message).
        """
        icp = self.env['ir.config_parameter'].sudo()

        def _int_param(key):
            try:
                return max(0, int(icp.get_param(key, '0') or 0))
            except ValueError:
                return 0

        company_budget = _int_param('ai_chatbot_enhance.budget_monthly_tokens')
        user_budget = _int_param('ai_chatbot_enhance.budget_user_monthly_tokens')
        action = icp.get_param('ai_chatbot_enhance.budget_action', 'warn')

        if company_budget:
            usage = self._get_month_usage()
            if usage['tokens'] >= company_budget:
                self._notify_budget_level(100, usage, company_budget)
                if action == 'block':
                    return True, self.env._(
                        "The company's monthly AI budget has been reached. "
                        "AI assistants are paused until next month — please contact your administrator."
                    )
            elif usage['tokens'] >= company_budget * 0.8:
                self._notify_budget_level(80, usage, company_budget)

        if user_budget and action == 'block':
            user_usage = self._get_month_usage(user)
            if user_usage['tokens'] >= user_budget:
                return True, self.env._(
                    "You have reached your personal monthly AI usage limit "
                    "(%(used)s tokens). Please contact your administrator.",
                    used=user_usage['tokens'],
                )

        return False, None

    @api.model
    def _notify_budget_level(self, level, usage, budget):
        """Email AI managers when a budget threshold is crossed (once per month/level)."""
        icp = self.env['ir.config_parameter'].sudo()
        month = fields.Date.today().strftime('%Y-%m')
        marker_key = 'ai_chatbot_enhance.budget_notified'
        previous = icp.get_param(marker_key, '')
        prev_month, __, prev_level = previous.partition(':')
        try:
            prev_level = int(prev_level or 0)
        except ValueError:
            prev_level = 0
        if prev_month == month and prev_level >= level:
            return
        icp.set_param(marker_key, f"{month}:{level}")
        try:
            manager_group = self.env.ref('ai_chatbot_enhance.group_ai_manager')
            recipients = manager_group.all_user_ids.partner_id.filtered('email')
            if not recipients:
                return
            self.env['mail.mail'].sudo().create({
                'subject': f"AI budget alert: {level}% of the monthly token budget used",
                'body_html': (
                    f"<p>AI usage this month: <b>{usage['tokens']:,} tokens</b> "
                    f"(~${usage['cost']}) — {level}% of the configured budget of "
                    f"{budget:,} tokens.</p>"
                    f"<p>Review usage under AI Chat &gt; Reporting &gt; Token Usage, "
                    f"or adjust the budget in AI settings.</p>"
                ),
                'recipient_ids': [(6, 0, recipients.ids)],
            }).send()
        except Exception:  # noqa: BLE001 - notification must never break the flow
            _logger.exception("AI: failed to send budget notification")

    @api.model
    def _compute_cost_amount(self, llm_model, input_tokens, cached_tokens, output_tokens):
        price = self.env['ai.llm.price'].sudo().search([('llm_model', '=', llm_model)], limit=1)
        if not price:
            return 0.0
        cached_price = price.cached_price_per_m or price.input_price_per_m
        fresh_input = max(0, (input_tokens or 0) - (cached_tokens or 0))
        return (
            fresh_input * price.input_price_per_m
            + (cached_tokens or 0) * cached_price
            + (output_tokens or 0) * price.output_price_per_m
        ) / 1000000.0
