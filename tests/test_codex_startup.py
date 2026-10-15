import copy
from dataclasses import replace
import json
import io
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from task_start import AgentNotReady, HerdrResponseError, TaskError, cli
from task_start.agent import AgentOptions, Codex, HerdrAgentAdapter
from task_start.contexts import ContextRegistry, HerdrContexts
from task_start.handoff import implementation_handoff
from task_start.workspace import Workspace
from codex_startup_fixture import AUTH_SCREEN, TRUST_SCREEN, CodexStartupTransport
from test_task_start import ISSUE, LOCAL, PROJECT


class FreshCodexStartTests(unittest.TestCase):
    """Normal start orchestration and registry with sequenced transport boundaries."""

    def setUp(self):
        directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.workspace = Workspace("dev-7-test", directory, "w1", "t1", "p1", "created")
        self.pane = dict(pane_id="p1", tab_id="t1", workspace_id="w1", terminal_id="term1",
                         cwd=str(directory), agent=None, agent_session=None, label="Shell")
        self.registry = ContextRegistry(directory / "contexts.sqlite3")
        self.enterContext(patch("task_start.contexts.registry_path", return_value=self.registry.path))
        identities = Mock(spec=HerdrContexts)
        identities.endpoint.return_value = "/server.sock"
        identities.snapshot.side_effect = lambda: [copy.deepcopy(self.pane)]
        identities.label.side_effect = lambda pane, label: self.pane.update(label=label)
        self.enterContext(patch("task_start.contexts.ContextRegistry", return_value=self.registry))
        self.enterContext(patch("task_start.contexts.HerdrContexts", return_value=identities))
        self.enterContext(patch("task_start.cli.load_local", return_value=LOCAL))
        self.enterContext(patch("task_start.cli.load_projects", return_value=[PROJECT]))
        self.enterContext(patch("task_start.cli.Linear")).return_value.get_issue.return_value = ISSUE
        self.enterContext(patch("task_start.cli.Git"))
        self.herdr = self.enterContext(patch("task_start.cli.Herdr")).return_value
        self.herdr.prepare.return_value = self.workspace
        self.transport = CodexStartupTransport(self, lambda: [self.pane])

    def test_first_start_waits_for_shell_runtime_session_and_receipt_once(self):
        result = cli.start("DEV-7")
        self.assertIn("DEV-7-I1: Codex turn task-turn confirmed", result)
        t = self.transport
        self.assertEqual(t.process_reads, 2)
        self.assertEqual(t.started_at, 0.25)
        self.assertEqual(t.now, 1.75)
        t.assert_effects(1, 1)
        self.assertEqual(t.queued["input"][0]["text"], implementation_handoff(ISSUE, self.workspace))
        row = self.registry.get("DEV-7-I1")
        self.assertEqual((row["state"], row["session_id"], row["resumability"]), ("active", t.thread_id, "yes"))

    def test_ready_reopened_workspace_needs_no_poll_delay(self):
        self.herdr.prepare.return_value = replace(self.workspace, action="reopened")
        t = self.transport
        t.processes = t.processes[-1:]
        t.runtime_delay = t.session_delay = t.receipt_delay = 0
        cli.start("DEV-7")
        t.sleep.assert_not_called()
        t.assert_effects(1, 1)
        # Reusing an occupied workspace still refuses a duplicate/context allocation.
        with self.assertRaisesRegex(TaskError, "already occupies"):
            cli.start("DEV-7")
        t.assert_effects(1, 1)
        self.assertEqual(len(self.registry.list()), 1)

    def test_observation_time_reduces_remaining_readiness_budget(self):
        t = self.transport
        t.process_durations = [0.125]
        with patch.object(Codex, "SHELL_READY_TIMEOUT", 1):
            cli.start("DEV-7")
        self.assertEqual(t.process_timeouts, [1, 0.625])
        self.assertEqual(t.started_at, 0.5)
        t.assert_effects(1, 1)

    def test_ready_observation_returning_after_deadline_sends_no_input(self):
        t = self.transport
        t.processes = t.processes[-1:]
        t.process_durations = [31]
        with self.assertRaisesRegex(TaskError, r"pane readiness.*Timed out.*foreground_pids=\[123\]"):
            cli.start("DEV-7")
        self.assertEqual(t.process_timeouts, [30])
        t.keys.assert_not_called()
        t.assert_effects(0, 0)
        self.assertIsNone(self.registry.get("DEV-7-I1")["session_id"])

    def test_ready_observation_returning_at_deadline_sends_no_input(self):
        t = self.transport
        t.processes = t.processes[-1:]
        t.process_durations = [0.5]
        with patch.object(Codex, "SHELL_READY_TIMEOUT", 0.5), self.assertRaisesRegex(TaskError, "Timed out"):
            cli.start("DEV-7")
        self.assertEqual(t.process_timeouts, [0.5])
        t.keys.assert_not_called()
        t.assert_effects(0, 0)

    def test_no_observation_starts_after_budget_is_consumed(self):
        t = self.transport
        t.process_durations = [0.375, 0]
        with patch.object(Codex, "SHELL_READY_TIMEOUT", 0.5), self.assertRaisesRegex(TaskError, "Timed out"):
            cli.start("DEV-7")
        self.assertEqual(t.now, 0.5)
        self.assertEqual(t.process_reads, 1)
        self.assertEqual(t.process_timeouts, [0.5])
        t.keys.assert_not_called()
        t.assert_effects(0, 0)

    def test_missing_initial_shell_metadata_can_settle(self):
        t = self.transport
        t.processes.insert(0, dict(shell_pid=None, foreground_process_group_id=None, foreground_processes=[]))
        cli.start("DEV-7")
        self.assertEqual(t.process_reads, 3)
        self.assertEqual(t.started_at, 0.5)
        t.assert_effects(1, 1)

    def test_shell_timeout_sends_no_input_and_consumes_ordinal(self):
        t = self.transport
        t.processes = t.processes[:1]
        with patch.object(Codex, "SHELL_READY_TIMEOUT", 0.5), self.assertRaisesRegex(
                TaskError, r"pane readiness.*Timed out.*shell_pid=123.*foreground_pids=\[123, 124, 125\]"):
            cli.start("DEV-7")
        self.assertEqual(t.now, 0.5)
        t.keys.assert_not_called()
        t.assert_effects(0, 0)
        row = self.registry.get("DEV-7-I1")
        self.assertEqual((row["state"], row["session_id"]), ("uncertain", None))
        t.processes = [dict(shell_pid=123, foreground_process_group_id=123, foreground_processes=[dict(pid=123)])]
        self.assertIn("DEV-7-I2", cli.start("DEV-7"))
        self.assertEqual([r["ordinal"] for r in self.registry.list()], [1, 2])

    def test_changed_shell_identity_is_not_polled_away(self):
        t = self.transport
        t.processes[1] = dict(shell_pid=456, foreground_process_group_id=456, foreground_processes=[dict(pid=456)])
        with self.assertRaisesRegex(TaskError, "pane readiness.*Shell identity changed.*shell_pid=456"):
            cli.start("DEV-7")
        t.keys.assert_not_called()
        t.assert_effects(0, 0)
        self.assertEqual(t.process_reads, 2)

    def test_wrong_pane_or_malformed_process_state_is_not_retried(self):
        t = self.transport
        original = t.processes[-1]
        for changes in (dict(pane_id="other"), dict(shell_pid="123"), dict(foreground_processes=[{}])):
            with self.subTest(changes=changes):
                t.processes = [dict(original, **changes)]
                with self.assertRaisesRegex(TaskError, "pane readiness.*last check"):
                    cli.start("DEV-7")
        t.keys.assert_not_called()
        t.assert_effects(0, 0)

    def test_session_timeout_never_reissues_launch_or_queues_task(self):
        t = self.transport
        t.session_delay = 100
        with self.assertRaisesRegex(TaskError, "readiness confirmation.*readiness turn not found"):
            cli.start("DEV-7")
        self.assertEqual(t.now, 30.75)
        t.assert_effects(1, 0)
        self.assertIsNone(self.registry.get("DEV-7-I1")["session_id"])

    def test_unexpected_runtime_fails_before_session_poll_or_prompt(self):
        t = self.transport
        t.start_changes = dict(agent="pi")
        with self.assertRaisesRegex(TaskError, "startup launch/runtime confirmation.*agent target mismatch"):
            cli.start("DEV-7")
        t.assert_effects(1, 0)
        t.rpc.request.assert_not_called()

    def test_contradictory_session_is_never_accepted(self):
        t = self.transport
        t.get_changes = dict(agent_session=dict(agent="codex", kind="id", value="different"))
        with self.assertRaisesRegex(TaskError, "session does not match"):
            cli.start("DEV-7")
        t.assert_effects(1, 0)
        self.assertEqual(self.registry.get("DEV-7-I1")["session_id"], t.thread_id)

    def test_missing_native_report_warns_before_delivery_and_saves_provider_identity(self):
        t = self.transport
        t.get_changes = dict(agent_session=None)
        with patch("sys.stderr", new_callable=io.StringIO) as stderr:
            t.on_queue = lambda prompt: self.assertIn("interrupted-pass recovery", stderr.getvalue())
            cli.start("DEV-7")
        t.assert_effects(1, 1)
        self.assertEqual(stderr.getvalue().count("Warning: Herdr"), 1)
        self.assertIn("herdr integration status", stderr.getvalue())
        self.assertIn("docs/configuration.md#herdr-codex-session-hook", stderr.getvalue())
        row = self.registry.get("DEV-7-I1")
        self.assertEqual((row["session_id"], row["resumability"]), (t.thread_id, "yes"))
        self.assertIsNone(row["herdr_session"])  # Do not invent a native report.

    def test_matching_native_hook_report_needs_no_warning(self):
        t = self.transport
        report = dict(agent="codex", kind="id", value=t.thread_id, source="herdr:codex")
        t.start_changes = t.get_changes = dict(agent_session=report)
        with patch("sys.stderr", new_callable=io.StringIO) as stderr:
            cli.start("DEV-7")
        t.assert_effects(1, 1)
        self.assertNotIn("Warning: Herdr", stderr.getvalue())
        self.assertEqual(json.loads(self.registry.get("DEV-7-I1")["herdr_session"]), report)

    def test_lost_native_report_is_not_downgraded_to_warning(self):
        t = self.transport
        t.start_changes = dict(agent_session=dict(agent="codex", kind="id", value=t.thread_id))
        t.get_changes = dict(agent_session=None)
        with patch("sys.stderr", new_callable=io.StringIO) as stderr, self.assertRaisesRegex(
                TaskError, "session changed or is no longer reported"):
            cli.start("DEV-7")
        t.assert_effects(1, 0)
        self.assertNotIn("Warning: Herdr", stderr.getvalue())
        self.assertEqual(self.registry.get("DEV-7-I1")["session_id"], t.thread_id)

    def test_ambiguous_native_report_refuses_before_warning_or_delivery(self):
        t = self.transport
        t.get_changes = dict(agent_session={})
        with patch("sys.stderr", new_callable=io.StringIO) as stderr, self.assertRaises(TaskError):
            cli.start("DEV-7")
        t.assert_effects(1, 0)
        self.assertNotIn("Warning: Herdr", stderr.getvalue())

    def test_prompt_timeout_keeps_identity_and_never_resubmits(self):
        t = self.transport
        t.receipt_delay = 100
        with self.assertRaisesRegex(TaskError, "prompt delivery/start.*Timed out"):
            cli.start("DEV-7")
        t.assert_effects(1, 1)
        row = self.registry.get("DEV-7-I1")
        self.assertEqual((row["state"], row["session_id"], row["resumability"]), ("uncertain", t.thread_id, "yes"))

    def human_input(self, action, *, interactive=True):
        self.enterContext(patch('task_start.agent.sys.stdin', SimpleNamespace(
            isatty=lambda: interactive, readline=action)))
        return self.enterContext(patch('task_start.agent.sys.stderr', new_callable=io.StringIO))

    def transient_observation(self, operation, *, count=1, when=lambda: True, change=lambda: None):
        transport = self.transport
        attempts = []
        def command(group, op, *args, **kwargs):
            if (group, op) == operation and when() and len(attempts) < count:
                attempts.append(True)
                change()
                raise HerdrResponseError(f"Unexpected Herdr {group} {op} response")
            return transport.herdr(group, op, *args, **kwargs)
        transport.command.side_effect = command
        return attempts

    def test_unrecognized_observation_envelope_has_distinct_recoverable_error(self):
        adapter = Codex(AgentOptions("codex"))
        for response in ("not json", '{"result":{"type":"unavailable"}}', '{"error":"temporarily unavailable"}'):
            with self.subTest(response=response), patch("task_start.agent.run", return_value=response):
                with self.assertRaisesRegex(HerdrResponseError, "Unexpected Herdr pane read response"):
                    HerdrAgentAdapter.command(adapter, "pane", "read", "p1")

    def test_transient_viewport_after_trust_continues_original_launch_once(self):
        t = self.transport
        t.blocker = TRUST_SCREEN
        self.human_input(lambda: (t.accept_setup(), "\n")[1])
        attempts = self.transient_observation(("pane", "read"), when=lambda: t.blocker is None)
        cli.start("DEV-7")
        self.assertEqual(attempts, [True])
        self.assertEqual(self.registry.get("DEV-7-I1")["session_id"], t.thread_id)
        t.assert_effects(1, 1)

    def test_transient_setup_observations_retry_only_reads(self):
        for operation in (("pane", "read"), ("pane", "process-info"), ("agent", "get")):
            with self.subTest(operation=operation):
                case = FreshCodexStartTests(); case.setUp()
                try:
                    t = case.transport
                    t.blocker = TRUST_SCREEN
                    case.human_input(lambda: (t.accept_setup(), "\n")[1])
                    case.transient_observation(operation, count=2, when=lambda: t.started_at is not None)
                    cli.start("DEV-7")
                    t.assert_effects(1, 1)
                finally:
                    case.doCleanups()

    def test_lost_start_response_reconciles_native_nonce_without_relaunch(self):
        t = self.transport
        def command(group, operation, *args, **kwargs):
            response = t.herdr(group, operation, *args, **kwargs)
            if (group, operation) == ("agent", "start"):
                raise HerdrResponseError("Unexpected Herdr agent start response")
            return response
        t.command.side_effect = command
        cli.start("DEV-7")
        t.assert_effects(1, 1)
        self.assertEqual(len(self.registry.list()), 1)

    def test_prequeue_target_read_error_requires_full_history_reconciliation(self):
        t = self.transport
        self.transient_observation(("agent", "get"))
        cli.start("DEV-7")
        t.assert_effects(1, 1)
        self.assertGreaterEqual(sum(c.args[0] == "thread/items/list" for c in t.rpc.request.call_args_list), 3)

    def test_persistent_post_trust_read_error_preserves_undelivered_context(self):
        t = self.transport
        t.blocker = TRUST_SCREEN
        self.human_input(lambda: (t.accept_setup(), "\n")[1])
        self.transient_observation(("pane", "read"), count=100, when=lambda: t.blocker is None)
        with patch.object(Codex, "POST_TRUST_READY_TIMEOUT", 0.5), self.assertRaisesRegex(
                TaskError, "Timed out.*No task prompt was sent"):
            cli.start("DEV-7")
        t.assert_effects(1, 0)
        self.assertEqual(self.registry.get("DEV-7-I1")["state"], "awaiting_user")

    def test_identity_conflicts_after_transient_observation_still_refuse(self):
        for kind in ("process", "terminal", "session", "checkout", "input"):
            with self.subTest(kind=kind):
                case = FreshCodexStartTests(); case.setUp()
                try:
                    t = case.transport
                    t.blocker = TRUST_SCREEN
                    case.human_input(lambda: (t.accept_setup(), "\n")[1])
                    def change():
                        if kind == "process":
                            t.process_pid += 1
                        elif kind == "terminal":
                            t.pane["terminal_id"] = "replacement"
                        elif kind == "session":
                            t.get_changes["agent_session"] = dict(agent="codex", kind="id", value="wrong")
                        elif kind == "checkout":
                            t.pane["foreground_cwd"] = "/different"
                        else:
                            t.extra_items = [dict(turnId="other", item=dict(type="userMessage",
                                content=[dict(type="text", text="external task input")]))]
                    case.transient_observation(("pane", "read"), when=lambda: t.blocker is None, change=change)
                    with case.assertRaises(TaskError):
                        cli.start("DEV-7")
                    t.assert_effects(1, 0)
                finally:
                    case.doCleanups()

    def test_recovery_refuses_pending_provider_queue_before_first_delivery(self):
        t = self.transport
        t.blocker = TRUST_SCREEN
        self.human_input(lambda: (t.accept_setup(), "\n")[1])
        def request(method, params, **kwargs):
            if method == "thread/queue/list":
                return dict(data=[dict(id="external-input")], nextCursor=None)
            return t.request(method, params, **kwargs)
        t.rpc.request.side_effect = request
        with self.assertRaisesRegex(TaskError, "pending or uncertain input"):
            cli.start("DEV-7")
        t.assert_effects(1, 0)

    def test_late_successful_read_after_transient_error_cannot_authorize_delivery(self):
        t = self.transport
        t.blocker = TRUST_SCREEN
        self.human_input(lambda: (t.accept_setup(), "\n")[1])
        reads = []
        def command(group, operation, *args, **kwargs):
            if (group, operation) == ("pane", "read") and t.blocker is None:
                reads.append(kwargs["timeout"])
                if len(reads) == 1:
                    raise HerdrResponseError("Unexpected Herdr pane read response")
                t.advance(kwargs["timeout"])
            return t.herdr(group, operation, *args, **kwargs)
        t.command.side_effect = command
        with patch.object(Codex, "POST_TRUST_READY_TIMEOUT", 1), self.assertRaisesRegex(TaskError, "Timed out"):
            cli.start("DEV-7")
        self.assertEqual(reads, [1, 0.75])
        t.assert_effects(1, 0)

    def test_transient_looking_queue_error_never_retries_or_reconciles_startup(self):
        t = self.transport
        t.queue_error = HerdrResponseError("unexpected queue response")
        with self.assertRaisesRegex(TaskError, "prompt queue"):
            cli.start("DEV-7")
        t.assert_effects(1, 1)
        self.assertFalse(any(c.args[:2] == ("pane", "read") for c in t.command.call_args_list))

    def test_trust_before_identity_continues_original_launch_and_context(self):
        t = self.transport
        t.blocker = TRUST_SCREEN
        def accept():
            row = self.registry.get('DEV-7-I1')
            self.assertEqual((row['state'], row['session_id']), ('awaiting_user', None))
            self.assertEqual((row['pane_id'], row['terminal_id']), ('p1', 'term1'))
            t.assert_effects(1, 0)
            with self.assertRaisesRegex(TaskError, 'already occupies'):
                cli.start('DEV-7')
            # Even a caller choosing a new pane cannot replace the waiting context.
            with self.assertRaisesRegex(TaskError, 'no replacement'):
                self.registry.allocate('DEV-7', 'implementation', agent='codex',
                    repository=row['repository'], worktree=row['worktree'], pane_id='replacement')
            t.accept_setup()
            return '\n'
        out = self.human_input(accept)
        result = cli.start('DEV-7')
        self.assertIn('DEV-7-I1: Codex turn task-turn confirmed', result)
        self.assertIn('waiting for user repository trust', out.getvalue())
        self.assertIn(str(self.workspace.path), out.getvalue())
        self.assertIn('pane: p1; terminal: term1', out.getvalue())
        self.assertIn('task delivery not attempted', out.getvalue())
        self.assertEqual(len(self.registry.list()), 1)
        self.assertEqual(self.registry.get('DEV-7-I1')['session_id'], t.thread_id)
        t.assert_effects(1, 1)
        t.keys.assert_called_once()  # Only the pre-launch shell Ctrl+C.

    def test_new_isolated_checkout_is_not_replaced_after_trust(self):
        path = self.workspace.path / 'integrations' / 'unique-attempt' / 'checkout'
        path.mkdir(parents=True)
        self.workspace = replace(self.workspace, path=path)
        self.herdr.prepare.return_value = self.workspace
        self.pane['cwd'] = str(path)
        t = self.transport
        t.blocker = TRUST_SCREEN
        self.human_input(lambda: (t.accept_setup(), '\n')[1])
        cli.start('DEV-7')
        self.assertEqual(self.registry.get('DEV-7-I1')['worktree'], str(path))
        self.assertEqual(t.argv[t.argv.index('--cd') + 1], str(path))
        self.assertEqual(t.queued['input'][0]['text'], implementation_handoff(ISSUE, self.workspace))
        t.assert_effects(1, 1)

    def test_known_authentication_then_trust_uses_same_human_boundary(self):
        t = self.transport
        t.blocker = AUTH_SCREEN
        actions = []
        def accept():
            actions.append(t.blocker)
            if len(actions) == 1:
                t.blocker = TRUST_SCREEN
            else:
                t.accept_setup()
            return '\n'
        out = self.human_input(accept)
        cli.start('DEV-7')
        self.assertEqual(actions, [AUTH_SCREEN, TRUST_SCREEN])
        self.assertIn('authentication setup', out.getvalue())
        self.assertIn('repository trust', out.getvalue())
        t.assert_effects(1, 1)

    def test_idle_trust_screen_without_provider_is_also_reconciled(self):
        t = self.transport
        t.blocker, t.start_not_ready = TRUST_SCREEN, False
        self.human_input(lambda: (t.accept_setup(), '\n')[1])
        cli.start('DEV-7')
        t.assert_effects(1, 1)

    def test_enter_does_not_prove_setup_completed_or_send_input(self):
        t = self.transport
        t.blocker = TRUST_SCREEN
        calls = []
        def accept():
            calls.append(True)
            t.assert_effects(1, 0)
            if len(calls) == 2:
                t.accept_setup()
            return '\n'
        self.human_input(accept)
        cli.start('DEV-7')
        self.assertEqual(len(calls), 2)
        t.assert_effects(1, 1)

    def test_noninteractive_wait_fails_closed_and_retains_pending_context(self):
        self.transport.blocker = TRUST_SCREEN
        out = self.human_input(lambda: self.fail('must not read noninteractive stdin'), interactive=False)
        with self.assertRaisesRegex(TaskError, 'stdin is not a terminal'):
            cli.start('DEV-7')
        self.assertEqual(self.registry.get('DEV-7-I1')['state'], 'awaiting_user')
        self.assertIn('Do not rerun', out.getvalue())
        self.transport.assert_effects(1, 0)

    def test_interrupted_wait_retains_context_without_provider_or_delivery(self):
        self.transport.blocker = TRUST_SCREEN
        def interrupt():
            raise KeyboardInterrupt()
        self.human_input(interrupt)
        with self.assertRaises(KeyboardInterrupt):
            cli.start('DEV-7')
        row = self.registry.get('DEV-7-I1')
        self.assertEqual((row['state'], row['session_id']), ('awaiting_user', None))
        self.transport.assert_effects(1, 0)

    def test_eof_during_wait_is_not_acknowledgement(self):
        self.transport.blocker = TRUST_SCREEN
        self.human_input(lambda: '')
        with self.assertRaisesRegex(TaskError, 'input closed'):
            cli.start('DEV-7')
        self.assertEqual(self.registry.get('DEV-7-I1')['state'], 'awaiting_user')
        self.transport.assert_effects(1, 0)

    def test_process_replacement_after_user_action_never_queues(self):
        t = self.transport
        t.blocker = TRUST_SCREEN
        def accept():
            t.accept_setup()
            t.process_pid += 1
            return '\n'
        self.human_input(accept)
        with self.assertRaisesRegex(TaskError, 'process changed'):
            cli.start('DEV-7')
        t.assert_effects(1, 0)

    def test_terminal_replacement_after_user_action_never_queues(self):
        t = self.transport
        t.blocker = TRUST_SCREEN
        def accept():
            t.accept_setup()
            self.pane['terminal_id'] = 'replacement'
            return '\n'
        self.human_input(accept)
        with self.assertRaisesRegex(TaskError, 'terminal changed'):
            cli.start('DEV-7')
        t.assert_effects(1, 0)

    def test_acknowledgement_without_provider_identity_never_delivers(self):
        t = self.transport
        t.blocker, t.session_delay = TRUST_SCREEN, 100
        self.human_input(lambda: (t.accept_setup(), '\n')[1])
        with self.assertRaisesRegex(TaskError, 'readiness turn not found'):
            cli.start('DEV-7')
        t.assert_effects(1, 0)

    def test_extra_provider_input_after_user_action_makes_delivery_uncertain(self):
        t = self.transport
        t.blocker = TRUST_SCREEN
        def accept():
            t.accept_setup()
            t.extra_items = [dict(turnId='external', item=dict(type='userMessage', clientId='external',
                content=[dict(type='text', text=implementation_handoff(ISSUE, self.workspace))]))]
            return '\n'
        self.human_input(accept)
        with self.assertRaisesRegex(TaskError, 'delivery is uncertain'):
            cli.start('DEV-7')
        t.assert_effects(1, 0)

    def test_queue_response_lost_after_trust_never_reenters_recovery(self):
        t = self.transport
        t.blocker, t.queue_error = TRUST_SCREEN, TaskError('queue response lost')
        calls = []
        def accept():
            calls.append(True)
            t.accept_setup()
            return '\n'
        self.human_input(accept)
        with self.assertRaisesRegex(TaskError, 'prompt queue.*queue response lost'):
            cli.start('DEV-7')
        self.assertEqual(calls, [True])
        self.assertEqual(self.registry.get('DEV-7-I1')['state'], 'uncertain')
        t.assert_effects(1, 1)

    def test_unknown_setup_screen_never_requests_or_sends_input(self):
        self.transport.blocker = 'Trust this folder?\nA quoted log line, not the recognized menu'
        self.human_input(lambda: self.fail('unrecognized screen must fail closed'))
        with self.assertRaisesRegex(TaskError, 'ambiguous or unsupported'):
            cli.start('DEV-7')
        self.transport.assert_effects(1, 0)

    def test_mismatched_viewport_is_not_accepted(self):
        self.transport.blocker = TRUST_SCREEN
        self.transport.view_changes = dict(pane_id='another-pane')
        self.human_input(lambda: self.fail('mismatched screen must fail closed'))
        with self.assertRaisesRegex(TaskError, 'invalid startup viewport'):
            cli.start('DEV-7')
        self.transport.assert_effects(1, 0)

    def test_wrapped_trust_disclosure_is_recognized_with_complete_menu(self):
        t = self.transport
        t.blocker = TRUST_SCREEN.replace('Codex can read, edit, and run files here,',
                                         'Codex can read,\n  edit, and run files here,')
        self.human_input(lambda: (t.accept_setup(), '\n')[1])
        cli.start('DEV-7')
        t.assert_effects(1, 1)

    def test_nonready_launch_with_stable_session_continues_after_user_trust_once(self):
        t = self.transport
        first = dict(agent='codex', kind='id', value=t.thread_id)
        t.blocker, t.start_not_ready = TRUST_SCREEN, False
        t.start_changes = dict(agent_status='blocked', agent_session=first)
        t.get_changes = dict(agent_status='blocked', agent_session=first)
        actions = []
        def accept():
            row = self.registry.get('DEV-7-I1')
            self.assertEqual((row['state'], row['session_id']), ('awaiting_user', first['value']))
            self.assertEqual(json.loads(row['herdr_session']), first)
            self.assertEqual((row['pane_id'], row['terminal_id'], row['worktree']),
                             ('p1', 'term1', str(self.workspace.path)))
            t.assert_effects(1, 0)
            with self.assertRaisesRegex(TaskError, 'no replacement'):
                self.registry.allocate('DEV-7', 'implementation', agent='codex',
                    repository=row['repository'], worktree=row['worktree'], pane_id='replacement')
            actions.append(True)
            t.accept_setup()
            t.get_changes = dict(agent_session=first)
            return '\n'
        out = self.human_input(accept)
        result = cli.start('DEV-7')
        self.assertIn('DEV-7-I1: Codex turn task-turn confirmed', result)
        self.assertEqual(actions, [True])
        self.assertIn('waiting for user repository trust', out.getvalue())
        self.assertIn('provider session observed: ' + first['value'], out.getvalue())
        self.assertNotIn('provider session not yet observed', out.getvalue())
        self.assertIn('task delivery not attempted', out.getvalue())
        row = self.registry.get('DEV-7-I1')
        self.assertEqual((row['state'], row['session_id'], row['resumability']), ('active', first['value'], 'yes'))
        self.assertEqual((row['pane_id'], row['terminal_id']), ('p1', 'term1'))
        self.assertEqual(len(self.registry.list()), 1)
        self.assertEqual(t.queued['threadId'], first['value'])
        self.assertEqual(t.queued['input'][0]['text'], implementation_handoff(ISSUE, self.workspace))
        t.assert_effects(1, 1)
        t.keys.assert_called_once()  # Only the pre-launch shell Ctrl+C.

    def test_established_startup_session_must_remain_reported_after_human_wait(self):
        t = self.transport
        first = dict(agent='codex', kind='id', value=t.thread_id)
        t.blocker, t.start_not_ready = TRUST_SCREEN, False
        t.start_changes = dict(agent_status='blocked', agent_session=first)
        t.get_changes = dict(agent_session=first)
        actions = []
        def accept():
            actions.append(True)
            t.accept_setup()
            t.get_changes = dict(agent_session=None)
            return '\n'
        self.human_input(accept)
        with self.assertRaisesRegex(TaskError, 'session changed or is no longer reported'):
            cli.start('DEV-7')
        self.assertEqual(actions, [True])
        self.assertEqual(self.registry.get('DEV-7-I1')['session_id'], first['value'])
        t.assert_effects(1, 0)

    def test_established_startup_session_cannot_change_during_human_wait(self):
        t = self.transport
        first = dict(agent='codex', kind='id', value=t.thread_id)
        t.blocker, t.start_not_ready = TRUST_SCREEN, False
        t.start_changes = dict(agent_status='blocked', agent_session=first)
        t.get_changes = dict(agent_session=first)
        actions = []
        def accept():
            actions.append(True)
            t.accept_setup()
            t.get_changes = dict(agent_session=dict(first, value='11a0d314-bd68-7203-8b68-f2520f892afa'))
            return '\n'
        self.human_input(accept)
        with self.assertRaisesRegex(TaskError, 'session changed'):
            cli.start('DEV-7')
        self.assertEqual(actions, [True])
        self.assertEqual(self.registry.get('DEV-7-I1')['session_id'], first['value'])
        self.assertEqual(len(self.registry.list()), 1)
        t.assert_effects(1, 0)

    def test_established_startup_session_with_prior_task_input_never_replays(self):
        t = self.transport
        first = dict(agent='codex', kind='id', value=t.thread_id)
        t.blocker, t.start_not_ready = TRUST_SCREEN, False
        t.start_changes = dict(agent_status='blocked', agent_session=first)
        t.get_changes = dict(agent_session=first)
        t.extra_items = [dict(turnId='external', item=dict(type='userMessage', clientId='external',
            content=[dict(type='text', text=implementation_handoff(ISSUE, self.workspace))]))]
        actions = []
        def accept():
            actions.append(True)
            t.accept_setup()
            return '\n'
        self.human_input(accept)
        with self.assertRaisesRegex(TaskError, 'delivery is uncertain'):
            cli.start('DEV-7')
        self.assertEqual(actions, [True])
        self.assertEqual(self.registry.get('DEV-7-I1')['session_id'], first['value'])
        t.assert_effects(1, 0)

    def test_nonready_launch_session_is_not_forgotten_before_recovery(self):
        # R1: start reports A, subsequent startup reads omit it, and accepting
        # trust would expose B. The first immutable identity must survive.
        t = self.transport
        first = dict(agent='codex', kind='id', value='11a0d314-bd68-7203-8b68-f2520f892afa')
        t.blocker, t.start_not_ready = TRUST_SCREEN, False
        t.start_changes = dict(agent_status='blocked', agent_session=first)
        self.human_input(lambda: (t.accept_setup(), '\n')[1])
        with self.assertRaisesRegex(TaskError, 'session changed or is no longer reported') as caught:
            cli.start('DEV-7')
        self.assertIn('Session: ' + first['value'], str(caught.exception))
        row = self.registry.get('DEV-7-I1')
        self.assertEqual((row['session_id'], row['session_kind']), (first['value'], 'id'))
        self.assertEqual(json.loads(row['herdr_session']), first)
        self.assertEqual(len(self.registry.list()), 1)
        t.assert_effects(1, 0)

    def test_nonready_launch_refuses_conflicting_next_session_without_rebinding(self):
        t = self.transport
        first = dict(agent='codex', kind='id', value='11a0d314-bd68-7203-8b68-f2520f892afa')
        t.blocker, t.start_not_ready = TRUST_SCREEN, False
        t.start_changes = dict(agent_status='unknown', agent_session=first)
        t.get_changes = dict(agent_session=dict(first, value=t.thread_id))
        self.human_input(lambda: self.fail('conflicting session must not enter human wait'))
        with self.assertRaisesRegex(TaskError, 'session changed'):
            cli.start('DEV-7')
        self.assertEqual(self.registry.get('DEV-7-I1')['session_id'], first['value'])
        t.assert_effects(1, 0)

    def test_post_trust_transient_runtime_statuses_reconcile_same_launch(self):
        # R1: the trust menu has gone but runtime detection still reports blocked
        # and unknown before observing idle on the same original process.
        t = self.transport
        t.blocker = TRUST_SCREEN
        pending, observed = [], []
        def accept():
            t.accept_setup()
            pending.extend(['blocked', 'unknown', 'idle'])
            return '\n'
        def herdr(group, operation, *args, **kwargs):
            result = t.herdr(group, operation, *args, **kwargs)
            if (group, operation) == ('agent', 'get') and pending:
                status = pending.pop(0)
                observed.append(status)
                result['agent']['agent_status'] = status
                t.assert_effects(1, 0)
            return result
        t.command.side_effect = herdr
        self.human_input(accept)
        result = cli.start('DEV-7')
        self.assertIn('DEV-7-I1: Codex turn task-turn confirmed', result)
        self.assertEqual(observed, ['blocked', 'unknown', 'idle'])
        self.assertEqual(len(self.registry.list()), 1)
        self.assertEqual(self.registry.get('DEV-7-I1')['session_id'], t.thread_id)
        t.assert_effects(1, 1)

    def test_post_trust_persistent_unknown_status_times_out_without_delivery(self):
        t = self.transport
        t.blocker = TRUST_SCREEN
        accepted = []
        def accept():
            t.accept_setup()
            accepted.append(t.now)
            t.get_changes = dict(agent_status='unknown')
            return '\n'
        self.human_input(accept)
        with patch.object(Codex, 'POST_TRUST_READY_TIMEOUT', 0.5), self.assertRaisesRegex(
                TaskError, 'Timed out reconciling Codex readiness'):
            cli.start('DEV-7')
        self.assertEqual(t.now, accepted[0] + 0.5)
        self.assertEqual(self.registry.get('DEV-7-I1')['state'], 'awaiting_user')
        t.assert_effects(1, 0)

    def test_post_trust_process_replacement_during_transient_status_refuses_delivery(self):
        t = self.transport
        t.blocker = TRUST_SCREEN
        pending = []
        def accept():
            t.accept_setup()
            pending.append(True)
            return '\n'
        def herdr(group, operation, *args, **kwargs):
            result = t.herdr(group, operation, *args, **kwargs)
            if pending and (group, operation) == ('agent', 'get'):
                pending.pop()
                result['agent']['agent_status'] = 'blocked'
                t.process_pid += 1
            return result
        t.command.side_effect = herdr
        self.human_input(accept)
        with self.assertRaisesRegex(TaskError, 'process changed during user action'):
            cli.start('DEV-7')
        t.assert_effects(1, 0)

    def test_post_trust_runtime_session_conflict_retains_first_observation(self):
        t = self.transport
        first = dict(agent='codex', kind='id', value='11a0d314-bd68-7203-8b68-f2520f892afa')
        t.blocker = TRUST_SCREEN
        pending = []
        def accept():
            t.accept_setup()
            pending.extend([first, dict(first, value=t.thread_id)])
            return '\n'
        def herdr(group, operation, *args, **kwargs):
            result = t.herdr(group, operation, *args, **kwargs)
            if pending and (group, operation) == ('agent', 'get'):
                result['agent'].update(agent_session=pending.pop(0), agent_status='blocked')
            return result
        t.command.side_effect = herdr
        self.human_input(accept)
        with self.assertRaisesRegex(TaskError, 'session changed'):
            cli.start('DEV-7')
        self.assertEqual(self.registry.get('DEV-7-I1')['session_id'], first['value'])
        t.assert_effects(1, 0)

    def test_post_trust_runtime_session_must_match_provider_discovery(self):
        t = self.transport
        first = dict(agent='codex', kind='id', value='11a0d314-bd68-7203-8b68-f2520f892afa')
        t.blocker = TRUST_SCREEN
        def accept():
            t.accept_setup()
            t.get_changes = dict(agent_session=first)
            return '\n'
        self.human_input(accept)
        with self.assertRaisesRegex(TaskError, 'session does not match the provider session'):
            cli.start('DEV-7')
        self.assertEqual(self.registry.get('DEV-7-I1')['session_id'], first['value'])
        t.assert_effects(1, 0)

    def test_post_trust_ready_observation_after_deadline_does_not_deliver(self):
        t = self.transport
        t.blocker = TRUST_SCREEN
        accepted, budgets = [], []
        def accept():
            t.accept_setup()
            accepted.append(t.now)
            return '\n'
        def herdr(group, operation, *args, **kwargs):
            result = t.herdr(group, operation, *args, **kwargs)
            if accepted:
                budgets.append(kwargs['timeout'])
                if (group, operation) == ('pane', 'read'):
                    t.advance(0.5)
            return result
        t.command.side_effect = herdr
        self.human_input(accept)
        with patch.object(Codex, 'POST_TRUST_READY_TIMEOUT', 0.5), self.assertRaisesRegex(
                TaskError, 'Timed out reconciling Codex readiness'):
            cli.start('DEV-7')
        self.assertEqual(budgets, [0.5, 0.5, 0.5])
        self.assertEqual(t.now, accepted[0] + 0.5)
        t.assert_effects(1, 0)

    def test_post_trust_provider_discovery_uses_remaining_runtime_budget(self):
        t = self.transport
        t.blocker, t.session_delay = TRUST_SCREEN, 100
        accepted, pending = [], []
        def accept():
            t.accept_setup()
            accepted.append(t.now)
            pending.append(True)
            return '\n'
        def herdr(group, operation, *args, **kwargs):
            result = t.herdr(group, operation, *args, **kwargs)
            if pending and (group, operation) == ('agent', 'get'):
                pending.pop()
                result['agent']['agent_status'] = 'blocked'
            return result
        t.command.side_effect = herdr
        self.human_input(accept)
        with patch.object(Codex, 'POST_TRUST_READY_TIMEOUT', 0.5), self.assertRaisesRegex(
                TaskError, 'readiness turn not found'):
            cli.start('DEV-7')
        self.assertEqual(t.now, accepted[0] + 0.5)
        calls = [c for c in t.rpc.request.call_args_list if c.args[0] == 'thread/list']
        self.assertEqual([c.kwargs['timeout'] for c in calls], [0.25])
        t.assert_effects(1, 0)

    def test_post_trust_delivery_history_is_rechecked_after_late_status_regression(self):
        t = self.transport
        t.blocker = TRUST_SCREEN
        reads, regressed = [], []
        self.human_input(lambda: (t.accept_setup(), '\n')[1])
        def request(method, params, **kwargs):
            if method == 'thread/items/list':
                reads.append(True)
            return t.request(method, params, **kwargs)
        def herdr(group, operation, *args, **kwargs):
            result = t.herdr(group, operation, *args, **kwargs)
            if len(reads) == 2 and not regressed and (group, operation) == ('agent', 'get'):
                regressed.append(True)
                result['agent']['agent_status'] = 'unknown'
                t.extra_items = [dict(turnId='external', item=dict(type='userMessage',
                    content=[dict(type='text', text='task already entered')]))]
            return result
        t.rpc.request.side_effect = request
        t.command.side_effect = herdr
        with self.assertRaisesRegex(TaskError, 'delivery is uncertain'):
            cli.start('DEV-7')
        self.assertEqual(regressed, [True])
        self.assertEqual(len(reads), 3)
        t.assert_effects(1, 0)

    def test_changed_process_during_history_reconciliation_prevents_delivery(self):
        t = self.transport
        t.blocker = TRUST_SCREEN
        self.human_input(lambda: (t.accept_setup(), '\n')[1])
        reads = []
        def request(method, params, **kwargs):
            result = t.request(method, params, **kwargs)
            if method == 'thread/items/list':
                reads.append(True)
                if len(reads) == 2:
                    t.process_pid += 1
            return result
        t.rpc.request.side_effect = request
        with self.assertRaisesRegex(TaskError, 'changed before delivery'):
            cli.start('DEV-7')
        t.assert_effects(1, 0)

    def test_additional_input_on_later_history_page_prevents_delivery(self):
        t = self.transport
        t.blocker = TRUST_SCREEN
        self.human_input(lambda: (t.accept_setup(), '\n')[1])
        def request(method, params, **kwargs):
            result = t.request(method, params, **kwargs)
            if method == 'thread/items/list':
                if params.get('cursor') == 'second':
                    return dict(data=[dict(turnId='external', item=dict(type='userMessage',
                        content=[dict(type='text', text='already delivered task')]))], nextCursor=None)
                result['nextCursor'] = 'second'
            return result
        t.rpc.request.side_effect = request
        with self.assertRaisesRegex(TaskError, 'delivery is uncertain'):
            cli.start('DEV-7')
        t.assert_effects(1, 0)

    def test_cyclic_history_pages_fail_closed(self):
        t = self.transport
        t.blocker = TRUST_SCREEN
        self.human_input(lambda: (t.accept_setup(), '\n')[1])
        def request(method, params, **kwargs):
            result = t.request(method, params, **kwargs)
            if method == 'thread/items/list':
                if params.get('cursor'):
                    return dict(data=[], nextCursor='cycle')
                result['nextCursor'] = 'cycle'
            return result
        t.rpc.request.side_effect = request
        with self.assertRaisesRegex(TaskError, 'ambiguous Codex history pagination'):
            cli.start('DEV-7')
        t.assert_effects(1, 0)

    def test_missing_process_evidence_is_not_accepted(self):
        self.transport.blocker = TRUST_SCREEN
        self.transport.process_changes = dict(foreground_processes=[])
        self.human_input(lambda: self.fail('missing process must fail closed'))
        with self.assertRaisesRegex(TaskError, 'Cannot prove the original'):
            cli.start('DEV-7')
        self.transport.assert_effects(1, 0)


