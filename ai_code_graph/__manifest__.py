# -*- coding: utf-8 -*-
{
    'name': 'AI Code Graph | Odoo Code Structure Visualization',
    'version': '19.0.1.0.0',
    'category': 'Productivity/Discuss',
    'summary': 'Interactive knowledge graph of your Odoo codebase: models, fields, views, '
               'menus and JS components with their cross-layer relationships.',
    'description': """
Builds a relationship graph of the Odoo codebase (Python models/fields/methods,
XML views/actions/menus, JS components/patches) and renders it as an interactive
force-directed graph under AI Studio. Also exposes read-only graph query tools
to AI chat agents (model map, blast radius).
    """,
    'author': 'Don Shan',
    'maintainer': 'Don Shan',
    'depends': ['ai_chatbot_enhance'],
    'data': [
        'security/ir.model.access.csv',
        'data/ai_tools_data.xml',
        'data/ir_cron_data.xml',
        'views/ai_code_graph_views.xml',
        'views/ir_model_views.xml',
        'views/ai_code_graph_menus.xml',
        'data/auto_rebuild.xml',
    ],
    'assets': {
        'web.assets_backend': [
            'ai_code_graph/static/lib/d3/d3.min.js',
            'ai_code_graph/static/src/**/*',
        ],
    },
    'license': 'OPL-1',
    'installable': True,
    'auto_install': False,
    'application': False,
}
