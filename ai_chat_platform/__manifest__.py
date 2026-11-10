# -*- coding: utf-8 -*-
{
    'name': 'AI Platform | 聊天平台',
    'version': '19.0.1.0.0',
    'category': 'Productivity/Discuss',
    'summary': 'Full-page AI chat workspace: conversation history sidebar + modern chat UI, '
               'built with own OWL components on top of AI Chatbot Enhance discuss channels.',
    'description': """
AI Platform (聊天平台)
======================
A standalone, full-page chat workspace for the AI agents of *AI Chatbot Enhance*:

- Left sidebar with the conversation history (new chat / rename / delete),
  agent selector and current user info.
- Main chat area with user / assistant bubbles, markdown-rendered answers,
  quick-reply option chips and AI forms, suggestion chips for a new chat.
- Composer with file upload (analyzed by the AI), plus-menu, token usage footer.

Everything is stored in standard Odoo discuss channels (channel_type = ai_chat),
so conversations are shared with the Discuss chat windows of the base module.
""",
    'author': 'Don Shan',
    'maintainer': 'Don Shan',
    'depends': ['ai_chatbot_enhance'],
    'data': [
        'views/ai_platform_menus.xml',
    ],
    'assets': {
        'web.assets_backend': [
            'ai_chat_platform/static/src/**/*',
        ],
    },
    'license': 'OPL-1',
    'images': ['static/description/icon.png'],
    'installable': True,
    'auto_install': False,
    'application': False,
}
