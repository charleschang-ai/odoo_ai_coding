# Part of AI Code Graph.
import types
from collections import defaultdict

from odoo import api, fields, models
from odoo.modules import get_module_path

INDEX_VERSION = 4  # keep in sync with ai_code_graph_builder.INDEX_VERSION

DEFAULT_EDITOR_URL = 'vscode://file/{path}:{line}'


def _short_source(func):
    """('sale/models/sale_order.py', 1024, absolute_path) from a function
    object, or ('', 0, '')."""
    try:
        code = func.__code__
        abs_path = code.co_filename.replace('\\', '/')
        path = abs_path
        if '/addons/' in path:
            path = path.split('/addons/')[-1]
        else:
            path = '/'.join(path.rsplit('/', 3)[1:])
        return path, code.co_firstlineno, abs_path
    except AttributeError:
        return '', 0, ''

NODE_TYPES = [
    ('module', "Module"),
    ('model', "Model"),
    ('field', "Field"),
    ('method', "Method"),
    ('view', "View"),
    ('qweb_template', "QWeb Template"),
    ('action', "Action"),
    ('menu', "Menu"),
    ('js_component', "JS Component"),
    ('js_service', "JS Service"),
    ('js_function', "JS Function"),
    ('server_action', "Server Action"),
]

LAYERS = [
    ('python', "Python"),
    ('xml', "XML"),
    ('js', "JavaScript"),
]


