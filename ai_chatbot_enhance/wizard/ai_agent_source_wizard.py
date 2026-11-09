# -*- coding: utf-8 -*-
from odoo import models, fields, api, _
from odoo.exceptions import UserError


class AIAgentSourceWizard(models.TransientModel):
    _name = 'ai.agent.source.wizard'
    _description = 'Wizard to add sources to AI Agent'

    # 关联当前的 Agent
    agent_id = fields.Many2one('ai.agent.bot', string="Agent", required=True)

    # 用户选择的输入模式
    source_type = fields.Selection([
        ('binary', 'Upload File'),
        ('url', 'Add URL(s)')
    ], string="Source Type", default='binary', required=True)

    # 文件上传相关字段
    file_data = fields.Binary(string="File")
    file_name = fields.Char(string="File Name")

    # URL 输入相关字段
    urls = fields.Text(string="URLs", help="Enter one URL per line.")

    def action_confirm(self):
        self.ensure_one()
        source_env = self.env['ai.en.agent.source']

        if self.source_type == 'binary':
            if not self.file_data:
                raise UserError(_("Please upload a file."))

            source_env.create_from_ai_binary_files(
                [{'name': self.file_name, 'datas': self.file_data}],
                self.agent_id.id
            )

            attachment = self.env['ir.attachment'].search(
                [('name', '=', self.file_name)], limit=1, order='id desc'
            )

        elif self.source_type == 'url':
            if not self.urls:
                raise UserError(_("Please enter at least one URL."))

            url_list = [u.strip() for u in self.urls.split('\n') if u.strip()]
            valid_urls = [u for u in url_list if u.startswith(('http://', 'https://', 'ftp://'))]

            if not valid_urls:
                raise UserError(_("No valid URLs found. URLs must start with http://, https://, or ftp://"))

            source_env.create_from_urls(valid_urls, self.agent_id.id)

        return {'type': 'ir.actions.client', 'tag': 'reload'}
