"""Explicit abandonment of a sessionless stale G context; no implicit replay."""

import copy
from concurrent.futures import ThreadPoolExecutor
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from task_start import TaskError, cli
from task_start.agent import AgentOptions, Codex
from task_start.codex_rpc import validate_readiness_only
from task_start.contexts import inspect_contexts
from task_start.integrate import abandon_integration, abandonment_runtime, integrate
from task_start.integration_process import stopped_checkout
from task_start.integration_state import IntegrationStore
from task_start.ownership import ownership_gate
from task_start.publish import publish
from task_start.review import review
import test_integrate as integration


class ReadinessProvider:
    """Codex 0.157.1 structured shape for the stranded pre-handoff startup turn."""
    def __init__(self, checkout):
        marker = ("Handoff readiness 00000000-0000-4000-8000-000000000001. "
                  "Do not use tools or modify files. Reply READY, then wait for the task prompt.")
        self.thread = dict(id="00000000-0000-4000-8000-000000000002", cwd=str(checkout), preview=marker,
                           source="cli", parentThreadId=None, forkedFromId=None, status=dict(type="notLoaded"))
        self.turn = dict(id="00000000-0000-4000-8000-000000000003", status="completed", error=None, itemsView="full",
                         items=[dict(type="userMessage", id="user", clientId="native",
                                     content=[dict(type="text", text=marker, text_elements=[])]),
                                dict(type="agentMessage", id="agent", text="READY", phase="final_answer")])
        self.threads, self.archived = [self.thread], []
        self.turns, self.queue = dict(data=[self.turn], nextCursor=None), dict(data=[], nextCursor=None)
        self.list_cursor = None
        self.calls = []

    def request(self, method, params, **kwargs):
        self.calls.append((method, params))
        if method == "thread/list":
            return copy.deepcopy(dict(data=self.archived if params["archived"] else self.threads, nextCursor=self.list_cursor))
        if params["threadId"] != self.thread["id"]:
            raise AssertionError("Changed provider identity")
        if method == "thread/read":
            return copy.deepcopy(dict(thread=self.thread))
        if method == "thread/turns/list":
            assert params["itemsView"] == "full"
            return copy.deepcopy(self.turns)
        if method == "thread/queue/list":
            return copy.deepcopy(self.queue)
        raise AssertionError("Unexpected mutating provider operation " + method)


