# Part of custom AI module.
import base64
import json
import re
import threading
from datetime import datetime, timedelta
import logging
from ast import literal_eval
from collections import defaultdict

from lxml import etree

from odoo import api, fields, models, modules, Command, _
from odoo.modules.registry import Registry
from odoo.fields import Domain
from odoo.exceptions import UserError, ValidationError
from odoo.tools import file_open
from odoo.tools.misc import mute_logger, submap
from odoo.tools import file_open, html_sanitize, SQL, is_html_empty, ormcache
from odoo.tools.mail import html_to_inner_content
from ..utils.llm_api_service import LLMApiService
from textwrap import dedent
from ..utils.ai_citation import apply_numeric_citations, get_attachment_ids_from_text
from ..utils.doc_extractor import extract_ai_files


try:
    from markdown2 import markdown
except ImportError:
    markdown = None

from ..utils.llm_providers import (
    PROVIDERS,
    get_provider,

)
from markupsafe import Markup  # ← 加這行

_logger = logging.getLogger(__name__)

# In-flight background AI generations, keyed by channel id (per server
# process): prevents double generation when the user sends again while a
# turn is still running.
_AI_JOBS_LOCK = threading.Lock()
_AI_JOBS_RUNNING = set()

TEMPERATURE_MAP = {
    'analytical': 0.1,
    'balanced': 0.5,
    'creative': 0.9,
}

# Marker the LLM appends to offer clickable quick-reply options in the chat
AI_OPTIONS_RE = re.compile(r'\[AI_OPTIONS\](.*?)\[/AI_OPTIONS\]', re.DOTALL)
AI_OPTIONS_MAX = 6
# Marker the LLM appends to show an inline input form (multiple values at once)
AI_FORM_RE = re.compile(r'\[AI_FORM\](.*?)\[/AI_FORM\]', re.DOTALL)
AI_FORM_MAX_FIELDS = 8

# Server-side safety net: when the LLM asks for a confirmation but forgot the
# [AI_OPTIONS] block, we detect the intent in the text and inject default
# confirm/cancel buttons so the user always gets clickable cards.
AI_CONFIRM_INTENT_RE = re.compile(
    r'(是否(确认|执行|继续|同意|需要|正确|无误)'
    r'|请.{0,4}确认'
    r'|确认(无误|一下|吗|后(再|执行|继续))'
    r'|(需要|要)我.{0,20}吗'
    r'|要不要'
    r'|(执行|继续|创建|新建|删除|修改|更新|发送|寄出|归档|生成|下单|操作)吗'
    r'|可以(执行|继续|操作|发送)吗'
    r'|please\s+confirm|confirm\s+(to|before|this|the)\b'
    r'|shall\s+i\b|should\s+i\s+(proceed|go|continue|create|send|update|delete)'
    r'|do\s+you\s+want\s+me\s+to|would\s+you\s+like\s+me\s+to'
    r'|(ok(ay)?|ready)\s+to\s+proceed|proceed\s*\?|go\s+ahead\s*\?'
    r'|is\s+(this|that|it)\s+(correct|right|ok(ay)?)\b'
    r')',
    re.IGNORECASE)
# Words hinting the pending question is about a data mutation (adds a "modify"
# option to the fallback buttons, matching the mutation confirmation protocol)
AI_MUTATION_HINT_RE = re.compile(
    r'(创建|新建|修改|更新|删除|归档|发送|寄出|生成|执行|下单|确认订单'
    r'|create|update|write|delete|archive|send|generate|execute)',
    re.IGNORECASE)
AI_CJK_RE = re.compile(r'[一-鿿]')
# Strong mutation-confirmation phrasing: a preview awaiting approval
AI_STRONG_CONFIRM_RE = re.compile(
    r'(请.{0,4}确认|是否(确认|执行)|确认(无误|后)|请核对'
    r'|please\s+confirm|confirm\s+(to|before)|shall\s+i\s+proceed)',
    re.IGNORECASE)
# Wrap-up offer ("anything else I can do?"): needs NO buttons at all
AI_WRAPUP_OFFER_RE = re.compile(
    r'(其他|别的|另外).{0,6}(操作|帮助|需要|需求|事情|问题)'
    r'|还(需要|想|有).{0,10}(帮|吗|什么)'
    r'|anything\s+else|something\s+else|further\s+(help|assistance)'
    r'|other\s+(operations?|actions?|help)',
    re.IGNORECASE)
# A bullet / numbered menu line, e.g. "* 📧 发送报价单邮件给客户" or "1. 创建发票"
AI_MENU_ITEM_RE = re.compile(r'^\s*(?:[-*•]|\d{1,2}[.、))])\s+(.+?)\s*$', re.MULTILINE)
AI_MENU_QUESTION_RE = re.compile(
    r'[??吗]|想要|需要|选择|哪|什么|告诉我|which|what\s+would|choose|select|tell\s+me',
    re.IGNORECASE)


def _extract_menu_options(text):
    """When the reply offers a MENU of follow-up actions as a bullet list plus
    a question, turn the list items themselves into the quick-reply options.
    Returns [] when the text does not look like a menu."""
    if not AI_MENU_QUESTION_RE.search(text):
        return []
    if AI_STRONG_CONFIRM_RE.search(text):
        # a mutation preview awaiting approval, not a menu
        return []
    items = [
        item for item in AI_MENU_ITEM_RE.findall(text)
        if 2 <= len(item) <= 60
    ]
    if not 2 <= len(items) <= 8:
        return []
    return items[:AI_OPTIONS_MAX]


def _fallback_confirmation_options(message):
    """Default quick-reply options when the reply asks the user something but
    carries no [AI_OPTIONS]/[AI_FORM] block. Menu-style replies get their own
    list items as buttons; confirmation questions get confirm/cancel; wrap-up
    offers ("anything else?") get none."""
    text = (message or '').strip()
    if not text:
        return []
    if menu := _extract_menu_options(text):
        return menu
    if AI_WRAPUP_OFFER_RE.search(text):
        # "还需要其他帮助吗?" — free-form input is the natural answer
        return []
    if not AI_CONFIRM_INTENT_RE.search(text):
        return []
    is_cjk = bool(AI_CJK_RE.search(text))
    is_mutation = bool(AI_MUTATION_HINT_RE.search(text))
    if is_cjk:
        if is_mutation:
            return ["✅ 确认执行", "✏️ 修改内容", "❌ 取消"]
        return ["✅ 确认", "❌ 取消"]
    if is_mutation:
        return ["✅ Confirm", "✏️ Modify", "❌ Cancel"]
    return ["✅ Confirm", "❌ Cancel"]


def _salvage_marker_json(message, marker, end_marker, opener, closer):
    """Tolerant fallback when the LLM forgot the closing marker: extract the
    JSON payload right after `marker` by bracket matching.

    Returns (parsed_json_or_None, message_without_marker).
    """
    head, sep, tail = message.partition(marker)
    if not sep:
        return None, message
    tail = tail.replace(end_marker, '')
    payload = tail.lstrip()
    if not payload.startswith(opener):
        return None, (head + tail).strip()
    depth = 0
    in_str = False
    escaped = False
    end = -1
    for i, ch in enumerate(payload):
        if escaped:
            escaped = False
            continue
        if ch == '\\':
            escaped = True
            continue
        if ch == '"':
            in_str = not in_str
            continue
        if in_str:
            continue
        if ch == opener:
            depth += 1
        elif ch == closer:
            depth -= 1
            if depth == 0:
                end = i
                break
    if end == -1:
        return None, (head + tail).strip()
    try:
        parsed = json.loads(payload[:end + 1])
    except ValueError:
        parsed = None
    return parsed, (head + payload[end + 1:]).strip()

