# -*- coding: utf-8 -*-
"""Toolset of the single-loop coding agent.

Produces the platform's tool-tuple format —
    {name: (description, allow_end_message, callable, json_schema)}
— where callable(arguments=<dict>) returns (result_str, error_or_None).

Every tool is available in every turn (the model decides what it needs);
the only turn-pausing tool is ask_user_question, which raises StopTurn so
the runner can post the question card and resume when the answer arrives.

Safety is enforced in the workspace service (path jail, extension
whitelist, pre-write validators, git checkpoints) plus, here, the
observation gate: a file may only be edited or overwritten after it was
read in this turn and is unchanged on disk since.
"""
import json
import pathlib

from ..utils import coderun, shell_guard
from ..utils.coding_llm_service import StopTurn

_S_STR = {"type": "string"}
_S_INT = {"type": "integer"}
_S_BOOL = {"type": "boolean"}

# chip labels are NOT translated on purpose: the reply must equal the label byte-for-byte
SHELL_ALLOW_LABEL = "▶️ Run / 执行"
SHELL_DENY_LABEL = "⛔ Deny / 不执行"

READONLY_TOOLS = {
    'read_file', 'grep', 'glob', 'tree', 'outline', 'run_code', 'repo_map', 'find_symbol',
    'code_graph_model_map', 'code_graph_blast_radius', 'read_module_source',
    'grep_module_source', 'list_projects', 'read_project_file', 'grep_project', 'odoo_docs',
    'mem_search', 'mem_get', 'get_full_observation', 'list_attachments', 'read_attachment',
    'git_status', 'git_log', 'git_diff',
}
WRITE_TOOLS = {'write_file', 'edit_file', 'multi_edit', 'scaffold_module', 'sandbox_test'}
MAX_QUESTIONS = 4
MAX_OPTIONS = 6


def _schema(props, required):
    return {"type": "object", "properties": props, "required": required,
            "additionalProperties": False}


def _s(value, default=''):
    return default if value is None else str(value)


def _i(value, default):
    try:
        v = int(value)
        return v if v > 0 else default
    except (TypeError, ValueError):
        return default


class TurnContext:
    """Per-turn mutable state shared by the tool closures."""

    def __init__(self):
        self.observed = {}        # rel path -> (mtime_ns, size)
        self.todos = None         # last todo_write payload, or None
        self.changed_files = []   # rel paths written this turn
        self.abort_check = None   # fn() -> bool


