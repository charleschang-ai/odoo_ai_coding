# -*- coding: utf-8 -*-
"""run_code: one Python program orchestrating many READ-ONLY explorations
(search -> filter -> read -> search again) in a single tool round. Ported from
ai-lucky-star coderun.py (itself a reduced DeepSeek Harness "code mode").

Contract shown to the model:
- `tools.read_file / grep / glob / tree / outline` are plain functions
  returning strings (same jail and caps as the ordinary tools);
- only print() output and the `result` variable come back;
- a failing sub-call raises (readable message), try/except may continue;
- output shares one character budget; overflow is truncated with a notice.

Containment, not a security boundary: restricted builtins, whitelisted
stdlib modules, no writes, sub-call cap, and a wall-clock deadline enforced
by sys.settrace on the model's own frames only.
"""
import collections
import difflib
import fnmatch
import itertools
import json
import math
import re
import sys
import time
import traceback

DEADLINE_S = 120
MAX_SUBCALLS = 50
OUT_CAP = 12_000

_SAFE_MODULES = {"re": re, "json": json, "math": math, "collections": collections,
                 "itertools": itertools, "difflib": difflib, "fnmatch": fnmatch}


def _safe_import(name, *args, **kwargs):
    if name in _SAFE_MODULES:
        return _SAFE_MODULES[name]
    raise ImportError(f"module {name} is not available (available: "
                      f"{', '.join(sorted(_SAFE_MODULES))}, already injected — no import "
                      "needed). File access goes through tools.read_file.")


class _ProgramError(Exception):
    pass


class _Output:
    def __init__(self, cap):
        self.cap = cap
        self.parts = []
        self.used = 0
        self.truncated = 0

    def write(self, text):
        room = self.cap - self.used
        if room <= 0:
            self.truncated += len(text)
            return
        if len(text) > room:
            self.parts.append(text[:room])
            self.used = self.cap
            self.truncated += len(text) - room
        else:
            self.parts.append(text)
            self.used += len(text)

    def render(self):
        body = "".join(self.parts)
        if self.truncated:
            body += (f"\n... [output exceeded the {self.cap}-char budget, {self.truncated} chars "
                     "dropped — filter with re/slicing inside the program before printing]")
        return body


class _Tools:
    """Read-only bindings. `impl` maps a tool name to a callable(**kwargs)
    returning a string (the workspace tool implementations)."""

    def __init__(self, impl, state, on_call=None):
        self._impl = impl
        self._state = state
        self._on_call = on_call

    def _guard(self, name, arg):
        st = self._state
        if st.get("abort_check") and st["abort_check"]():
            raise _ProgramError("run aborted by the user")
        if time.monotonic() > st["deadline"]:
            raise _ProgramError(f"program exceeded its deadline ({st['deadline_s']}s)")
        st["calls"] += 1
        if st["calls"] > st["max_calls"]:
            raise _ProgramError(
                f"sub-call cap ({st['max_calls']}) exceeded — narrow the search first, "
                "do not enumerate")
        if self._on_call:
            self._on_call(name, str(arg or "")[:120])

    def _call(self, name, **kwargs):
        fn = self._impl.get(name)
        if fn is None:
            raise _ProgramError(f"tools.{name} is not available in run_code")
        return str(fn(**kwargs))

    def read_file(self, path, start_line=1, num_lines=200):
        self._guard("read_file", path)
        return self._call("read_file", path=path, start_line=int(start_line or 1),
                          num_lines=int(num_lines or 200))

    def grep(self, pattern, glob="**/*", max_results=60, context_lines=0):
        self._guard("grep", pattern)
        return self._call("grep", pattern=pattern, glob=glob or "**/*",
                          max_results=int(max_results or 60),
                          context_lines=int(context_lines or 0))

    def glob(self, pattern="**/*", limit=200):
        self._guard("glob", pattern)
        return self._call("glob", pattern=pattern or "**/*", limit=int(limit or 200))

    def tree(self, subdir="", depth=3):
        self._guard("tree", subdir)
        return self._call("tree", subdir=subdir or "", depth=int(depth or 3))

    def outline(self, path):
        self._guard("outline", path)
        return self._call("outline", path=path)


