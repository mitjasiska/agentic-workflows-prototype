"""Explicit blocked-review continuation with real local state and fake providers."""

import copy
import io
import json
import unittest
from unittest.mock import patch

from task_start import TaskError, cli
from task_start.loop import LoopRuntime, loop
from task_start.loop_state import LoopStore
from task_start.publication_state import PublicationStore
from task_start.review_result import pending_review_checks
from task_start.status import render_loop_report
import test_loop as loops


BROWSER = dict(name="real browser", result="not_run", details="Sandbox browser access unavailable")
PASSED_BROWSER = dict(BROWSER, result="passed", details="Independent browser verification completed")
DEFERRED_REGRESSION = dict(name="full offline regression", result="not_run", scope="merge",
                           details="Required before merge; pending human verification, no CI gate established")
FAILED_CODE_CHECK = dict(name="focused unit tests", result="failed", scope="review",
                         details="Executed tests reproduced the implementation defect")
PASSED_CODE_CHECK = dict(FAILED_CODE_CHECK, result="passed", details="Independent re-review tests passed")


def blocked(*findings):
    return dict(state="blocked", summary="Browser prerequisite must be resolved", findings=list(findings),
                checks=[BROWSER])


class BlockedReviewTests(unittest.TestCase):
    command = loops.LoopIntegrationTests.command
    pane_command = loops.LoopIntegrationTests.pane_command
    worktrees = loops.LoopIntegrationTests.worktrees
    select_adapter = loops.LoopIntegrationTests.select_adapter
    setUp = loops.LoopIntegrationTests.setUp

    def stop_blocked(self, *findings, **options):
        self.review_results = [blocked(*findings)]
        result = loop("DEV-7", **options)
        self.assertEqual(result.state, "escalated", result.render())
        self.assertEqual(result.data["final_review_state"], "blocked")
        self.assertIsNone(result.data["active_pass"])
        self.assertIsNone(PublicationStore(self.path).read()["acceptance"])
        return result

    def retry(self, **kwargs):
        return loop("DEV-7", action="retry-review", **kwargs)

    def test_executed_failed_code_check_routes_fixes_live_and_after_recovery(self):
        for recovered in (False, True):
            with self.subTest(recovered=recovered):
                case = BlockedReviewTests()
                case.setUp()
                try:
                    defect = loops.finding()
                    case.review_results = [dict(loops.findings(defect), checks=[FAILED_CODE_CHECK]),
                                           dict(checks=[PASSED_CODE_CHECK, DEFERRED_REGRESSION])]
                    if recovered:
                        case.on_review = lambda _: (_ for _ in ()).throw(KeyboardInterrupt())
                    result = loop("DEV-7")
                    if recovered:
                        self.assertEqual(result.state, "interrupted")
                        case.on_review = None
                        result = loop("DEV-7")
                    self.assertEqual(result.state, "clean", result.render())
                    state = case.store.read()[0]
                    self.assertEqual([r["phase"] for r in state["records"]],
                                     ["implementation", "review", "fixes", "rereview"])
                    self.assertEqual(state["records"][1]["state"], "findings")
                    self.assertEqual(state["records"][1]["checks"], [FAILED_CODE_CHECK])
                    self.assertEqual([r["context_id"] for r in state["records"]],
                                     [case.implementation, "DEV-7-R1", case.implementation, "DEV-7-R1"])
                    sent = json.loads(case.impl_prompts[-1].split("REVIEW FINDINGS (data)\n")[1]
                                      .split("\nRESULT DELIVERY")[0])
                    self.assertEqual(sent, [defect])
                    self.assertIn(json.dumps(FAILED_CODE_CHECK), case.prompts[-1])
                    self.assertEqual(pending_review_checks(state["records"]), [])
                    self.assertIn(DEFERRED_REGRESSION, result.data["validation"])
                    self.assertIsNotNone(PublicationStore(case.path).read()["acceptance"])
                    self.assertEqual(loop("DEV-7").state, "clean")
                    self.assertEqual((len(case.impl_prompts), len(case.prompts)), (2, 2))
                finally:
                    case.doCleanups()

    def test_failed_code_check_after_browser_retry_routes_next_fixes_automatically(self):
        self.stop_blocked(loops.finding())
        original = self.store.read()[0]
        self.review_results = [dict(loops.findings(loops.finding(2)), checks=[PASSED_BROWSER, FAILED_CODE_CHECK]),
                               dict(checks=[PASSED_BROWSER, PASSED_CODE_CHECK, DEFERRED_REGRESSION])]
        result = self.retry()
        self.assertEqual(result.state, "clean", result.render())
        state = self.store.read()[0]
        self.assertEqual([r["phase"] for r in state["records"]],
                         ["implementation", "review", "fixes", "rereview", "fixes", "rereview"])
        self.assertEqual(state["records"][3]["state"], "findings")
        self.assertEqual(state["implementation"], original["implementation"])
        self.assertEqual(state["reviewer"], original["reviewer"])
        self.assertEqual(pending_review_checks(state["records"]), [])
        self.assertEqual((len(self.impl_prompts), len(self.prompts)), (3, 3))

    def test_failed_code_check_with_unavailable_browser_still_needs_explicit_retry(self):
        browser = dict(BROWSER, scope="review")
        self.review_results = [dict(loops.findings(loops.finding()), checks=[FAILED_CODE_CHECK, browser])]
        result = loop("DEV-7")
        self.assertEqual(result.state, "escalated", result.render())
        self.assertEqual(result.data["final_review_state"], "blocked")
        self.assertEqual((len(self.impl_prompts), len(self.prompts)), (1, 1))
        self.assertEqual(pending_review_checks(self.store.read()[0]["records"]), [FAILED_CODE_CHECK, browser])
        self.assertIsNone(PublicationStore(self.path).read()["acceptance"])
        self.review_results = [dict(checks=[PASSED_CODE_CHECK, PASSED_BROWSER])]
        self.assertEqual(self.retry().state, "clean")
        self.assertEqual((len(self.impl_prompts), len(self.prompts)), (2, 2))

    def test_clean_rereview_cannot_omit_or_fail_previously_executed_code_check(self):
        for checks in ([], [FAILED_CODE_CHECK]):
            with self.subTest(checks=checks):
                case = BlockedReviewTests()
                case.setUp()
                try:
                    case.review_results = [dict(loops.findings(loops.finding()), checks=[FAILED_CODE_CHECK]),
                                           dict(checks=checks)]
                    result = loop("DEV-7")
                    self.assertEqual(result.state, "escalated", result.render())
                    state = case.store.read()[0]
                    self.assertEqual(state["records"][1]["state"], "findings")
                    self.assertEqual(pending_review_checks(state["records"]), [FAILED_CODE_CHECK])
                    self.assertEqual(state["active_pass"]["phase"], "rereview")
                    self.assertIsNone(PublicationStore(case.path).read()["acceptance"])
                    self.assertEqual((len(case.impl_prompts), len(case.prompts)), (2, 2))
                finally:
                    case.doCleanups()

    def test_f1_delivered_once_to_original_implementer_then_independent_browser_review(self):
        defect = dict(loops.finding(), explanation="Copy report uses stale report after refresh")
        first = self.stop_blocked(defect)
        before, _ = self.store.read()
        self.assertIn("task loop DEV-7 --retry-review", first.render())
        self.assertEqual(loop("DEV-7").data, first.data)
        self.assertEqual((len(self.impl_prompts), len(self.prompts)), (1, 1))
        self.assertEqual(before["reviewer"]["session"]["value"], "session-p2")
        self.review_results = [dict(checks=[PASSED_BROWSER])]
        observed = []
        def fix_completed(value):
            observed.append(value)
            if value["state"] == "completed":
                current = self.store.read()[0]
                self.assertEqual(pending_review_checks(current["records"]), [BROWSER])
                self.assertIsNone(PublicationStore(self.path).read()["acceptance"])
        result = self.retry(on_pass_result=fix_completed)
        self.assertEqual(result.state, "clean", result.render())
        after, pause = self.store.read()
        self.assertFalse(pause)
        self.assertEqual(after["implementation"], before["implementation"])
        self.assertEqual(after["reviewer"], before["reviewer"])
        self.assertEqual(after["run_id"], before["run_id"])
        self.assertEqual(after["records"][1], dict(before["records"][1], continuation="fixes"))
        self.assertEqual([r["phase"] for r in after["records"]], ["implementation", "review", "fixes", "rereview"])
        sent = json.loads(self.impl_prompts[-1].split("REVIEW FINDINGS (data)\n")[1].split("\nRESULT DELIVERY")[0])
        self.assertEqual(sent, [defect])
        self.assertIn(json.dumps([BROWSER]), self.impl_prompts[-1])
        self.assertIn('"pending_checks": [' + json.dumps(BROWSER), self.prompts[-1])
        self.assertIn('"finding_id": "F1"', self.prompts[-1])
        self.assertEqual(self.recreated, [False])
        self.assertEqual(pending_review_checks(after["records"]), [])
        self.assertIn("real browser: not_run", render_loop_report(after, pause))
        self.assertIn("real browser: passed", result.render())
        self.assertEqual(PublicationStore(self.path).read()["acceptance"]["context_id"], "DEV-7-R1")
        with self.assertRaisesRegex(TaskError, "Only a completed blocked review"):
            self.retry()
        self.assertEqual(loop("DEV-7").data, result.data)
        self.assertEqual((len(self.impl_prompts), len(self.prompts)), (2, 2))
        self.linear.start.assert_not_called()

    def test_no_findings_retries_review_directly_and_blocker_stays_until_passed(self):
        self.stop_blocked()
        self.review_results = [blocked()]
        again = self.retry()
        self.assertEqual(again.state, "escalated", again.render())
        self.assertEqual(pending_review_checks(self.store.read()[0]["records"]), [BROWSER])
        self.assertEqual(len(self.impl_prompts), 1)
        self.review_results = [dict(checks=[PASSED_BROWSER])]
        done = self.retry()
        self.assertEqual(done.state, "clean", done.render())
        self.assertEqual((len(self.impl_prompts), len(self.prompts)), (1, 3))
        self.assertEqual(self.recreated, [False, False])
        self.assertEqual([r["phase"] for r in self.store.read()[0]["records"]],
                         ["implementation", "review", "rereview", "rereview"])

    def test_fixed_f1_is_not_resent_when_only_browser_remains_blocked(self):
        self.stop_blocked(loops.finding())
        self.review_results = [blocked()]
        self.assertEqual(self.retry().state, "escalated")
        self.review_results = [dict(checks=[PASSED_BROWSER])]
        self.assertEqual(self.retry().state, "clean")
        self.assertEqual((len(self.impl_prompts), len(self.prompts)), (2, 3))

    def test_browser_retry_keeps_deferred_merge_regression_pending_in_report_and_publication(self):
        # Historical checkpoints did not distinguish review and merge-time checks.
        legacy_regression = {k: v for k, v in DEFERRED_REGRESSION.items() if k != "scope"}
        self.review_results = [dict(blocked(loops.finding()), checks=[BROWSER, legacy_regression])]
        self.assertEqual(loop("DEV-7").state, "escalated")
        original = self.store.read()[0]
        self.review_results = [dict(checks=[PASSED_BROWSER, DEFERRED_REGRESSION],
            summary="Browser verified; full offline regression pending human verification",
            publication=dict(summary="fix report refresh", description="Refresh copied report content",
                             validation="Browser verified; full offline regression pending human verification"))]
        result = self.retry()
        self.assertEqual(result.state, "clean", result.render())
        state, pause = self.store.read()
        self.assertIsNone(state["active_pass"])
        self.assertEqual(state["records"][1], dict(original["records"][1], continuation="fixes"))
        self.assertIn(json.dumps([BROWSER, legacy_regression]), self.impl_prompts[-1])
        self.assertIn(DEFERRED_REGRESSION, result.data["validation"])
        self.assertIn(DEFERRED_REGRESSION["details"], render_loop_report(state, pause))
        self.assertEqual(pending_review_checks(state["records"]), [DEFERRED_REGRESSION])
        acceptance = PublicationStore(self.path).read()["acceptance"]
        self.assertIn("full offline regression pending", acceptance["publication"]["validation"])
        self.assertEqual((len(self.impl_prompts), len(self.prompts)), (2, 2))

    def test_findings_with_unavailable_browser_stop_until_another_explicit_retry(self):
        self.stop_blocked(loops.finding())
        new_finding = loops.finding(2)
        self.review_results = [dict(loops.findings(new_finding), checks=[BROWSER])]
        result = self.retry()
        self.assertEqual(result.state, "escalated", result.render())
        self.assertEqual(result.data["final_review_state"], "blocked")
        state, _ = self.store.read()
        self.assertEqual(state["records"][-1]["findings"], [new_finding])
        self.assertEqual(state["records"][-1]["checks"], [BROWSER])
        self.assertIsNone(state["active_pass"])
        self.assertIsNone(state["delivery"])
        self.assertIn("--retry-review", result.render())
        self.assertIsNone(PublicationStore(self.path).read()["acceptance"])
        self.assertEqual(loop("DEV-7").data, result.data)
        self.assertEqual((len(self.impl_prompts), len(self.prompts)), (2, 2))
        self.review_results = [dict(checks=[PASSED_BROWSER])]
        self.assertEqual(self.retry().state, "clean")
        sent = json.loads(self.impl_prompts[-1].split("REVIEW FINDINGS (data)\n")[1].split("\nRESULT DELIVERY")[0])
        self.assertEqual(sent, [new_finding])
        loop("DEV-7")
        self.assertEqual((len(self.impl_prompts), len(self.prompts)), (3, 3))

    def test_recovered_findings_keep_blocked_browser_without_replay(self):
        self.stop_blocked(loops.finding())
        self.review_results = [dict(loops.findings(loops.finding(2)), checks=[BROWSER, DEFERRED_REGRESSION])]
        def interrupt(_):
            raise KeyboardInterrupt()
        self.on_review = interrupt
        self.assertEqual(self.retry().state, "interrupted")
        self.on_review = None
        recovered = loop("DEV-7")
        self.assertEqual(recovered.state, "escalated", recovered.render())
        self.assertEqual(recovered.data["final_review_state"], "blocked")
        state, _ = self.store.read()
        self.assertIsNone(state["active_pass"])
        self.assertIsNone(state["delivery"])
        self.assertEqual(state["records"][-1]["findings"], [loops.finding(2)])
        self.assertEqual(loop("DEV-7").data, recovered.data)
        self.assertEqual((len(self.impl_prompts), len(self.prompts)), (2, 2))
        # The next independently evidenced review can leave only merge-time work.
        self.review_results = [dict(checks=[PASSED_BROWSER, DEFERRED_REGRESSION])]
        self.on_review = interrupt
        self.assertEqual(self.retry().state, "interrupted")
        self.on_review = None
        done = loop("DEV-7")
        self.assertEqual(done.state, "clean", done.render())
        self.assertEqual((len(self.impl_prompts), len(self.prompts)), (3, 3))
        self.assertIn(DEFERRED_REGRESSION, done.data["validation"])

    def test_new_required_check_holds_live_and_recovered_acceptance_and_fixes(self):
        required = dict(name="integration service", result="not_run", scope="review",
                        details="Required service unavailable")
        for verdict in ("clean", "findings"):
            for recovered in (False, True):
                with self.subTest(verdict=verdict, recovered=recovered):
                    case = BlockedReviewTests()
                    case.setUp()
                    try:
                        case.stop_blocked(loops.finding())
                        original = case.store.read()[0]
                        checks = [PASSED_BROWSER, required, DEFERRED_REGRESSION]
                        findings = [loops.finding(2)] if verdict == "findings" else []
                        case.review_results = [dict(state=verdict, findings=findings, checks=checks)]
                        if recovered:
                            def interrupt(_):
                                raise KeyboardInterrupt()
                            case.on_review = interrupt
                        result = case.retry()
                        if recovered:
                            self.assertEqual(result.state, "interrupted")
                            self.assertEqual(case.store.read()[0]["active_pass"]["phase"], "rereview")
                            case.on_review = None
                            result = loop("DEV-7")
                        self.assertEqual(result.state, "escalated", result.render())
                        self.assertIsNone(PublicationStore(case.path).read()["acceptance"])
                        state, _ = case.store.read()
                        self.assertEqual(state["implementation"], original["implementation"])
                        self.assertEqual(state["reviewer"], original["reviewer"])
                        if verdict == "clean":
                            self.assertIn("lacks passing evidence", result.data["reason"])
                            self.assertIsNotNone(state["active_pass"])
                            self.assertIsNotNone(state["delivery"])
                            self.assertEqual(loop("DEV-7").state, "escalated")
                            self.assertEqual(case.store.read()[0]["active_pass"], state["active_pass"])
                            with self.assertRaisesRegex(TaskError, "Only a completed blocked review"):
                                case.retry()
                        else:
                            self.assertEqual(result.data["final_review_state"], "blocked")
                            self.assertIn(required["name"], result.data["reason"])
                            self.assertEqual(state["records"][-1]["checks"], checks)
                            self.assertEqual(state["records"][-1]["findings"], findings)
                            self.assertIn(required, pending_review_checks(state["records"]))
                            self.assertIsNone(state["active_pass"])
                            self.assertIsNone(state["delivery"])
                            self.assertIn("--retry-review", result.render())
                            self.assertEqual(loop("DEV-7").data, result.data)
                        self.assertIsNone(PublicationStore(case.path).read()["acceptance"])
                        self.assertEqual((len(case.impl_prompts), len(case.prompts)), (2, 2))
                        if verdict == "findings":
                            case.review_results = [dict(checks=[PASSED_BROWSER, dict(required, result="passed",
                                details="Independent service validation passed"), DEFERRED_REGRESSION])]
                            done = case.retry()
                            self.assertEqual(done.state, "clean", done.render())
                            self.assertEqual((len(case.impl_prompts), len(case.prompts)), (3, 3))
                            self.assertEqual(pending_review_checks(case.store.read()[0]["records"]),
                                             [DEFERRED_REGRESSION])
                    finally:
                        case.doCleanups()

    def test_human_decision_mixed_or_alone_refuses_without_any_checkpoint_change(self):
        for findings in ([loops.finding(category="human_decision")],
                         [loops.finding(), loops.finding(2, category="human_decision")]):
            with self.subTest(findings=findings):
                case = BlockedReviewTests()
                case.setUp()
                try:
                    result = case.stop_blocked(*findings)
                    before = case.store.read()
                    self.assertNotIn("--retry-review", result.render())
                    with self.assertRaisesRegex(TaskError, "human decision"):
                        case.retry()
                    self.assertEqual(case.store.read(), before)
                    self.assertEqual((len(case.impl_prompts), len(case.prompts)), (1, 1))
                finally:
                    case.doCleanups()

    def test_new_cannot_discard_blocked_review(self):
        self.stop_blocked(loops.finding())
        before = self.store.read()
        with self.assertRaisesRegex(TaskError, "cannot be replaced.*--retry-review"):
            loop("DEV-7", action="new")
        with self.assertRaisesRegex(TaskError, "cannot be replaced"):
            self.store.create(before[0], replace=True)
        self.assertEqual(self.store.read(), before)
        self.assertEqual((len(self.impl_prompts), len(self.prompts)), (1, 1))

    def test_failed_malformed_and_active_review_are_not_retry_boundaries(self):
        for outcome in ("failed", "malformed", "active"):
            with self.subTest(outcome=outcome):
                case = BlockedReviewTests()
                case.setUp()
                try:
                    case.review_results = [dict(state="failed", summary="Failure")]
                    if outcome == "malformed":
                        case.raw = "{}"
                    elif outcome == "active":
                        case.on_review = lambda _: (_ for _ in ()).throw(KeyboardInterrupt())
                    loop("DEV-7")
                    before = case.store.read()
                    with self.assertRaisesRegex(TaskError, "Only a completed blocked review"):
                        case.retry()
                    self.assertEqual(case.store.read(), before)
                    self.assertEqual((len(case.impl_prompts), len(case.prompts)), (1, 1))
                finally:
                    case.doCleanups()

    def test_preflight_identity_drift_and_active_runtime_refuse_without_mutation(self):
        self.stop_blocked(loops.finding())
        before = self.store.read()
        for failure in ("implementation", "reviewer", "busy_i", "busy_r", "checkout", "requirements"):
            with self.subTest(failure=failure):
                original_issue = self.linear.get_issue.return_value
                self.impl_verification_error = failure == "implementation"
                self.impl_status = "working" if failure == "busy_i" else "done"
                self.status = "working" if failure == "busy_r" else "done"
                if failure == "checkout":
                    (self.path / "drift").write_text("external edit")
                if failure == "requirements":
                    self.linear.get_issue.return_value = loops.replace(original_issue, description="changed")
                try:
                    with patch.object(self.adapter, "verify_review_session",
                                      side_effect=TaskError("Reviewer session unavailable") if failure == "reviewer" else None,
                                      wraps=None if failure == "reviewer" else self.adapter.verify_review_session):
                        with self.assertRaises(TaskError):
                            self.retry()
                    self.assertEqual(self.store.read(), before)
                finally:
                    (self.path / "drift").unlink(missing_ok=True)
                    self.linear.get_issue.return_value = original_issue
        self.assertEqual((len(self.impl_prompts), len(self.prompts)), (1, 1))

    def test_saved_limits_refuse_before_fixes_that_cannot_be_reviewed(self):
        for options in (dict(max_reviews=1), dict(max_passes=3)):
            with self.subTest(options=options):
                case = BlockedReviewTests()
                case.setUp()
                try:
                    case.stop_blocked(loops.finding(), **options)
                    before = case.store.read()
                    with self.assertRaisesRegex(TaskError, "limits"):
                        case.retry()
                    self.assertEqual(case.store.read(), before)
                finally:
                    case.doCleanups()

    def test_interruption_after_authorization_before_claim_resumes_without_reauthorizing(self):
        self.stop_blocked(loops.finding())
        self.review_results = [dict(checks=[PASSED_BROWSER])]
        with patch.object(LoopRuntime, "execute", side_effect=KeyboardInterrupt):
            self.assertEqual(self.retry().state, "interrupted")
        saved, _ = self.store.read()
        self.assertEqual(saved["next_phase"], "fixes")
        self.assertIsNone(saved["active_pass"])
        with self.assertRaisesRegex(TaskError, "already continued"):
            self.retry()
        done = loop("DEV-7")
        self.assertEqual(done.state, "clean", done.render())
        self.assertEqual((len(self.impl_prompts), len(self.prompts)), (2, 2))

    def test_interrupted_fix_and_rereview_claims_recover_without_prompt_replay(self):
        for phase in ("fixes", "rereview"):
            with self.subTest(phase=phase):
                case = BlockedReviewTests()
                case.setUp()
                try:
                    case.stop_blocked(loops.finding())
                    case.review_results = [dict(checks=[PASSED_BROWSER])]
                    def stop(*_):
                        raise KeyboardInterrupt()
                    if phase == "fixes":
                        case.on_implementation = stop
                    else:
                        case.on_review = stop
                    self.assertEqual(case.retry().state, "interrupted")
                    before = case.store.read()
                    self.assertEqual(before[0]["active_pass"]["phase"], phase)
                    with self.assertRaisesRegex(TaskError, "Only a completed blocked review"):
                        case.retry()
                    self.assertEqual(case.store.read(), before)
                    case.on_implementation = case.on_review = None
                    result = loop("DEV-7")
                    self.assertEqual(result.state, "clean", result.render())
                    loop("DEV-7")
                    self.assertEqual((len(case.impl_prompts), len(case.prompts)), (2, 2))
                finally:
                    case.doCleanups()

    def test_blocked_result_collection_interruption_requires_disposal_before_retry(self):
        self.review_results = [blocked(loops.finding())]
        def stop(result):
            if result["state"] == "blocked":
                raise KeyboardInterrupt()
        self.assertEqual(loop("DEV-7", on_pass_result=stop).state, "interrupted")
        before = self.store.read()
        self.assertIsNone(before[0]["active_pass"])
        self.assertIsNotNone(before[0]["delivery"])
        with self.assertRaisesRegex(TaskError, "Retained delivery"):
            self.retry()
        self.assertEqual(self.store.read(), before)
        loop("DEV-7")
        self.assertIsNone(self.store.read()[0]["delivery"])
        self.review_results = [dict(checks=[PASSED_BROWSER])]
        self.assertEqual(self.retry().state, "clean")
        self.assertEqual((len(self.impl_prompts), len(self.prompts)), (2, 2))

    def test_pause_and_competing_controller_never_start_a_retry_pass(self):
        self.stop_blocked(loops.finding())
        before = self.store.read()
        with PublicationStore(self.path).locked():
            with self.assertRaisesRegex(TaskError, "Another controller"):
                self.retry()
        self.assertEqual(self.store.read(), before)
        original = LoopStore.retry_review
        def racing_pause(store, expected, reviewer):
            paused = loop("DEV-7", action="pause")
            self.assertEqual(paused.state, "escalated")
            self.assertTrue(paused.data["pause_requested"])
            self.assertEqual(store.read()[0], expected)
            self.assertEqual(loop("DEV-7", action="pause").data, paused.data)
            self.assertEqual((len(self.impl_prompts), len(self.prompts)), (1, 1))
            return original(store, expected, reviewer)
        with patch.object(LoopStore, "retry_review", racing_pause):
            result = self.retry()
        self.assertEqual(result.state, "paused", result.render())
        self.assertTrue(result.data["pause_requested"])
        self.assertIsNone(self.store.read()[0]["active_pass"])
        self.assertEqual((len(self.impl_prompts), len(self.prompts)), (1, 1))
        self.review_results = [dict(checks=[PASSED_BROWSER])]
        self.assertEqual(loop("DEV-7", action="continue").state, "clean")
        self.assertFalse(self.store.read()[1])
        self.assertEqual((len(self.impl_prompts), len(self.prompts)), (2, 2))

    def test_public_pause_survives_failed_retry_preflight_without_authorizing_review(self):
        self.stop_blocked()
        before = self.store.read()[0]
        def unavailable(*_):
            paused = loop("DEV-7", action="pause")
            self.assertEqual(paused.state, "escalated")
            self.assertTrue(paused.data["pause_requested"])
            raise TaskError("Reviewer session unavailable")
        with patch.object(self.adapter, "verify_review_session", side_effect=unavailable):
            with self.assertRaisesRegex(TaskError, "Reviewer session unavailable"):
                self.retry()
        self.assertEqual(self.store.read(), (before, True))
        self.assertEqual(loop("DEV-7").state, "escalated")
        with self.assertRaisesRegex(TaskError, "Only a paused boundary"):
            loop("DEV-7", action="continue")
        self.assertEqual(self.store.read(), (before, True))
        self.assertEqual((len(self.impl_prompts), len(self.prompts)), (1, 1))
        authorized = self.retry()
        self.assertEqual(authorized.state, "paused", authorized.render())
        self.assertEqual(authorized.data["next_phase"], "rereview")
        self.assertIsNone(authorized.data["active_pass"])
        self.assertTrue(loop("DEV-7", action="status").data["pause_requested"])
        with self.assertRaisesRegex(TaskError, "Only a completed blocked review"):
            self.retry()
        self.assertEqual((len(self.impl_prompts), len(self.prompts)), (1, 1))
        self.review_results = [dict(checks=[PASSED_BROWSER])]
        self.assertEqual(loop("DEV-7", action="continue").state, "clean")
        self.assertEqual((len(self.impl_prompts), len(self.prompts)), (1, 2))

    def test_public_pause_leaves_ineligible_blocked_boundaries_unchanged(self):
        for findings, options in (([loops.finding(category="human_decision")], {}),
                                  ([loops.finding()], dict(max_reviews=1))):
            with self.subTest(findings=findings, options=options):
                case = BlockedReviewTests()
                case.setUp()
                try:
                    case.stop_blocked(*findings, **options)
                    before = case.store.read()
                    paused = loop("DEV-7", action="pause")
                    self.assertEqual(paused.state, "escalated")
                    self.assertFalse(paused.data["pause_requested"])
                    self.assertEqual(case.store.read(), before)
                    with self.assertRaises(TaskError):
                        loop("DEV-7", action="continue")
                    self.assertEqual((len(case.impl_prompts), len(case.prompts)), (1, 1))
                finally:
                    case.doCleanups()

    def test_unproven_legacy_reviewer_and_corrupt_routing_evidence_refuse(self):
        self.stop_blocked(loops.finding())
        before, pause = self.store.read()
        legacy = copy.deepcopy(before)
        legacy["reviewer"] = dict(context_id="DEV-7-R1")
        self.store.save(legacy)
        with patch.object(self.adapter, "verify_review_session", side_effect=TaskError("Session unavailable")):
            with self.assertRaisesRegex(TaskError, "Session unavailable"):
                self.retry()
        self.assertEqual(self.store.read()[0], legacy)
        self.store.save(before)
        for changes in (dict(continuation="review"), dict(continuation="fixes", findings=[]),
                        dict(continuation="fixes", findings=[loops.finding(category="human_decision")])):
            invalid = copy.deepcopy(before)
            invalid["records"][-1].update(changes)
            with self.subTest(changes=changes), self.assertRaises(TaskError):
                LoopStore.decode((json.dumps(invalid), pause))

    def test_missing_or_unrun_browser_check_never_becomes_clean_even_on_restart(self):
        for checks in ([], [BROWSER], [dict(BROWSER, name="other", result="passed")], [PASSED_BROWSER, BROWSER]):
            with self.subTest(checks=checks):
                case = BlockedReviewTests()
                case.setUp()
                try:
                    case.stop_blocked()
                    case.review_results = [dict(checks=checks)]
                    result = case.retry()
                    self.assertEqual(result.state, "escalated", result.render())
                    self.assertIn("lacks passing evidence", result.data["reason"])
                    self.assertIsNotNone(result.data["active_pass"])
                    self.assertIsNone(PublicationStore(case.path).read()["acceptance"])
                    self.assertEqual(loop("DEV-7").state, "escalated")
                    self.assertIsNone(PublicationStore(case.path).read()["acceptance"])
                    self.assertEqual((len(case.impl_prompts), len(case.prompts)), (1, 2))
                finally:
                    case.doCleanups()

    def test_pending_browser_survives_an_intervening_findings_review_and_implementation_claims(self):
        self.stop_blocked(loops.finding())
        self.impl_overrides = dict(checks=[PASSED_BROWSER])
        self.review_results = [loops.findings(loops.finding(2)), dict(checks=[])]
        result = self.retry()
        self.assertEqual(result.state, "escalated", result.render())
        self.assertIn("Required review validation remains outstanding", result.data["reason"])
        self.assertEqual(result.data["final_review_state"], "blocked")
        self.assertIsNone(result.data["active_pass"])
        self.assertEqual((len(self.impl_prompts), len(self.prompts)), (2, 2))
        self.assertEqual(pending_review_checks(self.store.read()[0]["records"]), [BROWSER])
        self.assertIsNone(PublicationStore(self.path).read()["acceptance"])

    def test_repeat_findings_with_renamed_ids_still_stop(self):
        original = loops.finding()
        self.stop_blocked(original)
        self.review_results = [dict(loops.findings(dict(original, id="F2")), checks=[PASSED_BROWSER])]
        result = self.retry()
        self.assertEqual(result.state, "escalated", result.render())
        self.assertIn("repeated", result.data["reason"])
        self.assertEqual((len(self.impl_prompts), len(self.prompts)), (2, 2))

    def test_cli_retry_control_and_saved_options(self):
        self.stop_blocked()
        self.review_results = [dict(checks=[PASSED_BROWSER])]
        with patch("sys.stdout", new_callable=io.StringIO) as output:
            self.assertEqual(cli.main(["loop", "DEV-7", "--retry-review", "--json"]), 0)
        self.assertEqual(json.loads(output.getvalue())["state"], "clean")
        for option in ("--r-model", "--i-model", "--max-reviews"):
            with self.subTest(option=option), patch("sys.stderr", new_callable=io.StringIO) as error:
                self.assertEqual(cli.main(["loop", "DEV-7", "--retry-review", option, "2"]), 1)
                self.assertIn("preserve recorded settings", error.getvalue())
        with patch("sys.stderr", new_callable=io.StringIO), self.assertRaises(SystemExit):
            cli.parser().parse_args(["loop", "DEV-7", "--retry-review", "--new"])

    def test_original_blocked_review_is_recovered_before_explicit_continuation(self):
        self.review_results = [blocked(loops.finding())]
        def stop(_):
            raise KeyboardInterrupt()
        self.on_review = stop
        self.assertEqual(loop("DEV-7").state, "interrupted")
        self.on_review = None
        recovered = loop("DEV-7")
        self.assertEqual(recovered.state, "escalated", recovered.render())
        self.assertEqual((len(self.impl_prompts), len(self.prompts)), (1, 1))
        self.review_results = [dict(checks=[PASSED_BROWSER])]
        self.assertEqual(self.retry().state, "clean")
        self.assertEqual((len(self.impl_prompts), len(self.prompts)), (2, 2))

    def test_adopted_blocked_review_reuses_saved_provenance(self):
        from task_start.review import review
        from task_start.review_handoff_state import ReviewHandoffStore
        self.review_results = [blocked(loops.finding())]
        reviewed = review("DEV-7")
        self.assertEqual(reviewed.state, "blocked")
        first = loop("DEV-7")
        self.assertEqual(first.state, "escalated", first.render())
        self.assertEqual(len(self.impl_prompts), 0)
        self.review_results = [dict(checks=[PASSED_BROWSER])]
        done = self.retry()
        self.assertEqual(done.state, "clean", done.render())
        self.assertEqual((len(self.impl_prompts), len(self.prompts)), (1, 2))
        self.assertEqual(ReviewHandoffStore(self.path).read()["consumed"], self.store.read()[0]["run_id"])

    def test_missing_reviewer_pane_reuses_verified_conversation(self):
        self.stop_blocked()
        saved = self.store.read()[0]["reviewer"]
        self.panes = [p for p in self.panes if p["pane_id"] != "p2"]
        self.review_results = [dict(checks=[PASSED_BROWSER])]
        done = self.retry()
        self.assertEqual(done.state, "clean", done.render())
        self.assertEqual(self.store.read()[0]["reviewer"], saved)
        self.assertEqual(self.recreated, [True])

    def test_conflicting_check_evidence_cannot_clear_pending_validation(self):
        records = [dict(phase="review", **blocked())]
        for checks in ([BROWSER, PASSED_BROWSER], [PASSED_BROWSER, BROWSER]):
            candidate = records + [dict(phase="rereview", **dict(blocked(), checks=checks))]
            self.assertEqual(pending_review_checks(candidate), [BROWSER])

    def test_legacy_completed_blocked_checkpoint_restores_exact_registry_reviewer(self):
        self.stop_blocked(loops.finding())
        before, _ = self.store.read()
        legacy = copy.deepcopy(before)
        legacy["reviewer"] = dict(context_id=before["reviewer"]["context_id"])
        self.store.save(legacy)
        self.review_results = [dict(checks=[PASSED_BROWSER])]
        result = self.retry()
        self.assertEqual(result.state, "clean", result.render())
        after = self.store.read()[0]
        self.assertEqual(after["reviewer"], before["reviewer"])
        self.assertEqual(after["implementation"], before["implementation"])
        self.assertEqual(after["run_id"], before["run_id"])
        self.assertEqual((len(self.impl_prompts), len(self.prompts)), (2, 2))

    def test_legacy_missing_delivery_provenance_or_uncertain_registry_refuses(self):
        self.stop_blocked()
        before = self.store.read()[0]
        legacy = copy.deepcopy(before)
        legacy["reviewer"] = dict(context_id="DEV-7-R1")
        del legacy["delivery"]
        self.store.save(legacy)
        with self.assertRaisesRegex(TaskError, "completed delivery/context evidence"):
            self.retry()
        self.assertEqual(self.store.read()[0], legacy)
        legacy["delivery"] = None
        self.store.save(legacy)
        self.registry.update("DEV-7-R1", state="reviewing")
        with self.assertRaisesRegex(TaskError, "busy|active|stale"):
            self.retry()
        self.assertEqual(self.store.read()[0], legacy)

    def test_unfinished_attempt_cannot_become_a_completed_blocked_retry_boundary(self):
        from task_start.review_result import ReviewResult
        from uuid import uuid4
        self.stop_blocked()
        before = self.store.read()[0]
        def incomplete(*args, **kwargs):
            kwargs['before_handoff']()
            return ReviewResult('blocked', 'Checkout invalidated before delivery', 'DEV-7-R1', 'resumed',
                                {}, before['snapshot'], str(uuid4()), invalidated=True)
        with patch('task_start.loop.review_pass', side_effect=incomplete):
            result = self.retry()
        self.assertEqual(result.state, 'escalated', result.render())
        self.assertIn('lacks verified structured completion', result.data['reason'])
        after = self.store.read()
        self.assertIsNotNone(after[0]['active_pass'])
        self.assertEqual(len(after[0]['records']), len(before['records']))
        with self.assertRaisesRegex(TaskError, 'Only a completed blocked review'):
            self.retry()
        self.assertEqual(self.store.read(), after)
        self.assertEqual((len(self.impl_prompts), len(self.prompts)), (1, 1))

    def test_failed_fix_cannot_discard_original_blocked_review_with_new(self):
        self.stop_blocked(loops.finding())
        self.impl_overrides = dict(state='failed', summary='Fix validation could not finish')
        self.assertEqual(self.retry().state, 'escalated')
        before = self.store.read()
        self.assertEqual(before[0]['records'][-1]['phase'], 'fixes')
        self.assertEqual(before[0]['findings'], [loops.finding()])
        with self.assertRaisesRegex(TaskError, 'cannot be replaced'):
            loop('DEV-7', action='new')
        self.assertEqual(self.store.read(), before)
        self.assertEqual((len(self.impl_prompts), len(self.prompts)), (2, 1))

    def test_clean_continuation_permits_normal_new_loop(self):
        self.stop_blocked()
        self.review_results = [dict(checks=[PASSED_BROWSER])]
        self.assertEqual(self.retry().state, 'clean')
        old_id = self.store.read()[0]['run_id']
        result = loop('DEV-7', action='new')
        self.assertEqual(result.state, 'clean', result.render())
        self.assertNotEqual(self.store.read()[0]['run_id'], old_id)
        self.assertEqual(result.data['reviewer_context'], 'DEV-7-R2')