def build_toolset(runner, task, ctx):
    env = runner.env
    ws = env['ai.code.workspace'].with_context(ai_code_task_id=task.id)
    graph = env['ai.code.node'].sudo()
    snippet = env['ai.code.snippet'].sudo().with_context(ai_code_task_id=task.id)
    memory = env['ai.code.memory'].sudo()
    events = env['ai.code.event'].sudo().with_context(ai_code_task_id=task.id)
    files = env['ai.code.files']
    wd = task.target_dir

    def ok(fn):
        def run(arguments):
            try:
                result = str(fn(arguments))
            except StopTurn:
                raise
            except Exception as e:  # noqa: BLE001
                return f"ERROR: {type(e).__name__}: {e}", e
            is_err = result.startswith(("ERROR", "REJECTED"))
            return result, (True if is_err else None)
        return run

    # ── observation gate helpers ───────────────────────────────────────
    def observe(path):
        stamp = ws.file_stamp(wd, path)
        if stamp:
            ctx.observed[stamp[0]] = stamp[1]

    def gate(path, label):
        stamp = ws.file_stamp(wd, path)
        if not stamp:
            return None            # new file (or bad path: the tool reports it)
        rel, current = stamp
        rec = ctx.observed.get(rel)
        if rec is None:
            return (f"REJECTED (nothing written): {label} requires reading first — you have "
                    f"not read {rel} in this conversation turn. read_file it, then retry.")
        if rec != current:
            ctx.observed.pop(rel, None)
            return (f"REJECTED (nothing written): {rel} changed on disk after you read it "
                    "(the user or another process edited it). read_file it again, then retry.")
        return None

    def after_write(path, result):
        if result.startswith("OK"):
            observe(path)
            rel = str(path).replace('\\', '/')
            if rel not in ctx.changed_files:
                ctx.changed_files.append(rel)
            first = rel.split('/')[0]
            if first and (pathlib.Path(wd) / first / '__manifest__.py').is_file() \
                    and task.module_name != first:
                task.sudo().write({'module_name': first})
        return result

    tools = {}

    # ── read / locate ──────────────────────────────────────────────────
    def read_file(a):
        out = ws.tool_read_file(wd, a['path'], _i(a.get('start_line'), 1),
                                _i(a.get('num_lines'), 200))
        if not out.startswith("ERROR"):
            observe(a['path'])
        return out
    tools['read_file'] = (
        "Read a line-numbered window of a workspace file (max 400 lines per call; the "
        "header tells you how to continue). Reading a file is required before editing it.",
        False, ok(read_file),
        _schema({"path": _S_STR, "start_line": _S_INT, "num_lines": _S_INT}, ["path"]))

    tools['grep'] = (
        "Regex search across workspace text files. glob narrows the files (e.g. "
        "'**/*.py', 'my_module/**/*.xml'); context_lines adds surrounding lines.",
        False,
        ok(lambda a: ws.tool_grep(wd, a['pattern'], _s(a.get('glob')) or '**/*',
                                  _i(a.get('max_results'), 60), _i(a.get('context_lines'), 0)
                                  if a.get('context_lines') else 0)),
        _schema({"pattern": _S_STR, "glob": _S_STR, "max_results": _S_INT,
                 "context_lines": _S_INT}, ["pattern"]))
    tools['glob'] = (
        "List workspace files matching a glob pattern (relative paths).",
        False, ok(lambda a: ws.tool_glob(wd, _s(a.get('pattern')) or '**/*', _i(a.get('limit'), 200))),
        _schema({"pattern": _S_STR, "limit": _S_INT}, ["pattern"]))
    tools['tree'] = (
        "Compact directory tree of the workspace or a sub-directory.",
        False, ok(lambda a: ws.tool_tree(wd, _s(a.get('subdir')), _i(a.get('depth'), 3))),
        _schema({"subdir": _S_STR, "depth": _S_INT}, []))
    tools['outline'] = (
        "Symbols of one file with line numbers: Python classes/methods/fields (with "
        "_name/_inherit), XML record ids/models/views. Use before read_file to pick "
        "the exact window.",
        False, ok(lambda a: ws.tool_outline(wd, a['path'])),
        _schema({"path": _S_STR}, ["path"]))

    ro_impl = {
        'read_file': lambda path, start_line=1, num_lines=200: (
            observe(path) or ws.tool_read_file(wd, path, start_line, num_lines)),
        'grep': lambda pattern, glob='**/*', max_results=60, context_lines=0: ws.tool_grep(
            wd, pattern, glob, max_results, context_lines),
        'glob': lambda pattern='**/*', limit=200: ws.tool_glob(wd, pattern, limit),
        'tree': lambda subdir='', depth=3: ws.tool_tree(wd, subdir, depth),
        'outline': lambda path: ws.tool_outline(wd, path),
    }
    tools['run_code'] = (
        "Run ONE read-only Python program that chains several explorations in a single "
        "round. Available as plain functions returning strings: tools.read_file(path, "
        "start_line=1, num_lines=200), tools.grep(pattern, glob='**/*', max_results=60, "
        "context_lines=0), tools.glob(pattern), tools.tree(subdir='', depth=3), "
        "tools.outline(path). Also re, json, math, collections, itertools, difflib, fnmatch. "
        "Only print() output and a `result` variable come back (12k chars). No writes.",
        False,
        ok(lambda a: coderun.run_program(a.get('code', ''), ro_impl,
                                         abort_check=ctx.abort_check)),
        _schema({"code": _S_STR}, ["code"]))

    # ── code graph / Odoo source ───────────────────────────────────────
    tools['repo_map'] = (
        "High-signal map of a module (or the whole indexed graph): most-connected "
        "models/views/menus/actions.",
        False, ok(lambda a: _repo_map(graph, _s(a.get('module_name')), _i(a.get('limit'), 40))),
        _schema({"module_name": _S_STR, "limit": _S_INT}, []))
    tools['find_symbol'] = (
        "Search the code graph for a model/field/method/view by name; returns file:line hits.",
        False, ok(lambda a: _find_symbol(graph, a.get('term', ''), _i(a.get('limit'), 8))),
        _schema({"term": _S_STR, "limit": _S_INT}, ["term"]))
    tools['code_graph_model_map'] = (
        "Structural map of an Odoo model: definer, MRO/extenders, fields, views, methods.",
        False, ok(lambda a: graph.ai_tool_model_map(model_name=a['model_name'])),
        _schema({"model_name": _S_STR}, ["model_name"]))
    tools['code_graph_blast_radius'] = (
        "Impact of changing a model field: views, computes, relations that depend on it.",
        False, ok(lambda a: graph.ai_tool_blast_radius(model_name=a['model_name'],
                                                       field_name=a['field_name'])),
        _schema({"model_name": _S_STR, "field_name": _S_STR}, ["model_name", "field_name"]))
    tools['read_module_source'] = (
        "Read the ACTUAL source of any installed module (Odoo core like 'sale', 'stock', "
        "'mail', 'base', enterprise, or another addon). path='' lists its files. "
        "Read-only. The framework itself (odoo/orm, fields.py) is not a module — for ORM "
        "internals rely on odoo_docs and the idioms, do not go looking for them.",
        False,
        ok(lambda a: ws.tool_read_module_source(a['module_name'], _s(a.get('path')),
                                                _i(a.get('start_line'), 1),
                                                _i(a.get('num_lines'), 200))),
        _schema({"module_name": _S_STR, "path": _S_STR, "start_line": _S_INT,
                 "num_lines": _S_INT}, ["module_name"]))
    tools['grep_module_source'] = (
        "Regex-search one installed module's source (read-only).",
        False,
        ok(lambda a: ws.tool_grep_module_source(a['module_name'], a['pattern'],
                                                _s(a.get('glob')) or '**/*',
                                                _i(a.get('max_results'), 40))),
        _schema({"module_name": _S_STR, "pattern": _S_STR, "glob": _S_STR,
                 "max_results": _S_INT}, ["module_name", "pattern"]))
    tools['list_projects'] = (
        "List the OTHER project folders you may READ for reference (each with its Odoo "
        "version and modules). Writes stay in this workspace.",
        False, ok(lambda a: ws.list_projects()), _schema({}, []))
    tools['read_project_file'] = (
        "Read a file of ANOTHER project folder from list_projects (read-only). A different "
        "Odoo version gets a translation banner.",
        False,
        ok(lambda a: _read_project_file(ws, task, a['project_dir'], a['path'],
                                        _i(a.get('start_line'), 1), _i(a.get('num_lines'), 200))),
        _schema({"project_dir": _S_STR, "path": _S_STR, "start_line": _S_INT,
                 "num_lines": _S_INT}, ["project_dir", "path"]))
    tools['grep_project'] = (
        "Regex-search ANOTHER project folder path from list_projects (read-only).",
        False,
        ok(lambda a: ws.tool_grep(a['project_dir'], a['pattern'], _s(a.get('glob')) or '**/*',
                                  _i(a.get('max_results'), 40))),
        _schema({"project_dir": _S_STR, "pattern": _S_STR, "glob": _S_STR,
                 "max_results": _S_INT}, ["project_dir", "pattern"]))

    # ── knowledge ──────────────────────────────────────────────────────
    tools['odoo_docs'] = (
        "Version-accurate Odoo idiom snippets + lessons learned for an artifact type "
        "(manifest|model|view|security|data|menu|controller|wizard|report|js|test). Call "
        "before writing each kind of file, and when a sandbox error looks familiar.",
        False,
        ok(lambda a: snippet.tool_odoo_docs(_s(a.get('query')), _s(a.get('artifact_type')),
                                            _i(a.get('budget_tokens'), 2000))),
        _schema({"query": _S_STR, "artifact_type": _S_STR, "budget_tokens": _S_INT}, ["query"]))
    tools['mem_search'] = (
        "Search cross-task experience memory of this workspace; returns index lines.",
        False, ok(lambda a: memory.tool_mem_search(_s(a.get('query')), _i(a.get('limit'), 8))),
        _schema({"query": _S_STR, "limit": _S_INT}, ["query"]))
    tools['mem_get'] = (
        "Fetch the full text of memory ids from mem_search.",
        False, ok(lambda a: memory.tool_mem_get(a.get('memory_ids') or [])),
        _schema({"memory_ids": {"type": "array", "items": _S_INT}}, ["memory_ids"]))
    tools['get_full_observation'] = (
        "Retrieve the full text of a condensed tool output by its event id.",
        False, ok(lambda a: events.tool_get_full_observation(a['event_id'])),
        _schema({"event_id": _S_INT}, ["event_id"]))
    tools['list_attachments'] = (
        "Documents the user uploaded to this conversation (quotation, invoice, spec...). "
        "Check them when a requirement mentions a document.",
        False, ok(lambda a: files.tool_list_attachments(task.channel_id)), _schema({}, []))
    tools['read_attachment'] = (
        "Extract the text of one uploaded document (pdf text layer / xlsx / csv / docx); "
        "part= selects the page/sheet window (0-based).",
        False,
        ok(lambda a: files.tool_read_attachment(task.channel_id, a['attachment_id'],
                                                _i(a.get('part'), 0) if a.get('part') else 0)),
        _schema({"attachment_id": _S_INT, "part": _S_INT}, ["attachment_id"]))

    # ── git (read-only) ────────────────────────────────────────────────
    tools['git_status'] = ("Current branch + uncommitted files of the workspace.",
                           False, ok(lambda a: ws.tool_git_status(wd)), _schema({}, []))
    tools['git_log'] = ("Recent commits of the workspace.",
                        False, ok(lambda a: ws.tool_git_log(wd, _i(a.get('limit'), 20))),
                        _schema({"limit": _S_INT}, []))
    tools['git_diff'] = ("Diff stat of this session's branch vs its base (or an explicit ref).",
                         False, ok(lambda a: ws.tool_git_diff(wd, _s(a.get('base')))),
                         _schema({"base": _S_STR}, []))

    # ── write ──────────────────────────────────────────────────────────
    def write_file(a):
        refused = gate(a['path'], "write_file over an existing file")
        if refused:
            return refused
        return after_write(a['path'], ws.tool_write_file(wd, a['path'], a.get('content', '')))
    tools['write_file'] = (
        "Create a file or overwrite an existing one (existing files must have been read in "
        "this turn). Content is validated (syntax + Odoo version rules) before saving; a "
        "REJECTED result means nothing was written — fix and retry. Auto-committed.",
        False, ok(write_file), _schema({"path": _S_STR, "content": _S_STR}, ["path", "content"]))

    def edit_file(a):
        refused = gate(a['path'], "edit_file")
        if refused:
            return refused
        return after_write(a['path'], ws.tool_edit_file(
            wd, a['path'], a.get('old_string', ''), a.get('new_string', ''),
            bool(a.get('replace_all'))))
    tools['edit_file'] = (
        "Exact-string replacement in an existing file (old_string must match byte-for-byte "
        "and be unique unless replace_all). Validated before saving; returns the diff hunk.",
        False, ok(edit_file),
        _schema({"path": _S_STR, "old_string": _S_STR, "new_string": _S_STR,
                 "replace_all": _S_BOOL}, ["path", "old_string", "new_string"]))

    def multi_edit(a):
        refused = gate(a['path'], "multi_edit")
        if refused:
            return refused
        edits = a.get('edits')
        if isinstance(edits, str):
            try:
                edits = json.loads(edits)
            except ValueError:
                return "ERROR: edits must be a JSON array of {old_string, new_string}."
        return after_write(a['path'], ws.tool_multi_edit(wd, a['path'], edits or []))
    tools['multi_edit'] = (
        "Several ordered exact-string replacements in ONE file, all-or-nothing, validated "
        "once on the final content, one commit. edits: [{old_string, new_string}].",
        False, ok(multi_edit),
        _schema({"path": _S_STR,
                 "edits": {"type": "array", "items": {
                     "type": "object",
                     "properties": {"old_string": _S_STR, "new_string": _S_STR},
                     "required": ["old_string", "new_string"],
                     "additionalProperties": False}}},
                ["path", "edits"]))

    def scaffold(a):
        out = ws.tool_scaffold_module(wd, a['module_name'], _s(a.get('module_title')))
        if out.startswith("OK"):
            for rel in out.splitlines()[1:]:
                after_write(rel.strip(), "OK")
        return out
    tools['scaffold_module'] = (
        "Create a minimal valid NEW module skeleton (manifest + __init__ + models/) in one "
        "commit. Rejects names that collide with an existing Odoo module.",
        False, ok(scaffold),
        _schema({"module_name": _S_STR, "module_title": _S_STR}, ["module_name"]))

    # ── verify ─────────────────────────────────────────────────────────
    tools['sandbox_test'] = (
        "Verify a module for real: static preflight -> install in an isolated throwaway "
        "database -> runtime smoke (views, models, data methods); run_tests=true also runs "
        "the module's Python tests. Returns the failing stage with real error lines and the "
        "offending file windows, and says which levels were verified vs skipped.",
        False,
        ok(lambda a: ws.sandbox_verify(task, _s(a.get('module')) or task.module_name,
                                       bool(a.get('run_tests')))),
        _schema({"module": _S_STR, "run_tests": _S_BOOL}, ["module"]))

    # ── shell (policy + approval card) ─────────────────────────────────
    if ws.shell_enabled():
        def shell(a):
            command = str(a.get('command') or '').strip()
            verdict, reason = shell_guard.classify(command)
            if verdict == 'reject':
                return f"REJECTED: {reason}. This command will never run — find another way."
            if verdict == 'confirm' and not ws.shell_auto_approve():
                raise StopTurn('confirm_shell', {
                    'command': command, 'reason': reason,
                    'why': str(a.get('why') or '').strip()[:300]})
            return ws.tool_shell_run(wd, command)
        tools['shell'] = (
            "Run ONE shell command inside the workspace root (Windows cmd / POSIX sh syntax "
            "of the server). Read-only commands (ls/dir/cat/grep/find/git status|log|diff, "
            "python -m py_compile, pytest --collect-only, pip list...) run at once. Anything "
            "state-changing (pip install, npm, running scripts, deleting, chaining with && | ;) "
            "PAUSES the turn on an approval card — explain in `why`. git mutations (add/commit/"
            "checkout/push...) are always refused: your writes are checkpointed automatically. "
            "One command per call; never busy-loop.",
            False, ok(shell),
            _schema({"command": _S_STR,
                     "why": {"type": "string", "description": "one line: why this command is needed"}},
                    ["command"]))

    # ── task management ────────────────────────────────────────────────
    def todo_write(a):
        raw = a.get('todos')
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except ValueError:
                return "ERROR: todos must be a JSON array of {content, status}."
        clean = []
        for t in raw or []:
            if not isinstance(t, dict):
                continue
            content = str(t.get('content') or '').strip()
            if not content:
                continue
            status = t.get('status') if t.get('status') in ('pending', 'in_progress', 'completed') \
                else 'pending'
            clean.append({'content': content[:200], 'status': status})
        if not clean:
            return "ERROR: todos is empty."
        ctx.todos = clean
        runner._set_todos(task, clean)
        done = sum(1 for t in clean if t['status'] == 'completed')
        return f"Todo list updated ({done}/{len(clean)} completed)."
    tools['todo_write'] = (
        "Replace the task's todo list (the user sees it live). Call FIRST for any multi-step "
        "task, then update statuses as you go: exactly one in_progress at a time.",
        False, ok(todo_write),
        _schema({"todos": {"type": "array", "items": {
            "type": "object",
            "properties": {"content": _S_STR,
                           "status": {"type": "string",
                                      "description": "pending|in_progress|completed"}},
            "required": ["content", "status"], "additionalProperties": False}}},
                ["todos"]))

    def ask_user(a):
        questions, err = _normalize_questions(a.get('questions'))
        if err:
            return f"REJECTED: {err}"
        raise StopTurn('ask_user', {'questions': questions})
    tools['ask_user_question'] = (
        "Ask the user up to 4 structured questions, each with 2-6 concrete options "
        "(put your recommended option first, labelled '(recommended)'). The turn pauses "
        "until the user answers; the answers come back as this tool's result. Use only for "
        "decisions that belong to the user — never to confirm a clear task.",
        False, ok(ask_user),
        _schema({"questions": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "question": _S_STR,
                "header": {"type": "string", "description": "short label, <= 12 chars"},
                "options": {"type": "array", "items": _S_STR},
                "multi_select": _S_BOOL},
            "required": ["question", "options"], "additionalProperties": False}}},
                ["questions"]))

    return tools


