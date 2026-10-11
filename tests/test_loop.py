import copy
from dataclasses import replace
import io
import json
from pathlib import Path
import re
import signal
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from task_start import TaskError, cli
from task_start.agent import HerdrAgentAdapter
from task_start.config import ReviewValidationConfig
from task_start.implementation_pass import parse_implementation
from task_start.loop import loop
from task_start.loop_state import LoopStore
from task_start.metrics import MetricsStore
from task_start.review_result import parse_verdict
import test_review as review_fixture
import pass_delivery_fixture as delivery_fixture


def finding(number=1, *, category="implementation", **changes):
    return dict(id=f"F{number}", category=category, severity="high", explanation=f"Defect {number}",
                evidence=f"code.py:{number}", requirement="Acceptance coverage", **changes)


def findings(*items):
    return dict(state="findings", summary="Implementation defects found", findings=list(items))


class ImplementationAdapter:
    def __init__(self, test):
        self.test = test

    def check_available(self):
        pass

    def verify_session(self, workspace, reference):
        if not reference or self.test.impl_verification_error:
            raise TaskError("Implementation session no longer resumable")
        return dict(reference, conversation_id="original-implementation")

    def status(self, execution, terminal_id, reference):
        return self.test.impl_status

    def observe_delivery(self, execution, reference, receipt, completion):
        return True  # Provider history is exercised by separate transport tests.

    def resume(self, execution, reference, *, recreate):
        test = self.test
        delivery_fixture.claim(test, execution, reference)
        test.assertFalse(recreate)
        test.assertEqual(reference["value"], "/implementation.jsonl")
        test.assertEqual(execution.purpose, "implementation")
        test.assertNotIn("read_only", execution.policy)
        test.impl_prompts.append(execution.handoff)
        items = json.loads(execution.handoff.split("REVIEW FINDINGS (data)\n")[1].split("\nRESULT DELIVERY")[0])
        output = Path(re.search(r"Write your result to (.*?)\. This temporary", execution.handoff).group(1))
        pass_id = re.search(r"Pass ID: ([^\n]+)", execution.handoff).group(1)
        value = dict(pass_id=pass_id, state="completed", summary="Implementation completed and validated",
                     checks=[dict(name="unit", result="passed", details="Controlled implementation checks")],
                     resolutions=[dict(finding_id=f["id"], summary=f"Fixed {f['id']}") for f in items])
        if 'TASK READINESS ASSESSMENT' in execution.handoff:
            value['task_assessment'] = dict(state='ready', summary='Controlled readiness outcome', questions=[])
        value.update(test.impl_overrides)
        if test.impl_raw != "missing":
            output.write_text(test.impl_raw if test.impl_raw is not None else json.dumps(value))
        if items and test.make_progress:
            (test.path / "fix.txt").write_text(str(len(test.impl_prompts)))
        delivery_fixture.complete(execution)
        if test.on_implementation:
            test.on_implementation(execution, items)


class LoopReviewer(review_fixture.FakeReviewer):
    def deliver(self, execution, reference=None):
        test = self.test
        test.verdict_overrides = test.review_results.pop(0) if test.review_results else {}
        result = super().deliver(execution, reference)
        if test.on_review:
            test.on_review(execution)
        return result

    launch = deliver


