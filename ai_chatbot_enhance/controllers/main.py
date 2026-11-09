import logging
from werkzeug.exceptions import NotFound
from odoo import http
from odoo.addons.mail.tools.discuss import add_guest_to_context
from odoo.addons.mail.controllers.thread import ThreadController

_logger = logging.getLogger(__name__)


class AIController(ThreadController):

    @http.route(["/ai/get_ai_response"], type="jsonrpc", auth="public")
    # @add_guest_to_context
    def get_ai_response(self, mail_message_id, channel_id, **kwargs):
        # Returns immediately: the response is generated in a background
        # thread and pushed to the client by the bus when posted (a long
        # agent loop must not be killed with the HTTP request).
        channel = self._get_ai_channel_from_id(channel_id)
        if not channel:
            raise NotFound()
        message = self._get_message_with_access(mail_message_id)
        if not message:
            return False
        return channel.sudo().ai_en_agent_id._ai_spawn_response_job(message, channel)

    def _get_ai_channel_from_id(self, channel_id):
        channel = self.env['discuss.channel'].search([('id', '=', channel_id)])
        if channel.sudo().ai_en_agent_id:
            return channel
        return self.env['discuss.channel']

    @http.route('/ai/close_chat_ai', methods=["POST"], type="jsonrpc", auth='public')
    @add_guest_to_context
    def close_ai_chat(self, channel_id):
        # Closing the window only deletes ABANDONED drafts (no user message).
        # Real conversations are kept and cleaned later by the retention
        # autovacuum (ai_chatbot_enhance.chat_retention_days).
        channel = self._get_ai_channel_from_id(channel_id)
        if channel and not channel.sudo()._ai_has_user_messages():
            channel.sudo().unlink()