class AbandonmentTests(unittest.TestCase):
    setUp = integration.IntegrationTests.setUp
    command = integration.IntegrationTests.command
    worktrees = integration.IntegrationTests.worktrees
    select_adapter = integration.IntegrationTests.select_adapter
    prepare = integration.IntegrationTests.prepare
    api = integration.IntegrationTests.api
    operations = integration.IntegrationTests.operations
    advance_remote = integration.IntegrationTests.advance_remote
    pane_command = integration.IntegrationTests.pane_command
    prepare_integration = integration.IntegrationTests.prepare_integration
    assert_untouched = integration.IntegrationTests.assert_untouched

    def prepare_stale(self, *, missing=False):
        self.prepare_integration()
        self.advance_remote(conflict=True)
        def interrupted(execution):
            self.integrator.execution = execution
            self.integrator.launches += 1
            raise TaskError("launch acknowledgement unavailable")
        with patch.object(self.integrator, "launch", side_effect=interrupted), \
                self.assertRaisesRegex(TaskError, "acknowledgement"):
            integrate("DEV-7", agent_kind="codex", model="integration-model", mode="high")
        self.record = self.integration_store.read()
        self.context = self.registry.get("DEV-7-G1")
        self.assertEqual(self.record["state"], "uncertain")
        self.assertEqual(self.context["state"], "uncertain")
        self.assertEqual(self.context["resumability"], "unknown")
        self.assertIsNone(self.context["session_id"])
        self.path_g1 = Path(self.context["worktree"])
        self.index_g1 = (self.path_g1 / ".git" / "index").read_bytes()
        self.conflict_g1 = (self.path_g1 / "tracked.txt").read_bytes()
        self.assertTrue(self.command(self.path_g1, "ls-files", "--unmerged"))
        self.assertFalse(Path(self.record["output"]).exists())
        self.pane = next(p for p in self.panes if p["pane_id"] == self.context["pane_id"])
        if missing:
            self.panes.remove(self.pane)
        self.codex = Codex(AgentOptions(**self.record["options"]))
        self.process_info = dict(pane_id=self.pane["pane_id"], shell_pid=123,
            foreground_process_group_id=123, foreground_processes=[dict(pid=123)])
        self.transport = self.enterContext(patch.object(self.codex, "command",
            side_effect=lambda *args, **kwargs: dict(process_info=copy.deepcopy(self.process_info))))
        self.process = self.enterContext(patch("task_start.agent.stopped_checkout",
            side_effect=lambda path, shell: dict(pid=shell, started=7 if shell else None)))
        self.rpc = self.enterContext(patch("task_start.agent.CodexRPC")).return_value.__enter__.return_value
        self.rpc.request.return_value = dict(data=[], nextCursor=None)

    def abandon(self):
        with patch("task_start.integrate.adapter_for", return_value=self.codex):
            return abandon_integration("DEV-7", "DEV-7-G1")

    def assert_retained(self):
        self.assertEqual((self.path_g1 / ".git" / "index").read_bytes(), self.index_g1)
        self.assertEqual((self.path_g1 / "tracked.txt").read_bytes(), self.conflict_g1)
        self.assertTrue(Path(self.record["output"]).parent.is_dir())

    def test_stale_g1_requires_explicit_abandonment_then_launches_g2(self):
        self.prepare_stale()
        for action in (integrate, review, publish):
            with self.assertRaisesRegex(TaskError, "[Ii]ntegration"):
                action("DEV-7")
        self.assert_untouched()
        self.assertIn("Abandoned DEV-7-G1", self.abandon())
        abandoned = self.integration_store.read()
        retained = self.registry.get("DEV-7-G1")
        self.assertEqual(retained, dict(self.context, state="abandoned", retired_at=abandoned["abandonment"]["at"]))
        self.assertNotIn(retained, self.registry.list("DEV-7"))
        self.assertIn(retained, self.registry.list("DEV-7", include_retired=True))
        self.assertIn("abandoned", inspect_contexts("DEV-7", include_retired=True,
                                                   registry=self.registry, herdr=self.identities))
        self.assertEqual(self.integrator.launches, 1)
        self.assert_untouched()
        self.assert_retained()
        self.assertIn("already abandoned", self.abandon())
        self.assertIn("independent task review", integrate("DEV-7"))
        self.assertEqual(self.integrator.launches, 2)
        self.assertEqual(self.integration_store.read()["context"]["context_id"], "DEV-7-G2")
        self.assertNotEqual(self.registry.get("DEV-7-G2")["worktree"], str(self.path_g1))
        self.assertEqual(self.registry.get("DEV-7-G1"), retained)
        self.assert_retained()
        archive = self.integration_store.directory / "agentic-workflows-integration-DEV-7-G1.json"
        self.assertEqual(json.loads(archive.read_text()), abandoned)
        self.assertIsNone(self.store.read()["acceptance"])
        self.assertFalse(self.operations("push"))

    def test_missing_pane_still_requires_negative_provider_and_process_evidence(self):
        self.prepare_stale(missing=True)
        self.abandon()
        self.process.assert_any_call(self.path_g1, None)
        self.transport.assert_not_called()
        calls = self.rpc.request.call_args_list
        self.assertEqual([c.args[1]["archived"] for c in calls], [False, True])
        for call in calls:
            self.assertEqual(call.args[0], "thread/list")
            self.assertEqual(call.args[1]["cwd"], str(self.path_g1))
            self.assertEqual(call.args[1]["modelProviders"], [])
            self.assertIn("subAgent", call.args[1]["sourceKinds"])
        self.assert_untouched()
        self.assert_retained()

    def make_legacy(self):
        legacy = {k: v for k, v in self.record.items() if k not in {"output", "slice", "isolated_index"}}
        self.integration_store.write(legacy)
        provider = ReadinessProvider(self.path_g1)
        self.rpc.request.side_effect = provider.request
        return legacy, provider

    def test_legacy_idle_codex_requires_human_quit_then_proven_startup_abandonment_and_g2(self):
        self.prepare_stale()
        legacy, provider = self.make_legacy()
        self.pane.update(agent="codex", agent_status="idle", agent_session=None)
        self.process_info.update(foreground_process_group_id=456,
                                 foreground_processes=[dict(pid=456, name="codex", cwd=str(self.path_g1))])
        with self.assertRaisesRegex(TaskError, "idle agent is not an idle shell.*quit the agent"):
            self.abandon()
        self.rpc.request.assert_not_called()
        self.assertEqual(self.integration_store.read(), legacy)
        self.assertEqual(self.registry.get("DEV-7-G1"), self.context)
        self.assert_untouched()
        self.assert_retained()

        # Human quits Codex in the original pane. Its completed readiness
        # conversation remains on disk; neither it nor the G1 evidence is deleted.
        self.pane.update(agent=None, agent_status="idle")
        self.process_info.update(foreground_process_group_id=123, foreground_processes=[dict(pid=123)])
        self.abandon()
        proof = self.integration_store.read()["abandonment"]["proof"]
        self.assertEqual(proof["history"], "readiness_only")
        self.assertEqual(proof["startup"]["thread"]["id"], provider.thread["id"])
        self.assertEqual(proof["startup"]["turn"], provider.turn)
        self.assertEqual(self.integrator.launches, 1)
        self.assert_untouched()
        self.assert_retained()
        retained = self.registry.get("DEV-7-G1")
        self.assertEqual(retained["state"], "abandoned")
        self.assertIn("independent task review", integrate("DEV-7"))
        self.assertEqual(self.integration_store.read()["context"]["context_id"], "DEV-7-G2")
        self.assertEqual(self.registry.get("DEV-7-G1"), retained)
        self.assertIsNone(self.store.read()["acceptance"])
        self.assert_retained()

    def test_legacy_queued_handoff_never_abandons_or_replays(self):
        self.prepare_stale()
        legacy, provider = self.make_legacy()
        provider.queue["data"] = [dict(id="pending-task-input")]
        with self.assertRaisesRegex(TaskError, "not provably readiness-only"):
            self.abandon()
        with self.assertRaisesRegex(TaskError, "No replay"):
            integrate("DEV-7")
        self.assertEqual(self.integration_store.read(), legacy)
        self.assertEqual(self.registry.get("DEV-7-G1"), self.context)
        self.assertEqual(self.integrator.launches, 1)
        self.assert_untouched()
        self.assert_retained()

    def test_legacy_readiness_history_requires_original_shell_to_remain_observable(self):
        self.prepare_stale(missing=True)
        legacy, _ = self.make_legacy()
        with self.assertRaisesRegex(TaskError, "history may be resumable"):
            self.abandon()
        self.assertEqual(self.integration_store.read(), legacy)
        self.assert_untouched()

    def test_unsafe_or_unavailable_observations_preserve_the_uncertain_attempt(self):
        faults = ("session", "resumable", "live", "terminal", "endpoint", "foreground", "background",
                  "provider", "archived", "pagination", "malformed", "unavailable", "process_drift",
                  "output", "completion", "resolved", "registry_drift")
        for fault in faults:
            with self.subTest(fault=fault), AbandonmentTests() as case:
                case.prepare_stale()
                if fault == "session":
                    case.registry.update("DEV-7-G1", session_id="known-session", session_kind="id")
                elif fault == "resumable":
                    case.registry.update("DEV-7-G1", resumability="yes")
                elif fault == "live":
                    case.pane.update(agent="codex", agent_status="working")
                elif fault == "terminal":
                    case.pane["terminal_id"] = "replacement"
                elif fault == "endpoint":
                    case.identities.endpoint.return_value = "/another.sock"
                elif fault == "foreground":
                    case.process_info["foreground_processes"].append(dict(pid=999))
                elif fault == "background":
                    case.process.side_effect = TaskError("background execution")
                elif fault == "provider":
                    case.rpc.request.return_value = dict(data=[dict(id="unrecorded-session")], nextCursor=None)
                elif fault == "archived":
                    case.rpc.request.side_effect = [dict(data=[], nextCursor=None), dict(data=[{}], nextCursor=None)]
                elif fault == "pagination":
                    case.rpc.request.return_value = dict(data=[], nextCursor="more")
                elif fault == "malformed":
                    case.rpc.request.return_value = dict(data=[])
                elif fault == "unavailable":
                    case.rpc.request.side_effect = TaskError("provider unavailable")
                elif fault == "process_drift":
                    case.process.side_effect = [dict(pid=123, started=7), dict(pid=123, started=8)]
                elif fault == "output":
                    Path(case.record["output"]).write_text("{}")
                elif fault == "completion":
                    case.record["completion"] = dict(state="completed")
                    case.integration_store.write(case.record)
                elif fault == "resolved":
                    (case.path_g1 / "tracked.txt").write_text("resolution\n")
                    case.command(case.path_g1, "add", "--all")
                elif fault == "registry_drift":
                    def drift(*args, **kwargs):
                        case.registry.update("DEV-7-G1", resumability="yes")
                        return dict(data=[], nextCursor=None)
                    case.rpc.request.side_effect = drift
                with case.assertRaises(TaskError):
                    case.abandon()
                case.assertEqual(case.integration_store.read(), case.record)
                case.assertIsNone(case.registry.get("DEV-7-G1")["retired_at"])
                case.assertEqual(case.integrator.launches, 1)
                case.assert_untouched()

    def __enter__(self):
        self.setUp()
        return self

    def __exit__(self, *exc):
        self.doCleanups()

    def test_interrupted_disposition_requires_explicit_reconciliation(self):
        self.prepare_stale()
        with patch.object(self.registry, "abandon_integration", side_effect=KeyboardInterrupt()), self.assertRaises(KeyboardInterrupt):
            self.abandon()
        self.assertEqual(self.integration_store.read()["state"], "abandoned")
        self.assertEqual(self.registry.get("DEV-7-G1"), self.context)
        for action in (integrate, review, publish):
            with self.assertRaisesRegex(TaskError, "abandonment is incomplete"):
                action("DEV-7")
        self.abandon()
        self.assertEqual(self.registry.get("DEV-7-G1")["state"], "abandoned")
        self.assert_untouched()
        self.assert_retained()

    def test_interrupted_archive_write_gates_retry_until_explicit_reconciliation(self):
        self.prepare_stale()
        with patch.object(IntegrationStore, "archive", side_effect=TaskError("archive unavailable")), \
                self.assertRaisesRegex(TaskError, "archive unavailable"):
            self.abandon()
        with self.assertRaisesRegex(TaskError, "abandonment is incomplete"):
            integrate("DEV-7")
        self.assertEqual(self.registry.get("DEV-7-G1"), self.context)
        self.abandon()
        self.assertEqual(self.registry.get("DEV-7-G1")["state"], "abandoned")
        self.assert_untouched()

    def test_missing_controller_record_is_not_permission_to_restart(self):
        self.prepare_stale()
        self.abandon()
        self.integration_store.path.unlink()
        for action in (integrate, review, publish):
            with self.assertRaisesRegex(TaskError, "[Ii]ntegration"):
                action("DEV-7")
        with self.assertRaises(TaskError):
            self.abandon()
        self.assertEqual(self.integrator.launches, 1)
        self.assert_untouched()

    def test_missing_archive_never_authorizes_a_fresh_attempt(self):
        self.prepare_stale()
        self.abandon()
        (self.integration_store.directory / "agentic-workflows-integration-DEV-7-G1.json").unlink()
        for action in (integrate, review, publish):
            with self.assertRaisesRegex(TaskError, "archive is missing"):
                action("DEV-7")
        self.assertEqual(self.integrator.launches, 1)
        self.assert_untouched()

    def test_abandonment_uses_task_lock_and_exact_selector(self):
        self.prepare_stale()
        with self.store.locked(), self.assertRaisesRegex(TaskError, "owns this worktree"):
            self.abandon()
        for selector in ("DEV-8-G1", "DEV-7-I1", "DEV-7-G2", "../DEV-7-G1"):
            with self.subTest(selector=selector), self.assertRaises(TaskError):
                abandon_integration("DEV-7", selector)
        self.assertEqual(self.integration_store.read(), self.record)
        self.assert_untouched()

    def test_cli_abandon_launches_nothing_and_rejects_overrides(self):
        with patch("task_start.cli.abandon_integration", return_value="abandoned") as abandon, \
                patch("task_start.cli.integrate") as launch, patch("sys.stdout", new_callable=io.StringIO):
            self.assertEqual(cli.main(["integrate", "DEV-7", "--abandon", "DEV-7-G1"]), 0)
        abandon.assert_called_once_with("DEV-7", "DEV-7-G1")
        launch.assert_not_called()
        for flag, value in (("--agent", "codex"), ("--model", "m"), ("--mode", "high"), ("--timeout", "1")):
            with patch("task_start.cli.abandon_integration") as abandon, patch("sys.stderr", new_callable=io.StringIO):
                self.assertEqual(cli.main(["integrate", "DEV-7", "--abandon", "DEV-7-G1", flag, value]), 1)
                abandon.assert_not_called()


    def test_valid_abandonment_waits_for_short_registry_transaction(self):
        import threading
        self.prepare_stale()
        entered = threading.Event()
        def observed_configuration():
            entered.set()
            return self.local
        with patch("task_start.integrate.load_local", side_effect=observed_configuration), \
                ThreadPoolExecutor(max_workers=1) as pool:
            with ownership_gate(exclusive=True):
                attempt = pool.submit(self.abandon)
                self.assertTrue(entered.wait(5))
                self.assertFalse(attempt.done())
                self.assertEqual(self.integration_store.read()["state"], "uncertain")
            self.assertIn("Abandoned DEV-7-G1", attempt.result(timeout=10))
        self.assert_retained()


