from odoo import fields, models, _, api
from odoo.exceptions import UserError

INTERFACE_KEYS = [
    ("html_field_record", "Write in an HTML field"),
    ("mail_composer", "Write an email"),
    ("html_field_text_select", "Rewrite content"),
    ("chatter_ai_button", "Get help on a record"),
    ("html_prompt_shortcut", "Convert a prompt in an email"),
    ("systray_ai_button", "Ask AI for help"),
    ("voice_transcription_component", "Summary Buttons for Voice Transcription Component")
]


class AIComposer(models.Model):
    _name = "ai.en.composer"
    _description = "AI model configurations (system prompts) for text drafting."

    def _get_default_agent(self):
        return self.env["ir.model.data"]._xmlid_to_res_id("ai.ai_default_agent")

    name = fields.Char(
        "Rule Name", help="The identifier for the interface component to agent rule", required=True,
    )
    interface_key = fields.Selection(selection=INTERFACE_KEYS, string="Action", required=True)
    focused_models = fields.Many2many('ir.model', string="Models")
    ai_agent = fields.Many2one('ai.agent.bot', string="Agent", default=_get_default_agent)
    default_prompt = fields.Text("Instructions")
    is_system_default = fields.Boolean('Is the rule a system default or user created', default=False, readonly=True, copy=False)
    available_prompts = fields.One2many('ai.en.prompt.button', 'composer_id', string="Available User Prompts")

    @api.ondelete(at_uninstall=False)
    def _unlink_except_default_rules(self):
        if any(rule.is_system_default for rule in self):
            raise UserError(self.env._('System default prompts cannot be removed.'))

    def copy_data(self, default=None):
        default = dict(default or {})
        vals_list = super().copy_data(default=default)
        if 'name' not in default:
            for composer, vals in zip(self, vals_list):
                vals['name'] = _("%s (copy)", composer.name)
        return vals_list

    def get_prompts_by_agent(self, ai_en_agent_id):
        ai_agent = self.env['ai.agent.bot'].sudo().search([('id', '=', ai_en_agent_id)])

    @api.model
    def get_prompts_by_agent(self, ai_en_agent_id):
        """
        根据前端传过来的 ai_en_agent_id，查找对应的 ai.en.composer 记录，
        并返回其关联的 available_prompts 按钮数据（带前端兼容映射）。
        """
        if not ai_en_agent_id:
            return []

        composer = self.search([('ai_agent', '=', ai_en_agent_id)], limit=1)
        if not composer:
            return []

        prompts_data = composer.available_prompts.read(['id', 'name', 'sequence'])

        for prompt in prompts_data:
            prompt_text = prompt.get('name') or ''
            prompt['text'] = prompt_text
            prompt['content'] = prompt_text

        return prompts_data

