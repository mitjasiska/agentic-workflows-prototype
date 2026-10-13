import copy
from contextlib import ExitStack
from dataclasses import replace
import io
import json
import os
from pathlib import Path
import re
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from task_start import TaskError, cli
from task_start.agent import AgentExecution, AgentOptions, Codex, LaunchResult, Pi
from task_start.config import AgentConfig, LocalConfig, Project, ReviewValidationConfig, load_local
from task_start.contexts import ContextRegistry, HerdrContexts, context_observer, context_reference
from task_start.handoff import review_handoff
from task_start.review import finalize_reviewer, review
from task_start.review_result import parse_verdict
from task_start.review_state import snapshot
from task_start.metrics import MetricsStore
from task_start.sessions import SessionInvalid
from task_start.workspace import Git, Herdr, Workspace
from test_task_start import ISSUE
import test_task_start as baseline
from codex_startup_fixture import TRUST_SCREEN, CodexStartupTransport
import pass_delivery_fixture as delivery_fixture


def verdict(pass_id="pass", **changes):
    return dict(pass_id=pass_id, state="clean", summary="Checked requirements and implementation",
                findings=[], checks=[dict(name="unit", result="passed", details="All assertions passed")], **changes)


class ReviewTests(unittest.TestCase):
    """Real repositories/worktrees/registry; fake only Linear, config and agent/Herdr transport."""

    command = baseline.LocalGitIntegrationTests.command

    def setUp(self):
        baseline.LocalGitIntegrationTests.setUp(self)
        self.enterContext(patch.object(Pi, "clear_shell_input"))
        self.enterContext(patch.object(Pi, "filter_shell_credentials"))
        self.path = self.repo.parent / "task café"
        self.branch = "dev-7-original-title"
        self.command(self.repo, "worktree", "add", "-b", self.branch, str(self.path))
        self.git.save_scope(self.path, self.branch, "DEV-7", None)
        self.base = self.command(self.repo, "rev-parse", "HEAD")
        self.registry = ContextRegistry(self.repo.parent / "contexts.sqlite3")
        self.enterContext(patch("task_start.review.ContextRegistry", return_value=self.registry))
        self.panes = [dict(pane_id="p1", terminal_id="term1", workspace_id="w1", tab_id="t1",
                           cwd=str(self.path), agent="pi", agent_status="idle", label="DEV-7-I1",
                           agent_session=dict(agent="pi", kind="path", value="/implementation.jsonl"))]
        self.next_pane_number = 2
        self.implementation = self.registry.allocate("DEV-7", "implementation", agent="pi", model="implementation",
            mode="low", repository=str(self.repo), worktree=str(self.path), endpoint="/server.sock",
            workspace_id="w1", tab_id="t1", pane_id="p1", terminal_id="term1")
        self.registry.update(self.implementation, state="active", session_id="/implementation.jsonl", session_kind="path")
        self.identities = HerdrContexts()
        self.enterContext(patch.object(self.identities, "endpoint", return_value="/server.sock"))
        self.enterContext(patch.object(self.identities, "snapshot", side_effect=lambda: copy.deepcopy(self.panes)))
        self.identity_command = self.enterContext(patch.object(self.identities, "command", side_effect=self.pane_command))
        self.enterContext(patch("task_start.review.HerdrContexts", return_value=self.identities))
        self.enterContext(patch.object(Herdr, "command", side_effect=self.worktrees))
        self.enterContext(patch.object(Herdr, "workspaces", side_effect=lambda: [dict(workspace_id="w1", label="Renamed",
            worktree=dict(repo_root=str(self.repo), repo_key=str(self.repo / ".git"), repo_name=self.repo.name,
                          checkout_path=str(self.path), is_linked_worktree=True))]))
        self.local = LocalConfig(self.repo.parent, "placeholder", AgentConfig("pi", "implementer", "low"),
                                 reviewer=AgentConfig("codex", "review-model", "high"))
        self.config = self.enterContext(patch("task_start.review.load_local", side_effect=lambda: self.local))
        self.enterContext(patch("task_start.review.load_projects", return_value=[Project(ISSUE.project, self.repo.name, "main")]))
        self.linear = self.enterContext(patch("task_start.review.Linear")).return_value
        self.linear.get_issue.return_value = replace(ISSUE, title="Changed title", description="Current requirements")
        self.adapter = FakeReviewer(self)
        self.enterContext(patch("task_start.review.adapter_for", side_effect=self.select_adapter))
        self.prompts, self.options, self.recreated = [], [], []
        self.mutation = None
        self.raw = None
        self.verdict_overrides = {}
        self.status = "done"
        self.enterContext(patch("task_start.linear.urlopen", side_effect=AssertionError("Unexpected Linear access")))
        for method in ("update_base", "remote_branches", "check_history"):
            self.enterContext(patch.object(Git, method, side_effect=AssertionError("Unexpected remote/base mutation")))

    def select_adapter(self, options):
        self.options.append(options)
        return self.adapter

    def worktrees(self, operation, *args):
        self.assertEqual((operation, args), ("list", ()))
        return dict(source=dict(repo_root=str(self.repo)), worktrees=[dict(path=t["worktree"],
            branch=t.get("branch", "").removeprefix("refs/heads/"), label="Untrusted display name",
            is_linked_worktree=Path(t["worktree"]) != self.repo, is_bare=False, is_detached=False,
            is_prunable=False, open_workspace_id="w1" if Path(t["worktree"]) == self.path else None)
            for t in self.git.worktrees()])

    def pane_command(self, group, operation, *args):
        self.assertEqual(group, "pane")
        if operation == "split":
            anchor = next(p for p in self.panes if p["pane_id"] == args[0])
            number = self.next_pane_number
            self.next_pane_number += 1  # Closed runtime IDs are never reused by Herdr.
            pane = dict(anchor, pane_id=f"p{number}", terminal_id=f"term{number}", agent=None,
                        agent_session=None, label="Shell")
            self.assertEqual(args[1:], ("--direction", "down", "--cwd", str(self.path), "--no-focus"))
            self.panes.append(pane)
            return dict(pane=copy.deepcopy(pane))
        pane = next(p for p in self.panes if p["pane_id"] == args[0])
        if operation == "rename":
            pane["label"] = args[1]
        return dict(pane=copy.deepcopy(pane))

    def test_project_defaults_apply_to_fresh_review_but_resume_keeps_original_selection(self):
        from test_layered_config import install_lifecycle_layers
        local = install_lifecycle_layers(self)
        first = review('DEV-7', model='cli-reviewer')
        self.assertEqual(first.state, 'clean', first.render())
        context = self.registry.get(first.context_id)
        self.assertEqual((context['agent'], context['model'], context['mode']),
                         ('codex', 'cli-reviewer', 'medium'))
        local.write_text('invalid = [')
        resumed = review('DEV-7', resume=first.context_id)
        self.assertEqual(resumed.state, 'clean', resumed.render())
        self.assertEqual(resumed.execution, first.execution)
        self.assertEqual(resumed.context_id, first.context_id)
        before = len(self.prompts)
        with self.assertRaisesRegex(TaskError, 'valid project TOML'):
            review('DEV-7')
        self.assertEqual(len(self.prompts), before)

    def test_fresh_multiple_resume_latest_intent_same_tab_and_saved_selection(self):
        first, second = review("DEV-7"), review("DEV-7")
        self.assertEqual([first.context_id, second.context_id], ["DEV-7-R1", "DEV-7-R2"])
        self.assertEqual([first.state, second.state], ["clean", "clean"])
        self.assertEqual(len(self.panes), 3)
        self.assertEqual({p["tab_id"] for p in self.panes}, {"t1"})
        self.assertNotEqual(self.registry.get(first.context_id)["session_id"], self.registry.get(second.context_id)["session_id"])
        self.local = replace(self.local, reviewer=AgentConfig("pi", "changed-default", "low"))
        self.linear.get_issue.return_value = replace(ISSUE, description="Fresh requirements for follow-up")
        resumed = review("DEV-7", resume=first.context_id)
        self.assertEqual(resumed.state, "clean")
        self.assertEqual(resumed.pass_kind, "resumed")
        self.assertEqual(resumed.context_id, first.context_id)
        self.assertEqual(resumed.execution, first.execution)
        self.assertEqual(self.recreated, [False])
        self.assertEqual(len(self.panes), 3)
        self.assertIn("Fresh requirements for follow-up", self.prompts[-1])
        self.assertEqual(self.linear.get_issue.call_count, 3)
        self.linear.start.assert_not_called()
        self.assertEqual(len(self.registry.list()), 3)
        self.assertFalse(Path(self.adapter.output).exists())
        metrics = MetricsStore(self.registry.path).read()
        self.assertEqual([r["run_id"] for r in metrics], [first.pass_id, second.pass_id, resumed.pass_id])
        self.assertEqual([r["role"] for r in metrics], ["review", "review", "re-review"])
        self.assertEqual([r["context_id"] for r in metrics], [first.context_id, second.context_id, first.context_id])
        self.assertTrue(all(r["loop_id"] is None and r["review_round"] is None for r in metrics))
        self.assertTrue(all(r["status"] == "completed" and r["findings_count"] == 0 for r in metrics))
        self.assertTrue(all(r["implementation_context_id"] == self.implementation for r in metrics))

    def test_standalone_metrics_keep_verified_implementation_identity_on_fresh_and_resume(self):
        # Retired I1 and a misleading pane label must not replace the active I2 identity.
        self.registry.update(self.implementation, state="retired")
        self.implementation = self.registry.allocate("DEV-7", "implementation", agent="pi", model="implementation",
            mode="low", repository=str(self.repo), worktree=str(self.path), endpoint="/server.sock",
            workspace_id="w1", tab_id="t1", pane_id="p1", terminal_id="term1")
        self.registry.update(self.implementation, state="active", session_id="/implementation.jsonl", session_kind="path")
        self.assertEqual(self.implementation, "DEV-7-I2")
        self.assertEqual(self.panes[0]["label"], "DEV-7-I1")
        fresh = review("DEV-7")
        resumed = review("DEV-7", resume=fresh.context_id)
        self.assertEqual((fresh.state, resumed.state), ("clean", "clean"))
        metrics = MetricsStore(self.registry.path).read()
        self.assertEqual([r["run_id"] for r in metrics], [fresh.pass_id, resumed.pass_id])
        self.assertEqual([r["role"] for r in metrics], ["review", "re-review"])
        self.assertEqual([r["implementation_context_id"] for r in metrics], ["DEV-7-I2", "DEV-7-I2"])
        self.assertEqual([r["context_id"] for r in metrics], [fresh.context_id, fresh.context_id])
        self.assertTrue(all(r["loop_id"] is None and r["previous_run_id"] is None for r in metrics))

    def test_metrics_record_finalization_failure_after_blocked_runtime(self):
        self.status = "blocked"
        with patch("task_start.review.finalize_reviewer", side_effect=TaskError("identity changed")):
            result = review("DEV-7")
        self.assertEqual(result.state, "failed")
        metric, = MetricsStore(self.registry.path).read()
        self.assertEqual(metric["status"], "failed")
        self.assertNotIn("review_outcome", metric)
        self.assertNotIn("findings_count", metric)

    def codex_transport(self):
        self.enterContext(patch("task_start.review.adapter_for", side_effect=Codex))
        transport = CodexStartupTransport(self, lambda: self.panes)
        def write_result(prompt):
            self.prompts.append(prompt)
            output = re.search(r"output-file write is (.*?)\. This temporary", prompt).group(1)
            pass_id = re.search(r"Pass ID: ([^\n]+)", prompt).group(1)
            Path(output).write_text(json.dumps(verdict(pass_id)))
        transport.on_queue = write_result
        return transport

    def test_fresh_codex_review_waits_for_shell_and_session_then_delivers_once(self):
        transport = self.codex_transport()
        result = review("DEV-7")
        self.assertEqual((result.context_id, result.state, result.invalidated), ("DEV-7-R1", "clean", False))
        self.assertEqual(transport.process_reads, 2)
        self.assertEqual(transport.started_at, 0.25)
        self.assertEqual(transport.now, 1.75)
        transport.assert_effects(1, 1)
        self.assertEqual(len(self.prompts), 1)
        row = self.registry.get(result.context_id)
        self.assertEqual((row["state"], row["session_id"], row["resumability"]),
                         ("active", transport.thread_id, "yes"))

    def test_fresh_codex_review_trust_wait_preserves_pass_and_refuses_replacement(self):
        transport = self.codex_transport()
        transport.blocker = TRUST_SCREEN
        def accept():
            row = self.registry.get('DEV-7-R1')
            self.assertEqual((row['state'], row['session_id']), ('awaiting_user', None))
            pane_count = len(self.panes)
            with self.assertRaisesRegex(TaskError, 'owns this worktree'):
                review('DEV-7')
            self.assertEqual(len(self.panes), pane_count)
            transport.advance(2000)  # Human time does not consume the review result budget.
            transport.accept_setup()
            return '\n'
        with patch('task_start.agent.sys.stdin') as stdin, patch('task_start.agent.sys.stderr', new_callable=io.StringIO):
            stdin.isatty.return_value = True
            stdin.readline.side_effect = accept
            result = review('DEV-7', timeout=2)
        self.assertEqual((result.context_id, result.state, result.invalidated), ('DEV-7-R1', 'clean', False), result.summary)
        self.assertEqual([c['context_id'] for c in self.registry.list() if c['role'] == 'review'], ['DEV-7-R1'])
        self.assertEqual(len(self.prompts), 1)
        transport.assert_effects(1, 1)

    def test_fresh_codex_review_abandoned_trust_wait_keeps_context(self):
        transport = self.codex_transport()
        transport.blocker = TRUST_SCREEN
        with patch('task_start.agent.sys.stdin') as stdin, patch('task_start.agent.sys.stderr', new_callable=io.StringIO):
            stdin.isatty.return_value = False
            result = review('DEV-7')
        self.assertEqual(result.state, 'failed')
        self.assertIn('stdin is not a terminal', result.summary)
        self.assertNotIn('could not be finalized', result.summary)
        self.assertEqual(self.registry.get('DEV-7-R1')['state'], 'awaiting_user')
        with self.assertRaisesRegex(TaskError, 'waiting for Codex trust/setup'):
            review('DEV-7')
        transport.assert_effects(1, 0)

    def test_fresh_codex_review_gets_full_execution_timeout_after_slow_startup(self):
        transport = self.codex_transport()
        execution_timeout = 0.5
        # Confirm the handoff normally, but leave the review unfinished so its
        # entire execution budget elapses on the transport's mocked clock.
        transport.on_queue = self.prompts.append
        poll_times = []

        def pending(*args):
            poll_times.append(transport.now)
            return "working"

        with patch.object(Codex, "review_status", side_effect=pending):
            result = review("DEV-7", timeout=execution_timeout)

        handoff_completed_at = transport.queued_at + transport.receipt_delay
        self.assertGreater(handoff_completed_at, execution_timeout)
        self.assertEqual(poll_times, [handoff_completed_at, handoff_completed_at + 0.25])
        self.assertEqual(transport.now, handoff_completed_at + execution_timeout)
        self.assertEqual((result.state, result.invalidated), ("failed", False))
        self.assertIn("Timed out waiting for validated reviewer output", result.summary)
        transport.assert_effects(1, 1)

    def test_fresh_codex_review_shell_timeout_preserves_primary_failure_and_ordinal(self):
        transport = self.codex_transport()
        transport.processes = transport.processes[:1]
        with patch.object(Codex, "SHELL_READY_TIMEOUT", 0.5), \
                patch("task_start.review.finalize_reviewer") as finalize:
            result = review("DEV-7")
        self.assertEqual((result.context_id, result.state, result.invalidated), ("DEV-7-R1", "failed", False))
        self.assertIn("pane readiness", result.summary)
        self.assertIn("foreground_pids=[123, 124, 125]", result.summary)
        self.assertNotIn("could not be finalized", result.summary)
        finalize.assert_not_called()
        transport.keys.assert_not_called()
        transport.assert_effects(0, 0)
        row = self.registry.get(result.context_id)
        self.assertEqual((row["state"], row["session_id"], row["resumability"]), ("uncertain", None, "unknown"))
        self.assertEqual(row["terminal_id"], "term2")
        transport.processes = [dict(shell_pid=123, foreground_process_group_id=123, foreground_processes=[dict(pid=123)])]
        second = review("DEV-7")
        self.assertEqual((second.context_id, second.state), ("DEV-7-R2", "clean"))
        self.assertEqual(self.registry.get("DEV-7-R1"), row)
        self.assertEqual(len(self.panes), 3)

    def test_fresh_codex_review_rejects_ready_observation_after_readiness_deadline(self):
        transport = self.codex_transport()
        transport.processes = transport.processes[-1:]
        transport.process_durations = [31]
        result = review("DEV-7")
        self.assertEqual((result.state, result.invalidated), ("failed", False))
        self.assertIn("pane readiness", result.summary)
        self.assertIn("Timed out", result.summary)
        self.assertNotIn("could not be finalized", result.summary)
        self.assertEqual(transport.process_timeouts, [30])
        transport.keys.assert_not_called()
        transport.assert_effects(0, 0)
        row = self.registry.get(result.context_id)
        self.assertEqual((row["state"], row["session_id"], row["resumability"]), ("uncertain", None, "unknown"))

    def test_fresh_codex_review_queue_failure_keeps_observed_identity_uncertain(self):
        transport = self.codex_transport()
        transport.queue_error = TaskError("queue acknowledgement lost")
        with patch("task_start.review.finalize_reviewer") as finalize:
            result = review("DEV-7")
        self.assertEqual(result.state, "failed")
        self.assertIn("prompt queue", result.summary)
        self.assertIn("queue acknowledgement lost", result.summary)
        self.assertNotIn("could not be finalized", result.summary)
        finalize.assert_not_called()
        transport.assert_effects(1, 1)
        row = self.registry.get(result.context_id)
        self.assertEqual((row["state"], row["session_id"], row["resumability"]),
                         ("uncertain", transport.thread_id, "yes"))
        with self.assertRaisesRegex(TaskError, "stale, busy, or non-resumable"):
            review("DEV-7", resume=result.context_id)

    def test_recreate_missing_pane_preserves_session_context_and_settings(self):
        first = review("DEV-7")
        original = self.registry.get(first.context_id)
        self.panes = [p for p in self.panes if p["pane_id"] != original["pane_id"]]
        result = review("DEV-7", resume=first.context_id)
        self.assertEqual(result.state, "clean")
        rebound = self.registry.get(first.context_id)
        self.assertEqual(rebound["session_id"], original["session_id"])
        self.assertNotEqual(rebound["pane_id"], original["pane_id"])
        self.assertEqual(self.recreated, [True])

    def test_resume_refuses_unknown_mismatched_retired_busy_nonresumable_and_overrides(self):
        first = review("DEV-7")
        for context_id in ("DEV-7-R99", "DEV-7-I1", "DEV-8-R1", "latest"):
            with self.subTest(context_id=context_id), self.assertRaises(TaskError):
                review("DEV-7", resume=context_id)
        with self.assertRaisesRegex(TaskError, "preserves"):
            review("DEV-7", resume=first.context_id, model="override")
        for values in (dict(state="uncertain"), dict(state="reviewing"), dict(state="retired"), dict(resumability="no")):
            self.registry.update(first.context_id, state="active", resumability="yes")
            self.registry.update(first.context_id, **values)
            with self.subTest(values=values), self.assertRaises(TaskError):
                review("DEV-7", resume=first.context_id)
        self.assertEqual(len(self.panes), 2)

    def test_resume_rejects_different_live_session_or_terminal(self):
        first = review("DEV-7")
        original = copy.deepcopy(self.panes[-1])
        for changes in (dict(agent_session=dict(agent="codex", kind="id", value="other")),
                        dict(terminal_id="replacement")):
            self.panes[-1] = dict(original, **changes)
            with self.subTest(changes=changes), self.assertRaises(TaskError):
                review("DEV-7", resume=first.context_id)

    def test_relocated_reviewer_keeps_context_session_and_persists_location(self):
        first = review("DEV-7")
        original = self.registry.get(first.context_id)
        self.panes[-1].update(pane_id="relocated", tab_id="another-tab")
        result = review("DEV-7", resume=first.context_id)
        self.assertEqual((result.context_id, result.state, result.invalidated), (first.context_id, "clean", False))
        saved = ContextRegistry(self.registry.path).get(first.context_id)
        self.assertEqual((saved["pane_id"], saved["tab_id"]), ("relocated", "another-tab"))
        self.assertEqual(dict(saved, pane_id=original["pane_id"], tab_id=original["tab_id"]), original)
        self.assertEqual(self.recreated, [False])
        self.assertEqual(len(self.panes), 2)

    def test_relocation_during_polling_does_not_change_pass_or_identity(self):
        calls = []
        def poll(execution, terminal, reference):
            calls.append(execution.workspace.pane_id)
            if len(calls) == 1:
                self.panes[-1].update(pane_id="relocated", tab_id="another-tab")
                return "working"
            self.assertEqual(terminal, "term2")
            return "done"
        with patch.object(self.adapter, "review_status", side_effect=poll):
            result = review("DEV-7")
        self.assertEqual(calls, ["p2", "relocated"])
        self.assertEqual((result.state, result.invalidated), ("clean", False))
        saved = self.registry.get(result.context_id)
        self.assertEqual((saved["pane_id"], saved["terminal_id"], saved["resumability"]),
                         ("relocated", "term2", "yes"))
        self.assertEqual(saved["session_id"], "session-p2")

    def test_relocation_rejects_ambiguous_or_conflicting_runtime_evidence(self):
        first = review("DEV-7")
        original = self.registry.get(first.context_id)
        pane = dict(self.panes[-1], pane_id="relocated")
        cases = [
            [pane, dict(pane, pane_id="duplicate")],
            [dict(pane, agent_session=dict(agent="codex", kind="id", value="replacement"))],
            [dict(pane, workspace_id="another-workspace")],
            [dict(pane, pane_id=original["pane_id"], terminal_id="replacement"), pane],
        ]
        for candidates in cases:
            with self.subTest(candidates=candidates):
                self.panes[1:] = candidates
                with self.assertRaises(TaskError):
                    review("DEV-7", resume=first.context_id)
                self.assertEqual(self.registry.get(first.context_id), original)
        self.assertEqual(self.recreated, [])

    def test_relocation_at_finalization_preserves_verified_context(self):
        first = review("DEV-7")
        original = self.registry.get(first.context_id)
        self.panes[-1].update(pane_id="relocated", tab_id="another-tab")
        workspace = Workspace(self.branch, self.path, "w1", original["tab_id"], original["pane_id"], "review")
        finalize_reviewer(first.context_id, workspace, self.adapter, self.registry, self.identities)
        saved = self.registry.get(first.context_id)
        self.assertEqual((saved["pane_id"], saved["tab_id"]), ("relocated", "another-tab"))
        self.assertEqual(dict(saved, pane_id=original["pane_id"], tab_id=original["tab_id"]), original)
        self.assertEqual(review("DEV-7", resume=first.context_id).state, "clean")

    def test_labels_and_position_do_not_select_resume(self):
        first = review("DEV-7")
        self.panes.reverse()
        self.panes[0]["label"] = "Human renamed"
        result = review("DEV-7", resume=first.context_id)
        self.assertEqual(result.context_id, first.context_id)
        self.assertEqual(result.state, "clean")

    def test_task_resolution_refuses_ambiguity_missing_scope_and_registry_divergence(self):
        self.command(self.repo, "branch", "dev-7-other")
        with self.assertRaisesRegex(TaskError, "Ambiguous"):
            review("DEV-7")
        self.command(self.repo, "branch", "-d", "dev-7-other")
        self.git.scope_file(self.path).unlink()
        with self.assertRaisesRegex(TaskError, "scope metadata is missing"):
            review("DEV-7")
        self.git.save_scope(self.path, self.branch, "DEV-7", None)
        self.registry.allocate("DEV-7", "review", agent="codex", repository="/elsewhere")
        with self.assertRaisesRegex(TaskError, "inconsistent"):
            review("DEV-7")
        self.assertEqual(len(self.panes), 1)

    def test_dirty_current_worktree_is_reviewed_and_any_actor_mutation_invalidates(self):
        (self.path / "new.py").write_text("before review")
        for actor in ("reviewer", "human", "implementer"):
            self.mutation = lambda actor=actor: (self.path / "new.py").write_text(actor)
            result = review("DEV-7")
            self.assertEqual(result.state, "blocked")
            self.assertTrue(result.invalidated)
            self.assertIn("changed during review", result.summary)
        self.assertEqual(len(self.panes), 4)

    def test_malformed_output_never_clean_and_new_pass_can_resume(self):
        self.raw = "Here is a clean review"
        result = review("DEV-7")
        self.assertEqual(result.state, "failed")
        self.assertIn("Malformed", result.summary)
        self.assertEqual(self.registry.get(result.context_id)["state"], "active")

    def test_missing_output_and_blocked_runtime_do_not_pass(self):
        self.raw = "missing"
        result = review("DEV-7", timeout=0.01)
        self.assertEqual(result.state, "failed")
        self.status = "blocked"
        result = review("DEV-7")
        self.assertEqual(result.state, "blocked")

    def test_snapshot_semantic_index_head_binary_untracked_ignore_and_mtimes(self):
        before = snapshot(self.path, self.base, self.branch)
        tracked = self.path / "tracked.txt"
        os.utime(tracked, None)
        self.command(self.path, "update-index", "--refresh")
        self.assertEqual(snapshot(self.path, self.base, self.branch), before)
        tracked.write_bytes(b"binary\x00change")
        dirty = snapshot(self.path, self.base, self.branch)
        self.assertNotEqual(before, dirty)
        self.command(self.path, "add", "tracked.txt")
        staged = snapshot(self.path, self.base, self.branch)
        self.assertNotEqual(staged, dirty)
        new = self.path / "untracked\nfile"
        new.write_bytes(b"one")
        one = snapshot(self.path, self.base, self.branch)
        new.write_bytes(b"two")
        self.assertNotEqual(one, snapshot(self.path, self.base, self.branch))
        (self.path / ".gitignore").write_text("generated/\n")
        ignored = snapshot(self.path, self.base, self.branch)
        (self.path / "generated").mkdir()
        (self.path / "generated" / "artifact").write_text("validation result")
        self.assertEqual(ignored, snapshot(self.path, self.base, self.branch))
        self.command(self.path, "commit", "-m", "test change")
        self.assertNotEqual(ignored, snapshot(self.path, self.base, self.branch))

    def test_assume_unchanged_and_symlink_changes_are_visible(self):
        self.command(self.path, "update-index", "--assume-unchanged", "tracked.txt")
        before = snapshot(self.path, self.base, self.branch)
        (self.path / "tracked.txt").write_text("hidden change")
        self.assertNotEqual(before, snapshot(self.path, self.base, self.branch))
        link = self.path / "link"
        link.symlink_to("tracked.txt")
        before = snapshot(self.path, self.base, self.branch)
        link.unlink()
        link.symlink_to("elsewhere")
        self.assertNotEqual(before, snapshot(self.path, self.base, self.branch))

    def test_base_is_pinned_even_if_branch_advances(self):
        def advance():
            self.command(self.repo, "commit", "--allow-empty", "-m", "base advanced")
        self.mutation = advance
        result = review("DEV-7")
        self.assertEqual(result.state, "clean")
        self.assertEqual(result.review_state["base_commit"], self.base)
        self.assertNotEqual(self.command(self.repo, "rev-parse", "HEAD"), self.base)

    def test_mutation_seen_while_waiting_stays_invalid_after_restore(self):
        original = (self.path / "tracked.txt").read_bytes()
        calls = 0
        def status(*args):
            nonlocal calls
            calls += 1
            (self.path / "tracked.txt").write_bytes(b"human edit" if calls == 1 else original)
            return "working" if calls < 3 else "done"
        with patch.object(self.adapter, "review_status", side_effect=status):
            result = review("DEV-7")
        self.assertEqual(result.state, "blocked")
        self.assertTrue(result.invalidated)
        self.assertEqual(result.post_fingerprint, result.review_state["fingerprint"])

    def test_launch_and_resume_checkpoints_keep_drift_after_first_poll_restores_bytes(self):
        original = (self.path / "tracked.txt").read_bytes()
        context_id = None
        for pass_kind in ("fresh", "resumed"):
            with self.subTest(pass_kind=pass_kind):
                self.mutation = lambda: (self.path / "tracked.txt").write_bytes(b"edit during delivery")
                def restore(*args):
                    (self.path / "tracked.txt").write_bytes(original)
                    return "done"
                with patch.object(self.adapter, "review_status", side_effect=restore):
                    result = review("DEV-7", resume=context_id)
                context_id = result.context_id
                self.assertEqual(result.pass_kind, pass_kind)
                self.assertEqual(result.state, "blocked")
                self.assertTrue(result.invalidated)
                self.assertEqual(result.post_fingerprint, result.review_state["fingerprint"])

    def test_status_return_checkpoint_detects_drift_restored_before_final_acceptance(self):
        original = (self.path / "tracked.txt").read_bytes()
        def status(*args):
            (self.path / "tracked.txt").write_bytes(b"human edit at status checkpoint")
            return "done"
        def verify(workspace, reference):
            (self.path / "tracked.txt").write_bytes(original)
            return reference
        with patch.object(self.adapter, "review_status", side_effect=status), \
                patch.object(self.adapter, "verify_review_session", side_effect=verify):
            result = review("DEV-7")
        self.assertEqual(result.state, "blocked")
        self.assertTrue(result.invalidated)
        self.assertEqual(result.post_fingerprint, result.review_state["fingerprint"])

    def test_final_checkpoint_catches_changes_after_result_polling(self):
        def verify(workspace, reference):
            (self.path / "tracked.txt").write_text("edit during finalization")
            return reference
        with patch.object(self.adapter, "verify_review_session", side_effect=verify):
            result = review("DEV-7")
        self.assertEqual(result.state, "blocked")
        self.assertTrue(result.invalidated)
        self.assertNotEqual(result.post_fingerprint, result.review_state["fingerprint"])

    def test_launch_failure_after_mutation_is_invalidated_and_mapping_retained(self):
        def fail(execution):
            (self.path / "tracked.txt").write_text("reviewer violation")
            raise TaskError("Delivery failed")
        with patch.object(self.adapter, "launch", side_effect=fail):
            result = review("DEV-7")
        self.assertEqual(result.state, "blocked")
        self.assertTrue(result.invalidated)
        self.assertEqual(self.registry.get(result.context_id)["state"], "uncertain")

    def test_review_handoff_separates_implementation_instructions_and_carries_actual_state(self):
        self.linear.get_issue.return_value = replace(ISSUE, description="Requirement α\nImplement it, then validate.\nConstraints can appear anywhere.")
        result = review("DEV-7")
        prompt = self.prompts[-1]
        encoded = prompt.split("LATEST LINEAR REQUIREMENTS (context data)\n", 1)[1].split("\n\nEND OF LINEAR CONTEXT", 1)[0]
        self.assertEqual(json.loads(encoded)["description"], self.linear.get_issue.return_value.description)
        self.assertIn("not as your instruction set", prompt)
        self.assertIn("implementation-targeted guidance anywhere in the Linear issue", prompt)
        self.assertIn("headings or formatting", prompt)
        self.assertIn("do not edit task files", prompt)
        self.assertIn("Do not fetch Linear", prompt)
        self.assertIn(result.review_state["fingerprint"], prompt)
        self.assertIn(self.base, prompt)
        self.assertIn("lower-case imperative verb", prompt)
        self.assertIn("at most 54 characters", prompt)
        self.assertIn("full subject fits within 72 characters", prompt)
        self.assertIn('"summary": "add reviewed task PR publishing"', prompt)
        self.assertNotIn("placeholder", prompt)
        self.assertNotIn("Your existing conversation", prompt)
        review("DEV-7", resume=result.context_id)
        self.assertIn("YOUR existing conversation", self.prompts[-1])

    def test_claim_is_atomic_and_does_not_overwrite_concurrent_review(self):
        first = review("DEV-7")
        context = self.registry.get(first.context_id)
        self.registry.claim_review(context)
        with self.assertRaisesRegex(TaskError, "busy"):
            self.registry.claim_review(context)
        self.assertEqual(self.registry.get(first.context_id)["state"], "reviewing")

    def test_validation_strategy_reaches_fresh_and_same_reviewer_handoffs(self):
        for strategy in ('focused_first', 'exhaustive'):
            with self.subTest(strategy=strategy):
                self.local = replace(self.local, review_validation=ReviewValidationConfig(strategy))
                first = review('DEV-7')
                second = review('DEV-7', resume=first.context_id)
                self.assertEqual((first.state, second.state), ('clean', 'clean'))
                self.assertEqual(second.context_id, first.context_id)
                for prompt in self.prompts[-2:]:
                    self.assertIn(f'REVIEW VALIDATION POLICY: {strategy}', prompt)

    def test_invalid_validation_config_precedes_review_execution(self):
        before = self.registry.list()
        with patch('task_start.config.read_toml', return_value=dict(projects_root='/projects',
                linear=dict(api_key='test-placeholder'), review=dict(validation=dict(strategy='invalid')))), \
                patch('task_start.review.load_local', side_effect=load_local):
            with self.assertRaisesRegex(TaskError, 'review.validation.strategy'):
                review('DEV-7')
        self.linear.get_issue.assert_not_called()
        self.assertEqual(self.registry.list(), before)
        self.identity_command.assert_not_called()
        self.assertEqual(self.prompts, [])

    def test_settings_must_be_explicit_and_failed_resume_does_not_create_fresh(self):
        self.local = replace(self.local, reviewer=AgentConfig("codex"))
        with self.assertRaisesRegex(TaskError, "explicit model and mode"):
            review("DEV-7")
        self.assertEqual(len(self.panes), 1)
        self.local = replace(self.local, reviewer=AgentConfig("codex", "model", "high"))
        first = review("DEV-7")
        with patch.object(self.adapter, "verify_review_session", side_effect=TaskError("History missing")):
            with self.assertRaisesRegex(TaskError, "History missing"):
                review("DEV-7", resume=first.context_id)
        self.assertEqual(len(self.panes), 2)

    def test_same_terminal_can_be_rearranged_into_another_task_tab(self):
        first = review("DEV-7")
        self.panes[-1]["tab_id"] = "human-created-task-tab"
        self.assertEqual(review("DEV-7", resume=first.context_id).state, "clean")

    def test_model_verdict_does_not_determine_context_health(self):
        for state in ("clean", "findings", "blocked", "failed"):
            self.verdict_overrides = dict(state=state, findings=[dict(severity="medium", explanation="Missing check",
                evidence="tracked.txt:1", requirement="validation")] if state == "findings" else [])
            with self.subTest(state=state):
                result = review("DEV-7")
                row = self.registry.get(result.context_id)
                self.assertEqual(result.state, state)
                self.assertFalse(result.invalidated)
                self.assertEqual(result.post_fingerprint, result.review_state["fingerprint"])
                self.assertEqual((row["state"], row["resumability"]), ("active", "yes"))

    def test_timeout_and_keyboard_interrupt_preserve_verified_reviewer_for_resume(self):
        first = review("DEV-7")
        original = self.registry.get(first.context_id)
        self.status = "working"
        result = review("DEV-7", resume=first.context_id, timeout=0.01)
        self.assertEqual(result.state, "failed")
        self.assertIn("Timed out", result.summary)
        self.assertEqual(self.registry.get(first.context_id)["state"], "active")
        with patch.object(self.adapter, "review_status", side_effect=KeyboardInterrupt), self.assertRaises(KeyboardInterrupt):
            review("DEV-7", resume=first.context_id)
        row = self.registry.get(first.context_id)
        self.assertEqual((row["state"], row["resumability"]), ("active", "yes"))
        for key in ("pane_id", "terminal_id", "session_id", "model", "mode"):
            self.assertEqual(row[key], original[key])
        self.status = "done"
        self.assertEqual(review("DEV-7", resume=first.context_id).state, "clean")

    def test_finalization_rejects_replaced_or_ambiguous_runtime(self):
        for change in ("replaced", "ambiguous", "session"):
            with self.subTest(change=change):
                original = None
                def mutate(*args):
                    nonlocal original
                    pane = self.panes[-1]
                    original = copy.deepcopy(pane)
                    if change == "replaced":
                        pane["terminal_id"] = "replacement"
                    elif change == "ambiguous":
                        self.panes.append(dict(pane, pane_id="duplicate"))
                    else:
                        pane["agent_session"] = dict(agent="codex", kind="id", value="replacement")
                    return "done"
                with patch.object(self.adapter, "review_status", side_effect=mutate):
                    result = review("DEV-7")
                self.assertEqual(result.state, "failed")
                row = self.registry.get(result.context_id)
                self.assertEqual((row["state"], row["resumability"]), ("uncertain", "unknown"))
                self.assertEqual(row["terminal_id"], original["terminal_id"])
                self.assertEqual(row["session_id"], original["agent_session"]["value"])
                with self.assertRaises(TaskError):
                    review("DEV-7", resume=result.context_id)
                self.panes = self.panes[:1]

    def test_ignored_validation_artifacts_do_not_invalidate(self):
        self.mutation = lambda: (self.path / "ignored.txt").write_text("validation output")
        self.assertEqual(review("DEV-7").state, "clean")

    def test_staged_deletion_ignored_remaining_file_is_fingerprinted(self):
        self.command(self.path, "rm", "--cached", "tracked.txt")
        (self.path / ".gitignore").write_text("tracked.txt\n")
        before = snapshot(self.path, self.base, self.branch)
        (self.path / "tracked.txt").write_text("modified after staged removal")
        self.assertNotEqual(snapshot(self.path, self.base, self.branch), before)

    def test_submodule_tracked_and_untracked_contents_are_fingerprinted(self):
        self.command(self.path, "-c", "protocol.file.allow=always", "submodule", "add", str(self.remote), "module")
        module = self.path / "module"
        self.command(module, "checkout", "--detach")
        before = snapshot(self.path, self.base, self.branch)
        (module / "tracked.txt").write_text("nested task change")
        after = snapshot(self.path, self.base, self.branch)
        self.assertNotEqual(before, after)
        (module / "new-file").write_text("new nested code")
        self.assertNotEqual(after, snapshot(self.path, self.base, self.branch))

    def test_failure_after_model_clean_cannot_leave_workflow_clean(self):
        update = self.registry.update
        def fail(context_id, **values):
            if values == dict(state="active", resumability="yes"):
                raise TaskError("Registry write unavailable")
            update(context_id, **values)
        with patch.object(self.registry, "update", side_effect=fail):
            result = review("DEV-7")
        self.assertEqual(result.state, "failed")
        self.assertIn("Registry write unavailable", result.summary)

    def test_controlled_cli_smoke_fresh_then_resume_after_changes(self):
        def invoke(*args):
            with patch("task_start.cli.review", wraps=review), patch("sys.stdout", new_callable=io.StringIO) as out:
                self.assertEqual(cli.main(["review", "DEV-7", "--json", *args]), 0)
                return json.loads(out.getvalue())
        fresh = invoke()
        (self.path / "new-fix.py").write_text("# Fix made between review passes\n")
        self.linear.get_issue.return_value = replace(ISSUE, description="Latest follow-up requirements")
        resumed = invoke("--resume", fresh["context_id"])
        self.assertEqual(fresh["context_id"], resumed["context_id"])
        self.assertEqual((fresh["pass_kind"], resumed["pass_kind"]), ("fresh", "resumed"))
        self.assertNotEqual(fresh["pass_id"], resumed["pass_id"])
        self.assertNotEqual(fresh["review_state"]["fingerprint"], resumed["review_state"]["fingerprint"])
        for result in (fresh, resumed):
            self.assertEqual(result["review_state"]["fingerprint"], result["post_fingerprint"])
            self.assertFalse(result["invalidated"])
        self.assertEqual(self.linear.get_issue.call_count, 2)
        self.assertEqual(self.recreated, [False])

    def test_duplicate_context_session_binding_is_rejected(self):
        first, second = review("DEV-7"), review("DEV-7")
        self.registry.update(second.context_id, session_id=self.registry.get(first.context_id)["session_id"])
        with self.assertRaisesRegex(TaskError, "same.*identity"):
            review("DEV-7", resume=first.context_id)
        self.assertEqual(len(self.panes), 3)

    def test_explicit_invalid_resume_selectors_never_allocate_or_launch(self):
        before = self.registry.list(include_retired=True)
        for selector in ("", " ", "\t\n", "DEV-7", "DEV-7-I1", "DEV-7-R0", "DEV-7-R1 extra"):
            with self.subTest(selector=selector), self.assertRaisesRegex(TaskError, "explicit review context ID"):
                review("DEV-7", resume=selector)
            self.assertEqual(self.registry.list(include_retired=True), before)
        self.config.assert_not_called()
        self.assertEqual(len(self.panes), 1)
        self.assertEqual(self.prompts, [])
        self.assertEqual(review("DEV-7", resume=None).context_id, "DEV-7-R1")

    def test_cli_explicit_invalid_resume_selectors_fail_before_allocation(self):
        for selector in ("", "   ", "DEV-7-R-1", "latest"):
            with self.subTest(selector=selector), patch("task_start.cli.review", wraps=review), \
                    patch("sys.stderr", new_callable=io.StringIO) as err:
                self.assertEqual(cli.main(["review", "DEV-7", "--resume", selector]), 1)
                self.assertIn("explicit review context ID", err.getvalue())
                self.assertEqual([c["role"] for c in self.registry.list()], ["implementation"])
        self.config.assert_not_called()
        with patch("task_start.cli.review", wraps=review), patch("sys.stdout", new_callable=io.StringIO) as out:
            self.assertEqual(cli.main(["review", "DEV-7", "--json"]), 0)
            result = json.loads(out.getvalue())
        self.assertEqual((result["pass_kind"], result["context_id"]), ("fresh", "DEV-7-R1"))

    def late_pi_review(self, observations, *, persisted=True, resume=False):
        """Keep real adapter polling/provider verification; fake launch and Herdr transport."""
        self.local = replace(self.local, reviewer=AgentConfig("pi", "review-model", "high"))
        self.adapter = Pi(AgentOptions("pi", "review-model", "high"))
        self.enterContext(patch.object(self.adapter, "model_mode_capabilities",
                                      return_value=("review-model", "high", ("high",))))
        session_path = self.repo.parent / "pi-review.jsonl"
        session_path.write_text(json.dumps(dict(type="session", id="conversation-A", cwd=str(self.path))) + "\n" +
            (json.dumps(dict(type="message", message=dict(role="assistant", content=[]))) + "\n" if persisted else ""))
        session_a = dict(agent="pi", kind="path", value=str(session_path), source="hook")
        session_b = dict(session_a, value=str(session_path.with_name("B.jsonl")))
        references = {"A": session_a, "B": session_b, "missing": None,
                      "malformed": dict(agent="pi", kind="path", value=[])}
        pending = list(observations)
        writer = FakeReviewer(self)
        execution = None

        def launch(request):
            nonlocal execution
            execution = request
            writer.deliver(replace(request, runtime_observer=None))
            self.panes[-1].update(agent_session=None, foreground_cwd=str(self.path))
            row = self.registry.get("DEV-7-R1")
            self.assertIsNone(row["session_id"])
            self.assertEqual(row["resumability"], "unknown")
            return LaunchResult("pi", request.workspace.pane_id, "delivered without session metadata")

        def command(group, operation, *args):
            self.assertEqual(group, "agent")
            pane = self.panes[-1]
            if operation == "get":
                if pending:
                    reference, status, *changes = pending.pop(0)
                    pane.update(agent_session=references[reference], agent_status=status)
                    if changes:
                        pane.update(changes[0])
            elif operation == "prompt":
                writer.deliver(replace(execution, handoff=args[1], runtime_observer=None, delivery_observer=None), session_a)
            else:
                self.fail(operation)
            return dict(agent=copy.deepcopy(pane))

        with patch.object(self.adapter, "check_available"), patch.object(self.adapter, "launch", side_effect=launch), \
                patch.object(self.adapter, "command", side_effect=command):
            result = review("DEV-7")
            if resume:
                original = self.registry.get(result.context_id)
                self.assertEqual((original["state"], original["resumability"]), ("active", "yes"))
                self.panes[-1]["agent_status"] = "idle"  # Human releases the pause before requesting a follow-up.
                resumed = review("DEV-7", resume=result.context_id)
                self.assertEqual(resumed.state, "clean")
                self.assertEqual(resumed.context_id, result.context_id)
                self.assertEqual(len(self.panes), 2)
                for key in ("pane_id", "terminal_id", "session_id", "agent", "model", "mode"):
                    self.assertEqual(self.registry.get(result.context_id)[key], original[key])
        return result, self.registry.get(result.context_id), session_a

    def test_session_discovered_while_polling_is_persisted_verified_and_resumable(self):
        result, row, reference = self.late_pi_review([("missing", "working"), ("A", "done")], resume=True)
        self.assertEqual(result.state, "clean")
        self.assertEqual((row["session_id"], row["session_kind"], row["resumability"]),
                         (reference["value"], "path", "yes"))
        self.assertEqual(context_reference(row)["conversation_id"], "conversation-A")

    def test_pi_r7_idle_gap_then_working_and_late_result_keeps_waiting(self):
        # R7: Herdr's known-agent fallback was idle, its native session path was
        # reported before history flushed, and its first assistant response took
        # more than five seconds. Neither that idle nor a later idle gap is done.
        deliver, poll = FakeReviewer.deliver, Pi.review_status
        clock, calls, saved = [0.0], [], {}
        def launch_without_output(writer, execution, reference=None):
            result = deliver(writer, execution, reference)
            output = Path(writer.output)
            saved.update(output=output, result=output.read_bytes())
            output.unlink()
            history = self.repo.parent / "pi-review.jsonl"
            saved.update(history=history, entries=history.read_bytes())
            history.unlink()  # Pi has a native path before its first assistant.
            return result
        def observe(adapter, *args):
            calls.append(clock[0])
            if len(calls) == 6:
                saved["history"].write_bytes(saved["entries"])
            status = poll(adapter, *args)
            if len(calls) == 10:
                saved["output"].write_bytes(saved["result"])
            return status
        def tick(_):
            clock[0] += 2
        observations = [
            ("missing", "idle"), ("A", "idle"), ("A", "idle"), ("A", "idle"),
            ("missing", "unknown", dict(agent=None)), ("A", "working", dict(agent="pi")),
            ("A", "idle"), ("missing", "unknown"), ("A", "working"), ("A", "done"),
        ]
        with patch.object(FakeReviewer, "deliver", launch_without_output), \
                patch.object(Pi, "review_status", observe), \
                patch("task_start.review.time", SimpleNamespace(monotonic=lambda: clock[0], sleep=tick)):
            result, row, _ = self.late_pi_review(observations)
        self.assertEqual(len(calls), 10, result.render())
        self.assertGreater(calls[-1], 5)
        self.assertEqual((result.state, result.invalidated), ("clean", False))
        self.assertEqual((row["state"], row["resumability"]), ("active", "yes"))
        self.assertEqual(context_reference(row)["conversation_id"], "conversation-A")
        self.assertEqual(len(self.prompts), 1)

    def test_standalone_pi_handoff_retains_late_identity_without_requiring_it_before_prompt(self):
        from task_start.review_handoff_state import ReviewHandoffStore
        self.local = replace(self.local, reviewer=AgentConfig("pi", "review-model", "high"))
        self.adapter = Pi(AgentOptions("pi", "review-model", "high"))
        session = self.repo.parent / "late-standalone.jsonl"
        reference = dict(agent="pi", kind="path", value=str(session))
        writer = FakeReviewer(self)
        launch = self.adapter.launch
        request = None

        def execute(execution):
            nonlocal request
            request = execution
            return launch(execution)

        def start(workspace, args, **kwargs):
            pane = self.panes[-1]
            pane.update(agent="pi", agent_session=None, agent_status="idle", foreground_cwd=str(self.path))
            return copy.deepcopy(pane)

        def command(group, operation, *args):
            self.assertEqual(group, "agent")
            if operation == "prompt":
                self.assertFalse(session.exists())
                recorded = ReviewHandoffStore(self.path).read()
                self.assertIsNotNone(recorded["receipt"])
                self.assertIsNone(recorded["reviewer"]["session"])
                session.write_text(json.dumps(dict(type="session", id="late-conversation", cwd=str(self.path)))
                    + "\n" + json.dumps(dict(type="message", message=dict(role="assistant", content=[]))) + "\n")
                writer.deliver(replace(request, delivery_observer=None, runtime_observer=None), reference)
                self.panes[-1]["agent_status"] = "done"
            else:
                self.assertEqual(operation, "get")
            return dict(agent=copy.deepcopy(self.panes[-1]))

        with patch.object(self.adapter, "check_available"), patch.object(self.adapter, "validate_model_mode"), \
                patch.object(self.adapter, "check_target"), patch.object(self.adapter, "launch", side_effect=execute), \
                patch.object(self.adapter, "start_agent", side_effect=start), \
                patch.object(self.adapter, "command", side_effect=command), \
                patch.object(self.adapter, "delivery_session", side_effect=AssertionError("Premature identity requirement")):
            result = review("DEV-7")
        self.assertEqual(result.state, "clean", result.render())
        record = ReviewHandoffStore(self.path).read()
        self.assertEqual(record["reviewer"]["session"]["conversation_id"], "late-conversation")
        self.assertEqual(record["result"]["pass_id"], result.pass_id)
        self.assertEqual(len(self.prompts), 1)

    def test_late_session_divergence_is_surfaced_without_rebinding(self):
        result, row, reference = self.late_pi_review([("A", "working"), ("B", "done")])
        self.assertEqual(result.state, "failed")
        self.assertIn("identity", result.summary)
        self.assertEqual((row["session_id"], row["resumability"]), (reference["value"], "unknown"))
        self.assertEqual(json.loads(row["herdr_session"]), reference)

    def test_late_reference_without_provider_history_stays_unknown(self):
        result, row, reference = self.late_pi_review([("A", "done")], persisted=False)
        self.assertEqual(result.state, "clean")
        self.assertEqual((row["session_id"], row["resumability"]), (reference["value"], "unknown"))
        self.assertNotIn("conversation_id", context_reference(row))
        with self.assertRaisesRegex(TaskError, "non-resumable"):
            review("DEV-7", resume=result.context_id)

    def test_missing_late_observations_do_not_fabricate_session(self):
        result, row, _ = self.late_pi_review([("missing", "done")])
        self.assertEqual(result.state, "clean")
        self.assertIsNone(row["session_id"])
        self.assertIsNone(row["herdr_session"])
        self.assertEqual(row["resumability"], "unknown")

    def test_malformed_late_observation_fails_without_inventing_identity(self):
        result, row, _ = self.late_pi_review([("malformed", "done")])
        self.assertEqual(result.state, "failed")
        self.assertIsNone(row["session_id"])
        self.assertIsNone(row["herdr_session"])
        self.assertEqual(row["resumability"], "unknown")

    def test_pi_pause_and_tab_move_preserve_exact_resumable_reviewer(self):
        result, row, reference = self.late_pi_review([
            ("A", "working"), ("A", "blocked", dict(tab_id="human-moved-tab", label="Human label"))], resume=True)
        self.assertEqual(result.state, "blocked")
        self.assertEqual((row["state"], row["resumability"]), ("active", "yes"))
        self.assertEqual(context_reference(row)["conversation_id"], "conversation-A")
        self.assertEqual(row["session_id"], reference["value"])
        self.assertEqual(self.panes[-1]["tab_id"], "human-moved-tab")

    def test_pi_relocation_between_snapshot_and_status_remains_pending_then_resolves(self):
        result, row, reference = self.late_pi_review([
            ("A", "working"), ("A", "done", dict(pane_id="relocated", tab_id="another-tab")),
            ("A", "done")])
        self.assertEqual((result.state, result.invalidated), ("clean", False))
        self.assertEqual((row["pane_id"], row["tab_id"], row["terminal_id"]),
                         ("relocated", "another-tab", "term2"))
        self.assertEqual((row["state"], row["resumability"], row["session_id"]),
                         ("active", "yes", reference["value"]))

    def test_pi_unknown_status_does_not_destroy_verified_identity(self):
        result, row, _ = self.late_pi_review([("A", "unknown"), ("A", "done")], resume=True)
        self.assertEqual(result.state, "clean")
        self.assertEqual((row["state"], row["resumability"]), ("active", "yes"))

    def test_pi_missing_live_session_after_discovery_remains_conservative(self):
        result, row, reference = self.late_pi_review([("A", "working"), ("missing", "done")])
        self.assertEqual(result.state, "failed")
        self.assertEqual((row["state"], row["resumability"]), ("uncertain", "unknown"))
        self.assertEqual(row["session_id"], reference["value"])

    def verified_pi_reviewer(self):
        """Native Pi history and adapter; transport supports stop/restart in the same pane."""
        first, row, reference = self.late_pi_review([("A", "done")])
        self.pi_events = []
        self.pi_on_start = None
        writer = FakeReviewer(self)
        original_resume = self.adapter.resume_review
        original_verify = self.adapter.verify_review_session
        request = None

        def verify(workspace, identity):
            verified = original_verify(workspace, identity)
            self.pi_events.append(("verified", verified["conversation_id"]))
            return verified

        def resume(execution, identity, *, recreate):
            nonlocal request
            request = execution
            return original_resume(execution, identity, recreate=recreate)

        def command(group, operation, *args):
            self.pi_events.append((operation, *args))
            pane = self.panes[-1]
            mapped = self.registry.get(row["context_id"])
            if (group, operation) == ("pane", "list"):
                return dict(panes=copy.deepcopy(self.panes))
            self.assertEqual(group, "agent")
            if operation == "start":
                self.assertIsNone(pane["agent"])
                self.assertEqual(args[args.index("--pane") + 1], mapped["pane_id"])
                argv = ["pi", *args[args.index("--") + 1:]]
                self.assertEqual(argv[-2:], ["--session", reference["value"]])
                self.assertIn(row["model"], argv)
                self.assertIn(row["mode"], argv)
                if self.pi_on_start:
                    self.pi_on_start()
                pane.update(agent="pi", agent_status="idle", agent_session=reference, foreground_cwd=str(self.path))
                return dict(agent=copy.deepcopy(pane), argv=argv)
            if operation == "prompt":
                self.assertEqual(args[0], mapped["pane_id"])
                self.assertEqual(args[1], request.handoff)
                writer.deliver(replace(request, runtime_observer=None, delivery_observer=None), reference)
                pane["agent_status"] = "done"
            elif operation != "get":
                self.fail(operation)
            return dict(agent=copy.deepcopy(pane))

        self.enterContext(patch.object(self.adapter, "check_available"))
        self.enterContext(patch.object(self.adapter, "verify_review_session", side_effect=verify))
        self.enterContext(patch.object(self.adapter, "resume_review", side_effect=resume))
        self.enterContext(patch.object(self.adapter, "command", side_effect=command))
        return first, row, reference

    def stop_pi(self):
        self.panes[-1].update(agent=None, agent_status=None, agent_session=None)

    def assert_pi_identity_retained(self, original):
        row = self.registry.get(original["context_id"])
        self.assertEqual((row["state"], row["resumability"]), ("active", "yes"))
        for key in ("pane_id", "terminal_id", "session_id", "agent", "model", "mode"):
            self.assertEqual(row[key], original[key])
        self.assertEqual(context_reference(row)["conversation_id"], "conversation-A")
        self.assertEqual(len(self.panes), 2)
        self.assertEqual(len(self.registry.list()), 2)

    def test_stopped_pi_restarts_in_same_pane_after_verifying_retained_conversation(self):
        first, original, reference = self.verified_pi_reviewer()
        self.stop_pi()
        self.panes[-1]["tab_id"] = "human-moved-tab"
        self.local = replace(self.local, reviewer=AgentConfig("codex", "different", "low"))
        result = review("DEV-7", resume=first.context_id)
        self.assertEqual(result.state, "clean")
        self.assertEqual(result.context_id, first.context_id)
        self.assert_pi_identity_retained(original)
        events = [event[0] for event in self.pi_events]
        self.assertLess(events.index("verified"), events.index("start"))
        self.assertIn("verified", events[events.index("start") + 1:events.index("prompt")])
        self.assertEqual(self.panes[-1]["agent_session"]["value"], reference["value"])
        self.assertEqual(self.panes[-1]["tab_id"], "human-moved-tab")

    def test_closed_pi_pane_retains_verified_session_and_recreates_for_exact_resume(self):
        first, original, _ = self.verified_pi_reviewer()
        def close(*args):
            self.panes.pop()
            raise TaskError("Reviewer pane was closed")
        with patch.object(self.adapter, "review_status", side_effect=close):
            interrupted = review("DEV-7", resume=first.context_id)
        self.assertEqual(interrupted.state, "failed")
        row = self.registry.get(first.context_id)
        self.assertEqual((row["state"], row["resumability"]), ("active", "yes"))
        self.assertEqual(context_reference(row), context_reference(original))
        self.assertEqual(len(self.panes), 1)

        self.pi_events.clear()
        split = self.identities.split
        def verified_split(anchor, path):
            self.assertIn(("verified", "conversation-A"), self.pi_events)
            self.pi_events.append(("split", anchor["pane_id"], anchor["tab_id"]))
            return split(anchor, path)
        with patch.object(self.identities, "split", side_effect=verified_split):
            resumed = review("DEV-7", resume=first.context_id)
        self.assertEqual((resumed.state, resumed.context_id), ("clean", first.context_id), resumed.summary)
        row = self.registry.get(first.context_id)
        self.assertEqual((row["state"], row["resumability"]), ("active", "yes"))
        self.assertNotEqual(row["pane_id"], original["pane_id"])
        self.assertNotEqual(row["terminal_id"], original["terminal_id"])
        for key in ("session_id", "agent", "model", "mode", "workspace_id"):
            self.assertEqual(row[key], original[key])
        self.assertEqual(context_reference(row), context_reference(original))
        events = [e[0] for e in self.pi_events]
        self.assertLess(events.index("verified"), events.index("split"))
        self.assertIn("verified", events[events.index("start") + 1:events.index("prompt")])
        self.assertEqual(len(self.panes), 2)
        self.assertEqual(len(self.registry.list()), 2)

    def test_missing_reviewer_recreates_in_current_task_tab_when_original_tab_is_gone(self):
        first, original, _ = self.verified_pi_reviewer()
        self.panes.pop()
        self.panes[0]["tab_id"] = "current-task-tab"
        self.assertFalse(any(p["tab_id"] == original["tab_id"] for p in self.panes))
        result = review("DEV-7", resume=first.context_id)
        self.assertEqual((result.state, result.context_id), ("clean", first.context_id), result.summary)
        row = self.registry.get(first.context_id)
        self.assertEqual(row["tab_id"], "current-task-tab")
        self.assertEqual(row["workspace_id"], original["workspace_id"])
        self.assertEqual(self.panes[-1]["tab_id"], "current-task-tab")
        self.assertEqual(context_reference(row), context_reference(original))
        split_calls = [call.args for call in self.identity_command.call_args_list if call.args[1] == "split"]
        self.assertEqual(split_calls[-1][2], self.panes[0]["pane_id"])
        self.assertNotEqual(row["pane_id"], original["pane_id"])
        self.assertEqual(len(self.registry.list()), 2)

    def test_missing_reviewer_pane_does_not_bypass_provider_verification(self):
        first, original, reference = self.verified_pi_reviewer()
        self.panes.pop()
        path = Path(reference["value"])
        history = path.read_text()
        for change in ("deleted", "replaced", "empty", "unavailable"):
            with self.subTest(change=change), ExitStack() as unavailable:
                path.write_text(history)
                if change == "deleted":
                    path.unlink()
                elif change == "replaced":
                    path.write_text(history.replace("conversation-A", "replacement"))
                elif change == "empty":
                    path.write_text("")
                else:
                    unavailable.enter_context(patch.object(self.adapter, "verify_review_session", side_effect=TaskError("Unavailable")))
                self.identity_command.reset_mock()
                with self.assertRaises(TaskError):
                    review("DEV-7", resume=first.context_id)
                self.assertFalse(any(c.args[1] == "split" for c in self.identity_command.call_args_list))
                self.assertEqual(self.pi_events, [])
                self.assertEqual(len(self.panes), 1)
                self.assertEqual(len(self.registry.list()), 2)
                self.assertEqual(context_reference(self.registry.get(first.context_id)), context_reference(original))

    def test_closed_pane_finalization_still_rejects_lost_provider_identity(self):
        first, original, reference = self.verified_pi_reviewer()
        path = Path(reference["value"])
        history = path.read_text()
        pane = copy.deepcopy(self.panes[-1])
        for change in ("deleted", "replaced"):
            with self.subTest(change=change):
                path.write_text(history)
                self.registry.update(first.context_id, state="active", resumability="yes")
                self.panes = [self.panes[0], copy.deepcopy(pane)]
                def close(*args):
                    self.panes.pop()
                    if change == "deleted":
                        path.unlink()
                    else:
                        path.write_text(history.replace("conversation-A", "replacement"))
                    return "blocked"
                with patch.object(self.adapter, "review_status", side_effect=close):
                    result = review("DEV-7", resume=first.context_id)
                self.assertEqual(result.state, "failed")
                row = self.registry.get(first.context_id)
                self.assertEqual((row["state"], row["resumability"]), ("uncertain", "no"))
                self.assertEqual(context_reference(row), context_reference(original))
                with self.assertRaisesRegex(TaskError, "non-resumable"):
                    review("DEV-7", resume=first.context_id)

    def test_pi_stopped_during_pass_retains_resumability_and_later_resume_succeeds(self):
        first, original, _ = self.verified_pi_reviewer()
        self.raw = "missing"
        status = self.adapter.review_status
        def stop(*args):
            self.stop_pi()
            return status(*args)
        with patch.object(self.adapter, "review_status", side_effect=stop):
            interrupted = review("DEV-7", resume=first.context_id, timeout=0.5)
        self.assertEqual(interrupted.state, "failed")
        self.assertIn("Timed out", interrupted.summary)
        self.assert_pi_identity_retained(original)
        self.assertIsNone(self.panes[-1]["agent"])
        self.raw = None
        self.assertEqual(review("DEV-7", resume=first.context_id).state, "clean")
        self.assert_pi_identity_retained(original)

    def test_transient_final_pi_verification_preserves_yes_and_later_resume(self):
        first, original, reference = self.verified_pi_reviewer()
        status = self.adapter.review_status
        for stopped in (False, True):
            with self.subTest(stopped=stopped):
                # Restrict the file failure to Pi history, so result/fingerprint reads still run.
                history_open = Path.open
                def locked_history(path, *args, **kwargs):
                    if path == Path(reference["value"]):
                        raise PermissionError("temporarily locked")
                    return history_open(path, *args, **kwargs)
                def unavailable_at_completion(*args):
                    result = status(*args)
                    if stopped:
                        self.stop_pi()
                    unavailable.enter_context(patch.object(Path, "open", locked_history))
                    return result
                with ExitStack() as unavailable, patch.object(self.adapter, "review_status", side_effect=unavailable_at_completion):
                    result = review("DEV-7", resume=first.context_id)
                self.assertEqual(result.state, "clean")
                self.assertIn("last verified resumability retained", result.summary)
                self.assert_pi_identity_retained(original)
                self.assertEqual(review("DEV-7", resume=first.context_id).state, "clean")
                self.assert_pi_identity_retained(original)

    def test_temporarily_unavailable_pre_resume_sends_no_prompt_and_can_retry(self):
        first, original, _ = self.verified_pi_reviewer()
        self.stop_pi()
        with patch.object(self.adapter, "verify_review_session", side_effect=TaskError("Provider offline")), \
                self.assertRaisesRegex(TaskError, "Provider offline"):
            review("DEV-7", resume=first.context_id)
        self.assertEqual(self.pi_events, [])
        self.assert_pi_identity_retained(original)
        self.assertEqual(review("DEV-7", resume=first.context_id).state, "clean")

    def test_stopped_pi_rejects_replaced_deleted_and_empty_sessions_before_launch(self):
        first, original, reference = self.verified_pi_reviewer()
        path = Path(reference["value"])
        history = path.read_text()
        for change in ("replacement", "deleted", "empty", "header-only", "directory"):
            with self.subTest(change=change):
                self.stop_pi()
                path.write_text(history)
                if change == "replacement":
                    path.write_text(history.replace("conversation-A", "replacement"))
                elif change == "deleted":
                    path.unlink()
                elif change == "empty":
                    path.write_text("")
                elif change == "directory":
                    path.unlink()
                    path.mkdir()
                else:
                    path.write_text(history.splitlines()[0] + "\n")
                with self.assertRaises(SessionInvalid):
                    review("DEV-7", resume=first.context_id)
                self.assertFalse(any(event[0] in {"start", "prompt"} for event in self.pi_events))
                self.assertEqual(context_reference(self.registry.get(first.context_id))["conversation_id"], "conversation-A")
                self.assertEqual(len(self.registry.list()), 2)

    def test_stopped_pi_startup_race_never_prompts_replacement_conversation(self):
        first, original, reference = self.verified_pi_reviewer()
        self.stop_pi()
        path = Path(reference["value"])
        self.pi_on_start = lambda: path.write_text(json.dumps(dict(type="session", id="replacement", cwd=str(self.path))) + "\n")
        result = review("DEV-7", resume=first.context_id)
        self.assertEqual(result.state, "failed")
        self.assertTrue(any(event[0] == "start" for event in self.pi_events))
        self.assertFalse(any(event[0] == "prompt" for event in self.pi_events))
        row = self.registry.get(first.context_id)
        self.assertEqual((row["state"], row["resumability"]), ("uncertain", "no"))
        self.assertEqual(context_reference(row)["conversation_id"], "conversation-A")
        self.assertEqual(row["pane_id"], original["pane_id"])
        with self.assertRaisesRegex(TaskError, "non-resumable"):
            review("DEV-7", resume=first.context_id)

    def test_final_provider_identity_loss_is_not_treated_as_temporary(self):
        first, original, reference = self.verified_pi_reviewer()
        path = Path(reference["value"])
        history = path.read_text()
        status = self.adapter.review_status
        for change in ("replacement", "deleted", "empty"):
            with self.subTest(change=change):
                path.write_text(history)
                self.registry.update(first.context_id, state="active", resumability="yes")
                def invalidate(*args):
                    result = status(*args)
                    if change == "deleted":
                        path.unlink()
                    else:
                        path.write_text(history.replace("conversation-A", "replacement") if change == "replacement" else "")
                    return result
                with patch.object(self.adapter, "review_status", side_effect=invalidate):
                    result = review("DEV-7", resume=first.context_id)
                self.assertEqual(result.state, "failed")
                row = self.registry.get(first.context_id)
                self.assertEqual((row["state"], row["resumability"]), ("uncertain", "no"))
                self.assertEqual(context_reference(row)["conversation_id"], "conversation-A")

    def test_stopped_pi_still_rejects_contradictory_pane_and_session_evidence(self):
        first, original, _ = self.verified_pi_reviewer()
        self.stop_pi()
        shell = copy.deepcopy(self.panes[-1])
        for change in (dict(terminal_id="replacement"), dict(agent="codex"),
                       dict(agent_session=dict(agent="pi", kind="path", value="/different/session")),
                       dict(agent_session={}), dict(agent_session="")):
            with self.subTest(change=change):
                self.panes[-1] = dict(shell, **change)
                with self.assertRaises(TaskError):
                    review("DEV-7", resume=first.context_id)
                self.assertEqual(self.pi_events, [])

    def test_stopped_pi_cannot_infer_retained_identity_from_path_alone(self):
        first, original, reference = self.verified_pi_reviewer()
        self.stop_pi()
        self.registry.update(first.context_id, herdr_session=json.dumps(reference))
        with self.assertRaisesRegex(TaskError, "non-resumable"):
            review("DEV-7", resume=first.context_id)
        self.assertEqual(self.pi_events, [])

    def test_temporary_verification_failure_preserves_capability_for_every_model_verdict(self):
        for state in ("clean", "findings", "blocked", "failed"):
            self.verdict_overrides = dict(state=state, findings=[dict(severity="medium", explanation="Missing check",
                evidence="tracked.txt:1", requirement="validation")] if state == "findings" else [])
            with self.subTest(state=state), patch.object(self.adapter, "verify_review_session", side_effect=TaskError("Unavailable")):
                result = review("DEV-7")
                self.assertEqual(result.state, state)
                row = self.registry.get(result.context_id)
                self.assertEqual((row["state"], row["resumability"]), ("active", "yes"))

    def test_transient_final_runtime_observation_failure_does_not_destroy_verified_context(self):
        first, original, _ = self.verified_pi_reviewer()
        status = self.adapter.review_status
        def unavailable_at_completion(*args):
            result = status(*args)
            unavailable.enter_context(patch.object(self.identities, "snapshot", side_effect=TaskError("Herdr unavailable")))
            return result
        with ExitStack() as unavailable, patch.object(self.adapter, "review_status", side_effect=unavailable_at_completion):
            result = review("DEV-7", resume=first.context_id)
        self.assertEqual(result.state, "failed")
        self.assertIn("Herdr unavailable", result.summary)
        self.assert_pi_identity_retained(original)
        self.assertEqual(review("DEV-7", resume=first.context_id).state, "clean")


