import requests
import json
import logging
from odoo import http
from odoo.http import request
from odoo.addons.mail.controllers.thread import ThreadController

_logger = logging.getLogger(__name__)


class AIControllerTest(ThreadController):
    # 请替换为您自己的 DeepSeek API Key
    DEEPSEEK_API_KEY = 1

    @http.route('/ai/generate_response_test', type="jsonrpc", auth="public")
    def generate_response_test(self, mail_message_id, channel_id, **kwargs):
        """接收前端发送的消息ID，调用DeepSeek并回复"""
        message = request.env['mail.message'].sudo().browse(int(mail_message_id)).exists()
        if not message:
            return {'error': 'Message not found'}

        channel = request.env['discuss.channel'].sudo().browse(int(channel_id)).exists()
        if not channel:
            return {'error': 'Channel not found'}

        # 2. 避免 AI 回复自己产生的消息（防止死循环）
        ai_bot_partner = self._get_ai_bot_partner()
        if message.author_id.id == ai_bot_partner.id:
            return {'status': 'skipped, bot message'}

        user_input = message.body
        if not user_input:
            return {'error': 'Empty message'}

        # 3. 调用 DeepSeek API
        try:
            ai_response = self._call_deepseek_api(user_input)

        except Exception as e:
            _logger.exception("DeepSeek API error")
            ai_response = f"❌ AI 服务出错：{str(e)}"

        # 4. 将 AI 回复发送到频道
        channel.sudo().message_post(
            body=ai_response,
            message_type='comment',
            silent=True,
            author_id=ai_bot_partner.id,
            subtype_xmlid='mail.mt_comment'
        )
        return {'status': 'ok', 'response': ai_response}

    def _get_ai_bot_partner(self):
        """获取或创建 AI 机器人伙伴（用于标记AI回复）"""
        Partner = request.env['res.partner'].sudo()
        bot = Partner.search([('agent_ids', '!=', False), ('active', '=', False)], limit=1)
        print(bot, 888888)
        if not bot:
            bot = Partner.create({
                'name': 'DeepSeek AI',
                'email': 'deepseek.bot@localhost',
                'user_id': None,
                'im_status': 'agent',
                'company_id': self.env.company.id,
            })
        return bot

    def _call_deepseek_api(self, prompt):
        """调用 DeepSeek API 并返回文本回复"""
        url = "https://api.deepseek.com/v1/chat/completions"
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.DEEPSEEK_API_KEY}",
        }
        payload = {
            "model": "deepseek-chat",
            "messages": [
                {"role": "user", "content": prompt}
            ],
            "stream": False,  # 简单模式，非流式
            "temperature": 0.7,
        }

        response = requests.post(url, headers=headers, json=payload, timeout=30)
        response.raise_for_status()
        data = response.json()

        # 提取回复内容
        if 'choices' in data and len(data['choices']) > 0:
            return data['choices'][0]['message']['content']
        else:
            return "⚠️ DeepSeek 返回了空响应，请检查配置。"