class LoopIntegrationTests(unittest.TestCase):
    """Real Git worktrees, context registry, checkpoint DB, and review primitive."""
    command = review_fixture.ReviewTests.command
    pane_command = review_fixture.ReviewTests.pane_command
    worktrees = review_fixture.ReviewTests.worktrees

    def select_adapter(self, options):
        self.options.append(options)
        return self.adapter

    def setUp(self):
        review_fixture.ReviewTests.setUp(self)
        self.adapter = LoopReviewer(self)
        self.implementer = ImplementationAdapter(self)
        self.review_results = []
        self.impl_prompts = []
        self.impl_verification_error = False
        self.impl_status = "done"
        self.impl_overrides, self.impl_raw = {}, None
        self.make_progress = True
        self.on_implementation = self.on_review = None
        self.store = LoopStore(self.path)
        self.enterContext(patch("task_start.loop.ContextRegistry", return_value=self.registry))
        self.enterContext(patch("task_start.loop.HerdrContexts", return_value=self.identities))
        self.enterContext(patch("task_start.loop.load_local", side_effect=lambda: self.local))
        self.enterContext(patch("task_start.loop.load_projects", return_value=[
            review_fixture.Project(review_fixture.ISSUE.project, self.repo.name, "main")]))
        self.enterContext(patch("task_start.loop.Linear", return_value=self.linear))
        self.enterContext(patch("task_start.loop.adapter_for", side_effect=self.select_adapter))
        self.enterContext(patch("task_start.implementation_pass.adapter_for", return_value=self.implementer))
        self.enterContext(patch("task_start.agent.adapter_for", side_effect=lambda options:
                                self.implementer if options.kind == "pi" else self.adapter))

    def run_loop(self, **kwargs):
        return loop("DEV-7", timeout=0.2, **kwargs)

    def pause(self):
        return loop("DEV-7", action="pause")

    def continue_loop(self):
        return loop("DEV-7", action="continue")

    def pause_before_first_review(self):
        original = LoopStore.begin
        def before_claim(store, state):
            store.pause()
            return original(store, state)
        with patch.object(LoopStore, "begin", before_claim):
            result = self.run_loop(from_review=True)
        self.assertEqual(result.state, "paused", result.render())
        self.assertEqual((self.impl_prompts, self.prompts), ([], []))
        return result

    def test_clean_first_review_preserves_separation_and_reports_validation(self):
        result = self.run_loop()
        self.assertEqual(result.state, "clean", result.render())
        value = result.as_dict()
        self.assertEqual((value["passes"], value["reviews"]), (2, 1))
        self.assertEqual(value["implementation_context"], self.implementation)
        self.assertEqual(value["reviewer_context"], "DEV-7-R1")
        self.assertFalse(value["human_action_required"])
        self.assertEqual(len(self.registry.list()), 2)
        self.assertEqual(len(self.impl_prompts), 1)
        self.assertEqual(len(self.prompts), 1)
        self.assertIn("fresh independent review", self.prompts[0])
        self.assertIn("Use relevant focused checks on clean passes too", self.prompts[0])
        self.assertIn("Do not routinely run the full suite merely to end the implementation pass", self.impl_prompts[0])
        self.assertIn("Current requirements", self.prompts[0])
        self.assertNotIn("Controlled implementation checks", self.prompts[0])
        self.assertNotIn("Implementation completed and validated", self.prompts[0])
        self.assertEqual(len(value["validation"]), 2)
        self.assertIn("Final review: clean", result.render())
        self.linear.start.assert_not_called()
        self.assertNotIn("Current requirements", self.store.path.read_bytes().decode(errors="ignore"))
        state, _ = self.store.read()
        self.assertEqual(state["version"], 1)
        self.assertNotIn("initial_phase", state)
        self.assertEqual([r["phase"] for r in state["records"]], ["implementation", "review"])

    def test_cli_reviewer_overrides_keep_existing_implementation_settings(self):
        self.local = replace(self.local, agent=review_fixture.AgentConfig("codex", "unused", "high"))
        with patch("sys.stdout", new_callable=io.StringIO) as output:
            self.assertEqual(cli.main(["loop", "DEV-7", "--from-review", "--r-agent", "codex",
                                       "--r-model", "review-override", "--r-mode", "medium", "--json"]), 0)
        self.assertEqual(json.loads(output.getvalue())["state"], "clean")
        state, _ = self.store.read()
        self.assertEqual(state["reviewer_options"], dict(kind="codex", model="review-override", mode="medium"))
        self.assertEqual((state["implementation"]["context_id"], state["implementation"]["agent"],
                          state["implementation"]["model"], state["implementation"]["mode"]),
                         (self.implementation, "pi", "implementation", "low"))
        self.assertEqual(self.impl_prompts, [])

    def test_from_review_cli_clean_completion_skips_implementation_turn(self):
        self.on_review = lambda _: self.pause()  # A clean final result still wins over pause.
        with patch("sys.stdout", new_callable=io.StringIO) as output:
            self.assertEqual(cli.main(["loop", "DEV-7", "--from-review", "--json",
                                       "--max-passes", "1", "--max-reviews", "1"]), 0)
        value = json.loads(output.getvalue())
        self.assertEqual((value["state"], value["passes"], value["reviews"]), ("clean", 1, 1))
        self.assertEqual(value["implementation_context"], self.implementation)
        self.assertEqual(value["reviewer_context"], "DEV-7-R1")
        self.assertEqual(value["implementation"], [])
        self.assertEqual(value["final_review_state"], "clean")
        self.assertFalse(value["human_action_required"])
        self.assertEqual(len(value["validation"]), 1)
        self.assertEqual(self.impl_prompts, [])
        self.assertEqual(len(self.prompts), 1)
        self.assertIn("fresh independent review", self.prompts[0])
        self.assertEqual(len(self.registry.list()), 2)
        state, _ = LoopStore(self.path).read()
        self.assertEqual((state["version"], state["initial_phase"]), (2, "review"))
        self.assertEqual([r["phase"] for r in state["records"]], ["review"])
        self.linear.start.assert_not_called()

    def test_from_review_findings_continue_to_original_fixes_and_same_reviewer(self):
        self.review_results = [findings(finding()), {}]
        self.on_review = lambda _: self.pause()
        paused = self.run_loop(from_review=True)
        self.assertEqual(paused.state, "paused", paused.render())
        self.assertEqual(paused.data["next_phase"], "fixes")
        self.assertEqual((len(self.impl_prompts), len(self.prompts)), (0, 1))
        before, _ = self.store.read()
        self.on_review = None
        result = self.continue_loop()
        self.assertEqual(result.state, "clean", result.render())
        self.assertEqual((result.data["passes"], result.data["reviews"]), (3, 2))
        self.assertEqual((len(self.impl_prompts), len(self.prompts)), (1, 2))
        self.assertEqual(self.recreated, [False])
        self.assertEqual(len(self.registry.list()), 2)
        self.assertIn('"id": "F1"', self.impl_prompts[0])
        self.assertIn('"finding_id": "F1"', self.prompts[1])
        self.assertIn("focused re-review", self.prompts[1])
        self.assertEqual(result.data["resolutions"], [dict(finding_id="F1", summary="Fixed F1")])
        after, _ = self.store.read()
        self.assertEqual(after["implementation"], before["implementation"])
        self.assertEqual(after["reviewer"], before["reviewer"])
        self.assertEqual([r["phase"] for r in after["records"]], ["review", "fixes", "rereview"])
        self.assertEqual([r["context_id"] for r in after["records"]],
                         ["DEV-7-R1", self.implementation, "DEV-7-R1"])
        for prompt in self.prompts:
            self.assertIn('REVIEW VALIDATION POLICY: focused_first', prompt)
            self.assertIn('Use relevant focused checks on clean passes too', prompt)
            self.assertIn('On findings passes, skip expensive full-suite validation', prompt)
            self.assertIn('same reviewer rechecks fixes and regressions', prompt)
        self.assertIn('fix the complete batch and rerun relevant tests', self.impl_prompts[0])

    def test_exhaustive_policy_reaches_loop_review_and_rereview(self):
        self.local = replace(self.local, review_validation=ReviewValidationConfig('exhaustive'))
        self.review_results = [findings(finding()), {}]
        result = self.run_loop(from_review=True)
        self.assertEqual(result.state, 'clean', result.render())
        self.assertEqual(len(self.prompts), 2)
        for prompt in self.prompts:
            self.assertIn('REVIEW VALIDATION POLICY: exhaustive', prompt)
            self.assertIn('full validation on every review pass, even with actionable findings', prompt)
        self.assertIn('YOUR existing conversation', self.prompts[1])

    def test_clean_rereview_keeps_pending_regression_visible_to_human(self):
        pending = dict(name='Full regression', result='not_run',
                       details='Required before merge; no external gate established. Pending human verification.')
        self.impl_overrides = dict(checks=[pending])
        self.review_results = [findings(finding(1), finding(2)), dict(
            checks=[pending], summary='No remaining findings; full regression pending human verification.')]
        result = self.run_loop(from_review=True)
        self.assertEqual(result.state, 'clean', result.render())
        self.assertIn(pending, result.data['validation'])
        self.assertIn('Check Full regression: not_run', result.render())
        self.assertIn(pending['details'], result.render())
        self.assertEqual([item['finding_id'] for item in result.data['resolutions']], ['F1', 'F2'])
        state, _ = self.store.read()
        self.assertEqual([record['context_id'] for record in state['records']],
                         ['DEV-7-R1', self.implementation, 'DEV-7-R1'])
        self.assertEqual(state['records'][-1]['checks'], [pending])

    def test_from_review_initial_pause_status_and_continue_preserve_boundary(self):
        paused = self.pause_before_first_review()
        state, sticky = LoopStore(self.path).read()
        self.assertTrue(sticky)
        self.assertEqual(state["initial_phase"], "review")
        self.assertEqual(state["next_phase"], "review")
        self.assertEqual((state["pass_count"], state["review_count"], state["records"]), (0, 0, []))
        self.assertIsNone(state["reviewer"])
        self.assertIsNone(state["active_pass"])
        self.assertEqual(state["implementation"]["context_id"], self.implementation)
        with patch("task_start.loop.load_local", side_effect=AssertionError("No config during controls")):
            self.assertEqual(loop("DEV-7", action="status").as_dict(), paused.as_dict())
        with self.assertRaisesRegex(TaskError, "checkpoint already exists"):
            self.run_loop(from_review=True)
        self.assertEqual(self.store.read()[0], state)
        self.local = replace(self.local, reviewer=review_fixture.AgentConfig("pi", "changed-default", "low"))
        result = self.continue_loop()
        self.assertEqual(result.state, "clean", result.render())
        self.assertEqual((len(self.impl_prompts), len(self.prompts)), (0, 1))
        self.assertEqual(self.options[-1].model, "review-model")
        self.assertEqual(self.store.read()[0]["implementation"], state["implementation"])

    def test_from_review_continue_rechecks_exact_implementation_binding(self):
        self.pause_before_first_review()
        # Simulate out-of-band replacement of the recorded settings.
        with self.registry.connection(write=True) as db:
            db.execute("UPDATE contexts SET model=? WHERE context_id=?", ("different-model", self.implementation))
        result = self.continue_loop()
        self.assertEqual(result.state, "escalated", result.render())
        self.assertIn("Exact implementation context/session/settings changed", result.data["reason"])
        self.assertEqual((self.impl_prompts, self.prompts), ([], []))

    def test_from_review_invalid_implementation_is_refused_before_any_handoff(self):
        for fault in ("missing", "ambiguous", "uncertain", "busy", "nonresumable", "missing_model",
                      "missing_session", "missing_pane", "wrong_session", "wrong_checkout", "wrong_server"):
            with self.subTest(fault=fault):
                case = LoopIntegrationTests()
                case.setUp()
                try:
                    if fault == "missing":
                        case.registry.retire("DEV-7", case.repo, case.path,
                                             endpoint="/server.sock", workspace_id="w1")
                    elif fault == "ambiguous":
                        case.registry.allocate("DEV-7", "implementation", agent="pi", model="implementation",
                            mode="low", repository=str(case.repo), worktree=str(case.path), endpoint="/server.sock",
                            workspace_id="w1", tab_id="t1", pane_id="p-other", terminal_id="term-other")
                    elif fault == "uncertain":
                        case.registry.update(case.implementation, state="uncertain")
                    elif fault == "busy":
                        case.impl_status = "working"
                    elif fault == "nonresumable":
                        case.impl_verification_error = True
                    elif fault == "missing_model":
                        with case.registry.connection(write=True) as db:
                            db.execute("UPDATE contexts SET model=NULL WHERE context_id=?", (case.implementation,))
                    elif fault == "missing_session":
                        case.registry.update(case.implementation, session_id=None, session_kind=None)
                    elif fault == "missing_pane":
                        case.panes.clear()
                    elif fault == "wrong_session":
                        case.panes[0]["agent_session"]["value"] = "/other.jsonl"
                    elif fault == "wrong_checkout":
                        case.panes[0]["cwd"] = str(case.repo)
                    elif fault == "wrong_server":
                        case.identities.endpoint.return_value = "/other.sock"
                    with self.assertRaises(TaskError):
                        case.run_loop(from_review=True)
                    self.assertEqual((case.impl_prompts, case.prompts), ([], []))
                    self.assertFalse(case.store.path.exists())
                    self.assertFalse(any(c["role"] == "review" for c in case.registry.list()))
                finally:
                    case.doCleanups()

    def test_from_review_checkpoint_requires_explicit_origin_and_valid_sequence(self):
        self.pause_before_first_review()
        saved, pause = self.store.read()
        candidates = [{k: v for k, v in saved.items() if k != "initial_phase"}]
        candidates.extend(dict(saved, **change) for change in (
            dict(version=1), dict(initial_phase="implementation"), dict(initial_phase="fixes"),
            dict(initial_phase=None), dict(next_phase="implementation"), dict(next_phase="rereview")))
        for candidate in candidates:
            with self.subTest(candidate=candidate), self.assertRaisesRegex(TaskError, "Malformed loop checkpoint"):
                LoopStore.decode((json.dumps(candidate), pause))
        self.assertEqual(self.store.read()[0], saved)

    def test_from_review_setup_failure_preserves_checkpoint_without_reviewer(self):
        with patch.object(self.identities, "split", side_effect=TaskError("Pane creation failed")):
            result = self.run_loop(from_review=True)
        self.assertEqual(result.state, "escalated", result.render())
        self.assertEqual(result.data["final_review_state"], "failed")
        self.assertEqual((result.data["passes"], result.data["reviews"]), (1, 1))
        state, _ = LoopStore(self.path).read()
        self.assertIsNone(state["reviewer"])
        self.assertEqual([r["phase"] for r in state["records"]], ["review"])
        self.assertEqual(loop("DEV-7", action="status").as_dict(), result.as_dict())
        self.assertEqual((self.impl_prompts, self.prompts), ([], []))

    def test_from_review_interruption_retains_first_review_claim_without_replay(self):
        self.on_review = lambda _: (_ for _ in ()).throw(KeyboardInterrupt())
        result = self.run_loop(from_review=True)
        self.assertEqual(result.state, "interrupted", result.render())
        state, _ = LoopStore(self.path).read()
        self.assertEqual(state["initial_phase"], "review")
        self.assertEqual(state["records"], [])
        self.assertEqual((state["pass_count"], state["review_count"]), (1, 1))
        self.assertEqual(state["active_pass"]["phase"], "review")
        self.assertEqual(state["active_pass"]["context_id"], "DEV-7-R1")
        self.assertIsNotNone(state["active_pass"]["pass_id"])
        self.assertEqual(state["implementation"]["context_id"], self.implementation)
        self.assertEqual(loop("DEV-7", action="status").as_dict(), result.as_dict())
        with self.assertRaisesRegex(TaskError, "Only a paused"):
            self.continue_loop()
        self.assertEqual((len(self.impl_prompts), len(self.prompts)), (0, 1))

    def test_from_review_orphaned_first_claim_cannot_continue_or_be_replaced(self):
        self.pause_before_first_review()
        state = self.store.continue_paused()
        self.store.begin(state)  # SIGKILL after claim, before reviewer allocation.
        with self.assertRaisesRegex(TaskError, "Only a paused"):
            self.continue_loop()
        with self.assertRaisesRegex(TaskError, "unfinished handoff"):
            self.run_loop(action="new", from_review=True)
        result = loop("DEV-7", action="status")
        self.assertEqual(result.state, "running")
        self.assertEqual(result.data["active_pass"]["phase"], "review")
        self.assertEqual((self.impl_prompts, self.prompts), ([], []))

    def test_from_review_new_after_clean_allocates_fresh_reviewer(self):
        self.assertEqual(self.run_loop().state, "clean")
        implementation = self.store.read()[0]["implementation"]
        with patch("sys.stdout", new_callable=io.StringIO) as output:
            self.assertEqual(cli.main(["loop", "DEV-7", "--new", "--from-review", "--json"]), 0)
        self.assertEqual(json.loads(output.getvalue())["reviewer_context"], "DEV-7-R2")
        self.assertEqual(self.store.read()[0]["implementation"], implementation)
        self.assertEqual((len(self.impl_prompts), len(self.prompts)), (1, 2))

    def test_findings_fixes_and_focused_review_use_same_contexts(self):
        self.review_results = [findings(finding()), {}]
        result = self.run_loop()
        self.assertEqual(result.state, "clean", result.render())
        self.assertEqual((result.data["passes"], result.data["reviews"]), (4, 2))
        self.assertEqual(len(self.registry.list()), 2)
        self.assertEqual(self.recreated, [False])
        self.assertIn('"id": "F1"', self.impl_prompts[1])
        self.assertIn('"finding_id": "F1"', self.prompts[1])
        self.assertIn("focused re-review", self.prompts[1])
        self.assertEqual(result.data["resolutions"], [dict(finding_id="F1", summary="Fixed F1")])
        self.assertEqual({o.model for o in self.options}, {"review-model"})
        metrics = MetricsStore(self.registry.path).read()
        state, _ = self.store.read()
        self.assertEqual([r["run_id"] for r in metrics], [r["pass_id"] for r in state["records"]])
        self.assertEqual([r["role"] for r in metrics], ["implementation", "review", "fix", "re-review"])
        self.assertEqual([r["review_round"] for r in metrics], [None, 1, 1, 2])
        self.assertEqual([r["previous_run_id"] for r in metrics], [None] + [r["run_id"] for r in metrics[:-1]])
        self.assertEqual([r["model"] for r in metrics], ["implementation", "review-model"] * 2)
        self.assertTrue(all(r["loop_id"] == state["run_id"] and r["status"] == "completed" for r in metrics))
        self.assertTrue(all(r["implementation_context_id"] == self.implementation for r in metrics))
        self.assertTrue(all(r["finished_at"] is not None and r["wall_clock_seconds"] >= 0 for r in metrics))
        self.assertEqual((metrics[1]["review_outcome"], metrics[1]["findings_count"]), ("findings", 1))
        self.assertEqual((metrics[3]["review_outcome"], metrics[3]["findings_count"]), ("clean", 0))
        self.assertEqual(metrics[3]["loop_status"], "clean")

    def test_metrics_write_failure_does_not_change_loop_or_clean_acceptance(self):
        with patch("task_start.metrics.MetricsStore.write", side_effect=OSError("disk unavailable")):
            result = self.run_loop()
        self.assertEqual(result.state, "clean", result.render())
        from task_start.publication_state import PublicationStore
        accepted = PublicationStore(self.path).read()["acceptance"]
        self.assertEqual(accepted["pass_id"], self.store.read()[0]["records"][-1]["pass_id"])

    def test_corrupt_metrics_do_not_authorize_or_block_review(self):
        metrics = MetricsStore(self.registry.path)
        metrics.directory.mkdir(mode=0o700)
        path = metrics.directory / "runs.json"
        path.write_text('{"version":1,"runs":[{"review_outcome":"clean"}]}')
        path.chmod(0o600)
        original = path.read_bytes()
        self.raw = "malformed actual verdict"
        result = self.run_loop()
        self.assertEqual(result.state, "escalated")
        from task_start.publication_state import PublicationStore
        self.assertIsNone(PublicationStore(self.path).read()["acceptance"])
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual((len(self.impl_prompts), len(self.prompts)), (1, 1))

    def test_multiple_review_iterations_and_partial_progress(self):
        self.review_results = [findings(finding(), finding(2)), findings(finding(2)), {}]
        result = self.run_loop()
        self.assertEqual(result.state, "clean", result.render())
        self.assertEqual((result.data["passes"], result.data["reviews"]), (6, 3))
        self.assertEqual(self.recreated, [False, False])
        self.assertEqual(len(result.data["resolutions"]), 3)

    def test_pause_from_initial_implementation_collects_result_without_launching_review(self):
        observed = []
        def pause(execution, items):
            observed.append(self.pause().state)
        self.on_implementation = pause
        result = self.run_loop()
        self.assertEqual(observed, ["running"])
        self.assertEqual(result.state, "paused")
        self.assertEqual(result.data["next_phase"], "review")
        self.assertEqual((result.data["passes"], len(self.prompts)), (1, 0))
        state, sticky = self.store.read()
        self.assertTrue(sticky)
        self.assertIsNone(state["active_pass"])
        self.assertEqual(len(state["records"]), 1)
        self.on_implementation = None
        self.local = replace(self.local, reviewer=review_fixture.AgentConfig("pi", "changed-default", "low"))
        continued = self.continue_loop()
        self.assertEqual(continued.state, "clean", continued.render())
        self.assertEqual(len(self.impl_prompts), 1)
        self.assertEqual(self.options[-1].model, "review-model")

    def test_pause_from_review_and_explicit_continue_never_replay_review(self):
        self.review_results = [findings(finding()), {}]
        self.on_review = lambda _: self.pause()
        paused = self.run_loop()
        self.assertEqual(paused.state, "paused")
        self.assertEqual(paused.data["next_phase"], "fixes")
        self.assertEqual((len(self.impl_prompts), len(self.prompts)), (1, 1))
        identity = copy.deepcopy(self.store.read()[0]["reviewer"])
        self.on_review = None
        result = self.continue_loop()
        self.assertEqual(result.state, "clean", result.render())
        self.assertEqual(self.store.read()[0]["reviewer"], identity)
        self.assertEqual((len(self.impl_prompts), len(self.prompts)), (2, 2))
        self.assertEqual(self.recreated, [False])

    def test_pause_during_fixes_stays_sticky_and_continues_exact_rereview(self):
        self.review_results = [findings(finding()), {}]
        def pause(execution, items):
            if items:
                self.pause()
                # Repeated inspection/pause requests cannot consume the signal.
                self.assertTrue(loop("DEV-7", action="status").data["pause_requested"])
                self.pause()
        self.on_implementation = pause
        result = self.run_loop()
        self.assertEqual(result.state, "paused")
        self.assertEqual(result.data["next_phase"], "rereview")
        self.assertEqual((len(self.impl_prompts), len(self.prompts)), (2, 1))
        self.on_implementation = None
        result = self.continue_loop()
        self.assertEqual(result.state, "clean", result.render())
        self.assertEqual((len(self.impl_prompts), len(self.prompts)), (2, 2))

    def test_pause_at_each_handoff_claim_prevents_delivery(self):
        original = LoopStore.begin
        for boundary in ("implementation", "review", "fixes", "rereview"):
            with self.subTest(boundary=boundary):
                # A separate fixture isolates each exact boundary and checkpoint.
                case = LoopIntegrationTests()
                case.setUp()
                try:
                    case.review_results = [findings(finding()), {}]
                    def before_claim(store, state):
                        if state["next_phase"] == boundary:
                            store.pause()
                        return original(store, state)
                    with patch.object(LoopStore, "begin", before_claim):
                        result = case.run_loop()
                    self.assertEqual(result.state, "paused", result.render())
                    self.assertEqual(result.data["next_phase"], boundary)
                    expected = {"implementation": (0, 0), "review": (1, 0), "fixes": (1, 1), "rereview": (2, 1)}[boundary]
                    self.assertEqual((len(case.impl_prompts), len(case.prompts)), expected)
                finally:
                    case.doCleanups()

    def test_inspection_and_repeated_run_do_not_continue(self):
        self.on_implementation = lambda *_: self.pause()
        self.run_loop()
        with patch("task_start.loop.load_local", side_effect=AssertionError("No config during controls")):
            self.assertEqual(loop("DEV-7", action="status").state, "paused")
            self.assertEqual(self.pause().state, "paused")
        with self.assertRaisesRegex(TaskError, "checkpoint already exists"):
            self.run_loop()
        self.assertEqual((len(self.impl_prompts), len(self.prompts)), (1, 0))
        with self.assertRaisesRegex(TaskError, "preserve recorded"):
            loop("DEV-7", action="continue", model="other")

    def test_clean_termination_wins_over_pause_when_no_handoff_remains(self):
        self.on_review = lambda _: self.pause()
        result = self.run_loop()
        self.assertEqual(result.state, "clean", result.render())
        with self.assertRaisesRegex(TaskError, "Only a paused"):
            self.continue_loop()

    def test_interrupt_during_implementation_retains_uncertain_active_pass(self):
        def interrupt(*_):
            self.pause()  # Graceful request cannot reinterpret cancellation as completion.
            raise KeyboardInterrupt()
        self.on_implementation = interrupt
        result = self.run_loop()
        self.assertEqual(result.state, "interrupted")
        state, pause = self.store.read()
        self.assertTrue(pause)
        self.assertEqual(state["active_pass"]["context_id"], self.implementation)
        self.assertIsNotNone(state["active_pass"]["pass_id"])
        self.assertEqual(state["records"], [])
        self.assertEqual(self.prompts, [])
        metric, = MetricsStore(self.registry.path).read()
        self.assertEqual((metric["status"], metric["loop_status"]), ("interrupted", "interrupted"))
        self.assertEqual(metric["run_id"], state["active_pass"]["pass_id"])
        with self.assertRaisesRegex(TaskError, "Only a paused"):
            self.continue_loop()

    def test_interrupt_during_review_preserves_allocated_reviewer_without_replay(self):
        self.on_review = lambda _: (_ for _ in ()).throw(KeyboardInterrupt())
        result = self.run_loop()
        self.assertEqual(result.state, "interrupted", result.render())
        state, _ = self.store.read()
        self.assertEqual(state["active_pass"]["context_id"], "DEV-7-R1")
        self.assertEqual(state["reviewer"]["context_id"], "DEV-7-R1")
        self.assertEqual(self.registry.get("DEV-7-R1")["state"], "uncertain")
        with self.assertRaises(TaskError):
            self.continue_loop()
        self.assertEqual((len(self.impl_prompts), len(self.prompts)), (1, 1))

    def test_orphaned_running_claim_cannot_continue_or_be_silently_replaced(self):
        self.on_implementation = lambda *_: self.pause()
        self.run_loop()
        state = self.store.continue_paused()
        self.store.begin(state)  # Simulate SIGKILL after durable claim, before result persistence.
        with self.assertRaisesRegex(TaskError, "Only a paused"):
            self.continue_loop()
        with self.assertRaisesRegex(TaskError, "unfinished handoff"):
            self.run_loop(action="new")
        self.assertEqual(loop("DEV-7", action="status").state, "running")
        self.assertEqual(self.prompts, [])

    def test_paused_continue_rejects_checkout_or_requirement_drift(self):
        self.on_implementation = lambda *_: self.pause()
        self.run_loop()
        (self.path / "external.txt").write_text("External change")
        result = self.continue_loop()
        self.assertEqual(result.state, "escalated")
        self.assertIn("changed since", result.data["reason"])
        self.assertEqual(self.prompts, [])

    def test_latest_requirements_change_escalates_without_routing_stale_fixes(self):
        self.on_implementation = lambda *_: self.pause()
        self.run_loop()
        self.linear.get_issue.return_value = replace(self.linear.get_issue.return_value, description="New scope")
        result = self.continue_loop()
        self.assertEqual(result.state, "escalated")
        self.assertIn("requirements", result.data["reason"])
        self.assertEqual(self.prompts, [])

    def test_human_decision_mixed_findings_and_unclassified_output_stop(self):
        self.review_results = [findings(finding(), finding(2, category="human_decision"))]
        result = self.run_loop()
        self.assertEqual(result.state, "escalated")
        self.assertIn("human decision", result.data["reason"])
        self.assertEqual(len(self.impl_prompts), 1)

    def test_unclassified_legacy_finding_is_valid_for_manual_review_but_not_loop(self):
        legacy = {k: v for k, v in finding().items() if k not in {"id", "category"}}
        self.review_results = [findings(legacy)]
        result = self.run_loop()
        self.assertEqual(result.state, "escalated")
        self.assertIn("ambiguous", result.data["reason"])
        self.assertEqual(len(self.impl_prompts), 1)

    def test_repeated_findings_stop_even_when_prose_changes(self):
        altered = dict(finding(), explanation="Different prose for the same defect")
        self.review_results = [findings(finding()), findings(altered)]
        result = self.run_loop()
        self.assertEqual(result.state, "escalated")
        self.assertIn("repeated", result.data["reason"])
        self.assertEqual((len(self.impl_prompts), len(self.prompts)), (2, 2))

    def test_renamed_but_identical_findings_stop(self):
        self.review_results = [findings(finding()), findings(dict(finding(), id="F2"))]
        result = self.run_loop()
        self.assertEqual(result.state, "escalated")
        self.assertIn("repeated", result.data["reason"])

    def test_no_content_progress_stops_before_rereview(self):
        self.make_progress = False
        self.review_results = [findings(finding())]
        result = self.run_loop()
        self.assertEqual(result.state, "escalated")
        self.assertIn("no Git-visible progress", result.data["reason"])
        self.assertEqual(len(self.prompts), 1)

    def test_pass_limit_and_review_limit_are_deterministic(self):
        self.review_results = [findings(finding())]
        result = self.run_loop(max_passes=2)
        self.assertEqual(result.state, "escalated")
        self.assertEqual((len(self.impl_prompts), len(self.prompts)), (1, 1))
        self.assertIn("limit", result.data["reason"])
        for kwargs in (dict(max_passes=41), dict(max_reviews=0), dict(max_reviews=21), dict(timeout=float("nan"))):
            with self.subTest(kwargs=kwargs), self.assertRaises(TaskError):
                loop("DEV-7", **kwargs)

    def test_implementation_nonresumable_stops_before_review(self):
        self.on_implementation = lambda *_: self.pause()
        self.run_loop()
        self.impl_verification_error = True
        result = self.continue_loop()
        self.assertEqual(result.state, "escalated")
        self.assertIn("no longer resumable", result.data["reason"])
        self.assertEqual(self.prompts, [])

    def test_reviewer_nonresumable_never_allocates_replacement(self):
        self.review_results = [findings(finding())]
        self.on_review = lambda _: self.pause()
        self.run_loop()
        self.registry.update("DEV-7-R1", resumability="no")
        self.on_review = None
        result = self.continue_loop()
        self.assertEqual(result.state, "escalated")
        self.assertEqual(len(self.registry.list()), 2)
        self.assertEqual((len(self.impl_prompts), len(self.prompts)), (1, 1))

    def test_implementation_result_failure_or_missing_output_never_launches_review(self):
        self.impl_raw = "missing"
        result = self.run_loop()
        self.assertEqual(result.state, "escalated")
        self.assertIn("Timed out", result.data["reason"])
        self.assertEqual(self.prompts, [])
        self.assertIsNotNone(result.data["active_pass"])
        metric, = MetricsStore(self.registry.path).read()
        self.assertEqual((metric["status"], metric["loop_status"]), ("timed_out", "escalated"))

    def test_failed_checks_cannot_claim_implementation_completion(self):
        self.impl_overrides = dict(checks=[dict(name="unit", result="failed", details="Failure")])
        result = self.run_loop()
        self.assertEqual(result.state, "escalated")
        self.assertIn("ambiguous", result.data["reason"])
        self.assertEqual(self.prompts, [])
        self.assertEqual(MetricsStore(self.registry.path).read()[0]["status"], "failed")

    def test_blocked_implementation_reports_human_action_without_review(self):
        self.impl_overrides = dict(state="blocked", summary="Need a product decision")
        result = self.run_loop()
        self.assertEqual(result.state, "escalated")
        self.assertIn("product decision", result.data["reason"])
        self.assertEqual(len(result.data["implementation"]), 1)
        self.assertIsNone(result.data["active_pass"])
        metric, = MetricsStore(self.registry.path).read()
        self.assertEqual((metric["status"], metric["loop_status"]), ("blocked", "escalated"))

    def test_reviewer_failure_is_not_retried(self):
        self.raw = "malformed"
        result = self.run_loop()
        self.assertEqual(result.state, "escalated")
        self.assertEqual((len(self.impl_prompts), len(self.prompts)), (1, 1))
        self.assertEqual(result.data["final_review_state"], "not_run")
        self.assertEqual(result.data["active_pass"]["phase"], "review")

    def test_fresh_review_pane_creation_failure_preserves_readable_escalation(self):
        def fail_creation(group, operation, *args):
            if (group, operation) == ("pane", "split"):
                raise TaskError("Herdr pane creation failed")
            return self.pane_command(group, operation, *args)

        self.identity_command.side_effect = fail_creation
        with patch.object(self.registry, "allocate", wraps=self.registry.allocate) as allocate, \
                patch.object(self.adapter, "launch", wraps=self.adapter.launch) as launch:
            result = self.run_loop()
            self.assertEqual(result.state, "escalated", result.render())
            self.assertEqual(result.data["reason"], "Herdr pane creation failed")
            self.assertTrue(result.data["human_action_required"])
            self.assertIsNone(result.data["reviewer_context"])
            self.assertEqual(result.data["final_review_state"], "failed")
            self.assertEqual((result.data["passes"], result.data["reviews"]), (2, 1))

            # Reopen the durable store, independently of the controller's object.
            state, pause = LoopStore(self.path).read()
            self.assertEqual(state["status"], "escalated")
            self.assertFalse(pause)
            self.assertIsNone(state["reviewer"])
            self.assertIsNone(state["active_pass"])
            self.assertIsNone(state["next_phase"])
            self.assertEqual(len(state["records"]), 2)
            failed = state["records"][-1]
            self.assertEqual((failed["phase"], failed["state"]), ("review", "failed"))
            self.assertIsNone(failed["context_id"])
            self.assertTrue(failed["pass_id"])

            with patch("task_start.loop.load_local", side_effect=AssertionError("No config during inspection")), \
                    patch("sys.stdout", new_callable=io.StringIO) as output:
                self.assertEqual(cli.main(["loop", "DEV-7", "--status", "--json"]), 0)
            self.assertEqual(json.loads(output.getvalue()), result.as_dict())
            with self.assertRaisesRegex(TaskError, "Only a paused"):
                self.continue_loop()
            with self.assertRaisesRegex(TaskError, "checkpoint already exists"):
                self.run_loop()
            self.assertEqual(LoopStore(self.path).read()[0], state)
            allocate.assert_not_called()
            launch.assert_not_called()

        self.assertEqual(len(self.registry.list()), 1)
        self.assertEqual((len(self.impl_prompts), len(self.prompts)), (1, 0))
        splits = [call for call in self.identity_command.call_args_list if call.args[:2] == ("pane", "split")]
        self.assertEqual(len(splits), 1)

    def test_unallocated_reviewer_requires_a_terminal_setup_failure_checkpoint(self):
        with patch.object(self.identities, "split", side_effect=TaskError("Pane creation failed")):
            self.assertEqual(self.run_loop().state, "escalated")
        saved, pause = self.store.read()

        # A setup failure can also be invalidated by drift or followed by a
        # controller interruption; neither permits an automatic next handoff.
        for status in ("escalated", "interrupted"):
            for verdict in ("failed", "blocked"):
                with self.subTest(status=status, verdict=verdict):
                    candidate = copy.deepcopy(saved)
                    candidate["status"] = status
                    candidate["records"][-1]["state"] = verdict
                    self.assertEqual(LoopStore.decode((json.dumps(candidate), pause))[0], candidate)

        invalid = [dict(status="clean"), dict(status="paused", next_phase="review"),
                   dict(next_phase="rereview"), dict(reviewer=dict(context_id="DEV-7-R1"))]
        candidates = [dict(saved, **change) for change in invalid]
        for change in (dict(context_id="DEV-7-R1"), dict(state="clean"), dict(state="findings"),
                       dict(phase="rereview"), dict(findings=[finding()]),
                       dict(checks=[dict(name="unit", result="passed", details="Unallocated reviewer")]),
                       dict(resolutions=[dict(finding_id="F1", summary="Unallocated reviewer")])):
            candidate = copy.deepcopy(saved)
            candidate["records"][-1].update(change)
            candidates.append(candidate)
        for candidate in candidates:
            with self.subTest(candidate=candidate), self.assertRaisesRegex(TaskError, "Malformed loop checkpoint"):
                LoopStore.decode((json.dumps(candidate), pause))
        self.assertEqual(self.store.read()[0], saved)

    def test_failed_rereview_retains_and_requires_original_reviewer_identity(self):
        self.review_results = [findings(finding())]
        def fail_rereview(execution, items):
            if items:
                self.raw = "malformed"
        self.on_implementation = fail_rereview
        result = self.run_loop()
        self.assertEqual(result.state, "escalated", result.render())
        self.assertEqual(result.data["reviewer_context"], "DEV-7-R1")
        self.assertEqual(result.data["final_review_state"], "findings")
        state, pause = LoopStore(self.path).read()
        self.assertEqual(state["reviewer"]["context_id"], "DEV-7-R1")
        self.assertEqual(state["active_pass"]["context_id"], "DEV-7-R1")
        self.assertEqual(state["active_pass"]["phase"], "rereview")
        self.assertEqual(loop("DEV-7", action="status").as_dict(), result.as_dict())
        self.assertEqual(self.recreated, [False])
        self.assertEqual(len(self.registry.list()), 2)

        for change in (dict(reviewer=None), {}):
            candidate = copy.deepcopy(state)
            candidate.update(change)
            candidate["active_pass"]["context_id"] = None
            with self.subTest(change=change), self.assertRaisesRegex(TaskError, "Malformed loop checkpoint"):
                LoopStore.decode((json.dumps(candidate), pause))

    def test_explicit_new_after_clean_chooses_fresh_reviewer(self):
        self.assertEqual(self.run_loop().state, "clean")
        result = self.run_loop(action="new")
        self.assertEqual(result.state, "clean", result.render())
        self.assertEqual(result.data["reviewer_context"], "DEV-7-R2")
        self.assertEqual(result.data["implementation_context"], self.implementation)

    def test_concurrent_controller_is_refused_but_pause_remains_available(self):
        from task_start.publication_state import PublicationStore
        self.on_implementation = lambda *_: self.pause()
        self.run_loop()
        with PublicationStore(self.path).locked():
            with self.assertRaisesRegex(TaskError, "Another review"):
                self.continue_loop()
            self.assertEqual(self.pause().state, "paused")

    def test_pause_from_another_process_survives_controller_result_write(self):
        def pause(*_):
            result = subprocess.run([sys.executable, "-c",
                "from pathlib import Path; from task_start.loop_state import LoopStore; "
                "import sys; print(LoopStore(Path(sys.argv[1])).pause()[0]['status'])", str(self.path)],
                capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), "running")
        self.on_implementation = pause
        result = self.run_loop()
        self.assertEqual(result.state, "paused", result.render())
        self.assertTrue(self.store.read()[1])
        self.assertEqual(len(self.store.read()[0]["records"]), 1)
        self.assertEqual(self.prompts, [])

    def test_review_limit_prevents_fixes_that_cannot_be_reviewed(self):
        self.review_results = [findings(finding())]
        result = self.run_loop(max_reviews=1)
        self.assertEqual(result.state, "escalated")
        self.assertIn("limit", result.data["reason"])
        self.assertEqual((len(self.impl_prompts), len(self.prompts)), (1, 1))

    def test_corrupt_boundary_cannot_replay_completed_implementation(self):
        self.on_implementation = lambda *_: self.pause()
        self.run_loop()
        state, _ = self.store.read()
        state["next_phase"] = "implementation"
        with self.store.connection(write=True) as db:
            db.execute("UPDATE checkpoint SET payload=? WHERE id=1", (json.dumps(state),))
        with self.assertRaisesRegex(TaskError, "Malformed loop checkpoint"):
            self.continue_loop()
        self.assertEqual((len(self.impl_prompts), len(self.prompts)), (1, 0))

    def test_busy_reviewer_stops_fixes_even_after_a_validated_result(self):
        self.review_results = [findings(finding())]
        self.on_review = lambda _: self.pause()
        self.run_loop()
        self.status = "working"
        self.on_review = None
        result = self.continue_loop()
        self.assertEqual(result.state, "escalated", result.render())
        self.assertIn("not confirmed idle", result.data["reason"])
        self.assertEqual(len(self.impl_prompts), 1)

    def test_invalidated_review_retains_claim_and_never_routes_fixes(self):
        self.mutation = lambda: (self.path / "drift.txt").write_text("Reviewer drift")
        result = self.run_loop()
        self.assertEqual(result.state, "escalated", result.render())
        self.assertEqual(result.data["final_review_state"], "not_run")
        self.assertTrue(self.store.read()[0]["delivery"]["invalidated"])
        self.assertIn("invalidated", result.data["reason"])
        self.assertEqual(len(self.impl_prompts), 1)

    def test_pass_persistence_hook_observes_checkpointed_results(self):
        completed = []
        def hook(result):
            state, _ = self.store.read()
            self.assertIsNone(state["active_pass"])
            self.assertEqual(state["records"][-1]["pass_id"], result["pass_id"])
            completed.append(result)
        self.assertEqual(self.run_loop(on_pass_result=hook).state, "clean")
        self.assertEqual(len(completed), 2)

    def test_cli_json_controls_exit_codes_and_sigterm_is_interrupt(self):
        self.on_implementation = lambda *_: signal.raise_signal(signal.SIGTERM)
        with patch("sys.stdout", new_callable=io.StringIO) as output:
            self.assertEqual(cli.main(["loop", "DEV-7", "--json", "--timeout", "1"]), 130)
        self.assertEqual(json.loads(output.getvalue())["state"], "interrupted")
        with patch("task_start.loop.load_local", side_effect=AssertionError("No config")), \
                patch("sys.stdout", new_callable=io.StringIO) as output:
            self.assertEqual(cli.main(["loop", "DEV-7", "--status", "--json"]), 0)
        self.assertEqual(json.loads(output.getvalue())["state"], "interrupted")


