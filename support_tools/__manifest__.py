# -*- coding: utf-8 -*-
{
    'name': 'Support Tools | Remote Odoo Diagnostics via AI',
    'version': '19.0.1.0.0',
    'category': 'Productivity/Discuss',
    'summary': "Let the AI assistant connect to a customer's remote Odoo instance "
               "(read-only XML-RPC) and diagnose problems such as unposted invoices "
               "or stock mismatches — without a remote desktop session.",
    'description': """
Support Tools — Remote Odoo Diagnostics
=======================================

Extends *AI Chatbot Enhance* so your support team can point the AI at a
customer's Odoo instance and ask, in plain language, why something is broken.

Key ideas
---------
* **Remote instances** — store each customer's connection details (URL,
  database, login, API key). The API key is masked and manager-only.
* **Read-only by design** — the backend connector only permits read methods
  (search/read/search_read/read_group/fields_get/...). Write/create/unlink are
  rejected at the connector layer, independent of the remote user's rights.
* **AI diagnosis** — a diagnostic session drives the LLM configured in
  *AI Chatbot Enhance* (its providers, keys, token budget and usage logging are
  reused). The model investigates the remote database through the read-only
  tools and returns a structured diagnosis in Chinese.
""",
    'author': 'AstaCRM',
    'website': 'https://www.astacrm.com',
    'license': 'LGPL-3',
    'depends': ['ai_chatbot_enhance'],
    'data': [
        'security/ir.model.access.csv',
        'data/support_data.xml',
        'data/support_agent_data.xml',
        'views/support_remote_instance_views.xml',
        'views/support_diagnostic_session_views.xml',
        'views/support_menus.xml',
    ],
    'assets': {
        'web.assets_backend': [
            'support_tools/static/src/scss/support_tools.scss',
        ],
    },
    'installable': True,
    'application': False,
}