class FakeReviewer:
    def __init__(self, test):
        self.test = test

    def check_available(self):
        pass

    def verify_review_session(self, workspace, reference):
        if not reference or not reference["value"]:
            raise TaskError("Missing session")
        return dict(reference)

    verify_session = verify_review_session

    def observe_delivery(self, execution, reference, receipt, completion):
        return True

    def deliver(self, execution, reference=None):
        test = self.test
        test.prompts.append(execution.handoff)
        pane = next(p for p in test.panes if p["pane_id"] == execution.workspace.pane_id)
        reference = reference or dict(agent=execution.options.kind, kind="id", value="session-" + pane["pane_id"])
        pane.update(agent=execution.options.kind, agent_session=reference)
        delivery_fixture.claim(test, execution, reference)
        self.output = re.search(r"output-file write is (.*?)\. This temporary", execution.handoff).group(1)
        pass_id = re.search(r"Pass ID: ([^\n]+)", execution.handoff).group(1)
        if test.raw != "missing":
            result = dict(verdict(pass_id), **test.verdict_overrides)
            metadata = json.loads(execution.handoff.split("RESOLVED REVIEW METADATA\n", 1)[1]
                                  .split("\n\nLATEST LINEAR REQUIREMENTS", 1)[0])
            frozen = metadata.get("frozen_publication")
            if frozen is not None and result["state"] == "clean" and getattr(test, "approve_frozen_publication", True):
                result.setdefault("publication_approval", frozen["fingerprint"])
            Path(self.output).write_text(test.raw if test.raw is not None else json.dumps(result))
        if test.mutation:
            test.mutation()
        delivery_fixture.complete(execution)
        if execution.runtime_observer:
            execution.runtime_observer(dict(session_id=reference["value"], session_kind=reference["kind"],
                                            terminal_id=pane["terminal_id"], resumability="yes"))
        return LaunchResult(execution.options.kind, pane["pane_id"], "delivered", reference["value"],
                            session_kind=reference["kind"], resumability="yes")

    launch = deliver

    def resume_review(self, execution, reference, *, recreate):
        self.test.recreated.append(recreate)
        return self.deliver(execution, reference)

    def review_status(self, execution, terminal_id, reference):
        return self.test.status

    status = review_status


