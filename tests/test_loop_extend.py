"""Budget-only continuation against disposable Git, checkpoints and fake providers."""

import copy
from dataclasses import replace
import io
import json
import unittest
from unittest.mock import patch

from task_start import TaskError, cli
from task_start.config import Project
from task_start.loop import LoopRuntime, loop
from task_start.loop_state import BUDGET_STOP_REASON, LoopStore, extension_guidance
from task_start.publication_state import PublicationStore
from task_start.review_result import finding_signature
from task_start.status import guidance, task_status
import test_loop as loops
import test_loop_bootstrap as bootstrap
from test_loop_blocked import blocked, PASSED_BROWSER


class ExtendTests(unittest.TestCase):
    command = loops.LoopIntegrationTests.command
    pane_command = loops.LoopIntegrationTests.pane_command
    worktrees = loops.LoopIntegrationTests.worktrees
    select_adapter = loops.LoopIntegrationTests.select_adapter
    setUp = loops.LoopIntegrationTests.setUp

    def exhausted(self, **options):
        self.review_results = [loops.findings(loops.finding(n)) for n in (1, 2, 4)]
        result = loop("DEV-7", **options)
        self.assertEqual(result.state, "escalated", result.render())
        self.assertEqual(result.data["reason"], BUDGET_STOP_REASON)
        return self.store.read()[0]

    def extend(self, **options):
        return loop("DEV-7", action="extend", **options)

    def assert_unchanged_refusal(self, **options):
        before = self.store.read()
        counts = (len(self.impl_prompts), len(self.prompts), len(self.registry.list()))
        with self.assertRaises(TaskError):
            self.extend(**options)
        self.assertEqual(self.store.read(), before)
        self.assertEqual((len(self.impl_prompts), len(self.prompts), len(self.registry.list())), counts)

    def test_six_pass_f4_goes_once_to_i1_then_same_r1_with_history_intact(self):
        before = self.exhausted()
        self.assertEqual((before["pass_count"], before["review_count"], before["next_phase"]), (6, 3, "fixes"))
        self.assertEqual(before["findings"], [loops.finding(4)])
        with patch("task_start.loop.environment", side_effect=AssertionError("Refuse before environment preparation")):
            with self.assertRaisesRegex(TaskError, "skip.*correction.*--extend"):
                loop("DEV-7", action="new", from_review=True)
        self.assertEqual(self.store.read()[0], before)
        self.assertEqual(len(self.registry.list()), 2)
        def after_fix(execution, findings):
            self.assertEqual(findings, before["findings"])
            self.assertEqual(len(self.prompts), 3)  # No fresh review before F4 correction.
        self.on_implementation = after_fix
        self.review_results = [{}]
        done = self.extend(max_passes=10, max_reviews=5)
        self.assertEqual(done.state, "clean", done.render())
        after = self.store.read()[0]
        for key in ("run_id", "implementation", "reviewer", "reviewer_options", "requirements", "binding", "timeout"):
            self.assertEqual(after[key], before[key], key)
        self.assertEqual(after["records"][:6], before["records"])
        self.assertEqual((after["pass_count"], after["review_count"]), (8, 4))
        self.assertEqual((after["max_passes"], after["max_reviews"]), (10, 5))
        self.assertEqual([r["context_id"] for r in after["records"][-2:]], ["DEV-7-I1", "DEV-7-R1"])
        self.assertEqual(after["records"][-2]["resolutions"], [dict(finding_id="F4", summary="Fixed F4")])
        self.assertEqual(len(self.registry.list()), 2)
        self.assertEqual(self.recreated, [False, False, False])
        self.assertIn('"id": "F4"', self.prompts[-1])
        self.assertIn('"finding_id": "F4"', self.prompts[-1])
        self.assertEqual(loop("DEV-7").state, "clean")
        self.assert_unchanged_refusal(max_passes=12, max_reviews=6)
        self.assertEqual((len(self.impl_prompts), len(self.prompts)), (4, 4))
        self.linear.start.assert_not_called()

    def test_pending_review_and_rereview_use_recorded_boundary(self):
        for limit, phase, expected in ((1, "review", (1, 1)), (3, "rereview", (2, 2))):
            with self.subTest(phase=phase):
                case = ExtendTests()
                case.setUp()
                try:
                    before = case.exhausted(max_passes=limit)
                    self.assertEqual(before["next_phase"], phase)
                    case.review_results = [{}]
                    result = case.extend(max_passes=limit + 1)
                    self.assertEqual(result.state, "clean", result.render())
                    after = case.store.read()[0]
                    self.assertEqual(after["records"][:-1], before["records"])
                    self.assertEqual(after["records"][-1]["phase"], phase)
                    self.assertEqual(after["run_id"], before["run_id"])
                    self.assertEqual(after["reviewer"]["context_id"], "DEV-7-R1")
                    self.assertEqual((len(case.impl_prompts), len(case.prompts)), expected)
                finally:
                    case.doCleanups()

    def test_review_limit_alone_can_increase_and_repeated_exhaustion_keeps_run(self):
        before = self.exhausted(max_passes=10, max_reviews=1)
        self.review_results = [loops.findings(loops.finding(2))]
        again = self.extend(max_reviews=2)
        self.assertEqual(again.state, "escalated", again.render())
        middle = self.store.read()[0]
        self.assertEqual((middle["pass_count"], middle["review_count"]), (4, 2))
        self.assertEqual(middle["run_id"], before["run_id"])
        self.review_results = [{}]
        self.assertEqual(self.extend(max_reviews=3).state, "clean")
        after = self.store.read()[0]
        self.assertEqual(after["records"][:4], middle["records"])
        self.assertEqual(after["reviewer"], before["reviewer"])
        self.assertEqual((len(self.impl_prompts), len(self.prompts), len(self.registry.list())), (3, 3, 2))

    def test_review_first_origin_extends_fixes_without_an_initial_completion_pass(self):
        self.review_results = [loops.findings(loops.finding(4))]
        self.assertEqual(loop("DEV-7", from_review=True, max_passes=1, max_reviews=1).state, "escalated")
        before = self.store.read()[0]
        self.review_results = [{}]
        result = self.extend(max_passes=3, max_reviews=2)
        self.assertEqual(result.state, "clean", result.render())
        after = self.store.read()[0]
        self.assertEqual((after["version"], after["initial_phase"]), (2, "review"))
        self.assertEqual([r["phase"] for r in after["records"]], ["review", "fixes", "rereview"])
        self.assertEqual(after["reviewer"], before["reviewer"])
        self.assertEqual(after["run_id"], before["run_id"])
        self.assertEqual((len(self.impl_prompts), len(self.prompts)), (1, 2))

    def test_fresh_implementation_origin_extends_only_pending_review(self):
        case = bootstrap.BootstrapLoopTests()
        case.setUp()
        try:
            self.assertEqual(case.run_loop(max_passes=1).state, "escalated")
            before = case.store.read()[0]
            result = loop("DEV-7", action="extend", max_passes=2)
            self.assertEqual(result.state, "clean", result.render())
            after = case.store.read()[0]
            self.assertEqual(after["version"], 3)
            self.assertEqual(after["implementation"], before["implementation"])
            self.assertEqual(after["run_id"], before["run_id"])
            self.assertEqual((len(case.launches), len(case.impl_prompts), len(case.prompts)), (1, 1, 1))
            self.assertEqual(case.preparation, ["base", "create", "linear"])
        finally:
            case.doCleanups()

    def test_bounds_must_increase_and_cover_fix_and_review(self):
        self.exhausted()
        for options in ({}, dict(max_passes=6, max_reviews=3), dict(max_passes=5, max_reviews=5),
                        dict(max_passes=10, max_reviews=2), dict(max_passes=7, max_reviews=5),
                        dict(max_passes=10), dict(max_reviews=5), dict(max_passes=41),
                        dict(max_reviews=21), dict(max_passes=True), dict(max_reviews=4.5)):
            with self.subTest(options=options):
                self.assert_unchanged_refusal(**options)

    def test_interruptions_before_claim_after_delivery_and_after_collection_never_replay(self):
        for boundary in ("authorization", "fixes", "rereview", "collected"):
            with self.subTest(boundary=boundary):
                case = ExtendTests()
                case.setUp()
                try:
                    before = case.exhausted()
                    case.review_results = [{}]
                    def stop(*args):
                        raise KeyboardInterrupt()
                    if boundary == "fixes":
                        case.on_implementation = stop
                    elif boundary == "rereview":
                        case.on_review = stop
                    if boundary == "authorization":
                        with patch.object(LoopRuntime, "execute", side_effect=stop):
                            result = case.extend(max_passes=10, max_reviews=5)
                    else:
                        result = case.extend(max_passes=10, max_reviews=5,
                                             on_pass_result=stop if boundary == "collected" else None)
                    self.assertEqual(result.state, "interrupted", result.render())
                    case.assert_unchanged_refusal(max_passes=12, max_reviews=6)
                    case.on_implementation = case.on_review = None
                    done = loop("DEV-7")
                    self.assertEqual(done.state, "clean", done.render())
                    after = case.store.read()[0]
                    self.assertEqual(after["records"][:6], before["records"])
                    self.assertEqual(after["reviewer"], before["reviewer"])
                    self.assertEqual(after["run_id"], before["run_id"])
                    self.assertEqual(loop("DEV-7").state, "clean")
                    self.assertEqual((len(case.impl_prompts), len(case.prompts), len(case.registry.list())), (4, 4, 2))
                    self.assertEqual(len({r["pass_id"] for r in after["records"]}), 8)
                finally:
                    case.doCleanups()

    def test_context_requirements_and_git_drift_refuse_without_budget_change(self):
        before = self.exhausted()
        for failure in ("implementation", "reviewer", "busy_i", "busy_r", "unknown_i", "unknown_r",
                        "missing_i", "missing_r_session", "uncertain_r", "checkout", "base", "requirements"):
            with self.subTest(failure=failure):
                issue, panes = self.linear.get_issue.return_value, copy.deepcopy(self.panes)
                self.impl_verification_error = failure == "implementation"
                self.impl_status = {"busy_i": "working", "unknown_i": "unknown"}.get(failure, "done")
                self.status = {"busy_r": "working", "unknown_r": "unknown"}.get(failure, "done")
                if failure == "missing_i":
                    self.panes = self.panes[1:]
                if failure == "uncertain_r":
                    self.registry.update("DEV-7-R1", state="uncertain")
                if failure == "checkout":
                    (self.path / "drift").write_text("external edit")
                if failure == "base":
                    self.command(self.repo, "commit", "--allow-empty", "-m", "base advanced")
                if failure == "requirements":
                    self.linear.get_issue.return_value = replace(issue, description="changed requirements")
                try:
                    with patch.object(self.adapter, "verify_review_session",
                            side_effect=TaskError("Reviewer session missing") if failure in {"reviewer", "missing_r_session"} else None,
                            wraps=self.adapter.verify_review_session):
                        self.assert_unchanged_refusal(max_passes=10, max_reviews=5)
                finally:
                    self.panes = panes
                    self.registry.update("DEV-7-R1", state="active")
                    (self.path / "drift").unlink(missing_ok=True)
                    self.linear.get_issue.return_value = issue
                    if failure == "base":
                        self.command(self.repo, "update-ref", "refs/heads/main", before["snapshot"]["base_commit"])

    def test_competing_controller_and_checkpoint_race_refuse(self):
        self.exhausted()
        with PublicationStore(self.path).locked():
            self.assert_unchanged_refusal(max_passes=10, max_reviews=5)
        original = LoopStore.extend
        def changed(store, expected, passes, reviews):
            updated = copy.deepcopy(expected)
            updated["reason"] = "Another stop cause"
            store.save(updated)
            return original(store, expected, passes, reviews)
        with patch.object(LoopStore, "extend", changed):
            with self.assertRaisesRegex(TaskError, "checkpoint changed"):
                self.extend(max_passes=10, max_reviews=5)
        self.assertEqual(self.store.read()[0]["max_passes"], 6)
        self.assertEqual((len(self.impl_prompts), len(self.prompts)), (3, 3))

    def test_wrong_issue_repository_checkout_and_session_settings_refuse(self):
        before = self.exhausted()
        issue = self.linear.get_issue.return_value
        self.linear.get_issue.return_value = replace(issue, identifier="DEV-8")
        self.assert_unchanged_refusal(max_passes=10, max_reviews=5)
        self.linear.get_issue.return_value = issue
        changes = [dict(binding=dict(before["binding"], worktree=str(self.repo))),
                   dict(binding=dict(before["binding"], branch="dev-7-wrong"),
                        snapshot=dict(before["snapshot"], branch="dev-7-wrong")),
                   dict(reviewer_options=dict(before["reviewer_options"], model="different")),
                   # Last: the ownership guard also refuses subsequent writes
                   # against a checkpoint naming a non-permanent repository.
                   dict(binding=dict(before["binding"], repository=str(self.path)))]
        for change in changes:
            with self.subTest(change=change):
                self.store.save(dict(copy.deepcopy(before), **change))
                self.assert_unchanged_refusal(max_passes=10, max_reviews=5)

    def test_unresolved_standalone_handoff_and_publication_refuse_before_authorization(self):
        self.exhausted()
        with patch("task_start.loop.ReviewHandoffStore.read", return_value=dict(consumed=None)):
            self.assert_unchanged_refusal(max_passes=10, max_reviews=5)
        saved = PublicationStore(self.path).read()
        with patch.object(PublicationStore, "read", return_value=dict(saved, rebase=dict(result=None))):
            self.assert_unchanged_refusal(max_passes=10, max_reviews=5)
        with patch("task_start.loop.prepare_review_continuation", side_effect=TaskError("Unresolved publication")):
            self.assert_unchanged_refusal(max_passes=10, max_reviews=5)

    def test_exhaustion_after_resolved_blocked_review_preserves_retry_history(self):
        self.review_results = [blocked(loops.finding())]
        self.assertEqual(loop("DEV-7").state, "escalated")
        self.review_results = [dict(loops.findings(loops.finding(n)), checks=[PASSED_BROWSER]) for n in (2, 4)]
        self.assertEqual(loop("DEV-7", action="retry-review").state, "escalated")
        before = self.store.read()[0]
        self.assertEqual(before["reason"], BUDGET_STOP_REASON)
        self.review_results = [dict(checks=[PASSED_BROWSER])]
        result = self.extend(max_passes=10, max_reviews=5)
        self.assertEqual(result.state, "clean", result.render())
        self.assertEqual(self.store.read()[0]["records"][:6], before["records"])

    def test_pause_during_authorization_is_sticky_until_continue(self):
        self.exhausted()
        self.review_results = [{}]
        original = LoopStore.extend
        def pause(store, expected, passes, reviews):
            result = loop("DEV-7", action="pause")
            self.assertTrue(result.data["pause_requested"])
            return original(store, expected, passes, reviews)
        with patch.object(LoopStore, "extend", pause):
            self.assertEqual(self.extend(max_passes=10, max_reviews=5).state, "paused")
        with self.assertRaisesRegex(TaskError, "--continue only resumes a pause"):
            loop("DEV-7")
        self.assertEqual(loop("DEV-7", action="status").state, "paused")
        self.assertEqual((len(self.impl_prompts), len(self.prompts)), (3, 3))
        self.assertEqual(loop("DEV-7", action="continue").state, "clean")
        self.assertEqual((len(self.impl_prompts), len(self.prompts)), (4, 4))

    def test_non_budget_stops_and_human_external_blockers_are_not_extensions(self):
        for verdict in (blocked(loops.finding(4)), loops.findings(loops.finding(category="human_decision")),
                        dict(state="failed", summary="Reviewer failure")):
            with self.subTest(verdict=verdict):
                case = ExtendTests()
                case.setUp()
                try:
                    case.review_results = [verdict]
                    loop("DEV-7", max_passes=2, max_reviews=1)
                    case.assert_unchanged_refusal(max_passes=10, max_reviews=5)
                    # A matching reason alone cannot turn these stops into budget proof.
                    state = case.store.read()[0]
                    state["reason"] = BUDGET_STOP_REASON
                    case.store.save(state)
                    case.assert_unchanged_refusal(max_passes=10, max_reviews=5)
                finally:
                    case.doCleanups()

    def test_reason_counts_findings_and_complete_session_proof_are_all_required(self):
        before = self.exhausted()
        changes = [dict(reason="Unrelated failure"), dict(max_passes=10, max_reviews=5),
                   dict(reviewer=dict(context_id="DEV-7-R1")), dict(findings=[loops.finding(99)]), dict(seen=[])]
        for change in changes:
            with self.subTest(change=change):
                state = dict(copy.deepcopy(before), **change)
                self.store.save(state)
                self.assert_unchanged_refusal(max_passes=12, max_reviews=6)
        legacy = copy.deepcopy(before)
        del legacy["delivery"]
        self.store.save(legacy)
        self.assert_unchanged_refusal(max_passes=10, max_reviews=5)

    def test_pending_fixes_guard_does_not_require_extension_continuity(self):
        before = self.exhausted()
        for missing in ("delivery", "reviewer", "both"):
            with self.subTest(missing=missing):
                state = copy.deepcopy(before)
                if missing in {"delivery", "both"}:
                    del state["delivery"]
                if missing in {"reviewer", "both"}:
                    state["reviewer"] = dict(context_id="DEV-7-R1")
                self.store.save(state)
                self.assert_unchanged_refusal(max_passes=10, max_reviews=5)
                contexts = self.registry.list()
                prompts = (list(self.impl_prompts), list(self.prompts))
                # Refuse before preparing a fresh run, even when extension itself
                # cannot authorize work from the retained continuity evidence.
                with patch("task_start.loop.environment", side_effect=AssertionError("Prepared a replacement run")):
                    with self.assertRaisesRegex(TaskError, "skip.*correction.*--extend"):
                        loop("DEV-7", action="new", from_review=True)
                self.assertEqual(self.store.read()[0], state)
                fresh = dict(copy.deepcopy(before), version=2, initial_phase="review")
                with self.assertRaisesRegex(TaskError, "skip.*correction.*--extend"):
                    self.store.create(fresh, replace=True)
                self.assertEqual(self.store.read()[0], state)
                self.assertEqual(self.registry.list(), contexts)
                self.assertEqual((self.impl_prompts, self.prompts), prompts)

    def test_extension_requires_observed_original_reviewer_inactivity(self):
        before = self.exhausted()
        panes = copy.deepcopy(self.panes)
        for runtime in ("unreported", "unreported_idle_label", "absent"):
            with self.subTest(runtime=runtime):
                if runtime.startswith("unreported"):
                    self.panes[-1].update(agent=None, agent_session=None, agent_status=None)
                    if runtime == "unreported_idle_label":
                        self.panes[-1]["agent_status"] = "idle"
                else:
                    self.panes = self.panes[:-1]
                self.status = "idle" if runtime == "unreported_idle_label" else "unknown"
                try:
                    with patch.object(LoopStore, "extend", side_effect=AssertionError("Authorized without idle evidence")) as authorize:
                        self.assert_unchanged_refusal(max_passes=10, max_reviews=5)
                        authorize.assert_not_called()
                finally:
                    self.panes = copy.deepcopy(panes)
        self.status = "done"
        self.review_results = [{}]
        result = self.extend(max_passes=10, max_reviews=5)
        self.assertEqual(result.state, "clean", result.render())
        after = self.store.read()[0]
        self.assertEqual(after["run_id"], before["run_id"])
        self.assertEqual(after["reviewer"], before["reviewer"])
        self.assertEqual([r["context_id"] for r in after["records"][-2:]], ["DEV-7-I1", "DEV-7-R1"])
        self.assertEqual((len(self.impl_prompts), len(self.prompts), len(self.registry.list())), (4, 4, 2))

    def test_unresolved_active_or_collected_delivery_requires_ordinary_recovery(self):
        for collected in (False, True):
            with self.subTest(collected=collected):
                case = ExtendTests()
                case.setUp()
                try:
                    case.review_results = [loops.findings(loops.finding(4))]
                    def stop(*args):
                        raise KeyboardInterrupt()
                    if not collected:
                        case.on_review = stop
                    loop("DEV-7", max_passes=2, max_reviews=1,
                         on_pass_result=(lambda value: stop() if value["state"] == "findings" else None) if collected else None)
                    state = case.store.read()[0]
                    self.assertIsNotNone(state["delivery"])
                    # A stop label plus exhausted counters still cannot grant
                    # extension while the original delivery is unresolved.
                    state.update(status="escalated", reason=BUDGET_STOP_REASON)
                    case.store.save(state)
                    case.assert_unchanged_refusal(max_passes=10, max_reviews=5)
                finally:
                    case.doCleanups()

    def test_completed_fixes_can_start_review_first_and_replaced_run_is_never_resurrected(self):
        before = self.exhausted(max_passes=3)
        self.assertEqual(before["next_phase"], "rereview")
        self.review_results = [{}]
        result = loop("DEV-7", action="new", from_review=True)
        self.assertEqual(result.state, "clean", result.render())
        current = self.store.read()[0]
        self.assertNotEqual(current["run_id"], before["run_id"])
        self.assertEqual(current["reviewer"]["context_id"], "DEV-7-R2")
        self.assertEqual(len(current["records"]), 1)
        self.assert_unchanged_refusal(max_passes=10, max_reviews=5)
        self.assertEqual(self.store.read()[0], current)

    def test_legacy_completed_fixes_remain_eligible_for_review_first(self):
        before = self.exhausted(max_passes=3)
        self.assertEqual(before["next_phase"], "rereview")
        self.assertEqual(before["records"][-1]["phase"], "fixes")
        self.assertEqual(before["records"][-1]["resolutions"], [dict(finding_id="F1", summary="Fixed F1")])
        del before["delivery"]
        before["reviewer"] = dict(context_id="DEV-7-R1")
        self.store.save(before)
        self.review_results = [{}]
        result = loop("DEV-7", action="new", from_review=True)
        self.assertEqual(result.state, "clean", result.render())
        current = self.store.read()[0]
        self.assertNotEqual(current["run_id"], before["run_id"])
        self.assertEqual(current["reviewer"]["context_id"], "DEV-7-R2")
        self.assertEqual([r["phase"] for r in current["records"]], ["review"])
        self.assertEqual((len(self.impl_prompts), len(self.prompts)), (2, 2))

    def test_checkpoint_replacement_guard_is_atomic(self):
        before = self.exhausted()
        fresh = dict(copy.deepcopy(before), version=2, initial_phase="review")
        with self.assertRaisesRegex(TaskError, "--extend"):
            self.store.create(fresh, replace=True)
        self.assertEqual(self.store.read()[0], before)

    def test_status_and_report_offer_same_conditional_extension_without_mutation(self):
        before = self.exhausted()
        result = loop("DEV-7", action="status")
        status = task_status("DEV-7", registry=self.registry, identities=self.identities,
                             projects=[Project(self.linear.get_issue.return_value.project, self.repo.name, "main")],
                             projects_root=self.repo.parent).as_dict()["tasks"][0]
        hint = status["guidance"]
        self.assertEqual(hint["kind"], "extend", status)
        self.assertEqual(hint["command"], "task loop DEV-7 --extend --max-passes 10 --max-reviews 5")
        self.assertEqual(hint["message"], result.data["next_action"])
        for text in ("DEV-7-I1 / DEV-7-R1", "unresolved findings", "must still verify", "omits prior findings"):
            self.assertIn(text, result.render())
        for current in (dict(status, controller="held"), dict(status, state="integration_pending")):
            self.assertNotEqual(guidance(current, extension=extension_guidance(before))["kind"], "extend")
        self.assertEqual(self.store.read()[0], before)

    def test_ceiling_guidance_never_suggests_impossible_bounds(self):
        # Use ordered synthetic completed passes to exercise the supported ceiling
        # without launching forty fixture turns. No historical result is inferred.
        state = self.exhausted()
        implementation, review = state["records"][:2]
        state.update(records=[], seen=[], pass_count=40, review_count=20, max_passes=40, max_reviews=20)
        for number in range(1, 21):
            items = [loops.finding(number)]
            impl = dict(implementation, phase="implementation" if number == 1 else "fixes",
                        pass_id=f"implementation-{number}", resolutions=[] if number == 1 else
                        [dict(finding_id=f"F{number - 1}", summary="Corrected previous finding")])
            rev = dict(review, phase="review" if number == 1 else "rereview", pass_id=f"review-{number}", findings=items)
            state["records"].extend([impl, rev])
            state["seen"].append(dict(ids=[f"F{number}"], signature=finding_signature(items)))
        state["findings"] = items
        hint = extension_guidance(state)
        self.assertIsNone(hint["command"])
        self.assertIn("40 passes/20 reviews", hint["message"])

    def test_cli_controls_selections_help_and_awl_legacy_output(self):
        self.exhausted()
        for flag in ("--new", "--continue", "--retry-review", "--abort", "--status", "--pause-after-current"):
            with self.subTest(flag=flag), patch("sys.stderr", new_callable=io.StringIO):
                with self.assertRaises(SystemExit):
                    cli.parser().parse_args(["loop", "DEV-7", "--extend", flag])
        for args in (["--from-review"], ["--timeout", "20"], ["--r-agent", "codex"],
                     ["--r-model", "x"], ["--r-mode", "high"], ["--i-agent", "pi"],
                     ["--i-model", "x"], ["--i-mode", "low"]):
            with self.subTest(args=args), patch("sys.stderr", new_callable=io.StringIO):
                self.assertEqual(cli.main(["loop", "DEV-7", "--extend", "--max-passes", "10", *args]), 1)
        for prog in ("awl", "task"):
            with patch("sys.stdout", new_callable=io.StringIO) as output:
                self.assertEqual(cli.main(["loop", "DEV-7", "--status"], prog=prog), 0)
            self.assertIn(f"{prog} loop DEV-7 --extend --max-passes 10 --max-reviews 5", output.getvalue())
            with patch("sys.stdout", new_callable=io.StringIO) as output:
                with self.assertRaises(SystemExit) as exited:
                    cli.main(["loop", "--help"], prog=prog)
            self.assertEqual(exited.exception.code, 0)
            self.assertIn("--extend", output.getvalue())
            self.assertIn("original conversations", output.getvalue())
        self.review_results = [{}]
        with patch("sys.stdout", new_callable=io.StringIO) as output:
            self.assertEqual(cli.main(["loop", "DEV-7", "--extend", "--max-passes", "10", "--max-reviews", "5", "--json"], prog="awl"), 0)
        value = json.loads(output.getvalue())
        self.assertEqual((value["state"], value["passes"], value["reviewer_context"]), ("clean", 8, "DEV-7-R1"))


if __name__ == "__main__":
    unittest.main()
