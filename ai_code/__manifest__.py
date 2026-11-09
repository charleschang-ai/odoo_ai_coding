# -*- coding: utf-8 -*-
{
    'name': 'AI Code | Odoo Module Writing Agent',
    'version': '19.0.2.0.0',
    'category': 'Productivity/Discuss',
    'summary': 'Chat-first AI coding agent for Odoo modules: a single tool-using loop '
               'with a live todo list, read-before-edit guardrails, pre-write validation, '
               'git checkpoints with step rollback, and an isolated sandbox install/test '
               'loop with honest verification reporting.',
    'description': """
An Odoo-specialist coding agent built on ai_chatbot_enhance (agent runtime),
ai_code_graph (code knowledge graph) and ai_chat_platform (chat workspace UI).

Workflow: pick a working directory -> describe the requirement -> the agent
plans (todo list you watch live), reads the real code, writes files (validated
before saving, one git checkpoint per step), runs the sandbox install/smoke
loop and repairs its own errors, then reports what was verified. It only asks
when a decision genuinely belongs to you.
    """,
    'author': 'Don Shan',
    'maintainer': 'Don Shan',
    'depends': ['ai_chatbot_enhance', 'ai_code_graph', 'ai_chat_platform'],
    'data': [
        'security/ir.model.access.csv',
        'data/ai_code_snippet_data.xml',
        'data/ai_code_snippet_data_v2.xml',
        'views/ai_code_task_views.xml',
        'views/ai_code_workspace_views.xml',
        'views/res_config_settings_views.xml',
        'views/ai_code_menus.xml',
    ],
    'assets': {
        'web.assets_backend': [
            'ai_code/static/src/**/*',
        ],
    },
    'post_init_hook': '_post_init_create_coding_agent',
    'license': 'OPL-1',
    'installable': True,
    'auto_install': False,
    'application': False,
}