class ResultTests(unittest.TestCase):
    def test_strict_contract_rejects_ambiguity_and_inconsistent_verdicts(self):
        valid = verdict()
        self.assertEqual(parse_verdict(json.dumps(valid), "pass"), valid)
        invalid = ["", "null", "[]", "```json\n" + json.dumps(valid) + "\n```", "{}",
                   json.dumps(valid).replace('"state": "clean"', '"state": "clean", "state": "clean"')]
        for change in (dict(pass_id="other"), dict(state="findings"), dict(state="unknown"), dict(findings=[{}]),
                       dict(checks=[dict(name="test", result="failed", details="failure")]), dict(summary="")):
            invalid.append(json.dumps(dict(valid, **change)))
        for raw in invalid:
            with self.subTest(raw=raw), self.assertRaises(TaskError):
                parse_verdict(raw, "pass")

    def test_cli_json_and_exit_states(self):
        from task_start.review_result import ReviewResult
        for state, code in (("clean", 0), ("findings", 2), ("blocked", 3), ("failed", 1)):
            result = ReviewResult(state, "Summary", "DEV-7-R1", "fresh", {}, {}, "pass")
            with patch("task_start.cli.review", return_value=result), patch("sys.stdout", new_callable=io.StringIO) as out:
                self.assertEqual(cli.main(["review", "dev-7", "--json"]), code)
                self.assertEqual(json.loads(out.getvalue())["state"], state)

    def test_review_configuration_is_independent(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "config.toml"
            config.write_text('projects_root="/projects"\n[linear]\napi_key="placeholder"\n'
                              '[agent]\nkind="pi"\nmodel="implementer"\nmode="low"\n'
                              '[reviewer]\nkind="codex"\nmodel="reviewer"\nmode="high"\n')
            local = load_local(config)
            self.assertEqual(local.agent.kind, "pi")
            self.assertEqual(local.reviewer, AgentConfig("codex", "reviewer", "high"))

    def test_findings_and_blocked_with_evidence_and_test_linkage(self):
        value = verdict()
        value.update(state="findings", findings=[dict(severity="high", explanation="Requirement missed",
                     evidence="src/task.py:12", requirement="Acceptance criterion 2 / test_missing")])
        self.assertEqual(parse_verdict(json.dumps(value), "pass"), value)
        value["state"] = "blocked"
        self.assertEqual(parse_verdict(json.dumps(value), "pass")["state"], "blocked")


class ReviewAdapterTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.object(Pi, "clear_shell_input"))
        self.enterContext(patch("task_start.agent.HerdrAgentAdapter.filter_shell_credentials"))
        self.directory = self.enterContext(tempfile.TemporaryDirectory())
        self.workspace = Workspace("dev-7-task", Path(self.directory), "w1", "t1", "p1", "existing")
        self.reference = dict(agent="codex", kind="id", value="01a0d314-bd68-7203-8b68-f2520f892afa")
        self.capabilities = self.enterContext(patch.object(Pi, "model_mode_capabilities",
                                              return_value=("saved-model", "high", ("high",))))

    def execution(self, kind):
        return AgentExecution(ISSUE, Path(self.directory), self.workspace,
                              AgentOptions(kind, "saved-model", "high"), "Review follow-up", purpose="review")

    def agent(self, kind, reference=None):
        return dict(agent=kind, agent_status="idle", workspace_id="w1", tab_id="t1", pane_id="p1",
                    cwd=self.directory, foreground_cwd=self.directory, terminal_id="terminal",
                    agent_session=reference)

    def test_codex_resume_queues_only_exact_thread_and_paginates_receipt(self):
        execution = self.execution("codex")
        adapter = Codex(execution.options)
        rpc = MagicMock()
        queued = {}
        def request(method, params, **kwargs):
            self.assertEqual(params["threadId"], self.reference["value"])
            if method == "thread/read":
                return dict(thread=dict(id=self.reference["value"], cwd=self.directory))
            if method == "thread/queue/add":
                queued.update(params)
                return dict(queuedSubmission=dict(id="queue", **params))
            if method == "thread/items/list":
                if not params.get("cursor"):
                    return dict(data=[], nextCursor="page2")
                return dict(data=[dict(turnId="follow-up", item=dict(type="userMessage",
                    clientId=queued["clientUserMessageId"], content=queued["input"]))], nextCursor=None)
            self.fail(method)
        rpc.request.side_effect = request
        with patch("task_start.agent.CodexRPC") as factory, patch.object(adapter, "command", return_value=dict(agent=self.agent("codex"))):
            factory.return_value.__enter__.return_value = rpc
            result = adapter.resume_review(execution, self.reference, recreate=False)
        self.assertEqual(result.session_id, self.reference["value"])
        self.assertEqual(result.turn_id, "follow-up")
        self.assertEqual(queued["input"][0]["text"], execution.handoff)
        args = adapter.resume_args(execution, self.reference)
        self.assertEqual(args[-1], self.reference["value"])
        self.assertIn("saved-model", args)
        self.assertNotIn("--last", args)

    def test_codex_rejects_wrong_history_and_live_runtime(self):
        execution = self.execution("codex")
        adapter = Codex(execution.options)
        with patch("task_start.agent.CodexRPC") as factory:
            factory.return_value.__enter__.return_value.request.return_value = dict(thread=dict(id="different", cwd=self.directory))
            with self.assertRaisesRegex(SessionInvalid, "missing or mismatched"):
                adapter.verify_review_session(self.workspace, self.reference)
        with patch.object(adapter, "command", return_value=dict(agent=self.agent("codex", dict(self.reference, value="other")))):
            with self.assertRaisesRegex(TaskError, "identity"):
                adapter.review_status(execution, "terminal", self.reference)

    def test_codex_unavailable_or_malformed_verification_is_not_proof_of_session_loss(self):
        adapter = Codex(self.execution("codex").options)
        cases = [TaskError("Provider offline"), PermissionError("Unavailable"), {}, dict(thread=None),
                 dict(thread=dict(id=self.reference["value"]))]
        for response in cases:
            with self.subTest(response=response), patch("task_start.agent.CodexRPC") as factory:
                request = factory.return_value.__enter__.return_value.request
                if isinstance(response, Exception):
                    request.side_effect = response
                else:
                    request.return_value = response
                with self.assertRaises(TaskError) as error:
                    adapter.verify_review_session(self.workspace, self.reference)
                self.assertNotIsInstance(error.exception, SessionInvalid)

    def test_pi_requires_persisted_exact_path_and_resumes_with_saved_options(self):
        execution = self.execution("pi")
        adapter = Pi(execution.options)
        path = Path(self.directory) / "session.jsonl"
        path.write_text(json.dumps(dict(type="session", id="provider-id", cwd=self.directory)) + "\n" +
                        json.dumps(dict(type="message", message=dict(role="assistant", content=[]))) + "\n")
        reference = dict(agent="pi", kind="path", value=str(path))
        adapter.verify_review_session(self.workspace, reference)
        observed = self.agent("pi", reference)
        with patch.object(adapter, "command", return_value=dict(agent=observed)) as command:
            result = adapter.resume_review(execution, reference, recreate=False)
        self.assertEqual(result.session_id, str(path))
        self.capabilities.assert_called_once_with(self.workspace)
        self.assertIn(("agent", "prompt", "p1", execution.handoff), [c.args for c in command.call_args_list])
        self.assertEqual(adapter.resume_args(execution, reference),
                         ["--model", "saved-model", "--thinking", "high", "--extension",
                          str(Path(__file__).resolve().parents[1] / "task_start/pi_session.mjs"), "--session", str(path)])
        path.unlink()
        with self.assertRaisesRegex(TaskError, "persisted"):
            adapter.verify_review_session(self.workspace, reference)
        with patch.object(Path, "stat", side_effect=PermissionError("History filesystem unavailable")):
            with self.assertRaises(TaskError) as error:
                adapter.verify_review_session(self.workspace, reference)
            self.assertNotIsInstance(error.exception, SessionInvalid)

    def test_pi_resume_validates_saved_effective_mode_before_launch_or_prompt(self):
        execution = self.execution("pi")
        adapter = Pi(execution.options)
        path = Path(self.directory) / "session.jsonl"
        path.write_text(json.dumps(dict(type="session", id="original", cwd=self.directory)) + "\n" +
                        json.dumps(dict(type="message", message=dict(role="assistant", content=[]))) + "\n")
        reference = dict(agent="pi", kind="path", value=str(path))
        for recreate in (False, True):
            for supported in (True, False):
                with self.subTest(recreate=recreate, supported=supported):
                    self.capabilities.reset_mock()
                    self.capabilities.return_value = (("saved-model", "high", ("high",)) if supported else
                                                      ("saved-model", "off", ("off",)))
                    observed = self.agent("pi", reference)
                    def prompted(*args):
                        self.capabilities.assert_called_once_with(self.workspace)
                        return dict(agent=observed)
                    with patch.object(adapter, "check_target"), patch.object(adapter, "start_agent", return_value=observed) as start, \
                            patch.object(adapter, "command", side_effect=prompted) as command:
                        if supported:
                            adapter.resume_review(execution, reference, recreate=recreate)
                            self.assertIn(("agent", "prompt", "p1", execution.handoff), [c.args for c in command.call_args_list])
                            self.assertEqual(start.call_count, int(recreate))
                        else:
                            with self.assertRaisesRegex(TaskError, "does not support requested mode.*Pi would use"):
                                adapter.resume_review(execution, reference, recreate=recreate)
                            command.assert_not_called()
                            start.assert_not_called()
                    self.capabilities.assert_called_once_with(self.workspace)
                    self.assertEqual(adapter.options, execution.options)

    def test_review_status_absence_and_unavailable_observation_are_pending_but_replacement_fails(self):
        execution = self.execution("pi")
        adapter = Pi(execution.options)
        for changes in (dict(agent=None, agent_status=None), dict(agent_status="unknown"), dict(launch_pending=True)):
            with self.subTest(changes=changes), patch.object(adapter, "command", return_value=dict(agent=dict(self.agent("pi"), **changes))):
                self.assertEqual(adapter.review_status(execution, "terminal", None), "unknown")
        with patch.object(adapter, "command", side_effect=TaskError("Temporarily unavailable")):
            self.assertEqual(adapter.review_status(execution, "terminal", None), "unknown")
        for changes in (dict(terminal_id="replacement"), dict(agent="codex"), dict(workspace_id="elsewhere")):
            with self.subTest(changes=changes), patch.object(adapter, "command", return_value=dict(agent=dict(self.agent("pi"), **changes))):
                with self.assertRaises(TaskError):
                    adapter.review_status(execution, "terminal", None)

    def test_fresh_target_allows_other_reviewers_but_never_an_occupied_target(self):
        adapter = Pi(AgentOptions("pi", "model", "high"))
        panes = [dict(self.agent("pi"), pane_id="other"), dict(self.agent("pi"), agent=None)]
        with patch.object(adapter, "command", return_value=dict(panes=panes)):
            adapter.check_target(self.workspace, review=True)
            with self.assertRaises(TaskError):
                adapter.check_target(self.workspace)
            panes[-1]["agent"] = "pi"
            with self.assertRaises(ValueError):
                adapter.check_target(self.workspace, review=True)

    def test_pi_resume_rejects_session_recreated_at_same_path_before_prompt(self):
        execution = self.execution("pi")
        adapter = Pi(execution.options)
        path = Path(self.directory) / "session.jsonl"
        header = dict(type="session", id="original-conversation", cwd=self.directory)
        message = dict(type="message", message=dict(role="assistant", content=[]))
        reference = dict(agent="pi", kind="path", value=str(path))
        observed = self.agent("pi", reference)
        # Also test a replacement with nonempty history: history alone is not
        # enough when the immutable conversation ID differs from the verified ID.
        for replacement_id, history in (("new-conversation", []), ("new-conversation", [message]),
                                        ("original-conversation", [])):
            with self.subTest(replacement_id=replacement_id, history=history):
                path.write_text(json.dumps(header) + "\n" + json.dumps(message) + "\n")
                registry = ContextRegistry(Path(self.directory) / "contexts.sqlite3")
                context_id = registry.allocate("DEV-7", "review", agent="pi", terminal_id="terminal")
                observer = context_observer(registry, context_id)

                def start(workspace, args, *, review):
                    self.assertTrue(review)
                    self.assertEqual(context_reference(registry.get(context_id))["conversation_id"], header["id"])
                    path.unlink()  # Existing, verified history disappears before Pi starts.
                    path.write_text("\n".join(json.dumps(v) for v in
                        [dict(header, id=replacement_id), *history]) + "\n")
                    return dict(observed)  # Provider reports the SAME session path.

                with patch.object(adapter, "check_target"), patch.object(adapter, "start_agent", side_effect=start), \
                        patch.object(adapter, "command", return_value=dict(agent=observed)) as command:
                    with self.assertRaisesRegex(TaskError, "matching conversation identity"):
                        adapter.resume_review(replace(execution, runtime_observer=observer), reference, recreate=True)
                self.assertNotIn("prompt", [call.args[1] for call in command.call_args_list])
                saved = context_reference(registry.get(context_id))
                self.assertEqual((saved["value"], saved["conversation_id"]), (str(path), header["id"]))

    def test_pi_immutable_identity_survives_observer_restart_and_locator_only_reports(self):
        path = Path(self.directory) / "session.jsonl"
        path.write_text(json.dumps(dict(type="session", id="original", cwd=self.directory)) + "\n" +
                        json.dumps(dict(type="message", message=dict(role="assistant", content=[]))) + "\n")
        adapter = Pi(self.execution("pi").options)
        reference = dict(agent="pi", kind="path", value=str(path), source="hook")
        verified = adapter.verify_review_session(self.workspace, reference)
        registry = ContextRegistry(Path(self.directory) / "contexts.sqlite3")
        context_id = registry.allocate("DEV-7", "review", agent="pi", terminal_id="terminal")
        context_observer(registry, context_id)(dict(herdr_session=json.dumps(verified)))
        self.assertEqual(registry.get(context_id)["resumability"], "unknown")
        observer = context_observer(registry, context_id)
        observer(dict(herdr_session=json.dumps(dict(reference, source="later-hook"))))
        observer(dict(herdr_session=json.dumps(reference["value"])))
        saved = context_reference(registry.get(context_id))
        self.assertEqual(saved["conversation_id"], "original")
        with self.assertRaisesRegex(TaskError, "session changed"):
            observer(dict(herdr_session=json.dumps(dict(verified, conversation_id="replacement"))))
        self.assertEqual(context_reference(registry.get(context_id)), saved)
        path.write_text(path.read_text().replace('"original"', '"replacement"'))
        with patch.object(adapter, "command") as command, self.assertRaisesRegex(TaskError, "conversation identity"):
            adapter.resume_review(self.execution("pi"), saved, recreate=False)
        command.assert_not_called()

    def test_pi_recreated_pane_continues_verified_original_conversation(self):
        path = Path(self.directory) / "session.jsonl"
        path.write_text(json.dumps(dict(type="session", id="original", cwd=self.directory)) + "\n" +
                        json.dumps(dict(type="message", message=dict(role="assistant", content=[]))) + "\n")
        execution = self.execution("pi")
        adapter = Pi(execution.options)
        reference = dict(agent="pi", kind="path", value=str(path))
        observed = self.agent("pi", reference)
        with patch.object(adapter, "check_target"), patch.object(adapter, "start_agent", return_value=observed), \
                patch.object(adapter, "command", return_value=dict(agent=observed)) as command:
            result = adapter.resume_review(execution, reference, recreate=True)
        self.assertEqual((result.session_id, result.resumability), (str(path), "yes"))
        self.assertEqual([c.args for c in command.call_args_list if c.args[1] == "prompt"],
                         [("agent", "prompt", "p1", execution.handoff)])
