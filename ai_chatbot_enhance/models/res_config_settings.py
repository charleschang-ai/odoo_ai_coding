from odoo import fields, models


class ResConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'

    openai_key_enabled = fields.Boolean(
        string="Enable custom OpenAI API key",
        compute='_compute_openai_key_enabled',
        readonly=False,
        groups='base.group_system',
    )
    openai_key = fields.Char(
        string="OpenAI API key",
        config_parameter='ai_chatbot_enhance.openai_key',
        readonly=False,
        groups='base.group_system',
    )

    google_key_enabled = fields.Boolean(
        string="Enable custom Google API key",
        compute='_compute_google_key_enabled',
        readonly=False,
        groups='base.group_system',
    )
    google_key = fields.Char(
        string="Google AI API key",
        config_parameter='ai_chatbot_enhance.google_key',
        readonly=False,
        groups='base.group_system',
    )

    deepseek_key_enabled = fields.Boolean(
        string="Enable DeepSeek API key",
        compute='_compute_deepseek_key_enabled',
        readonly=False,
        groups='base.group_system',
    )
    deepseek_key = fields.Char(
        string="DeepSeek API key",
        config_parameter='ai_chatbot_enhance.deepseek_key',
        readonly=False,
        groups='base.group_system',
    )

    qwen_key_enabled = fields.Boolean(
        string="Enable Qwen API key",
        compute='_compute_qwen_key_enabled',
        readonly=False,
        groups='base.group_system',
    )
    qwen_key = fields.Char(
        string="Qwen API key",
        config_parameter='ai_chatbot_enhance.qwen_key',
        readonly=False,
        groups='base.group_system',
    )

    kimi_key_enabled = fields.Boolean(
        string="Enable Kimi API key",
        compute='_compute_kimi_key_enabled',
        readonly=False,
        groups='base.group_system',
    )
    kimi_key = fields.Char(
        string="Kimi (Moonshot) API key",
        config_parameter='ai_chatbot_enhance.kimi_key',
        readonly=False,
        groups='base.group_system',
    )

    zhipu_key_enabled = fields.Boolean(
        string="Enable Zhipu API key",
        compute='_compute_zhipu_key_enabled',
        readonly=False,
        groups='base.group_system',
    )
    zhipu_key = fields.Char(
        string="Zhipu GLM API key",
        config_parameter='ai_chatbot_enhance.zhipu_key',
        readonly=False,
        groups='base.group_system',
    )

    ai_budget_monthly_tokens = fields.Integer(
        string="Company monthly token budget",
        config_parameter='ai_chatbot_enhance.budget_monthly_tokens',
        groups='base.group_system',
        help="Total tokens the whole company may use per month. 0 = unlimited. "
             "AI managers are emailed at 80% and 100%.",
    )
    ai_budget_user_monthly_tokens = fields.Integer(
        string="Per-user monthly token limit",
        config_parameter='ai_chatbot_enhance.budget_user_monthly_tokens',
        groups='base.group_system',
        help="Tokens each user may use per month. 0 = unlimited. Only enforced when the action is 'Warn and block'.",
    )
    ai_budget_action = fields.Selection([
        ('warn', 'Warn managers only'),
        ('block', 'Warn and block'),
    ], string="When the budget is reached",
        config_parameter='ai_chatbot_enhance.budget_action',
        default='warn',
        groups='base.group_system',
    )
    ai_current_month_tokens = fields.Integer(
        string="Used this month (tokens)", compute='_compute_ai_current_month_usage')
    ai_current_month_cost = fields.Float(
        string="Cost this month ($)", digits=(12, 2), compute='_compute_ai_current_month_usage')

    def _compute_ai_current_month_usage(self):
        usage = self.env['ai.token.usage'].sudo()._get_month_usage()
        for record in self:
            record.ai_current_month_tokens = usage['tokens']
            record.ai_current_month_cost = usage['cost']

    ai_chat_retention_days = fields.Integer(
        string="AI chat retention (days)",
        config_parameter='ai_chatbot_enhance.chat_retention_days',
        default=30,
        groups='base.group_system',
        help="Conversations with an AI assistant are kept for this many days after the "
             "last activity, then deleted automatically. 0 = keep forever. "
             "Abandoned empty chats are always cleaned after one day.",
    )

    def _compute_openai_key_enabled(self):
        for record in self:
            record.openai_key_enabled = bool(record.openai_key)

    def _compute_google_key_enabled(self):
        for record in self:
            record.google_key_enabled = bool(record.google_key)

    def _compute_deepseek_key_enabled(self):
        for record in self:
            record.deepseek_key_enabled = bool(record.deepseek_key)

    def _compute_qwen_key_enabled(self):
        for record in self:
            record.qwen_key_enabled = bool(record.qwen_key)

    def _compute_kimi_key_enabled(self):
        for record in self:
            record.kimi_key_enabled = bool(record.kimi_key)

    def _compute_zhipu_key_enabled(self):
        for record in self:
            record.zhipu_key_enabled = bool(record.zhipu_key)