class RecoveryContractTests(unittest.TestCase):
    def setUp(self):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        self.registry_path = Path(directory) / "workflow" / "contexts.sqlite3"
        self.enterContext(patch("task_start.contexts.registry_path", return_value=self.registry_path))

    def test_runtime_observation_requires_integer_process_identity(self):
        context = dict(endpoint="/server.sock", pane_id="p1", terminal_id="t1", tab_id="tab1",
                       workspace_id="w1", agent="codex", worktree="/isolated")
        pane = dict(context, cwd="/isolated", agent=None, agent_session=None, agent_status="idle")
        identities = Mock()
        identities.endpoint.return_value = context["endpoint"]
        identities.snapshot.return_value = [pane]
        valid = dict(pane_id="p1", shell_pid=1, foreground_process_group_id=1, foreground_processes=[dict(pid=1)])
        adapter = Mock()
        adapter.command.return_value = dict(process_info=valid)
        self.assertEqual(abandonment_runtime(context, identities, adapter)["shell_pid"], 1)
        for change in (dict(shell_pid=True), dict(foreground_process_group_id=True),
                       dict(foreground_processes=[dict(pid=True)]), dict(foreground_processes=None)):
            with self.subTest(change=change):
                adapter.command.return_value = dict(process_info=dict(valid, **change))
                with self.assertRaisesRegex(TaskError, "Cannot prove an idle"):
                    abandonment_runtime(context, identities, adapter)

    def test_git_routing_overrides_refuse_before_loading_configuration(self):
        for name in ("GIT_INDEX_FILE", "GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR",
                     "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES"):
            with self.subTest(name=name), patch.dict(os.environ, {name: "/another/repository"}), \
                    patch("task_start.integrate.load_local", side_effect=AssertionError("No config")), \
                    self.assertRaisesRegex(TaskError, "Git routing"):
                abandon_integration("DEV-7", "DEV-7-G1")
        self.assertFalse(self.registry_path.parent.exists(), "Invalid routing must not create lock state")

    def test_invalid_abandonment_selector_refuses_before_lock_or_configuration(self):
        for selector in ("DEV-8-G1", "DEV-7-I1", "DEV-7-G0", "../DEV-7-G1"):
            with self.subTest(selector=selector), \
                    patch("task_start.integrate.load_local", side_effect=AssertionError("No config")), \
                    self.assertRaisesRegex(TaskError, "exact integration context ID"):
                abandon_integration("DEV-7", selector)
        self.assertFalse(self.registry_path.parent.exists(), "Invalid arguments must not create lock state")

