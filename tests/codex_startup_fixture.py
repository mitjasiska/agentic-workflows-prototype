"""Sequenced Herdr/Codex observations shared by launch caller regressions."""

import copy
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from task_start.agent import Codex
from task_start import AgentNotReady
import pass_delivery_fixture


TRUST_SCREEN = """  Folder access
  /some/new/checkout

  Trust this folder? Codex can read, edit, and run files here,
  subject to your permission settings.

› 1. Trust and continue
  2. Quit
"""
AUTH_SCREEN = """  Sign in with ChatGPT to use Codex as part of your paid plan
› 1. Sign in with ChatGPT
  2. Sign in with Device Code
  3. Use an OpenAI API key
"""


class CodexStartupTransport:
    def __init__(self, test, panes):
        self.test, self.panes = test, panes
        self.now = 0
        # Captured fresh Herdr shell shape: startup children share the shell PGID.
        self.processes = [dict(shell_pid=123, foreground_process_group_id=123,
                               foreground_processes=[dict(pid=123), dict(pid=124), dict(pid=125)]),
                          dict(shell_pid=123, foreground_process_group_id=123,
                               foreground_processes=[dict(pid=123)])]
        self.process_reads = 0
        self.process_durations = [0]
        self.process_timeouts = []
        self.runtime_delay = 0.5  # Herdr agent start waits internally, then returns readiness.
        self.session_delay = 0.5
        self.receipt_delay = 0.5
        self.started_at = None
        self.queued_at = None
        self.thread_id = "01a0d314-bd68-7203-8b68-f2520f892afa"
        self.on_queue = lambda prompt: None
        self.start_changes = {}
        self.get_changes = {}
        self.queue_error = None
        self.blocker = None
        self.start_not_ready = True
        self.process_pid = 456
        self.process_changes = {}
        self.view_changes = {}
        self.extra_items = []
        self.command = test.enterContext(patch.object(Codex, "command", side_effect=self.herdr))
        self.filter_credentials = test.enterContext(patch.object(Codex, "filter_shell_credentials"))
        self.keys = test.enterContext(patch("task_start.agent.run", side_effect=self.send_keys))
        test.enterContext(patch.object(Codex, "check_available"))
        self.sleep = MagicMock(side_effect=self.advance)
        clock = SimpleNamespace(monotonic=lambda: self.now, sleep=self.sleep)
        # Share workflow time without advancing it for subprocess/Git waits.
        test.enterContext(patch("task_start.agent.time", clock))
        test.enterContext(patch("task_start.review.time", clock))
        factory = test.enterContext(patch("task_start.agent.CodexRPC"))
        self.rpc = MagicMock()
        self.rpc.request.side_effect = self.request
        factory.return_value.__enter__.return_value = self.rpc

    def advance(self, seconds):
        self.now += seconds

    def send_keys(self, args):
        self.test.assertEqual(args, ["herdr", "pane", "send-keys", self.target, "ctrl+c"])
        self.test.assertIsNone(self.started_at)
        self.test.assertEqual(self.last_process["foreground_processes"], [dict(pid=123)])

    def herdr(self, group, operation, *args, timeout=120):
        if (group, operation) == ("pane", "list"):
            return dict(panes=copy.deepcopy(self.panes()))
        if (group, operation) == ("pane", "process-info"):
            if self.started_at is not None:
                return dict(process_info=dict(dict(pane_id=self.target, shell_pid=123,
                    foreground_process_group_id=456, foreground_processes=[dict(pid=self.process_pid,
                    argv=self.argv, cwd=self.pane['cwd'])]), **self.process_changes))
            self.target = args[-1]
            self.last_process = self.processes[min(self.process_reads, len(self.processes) - 1)]
            self.process_timeouts.append(timeout)
            self.advance(self.process_durations[min(self.process_reads, len(self.process_durations) - 1)])
            self.process_reads += 1
            return dict(process_info=dict(dict(pane_id=self.target), **self.last_process))
        if (group, operation) == ("agent", "start"):
            self.test.assertIsNone(self.started_at, "launch must never be retried")
            self.keys.assert_called_once()
            self.filter_credentials.assert_called_once()
            self.started_at = self.now
            self.bootstrap = args[-1]
            self.pane = next(p for p in self.panes() if p["pane_id"] == self.target)
            self.test.assertIsNone(self.pane.get("agent"))
            self.pane.update(launch_pending=True)
            self.advance(self.runtime_delay)
            self.pane.update(agent="codex", agent_status="idle", launch_pending=False,
                             foreground_cwd=self.pane["cwd"])
            self.argv = ["codex", *args[args.index("--") + 1:]]
            if self.blocker:
                self.pane['agent_status'] = 'blocked'
                if self.start_not_ready:
                    raise AgentNotReady('agent_not_ready')
                self.pane['agent_status'] = 'idle'
            return dict(agent=dict(self.pane, **self.start_changes), argv=self.argv)
        if (group, operation) == ("agent", "get"):
            if not self.blocker and self.now >= self.started_at + self.runtime_delay + self.session_delay:
                self.pane["agent_session"] = dict(agent="codex", kind="id", value=self.thread_id)
            return dict(agent=dict(self.pane, **self.get_changes))
        if (group, operation) == ('pane', 'read'):
            return dict(read=dict(dict(pane_id=self.target, workspace_id=self.pane['workspace_id'],
                tab_id=self.pane['tab_id'], source='visible', format='text', truncated=False,
                text=self.blocker or 'Codex ready'), **self.view_changes))
        self.test.fail((group, operation, args))

    def request(self, method, params, **kwargs):
        thread = dict(id=self.thread_id, cwd=self.pane["cwd"], preview=self.bootstrap)
        if method == "thread/list":
            visible = not self.blocker and self.now >= self.started_at + self.runtime_delay + self.session_delay
            return dict(data=[thread] if visible else [])
        if method == "thread/read":
            return dict(thread=thread)
        if method == "thread/items/list":
            items = [dict(turnId="ready", item=dict(type="userMessage", clientId="native",
                          content=[dict(type="text", text=self.bootstrap)]))]
            if self.queued_at is not None and self.now >= self.queued_at + self.receipt_delay:
                items.append(dict(turnId="task-turn", item=dict(type="userMessage",
                             clientId=self.queued["clientUserMessageId"], content=self.queued["input"])))
            return dict(data=items + self.extra_items, nextCursor=None)
        if method == "thread/queue/add":
            self.test.assertIsNone(self.queued_at, "handoff must never be resubmitted")
            self.queued_at, self.queued = self.now, params
            self.on_queue(params["input"][0]["text"])
            self.completion = pass_delivery_fixture.complete_prompt(params["input"][0]["text"])
            if self.queue_error:
                raise self.queue_error
            return dict(queuedSubmission=dict(id="queue", **params))
        if method == "thread/queue/list":
            return dict(data=[], nextCursor=None)
        if method == "thread/turns/list":
            return dict(data=[dict(id="task-turn", status="completed", error=None, itemsView="full", items=[
                dict(type="userMessage", clientId=self.queued["clientUserMessageId"], content=self.queued["input"]),
                dict(type="agentMessage", phase="final_answer", text=self.completion)])], nextCursor=None)
        self.test.fail(method)

    def accept_setup(self):
        self.blocker = None
        self.pane['agent_status'] = 'idle'

    def assert_effects(self, launches, prompts):
        self.test.assertEqual(sum(c.args[:2] == ("agent", "start") for c in self.command.call_args_list), launches)
        self.test.assertEqual(sum(c.args[0] == "thread/queue/add" for c in self.rpc.request.call_args_list), prompts)
        self.test.assertFalse(any(c.args[:2] == ("agent", "prompt") for c in self.command.call_args_list))