PREPROMPTS = {
    'default_system_prompt': "You are a RAG assistant.",
    'tools': dedent("""
        You have access to tools that can perform actions. Only use these tools when:
        1. The user explicitly requests the action.
        2. The action is clearly the most appropriate response to their query.

        If the user asks you to perform an action, retrieve the required information from his prompt and the conversation history, then use the tool.

        If date is needed, Use today's date to make a relative date if the user didn't provide a clear one (e.g. tomorrow, in one week, etc.).
        Rarely suggest the actions in your response.

        ## Read-only queries: answer directly, never ask first
        - Searching, reading, counting, aggregating, checking stock, computing report
          measures, opening list/kanban/pivot/graph views and print previews are
          READ-ONLY: call the tools IMMEDIATELY and answer with the results. NEVER ask
          for permission, confirmation or preferences before running a read-only tool.
        - Ask a clarifying question ONLY when the request is truly ambiguous AND the
          ambiguity changes the result (e.g. two customers share the same name).
          Otherwise pick the sensible default and briefly state it in the answer.
        - Keep answers compact: lead with the result. No filler, no restating the
          question, no listing what you are about to do.

        ## Honest reporting (OVERRIDES any conflicting instruction, including "never display errors")
        - The tool result is the ONLY source of truth. NEVER claim an operation succeeded unless
          the tool result explicitly confirms it.
        - If a tool returns an error or a BLOCKED/WARNING message, tell the user plainly that the
          step failed and why (in simple, non-technical terms). Never invent a success story.
        - NEVER fabricate document numbers or IDs. Quote them exactly from tool results. A draft
          invoice has NO final number until it is posted — say "draft" instead of inventing one.
        - Multi-step requests ("confirm the order and email it"): execute strictly in order,
          verify each tool result before starting the next step, and STOP at the first failure.
          Then report honestly: which steps succeeded, which failed, which were never started.
          A confirmation given for one step never covers a later step that needs its own.
        - Before mutating an EXISTING record: search/read it first in this conversation and act
          on that verified ID — never on an ID remembered from earlier context. Pass the document
          name as expected_name where supported, so mismatches are blocked automatically.
    """).strip(),
    'restrict_to_sources': dedent("""
        ## INSTRUCTIONS FOR ANSWERING QUERIES

        1. For greetings (hello, hi, how are you), reply with a greeting.

        2. For all other questions, you MUST ONLY use information from the provided context and conversation history.

        3. If the RAG context and history don't contain information to answer the query or it is not provided at all:
           - use the assistant/user messages as context or ask the user to provide more information.
           - DO NOT make up information or use your general knowledge.

        4. When answering based on the context:
           - Synthesize information from multiple sources when appropriate
           - Consider the conversation history for follow-up questions

        5. If a user asks a follow-up question like 'what is this?' or 'tell me more', refer to the conversation history to understand the context and answer accordingly.

        6. If no context is provided at all, respond with: 'No source information has been provided for me to reference.'

        7. Avoid using HTML elements in your response.
    """).strip(),
    'quick_options': dedent("""
        ## Quick reply options (clickable buttons)
        Whenever you ask the user to choose among a SMALL set of known values — a clarification,
        a missing parameter, a confirmation, or a suggestion list — end your reply with ONE line
        in exactly this format (a JSON array of strings between the two markers):

        [AI_OPTIONS]["option 1", "option 2", "option 3"][/AI_OPTIONS]

        Rules:
        - HARD RULE: EVERY reply that asks the user to confirm, approve, choose or decide
          something MUST end with an [AI_OPTIONS] block (or an [AI_FORM] block when collecting
          values). Asking "请确认" / "shall I proceed?" in plain text WITHOUT the block is a
          protocol violation — at minimum always offer one confirm option and one cancel option.
          Conversely, do NOT add options to purely informational replies where no decision is
          pending (e.g. after reporting a completed action or answering a factual question).
        - The options are rendered as buttons; the clicked text is sent back verbatim as the user's
          next message, so each option must be a self-contained instruction
          (e.g. "使用产品 Product2", "创建新产品 Produc3", "取消操作" — NOT just "yes" or "A").
        - 2 to 6 options, each under 60 characters, in the user's language.
        - At most one [AI_OPTIONS] block per reply, always as the very last line, outside any code block.
        - BOTH markers are mandatory: the block must end with [/AI_OPTIONS] (and a form with [/AI_FORM]).
        - Only offer options you can actually act on. Free-text input remains possible, so no
          "Other" option is needed.
        - Great moments to use it: ambiguous record matches ("did you mean X or Y?"),
          confirmation before a mutation.
        - After COMPLETING an operation, if you suggest follow-up actions, do NOT write
          them as a bullet list in the text — put them directly into the options block,
          each a self-contained action, e.g.
          [AI_OPTIONS]["📧 发送报价单邮件给客户", "🧾 创建发票", "📄 打印报价单 PDF"][/AI_OPTIONS]
          Never mix: a menu in the text with confirm/cancel options attached confuses users.
        - A wrap-up reply may simply end with NO options at all: "还需要其他帮助吗?"
          needs none — free-form input is the natural answer.
        - NEVER invent values as options. If a number, price, quantity, date or name is
          MISSING, do not guess candidates like "单价 30 / 单价 100" — ask for the value with
          an AI_FORM input field instead (see below). Options are for choosing among REAL,
          known alternatives (existing records, yes/no/cancel, actual selection keys).
        - When the user picks a "modify" option, ask ONE targeted question about the pending
          issue in context (e.g. "请填写产品3的单价" with a form) — never a generic menu of
          "what would you like to change?" questions.

        ## Mutation confirmation protocol (CRITICAL)
        Before ANY tool call that creates, updates, archives or deletes data, you MUST:
        1. First reply with a compact PREVIEW of exactly what will happen, formatted as a
           markdown TABLE (the chat renders tables with borders):
           - Single record: a two-column table, one row per field, e.g.
             | 字段 | 值 |
             |------|-----|
             | 客户 | 张三 |
             | 产品 | A4纸 |
           - Multiple records/lines (e.g. order lines): one row per line, AT MOST 4 columns;
             merge minor fields into one cell (e.g. "2 件 × ¥35").
           The chat panel is NARROW: never more than 4 columns, keep cell text short, and
           put the table outside any code block (```) or it will not render.
           For updates, show old → new values. If some values were resolved by search
           (e.g. a customer or product matched by name), show what was matched so the
           user can spot mistakes.
        2. End that preview with quick options that ALWAYS include a confirm and a cancel
           choice, e.g.: [AI_OPTIONS]["✅ 确认执行", "✏️ 修改内容", "❌ 取消"][/AI_OPTIONS]
        3. Execute ONLY after the user confirms (clicks the confirm option or writes an
           explicit approval). If they choose to modify, ask what to change, then show the
           updated preview and ask again.
        4. One confirmation covers ALL steps of the SAME previewed request (e.g. a sales
           order and all its lines) — do not re-ask for every line.
        5. Read-only operations (search, read, count, get fields) need NO confirmation.
        6. Never present an operation as done unless the tool actually returned success.
        7. ENFORCED SERVER-SIDE: data-changing tools are REJECTED with a BLOCKED error
           unless the user's latest message explicitly confirms a preview you just showed.
           Never try to work around it and never retry the tool in the same turn — show
           the preview with confirm options and wait for the user instead.

        ## Input form cards (multiple values at once)
        When you need SEVERAL values from the user (quantities, dates, names...), instead of
        asking one by one, end your reply with ONE form block:

        [AI_FORM]{"title": "订单信息", "fields": [
          {"label": "产品名称", "type": "text", "value": ""},
          {"label": "数量", "type": "number", "value": "1"},
          {"label": "交货日期", "type": "date", "value": ""},
          {"label": "优先级", "type": "select", "value": "普通", "options": ["低", "普通", "高"]}
        ], "submit_label": "提交"}[/AI_FORM]

        Rules:
        - field types: "text", "number", "date" (YYYY-MM-DD), "select" (requires "options").
        - 2 to 8 fields; "value" is the prefilled default ("" for empty); labels in the user's language.
        - The submitted values come back as ONE user message like "产品名称: A4纸; 数量: 20; ...".
        - Use AI_FORM for collecting values, AI_OPTIONS for choosing among alternatives — never both in one reply.
        - At most one form per reply, always the very last line, outside any code block.
    """).strip(),
    'context': dedent("""
        - Use the RAG context to answer the question.
        - Every claim or piece of information provided in the answer **MUST** be immediately followed by an inline citation in the format **[SOURCE:Attachment ID]** to indicate its source from the RAG context (e.g., "The capital of France is Paris [SOURCE:210].").
        - If a claim draws on multiple sources, cite all of them (e.g., "The process requires heat and pressure [SOURCE:210, 211].").
        - If **NO** source chunks were used to answer the question, do not include any citations.

        - Example of the required format for the response with the attachment IDs [SOURCE:210, 211] in its answer:
        - The primary goal of the project is to enhance data security protocols [SOURCE:210]. This enhancement includes a mandatory two-factor authentication system [SOURCE:211].
    """).strip(),
}