class LegacyReadinessProofTests(unittest.TestCase):
    def setUp(self):
        self.path = Path("/isolated")
        self.adapter = Codex(AgentOptions("codex", "model", "high"))
        self.process = self.enterContext(patch("task_start.agent.stopped_checkout", return_value=dict(pid=123, started=7)))
        self.rpc = self.enterContext(patch("task_start.agent.CodexRPC")).return_value.__enter__.return_value

    def test_complete_readiness_is_checked_twice_and_identity_is_retained(self):
        provider = ReadinessProvider(self.path)
        self.rpc.request.side_effect = provider.request
        proof = self.adapter.verify_abandonment(self.path, 123, allow_readiness=True)
        self.assertEqual(proof["history"], "readiness_only")
        validate_readiness_only(proof["startup"], self.path)
        self.assertEqual([m for m, _ in provider.calls].count("thread/queue/list"), 2)
        self.assertEqual([m for m, _ in provider.calls].count("thread/list"), 4)
        self.assertEqual(self.process.call_count, 2)

    def test_modern_attempt_does_not_gain_a_readiness_history_exception(self):
        self.rpc.request.side_effect = ReadinessProvider(self.path).request
        with self.assertRaisesRegex(TaskError, "history may be resumable"):
            self.adapter.verify_abandonment(self.path, 123)

    def test_partial_or_task_bearing_history_never_counts_as_readiness(self):
        for fault in ("cwd", "thread_id", "fork", "parent", "source", "active", "system_error", "preview",
                      "extra_thread", "extra_archived", "archived_only", "list_page", "turn_page", "second_turn", "summary",
                      "unfinished", "failed", "missing_error", "missing_status", "missing_cursor", "extra_input",
                      "tool", "compaction", "reasoning", "reply", "reply_phase", "input", "empty_queue_page", "queue"):
            with self.subTest(fault=fault):
                p = ReadinessProvider(self.path)
                if fault == "cwd": p.thread["cwd"] = "/another"
                elif fault == "thread_id": p.thread["id"] = "unknown"
                elif fault == "fork": p.thread["forkedFromId"] = "parent"
                elif fault == "parent": p.thread["parentThreadId"] = "parent"
                elif fault == "source": p.thread["source"] = "exec"
                elif fault == "active": p.thread["status"] = dict(type="active")
                elif fault == "system_error": p.thread["status"] = dict(type="systemError")
                elif fault == "preview": p.thread["preview"] = "READY"
                elif fault == "extra_thread": p.threads.append(copy.deepcopy(p.thread))
                elif fault == "extra_archived": p.archived.append(copy.deepcopy(p.thread))
                elif fault == "archived_only": p.archived, p.threads = p.threads, []
                elif fault == "list_page": p.list_cursor = "more"
                elif fault == "turn_page": p.turns["nextCursor"] = "more"
                elif fault == "second_turn": p.turns["data"].append(copy.deepcopy(p.turn))
                elif fault == "summary": p.turn["itemsView"] = "summary"
                elif fault == "unfinished": p.turn["status"] = "inProgress"
                elif fault == "failed": p.turn["status"] = "failed"
                elif fault == "missing_error": del p.turn["error"]
                elif fault == "missing_status": del p.thread["status"]
                elif fault == "missing_cursor": del p.turns["nextCursor"]
                elif fault == "extra_input": p.turn["items"].append(copy.deepcopy(p.turn["items"][0]))
                elif fault == "tool": p.turn["items"].append(dict(type="commandExecution"))
                elif fault == "compaction": p.turn["items"].append(dict(type="contextCompaction"))
                elif fault == "reasoning": p.turn["items"].append(dict(type="reasoning"))
                elif fault == "reply": p.turn["items"][1]["text"] = "Integration completed"
                elif fault == "reply_phase": p.turn["items"][1]["phase"] = "commentary"
                elif fault == "input": p.turn["items"][0]["content"][0]["text"] = "Do the task"
                elif fault == "empty_queue_page": p.queue["nextCursor"] = "more"
                elif fault == "queue": p.queue["data"] = [dict(id="unconfirmed-task")]
                self.rpc.request.side_effect = p.request
                with self.assertRaises(TaskError):
                    self.adapter.verify_abandonment(self.path, 123, allow_readiness=True)

    def test_provider_failure_and_late_queued_input_refuse(self):
        p = ReadinessProvider(self.path)
        def request(method, params, **kwargs):
            if method == "thread/queue/list" and any(m == method for m, _ in p.calls):
                p.queue["data"] = [dict(id="late-input")]
            return p.request(method, params, **kwargs)
        self.rpc.request.side_effect = request
        with self.assertRaises(TaskError):
            self.adapter.verify_abandonment(self.path, 123, allow_readiness=True)
        self.rpc.request.side_effect = TaskError("history unreadable")
        with self.assertRaisesRegex(TaskError, "history unreadable"):
            self.adapter.verify_abandonment(self.path, 123, allow_readiness=True)

    def test_late_provider_history_or_changed_identity_refuses(self):
        for fault in ("new_thread", "changed_id", "changed_cwd", "changed_preview"):
            with self.subTest(fault=fault):
                p = ReadinessProvider(self.path)
                def request(method, params, **kwargs):
                    if method == "thread/list" and any(m == "thread/queue/list" for m, _ in p.calls):
                        if fault == "new_thread": p.archived = [copy.deepcopy(p.thread)]
                        elif fault == "changed_id": p.thread["id"] = "00000000-0000-4000-8000-000000000004"
                        elif fault == "changed_cwd": p.thread["cwd"] = "/another"
                        elif fault == "changed_preview": p.thread["preview"] = "Do the task"
                    return p.request(method, params, **kwargs)
                self.rpc.request.side_effect = request
                with self.assertRaises(TaskError):
                    self.adapter.verify_abandonment(self.path, 123, allow_readiness=True)


class ProcessEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.proc = Path(self.directory.name)
        self.checkout = self.proc / "checkout"
        self.checkout.mkdir()

    def process(self, pid, parent, cwd, argv="bash", started=7):
        path = self.proc / str(pid)
        path.mkdir()
        (path / "stat").write_text(f"{pid} (bash) " + " ".join(["S", str(parent), *("0" for _ in range(17)), str(started)]))
        (path / "cwd").symlink_to(cwd)
        (path / "cmdline").write_bytes(os.fsencode(argv))

    def test_absent_execution_and_exact_empty_shell_are_proven(self):
        self.assertEqual(stopped_checkout(self.checkout, proc=self.proc), dict(pid=None, started=None))
        self.process(123, 1, self.checkout)
        self.assertEqual(stopped_checkout(self.checkout, 123, proc=self.proc), dict(pid=123, started=7))

    def test_unobservable_original_shell_never_counts_as_stopped_execution(self):
        with self.assertRaisesRegex(TaskError, "same host/PID namespace as Herdr"):
            stopped_checkout(self.checkout, 123, proc=self.proc)

    def test_live_and_background_processes_fail_even_after_changing_directory(self):
        self.process(123, 1, self.checkout)
        self.process(124, 123, self.proc)
        with self.assertRaisesRegex(TaskError, "process may still use"):
            stopped_checkout(self.checkout, 123, proc=self.proc)

    def test_detached_process_with_checkout_argument_refuses(self):
        self.process(125, 1, self.proc, argv=f"codex\0--cd\0{self.checkout}\0")
        with self.assertRaisesRegex(TaskError, "process may still use"):
            stopped_checkout(self.checkout, proc=self.proc)

    def test_missing_process_evidence_or_unsupported_platform_refuses(self):
        self.process(123, 1, self.checkout)
        (self.proc / "123" / "cmdline").unlink()
        with self.assertRaisesRegex(TaskError, "Cannot prove"):
            stopped_checkout(self.checkout, 123, proc=self.proc)
        with patch("task_start.integration_process.sys.platform", "darwin"), self.assertRaisesRegex(TaskError, "Linux"):
            stopped_checkout(self.checkout, proc=self.proc)
