# -*- coding: utf-8 -*-
"""Workspace service: every file / git primitive the coding agent uses.

Tableless helper (like ai.studio): all disk access goes through here so the
path jail, extension whitelist, size caps and write-gate validators are
enforced in exactly one place. AI tools (server actions) and UI buttons both
call these methods — nothing else in the module touches the filesystem.

SWE-agent lesson baked in: the write tools validate BEFORE saving (Python AST,
XML parse, Odoo 19 rule lint) and refuse broken content with the error text
returned to the LLM, so a bad edit never lands on disk.
"""
import ast
import difflib
import hashlib
import json
import logging
import os
import pathlib
import re
import subprocess
import sys
import time

import psycopg2
import psycopg2.sql

from odoo import models, _
from odoo.exceptions import UserError
from odoo.tools import config as odoo_config

_logger = logging.getLogger(__name__)

# File types the agent may create or edit inside a workspace
ALLOWED_EXTENSIONS = {'.py', '.xml', '.js', '.scss', '.css', '.csv', '.md', '.po', '.pot', '.txt'}
# File types grep/read may look at
TEXT_EXTENSIONS = ALLOWED_EXTENSIONS | {'.json', '.yml', '.yaml', '.rst', '.cfg', '.html'}
SKIP_DIRS = {'.git', '__pycache__', 'node_modules', '.idea', '.vscode', 'static/lib'}
MAX_FILE_BYTES = 512 * 1024          # hard cap for a single written file
MAX_READ_WINDOW = 400                # max lines per read call
MAX_GREP_RESULTS = 60
MAX_TOOL_TEXT = 6000                 # cap of any tool result fed to the LLM
GIT_TIMEOUT = 30                     # seconds per git command


def _odoo_core_root():
    """Root of the Odoo source tree (never writable). odoo.__file__ can be
    None (namespace package) — anchor on odoo.modules.module instead."""
    import odoo.modules.module
    return pathlib.Path(odoo.modules.module.__file__).resolve().parents[2]


def _clip(text, limit=MAX_TOOL_TEXT):
    text = text or ''
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n... [truncated, {len(text) - limit} more chars]"


def _tool(func):
    """Tool methods never raise at the LLM boundary: guardrail/jail violations
    come back as an ERROR string the model can react to."""
    import functools

    @functools.wraps(func)
    def wrapper(self, *args, **kwargs):
        try:
            return func(self, *args, **kwargs)
        except UserError as e:
            return f"ERROR: {e.args[0] if e.args else e}"
    return wrapper