class ShellReadinessTransportTests(unittest.TestCase):
    def test_agent_not_ready_transport_error_is_typed_without_echoing_output(self):
        from task_start.workspace import run
        error = json.dumps(dict(error=dict(code='agent_not_ready', message='sensitive UI text'))).encode()
        with patch('task_start.workspace.subprocess.run', return_value=subprocess.CompletedProcess(
                [], 1, b'', error)), self.assertRaises(AgentNotReady) as caught:
            run(['herdr', 'agent', 'start', 'task-name'])
        self.assertIn('agent_not_ready', str(caught.exception))
        self.assertNotIn('sensitive', str(caught.exception))

    def test_remaining_budget_reaches_subprocess_and_transport_timeout_stops_input(self):
        workspace = Workspace("dev-7-test", Path("/checkout"), "w1", "t1", "p1", "ready")
        adapter = Codex(AgentOptions("codex"))
        now, budgets = [0], []

        def advance(seconds):
            now[0] += seconds

        def observe(args, **kwargs):
            self.assertEqual(args, ["herdr", "pane", "process-info", "--pane", "p1"])
            budgets.append(kwargs["timeout"])
            if len(budgets) == 1:
                advance(0.125)
                process = dict(pane_id="p1", shell_pid=123, foreground_process_group_id=123,
                               foreground_processes=[dict(pid=123), dict(pid=124)])
                payload = dict(result=dict(type="pane_process_info", process_info=process))
                return subprocess.CompletedProcess(args, 0, json.dumps(payload).encode(), b"")
            advance(kwargs["timeout"])
            raise subprocess.TimeoutExpired(args, kwargs["timeout"])

        clock = SimpleNamespace(monotonic=lambda: now[0], sleep=advance)
        with patch("task_start.agent.time", clock), patch.object(adapter, "SHELL_READY_TIMEOUT", 1), \
                patch("task_start.workspace.subprocess.run", side_effect=observe), \
                self.assertRaisesRegex(TaskError, r"Timed out.*foreground_pids=\[123, 124\]"):
            adapter.clear_shell_input(workspace)
        self.assertEqual(budgets, [1, 0.625])
        self.assertEqual(now[0], 1)
