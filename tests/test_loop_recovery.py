"""Disposable subprocess regressions; no real agent, Herdr session, or Linear."""

import copy
from dataclasses import replace
import io
import json
import multiprocessing
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from task_start import TaskError, cli
from task_start.agent import Codex, AgentOptions
from task_start.loop import loop, LoopRuntime, environment
from task_start.loop_state import LoopStore
from task_start.metrics import MetricsStore, RunMetrics
from task_start.publication_state import PublicationStore
from task_start.pass_delivery import PassDelivery
from task_start.review import review
import test_loop as loops
import test_loop_bootstrap as bootstrap
import test_review as reviews


class RecoveryTests(unittest.TestCase):
    """Real loop, parsers, worktree, checkpoint, lock and context transactions."""
    command = loops.LoopIntegrationTests.command
    pane_command = loops.LoopIntegrationTests.pane_command
    worktrees = loops.LoopIntegrationTests.worktrees
    select_adapter = loops.LoopIntegrationTests.select_adapter
    setUp = loops.LoopIntegrationTests.setUp

    def interrupt(self, phase):
        def stop_implementation(execution, items):
            if phase == ("fixes" if items else "implementation"):
                raise KeyboardInterrupt()
        def stop_review(execution):
            if phase == ("review" if len(self.prompts) == 1 else "rereview"):
                raise KeyboardInterrupt()
        self.on_implementation, self.on_review = stop_implementation, stop_review
        if phase in {"fixes", "rereview"}:
            self.review_results = [loops.findings(loops.finding())]
        result = loop("DEV-7", timeout=10)
        self.assertEqual(result.state, "interrupted", result.render())
        self.on_implementation = self.on_review = None
        return self.store.read()[0]

    def test_project_changes_during_interruption_preserve_saved_roles_timeout_and_reviewer(self):
        from test_layered_config import install_lifecycle_layers
        local = install_lifecycle_layers(self)
        before = self.interrupt('review')
        self.assertEqual(before['timeout'], 10)
        self.assertEqual(before['reviewer_options']['model'], 'shared-r')
        from task_start.implementation_pass import binding
        expected_reviewer = binding(self.registry.get(before['reviewer']['context_id']))
        local.write_text('[codex]\nprofile="forbidden for a new run"')
        result = loop('DEV-7')
        self.assertEqual(result.state, 'clean', result.render())
        after = self.store.read()[0]
        for key in ('implementation', 'reviewer_options', 'timeout', 'run_id'):
            self.assertEqual(after[key], before[key], key)
        self.assertEqual(after['reviewer'], expected_reviewer)
        self.assertEqual((len(self.impl_prompts), len(self.prompts)), (1, 1))

    def test_each_phase_recovers_once_and_retains_checks_and_reviewer(self):
        for phase in ("implementation", "review", "fixes", "rereview"):
            with self.subTest(phase=phase):
                case = RecoveryTests()
                case.setUp()
                try:
                    case.impl_overrides = dict(checks=[dict(name="live", result="not_run", details="Offline fixture only")])
                    clock = SimpleNamespace(now=2000000000.0)
                    complete = loops.delivery_fixture.complete
                    def late_complete(execution):
                        clock.now += 1278  # 21m08s after this fixture's 10s threshold.
                        return complete(execution)
                    with patch("task_start.pass_delivery.time", SimpleNamespace(time=lambda: clock.now)), \
                            patch.object(loops.delivery_fixture, "complete", side_effect=late_complete):
                        before = case.interrupt(phase)
                    directory = Path(before["delivery"]["directory"])
                    proof = json.loads((directory / "completion.json").read_text())
                    case.assertEqual(proof["completed_at"] - before["delivery"]["deadline"], 1268)
                    with patch("task_start.pass_delivery.time", SimpleNamespace(time=lambda: clock.now)), \
                            patch("sys.stderr", new_callable=io.StringIO):
                        result = loop("DEV-7")
                    case.assertEqual(result.state, "clean", result.render())
                    after = case.store.read()[0]
                    case.assertEqual(after["run_id"], before["run_id"])
                    case.assertEqual(len(case.impl_prompts), 2 if phase in {"fixes", "rereview"} else 1)
                    case.assertEqual(len(case.prompts), 2 if phase in {"fixes", "rereview"} else 1)
                    case.assertEqual(after["reviewer"]["context_id"], "DEV-7-R1")
                    case.assertIn(dict(name="live", result="not_run", details="Offline fixture only"), result.data["validation"])
                    case.assertFalse(directory.exists())
                    case.assertEqual(loop("DEV-7").data, result.data)
                finally:
                    case.doCleanups()

    def check_recovered_metrics(self, phase, outcome, *, killed=False):
        metrics = MetricsStore(self.registry.path)
        finish = RunMetrics.finish
        role = {"implementation": "implementation", "fixes": "fix", "review": "review", "rereview": "re-review"}[phase]

        def observe_finish(metric, error=None):
            # Model a killed controller's missing final observation for this pass.
            if not (killed and metric.record and metric.record["role"] == role):
                finish(metric, error)

        if phase in {"implementation", "fixes"}:
            self.impl_overrides = dict(state=outcome)
        elif outcome == "findings":
            self.review_results = [loops.findings(loops.finding())]
        else:
            self.review_results = [dict(state=outcome)]
        with patch.object(RunMetrics, "finish", observe_finish):
            before = self.interrupt(phase)
        pass_id = before["active_pass"]["pass_id"]
        original = next(r for r in metrics.read() if r["run_id"] == pass_id)
        self.assertEqual(original["status"], "running" if killed else "interrupted")
        self.assertEqual(original["finished_at"] is None, killed)
        prompts = (len(self.impl_prompts), len(self.prompts))
        self.store.pause()  # Collect only the retained pass; do not deliver a next pass.
        observed_at = "2050-01-02T03:04:05+00:00"
        with patch("task_start.metrics.timestamp", return_value=observed_at):
            result = loop("DEV-7")
        self.assertEqual(result.state, {"completed": "paused", "findings": "paused", "clean": "clean",
                                       "blocked": "escalated", "failed": "escalated"}[outcome], result.render())
        self.assertEqual((len(self.impl_prompts), len(self.prompts)), prompts)
        rows = [r for r in metrics.read() if r["run_id"] == pass_id]
        self.assertEqual(len(rows), 1)
        recovered, = rows
        for field in ("run_id", "task_id", "context_id", "loop_id", "role", "model", "agent", "mode",
                      "previous_run_id", "review_round", "implementation_context_id",
                      "started_at", "finished_at", "wall_clock_seconds"):
            self.assertEqual(recovered[field], original[field], field)
        self.assertEqual(recovered["result_observed_at"], observed_at)
        self.assertEqual(recovered["status"], "completed" if outcome in {"clean", "findings"} else outcome)
        self.assertEqual(recovered["loop_status"], result.state)
        if phase in {"review", "rereview"}:
            self.assertEqual(recovered["findings_count"], 1 if outcome == "findings" else 0)
            self.assertEqual(recovered.get("review_outcome"), outcome if outcome in {"clean", "findings"} else None)
        else:
            self.assertNotIn("findings_count", recovered)
            self.assertNotIn("review_outcome", recovered)
        after = self.store.read()[0]
        self.assertEqual(sum(r["pass_id"] == pass_id for r in after["records"]), 1)
        self.assertIsNone(after["active_pass"])
        self.assertIsNone(after["delivery"])
        # A later status read cannot replace the late observation timestamp.
        self.assertEqual(loop("DEV-7", action="status").state, result.state)
        self.assertEqual(next(r for r in metrics.read() if r["run_id"] == pass_id), recovered)

    def test_recovered_implementation_metrics_preserve_interval_and_verified_result(self):
        for phase, outcome, killed in (("implementation", "completed", False), ("implementation", "blocked", True),
                                      ("implementation", "failed", False), ("fixes", "completed", True)):
            with self.subTest(phase=phase, outcome=outcome, killed=killed):
                case = RecoveryTests(); case.setUp()
                try:
                    case.check_recovered_metrics(phase, outcome, killed=killed)
                finally:
                    case.doCleanups()

    def test_recovered_review_metrics_preserve_interval_and_verified_verdict(self):
        for phase, outcome, killed in (("review", "clean", False), ("review", "findings", True),
                                      ("review", "blocked", False), ("review", "failed", True),
                                      ("rereview", "clean", True)):
            with self.subTest(phase=phase, outcome=outcome, killed=killed):
                case = RecoveryTests(); case.setUp()
                try:
                    case.check_recovered_metrics(phase, outcome, killed=killed)
                finally:
                    case.doCleanups()

    def test_rejected_recovery_cannot_upgrade_metrics_or_accept_review(self):
        before = self.interrupt("review")
        metrics = MetricsStore(self.registry.path)
        original = metrics.read()[-1]
        self.assertEqual(original["status"], "interrupted")
        with patch.object(self.adapter, "observe_delivery", side_effect=TaskError("Unverified completion")):
            result = loop("DEV-7")
        self.assertEqual(result.state, "escalated", result.render())
        self.assertEqual(metrics.read()[-1], dict(original, loop_status="escalated"))
        self.assertEqual(self.store.read()[0]["active_pass"], before["active_pass"])
        self.assertEqual((len(self.impl_prompts), len(self.prompts)), (1, 1))
        self.assertIsNone(PublicationStore(self.path).read()["acceptance"])

    def test_invalid_metrics_do_not_block_verified_recovery_or_authorize_replay(self):
        before = self.interrupt("review")
        metrics = MetricsStore(self.registry.path)
        path = metrics.directory / "runs.json"
        value = json.loads(path.read_text())
        value["runs"][-1]["findings_count"] = -1
        raw = json.dumps(value)
        path.write_text(raw)
        result = loop("DEV-7")
        self.assertEqual(result.state, "clean", result.render())
        self.assertIsNone(metrics.read())
        self.assertEqual(path.read_text(), raw)
        self.assertEqual((len(self.impl_prompts), len(self.prompts)), (1, 1))
        self.assertEqual(PublicationStore(self.path).read()["acceptance"]["pass_id"], before["active_pass"]["pass_id"])

    def test_lost_or_conflicting_evidence_never_replays(self):
        for change in ("truncated", "forged", "wrong_pass", "session", "moved", "git", "issue", "base", "slice", "receipt", "symlink", "seal_run", "seal_pass", "seal_partial", "seal_hash"):
            with self.subTest(change=change):
                case = RecoveryTests(); case.setUp()
                try:
                    state = case.interrupt("implementation")
                    directory = Path(state["delivery"]["directory"])
                    output = directory / "result.json"
                    # Expired legacy thresholds do not bypass any integrity checks.
                    state["delivery"]["deadline"] = time.time() - 1268
                    case.store.save(state)
                    if change.startswith("seal_"):
                        seal_file = directory / "completion.json"
                        proof = json.loads(seal_file.read_text())
                        key = {"seal_run": "run_id", "seal_pass": "pass_id", "seal_hash": "result_sha256"}
                        if change == "seal_partial":
                            seal_file.write_text('{"run_id":')
                        else:
                            proof[key[change]] = "forged"
                            seal_file.write_text(json.dumps(proof))
                    elif change == "truncated":
                        output.write_text('{"pass_id":')
                    elif change in {"forged", "wrong_pass"}:
                        value = json.loads(output.read_text())
                        value["summary" if change == "forged" else "pass_id"] = "forged"
                        output.write_text(json.dumps(value))
                    elif change == "session":
                        case.registry.update("DEV-7-I1", session_id="/replacement.jsonl")
                    elif change == "moved":
                        case.panes[0]["pane_id"] = "moved"
                    elif change == "git":
                        (case.path / "external.txt").write_text("drift after result")
                    elif change == "issue":
                        case.linear.get_issue.return_value = replace(case.linear.get_issue.return_value, description="changed")
                    elif change == "base":
                        case.command(case.repo, "commit", "--allow-empty", "-m", "advanced base")
                    elif change == "slice":
                        scope_file = case.git.scope_file(case.path)
                        scope = json.loads(scope_file.read_text())
                        scope["slice"] = case.branch.removeprefix("dev-7-")
                        scope_file.write_text(json.dumps(scope))
                    elif change == "receipt":
                        case.enterContext(patch.object(case.implementer, "observe_delivery", side_effect=TaskError("Wrong provider pass")))
                    elif change == "symlink":
                        output.unlink(); output.symlink_to(directory / "complete.py")
                    result = loop("DEV-7")
                    case.assertEqual(result.state, "escalated", result.render())
                    case.assertEqual((len(case.impl_prompts), len(case.prompts)), (1, 0))
                    case.assertEqual(case.store.read()[0]["active_pass"], state["active_pass"])
                    case.assertIsNone(PublicationStore(case.path).read()["acceptance"])
                finally:
                    case.doCleanups()

    def test_pause_remains_sticky_while_collecting_interrupted_work(self):
        state = self.interrupt("implementation")
        self.store.pause()
        result = loop("DEV-7")
        self.assertEqual(result.state, "paused", result.render())
        self.assertEqual((len(self.impl_prompts), len(self.prompts)), (1, 0))
        self.assertEqual(loop("DEV-7", action="continue").state, "clean")

    def test_review_drift_cannot_be_forgotten_after_restoration_and_restart(self):
        for phase in ("review", "rereview"):
            for boundary in ("restart", "base_observation", "snapshot_error", "poll", "poll_interrupt", "provider_result", "final_snapshot"):
                with self.subTest(phase=phase, boundary=boundary):
                    case = RecoveryTests(); case.setUp()
                    try:
                        before = case.interrupt(phase)
                        tracked = case.path / "tracked.txt"
                        original = tracked.read_bytes()
                        prompts = (len(case.impl_prompts), len(case.prompts))
                        calls = []
                        revoke = LoopRuntime.revoke_active_acceptance
                        def revoke_after_persistence(runtime, state):
                            # Invalidation must be durable before escalation,
                            # not merely included in the controller's final save.
                            case.assertTrue(LoopStore(case.path).read()[0]["delivery"]["invalidated"])
                            return revoke(runtime, state)
                        case.enterContext(patch.object(LoopRuntime, "revoke_active_acceptance", revoke_after_persistence))
                        def drift(*args):
                            calls.append(True)
                            tracked.write_text("edit observed during recovery")
                            if boundary == "poll_interrupt":
                                raise KeyboardInterrupt()
                            return "working"
                        if boundary == "restart":
                            tracked.write_text("edit before restart")
                            result = loop("DEV-7")
                        elif boundary == "base_observation":
                            def changed_base(*args, **kwargs):
                                env = environment(*args, **kwargs)
                                # A base read observes drift, then the ref is
                                # restored before the next snapshot is captured.
                                env.base = "0" * 40
                                return env
                            with patch("task_start.loop.environment", side_effect=changed_base):
                                result = loop("DEV-7")
                        elif boundary == "snapshot_error":
                            # Git omits untracked FIFOs; replace a tracked path
                            # so the snapshot must inspect the unsupported type.
                            tracked.unlink()
                            os.mkfifo(tracked)
                            result = loop("DEV-7")
                            tracked.unlink()
                        elif boundary in {"poll", "poll_interrupt"}:
                            def stopped_wait(*args):
                                raise KeyboardInterrupt()
                            with patch.object(case.adapter, "status", side_effect=drift), \
                                    patch("task_start.loop.time", SimpleNamespace(time=time.time, sleep=stopped_wait)):
                                result = loop("DEV-7")
                            case.assertEqual(len(calls), 1)
                        elif boundary == "provider_result":
                            def completed(*args):
                                if args[-1] is not None:
                                    drift()
                                return True
                            with patch.object(case.adapter, "observe_delivery", side_effect=completed):
                                result = loop("DEV-7")
                        else:
                            original_snapshot = LoopRuntime.snapshot
                            def snapshot(runtime):
                                if hasattr(runtime, "completed_snapshot"):
                                    drift()
                                return original_snapshot(runtime)
                            with patch.object(LoopRuntime, "snapshot", snapshot):
                                result = loop("DEV-7")
                        case.assertIn(result.state, {"escalated", "interrupted"}, result.render())
                        # Reopen the durable checkpoint before restoring the bytes.
                        saved = LoopStore(case.path).read()[0]
                        case.assertTrue(saved["delivery"]["invalidated"])
                        tracked.write_bytes(original)
                        result = loop("DEV-7")
                        case.assertEqual(result.state, "escalated", result.render())
                        case.assertIn("invalidated", result.data["reason"])
                        case.assertEqual(LoopStore(case.path).read()[0]["active_pass"], before["active_pass"])
                        case.assertEqual((len(case.impl_prompts), len(case.prompts)), prompts)
                        case.assertIsNone(PublicationStore(case.path).read()["acceptance"])
                    finally:
                        case.doCleanups()

    def test_recreated_reviewer_recipient_survives_interruption_without_replay(self):
        self.review_results = [loops.findings(loops.finding())]
        self.on_review = lambda _: self.store.pause()
        self.assertEqual(loop("DEV-7", timeout=10).state, "paused")
        original = self.registry.get("DEV-7-R1")
        self.panes = [p for p in self.panes if p["pane_id"] != original["pane_id"]]
        recipient_at_claim = []
        claim = reviews.delivery_fixture.claim
        def claimed(test, execution, reference):
            claim(test, execution, reference)
            if execution.purpose == "review":
                recipient_at_claim.append(self.store.read()[0]["delivery"]["location"])
        def interrupted(execution):
            raise KeyboardInterrupt()
        self.on_review = interrupted
        with patch.object(reviews.delivery_fixture, "claim", side_effect=claimed):
            result = loop("DEV-7", action="continue")
        self.assertEqual(result.state, "interrupted", result.render())
        before = self.store.read()[0]
        rebound = self.registry.get("DEV-7-R1")
        expected = {k: rebound[k] for k in ("pane_id", "tab_id", "terminal_id")}
        self.assertEqual(recipient_at_claim, [expected])
        self.assertEqual(before["delivery"]["location"], expected)
        self.assertNotEqual(rebound["terminal_id"], original["terminal_id"])
        self.assertEqual(rebound["session_id"], original["session_id"])
        self.assertEqual(self.recreated, [True])
        self.on_review = None
        result = loop("DEV-7")
        self.assertEqual(result.state, "clean", result.render())
        self.assertEqual((len(self.impl_prompts), len(self.prompts)), (2, 2))
        after = self.store.read()[0]
        self.assertEqual(after["reviewer"], before["reviewer"])
        self.assertEqual(after["records"][-1]["pass_id"], before["active_pass"]["pass_id"])
        self.assertEqual(PublicationStore(self.path).read()["acceptance"]["session"], before["reviewer"]["session"])

    def test_recovery_owns_existing_lock_and_preserves_limits(self):
        self.interrupt("implementation")
        with PublicationStore(self.path).locked(), self.assertRaisesRegex(TaskError, "owns this worktree"):
            loop("DEV-7")
        for options in (dict(timeout=11), dict(model="replacement"), dict(max_reviews=7)):
            with self.assertRaisesRegex(TaskError, "preserves recorded"):
                loop("DEV-7", **options)
        self.assertEqual(loop("DEV-7").state, "clean")

    def test_claim_before_delivery_refuses_new_or_replay(self):
        with patch.object(PassDelivery, "attach", side_effect=KeyboardInterrupt()):
            result = loop("DEV-7", timeout=10)
        self.assertEqual(result.state, "interrupted")
        with self.assertRaisesRegex(TaskError, "no durable delivery proof"):
            loop("DEV-7")
        with self.assertRaises(TaskError):
            loop("DEV-7", action="new")
        self.assertEqual(self.impl_prompts, [])

    def test_complete_output_survives_controller_downtime(self):
        state = self.interrupt("implementation")
        with patch("task_start.loop.time.time", return_value=state["delivery"]["deadline"] + 1):
            # Completion happened on time; controller downtime does not erase it.
            self.assertEqual(loop("DEV-7").state, "clean")

    def test_consumed_review_and_cleanup_survive_interruption_without_reacceptance(self):
        accepted = []
        def stop_after_consumption(result):
            accepted.append(result["pass_id"])
            if result["state"] == "clean":
                raise KeyboardInterrupt()
        result = loop("DEV-7", timeout=10, on_pass_result=stop_after_consumption)
        self.assertEqual(result.state, "interrupted")
        state = self.store.read()[0]
        self.assertIsNone(state["active_pass"])
        directory = Path(state["delivery"]["directory"])
        before = PublicationStore(self.path).read()["acceptance"]
        self.assertEqual(loop("DEV-7", on_pass_result=lambda _: self.fail("Already consumed")).state, "clean")
        self.assertEqual(PublicationStore(self.path).read()["acceptance"], before)
        self.assertFalse(directory.exists())
        self.assertEqual((len(self.impl_prompts), len(self.prompts), len(accepted)), (1, 1, 2))

    def test_partial_result_while_provider_working_remains_pending(self):
        state = self.interrupt("implementation")
        directory = Path(state["delivery"]["directory"])
        raw = (directory / "result.json").read_bytes()
        (directory / "result.json").write_bytes(b'{"partial":')
        calls = []
        def observe(execution, reference, receipt, completion):
            calls.append(completion)
            if len(calls) == 1:
                return False  # Runtime idleness does not override this turn.
            (directory / "result.json").write_bytes(raw)
            return True
        with patch.object(self.implementer, "observe_delivery", side_effect=observe):
            result = loop("DEV-7")
        self.assertEqual(result.state, "clean", result.render())
        self.assertEqual(len(self.impl_prompts), 1)
        self.assertGreaterEqual(len(calls), 3)

    def test_intervening_failed_review_cannot_resurrect_older_acceptance(self):
        before = self.interrupt("review")
        self.raw = "malformed"
        self.assertEqual(review("DEV-7").state, "failed")
        self.assertIsNone(PublicationStore(self.path).read()["acceptance"])
        result = loop("DEV-7")
        self.assertEqual(result.state, "escalated")
        self.assertIn("context set changed", result.data["reason"])
        self.assertEqual(self.store.read()[0]["active_pass"], before["active_pass"])
        self.assertIsNone(PublicationStore(self.path).read()["acceptance"])

    def test_publication_change_during_interrupted_review_refuses_acceptance(self):
        self.interrupt("review")
        publication = PublicationStore(self.path)
        saved = publication.read()
        saved["publication_history"] = dict(intervening="publication")
        publication.write(saved)
        result = loop("DEV-7")
        self.assertEqual(result.state, "escalated")
        self.assertIn("Publication state changed", result.data["reason"])
        self.assertIsNone(publication.read()["acceptance"])


