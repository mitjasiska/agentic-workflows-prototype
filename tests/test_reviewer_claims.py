"""Abandoned review claims: real Git/registry/locks, controlled runtime evidence."""

import copy
import json
import subprocess
import sys
import unittest
from unittest.mock import patch
from uuid import uuid4

from task_start import TaskError
from task_start.agent import AgentOptions, Codex, Pi
from task_start.contexts import context_reference
from task_start.review import recover_reviewer_claim, review
from task_start.sessions import SessionInvalid
from task_start.workspace import Workspace
import test_review as fixture
import test_loop as loop_fixture


class ReviewerClaimTests(unittest.TestCase):
    setUp = fixture.ReviewTests.setUp
    command = fixture.ReviewTests.command
    pane_command = fixture.ReviewTests.pane_command
    worktrees = fixture.ReviewTests.worktrees
    select_adapter = fixture.ReviewTests.select_adapter

    def abandoned(self, *, missing=True):
        result = review("DEV-7")
        self.assertEqual(result.state, "clean", result.render())
        self.registry.claim_review(self.registry.get(result.context_id))
        context = self.registry.get(result.context_id)
        if missing:
            self.panes = [p for p in self.panes if p["pane_id"] != context["pane_id"]]
        return context

    def recover(self, context):
        workspace = Workspace(self.branch, self.path, "w1", "t1", "p1", "existing task tab", None)
        recover_reviewer_claim(context, fixture.ISSUE, workspace, self.repo, "/server.sock",
                               self.registry, self.identities)

    def test_missing_pane_releases_only_state_and_exact_resume_keeps_history(self):
        context = self.abandoned()
        with patch.object(self.adapter, "verify_review_session", wraps=self.adapter.verify_review_session) as stopped:
            self.recover(context)
        stopped.assert_called_once()
        self.assertEqual(stopped.call_args.args[1], context_reference(context))
        self.assertEqual(self.registry.get(context["context_id"]), dict(context, state="active"))
        result = review("DEV-7", resume=context["context_id"])
        self.assertEqual((result.state, result.context_id), ("clean", context["context_id"]))
        after = self.registry.get(context["context_id"])
        for key in ("ordinal", "allocated_at", "session_id", "session_kind", "herdr_session", "resumability", "model", "mode"):
            self.assertEqual(after[key], context[key])
        self.assertEqual(self.recreated, [True])
        self.assertEqual(review("DEV-7").context_id, "DEV-7-R2")

    def test_explicit_resume_recovers_stopped_claim_before_exact_selection(self):
        context = self.abandoned()
        result = review("DEV-7", resume=context["context_id"])
        self.assertEqual((result.state, result.context_id), ("clean", context["context_id"]))
        self.assertEqual(len(self.registry.list()), 2)

    def test_provider_failure_or_changed_identity_keeps_claim_and_resumability(self):
        context = self.abandoned()
        for failure in (TaskError("pending input"), TaskError("turn still running"),
                        TaskError("provider unavailable"), SessionInvalid("history replaced"), OSError("transport lost")):
            with self.subTest(failure=str(failure)), patch.object(self.adapter, "verify_review_session", side_effect=failure):
                with self.assertRaisesRegex(TaskError, "claim cannot be recovered safely.*claim retained"):
                    self.recover(context)
            self.assertEqual(self.registry.get(context["context_id"]), context)
        with patch.object(self.adapter, "verify_review_session", return_value=dict(agent="codex", kind="id", value="replacement")):
            with self.assertRaisesRegex(TaskError, "provider session identity changed"):
                self.recover(context)
        self.assertEqual(self.registry.get(context["context_id"]), context)

    def test_missing_pane_with_session_elsewhere_or_unknown_reporting_refuses(self):
        context = self.abandoned()
        for changes in (dict(agent_session=context_reference(context)), dict(agent_session=None),
                        dict(agent_session={"malformed": True}),
                        dict(agent=None, agent_session=context_reference(context)),
                        dict(agent=None, agent_session=context["session_id"], agent_status="working"),
                        dict(agent="pi", agent_session=context["session_id"], agent_status="working"),
                        dict(agent=None, agent_session="", agent_status="working")):
            other = dict(self.panes[0], pane_id="elsewhere", terminal_id="elsewhere-terminal",
                         workspace_id="other-workspace", agent="codex")
            other.update(changes)
            self.panes.append(other)
            with self.subTest(changes=changes), self.assertRaisesRegex(TaskError, "claim cannot be recovered safely"):
                self.recover(context)
            self.assertEqual(self.registry.get(context["context_id"]), context)
            self.panes.pop()

    def test_distinct_legacy_session_elsewhere_does_not_block_recovery(self):
        context = self.abandoned()
        self.panes.append(dict(self.panes[0], pane_id="elsewhere", terminal_id="elsewhere-terminal",
            workspace_id="other-workspace", agent=None, agent_session="different-session", agent_status="working"))
        self.recover(context)
        self.assertEqual(self.registry.get(context["context_id"]), dict(context, state="active"))

    def test_legacy_session_appearing_during_provider_check_keeps_claim(self):
        context = self.abandoned()
        def appear(workspace, reference):
            self.panes.append(dict(self.panes[0], pane_id="competing", terminal_id="competing-terminal",
                agent=None, agent_session=reference["value"], agent_status="working"))
            return reference
        with patch.object(self.adapter, "verify_review_session", side_effect=appear):
            with self.assertRaisesRegex(TaskError, "session is reported in another pane"):
                self.recover(context)
        self.assertEqual(self.registry.get(context["context_id"]), context)

    def test_busy_unknown_blocked_or_launch_pending_runtime_keeps_claim(self):
        context = self.abandoned(missing=False)
        for status in ("working", "blocked", "unknown"):
            self.status = status
            with self.subTest(status=status), self.assertRaisesRegex(TaskError, "not confirmed idle"):
                self.recover(context)
            self.assertEqual(self.registry.get(context["context_id"]), context)
        self.status = "done"
        for changes in (dict(agent_status="working"), dict(agent_status="blocked"), dict(launch_pending=True),
                        dict(agent_session=None)):
            original = copy.deepcopy(self.panes[-1])
            self.panes[-1].update(changes)
            with self.subTest(changes=changes), self.assertRaises(TaskError):
                self.recover(context)
            self.panes[-1] = original
            self.assertEqual(self.registry.get(context["context_id"]), context)

    def test_idle_relocated_terminal_recovers_without_rewriting_binding(self):
        context = self.abandoned(missing=False)
        self.panes[-1].update(pane_id="relocated", tab_id="new-tab", label="human label")
        self.recover(context)
        self.assertEqual(self.registry.get(context["context_id"]), dict(context, state="active"))
        self.assertEqual(review("DEV-7", resume=context["context_id"]).state, "clean")
        self.assertEqual(self.registry.get(context["context_id"])["pane_id"], "relocated")

    def test_replaced_ambiguous_or_wrong_checkout_runtime_refuses(self):
        context = self.abandoned(missing=False)
        original = copy.deepcopy(self.panes)
        for fault in ("terminal", "workspace", "session", "cwd", "duplicate", "endpoint", "unavailable"):
            self.panes = copy.deepcopy(original)
            with self.subTest(fault=fault):
                if fault in {"terminal", "workspace"}:
                    self.panes[-1][fault + "_id"] = "replacement"
                elif fault == "session":
                    self.panes[-1]["agent_session"] = dict(agent="codex", kind="id", value="other")
                elif fault == "cwd":
                    self.panes[-1]["cwd"] = str(self.repo)
                elif fault == "duplicate":
                    self.panes.append(dict(self.panes[-1], pane_id="duplicate"))
                with patch.object(self.identities, "endpoint", return_value="other" if fault == "endpoint" else "/server.sock"):
                    if fault == "unavailable":
                        with patch.object(self.identities, "snapshot", side_effect=TaskError("Herdr unavailable")):
                            with self.assertRaises(TaskError):
                                self.recover(context)
                    else:
                        with self.assertRaises(TaskError):
                            self.recover(context)
                self.assertEqual(self.registry.get(context["context_id"]), context)

    def test_rechecks_runtime_after_provider_read_and_cas_refuses_registry_drift(self):
        context = self.abandoned()
        def reappear(workspace, reference):
            self.panes.append(dict(self.panes[0], pane_id=context["pane_id"], terminal_id=context["terminal_id"],
                agent="codex", agent_status="working", agent_session=reference, label=context["context_id"]))
            return reference
        with patch.object(self.adapter, "verify_review_session", side_effect=reappear):
            with self.assertRaisesRegex(TaskError, "still be busy"):
                self.recover(context)
        self.assertEqual(self.registry.get(context["context_id"]), context)
        self.panes.pop()
        def drift(workspace, reference):
            self.registry.update(context["context_id"], state="uncertain")
            return reference
        with patch.object(self.adapter, "verify_review_session", side_effect=drift):
            with self.assertRaisesRegex(TaskError, "claim changed"):
                self.recover(context)
        self.assertEqual(self.registry.get(context["context_id"]), dict(context, state="uncertain"))

    def test_unverified_or_conflicting_retained_identity_never_releases(self):
        context = self.abandoned()
        for values in (dict(resumability="unknown"), dict(session_id=None, session_kind=None, herdr_session=None),
                       dict(herdr_session='{"agent":"codex","kind":"id","value":"other"}')):
            self.registry.update(context["context_id"], **values)
            before = self.registry.get(context["context_id"])
            with self.subTest(values=values), self.assertRaises(TaskError):
                self.recover(before)
            self.assertEqual(self.registry.get(context["context_id"]), before)
            self.registry.update(context["context_id"], **{key: context[key] for key in values})

    def test_stopped_shell_requires_process_family_proof(self):
        context = self.abandoned(missing=False)
        self.panes[-1].update(agent=None, agent_session=None, agent_status="unknown")
        info = dict(pane_id=context["pane_id"], shell_pid=123, foreground_process_group_id=123,
                    foreground_processes=[dict(pid=123, argv=["bash"])])
        with patch.object(self.identities, "command", return_value=dict(process_info=info)), \
                patch("task_start.integration_process.stopped_execution", side_effect=TaskError("child still running")) as stopped:
            with self.assertRaisesRegex(TaskError, "child still running"):
                self.recover(context)
            self.assertEqual(self.registry.get(context["context_id"]), context)
            stopped.side_effect = None
            self.recover(context)
            stopped.assert_called_with([], {123: dict(argv=["bash"], cwd=self.path)})
        self.assertEqual(self.registry.get(context["context_id"]), dict(context, state="active"))

    def test_other_controller_lock_refuses_before_runtime_observation(self):
        context = self.abandoned()
        code = """from pathlib import Path
import sys
from task_start.publication_state import PublicationStore
with PublicationStore(Path(sys.argv[1])).locked(registry=Path(sys.argv[2])):
    print('locked', flush=True)
    sys.stdin.readline()
"""
        process = subprocess.Popen([sys.executable, "-c", code, str(self.path), str(self.registry.path)],
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            self.assertEqual(process.stdout.readline().strip(), "locked")
            with patch.object(self.adapter, "verify_review_session") as stopped:
                with self.assertRaisesRegex(TaskError, "Another controller"):
                    self.recover(context)
                stopped.assert_not_called()
            self.assertEqual(self.registry.get(context["context_id"]), context)
        finally:
            process.communicate("done\n", timeout=10)
        self.assertEqual(process.returncode, 0)
        self.recover(context)
        self.assertEqual(self.registry.get(context["context_id"]), dict(context, state="active"))

    def test_new_review_refuses_live_historical_claim_before_allocation(self):
        context = self.abandoned(missing=False)
        self.status = "working"
        with self.assertRaisesRegex(TaskError, "not confirmed idle"):
            review("DEV-7")
        self.assertEqual(len(self.registry.list()), 2)
        self.assertEqual(len(self.prompts), 1)
        self.assertEqual(self.registry.get(context["context_id"]), context)

    def test_finalization_failure_or_interruption_leaves_recoverable_claim(self):
        for error in (TaskError("finalization unavailable"), OSError("observation failed"), KeyboardInterrupt()):
            with self.subTest(error=type(error).__name__):
                with patch("task_start.review.finalize_reviewer", side_effect=error):
                    if isinstance(error, TaskError):
                        self.assertEqual(review("DEV-7").state, "failed")
                    else:
                        with self.assertRaises(type(error)):
                            review("DEV-7")
                context = [c for c in self.registry.list() if c["role"] == "review"][-1]
                self.assertEqual(context["state"], "reviewing")
                self.panes = [p for p in self.panes if p["pane_id"] != context["pane_id"]]
                self.recover(context)
                self.assertEqual(self.registry.get(context["context_id"]), dict(context, state="active"))

    def test_missing_pane_native_codex_proof_requires_idle_thread_turn_and_queue(self):
        context = self.abandoned()
        reference = dict(agent="codex", kind="id", value=str(uuid4()))
        self.registry.update(context["context_id"], session_id=reference["value"], herdr_session=json.dumps(reference))
        context = self.registry.get(context["context_id"])
        adapter = Codex(AgentOptions("codex", "review-model", "high"))
        for scenario in ("completed", "interrupted", "failed", "active", "working", "queue", "partial", "wrong_thread"):
            with self.subTest(scenario=scenario), patch("task_start.review.adapter_for", return_value=adapter), \
                    patch("task_start.agent.CodexRPC") as rpc:
                thread = dict(id=reference["value"], cwd=str(self.path), status=dict(type="idle"))
                page = dict(data=[dict(status="completed", itemsView="full")], nextCursor=None)
                queue = dict(data=[], nextCursor=None)
                if scenario in {"completed", "interrupted", "failed"}:
                    page["data"][0]["status"] = scenario
                elif scenario == "active":
                    thread["status"] = dict(type="active")
                elif scenario == "working":
                    page["data"][0]["status"] = "inProgress"
                elif scenario == "queue":
                    queue["data"] = [dict(id="pending")]
                elif scenario == "partial":
                    page["data"][0]["itemsView"] = "summary"
                else:
                    thread["id"] = str(uuid4())
                request = rpc.return_value.__enter__.return_value.request
                request.side_effect = lambda method, params: {
                    "thread/read": dict(thread=thread), "thread/turns/list": page, "thread/queue/list": queue}[method]
                if scenario in {"completed", "interrupted", "failed"}:
                    self.recover(context)
                    self.assertEqual(self.registry.get(context["context_id"]), dict(context, state="active"))
                    self.registry.claim_review(self.registry.get(context["context_id"]))
                else:
                    with self.assertRaisesRegex(TaskError, "claim cannot be recovered safely"):
                        self.recover(context)
                    self.assertEqual(self.registry.get(context["context_id"]), context)
                self.assertTrue(all(call.args[0] in {"thread/read", "thread/turns/list", "thread/queue/list"}
                                    for call in request.call_args_list))

    def test_missing_pane_native_pi_proof_requires_exact_terminal_history(self):
        path = self.repo.parent / "reviewer.jsonl"
        reference = dict(agent="pi", kind="path", value=str(path), conversation_id="original")
        context_id = self.registry.allocate("DEV-7", "review", agent="pi", model="review-model", mode="high",
            repository=str(self.repo), worktree=str(self.path), endpoint="/server.sock", workspace_id="w1",
            tab_id="t1", pane_id="missing", terminal_id="missing-terminal")
        self.registry.update(context_id, state="reviewing", session_id=str(path), session_kind="path",
                             resumability="yes", herdr_session=json.dumps(reference))
        context = self.registry.get(context_id)
        header = dict(type="session", id="original", cwd=str(self.path))
        user = dict(type="message", id="1", parentId=None, message=dict(role="user", content="review"))
        for scenario in ("stop", "aborted", "error", "pending", "tool", "replaced", "partial"):
            with self.subTest(scenario=scenario), patch("task_start.review.adapter_for", return_value=Pi(AgentOptions("pi"))):
                entries = [header, user, dict(type="message", id="2", parentId="1",
                    message=dict(role="assistant", content="finished", stopReason=scenario))]
                if scenario == "pending":
                    entries = [header, user]
                elif scenario == "tool":
                    entries[-1]["message"] = dict(role="toolResult")
                elif scenario == "replaced":
                    entries[0] = dict(header, id="replacement")
                path.write_text("".join(json.dumps(entry) + "\n" for entry in entries)
                                + ('{"partial":' if scenario == "partial" else ""))
                if scenario in {"stop", "aborted", "error"}:
                    self.recover(context)
                    self.assertEqual(self.registry.get(context_id), dict(context, state="active"))
                    self.registry.claim_review(self.registry.get(context_id))
                else:
                    with self.assertRaisesRegex(TaskError, "claim cannot be recovered safely"):
                        self.recover(context)
                    self.assertEqual(self.registry.get(context_id), context)


class LoopReviewerClaimTests(unittest.TestCase):
    setUp = loop_fixture.LoopIntegrationTests.setUp
    command = loop_fixture.LoopIntegrationTests.command
    pane_command = loop_fixture.LoopIntegrationTests.pane_command
    worktrees = loop_fixture.LoopIntegrationTests.worktrees
    select_adapter = loop_fixture.LoopIntegrationTests.select_adapter
    run_loop = loop_fixture.LoopIntegrationTests.run_loop

    def historical_reviewers(self, *, findings=False):
        first = review("DEV-7", model="gpt-6.1-sol")
        original = self.registry.get(first.context_id)
        self.registry.claim_review(original)
        self.panes = [p for p in self.panes if p["pane_id"] != original["pane_id"]]
        # Reproduce history left by older code: a newer successful independent
        # reviewer coexists with R1's abandoned claim and missing terminal.
        if findings:
            self.review_results = [loop_fixture.findings(loop_fixture.finding())]
        with patch("task_start.review.recover_reviewer_claim"):
            second = review("DEV-7", model="gpt-6-astra")
        self.assertEqual((first.context_id, second.context_id), ("DEV-7-R1", "DEV-7-R2"))
        return original, self.registry.get(second.context_id)

    def test_dev64_old_r1_missing_pane_and_newer_idle_r2_allows_later_loop(self):
        original, second = self.historical_reviewers()
        result = self.run_loop()
        self.assertEqual(result.state, "clean", result.render())
        self.assertEqual(self.registry.get(original["context_id"]), original)
        self.assertEqual(self.registry.get(second["context_id"]), second)
        self.assertEqual([c["ordinal"] for c in self.registry.list() if c["role"] == "review"], [1, 2])
        self.assertEqual(len(self.impl_prompts), 0)  # Existing clean-review adoption is unchanged.
        self.assertEqual(self.store.read()[0]["reviewer"]["context_id"], "DEV-7-R2")

    def test_recovered_history_allows_fixes_in_original_implementation(self):
        original, second = self.historical_reviewers(findings=True)
        result = self.run_loop()
        self.assertEqual(result.state, "clean", result.render())
        self.assertEqual(self.registry.get(original["context_id"]), original)
        self.assertEqual(self.registry.get(second["context_id"]), second)
        self.assertEqual([c["ordinal"] for c in self.registry.list() if c["role"] == "review"], [1, 2])
        self.assertEqual(len(self.impl_prompts), 1)
        self.assertEqual(self.store.read()[0]["reviewer"]["context_id"], "DEV-7-R2")

    def test_old_missing_claim_with_busy_provider_stops_all_handoffs(self):
        original, second = self.historical_reviewers()
        with patch.object(self.adapter, "cleanup_provider_idle", side_effect=TaskError("turn still running")):
            with self.assertRaisesRegex(TaskError, "turn still running"):
                self.run_loop()
        self.assertEqual(self.registry.get(original["context_id"]), dict(original, state="reviewing"))
        self.assertEqual(self.registry.get(second["context_id"]), second)
        self.assertEqual((len(self.impl_prompts), len(self.prompts)), (0, 2))

    def test_busy_legacy_session_elsewhere_prevents_implementation(self):
        original, second = self.historical_reviewers(findings=True)
        before = self.registry.list()
        competing = dict(self.panes[0], pane_id="competing", terminal_id="competing-terminal",
            workspace_id="other-workspace", agent=None, agent_session=original["session_id"], agent_status="working")
        self.panes.append(competing)
        for omit_agent in (False, True):
            with self.subTest(omit_agent=omit_agent):
                if omit_agent:
                    competing.pop("agent")
                with self.assertRaisesRegex(TaskError, "session is reported in another pane"):
                    self.run_loop()
                self.assertEqual([c["context_id"] for c in self.registry.list()], [c["context_id"] for c in before])
                self.assertEqual(self.registry.get(original["context_id"]), dict(original, state="reviewing"))
                self.assertEqual(self.registry.get(second["context_id"]), second)
                self.assertEqual((len(self.impl_prompts), len(self.prompts)), (0, 2))
                self.assertFalse(self.store.path.exists())

    def test_recovering_old_claim_does_not_bypass_newer_busy_reviewer(self):
        original, second = self.historical_reviewers()
        self.status = "working"
        with self.assertRaisesRegex(TaskError, "not confirmed idle"):
            self.run_loop()
        self.assertEqual(self.registry.get(original["context_id"]), original)
        self.assertEqual(self.registry.get(second["context_id"]), second)
        self.assertEqual((len(self.impl_prompts), len(self.prompts)), (0, 2))


if __name__ == "__main__":
    unittest.main()