class AiCodeNode(models.Model):
    _name = 'ai.code.node'
    _description = "Code Graph Node"
    _order = 'node_type, technical_key'
    _rec_name = 'name'

    name = fields.Char(required=True, index=True)
    technical_key = fields.Char(
        required=True, index=True,
        help="Stable identifier inside its type, e.g. 'sale.order', "
             "'sale.order.partner_id', 'sale.view_order_form'.")
    node_type = fields.Selection(NODE_TYPES, required=True, index=True)
    layer = fields.Selection(LAYERS, required=True, default='python', index=True)
    module_name = fields.Char(index=True, help="Module that defines this element.")
    model_name = fields.Char(index=True, help="Related Odoo model, when applicable.")
    file_path = fields.Char(help="Source file, relative to the module directory.")
    line_no = fields.Integer()
    ref_model = fields.Char(help="Odoo model of the backing record (e.g. ir.model).")
    ref_id = fields.Integer(help="ID of the backing record.")
    meta = fields.Json()
    is_skeleton = fields.Boolean(
        default=False,
        help="Created only as the target of a relation; not deeply indexed itself.")
    in_degree = fields.Integer(default=0, help="Denormalized incoming edge count.")
    out_degree = fields.Integer(default=0, help="Denormalized outgoing edge count.")
    degree = fields.Integer(compute='_compute_degree', store=True)

    out_edge_ids = fields.One2many('ai.code.edge', 'source_id', string="Outgoing Edges")
    in_edge_ids = fields.One2many('ai.code.edge', 'target_id', string="Incoming Edges")

    _type_key_uniq = models.Constraint(
        'UNIQUE(node_type, technical_key)',
        "A node with this type and technical key already exists.",
    )

    @api.depends('in_degree', 'out_degree')
    def _compute_degree(self):
        for node in self:
            node.degree = node.in_degree + node.out_degree

    # ══════════════════════════════════════════════
    # Graph API for the client action
    # ══════════════════════════════════════════════

    @api.model
    def _editor_url_template(self):
        """Click-to-open link template; override with the config parameter
        ai_code_graph.editor_url (e.g. PyCharm: 'pycharm://open?file={path}&line={line}')."""
        return (self.env['ir.config_parameter'].sudo().get_param(
            'ai_code_graph.editor_url') or DEFAULT_EDITOR_URL).strip()

    def _editor_url(self, module, rel_path, line, cache, abs_path=''):
        """Editor deep link for a module-relative source position."""
        if not abs_path:
            if not (module and rel_path):
                return ''
            if module not in cache:
                cache[module] = get_module_path(module, display_warning=False) or ''
            if not cache[module]:
                return ''
            abs_path = f"{cache[module]}/{rel_path}"
        try:
            return cache.setdefault('__tmpl__', self._editor_url_template()).format(
                path=abs_path.replace('\\', '/'), line=line or 1)
        except (KeyError, IndexError, ValueError):
            return ''

    @api.model
    def _custom_module_names(self):
        """Names of the deep-indexed (custom) modules — the frontend
        highlights them so 'your code' pops out of core noise."""
        return self.search([('node_type', '=', 'module')]).filtered(
            lambda n: (n.meta or {}).get('is_custom')).mapped('technical_key')

    @api.model
    def _index_is_stale(self):
        """True when the stored index was built by an older extractor (or
        never built): the viewers show a rebuild banner."""
        stored = self.env['ir.config_parameter'].sudo().get_param(
            'ai_code_graph.index_version')
        return stored != str(INDEX_VERSION)

    def _node_payload(self, nodes):
        return [{
            'id': n.id,
            'key': n.technical_key,
            'name': n.name,
            'type': n.node_type,
            'layer': n.layer,
            'module': n.module_name or '',
            'model': n.model_name or '',
            'degree': n.degree,
            'skeleton': n.is_skeleton,
        } for n in nodes]

    @api.model
    def get_graph_data(self, focus_key=False, depth=2, layers=None,
                       edge_types=None, limit=300):
        """Return {nodes, edges, truncated, total, focus_id} for the viewer.

        Without focus: module-level overview (modules + dependency edges).
        With focus: BFS neighborhood of the focus node up to `depth` hops,
        filtered by layers/edge types, capped at `limit` nodes (closest first).
        """
        limit = max(20, min(int(limit or 300), 800))
        depth = max(1, min(int(depth or 2), 3))
        layers = set(layers or ['python', 'xml', 'js'])
        Edge = self.env['ai.code.edge']

        def _passes_layer(node):
            return node.node_type == 'module' or node.layer in layers

        if not focus_key:
            nodes = self.search([('node_type', '=', 'module')], limit=limit + 1)
            truncated = len(nodes) > limit
            nodes = nodes[:limit]
            node_ids = set(nodes.ids)
            edges = Edge.search([
                ('edge_type', '=', 'module_depends'),
                ('source_id', 'in', list(node_ids)),
                ('target_id', 'in', list(node_ids)),
            ])
            return {
                'nodes': self._node_payload(nodes),
                'edges': edges._edge_payload(),
                'truncated': truncated,
                'total': self.search_count([('node_type', '=', 'module')]),
                'focus_id': False,
                'stale': self._index_is_stale(),
            }

        focus = self.search([('technical_key', '=', focus_key)], limit=1)
        if not focus:
            focus = self.search([('technical_key', 'ilike', focus_key)], limit=1)
        if not focus:
            return {'nodes': [], 'edges': [], 'truncated': False,
                    'total': 0, 'focus_id': False, 'error': 'not_found'}

        selected = focus
        frontier = focus
        seen_ids = set(focus.ids)
        truncated = False
        for _hop in range(depth):
            if not frontier or len(selected) >= limit:
                break
            hop_edges = Edge.search([
                '|',
                ('source_id', 'in', frontier.ids),
                ('target_id', 'in', frontier.ids),
            ])
            if edge_types:
                hop_edges = hop_edges.filtered(lambda e: e.edge_type in edge_types)
            neighbor_ids = (set(hop_edges.mapped('source_id').ids)
                            | set(hop_edges.mapped('target_id').ids)) - seen_ids
            neighbors = self.browse(list(neighbor_ids)).filtered(_passes_layer)
            neighbors = neighbors.sorted(key=lambda n: -n.degree)
            room = limit - len(selected)
            if len(neighbors) > room:
                truncated = True
                neighbors = neighbors[:room]
            selected |= neighbors
            seen_ids |= set(neighbors.ids)
            frontier = neighbors

        node_ids = set(selected.ids)
        edges = Edge.search([
            ('source_id', 'in', list(node_ids)),
            ('target_id', 'in', list(node_ids)),
        ])
        if edge_types:
            edges = edges.filtered(lambda e: e.edge_type in edge_types)

        return {
            'nodes': self._node_payload(selected),
            'edges': edges._edge_payload(),
            'truncated': truncated,
            'total': len(selected),
            'focus_id': focus.id,
            'stale': self._index_is_stale(),
        }

    @api.model
    def search_nodes(self, term, limit=12):
        """Autocomplete for the viewer search box.

        Ranking: exact key > key suffix match (method/field name) > type
        priority (models and methods before views/fields) > degree. This is
        what makes 'action_confirm' land on the method, not a random view.
        """
        term = (term or '').strip()
        if not term:
            return []
        candidates = self.search([
            '|',
            ('technical_key', 'ilike', term),
            ('name', 'ilike', term),
        ], limit=80)
        type_priority = {'model': 0, 'method': 1, 'js_component': 2,
                         'js_function': 2, 'server_action': 2, 'module': 3,
                         'js_service': 4, 'view': 5, 'qweb_template': 6,
                         'action': 7, 'menu': 8, 'field': 9}
        lowered = term.lower()

        def _score(node):
            key = node.technical_key.lower()
            return (
                0 if key == lowered else 1,
                0 if key.endswith('.' + lowered) or key.split('.')[-1] == lowered else 1,
                1 if node.is_skeleton else 0,
                type_priority.get(node.node_type, 10),
                -node.degree,
            )

        ranked = sorted(candidates, key=_score)[:int(limit)]
        results = self._node_payload(self.browse([n.id for n in ranked]))
        # models known to the registry but absent from the index (fully core,
        # never referenced): offer them as virtual results -> live structure
        found_models = {r['key'] for r in results if r['type'] == 'model'}
        extra_models = self.env['ir.model'].sudo().search([
            '|', ('model', 'ilike', term), ('name', 'ilike', term),
            ('model', 'not in', list(found_models)),
        ], limit=4)
        virtual_id = -1
        for rec in extra_models:
            if rec.model in self.env and rec.model not in found_models:
                results.append({
                    'id': virtual_id, 'key': rec.model, 'name': rec.name or rec.model,
                    'type': 'model', 'layer': 'python', 'module': '',
                    'model': rec.model, 'degree': 0, 'skeleton': True,
                })
                virtual_id -= 1
        return results[:int(limit) + 4]

    def get_node_details(self):
        """Detail payload for the viewer sidebar."""
        self.ensure_one()
        by_type_out = defaultdict(list)
        by_type_in = defaultdict(list)
        for edge in self.out_edge_ids:
            by_type_out[edge.edge_type].append({
                'id': edge.target_id.id,
                'key': edge.target_id.technical_key,
                'name': edge.target_id.name,
                'type': edge.target_id.node_type,
            })
        for edge in self.in_edge_ids:
            by_type_in[edge.edge_type].append({
                'id': edge.source_id.id,
                'key': edge.source_id.technical_key,
                'name': edge.source_id.name,
                'type': edge.source_id.node_type,
            })

        def _summarize(groups):
            return [{
                'edge_type': etype,
                'count': len(items),
                'items': items[:12],
            } for etype, items in sorted(groups.items(), key=lambda kv: -len(kv[1]))]

        return {
            'id': self.id,
            'key': self.technical_key,
            'name': self.name,
            'type': self.node_type,
            'layer': self.layer,
            'module': self.module_name or '',
            'model': self.model_name or '',
            'file_path': self.file_path or '',
            'line_no': self.line_no,
            'ref_model': self.ref_model or '',
            'ref_id': self.ref_id,
            'meta': self.meta or {},
            'skeleton': self.is_skeleton,
            'out': _summarize(by_type_out),
            'in': _summarize(by_type_in),
        }

    # ══════════════════════════════════════════════
    # Call hierarchy (IDE-style caller/callee trees)
    # ══════════════════════════════════════════════

    @api.model
    def get_call_hierarchy(self, method_key, down_depth=3, up_depth=2):
        """Caller/callee trees for one method — the 'Ctrl+Alt+H' of Odoo.

        Downward: what this method calls, recursively.
        Upward: methods and view buttons that call it.
        Items with has_more=True can be lazily expanded by calling this
        again with that item's key.
        """
        node = self.search([
            ('node_type', '=', 'method'), ('technical_key', '=', method_key),
        ], limit=1)
        if not node:
            return {'error': 'not_found'}

        url_cache = {}

        def _entry(n, edge=None):
            edge_meta = (edge.meta or {}) if edge else {}
            return {
                'id': n.id,
                'key': n.technical_key,
                'ntype': n.node_type,
                'name': (n.name if n.node_type == 'server_action'
                         else n.technical_key.rsplit('.', 1)[-1]),
                'model': n.model_name or '',
                'module': n.module_name or '',
                'file': n.file_path or '',
                'line': n.line_no or 0,
                'url': self._editor_url(n.module_name, n.file_path,
                                        n.line_no, url_cache),
                'skeleton': n.is_skeleton,
                'kind': edge_meta.get('kind', ''),
                'call_file': edge_meta.get('file', ''),
                'call_line': edge_meta.get('line', 0),
                'children': [],
                'has_more': False,
                'ref_id': 0,
                'view_name': '',
            }

        def _down(n, depth, visited):
            items = []
            edges = n.out_edge_ids.filtered(
                lambda e: e.edge_type == 'method_calls').sorted(
                lambda e: (e.meta or {}).get('line') or 0)
            for edge in edges[:40]:
                target = edge.target_id
                item = _entry(target, edge)
                child_edges = target.out_edge_ids.filtered(
                    lambda e: e.edge_type == 'method_calls')
                if depth > 1 and target.id not in visited and child_edges:
                    item['children'] = _down(target, depth - 1, visited | {target.id})
                item['has_more'] = bool(child_edges) and not item['children']
                items.append(item)
            return items

        def _up(n, depth, visited):
            items = []
            for edge in n.in_edge_ids.filtered(
                    lambda e: e.edge_type == 'method_calls')[:40]:
                source = edge.source_id
                item = _entry(source, edge)
                caller_edges = source.in_edge_ids.filtered(
                    lambda e: e.edge_type in ('method_calls', 'view_calls_method'))
                if depth > 1 and source.id not in visited and caller_edges:
                    item['children'] = _up(source, depth - 1, visited | {source.id})
                item['has_more'] = bool(caller_edges) and not item['children']
                items.append(item)
            for edge in n.in_edge_ids.filtered(
                    lambda e: e.edge_type == 'view_calls_method')[:20]:
                view = edge.source_id
                item = _entry(view, edge)
                item.update({
                    'kind': 'button',
                    'name': (edge.meta or {}).get('string') or view.name,
                    'view_name': view.name,
                    'ref_id': view.ref_id or 0,
                })
                items.append(item)
            return items

        meta = node.meta or {}
        method_info = _entry(node)
        method_info.update({
            'defs': meta.get('defs') or [],
            'mro': meta.get('mro') or [],
            'unresolved': meta.get('unresolved') or [],
        })
        down = _down(node, max(1, min(int(down_depth), 5)), {node.id})
        up = _up(node, max(1, min(int(up_depth), 4)), {node.id})
        # empty tree + zero call edges in the whole index = index predates
        # the call extractor -> tell the user to rebuild instead of showing
        # a silently useless page
        needs_rebuild = False
        if not down and not up:
            needs_rebuild = not bool(self.env['ai.code.edge'].search_count(
                [('edge_type', '=', 'method_calls')], limit=1))
        return {
            'method': method_info,
            'down': down,
            'up': up,
            'needs_rebuild': needs_rebuild,
            'stale': self._index_is_stale(),
            'custom_modules': self._custom_module_names(),
        }

    @api.model
    def ai_tool_call_chain(self, model_name, method_name):
        """Text call hierarchy for chat agents."""
        key = f"{(model_name or '').strip()}.{(method_name or '').strip()}"
        data = self.sudo().get_call_hierarchy(key, down_depth=3, up_depth=2)
        if data.get('error'):
            return (f"Method '{key}' not found in the code graph. It is either "
                    f"outside the indexed scope (only custom addons are deep-"
                    f"indexed) or the index needs a rebuild.")
        method = data['method']
        lines = [f"# Call hierarchy: {key}"]
        if method.get('defs'):
            lines.append("Implementations:")
            for definition in method['defs']:
                super_note = " (calls super)" if definition.get('super') else ""
                lines.append(f"  - [{definition['module']}] "
                             f"{definition['file']}:{definition['line']}{super_note}")
        if method.get('mro'):
            lines.append("Override order (runtime MRO): "
                         + " -> ".join(reversed(method['mro'])))

        def _render(items, indent):
            for item in items:
                marker = {'super': '↥super', 'data': '✎data',
                          'button': '⏺button', 'cross': '→'}.get(item['kind'], '·')
                loc = (f"  [{item['module'] or 'core'}"
                       + (f" {item['file']}:{item['line']}" if item['file'] else "")
                       + "]")
                lines.append("  " * indent + f"{marker} {item['model']}."
                             f"{item['name']}(){loc}")
                _render(item['children'], indent + 1)
                if item['has_more']:
                    lines.append("  " * (indent + 1)
                                 + "… (deeper levels: call this tool on that method)")

        lines.append("\n## Calls (downward):")
        if data['down']:
            _render(data['down'], 1)
        else:
            lines.append("  (no resolved outgoing calls)")
        lines.append("\n## Called by (upward, incl. view buttons):")
        if data['up']:
            _render(data['up'], 1)
        else:
            lines.append("  (no indexed callers — may be called from core "
                         "code outside the indexed scope)")
        if method.get('unresolved'):
            lines.append("\nUnresolved dynamic calls in this method (static "
                         "analysis limit): " + ", ".join(method['unresolved']))
        lines.append("\nNote: only custom addons are deep-indexed; core-to-core "
                     "calls are not traced.")
        return "\n".join(lines)

    # ══════════════════════════════════════════════
    # Structure view (hierarchical, flowchart-style)
    # ══════════════════════════════════════════════

    @api.model
    def get_structure_data(self, focus_key):
        """Structured, human-readable breakdown of one model or module,
        rendered by the Structure view (no force layout)."""
        node = self.search([
            ('technical_key', '=', focus_key),
            ('node_type', 'in', ('model', 'module')),
        ], limit=1) or self.search([('technical_key', '=', focus_key)], limit=1)
        if not node:
            # model exists in the registry but was never referenced by the
            # index (fully core): serve it live anyway
            if focus_key in self.env:
                return dict(self._live_structure_for_model(self.browse(), focus_key),
                            stale=self._index_is_stale())
            return {'error': 'not_found'}
        if node.node_type == 'model':
            return dict(self._structure_for_model(node), stale=self._index_is_stale())
        if node.node_type == 'module':
            return dict(self._structure_for_module(node), stale=self._index_is_stale())
        return {'type': 'other', 'detail': node.get_node_details(),
                'stale': self._index_is_stale()}

    def _security_payload(self, model_name):
        """Live ACL + record-rule summary for one model (never indexed:
        security must always reflect the current database)."""
        access_rows = self.env['ir.model.access'].sudo().search_read(
            [('model_id.model', '=', model_name)],
            ['name', 'group_id', 'perm_read', 'perm_write',
             'perm_create', 'perm_unlink'],
            limit=80, order='id')
        access = [{
            'group': (row['group_id'] and row['group_id'][1]) or '',
            'perms': ''.join(flag for flag, field_name in (
                ('R', 'perm_read'), ('W', 'perm_write'),
                ('C', 'perm_create'), ('U', 'perm_unlink')) if row[field_name]),
            'name': row['name'],
        } for row in access_rows]
        rules = [{
            'id': rule.id,
            'name': rule.name,
            'groups': ', '.join(rule.groups.mapped('name')),
            'perms': ''.join(flag for flag, value in (
                ('R', rule.perm_read), ('W', rule.perm_write),
                ('C', rule.perm_create), ('U', rule.perm_unlink)) if value),
            'domain': (rule.domain_force or '').replace('\n', ' ')[:160],
        } for rule in self.env['ir.rule'].sudo().with_context(
            active_test=False).search(
            [('model_id.model', '=', model_name)], limit=40)]
        return {'access': access, 'rules': rules}

    def _structure_for_model(self, node):
        meta = node.meta or {}
        in_edges = node.in_edge_ids
        url_cache = {}
        # Out-of-scope model (core code, nothing indexed beyond a skeleton):
        # read everything LIVE from the registry instead of showing emptiness
        if not in_edges.filtered(lambda e: e.edge_type == 'field_of'):
            return self._live_structure_for_model(node)
        mro = meta.get('mro_modules') or ([node.module_name] if node.module_name else [])
        module_nodes = {
            n.technical_key: n for n in self.search([
                ('node_type', '=', 'module'), ('technical_key', 'in', mro)])
        }
        extend_meta = {
            e.source_id.technical_key: (e.meta or {})
            for e in in_edges.filtered(lambda e: e.edge_type == 'model_extend')
        }
        chain = []
        for index, module in enumerate(mro):
            module_node = module_nodes.get(module)
            edge_meta = extend_meta.get(module, {})
            file_path = (node.file_path if index == 0 else edge_meta.get('file')) or ''
            line_no = (node.line_no if index == 0 else edge_meta.get('line')) or 0
            chain.append({
                'module': module,
                'is_definer': index == 0,
                'is_custom': bool(module_node and (module_node.meta or {}).get('is_custom')),
                'file': file_path,
                'line': line_no,
                'url': self._editor_url(module, file_path, line_no, url_cache),
            })

        parents = [{
            'key': e.target_id.technical_key,
            'name': e.target_id.name,
            'kind': 'delegate' if e.edge_type == 'model_delegate' else 'inherit',
            'field': (e.meta or {}).get('field', ''),
        } for e in node.out_edge_ids.filtered(
            lambda e: e.edge_type in ('model_inherit', 'model_delegate'))]

        field_edges = in_edges.filtered(lambda e: e.edge_type == 'field_of')
        custom_fields = sorted([{
            'id': s.id,
            'name': s.technical_key.rsplit('.', 1)[-1],
            'label': s.name,
            'ttype': (s.meta or {}).get('ttype', ''),
            'module': s.module_name or '',
        } for e in field_edges
            if (s := e.source_id) and (s.meta or {}).get('is_custom')],
            key=lambda f: f['name'])[:60]

        view_nodes = [e.source_id for e in in_edges.filtered(
            lambda e: e.edge_type == 'view_of_model')]
        view_ids = {v.id for v in view_nodes}
        children_map = {}
        roots = []
        for view in view_nodes:
            parent_edge = view.out_edge_ids.filtered(
                lambda e: e.edge_type == 'view_inherits')[:1]
            if parent_edge and parent_edge.target_id.id in view_ids:
                children_map.setdefault(parent_edge.target_id.id, []).append(view)
            else:
                roots.append(view)

        def _view_entry(view, depth=0, visited=None):
            visited = visited or set()
            if view.id in visited or depth > 4:
                return None
            visited.add(view.id)
            kids = sorted(children_map.get(view.id, []),
                          key=lambda v: (v.module_name or '', v.name))
            return {
                'id': view.id,
                'key': view.technical_key,
                'name': view.name,
                'module': view.module_name or '',
                'view_type': (view.meta or {}).get('view_type', ''),
                'ref_id': view.ref_id,
                'children': [c for k in kids if (c := _view_entry(k, depth + 1, visited))],
            }

        view_tree = [entry for view in sorted(
            roots, key=lambda v: ((v.meta or {}).get('view_type', ''), v.name))
            if (entry := _view_entry(view))]

        method_edges = in_edges.filtered(lambda e: e.edge_type == 'method_of')
        overridden = []
        all_methods = []
        for e in method_edges:
            m = e.source_id
            m_meta = m.meta or {}
            m_mro = m_meta.get('mro') or []
            m_defs = m_meta.get('defs') or []
            method_name = m.technical_key.rsplit('.', 1)[-1]
            if len(m_mro) > 1:
                overridden.append({
                    'id': m.id,
                    'name': method_name,
                    'chain': list(reversed(m_mro)),  # definer first
                    'file': m.file_path or '',
                    'line': m.line_no or 0,
                    'url': self._editor_url(m.module_name, m.file_path,
                                            m.line_no, url_cache),
                })
            if not m.is_skeleton:  # actually defined in the indexed scope
                calls_count = len(m.out_edge_ids.filtered(
                    lambda x: x.edge_type == 'method_calls'))
                all_methods.append({
                    'id': m.id,
                    'key': m.technical_key,
                    'name': method_name,
                    'modules': sorted({d.get('module') for d in m_defs if d.get('module')})
                               or ([m.module_name] if m.module_name else []),
                    'file': m.file_path or '',
                    'line': m.line_no or 0,
                    'url': self._editor_url(m.module_name, m.file_path,
                                            m.line_no, url_cache),
                    'overridden': len(m_mro) > 1,
                    'calls': calls_count,
                })
        overridden.sort(key=lambda m: m['name'])
        all_methods.sort(key=lambda m: m['name'])

        entries = []
        for e in in_edges.filtered(lambda e: e.edge_type == 'action_targets_model'):
            action = e.source_id
            menus = [me.source_id.name for me in action.in_edge_ids.filtered(
                lambda x: x.edge_type == 'menu_opens_action')]
            entries.append({
                'id': action.id,
                'key': action.technical_key,
                'kind': action.node_type,
                'ai': bool((action.meta or {}).get('is_ai_tool')),
                'usage': (action.meta or {}).get('usage', ''),
                'action': action.name,
                'module': action.module_name or '',
                'menus': menus,
            })
        entries.sort(key=lambda x: (x['kind'], x['action']))

        return {
            'type': 'model',
            'id': node.id,
            'key': node.technical_key,
            'name': node.name,
            'definer': node.module_name or '',
            'file': node.file_path or '',
            'line': node.line_no or 0,
            'ref_model': node.ref_model,
            'ref_id': node.ref_id,
            'chain': chain,
            'parents': parents,
            'fields_total': len(field_edges),
            'custom_fields': custom_fields,
            'views': view_tree,
            'views_total': len(view_nodes),
            'methods_total': len(method_edges),
            'overridden_methods': overridden[:50],
            'methods': all_methods[:300],
            'entries': entries,
            'security': self._security_payload(node.technical_key),
            'custom_modules': self._custom_module_names(),
        }

    def _live_structure_for_model(self, node, model_name=None):
        """Structure payload computed straight from the running registry for
        models outside the deep-indexed scope (core models like sale.order).
        No storage, always current; call chains stay index-only."""
        model_name = model_name or node.technical_key
        env = self.env
        if model_name not in env:
            if node:
                return {'type': 'other', 'detail': node.get_node_details()}
            return {'error': 'not_found'}
        Builder = env['ai.code.graph.builder']
        cls = type(env[model_name])

        declaring = [klass for klass in cls.mro()
                     if Builder._class_declares(klass, model_name)]
        chain_modules = []
        for klass in declaring:
            module = getattr(klass, '_module', None)
            if module and module not in chain_modules:
                chain_modules.append(module)
        definition_chain = list(reversed(chain_modules))  # definer first
        definer = definition_chain[0] if definition_chain else ''

        chain = [{'module': module, 'is_definer': index == 0,
                  'is_custom': False, 'file': '', 'line': 0}
                 for index, module in enumerate(definition_chain)]

        # methods: every function declared by a class of this model, with
        # override chains, file:line via introspection
        url_cache = {}
        method_map = {}
        for klass in reversed(declaring):  # base first -> chains in def order
            module = getattr(klass, '_module', None) or '?'
            for name, func in vars(klass).items():
                if name.startswith('__') or not isinstance(func, types.FunctionType):
                    continue
                file_path, line_no, abs_path = _short_source(func)
                entry = method_map.setdefault(name, {
                    'id': 0, 'key': f"{model_name}.{name}", 'name': name,
                    'modules': [], 'file': '', 'line': 0, 'url': '',
                    'overridden': False, 'calls': 0,
                })
                if module not in entry['modules']:
                    entry['modules'].append(module)
                entry['file'], entry['line'] = file_path, line_no  # last override wins
                entry['url'] = self._editor_url('', '', line_no, url_cache,
                                                abs_path=abs_path)
        for entry in method_map.values():
            entry['overridden'] = len(entry['modules']) > 1
        all_methods = sorted(method_map.values(), key=lambda m: m['name'])

        parents = [{'key': parent, 'name': parent, 'kind': 'delegate', 'field': fname}
                   for parent, fname in (env[model_name]._inherits or {}).items()]

        field_recs = env['ir.model.fields'].sudo().search_read(
            [('model', '=', model_name)], ['name', 'field_description', 'ttype', 'state'])
        custom_fields = sorted([{
            'id': 0, 'name': r['name'], 'label': r['field_description'],
            'ttype': r['ttype'], 'module': 'manual',
        } for r in field_recs if r['state'] == 'manual'], key=lambda f: f['name'])

        views = env['ir.ui.view'].sudo().search(
            [('model', '=', model_name), ('type', '!=', 'qweb')])
        view_ids = set(views.ids)
        children_map = defaultdict(list)
        roots = []
        for view in views:
            if view.inherit_id and view.inherit_id.id in view_ids:
                children_map[view.inherit_id.id].append(view)
            else:
                roots.append(view)
        imd = {r['res_id']: r['module'] for r in env['ir.model.data'].sudo().search_read(
            [('model', '=', 'ir.ui.view'), ('res_id', 'in', list(view_ids))],
            ['res_id', 'module'], order='id')}

        def _view_entry(view, depth=0, visited=None):
            visited = visited or set()
            if view.id in visited or depth > 4:
                return None
            visited.add(view.id)
            kids = sorted(children_map.get(view.id, []),
                          key=lambda v: (imd.get(v.id, ''), v.name))
            return {
                'id': view.id, 'key': view.key or f"view_{view.id}",
                'name': view.name, 'module': imd.get(view.id, ''),
                'view_type': view.type, 'ref_id': view.id,
                'children': [c for k in kids if (c := _view_entry(k, depth + 1, visited))],
            }

        view_tree = [entry for view in sorted(roots, key=lambda v: (v.type, v.name))
                     if (entry := _view_entry(view))][:60]

        actions = env['ir.actions.act_window'].sudo().search(
            [('res_model', '=', model_name)])
        entries = [{'id': a.id, 'key': '', 'kind': 'action', 'ai': False,
                    'usage': '', 'action': a.name, 'module': '', 'menus': []}
                   for a in actions[:20]]

        rec = env['ir.model'].sudo().search([('model', '=', model_name)], limit=1)
        return {
            'type': 'model',
            'live': True,
            'id': node.id if node else 0,
            'key': model_name,
            'name': rec.name or model_name,
            'definer': definer,
            'file': '', 'line': 0,
            'ref_model': 'ir.model', 'ref_id': rec.id or 0,
            'chain': chain,
            'parents': parents,
            'fields_total': len(field_recs),
            'custom_fields': custom_fields[:60],
            'views': view_tree,
            'views_total': len(views),
            'methods_total': len(all_methods),
            'overridden_methods': [],
            'methods': all_methods[:400],
            'entries': entries,
            'security': self._security_payload(model_name),
            'custom_modules': self._custom_module_names(),
        }

    def _structure_for_module(self, node):
        key = node.technical_key
        url_cache = {}
        depends = [{
            'name': e.target_id.technical_key,
            'is_custom': bool((e.target_id.meta or {}).get('is_custom')),
        } for e in node.out_edge_ids.filtered(lambda e: e.edge_type == 'module_depends')]
        dependents = [e.source_id.technical_key for e in node.in_edge_ids.filtered(
            lambda e: e.edge_type == 'module_depends')]
        models_defined = [{
            'id': n.id, 'key': n.technical_key, 'name': n.name,
        } for n in self.search([
            ('node_type', '=', 'model'), ('module_name', '=', key)], limit=100)]
        models_extended = [{
            'id': e.target_id.id,
            'key': e.target_id.technical_key,
            'name': e.target_id.name,
            'file': (e.meta or {}).get('file', ''),
            'line': (e.meta or {}).get('line', 0),
            'url': self._editor_url(key, (e.meta or {}).get('file'),
                                    (e.meta or {}).get('line'), url_cache),
        } for e in node.out_edge_ids.filtered(lambda e: e.edge_type == 'model_extend')]
        js_nodes = [{
            'id': n.id, 'name': n.name, 'kind': n.node_type,
            'key': n.technical_key, 'node_type': n.node_type,
            'file': n.file_path or '', 'line': n.line_no or 0,
            'url': self._editor_url(key, n.file_path, n.line_no, url_cache),
            'exported': bool((n.meta or {}).get('exported')),
        } for n in self.search([
            ('node_type', 'in', ('js_component', 'js_service', 'js_function')),
            ('module_name', '=', key), ('is_skeleton', '=', False)],
            order='node_type, name', limit=200)]
        patches = [{
            'id': e.target_id.id, 'name': e.target_id.name,
            'key': e.target_id.technical_key,
            'node_type': e.target_id.node_type,
            'file': (e.meta or {}).get('file', ''),
            'line': (e.meta or {}).get('line', 0),
            'url': self._editor_url(key, (e.meta or {}).get('file'),
                                    (e.meta or {}).get('line'), url_cache),
        } for e in node.out_edge_ids.filtered(lambda e: e.edge_type == 'js_patches')]
        views_count = self.search_count([
            ('node_type', '=', 'view'), ('module_name', '=', key)])
        server_actions = [{
            'id': n.id, 'key': n.technical_key, 'name': n.name,
            'node_type': n.node_type,
            'model': n.model_name or '',
            'usage': (n.meta or {}).get('usage', ''),
            'ai': bool((n.meta or {}).get('is_ai_tool')),
        } for n in self.search([
            ('node_type', '=', 'server_action'), ('module_name', '=', key)],
            order='name', limit=120)]
        return {
            'type': 'module',
            'id': node.id,
            'key': key,
            'name': node.name,
            'is_custom': bool((node.meta or {}).get('is_custom')),
            'shortdesc': (node.meta or {}).get('shortdesc', ''),
            'depends': sorted(depends, key=lambda d: (not d['is_custom'], d['name'])),
            'dependents': sorted(dependents),
            'models_defined': sorted(models_defined, key=lambda m: m['key']),
            'models_extended': sorted(models_extended, key=lambda m: m['key']),
            'js': js_nodes,
            'patches': patches,
            'views_count': views_count,
            'server_actions': server_actions,
            'custom_modules': self._custom_module_names(),
        }

    # ══════════════════════════════════════════════
    # AI tool helpers (read-only, called from server actions)
    # ══════════════════════════════════════════════

    @api.model
    def ai_tool_model_map(self, model_name):
        """Text map of a model for chat agents: definer, extenders (MRO),
        fields summary, views tree, actions/menus, JS touching it."""
        model_name = (model_name or '').strip()
        node = self.sudo().search([
            ('node_type', '=', 'model'), ('technical_key', '=', model_name),
        ], limit=1)
        if not node:
            return (f"Model '{model_name}' not found in the code graph. "
                    f"Rebuild the index (AI Studio > Code Graph) or check the name.")
        meta = node.meta or {}
        lines = [
            f"# Code map: {model_name} ({node.name})",
            f"Defined by module: {node.module_name or '?'}"
            + (f"  [{node.file_path}:{node.line_no}]" if node.file_path else ""),
        ]
        if mro := meta.get('mro_modules'):
            lines.append("Extension chain (runtime MRO order): " + " -> ".join(mro))

        edges_in = node.sudo().in_edge_ids
        extenders = edges_in.filtered(lambda e: e.edge_type == 'model_extend')
        if extenders:
            lines.append("Extended in-place by modules: "
                         + ", ".join(sorted(set(extenders.mapped('source_id.name')))))

        field_edges = edges_in.filtered(lambda e: e.edge_type == 'field_of')
        lines.append(f"\nFields: {len(field_edges)} total")
        custom = [e.source_id for e in field_edges
                  if e.source_id.technical_key.split('.')[-1].startswith('x_')
                  or (e.source_id.meta or {}).get('is_custom')]
        if custom:
            lines.append("Custom/extension fields: "
                         + ", ".join(sorted(n.technical_key.split('.')[-1] for n in custom)[:40]))

        view_edges = edges_in.filtered(lambda e: e.edge_type == 'view_of_model')
        if view_edges:
            lines.append(f"\nViews ({len(view_edges)}):")
            for e in view_edges.sorted(lambda e: e.source_id.name)[:25]:
                v = e.source_id
                vmeta = v.meta or {}
                inh = ""
                parents = v.out_edge_ids.filtered(lambda x: x.edge_type == 'view_inherits')
                if parents:
                    inh = f" (inherits {parents[0].target_id.name})"
                lines.append(f"  - [{vmeta.get('view_type', '?'):<7}] {v.name}"
                             f" [{v.module_name or '?'}]{inh}")

        act_edges = edges_in.filtered(lambda e: e.edge_type == 'action_targets_model')
        if act_edges:
            lines.append(f"\nWindow actions: "
                         + ", ".join(e.source_id.name for e in act_edges[:10]))

        method_edges = edges_in.filtered(lambda e: e.edge_type == 'method_of')
        overridden = [e.source_id for e in method_edges
                      if len((e.source_id.meta or {}).get('defs') or []) > 1]
        if overridden:
            lines.append("\nMethods overridden across modules: "
                         + ", ".join(sorted(n.technical_key.split('.')[-1]
                                            for n in overridden)[:25]))
        lines.append("\nTip: use 'Code Graph: Blast Radius' with a field name to see "
                     "what a field change would affect.")
        return "\n".join(lines)

    @api.model
    def ai_tool_blast_radius(self, model_name, field_name):
        """Text list of everything referencing model_name.field_name."""
        model_name = (model_name or '').strip()
        field_name = (field_name or '').strip()
        key = f"{model_name}.{field_name}"
        node = self.sudo().search([
            ('node_type', '=', 'field'), ('technical_key', '=', key),
        ], limit=1)
        if not node:
            return (f"Field '{key}' not found in the code graph. "
                    f"Check the names or rebuild the index.")
        meta = node.meta or {}
        lines = [
            f"# Blast radius: {key} ({node.name}, type {meta.get('ttype', '?')})",
            f"Defined by module: {node.module_name or '?'}",
        ]
        edges_in = node.sudo().in_edge_ids
        views = edges_in.filtered(lambda e: e.edge_type == 'view_uses_field')
        if views:
            lines.append(f"\nDisplayed in {len(views)} view(s):")
            for e in views[:20]:
                v = e.source_id
                lines.append(f"  - {v.name} [{v.module_name or '?'}]")
        computes = edges_in.filtered(lambda e: e.edge_type == 'compute_depends')
        if computes:
            lines.append(f"\nComputed fields depending on it ({len(computes)}):")
            for e in computes[:20]:
                lines.append(f"  - {e.source_id.technical_key}"
                             f" (path: {(e.meta or {}).get('path', '?')})")
        out_edges = node.sudo().out_edge_ids
        comodel = out_edges.filtered(lambda e: e.edge_type == 'field_comodel')
        if comodel:
            lines.append(f"\nRelational target: {comodel[0].target_id.technical_key}")
        if related := meta.get('related'):
            lines.append(f"Related path: {related}")
        if deps := meta.get('depends'):
            lines.append(f"Compute depends: {', '.join(deps)}")
        if not views and not computes:
            lines.append("\nNo views or computed fields reference this field "
                         "in the indexed scope.")
        lines.append("\nCaution: the graph covers the indexed scope only; server "
                     "action code, reports and domain expressions are not traced yet.")
        return "\n".join(lines)

    def action_open_backing_record(self):
        self.ensure_one()
        if not (self.ref_model and self.ref_id):
            return False
        return {
            'type': 'ir.actions.act_window',
            'res_model': self.ref_model,
            'res_id': self.ref_id,
            'view_mode': 'form',
            'target': 'current',
        }
