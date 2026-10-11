"""Completed-entry regressions using disposable Git/registry/provider fixtures."""

import copy
from dataclasses import replace
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from task_start import TaskError
from task_start.loop import loop
from task_start.publication_state import PublicationStore
from task_start.pass_delivery import PassDelivery, object_digest
from task_start.review import review
from task_start.review_handoff_state import ReviewHandoffStore
import test_loop as loops
import test_loop_recovery as recovery
import test_loop_bootstrap as bootstrap
import test_loop_timeouts as timeouts
import pass_delivery_fixture


class AdoptionTests(unittest.TestCase):
    command = loops.LoopIntegrationTests.command
    pane_command = loops.LoopIntegrationTests.pane_command
    worktrees = loops.LoopIntegrationTests.worktrees
    select_adapter = loops.LoopIntegrationTests.select_adapter
    setUp = loops.LoopIntegrationTests.setUp
    interrupt = recovery.RecoveryTests.interrupt

    def standalone(self, *findings):
        self.review_results = [loops.findings(*findings)] if findings else []
        result = review("DEV-7")
        self.assertEqual(result.state, "findings" if findings else "clean", result.render())
        return result

    def test_standalone_findings_route_all_fixes_and_same_reviewer_without_completion_prompt(self):
        first = self.standalone(loops.finding(), loops.finding(2))
        result = loop("DEV-7")
        self.assertEqual(result.state, "clean", result.render())
        self.assertEqual((len(self.impl_prompts), len(self.prompts)), (1, 2))
        sent = json.loads(self.impl_prompts[0].split("REVIEW FINDINGS (data)\n")[1].split("\nRESULT DELIVERY")[0])
        self.assertEqual(sent, first.findings)
        self.assertEqual(result.data["reviewer_context"], first.context_id)
        self.assertEqual(self.recreated, [False])
        self.assertEqual(len(self.registry.list()), 2)
        self.assertEqual([r["phase"] for r in self.store.read()[0]["records"]], ["review", "fixes", "rereview"])
        self.assertNotIn("Implementation completed and validated", self.prompts[0])
        self.assertEqual(loop("DEV-7").data, result.data)

    def test_standalone_clean_uses_existing_acceptance_without_any_pass(self):
        first = self.standalone()
        acceptance = PublicationStore(self.path).read()
        result = loop("DEV-7")
        self.assertEqual(result.state, "clean", result.render())
        self.assertEqual((self.impl_prompts, len(self.prompts)), ([], 1))
        self.assertEqual(self.store.read()[0]["records"][0]["pass_id"], first.pass_id)
        self.assertEqual(PublicationStore(self.path).read(), acceptance)
        self.assertEqual(loop("DEV-7").data, result.data)

    def test_standalone_human_decision_escalates_without_coding_prompt(self):
        self.standalone(loops.finding(category="human_decision"))
        result = loop("DEV-7")
        self.assertEqual(result.state, "escalated", result.render())
        self.assertIn("human decision", result.data["reason"])
        self.assertEqual((self.impl_prompts, len(self.prompts)), ([], 1))

    def test_late_i1_with_intervening_r1_recovers_original_proof_then_routes_unattended(self):
        before = self.interrupt("implementation")
        directory = Path(before["delivery"]["directory"])
        original = {name: (directory / name).read_bytes() for name in ("result.json", "completion.json")}
        first = self.standalone(loops.finding())
        observed = []
        def collected(result):
            observed.append(result["pass_id"])
            if result["pass_id"] == before["active_pass"]["pass_id"]:
                for name, data in original.items():
                    self.assertEqual((directory / name).read_bytes(), data)
        result = loop("DEV-7", on_pass_result=collected)
        self.assertEqual(result.state, "clean", result.render())
        state = self.store.read()[0]
        self.assertEqual(state["run_id"], before["run_id"])
        self.assertEqual([r["phase"] for r in state["records"]], ["implementation", "review", "fixes", "rereview"])
        self.assertEqual(state["records"][1]["pass_id"], first.pass_id)
        self.assertEqual((len(self.impl_prompts), len(self.prompts)), (2, 2))
        self.assertEqual(observed.count(before["active_pass"]["pass_id"]), 1)
        self.assertEqual(loop("DEV-7").data, result.data)

    def test_pause_during_late_recovery_collects_adopted_review_and_waits_before_fixes(self):
        self.interrupt("implementation")
        self.standalone(loops.finding())
        loop("DEV-7", action="pause")
        paused = loop("DEV-7")
        self.assertEqual(paused.state, "paused", paused.render())
        self.assertEqual(paused.data["next_phase"], "fixes")
        self.assertEqual((len(self.impl_prompts), len(self.prompts)), (1, 1))
        with self.assertRaises(TaskError):
            loop("DEV-7")
        self.assertTrue(self.store.read()[1])
        result = loop("DEV-7", action="continue")
        self.assertEqual(result.state, "clean", result.render())
        self.assertEqual((len(self.impl_prompts), len(self.prompts)), (2, 2))

    def test_checkpoint_first_consumption_gap_does_not_repeat_review_or_fixes(self):
        self.standalone(loops.finding())
        consume = ReviewHandoffStore.consume
        with patch.object(ReviewHandoffStore, "consume", side_effect=KeyboardInterrupt):
            result = loop("DEV-7")
        self.assertEqual(result.state, "interrupted", result.render())
        self.assertEqual(self.impl_prompts, [])
        self.assertIsNone(ReviewHandoffStore(self.path).read()["consumed"])
        with patch.object(ReviewHandoffStore, "consume", consume):
            result = loop("DEV-7")
        self.assertEqual(result.state, "clean", result.render())
        self.assertEqual((len(self.impl_prompts), len(self.prompts)), (1, 2))

    def test_terminal_consumption_gap_preserves_checkpoint_on_retry_and_new(self):
        for outcome in ("human_decision", "blocked", "failed", "clean"):
            for action in ("run", "new"):
                with self.subTest(outcome=outcome, action=action):
                    case = AdoptionTests()
                    case.setUp()
                    try:
                        if outcome == "human_decision":
                            first = case.standalone(loops.finding(category=outcome))
                            reason = "Review requires a human decision or cannot be safely classified"
                        elif outcome == "clean":
                            first = case.standalone()
                            reason = "Implementation and independent review completed cleanly"
                        else:
                            reason = "An external decision is required"
                            case.review_results = [dict(state=outcome, summary=reason, findings=[])]
                            first = review("DEV-7")
                        with patch.object(ReviewHandoffStore, "consume", side_effect=KeyboardInterrupt), \
                                case.assertRaises(KeyboardInterrupt):
                            loop("DEV-7")
                        before = case.store.read()
                        case.assertEqual(before[0]["status"], "clean" if outcome == "clean" else "escalated")
                        case.assertEqual(before[0]["reason"], reason)
                        case.assertIsNone(before[0]["active_pass"])
                        case.assertIsNone(before[0]["next_phase"])
                        case.assertEqual([r["pass_id"] for r in before[0]["records"]], [first.pass_id])
                        case.assertIsNone(ReviewHandoffStore(case.path).read()["consumed"])
                        acceptance = PublicationStore(case.path).read()
                        with patch("task_start.loop.route", side_effect=AssertionError("Already routed")), \
                                patch("task_start.loop.environment", side_effect=AssertionError("Checkpoint owns recovery")):
                            result = loop("DEV-7", action=action)
                            case.assertEqual(loop("DEV-7").data, result.data)
                        case.assertEqual(case.store.read(), before)
                        case.assertEqual(result.data["reason"], reason)
                        case.assertEqual(ReviewHandoffStore(case.path).read()["consumed"], before[0]["run_id"])
                        case.assertEqual(PublicationStore(case.path).read(), acceptance)
                        case.assertEqual((case.impl_prompts, len(case.prompts)), ([], 1))
                    finally:
                        case.doCleanups()

    def test_conflicting_reviewer_flags_do_not_replace_standalone_session(self):
        self.standalone(loops.finding())
        with self.assertRaisesRegex(TaskError, "original reviewer settings"):
            loop("DEV-7", model="replacement")
        self.assertFalse(self.store.path.exists())
        self.assertEqual((self.impl_prompts, len(self.prompts)), ([], 1))

    def test_late_terminal_adoption_marker_failure_preserves_reason_and_pause(self):
        original = self.interrupt("implementation")
        first = self.standalone(loops.finding(category="human_decision"))
        loop("DEV-7", action="pause")
        consume = ReviewHandoffStore.consume
        def fail_terminal_write(handoffs, state):
            if state["next_phase"] is None:
                raise OSError("Interrupted marker write")
            return consume(handoffs, state)
        with patch.object(ReviewHandoffStore, "consume", fail_terminal_write), self.assertRaises(OSError):
            loop("DEV-7")
        before, pause = self.store.read()
        self.assertEqual(before["status"], "escalated")
        self.assertIn("human decision", before["reason"])
        self.assertEqual(before["run_id"], original["run_id"])
        self.assertEqual([r["phase"] for r in before["records"]], ["implementation", "review"])
        self.assertEqual(before["records"][-1]["pass_id"], first.pass_id)
        self.assertTrue(pause)
        result = loop("DEV-7")
        self.assertEqual(result.data["reason"], before["reason"])
        self.assertTrue(result.data["pause_requested"])
        self.assertEqual(self.store.read(), (before, pause))
        self.assertEqual(ReviewHandoffStore(self.path).read()["consumed"], original["run_id"])
        self.assertEqual((len(self.impl_prompts), len(self.prompts)), (1, 1))

    def test_terminal_consumption_repair_refuses_conflicting_checkpoint_evidence(self):
        self.standalone(loops.finding(category="human_decision"))
        with patch.object(ReviewHandoffStore, "consume", side_effect=KeyboardInterrupt), self.assertRaises(KeyboardInterrupt):
            loop("DEV-7")
        before = self.store.read()
        handoffs = ReviewHandoffStore(self.path)
        value = handoffs.read()
        value["result"]["summary"] = "Conflicting result"
        handoffs.write(value)
        for action in ("run", "new"):
            with self.subTest(action=action), self.assertRaisesRegex(TaskError, "conflicts with its checkpointed result"):
                loop("DEV-7", action=action)
            self.assertEqual(self.store.read(), before)
            self.assertEqual(handoffs.read(), value)
        self.assertEqual((self.impl_prompts, len(self.prompts)), ([], 1))

    def test_explicit_new_after_consumption_still_starts_a_fresh_review(self):
        first = self.standalone()
        accepted = loop("DEV-7")
        result = loop("DEV-7", action="new", from_review=True)
        self.assertEqual(result.state, "clean", result.render())
        state = self.store.read()[0]
        self.assertNotEqual(state["run_id"], accepted.data["run_id"])
        self.assertEqual([r["phase"] for r in state["records"]], ["review"])
        self.assertNotEqual(state["records"][0]["pass_id"], first.pass_id)
        self.assertEqual(state["reviewer"]["context_id"], "DEV-7-R2")
        self.assertEqual(ReviewHandoffStore(self.path).read()["consumed"], accepted.data["run_id"])
        self.assertEqual((self.impl_prompts, len(self.prompts)), ([], 2))

    def test_successive_fixes_and_same_reviewer_continue_without_human_commands(self):
        first = self.standalone(loops.finding())
        self.review_results = [loops.findings(loops.finding(2)), {}]
        result = loop("DEV-7")
        self.assertEqual(result.state, "clean", result.render())
        self.assertEqual(result.data["reviewer_context"], first.context_id)
        self.assertEqual((len(self.impl_prompts), len(self.prompts)), (2, 3))
        self.assertEqual(self.recreated, [False, False])
        self.assertEqual([r["phase"] for r in self.store.read()[0]["records"]],
                         ["review", "fixes", "rereview", "fixes", "rereview"])

    def test_adopted_fix_claim_recovers_without_resending_findings(self):
        self.standalone(loops.finding())
        self.on_implementation = lambda *_: (_ for _ in ()).throw(KeyboardInterrupt())
        self.assertEqual(loop("DEV-7").state, "interrupted")
        claim = copy.deepcopy(self.store.read()[0]["active_pass"])
        self.on_implementation = None
        result = loop("DEV-7")
        self.assertEqual(result.state, "clean", result.render())
        self.assertEqual(self.store.read()[0]["records"][1]["pass_id"], claim["pass_id"])
        self.assertEqual((len(self.impl_prompts), len(self.prompts)), (1, 2))

    def test_verified_saved_implementation_completion_skips_a_second_i1_turn(self):
        def stop_after_collection(_):
            raise KeyboardInterrupt()
        self.assertEqual(loop("DEV-7", on_pass_result=stop_after_collection).state, "interrupted")
        completed = self.store.read()[0]
        self.assertIsNone(completed["active_pass"])
        result = loop("DEV-7")
        self.assertEqual(result.state, "clean", result.render())
        self.assertEqual((len(self.impl_prompts), len(self.prompts)), (1, 1))
        self.assertEqual(self.store.read()[0]["implementation"], completed["implementation"])

    def test_standalone_after_collected_i1_adopts_at_the_saved_review_boundary(self):
        def stop_after_collection(_):
            raise KeyboardInterrupt()
        self.assertEqual(loop("DEV-7", on_pass_result=stop_after_collection).state, "interrupted")
        first = self.standalone(loops.finding())
        result = loop("DEV-7")
        self.assertEqual(result.state, "clean", result.render())
        self.assertEqual(self.store.read()[0]["records"][1]["pass_id"], first.pass_id)
        self.assertEqual((len(self.impl_prompts), len(self.prompts)), (2, 2))

    def test_legacy_review_without_handoff_can_only_fall_back_to_fresh_review(self):
        self.standalone()
        ReviewHandoffStore(self.path).path.unlink()  # Synthetic pre-feature output.
        result = loop("DEV-7")
        self.assertEqual(result.state, "clean", result.render())
        self.assertEqual((len(self.impl_prompts), len(self.prompts)), (1, 2))
        self.assertEqual(result.data["reviewer_context"], "DEV-7-R2")

    def test_late_original_result_cannot_admit_legacy_intervening_review(self):
        before = self.interrupt("implementation")
        self.standalone()
        ReviewHandoffStore(self.path).path.unlink()
        result = loop("DEV-7")
        self.assertEqual(result.state, "escalated", result.render())
        self.assertIn("context set changed", result.data["reason"])
        self.assertEqual(self.store.read()[0]["active_pass"], before["active_pass"])
        self.assertEqual((len(self.impl_prompts), len(self.prompts)), (1, 1))

    def test_adoption_refuses_stale_missing_ambiguous_and_contradictory_evidence(self):
        faults = ("pending", "receipt", "implementation_session", "reviewer_session", "busy_i1", "busy_r1",
                  "missing_context", "additional_context", "git", "base", "requirements", "slice",
                  "legacy_findings", "duplicate_findings", "acceptance", "publication", "provider", "origin")
        for fault in faults:
            with self.subTest(fault=fault):
                case = AdoptionTests()
                case.setUp()
                try:
                    first = case.standalone() if fault == "acceptance" else case.standalone(loops.finding())
                    store = ReviewHandoffStore(case.path)
                    value = store.read()
                    if fault == "pending":
                        value["result"] = None
                    elif fault == "receipt":
                        value["receipt"] = None
                    elif fault in {"implementation_session", "reviewer_session"}:
                        key = "implementation" if fault == "implementation_session" else "reviewer"
                        value[key]["session"]["value"] = "replacement"
                    elif fault == "busy_i1":
                        case.impl_status = "working"
                    elif fault == "busy_r1":
                        case.status = "working"
                    elif fault == "missing_context":
                        case.registry.update(first.context_id, state="uncertain")
                    elif fault == "additional_context":
                        case.registry.allocate("DEV-7", "review", agent="codex", model="other", mode="high",
                            repository=str(case.repo), worktree=str(case.path), endpoint="/server.sock",
                            workspace_id="w1", tab_id="t1", pane_id="other", terminal_id="other")
                    elif fault == "git":
                        (case.path / "changed.txt").write_text("drift")
                    elif fault == "base":
                        case.command(case.repo, "commit", "--allow-empty", "-m", "Moved base")
                    elif fault == "requirements":
                        case.linear.get_issue.return_value = replace(case.linear.get_issue.return_value, description="Changed")
                    elif fault == "slice":
                        value["slice"] = "different"
                    elif fault == "legacy_findings":
                        for finding in value["result"]["findings"]:
                            finding.pop("id")
                            finding.pop("category")
                    elif fault == "duplicate_findings":
                        value["result"]["findings"] *= 2
                    elif fault == "acceptance":
                        publication = PublicationStore(case.path)
                        saved = publication.read()
                        saved["acceptance"] = None
                        publication.write(saved)
                        value["publication"] = object_digest(saved)
                    elif fault == "publication":
                        publication = PublicationStore(case.path)
                        saved = publication.read()
                        saved["intent"] = dict(competing="claim")
                        publication.write(saved)
                    elif fault == "provider":
                        case.enterContext(patch.object(case.adapter, "observe_delivery", return_value=False))
                    elif fault == "origin":
                        value["origin"] = dict(run_id="another-loop")
                    store.write(value)
                    with case.assertRaises(TaskError):
                        loop("DEV-7")
                    case.assertFalse(case.store.path.exists())
                    case.assertEqual((case.impl_prompts, len(case.prompts)), ([], 1))
                    case.assertIsNone(store.read()["consumed"])
                finally:
                    case.doCleanups()

    def test_dev90_shaped_late_initial_i1_and_r1_preserve_seal_and_receipts(self):
        case = bootstrap.BootstrapLoopTests()
        case.setUp()
        try:
            case.enterContext(patch("task_start.review.ContextRegistry", return_value=case.registry))
            clock = timeouts.AdvisoryTimeoutTests.clock(self, case)
            def interrupt_before_collection(*_):
                # Launch has returned and registered I1 as active. Model the
                # original turn completing after its controller's old deadline.
                clock.now += 3100
                raise KeyboardInterrupt()
            with patch.object(pass_delivery_fixture, "complete"), \
                    patch.object(PassDelivery, "verify", side_effect=interrupt_before_collection):
                case.assertEqual(loop("DEV-7", timeout=1800).state, "interrupted")
            before = case.store.read()[0]
            before.update(status="escalated", reason="Historical hard timeout")
            case.store.save(before)
            pass_delivery_fixture.complete_prompt(case.impl_prompts[0])
            proof = Path(before["delivery"]["directory"]) / "completion.json"
            sealed = proof.read_bytes()
            case.assertGreater(json.loads(sealed)["completed_at"], before["delivery"]["deadline"])
            case.review_results = [loops.findings(loops.finding())]
            first = review("DEV-7")
            case.assertEqual(first.state, "findings", first.render())
            def collected(result):
                if result["pass_id"] == before["active_pass"]["pass_id"]:
                    case.assertEqual(proof.read_bytes(), sealed)
                    case.assertEqual(case.store.read()[0]["delivery"], before["delivery"])
            result = loop("DEV-7", on_pass_result=collected)
            case.assertEqual(result.state, "clean", result.render())
            case.assertEqual((len(case.impl_prompts), len(case.prompts)), (2, 2))
            case.assertEqual(case.store.read()[0]["reviewer"]["context_id"], first.context_id)
            case.assertEqual(case.store.read()[0]["implementation"], before["implementation"])
        finally:
            case.doCleanups()

    def test_proven_intervening_review_never_relaxes_original_completion_checks(self):
        for fault in ("result_bytes", "seal", "original_provider", "reviewer_provider", "foreign_context",
                      "wrong_origin", "git_after_seal"):
            with self.subTest(fault=fault):
                case = AdoptionTests()
                case.setUp()
                try:
                    before = case.interrupt("implementation")
                    directory = Path(before["delivery"]["directory"])
                    if fault == "git_after_seal":
                        (case.path / "foreign.txt").write_text("R1 sees later, unsealed edits")
                    case.standalone(loops.finding())
                    if fault == "result_bytes":
                        path = directory / "result.json"
                        path.write_bytes(path.read_bytes() + b"\n")
                    elif fault == "seal":
                        (directory / "completion.json").unlink()
                    elif fault == "original_provider":
                        case.enterContext(patch.object(case.implementer, "observe_delivery",
                            side_effect=TaskError("Original receipt changed")))
                    elif fault == "reviewer_provider":
                        case.enterContext(patch.object(case.adapter, "observe_delivery",
                            side_effect=TaskError("Additional reviewer input")))
                    elif fault == "foreign_context":
                        case.registry.allocate("DEV-7", "review", agent="codex", model="other", mode="high",
                            repository=str(case.repo), worktree=str(case.path), endpoint="/server.sock",
                            workspace_id="w1", tab_id="t1", pane_id="other", terminal_id="other")
                    elif fault == "wrong_origin":
                        handoffs = ReviewHandoffStore(case.path)
                        value = handoffs.read()
                        value["origin"]["pass_id"] = value["result"]["pass_id"]
                        handoffs.write(value)
                    result = loop("DEV-7")
                    case.assertEqual(result.state, "escalated", result.render())
                    case.assertEqual(case.store.read()[0]["active_pass"], before["active_pass"])
                    case.assertEqual(case.store.read()[0]["delivery"], before["delivery"])
                    case.assertTrue(directory.exists())
                    case.assertEqual((len(case.impl_prompts), len(case.prompts)), (1, 1))
                finally:
                    case.doCleanups()

    def test_slow_provider_observation_cannot_hide_base_or_session_drift(self):
        for fault in ("base", "scope", "implementation", "reviewer"):
            with self.subTest(fault=fault):
                case = AdoptionTests()
                case.setUp()
                try:
                    case.standalone(loops.finding())
                    def drift(*_):
                        if fault == "base":
                            case.command(case.repo, "commit", "--allow-empty", "-m", "Base advanced")
                        elif fault == "scope":
                            path = ReviewHandoffStore(case.path).storage.directory / "agentic-workflows-scope.json"
                            value = json.loads(path.read_text())
                            value["slice"] = "other"
                            path.write_text(json.dumps(value))
                        else:
                            context = case.implementation if fault == "implementation" else "DEV-7-R1"
                            with case.registry.connection(write=True) as db:
                                db.execute("UPDATE contexts SET model='replaced' WHERE context_id=?", (context,))
                        return True
                    with patch.object(case.adapter, "observe_delivery", side_effect=drift), case.assertRaises(TaskError):
                        loop("DEV-7")
                    case.assertFalse(case.store.path.exists())
                    case.assertEqual((case.impl_prompts, len(case.prompts)), ([], 1))
                finally:
                    case.doCleanups()

    def test_slow_provider_observation_rechecks_live_contexts_before_clean_adoption(self):
        for role in ("implementation", "reviewer"):
            for fault in ("busy", "uncertain", "missing_pane", "ambiguous_pane", "cwd", "session", "mapping"):
                with self.subTest(role=role, fault=fault):
                    case = AdoptionTests()
                    case.setUp()
                    try:
                        case.standalone()
                        acceptance = PublicationStore(case.path).read()
                        def drift(*_):
                            context_id = case.implementation if role == "implementation" else "DEV-7-R1"
                            context = case.registry.get(context_id)
                            pane = next(p for p in case.panes if p["pane_id"] == context["pane_id"])
                            if fault == "busy":
                                if role == "implementation":
                                    case.impl_status = "working"
                                else:
                                    case.status = "working"
                            elif fault == "uncertain":
                                case.registry.update(context_id, state="uncertain")
                            elif fault == "missing_pane":
                                case.panes.remove(pane)
                            elif fault == "ambiguous_pane":
                                case.panes.append(copy.deepcopy(pane))
                            elif fault == "cwd":
                                pane["cwd"] = str(case.repo)
                            elif fault == "session":
                                pane["agent_session"] = dict(agent=context["agent"], kind="id", value="replacement")
                            else:
                                with case.registry.connection(write=True) as db:
                                    db.execute("UPDATE contexts SET endpoint='/replaced.sock' WHERE context_id=?", (context_id,))
                            return True
                        with patch.object(case.adapter, "observe_delivery", side_effect=drift), case.assertRaises(TaskError):
                            loop("DEV-7")
                        case.assertFalse(case.store.path.exists())
                        case.assertIsNone(ReviewHandoffStore(case.path).read()["consumed"])
                        case.assertEqual(PublicationStore(case.path).read(), acceptance)
                        case.assertEqual((case.impl_prompts, len(case.prompts)), ([], 1))
                    finally:
                        case.doCleanups()

    def test_pending_standalone_claim_is_not_bypassed_by_explicit_review_entry(self):
        self.standalone(loops.finding())
        handoffs = ReviewHandoffStore(self.path)
        value = handoffs.read()
        value["result"] = None
        handoffs.write(value)
        with self.assertRaisesRegex(TaskError, "no finished"):
            loop("DEV-7", from_review=True)
        self.assertEqual((self.impl_prompts, len(self.prompts)), ([], 1))

    def test_final_registry_health_is_checked_after_post_receipt_session_verification(self):
        for role in ("implementation", "reviewer"):
            for fault in (("uncertain", "retired") if role == "implementation" else
                          ("uncertain", "retired", "nonresumable")):
                with self.subTest(role=role, fault=fault):
                    case = AdoptionTests()
                    case.setUp()
                    try:
                        first = case.standalone()
                        acceptance = PublicationStore(case.path).read()
                        handoffs = ReviewHandoffStore(case.path)
                        before = handoffs.read()
                        context_id = case.implementation if role == "implementation" else first.context_id
                        adapter = case.implementer if role == "implementation" else case.adapter
                        method = "verify_session" if role == "implementation" else "verify_review_session"
                        verify = getattr(adapter, method)
                        observed = False
                        changed = []

                        def observe(*_):
                            nonlocal observed
                            observed = True
                            return True

                        def verify_then_change(*args):
                            reference = verify(*args)
                            if observed:
                                if fault == "retired":
                                    with case.registry.connection(write=True) as db:
                                        db.execute("UPDATE contexts SET retired_at='retired during verification' "
                                                   "WHERE context_id=?", (context_id,))
                                else:
                                    case.registry.update(context_id, **(
                                        dict(state="uncertain") if fault == "uncertain" else dict(resumability="unknown")))
                                changed.append(context_id)
                            return reference

                        refusal = "absent or retired" if role == "implementation" and fault == "retired" else "context health changed"
                        with patch.object(case.adapter, "observe_delivery", side_effect=observe), \
                                patch.object(adapter, method, side_effect=verify_then_change), \
                                case.assertRaisesRegex(TaskError, refusal):
                            loop("DEV-7")
                        case.assertEqual(changed, [context_id])
                        case.assertFalse(case.store.path.exists())
                        case.assertEqual(handoffs.read(), before)
                        case.assertEqual(PublicationStore(case.path).read(), acceptance)
                        current = case.registry.get(context_id)
                        case.assertEqual(current["state"] if fault == "uncertain" else
                                         current["retired_at"] if fault == "retired" else current["resumability"],
                                         "uncertain" if fault == "uncertain" else
                                         "retired during verification" if fault == "retired" else "unknown")
                        case.assertEqual((case.impl_prompts, len(case.prompts)), ([], 1))
                    finally:
                        case.doCleanups()

    def test_intervening_review_preserves_recovery_of_uncertain_original_i1(self):
        before = self.interrupt("implementation")
        first = self.standalone()
        self.registry.update(self.implementation, state="uncertain")
        result = loop("DEV-7")
        self.assertEqual(result.state, "clean", result.render())
        after = self.store.read()[0]
        self.assertEqual(after["run_id"], before["run_id"])
        self.assertEqual([r["pass_id"] for r in after["records"]],
                         [before["active_pass"]["pass_id"], first.pass_id])
        self.assertEqual(self.registry.get(self.implementation)["state"], "active")
        self.assertEqual(ReviewHandoffStore(self.path).read()["consumed"], before["run_id"])
        self.assertEqual((len(self.impl_prompts), len(self.prompts)), (1, 1))

    def test_standalone_blocker_and_limits_stop_without_implementation_prompt(self):
        self.review_results = [dict(state="blocked", summary="Needs a scope decision", findings=[])]
        self.assertEqual(review("DEV-7").state, "blocked")
        blocked = loop("DEV-7")
        self.assertEqual(blocked.state, "escalated", blocked.render())
        self.assertIn("Needs a scope decision", blocked.data["reason"])
        self.assertEqual(self.impl_prompts, [])
        case = AdoptionTests()
        case.setUp()
        try:
            case.standalone(loops.finding())
            limited = loop("DEV-7", max_reviews=1)
            case.assertEqual(limited.state, "escalated", limited.render())
            case.assertIn("limit", limited.data["reason"])
            case.assertEqual(case.impl_prompts, [])
        finally:
            case.doCleanups()

    def test_private_record_is_bounded_and_has_no_task_prompt_or_conversation(self):
        first = self.standalone(loops.finding())
        handoffs = ReviewHandoffStore(self.path)
        value = handoffs.read()
        self.assertLess(handoffs.path.stat().st_size, 1024 * 1024)
        self.assertEqual(handoffs.path.stat().st_mode & 0o777, 0o600)
        self.assertNotIn("Current requirements", handoffs.path.read_text())
        self.assertNotIn(self.prompts[0], handoffs.path.read_text())
        self.assertEqual(value["result"], first.as_dict())
        self.assertIn("STRUCTURED FINDINGS CONTRACT", self.prompts[0])
        self.assertNotIn("AUTOMATED LOOP CONTRACT", self.prompts[0])
        second = self.standalone()
        self.assertEqual(handoffs.read()["result"]["pass_id"], second.pass_id)
        self.assertNotIn(first.pass_id, handoffs.path.read_text())

    def test_unclaimed_existing_entry_checkpoint_restarts_once_and_preserves_pause(self):
        for from_review, pause, interrupted in ((False, False, False), (True, False, False), (False, True, True)):
            with self.subTest(from_review=from_review, pause=pause, interrupted=interrupted):
                case = AdoptionTests()
                case.setUp()
                try:
                    with patch("task_start.loop.drive", side_effect=KeyboardInterrupt), case.assertRaises(KeyboardInterrupt):
                        loop("DEV-7", from_review=from_review)
                    before = case.store.read()[0]
                    if interrupted:
                        before["status"] = "interrupted"
                        case.store.save(before)
                    if pause:
                        loop("DEV-7", action="pause")
                    result = loop("DEV-7")
                    if pause:
                        case.assertEqual(result.state, "paused", result.render())
                        case.assertEqual((case.impl_prompts, case.prompts), ([], []))
                        result = loop("DEV-7", action="continue")
                    case.assertEqual(result.state, "clean", result.render())
                    case.assertEqual(case.store.read()[0]["run_id"], before["run_id"])
                    case.assertEqual((len(case.impl_prompts), len(case.prompts)), (0 if from_review else 1, 1))
                finally:
                    case.doCleanups()

    def test_unclaimed_entry_still_refuses_drift_before_delivery(self):
        with patch("task_start.loop.drive", side_effect=KeyboardInterrupt), self.assertRaises(KeyboardInterrupt):
            loop("DEV-7")
        before = self.store.read()[0]
        (self.path / "changed.txt").write_text("out of band work")
        result = loop("DEV-7")
        self.assertEqual(result.state, "escalated", result.render())
        self.assertIn("Checkout/base changed", result.data["reason"])
        self.assertEqual(self.store.read()[0]["run_id"], before["run_id"])
        self.assertEqual((self.impl_prompts, self.prompts), ([], []))

    def test_competing_standalone_claim_cannot_silently_override_an_unclaimed_checkpoint(self):
        with patch("task_start.loop.drive", side_effect=KeyboardInterrupt), self.assertRaises(KeyboardInterrupt):
            loop("DEV-7")
        self.standalone(loops.finding())
        result = loop("DEV-7")
        self.assertEqual(result.state, "escalated", result.render())
        self.assertIn("conflicts with the saved loop boundary", result.data["reason"])
        self.assertEqual((self.impl_prompts, len(self.prompts)), ([], 1))


if __name__ == "__main__":
    unittest.main()
