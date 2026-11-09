# -*- coding: utf-8 -*-
"""Pre-flight static linker: cross-artifact consistency checks that catch the
bulk of sandbox failures in milliseconds (no database, no subprocess).

Runs AFTER the coding phase and BEFORE the sandbox: view fields vs model
fields, xmlid references vs definitions (in LOAD order), access csv coverage,
manifest data completeness, __init__ imports. Pure stdlib+lxml on purpose —
no Odoo imports — so it is unit-testable outside the server.

Design rule: NO false positives. Every check is scoped to facts fully visible
in the module's own files (own models, own xmlids); anything that would need
the registry (inherited core fields, foreign xmlids) is skipped or reduced to
what IS verifiable (e.g. the foreign module directory exists).
"""
import ast
import csv
import io
import pathlib
import re

from lxml import etree

# fields present on every model / added by well-known mixins
MAGIC_FIELDS = {'id', 'display_name', 'create_uid', 'create_date',
                'write_uid', 'write_date', '__last_update'}
MIXIN_FIELDS = {
    'mail.thread': {'message_ids', 'message_follower_ids', 'message_partner_ids',
                    'message_attachment_count', 'message_main_attachment_id',
                    'message_is_follower', 'message_needaction', 'message_has_error'},
    'mail.activity.mixin': {'activity_ids', 'activity_state', 'activity_user_id',
                            'activity_type_id', 'activity_date_deadline',
                            'activity_summary', 'activity_exception_decoration',
                            'activity_exception_icon'},
}
# methods available on every BaseModel (buttons may call them without defining)
BASE_METHODS = {'toggle_active', 'action_archive', 'action_unarchive',
                'copy', 'unlink', 'read', 'write', 'create',
                'message_post', 'activity_schedule'}
XMLID_TAGS = ('record', 'menuitem', 'template', 'act_window', 'report')
_REF_IN_EVAL = re.compile(r"ref\(\s*['\"]([\w.]+)['\"]\s*\)")
_PERCENT_REF = re.compile(r"%\(([\w.]+)\)[ds]")


class _Model:
    __slots__ = ('name', 'inherits', 'fields', 'methods', 'file')

    def __init__(self, name, file):
        self.name = name
        self.file = file
        self.inherits = []
        self.fields = set()
        self.methods = set()

    @property
    def all_fields(self):
        out = set(self.fields) | MAGIC_FIELDS
        for mixin, extra in MIXIN_FIELDS.items():
            if mixin in self.inherits:
                out |= extra
        return out


def _const_str(node):
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def _scan_python(py_path, rel, models, issues):
    """Collect _name models (fields + methods) from one python file."""
    try:
        tree = ast.parse(py_path.read_text(encoding='utf-8', errors='replace'))
    except SyntaxError as e:   # write-gate should have caught it; report anyway
        issues.append(f"{rel}: python syntax error at line {e.lineno}: {e.msg}")
        return
    for cls in [n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)]:
        name = None
        inherits = []
        fields = set()
        methods = set()
        for stmt in cls.body:
            if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1 \
                    and isinstance(stmt.targets[0], ast.Name):
                target = stmt.targets[0].id
                if target == '_name':
                    name = _const_str(stmt.value)
                elif target == '_inherit':
                    if isinstance(stmt.value, (ast.List, ast.Tuple)):
                        inherits += [s for s in map(_const_str, stmt.value.elts) if s]
                    else:
                        s = _const_str(stmt.value)
                        if s:
                            inherits.append(s)
                elif isinstance(stmt.value, ast.Call):
                    func = stmt.value.func
                    if isinstance(func, ast.Attribute) and \
                            isinstance(func.value, ast.Name) and func.value.id == 'fields':
                        fields.add(target)
            elif isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
                methods.add(stmt.name)
        if name:   # own model (a bare _inherit extension is not field-checkable)
            model = models.get(name) or _Model(name, rel)
            model.inherits += inherits
            model.fields |= fields
            model.methods |= methods
            models[name] = model


def _iter_data_elements(root):
    """Yield the record-ish elements in document order (through <data> wrappers)."""
    for el in root.iter():
        if isinstance(el.tag, str) and el.tag in XMLID_TAGS:
            yield el


def _local_xmlid(xmlid, module):
    """Normalize an xmlid to its local name if it belongs to this module."""
    if '.' not in xmlid:
        return xmlid
    mod, _, local = xmlid.partition('.')
    return local if mod == module else None