class AICodeWorkspace(models.AbstractModel):
    _name = 'ai.code.workspace'
    _description = 'AI Code Workspace Service'

    # ══════════════════════════════════════════════════════════════════
    # Configuration & path jail
    # ══════════════════════════════════════════════════════════════════

    def _get_workspace_dirs(self):
        """Addons directories the coding agent may work in.

        Explicit whitelist (Settings > AI Chatbot > AI Code) wins. When it is
        empty we auto-detect: every addons-path entry OUTSIDE the Odoo source
        tree — i.e. the user's custom addons roots — so the folder picker is
        never empty and no manual configuration is needed to get started.
        """
        raw = self.env['ir.config_parameter'].sudo().get_param('ai_code.workspace_dirs', '')
        dirs, seen = [], set()
        for chunk in raw.replace('\n', ',').split(','):
            chunk = chunk.strip().strip('"').strip("'")
            if chunk:
                path = pathlib.Path(chunk)
                if path.is_dir() and path.resolve() not in seen:
                    path = path.resolve()
                    seen.add(path)
                    dirs.append(path)
        # UNION with auto-detected addons roots: adding a folder to the whitelist
        # ADDS to the auto-detected ones, it does not replace them.
        for path in self._auto_detect_addons_dirs():
            if path not in seen:
                seen.add(path)
                dirs.append(path)
        return dirs

    def _auto_detect_addons_dirs(self):
        """Custom addons roots from the running server's addons_path (those not
        inside the Odoo source tree). Zero-config default for the picker."""
        raw = odoo_config.get('addons_path') or ''
        entries = raw if isinstance(raw, (list, tuple)) else str(raw).split(',')
        core = _odoo_core_root()
        found, seen = [], set()
        for entry in entries:
            entry = str(entry).strip()
            if not entry:
                continue
            path = pathlib.Path(entry)
            if not path.is_dir():
                continue
            path = path.resolve()
            if path == core or core in path.parents or path in seen:
                continue
            seen.add(path)
            found.append(path)
        return found

    def _dir_is_git(self, path):
        return self._is_git_repo(pathlib.Path(path))

    @staticmethod
    def dir_writable(path):
        """Whether files can actually be created in `path` (a read-only bind
        mount passes os.access on some setups, so try a real write)."""
        try:
            probe = pathlib.Path(path) / '.ai_code_write_probe'
            with open(probe, 'w', encoding='utf-8') as f:
                f.write('ok')
            probe.unlink()
            return True
        except OSError:
            return False

    def count_modules(self, path):
        """Cheap count of Odoo modules directly under `path` (a __manifest__.py
        in a direct child dir). Filesystem-only, no parsing — for picker badges."""
        try:
            root = pathlib.Path(path)
            return sum(1 for c in root.iterdir()
                       if c.is_dir() and (c / '__manifest__.py').is_file())
        except OSError:
            return 0

    # ══════════════════════════════════════════════════════════════════
    # Server-side folder browser (the web-safe replacement for a native
    # "open folder": the SERVER lists its own reachable dirs, the browser
    # never touches the filesystem — works the same on Windows and Linux).
    # ══════════════════════════════════════════════════════════════════

    def _browse_roots(self):
        """Directories the folder browser may descend into: each addons root,
        its PARENT (so sibling project folders are reachable), and every
        whitelist entry. Bounds the browser to the project area — it cannot
        wander into system directories."""
        roots = set()
        for d in self._auto_detect_addons_dirs():
            roots.add(d)
            if d.parent != d:
                roots.add(d.parent)
        for d in self._get_workspace_dirs():
            roots.add(d)
        return roots

    def _in_browse_scope(self, path):
        path = pathlib.Path(path).resolve()
        return any(path == r or r in path.parents for r in self._browse_roots())

    def browse_dir(self, path=''):
        """List browsable sub-directories. Empty path → the browse roots
        (starting points). Otherwise → the sub-dirs of `path` (must be in
        scope), each with a module count + git flag, plus the parent if it is
        still in scope. `selectable` = whether `path` itself can be chosen."""
        if not path:
            roots = sorted(self._browse_roots(), key=lambda p: str(p).lower())
            return {
                'current': '', 'parent': '', 'selectable': False, 'current_is_git': False,
                'entries': [{
                    'path': str(r), 'name': r.name or str(r),
                    'module_count': self.count_modules(r), 'is_git': self._is_git_repo(r),
                } for r in roots],
            }
        current = pathlib.Path(path).resolve()
        if not self._in_browse_scope(current):
            raise UserError(_("Folder %s is outside the allowed area.", path))
        if not current.is_dir():
            raise UserError(_("%s is not a directory.", path))
        try:
            children = sorted(
                (c for c in current.iterdir() if c.is_dir() and not c.name.startswith('.')),
                key=lambda p: p.name.lower())
        except OSError as e:
            raise UserError(_("Cannot read %s: %s", current, e))
        parent = current.parent
        parent_ok = parent != current and self._in_browse_scope(parent)
        return {
            'current': str(current),
            'parent': str(parent) if parent_ok else '',
            'selectable': True,
            'current_is_git': self._is_git_repo(current),
            'entries': [{
                'path': str(c), 'name': c.name,
                'module_count': self.count_modules(c), 'is_git': self._is_git_repo(c),
            } for c in children][:300],
        }

    # ── per-user recent folders ────────────────────────────────────────
    def _recent_param_key(self):
        return f'ai_code.recent_dirs.{self.env.uid}'

    def get_recent_dirs(self):
        raw = self.env['ir.config_parameter'].sudo().get_param(self._recent_param_key(), '')
        out = []
        for line in raw.split('\n'):
            line = line.strip()
            if line and pathlib.Path(line).is_dir():
                out.append(line)
        return out

    def push_recent_dir(self, path):
        if not path:
            return
        path = str(pathlib.Path(path).resolve())
        dirs = [d for d in self.get_recent_dirs() if d != path]
        dirs.insert(0, path)
        self.env['ir.config_parameter'].sudo().set_param(
            self._recent_param_key(), '\n'.join(dirs[:8]))

    def _is_whitelisted(self, resolved):
        return any(resolved == d or d in resolved.parents
                   for d in self._get_workspace_dirs())

    def adopt_working_dir(self, path):
        """Make a chosen folder usable: it must be inside the browse scope (the
        project area); if it is not already covered by the whitelist/detected
        roots, add it so write operations are jailed to it. Records it as a
        recent folder. Returns the resolved path string."""
        if not path:
            raise UserError(_("No folder chosen."))
        resolved = pathlib.Path(path).resolve()
        if not resolved.is_dir():
            raise UserError(_("Folder %s does not exist.", path))
        core = _odoo_core_root()
        if resolved == core or core in resolved.parents:
            raise UserError(_("Refusing a folder inside the Odoo source tree."))
        if not (self._is_whitelisted(resolved) or self._in_browse_scope(resolved)):
            raise UserError(_("Folder %s is outside the allowed project area.", path))
        if not self._is_whitelisted(resolved):
            params = self.env['ir.config_parameter'].sudo()
            current = params.get_param('ai_code.workspace_dirs', '') or ''
            existing = [c.strip() for c in current.replace('\n', ',').split(',') if c.strip()]
            existing.append(str(resolved))
            params.set_param('ai_code.workspace_dirs', ",".join(existing))
        self.push_recent_dir(str(resolved))
        return str(resolved)

    def detect_odoo_version(self, working_dir, module_name=None):
        """Best-effort Odoo series for a working directory: read a module's
        __manifest__.py 'version' (e.g. '18.0.1.0.0' -> '18.0'); else infer from
        the running server's base module; else fall back to the server series.
        Returns a 'NN.0' string. Never raises."""
        default = self._server_odoo_series()
        try:
            wd = pathlib.Path(working_dir)
        except (TypeError, ValueError):
            return default
        # a specific module first (modify_module), then any module in the dir
        candidates = []
        if module_name:
            candidates.append(wd / module_name / '__manifest__.py')
        try:
            for child in sorted(wd.iterdir()):
                mf = child / '__manifest__.py'
                if child.is_dir() and mf.is_file():
                    candidates.append(mf)
        except OSError:
            pass
        for mf in candidates:
            series = self._series_from_manifest(mf)
            if series:
                return series
        return default

    @staticmethod
    def _series_from_manifest(manifest_path):
        try:
            data = ast.literal_eval(pathlib.Path(manifest_path).read_text(
                encoding='utf-8', errors='replace'))
        except (ValueError, SyntaxError, OSError):
            return None
        if not isinstance(data, dict):
            return None
        version = str(data.get('version') or '')
        # '18.0.1.2.3' -> '18.0'; '1.0' (no series) -> ignore
        m = re.match(r'\s*(\d{2})\.(\d+)', version)
        return f"{m.group(1)}.{m.group(2)}" if m else None

    def _server_odoo_series(self):
        """The running server's Odoo series, e.g. '19.0'."""
        try:
            import odoo.release
            return f"{odoo.release.version_info[0]}.0"
        except Exception:  # noqa: BLE001
            return '19.0'

    def list_projects(self):
        """Text list of the project folders the agent may READ for reference
        (all whitelisted dirs + their modules), each tagged with its detected
        Odoo version. Reads are cross-project; writes stay jailed to the task's
        own working directory. Version tags let the agent translate idioms when
        a reference project is a DIFFERENT Odoo series than the target."""
        target = self._lint_version()
        lines = []
        for d in self._get_workspace_dirs():
            try:
                mods = self.scan_workspace(str(d))['modules']
            except UserError:
                continue
            names = ", ".join(m['name'] for m in mods) or "(no modules)"
            version = self.detect_odoo_version(str(d))
            tag = f"Odoo {version}"
            if version != target:
                tag += " — DIFFERENT from your target; reference its LOGIC, translate idioms"
            lines.append(f"{d}  [{tag}]\n    modules: {names}")
        return "\n".join(lines) or "No project folders registered."

    def scan_workspace(self, working_dir):
        """List the Odoo modules already in a working directory (one manifest per
        direct child dir), with their titles — so the UI and the analyzing phase
        know whether this is a new project (empty) or an existing one to modify.
        Filesystem-only, works on ANY whitelisted folder (no install needed)."""
        working_dir = self._validate_working_dir(working_dir)
        modules = []
        for child in sorted(working_dir.iterdir()):
            if not child.is_dir() or child.name.startswith('.'):
                continue
            manifest = child / '__manifest__.py'
            if not manifest.is_file():
                continue
            title, installable = child.name, True
            try:
                data = ast.literal_eval(manifest.read_text(encoding='utf-8', errors='replace'))
                if isinstance(data, dict):
                    title = data.get('name') or child.name
                    installable = bool(data.get('installable', True))
            except (ValueError, SyntaxError, OSError):
                pass
            modules.append({
                'name': child.name,
                'title': str(title)[:60],
                'installable': installable,
            })
            if len(modules) >= 60:
                break
        version = self.detect_odoo_version(str(working_dir))
        return {'is_empty': not modules, 'modules': modules,
                'version': (version or '').split('.')[0] or '19',
                'git': self.git_status(str(working_dir))}

    def module_name_conflict(self, name, working_dir):
        """If `name` is already an installable Odoo module whose source lives
        OUTSIDE the working dir (Odoo core, enterprise, another addon), return
        that path — the generated module would be shadowed by it and never
        load. Empty string if the name is free (or already inside the workspace)."""
        from odoo.modules.module import get_module_path
        if not re.fullmatch(r'[a-z][a-z0-9_]*', name or ''):
            return ''
        try:
            path = get_module_path(name, display_warning=False)
        except Exception:  # noqa: BLE001
            return ''
        if not path:
            return ''
        path = pathlib.Path(path).resolve()
        working_dir = pathlib.Path(working_dir).resolve()
        if working_dir == path or working_dir in path.parents:
            return ''  # it IS the workspace module, no conflict
        return str(path)

    def _validate_working_dir(self, path):
        """A working directory must be whitelisted (or inside a whitelisted
        dir), outside the Odoo core tree, and a git repository. Returns the
        resolved pathlib.Path; raises UserError otherwise."""
        if not path:
            raise UserError(_("No working directory given."))
        target = pathlib.Path(path)
        if not target.is_dir():
            raise UserError(_("Working directory %s does not exist.", path))
        target = target.resolve()

        dirs = self._get_workspace_dirs()
        if not dirs:
            raise UserError(_(
                "No workspace whitelist configured. Add your custom addons "
                "directories under Settings > AI Chatbot > AI Code first."))
        if not any(target == base or base in target.parents for base in dirs):
            raise UserError(_(
                "Working directory %s is outside the configured whitelist.", path))

        core = _odoo_core_root()
        if target == core or core in target.parents:
            raise UserError(_(
                "Working directory %s is inside the Odoo source tree — refused.", path))

        # git is OPTIONAL: a git repo unlocks branch isolation + per-step commit
        # + revert; a plain folder still works (the agent just writes files, and
        # changes are recorded with diffs but without checkpoints).
        return target

    def _jail_path(self, working_dir, rel_path, for_write=False):
        """Resolve rel_path inside working_dir; refuse escapes, .git internals,
        and (for writes) non-whitelisted extensions."""
        if not rel_path or pathlib.PurePath(rel_path).is_absolute():
            raise UserError(_("Give a path relative to the working directory, got %r.", rel_path))
        working_dir = pathlib.Path(working_dir).resolve()
        candidate = (working_dir / rel_path).resolve()
        if candidate != working_dir and working_dir not in candidate.parents:
            raise UserError(_("Path %s escapes the working directory — refused.", rel_path))
        rel_parts = candidate.relative_to(working_dir).parts
        if '.git' in rel_parts:
            raise UserError(_("Direct access to .git internals is refused."))
        if for_write and not self.dir_writable(working_dir):
            raise UserError(_(
                "Write refused: the working directory %s is mounted READ-ONLY (e.g. a "
                "git-deployed addons volume). Nothing can be written there by anyone — "
                "tell the user to pick a writable workspace (for example the AI workspace "
                "mount) instead of retrying.", working_dir))
        if for_write and candidate.suffix.lower() not in ALLOWED_EXTENSIONS:
            raise UserError(_(
                "Writing %s files is not allowed (allowed: %s).",
                candidate.suffix or '(no extension)', ', '.join(sorted(ALLOWED_EXTENSIONS))))
        # Module jail: while a task runs, writes stay inside ITS module directory
        # (a confused repair session must not create/patch other modules)
        module_jail = self.env.context.get('ai_code_module_jail')
        if for_write and module_jail and (not rel_parts or rel_parts[0] != module_jail):
            raise UserError(_(
                "Write refused: this task only works on the %(module)s/ module "
                "directory, got %(path)s. If an error mentions another module, "
                "that is an environment issue — report it, do not create files for it.",
                module=module_jail, path=rel_path))
        return candidate

    def _iter_files(self, working_dir, pattern='**/*'):
        """Yield text files under working_dir matching the glob, jail-aware."""
        working_dir = pathlib.Path(working_dir)
        for path in sorted(working_dir.glob(pattern)):
            if not path.is_file():
                continue
            rel = path.relative_to(working_dir)
            parts = set(rel.parts)
            if parts & SKIP_DIRS or 'lib' in rel.parts and 'static' in rel.parts:
                continue
            if path.suffix.lower() not in TEXT_EXTENSIONS:
                continue
            yield path, rel

    # ══════════════════════════════════════════════════════════════════
    # Read-only access to ANY installed module's source (Odoo core, enterprise,
    # other custom addons). This is how the agent grounds itself in real Odoo
    # code — read sale_order.py to see how a field is really declared — the same
    # way a human developer does. Strictly read-only: writes stay jailed to the
    # task's working directory.
    # ══════════════════════════════════════════════════════════════════

    def _module_source_root(self, module_name):
        from odoo.modules.module import get_module_path
        name = (module_name or '').strip()
        if not re.fullmatch(r'[a-z_][a-z0-9_]*', name):
            raise UserError(_("Invalid module name %r.", module_name))
        path = get_module_path(name, display_warning=False)
        if not path or not pathlib.Path(path).is_dir():
            raise UserError(_(
                "Module %r is not installed / not found on the addons path.", name))
        return pathlib.Path(path).resolve()

    def _jail_source_path(self, root, rel_path):
        """Resolve rel_path under a module source root; refuse escapes and
        binary/huge files. No extension-whitelist (reading is safe)."""
        if pathlib.PurePath(rel_path or '').is_absolute():
            raise UserError(_("Give a path relative to the module, got %r.", rel_path))
        candidate = (root / (rel_path or '')).resolve()
        if candidate != root and root not in candidate.parents:
            raise UserError(_("Path %s escapes the module directory.", rel_path))
        return candidate

    @_tool
    def tool_read_module_source(self, module_name, path='', start_line=1, num_lines=200):
        """Read a file (windowed) inside any installed module's source tree.
        path='' lists the module's files."""
        root = self._module_source_root(module_name)
        target = self._jail_source_path(root, path)
        if target.is_dir():
            listing = []
            for p in sorted(target.rglob('*')):
                if p.is_file() and p.suffix.lower() in TEXT_EXTENSIONS \
                        and not (set(p.relative_to(root).parts) & SKIP_DIRS):
                    listing.append(p.relative_to(root).as_posix())
                if len(listing) >= 300:
                    break
            header = f"{module_name} at {root} — {len(listing)} source files:"
            return _clip(header + "\n" + "\n".join(listing))
        if not target.is_file():
            return f"ERROR: not found in {module_name}: {path}"
        if target.suffix.lower() not in TEXT_EXTENSIONS or target.stat().st_size > 2 * 1024 * 1024:
            return f"ERROR: {path} is binary or too large to read."
        lines = target.read_text(encoding='utf-8', errors='replace').splitlines()
        total = len(lines)
        start = max(1, int(start_line or 1))
        count = min(int(num_lines or 200), MAX_READ_WINDOW)
        window = lines[start - 1:start - 1 + count]
        body = '\n'.join(f"{start + i:5d}\t{line}" for i, line in enumerate(window))
        header = f"{module_name}/{path} (lines {start}-{min(start + count - 1, total)} of {total})"
        if start + count - 1 < total:
            header += f" — continue with start_line={start + count}"
        return _clip(f"{header}\n{body}")

    @_tool
    def tool_grep_module_source(self, module_name, pattern, glob='**/*', max_results=40):
        """Regex-search a single installed module's source (read-only)."""
        root = self._module_source_root(module_name)
        try:
            regex = re.compile(pattern)
        except re.error as e:
            return f"ERROR: invalid regex: {e}"
        max_results = min(int(max_results or 40), MAX_GREP_RESULTS)
        hits = []
        for path in sorted(root.glob(glob or '**/*')):
            if not path.is_file() or path.suffix.lower() not in TEXT_EXTENSIONS:
                continue
            if set(path.relative_to(root).parts) & SKIP_DIRS or path.stat().st_size > MAX_FILE_BYTES:
                continue
            try:
                for no, line in enumerate(path.read_text(encoding='utf-8', errors='replace').splitlines(), 1):
                    if regex.search(line):
                        hits.append(f"{path.relative_to(root).as_posix()}:{no}: {line.strip()[:200]}")
                        if len(hits) >= max_results:
                            break
            except OSError:
                continue
            if len(hits) >= max_results:
                break
        if not hits:
            return f"No match for /{pattern}/ in module {module_name}."
        return _clip(f"{len(hits)} match(es) in {module_name}:\n" + "\n".join(hits))

    # ══════════════════════════════════════════════════════════════════
    # Read tools (working directory)
    # ══════════════════════════════════════════════════════════════════

    def tool_read_file_raw(self, working_dir, path):
        """Full file content, no line numbers, jail-checked — for the UI 'copy
        file' button. Not an LLM tool."""
        working_dir = self._validate_working_dir(working_dir)
        target = self._jail_path(working_dir, path)
        if not target.is_file():
            return ""
        try:
            data = target.read_text(encoding='utf-8', errors='replace')
        except OSError:
            return ""
        return data[:200000]

    @_tool
    def tool_read_file(self, working_dir, path, start_line=1, num_lines=200):
        """Windowed file read (never dumps whole big files into context)."""
        working_dir = self._validate_working_dir(working_dir)
        target = self._jail_path(working_dir, path)
        if not target.is_file():
            return f"ERROR: file not found: {path}"
        try:
            lines = target.read_text(encoding='utf-8', errors='replace').splitlines()
        except OSError as e:
            return f"ERROR: cannot read {path}: {e}"
        total = len(lines)
        start = max(1, int(start_line or 1))
        count = min(int(num_lines or 200), MAX_READ_WINDOW)
        window = lines[start - 1:start - 1 + count]
        body = '\n'.join(f"{start + i:5d}\t{line}" for i, line in enumerate(window))
        header = f"{path} (lines {start}-{min(start + count - 1, total)} of {total})"
        if start + count - 1 < total:
            header += f" — continue with start_line={start + count}"
        return _clip(f"{header}\n{body}")

    @_tool
    def tool_grep(self, working_dir, pattern, glob='**/*', max_results=MAX_GREP_RESULTS,
                  context_lines=0):
        """Regex search across workspace text files."""
        working_dir = self._validate_working_dir(working_dir)
        try:
            regex = re.compile(pattern)
        except re.error as e:
            return f"ERROR: invalid regex: {e}"
        max_results = min(int(max_results or MAX_GREP_RESULTS), MAX_GREP_RESULTS)
        context_lines = min(int(context_lines or 0), 5)
        hits, files_scanned = [], 0
        for path, rel in self._iter_files(working_dir, glob or '**/*'):
            if path.stat().st_size > MAX_FILE_BYTES:
                continue
            files_scanned += 1
            try:
                lines = path.read_text(encoding='utf-8', errors='replace').splitlines()
            except OSError:
                continue
            for no, line in enumerate(lines, 1):
                if regex.search(line):
                    if context_lines:
                        lo, hi = max(0, no - 1 - context_lines), no + context_lines
                        chunk = '\n'.join(
                            f"{rel.as_posix()}:{i + 1}:{'>' if i + 1 == no else ' '} {lines[i]}"
                            for i in range(lo, min(hi, len(lines))))
                        hits.append(chunk)
                    else:
                        hits.append(f"{rel.as_posix()}:{no}: {line.strip()[:200]}")
                    if len(hits) >= max_results:
                        break
            if len(hits) >= max_results:
                break
        if not hits:
            return f"No match for /{pattern}/ in {files_scanned} files."
        header = f"{len(hits)} match(es)" + (" (capped)" if len(hits) >= max_results else "")
        return _clip(header + "\n" + "\n".join(hits))

    @_tool
    def tool_glob(self, working_dir, pattern='**/*', limit=200):
        """List files matching a glob, relative paths."""
        working_dir = self._validate_working_dir(working_dir)
        rels = [rel.as_posix() for _p, rel in self._iter_files(working_dir, pattern or '**/*')]
        shown = rels[:min(int(limit or 200), 500)]
        suffix = f"\n... and {len(rels) - len(shown)} more" if len(rels) > len(shown) else ""
        return _clip("\n".join(shown) + suffix) if shown else "No file matches."

    @_tool
    def tool_tree(self, working_dir, subdir='', depth=3):
        """Compact directory tree."""
        working_dir = self._validate_working_dir(working_dir)
        root = self._jail_path(working_dir, subdir) if subdir else working_dir
        if not root.is_dir():
            return f"ERROR: not a directory: {subdir}"
        depth = min(int(depth or 3), 5)
        out = []

        def walk(node, prefix, level):
            if level > depth or len(out) > 400:
                return
            entries = sorted(node.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))
            for entry in entries:
                if entry.name in SKIP_DIRS or entry.name.endswith('.pyc'):
                    continue
                out.append(f"{prefix}{entry.name}{'/' if entry.is_dir() else ''}")
                if entry.is_dir():
                    walk(entry, prefix + '  ', level + 1)

        out.append(root.name + '/')
        walk(root, '  ', 1)
        return _clip('\n'.join(out))

    # ══════════════════════════════════════════════════════════════════
    # Write tools (guardrails inside)
    # ══════════════════════════════════════════════════════════════════

    @_tool
    def tool_write_file(self, working_dir, path, content):
        """Create or overwrite a file. Content is validated first; on
        validation failure NOTHING is written and the error is returned."""
        working_dir = self._validate_working_dir(working_dir)
        target = self._jail_path(working_dir, path, for_write=True)
        content = (content or '').replace('\r\n', '\n')
        if not content.endswith('\n'):
            content += '\n'
        if len(content.encode('utf-8')) > MAX_FILE_BYTES:
            return f"ERROR: refusing to write {path}: exceeds {MAX_FILE_BYTES // 1024} KB cap."
        problems = self._validate_content(target, content)
        if problems:
            return "REJECTED (nothing written). Fix these and retry:\n- " + "\n- ".join(problems)

        existed = target.is_file()
        old = target.read_text(encoding='utf-8', errors='replace') if existed else ''
        if existed and old == content:
            return f"No change: {path} already has exactly this content."
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding='utf-8', newline='\n')
        sha = self._commit_file(working_dir, target,
                                f"{'edit' if existed else 'add'} {target.name}")
        self._record_change(working_dir, target, 'modify' if existed else 'create', old, content, sha)
        n = content.count('\n')
        return f"OK: wrote {path} ({n} lines){f', commit {sha[:7]}' if sha else ''}"

    @_tool
    def tool_edit_file(self, working_dir, path, old_string, new_string, replace_all=False):
        """Exact-string replacement edit (the safe, reviewable edit form)."""
        working_dir = self._validate_working_dir(working_dir)
        target = self._jail_path(working_dir, path, for_write=True)
        if not target.is_file():
            return f"ERROR: file not found: {path}"
        if not old_string:
            return "ERROR: old_string must not be empty (use write for new files)."
        if old_string == new_string:
            return "ERROR: old_string and new_string are identical."
        old = target.read_text(encoding='utf-8', errors='replace')
        count = old.count(old_string)
        if count == 0:
            return ("ERROR: old_string not found in the file. Read the file again and "
                    "copy the exact text including whitespace.")
        if count > 1 and not replace_all:
            return (f"ERROR: old_string occurs {count} times. Add more surrounding context "
                    "to make it unique, or pass replace_all=true.")
        content = old.replace(old_string, new_string) if replace_all \
            else old.replace(old_string, new_string, 1)
        problems = self._validate_content(target, content)
        if problems:
            return "REJECTED (file unchanged). The edit would break the file:\n- " + "\n- ".join(problems)
        target.write_text(content, encoding='utf-8', newline='\n')
        sha = self._commit_file(working_dir, target, f"edit {target.name}")
        self._record_change(working_dir, target, 'modify', old, content, sha)
        hunk = self._unified_diff(old, content, path)
        return _clip(f"OK: edited {path}{f', commit {sha[:7]}' if sha else ''}\n{hunk}", 3000)

    @_tool
    def tool_scaffold_module(self, working_dir, module_name, module_title=''):
        """Create a minimal valid module skeleton in one commit."""
        working_dir = self._validate_working_dir(working_dir)
        if not re.fullmatch(r'[a-z][a-z0-9_]*', module_name or ''):
            return f"ERROR: invalid technical module name {module_name!r} (snake_case required)."
        module_root = working_dir / module_name
        if module_root.exists():
            return f"ERROR: {module_name} already exists in the working directory."
        conflict = self.module_name_conflict(module_name, working_dir)
        if conflict:
            return (f"ERROR: the name `{module_name}` is already taken by an existing Odoo "
                    f"module at {conflict}; a new module with this name would be shadowed "
                    f"and never load. Choose a more specific / prefixed technical name.")
        title = module_title or module_name.replace('_', ' ').title()
        files = {
            f"{module_name}/__init__.py": "# -*- coding: utf-8 -*-\nfrom . import models\n",
            f"{module_name}/__manifest__.py": (
                "# -*- coding: utf-8 -*-\n{\n"
                f"    'name': {title!r},\n"
                "    'version': '19.0.1.0.0',\n"
                "    'category': 'Uncategorized',\n"
                "    'summary': '',\n"
                "    'depends': ['base'],\n"
                "    'data': [],\n"
                "    'license': 'OPL-1',\n"
                "    'installable': True,\n"
                "    'application': False,\n"
                "}\n"),
            f"{module_name}/models/__init__.py": "# -*- coding: utf-8 -*-\n",
        }
        written = []
        for rel, content in files.items():
            target = self._jail_path(working_dir, rel, for_write=True)
            problems = self._validate_content(target, content)
            if problems:
                return "ERROR: scaffold template invalid: " + "; ".join(problems)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding='utf-8', newline='\n')
            written.append(rel)
        sha = self._commit_paths(working_dir, written, f"scaffold module {module_name}")
        for rel in written:
            target = working_dir / rel
            self._record_change(working_dir, target, 'create', '',
                                target.read_text(encoding='utf-8'), sha)
        return (f"OK: scaffolded {module_name} ({len(written)} files)"
                f"{f', commit {sha[:7]}' if sha else ''}\n" + "\n".join(written))

    # ══════════════════════════════════════════════════════════════════
    # Guardrail validators (version-aware write gate)
    # ══════════════════════════════════════════════════════════════════

    def _lint_version(self):
        """Target Odoo series for the guardrail rules (from the running task,
        else the server series). Version-specific rules only fire at/above the
        version where the idiom actually changed — so an Odoo 17/18 project is
        NOT falsely rejected for legacy-but-valid patterns."""
        task_id = self.env.context.get('ai_code_task_id')
        if task_id:
            task = self.env['ai.code.task'].sudo().browse(task_id)
            if task.odoo_version:
                return task.odoo_version
        return self._server_odoo_series()

    @staticmethod
    def _ver_ge(version, threshold):
        """True if series `version` (e.g. '18.0') >= threshold (e.g. 18)."""
        try:
            return int(str(version).split('.')[0]) >= threshold
        except (ValueError, IndexError):
            return True  # unknown → assume modern (fail toward the strict rules)

    def _validate_content(self, target, content):
        """Return a list of problem strings; empty list = content accepted."""
        suffix = target.suffix.lower()
        name = target.name
        version = self._lint_version()
        problems = []
        if suffix == '.py':
            problems += self._validate_python(name, content, version)
        elif suffix == '.xml':
            problems += self._validate_xml(content, version)
        elif suffix == '.csv':
            problems += self._validate_access_csv(name, content)
        elif suffix in ('.po', '.pot'):
            problems += self._validate_po(content)
        elif suffix == '.js':
            problems += self._validate_js(content)
        return problems

    def _validate_python(self, name, content, version='19.0'):
        problems = []
        try:
            tree = ast.parse(content)
        except SyntaxError as e:
            return [f"Python syntax error line {e.lineno}: {e.msg}"]

        if name == '__manifest__.py':
            try:
                data = ast.literal_eval(content)
                if not isinstance(data, dict):
                    problems.append("__manifest__.py must evaluate to a dict.")
                elif 'depends' in data and not isinstance(data['depends'], list):
                    problems.append("'depends' must be a list.")
            except (ValueError, SyntaxError):
                problems.append("__manifest__.py must be a literal dict (no imports/expressions).")

        # ── version-specific idiom rules (only fire at/above the version where
        #    the idiom actually changed, so older projects aren't false-flagged) ──
        if self._ver_ge(version, 19) and re.search(r'^\s*_sql_constraints\s*=', content, re.M):
            problems.append(
                "Odoo 19: '_sql_constraints' is ignored. Declare class attributes instead: "
                "_my_uniq = models.Constraint('UNIQUE(col_a, col_b)', \"error message\")")
        if self._ver_ge(version, 17) and re.search(r'\bdef\s+name_get\s*\(', content):
            problems.append(
                "Odoo 17+: name_get() was removed. Override _compute_display_name() and set "
                "record.display_name instead.")
        if self._ver_ge(version, 17) and re.search(r'\.name_get\s*\(', content):
            problems.append(
                "Odoo 17+: .name_get() calls do not exist anymore. Read .display_name instead.")
        # @api.multi/@api.one gone since Odoo 13 — always flag on supported versions
        if re.search(r'@api\.(multi|one)\b', content):
            problems.append("@api.multi/@api.one were removed in Odoo 13 — just drop them.")
        if re.search(r'\bodoo\.__file__', content):
            problems.append(
                "odoo.__file__ can be None (namespace package). Anchor paths on "
                "odoo.modules.module.__file__ instead.")

        # BaseModel defines __slots__: `self._x = ...` inside an Odoo model
        # class crashes at runtime. Only flag classes that declare _name/_inherit
        # so plain Python helper classes stay allowed.
        for klass in (n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)):
            is_model = any(
                isinstance(stmt, ast.Assign)
                and any(isinstance(t, ast.Name) and t.id in ('_name', '_inherit')
                        for t in stmt.targets)
                for stmt in klass.body)
            if not is_model:
                continue
            for node in ast.walk(klass):
                if isinstance(node, ast.Assign):
                    for tgt in node.targets:
                        if (isinstance(tgt, ast.Attribute)
                                and isinstance(tgt.value, ast.Name)
                                and tgt.value.id == 'self'
                                and tgt.attr.startswith('_')):
                            problems.append(
                                f"line {node.lineno}: assigning self.{tgt.attr} — BaseModel "
                                "uses __slots__, ad-hoc private attributes on recordsets raise "
                                "AttributeError. Thread state through method arguments instead.")
        return problems

    def _validate_xml(self, content, version='19.0'):
        from lxml import etree
        try:
            root = etree.fromstring(content.encode('utf-8'))
        except etree.XMLSyntaxError as e:
            return [f"XML syntax error: {e}"]
        problems = []
        # <list> replaced <tree> in Odoo 18; only flag on 18+
        if self._ver_ge(version, 18):
            for el in root.iter('tree'):
                problems.append(
                    f"line {el.sourceline}: <tree> views are <list> in Odoo 18+ — rename the tag.")
        # attrs=/states= removed in Odoo 17; only flag on 17+
        if self._ver_ge(version, 17):
            for el in root.iter():
                if 'attrs' in el.attrib:
                    problems.append(
                        f"line {el.sourceline}: attrs= was removed (Odoo 17+). Use direct "
                        "expressions: invisible=\"state != 'draft'\", readonly=\"...\", etc.")
                if 'states' in el.attrib and el.tag in ('button', 'field'):
                    problems.append(
                        f"line {el.sourceline}: states= was removed (Odoo 17+). Use "
                        "invisible=\"state not in (...)\" instead.")
        for search in root.iter('search'):
            for group in search.iter('group'):
                bad = [a for a in ('expand', 'string', 'col') if a in group.attrib]
                if bad:
                    problems.append(
                        f"line {group.sourceline}: <group {bad[0]}=...> inside a search view "
                        "violates the Odoo 19 RNG — search groups take no such attributes, "
                        "use a plain <group>.")
        return problems

    def _validate_access_csv(self, name, content):
        if name != 'ir.model.access.csv':
            return []
        lines = [line for line in content.splitlines() if line.strip()]
        if not lines:
            return ["ir.model.access.csv is empty."]
        header = 'id,name,model_id:id,group_id:id,perm_read,perm_write,perm_create,perm_unlink'
        if lines[0].strip() != header:
            return [f"ir.model.access.csv header must be exactly: {header}"]
        problems = []
        for no, line in enumerate(lines[1:], 2):
            if line.count(',') != 7:
                problems.append(f"line {no}: expected 8 comma-separated columns.")
        return problems

    def _validate_po(self, content):
        if 'msgid' in content and '#. module:' not in content:
            return [
                "Never hand-write .po files without '#. module: <name>' comments on every "
                "entry — Odoo 19's PO reader crashes on them at install. Generate the file "
                "with the base.language.export wizard instead, then only fill msgstr values."]
        return []

    def _validate_js(self, content):
        problems = []
        for open_c, close_c, label in (('{', '}', 'braces'), ('(', ')', 'parentheses'),
                                       ('[', ']', 'brackets')):
            if content.count(open_c) != content.count(close_c):
                problems.append(f"Unbalanced {label}: {content.count(open_c)} '{open_c}' vs "
                                f"{content.count(close_c)} '{close_c}'.")
        # Odoo 19 OWL: a component registered as a client action must accept the
        # standard action props; `static props = {}` fails validation in debug mode.
        if re.search(r'category\(\s*["\']actions["\']\s*\)\s*\.add', content) \
                and re.search(r'static\s+props\s*=\s*\{\s*\}', content) \
                and 'standardActionServiceProps' not in content:
            problems.append(
                "OWL client action with `static props = {}`: a client-action component "
                "receives standard action props. Use "
                "`import { standardActionServiceProps } from \"@web/webclient/actions/action_service\";` "
                "then `static props = { ...standardActionServiceProps };` (or `[\"*\"]`).")
        return problems


    # ══════════════════════════════════════════════════════════════════
    # Observation gate support + structural reads + multi-edit
    # ══════════════════════════════════════════════════════════════════

    def file_stamp(self, working_dir, path):
        """(rel_path, (mtime_ns, size)) of a workspace file, or None when the
        path is invalid or the file does not exist. Used by the tools layer to
        enforce read-before-edit and to detect external modification."""
        try:
            working_dir = self._validate_working_dir(working_dir)
            target = self._jail_path(working_dir, path)
        except UserError:
            return None
        if not target.is_file():
            return None
        try:
            st = target.stat()
        except OSError:
            return None
        rel = target.relative_to(working_dir).as_posix()
        return rel, (st.st_mtime_ns, st.st_size)

    def module_exists(self, name):
        from odoo.modules.module import get_module_path
        try:
            return bool(get_module_path(name, display_warning=False))
        except Exception:  # noqa: BLE001
            return False

    def sandbox_available(self):
        params = self.env['ir.config_parameter'].sudo()
        if params.get_param('ai_code.sandbox_enabled', '1') in ('0', 'False', 'off'):
            return False
        return (_odoo_core_root() / 'odoo-bin').is_file()

    @_tool
    def tool_outline(self, working_dir, path):
        """Symbols of one file with line numbers (Python: classes, Odoo model
        names, fields, methods; XML: records/templates/menus with ids)."""
        working_dir = self._validate_working_dir(working_dir)
        target = self._jail_path(working_dir, path)
        if not target.is_file():
            return f"ERROR: file not found: {path}"
        try:
            text = target.read_text(encoding='utf-8', errors='replace')
        except OSError as e:
            return f"ERROR: cannot read {path}: {e}"
        suffix = target.suffix.lower()
        lines = []
        if suffix == '.py':
            try:
                tree = ast.parse(text)
            except SyntaxError as e:
                return f"ERROR: {path} has a syntax error at line {e.lineno}: {e.msg}"
            for node in tree.body:
                if isinstance(node, ast.ClassDef):
                    bases = ", ".join(ast.unparse(b) for b in node.bases)
                    lines.append(f"{node.lineno:5d}  class {node.name}({bases})")
                    for stmt in node.body:
                        if isinstance(stmt, ast.Assign) and stmt.targets:
                            tgt = stmt.targets[0]
                            if isinstance(tgt, ast.Name):
                                if tgt.id in ('_name', '_inherit', '_description', '_order', '_auto'):
                                    lines.append(f"{stmt.lineno:5d}      {tgt.id} = {ast.unparse(stmt.value)[:80]}")
                                elif isinstance(stmt.value, ast.Call) and 'fields.' in ast.unparse(stmt.value.func):
                                    lines.append(f"{stmt.lineno:5d}      {tgt.id} = {ast.unparse(stmt.value.func)}(...)")
                        elif isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
                            decos = " ".join("@" + ast.unparse(d)[:40] for d in stmt.decorator_list)
                            args = ", ".join(a.arg for a in stmt.args.args)
                            lines.append(f"{stmt.lineno:5d}      def {stmt.name}({args})"
                                         + (f"  {decos}" if decos else ""))
                elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    args = ", ".join(a.arg for a in node.args.args)
                    lines.append(f"{node.lineno:5d}  def {node.name}({args})")
                elif isinstance(node, ast.Assign) and node.targets and isinstance(node.targets[0], ast.Name):
                    lines.append(f"{node.lineno:5d}  {node.targets[0].id} = ...")
                elif isinstance(node, (ast.Import, ast.ImportFrom)):
                    lines.append(f"{node.lineno:5d}  {ast.unparse(node)[:100]}")
        elif suffix == '.xml':
            from lxml import etree
            try:
                root = etree.fromstring(text.encode('utf-8'))
            except etree.XMLSyntaxError as e:
                return f"ERROR: {path} is not well-formed XML: {e}"
            for el in root.iter():
                if not isinstance(el.tag, str):
                    continue
                tag = etree.QName(el).localname
                if tag in ('record', 'template', 'menuitem', 'report', 'act_window', 'function', 'delete'):
                    extra = ""
                    if tag == 'record':
                        extra = f" model={el.get('model', '')}"
                        for f in el.findall('field'):
                            if f.get('name') in ('name', 'model', 'inherit_id'):
                                val = f.get('ref') or (f.text or '').strip()[:50]
                                extra += f" {f.get('name')}={val}"
                    lines.append(f"{el.sourceline:5d}  <{tag} id=\"{el.get('id', '')}\"{extra}>")
                elif tag == 't' and el.get('t-name'):
                    lines.append(f"{el.sourceline:5d}  <t t-name=\"{el.get('t-name')}\">")
        elif suffix == '.js':
            for no, line in enumerate(text.splitlines(), 1):
                s = line.strip()
                if re.match(r'(export\s+)?(class|function|const|let|import)\b', s) or \
                        re.match(r'(static\s+)?[A-Za-z_$][\w$]*\s*\(.*\)\s*\{$', s):
                    lines.append(f"{no:5d}  {s[:110]}")
        else:
            return f"outline supports .py/.xml/.js; use read_file for {path}."
        total = text.count('\n') + 1
        if not lines:
            return f"{path} ({total} lines): no symbols found."
        return _clip(f"{path} ({total} lines)\n" + "\n".join(lines[:300]))

    @_tool
    def tool_multi_edit(self, working_dir, path, edits):
        """Several ordered exact-string replacements in one file; all-or-
        nothing, validated once, one commit."""
        working_dir = self._validate_working_dir(working_dir)
        target = self._jail_path(working_dir, path, for_write=True)
        if not target.is_file():
            return f"ERROR: file not found: {path}"
        if not isinstance(edits, list) or not edits:
            return "ERROR: edits must be a non-empty list of {old_string, new_string}."
        old = target.read_text(encoding='utf-8', errors='replace')
        content = old
        for i, edit in enumerate(edits, 1):
            if not isinstance(edit, dict):
                return f"ERROR: edit {i} is not an object."
            old_s, new_s = edit.get('old_string') or '', edit.get('new_string') or ''
            if not old_s:
                return f"ERROR: edit {i}: old_string is empty."
            count = content.count(old_s)
            if count == 0:
                return (f"ERROR: edit {i}: old_string not found (after applying the previous "
                        "edits). Nothing was written. Copy the exact text from read_file.")
            if count > 1 and not edit.get('replace_all'):
                return (f"ERROR: edit {i}: old_string occurs {count} times; add context to make "
                        "it unique. Nothing was written.")
            content = content.replace(old_s, new_s) if edit.get('replace_all') \
                else content.replace(old_s, new_s, 1)
        if content == old:
            return f"No change: {path} already has this content."
        problems = self._validate_content(target, content)
        if problems:
            return "REJECTED (file unchanged). The edits would break the file:\n- " + "\n- ".join(problems)
        target.write_text(content, encoding='utf-8', newline='\n')
        sha = self._commit_file(working_dir, target, f"edit {target.name} ({len(edits)} edits)")
        self._record_change(working_dir, target, 'modify', old, content, sha)
        hunk = self._unified_diff(old, content, path)
        return _clip(f"OK: applied {len(edits)} edits to {path}{f', commit {sha[:7]}' if sha else ''}\n{hunk}", 3000)

    # ══════════════════════════════════════════════════════════════════
    # Layered verification (static -> install -> runtime -> tests) with
    # honest reporting of what did NOT run
    # ══════════════════════════════════════════════════════════════════

    def sandbox_verify(self, task, module, run_tests=False):
        """Verify a module: preflight (static) -> isolated install -> runtime
        smoke -> optional unit tests. Returns text for the model that names
        the failing stage with evidence, and the levels verified vs skipped."""
        module = (module or '').strip()
        working_dir = self._validate_working_dir(task.target_dir)
        if not module or not (working_dir / module / '__manifest__.py').is_file():
            mods = [c.name for c in working_dir.iterdir()
                    if c.is_dir() and (c / '__manifest__.py').is_file()]
            return (f"ERROR: module {module!r} not found in the workspace. Modules here: "
                    f"{', '.join(mods) or '(none)'}. Pass the module technical name.")
        verified, skipped = [], []
        # L1 static
        from .ai_code_preflight import preflight_scan
        try:
            issues = preflight_scan(working_dir / module, module, self._module_exists_or_sibling(working_dir))
        except Exception as e:  # noqa: BLE001
            issues = []
            skipped.append(('static', f"preflight crashed: {type(e).__name__}: {e}"))
        if issues:
            return self._render_verify(False, 'static', verified, skipped + [
                ('install', 'not run — fix the static issues first'),
                ('runtime', 'not run'), ('tests', 'not run')],
                "\n".join(f"- {i}" for i in issues), module)
        if not any(s[0] == 'static' for s in skipped):
            verified.append('static')
        if not self.sandbox_available():
            return self._render_verify(True, 'done', verified, skipped + [
                ('install', 'sandbox disabled / odoo-bin not found'),
                ('runtime', 'not run'), ('tests', 'not run')], '', module)
        # L2 install (+ L3 smoke inside sandbox_run)
        task.sudo().write({'module_name': module})
        try:
            return self._sandbox_verify_run(task, module, run_tests, working_dir, verified, skipped)
        finally:
            if self._sandbox_template_enabled():
                try:
                    self._drop_sandbox_db(self._sandbox_db_for(task))
                except Exception:  # noqa: BLE001 — cleanup only
                    _logger.debug("ai_code sandbox: cleanup failed", exc_info=True)

    def _sandbox_verify_run(self, task, module, run_tests, working_dir, verified, skipped):
        ok, excerpt = self.sandbox_run(task)
        if not ok:
            stage = 'runtime' if excerpt.startswith('SMOKE') else 'install'
            if stage == 'runtime':
                verified.append('install')
            offending = self.sandbox_error_files(task, excerpt)
            log = excerpt[-6000:]
            if offending:
                log += "\n\nOffending file(s) around the error line:\n" + offending
            return self._render_verify(False, stage, verified, skipped + [
                ('runtime', 'not run') if stage == 'install' else ('tests', 'not run'),
                ] + ([('tests', 'not run')] if stage == 'install' else []), log, module)
        verified += ['install', 'runtime']
        # L4 tests
        if run_tests:
            db_name = self._sandbox_db_for(task) if self._sandbox_template_enabled() \
                else self._sandbox_params()[0]
            rc, out = self._run_odoo_subprocess(
                self._odoo_bin_cmd(db_name, working_dir, '-u', module, test_enable=True))
            errors = self._extract_errors(out)
            failed = [ln for ln in out.splitlines() if ' ERROR ' in ln and ('FAIL' in ln or 'ERROR: ' in ln)]
            if rc != 0 or errors or failed:
                return self._render_verify(False, 'tests', verified, skipped,
                                           "\n".join(errors or failed)[-6000:] or out[-3000:], module)
            if 'tests' not in out and '0 tests' in out:
                skipped.append(('tests', 'no test collected in the module'))
            else:
                verified.append('tests')
        else:
            skipped.append(('tests', 'run_tests=false'))
        return self._render_verify(True, 'done', verified, skipped, '', module)

    def _module_exists_or_sibling(self, working_dir):
        def module_exists(name):
            if (working_dir / name / '__manifest__.py').is_file():
                return True
            return self.module_exists(name) or False
        return module_exists

    _VERIFY_LABEL = {'static': 'static preflight', 'install': 'sandbox install',
                     'runtime': 'runtime smoke', 'tests': 'module tests'}

    @classmethod
    def _render_verify(cls, ok, stage, verified, skipped, log, module):
        lines = []
        done = ", ".join(cls._VERIFY_LABEL.get(v, v) for v in verified) or "(none)"
        if ok:
            if skipped:
                lines.append(f"⚠️ PARTIALLY verified ({module}): {done} passed, but these levels "
                             f"did NOT run — do not claim they did:")
            else:
                lines.append(f"✅ ALL levels verified ({module}): {done}.")
        else:
            lines.append(f"❌ FAILED at [{cls._VERIFY_LABEL.get(stage, stage)}] ({module})"
                         + (f" — passed before it: {done}" if verified else ""))
        for level, reason in skipped:
            lines.append(f"  · {cls._VERIFY_LABEL.get(level, level)}: not verified — {reason}")
        if log:
            lines.append("")
            lines.append(log)
        if ok and skipped:
            lines.append("")
            lines.append("When reporting to the user, state which levels were NOT verified.")
        if not ok:
            lines.append("")
            lines.append("Fix from this evidence (read the offending file around the line), then "
                         "run sandbox_test again. Do not declare the module done before it is green.")
        return "\n".join(lines)


    # ══════════════════════════════════════════════════════════════════
    # Shell (policy decided by utils/shell_guard; approval by the runner)
    # ══════════════════════════════════════════════════════════════════

    SHELL_TIMEOUT = 120
    SHELL_MAX_OUTPUT = 12000

    def shell_enabled(self):
        params = self.env['ir.config_parameter'].sudo()
        return params.get_param('ai_code.shell_enabled', '1') not in ('0', 'False', 'off')

    def shell_auto_approve(self):
        params = self.env['ir.config_parameter'].sudo()
        return params.get_param('ai_code.shell_auto_approve', '0') in ('1', 'True', 'on')

    def tool_shell_run(self, working_dir, command, timeout=None):
        """Run ONE command inside the workspace root (no policy check here —
        callers decide through shell_guard.classify). Output is head+tail
        clipped; the exit code is always reported."""
        working_dir = self._validate_working_dir(working_dir)
        timeout = int(timeout or self.SHELL_TIMEOUT)
        env = dict(os.environ, PYTHONUTF8='1', PYTHONIOENCODING='utf-8', GIT_TERMINAL_PROMPT='0')
        started = time.perf_counter()
        try:
            proc = subprocess.run(
                command, shell=True, cwd=str(working_dir), capture_output=True, text=True,
                encoding='utf-8', errors='replace', timeout=timeout, env=env,
                stdin=subprocess.DEVNULL)
        except subprocess.TimeoutExpired as e:
            partial = ((e.stdout or b'') if isinstance(e.stdout, str) else (e.stdout or b'').decode('utf-8', 'replace'))
            return (f"TIMEOUT after {timeout}s (command killed). Partial output:\n"
                    f"{str(partial)[-3000:]}")
        except OSError as e:
            return f"ERROR: could not start the command: {e}"
        out = (proc.stdout or '')
        err = (proc.stderr or '')
        body = out + (("\n[stderr]\n" + err) if err.strip() else '')
        if len(body) > self.SHELL_MAX_OUTPUT:
            head, tail = body[:self.SHELL_MAX_OUTPUT * 2 // 3], body[-self.SHELL_MAX_OUTPUT // 3:]
            body = head + f"\n... [{len(body) - len(head) - len(tail)} chars omitted] ...\n" + tail
        secs = time.perf_counter() - started
        return f"[exit={proc.returncode}, {secs:.1f}s] {command}\n{body.strip() or '(no output)'}"

    # ══════════════════════════════════════════════════════════════════
    # Git
    # ══════════════════════════════════════════════════════════════════

    def _git(self, working_dir, *args, check=True):
        """Run one git command inside working_dir and return stdout."""
        identity = self._git_identity_args()
        cmd = ['git', '-C', str(working_dir)] + identity + list(args)
        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True, encoding='utf-8',
                errors='replace', timeout=GIT_TIMEOUT)
        except FileNotFoundError:
            raise UserError(_("git executable not found on the server."))
        except subprocess.TimeoutExpired:
            raise UserError(_("git command timed out: %s", ' '.join(args)))
        if check and proc.returncode != 0:
            raise UserError(_("git %s failed: %s", ' '.join(args),
                              (proc.stderr or proc.stdout or '').strip()[:2000]))
        return (proc.stdout or '').strip()

    def _git_identity_args(self):
        params = self.env['ir.config_parameter'].sudo()
        name = params.get_param('ai_code.git_user_name', 'AI Code Agent')
        email = params.get_param('ai_code.git_user_email', 'ai-code@localhost')
        return ['-c', f'user.name={name}', '-c', f'user.email={email}']

    def _is_git_repo(self, working_dir):
        try:
            proc = subprocess.run(
                ['git', '-C', str(working_dir), 'rev-parse', '--is-inside-work-tree'],
                capture_output=True, text=True, timeout=GIT_TIMEOUT)
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return False
        return proc.returncode == 0 and proc.stdout.strip() == 'true'

    def server_dev_reload(self):
        """True when THIS server runs with --dev=reload/all: every .py the agent
        writes then RESTARTS the server, killing the coding run mid-task (the
        'Interrupted — server restart or crash' failures) and hammering the
        machine. The UI/runner warn the user to restart without it."""
        try:
            dev = odoo_config['dev_mode'] or []
        except (KeyError, TypeError):
            return False
        return 'reload' in dev or 'all' in dev

    def git_status(self, working_dir):
        """Compact git state for the UI: whether the folder is a repo, its
        current branch, and how many entries are uncommitted. Returns
        {'is_git': False} for non-repos so the workspace simply hides the git
        chip (version control is optional here)."""
        try:
            path = pathlib.Path(working_dir)
        except (TypeError, ValueError):
            return {'is_git': False}
        if not self._is_git_repo(path):
            return {'is_git': False}
        branch = self._git(path, 'rev-parse', '--abbrev-ref', 'HEAD', check=False) or 'HEAD'
        porcelain = self._git(path, 'status', '--porcelain', check=False)
        dirty = sum(1 for ln in porcelain.splitlines() if ln.strip())
        return {'is_git': True, 'branch': branch[:60], 'dirty': dirty}

    def _current_task(self):
        task_id = self.env.context.get('ai_code_task_id')
        return self.env['ai.code.task'].browse(task_id) if task_id else self.env['ai.code.task']

    def ensure_task_branch(self, working_dir, task):
        """Create/checkout the task's isolation branch (called by the runner
        before the first write). No-op on a NON-git workspace (git is optional)."""
        working_dir = self._validate_working_dir(working_dir)
        if not self._is_git_repo(working_dir):
            return ''
        if task.branch_name:
            current = self._git(working_dir, 'rev-parse', '--abbrev-ref', 'HEAD', check=False)
            if current and current != task.branch_name:
                self._git(working_dir, 'checkout', task.branch_name, check=False)
            return task.branch_name
        slug = re.sub(r'[^a-z0-9]+', '-', (task.module_name or 'task').lower()).strip('-')[:30]
        branch = f"ai-code/{task.id}-{slug}"
        base = self._git(working_dir, 'rev-parse', 'HEAD', check=False)  # '' if unborn
        self._git(working_dir, 'checkout', '-b', branch)
        task.write({'branch_name': branch, 'branch_base_sha': base or False})
        return branch

    def _commit_file(self, working_dir, target, message):
        return self._commit_paths(
            working_dir, [target.relative_to(working_dir).as_posix()], message)

    def _commit_paths(self, working_dir, rel_paths, message):
        """Stage ONLY the given paths and commit them (never sweeps unrelated
        local changes into the agent's commit). Returns the commit sha, or ''
        on a non-git workspace (the change is still recorded, just uncheckpointed)."""
        if not self._is_git_repo(pathlib.Path(working_dir)):
            return ''
        task = self._current_task()
        if task:
            self.ensure_task_branch(working_dir, task)
        self._git(working_dir, 'add', '--', *rel_paths)
        prefix = f"[ai_code] task#{task.id} " if task else "[ai_code] "
        # commit only what we staged, even if the index holds other entries
        self._git(working_dir, 'commit', '--no-verify', '-m', prefix + message,
                  '--only', '--', *rel_paths)
        return self._git(working_dir, 'rev-parse', 'HEAD')

    def _record_change(self, working_dir, target, change_type, old, new, sha):
        task = self._current_task()
        if not task:
            return
        rel = target.relative_to(pathlib.Path(working_dir)).as_posix()
        self.env['ai.code.change'].sudo().create({
            'task_id': task.id,
            'file_path': rel,
            'change_type': change_type,
            'diff': self._unified_diff(old, new, rel),
            'git_sha': sha or False,
        })

    @staticmethod
    def _unified_diff(old, new, rel_path):
        diff = difflib.unified_diff(
            old.splitlines(keepends=True), new.splitlines(keepends=True),
            fromfile=f"a/{rel_path}", tofile=f"b/{rel_path}")
        return _clip(''.join(diff), 8000)

    @_tool
    def tool_git_diff(self, working_dir, base=''):
        """Diff of the task branch vs its base (or an explicit ref)."""
        working_dir = self._validate_working_dir(working_dir)
        task = self._current_task()
        ref = base or (task.branch_base_sha if task and task.branch_base_sha else 'HEAD~1')
        out = self._git(working_dir, 'diff', '--stat', ref, 'HEAD', check=False)
        return _clip(out or "No difference.")

    @_tool
    def tool_git_log(self, working_dir, limit=20):
        working_dir = self._validate_working_dir(working_dir)
        out = self._git(working_dir, 'log', '--oneline', f'-{min(int(limit or 20), 50)}',
                        check=False)
        return _clip(out or "No commits.")

    @_tool
    def tool_git_status(self, working_dir):
        """Current branch + short status (uncommitted files) of the workspace.
        Version control is optional — say so plainly if it is not a repo."""
        working_dir = self._validate_working_dir(working_dir)
        if not self._is_git_repo(working_dir):
            return "Not a git repository (version control is optional here)."
        branch = self._git(working_dir, 'rev-parse', '--abbrev-ref', 'HEAD', check=False)
        out = self._git(working_dir, 'status', '--short', '--branch', check=False)
        return _clip(f"On branch {branch}\n{out or '(working tree clean)'}")

    # ══════════════════════════════════════════════════════════════════
    # Sandbox: disposable database install-test (generate-test-repair)
    # ══════════════════════════════════════════════════════════════════

    SANDBOX_TIMEOUT = 420  # seconds per odoo-bin run

    def _sandbox_params(self):
        params = self.env['ir.config_parameter'].sudo()
        db_name = (params.get_param('ai_code.sandbox_db') or 'aicode-sandbox').strip()
        preinstall = (params.get_param('ai_code.sandbox_modules') or 'base').strip()
        if not re.fullmatch(r'[A-Za-z0-9_-]+', db_name):
            raise UserError(_("Invalid sandbox database name %r.", db_name))
        return db_name, preinstall

    def _sandbox_db_for(self, task):
        """Every task gets its OWN sandbox database (<base>-t<id>): two tasks
        or two sessions can never race on one database, and a leftover of a
        crashed run only ever affects the same task."""
        base, _pre = self._sandbox_params()
        return f"{base}-t{task.id}"[:60]

    def _pg_connect(self, dbname):
        return psycopg2.connect(
            dbname=dbname,
            host=odoo_config.get('db_host') or None,
            port=odoo_config.get('db_port') or None,
            user=odoo_config.get('db_user') or None,
            password=odoo_config.get('db_password') or None,
        )

    def _sandbox_db_exists(self, db_name):
        with self._pg_connect('postgres') as conn:
            with conn.cursor() as cr:
                cr.execute("SELECT 1 FROM pg_database WHERE datname = %s", (db_name,))
                return bool(cr.fetchone())

    def _sandbox_module_state(self, db_name, module):
        try:
            with self._pg_connect(db_name) as conn, conn.cursor() as cr:
                cr.execute("SELECT state FROM ir_module_module WHERE name = %s", (module,))
                row = cr.fetchone()
                return row[0] if row else None
        except psycopg2.Error:
            return None

    def _odoo_bin_cmd(self, db_name, addons_extra, install_flag, modules, test_enable=False):
        odoo_bin = _odoo_core_root() / 'odoo-bin'
        raw = odoo_config.get('addons_path') or ''
        # Odoo 19 stores addons_path as a list; the CLI wants a comma string
        addons_path = ",".join(raw) if isinstance(raw, (list, tuple)) else str(raw)
        if addons_extra:
            # working dir FIRST so the GENERATED module is the one installed,
            # never a same-named core/enterprise module that would shadow it
            addons_path = f"{addons_extra},{addons_path}" if addons_path else str(addons_extra)
        return [
            sys.executable, str(odoo_bin),
            '-c', odoo_config['config'],
            '-d', db_name,
            '--addons-path', addons_path,
            install_flag, modules,
            '--stop-after-init',
            '--log-level=warn',
            # Odoo hides the REAL reason for view/data ParseErrors behind
            # "Restart with --log-handler odoo.tools.convert:DEBUG". We turn it
            # on so the repair loop sees the actual cause (missing field, bad
            # xpath, unknown external id) instead of a bare ParseError line.
            '--log-handler=odoo.tools.convert:DEBUG',
            '--http-port', '8471',   # never collide with a live server if it binds
        ] + (['--test-enable', '--test-tags', '/' + modules] if test_enable else [])

    def _run_odoo_subprocess(self, cmd):
        env = dict(os.environ, PYTHONUTF8='1')
        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True, encoding='utf-8',
                errors='replace', timeout=self.SANDBOX_TIMEOUT, env=env)
        except subprocess.TimeoutExpired:
            return 124, _("sandbox run timed out after %s seconds", self.SANDBOX_TIMEOUT)
        return proc.returncode, (proc.stdout or '') + (proc.stderr or '')

    def _ensure_sandbox_db(self, db_name, preinstall):
        """Create + initialize the sandbox database once; reused afterwards.
        An existing-but-uninitialized DB (crashed earlier bootstrap) is
        initialized too — readiness means base is actually installed."""
        if not self._sandbox_db_exists(db_name):
            conn = self._pg_connect('postgres')
            try:
                conn.autocommit = True
                with conn.cursor() as cr:
                    cr.execute(psycopg2.sql.SQL("CREATE DATABASE {}").format(
                        psycopg2.sql.Identifier(db_name)))
            finally:
                conn.close()
        if self._sandbox_module_state(db_name, 'base') == 'installed':
            return _("sandbox db %s ready", db_name)
        rc, out = self._run_odoo_subprocess(
            self._odoo_bin_cmd(db_name, '', '-i', preinstall or 'base'))
        if rc != 0 or self._sandbox_module_state(db_name, 'base') != 'installed':
            raise UserError(_("Sandbox bootstrap failed (rc=%s):\n%s", rc, out[-2000:]))
        return _("sandbox db %s initialized with %s", db_name, preinstall)

    # substrings that mark a line as carrying the real cause of a failure,
    # even at DEBUG level (view/data ParseErrors log their reason this way)
    # STRONG error markers (exception class names / hard failures). Kept tight so
    # benign WARNING/INFO lines (e.g. "pdfminer.six ... cannot be found") are NOT
    # mistaken for install errors — that false-positive made a clean install look
    # failed and sent the repair loop thrashing on a warning.
    _ERROR_SIGNALS = (
        'ParseError', 'ValidationError', 'ValueError', 'KeyError', 'AttributeError',
        'TypeError', 'ImportError', 'AssertionError', 'IntegrityError', 'ProgrammingError',
        'External ID not found', 'Invalid field', 'expected singleton',
        'NotNullViolation', 'psycopg2.', 'odoo.exceptions.',
    )

    @classmethod
    def _extract_errors(cls, output):
        """Pull the meaningful failure lines out of an odoo log: ERROR/CRITICAL
        lines, full traceback blocks, and the DEBUG-level cause lines convert.py
        emits for view/data parse failures. WARNING/INFO lines are ignored even
        if they contain a signal word (they are not failures)."""
        lines = output.splitlines()
        picked, in_tb = [], False
        for line in lines:
            if 'Traceback (most recent call last)' in line:
                in_tb = True
            if in_tb:
                picked.append(line)
                # a traceback ends at the final "SomeError: message" line
                if line and not line.startswith((' ', '\t', 'Traceback', 'During', 'The above')):
                    in_tb = False
                continue
            if ' ERROR ' in line or ' CRITICAL ' in line:
                picked.append(line)
            elif ' WARNING ' in line or ' INFO ' in line:
                continue  # benign log line — never an install failure
            elif any(sig in line for sig in cls._ERROR_SIGNALS):
                picked.append(line)
        # de-dup while preserving order
        seen, out_lines = set(), []
        for line in picked:
            key = line.strip()
            if key and key not in seen:
                seen.add(key)
                out_lines.append(line)
        return out_lines

    def _files_in_log(self, working_dir, output):
        """Module files referenced in the log (e.g. '...views/x.xml:18') with
        the line number, so the repair loop can attach the offending source."""
        working_dir = pathlib.Path(working_dir).resolve()
        found = {}
        for m in re.finditer(r'([A-Za-z]:[\\/][^\s:"\']+?\.(?:py|xml|csv))(?::(\d+))?', output):
            raw, lineno = m.group(1), m.group(2)
            try:
                path = pathlib.Path(raw).resolve()
            except (OSError, ValueError):
                continue
            if working_dir not in path.parents or not path.is_file():
                continue
            rel = path.relative_to(working_dir).as_posix()
            if rel not in found and lineno:
                found[rel] = int(lineno)
            elif rel not in found:
                found[rel] = 0
        return found

    def _sandbox_orphan_modules(self, db_name, working_dir):
        """Modules installed in the sandbox whose source is not reachable from
        the CURRENT addons-path (leftovers of earlier tasks from other
        workspaces). They poison every install with load errors."""
        try:
            with self._pg_connect(db_name) as conn, conn.cursor() as cr:
                cr.execute("SELECT name FROM ir_module_module "
                           "WHERE state NOT IN ('uninstalled', 'uninstallable')")
                installed = [row[0] for row in cr.fetchall()]
        except psycopg2.Error:
            return []
        raw = odoo_config.get('addons_path') or []
        roots = [pathlib.Path(p) for p in (raw if isinstance(raw, (list, tuple))
                                           else str(raw).split(','))]
        roots.append(pathlib.Path(working_dir))
        orphans = []
        for name in installed:
            if not any((root / name / '__manifest__.py').is_file() for root in roots):
                orphans.append(name)
        return orphans

    # ── sandbox TEMPLATE cache: createdb -T beats reinstalling deps ────
    def _sandbox_template_enabled(self):
        return (self.env['ir.config_parameter'].sudo()
                .get_param('ai_code.sandbox_template_cache', '1')) not in ('0', 'False', 'off')

    def _module_depends(self, working_dir, module):
        """Direct depends of the module's manifest (transitives resolve at
        template build time). Names sanitized; the module itself excluded."""
        manifest = pathlib.Path(working_dir) / module / '__manifest__.py'
        try:
            data = ast.literal_eval(manifest.read_text(encoding='utf-8', errors='replace'))
            deps = [str(d) for d in (data.get('depends') or [])]
        except (OSError, ValueError, SyntaxError):
            deps = []
        return {d for d in deps
                if re.fullmatch(r'[A-Za-z0-9_]+', d) and d != module}

    def _template_name(self, working_dir, modules):
        key = f"{working_dir}|{','.join(sorted(modules))}"
        return 'aicode-tpl-' + hashlib.md5(key.encode('utf-8')).hexdigest()[:10]

    def _template_ready(self, tpl_name, modules):
        try:
            with self._pg_connect(tpl_name) as conn, conn.cursor() as cr:
                cr.execute("SELECT name FROM ir_module_module "
                           "WHERE name = ANY(%s) AND state = 'installed'",
                           (sorted(modules),))
                return {r[0] for r in cr.fetchall()} >= set(modules)
        except psycopg2.Error:
            return False

    def _create_db_from_template(self, db_name, tpl_name, attempts=3):
        """CREATE DATABASE db_name TEMPLATE tpl_name. The template must have no
        connections during the copy; a leftover target database (a crashed
        earlier run, or a drop that raced a lingering connection) is dropped
        and the copy retried."""
        last = None
        for attempt in range(1, attempts + 1):
            conn = self._pg_connect('postgres')
            try:
                conn.autocommit = True
                with conn.cursor() as cr:
                    cr.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                               "WHERE datname IN (%s, %s) AND pid <> pg_backend_pid()",
                               (tpl_name, db_name))
                    cr.execute(psycopg2.sql.SQL("DROP DATABASE IF EXISTS {}").format(
                        psycopg2.sql.Identifier(db_name)))
                    cr.execute(psycopg2.sql.SQL("CREATE DATABASE {} TEMPLATE {}").format(
                        psycopg2.sql.Identifier(db_name),
                        psycopg2.sql.Identifier(tpl_name)))
                return
            except psycopg2.Error as e:
                last = e
                _logger.warning("ai_code sandbox: clone %s from %s failed (attempt %s/%s): %s",
                                db_name, tpl_name, attempt, attempts, str(e).strip()[:200])
                time.sleep(1.5 * attempt)
            finally:
                conn.close()
        raise UserError(_("Sandbox database could not be created: %s", last))

    def _ensure_sandbox_template(self, working_dir, modules):
        """Get-or-build the template DB with `modules` installed. Returns the
        template name, or '' when the build failed (caller falls back to the
        legacy non-template path). Concurrent builders serialize on a PG
        advisory lock keyed by the template hash."""
        tpl = self._template_name(working_dir, modules)
        if self._sandbox_db_exists(tpl) and self._template_ready(tpl, modules):
            return tpl
        lock_key = int.from_bytes(hashlib.md5(tpl.encode()).digest()[:8],
                                  'big', signed=True)
        guard = self._pg_connect('postgres')
        try:
            guard.autocommit = True
            with guard.cursor() as cr:
                cr.execute("SELECT pg_advisory_lock(%s)", (lock_key,))
                try:
                    # another worker may have built it while we waited
                    if self._sandbox_db_exists(tpl):
                        if self._template_ready(tpl, modules):
                            return tpl
                        _logger.warning("ai_code: rebuilding incomplete template %s", tpl)
                        self._drop_sandbox_db(tpl)
                    cr.execute(psycopg2.sql.SQL("CREATE DATABASE {}").format(
                        psycopg2.sql.Identifier(tpl)))
                    rc, out = self._run_odoo_subprocess(self._odoo_bin_cmd(
                        tpl, working_dir, '-i', ','.join(sorted(modules))))
                    if rc != 0 or not self._template_ready(tpl, modules):
                        _logger.warning("ai_code: template build failed rc=%s:\n%s",
                                        rc, out[-1500:])
                        self._drop_sandbox_db(tpl)
                        return ''
                    _logger.info("ai_code: sandbox template %s ready (%s)",
                                 tpl, ', '.join(sorted(modules)))
                    return tpl
                finally:
                    cr.execute("SELECT pg_advisory_unlock(%s)", (lock_key,))
        finally:
            guard.close()

    def _drop_sandbox_db(self, db_name):
        conn = self._pg_connect('postgres')
        try:
            conn.autocommit = True
            with conn.cursor() as cr:
                cr.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                           "WHERE datname = %s AND pid <> pg_backend_pid()", (db_name,))
                cr.execute(psycopg2.sql.SQL("DROP DATABASE IF EXISTS {}").format(
                    psycopg2.sql.Identifier(db_name)))
        finally:
            conn.close()

    def sandbox_error_files(self, task, log_excerpt):
        """For a failed sandbox run, return the offending module files with a
        window around the reported error line — fed to the repair agent so it
        does not have to guess which file/line broke."""
        working_dir = self._validate_working_dir(task.target_dir)
        refs = self._files_in_log(working_dir, log_excerpt)
        blocks = []
        for rel, lineno in list(refs.items())[:4]:
            target = working_dir / rel
            try:
                lines = target.read_text(encoding='utf-8', errors='replace').splitlines()
            except OSError:
                continue
            if lineno:
                lo, hi = max(1, lineno - 12), min(len(lines), lineno + 12)
            else:
                lo, hi = 1, min(len(lines), 60)
            body = '\n'.join(f"{i:5d}\t{lines[i - 1]}" for i in range(lo, hi + 1))
            blocks.append(f"### {rel} (around line {lineno or 1})\n{body}")
        return "\n\n".join(blocks)

    # ── functional smoke: "installs" is not "works" ────────────────────
    SMOKE_BEGIN, SMOKE_END = 'AICODE_SMOKE_BEGIN', 'AICODE_SMOKE_END'

    def _odoo_shell_cmd(self, db_name, addons_extra):
        odoo_bin = _odoo_core_root() / 'odoo-bin'
        raw = odoo_config.get('addons_path') or ''
        addons_path = ",".join(raw) if isinstance(raw, (list, tuple)) else str(raw)
        if addons_extra:
            addons_path = f"{addons_extra},{addons_path}" if addons_path else str(addons_extra)
        return [
            sys.executable, str(odoo_bin), 'shell',
            '-c', odoo_config['config'],
            '-d', db_name,
            '--addons-path', addons_path,
            '--no-http', '--log-level=error',
        ]

    @classmethod
    def _smoke_script(cls, module):
        """Python source executed inside `odoo-bin shell` on the sandbox DB.
        Only no-false-positive probes: view rendering, model metadata, action
        target existence, zero-required-arg get_*_data() calls. Everything runs
        in the shell transaction and is rolled back."""
        return f'''
import inspect, json, traceback
issues = []
MOD = {module!r}
def check(where, fn):
    # each probe in its own SAVEPOINT: a failing probe (even a SQL error that
    # aborts the transaction) must not poison the probes after it
    try:
        with env.cr.savepoint():
            fn()
    except Exception:
        tb = traceback.format_exc()
        lines = [l for l in tb.strip().splitlines() if any(c.isalpha() for c in l)]
        issues.append({{'where': where, 'error': (lines[-1] if lines else tb)[:300],
                        'trace': tb[-1200:]}})
data = env['ir.model.data'].search([('module', '=', MOD)])
view_ids = [r.res_id for r in data if r.model == 'ir.ui.view']
for v in env['ir.ui.view'].browse(view_ids).exists():
    if v.model and v.model in env and not v.inherit_id:
        check('view %s (%s of %s)' % (v.xml_id or v.id, v.type, v.model),
              lambda v=v: env[v.model].get_view(v.id, v.type))
    elif v.model and v.model in env:
        check('inherited view %s on %s' % (v.xml_id or v.id, v.model),
              lambda v=v: env[v.model].get_view(None, v.type))
model_ids = [r.res_id for r in data if r.model == 'ir.model']
own_models = [im.model for im in env['ir.model'].browse(model_ids).exists()
              if im.model in env and not env[im.model]._abstract]
for name in own_models:
    M = env[name]
    check('model %s fields_get' % name, M.fields_get)
    check('model %s default_get' % name,
          lambda M=M: M.default_get(list(M._fields)))
    check('model %s search' % name, lambda M=M: M.search_count([]))
for a in env['ir.actions.act_window'].browse(
        [r.res_id for r in data if r.model == 'ir.actions.act_window']).exists():
    if a.res_model not in env:
        issues.append({{'where': 'action %s' % (a.xml_id or a.name),
                        'error': "res_model '%s' does not exist" % a.res_model,
                        'trace': ''}})
for name in own_models:
    M = env[name]
    for attr in dir(type(M)):
        if not (attr.startswith('get_') and attr.endswith('_data')):
            continue
        fn = getattr(M, attr, None)
        if not callable(fn):
            continue
        try:
            params = [p for p in inspect.signature(fn).parameters.values()
                      if p.default is p.empty
                      and p.kind == p.POSITIONAL_OR_KEYWORD and p.name != 'self']
        except (ValueError, TypeError):
            continue
        if params:
            continue     # requires arguments - not safely callable
        check('%s.%s()' % (name, attr), fn)
env.cr.rollback()
print({cls.SMOKE_BEGIN!r} + json.dumps(issues) + {cls.SMOKE_END!r})
'''

    def sandbox_smoke(self, db_name, working_dir, module):
        """Run the functional smoke in the sandbox. Returns (issues, harness_err):
        issues = list of dicts from the probes; harness_err = '' or a note when
        the smoke itself could not run (never fails the task on its own)."""
        cmd = self._odoo_shell_cmd(db_name, working_dir)
        env_vars = dict(os.environ, PYTHONUTF8='1')
        try:
            proc = subprocess.run(
                cmd, input=self._smoke_script(module), capture_output=True,
                text=True, encoding='utf-8', errors='replace',
                timeout=self.SANDBOX_TIMEOUT, env=env_vars)
        except subprocess.TimeoutExpired:
            return [], _("smoke run timed out")
        out = (proc.stdout or '') + (proc.stderr or '')
        begin = out.rfind(self.SMOKE_BEGIN)
        end = out.rfind(self.SMOKE_END)
        if begin < 0 or end <= begin:
            _logger.warning("ai_code smoke: no marker in output (rc=%s):\n%s",
                            proc.returncode, out[-1500:])
            return [], _("smoke harness produced no result (rc=%s)", proc.returncode)
        try:
            issues = json.loads(out[begin + len(self.SMOKE_BEGIN):end])
        except ValueError:
            return [], _("smoke result was not parseable")
        return issues, ''

    @staticmethod
    def _format_smoke_issues(issues):
        blocks = []
        for i in issues[:8]:
            blocks.append("SMOKE (runtime) — %s\n%s\n%s" % (
                i.get('where', '?'), i.get('error', ''), i.get('trace', '')))
        return "\n\n".join(blocks)

    def preflight_check(self, task):
        """Static cross-artifact lint of the task's module (view fields vs model
        fields, xmlid load order, access coverage, manifest completeness).
        Milliseconds instead of a sandbox round; returns '' when clean."""
        from .ai_code_preflight import preflight_scan
        working_dir = self._validate_working_dir(task.target_dir)
        module = (task.module_name or '').strip()
        module_dir = working_dir / module
        if not module or not module_dir.is_dir():
            return ''
        from odoo.modules.module import get_module_path

        def module_exists(name):
            if (working_dir / name / '__manifest__.py').is_file():
                return True   # sibling module in the same workspace
            try:
                return bool(get_module_path(name, display_warning=False))
            except Exception:  # noqa: BLE001
                return True    # resolver hiccup — never fabricate an issue
        try:
            issues = preflight_scan(module_dir, module, module_exists)
        except Exception:  # noqa: BLE001
            _logger.exception("ai_code: preflight crashed (skipping)")
            return ''
        return "\n".join(f"- {i}" for i in issues)

    def sandbox_run(self, task):
        """Install/upgrade the task's module in the sandbox DB.
        Returns (ok: bool, log_excerpt: str)."""
        working_dir = self._validate_working_dir(task.target_dir)
        module = (task.module_name or '').strip()
        if not module:
            return False, _("task has no module_name to install")
        db_name, preinstall = self._sandbox_params()

        tpl = ''
        if self._sandbox_template_enabled():
            mods = self._module_depends(working_dir, module)
            mods |= {m.strip() for m in preinstall.split(',') if m.strip()}
            mods.add('base')
            tpl = self._ensure_sandbox_template(working_dir, mods)
        if tpl:
            # fresh copy from the dep-template EVERY run into the TASK's own db:
            # ~2s, perfect isolation, no cross-task races
            db_name = self._sandbox_db_for(task)
            self._create_db_from_template(db_name, tpl)
            flag = '-i'
        else:
            # legacy path: shared sandbox DB, reset when polluted
            self._ensure_sandbox_db(db_name, preinstall)
            orphans = self._sandbox_orphan_modules(db_name, working_dir)
            if orphans:
                _logger.warning("ai_code sandbox: resetting %s (orphan modules: %s)",
                                db_name, ', '.join(orphans))
                self._drop_sandbox_db(db_name)
                self._ensure_sandbox_db(db_name, preinstall)
            state = self._sandbox_module_state(db_name, module)
            flag = '-u' if state == 'installed' else '-i'
        rc, out = self._run_odoo_subprocess(
            self._odoo_bin_cmd(db_name, working_dir, flag, module))
        errors = self._extract_errors(out)
        ok = rc == 0 and not errors
        if ok:
            state_after = self._sandbox_module_state(db_name, module)
            if state_after != 'installed':
                ok = False
                errors = [_("module state after run: %s (expected installed)", state_after)]
        if ok:
            # install GREEN — now prove it WORKS: render views, probe models,
            # call the zero-arg data methods (runtime errors install cannot see)
            smoke_issues, harness_err = self.sandbox_smoke(db_name, working_dir, module)
            if harness_err:
                _logger.warning("ai_code smoke skipped: %s", harness_err)
            elif smoke_issues:
                ok = False
                return ok, self._format_smoke_issues(smoke_issues)[-8000:]
        excerpt = "\n".join(errors)[-8000:] if errors else out[-1500:]
        return ok, excerpt

    def _revert_change(self, change):
        """Revert one step commit (button on ai.code.change / revert tool)."""
        change.ensure_one()
        if change.reverted:
            raise UserError(_("This step was already reverted."))
        if not change.git_sha:
            raise UserError(_("This step has no commit to revert."))
        task = change.task_id
        working_dir = self._validate_working_dir(task.target_dir)
        try:
            self._git(working_dir, 'revert', '--no-edit', change.git_sha)
        except UserError:
            self._git(working_dir, 'revert', '--abort', check=False)
            raise UserError(_(
                "Reverting %s conflicts with later changes. Revert the later steps "
                "first (newest to oldest).", change.file_path))
        change.write({'reverted': True})
        return True
