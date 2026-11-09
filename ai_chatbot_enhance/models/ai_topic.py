from odoo import fields, models

from ..utils.ai_logging import get_ai_logging_session


class AITopic(models.Model):
    _name = 'ai.en.topic'
    _description = "Create tools to direct Odoo AI in assisting the user with their tasks."

    name = fields.Char(string="Title", required=True)
    description = fields.Text(
        string="Description",
        help="One-line summary shown in the agent's always-loaded skill index. "
             "The AI uses it to decide when to load the detailed guide.",
    )
    instructions = fields.Text(
        string="Instructions",
        help="Short core rules, ALWAYS loaded into the agent context. Keep it small; "
             "put the full handbook in the Detailed Guide (loaded on demand).",
    )
    detailed_guide = fields.Text(
        string="Detailed Guide",
        help="Full domain handbook (workflows, tool usage details, edge cases). "
             "NOT loaded by default — the AI pulls it with the get_skill_guide tool "
             "when the task requires this skill (progressive disclosure).",
    )
    tool_ids = fields.Many2many('ir.actions.server', string="AI Tools", domain=[('use_in_ai', '=', True)],
                                groups='ai_chatbot_enhance.group_ai_manager')

    def ai_get_skill_guide(self, skill_name):
        """Return the detailed guide of the skill matching `skill_name` and
        mark it as loaded for the current AI turn (mutation tools flagged with
        `ai_tool_requires_guide` stay blocked until this happened).

        Called from the get_skill_guide meta-tool (safe_eval).
        """
        agent_id = self.env.context.get('ai_en_agent_id')
        if agent_id:
            topics = self.env['ai.agent.bot'].sudo().browse(agent_id).topic_ids
        else:
            topics = self.sudo().search([])
        candidates = topics.filtered(lambda t: (t.detailed_guide or '').strip())

        needle = (skill_name or '').strip().lower()
        topic = None
        if needle:
            topic = next((t for t in candidates if needle == t.name.strip().lower()), None) \
                or next((t for t in candidates if needle in t.name.lower()), None)
        if not topic and len(candidates) == 1:
            topic = candidates[0]
        if not topic:
            names = ", ".join(candidates.mapped('name')) or "(none)"
            return f"ERROR: no skill guide found matching '{skill_name}'. Available skill guides: {names}"

        session = get_ai_logging_session()
        if session is not None:
            session.setdefault('ai_skill_guides', set()).add(topic.id)

        return (
            f"SKILL GUIDE LOADED: {topic.name}\n"
            "(Mutation tools are now unlocked for this turn. Follow this guide exactly.)\n\n"
            f"{topic.detailed_guide}"
        )
