import inspect
import json
import logging
import traceback

from odoo import models, _
from odoo.exceptions import AccessError, UserError

from ..utils.tools_schema.validators import validate_schema

_logger = logging.getLogger(__name__)


class _SkillTestRollback(Exception):
    """Internal sentinel used to force a rollback after a successful test run."""


# Models the generated skill code must never touch: modifying them changes the
# system itself (views, security, automations, users) instead of business data.
PROTECTED_MODELS = [
    'ir.model',
    'ir.model.fields',
    'ir.model.access',
    'ir.ui.view',
    'ir.ui.menu',
    'ir.rule',
    'ir.config_parameter',
    'ir.actions.server',
    'ir.actions.act_window',
    'ir.actions.actions',
    'ir.cron',
    'ir.module.module',
    'res.groups',
    'res.users',
    'ai.studio',
    'ai.skill.builder',
]

FORBIDDEN_CODE_PATTERNS = [
    ('sudo(', "sudo() is forbidden: skill code must run with the current user's access rights."),
    ('.sudo', "sudo() is forbidden: skill code must run with the current user's access rights."),
]


class AiSkillBuilder(models.Model):
    """
    Helper model for the Skill Builder agent tools.
    _auto = False: no database table. Model exists purely for method dispatch,
    same pattern as ai.studio. All methods are called from ir.actions.server
    code (safe_eval), so they must be public and return LLM-friendly strings.
    """
    _name = 'ai.skill.builder'
    _description = 'AI Skill Builder Helpers'
    _auto = False
    _log_access = False

    # ══════════════════════════════════════════════
    # Guards
    # ══════════════════════════════════════════════

    def _check_builder_access(self):
        # Hard gate: these tools create executable code, so they are reserved
        # to AI administrators no matter which user chats with the agent.
        if not self.env.user.has_group('ai_chatbot_enhance.group_ai_manager'):
            raise AccessError(_(
                "The Skill Builder is restricted to AI administrators."
            ))

    def _check_code_safety(self, code):
        """Reject code touching system models or escalating rights.

        Returns an error string (for the LLM) or None when the code is clean.
        """
        for pattern, message in FORBIDDEN_CODE_PATTERNS:
            if pattern in code:
                return f"REJECTED: {message}"
        for model_name in PROTECTED_MODELS:
            if f"'{model_name}'" in code or f'"{model_name}"' in code:
                return (
                    f"REJECTED: the code references the system model '{model_name}'. "
                    "Skills may only manipulate business data that users can edit in "
                    "the UI (partners, orders, leads, tasks, ...), never system "
                    "configuration models."
                )
        return None

    def _validate_schema_json(self, schema_json):
        """Return (schema_dict, None) or (None, error string for the LLM)."""
        try:
            schema = json.loads(schema_json)
        except json.decoder.JSONDecodeError as e:
            return None, f"REJECTED: schema_json is not valid JSON: {e}"
        if not isinstance(schema, dict) or schema.get('type') != 'object':
            return None, 'REJECTED: the schema top level must be {"type": "object", "properties": {...}, "required": [...]}'
        try:
            validate_schema(schema)
        except Exception as e:  # noqa: BLE001 - validator raises ValueError/TypeError
            return None, f"REJECTED: invalid schema: {e}"
        return schema, None

    def _get_editable_skill(self, skill_id):
        """Return the skill record if the builder is allowed to modify it.

        Refuses records defined in module data (built-in skills) and server
        actions that are not AI skills, so the agent can never break existing
        automations or system tools.
        """
        skill = self.env['ir.actions.server'].browse(int(skill_id)).exists()
        if not skill:
            raise UserError(_("No skill found with ID %s.", skill_id))
        if skill.get_external_id().get(skill.id):
            raise UserError(_(
                "Skill '%s' is a built-in skill defined in module data and cannot be "
                "modified. Create a new skill instead (you can use Get Skill Details "
                "on it as an example).", skill.name,
            ))
        if skill.state != 'code' or not (skill.use_in_ai or skill.ai_skill_draft):
            raise UserError(_(
                "Action '%s' is not an AI skill managed by the Skill Builder.", skill.name,
            ))
        return skill

    def _skill_status(self, skill):
        if skill.ai_skill_draft:
            return "draft"
        return "active" if skill.use_in_ai else "inactive"

    # ══════════════════════════════════════════════
    # Discovery helpers
    # ══════════════════════════════════════════════

    def list_model_methods(self, model_name, search_term=''):
        """List public (callable) methods of a model with signature and doc.

        Generic ORM methods inherited unchanged from BaseModel are hidden
        unless a search term is given, to keep the output focused on business
        methods (action_confirm, ...).
        """
        self._check_builder_access()
        if model_name not in self.env:
            return f"ERROR: Model '{model_name}' does not exist."

        Model = self.env[model_name]
        cls = type(Model)
        base_cls = models.BaseModel
        search_lower = (search_term or '').lower()

        lines = []
        for name in sorted(dir(cls)):
            if name.startswith('_') or name in Model._fields:
                continue
            if search_lower and search_lower not in name.lower():
                continue
            try:
                attr = getattr(cls, name, None)
            except Exception:  # noqa: BLE001 - some attrs raise on access
                continue
            if not callable(attr):
                continue
            # Skip generic ORM plumbing unless explicitly searched for
            if not search_lower and getattr(base_cls, name, None) is attr:
                continue
            try:
                sig = str(inspect.signature(attr))
            except (ValueError, TypeError):
                sig = '(...)'
            doc = (inspect.getdoc(attr) or '').strip().split('\n')[0][:100]
            lines.append(f"  {name}{sig}" + (f"  # {doc}" if doc else ""))

        if not lines:
            return f"No public methods found on {model_name} matching '{search_term}'."

        truncated = ""
        if len(lines) > 60:
            truncated = f"\n... ({len(lines) - 60} more, refine with search_term)"
            lines = lines[:60]
        return (
            f"Public methods of {model_name} (callable from skill code, e.g. record.method_name()):\n"
            + "\n".join(lines) + truncated
            + "\n\nNote: names only prove the method exists — verify behavior with a Test Run."
        )

    # ══════════════════════════════════════════════
    # Skill CRUD
    # ══════════════════════════════════════════════

    def list_skills(self, search_term=''):
        """List AI skills (drafts and active), flagging built-in ones."""
        self._check_builder_access()
        domain = ['|', ('use_in_ai', '=', True), ('ai_skill_draft', '=', True)]
        if search_term:
            domain += [('name', 'ilike', search_term)]
        skills = self.env['ir.actions.server'].search(domain, order='id desc', limit=80)
        if not skills:
            return f"No AI skills found matching '{search_term}'."
        xml_ids = skills.get_external_id()
        lines = [f"{'ID':<7}{'Status':<10}{'Tested':<8}{'Origin':<10}{'Model':<25}Name", "-" * 90]
        for s in skills:
            lines.append(
                f"{s.id:<7}"
                f"{self._skill_status(s):<10}"
                f"{('yes' if s.ai_tool_test_passed else 'no'):<8}"
                f"{('built-in' if xml_ids.get(s.id) else 'custom'):<10}"
                f"{(s.model_id.model or ''):<25}"
                f"{s.name}"
            )
        lines.append("\nBuilt-in skills are read-only (use Get Skill Details to read them as examples).")
        return "\n".join(lines)

    def get_skill(self, skill_id):
        """Full read-out of one skill: description, schema, code, status."""
        self._check_builder_access()
        skill = self.env['ir.actions.server'].browse(int(skill_id)).exists()
        if not skill:
            return f"ERROR: No skill found with ID {skill_id}."
        is_builtin = bool(skill.get_external_id().get(skill.id))
        return (
            f"Skill ID: {skill.id}\n"
            f"Name: {skill.name}\n"
            f"Model: {skill.model_id.model or '(none)'}\n"
            f"Status: {self._skill_status(skill)}"
            f"{' (built-in, read-only)' if is_builtin else ''}\n"
            f"Test passed: {'yes' if skill.ai_tool_test_passed else 'no'}\n"
            f"\n--- DESCRIPTION ---\n{skill.ai_tool_description or '(empty)'}\n"
            f"\n--- SCHEMA ---\n{skill.ai_tool_schema or '(empty)'}\n"
            f"\n--- PYTHON CODE ---\n{skill.code or '(empty)'}"
        )

    def create_skill_draft(self, name, model_name, description, schema_json, code):
        """Create a new skill as an inactive draft (never visible to agents)."""
        self._check_builder_access()

        if not (name or '').strip():
            return "REJECTED: a skill name is required."
        if model_name not in self.env:
            return f"ERROR: Model '{model_name}' does not exist. Use Get Models to find the technical name."
        ir_model = self.env['ir.model'].search([('model', '=', model_name)], limit=1)
        if not ir_model:
            return f"ERROR: Model '{model_name}' not found in ir.model."
        if not (description or '').strip():
            return "REJECTED: a description is required — it is what the AI reads to decide when to call the skill."
        if not (code or '').strip():
            return "REJECTED: python code is required."

        schema, error = self._validate_schema_json(schema_json)
        if error:
            return error
        if error := self._check_code_safety(code):
            return error

        skill = self.env['ir.actions.server'].create({
            'name': name,
            'state': 'code',
            'model_id': ir_model.id,
            'use_in_ai': False,
            'ai_skill_draft': True,
            'ai_tool_description': description,
            'ai_tool_schema': json.dumps(schema, indent=4, ensure_ascii=False),
            'code': code,
        })
        return (
            f"Draft skill created (INACTIVE until activated).\n"
            f"  ID: {skill.id}\n"
            f"  Name: {skill.name}\n"
            f"  Model: {model_name}\n"
            f"Next step: run Test Run Skill with realistic sample arguments."
        )

    def update_skill_draft(self, skill_id, name='', description='', schema_json='', code=''):
        """Update a custom skill. Empty string parameters keep the current value."""
        self._check_builder_access()
        skill = self._get_editable_skill(skill_id)

        vals = {}
        if (name or '').strip():
            vals['name'] = name
        if (description or '').strip():
            vals['ai_tool_description'] = description
        if (schema_json or '').strip():
            schema, error = self._validate_schema_json(schema_json)
            if error:
                return error
            vals['ai_tool_schema'] = json.dumps(schema, indent=4, ensure_ascii=False)
        if (code or '').strip():
            if error := self._check_code_safety(code):
                return error
            vals['code'] = code

        if not vals:
            return "Nothing to update: all parameters were empty."

        skill.write(vals)
        changed = ', '.join(vals.keys())
        note = ""
        if 'code' in vals or 'ai_tool_schema' in vals:
            note = "\nThe test flag was reset: run Test Run Skill again before activating."
        return f"Skill {skill.id} '{skill.name}' updated ({changed}).{note}"

    # ══════════════════════════════════════════════
    # Test & lifecycle
    # ══════════════════════════════════════════════

    def test_run_skill(self, skill_id, arguments_json):
        """Execute the skill in a savepoint and ALWAYS roll back.

        Database changes never persist; the result (or full error traceback)
        is returned so the agent can fix the code and retry.
        """
        self._check_builder_access()
        skill = self._get_editable_skill(skill_id)

        try:
            arguments = json.loads(arguments_json or '{}')
        except json.decoder.JSONDecodeError as e:
            return f"ERROR: arguments_json is not valid JSON: {e}"
        if not isinstance(arguments, dict):
            return "ERROR: arguments_json must be a JSON object matching the skill schema."

        result = None
        error_text = None
        try:
            with self.env.cr.savepoint():
                result = skill._ai_tool_run(None, dict(arguments))
                raise _SkillTestRollback()
        except _SkillTestRollback:
            pass
        except Exception:  # noqa: BLE001 - report any failure to the LLM
            tb = traceback.format_exc()
            error_text = tb[-1500:]

        passed = error_text is None
        skill.write({'ai_tool_test_passed': passed})

        if passed:
            if result is None:
                result = "(no result set — consider setting ai['result'])"
            return (
                "TEST PASSED. All database changes were rolled back.\n"
                f"Result returned to the LLM:\n{result}"
            )
        return (
            "TEST FAILED. All database changes were rolled back.\n"
            f"Traceback (tail):\n{error_text}\n"
            "Fix the code with Update Skill Draft and test again."
        )

    def activate_skill(self, skill_id):
        """Activate a draft. Hard gate: schema/code present and test passed."""
        self._check_builder_access()
        skill = self._get_editable_skill(skill_id)

        problems = []
        if not (skill.ai_tool_description or '').strip():
            problems.append("description is empty")
        if not (skill.code or '').strip():
            problems.append("python code is empty")
        if not skill.ai_tool_test_passed:
            problems.append("the last test run did not pass (or code changed since) — run Test Run Skill first")
        if problems:
            return "CANNOT ACTIVATE:\n" + "\n".join(f"  - {p}" for p in problems)

        skill.write({'use_in_ai': True, 'ai_skill_draft': False})
        return (
            f"Skill {skill.id} '{skill.name}' is now ACTIVE.\n"
            "To let an agent use it, attach it to a Topic (AI menu / Topics) — "
            "this is a deliberate manual step for the administrator."
        )

    def deactivate_skill(self, skill_id):
        """Put a custom skill back into draft (soft delete: nothing is removed)."""
        self._check_builder_access()
        skill = self._get_editable_skill(skill_id)
        skill.write({'use_in_ai': False, 'ai_skill_draft': True})
        return (
            f"Skill {skill.id} '{skill.name}' deactivated (back to draft). "
            "It is no longer available to any agent. Nothing was deleted."
        )
