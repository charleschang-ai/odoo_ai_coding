# Part of AI Code Graph.
#
# ai.code.graph.builder rebuilds the whole code graph. Three extractors:
#   A. Registry harvest - models/fields/views/actions/menus straight from the
#      live database and registry (no file parsing at all).
#   B. Python AST      - custom addons only: class/method definitions, _inherit
#      declarations, super() usage, exact file:line positions.
#   C. Asset scan      - static/src XML templates (lxml) and JS files parsed by
#      Odoo coding conventions (patch/registry/Component), no tree-sitter.
#
# BaseModel uses __slots__, so build state lives in an explicit _BuildState
# object threaded through the extractor methods.
import ast
import logging
import os
import re
from collections import defaultdict
from pathlib import Path

from lxml import etree

from odoo import api, fields, models, _
from odoo.exceptions import AccessError
from odoo.modules import get_module_path
from odoo.modules import module as _module_loader


def _core_root():
    """Root of the Odoo source checkout (parent of the odoo package).

    odoo.__file__ can be None (namespace package), so anchor on a real
    submodule file instead: .../odoo/odoo/modules/module.py -> .../odoo
    """
    return str(Path(_module_loader.__file__).resolve().parents[2])

_logger = logging.getLogger(__name__)

# Bump whenever the extractors learn new relation types: viewers compare it
# with the stored ai_code_graph.index_version and prompt for a rebuild.
INDEX_VERSION = 4

SKIP_DIRS = {'__pycache__', 'tests', 'migrations', '.git', 'node_modules'}
MAGIC_FIELDS = {'id', 'create_uid', 'create_date', 'write_uid', 'write_date',
                'display_name', '__last_update'}
# Base classes never worth an edge when a JS class extends them
JS_BASE_CLASSES = {'Component', 'owl.Component', 'Object', 'Error', 'Model',
                   'Record', 'Interaction', 'Plugin'}

# ORM plumbing: calling these on self is noise; on OTHER models only the
# data-changing ones (DATA_METHODS) are business-relevant.
ORM_COMMON = {
    'ensure_one', 'exists', 'browse', 'search', 'search_count', 'search_read',
    'search_fetch', 'read', 'read_group', 'mapped', 'filtered', 'sorted',
    'sudo', 'with_context', 'with_user', 'with_company', 'with_env',
    'with_prefetch', 'flush_all', 'flush_model', 'flush_recordset',
    'invalidate_all', 'invalidate_model', 'invalidate_recordset',
    'check_access', 'has_access', 'get_metadata', 'default_get', 'fields_get',
    'name_search', 'name_create', 'get', 'ref', 'copy_data', 'onchange',
    'new', 'union', 'grouped', 'get_param', 'set_param',
}
DATA_METHODS = {'create', 'write', 'unlink', 'copy', 'message_post'}
# Plain Python object methods: never worth reporting as unresolved calls
PY_BUILTIN_METHODS = {
    'strip', 'lstrip', 'rstrip', 'format', 'format_map', 'join', 'split',
    'rsplit', 'splitlines', 'replace', 'startswith', 'endswith', 'lower',
    'upper', 'title', 'capitalize', 'partition', 'rpartition', 'find',
    'rfind', 'index', 'count', 'zfill', 'ljust', 'rjust', 'encode', 'decode',
    'append', 'extend', 'insert', 'remove', 'pop', 'clear', 'reverse',
    'sort', 'add', 'discard', 'update', 'items', 'keys', 'values',
    'setdefault', 'popitem', 'copy', 'get',
    'isoformat', 'strftime', 'strptime', 'astimezone', 'timestamp', 'date',
    'time', 'weekday', 'total_seconds',
    'sub', 'match', 'search', 'findall', 'finditer', 'fullmatch', 'group',
    'groups', 'groupdict', 'compile', 'escape',
    'dumps', 'loads', 'dump', 'load', 'read', 'readline', 'readlines',
    'close', 'seek', 'tell', 'flush', 'getvalue', 'hexdigest', 'digest',
    'info', 'debug', 'warning', 'error', 'exception', 'critical',
}
# Recordset-preserving chain methods: model of X.<these>() == model of X
CHAINABLE = {
    'sudo', 'with_context', 'with_user', 'with_company', 'with_env',
    'with_prefetch', 'browse', 'search', 'search_fetch', 'filtered', 'sorted',
    'exists', 'copy', 'create', 'new', 'ref',
}

JS_CLASS_RE = re.compile(r'\bclass\s+(\w+)\s+extends\s+([\w.]+)')
JS_PLAIN_CLASS_RE = re.compile(r'\bclass\s+(\w+)\s*\{')
JS_EXPORT_FUNC_RE = re.compile(r'\bexport\s+(?:async\s+)?function\s+(\w+)')
JS_EXPORT_CONST_FUNC_RE = re.compile(
    r'\bexport\s+const\s+(\w+)\s*=\s*(?:async\s*)?(?:\(|function\b|[\w$]+\s*=>)')
JS_TEMPLATE_RE = re.compile(r'static\s+template\s*=\s*["\']([\w.\-]+)["\']')
JS_REGISTRY_RE = re.compile(
    r'registry\s*\.\s*category\(\s*["\']([\w.\-]+)["\']\s*\)'
    r'\s*\.\s*add\(\s*["\']([^"\']+)["\']\s*(?:,\s*([\w.]+))?')
JS_PATCH_RE = re.compile(r'\bpatch\(\s*([\w]+)(?:\.prototype)?\s*,')


