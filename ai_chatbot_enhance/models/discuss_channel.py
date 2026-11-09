import logging
import random

from markupsafe import Markup

from odoo import _, fields, models, api
from odoo.tools.mail import html_to_inner_content
from odoo.exceptions import AccessError
from odoo.fields import Domain

from odoo.addons.mail.tools.discuss import Store
import base64
from hashlib import sha512

_logger = logging.getLogger(__name__)

DEFAULT_CHAT_RETENTION_DAYS = 30

channel_avatar = '''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 530.06 530.06">
<rect width="530.06" height="530.06" fill="#875a7b"/>
<path d="M416.74,217.29l5-28a8.4,8.4,0,0,0-8.27-9.88H361.09l10.24-57.34a8.4,8.4,0,0,0-8.27-9.88H334.61a8.4,8.4,0,0,0-8.27,6.93L315.57,179.4H246.5l10.24-57.34a8.4,8.4,0,0,0-8.27-9.88H220a8.4,8.4,0,0,0-8.27,6.93L201,179.4H145.6a8.42,8.42,0,0,0-8.28,6.93l-5,28a8.4,8.4,0,0,0,8.27,9.88H193l-16,89.62H121.59a8.4,8.4,0,0,0-8.27,6.93l-5,28a8.4,8.4,0,0,0,8.27,9.88H169L158.73,416a8.4,8.4,0,0,0,8.27,9.88h28.45a8.42,8.42,0,0,0,8.28-6.93l10.76-60.29h69.07L273.32,416a8.4,8.4,0,0,0,8.27,9.88H310a8.4,8.4,0,0,0,8.27-6.93l10.77-60.29h55.38a8.41,8.41,0,0,0,8.28-6.93l5-28a8.4,8.4,0,0,0-8.27-9.88H337.08l16-89.62h55.38A8.4,8.4,0,0,0,416.74,217.29ZM291.56,313.84H222.5l16-89.62h69.07Z" fill="#ffffff"/>
</svg>'''
group_avatar = '''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 530.06 530.06">
<rect width="530.06" height="530.06" fill="#875a7b"/>
<path d="m184.356059,265.030004c-23.740561,0.73266 -43.157922,10.11172 -58.252302,28.136961l-29.455881,0c-12.0169,0 -22.128621,-2.96757 -30.335161,-8.90271s-12.309921,-14.618031 -12.309921,-26.048671c0,-51.730902 9.08582,-77.596463 27.257681,-77.596463c0.87928,0 4.06667,1.53874 9.56217,4.61622s12.639651,6.19167 21.432451,9.34235s17.512401,4.72613 26.158581,4.72613c9.8187,0 19.563981,-1.68536 29.236061,-5.05586c-0.73266,5.4223 -1.0991,10.25834 -1.0991,14.508121c0,20.370061 5.93514,39.127962 17.805421,56.273922zm235.42723,140.025346c0,17.585601 -5.34888,31.470971 -16.046861,41.655892s-24.912861,15.277491 -42.645082,15.277491l-192.122688,0c-17.732221,0 -31.947101,-5.09257 -42.645082,-15.277491s-16.046861,-24.070291 -16.046861,-41.655892c0,-7.7669 0.25653,-15.350691 0.76937,-22.751371s1.53874,-15.387401 3.07748,-23.960381s3.48041,-16.523211 5.82523,-23.850471s5.4955,-14.471411 9.45226,-21.432451s8.49978,-12.89618 13.628841,-17.805421c5.12906,-4.90924 11.393931,-8.82951 18.794611,-11.76037s15.570511,-4.3964 24.509931,-4.3964c1.46554,0 4.61622,1.57545 9.45226,4.72613s10.18492,6.6678 16.046861,10.55136c5.86194,3.88356 13.702041,7.40068 23.520741,10.55136s19.710601,4.72613 29.675701,4.72613s19.857001,-1.57545 29.675701,-4.72613s17.658801,-6.6678 23.520741,-10.55136c5.86194,-3.88356 11.21082,-7.40068 16.046861,-10.55136s7.98672,-4.72613 9.45226,-4.72613c8.93942,0 17.109251,1.46554 24.509931,4.3964s13.665551,6.85113 18.794611,11.76037c5.12906,4.90924 9.67208,10.844381 13.628841,17.805421s7.10744,14.105191 9.45226,21.432451s4.28649,15.277491 5.82523,23.850471s2.56464,16.559701 3.07748,23.960381s0.76937,14.984471 0.76937,22.751371zm-225.095689,-280.710152c0,15.534021 -5.4955,28.796421 -16.486501,39.787422s-24.253401,16.486501 -39.787422,16.486501s-28.796421,-5.4955 -39.787422,-16.486501s-16.486501,-24.253401 -16.486501,-39.787422s5.4955,-28.796421 16.486501,-39.787422s24.253401,-16.486501 39.787422,-16.486501s28.796421,5.4955 39.787422,16.486501s16.486501,24.253401 16.486501,39.787422zm154.753287,84.410884c0,23.300921 -8.24325,43.194632 -24.729751,59.681133s-36.380212,24.729751 -59.681133,24.729751s-43.194632,-8.24325 -59.681133,-24.729751s-24.729751,-36.380212 -24.729751,-59.681133s8.24325,-43.194632 24.729751,-59.681133s36.380212,-24.729751 59.681133,-24.729751s43.194632,8.24325 59.681133,24.729751s24.729751,36.380212 24.729751,59.681133zm126.616325,49.459502c0,11.43064 -4.10338,20.113531 -12.309921,26.048671s-18.318261,8.90271 -30.335161,8.90271l-29.455881,0c-15.094381,-18.025241 -34.511741,-27.404301 -58.252302,-28.136961c11.87028,-17.145961 17.805421,-35.903862 17.805421,-56.273922c0,-4.24978 -0.36644,-9.08582 -1.0991,-14.508121c9.67208,3.3705 19.417361,5.05586 29.236061,5.05586c8.64618,0 17.365781,-1.57545 26.158581,-4.72613s15.936951,-6.26487 21.432451,-9.34235s8.68289,-4.61622 9.56217,-4.61622c18.171861,0 27.257681,25.865561 27.257681,77.596463zm-28.136961,-133.870386c0,15.534021 -5.4955,28.796421 -16.486501,39.787422s-24.253401,16.486501 -39.787422,16.486501s-28.796421,-5.4955 -39.787422,-16.486501s-16.486501,-24.253401 -16.486501,-39.787422s5.4955,-28.796421 16.486501,-39.787422s24.253401,-16.486501 39.787422,-16.486501s28.796421,5.4955 39.787422,16.486501s16.486501,24.253401 16.486501,39.787422z" fill="#ffffff"/>
</svg>'''


