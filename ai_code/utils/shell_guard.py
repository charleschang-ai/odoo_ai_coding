# -*- coding: utf-8 -*-
"""Shell command policy for the coding agent's `shell` tool.

Three verdicts (ported from ai-lucky-star guards.py, adapted to a server
that cannot block on a dialog):

  'allow'   — read-only / harmless command: runs immediately;
  'confirm' — state-changing command: the turn pauses on an approval card,
              runs only after the user clicks "allow";
  'reject'  — catastrophic, or a git mutation (the agent never mutates git:
              writes are checkpointed automatically) — never runs, even with
              auto-approve.

The checks are conservative on purpose: any shell metacharacter, any
pipe-to-interpreter, and any dangerous argument of an otherwise whitelisted
command escalate to 'confirm' or 'reject'.
"""
import re
import shlex

# commands that only READ (workspace inspection, test/lint runners)
_READONLY_PREFIXES = (
    # files / text
    'ls', 'dir', 'cat', 'type', 'head', 'tail', 'wc', 'find', 'tree', 'pwd', 'cd', 'echo',
    'grep', 'egrep', 'fgrep', 'rg', 'ag', 'findstr', 'sort', 'uniq', 'cut', 'tr', 'diff',
    'cmp', 'comm', 'sed -n', 'less', 'more', 'stat', 'file', 'du', 'md5sum', 'sha1sum',
    'sha256sum', 'realpath', 'readlink', 'basename', 'dirname', 'printf', 'test', '[',
    'true', 'false', 'xargs grep', 'xargs wc', 'xargs ls', 'xargs head',
    # system / environment (diagnostics)
    'which', 'where', 'whoami', 'id', 'hostname', 'uname', 'date', 'uptime', 'env',
    'printenv', 'set', 'mount', 'df', 'free', 'nproc', 'lscpu', 'lsblk', 'ps', 'pgrep',
    'top -b', 'netstat', 'ss', 'ip addr', 'ip route', 'ifconfig', 'ipconfig', 'ping -c',
    'ping -n', 'ver', 'systeminfo', 'tasklist', 'getconf', 'ulimit', 'locale',
    'lsb_release', 'cat /etc/os-release', 'systemctl status', 'systemctl list-units',
    'service --status-all', 'journalctl', 'dmesg', 'lsof -i',
    # python / node toolchain (inspection & static checks only)
    'python --version', 'python3 --version', 'python -V', 'python3 -V', 'py -3 --version',
    'python -m py_compile', 'python3 -m py_compile', 'python -m pyflakes', 'python3 -m pyflakes',
    'python -m flake8', 'python -m pylint', 'python -m json.tool', 'python -m pip list',
    'python -m pip show', 'python -m pip freeze', 'python -m pip check', 'python -m site',
    'pip list', 'pip show', 'pip freeze', 'pip --version', 'pip check', 'pip3 list',
    'pip3 show', 'pip3 freeze', 'ruff check', 'ruff --version', 'flake8', 'pyflakes',
    'pylint', 'mypy', 'black --check', 'isort --check', 'pytest --collect-only',
    'node --version', 'node -v', 'npm --version', 'npm ls', 'npm list', 'npm outdated',
    'npm view', 'npx eslint', 'eslint', 'prettier --check', 'tsc --noEmit',
    # odoo / postgres inspection
    'psql --version', 'pg_config', 'pg_lsclusters', 'odoo-bin --version',
    'python odoo-bin --version', 'python3 odoo-bin --version',
    # git (read-only subcommands; the git verdict function refines these)
    'git status', 'git log', 'git diff', 'git show', 'git blame', 'git branch',
    'git rev-parse', 'git ls-files', 'git grep', 'git remote -v', 'git stash list',
    'git tag', 'git describe', 'git shortlog', 'git ls-tree', 'git cat-file',
)
_GIT_READONLY = {'status', 'log', 'diff', 'show', 'blame', 'branch', 'rev-parse',
                 'ls-files', 'grep', 'remote', 'stash', 'tag', 'describe', 'shortlog',
                 'cat-file', 'ls-tree', 'config'}
_GIT_ALLOWED_ARGS = {'branch': ('', '-a', '-v', '-vv', '--list', '-l', '-r', '--show-current'),
                     'stash': ('list', 'show'), 'remote': ('', '-v', 'show', 'get-url'),
                     'tag': ('', '-l', '--list'), 'config': ('--get', '--list', '-l', 'user.name', 'user.email')}