# ── helpers ────────────────────────────────────────────────────────────
def _normalize_questions(raw):
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return [], "questions must be a JSON array"
    if not isinstance(raw, list) or not raw:
        return [], "questions must be a non-empty array"
    if len(raw) > MAX_QUESTIONS:
        return [], f"at most {MAX_QUESTIONS} questions per call — batch related ones"
    out = []
    for i, q in enumerate(raw, 1):
        if not isinstance(q, dict):
            return [], f"question {i} is not an object"
        text = str(q.get('question') or '').strip()
        if not text:
            return [], f"question {i} has no text"
        opts = q.get('options')
        if isinstance(opts, str):
            opts = [o.strip() for o in opts.split('|') if o.strip()]
        if not isinstance(opts, list) or len(opts) < 2:
            return [], (f"question '{text[:30]}' needs at least 2 options — this tool exists "
                        "so the user can click instead of type; open questions go in prose")
        labels = []
        for op in opts[:MAX_OPTIONS]:
            label = str(op.get('label') if isinstance(op, dict) else op or '').strip()
            if label and label not in labels:
                labels.append(label[:80])
        if len(labels) < 2:
            return [], f"question '{text[:30]}' has fewer than 2 distinct option labels"
        out.append({'question': text[:300], 'header': str(q.get('header') or '').strip()[:24],
                    'options': labels, 'multi_select': bool(q.get('multi_select'))})
    return out, ''