_SAFE_BUILTINS = {
    "abs": abs, "all": all, "any": any, "bool": bool, "dict": dict, "dir": dir,
    "enumerate": enumerate, "filter": filter, "float": float, "getattr": getattr,
    "hasattr": hasattr, "int": int, "isinstance": isinstance, "iter": iter,
    "len": len, "list": list, "map": map, "max": max, "min": min, "next": next,
    "range": range, "repr": repr, "reversed": reversed, "round": round, "set": set,
    "sorted": sorted, "str": str, "sum": sum, "tuple": tuple, "zip": zip,
    "Exception": Exception, "ValueError": ValueError, "TypeError": TypeError,
    "KeyError": KeyError, "IndexError": IndexError, "AttributeError": AttributeError,
    "RuntimeError": RuntimeError, "StopIteration": StopIteration,
    "__import__": _safe_import,
}


def run_program(code, impl, deadline_s=DEADLINE_S, max_calls=MAX_SUBCALLS,
                out_cap=OUT_CAP, abort_check=None, on_call=None):
    """Execute a read-only exploration program; every failure is normalized
    into self-correctable text (with whatever was printed before it)."""
    code = (code or "").strip()
    if not code:
        return "REJECTED: code is empty"
    if len(code) > 20_000:
        return "REJECTED: program over 20000 chars — exploration programs must be short"
    out = _Output(out_cap)
    state = {"calls": 0, "max_calls": max_calls, "deadline_s": deadline_s,
             "deadline": time.monotonic() + deadline_s, "abort_check": abort_check}

    def _print(*args, sep=" ", end="\n", **_kw):
        out.write(sep.join(str(a) for a in args) + end)

    ns = {"__builtins__": dict(_SAFE_BUILTINS, print=_print),
          "tools": _Tools(impl, state, on_call), **_SAFE_MODULES}
    t0 = time.monotonic()

    def _tracer(frame, event, arg):
        if frame.f_code.co_filename != "<run_code>":
            return None
        if event == "line":
            if time.monotonic() > state["deadline"]:
                raise _ProgramError(f"program exceeded its deadline ({deadline_s}s)")
            if abort_check and abort_check():
                raise _ProgramError("run aborted by the user")
        return _tracer

    try:
        compiled = compile(code, "<run_code>", "exec")
    except SyntaxError as e:
        return f"Program syntax error line {e.lineno}: {e.msg}\n>>> {(e.text or '').rstrip()}"
    old_trace = sys.gettrace()
    sys.settrace(_tracer)
    try:
        exec(compiled, ns)  # noqa: S102 — restricted namespace + read-only bindings
        error = ""
    except _ProgramError as e:
        error = str(e)
    except Exception as e:  # noqa: BLE001
        lineno = next((f.lineno for f in reversed(traceback.extract_tb(e.__traceback__))
                       if f.filename == "<run_code>"), "?")
        error = f"program line {lineno} raised {type(e).__name__}: {e}"
    finally:
        sys.settrace(old_trace)

    header = (f"(program {'interrupted' if error else 'finished'}: {state['calls']} sub-calls, "
              f"{time.monotonic() - t0:.1f}s)")
    body = out.render()
    result = ns.get("result")
    if result is not None and not error:
        out2 = _Output(max(400, out_cap - out.used))
        out2.write("[result] " + (result if isinstance(result, str)
                                  else json.dumps(result, ensure_ascii=False, default=str)))
        body = (body + "\n" if body else "") + out2.render()
    if error:
        return header + ("\n" + body if body else "") + \
            f"\n⚠ {error}\n(above: what was printed before the interruption; fix and retry)"
    if not body:
        return header + ("\n(the program printed nothing and set no `result` — print the "
                         "excerpts you need, otherwise you see nothing)")
    return header + "\n" + body