class LoopContractTests(unittest.TestCase):
    def setUp(self):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        self.enterContext(patch("task_start.contexts.registry_path", return_value=Path(directory) / "contexts.sqlite3"))

    def test_controls_refuse_missing_or_ambiguous_task_context_paths(self):
        integration = dict(role="integration", worktree="/isolated")
        for contexts in ([integration],
                         [dict(role="implementation", worktree="/task"),
                          dict(role="review", worktree="/other-task"), integration]):
            for action in ("status", "pause", "continue"):
                with self.subTest(contexts=contexts, action=action), \
                        patch("task_start.loop.ContextRegistry") as registry, \
                        patch("task_start.loop.LoopStore", side_effect=AssertionError("No checkpoint access")):
                    registry.return_value.list.return_value = contexts
                    with self.assertRaisesRegex(TaskError, "exactly one registered task checkout"):
                        loop("DEV-7", action=action)

    def test_cli_accepts_only_role_explicit_loop_selection_flags(self):
        args = cli.parser().parse_args([
            "loop", "DEV-7", "--i-agent", "pi", "--i-model", "implementation", "--i-mode", "low",
            "--r-agent", "codex", "--r-model", "reviewer", "--r-mode", "high"])
        self.assertEqual((args.i_agent_kind, args.i_model, args.i_mode), ("pi", "implementation", "low"))
        self.assertEqual((args.r_agent_kind, args.r_model, args.r_mode), ("codex", "reviewer", "high"))
        for flag, value in (("--agent", "codex"), ("--model", "model"), ("--mode", "high"),
                            ("--impl-agent", "pi"), ("--impl-model", "model"), ("--impl-mode", "low")):
            with self.subTest(flag=flag), patch("task_start.cli.loop") as run, \
                    patch("sys.stderr", new_callable=io.StringIO), self.assertRaises(SystemExit) as error:
                cli.main(["loop", "DEV-7", flag, value])
            self.assertEqual(error.exception.code, 2)
            run.assert_not_called()

    def test_start_and_review_keep_generic_selection_flags(self):
        for command in ("start", "review"):
            with self.subTest(command=command):
                args = cli.parser().parse_args([command, "DEV-7", "--agent", "pi",
                                                "--model", "provider/model", "--mode", "high"])
                self.assertEqual((args.agent_kind, args.model, args.mode), ("pi", "provider/model", "high"))
                for prefix in ("i", "r"):
                    with patch("sys.stderr", new_callable=io.StringIO), self.assertRaises(SystemExit):
                        cli.parser().parse_args([command, "DEV-7", f"--{prefix}-model", "model"])

    def test_loop_controls_reject_both_roles_before_loading_config_or_checkpoint(self):
        with patch("task_start.loop.control_store", side_effect=AssertionError("No checkpoint access")), \
                patch("task_start.loop.load_local", side_effect=AssertionError("No config access")), \
                patch("task_start.ownership.ownership_gate", side_effect=AssertionError("No ownership acquisition")):
            for control in ("--continue", "--status", "--pause-after-current"):
                for prefix in ("i", "r"):
                    for option, value in (("agent", "codex"), ("model", "model"), ("mode", "high")):
                        flag = f"--{prefix}-{option}"
                        with self.subTest(control=control, flag=flag), \
                                patch("sys.stderr", new_callable=io.StringIO) as error:
                            self.assertEqual(cli.main(["loop", "DEV-7", control, flag, value]), 1)
                            self.assertIn("preserve recorded settings", error.getvalue())

    def test_from_review_cannot_override_existing_loop_controls(self):
        with patch("task_start.loop.control_store", side_effect=AssertionError("No checkpoint access")), \
                patch("task_start.loop.load_local", side_effect=AssertionError("No config access")), \
                patch("task_start.ownership.ownership_gate", side_effect=AssertionError("No ownership acquisition")):
            for flag in ("--continue", "--status", "--pause-after-current"):
                with self.subTest(flag=flag), patch("sys.stderr", new_callable=io.StringIO) as error:
                    self.assertEqual(cli.main(["loop", "DEV-7", flag, "--from-review"]), 1)
                    self.assertIn("--from-review only starts a new loop", error.getvalue())

    def test_invalid_actions_limits_and_initial_selection_refuse_before_ownership(self):
        invalid = [(dict(action="unknown"), "Unknown loop control action"),
                   (dict(max_reviews=0), "Loop requires"), (dict(max_passes=41), "Loop requires"),
                   (dict(timeout=float("nan")), "Loop requires"),
                   (dict(from_review=True, impl_model="model"), "--i-.* options require")]
        invalid.extend((dict(action=action, **{option: 1}), "preserve recorded settings")
                       for action in ("status", "pause", "continue")
                       for option in ("max_reviews", "max_passes", "timeout"))
        with patch("task_start.loop.control_store", side_effect=AssertionError("No checkpoint access")), \
                patch("task_start.loop.load_local", side_effect=AssertionError("No config access")), \
                patch("task_start.ownership.ownership_gate", side_effect=AssertionError("No ownership acquisition")):
            for options, message in invalid:
                with self.subTest(options=options), self.assertRaisesRegex(TaskError, message):
                    loop("DEV-7", **options)

    def test_routing_result_rejects_duplicate_ids_unknown_categories_and_missing_ids(self):
        for items in ([finding(), finding()], [dict(finding(), category="scope")],
                      [{k: v for k, v in finding().items() if k != "id"}]):
            with self.subTest(items=items), self.assertRaises(TaskError):
                parse_verdict(json.dumps(dict(review_fixture.verdict(), **findings(*items))), "pass", routing=True)

    def test_implementation_requires_exact_resolution_ids_and_nonce(self):
        value = dict(pass_id="pass", state="completed", summary="Fixed", checks=[],
                     resolutions=[dict(finding_id="F1", summary="Correction")])
        self.assertEqual(parse_implementation(json.dumps(value), "pass", [finding()]), value)
        for change in (dict(pass_id="other"), dict(resolutions=[]), dict(resolutions=value["resolutions"] * 2),
                       dict(resolutions=[dict(finding_id="F2", summary="Unrequested")])):
            with self.subTest(change=change), self.assertRaises(TaskError):
                parse_implementation(json.dumps(dict(value, **change)), "pass", [finding()])

    def test_generic_adapter_resume_uses_existing_transport_and_purpose(self):
        adapter = HerdrAgentAdapter.__new__(HerdrAgentAdapter)
        with patch.object(adapter, "resume_review", return_value="receipt") as resume:
            self.assertEqual(adapter.resume("execution", {"value": "exact"}, recreate=False), "receipt")
            resume.assert_called_once_with("execution", {"value": "exact"}, recreate=False)
