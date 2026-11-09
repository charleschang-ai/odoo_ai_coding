# -*- coding: utf-8 -*-
import re

from odoo import fields, models

from odoo.addons.ai_chatbot_enhance.orm.field_vector import Vector

ARTIFACT_TYPES = [
    ('manifest', 'Manifest'),
    ('model', 'Model (Python)'),
    ('view', 'View (XML)'),
    ('security', 'Security'),
    ('data', 'Data (XML/CSV)'),
    ('menu', 'Menu / Action'),
    ('controller', 'Controller'),
    ('wizard', 'Wizard'),
    ('report', 'Report'),
    ('js', 'JS / OWL'),
    ('test', 'Test'),
    ('other', 'Other'),
]


class AICodeSnippet(models.Model):
    """Version-accurate Odoo idiom library (Context7-style).

    Small, curated code examples + rules tagged per artifact type. The agent
    pulls them with the odoo_docs tool (query + token budget) while generating
    a file of that type, so it writes Odoo 19 idioms instead of what its
    training data remembers from older versions.
    """
    _name = 'ai.code.snippet'
    _description = 'AI Code Writing Idiom Snippet'
    _order = 'artifact_type, sequence, id'

    active = fields.Boolean(default=True)
    sequence = fields.Integer(default=10)
    topic = fields.Char(
        string='Topic', required=True, index=True,
        help="Short retrieval key, e.g. 'unique constraint', 'compute field', 'list view'.")
    title = fields.Char(string='Title', required=True)
    artifact_type = fields.Selection(ARTIFACT_TYPES, string='Artifact', required=True,
                                     default='other', index=True)
    odoo_version = fields.Char(string='Odoo Version', default='19.0', required=True)
    code = fields.Text(string='Snippet / Rule', required=True,
                       help="The example code or the rule text shown to the agent.")
    source_ref = fields.Char(string='Source',
                             help="Where this idiom comes from (file path, doc URL).")
    embedding = Vector(size=1536, string='Embedding')
    embedding_model = fields.Char(string='Embedding Model', readonly=True)

    # ══════════════════════════════════════════════════════════════════
    # Retrieval (Context7-style: query + token budget -> trimmed snippets)
    # ══════════════════════════════════════════════════════════════════

    def _target_odoo_version(self):
        """Series this retrieval targets: the running task's detected version,
        else the server series. '*' snippets are always eligible."""
        task_id = self.env.context.get('ai_code_task_id')
        if task_id:
            task = self.env['ai.code.task'].sudo().browse(task_id)
            if task.odoo_version:
                return task.odoo_version
        return self.env['ai.code.workspace']._server_odoo_series()

    def tool_odoo_docs(self, query='', artifact_type='', budget_tokens=2000):
        """Return the most relevant idiom snippets for the query, trimmed to
        the token budget. Filtered to the TARGET Odoo version (+ version-neutral
        '*' snippets) so an Odoo 18 project never gets a 19-only idiom."""
        budget_tokens = max(300, min(int(budget_tokens or 2000), 8000))
        version = self._target_odoo_version()
        domain = [('odoo_version', 'in', [version, '*'])]
        if artifact_type:
            domain.append(('artifact_type', '=', artifact_type))
        snippets = self.sudo().search(domain)
        if not snippets:
            # no version-specific snippet yet: fall back to any version so the
            # agent still gets guidance (most base idioms are cross-version)
            fallback = [('artifact_type', '=', artifact_type)] if artifact_type else []
            snippets = self.sudo().search(fallback)
        if not snippets:
            return "No snippet in the idiom library" + (
                f" for artifact type {artifact_type!r}." if artifact_type else ".")

        words = [w for w in re.findall(r'[a-z0-9_]+', (query or '').lower()) if len(w) > 2]
        scored = []
        for snippet in snippets:
            topic = (snippet.topic or '').lower()
            title = (snippet.title or '').lower()
            code = (snippet.code or '').lower()
            score = sum(3 for w in words if w in topic) \
                + sum(2 for w in words if w in title) \
                + sum(1 for w in words if w in code)
            scored.append((score, -snippet.sequence, snippet))
        scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
        if words and scored and scored[0][0] == 0 and not artifact_type:
            return (f"No idiom snippet matches {query!r}. Retry with an artifact_type "
                    f"or broader keywords. Available topics: "
                    + ", ".join(sorted(set(snippets.mapped('topic')))[:30]))

        chunks, spent = [], 0
        for score, _seq, snippet in scored:
            if words and score == 0 and chunks:
                break  # defaults only fill the gap when nothing matched
            text = (f"### [{snippet.artifact_type}] {snippet.title}\n"
                    f"{snippet.code.strip()}\n")
            cost = len(text) // 3 + 1
            if spent + cost > budget_tokens and chunks:
                break
            chunks.append(text)
            spent += cost
        return "\n".join(chunks) if chunks else "No snippet fits the token budget."