def _looks_like_module_name(s):
    import re as _re
    return bool(_re.fullmatch(r'[a-z][a-z0-9_]*', (s or '').strip()))


def _read_project_file(ws, task, project_dir, path, start_line, num_lines):
    if _looks_like_module_name(project_dir):
        return (f"project_dir must be a folder PATH from list_projects, not a bare module "
                f"name. To read the installed module {project_dir!r} use read_module_source.")
    body = ws.tool_read_file(project_dir, path, start_line, num_lines)
    if isinstance(body, str) and body.startswith("ERROR"):
        return body
    target = task.odoo_version or ws._server_odoo_series()
    ref_version = ws.detect_odoo_version(project_dir)
    if ref_version != target:
        banner = (
            f"⚠️ REFERENCE ONLY — this file is Odoo {ref_version}, but you are writing "
            f"Odoo {target}. Reuse its LOGIC and structure, but TRANSLATE the idioms to "
            f"{target} (e.g. <tree>→<list>, attrs=→direct expressions, "
            f"_sql_constraints→models.Constraint, oe_chatter div→<chatter/>).\n{'-' * 60}\n")
        return banner + body
    return body


def _find_symbol(graph, term, limit):
    try:
        hits = graph.search_nodes(term, limit=min(int(limit or 8), 20)) or []
    except Exception as e:  # noqa: BLE001
        return f"Code graph unavailable ({type(e).__name__}). Use grep/read_module_source instead."
    if not hits:
        return (f"No symbol matching {term!r} in the code graph (it may be empty/stale for this "
                f"repo). Use grep or read_module_source instead.")
    by_id = {n.id: n for n in graph.browse([h['id'] for h in hits]).exists()}
    lines = []
    for h in hits:
        node = by_id.get(h['id'])
        loc = f" {node.file_path}:{node.line_no}" if node and node.file_path else ""
        lines.append(f"{h.get('key')} [{h.get('type')}] module={h.get('module')}{loc}")
    return "\n".join(lines)


def _repo_map(graph, module_name, limit):
    domain = [('is_skeleton', '=', False)]
    if module_name:
        domain.append(('module_name', '=', module_name))
    nodes = graph.search(domain + [('node_type', 'in', ('model', 'view', 'menu', 'action'))],
                         order='degree desc', limit=min(int(limit or 40), 120))
    if not nodes:
        return (f"No indexed nodes for module {module_name!r}." if module_name
                else "The code graph is empty — rebuild it from AI Studio > Code Graph.")
    lines = [f"{n.technical_key} [{n.node_type}] deg={n.degree}"
             + (f" {n.file_path}" if n.file_path else "") for n in nodes]
    return f"repo map — {module_name or 'all modules'} (top {len(nodes)} by connectivity):\n" \
        + "\n".join(lines)
