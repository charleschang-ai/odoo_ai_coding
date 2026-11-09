# -*- coding: utf-8 -*-
"""Post-init: create the coding agent from Python.

ai.agent.bot.create() builds a res.partner, and during XML data loading the
partner-level defaults of other modules (e.g. account.autopost_bills, NOT NULL)
are not applied, which crashes the load. Creating the agent through the normal
ORM create in a post-init hook applies all defaults correctly, so we register
its xmlid by hand afterwards for env.ref('ai_code.coding_agent') to resolve.
"""
import logging

_logger = logging.getLogger(__name__)

_XMLID = 'ai_code.coding_agent'

SYSTEM_PROMPT = ("This agent writes Odoo 19 modules. Its chat is handled by the "
                 "AI Code task runner (requirement analysis, planning, code generation).")


def _post_init_create_coding_agent(env):
    existing = env.ref(_XMLID, raise_if_not_found=False)
    if existing:
        existing.write({'is_coding_agent': True, 'active': True})
        return

    manager_group = env.ref('ai_chatbot_enhance.group_ai_manager', raise_if_not_found=False)
    agent = env['ai.agent.bot'].create({
        'name': 'Odoo Coding Agent',
        'subtitle': 'Analyzes your codebase and writes Odoo 19 modules from a requirement.',
        'is_coding_agent': True,
        'is_system_agent': True,
        'llm_model': 'deepseek-v4-pro',
        'response_style': 'analytical',
        'system_prompt': SYSTEM_PROMPT,
        'allowed_group_ids': [(6, 0, manager_group.ids)] if manager_group else False,
    })
    module, name = _XMLID.split('.')
    env['ir.model.data'].create({
        'name': name,
        'module': module,
        'model': 'ai.agent.bot',
        'res_id': agent.id,
        'noupdate': True,
    })
    _logger.info("ai_code: created coding agent %s", agent.id)
