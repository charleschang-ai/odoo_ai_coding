# -*- coding: utf-8 -*-
"""Odoo breaking-changes / pitfalls checklist, filtered by the target major
version and injected straight into the system prompt (ported from
ai-lucky-star odoo_kb.py). Each (since, until, text): None = unbounded.
Every line here crashed a real install or runtime at least once."""

_PITFALLS = [
    (19, None, "`_sql_constraints = [...]` is IGNORED -> "
               "`_x_uniq = models.Constraint('UNIQUE(a,b)', \"msg\")`."),
    (None, 18, "`_sql_constraints = [...]` still works (silently ignored from 19.0)."),
    (17, None, "`name_get()` removed -> override `_compute_display_name()`, read `.display_name`."),
    (None, None, "Search views: `<group expand=\"0\" string=\"...\">` is INVALID -> plain `<group>`."),
    (17, None, "`attrs=` / `states=` on view elements removed -> direct `invisible=\"...\"` expressions."),
    (18, None, "List views use `<list>`, not `<tree>`."),
    (None, None, "BaseModel has `__slots__`: `self._anything = x` on a recordset raises AttributeError."),
    (None, None, "`odoo.__file__` can be None -> anchor paths on `odoo.modules.module.__file__`."),
    (None, None, "`@api.multi` / `@api.one` do not exist."),
    (18, None, "Controllers: `type='jsonrpc'` (not `type='json'`)."),
    (None, None, "Never hand-write .po files; generate via base.language.export, then fill msgstr."),
    (18, None, "Chatter: model inherits `mail.thread` and the form view uses the `<chatter/>` tag "
               "(through 17.x it is a hand-written `<div class=\"oe_chatter\">`)."),
    (None, None, "Smart buttons: `type=\"object\"` + a Python method returning the action. "
                 "NEVER `type=\"action\"` with `active_id`/`id` in context -> "
                 "\"Access Rights Inconsistency\"."),
    (None, None, "A view referencing an action/record by xmlid must load AFTER it is defined — "
                 "order the manifest data list (security first, actions before views/menus)."),
    (None, None, "Every new model needs an ir.model.access.csv line, or nobody (not even admin "
                 "in some paths) can read it and the install logs a warning."),
    (None, None, "A method creating a log/history record for a user without create rights on that "
                 "model must sudo() that create (and only that call)."),
    (None, None, "A STORED computed field derived from a search() on ANOTHER model goes stale — "
                 "recompute it from that other model's write/create/unlink."),
    (17, None, "OWL client action: `static props = {}` fails in debug mode — spread "
               "`standardActionServiceProps`; the .xml t-name MUST equal `static template`."),
    (None, None, "Frontend data contract: a backend data method must return EVERY field the UI "
                 "reads/filters/groups by. Install-testing does not run JS — verify by rendering."),
    (None, None, "Dashboard/report aggregation methods: sudo() the read-only reads AND gate the "
                 "menu to the same group, else non-privileged users hit AccessError at onWillStart."),
    (19, None, "res.users groups field is `group_ids` (was `groups_id` through 18.x)."),
    (19, None, "res.groups: `category_id` became `privilege_id` (res.groups.privilege)."),
    (None, None, "On a virtual model (`_auto = False`) there is NO table: never `self.search_read()` "
                 "or filter by business fields on it -> `self.env['real.model'].sudo().search_read(...)`."),
    (None, None, "Kanban (18+): the card template is `<t t-name=\"card\">` (was `kanban-box`)."),
    (None, None, "`--dev=reload`/`--dev=all` servers restart on every .py write: warn the user."),
]


def major_of(version):
    try:
        return int(str(version or "").strip().split(".")[0])
    except (ValueError, IndexError):
        return None


def _in_range(since, until, major):
    if major is None:
        return True
    if since is not None and major < since:
        return False
    if until is not None and major > until:
        return False
    return True


def pitfalls_for(version):
    major = major_of(version)
    items = [t for since, until, t in _PITFALLS if _in_range(since, until, major)]
    return "\n".join(f"{i}. {t}" for i, t in enumerate(items, 1))
