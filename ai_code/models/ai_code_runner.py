# -*- coding: utf-8 -*-
"""Task runner: one coding session per conversation, one ReAct turn per user
message, no approval gates.

    user message -> (system prompt + persistent history) -> rounds of
    LLM call -> tool calls -> results ... until the model answers without
    tools (turn done), asks the user (turn paused), hits the round cap, or
    the user presses Stop.

The runner is invoked INSIDE the platform's background response thread
(ai.agent.bot._ai_spawn_response_job), so nothing here blocks an HTTP
request; progress is made visible by committing after every round and by
posting assistant text / activity events to the channel as they happen.
"""
import datetime
import json
import logging
import os
import pathlib
import sys
import time

from odoo import api, fields, models, _
from odoo.exceptions import UserError

from odoo.addons.ai_chatbot_enhance.utils.llm_providers import get_provider, get_provider_display_name
from odoo.addons.ai_chatbot_enhance.utils.ai_logging import (
    ai_response_logging, get_ai_logging_session)

from ..utils.coding_llm_service import CodingLLMService, TaskAborted
from . import ai_code_prompts as prompts
from .ai_code_tools import build_toolset, TurnContext, READONLY_TOOLS, WRITE_TOOLS

_logger = logging.getLogger(__name__)

DEFAULT_MAX_ROUNDS = 60
MAX_NUDGES = 2
MAX_LANGUAGE_NUDGES = 3     # language-drift reminders per turn
EXPLORE_NUDGE_AT = 18       # read-only calls without a write before the first nudge
EXPLORE_NUDGE_EVERY = 12    # ... and again every N further reads

# executed by `odoo-bin shell` in the detached turn process (`env` is the
# shell's superuser environment; we rebuild one for the real user)
_PROCESS_TURN_SCRIPT = '''
import traceback
from odoo import api
_env = api.Environment(env.cr, {uid}, {{'ai_background_job': True}})
agent = _env['ai.agent.bot'].browse({agent_id}).sudo()
channel = _env['discuss.channel'].browse({channel_id})
msg = _env['mail.message'].sudo().browse({message_id})
member = channel.sudo().channel_member_ids.filtered(lambda m: m.partner_id == agent.partner_id)
try:
    try:
        member._notify_typing(True)
        env.cr.commit()
    except Exception:
        traceback.print_exc()
    text = agent._html_to_text(msg.body)
    _env['ai.code.runner'].run_spawned_turn({task_id}, text, agent)
    env.cr.commit()
except Exception:
    traceback.print_exc()
    env.cr.rollback()
finally:
    try:
        member._notify_typing(False)
        env.cr.commit()
    except Exception:
        pass
'''
STALE_SECONDS = 180          # a running task with no heartbeat for this long is dead
ACTIVE_STATES = ('running',)
SESSION_OVER = ('cancelled',)


class _Streamer:
    """Batches content deltas of one LLM call into bus pushes (`ai_code/stream`)
    so the workspace shows the answer as it is generated. Each push commits
    the runner's own cursor (bus notifications only leave on commit)."""
    FLUSH_CHARS = 48
    FLUSH_SECONDS = 0.35

    def __init__(self, runner, task):
        self.runner = runner
        self.task = task
        self.channel = task.channel_id
        self.buffer = ''
        self.seq = 0
        self.stream_id = 0
        self.last_flush = 0.0
        self.active = False

    def delta(self, text):
        if not self.active:
            self.active = True
            self.stream_id += 1
            self.seq = 0
        self.buffer += text
        now = time.monotonic()
        if len(self.buffer) >= self.FLUSH_CHARS or now - self.last_flush >= self.FLUSH_SECONDS:
            self.flush()

    def flush(self):
        if not self.buffer:
            return
        self.seq += 1
        self._send({'kind': 'delta', 'stream': self.stream_id, 'seq': self.seq, 'text': self.buffer})
        self.buffer = ''
        self.last_flush = time.monotonic()

    def end(self):
        if not self.active:
            return
        self.flush()
        self._send({'kind': 'end', 'stream': self.stream_id, 'seq': self.seq + 1})
        self.active = False

    def _send(self, payload):
        try:
            data = dict(payload, task_id=self.task.id, channel_id=self.channel.id)
            self.channel.sudo()._bus_send('ai_code/stream', data)
            self.runner.env.cr.commit()
        except Exception:  # noqa: BLE001 — cosmetic
            _logger.debug("ai_code: stream push failed", exc_info=True)