def preflight_scan(module_dir, module_name=None, module_exists=None):
    """Scan one module directory; return a list of human-readable issues.

    module_exists: optional callable(name) -> bool for foreign-module existence
    (the caller passes Odoo's resolver; None disables that check).
    """
    module_dir = pathlib.Path(module_dir)
    module = module_name or module_dir.name
    issues = []

    manifest_path = module_dir / '__manifest__.py'
    if not manifest_path.is_file():
        return [f"__manifest__.py is missing in {module}"]
    try:
        manifest = ast.literal_eval(manifest_path.read_text(encoding='utf-8',
                                                            errors='replace'))
    except (ValueError, SyntaxError) as e:
        return [f"__manifest__.py cannot be parsed: {e}"]
    if not isinstance(manifest, dict):
        return ["__manifest__.py must evaluate to a dict"]

    data_files = [str(f) for f in (manifest.get('data') or [])]
    demo_files = [str(f) for f in (manifest.get('demo') or [])]

    # ── manifest data completeness ─────────────────────────────────────
    for f in data_files + demo_files:
        if not (module_dir / f).is_file():
            issues.append(f"__manifest__.py lists '{f}' but the file does not exist")
    on_disk = [p.relative_to(module_dir).as_posix()
               for p in module_dir.rglob('*')
               if p.is_file() and p.suffix in ('.xml', '.csv')
               and 'static' not in p.relative_to(module_dir).parts
               and p.relative_to(module_dir).parts[0] not in ('tests', 'i18n')]
    for rel in on_disk:
        if rel not in data_files and rel not in demo_files:
            issues.append(f"'{rel}' exists on disk but is NOT in the manifest "
                          f"'data' list — it will never load")

    # ── __init__ import chain ──────────────────────────────────────────
    for pkg in ('', 'models', 'wizards', 'wizard', 'controllers', 'report'):
        pkg_dir = module_dir / pkg if pkg else module_dir
        init = pkg_dir / '__init__.py'
        if not pkg_dir.is_dir():
            continue
        if not init.is_file():
            if any(p.suffix == '.py' and p.name != '__init__.py'
                   for p in pkg_dir.iterdir() if p.is_file()):
                issues.append(f"{(pkg or '.')}/__init__.py is missing")
            continue
        # comments must not satisfy the check ("# TODO import thing" is not an import)
        init_src = re.sub(r'#[^\n]*', '', init.read_text(encoding='utf-8', errors='replace'))
        for p in pkg_dir.iterdir():
            if p.is_file() and p.suffix == '.py' and not p.name.startswith('__'):
                mod_name = p.stem
                if not re.search(rf'\bimport\b[^\n]*\b{re.escape(mod_name)}\b', init_src):
                    issues.append(f"{(pkg or '.')}/__init__.py does not import "
                                  f"'{mod_name}' — {p.relative_to(module_dir).as_posix()} "
                                  f"will never load")
        if not pkg:   # root __init__ must import each python package used
            for sub in ('models', 'wizards', 'wizard', 'controllers', 'report'):
                if (module_dir / sub / '__init__.py').is_file() and \
                        not re.search(rf'\bimport\b[^\n]*\b{sub}\b', init_src):
                    issues.append(f"__init__.py does not import the '{sub}' package")

    # ── python model registry ──────────────────────────────────────────
    models = {}
    for p in module_dir.rglob('*.py'):
        rel = p.relative_to(module_dir).as_posix()
        if rel.startswith(('static/', 'tests/')):
            continue
        _scan_python(p, rel, models, issues)

    # ── xmlid definitions in LOAD order + reference checks ─────────────
    defined_order = {}   # local xmlid -> (file_index, element_index)
    deferred_refs = []   # (file, file_index, elem_index, xmlid, kind)
    parsed_xml = {}
    for f_idx, rel in enumerate(data_files):
        path = module_dir / rel
        if not path.is_file() or path.suffix != '.xml':
            continue
        try:
            root = etree.parse(str(path)).getroot()
        except etree.XMLSyntaxError as e:
            issues.append(f"{rel}: XML syntax error: {e.msg}")
            continue
        parsed_xml[rel] = root
        for e_idx, el in enumerate(_iter_data_elements(root)):
            xmlid = el.get('id')
            if xmlid:
                local = _local_xmlid(xmlid, module)
                if local and local not in defined_order:
                    defined_order[local] = (f_idx, e_idx)
            # collect every reference this element makes
            refs = []
            if el.tag == 'menuitem':
                for attr in ('parent', 'action'):
                    if el.get(attr):
                        refs.append((el.get(attr), attr))
                for g in (el.get('groups') or '').split(','):
                    g = g.strip().lstrip('-').lstrip('!')
                    if g:
                        refs.append((g, 'groups'))
            for sub in el.iter():
                if sub.get('ref'):
                    refs.append((sub.get('ref'), f"<{sub.tag} ref>"))
                for attr in ('groups',):
                    if sub is not el and sub.get(attr):
                        for g in sub.get(attr).split(','):
                            g = g.strip().lstrip('-').lstrip('!')
                            if g:
                                refs.append((g, attr))
                for m in _REF_IN_EVAL.finditer(sub.get('eval') or ''):
                    refs.append((m.group(1), 'eval ref()'))
                if sub.get('name') and (sub.get('type') == 'action'
                                        or (sub.get('name') or '').startswith('%(')):
                    for m in _PERCENT_REF.finditer(sub.get('name')):
                        refs.append((m.group(1), 'button action'))
            for target, kind in refs:
                deferred_refs.append((rel, f_idx, e_idx, target, kind))

    for rel, f_idx, e_idx, target, kind in deferred_refs:
        local = _local_xmlid(target, module)
        if local is not None:
            pos = defined_order.get(local)
            if pos is None:
                issues.append(f"{rel}: {kind} references '{target}' but no such "
                              f"xmlid is defined in this module")
            elif pos > (f_idx, e_idx):
                def_file = data_files[pos[0]]
                issues.append(f"{rel}: {kind} references '{target}' which is only "
                              f"defined LATER ({def_file}) — reorder the manifest "
                              f"data list or move the record")
        elif module_exists is not None and '.' in target:
            foreign = target.split('.', 1)[0]
            if not module_exists(foreign):
                issues.append(f"{rel}: {kind} references '{target}' but there is "
                              f"no module named '{foreign}' (typo?)")

    # ── view arch checks: fields + button methods on OWN models ────────
    for rel, root in parsed_xml.items():
        for record in root.iter('record'):
            if record.get('model') != 'ir.ui.view':
                continue
            model_name = None
            arch = None
            inherit = False
            for fld in record.findall('field'):
                if fld.get('name') == 'model':
                    model_name = (fld.text or '').strip()
                elif fld.get('name') == 'arch':
                    arch = fld
                elif fld.get('name') == 'inherit_id':
                    inherit = True
            model = models.get(model_name)
            if arch is None or model is None or inherit:
                continue   # inherited/foreign views need the registry — sandbox's job
            known = model.all_fields

            def _in_subview(node):
                # a field/button nested under another <field> belongs to the
                # COMODEL's inline subview — not checkable against this model
                return any(anc.tag == 'field' and anc is not arch
                           for anc in node.iterancestors())

            for node in arch.iter('field'):
                if node is arch:      # lxml iter() yields the element itself
                    continue
                fname = node.get('name')
                if not fname or '.' in fname or _in_subview(node):
                    continue
                if fname not in known:
                    issues.append(f"{rel}: view of {model_name} uses field "
                                  f"'{fname}' which is not defined on the model "
                                  f"({model.file})")
            for btn in arch.iter('button'):
                if btn.get('type') == 'object' and not _in_subview(btn):
                    meth = btn.get('name') or ''
                    if meth and meth not in model.methods and meth not in BASE_METHODS:
                        issues.append(f"{rel}: button calls method '{meth}' which "
                                      f"is not defined on {model_name} ({model.file})")

    # ── access csv coverage ────────────────────────────────────────────
    own_models = set(models)
    covered = set()
    for rel in data_files:
        if not rel.endswith('.csv'):
            continue
        path = module_dir / rel
        if not path.is_file():
            continue
        try:
            rows = list(csv.DictReader(io.StringIO(
                path.read_text(encoding='utf-8', errors='replace'))))
        except csv.Error as e:
            issues.append(f"{rel}: csv parse error: {e}")
            continue
        for i, row in enumerate(rows, 2):
            mid = (row.get('model_id:id') or row.get('model_id/id') or '').strip()
            if not mid:
                continue
            local = _local_xmlid(mid, module)
            if local is None:      # foreign model xmlid — not verifiable here
                continue
            if not local.startswith('model_'):
                issues.append(f"{rel}:{i}: model_id:id '{mid}' should look like "
                              f"model_<model_name_with_underscores>")
                continue
            dotted = local[len('model_'):].replace('_', '.')
            # underscores are ambiguous ('.' vs '_'): accept if ANY own model matches
            match = next((m for m in own_models
                          if m.replace('.', '_') == local[len('model_'):]), None)
            if match:
                covered.add(match)
            else:
                issues.append(f"{rel}:{i}: access line targets '{dotted}' but this "
                              f"module defines no such model")
    for m in sorted(own_models - covered):
        # TransientModels get default access since 13.0 — still list them; but
        # only WARN-level for wizards (heuristic: name defined in wizard file)
        issues.append(f"ir.model.access.csv has NO line for model '{m}' — "
                      f"non-admin users cannot use it")

    return issues
