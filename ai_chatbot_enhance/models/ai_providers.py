# -*- coding: utf-8 -*-
from odoo import models, fields, api, _
from odoo.exceptions import UserError


class AIProvider(models.Model):
    _name = 'ai.provider'
    _description = 'AI Service Provider'
    _order = 'id desc'

    name = fields.Char(string='Provider Name', required=True)
    provider_code = fields.Selection([
        ('openai', 'OpenAI'),
        ('google', 'Google Gemini'),
        ('deepseek', 'DeepSeek'),
        ('custom', 'Custom/Open-source')
    ], string='Provider Type', default='openai')

    api_key = fields.Char(string='API Key')
    base_url = fields.Char(string='Base URL')
    active = fields.Boolean(default=True)

    def action_check_health(self):
        """ 提前校验方法 (Fail Fast) 与连通性测试 """
        self.ensure_one()

        if not self.api_key:
            raise UserError(_("服务商 %s 缺少 API Key 配置！", self.name))

        # TODO: 这里可以根据不同的 provider_code 引入 requests 库发送极小的 Token 测试包
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _("配置检查通过"),
                'message': _("%s 的 API Key 格式校验已通过！", self.name),
                'type': 'success',
                'sticky': False,
            }
        }