class ProcessRecoveryTests(unittest.TestCase):
    """A terminal-owned fake Codex process outlives killed controller processes."""
    command = bootstrap.BootstrapLoopTests.command
    pane_command = bootstrap.BootstrapLoopTests.pane_command
    worktrees = bootstrap.BootstrapLoopTests.worktrees
    select_adapter = bootstrap.BootstrapLoopTests.select_adapter
    implementation_adapter = bootstrap.BootstrapLoopTests.implementation_adapter
    update_base = bootstrap.BootstrapLoopTests.update_base

    def setUp(self):
        bootstrap.BootstrapLoopTests.setUp(self)
        self.local = replace(self.local, agent=reviews.AgentConfig("codex", "initial-codex", "high"))
        self.root = self.repo.parent
        self.provider = self.root / "provider.json"
        self.gate = self.root / "finish"
        self.processes = []
        self.enterContext(patch("task_start.implementation_pass.adapter_for", side_effect=Codex))
        self.enterContext(patch("task_start.agent.adapter_for", side_effect=lambda options:
            Codex(options) if options.model == "initial-codex" else self.adapter))
        self.enterContext(patch.object(Codex, "check_available"))
        self.enterContext(patch.object(Codex, "clear_shell_input"))
        self.enterContext(patch.object(Codex, "filter_shell_credentials"))
        self.enterContext(patch.object(Codex, "command", side_effect=self.herdr))
        rpc = self.enterContext(patch("task_start.agent.CodexRPC"))
        rpc.return_value.__enter__.return_value.request.side_effect = self.request
        self.enterContext(patch.object(self.identities, "snapshot", side_effect=self.pane_snapshot))
        self.addCleanup(self.stop_children)

    def read_provider(self):
        return json.loads(self.provider.read_text()) if self.provider.exists() else None

    def save_provider(self, value):
        target = self.provider.with_suffix(".next")
        target.write_text(json.dumps(value)); target.replace(self.provider)

    def pane_snapshot(self):
        value = self.read_provider()
        if value:
            self.panes[0].update(value["pane"])
        return copy.deepcopy(self.panes)

    def herdr(self, group, operation, *args, **kwargs):
        if (group, operation) == ("pane", "list"):
            return dict(panes=self.pane_snapshot())
        if operation == "start":
            self.assertIsNone(self.read_provider())
            pane = dict(self.panes[0], agent="codex", agent_status="idle", label="DEV-7-I1",
                        foreground_cwd=str(self.path), launch_pending=False,
                        agent_session=dict(agent="codex", kind="id", value="01a0d314-bd68-7203-8b68-f2520f892afa"))
            value = dict(pane=pane, bootstrap=args[-1], argv=["codex", *args[args.index("--") + 1:]], prompts=[])
            self.save_provider(value)
            return dict(agent=pane, argv=value["argv"])
        if operation == "get":
            return dict(agent=self.read_provider()["pane"])
        self.fail((group, operation))

    def request(self, method, params, **kwargs):
        value = self.read_provider()
        thread_id = value["pane"]["agent_session"]["value"] if value else None
        thread = dict(id=thread_id, cwd=str(self.path), preview=value["bootstrap"] if value else "")
        if method == "thread/list":
            return dict(data=[thread])
        if method == "thread/read":
            return dict(thread=thread)
        if method == "thread/items/list":
            items = [dict(turnId="ready", item=dict(type="userMessage", clientId="native",
                          content=[dict(type="text", text=value["bootstrap"])]))]
            if value["prompts"]:
                queued = value["prompts"][0]
                items.append(dict(turnId="task", item=dict(type="userMessage",
                    clientId=queued["clientUserMessageId"], content=queued["input"])))
            return dict(data=items, nextCursor=None)
        if method == "thread/queue/add":
            self.assertEqual(value["prompts"], [])
            value["prompts"].append(params)
            value["pane"]["agent_status"] = "working"
            self.save_provider(value)
            # An independent test process models the original terminal-owned
            # agent. Its only edits are in this disposable task checkout.
            self.worker = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "worker",
                str(self.provider), str(self.gate), str(self.path)], start_new_session=True,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            (self.root / "worker.pid").write_text(str(self.worker.pid))
            return dict(queuedSubmission=dict(id="one", **params))
        if method == "thread/queue/list":
            return dict(data=[], nextCursor=None)
        if method == "thread/turns/list":
            queued = value["prompts"][0]
            items = [dict(type="userMessage", clientId=queued["clientUserMessageId"], content=queued["input"])]
            if value.get("completion"):
                items.append(dict(type="agentMessage", phase="final_answer", text=value["completion"]))
            return dict(data=[dict(id="task", status="completed" if value.get("completion") else "inProgress",
                                  error=None, itemsView="full", items=items)], nextCursor=None)
        self.fail(method)

    def controller(self, name, threshold=20):
        output = self.root / (name + ".json")
        diagnostics = self.root / (name + ".stderr")
        def run():
            with output.open("w") as target, diagnostics.open("w") as errors, \
                    patch("sys.stdout", target), patch("sys.stderr", errors):
                code = cli.main(["loop", "DEV-7", "--json", "--timeout", str(threshold)])
            os._exit(code)
        process = multiprocessing.get_context("fork").Process(target=run)
        process.start(); self.processes.append(process)
        return process, output

    def wait_until(self, predicate):
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.03)
        self.fail("Controlled subprocess failed to reach its boundary")

    def stop_children(self):
        self.gate.touch()
        for process in self.processes:
            if process.is_alive():
                process.terminate()
            process.join(5)
        pid = self.root / "worker.pid"
        if pid.exists():
            try:
                os.kill(int(pid.read_text()), signal.SIGTERM)
            except ProcessLookupError:
                pass
        if self.path.exists():
            state = LoopStore(self.path).read()[0]
            if state.get("delivery"):
                import shutil
                shutil.rmtree(state["delivery"]["directory"], ignore_errors=True)

    def scenario(self, sig, while_working, expected="clean"):
        first, first_output = self.controller("first")
        self.wait_until(lambda: self.read_provider() and self.read_provider()["prompts"])
        self.wait_until(lambda: self.registry.get("DEV-7-I1")["state"] == "active")
        os.kill(first.pid, sig)
        first.join(10); self.assertFalse(first.is_alive())
        self.store = LoopStore(self.path)
        before = self.store.read()[0]
        self.assertEqual(before["status"], "running" if sig == signal.SIGKILL else "interrupted")
        if not while_working:
            self.gate.touch()
            self.wait_until(lambda: self.read_provider().get("completion"))
        second, result_file = self.controller("second")
        if while_working:
            time.sleep(0.3)
            self.assertTrue(second.is_alive())
            self.assertEqual(self.store.read()[0]["pass_count"], 1)
            self.assertEqual(len(self.read_provider()["prompts"]), 1)
            # A concurrent restart cannot become a second controller.
            third, _ = self.controller("concurrent")
            third.join(10); self.assertEqual(third.exitcode, 1)
            self.gate.touch()
        second.join(15)
        self.assertFalse(second.is_alive())
        result = json.loads(result_file.read_text())
        self.assertEqual(result["state"], expected, result)
        after = self.store.read()[0]
        self.assertEqual(after["run_id"], before["run_id"])
        self.assertEqual((after["pass_count"], after["review_count"]), (2, 1))
        self.assertEqual(len(self.read_provider()["prompts"]), 1)
        self.assertEqual([c["context_id"] for c in self.registry.list()], ["DEV-7-I1", "DEV-7-R1"])
        self.assertIn(dict(name="live", result="not_run", details="Controlled provider fixture"), result["validation"])

    def test_overdue_controller_crash_keeps_turn_time_and_collects_late_seal_once(self):
        first, _ = self.controller("first", threshold=1)
        first_errors = self.root / "first.stderr"
        self.wait_until(lambda: first_errors.exists() and "warning threshold" in first_errors.read_text())
        self.store = LoopStore(self.path)
        before = self.store.read()[0]
        self.assertTrue(first.is_alive())
        self.assertEqual(before["status"], "running")
        self.assertIsNone(self.read_provider().get("completion"))
        os.kill(first.pid, signal.SIGKILL)
        first.join(10)
        self.assertFalse(first.is_alive())
        second, output = self.controller("second", threshold=1)
        second_errors = self.root / "second.stderr"
        self.wait_until(lambda: second_errors.exists() and "warning threshold" in second_errors.read_text())
        self.assertTrue(second.is_alive())
        self.assertEqual(self.store.read()[0]["delivery"], before["delivery"])
        self.assertEqual(len(self.read_provider()["prompts"]), 1)
        self.store.pause()  # Keep late implementation consumption inspectable.
        self.gate.touch()
        second.join(15)
        self.assertFalse(second.is_alive())
        self.assertEqual(json.loads(output.read_text())["state"], "paused")
        after = self.store.read()[0]
        self.assertEqual(after["records"][0]["pass_id"], before["active_pass"]["pass_id"])
        self.assertEqual(after["run_id"], before["run_id"])
        self.assertEqual(after["timeout"], 1)
        self.assertEqual(first_errors.read_text().count("warning threshold"), 1)
        self.assertEqual(second_errors.read_text().count("warning threshold"), 1)
        # Parent's resumed controller uses the same fixture provider history.
        result = loop("DEV-7", action="continue")
        self.assertEqual(result.state, "clean", result.render())
        self.assertEqual((result.data["passes"], result.data["reviews"]), (2, 1))
        self.assertEqual(len(self.read_provider()["prompts"]), 1)
        self.assertEqual([c["context_id"] for c in self.registry.list()], ["DEV-7-I1", "DEV-7-R1"])

    def test_ctrl_c_complete_then_restart(self):
        self.scenario(signal.SIGINT, False)

    def test_sigterm_restart_observes_same_running_agent(self):
        self.scenario(signal.SIGTERM, True)

    def test_sigkill_restart_observes_same_running_agent(self):
        self.scenario(signal.SIGKILL, True)

    def test_terminal_disconnect_complete_then_restart(self):
        self.scenario(signal.SIGHUP, False)

    def test_recovered_implementation_reaches_normal_human_finding_routing(self):
        self.review_results = [loops.findings(loops.finding(category="human_decision"))]
        self.scenario(signal.SIGINT, False, expected="escalated")