_SYSTEM_ROOTS = r'(/|/\*|~|\$HOME|[A-Za-z]:\\?|/(etc|usr|bin|sbin|lib|var|home|root|boot|opt)(/\S*)?|[A-Za-z]:\\(Windows|Users|Program Files)[^\s]*)'
_CATASTROPHIC = [
    # recursive delete of the filesystem root, home, a drive or a system directory
    r'\brm\s+-[a-zA-Z]*r[a-zA-Z]*\s+(-[a-zA-Z]+\s+)*' + _SYSTEM_ROOTS + r'(\s|$)',
    r'\bdel\s+/[sS]\b.*\\\s*$', r'\brd\s+/[sS]\s+[A-Za-z]:\\?\s*$',
    r'\bformat\s+[A-Za-z]:', r'\bmkfs\b', r'\bdd\s+if=', r'\bshutdown\b', r'\breboot\b',
    r'\bsystemctl\s+(stop|disable|restart)', r'\breg\s+(add|delete)', r'\bregedit\b',
    r'\bnet\s+user\b', r'\bschtasks\b', r'\biex\b', r'\bInvoke-Expression\b',
    r'\bcurl\b.*\|\s*(sh|bash|python|powershell)', r'\bwget\b.*\|\s*(sh|bash|python)',
    r':\(\)\s*\{', r'\bchmod\s+-R\s+777\s+/', r'\bchown\s+-R\b.*\s/\s*$',
    r'\bdropdb\b', r'\bpsql\b.*\b(drop|delete)\b', r'\bDROP\s+DATABASE\b',
]
_DANGEROUS_ARGS = [
    # `mount` alone lists mounts; with any argument it (re)mounts something
    (r'^mount\b', r'^mount\s+(?!-l\b|\|)\S'),
    (r'^(ip|ifconfig)\b', r'\s(set|add|del|delete|up|down|flush)\b'),
    (r'^(systemctl|service)\b', r'\s(start|stop|restart|reload|enable|disable|mask)\b'),
    (r'^find\b', r'\s-(delete|exec|execdir|ok)\b'),
    (r'^(rg|grep)\b', r'\s--pre\b'),
    (r'^git\b', r'\s--(output|ext-diff|textconv)\b'),
    (r'^git\s+config\b', r'\s(--global|--system|alias\.|core\.(pager|editor|sshcommand|fsmonitor)|credential\.)'),
    (r'^(python|python3|node)\b', r'\s-c\b'),
]
# chaining / piping between read-only commands is harmless; what escalates is
# REDIRECTION (writes a file), command substitution and backgrounding
_UNSAFE_META = re.compile(r'([<>`]|\$\(|(?<![&|])&(?![&|]))')
_CHAIN = re.compile(r'(&&|\|\||\||;)')
_GIT_MUTATING = {'add', 'commit', 'checkout', 'switch', 'merge', 'rebase', 'push', 'pull',
                 'fetch', 'reset', 'revert', 'cherry-pick', 'rm', 'mv', 'clean', 'restore',
                 'apply', 'am', 'init', 'clone', 'submodule', 'worktree', 'gc', 'prune',
                 'reflog', 'filter-branch', 'update-ref', 'symbolic-ref'}


def _segments(command):
    """Split on connectors; strip wrappers (cmd /c, sh -c, quotes) so the
    real command of each segment is inspected."""
    parts = re.split(r'&&|\|\||\||;|\n', command)
    out = []
    for part in parts:
        p = part.strip()
        p = re.sub(r'^(cmd(\.exe)?\s+/[cCkK]\s+|(sh|bash|zsh)\s+-c\s+|powershell(\.exe)?\s+(-Command\s+)?)', '', p)
        p = p.strip().strip('"').strip("'").strip()
        if p:
            out.append(p)
    return out


def _git_verdict(seg):
    try:
        toks = shlex.split(seg, posix=True)
    except ValueError:
        toks = seg.split()
    args = toks[1:]
    # skip global flags: -C <dir>, -c k=v, --no-pager, --git-dir=...
    i = 0
    while i < len(args) and args[i].startswith('-'):
        if args[i] in ('-C', '-c'):
            i += 2
        else:
            i += 1
    sub = args[i] if i < len(args) else ''
    rest = args[i + 1:]
    if sub in _GIT_MUTATING:
        return 'reject', (f"git {sub} is not allowed: you never mutate git — every file you "
                          "save is already checkpointed; the user commits from the UI.")
    if sub in _GIT_READONLY:
        allowed = _GIT_ALLOWED_ARGS.get(sub)
        if allowed is not None:
            first = rest[0] if rest else ''
            if not any(first == a or (a and first.startswith(a)) for a in allowed):
                return 'confirm', f"git {sub} {first} may mutate state"
        return 'allow', ''
    return 'confirm', f"git {sub or '(no subcommand)'} is not in the read-only set"


def classify(command):
    """-> (verdict, reason) with verdict in allow | confirm | reject."""
    cmd = (command or '').strip()
    if not cmd:
        return 'reject', 'empty command'
    if len(cmd) > 2000:
        return 'reject', 'command too long'
    low = cmd.lower()
    for pat in _CATASTROPHIC:
        if re.search(pat, cmd, re.IGNORECASE):
            return 'reject', f"catastrophic command pattern: /{pat}/"
    segments = _segments(cmd)
    verdict = 'allow'
    reasons = []
    # discarding stderr / merging streams writes nothing: not a redirection that matters
    harmless = re.sub(r'\s2>&1|\s[12]?>\s*(/dev/null|NUL)\b', ' ', cmd, flags=re.IGNORECASE)
    if _UNSAFE_META.search(harmless):
        verdict = 'confirm'
        reasons.append('redirection / command substitution / backgrounding')
    for seg in segments:
        seg_low = seg.lower()
        if re.match(r'^git\b', seg_low):
            v, r = _git_verdict(seg)
            if v == 'reject':
                return 'reject', r
            if v == 'confirm':
                verdict = 'confirm'
                reasons.append(r)
            continue
        for head_pat, arg_pat in _DANGEROUS_ARGS:
            if re.match(head_pat, seg_low) and re.search(arg_pat, seg_low):
                verdict = 'confirm'
                reasons.append(f"dangerous argument in '{seg[:40]}'")
        if not any(seg_low == p or seg_low.startswith(p + ' ') for p in _READONLY_PREFIXES):
            verdict = 'confirm'
            reasons.append(f"'{seg.split()[0]}' is not a read-only command")
    return verdict, '; '.join(dict.fromkeys(reasons))