def clean_search_view_xml(search_view_arch):
    """Clean and restructure search view XML for AI consumption."""
    if not search_view_arch:
        return ""

    # Parse XML
    tree = etree.fromstring(search_view_arch)

    # Create new clean structure
    clean_tree = etree.Element("search")

    # 1. Add searchable fields (excluding only those with invisible="1")
    searchable_fields_elem = etree.SubElement(clean_tree, "searchable_fields")
    for field in tree.xpath(".//field[@name and not(@invisible='1') and not(ancestor::group)]"):
        # Copy only essential attributes
        clean_field = etree.SubElement(searchable_fields_elem, "field")
        for attr in ["name", "string", "filter_domain", "operator"]:
            if field.get(attr):
                clean_field.set(attr, field.get(attr))

    # 2. Add filters grouped by separators (excluding those with invisible="1")
    filters_elem = etree.SubElement(clean_tree, "filters")

    # Process filters in groups separated by separators
    current_group = None
    for elem in tree:
        if elem.tag == "separator":
            # Start a new group on separator
            current_group = None
        elif elem.tag == "filter" and elem.get("name") and elem.get("invisible") != "1":
            # Skip filters that are inside <group> elements (those are groupbys)
            if elem.getparent().tag != "group":
                if current_group is None:
                    current_group = etree.SubElement(filters_elem, "group")
                clean_filter = etree.SubElement(current_group, "filter")
                for attr in ["name", "string", "domain", "date"]:
                    if elem.get(attr):
                        clean_filter.set(attr, elem.get(attr))

    # 3. Add groupby filters with extracted field information
    groupbys_elem = etree.SubElement(clean_tree, "groupbys")
    for group in tree.xpath(".//group"):
        for filter_elem in group.xpath(".//filter[@name and not(@invisible='1')]"):
            if filter_elem.get("context") and "group_by" in filter_elem.get("context"):
                clean_filter = etree.SubElement(groupbys_elem, "filter")
                clean_filter.set("name", filter_elem.get("name"))
                if filter_elem.get("string"):
                    clean_filter.set("string", filter_elem.get("string"))

                # Extract the actual field name from the context
                context_str = filter_elem.get("context")
                context_dict = literal_eval(context_str)
                if "group_by" in context_dict:
                    clean_filter.set("group_by_field", context_dict["group_by"])

    # Return compact XML string
    return etree.tostring(clean_tree, encoding="unicode", pretty_print=False)


