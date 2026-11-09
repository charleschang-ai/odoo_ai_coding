# -*- coding: utf-8 -*-
import logging
from datetime import timedelta

from odoo import api, fields, models

_logger = logging.getLogger(__name__)


class AiTokenUsage(models.Model):
    _inherit = 'ai.token.usage'

    @api.model
    def get_usage_dashboard_data(self, days=30):
        """Aggregate AI usage data for the dashboard.

        sudo-reads ai.token.usage records within the last *days* days and
        returns KPIs, groupings by model and agent, and the most recent records.

        :param int days: lookback window (7, 30, or 90)
        :returns: dict with keys kpis, by_model, by_agent, recent
        """
        since = fields.Datetime.now() - timedelta(days=days)
        domain = [('create_date', '>=', since)]
        fields_list = [
            'id', 'create_date', 'agent_id', 'llm_model', 'provider',
            'input_tokens', 'output_tokens', 'total_tokens',
            'api_calls', 'cost',
        ]

        # sudo(): read-only org-wide aggregation for the dashboard —
        # the menu is already gated to group_ai_manager
        records = self.sudo().search_read(
            domain, fields_list, order='id desc')

        # ── KPIs ────────────────────────────────────────────────
        total_cost = 0.0
        total_input_tokens = 0
        total_output_tokens = 0
        total_tokens_sum = 0
        total_api_calls = 0

        for r in records:
            total_cost += r['cost'] or 0.0
            total_input_tokens += r['input_tokens'] or 0
            total_output_tokens += r['output_tokens'] or 0
            total_tokens_sum += r['total_tokens'] or 0
            total_api_calls += r['api_calls'] or 0

        avg_cost_per_call = round(total_cost / total_api_calls, 6) if total_api_calls else 0.0

        kpis = {
            'total_cost': round(total_cost, 6),
            'total_input_tokens': total_input_tokens,
            'total_output_tokens': total_output_tokens,
            'total_tokens': total_tokens_sum,
            'api_calls': total_api_calls,
            'avg_cost_per_call': avg_cost_per_call,
        }

        # ── By model ───────────────────────────────────────────
        by_model_map = {}
        for r in records:
            model = r['llm_model'] or 'Unknown'
            by_model_map.setdefault(model, 0.0)
            by_model_map[model] += r['cost'] or 0.0

        by_model = [
            {'llm_model': model, 'cost': round(cost, 6)}
            for model, cost in sorted(
                by_model_map.items(), key=lambda kv: kv[1], reverse=True)
        ]

        # ── By agent ───────────────────────────────────────────
        by_agent_map = {}
        for r in records:
            agent_name = r['agent_id'] and r['agent_id'][1] or '(none)'
            by_agent_map.setdefault(agent_name, 0.0)
            by_agent_map[agent_name] += r['cost'] or 0.0

        by_agent = [
            {'agent_name': name, 'cost': round(cost, 6)}
            for name, cost in sorted(
                by_agent_map.items(), key=lambda kv: kv[1], reverse=True)
        ]

        # ── Recent ─────────────────────────────────────────────
        recent = []
        for r in records[:20]:
            recent.append({
                'id': r['id'],
                'create_date': r['create_date'],
                'agent_name': r['agent_id'] and r['agent_id'][1] or '(none)',
                'llm_model': r['llm_model'] or 'Unknown',
                'provider': r['provider'] or 'Unknown',
                'total_tokens': r['total_tokens'] or 0,
                'cost': round(r['cost'] or 0.0, 6),
            })

        return {
            'kpis': kpis,
            'by_model': by_model,
            'by_agent': by_agent,
            'recent': recent,
        }
