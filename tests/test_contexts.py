import copy
from dataclasses import replace
import io
import json
import multiprocessing
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from task_start import TaskError, cli
from task_start.agent import AgentExecution, AgentOptions, LaunchResult
from task_start.contexts import ContextRegistry, HerdrContexts, inspect_contexts, launch_registered
from task_start.workspace import Workspace
from test_task_start import ISSUE, LOCAL, PROJECT


def allocate_process(path, count):
    registry = ContextRegistry(Path(path))
    return [registry.allocate("DEV-20", "review", agent="pi") for _ in range(count)]


class RegistryTests(unittest.TestCase):
    def setUp(self):
        self.directory = self.enterContext(tempfile.TemporaryDirectory())
        self.path = Path(self.directory) / "runtime" / "contexts.sqlite3"
        self.registry = ContextRegistry(self.path)
        self.enterContext(patch("task_start.contexts.registry_path", return_value=self.path))

    def allocate(self, issue="DEV-20", role="implementation", **kwargs):
        return self.registry.allocate(issue, role, agent="codex", **kwargs)

    def test_roles_issues_and_restart_have_independent_monotonic_ordinals(self):
        self.assertEqual(self.allocate(), "DEV-20-I1")
        self.assertEqual(self.allocate(role="review"), "DEV-20-R1")
        self.assertEqual(self.allocate("DEV-21"), "DEV-21-I1")
        self.registry = ContextRegistry(self.path)
        self.assertEqual(self.allocate(), "DEV-20-I2")
        self.assertEqual(self.allocate(role="review"), "DEV-20-R2")
        self.assertEqual(self.allocate(role="integration"), "DEV-20-G1")

    def test_v1_migration_preserves_existing_contexts_and_ordinals(self):
        first = self.allocate()
        original = self.registry.get(first)
        with sqlite3.connect(self.path) as db:
            sql = db.execute("SELECT sql FROM sqlite_master WHERE name='contexts'").fetchone()[0]
            db.execute("ALTER TABLE contexts RENAME TO newer_contexts")
            db.execute(sql.replace(", 'integration'", ""))
            db.execute("INSERT INTO contexts SELECT * FROM newer_contexts")
            db.execute("DROP TABLE newer_contexts")
            db.execute("PRAGMA user_version=1")
        self.assertEqual(self.registry.get(first), original)  # Read-only access never migrates.
        self.assertEqual(self.allocate(role="integration"), "DEV-20-G1")
        self.assertEqual(self.registry.get(first), original)
        self.assertEqual(self.allocate(), "DEV-20-I2")

    def test_v2_repository_discovery_migration_preserves_context_history(self):
        context = self.allocate()
        before = self.registry.get(context)
        with sqlite3.connect(self.path) as db:
            db.execute("DROP TABLE ownership_repositories")
            db.execute("PRAGMA user_version=2")
        self.assertEqual(self.registry.repositories(), [])
        self.assertEqual(self.registry.get(context), before)
        repo = self.path.parent / "contextless-repository"
        self.registry.remember_repository(repo)
        self.assertEqual(ContextRegistry(self.path).repositories(), [str(repo)])
        self.assertEqual(self.registry.get(context), before)
        self.assertEqual(self.allocate(), "DEV-20-I2")

    def test_concurrent_processes_allocate_once_each_including_initial_schema(self):
        with multiprocessing.get_context("spawn").Pool(6) as pool:
            batches = pool.starmap(allocate_process, [(str(self.path), 8)] * 6)
        ids = [value for batch in batches for value in batch]
        self.assertEqual(set(ids), {f"DEV-20-R{n}" for n in range(1, 49)})
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(self.allocate(role="review"), "DEV-20-R49")

    def test_second_controller_can_allocate_before_creator_commits_schema(self):
        import os
        parent, child = multiprocessing.Pipe()
        real_open = os.open
        def creator():
            def paused_open(path, flags, *args, **kwargs):
                fd = real_open(path, flags, *args, **kwargs)
                if Path(path) == self.path and flags & os.O_EXCL:
                    child.send("created")
                    if not child.poll(10):
                        raise AssertionError("creator was never released")
                    child.recv()
                return fd
            with patch("task_start.contexts.os.open", side_effect=paused_open):
                child.send(self.allocate())
        process = multiprocessing.get_context("fork").Process(target=creator)
        process.start(); child.close()
        try:
            self.assertTrue(parent.poll(10)); self.assertEqual(parent.recv(), "created")
            self.assertEqual(self.allocate(), "DEV-20-I1")
            parent.send("finish")
            self.assertTrue(parent.poll(10)); self.assertEqual(parent.recv(), "DEV-20-I2")
            process.join(5)
            self.assertEqual(process.exitcode, 0)
            self.assertEqual(len(self.registry.list()), 2)
        finally:
            if process.is_alive():
                process.terminate()
            process.join(5)
            parent.close()

    def test_pending_pane_claim_is_atomic_and_cannot_be_silently_reused(self):
        metadata = dict(endpoint="/server.sock", pane_id="w1:p1")
        first = self.allocate(**metadata)
        with self.assertRaisesRegex(TaskError, first):
            self.allocate(**metadata)
        self.registry.update(first, state="uncertain")
        self.assertEqual(self.allocate(**metadata), "DEV-20-I2")

    def test_process_dying_after_allocation_does_not_release_ordinal(self):
        script = """
import os, sys
from pathlib import Path
from task_start.contexts import ContextRegistry
ContextRegistry(Path(sys.argv[1])).allocate('DEV-20', 'implementation', agent='codex')
os._exit(9)
"""
        result = subprocess.run([sys.executable, "-c", script, str(self.path)],
                                cwd=Path(__file__).resolve().parent.parent, capture_output=True)
        self.assertEqual(result.returncode, 9, result.stderr)
        self.assertEqual(self.registry.list()[0]["state"], "launching")
        self.assertEqual(self.allocate(), "DEV-20-I2")

    def test_lifecycle_and_resumability_are_independent(self):
        context_id = self.allocate()
        self.registry.update(context_id, state="active", resumability="no")
        self.registry.update(context_id, state="uncertain")
        row = self.registry.list()[0]
        self.assertEqual((row["state"], row["resumability"]), ("uncertain", "no"))

    def test_retirement_is_exact_and_drops_handles_but_preserves_history(self):
        metadata = dict(repository="/repo", worktree="/tree", endpoint="/server.sock", workspace_id="w1")
        first = self.allocate(**metadata)
        self.registry.update(first, state="active", session_id="opaque", session_kind="id",
                             herdr_session='"opaque"', resumability="yes")
        self.allocate(role="review", **dict(metadata, workspace_id="w2"))
        self.allocate("DEV-21", **metadata)
        self.registry.retire("DEV-20", Path("/repo"), Path("/tree"), endpoint="/other.sock", workspace_id="w1")
        self.assertEqual(len(self.registry.list()), 3)
        self.registry.retire("DEV-20", Path("/repo"), Path("/tree"), endpoint="/server.sock", workspace_id="w1")
        self.assertEqual(len(self.registry.list()), 2)
        retired = self.registry.list(include_retired=True)[0]
        self.assertEqual(retired["state"], "retired")
        self.assertIsNone(retired["session_id"])
        self.assertIsNone(retired["herdr_session"])
        self.assertIsNotNone(retired["retired_at"])
        with self.assertRaisesRegex(TaskError, "retired"):
            self.registry.update(first, state="active")
        self.assertEqual(ContextRegistry(self.path).allocate("DEV-20", "implementation", agent="pi"), "DEV-20-I2")

    def test_corruption_and_unsupported_version_fail_without_resetting(self):
        self.allocate()
        with sqlite3.connect(self.path) as db:
            db.execute("PRAGMA user_version=99")
        before = self.path.read_bytes()
        with self.assertRaisesRegex(TaskError, "version"):
            self.allocate()
        self.assertEqual(self.path.read_bytes(), before)
        self.path.write_bytes(b"not a database")
        with self.assertRaisesRegex(TaskError, "history was not reset"):
            self.allocate()
        self.assertEqual(self.path.read_bytes(), b"not a database")

    def test_inspection_of_missing_registry_creates_nothing_and_uses_no_config(self):
        with patch("task_start.cli.load_local", side_effect=AssertionError("config read")), \
                patch("task_start.cli.Linear", side_effect=AssertionError("Linear access")), \
                patch("task_start.contexts.registry_path", return_value=self.path), \
                patch("task_start.contexts.run", side_effect=AssertionError("Herdr call")), \
                patch("sys.stdout", new=io.StringIO()) as output:
            self.assertEqual(cli.main(["contexts"]), 0)
        self.assertIn("No known", output.getvalue())
        self.assertFalse(self.path.parent.exists())

    def test_invalid_role_and_noncanonical_identifier_do_not_allocate(self):
        for issue, role in [("dev-20", "implementation"), ("DEV-020", "review"), ("DEV-20", "other")]:
            with self.assertRaises(TaskError):
                self.allocate(issue, role)
        self.assertFalse(self.path.exists())


