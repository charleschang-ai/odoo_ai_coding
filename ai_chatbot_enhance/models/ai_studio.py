import re
import time
import logging
from lxml import etree
from odoo import models

_logger = logging.getLogger(__name__)


class AiStudio(models.Model):
    """
    Helper model for AI Studio safe_eval tools.
    _auto = False: no database table. Model exists purely for method dispatch.
    All public methods callable from ir.actions.server code via record.sudo().
    """
    _name = 'ai.studio'
    _description = 'AI Studio Helpers'
    _auto = False
    _log_access = False

    # ══════════════════════════════════════════════
    # Model helpers
    # ══════════════════════════════════════════════

    def get_models_list(self, search_term='', limit=50):
        """List installed non-transient models matching search_term."""
        domain = [('transient', '=', False)]
        if search_term:
            domain += ['|',
                       ('model', 'ilike', search_term),
                       ('name', 'ilike', search_term),
                       ]
        recs = self.env['ir.model'].sudo().search(
            domain, order='model asc', limit=int(limit)
        )
        if not recs:
            return f"No models found matching '{search_term}'."
        lines = [f"{'Technical Name':<45} Display Name", "-" * 70]
        lines += [f"{m.model:<45} {m.name}" for m in recs]
        return "\n".join(lines)

    def get_model_id(self, model_name):
        """Return ir.model id for a technical model name, or None."""
        rec = self.env['ir.model'].sudo().search(
            [('model', '=', model_name)], limit=1
        )
        return rec.id if rec else None

    # ══════════════════════════════════════════════
    # Field helpers
    # ══════════════════════════════════════════════

    def search_fields(self, model_name, search_term=''):
        """
        Search model fields by technical name OR string label.
        Returns formatted string — helps LLM find correct field names.
        """
        if model_name not in self.env:
            return f"Model '{model_name}' not found."

        all_fields = self.env[model_name].sudo().fields_get(
            attributes=['string', 'type', 'required']
        )
        if search_term:
            search_lower = search_term.lower()
            all_fields = {
                k: v for k, v in all_fields.items()
                if search_lower in k.lower() or search_lower in v.get('string', '').lower()
            }

        if not all_fields:
            return f"No fields found matching '{search_term}' on {model_name}."

        lines = [f"{'Technical Name':<35} {'Label':<30} Type", "-" * 80]
        for fname, fdef in sorted(all_fields.items()):
            lines.append(
                f"{fname:<35} {fdef.get('string', ''):<30} {fdef.get('type', '')}"
            )
        return "\n".join(lines)

    def validate_field_name(self, name):
        """Validate/normalize custom field name. Returns (name, error_or_None)."""
        name = name.strip().lower().replace(' ', '_').replace('-', '_')
        if not name:
            return None, "Field name cannot be empty."
        name = re.sub(r'[^a-z0-9_]', '', name)
        if not name:
            return None, "Field name contains no valid characters after cleanup."
        if not name.startswith('x_'):
            name = 'x_' + name
        if len(name) > 64:
            return None, f"Field name too long ({len(name)} chars, max 64)."
        return name, None

    def parse_selection(self, selection_input):
        """
        Parse selection options into list of [value, label] pairs.
        Accepts:
          'Bronze, Silver, Gold'          → [['bronze','Bronze'],...]
          'bronze:Bronze, silver:Silver'  → explicit key:label
          [['bronze','Bronze']]           → passed through as-is
        Returns (list_of_pairs, error_or_None)
        """
        if isinstance(selection_input, list):
            return selection_input, None
        if not isinstance(selection_input, str) or not selection_input.strip():
            return None, "Selection options cannot be empty."
        result = []
        for item in [s.strip() for s in selection_input.split(',') if s.strip()]:
            if ':' in item:
                parts = item.split(':', 1)
                val = parts[0].strip().lower().replace(' ', '_')
                label = parts[1].strip()
            else:
                label = item.strip()
                val = label.lower().replace(' ', '_')
            if val and label:
                result.append([val, label])
        return (result, None) if result else (None, "Could not parse selection options.")

    def add_custom_field(self, model_name, field_name, field_type,
                         field_label='', selection='', relation='',
                         required=False, field_help=''):
        """
        Create a custom x_ field on a model.
        Returns (field_record, error_or_None)
        """
        VALID_TYPES = [
            'char', 'text', 'html',
            'integer', 'float', 'monetary',
            'boolean', 'date', 'datetime',
            'selection', 'many2one', 'many2many', 'one2many',
            'binary',
        ]

        # ── Validate model ──
        model_id = self.get_model_id(model_name)
        if not model_id:
            return None, (
                f"Model '{model_name}' not found. "
                f"Call 'AI Studio: Get Models' to find the correct model name."
            )

        # ── Validate and normalize field name ──
        field_name, err = self.validate_field_name(field_name)
        if err:
            return None, err

        # ── Check for duplicates ──
        existing = self.env['ir.model.fields'].sudo().search([
            ('model_id', '=', model_id),
            ('name', '=', field_name),
        ], limit=1)
        if existing:
            return None, (
                f"Field '{field_name}' already exists on {model_name}. "
                f"Label: '{existing.field_description}', Type: {existing.ttype}. "
                f"Use 'AI Studio: Extend View' to add it to a view."
            )

        # ── Validate field type ──
        if field_type not in VALID_TYPES:
            return None, (
                f"Invalid field type '{field_type}'. "
                f"Valid types: {', '.join(VALID_TYPES)}"
            )

        # ── Build vals ──
        auto_label = field_name.replace('x_', '').replace('_', ' ').title()
        vals = {
            'model_id': model_id,
            'name': field_name,
            'field_description': field_label.strip() if field_label.strip() else auto_label,
            'ttype': field_type,
            'state': 'manual',
            'required': bool(required),
        }
        if field_help:
            vals['help'] = field_help

        # ── Selection: build selection_ids (Odoo 16+ format) ──
        if field_type == 'selection':
            if not selection:
                return None, (
                    "Selection field requires options. "
                    "Example: 'Bronze, Silver, Gold' or 'draft:Draft, done:Done'"
                )
            sel_list, err = self.parse_selection(selection)
            if err:
                return None, err
            vals['selection_ids'] = [
                (0, 0, {'value': v, 'name': l, 'sequence': i * 10})
                for i, (v, l) in enumerate(sel_list)
            ]

        # ── Relation fields ──
        if field_type in ('many2one', 'many2many', 'one2many'):
            if not relation:
                return None, (
                    f"Field type '{field_type}' requires 'relation' "
                    f"(the related model). Example: 'res.partner'"
                )
            if not self.env['ir.model'].sudo().search(
                    [('model', '=', relation)], limit=1
            ):
                return None, (
                    f"Related model '{relation}' not found. "
                    f"Call 'AI Studio: Get Models' to find the correct name."
                )
            vals['relation'] = relation

        # ── Create ──
        try:
            field = self.env['ir.model.fields'].sudo().create(vals)
            _logger.info("AI Studio: created field %s on %s", field_name, model_name)
            return field, None
        except Exception as e:
            _logger.exception("AI Studio: add_custom_field failed")
            return None, f"Database error creating field: {e}"

    # ══════════════════════════════════════════════
    # View helpers
    # ══════════════════════════════════════════════

    def get_views_for_model(self, model_name, view_type=''):
        """
        List views for a model with key structural info.
        Returns formatted string including view IDs, types, keys,
        and a note that Get View Structure can show the full structure.
        """
        domain = [('model', '=', model_name), ('type', '!=', 'qweb')]
        if view_type:
            domain.append(('type', '=', view_type))

        # Only show primary views (not inherited ones) first
        primary = self.env['ir.ui.view'].sudo().search(
            domain + [('inherit_id', '=', False)], order='type asc, priority asc'
        )
        inherited = self.env['ir.ui.view'].sudo().search(
            domain + [('inherit_id', '!=', False)], order='type asc, priority asc', limit=10
        )

        if not primary and not inherited:
            return f"No views found for model '{model_name}'."

        lines = [f"Views for '{model_name}':"]
        if primary:
            lines.append("\nPrimary views (use these IDs for Extend View):")
            for v in primary:
                key = f" key={v.key}" if v.key else ""
                lines.append(f"  ID:{v.id:<6} [{v.type:<8}] {v.name}{key}")
        if inherited:
            lines.append("\nInherited views (extensions):")
            for v in inherited[:5]:
                lines.append(f"  ID:{v.id:<6} [{v.type:<8}] {v.name} (extends: {v.inherit_id.name})")

        lines.append(
            f"\nTIP: Call 'AI Studio: Get View Structure' with the view ID "
            f"to see fields, groups and correct xpath expressions."
        )
        return "\n".join(lines)

    def get_view_structure(self, view_id):
        """
        Return the arch structure of a view with safe xpath recommendations.
        Detects fields inside <div>/<span>/<label> wrappers and warns the AI
        not to use 'after' on those — suggests the parent group instead.
        Uses the COMBINED arch so all inherited fields are visible.
        """
        view = self.env['ir.ui.view'].sudo().browse(int(view_id))
        if not view.exists():
            return f"View ID {view_id} not found."

        try:
            arch_str = view.arch  # combined with all inheritance applied
            tree = etree.fromstring(arch_str.encode('utf-8'))
        except Exception as e:
            return f"Error parsing view arch: {e}"

        def field_safety(field_el):
            """
            Returns (is_safe_for_after, safe_xpath, warning_note).
            A field is safe for 'after' only if its direct parent is <group>.
            If inside <div>/<span>/<label>, suggest the ancestor group instead.
            """
            fname = field_el.get('name', '')
            parent = field_el.getparent()
            if parent is None:
                return False, '//sheet', 'unknown parent'
            if parent.tag == 'group':
                return True, f"//field[@name='{fname}']", ''
            # Find nearest group ancestor
            ancestor = parent
            while ancestor is not None:
                if ancestor.tag == 'group':
                    gname = ancestor.get('name', '')
                    alt = f"//group[@name='{gname}']" if gname else '//group'
                    note = (
                        f"'{fname}' is inside <{parent.tag}> (not directly in a group). "
                        f"Using 'after //field[@name=\"{fname}\"]' will fail or mis-place the field. "
                        f"Use '{alt}' with position='inside' instead."
                    )
                    return False, alt, note
                ancestor = ancestor.getparent()
            return False, '//sheet', f"'{fname}' has complex nesting"

        lines = [
            f"View: {view.name} (ID:{view.id})",
            f"Model: {view.model} | Type: {view.type}",
            f"Key: {view.key or '(none)'}",
            "",
            "Structure — safe xpath targets for Extend View:",
        ]

        # Header
        header_els = tree.xpath('//header')
        if header_els:
            btns = [b.get('string', b.get('name', '?')) for b in header_els[0].xpath('button')]
            sbar = header_els[0].xpath("field[@widget='statusbar']/@name")
            lines.append(f"\n  //header (position: inside)")
            if btns:
                lines.append(f"    buttons: {', '.join(btns)}")
            if sbar:
                lines.append(f"    statusbar field: {sbar[0]}")

        # Groups (not inside pages)
        for group in tree.xpath('//group[not(ancestor::page)]'):
            gname = group.get('name', '')
            gstr = group.get('string', '')
            gxp = f"//group[@name='{gname}']" if gname else '//group'
            header_label = gxp
            if gstr:
                header_label += f" ('{gstr}')"
            lines.append(f"\n  {header_label} — position: inside")

            for field_el in group.xpath('.//field'):
                fname = field_el.get('name', '')
                if not fname:
                    continue
                is_safe, suggestion, note = field_safety(field_el)
                if is_safe:
                    lines.append(
                        f"    ✅ {fname:<30} "
                        f"→ safe target: //field[@name='{fname}'] after/before"
                    )
                else:
                    lines.append(
                        f"    ⚠️  {fname:<30} "
                        f"→ UNSAFE for 'after' (wrapped in <{field_el.getparent().tag}>)"
                    )
                    lines.append(f"       Instead use: {suggestion} position: inside")

        # Notebook pages
        pages = tree.xpath('//page')
        if pages:
            lines.append("")
        for page in pages:
            pname = page.get('name', '')
            pstr = page.get('string', '')
            pxp = (
                f"//page[@name='{pname}']" if pname
                else f"//page[@string='{pstr}']"
            )
            fields_in_page = [
                                 f.get('name') for f in page.xpath('.//field')
                                 if f.get('name') and f.getparent().tag == 'group'
                             ][:6]
            lines.append(f"\n  {pxp} (tab: '{pstr}') — position: inside")
            if fields_in_page:
                lines.append(f"    safe fields: {', '.join(fields_in_page)}")

        lines.append(
            "\n\nRules:\n"
            "  ✅ safe fields → use //field[@name='x'] with after/before\n"
            "  ⚠️  unsafe fields → use the parent //group[@name='...'] with inside\n"
            "  Always prefer named groups over field-level targeting when in doubt.\n"
            "  Use single quotes inside xpath: [@name='value'] NOT [@name=\"value\"]"
        )
        return "\n".join(lines)

    def validate_xpath(self, arch_str, xpath_expr):
        """
        Validate xpath against view arch string.
        Returns (match_count, error_or_None)
        """
        try:
            tree = etree.fromstring(arch_str.encode('utf-8'))
            results = tree.xpath(xpath_expr)
            return len(results), None
        except etree.XPathSyntaxError as e:
            return 0, f"Invalid xpath syntax: {e}"
        except etree.XPathEvalError as e:
            return 0, f"XPath eval error: {e}"
        except etree.XMLSyntaxError as e:
            return 0, f"Invalid view XML: {e}"

    def extend_view(self, view_id_or_xmlid, xpath_expr, position,
                    field_name='', extra_xml='', view_key_suffix=''):
        """
        Extend a view via xpath inheritance.
        - Normalizes xpath quotes (converts " to ') to prevent XML attribute conflicts
        - Uses lxml to build arch_db (proper XML escaping)
        - Uses savepoint to prevent broken views from persisting on failure
        - Validates against COMBINED arch so inherited fields are found
        Returns (new_view_record, error_or_None)
        """
        VALID_POSITIONS = ('before', 'after', 'inside', 'replace')

        # ── Find base view ──
        try:
            vid = int(view_id_or_xmlid)
            base_view = self.env['ir.ui.view'].sudo().browse(vid)
            if not base_view.exists():
                return None, f"View with ID {vid} not found."
        except (ValueError, TypeError):
            base_view = self.env.ref(str(view_id_or_xmlid), raise_if_not_found=False)
            if not base_view:
                return None, f"View '{view_id_or_xmlid}' not found."

        # ── Validate position ──
        if position not in VALID_POSITIONS:
            return None, (
                f"Invalid position '{position}'. "
                f"Use: {', '.join(VALID_POSITIONS)}"
            )

        # ── Normalize xpath: replace double quotes with single quotes ──
        # Both are valid xpath string literal delimiters.
        # Single quotes are safer inside XML double-quoted attributes.
        xpath_expr = xpath_expr.replace('"', "'")

        # ── Validate xpath against COMBINED arch (includes inherited fields) ──
        combined_arch = base_view.arch
        count, err = self.validate_xpath(combined_arch, xpath_expr)
        if err:
            return None, (
                f"XPath error: {err}. "
                f"Call 'AI Studio: Get View Structure' (view_id={base_view.id}) "
                f"to see correct xpath expressions."
            )
        if count == 0:
            return None, (
                f"XPath '{xpath_expr}' matched 0 elements in the combined view. "
                f"Call 'AI Studio: Get View Structure' (view_id={base_view.id}) "
                f"to see available elements and their correct xpath expressions."
            )

        # ── Build content XML ──
        if field_name:
            if field_name not in self.env[base_view.model].sudo().fields_get():
                return None, (
                    f"Field '{field_name}' does not exist on model '{base_view.model}'. "
                    f"Use 'AI Studio: Search Fields' to verify the field name."
                )
            content_xml = f'<field name="{field_name}"/>'
        elif extra_xml:
            content_xml = extra_xml.strip()
        else:
            return None, "Provide either field_name or extra_xml."

        # ── Validate content XML syntax ──
        try:
            etree.fromstring(f'<root>{content_xml}</root>'.encode('utf-8'))
        except etree.XMLSyntaxError as e:
            return None, f"Invalid XML content: {e}"

        # ── Build arch_db using lxml (guarantees correct XML escaping) ──
        data_el = etree.Element('data')
        xpath_el = etree.SubElement(data_el, 'xpath')
        xpath_el.set('expr', xpath_expr)  # lxml escapes any special chars
        xpath_el.set('position', position)
        content_root = etree.fromstring(f'<root>{content_xml}</root>'.encode('utf-8'))
        for child in content_root:
            xpath_el.append(child)
        arch_db = etree.tostring(data_el, pretty_print=True, encoding='unicode')

        # ── Generate unique view key ──
        suffix = view_key_suffix or (
            field_name.replace('x_', '') if field_name
            else f'{position}_{int(time.time()) % 10000}'
        )
        base = base_view.model.replace('.', '_')
        view_key = f'ai_studio.{base}_{base_view.type}_{suffix}'
        if self.env['ir.ui.view'].sudo().search([('key', '=', view_key)], limit=1):
            view_key = f'{view_key}_{int(time.time()) % 10000}'

        # ── Create with savepoint: prevents broken views from persisting on failure ──
        try:
            with self.env.cr.savepoint():
                new_view = self.env['ir.ui.view'].sudo().create({
                    'name': f'AI Studio: {base_view.model} {base_view.type} +{suffix}',
                    'model': base_view.model,
                    'type': base_view.type,
                    'inherit_id': base_view.id,
                    'arch_db': arch_db,
                    'key': view_key,
                    'priority': 99,
                })
            _logger.info(
                "AI Studio: extended view %s (ID:%s) xpath=%s position=%s",
                base_view.name, base_view.id, xpath_expr, position
            )
            return new_view, None
        except Exception as e:
            _logger.exception("AI Studio: extend_view failed")
            return None, (
                f"Error creating view: {e}. "
                f"The failed view was NOT saved (savepoint rolled back). "
                f"Call 'AI Studio: Get View Structure' (view_id={base_view.id}) "
                f"to verify the correct xpath before retrying."
            )

    def get_view_arch_raw(self, view_id):
        """
        Return the raw arch_db of a specific view (not combined).
        Use this to inspect exactly what was stored, especially for debugging
        AI Studio-created inheritance views.
        """
        view = self.env['ir.ui.view'].sudo().browse(int(view_id))
        if not view.exists():
            return f"View ID {view_id} not found."
        return (
            f"View ID   : {view.id}\n"
            f"Name      : {view.name}\n"
            f"Model     : {view.model}\n"
            f"Type      : {view.type}\n"
            f"Key       : {view.key or '(none)'}\n"
            f"Active    : {view.active}\n"
            f"Inherit ID: {view.inherit_id.id if view.inherit_id else 'None'} "
            f"({view.inherit_id.name if view.inherit_id else 'None'})\n"
            f"Priority  : {view.priority}\n"
            f"\narch_db (stored XML):\n{view.arch_db}"
        )

    def create_view(self, model_name, view_type, field_names,
                    view_name='', create_action=False, action_name=''):
        """
        Create a brand-new standalone view.
        Returns (view_record, action_or_None, error_or_None)
        """
        VALID_TYPES = ('list', 'form', 'kanban')

        if not self.get_model_id(model_name):
            return None, None, f"Model '{model_name}' not found."

        if view_type not in VALID_TYPES:
            return None, None, (
                f"Invalid view type '{view_type}'. "
                f"Use: {', '.join(VALID_TYPES)}"
            )

        if not field_names:
            return None, None, "At least one field name is required."

        # ── Validate all fields exist ──
        all_fields = self.env[model_name].sudo().fields_get(attributes=['type'])
        bad = [f for f in field_names if f not in all_fields]
        if bad:
            return None, None, (
                f"Fields not found on {model_name}: {bad}. "
                f"Call 'AI Studio: Search Fields' to verify field names."
            )

        # ── Build arch ──
        vname = view_name.strip() or f'{model_name} {view_type}'
        field_tags = ''.join(f'\n        <field name="{f}"/>' for f in field_names)

        if view_type == 'list':
            arch_db = f'<list string="{vname}">{field_tags}\n    </list>'
        elif view_type == 'form':
            arch_db = (
                f'<form string="{vname}">\n'
                f'  <sheet>\n'
                f'    <group>{field_tags}\n    </group>\n'
                f'  </sheet>\n'
                f'</form>'
            )
        else:
            f0 = field_names[0]
            rest = ''.join(f'\n      <field name="{f}"/>' for f in field_names[1:])
            arch_db = (
                f'<kanban>\n  <templates>\n    <t t-name="card">\n'
                f'      <field name="{f0}"/>{rest}\n'
                f'    </t>\n  </templates>\n</kanban>'
            )

        base = model_name.replace('.', '_')
        suffix = vname.lower().replace(' ', '_').replace('.', '_')
        view_key = f'ai_studio.{base}_{view_type}_{suffix}'

        try:
            view = self.env['ir.ui.view'].sudo().create({
                'name': vname,
                'model': model_name,
                'type': view_type,
                'arch_db': arch_db,
                'key': view_key,
                'priority': 99,
            })
        except Exception as e:
            _logger.exception("AI Studio: create_view failed")
            return None, None, f"Error creating view: {e}"

        action = None
        if create_action:
            try:
                action = self.env['ir.actions.act_window'].sudo().create({
                    'name': action_name or vname,
                    'res_model': model_name,
                    'view_mode': view_type,
                })
            except Exception as e:
                _logger.warning("AI Studio: could not create action: %s", e)

        _logger.info("AI Studio: created %s view for %s", view_type, model_name)
        return view, action, None

    # ══════════════════════════════════════════════
    # Delete / Rollback helpers
    # ══════════════════════════════════════════════

    def delete_custom_field(self, model_name, field_name):
        """
        Delete a custom (state=manual, x_ prefix) field.
        Refuses system fields. Returns (success, message).
        """
        if not field_name.startswith('x_'):
            return False, (
                f"Refused: '{field_name}' does not start with 'x_'. "
                f"Only custom x_ fields can be deleted."
            )

        field = self.env['ir.model.fields'].sudo().search([
            ('model', '=', model_name),
            ('name', '=', field_name),
        ], limit=1)

        if not field:
            return False, f"Field '{field_name}' not found on model '{model_name}'."

        if field.state != 'manual':
            return False, (
                f"Refused: '{field_name}' is a system field (state={field.state}). "
                f"Only manually created fields can be deleted."
            )

        label = field.field_description
        ttype = field.ttype
        try:
            field.sudo().unlink()
            _logger.info("AI Studio: deleted field %s.%s", model_name, field_name)
            return True, (
                    "Field deleted.\n"
                    "  Model : " + model_name + "\n"
                                                "  Field : " + field_name + "\n"
                                                                            "  Label : " + label + "\n"
                                                                                                   "  Type  : " + ttype + "\n"
                                                                                                                          "Note: views referencing this field may show errors. "
                                                                                                                          "Use Rollback View to clean them up."
            )
        except Exception as e:
            _logger.exception("AI Studio: delete_custom_field failed")
            return False, f"Error deleting field: {e}"

    def rollback_view(self, view_id):
        """
        Delete an AI Studio view (key must start with 'ai_studio.').
        Returns (success, message).
        """
        view = self.env['ir.ui.view'].sudo().browse(int(view_id))

        if not view.exists():
            return False, f"View ID {view_id} not found."

        if not view.key or not view.key.startswith('ai_studio.'):
            return False, (
                    "Refused: view '" + view.name + "' (key=" + (view.key or 'none') + ") "
                                                                                       "was not created by AI Studio. "
                                                                                       "Only views with key starting with 'ai_studio.' can be deleted here."
            )

        view_name = view.name
        view_key = view.key
        inherit_name = view.inherit_id.name if view.inherit_id else 'none'

        try:
            view.sudo().unlink()
            _logger.info("AI Studio: rolled back view %s", view_key)
            return True, (
                    "View rolled back successfully.\n"
                    "  Name     : " + view_name + "\n"
                                                  "  Key      : " + view_key + "\n"
                                                                               "  Extended : " + inherit_name + "\n"
                                                                                                                "Refresh your browser to see the revert."
            )
        except Exception as e:
            _logger.exception("AI Studio: rollback_view failed")
            return False, f"Error deleting view: {e}"

    def list_ai_studio_views(self, model_name=''):
        """List all AI Studio views for review before rollback."""
        domain = [('key', 'like', 'ai_studio.')]
        if model_name:
            domain.append(('model', '=', model_name))

        views = self.env['ir.ui.view'].sudo().search(domain, order='model asc')
        if not views:
            suffix = f" for model '{model_name}'" if model_name else ""
            return "No AI Studio views found" + suffix + "."

        lines = ["AI Studio views (can be rolled back):"]
        for v in views:
            inherit = (" extends: " + v.inherit_id.name) if v.inherit_id else ""
            active_flag = "" if v.active else " [INACTIVE]"
            lines.append(
                "  ID:" + str(v.id).ljust(6) +
                " [" + v.type.ljust(8) + "] " +
                v.model.ljust(30) + " " + v.name + inherit + active_flag
            )
        lines.append("Total: " + str(len(views)) + " view(s).")
        return "\n".join(lines)

    # ══════════════════════════════════════════════
    # Menu creation helper
    # ══════════════════════════════════════════════

    def create_menu(self, menu_name, parent_menu_name, model_name,
                    view_mode='list,form', domain=''):
        """
        Create a menu item + act_window for a model.
        Returns (menu_record, action_record, error_or_None).
        """
        if not self.get_model_id(model_name):
            return None, None, f"Model '{model_name}' not found."

        # Find parent menu
        parent = None
        if parent_menu_name:
            parent = self.env['ir.ui.menu'].sudo().search(
                [('name', 'ilike', parent_menu_name)], limit=1
            )
            if not parent:
                parent = self.env['ir.ui.menu'].sudo().search(
                    [('complete_name', 'ilike', parent_menu_name)], limit=1
                )
            if not parent:
                return None, None, (
                        "Parent menu '" + parent_menu_name + "' not found. "
                                                             "Try names like 'Sales', 'Contacts', 'Inventory', 'Settings'."
                )

        # Create act_window
        action_vals = {
            'name': menu_name,
            'res_model': model_name,
            'view_mode': view_mode or 'list,form',
            'type': 'ir.actions.act_window',
        }
        if domain:
            action_vals['domain'] = domain

        try:
            with self.env.cr.savepoint():
                action = self.env['ir.actions.act_window'].sudo().create(action_vals)
        except Exception as e:
            return None, None, f"Error creating action: {e}"

        # Create menu
        menu_vals = {
            'name': menu_name,
            'action': 'ir.actions.act_window,' + str(action.id),
        }
        if parent:
            menu_vals['parent_id'] = parent.id

        try:
            with self.env.cr.savepoint():
                menu = self.env['ir.ui.menu'].sudo().create(menu_vals)
            _logger.info("AI Studio: created menu '%s' ID:%s", menu_name, menu.id)
            return menu, action, None
        except Exception as e:
            try:
                action.sudo().unlink()
            except Exception:
                pass
            return None, None, f"Error creating menu: {e}"

    def delete_menu(self, menu_id):
        """Delete an AI Studio menu and its action. Returns (success, message)."""
        menu = self.env['ir.ui.menu'].sudo().browse(int(menu_id))
        if not menu.exists():
            return False, f"Menu ID {menu_id} not found."

        menu_name = menu.name
        action = menu.action
        try:
            menu.sudo().unlink()
            if action:
                try:
                    action.sudo().unlink()
                except Exception:
                    pass
            _logger.info("AI Studio: deleted menu '%s' ID:%s", menu_name, menu_id)
            return True, (
                    "Menu deleted.\n"
                    "  Name: " + menu_name + "\n"
                                             "  ID  : " + str(menu_id) + "\n"
                                                                         "Refresh your browser to see the change."
            )
        except Exception as e:
            return False, f"Error deleting menu: {e}"