class _BuildState:
    """Mutable accumulation state for one rebuild run."""

    def __init__(self):
        self.nodes = {}      # (type, key) -> vals dict
        self.edges = {}      # (src, tgt, type) -> vals dict
        self.js_alias = {}   # bare JS class name -> node ident tuple

    def node(self, node_type, key, **vals):
        """Get-or-create an accumulated node; upgrades skeletons in place."""
        ident = (node_type, key)
        existing = self.nodes.get(ident)
        if existing is None:
            self.nodes[ident] = {
                'node_type': node_type,
                'technical_key': key,
                'name': vals.pop('name', key),
                'layer': vals.pop('layer', 'python'),
                'is_skeleton': vals.pop('is_skeleton', False),
                'meta': vals.pop('meta', {}),
                **vals,
            }
        else:
            if not vals.get('is_skeleton') and existing.get('is_skeleton'):
                existing['is_skeleton'] = False
            meta = vals.pop('meta', None)
            if meta:
                existing.setdefault('meta', {}).update(meta)
            vals.pop('is_skeleton', None)
            for field_name, value in vals.items():
                if value and not existing.get(field_name):
                    existing[field_name] = value
        return ident

    def edge(self, src, tgt, edge_type, module_name=False, meta=None):
        if src == tgt:
            return
        ident = (src, tgt, edge_type)
        existing = self.edges.get(ident)
        if existing is None:
            self.edges[ident] = {
                'edge_type': edge_type,
                'module_name': module_name or False,
                'meta': meta or {},
            }
        elif meta and not existing['meta']:
            # a later extractor knows more (e.g. AST file:line on an edge the
            # registry pass created bare) - keep the richer meta
            existing['meta'] = meta