class AICodeRunner(models.AbstractModel):
    _name = 'ai.code.runner'
    _description = 'AI Code Task Runner'

    # ══════════════════════════════════════════════════════════════════
    # Entry points
    # ══════════════════════════════════════════════════════════════════

    def get_session_task(self, channel):
        """The coding session of a conversation (latest non-cancelled task)."""
        return self.env['ai.code.task'].sudo().search(
            [('channel_id', '=', channel.id), ('state', 'not in', SESSION_OVER)],
            order='id desc', limit=1)

    def handle_message(self, agent, channel, text, mail_message=None):
        """A user message in a coding conversation (called in the platform's
        background response thread)."""
        text = (text or '').strip()
        if not text:
            return False
        working_dir = channel.sudo().ai_code_working_dir
        if not working_dir:
            self._post_raw(agent, channel, _(
                "Pick a working directory for this conversation first (the folder "
                "selector at the top), then tell me what to build."))
            return False
        task = self.get_session_task(channel)
        if task and task.state == 'running' and not self._is_stale(task):
            # should not happen (the agent queues messages while running) — be safe
            self._queue_text(task, text)
            return True
        if not task:
            task = self.env['ai.code.task'].sudo().create({
                'target_dir': working_dir,
                'requirement': text,
                'channel_id': channel.id,
                'user_id': self.env.user.id,
                'main_model': agent._coding_main_model(),
                'cheap_model': self.env['ir.config_parameter'].sudo().get_param(
                    'ai_code.default_cheap_model') or False,
                'state': 'idle',
            })
        try:
            self._run_turn(task, text, agent)
        except Exception as e:  # noqa: BLE001 — a setup bug must not leave the task 'running'
            _logger.exception("ai_code: turn setup failed")
            self._fail(task, f"{type(e).__name__}: {e}")
        return True

    # ── process executor (optional): the turn runs in a detached odoo-bin
    #    shell process, immune to HTTP-worker recycling ─────────────────
    def spawn_process_turn(self, agent, channel, mail_message):
        """Called in the HTTP request. Creates/marks the session task as
        running (so a second message queues instead of starting a second
        turn) and launches the detached process. Returns False when the
        process could not be started (caller falls back to the thread)."""
        import subprocess
        from odoo.tools import config as odoo_config
        from .ai_code_workspace import _odoo_core_root
        text = agent._html_to_text(mail_message.body)
        working_dir = channel.sudo().ai_code_working_dir
        if not text or not working_dir:
            return False
        task = self.get_session_task(channel)
        if not task:
            task = self.env['ai.code.task'].sudo().create({
                'target_dir': working_dir, 'requirement': text, 'channel_id': channel.id,
                'user_id': self.env.user.id, 'main_model': agent._coding_main_model(),
                'cheap_model': self.env['ir.config_parameter'].sudo().get_param(
                    'ai_code.default_cheap_model') or False, 'state': 'idle'})
        task.sudo().write({'state': 'running', 'heartbeat': fields.Datetime.now(),
                           'abort_flag': False})
        odoo_bin = _odoo_core_root() / 'odoo-bin'
        if not odoo_bin.is_file():
            _logger.warning("ai_code: odoo-bin not found, process executor unavailable")
            return False
        log_dir = pathlib.Path(odoo_config['data_dir']) / 'ai_code'
        try:
            log_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            log_dir = pathlib.Path(odoo_config['data_dir'])
        log_path = log_dir / f"turn_{task.id}_{task.turn_count + 1}.log"
        cmd = [sys.executable, str(odoo_bin), 'shell', '-c', odoo_config['config'],
               '-d', self.env.cr.dbname, '--no-http', '--log-level=warn']
        script = _PROCESS_TURN_SCRIPT.format(
            uid=self.env.uid, agent_id=agent.id, channel_id=channel.id,
            message_id=mail_message.id, task_id=task.id)
        popen_kwargs = {}
        if os.name == 'nt':
            popen_kwargs['creationflags'] = (subprocess.DETACHED_PROCESS
                                             | subprocess.CREATE_NEW_PROCESS_GROUP)
        else:
            popen_kwargs['start_new_session'] = True
        try:
            log = open(log_path, 'ab')
            proc = subprocess.Popen(
                cmd, stdin=subprocess.PIPE, stdout=log, stderr=subprocess.STDOUT,
                cwd=str(odoo_bin.parent), env=dict(os.environ, PYTHONUTF8='1'), **popen_kwargs)
            proc.stdin.write(script.encode('utf-8'))
            proc.stdin.close()
        except OSError:
            _logger.exception("ai_code: could not spawn the turn process")
            task.sudo().write({'state': 'idle'})
            return False
        _logger.info("ai_code: turn of task %s runs in process %s (log %s)", task.id, proc.pid, log_path)
        return True

    def run_spawned_turn(self, task_id, text, agent):
        """Entry point inside the spawned process (task already marked running)."""
        task = self.env['ai.code.task'].sudo().browse(task_id)
        try:
            self._run_turn(task, text, agent)
        except Exception as e:  # noqa: BLE001
            _logger.exception("ai_code: spawned turn failed")
            self._fail(task, f"{type(e).__name__}: {e}")

    def _queue_text(self, task, text):
        """A message that arrived while a turn is running: the loop picks it up
        before its next LLM call (steering), the user gets an instant ack."""
        pending = (task.pending_user_text or '').strip()
        task.sudo().write({'pending_user_text': (pending + "\n" + text).strip()})
        self._post(task, _("📥 Got it — I'll take this into account after the current step."))

    def stop(self, task):
        task.sudo().write({'abort_flag': True})

    # ══════════════════════════════════════════════════════════════════
    # One turn
    # ══════════════════════════════════════════════════════════════════

    def _run_turn(self, task, text, agent):
        now = fields.Datetime.now()
        turn = (task.turn_count or 0) + 1
        task.sudo().write({
            'state': 'running', 'abort_flag': False, 'error_message': False,
            'error_ack': False, 'heartbeat': now, 'turn_count': turn,
            'started_at': task.started_at or now, 'pending_user_text': False,
        })
        if not task.odoo_version:
            task.sudo().write({'odoo_version': self.env['ai.code.workspace'].detect_odoo_version(
                task.target_dir, task.module_name or None)})
        self._journal(task, 'message', role='user', content=text, turn=turn)
        self._journal(task, 'phase_change', content=f"turn {turn} start", turn=turn)
        self.env.cr.commit()   # the running state must be visible right away

        ws = self.env['ai.code.workspace'].with_context(ai_code_task_id=task.id)
        provider = get_provider(self.env, task.main_model)
        if not provider:
            self._fail(task, _("Model %s is not available on any configured provider. "
                               "Configure a model in Settings > AI Chatbot > AI Code.",
                               task.main_model))
            return
        ctx = TurnContext()
        turn_start = fields.Datetime.now()
        ctx.abort_check = lambda: self._abort_requested(task, turn_start, raise_=False)
        tools = build_toolset(self, task, ctx)
        system_prompt = self._system_prompt(task, ws)

        history = dict(task.history_json or {})
        first_user = history.get('first_user')
        inputs = list(history.get('inputs') or [])
        # ai_background_job + discuss_channel + ai_en_agent_id let the platform
        # service refresh the typing indicator between rounds
        streamer = self._make_streamer(task)
        service = CodingLLMService(self.with_context(
            ai_background_job=True, discuss_channel=task.channel_id,
            ai_en_agent_id=agent.id if agent else False).env, provider,
            on_delta=streamer.delta if streamer else None)

        if task.pending_call_id:
            # the user's reply resolves the tool call that paused the last turn
            pending = dict(task.pending_action_json or {})
            task.sudo().write({'pending_call_id': False, 'pending_action_json': False})
            if pending.get('kind') == 'shell':
                result = self._resolve_shell_approval(task, ws, pending, text)
            else:
                result = f"User answered:\n{text}"
            inputs.append(service._build_tool_call_response(task.pending_call_id or
                                                            pending.get('call_id'), result))
        elif first_user is None:
            first_user = text
        else:
            inputs.append({"role": "user", "content": text})
        self._persist_history(task, first_user, inputs)

        state = {'last_text': '', 'rounds': 0}
        user_lang = self.env.user.lang

        def on_text(t):
            state['last_text'] = t
            if streamer:
                streamer.end()          # the real message replaces the live draft
            self._journal(task, 'message', role='assistant', content=t, turn=turn)
            self._post(task, t)
            # language drift guard: long tool-heavy turns pull the model's prose
            # into English (tool output and nudges are English). The message is
            # already out; the next round gets a reminder (capped per turn).
            if not prompts.text_matches_language(t, user_lang) \
                    and state.get('lang_nudges', 0) < MAX_LANGUAGE_NUDGES:
                state['lang_nudge_due'] = True

        def on_tool_start(name, args):
            summary = self._args_summary(name, args)
            ev = self._journal(task, 'tool_call', role='assistant', content=f"{name}({summary})",
                               tool_name=name, status='running', turn=turn)
            state['current_event'] = ev.id
            self._beat(task, commit=True)
            self._notify(task, {'kind': 'tool_start', 'name': name, 'summary': summary})
            service._keep_typing_alive()

        def on_tool_result(name, result, error, ms):
            state['tool_calls'] = state.get('tool_calls', 0) + 1
            # exploration-spiral guard: count read-only calls since the last write
            if name in WRITE_TOOLS:
                state['reads_since_write'] = 0
            elif name in READONLY_TOOLS:
                state['reads_since_write'] = state.get('reads_since_write', 0) + 1
            ev_id = state.pop('current_event', None)
            if ev_id:
                self.env['ai.code.event'].sudo().browse(ev_id).write({
                    'status': 'error' if error else 'ok', 'duration_ms': ms})
            self._journal(task, 'observation', role='tool', content=result, tool_name=name,
                          status='error' if error else 'ok', duration_ms=ms, turn=turn)
            self._notify(task, {'kind': 'tool_result', 'name': name,
                                'status': 'error' if error else 'ok', 'ms': ms})

        def on_round(round_no, has_tools):
            state['rounds'] = round_no
            self._persist_history(task, first_user, inputs)
            self._beat(task, commit=True)
            self._notify(task, {'kind': 'round', 'round': round_no})

        def inbox():
            items = []
            if state.pop('lang_nudge_due', False):
                state['lang_nudges'] = state.get('lang_nudges', 0) + 1
                nudge = prompts.NUDGE_LANGUAGE.format(lang=prompts.language_name(user_lang))
                self._journal(task, 'message', role='system', content=nudge, turn=turn)
                items.append(nudge)
            reads = state.get('reads_since_write', 0)
            if reads >= EXPLORE_NUDGE_AT and state.get('explore_nudged_at', 0) < reads - EXPLORE_NUDGE_EVERY + 1:
                state['explore_nudged_at'] = reads
                nudge = prompts.NUDGE_EXPLORING.format(count=reads) + prompts.language_tail(user_lang)
                self._journal(task, 'message', role='system', content=nudge, turn=turn)
                items.append(nudge)
            task.invalidate_recordset(['pending_user_text'])
            pending = (task.pending_user_text or '').strip()
            if pending:
                task.sudo().write({'pending_user_text': False})
                self._journal(task, 'message', role='user', content=pending, turn=turn)
                items.append(f"[User message received while you were working — take it into account]\n{pending}")
            return items

        def check_continue():
            self._abort_requested(task, turn_start, raise_=True)

        max_rounds = int(self.env['ir.config_parameter'].sudo().get_param(
            'ai_code.max_rounds', DEFAULT_MAX_ROUNDS) or DEFAULT_MAX_ROUNDS)
        status, stop = 'done', None
        try:
            with ai_response_logging(task.main_model):
                try:
                    status, stop = service.run_turn(
                        task.main_model, [system_prompt], first_user, inputs, tools,
                        max_rounds=max_rounds, on_text=on_text, on_tool_start=on_tool_start,
                        on_tool_result=on_tool_result, on_round=on_round,
                        check_continue=check_continue, inbox=inbox)
                    nudges = 0
                    while status == 'done' and nudges < MAX_NUDGES:
                        nudge = self._nudge_text(task, ctx, state['last_text'], text)
                        if not nudge:
                            break
                        nudge += prompts.language_tail(user_lang)
                        nudges += 1
                        self._journal(task, 'message', role='system', content=nudge, turn=turn)
                        inputs.append({"role": "user", "content": nudge})
                        status, stop = service.run_turn(
                            task.main_model, [system_prompt], first_user, inputs, tools,
                            max_rounds=max(8, max_rounds // 2), on_text=on_text,
                            on_tool_start=on_tool_start, on_tool_result=on_tool_result,
                            on_round=on_round, check_continue=check_continue, inbox=inbox)
                finally:
                    self._accumulate_usage(task)
                    task.sudo().write({'tool_calls': task.tool_calls + state.get('tool_calls', 0)})
                    self._persist_history(task, first_user, inputs)
                    self.env.cr.commit()
        except TaskAborted:
            if streamer:
                streamer.end()
            task.sudo().write({'state': 'idle', 'abort_flag': False})
            self._journal(task, 'phase_change', content=f"turn {turn} stopped", turn=turn)
            self._post(task, _("⏹ Stopped. Tell me how to continue whenever you like."))
            self.env.cr.commit()
            return
        except UserError as e:
            if streamer:
                streamer.end()
            self._fail(task, str(e.args[0] if e.args else e))
            return
        except Exception as e:  # noqa: BLE001
            _logger.exception("ai_code: turn failed")
            if streamer:
                streamer.end()
            self._fail(task, f"{type(e).__name__}: {e}")
            return

        if status == 'paused' and stop is not None:
            if stop.reason == 'confirm_shell':
                pending = dict(stop.payload, kind='shell', call_id=stop.call_id)
                task.sudo().write({'state': 'waiting_user', 'pending_call_id': stop.call_id,
                                   'pending_action_json': pending})
                self._journal(task, 'phase_change', content=f"turn {turn} paused (shell approval)",
                              turn=turn)
                self._post(task, self._render_shell_approval(pending))
            else:
                task.sudo().write({'state': 'waiting_user', 'pending_call_id': stop.call_id,
                                   'pending_action_json': {'kind': 'question', 'call_id': stop.call_id}})
                self._journal(task, 'phase_change', content=f"turn {turn} paused (question)", turn=turn)
                self._post(task, self._render_questions(stop.payload.get('questions') or []))
        elif status == 'max_rounds':
            task.sudo().write({'state': 'idle'})
            self._post(task, _("⚠️ I reached the round limit for one turn (%s rounds). The work so "
                               "far is saved; say \"continue\" and I'll pick up where I stopped.",
                               max_rounds))
        else:
            task.sudo().write({'state': 'idle'})
            self._journal(task, 'phase_change', content=f"turn {turn} done", turn=turn)
        self.env.cr.commit()
        self._notify(task, {'kind': 'turn_end', 'state': task.state})
        if ctx.changed_files:
            self._distill_memory(task, ctx, text)

    # ══════════════════════════════════════════════════════════════════
    # Nudges: a plan is not a deliverable
    # ══════════════════════════════════════════════════════════════════

    def _nudge_text(self, task, ctx, last_text, user_text):
        """Return a system nudge when the model stopped with work visibly
        left, or None when the turn ended legitimately."""
        last = (last_text or '').strip()
        if '?' in last or '？' in last:
            return None            # it asked something in prose — let the user answer
        todos = task.todo_json or []
        pending = [t for t in todos if t.get('status') != 'completed']
        if todos and pending:
            return prompts.NUDGE_CONTINUE.format(pending=len(pending))
        return None

    # ══════════════════════════════════════════════════════════════════
    # Prompt assembly
    # ══════════════════════════════════════════════════════════════════

    def _system_prompt(self, task, ws):
        try:
            scan = ws.scan_workspace(task.target_dir)
            modules = [m['name'] for m in scan['modules']]
            is_git = bool(scan['git'].get('is_git'))
        except Exception:  # noqa: BLE001
            modules, is_git = [], False
        version = task.odoo_version or ws._server_odoo_series()
        edition = 'enterprise' if ws.module_exists('web_enterprise') else 'community'
        memories = self.env['ai.code.memory'].top_for_injection(task.target_dir)
        provider = get_provider(self.env, task.main_model)
        model_name = (f"{task.main_model} ({get_provider_display_name(provider)})"
                      if provider else task.main_model)
        return prompts.build_system_prompt(
            working_dir=task.target_dir, is_git=is_git, odoo_version=version,
            edition=edition, modules=modules, user_lang=self.env.user.lang,
            memories=memories, sandbox_ok=ws.sandbox_available(),
            dev_reload=ws.server_dev_reload(), model_name=model_name)

    # ══════════════════════════════════════════════════════════════════
    # Persistence / journaling / notifications
    # ══════════════════════════════════════════════════════════════════

    def _persist_history(self, task, first_user, inputs):
        task.sudo().write({'history_json': {'first_user': first_user, 'inputs': inputs}})

    def _set_todos(self, task, todos):
        task.sudo().write({'todo_json': todos})
        self._journal(task, 'summary', role='assistant',
                      content="todo: " + json.dumps(todos, ensure_ascii=False))
        self.env.cr.commit()
        self._notify(task, {'kind': 'todos'})

    def _next_seq(self, task):
        last = self.env['ai.code.event'].sudo().search(
            [('task_id', '=', task.id)], order='seq desc', limit=1)
        return (last.seq + 1) if last else 1

    def _journal(self, task, event_type, role=None, content=None, tool_name=None, status=None,
                 duration_ms=None, turn=None, pinned=False):
        vals = {
            'task_id': task.id, 'seq': self._next_seq(task), 'phase': task.state,
            'role': role, 'event_type': event_type, 'pinned': pinned,
            'tool_name': tool_name, 'status': status, 'duration_ms': duration_ms,
            'turn': turn if turn is not None else task.turn_count,
        }
        if content is not None:
            stub, full = self._maybe_stub(content)
            vals['content'] = stub
            vals['content_full'] = full
            vals['token_est'] = len(stub or '') // 3
        return self.env['ai.code.event'].sudo().create(vals)

    @staticmethod
    def _maybe_stub(content):
        content = '' if content is None else str(content)
        if len(content) > 4000:
            return (content[:1500] + f"\n... [stub: {len(content)} chars, full text stored]"), content
        return content, False

    @staticmethod
    def _args_summary(name, args):
        args = args or {}
        if name in ('write_file', 'edit_file', 'multi_edit', 'read_file', 'outline'):
            return str(args.get('path', ''))[:120]
        if name == 'grep':
            return f"/{str(args.get('pattern', ''))[:60]}/ {str(args.get('glob') or '')[:40]}"
        if name == 'todo_write':
            todos = args.get('todos') or []
            return f"{len(todos)} items" if isinstance(todos, list) else ''
        if name == 'run_code':
            return f"{len(str(args.get('code', '')))} chars"
        if name == 'ask_user_question':
            qs = args.get('questions') or []
            return f"{len(qs)} question(s)" if isinstance(qs, list) else ''
        try:
            text = json.dumps(args, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            text = str(args)
        return text[:140]

    def _notify(self, task, payload):
        """Bus push to the conversation members so the workspace refreshes at
        once (polling is the fallback)."""
        channel = task.channel_id
        if not channel:
            return
        try:
            data = dict(payload, task_id=task.id, state=task.state, channel_id=channel.id)
            channel.sudo()._bus_send('ai_code/activity', data)
        except Exception:  # noqa: BLE001 — cosmetic
            _logger.debug("ai_code: bus notify failed", exc_info=True)

    def _accumulate_usage(self, task):
        session = get_ai_logging_session()
        if not session:
            return
        Usage = self.env['ai.token.usage'].sudo()
        input_tokens = int(session.get('real_input_tokens') or 0)
        output_tokens = int(session.get('output_tokens') or 0)
        cached = int(session.get('cached_tokens') or 0)
        cost = Usage._compute_cost_amount(task.main_model, input_tokens, cached, output_tokens)
        task.sudo().write({
            'input_tokens': task.input_tokens + input_tokens,
            'cached_tokens': task.cached_tokens + cached,
            'output_tokens': task.output_tokens + output_tokens,
            'api_calls': task.api_calls + int(session.get('api_calls') or 0),
            'tool_calls': task.tool_calls + int(session.get('tool_calls') or 0),
            'cost': task.cost + (cost or 0.0),
            'duration': task.duration + (time.perf_counter() - session.get('start_time', time.perf_counter())),
        })

    def _abort_requested(self, task, turn_start, raise_=True):
        task.invalidate_recordset(['abort_flag', 'token_budget', 'input_tokens', 'output_tokens'])
        stopped = bool(task.abort_flag)
        if not stopped and task.channel_id:
            try:
                stopped = task.channel_id.sudo()._ai_stop_requested_since(turn_start)
            except Exception:  # noqa: BLE001
                stopped = False
        if not stopped and task.token_budget and \
                (task.input_tokens + task.output_tokens) > task.token_budget:
            self._post(task, _("⏹ Token budget reached — stopping."))
            stopped = True
        if stopped and raise_:
            raise TaskAborted()
        return stopped

    def _beat(self, task, commit=False):
        task.sudo().write({'heartbeat': fields.Datetime.now()})
        if commit:
            self.env.cr.commit()

    @staticmethod
    def _is_stale(task):
        hb = task.heartbeat or task.write_date
        return not hb or (fields.Datetime.now() - hb).total_seconds() > STALE_SECONDS

    @api.model
    def reap_stale_tasks(self, minutes=3):
        """A running task whose worker died (server restart / worker recycle)
        goes back to idle with a note, so it never spins forever."""
        cut = fields.Datetime.now() - datetime.timedelta(minutes=minutes)
        stale = self.env['ai.code.task'].sudo().search([
            ('state', '=', 'running'),
            '|', '&', ('heartbeat', '!=', False), ('heartbeat', '<', fields.Datetime.to_string(cut)),
                 '&', ('heartbeat', '=', False), ('write_date', '<', fields.Datetime.to_string(cut)),
        ])
        for task in stale:
            task.write({'state': 'idle', 'abort_flag': False,
                        'error_message': _("The run was interrupted (server restart or crash). "
                                           "Send a message to continue.")})
            try:
                self._post(task, _("⚠️ My previous run was interrupted (server restart or "
                                   "crash). Everything already saved is kept — send a message "
                                   "and I'll continue."))
            except Exception:  # noqa: BLE001
                pass
        return len(stale)

    def _fail(self, task, message):
        task.sudo().write({'state': 'failed', 'error_message': (message or '')[:2000],
                           'abort_flag': False})
        self._journal(task, 'phase_change', content=f"turn failed: {message}"[:2000])
        self._post(task, _("❌ I hit an error: %s\n\nEverything saved so far is kept. Send a "
                           "message to continue.", message))
        self.env.cr.commit()

    # ── chat posting ───────────────────────────────────────────────────
    def _post(self, task, body):
        if not body or not task.channel_id:
            return
        agent = self.env.ref('ai_code.coding_agent', raise_if_not_found=False)
        self._post_raw(agent, task.channel_id, body)

    def _post_raw(self, agent, channel, body):
        if agent:
            agent.sudo()._post_ai_response(channel, body)
        else:
            channel.sudo().message_post(body=body, message_type='comment',
                                        subtype_xmlid='mail.mt_comment', silent=True)
        try:
            self.env.cr.commit()      # the message must reach the bus now, not at turn end
        except Exception:  # noqa: BLE001
            pass

    # ── token streaming to the workspace UI ────────────────────────────
    def _make_streamer(self, task):
        params = self.env['ir.config_parameter'].sudo()
        if params.get_param('ai_code.streaming', '1') in ('0', 'False', 'off') or not task.channel_id:
            return None
        return _Streamer(self, task)

    # ── shell approval card ────────────────────────────────────────────
    def _render_shell_approval(self, pending):
        from .ai_code_tools import SHELL_ALLOW_LABEL, SHELL_DENY_LABEL
        lines = [_("🖥️ I'd like to run this command in the workspace:"),
                 f"```\n{pending.get('command', '')}\n```"]
        if pending.get('why'):
            lines.append(_("Why: %s", pending['why']))
        if pending.get('reason'):
            lines.append(_("Needs your approval because: %s", pending['reason']))
        lines.append(f"[AI_OPTIONS]{json.dumps([SHELL_ALLOW_LABEL, SHELL_DENY_LABEL], ensure_ascii=False)}[/AI_OPTIONS]")
        return "\n".join(lines)

    def _resolve_shell_approval(self, task, ws, pending, text):
        """The reply to a shell approval card: ONLY the exact allow chip runs
        the command; anything else is a denial carrying the user's words."""
        from .ai_code_tools import SHELL_ALLOW_LABEL
        command = pending.get('command') or ''
        if (text or '').strip() == SHELL_ALLOW_LABEL:
            self._journal(task, 'tool_call', role='assistant', content=f"shell({command[:120]})",
                          tool_name='shell', status='ok', turn=task.turn_count)
            out = ws.tool_shell_run(task.target_dir, command)
            self._journal(task, 'observation', role='tool', content=out, tool_name='shell',
                          status='ok', turn=task.turn_count)
            return f"User APPROVED the command. Result:\n{out}"
        return (f"User DENIED running `{command}`. Their reply: {text}\n"
                "Do not retry the same command; find another way or ask what they prefer.")

    def _render_questions(self, questions):
        if not questions:
            return _("Could you clarify?")
        if len(questions) == 1:
            q = questions[0]
            return f"{q['question']}\n\n[AI_OPTIONS]{json.dumps(q['options'], ensure_ascii=False)}[/AI_OPTIONS]"
        form = {
            'title': _("A few quick decisions"),
            'fields': [{'label': (q.get('header') or q['question'])[:80], 'type': 'select',
                        'options': q['options'][:10]} for q in questions],
            'submit_label': _("Answer"),
        }
        lines = [f"{i}. {q['question']}" for i, q in enumerate(questions, 1)]
        return "\n".join(lines) + "\n\n[AI_FORM]" + json.dumps(form, ensure_ascii=False) + "[/AI_FORM]"

    # ══════════════════════════════════════════════════════════════════
    # Memory distillation (cheap model, best effort)
    # ══════════════════════════════════════════════════════════════════

    def _distill_memory(self, task, ctx, user_text):
        try:
            model = task.cheap_model or task.main_model
            provider = get_provider(self.env, model)
            if not provider:
                return
            service = CodingLLMService(self.env, provider)
            events = self.env['ai.code.event'].sudo().search(
                [('task_id', '=', task.id), ('turn', '=', task.turn_count),
                 ('event_type', 'in', ('message', 'observation'))], order='seq')
            transcript = "\n".join(
                f"{e.role}: {(e.content or '')[:500]}" for e in events)[-9000:]
            prompt = (f"Workspace: {task.target_dir}\nRequest: {user_text[:600]}\n"
                      f"Files changed: {', '.join(ctx.changed_files[:20])}\n\nTranscript:\n{transcript}")
            with ai_response_logging(model):
                out = service.simple_completion(model, prompts.MEMORY_DISTILL_SYSTEM, prompt)
            Memory = self.env['ai.code.memory']
            stored = 0
            for line in (out or '').splitlines():
                line = line.strip("-*• \t")
                if ':' not in line:
                    continue
                kind, _sep, text = line.partition(':')
                kind = kind.strip().lower()
                if kind not in ('convention', 'decision', 'pitfall', 'outcome') or len(text.strip()) < 12:
                    continue
                Memory.store(kind, text.strip(), working_dir=task.target_dir, source_task=task)
                stored += 1
                if stored >= 4:
                    break
            self.env.cr.commit()
        except Exception:  # noqa: BLE001
            _logger.info("ai_code: memory distillation skipped", exc_info=True)
