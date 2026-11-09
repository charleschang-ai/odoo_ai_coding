# -*- coding: utf-8 -*-
from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError

from odoo.addons.mail.tools.discuss import Store


class DiscussChannel(models.Model):
    """RPC surface of the AI Platform page.

    All conversations are plain ``discuss.channel`` records with
    ``channel_type = 'ai_chat'`` created by *ai_chatbot_enhance*, so they stay
    visible in the regular Discuss chat windows too.
    """
    _inherit = "discuss.channel"

    # ------------------------------------------------------------------
    # Bootstrap data for the page
    # ------------------------------------------------------------------
    @api.model
    def ai_platform_get_data(self):
        # same permission gate as the systray list: empty allowed_group_ids
        # means everyone, otherwise the user needs one of the groups
        agents = self.env["ai.agent.bot"].search([]).filtered(
            lambda a: not a.sudo().allowed_group_ids
            or (self.env.user.all_group_ids & a.sudo().allowed_group_ids)
        )
        composer = self.env["ai.en.composer"].sudo().search(
            [("interface_key", "=", "systray_ai_button")],
            limit=1, order="create_date DESC")
        default_agent = composer.ai_agent if composer and composer.ai_agent in agents else agents[:1]
        suggestions = composer.available_prompts.sorted("sequence").mapped("name")[:8] if composer else []
        return {
            "agents": [{
                "id": agent.id,
                "name": agent.name or "",
                "subtitle": agent.subtitle or "",
                "model": agent.llm_model or "",
            } for agent in agents],
            "default_agent_id": default_agent.id or False,
            "conversations": self.ai_platform_get_conversations(),
            "suggestions": suggestions,
            "user_name": self.env.user.name,
            "can_view_all": self._ai_platform_can_view_all(),
        }

    # ------------------------------------------------------------------
    # Conversation list
    # ------------------------------------------------------------------
    @api.model
    def _ai_platform_can_view_all(self):
        """Settings administrators may list every user's AI conversation —
        the same population Odoo's core rule "discuss.channel: admin full
        access" already grants them (no sudo involved)."""
        return self.env.user.has_group("base.group_system")

    @api.model
    def _ai_platform_conv_domain(self, all_users=False):
        domain = [("channel_type", "=", "ai_chat")]
        if not (all_users and self._ai_platform_can_view_all()):
            domain.append(("is_member", "=", True))
        return domain

    def _ai_platform_conv_owner(self):
        """Display name of the human member(s) of an AI chat (the agent's
        partner and guests excluded) + whether the current user is one."""
        self.ensure_one()
        agent_partner = self.sudo().ai_en_agent_id.partner_id
        humans = self.sudo().channel_partner_ids - agent_partner
        me = self.env.user.partner_id
        return ", ".join(humans.mapped("name")), me in humans

    @api.model
    def ai_platform_get_conversations(self, all_users=False):
        channels = self.search(
            self._ai_platform_conv_domain(all_users),
            order="last_interest_dt DESC, id DESC", limit=200)
        result = []
        for channel in channels:
            agent = channel.sudo().ai_en_agent_id
            owner_name, is_mine = channel._ai_platform_conv_owner()
            result.append({
                "id": channel.id,
                "name": channel.name or "",
                "agent_id": agent.id,
                "agent_name": agent.name or "",
                "agent_model": agent.llm_model or "",
                "owner_name": owner_name,
                "is_mine": is_mine,
                "last_interest_dt": fields.Datetime.to_string(channel.last_interest_dt) if channel.last_interest_dt else "",
            })
        return result

    # ------------------------------------------------------------------
    # Conversation lifecycle
    # ------------------------------------------------------------------
    @api.model
    def ai_platform_new_conversation(self, agent_id, name=None):
        # NO sudo here: _create_ai_chat_channel adds env.user's partner as member
        agent = self.env["ai.agent.bot"].browse(int(agent_id)).exists()
        if not agent:
            raise UserError(_("AI agent not found. Please configure an agent first."))
        channel = agent._create_ai_chat_channel(channel_name=(name or "").strip()[:120] or None)
        composer = self.env["ai.en.composer"].sudo().search(
            [("ai_agent", "=", agent.id)], limit=1, order="create_date DESC")
        if composer and composer.default_prompt:
            channel.sudo().ai_env_context = [composer.default_prompt]
        return {
            "channel_id": channel.id,
            "store_data": Store().add(channel).get_result(),
        }

    def _ai_platform_check_member(self):
        # Settings admins pass too: they already have full access to every
        # channel through Odoo's core record rule (see _ai_platform_can_view_all)
        is_admin = self._ai_platform_can_view_all()
        for channel in self:
            if channel.channel_type != "ai_chat":
                raise AccessError(_("This is not an AI conversation."))
            if not is_admin and self.env.user.partner_id not in channel.sudo().channel_partner_ids:
                raise AccessError(_("You are not a member of this AI conversation."))

    def ai_platform_rename(self, name):
        self.ensure_one()
        self._ai_platform_check_member()
        name = (name or "").strip()[:120]
        if name:
            # sudo() => members may rename their own AI chat
            self.sudo().write({"name": name})
        return name

    def ai_platform_delete(self):
        self._ai_platform_check_member()
        # sudo() => members may drop their own AI chat (same policy as the
        # retention autovacuum of ai_chatbot_enhance)
        self.sudo().unlink()