class AiCodeGraphBuilder(models.Model):
    """Service model (no table) that rebuilds ai.code.node / ai.code.edge."""
    _name = 'ai.code.graph.builder'
    _description = "AI Code Graph Builder"
    _auto = False
    _log_access = False

    # ══════════════════════════════════════════════
    # Public entry points
    # ══════════════════════════════════════════════

    @api.model
    def action_rebuild(self):
        if not (self.env.su or self.env.user.has_group('base.group_system')):
            raise AccessError(_("Only system administrators can rebuild the code graph."))
        stats = self.sudo()._rebuild()
        message = _(
            "Code graph rebuilt: %(nodes)s nodes, %(edges)s edges "
            "(%(modules)s modules in deep scope).",
            nodes=stats['nodes'], edges=stats['edges'], modules=stats['scope'],
        )
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {'type': 'success', 'message': message},
        }

    @api.model
    def _cron_rebuild(self):
        self.sudo()._rebuild()

    @api.model
    def _auto_rebuild_on_upgrade(self):
        """Called from data via <function> on every install/upgrade.

        Rebuilding INLINE here would index a partial registry (modules that
        load after this one are not registered yet), so we only _trigger()
        the cron: it runs right after the upgrade finishes, with the full
        registry. Same trigger-style-cron pattern as ai_chatbot_enhance.
        Must never break the module upgrade."""
        try:
            cron = self.env.ref('ai_code_graph.ir_cron_code_graph_rebuild',
                                raise_if_not_found=False)
            if cron:
                if not cron.sudo().active:
                    # still the shipped default record: convert it to
                    # trigger-style (an admin-configured nightly cron is kept)
                    cron.sudo().write({
                        'active': True,
                        'interval_number': 9999,
                        'interval_type': 'months',
                    })
                cron.sudo()._trigger()
                _logger.info("AI Code Graph: index rebuild scheduled "
                             "(runs right after this upgrade completes)")
            else:
                self.sudo()._rebuild()
        except Exception:  # noqa: BLE001
            _logger.exception("AI Code Graph: could not schedule the automatic "
                              "rebuild - use the Rebuild Index button instead")

    # ══════════════════════════════════════════════
    # Build orchestration
    # ══════════════════════════════════════════════

    def _rebuild(self):
        state = _BuildState()
        scope_modules = self._get_scope_modules()
        _logger.info("AI Code Graph: rebuilding, deep scope = %s", sorted(scope_modules))

        scope_models = self._extract_registry(state, scope_modules)
        for module in scope_modules:
            module_path = get_module_path(module, display_warning=False)
            if not module_path:
                continue
            try:
                self._extract_python(state, module, Path(module_path), scope_models)
            except Exception:  # noqa: BLE001 - one bad file must not kill the build
                _logger.exception("AI Code Graph: python extraction failed for %s", module)
            try:
                self._extract_assets(state, module, Path(module_path))
            except Exception:  # noqa: BLE001
                _logger.exception("AI Code Graph: asset extraction failed for %s", module)
        self._annotate_method_overrides(state)
        self._patch_module_stats(state)

        node_count, edge_count = self._persist(state)
        icp = self.env['ir.config_parameter'].sudo()
        icp.set_param(
            'ai_code_graph.last_build',
            f"{fields.Datetime.now()} UTC | nodes={node_count} edges={edge_count}")
        icp.set_param('ai_code_graph.index_version', str(INDEX_VERSION))
        _logger.info("AI Code Graph: done, %s nodes / %s edges", node_count, edge_count)
        return {'nodes': node_count, 'edges': edge_count, 'scope': len(scope_modules)}

    def _get_scope_modules(self):
        """Modules that get deep (file-level) indexing.

        Default: every installed module living outside the Odoo source tree
        (i.e. your custom addons). Extend with the comma-separated config
        parameter ai_code_graph.extra_modules; restrict everything with
        ai_code_graph.scope_modules (explicit list overrides autodetection).
        """
        # 'to upgrade'/'to install' included: the auto-rebuild runs while THIS
        # module's own state is still transitioning during its upgrade
        ACTIVE_STATES = ('installed', 'to upgrade', 'to install')
        icp = self.env['ir.config_parameter'].sudo()
        if explicit := (icp.get_param('ai_code_graph.scope_modules') or '').strip():
            wanted = {m.strip() for m in explicit.split(',') if m.strip()}
            return set(self.env['ir.module.module'].sudo().search([
                ('state', 'in', ACTIVE_STATES), ('name', 'in', list(wanted)),
            ]).mapped('name'))

        core_root = _core_root()
        scope = set()
        for module in self.env['ir.module.module'].sudo().search([('state', 'in', ACTIVE_STATES)]):
            path = get_module_path(module.name, display_warning=False)
            if path and not str(Path(path).resolve()).startswith(core_root):
                scope.add(module.name)
        for extra in (icp.get_param('ai_code_graph.extra_modules') or '').split(','):
            if extra.strip():
                scope.add(extra.strip())
        return scope

    # ══════════════════════════════════════════════
    # Extractor A: registry / database harvest
    # ══════════════════════════════════════════════

    @staticmethod
    def _class_declares(klass, model_name):
        """True when this MRO class's own body declares model_name via
        _name/_inherit. Filters out the 'base' mixin classes that every
        model inherits (web, base_import... extend model 'base', and would
        otherwise pollute every model's extension chain)."""
        class_dict = getattr(klass, '__dict__', {})
        targets = set()
        declared_name = class_dict.get('_name')
        if isinstance(declared_name, str):
            targets.add(declared_name)
        declared_inherit = class_dict.get('_inherit')
        if isinstance(declared_inherit, str):
            targets.add(declared_inherit)
        elif isinstance(declared_inherit, (list, tuple)):
            targets.update(t for t in declared_inherit if isinstance(t, str))
        return model_name in targets

    def _imd_map(self, res_model, res_ids):
        """res_id -> ordered [(module, xmlid)] from ir.model.data."""
        result = defaultdict(list)
        if not res_ids:
            return result
        rows = self.env['ir.model.data'].sudo().search_read(
            [('model', '=', res_model), ('res_id', 'in', list(res_ids))],
            ['res_id', 'module', 'name'], order='id')
        for row in rows:
            if row['module'] == '__export__':
                continue
            result[row['res_id']].append((row['module'], f"{row['module']}.{row['name']}"))
        return result

    def _extract_registry(self, state, scope_modules):
        env = self.env
        core_root = _core_root()

        # -- modules ------------------------------------------------------
        installed = env['ir.module.module'].sudo().search([
            ('state', 'in', ('installed', 'to upgrade', 'to install'))])
        installed_names = set(installed.mapped('name'))
        for module in installed:
            path = get_module_path(module.name, display_warning=False)
            is_custom = bool(path and not str(Path(path).resolve()).startswith(core_root))
            state.node(
                'module', module.name,
                name=module.name, layer='python',
                module_name=module.name,
                ref_model='ir.module.module', ref_id=module.id,
                meta={'is_custom': is_custom, 'in_scope': module.name in scope_modules,
                      'shortdesc': module.shortdesc or ''},
            )
        for module in installed:
            for dep in module.dependencies_id:
                if dep.name in installed_names:
                    state.edge(('module', module.name), ('module', dep.name),
                               'module_depends', module_name=module.name)

        # -- models in scope ----------------------------------------------
        model_recs = env['ir.model'].sudo().search([])
        scope_model_recs = model_recs.filtered(
            lambda m: m.modules and set(m.modules.split(', ')) & scope_modules)
        imd_models = self._imd_map('ir.model', scope_model_recs.ids)
        scope_models = set()

        for rec in scope_model_recs:
            model_name = rec.model
            if model_name not in env:
                continue
            scope_models.add(model_name)
            cls = type(env[model_name])
            mro_modules = []
            for klass in cls.mro():
                if not self._class_declares(klass, model_name):
                    continue
                mod = getattr(klass, '_module', None)
                if mod and mod not in mro_modules:
                    mro_modules.append(mod)
            definition_chain = list(reversed(mro_modules))  # definer first
            imd_definer = imd_models[rec.id][0][0] if imd_models[rec.id] else False
            definer = imd_definer or (definition_chain[0] if definition_chain else False)

            state.node(
                'model', model_name,
                name=rec.name or model_name, layer='python',
                module_name=definer, model_name=model_name,
                ref_model='ir.model', ref_id=rec.id,
                meta={'mro_modules': definition_chain,
                      'transient': rec.transient,
                      'description': rec.name or ''},
            )
            for extender in definition_chain:
                if extender != definer and extender in installed_names:
                    state.edge(('module', extender), ('model', model_name),
                               'model_extend', module_name=extender)
            for parent_model, delegate_field in (env[model_name]._inherits or {}).items():
                self._ensure_model_skeleton(state, parent_model)
                state.edge(('model', model_name), ('model', parent_model),
                           'model_delegate', meta={'field': delegate_field})

        # -- fields of scope models ----------------------------------------
        field_recs = env['ir.model.fields'].sudo().search([
            ('model', 'in', list(scope_models))])
        imd_fields = self._imd_map('ir.model.fields', field_recs.ids)
        for rec in field_recs:
            if rec.name in MAGIC_FIELDS:
                continue
            model_name = rec.model
            key = f"{model_name}.{rec.name}"
            registry_field = env[model_name]._fields.get(rec.name)
            definer = imd_fields[rec.id][0][0] if imd_fields[rec.id] else False
            meta = {
                'ttype': rec.ttype,
                'label': rec.field_description or rec.name,
                'required': rec.required,
                'store': rec.store,
                'is_custom': rec.state == 'manual' or (definer in scope_modules),
            }
            if registry_field is not None and registry_field.related:
                meta['related'] = '.'.join(registry_field.related) \
                    if isinstance(registry_field.related, (list, tuple)) \
                    else registry_field.related
            state.node(
                'field', key,
                name=meta['label'], layer='python',
                module_name=definer, model_name=model_name,
                ref_model='ir.model.fields', ref_id=rec.id,
                meta=meta,
            )
            state.edge(('field', key), ('model', model_name), 'field_of')
            if rec.relation and rec.relation in env:
                self._ensure_model_skeleton(state, rec.relation)
                state.edge(('field', key), ('model', rec.relation),
                           'field_comodel', meta={'ttype': rec.ttype})

        # compute dependencies (registry truth, first hop only)
        for model_name in scope_models:
            model_cls = env[model_name]
            for fname, registry_field in model_cls._fields.items():
                if fname in MAGIC_FIELDS or not registry_field.compute:
                    continue
                source_key = f"{model_name}.{fname}"
                if ('field', source_key) not in state.nodes:
                    continue
                try:
                    depends = list(registry_field.get_depends(model_cls)[0])
                except Exception:  # noqa: BLE001 - depends resolution is best effort
                    depends = list(getattr(registry_field, 'depends', ()) or ())
                if depends:
                    state.nodes[('field', source_key)]['meta']['depends'] = depends[:20]
                for path in depends:
                    first_hop = path.split('.')[0]
                    target_key = f"{model_name}.{first_hop}"
                    if ('field', target_key) in state.nodes and target_key != source_key:
                        state.edge(('field', source_key), ('field', target_key),
                                   'compute_depends', meta={'path': path})

        # -- views ----------------------------------------------------------
        views = env['ir.ui.view'].sudo().search([
            ('model', 'in', list(scope_models)), ('type', '!=', 'qweb')])
        imd_scope_views = env['ir.model.data'].sudo().search_read(
            [('model', '=', 'ir.ui.view'), ('module', 'in', list(scope_modules))],
            ['res_id'])
        extra_view_ids = {r['res_id'] for r in imd_scope_views} - set(views.ids)
        if extra_view_ids:
            views |= env['ir.ui.view'].sudo().browse(list(extra_view_ids)).exists()
        views = views.filtered(lambda v: v.type != 'qweb')
        imd_views = self._imd_map('ir.ui.view', views.ids)

        def _view_key(view):
            entries = imd_views.get(view.id)
            return entries[0][1] if entries else (view.key or f"view_{view.id}")

        for view in views:
            entries = imd_views.get(view.id)
            module = entries[0][0] if entries else (
                (view.key or '').split('.')[0] if view.key and '.' in view.key else False)
            state.node(
                'view', _view_key(view),
                name=view.name or _view_key(view), layer='xml',
                module_name=module, model_name=view.model or False,
                ref_model='ir.ui.view', ref_id=view.id,
                meta={'view_type': view.type, 'priority': view.priority,
                      'inherited': bool(view.inherit_id)},
            )
        for view in views:
            key = _view_key(view)
            if view.model and view.model in scope_models:
                state.edge(('view', key), ('model', view.model), 'view_of_model',
                           module_name=state.nodes[('view', key)].get('module_name'))
            if view.inherit_id:
                parent = view.inherit_id
                if parent in views:
                    parent_key = _view_key(parent)
                else:
                    parent_entries = self._imd_map('ir.ui.view', [parent.id]).get(parent.id)
                    parent_key = parent_entries[0][1] if parent_entries else (
                        parent.key or f"view_{parent.id}")
                    state.node('view', parent_key, name=parent.name or parent_key,
                               layer='xml', model_name=parent.model or False,
                               ref_model='ir.ui.view', ref_id=parent.id,
                               is_skeleton=True,
                               meta={'view_type': parent.type})
                state.edge(('view', key), ('view', parent_key), 'view_inherits',
                           module_name=state.nodes[('view', key)].get('module_name'))
            self._extract_view_fields(state, view, key, scope_models)

        # -- window actions & menus -----------------------------------------
        actions = env['ir.actions.act_window'].sudo().search([
            ('res_model', 'in', list(scope_models))])
        imd_actions = self._imd_map('ir.actions.act_window', actions.ids)
        action_keys = {}
        for action in actions:
            entries = imd_actions.get(action.id)
            key = entries[0][1] if entries else f"action_{action.id}"
            module = entries[0][0] if entries else False
            action_keys[action.id] = key
            state.node(
                'action', key,
                name=action.name or key, layer='xml',
                module_name=module, model_name=action.res_model,
                ref_model='ir.actions.act_window', ref_id=action.id,
                meta={'view_mode': action.view_mode or ''},
            )
            state.edge(('action', key), ('model', action.res_model),
                       'action_targets_model', module_name=module)

        self._extract_server_actions(state)

        menus = env['ir.ui.menu'].sudo().with_context(
            ir_ui_menu_full_list=True).search([('action', '!=', False)])
        menu_candidates = [m for m in menus
                           if m.action and m.action._name == 'ir.actions.act_window'
                           and m.action.id in action_keys]
        imd_menus = self._imd_map('ir.ui.menu', [m.id for m in menu_candidates])
        for menu in menu_candidates:
            entries = imd_menus.get(menu.id)
            key = entries[0][1] if entries else f"menu_{menu.id}"
            module = entries[0][0] if entries else False
            state.node(
                'menu', key,
                name=menu.complete_name or menu.name, layer='xml',
                module_name=module,
                ref_model='ir.ui.menu', ref_id=menu.id,
            )
            state.edge(('menu', key), ('action', action_keys[menu.action.id]),
                       'menu_opens_action', module_name=module)

        return scope_models

    def _extract_server_actions(self, state):
        """Index python server actions (state='code'): AI skills, crons and
        automation rules all live here. Their code is parsed with the shared
        call collector, so 'called by' on a method also shows the skills and
        crons that trigger it. The safe_eval context binds record/records/
        model to the action's model - seeded into the receiver resolver."""
        env = self.env
        actions = env['ir.actions.server'].sudo().with_context(
            active_test=False).search([('state', '=', 'code')])
        if not actions:
            return
        imd_actions = self._imd_map('ir.actions.server', actions.ids)

        cron_action_ids = set()
        if 'ir.cron' in env:
            crons = env['ir.cron'].sudo().with_context(active_test=False).search([])
            cron_action_ids = set(crons.mapped('ir_actions_server_id').ids)
        automation_action_ids = set()
        if 'base.automation' in env:
            try:
                automations = env['base.automation'].sudo().with_context(
                    active_test=False).search([])
                automation_action_ids = set(automations.mapped('action_server_ids').ids)
            except Exception:  # noqa: BLE001 - optional model, best effort
                pass

        for action in actions:
            entries = imd_actions.get(action.id)
            key = entries[0][1] if entries else f"server_action_{action.id}"
            module = entries[0][0] if entries else False
            model_name = action.model_id.model or False
            is_ai_tool = bool(getattr(action, 'use_in_ai', False)
                              or getattr(action, 'ai_skill_draft', False))
            usage = ('cron' if action.id in cron_action_ids
                     else 'automation' if action.id in automation_action_ids
                     else 'ai_tool' if is_ai_tool else 'manual')
            ident = state.node(
                'server_action', key,
                name=action.name or key, layer='python',
                module_name=module, model_name=model_name,
                ref_model='ir.actions.server', ref_id=action.id,
                meta={'usage': usage, 'is_ai_tool': is_ai_tool},
            )
            if model_name and model_name in env:
                self._ensure_model_skeleton(state, model_name)
                state.edge(ident, ('model', model_name), 'action_targets_model',
                           module_name=module)
            code = (action.code or '').strip()
            if not code:
                continue
            try:
                tree = ast.parse(code)
            except SyntaxError:
                continue
            seed = {}
            if model_name and model_name in env:
                seed = {'record': model_name, 'records': model_name,
                        'model': model_name}
            unresolved = self._collect_calls(
                state, ident, tree, model_name or '', seed, module,
                f"(server action) {action.name or key}")
            if unresolved:
                state.nodes[ident]['meta']['unresolved'] = unresolved

    def _ensure_method_skeleton(self, state, model_name, method_name):
        """Method node that a call/button targets; may live outside the deep
        scope (core code) — created as a skeleton so trees can still show it."""
        key = f"{model_name}.{method_name}"
        ident = ('method', key)
        if ident not in state.nodes:
            self._ensure_model_skeleton(state, model_name)
            state.node('method', key, name=method_name, layer='python',
                       model_name=model_name, is_skeleton=True)
            state.edge(ident, ('model', model_name), 'method_of')
        return ident

    def _ensure_model_skeleton(self, state, model_name):
        if ('model', model_name) in state.nodes:
            return
        rec = self.env['ir.model'].sudo().search([('model', '=', model_name)], limit=1)
        state.node(
            'model', model_name,
            name=rec.name or model_name, layer='python',
            model_name=model_name,
            ref_model='ir.model', ref_id=rec.id or 0,
            is_skeleton=True,
        )

    def _extract_view_fields(self, state, view, view_key, scope_models):
        """Fields this view's own arch adds/places (not the combined arch)."""
        arch = view.arch_db
        if not arch or not view.model or view.model not in scope_models:
            return
        try:
            tree = etree.fromstring(arch.encode('utf-8'))
        except (etree.XMLSyntaxError, ValueError):
            return
        module = state.nodes[('view', view_key)].get('module_name')
        seen = set()
        for element in tree.iter('field'):
            fname = element.get('name')
            if not fname or fname in seen:
                continue
            # fields nested inside another <field> belong to a sub-view model
            if any(anc.tag == 'field' for anc in element.iterancestors()):
                continue
            seen.add(fname)
            field_key = f"{view.model}.{fname}"
            if ('field', field_key) in state.nodes:
                state.edge(('view', view_key), ('field', field_key),
                           'view_uses_field', module_name=module)
        # buttons calling python methods: the entry points of call chains
        if view.model in self.env:
            for element in tree.iter('button'):
                if element.get('type') != 'object':
                    continue
                method_name = element.get('name')
                if not method_name or any(
                        anc.tag == 'field' for anc in element.iterancestors()):
                    continue
                target = self._ensure_method_skeleton(state, view.model, method_name)
                state.edge(('view', view_key), target, 'view_calls_method',
                           module_name=module,
                           meta={'string': element.get('string') or ''})

    # ══════════════════════════════════════════════
    # Extractor B: Python AST over custom addons
    # ══════════════════════════════════════════════

    def _extract_python(self, state, module, module_path, scope_models):
        for py_file in sorted(module_path.rglob('*.py')):
            relative = py_file.relative_to(module_path)
            if any(part in SKIP_DIRS for part in relative.parts):
                continue
            if relative.name == '__manifest__.py':
                continue
            try:
                source = py_file.read_text(encoding='utf-8', errors='replace')
                tree = ast.parse(source)
            except (SyntaxError, OSError):
                continue
            rel_path = str(relative).replace(os.sep, '/')
            for node in tree.body:
                if isinstance(node, ast.ClassDef):
                    self._extract_python_class(state, module, node, rel_path, scope_models)

    @staticmethod
    def _ast_str(value):
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            return value.value
        return None

    def _ast_str_list(self, value):
        if (single := self._ast_str(value)) is not None:
            return [single]
        if isinstance(value, (ast.List, ast.Tuple)):
            return [s for item in value.elts if (s := self._ast_str(item)) is not None]
        return []

    def _extract_python_class(self, state, module, class_node, rel_path, scope_models):
        model_name = None
        inherit_list = []
        inherits_map = {}
        for statement in class_node.body:
            targets = []
            value = None
            if isinstance(statement, ast.Assign):
                targets = [t.id for t in statement.targets if isinstance(t, ast.Name)]
                value = statement.value
            elif isinstance(statement, ast.AnnAssign) and isinstance(statement.target, ast.Name):
                targets = [statement.target.id]
                value = statement.value
            if not targets or value is None:
                continue
            if '_name' in targets:
                model_name = self._ast_str(value)
            if '_inherit' in targets:
                inherit_list = self._ast_str_list(value)
            if '_inherits' in targets and isinstance(value, ast.Dict):
                for k, v in zip(value.keys, value.values):
                    parent = self._ast_str(k)
                    delegate_field = self._ast_str(v)
                    if parent:
                        inherits_map[parent] = delegate_field

        primary = model_name or (inherit_list[0] if inherit_list else None)
        if not primary:
            return

        if model_name and ('model', model_name) in state.nodes:
            node_vals = state.nodes[('model', model_name)]
            # only the defining module's file counts as THE definition site
            if node_vals.get('module_name') in (module, False):
                node_vals['file_path'] = rel_path
                node_vals['line_no'] = class_node.lineno
                node_vals['module_name'] = node_vals.get('module_name') or module

        if model_name:
            for parent in inherit_list:
                if parent and parent != model_name:
                    self._ensure_model_skeleton(state, parent)
                    state.edge(('model', model_name), ('model', parent),
                               'model_inherit', module_name=module,
                               meta={'file': rel_path, 'line': class_node.lineno})
                elif parent == model_name and ('model', model_name) in state.nodes:
                    # `_name = X` + `_inherit = X`: in-place extension style
                    state.edge(('module', module), ('model', model_name),
                               'model_extend', module_name=module,
                               meta={'file': rel_path, 'line': class_node.lineno})
        else:
            for target in inherit_list:
                if ('model', target) in state.nodes:
                    state.edge(('module', module), ('model', target),
                               'model_extend', module_name=module,
                               meta={'file': rel_path, 'line': class_node.lineno})

        for parent, delegate_field in inherits_map.items():
            if primary and parent != primary:
                self._ensure_model_skeleton(state, parent)
                state.edge(('model', primary), ('model', parent),
                           'model_delegate', module_name=module,
                           meta={'field': delegate_field})

        if primary not in scope_models:
            return
        for statement in class_node.body:
            if not isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            method_name = statement.name
            if method_name.startswith('__'):
                continue
            calls_super = any(
                isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name)
                and sub.func.id == 'super'
                for sub in ast.walk(statement))
            key = f"{primary}.{method_name}"
            ident = state.node(
                'method', key,
                name=method_name, layer='python',
                module_name=module, model_name=primary,
                file_path=rel_path, line_no=statement.lineno,
                meta={'defs': []},
            )
            state.nodes[ident]['meta'].setdefault('defs', []).append({
                'module': module, 'file': rel_path,
                'line': statement.lineno, 'super': calls_super,
            })
            state.edge(('method', key), ('model', primary), 'method_of',
                       module_name=module)
            self._extract_method_calls(state, primary, statement, rel_path, module)

    def _resolve_receiver_model(self, expr, primary, varmap):
        """Best-effort static resolution of which model a call receiver is.

        Handles the conventional Odoo patterns:
          self.foo()                     -> primary model
          super().foo()                  -> 'SUPER' marker
          self.env['x'].foo()            -> 'x'
          self.partner_id.foo()          -> comodel chain via registry fields
          rec = self.env['x']; rec.foo() -> simple local variable tracking
          x.sudo().foo(), x.search().y() -> recordset-preserving chains
        Returns a model name, 'SUPER', or None (unresolvable).
        """
        env = self.env
        if isinstance(expr, ast.Name):
            if expr.id == 'self':
                return primary
            return varmap.get(expr.id)
        if isinstance(expr, ast.Subscript):
            base = expr.value
            is_env = ((isinstance(base, ast.Attribute) and base.attr == 'env')
                      or (isinstance(base, ast.Name) and base.id == 'env'))
            if is_env:
                return self._ast_str(expr.slice)
            return None
        if isinstance(expr, ast.Call):
            func = expr.func
            if isinstance(func, ast.Name) and func.id == 'super':
                return 'SUPER'
            if isinstance(func, ast.Attribute) and func.attr in CHAINABLE:
                return self._resolve_receiver_model(func.value, primary, varmap)
            return None
        if isinstance(expr, ast.Attribute):
            base_model = self._resolve_receiver_model(expr.value, primary, varmap)
            if base_model and base_model != 'SUPER' and base_model in env:
                field = env[base_model]._fields.get(expr.attr)
                if field is not None and field.relational:
                    return field.comodel_name
            return None
        return None

    def _extract_method_calls(self, state, primary, fn_node, rel_path, module):
        caller = ('method', f"{primary}.{fn_node.name}")
        unresolved = self._collect_calls(
            state, caller, fn_node, primary, {}, module, rel_path)
        if unresolved and caller in state.nodes:
            state.nodes[caller]['meta']['unresolved'] = unresolved

    def _collect_calls(self, state, caller_ident, root, primary, varmap_seed,
                       module, rel_path):
        """Shared call collector: walks any AST root (method body or server
        action code) and records method_calls edges from caller_ident.
        Returns the unresolved-call notes."""
        env = self.env

        # one pass building a var -> model map from simple assignments
        varmap = dict(varmap_seed)
        for sub in ast.walk(root):
            if (isinstance(sub, ast.Assign) and len(sub.targets) == 1
                    and isinstance(sub.targets[0], ast.Name)):
                resolved = self._resolve_receiver_model(sub.value, primary, varmap)
                if resolved and resolved != 'SUPER':
                    varmap[sub.targets[0].id] = resolved

        unresolved = []
        for sub in ast.walk(root):
            if not (isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute)):
                continue
            method_name = sub.func.attr
            receiver = self._resolve_receiver_model(sub.func.value, primary, varmap)
            if receiver == 'SUPER':
                target_model, kind = primary, 'super'
            elif receiver and receiver in env:
                target_model = receiver
                kind = 'self' if receiver == primary else 'cross'
            else:
                # record-looking receivers we could not resolve (honesty note)
                if (method_name not in ORM_COMMON
                        and method_name not in DATA_METHODS
                        and method_name not in PY_BUILTIN_METHODS
                        and not method_name.startswith('__')
                        and isinstance(sub.func.value, (ast.Attribute, ast.Call))
                        and f"{method_name}()" not in unresolved
                        and len(unresolved) < 12):
                    unresolved.append(f"{method_name}()")
                continue
            if kind != 'super':
                if method_name in ORM_COMMON:
                    continue  # ORM plumbing is noise
                if method_name in DATA_METHODS:
                    if kind == 'self' and primary:
                        continue  # self.write/create everywhere: noise
                    kind = 'data'  # cross-model data mutation: relevant
            target = self._ensure_method_skeleton(state, target_model, method_name)
            state.edge(caller_ident, target, 'method_calls', module_name=module,
                       meta={'file': rel_path, 'line': sub.lineno, 'kind': kind})
        return unresolved

    def _annotate_method_overrides(self, state):
        """Use the runtime MRO to order overrides and link core overriders."""
        env = self.env
        methods_by_model = defaultdict(list)
        for (node_type, key), vals in state.nodes.items():
            if node_type == 'method':
                methods_by_model[vals['model_name']].append((key, vals))
        for model_name, entries in methods_by_model.items():
            if model_name not in env:
                continue
            mro = [klass for klass in type(env[model_name]).mro()
                   if self._class_declares(klass, model_name)]
            for key, vals in entries:
                method_name = key.rsplit('.', 1)[-1]
                chain = []
                for klass in mro:
                    if method_name in getattr(klass, '__dict__', {}):
                        mod = getattr(klass, '_module', None)
                        if mod and mod not in chain:
                            chain.append(mod)
                if len(chain) > 1:
                    vals['meta']['mro'] = chain
                    for overrider in chain[:-1]:
                        if ('module', overrider) in state.nodes:
                            state.edge(('module', overrider), ('method', key),
                                       'method_override', module_name=overrider)

    # ══════════════════════════════════════════════
    # Extractor C: static assets (QWeb XML + JS conventions)
    # ══════════════════════════════════════════════

    def _extract_assets(self, state, module, module_path):
        static_src = module_path / 'static' / 'src'
        if not static_src.is_dir():
            return

        for xml_file in sorted(static_src.rglob('*.xml')):
            rel_path = str(xml_file.relative_to(module_path)).replace(os.sep, '/')
            try:
                tree = etree.parse(str(xml_file))
            except (etree.XMLSyntaxError, OSError):
                continue
            for t_el in tree.iter('t'):
                t_name = t_el.get('t-name')
                inherit = t_el.get('t-inherit')
                if t_name:
                    state.node(
                        'qweb_template', t_name,
                        name=t_name, layer='xml',
                        module_name=module,
                        file_path=rel_path, line_no=t_el.sourceline or 0,
                        meta={'owl': t_el.get('owl') or ''},
                    )
                if inherit:
                    source_key = t_name or f"{module}:{rel_path}:{t_el.sourceline}"
                    if not t_name:
                        state.node('qweb_template', source_key,
                                   name=f"(anonymous) {rel_path}", layer='xml',
                                   module_name=module, file_path=rel_path,
                                   line_no=t_el.sourceline or 0)
                    state.node('qweb_template', inherit, name=inherit, layer='xml',
                               is_skeleton=True)
                    state.edge(('qweb_template', source_key),
                               ('qweb_template', inherit),
                               'template_inherits', module_name=module,
                               meta={'mode': t_el.get('t-inherit-mode') or 'primary'})

        pending = []  # raw JS facts, resolved after every file is scanned
        for js_file in sorted(static_src.rglob('*.js')):
            rel_path = str(js_file.relative_to(module_path)).replace(os.sep, '/')
            try:
                source = js_file.read_text(encoding='utf-8', errors='replace')
            except OSError:
                continue
            last_class_key = None
            for line_no, line in enumerate(source.splitlines(), start=1):
                exported = 'export ' in line
                if match := JS_CLASS_RE.search(line):
                    class_name, parent_name = match.group(1), match.group(2)
                    key = f"{module}.{class_name}"
                    state.node(
                        'js_component', key,
                        name=class_name, layer='js',
                        module_name=module,
                        file_path=rel_path, line_no=line_no,
                        meta={'exported': exported},
                    )
                    state.js_alias.setdefault(class_name, ('js_component', key))
                    last_class_key = key
                    if parent_name not in JS_BASE_CLASSES:
                        pending.append(('extends', module, key, parent_name,
                                        rel_path, line_no))
                elif match := JS_PLAIN_CLASS_RE.search(line):
                    class_name = match.group(1)
                    key = f"{module}.{class_name}"
                    state.node(
                        'js_component', key,
                        name=class_name, layer='js',
                        module_name=module,
                        file_path=rel_path, line_no=line_no,
                        meta={'exported': exported, 'plain_class': True},
                    )
                    state.js_alias.setdefault(class_name, ('js_component', key))
                    last_class_key = key
                for func_re in (JS_EXPORT_FUNC_RE, JS_EXPORT_CONST_FUNC_RE):
                    if match := func_re.search(line):
                        func_name = match.group(1)
                        state.node(
                            'js_function', f"{module}.{func_name}",
                            name=func_name, layer='js',
                            module_name=module,
                            file_path=rel_path, line_no=line_no,
                            meta={'exported': True},
                        )
                        break
                if last_class_key and (match := JS_TEMPLATE_RE.search(line)):
                    pending.append(('template', module, last_class_key,
                                    match.group(1), rel_path, line_no))
                if match := JS_REGISTRY_RE.search(line):
                    category, entry, symbol = match.groups()
                    pending.append(('registry', module, category, entry,
                                    symbol, rel_path, line_no))
                if match := JS_PATCH_RE.search(line):
                    target = match.group(1)
                    if target not in ('this',):
                        pending.append(('patch', module, target, rel_path, line_no))

        for fact in pending:
            kind = fact[0]
            if kind == 'extends':
                _kind, module_name, child_key, parent_name, rel_path, line_no = fact
                parent = state.js_alias.get(parent_name.split('.')[-1])
                if parent:
                    state.edge(('js_component', child_key), parent,
                               'js_component_extends', module_name=module_name,
                               meta={'file': rel_path, 'line': line_no})
            elif kind == 'template':
                _kind, module_name, comp_key, template_name, rel_path, line_no = fact
                state.node('qweb_template', template_name, name=template_name,
                           layer='xml', is_skeleton=True)
                state.edge(('js_component', comp_key),
                           ('qweb_template', template_name),
                           'component_uses_template', module_name=module_name)
            elif kind == 'registry':
                _kind, module_name, category, entry, symbol, rel_path, line_no = fact
                symbol_node = state.js_alias.get((symbol or '').split('.')[-1])
                if symbol_node:
                    state.edge(('module', module_name), symbol_node,
                               'js_registers', module_name=module_name,
                               meta={'category': category, 'entry': entry,
                                     'file': rel_path, 'line': line_no})
                    state.nodes[symbol_node]['meta'].setdefault(
                        'registry', f"{category}/{entry}")
                elif category == 'services':
                    key = f"{module_name}.{entry}"
                    state.node('js_service', key, name=entry, layer='js',
                               module_name=module_name,
                               file_path=rel_path, line_no=line_no,
                               meta={'category': category})
                    state.edge(('module', module_name), ('js_service', key),
                               'js_registers', module_name=module_name,
                               meta={'category': category, 'entry': entry})
            elif kind == 'patch':
                _kind, module_name, target, rel_path, line_no = fact
                target_node = state.js_alias.get(target)
                if not target_node:
                    target_node = ('js_component', target)
                    state.node('js_component', target, name=target, layer='js',
                               is_skeleton=True)
                state.edge(('module', module_name), target_node, 'js_patches',
                           module_name=module_name,
                           meta={'file': rel_path, 'line': line_no})

    # ══════════════════════════════════════════════
    # Persistence
    # ══════════════════════════════════════════════

    def _patch_module_stats(self, state):
        model_counts = defaultdict(int)
        for (node_type, _key), vals in state.nodes.items():
            if node_type == 'model' and vals.get('module_name'):
                model_counts[vals['module_name']] += 1
        for (node_type, key), vals in state.nodes.items():
            if node_type == 'module':
                vals['meta']['model_count'] = model_counts.get(key, 0)

    def _persist(self, state):
        env = self.env
        env.flush_all()
        env.cr.execute("DELETE FROM ai_code_edge")
        env.cr.execute("DELETE FROM ai_code_node")
        env.invalidate_all()

        Node = env['ai.code.node'].sudo()
        idents = list(state.nodes.keys())
        vals_list = [state.nodes[ident] for ident in idents]
        key_to_id = {}
        BATCH = 2000
        for offset in range(0, len(vals_list), BATCH):
            records = Node.create(vals_list[offset:offset + BATCH])
            for ident, record in zip(idents[offset:offset + BATCH], records):
                key_to_id[ident] = record.id

        Edge = env['ai.code.edge'].sudo()
        edge_vals = []
        for (src, tgt, _etype), vals in state.edges.items():
            source_id = key_to_id.get(src)
            target_id = key_to_id.get(tgt)
            if source_id and target_id:
                edge_vals.append({**vals, 'source_id': source_id, 'target_id': target_id})
        for offset in range(0, len(edge_vals), BATCH):
            Edge.create(edge_vals[offset:offset + BATCH])

        env.flush_all()
        env.cr.execute("""
            UPDATE ai_code_node n SET out_degree = COALESCE(s.c, 0)
            FROM (SELECT source_id, COUNT(*) c FROM ai_code_edge GROUP BY source_id) s
            WHERE s.source_id = n.id
        """)
        env.cr.execute("""
            UPDATE ai_code_node n SET in_degree = COALESCE(s.c, 0)
            FROM (SELECT target_id, COUNT(*) c FROM ai_code_edge GROUP BY target_id) s
            WHERE s.target_id = n.id
        """)
        env.cr.execute("UPDATE ai_code_node SET degree = in_degree + out_degree")
        env.invalidate_all()
        return len(vals_list), len(edge_vals)