class LaunchAndInspectionTests(unittest.TestCase):
    def setUp(self):
        RegistryTests.setUp(self)
        self.workspace = Workspace("dev-7-test", Path("/tree"), "w1", "w1:t1", "w1:p1", "ready")
        self.execution = AgentExecution(ISSUE, Path("/repo"), self.workspace,
                                        AgentOptions("codex", "model", "high"), "A test handoff")
        self.pane = dict(workspace_id="w1", tab_id="w1:t1", pane_id="w1:p1", terminal_id="term_one",
                         agent_status="unknown", label=None)
        self.herdr = Mock(spec=HerdrContexts)
        self.herdr.endpoint.return_value = "/server.sock"
        self.herdr.snapshot.side_effect = lambda: [copy.deepcopy(self.pane)]
        self.herdr.label.side_effect = lambda pane, label: self.pane.update(label=label)
        self.adapter = Mock()
        self.adapter.launch.side_effect = self.launch_agent

    def launch_agent(self, execution):
        self.assertEqual(self.registry.list()[0]["state"], "launching")
        self.assertEqual(self.pane["label"], "DEV-7-I1")
        self.pane.update(agent="codex", agent_status="working",
                         agent_session=dict(agent="codex", kind="id", value="real-session", source="hook"))
        execution.runtime_observer(dict(terminal_id="term_one", herdr_session=json.dumps(self.pane["agent_session"])))
        execution.runtime_observer(dict(session_id="real-session", session_kind="id", resumability="yes"))
        return LaunchResult("codex", "w1:p1", "started", "real-session", session_kind="id", resumability="yes")

    def launch(self):
        return launch_registered(self.adapter, self.execution, registry=self.registry, herdr=self.herdr)

    def inspect(self, issue=None, include_retired=False):
        return inspect_contexts(issue, include_retired=include_retired, registry=self.registry, herdr=self.herdr)

    def test_launch_labels_exact_id_and_keeps_opaque_runtime_identity_separate(self):
        result = self.launch()
        context = self.registry.list()[0]
        self.assertIn("DEV-7-I1", result.summary)
        self.assertEqual(context["pane_id"], "w1:p1")
        self.assertEqual(context["terminal_id"], "term_one")
        self.assertEqual(context["session_id"], "real-session")
        self.assertEqual(context["resumability"], "yes")
        self.assertNotIn("A test handoff", self.path.read_bytes().decode(errors="replace"))
        with self.assertRaisesRegex(TaskError, "identity was preserved"):
            self.launch()
        self.assertEqual(len(self.registry.list()), 1)

    def test_missing_resume_handle_is_explicitly_unknown(self):
        self.adapter.launch.side_effect = None
        self.adapter.launch.return_value = LaunchResult("codex", "w1:p1", "started")
        self.launch()
        context = self.registry.list()[0]
        self.assertIsNone(context["session_id"])
        self.assertEqual(context["resumability"], "unknown")

    def test_default_result_cannot_discard_identity_discovered_by_observer(self):
        def launch(execution):
            self.launch_agent(execution)
            return LaunchResult("codex", "w1:p1", "started")
        self.adapter.launch.side_effect = launch
        self.launch()
        row = self.registry.list()[0]
        self.assertEqual((row["session_id"], row["session_kind"], row["resumability"]),
                         ("real-session", "id", "yes"))
        self.assertEqual(json.loads(row["herdr_session"])["value"], "real-session")

    def test_final_result_cannot_replace_identity_discovered_by_observer(self):
        def launch(execution):
            self.launch_agent(execution)
            return LaunchResult("codex", "w1:p1", "started", "replacement", session_kind="id")
        self.adapter.launch.side_effect = launch
        with self.assertRaisesRegex(TaskError, "session changed"):
            self.launch()
        row = self.registry.list()[0]
        self.assertEqual((row["state"], row["session_id"]), ("uncertain", "real-session"))

    def test_failure_after_session_discovery_retains_handle_and_consumes_ordinal(self):
        def fail(execution):
            self.launch_agent(execution)
            raise TaskError("handoff timed out")
        self.adapter.launch.side_effect = fail
        with self.assertRaisesRegex(TaskError, "DEV-7-I1.*timed out"):
            self.launch()
        context = self.registry.list()[0]
        self.assertEqual(context["state"], "uncertain")
        self.assertEqual(context["session_id"], "real-session")
        self.assertEqual(self.registry.allocate("DEV-7", "implementation", agent="codex"), "DEV-7-I2")

    def test_label_failure_keeps_identity_and_never_starts_agent(self):
        self.herdr.label.side_effect = TaskError("rename failed")
        with self.assertRaisesRegex(TaskError, "DEV-7-I1.*rename failed"):
            self.launch()
        self.adapter.launch.assert_not_called()
        self.assertEqual(self.registry.list()[0]["state"], "uncertain")

    def test_mismatched_terminal_during_launch_is_not_rebound(self):
        def changed(execution):
            execution.runtime_observer(dict(terminal_id="replacement"))
        self.adapter.launch.side_effect = changed
        with self.assertRaisesRegex(TaskError, "not rebound"):
            self.launch()
        self.assertEqual(self.registry.list()[0]["terminal_id"], "term_one")

    def test_inspection_prefers_live_facts_and_never_mutates(self):
        self.launch()
        before = self.path.read_bytes()
        self.herdr.reset_mock()
        self.pane.update(agent_status="blocked", tab_id="w1:t9", focused=False, rect={"x": 50})
        output = self.inspect()
        self.assertIn("active / blocked", output)
        self.assertIn("w1:t9", output)
        self.assertIn("tab changed", output)
        self.assertEqual(self.path.read_bytes(), before)
        self.herdr.label.assert_not_called()

    def test_renamed_closed_replaced_and_moved_panes_are_reported(self):
        self.launch()
        original = copy.deepcopy(self.pane)
        cases = [
            ({"label": "human label"}, "renamed/unlabeled: human label"),
            ({"label": "\x1b[31mspoof"}, "?"),
            ({"terminal_id": "new_terminal"}, "mismatched terminal"),
            ({"agent": "pi"}, "agent mismatch"),
            ({"agent": None}, "agent absent"),
            ({"agent_session": {"value": "another-session"}}, "session mismatch"),
            ({"agent_session": None}, "session unknown/stale"),
            ({"pane_id": "w2:p8", "workspace_id": "w2"}, "moved/stale"),
        ]
        for changes, expected in cases:
            with self.subTest(changes=changes):
                self.pane = dict(original, **changes)
                self.assertIn(expected, self.inspect())
                self.assertNotIn("\x1b", self.inspect())
        self.herdr.snapshot.side_effect = lambda: []
        self.assertIn("missing pane", self.inspect())
        self.assertEqual(self.registry.list()[0]["pane_id"], "w1:p1")

    def test_failed_or_other_server_snapshot_is_unknown_not_missing(self):
        self.launch()
        self.herdr.endpoint.return_value = "/different.sock"
        self.assertIn("different Herdr server", self.inspect())
        self.herdr.snapshot.side_effect = TaskError("server unavailable")
        output = self.inspect()
        self.assertIn("unknown/stale", output)
        self.assertNotIn("missing pane", output)

    def test_session_reported_later_is_checked_against_adapter_handle(self):
        self.launch()
        self.registry.update("DEV-7-I1", herdr_session=None)
        self.assertNotIn("session identity unknown", self.inspect())
        self.pane["agent_session"]["value"] = "different-session"
        self.assertIn("session mismatch", self.inspect())

    def test_session_reporting_source_is_not_identity(self):
        self.launch()
        self.pane["agent_session"]["source"] = "new-report-source"
        self.assertNotIn("session mismatch", self.inspect())

    def test_inspection_compares_session_agent_kind_and_value(self):
        self.launch()
        session = dict(self.pane["agent_session"])
        for changes in [dict(agent="pi"), dict(kind="path"), dict(value="replacement")]:
            with self.subTest(changes=changes):
                self.pane["agent_session"] = dict(session, **changes)
                self.assertIn("session mismatch", self.inspect())

    def test_filter_and_historical_view_are_read_only(self):
        self.launch()
        self.registry.allocate("DEV-8", "review", agent="pi")
        self.registry.retire("DEV-7", Path("/repo"), Path("/tree"), endpoint="/server.sock", workspace_id="w1")
        before = self.path.read_bytes()
        self.assertNotIn("DEV-7-I1", self.inspect())
        self.assertIn("DEV-8-R1", self.inspect())
        output = self.inspect("DEV-7", include_retired=True)
        self.assertIn("DEV-7-I1", output)
        self.assertIn("retired", output)
        self.assertNotIn("DEV-8", output)
        self.assertEqual(self.path.read_bytes(), before)
        args = cli.parser().parse_args(["contexts", "dev-7", "--all"])
        self.assertEqual((args.issue, args.all), ("DEV-7", True))

    def test_task_start_uses_canonical_issue_and_no_agent_does_not_allocate(self):
        with patch("task_start.cli.load_local", return_value=LOCAL), \
                patch("task_start.cli.load_projects", return_value=[PROJECT]), \
                patch("task_start.cli.repository_path", return_value=Path("/repo")), \
                patch("task_start.cli.Linear") as linear, patch("task_start.cli.Git"), \
                patch("task_start.cli.Herdr") as workspace_herdr, \
                patch("task_start.cli.adapter_for", return_value=self.adapter), \
                patch("task_start.contexts.registry_path", return_value=self.path), \
                patch("task_start.contexts.HerdrContexts", return_value=self.herdr):
            linear.return_value.get_issue.return_value = ISSUE
            workspace_herdr.return_value.prepare.return_value = self.workspace
            cli.start("dev-7", no_agent=True)
            self.assertFalse(self.path.exists())
            self.assertIn("DEV-7-I1", cli.start("dev-7"))


