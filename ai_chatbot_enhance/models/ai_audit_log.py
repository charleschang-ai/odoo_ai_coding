import logging
from datetime import timedelta

from odoo import fields, models

_logger = logging.getLogger(__name__)

DEFAULT_RETENTION_DAYS = 180


class AiAuditLog(models.Model):
    """Immutable audit trail of every AI tool execution.

    Records are written with sudo() from the tool execution choke point
    (ir.actions.server._get_ai_tools/_exec_tool); regular users have no
    access at all, administrators can only read.
    """
    _name = 'ai.audit.log'
    _description = 'AI Operation Audit Log'
    _order = 'id desc'

    summary = fields.Char(string='Summary', readonly=True,
                          help="One-line description of what the AI actually did.")
    operation_type = fields.Selection([
        ('read', 'Read / Search'),
        ('create', 'Create'),
        ('update', 'Update'),
        ('archive', 'Archive / Delete'),
        ('workflow', 'Workflow Action'),
        ('send', 'Send Email'),
        ('print', 'Print Report'),
        ('undo', 'Undo'),
        ('other', 'Other'),
    ], string='Operation', index=True, readonly=True)
    user_id = fields.Many2one('res.users', string='User', required=True, index=True, readonly=True)
    agent_id = fields.Many2one('ai.agent.bot', string='AI Agent', index=True, readonly=True, ondelete='set null')
    channel_id = fields.Many2one('discuss.channel', string='Chat Channel', readonly=True, ondelete='set null')
    action_id = fields.Many2one('ir.actions.server', string='AI Skill', readonly=True, ondelete='set null')
    tool_name = fields.Char(string='Tool', index=True, readonly=True)
    model_name = fields.Char(string='Target Model', index=True, readonly=True)
    arguments = fields.Text(string='Arguments', readonly=True)
    result = fields.Text(string='Result', readonly=True)
    has_error = fields.Boolean(string='Failed', index=True, readonly=True)
    error = fields.Text(string='Error', readonly=True)
    duration = fields.Float(string='Duration (s)', digits=(10, 3), readonly=True, aggregator='avg')

    # Undo tracking: filled when the tool touched one identifiable record
    target_model = fields.Char(string='Affected Model', index=True, readonly=True)
    target_record_id = fields.Integer(string='Affected Record ID', readonly=True)
    undone = fields.Boolean(string='Undone', readonly=True,
                            help="This operation was reverted by the Undo skill.")

    def _infer_operation_type(self, tool_name):
        # Custom tools may be named in Chinese, so both English and Chinese
        # keywords are matched. Mutation keywords are checked before read
        # keywords: a name matching neither stays 'other' (treated as a
        # mutation by the confirmation gate — fail closed).
        n = (tool_name or '').lower()
        if 'undo' in n or '撤销' in n or '撤銷' in n:
            return 'undo'
        if any(k in n for k in ('create', 'duplicate', '创建', '新建', '複製', '复制')):
            return 'create'
        if any(k in n for k in ('update', '更新', '修改')):
            return 'update'
        if any(k in n for k in ('archive', 'deactivate', 'delete', '归档', '歸檔', '删除', '刪除')):
            return 'archive'
        if any(k in n for k in ('email', 'send', '发送', '發送', '邮件', '郵件')):
            return 'send'
        if 'print' in n or '打印' in n or '列印' in n:
            return 'print'
        if any(k in n for k in ('trigger', 'payment', 'invoice', 'activate', '付款', '开票', '開票', '发票', '發票')):
            return 'workflow'
        if any(k in n for k in (
            'search', 'get', 'read', 'check', 'list', 'fields', 'stock',
            'open', 'menu', 'compute', 'measure', 'adjust', 'count', 'preview',
            '查询', '查詢', '搜索', '搜尋', '读取', '讀取', '查看', '统计',
            '統計', '列出', '库存', '庫存', '打开', '打開', '报表', '報表',
        )):
            return 'read'
        return 'other'

    def _cron_gc_logs(self):
        """Delete audit logs older than the configured retention period.

        Configurable via ir.config_parameter 'ai_chatbot_enhance.audit_retention_days'
        (default 180 days).
        """
        param = self.env['ir.config_parameter'].sudo().get_param(
            'ai_chatbot_enhance.audit_retention_days', str(DEFAULT_RETENTION_DAYS))
        try:
            retention_days = max(1, int(param))
        except ValueError:
            retention_days = DEFAULT_RETENTION_DAYS
        cutoff = fields.Datetime.now() - timedelta(days=retention_days)
        old_logs = self.sudo().search([('create_date', '<', cutoff)])
        if old_logs:
            count = len(old_logs)
            old_logs.unlink()
            _logger.info("AI audit: garbage-collected %d logs older than %d days", count, retention_days)
