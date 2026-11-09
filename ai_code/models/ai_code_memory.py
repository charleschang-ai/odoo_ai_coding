# -*- coding: utf-8 -*-
import re

from odoo import fields, models

from odoo.addons.ai_chatbot_enhance.orm.field_vector import Vector

_STOP = {"the", "and", "for", "with", "this", "that", "有", "的", "了", "和"}
_DUP_RATIO = 0.75


def _tokens(s):
    return [w for w in re.findall(r"[a-z0-9_]+|[一-鿿]+", (s or "").lower())
            if len(w) > 1 and w not in _STOP]


def _dup_features(s):
    """Dedup features: English by word, Chinese by adjacent bigrams (a whole
    Chinese run would otherwise be ONE token and never match a rewording)."""
    feats = set()
    for tok in _tokens(s):
        if tok[0] < "一":
            feats.add(tok)
        elif len(tok) == 1:
            feats.add(tok)
        else:
            feats.update(tok[i:i + 2] for i in range(len(tok) - 1))
    return feats


def similar(a, b):
    fa, fb = _dup_features(a), _dup_features(b)
    if not fa or not fb:
        return False
    return len(fa & fb) / min(len(fa), len(fb)) >= _DUP_RATIO


class AICodeMemory(models.Model):
    """Cross-task experience memory (claude-mem style).

    After a turn that changed files, the cheap model distills 1-4 reusable
    observations. New turns inject the top-weighted ones into the system
    prompt, and the agent can search them with mem_search / mem_get.
    Re-learning the same lesson bumps its weight instead of duplicating it.
    """
    _name = 'ai.code.memory'
    _description = 'AI Coding Experience Memory'
    _order = 'weight desc, id desc'

    active = fields.Boolean(default=True)
    kind = fields.Selection([
        ('convention', 'Project Convention'),
        ('decision', 'Design Decision'),
        ('pitfall', 'Pitfall / Fix'),
        ('outcome', 'Task Outcome'),
    ], string='Kind', required=True, default='outcome', index=True)
    text = fields.Text(string='Memory', required=True)
    working_dir = fields.Char(string='Workspace', index=True,
                              help="Workspace the memory was learned in (empty = global).")
    source_task_id = fields.Many2one('ai.code.task', string='Source Task',
                                     ondelete='set null')
    weight = fields.Float(string='Weight', default=1.0,
                          help="Bumped when a memory proves useful again; decays over time.")
    embedding = Vector(size=1536, string='Embedding')
    embedding_model = fields.Char(string='Embedding Model', readonly=True)

    def _compute_display_name(self):
        for memory in self:
            text = (memory.text or '').strip().replace('\n', ' ')
            memory.display_name = text[:80] or f"memory #{memory.id}"

    # ── writing ────────────────────────────────────────────────────────
    def store(self, kind, text, working_dir='', source_task=None, weight=1.0):
        """Store a memory; a near-duplicate of an existing one (same workspace)
        bumps that one's weight instead of adding a copy."""
        text = (text or '').strip()
        if len(text) < 12:
            return self.browse()
        Memory = self.sudo()
        domain = [('kind', '=', kind)]
        if working_dir:
            domain += ['|', ('working_dir', '=', working_dir), ('working_dir', '=', False)]
        for existing in Memory.search(domain, limit=300):
            if similar(text, existing.text):
                existing.write({'weight': existing.weight + weight,
                                'source_task_id': source_task.id if source_task
                                else existing.source_task_id.id})
                return existing
        return Memory.create({
            'kind': kind if kind in ('convention', 'decision', 'pitfall', 'outcome') else 'outcome',
            'text': text[:2000],
            'working_dir': working_dir or False,
            'source_task_id': source_task.id if source_task else False,
            'weight': weight,
        })

    def top_for_injection(self, working_dir='', limit=6):
        domain = ['|', ('working_dir', '=', working_dir or ''), ('working_dir', '=', False)]
        rows = self.sudo().search(domain, limit=limit)
        if not rows:
            return ''
        return "\n".join(f"- ({m.kind}) {(m.text or '').strip()[:220]}" for m in rows)

    # ── three-layer retrieval: cheap index -> full text ────────────────
    def tool_mem_search(self, query='', limit=8):
        limit = min(int(limit or 8), 20)
        memories = self.sudo().search([], limit=500)
        if not memories:
            return "No experience memory stored yet."
        words = _tokens(query)
        scored = []
        for memory in memories:
            text = (memory.text or '').lower()
            score = sum(1 for w in words if w in text) + memory.weight * 0.01
            scored.append((score, memory))
        scored.sort(key=lambda item: item[0], reverse=True)
        lines = [
            f"[{memory.id}] ({memory.kind}) {(memory.text or '').strip()[:100].replace(chr(10), ' ')}"
            for score, memory in scored[:limit] if not words or score >= 1
        ]
        return "\n".join(lines) if lines else f"No memory matches {query!r}."

    def tool_mem_get(self, memory_ids):
        if isinstance(memory_ids, (int, str)):
            memory_ids = [memory_ids]
        try:
            ids = [int(i) for i in memory_ids][:10]
        except (TypeError, ValueError):
            return "ERROR: memory_ids must be a list of integer ids from mem_search."
        memories = self.sudo().browse(ids).exists()
        if not memories:
            return "No memory found for those ids."
        blocks = [f"[{memory.id}] ({memory.kind}, weight {memory.weight:.1f})\n{memory.text}"
                  for memory in memories]
        for memory in memories:
            memory.weight += 0.1
        return "\n\n".join(blocks)