def worker(provider, gate, checkout):
    provider, gate, checkout = Path(provider), Path(gate), Path(checkout)
    deadline = time.monotonic() + 30
    while not gate.exists() and time.monotonic() < deadline:
        time.sleep(0.03)
    value = json.loads(provider.read_text())
    prompt = value["prompts"][0]["input"][0]["text"]
    output = Path(re.search(r"Write your result to (.*?)\. This temporary", prompt)[1])
    pass_id = re.search(r"Pass ID: ([^\n]+)", prompt)[1]
    (checkout / "implemented.txt").write_text("Completed by the original provider process")
    output.write_text(json.dumps(dict(pass_id=pass_id, state="completed", summary="Implementation completed",
        checks=[dict(name="live", result="not_run", details="Controlled provider fixture")], resolutions=[],
        task_assessment=dict(state="ready", summary="Fixture ready", questions=[]))))
    value["completion"] = subprocess.check_output([sys.executable, str(output.parent / "complete.py")], text=True).strip()
    value["pane"]["agent_status"] = "done"
    temporary = provider.with_suffix(".worker")
    temporary.write_text(json.dumps(value)); temporary.replace(provider)


if __name__ == "__main__" and len(sys.argv) > 1 and sys.argv[1] == "worker":
    worker(*sys.argv[2:])