def get_hsl_from_seed(seed):
    hashed_seed = sha512(seed.encode()).hexdigest()
    # full range of colors, in degree
    hue = int(hashed_seed[0:2], 16) * 360 / 255
    # colorful result but not too flashy, in percent
    sat = int(hashed_seed[2:4], 16) * ((70 - 40) / 255) + 40
    # not too bright and not too dark, in percent
    lig = 45
    return f'hsl({hue:.0f}, {sat:.0f}%, {lig:.0f}%)'


def is_ai_chat_channel(channel):
    """Predicate to filter channels for which the channel type is 'ai_chat'.

    :returns: Whether the channel is an ai_chat channel.
    :rtype: bool
    """
    return channel.channel_type == "ai_chat"


class DiscussChannel(models.Model):
    _name = "discuss.channel"
    _inherit = ["discuss.channel"]

    channel_type = fields.Selection(
        selection_add=[("ai_chat", "AI chat")],
        ondelete={"ai_chat": "cascade"},
    )
    ai_env_context = fields.Json("Context for AI agent")
    ai_stop_request_dt = fields.Datetime(
        "AI Stop Requested At",
        help="Set when the user clicks Stop while the AI is generating. The "
             "background generation loop compares it with its own start time "
             "and aborts the turn (works across workers — it goes through "
             "the database, not process memory).",
    )
    # ai_agent_id = fields.Many2one("ai.agent", index="btree_not_null", groups=fields.NO_ACCESS)
    # ai_agent_id = fields.Many2one("ai.agent", index="btree_not_null")
    ai_en_agent_id = fields.Many2one("ai.agent.bot", index="btree_not_null")

    _ai_channel_type_check = models.Constraint(
        "CHECK(ai_en_agent_id IS NULL or channel_type = 'ai_chat' or channel_type = 'livechat')",
        'AI Agent can only be set for ai_chat or livechat channels.',
    )

    def ai_set_option_choice(self, message_id, option):
        """Persist the quick-reply option the user clicked as a small footnote
        appended to the AI message body. Returns the updated body HTML (or
        False when nothing was written)."""
        self.ensure_one()
        if self.channel_type != 'ai_chat':
            return False
        if self.env.user.partner_id not in self.sudo().channel_partner_ids:
            return False
        message = self.env['mail.message'].sudo().browse(int(message_id)).exists()
        if not message or message.model != 'discuss.channel' or message.res_id != self.id:
            return False
        option = str(option or '').strip()[:200]
        if not option:
            return False
        # Markup() wrap is critical: plain-str + Markup concatenation would
        # HTML-escape the whole existing body (tags shown as literal text)
        body = Markup(message.body) if message.body else Markup('')
        if 'o_ai_option_chosen' in str(body):
            # already answered: keep the first choice
            return str(body)
        message.write({
            'body': body + Markup('<div class="o_ai_option_chosen">{}</div>').format(
                self.env._("Selected: %s", option)
            ),
        })
        return str(message.body)

    def ai_request_stop(self):
        """Called from the chat UI: stop the AI generation currently running
        for this channel. Members only."""
        self.ensure_one()
        if self.channel_type != 'ai_chat':
            return False
        if self.env.user.partner_id not in self.sudo().channel_partner_ids:
            return False
        self.sudo().write({'ai_stop_request_dt': fields.Datetime.now()})
        return True

    def _ai_stop_requested_since(self, since_dt):
        """Whether the user clicked Stop after `since_dt` (the turn start).
        Reads the freshest committed value — READ COMMITTED shows the stop
        write as soon as the stop RPC commits, across workers."""
        self.ensure_one()
        self.invalidate_recordset(['ai_stop_request_dt'])
        stop_dt = self.sudo().ai_stop_request_dt
        return bool(stop_dt and stop_dt >= since_dt)

    def ai_get_session_usage(self):
        """Aggregated token usage of this AI conversation, for the chat footer.
        Members only; returns {} otherwise."""
        self.ensure_one()
        if self.env.user.partner_id not in self.sudo().channel_partner_ids:
            return {}
        rows = self.env['ai.token.usage'].sudo().search_read(
            [('channel_id', '=', self.id)], ['input_tokens', 'output_tokens', 'cost'])
        return {
            'turns': len(rows),
            'total_tokens': sum(r['input_tokens'] + r['output_tokens'] for r in rows),
            'cost': round(sum(r['cost'] for r in rows), 6),
        }

    def ai_preview_mail_template(self, model_name, res_id, template_search=''):
        """Render the official mail template for a record WITHOUT sending:
        recipient, subject, body (text extract) and attachment list.
        Used by the Send Document by Email skill for its confirmation preview.
        """
        self.ensure_one()
        if self.env.user.partner_id not in self.sudo().channel_partner_ids:
            return "ERROR: you are not a member of this channel."
        if model_name not in self.env:
            return f"ERROR: Model '{model_name}' does not exist."
        record = self.env[model_name].browse(int(res_id)).exists()
        if not record:
            return f"ERROR: No record with ID {res_id} in {model_name}."
        try:
            record.check_access('read')
        except Exception:  # noqa: BLE001
            return "ERROR: you don't have access to this record."

        Template = self.env['mail.template'].sudo()
        template = None
        if template_search:
            template = Template.search(
                [('model', '=', model_name), ('name', 'ilike', template_search)], limit=1)
        if not template:
            template = Template.search([('model', '=', model_name)], limit=1)
        if not template:
            return f"ERROR: No email template exists for {model_name}."

        try:
            subject = template._render_field('subject', [record.id])[record.id]
            body_html = template._render_field('body_html', [record.id])[record.id]
        except Exception as e:  # noqa: BLE001
            return f"ERROR: failed to render template '{template.name}': {e}"

        body_text = html_to_inner_content(body_html or '')
        if len(body_text) > 1200:
            body_text = body_text[:1200] + "..."

        recipient = ""
        if 'partner_id' in record._fields and record.partner_id:
            partner = record.partner_id
            recipient = f"{partner.display_name} <{partner.email or 'NO EMAIL — fix the contact first!'}>"

        attachments = []
        if 'report_template_ids' in template._fields:
            attachments += template.report_template_ids.mapped('name')
        attachments += template.attachment_ids.mapped('name')

        return (
            "EMAIL PREVIEW (nothing has been sent yet)\n"
            f"Template: {template.name}\n"
            f"Recipient: {recipient or '(recipients defined by the template)'}\n"
            f"Subject: {subject}\n"
            f"Attachments: {', '.join(attachments) if attachments else '(none)'}\n"
            f"--- BODY (text extract) ---\n{body_text}"
        )

    def ai_post_report_pdf(self, model_name, res_id, report_search=''):
        """Render a QWeb PDF report for a record and post it in this channel.

        Called from AI skill code (safe_eval cannot call the underscore
        rendering methods itself). Read access on the record is checked with
        the real user's rights before rendering.
        """
        self.ensure_one()
        if self.env.user.partner_id not in self.sudo().channel_partner_ids:
            return "ERROR: you are not a member of this channel."
        if model_name not in self.env:
            return f"ERROR: Model '{model_name}' does not exist."
        record = self.env[model_name].browse(int(res_id)).exists()
        if not record:
            return f"ERROR: No record with ID {res_id} in {model_name}."
        try:
            record.check_access('read')
        except Exception:  # noqa: BLE001
            return "ERROR: you don't have access to this record."

        domain = [('model', '=', model_name), ('report_type', '=', 'qweb-pdf')]
        if report_search:
            domain.append(('name', 'ilike', report_search))
        report = self.env['ir.actions.report'].sudo().search(domain, limit=1)
        if not report:
            available = self.env['ir.actions.report'].sudo().search(
                [('model', '=', model_name), ('report_type', '=', 'qweb-pdf')], limit=5)
            names = ", ".join(available.mapped('name')) or "(none)"
            return (
                f"ERROR: No PDF report found for {model_name}"
                f"{f' matching {report_search!r}' if report_search else ''}. "
                f"Available reports: {names}"
            )
        try:
            pdf_content, __ = self.env['ir.actions.report'].sudo()._render_qweb_pdf(
                report.report_name, res_ids=[record.id])
        except Exception as e:  # noqa: BLE001
            return f"ERROR: failed to render report '{report.name}': {e}"

        filename = f"{report.name} - {record.display_name}.pdf".replace('/', '-')
        attachment = self.env['ir.attachment'].sudo().create({
            'name': filename,
            'raw': pdf_content,
            'res_model': 'discuss.channel',
            'res_id': self.id,
            'mimetype': 'application/pdf',
        })
        self.sudo().message_post(
            body=self.env._("Report: %s", report.name),
            attachment_ids=[attachment.id],
            message_type='comment',
            subtype_xmlid='mail.mt_comment',
            silent=True,
        )
        return f"Report '{report.name}' for {record.display_name} was rendered and posted in this chat as a PDF."

    @api.model
    def create_ai_draft_channel(self, caller_component, channel_title=None, record_model=None, record_id=None,
                                front_end_info=None, text_selection=None, agent_id=None):
        ai_composer = None
        if record_model:
            ai_composer = self.env['ai.en.composer'].sudo().search([
                ('interface_key', '=', caller_component),
                ('focused_models', 'in', record_model),
            ], limit=1, order="create_date DESC")
        if not ai_composer:
            ai_composer = self.env['ai.en.composer'].sudo().search([
                ('interface_key', '=', caller_component),
                ('focused_models', '=', False),
            ], limit=1, order="create_date DESC")

        if agent_id:
            ai_agent = self.env['ai.agent.bot'].sudo().browse(int(agent_id))
        else:
            ai_agent = ai_composer.ai_agent if ai_composer else self.env['ai.agent.bot']

        if not ai_agent or not ai_agent.exists():
            raise AccessError(_("AI not reachable, AI Agent not found."))

        channel_name = self.env._("AI: %(name)s", name=channel_title) if channel_title else ai_agent.name
        channel = ai_agent._create_ai_chat_channel(channel_name=channel_name)
        model_context = []
        if composer_prompt := ai_composer.default_prompt:
            model_context.append(composer_prompt)

        model_has_thread = False
        if record_model:
            original_record = self.env[record_model].search([('id', '=', record_id)])
            model_context += original_record._ai_initialise_context(
                caller_component, text_selection, front_end_info
            )
            if isinstance(original_record, self.pool['mail.thread']):
                model_has_thread = True

        channel.ai_env_context = model_context

        prompts = ai_composer.available_prompts
        if caller_component == "chatter_ai_button" and not model_has_thread:
            chatter_prompts = {
                self.env.ref('ai.ai_prompt_summarize_chatter', raise_if_not_found=False),
                self.env.ref('ai.ai_prompt_write_followup_chatter', raise_if_not_found=False),
            }
            prompts = [p for p in prompts if p not in chatter_prompts]
        random_prompts = random.sample(prompts, min(7, len(prompts)))

        return {
            "ai_channel_id": channel.id,
            "data": Store().add(channel).get_result(),
            "prompts": [prompt.name for prompt in random_prompts],
            "model_has_thread": model_has_thread,
        }

    def _ai_has_user_messages(self):
        """Whether a real conversation happened here (any non-agent comment)."""
        self.ensure_one()
        agent_partner = self.sudo().ai_en_agent_id.partner_id
        return bool(self.env['mail.message'].sudo().search_count([
            ('model', '=', 'discuss.channel'),
            ('res_id', '=', self.id),
            ('message_type', '=', 'comment'),
            ('author_id', '!=', agent_partner.id),
        ], limit=1))

    @api.autovacuum
    def _remove_ai_chat_channels(self):
        """Two-tier cleanup of AI chats:

        - ABANDONED DRAFTS (opened but the user never wrote anything):
          removed after 1 day of inactivity — they are just clutter.
        - REAL CONVERSATIONS: kept for the configurable retention period
          (ir.config_parameter 'ai_chatbot_enhance.chat_retention_days',
          default 30 days; 0 = keep forever).
        """
        base_domain = (
            Domain("ai_en_agent_id", "!=", False)
            & Domain('channel_type', '=', 'ai_chat')
        )
        to_unlink = self.env['discuss.channel'].sudo()

        # Tier 1: empty drafts older than one day
        for channel in self.sudo().search(base_domain & Domain('last_interest_dt', '<', '-1d')):
            if not channel._ai_has_user_messages():
                to_unlink |= channel

        # Tier 2: real conversations beyond the retention period
        param = self.env['ir.config_parameter'].sudo().get_param(
            'ai_chatbot_enhance.chat_retention_days', str(DEFAULT_CHAT_RETENTION_DAYS))
        try:
            retention_days = max(0, int(param))
        except ValueError:
            retention_days = DEFAULT_CHAT_RETENTION_DAYS
        if retention_days:
            to_unlink |= self.sudo().search(
                base_domain & Domain('last_interest_dt', '<', f'-{retention_days}d'))

        if to_unlink:
            _logger.info("AI chat cleanup: removing %d channels (retention: %s days)",
                         len(to_unlink), retention_days or 'unlimited')
            to_unlink.unlink()

    def _to_store_defaults(self, target):
        return super()._to_store_defaults(target) + [Store.One("ai_en_agent_id", predicate=is_ai_chat_channel, sudo=True)]

    def _sync_field_names(self):
        field_names = super()._sync_field_names()
        field_names[None].append(Store.One("ai_en_agent_id", predicate=is_ai_chat_channel, sudo=True))
        return field_names

    def _generate_avatar(self):
        if self.channel_type not in ('channel', 'group', 'ai_chat'):
            return False
        avatar = group_avatar if self.channel_type == 'group' else channel_avatar
        bgcolor = get_hsl_from_seed(self.uuid)
        avatar = avatar.replace('fill="#875a7b"', f'fill="{bgcolor}"')
        return base64.b64encode(avatar.encode())
