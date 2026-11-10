# -*- coding: utf-8 -*-
{
    'name': 'AI Usage Dashboard',
    'version': '19.0.1.0.0',
    'category': 'Productivity/Discuss',
    'summary': 'Dashboard for AI token usage and cost analytics',
    'author': 'Don Shan',
    'maintainer': 'Don Shan',
    'depends': ['ai_chatbot_enhance', 'web'],
    'data': [
        'security/ir.model.access.csv',
        'views/ai_usage_dashboard_menus.xml',
    ],
    'assets': {
        'web.assets_backend': [
            'ai_usage_dashboard/static/src/dashboard/*',
        ],
    },
    'license': 'LGPL-3',
    'images': ['static/description/icon.png'],
    'installable': True,
    'application': False,
}