class HerdrProtocolTests(unittest.TestCase):
    def test_real_herdr_pane_label_protocol_and_confirmation(self):
        pane = dict(pane_id="w1:p1", workspace_id="w1", tab_id="w1:t1", terminal_id="t")
        responses = [dict(type="pane_info", pane=pane), dict(type="pane_info", pane=dict(pane, label="DEV-7-I1")),
                     dict(type="pane_info", pane=dict(pane, label="DEV-7-I1"))]
        with patch("task_start.contexts.run", side_effect=[json.dumps(dict(result=r)) for r in responses]) as run:
            HerdrContexts().label(pane, "DEV-7-I1")
        self.assertEqual(run.call_args_list[1].args[0], ["herdr", "pane", "rename", "w1:p1", "DEV-7-I1"])

    def test_mismatched_identity_prevents_rename(self):
        pane = dict(pane_id="w1:p1", workspace_id="w1", tab_id="w1:t1", terminal_id="t")
        for changes in [dict(terminal_id="new"), dict(agent="codex"), dict(label="human change")]:
            with self.subTest(changes=changes), patch.object(
                    HerdrContexts, "command", return_value=dict(pane=dict(pane, **changes))) as command:
                with self.assertRaisesRegex(TaskError, "not rebound"):
                    HerdrContexts().label(pane, "DEV-7-I1")
            self.assertEqual(command.call_count, 1)

    def test_malformed_snapshot_is_rejected(self):
        for panes in [None, [None], [{}], [dict(pane_id="w1:p1", workspace_id="w1", tab_id="t1", terminal_id=None)]]:
            with self.subTest(panes=panes), patch.object(HerdrContexts, "command", return_value=dict(snapshot=dict(panes=panes))):
                with self.assertRaises(TaskError):
                    HerdrContexts().snapshot()