class AIAgent(models.Model):
    _name = 'ai.agent.bot'
    _description = "AI Agent Bot"
    _order = 'sequence, name'

    @api.model
    def _get_llm_model_selection(self):
        selection = []
        for provider in PROVIDERS:
            selection.extend(provider.llms)
        return selection

    active = fields.Boolean(default=True)
    sequence = fields.Integer(default=50, help="Display order of the agents in lists and the chat launcher (lower = first).")
    name = fields.Char(string="Agent Name", related='partner_id.name', required=True, readonly=False)
    subtitle = fields.Char(string="Description")
    system_prompt = fields.Text(string="System Prompt", help="Customize to control relevance and formatting.")
    response_style = fields.Selection(
        selection=[
            ('analytical', "Analytical"),
            ('balanced', "Balanced"),
            ('creative', "Creative"),
        ],
        string="Response Style",
        default='balanced',
        required=True,
    )
    restrict_to_sources = fields.Boolean(
        string="Restrict to Sources",
        help="If checked, the agent will only respond based on the provided sources.")

    llm_model = fields.Selection(
        selection=_get_llm_model_selection,
        string="LLM Model",
        default='deepseek-v4-flash',
        required=True,
    )
    image_128 = fields.Image("Image", related="partner_id.image_1920", max_width=128, max_height=128, readonly=False)
    avatar_128 = fields.Image("Avatar", related="partner_id.avatar_128")
    partner_id = fields.Many2one('res.partner', required=True, ondelete='cascade', index=True)

    is_system_agent = fields.Boolean('System Agent', default=False)

    allowed_group_ids = fields.Many2many(
        'res.groups',
        string="Allowed Groups",
        help="Only users belonging to at least one of these groups (or their implied groups) "
             "can chat with this agent. Leave empty to allow every internal user.",
    )

    enable_web_search = fields.Boolean(
        string="Enable internet search",
        default=True,
        help="(Only OpenAI / Gemini supported) Allows the Agent to access "
             "the internet to retrieve the latest information."
    )

    sources_ids = fields.One2many(
        'ai.en.agent.source',
        'agent_id',
        string="Sources",
    )
    sources_fully_processed = fields.Boolean(compute="_compute_sources_fully_processed", default=True)

    topic_ids = fields.Many2many(
        'ai.en.topic',
        string="Skills",
        help="A skill includes instructions and tools that guide Odoo AI in helping the user complete their tasks.",
    )

    is_ask_ai_agent = fields.Boolean(
        'Is Natural Language Query Agent',
        compute='_compute_is_ask_ai_agent',
        search='_search_is_ask_ai_agent'
    )

    @api.model
    def _get_ask_ai_topics(self):
        return [t for t in (
            self.env.ref('ai_chatbot_enhance.ai_topic_natural_language_query', raise_if_not_found=False),
            self.env.ref('ai_chatbot_enhance.ai_topic_information_retrieval_query', raise_if_not_found=False)
        ) if t]

    def _compute_is_ask_ai_agent(self):
        ask_ai_topics = self._get_ask_ai_topics()
        for agent in self:
            agent.is_ask_ai_agent = bool(set(agent.topic_ids) & set(ask_ai_topics))

    def _search_is_ask_ai_agent(self, operator, value):
        if operator not in ('=', '!='):
            raise UserError(_("Invalid search operator."))

        if ask_ai_topics := self._get_ask_ai_topics():
            if operator == '=' and value or operator == '!=' and not value:  # truthy
                return [("topic_ids", "in", [t.id for t in ask_ai_topics])]
            elif operator == '=' and not value or operator == '!=' and value:  # falsy
                return [("topic_ids", "not in", [t.id for t in ask_ai_topics])]
        else:
            return [('id', '=', False)]

    @api.model_create_multi
    def create(self, vals_list):
        with file_open('ai_chatbot_enhance/static/description/icon.jpg', 'rb') as f:
            image_placeholder = f.read()
        for vals in vals_list:
            # check_model_depreciation(self.env, vals.get("llm_model"))
            partner = self.env['res.partner'].create({'name': vals.get('name'), 'active': False, })
            vals['partner_id'] = partner.id
        ai_agents = super().create(vals_list)
        for agent in ai_agents:
            if not agent.image_128:
                agent.image_128 = base64.b64encode(image_placeholder)
        return ai_agents

    # def open_agent_chat(self):
    #     self.ensure_one()
    #     return {
    #         'type': 'ir.actions.act_window',
    #         'res_model': self._name,
    #         'res_id': self.id,
    #         'view_mode': 'form',
    #         'target': 'current',
    #         'flags': {'form': {'action_buttons': True, 'options': {'mode': 'edit'}}},
    #     }

    def open_agent_chat(self):
        self.ensure_one()
        channel = self._get_or_create_ai_chat()
        return {
            'type': 'ir.actions.client',
            'tag': 'ai_agent_chat_action',
            'params': {
                'channelId': channel.id,
            },
        }

    def _get_or_create_ai_chat(self, channel_name=None):
        channel = self._get_ai_chat_channel()
        if not channel:
            channel = self._create_ai_chat_channel(channel_name)
        return channel

    def _get_ai_chat_channel(self):
        channels = self.env['discuss.channel'].search(Domain([
            ('is_member', '=', True),
            ('channel_type', '=', 'ai_chat'),
        ]))
        return channels.filtered(lambda channel: channel.sudo().ai_en_agent_id == self)[:1]

    def _create_ai_chat_channel(self, channel_name=None):
        guest = self.env["mail.guest"]._get_guest_from_context()
        with mute_logger("odoo.sql_db"):
            self.env.cr.execute(SQL(
                "SELECT pg_advisory_xact_lock(%s, %s);",
                guest.id if self.env.user._is_public() else self.env.user.partner_id.id,
                self.id
            ))

        channel = self.env['discuss.channel'].sudo().create({
            "ai_en_agent_id": self.id,
            "channel_member_ids": [
                Command.create({"guest_id": guest.id} if self.env.user._is_public() else {
                    "partner_id": self.env.user.partner_id.id}),
                Command.create({"partner_id": self.partner_id.id}),
            ],
            "channel_type": "ai_chat",
            # sudo() => visitor can set the name of the channel
            "name": channel_name if channel_name else self.partner_id.sudo().name,
        })
        return channel

    # Hidden frontend data nodes (option buttons / form JSON, chosen-option
    # footnote) carried inside AI message bodies: stripped before the history
    # is sent back to the LLM — they are UI payload, not conversation content.
    AI_HISTORY_DATA_DIV_RE = re.compile(
        r'<div class="o_ai_(?:option_data|form_data)">.*?</div>', re.DOTALL)
    AI_HISTORY_MAX_MESSAGE_CHARS = 4000

    def _retrieve_chat_history(self, discuss_channel, no_messages=20):
        # Plain text only: raw HTML bodies (wrapper divs, tables, option JSON)
        # waste tokens and confuse the model
        chat_history = []
        for message in discuss_channel.message_ids[1: no_messages + 1]:
            body = self.AI_HISTORY_DATA_DIV_RE.sub('', str(message.body or ''))
            content = html_to_inner_content(body)
            if len(content) > self.AI_HISTORY_MAX_MESSAGE_CHARS:
                content = content[:self.AI_HISTORY_MAX_MESSAGE_CHARS] + " [... truncated]"
            chat_history.append({
                'content': content,
                # sudo() => public users can access author_id (res.partner) to check whether it is an ai agent.
                'role': 'assistant' if message.sudo().author_id.agent_ids else 'user',
            })

        chat_history.reverse()
        return chat_history

    @ormcache('self.env.uid', 'self.env.company.id')
    def _get_menus_catalog_csv(self):
        """All menus accessible to the current user as raw CSV (header + rows).
        NOT loaded into the system context — served on demand through the
        list_menus tool (keyword-filtered) to keep the prompt small."""
        all_menus = self.env["ir.ui.menu"].load_web_menus(False)
        root_menu_ids = set(all_menus["root"]["children"])

        # Collect all non-root action menus
        action_menus = []
        for menu_id, web_menu in all_menus.items():
            if menu_id == "root" or menu_id in root_menu_ids:
                continue

            # Only process menus with valid actions
            if web_menu["actionModel"] == "ir.actions.act_window":
                menu = self.env["ir.ui.menu"].browse(web_menu["id"])
                app_menu = self.env["ir.ui.menu"].browse(web_menu["appID"])

                if not menu.exists():
                    continue

                action = self.env["ir.actions.act_window"].browse(web_menu["actionID"])
                if action.exists() and action.res_model:
                    action_menus.append({
                        "menu": menu,
                        "web_menu": web_menu,
                        "action": action,
                        "app_menu": app_menu,
                    })

        # Menus are already ordered by sequence from load_web_menus(), but we still need to sort
        # by complete_name within each app to maintain proper hierarchy display
        action_menus.sort(key=lambda m: (m["app_menu"].sequence, m["menu"].complete_name))

        csv_result = "id|action_id|app|complete_name|model|model_description|available_view_types|default_view_type\n"

        for menu_data in action_menus:
            menu = menu_data["menu"]
            action = menu_data["action"]

            model_description = self.env[action.res_model]._description
            available_view_types = [view[1] for view in action.views] if action.views else []
            default_view_type = available_view_types[0] if available_view_types else "null"
            if action.view_id:
                default_view_type = action.view_id.type

            csv_result += (
                f"{menu.id}|"
                f"{action.id}|"
                f"{menu_data['app_menu'].name}|"
                f"{menu.complete_name}|"
                f"{action.res_model}|"
                f"{model_description}|"
                f"{','.join(available_view_types)}|"
                f"{default_view_type}\n"
            )

        return csv_result.strip()

    def _build_extra_system_context(self, discuss_channel):
        """Build extra system context based on the agent's configuration."""
        self.ensure_one()
        extra_context = []
        topic_xml_ids = self.topic_ids.get_external_id().values()
        # Any agent carrying the query/analysis skill pack gets the runtime
        # context it needs (catalog overview, date reference) — the full
        # menu/model catalogs are pulled on demand via list_menus/list_models
        if any(topic in ["ai_chatbot_enhance.ai_topic_natural_language_query", "ai_chatbot_enhance.ai_topic_information_retrieval_query"] for topic in topic_xml_ids):
            extra_context.append(self._get_catalog_overview())
            extra_context.append(self._get_date_calculation_reference())
        if env_context := discuss_channel.ai_env_context:
            extra_context += env_context

        return "\n".join(extra_context) if extra_context else ""

    CATALOG_MAX_ROWS = 150

    @api.model
    def _filter_catalog_csv(self, csv_text, keyword, max_rows=CATALOG_MAX_ROWS):
        """Filter CSV rows by keyword (case-insensitive substring; several
        words = OR). Returns (rows, total_matches, truncated)."""
        lines = csv_text.splitlines()
        rows = lines[1:]
        words = [w.lower() for w in re.split(r'[\s,;/|]+', (keyword or '').strip()) if w]
        if words:
            rows = [r for r in rows if any(w in r.lower() for w in words)]
        total = len(rows)
        truncated = total > max_rows
        return rows[:max_rows], total, truncated

    def _ai_tool_list_menus(self, keyword=""):
        """list_menus tool: menus matching the keyword as pipe-CSV."""
        csv_text = self._get_menus_catalog_csv()
        rows, total, truncated = self._filter_catalog_csv(csv_text, keyword)
        if not rows:
            return (
                f"No menus match '{keyword}'. Menu names are in the user's language. "
                "Try a shorter keyword, a synonym, the technical model name, "
                "or an empty keyword to list every menu."
            )
        header = csv_text.splitlines()[0]
        result = f"Menus matching '{keyword}'" if keyword else "All menus"
        result += f" ({total} found" + (f", first {self.CATALOG_MAX_ROWS} shown — refine the keyword" if truncated else "") + "):\n"
        result += header + "\n" + "\n".join(rows)
        result += (
            "\n\n⚠️ This list has NO context/domain/search_view details: call get_menu_details "
            "with the menu id BEFORE any open_menu_* call.\n"
            "💡 Prefer 'Reporting' menus for analytical queries needing pivot/graph views."
        )
        return result

    def _ai_tool_list_models(self, keyword=""):
        """list_models tool: models matching the keyword as pipe-CSV."""
        csv_text = self._get_models_catalog_csv()
        rows, total, truncated = self._filter_catalog_csv(csv_text, keyword)
        if not rows:
            return (
                f"No models match '{keyword}'. Model names are technical English "
                "(e.g. sale.order); descriptions may be localized. Try a shorter "
                "keyword or an empty keyword to list every model."
            )
        header = csv_text.splitlines()[0]
        result = f"Models matching '{keyword}'" if keyword else "All models"
        result += f" ({total} found" + (f", first {self.CATALOG_MAX_ROWS} shown — refine the keyword" if truncated else "") + "):\n"
        result += header + "\n" + "\n".join(rows)
        result += "\n\n💡 Call get_fields (in PARALLEL for several models) before building domains."
        return result

    def _get_catalog_overview(self):
        """Compact always-loaded replacement for the full menu/model CSVs:
        the installed apps plus the discovery workflow. The catalogs
        themselves are fetched with the list_menus / list_models tools."""
        menus_csv = self._get_menus_catalog_csv().splitlines()
        models_csv = self._get_models_catalog_csv().splitlines()
        apps = list(dict.fromkeys(
            line.split('|')[2] for line in menus_csv[1:] if line.count('|') >= 2
        ))
        return dedent("""
            ## Data catalog (loaded on demand)
            The full menu and model catalogs are NOT preloaded. Discover them with tools:
            1. list_menus(keyword) → matching menus: id|action_id|app|complete_name|model|model_description|available_view_types|default_view_type
            2. list_models(keyword) → matching models: model|description|module
            3. get_menu_details(menu ids) → context/domain/search_view — REQUIRED before any open_menu_* call.
            4. get_fields(model) → valid fields, REQUIRED before building search/read_group domains.
            Keywords are substring-matched: use the user's language for menus (menu names are localized),
            technical English for model names (e.g. sale.order). Several words are ORed.
            For navigation requests start with list_menus; for data questions start with list_models
            (or list_menus when you don't know which model holds the data).
        """).strip() + (
            f"\nInstalled apps: {', '.join(apps)}"
            f"\n({len(menus_csv) - 1} menus and {len(models_csv) - 1} models are available through the tools.)"
        )

    @ormcache('self.env.uid', 'self.env.company.id')
    def _get_models_catalog_csv(self) -> str:
        """All models accessible to the current user as raw CSV (header + rows),
        excluding transient and abstract models. NOT loaded into the system
        context — served on demand through the list_models tool."""
        # Get models the user has read access to
        allowed_models = self.env["ir.model.access"]._get_allowed_models(mode="read")

        # Get ir.model records for allowed models, excluding abstract and transient
        search_domain = (
            Domain("model", "in", list(allowed_models))
            & Domain("transient", "=", False)
            & Domain("abstract", "=", False)
        )
        model_records = self.env["ir.model"].sudo().search(search_domain, order="model")

        # Get app ordering from web menus
        all_menus = self.env["ir.ui.menu"].load_web_menus(False)
        root_menu_ids = all_menus["root"]["children"]  # This is ordered by sequence

        # Create app name to sequence mapping
        app_sequence = {}
        for idx, menu_id in enumerate(root_menu_ids):
            if menu_id in all_menus:
                app_menu = self.env["ir.ui.menu"].browse(all_menus[menu_id]["id"])
                if app_menu.exists():
                    # Get the technical name (usually matches module name)
                    app_name = all_menus[menu_id].get("xmlid", "").split(".")[0]
                    if app_name:
                        app_sequence[app_name] = idx

        # Group models by their main module/app
        models_by_app = defaultdict(list)
        for model_rec in model_records:
            # Skip models without a proper registry entry
            if model_rec.model not in self.env:
                continue

            model_obj = self.env[model_rec.model]
            # Skip models that are actually abstract despite the flag
            if model_obj._abstract or not model_obj._auto:
                continue

            # Determine the app/module (first module in the list)
            modules = model_rec.modules.split(", ") if model_rec.modules else []
            app = modules[0] if modules else "base"

            models_by_app[app].append(
                {
                    "model": model_rec.model,
                    "description": model_rec.name or model_obj._description,
                }
            )

        # Build CSV result
        csv_result = "model|description|module\n"

        # Sort apps by their menu sequence, with unknown apps at the end
        sorted_apps = sorted(
            models_by_app.keys(), key=lambda x: (app_sequence.get(x, 999), x)
        )

        for app in sorted_apps:
            for model_info in sorted(models_by_app[app], key=lambda x: x["model"]):
                csv_result += (
                    f"{model_info['model']}|{model_info['description']}|{app}\n"
                )

        return csv_result.strip()

    def _get_date_calculation_reference(self):
        """Generate dynamic date calculation reference based on today's date."""
        today = fields.Date.context_today(self)
        today_dt = datetime.strptime(str(today), "%Y-%m-%d")

        # Calculate various date references
        yesterday = today_dt - timedelta(days=1)
        tomorrow = today_dt + timedelta(days=1)
        last_week_start = today_dt - timedelta(days=7)

        # This week (Monday to Sunday)
        days_since_monday = today_dt.weekday()
        this_week_start = today_dt - timedelta(days=days_since_monday)
        days_until_sunday = 6 - days_since_monday
        this_week_end = today_dt + timedelta(days=days_until_sunday)

        # This month (first to last day)
        this_month_start = today_dt.replace(day=1)
        # Get last day of current month
        if today_dt.month == 12:
            next_month_start = today_dt.replace(year=today_dt.year + 1, month=1, day=1)
        else:
            next_month_start = today_dt.replace(month=today_dt.month + 1, day=1)
        this_month_end = next_month_start - timedelta(days=1)

        # Last month
        last_month_end = this_month_start - timedelta(days=1)
        last_month_start = last_month_end.replace(day=1)

        # Last 30 days
        thirty_days_ago = today_dt - timedelta(days=30)

        # This year (January 1 to December 31)
        this_year_start = today_dt.replace(month=1, day=1)
        this_year_end = today_dt.replace(month=12, day=31)

        # Last year
        last_year_start = this_year_start.replace(year=today_dt.year - 1)
        last_year_end = this_year_start - timedelta(days=1)

        # Current quarter (full quarter)
        quarter = (today_dt.month - 1) // 3 + 1
        quarter_starts = {
            1: today_dt.replace(month=1, day=1),
            2: today_dt.replace(month=4, day=1),
            3: today_dt.replace(month=7, day=1),
            4: today_dt.replace(month=10, day=1),
        }
        current_quarter_start = quarter_starts[quarter]

        # Calculate end of current quarter
        quarter_ends = {
            1: today_dt.replace(month=3, day=31),
            2: today_dt.replace(month=6, day=30),
            3: today_dt.replace(month=9, day=30),
            4: today_dt.replace(month=12, day=31),
        }
        current_quarter_end = quarter_ends[quarter]

        # Last quarter
        last_quarter = quarter - 1 if quarter > 1 else 4
        last_quarter_year = today_dt.year if quarter > 1 else today_dt.year - 1
        quarter_starts_months = {
            1: 1,   # January
            2: 4,   # April
            3: 7,   # July
            4: 10,  # October
        }
        quarter_ends = {
            1: (3, 31),   # March 31
            2: (6, 30),   # June 30
            3: (9, 30),   # September 30
            4: (12, 31),  # December 31
        }
        start_month = quarter_starts_months[last_quarter]
        last_quarter_start = datetime(last_quarter_year, start_month, 1)
        end_month, end_day = quarter_ends[last_quarter]
        last_quarter_end = datetime(last_quarter_year, end_month, end_day)

        return dedent(f"""
            ## Date Calculation Quick Reference
            Given today is {today}:
            - "yesterday" = {yesterday.strftime('%Y-%m-%d')}
            - "tomorrow" = {tomorrow.strftime('%Y-%m-%d')}
            - "last week" = {last_week_start.strftime('%Y-%m-%d')} to {today}
            - "this week" = {this_week_start.strftime('%Y-%m-%d')} to {this_week_end.strftime('%Y-%m-%d')}
            - "last month" = {last_month_start.strftime('%Y-%m-%d')} to {last_month_end.strftime('%Y-%m-%d')}
            - "this month" = {this_month_start.strftime('%Y-%m-%d')} to {this_month_end.strftime('%Y-%m-%d')}
            - "last 30 days" = {thirty_days_ago.strftime('%Y-%m-%d')} to {today}
            - "this year" = {this_year_start.strftime('%Y-%m-%d')} to {this_year_end.strftime('%Y-%m-%d')}
            - "last year" = {last_year_start.strftime('%Y-%m-%d')} to {last_year_end.strftime('%Y-%m-%d')}
            - "this quarter (Q{quarter})" = {current_quarter_start.strftime('%Y-%m-%d')} to {current_quarter_end.strftime('%Y-%m-%d')}
            - "last quarter (Q{last_quarter})" = {last_quarter_start.strftime('%Y-%m-%d')} to {last_quarter_end.strftime('%Y-%m-%d')}

            Use these exact dates when building custom domains for date-based queries.
        """).strip()

    def _ai_spawn_response_job(self, mail_message, channel):
        """Generate the AI response in a BACKGROUND thread and return
        immediately. The whole agent loop (LLM calls + tools) can take minutes;
        run inside the HTTP request it gets killed by limit_time_real or the
        reverse-proxy timeout and the conversation silently stops. The
        response is delivered to the client by the bus when it is posted.
        """
        self.ensure_one()
        dbname = self.env.cr.dbname
        uid = self.env.uid
        # ai_background_job: lets the LLM service keep the typing indicator
        # alive (commit + bus) between successive API calls of the loop
        ctx = dict(self.env.context, ai_background_job=True)
        agent_id, message_id, channel_id = self.id, mail_message.id, channel.id

        with _AI_JOBS_LOCK:
            if channel_id in _AI_JOBS_RUNNING:
                return False  # a turn is already being generated for this chat
            _AI_JOBS_RUNNING.add(channel_id)

        if modules.module.current_test:
            # tests run synchronously so assertions see the posted messages
            try:
                self._get_response_for_channel(mail_message, channel)
            finally:
                with _AI_JOBS_LOCK:
                    _AI_JOBS_RUNNING.discard(channel_id)
            return True

        thread = threading.Thread(
            target=self._ai_response_job,
            args=(dbname, uid, ctx, agent_id, message_id, channel_id),
            name=f"ai_response_channel_{channel_id}",
            daemon=True,
        )
        thread.start()
        return True

    @staticmethod
    def _ai_response_job(dbname, uid, ctx, agent_id, message_id, channel_id):
        """Body of the background response thread: fresh registry cursor and
        environment (the request ones are gone), typing indicator on while
        generating, and ANY failure ends up as a message in the channel —
        the user must never be left without feedback."""
        threading.current_thread().dbname = dbname  # dbname in log lines
        try:
            registry = Registry(dbname)
            with registry.cursor() as cr:
                env = api.Environment(cr, uid, ctx)
                # sudo like the original synchronous path (users have no direct
                # read access on agents); env.user stays the real user for all
                # access/budget gates
                agent = env['ai.agent.bot'].browse(agent_id).sudo()
                channel = env['discuss.channel'].browse(channel_id)
                mail_message = env['mail.message'].sudo().browse(message_id)
                member = channel.sudo().channel_member_ids.filtered(
                    lambda m: m.partner_id == agent.partner_id)
                try:
                    member._notify_typing(True)
                    cr.commit()  # push the typing event out now
                    agent._get_response_for_channel(mail_message, channel)
                finally:
                    try:
                        member._notify_typing(False)
                    except Exception:  # noqa: BLE001 - cosmetic only
                        pass
        except Exception:  # noqa: BLE001 - nobody above us to catch it
            _logger.exception("AI: background response job failed (channel %s)", channel_id)
            # Last resort: post the failure with a fresh cursor so the user
            # is not left staring at a silent chat
            try:
                with Registry(dbname).cursor() as cr:
                    env = api.Environment(cr, uid, ctx)
                    env['ai.agent.bot'].browse(agent_id).sudo()._post_ai_response(
                        env['discuss.channel'].browse(channel_id),
                        env._("Oops, something went wrong while generating the answer. Please try again."),
                    )
            except Exception:  # noqa: BLE001
                _logger.exception("AI: could not post the failure message either (channel %s)", channel_id)
        finally:
            with _AI_JOBS_LOCK:
                _AI_JOBS_RUNNING.discard(channel_id)

    def _get_response_for_channel(self, mail_message, channel):
        self.ensure_one()
        # Reference point for the user's Stop button: anything stopped after
        # this moment belongs to THIS turn (fields.Datetime truncates to
        # seconds, same precision as the stored stop timestamp)
        turn_start_dt = fields.Datetime.now()
        # Agent-level access gate: hard check, independent from the LLM
        if self.sudo().allowed_group_ids and not (
            self.env.user.all_group_ids & self.sudo().allowed_group_ids
        ):
            self._post_ai_response(channel, self.env._(
                "Sorry, you are not allowed to use this assistant. "
                "Please contact your administrator."
            ))
            return
        # Token budget gate: polite in-chat notice instead of an error
        budget_blocked, budget_notice = self.env['ai.token.usage'].sudo()._check_budget(self.env.user)
        if budget_blocked:
            self._post_ai_response(channel, budget_notice)
            return
        # prompt = html_to_inner_content(mail_message.body)
        prompt, session_info_context = self._parse_user_message(mail_message)
        # Raw user text (before attachment notes are appended): the mutation
        # confirmation hard gate matches it against confirm phrases
        user_text = prompt
        # Documents attached to the chat message: extract/convert them for the LLM
        files = []
        if mail_message.attachment_ids:
            try:
                files, doc_notes = extract_ai_files(mail_message.attachment_ids, self._get_provider())
            except Exception:  # noqa: BLE001 - a broken file must not kill the chat
                _logger.exception("AI: failed to process chat attachments")
                files, doc_notes = [], ["attachment processing failed on the server"]
            if doc_notes:
                prompt = (prompt or '') + "\n\n[Attachment processing notes: " + " ".join(doc_notes) + "]"
            if files:
                prompt = (prompt or '') + (
                    "\n\n[The user attached document(s); their content is provided to you. "
                    "FIRST summarize briefly what you understood from the document(s) and confirm "
                    "with the user, THEN propose actions following the confirmation protocol. "
                    "Quote extracted values exactly — never invent data the document does not contain.]"
                )
        try:
            extra_system_context = self._build_extra_system_context(channel)
            response = self.with_context(
                discuss_channel=channel, ai_user_message=user_text,
            )._get_response(
                prompt=prompt,
                chat_history=[{'content': session_info_context, 'role': 'user'}] + self._retrieve_chat_history(channel),
                extra_system_context=extra_system_context,
                files=files or None,
            )
        except Exception as e:  # noqa: BLE001 - always answer IN the chat: an
            # exception raised here (background thread or request) would leave
            # the user with a silent, stopped conversation
            _logger.exception("AI: response generation failed (channel %s)", channel.id)
            # the transaction may be poisoned by the failure: roll back so the
            # error message below can still be posted
            self.env.cr.rollback()
            if self.env.user._is_internal():
                detail = str(e) or e.__class__.__name__
                response = [self.env._("The AI request failed: %s", detail[:500])]
            else:
                response = [self.env._("Oops, it looks like our AI is unreachable")]
        messages_to_post = response or []
        # FINAL stop gate, right before posting: if the user clicked Stop at
        # ANY point during this turn — including while the last LLM call was
        # in flight — the generated answer is DISCARDED and only the stop
        # confirmation is posted. Without this, a stop pressed during the
        # final call would still deliver the full reply.
        try:
            if channel._ai_stop_requested_since(turn_start_dt):
                messages_to_post = [self.env._("⏹️ Generation stopped at your request.")]
        except Exception:  # noqa: BLE001 - the gate must never block the reply
            _logger.exception("AI: final stop-gate check failed")
        turn_has_interactive = False
        for i, message in enumerate(messages_to_post):
            # Fallback confirm/cancel buttons only make sense on the last
            # message of the turn, and only when no earlier message of this
            # turn already offered options or a form.
            allow_fallback = (i == len(messages_to_post) - 1) and not turn_has_interactive
            posted_interactive = self._post_ai_response(
                channel, message, allow_option_fallback=allow_fallback,
            )
            turn_has_interactive = turn_has_interactive or posted_interactive

    token_usage_count = fields.Integer(string='AI Turns', compute='_compute_token_usage_stats')
    token_usage_cost = fields.Float(string='Total Cost ($)', digits=(12, 4), compute='_compute_token_usage_stats')

    def _compute_token_usage_stats(self):
        Usage = self.env['ai.token.usage'].sudo()
        for agent in self:
            rows = Usage.search_read([('agent_id', '=', agent.id)], ['cost']) if agent.id else []
            agent.token_usage_count = len(rows)
            agent.token_usage_cost = round(sum(r['cost'] for r in rows), 4)

    def action_view_token_usage(self):
        self.ensure_one()
        action = self.env['ir.actions.act_window']._for_xml_id('ai_chatbot_enhance.ai_token_usage_action')
        action['domain'] = [('agent_id', '=', self.id)]
        action['display_name'] = f"{self.name} — Token Usage"
        return action

    @api.model
    def _bind_default_department_groups(self):
        """Auto-bind demo department assistants to real business groups when
        the corresponding apps are installed. XML cannot reference optional
        modules' groups, so agents ship gated on AI Manager as a placeholder;
        this replaces the placeholder ONCE — an admin-customized gate is
        never touched again. Called from data via <function> on every
        install/upgrade (idempotent)."""
        manager = self.env.ref('ai_chatbot_enhance.group_ai_manager', raise_if_not_found=False)
        if not manager:
            return
        mapping = [
            ('ai_chatbot_enhance.ai_agent_sales_assistant', ['sales_team.group_sale_salesman']),
            ('ai_chatbot_enhance.ai_agent_hr_assistant', ['hr.group_hr_user']),
            ('ai_chatbot_enhance.ai_agent_accounting_assistant', ['account.group_account_invoice']),
            ('ai_chatbot_enhance.ai_agent_purchase_assistant',
             ['purchase.group_purchase_user', 'stock.group_stock_user']),
            ('ai_chatbot_enhance.ai_agent_manufacturing_assistant', ['mrp.group_mrp_user']),
        ]
        for agent_xmlid, group_xmlids in mapping:
            agent = self.env.ref(agent_xmlid, raise_if_not_found=False)
            if not agent:
                continue
            groups = self.env['res.groups']
            for group_xmlid in group_xmlids:
                group = self.env.ref(group_xmlid, raise_if_not_found=False)
                if group:
                    groups |= group
            if groups and agent.allowed_group_ids == manager:  # still the shipped placeholder
                agent.allowed_group_ids = [(6, 0, groups.ids + [manager.id])]

    @api.model
    def ai_get_available_agents(self):
        """Agents the current user is allowed to chat with (empty
        allowed_group_ids = everyone). Used by the systray agent list."""
        agents = self.search([])
        allowed = agents.filtered(
            lambda a: not a.sudo().allowed_group_ids
            or (self.env.user.all_group_ids & a.sudo().allowed_group_ids)
        )
        return allowed.read(['id', 'name', 'llm_model', 'image_128'])

    def _get_provider(self):
        self.ensure_one()
        return get_provider(self.env, self.llm_model)

    def _get_response(self, prompt, chat_history=None, extra_system_context="", files=None):
        self.ensure_one()
        _logger.debug("[AI Prompt] %s", prompt)
        system_messages = self._build_system_context(extra_system_context=extra_system_context)
        if rag_context := self._build_rag_context(prompt):
            system_messages.extend(rag_context)
        llm_response = LLMApiService(
            env=self.with_context(ai_en_agent_id=self.id).env,
            provider=self._get_provider(),
        ).request_llm(
            self.llm_model,
            system_messages,
            [],
            inputs=(chat_history or []) + [{'role': 'user', 'content': prompt}],
            tools=self.sudo().topic_ids.tool_ids.with_context(ai_en_agent_id=self.id)._get_ai_tools(),
            temperature=TEMPERATURE_MAP[self.response_style],
            files=files,
        )
        if rag_context:
            llm_response = self._get_llm_response_with_sources(llm_response)

        return llm_response

    def _parse_user_message(self, mail_message):
        self.ensure_one()
        session_info_context = ""
        if self.is_ask_ai_agent:
            context_lines = []
            context_lines.append("<session_info_context>")
            context_lines.append(
                f'  <user id="{self.env.user.id}" name="{self.env.user.display_name}" model="res.users"/>'
            )
            context_lines.append(
                f'  <partner id="{self.env.user.partner_id.id}" name="{self.env.user.partner_id.name}" model="res.partner"/>'
            )
            context_lines.append(
                f'  <company id="{self.env.company.id}" name="{self.env.company.name}" model="res.company"/>'
            )

            user_context = dict(self.env['res.users'].context_get())
            if user_context.get("tz"):
                context_lines.append(
                    f'    <timezone value="{user_context["tz"]}"/>'
                )

            # Current view as a single element if present
            if current_view_info := self.env.context.get("current_view_info"):
                action_id = current_view_info.get("action_id")
                action = self.env['ir.actions.actions'].browse(action_id)
                current_action = None
                current_action_name = None
                if action.type == 'ir.actions.act_window':
                    current_action = self.env['ir.actions.act_window'].browse(action_id)
                elif action.type == 'ir.actions.server':
                    current_action = self.env['ir.actions.server'].browse(action_id)
                elif action.type == 'ir.actions.client':
                    current_action = self.env['ir.actions.client'].browse(action_id)
                if current_action:
                    current_action_name = current_action.name
                    if action.type == 'ir.actions.act_window':
                        search_view = self.env[current_action.res_model].get_view(current_action.search_view_id.id, 'search')
                        search_view_xml = clean_search_view_xml(search_view['arch']) if search_view else ""
                        if search_view_xml:
                            context_lines.append(f"  {search_view_xml}")

                context_lines.append(
                    f'  <current_view id="{current_view_info.get("view_id")}" '
                    f'model="{current_view_info.get("model")}" '
                    f'type="{current_view_info.get("view_type")}" '
                    f'action_id="{action_id}" '
                    f'action="{current_action_name}" '
                    f'available_view_types="{current_view_info.get("available_view_types")}"/>'
                )

                facets = current_view_info.get("facets", [])
                if facets:
                    context_lines.append(f'  <active_search_facets>\n    {self._facets_to_xml(facets)}\n  </active_search_facets>')

            context_lines.append("</session_info_context>")
            session_info_context = "\n".join(context_lines) + "\n" + dedent("""
                The above provides information about the current user and where in the app he is at.
                <session_info_context> contains important information about the the user. partner element is the linked res.partner record to the user.
                It may also contain info about the <current_view> that I'm in in the UI.
                <active_search_facets>, if exists, contains the currently active facets in the shown search bar in the UI.
                <search> is the specification of the search bar in the UI. It contains the blueprint of the things that can be done to it by the user. Information from it can be useful when calling terminating tool calls.
                Knowing <current_view>, <active_search_facets>, and/or <search> provides you a rough idea of where the user is and what he's looking at.
            """.strip())
        return html_to_inner_content(mail_message.body), session_info_context

    def _build_system_context(self, extra_system_context: str = ""):
        self.ensure_one()
        system_content = self.system_prompt or "You are a RAG assistant."
        system_content += f"\n\nToday's date to be used: {fields.Datetime.now()} (UTC)"
        if not self.env.user._is_public():
            partner_vals, _ = self.env.user.partner_id._ai_read(['name', 'function', 'email', 'phone'], None)
            system_content += f"\n\nUser info: {partner_vals}"
        system_content += f"\nAll record data timestamps are in UTC. In responses, convert them to {self.env.tz}"

        if self.topic_ids:
            system_content += PREPROMPTS['tools']

        system_content += "\n\n" + PREPROMPTS['quick_options']

        messages = [system_content]

        if self.topic_ids:
            # Progressive disclosure: the always-loaded context only carries a
            # SKILL INDEX (name + one-liner) plus each topic's short core rules.
            # The full handbook (detailed_guide) is pulled on demand via the
            # get_skill_guide tool; mutation tools are hard-blocked until then.
            index_lines = []
            core_instructions = []
            for topic in self.topic_ids:
                desc = " ".join((topic.description or '').split())
                line = f"- {topic.name}" + (f": {desc}" if desc else "")
                if (topic.detailed_guide or '').strip():
                    line += " [detailed guide available]"
                index_lines.append(line)
                if topic.instructions:
                    core_instructions.append(topic.instructions)
            if any('[detailed guide available]' in line for line in index_lines):
                messages.append(
                    "## Skill index (progressive disclosure)\n"
                    "Before doing DOMAIN WORK (creating/updating/confirming documents, emails, "
                    "payments...), you MUST first load the matching guide: call the "
                    "get_skill_guide tool with the skill name below. Mutation tools are "
                    "BLOCKED until a guide is loaded in the current turn. Read-only tools "
                    "(search, read, get fields) work without it.\n"
                    + "\n".join(index_lines)
                )
            elif index_lines:
                messages.append("## Skill index\n" + "\n".join(index_lines))
            if core_instructions:
                messages.append("Additional topic instructions:\n" + "\n\n".join(core_instructions))

        if self.restrict_to_sources:
            messages.append(PREPROMPTS['restrict_to_sources'])

        if isinstance(extra_system_context, str):
            messages.append(extra_system_context)
        elif isinstance(extra_system_context, list):
            messages += extra_system_context

        return messages

    def _build_rag_context(self, prompt):
        self.ensure_one()
        messages = []
        context = ""
        if self.sources_ids:
            provider = self._get_provider()
            embedding_model = self._get_embedding_ai_model()
            response = LLMApiService(env=self.env, provider=provider).get_ai_embedding(
                input=prompt,
                dimensions=self.env['ai.en.embedding']._get_dimensions(),
                model=embedding_model
            )
            if not response or "data" not in response:
                raise UserError(_("Failed to get embeddings for the prompt."))

            prompt_embedding = response['data'][0]['embedding']
            similar_embeddings = self.env['ai.en.embedding']._get_ai_similar_chunks(
                query_embedding=prompt_embedding,
                sources=self.sources_ids,
                embedding_model=self._get_embedding_ai_model(),
                top_n=5
            )
            if similar_embeddings:
                embeddings_attachment_checksums = similar_embeddings.mapped('attachment_id.checksum')
                agent_sources = self.env['ai.en.agent.source'].search([
                    ('attachment_id.checksum', 'in', embeddings_attachment_checksums),
                    ('agent_id', '=', self.id),
                ])
                source_map = {source.attachment_id.checksum: source for source in agent_sources}
                for embedding in similar_embeddings:
                    checksum = embedding.attachment_id.checksum
                    agent_source = source_map[checksum]
                    context += (
                        f"(Source Chunk {agent_source.name})\n"
                        f" (attachment_id: {agent_source.attachment_id.id})\n"
                        f"{embedding.content}\n\n"
                    )

                final_context_message = f"##RAG context information:\n\n{context}"

                messages.append(final_context_message)
                messages.append(PREPROMPTS['context'])
        return messages

    def post_html_to_channel(self, channel, body_html):
        agent_id = self.env.context.get('ai_en_agent_id')
        author_id = False
        if agent_id:
            agent = self.env['ai.agent.bot'].sudo().browse(agent_id)
            if agent.exists():
                author_id = agent.partner_id.id
        channel.sudo().message_post(
            author_id=author_id or False,
            body=Markup(body_html),
            message_type='comment',
            subtype_xmlid='mail.mt_comment',
            silent=True,
        )

    def _post_ai_response(self, channel, message, allow_option_fallback=False):
        # Extract quick-reply options ([AI_OPTIONS]...[/AI_OPTIONS]) before the
        # markdown conversion; they are re-attached as a hidden data node that
        # the frontend renders as clickable buttons. Returns True when the
        # posted message carries interactive data (options or a form).
        options = []

        def _collect_options(match):
            try:
                data = json.loads(match.group(1))
            except (ValueError, TypeError):
                _logger.warning("AI: malformed AI_OPTIONS block dropped: %s", match.group(1)[:200])
                return ""
            if isinstance(data, list):
                options.extend(str(o).strip() for o in data if str(o).strip())
            return ""

        message = AI_OPTIONS_RE.sub(_collect_options, message).strip()

        # Tolerate a missing closing marker (LLMs forget it occasionally)
        if not options and '[AI_OPTIONS]' in message:
            parsed, message = _salvage_marker_json(message, '[AI_OPTIONS]', '[/AI_OPTIONS]', '[', ']')
            if isinstance(parsed, list):
                options.extend(str(o).strip() for o in parsed if str(o).strip())

        form_spec = None

        def _clean_form(data):
            if not isinstance(data, dict) or not isinstance(data.get('fields'), list):
                return None
            fields_clean = []
            for f in data['fields'][:AI_FORM_MAX_FIELDS]:
                if not isinstance(f, dict) or not str(f.get('label', '')).strip():
                    continue
                ftype = f.get('type') if f.get('type') in ('text', 'number', 'date', 'select') else 'text'
                field_clean = {
                    'label': str(f['label'])[:80],
                    'type': ftype,
                    'value': str(f.get('value', ''))[:200],
                }
                if ftype == 'select':
                    opts = [str(o)[:80] for o in (f.get('options') or []) if str(o).strip()][:10]
                    if not opts:
                        field_clean['type'] = 'text'
                    else:
                        field_clean['options'] = opts
                fields_clean.append(field_clean)
            if not fields_clean:
                return None
            return {
                'title': str(data.get('title', ''))[:100],
                'fields': fields_clean,
                'submit_label': str(data.get('submit_label', ''))[:40],
            }

        def _collect_form(match):
            nonlocal form_spec
            try:
                data = json.loads(match.group(1))
            except (ValueError, TypeError):
                _logger.warning("AI: malformed AI_FORM block dropped: %s", match.group(1)[:200])
                return ""
            form_spec = _clean_form(data) or form_spec
            return ""

        message = AI_FORM_RE.sub(_collect_form, message).strip()

        if form_spec is None and '[AI_FORM]' in message:
            parsed, message = _salvage_marker_json(message, '[AI_FORM]', '[/AI_FORM]', '{', '}')
            form_spec = _clean_form(parsed)

        # Safety net: the model asked for a confirmation but forgot the
        # options block — inject default confirm/cancel buttons so the user
        # is never left without clickable cards.
        if allow_option_fallback and not options and form_spec is None:
            options = _fallback_confirmation_options(message)
            if options:
                _logger.info("AI: injected fallback confirmation options")

        formatted_message = message
        if markdown:
            raw_html = markdown(message, extras=['fenced-code-blocks', 'tables', 'strike'])
            formatted_message = html_sanitize(raw_html)
        else:
            formatted_message = html_sanitize(message)
        # Styling hook: scopes the chat CSS (bordered tables, ...) to
        # AI-authored message bodies only
        formatted_message = Markup('<div class="o_ai_msg">{}</div>').format(
            Markup(formatted_message)
        )

        if options:
            formatted_message = Markup(formatted_message) + Markup(
                '<div class="o_ai_option_data">{}</div>'
            ).format(json.dumps(options[:AI_OPTIONS_MAX], ensure_ascii=False))
        if form_spec and not options:
            formatted_message = Markup(formatted_message) + Markup(
                '<div class="o_ai_form_data">{}</div>'
            ).format(json.dumps(form_spec, ensure_ascii=False))
        channel.sudo().message_post(
            author_id=self.partner_id.id,
            body=formatted_message,
            message_type='comment',
            silent=True,
            subtype_xmlid='mail.mt_comment'
        )
        return bool(options or form_spec)

    @api.depends("sources_ids.status")
    def _compute_sources_fully_processed(self):
        for record in self:
            record.sources_fully_processed = not record.sources_ids.filtered(lambda s: s.status == 'processing')

    def _get_embedding_ai_model(self):
        self.ensure_one()
        provider = self._get_provider()
        supported_models = {
            'openai': 'text-embedding-3-small',
            'google': 'gemini-embedding-001',
            'qwen': 'text-embedding-v2',
        }
        if provider not in supported_models:
            # return ""
            raise UserError(_("Only OpenAI/Google/QWen are supported. Invalid provider: %s") % provider)
        return supported_models[provider]

    def _get_llm_response_with_sources(self, llm_response):
        """
        Parses inline citations (e.g., [SOURCE:210]) from each LLM message,
        replaces them with clickable sequential superscript numbers, and enriches
        the message content with a numbered list of corresponding source names
        and links.

        :param llm_response: The list of messages from the LLM
        :type llm_response: list[str]
        :return: The list of messages with the sources added
        :rtype: list[str]
        """
        llm_response_with_sources = []
        base_url = self.get_base_url()
        link_attrs = 'target="_blank" rel="noreferrer noopener"'

        for message_content in llm_response:
            unique_attachment_ids = get_attachment_ids_from_text(message_content)
            attachment_data = {}
            accessible_sources = self.env['ai.en.agent.source']
            if unique_attachment_ids:
                sources = self.env['ai.en.agent.source'].search([
                    ('attachment_id', 'in', unique_attachment_ids),
                    ('agent_id', '=', self.id),
                ])
                accessible_sources = sources.filtered(lambda s: s.user_has_access)
                for source in accessible_sources:
                    attachment_data[source.attachment_id.id] = {
                        'source_name': source.name,
                        'url': source.url or f"{base_url}/web/content/{source.attachment_id.id}",
                    }

            new_content = apply_numeric_citations(message_content, attachment_data, link_attrs=link_attrs)
            llm_response_with_sources.append(new_content)

        return llm_response_with_sources

    def action_refresh_sources(self):
        """
        Refresh the sources to show the new status if any was changed by the cron.
        """
        self.ensure_one()
        return {
            'type': 'ir.actions.client',
            'tag': 'soft_reload',
        }

