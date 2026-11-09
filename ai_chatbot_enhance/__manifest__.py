# -*- coding: utf-8 -*-
################################################################################
#    Author: Don Shan
#
################################################################################
{
    'name': 'AI Chatbot Enhance | RAG & Vector Search | Odoo Query & Record Automation | AI RAG | AI Skills | AI Tools| AI Copilot | AI Assistant for Discuss | AI Studio | Odoo Studio',
    'version': '19.0.1.0.2',
    'category': 'Productivity/Discuss',
    'summary': "Key Features:- RAG (Retrieval-Augmented Generation): Elevate AI accuracy by ingesting custom documents and URLs, creating an intelligent corporate knowledge base.- OS & System Automation: Bridge the gap between generative AI and core operations, enabling autonomous task execution and system-level workflows.",
    'description': "A powerful, all-in-one AI orchestration module designed for Odoo. Effortlessly deploy and configure various large AI models while expanding their capabilities with advanced Retrieval-Augmented Generation (RAG) using your own documentation and web URLs. Armed with OS-level integration features, this module goes beyond simple chat, driving intelligent automation, contextual data analysis, and autonomous workflow execution directly within your Odoo environment.",
    'author': 'Don Shan',
    'maintainer': 'Don Shan',
    'depends': ['mail'],
    'external_dependencies': {
        'python': ['markdown2'],
    },
    'data': [
        'security/ai_security.xml',
        'data/ir_actions_server_data.xml',
        'data/ai_topic_data.xml',
        'data/ai_agent_data.xml',
        'data/ai_composer_data.xml',
        'data/ai_providers_data.xml',
        'data/ai_skills_crud_actions.xml',
        'data/ai_skills_report_confirm_send_email.xml',
        'data/ai_studio_actions_data.xml',
        'data/ai_skill_builder_actions.xml',
        'data/ai_industry_skills.xml',
        'data/ai_manufacturing_skills.xml',
        'data/ai_llm_price_data.xml',
        'data/ir_ai_cron.xml',
        'security/ir.model.access.csv',
        'views/ai_agent_views.xml',
        'views/ai_composer_views.xml',
        'views/ai_providers_views.xml',
        'views/res_config_settings_views.xml',
        'views/ai_topic_views.xml',
        'views/ai_actions_views.xml',
        'views/ai_audit_views.xml',
        'views/ai_analysis_views.xml',
        'views/ai_studio_views.xml',
        'views/ai_menus.xml',
        'views/templates.xml',
        'wizard/ai_agent_source_wizard_views.xml',
    ],
    'assets': {
        'web.assets_backend': [
            ('after', 'web/static/src/views/form/form_controller.js', 'ai_chatbot_enhance/static/src/web/form_controller_patch.js'),
            'ai_chatbot_enhance/static/src/**/*',
            ('remove', 'ai_chatbot_enhance/static/src/web/lazy/**'),
        ],
        'web.assets_backend_lazy': [
            'ai_chatbot_enhance/static/src/web/lazy/*',
        ],
        'mail.assets_public': [
            'ai_chatbot_enhance/static/src/discuss/core/common/**/*',
            # hide the raw option/form JSON payloads in public chats too
            'ai_chatbot_enhance/static/src/discuss/message/ai_option_buttons.scss',
        ],
        'portal.assets_chatter_helpers': [
            'ai_chatbot_enhance/static/src/discuss/core/common/**/*',
            'ai_chatbot_enhance/static/src/discuss/message/ai_option_buttons.scss',
        ],
    },
    'images': ['static/description/icon.jpg'],
    'pre_init_hook': '_pre_init_ai_agent',
    # 'post_init_hook': '_auto_install_ai',
    'license': 'OPL-1',
    'installable': True,
    'auto_install': False,
    'application': True,
    'price': 450,
    'currency': "USD",
}
