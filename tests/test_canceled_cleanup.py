"""Forced local-execution disposal with real Git/SQLite/filesystem effects."""

import copy
from dataclasses import replace
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import call, patch
from uuid import uuid4
from types import SimpleNamespace

from task_start import HerdrResponseError, TaskError, cli
from task_start.cleanup import DisposalStore, check_contents, check_mounts, mount_points, read_mounts, overlapping
from task_start.contexts import ContextRegistry, now
from task_start.integrate import history_identity
from task_start.integration_process import stopped_execution
from task_start.integration_state import IntegrationStore, check_integration
from task_start.publication_state import PublicationStore
from task_start.loop import new_state
from task_start.loop_state import LoopStore
from task_start.pass_delivery import PassDelivery
from task_start.agent import AgentOptions
from task_start.workspace import Git, Herdr, Workspace
import test_cleanup as completed
import test_integration_abandon as abandonment


def mount_record(number, root, point, *, parent=20, device=None, filesystem="ext4",
                 source="/dev/example", options="rw", super_options="rw", optional=""):
    if device is None:
        observed = Path(tempfile.gettempdir()).stat().st_dev
        device = f"{os.major(observed)}:{os.minor(observed)}"
    def escaped(path):
        return (os.fsencode(path).replace(b"\\", b"\\134").replace(b" ", b"\\040")
                .replace(b"\t", b"\\011").replace(b"\n", b"\\012"))
    return (f"{number} {parent} {device} ".encode() + escaped(root) + b" " + escaped(point)
            + f" {options}{' ' + optional if optional else ''} - {filesystem} {source} {super_options}\n".encode())


def mount_table(*points):
    """Same-filesystem binds have distinct mount IDs, with no required bind flag."""
    rows = [mount_record(20, "/", "/", parent=1)]
    rows.extend(mount_record(number, "/outside", point, optional="shared:2 future:field")
                for number, point in enumerate(points, 21))
    return b"".join(rows)


def foreign_process_owners(proc, *pids):
    """Reproduce foreign proc directory ownership without requiring chown/root."""
    original = Path.stat
    def observed(path, *args, **kwargs):
        result = original(path, *args, **kwargs)
        if path in {proc / str(pid) for pid in pids}:
            fields = list(result)
            fields[4] = os.getuid() + 1
            return os.stat_result(fields)
        return result
    return patch.object(Path, "stat", observed)


def process_mounts(proc):
    (proc / "self").mkdir()
    point = os.fsencode(proc).replace(b"\\", b"\\134").replace(b" ", b"\\040")
    (proc / "self" / "mounts").write_bytes(b"proc " + point + b" proc rw,nosuid,nodev,noexec 0 0\n")


class ForcedCleanupTests(unittest.TestCase):
    command = completed.CleanupTests.command
    workspace_entry = completed.CleanupTests.workspace_entry
    remote_branch_lookup = staticmethod(Git.remote_branches)

    def setUp(self):
        completed.CleanupTests.setUp(self)
        # This fixture exercises unpublished disposal, not completed cleanup.
        # Keep a real unmerged local tip so publication evidence cannot qualify
        # for the separate merged-execution recovery path.
        self.command(self.path, "commit", "--allow-empty", "-m", "unpublished execution")
        self.enterContext(patch("task_start.workspace.merged_pull",
                                side_effect=TaskError("No verified merged PR in unpublished fixture")))
        self.mountinfo = self.repo.parent / "mountinfo"
        self.mountinfo.write_bytes(mount_table())
        self.enterContext(patch("task_start.cleanup.MOUNTINFO", self.mountinfo))
        self.issue = replace(self.issue, title="Clarify agent instructions during task handoff",
                             state_name="In Progress", state_type="started")
        self.linear.get_issue.return_value = self.issue
        self.workspace_id, self.workspace_label = "w-task", "DEV-7"
        self.panes = []
        self.remote_branches = self.enterContext(patch.object(Git, "remote_branches", return_value=[]))
        self.command(self.repo, "remote", "set-url", "origin", "git@github.com:example/project.git")
        self.enterContext(patch.object(Git, "history_remote", return_value="git@github.com:example/project.git"))
        self.pulls = self.enterContext(patch("task_start.cleanup.pull_requests", return_value=[]))
        self.identities = self.enterContext(patch("task_start.cleanup.HerdrContexts")).return_value
        self.identities.endpoint.return_value = "/tmp/test-herdr.sock"
        self.identities.snapshot.side_effect = lambda: copy.deepcopy(self.panes)
        self.identities.command.side_effect = self.process_info
        self.process = self.enterContext(patch("task_start.cleanup.stopped_execution",
            side_effect=lambda paths, shells, **kw: {str(pid): 1234 for pid in shells}))
        self.registry = ContextRegistry(self.registry_path)
        self.context("implementation", self.path, "active")
        self.context("review", self.path, "active")
        self.publication = PublicationStore(self.path)
        saved = self.publication.read()
        saved["acceptance"] = dict(pass_id="reviewed", verdict="clean", issue="DEV-7", repository=str(self.repo),
                                   worktree=str(self.path), branch=self.branch, base_branch="main")
        self.publication.write(saved)
        (self.path / "tracked.txt").write_text("dirty reviewed implementation\n")
        (self.path / "ignored.txt").write_text("discard explicitly\n")
        self.other = self.repo.parent / "unrelated-task"
        self.command(self.repo, "worktree", "add", "-b", "dev-8-unrelated", str(self.other))
        self.git.save_scope(self.other, "dev-8-unrelated", "DEV-8", None)
        self.unrelated = self.registry.allocate("DEV-8", "implementation", agent="codex",
            repository=str(self.repo), worktree=str(self.other), endpoint="/tmp/test-herdr.sock",
            workspace_id="other", pane_id="other:p1", terminal_id="other-term", tab_id="other:t1")
        self.unrelated_before = self.registry.get(self.unrelated)
        self.panes.append(dict(workspace_id="other", pane_id="other:p1", terminal_id="other-term",
                               tab_id="other:t1", cwd=str(self.other), agent="codex", agent_status="working"))
        self.extra_workspaces = [self.workspace_entry("other", self.other, "DEV-8")]
        self.other_head = self.command(self.other, "rev-parse", "HEAD")

    def list_worktrees(self, operation, *args):
        result = completed.CleanupTests.list_worktrees(self, operation, *args)
        if not self.workspace_active:
            for tree in result["worktrees"]:
                tree["open_workspace_id"] = None
        return result

    def workspace_commands(self, operation, *args):
        result = completed.CleanupTests.workspace_commands(self, operation, *args)
        if operation == "close":
            self.panes[:] = [p for p in self.panes if p["workspace_id"] != self.workspace_id]
        else:
            for workspace in result["workspaces"]:
                workspace["pane_count"] = sum(p["workspace_id"] == workspace["workspace_id"] for p in self.panes)
        return result

    def process_info(self, group, operation, *args):
        self.assertEqual((group, operation, args[0]), ("pane", "process-info", "--pane"))
        number = next(n for n, p in enumerate(self.panes, 101) if p["pane_id"] == args[1])
        return dict(process_info=dict(pane_id=args[1], shell_pid=number,
            foreground_process_group_id=number, foreground_processes=[dict(pid=number, argv=["bash"])]))

    def context(self, role, path, state):
        number = len(self.panes) + 1
        pane = dict(workspace_id=self.workspace_id, pane_id=f"p{number}", terminal_id=f"term{number}",
                    tab_id="t1", cwd=str(path), agent=None, agent_status="idle")
        context = self.registry.allocate("DEV-7", role, agent="codex", model="model", mode="high",
            repository=str(self.repo), worktree=str(path), endpoint="/tmp/test-herdr.sock",
            **{k: pane[k] for k in ("workspace_id", "pane_id", "tab_id", "terminal_id")})
        self.registry.update(context, state=state)
        if role != "integration":
            self.registry.update(context, session_id=f"session-{context}", session_kind="id", resumability="yes")
        self.panes.append(pane)
        return context

    def retained_integration(self, *, legacy=False):
        pass_id = str(uuid4())
        self.isolated = self.registry_path.parent / "integrations" / pass_id / "checkout"
        self.isolated.mkdir(parents=True)
        self.command(self.isolated, "init", "--initial-branch=integration")
        (self.isolated / "conflict.txt").write_text("base\n")
        self.command(self.isolated, "add", ".")
        self.command(self.isolated, "commit", "-m", "isolated base")
        # Retain an actual unmerged index, as in an uncertain DEV-30 launch.
        base = self.command(self.isolated, "rev-parse", "HEAD")
        self.command(self.isolated, "checkout", "-b", "opposing")
        (self.isolated / "conflict.txt").write_text("other\n")
        self.command(self.isolated, "commit", "-am", "other")
        self.command(self.isolated, "checkout", "integration")
        (self.isolated / "conflict.txt").write_text("task\n")
        self.command(self.isolated, "commit", "-am", "task")
        with self.assertRaises(subprocess.CalledProcessError):
            self.command(self.isolated, "merge", "opposing")
        self.assertTrue(self.command(self.isolated, "ls-files", "--unmerged"))
        context = self.context("integration", self.isolated, "uncertain")
        self.output = self.repo.parent / f"task-integration-{pass_id}-output" / "result.json"
        if not legacy:
            self.output.parent.mkdir()
            self.output.write_text('{"state":"blocked"}')
        record = dict(version=1, pass_id=pass_id, state="uncertain", checkout=str(self.isolated),
            binding=dict(issue="DEV-7", repository=str(self.repo), worktree=str(self.path), branch=self.branch,
                         base_branch="main", endpoint="/tmp/test-herdr.sock", workspace_id=self.workspace_id),
            source=dict(head=base, fingerprint="source-fingerprint"), base=base,
            options=dict(kind="codex", model="model", mode="high"), plan=None, context=dict(context_id=context),
            history=history_identity(self.isolated))
        if not legacy:
            record.update(output=str(self.output), slice=None)
        IntegrationStore(self.path).write(record)
        return record

    def journal_path(self):
        paths = self.git.disposal_files("DEV-7")
        pending = [p for p in paths if json.loads(p.read_text())["state"] == "pending"]
        return pending[0] if pending else max(paths, key=lambda p: (json.loads(p.read_text())["at"], p.name))

    def journal(self):
        return json.loads(self.journal_path().read_text())

    def assert_preserved(self):
        self.assertTrue(self.path.is_dir())
        self.assertIn(self.branch, self.git.branches("DEV-7"))
        self.assertEqual((self.path / "tracked.txt").read_text(), "dirty reviewed implementation\n")
        self.assertEqual(self.registry.get(self.unrelated), self.unrelated_before)
        self.assertEqual(self.command(self.other, "rev-parse", "HEAD"), self.other_head)

    def assert_disposed(self):
        self.assertFalse(self.path.exists())
        self.assertFalse(self.git.branches("DEV-7"))
        self.assertFalse(self.workspace_active)
        self.assertEqual(self.registry.list("DEV-7"), [])
        for context in self.registry.list("DEV-7", include_retired=True):
            self.assertEqual(context["state"], "retired")
            for key in ("terminal_id", "session_id", "session_kind", "herdr_session"):
                self.assertIsNone(context[key])
        self.assertEqual(self.registry.get(self.unrelated), self.unrelated_before)
        self.assertEqual(self.command(self.other, "rev-parse", "HEAD"), self.other_head)
        self.assertEqual(self.journal()["state"], "complete")

    def test_in_progress_dev_30_shape_is_discarded_without_changing_linear_state(self):
        record = self.retained_integration()
        self.assertIn("discarded", cli.cleanup("DEV-7", force=True))
        self.assertEqual((self.issue.state_name, self.issue.state_type), ("In Progress", "started"))
        self.linear.start.assert_not_called()
        self.assertEqual(self.linear.mock_calls, [call.get_issue("DEV-7")])
        self.assert_disposed()
        self.assertFalse(self.isolated.parent.exists())
        self.assertFalse(self.output.parent.exists())
        archived = self.journal()["integrations"][0]
        self.assertEqual(archived["pass_id"], record["pass_id"])
        self.assertEqual(archived["state"], "uncertain")
        self.assertEqual(len(archived["record_sha256"]), 64)
        self.assertEqual(len(archived["output_sha256"]), 64)
        self.assertLess(self.journal_path().stat().st_size, 20000)
        self.assertIn("discarded", cli.cleanup("DEV-7", force=True))
        self.assertEqual(self.registry.allocate("DEV-7", "implementation", agent="codex"), "DEV-7-I2")

    def test_legacy_retained_integration_without_output_is_supported(self):
        self.retained_integration(legacy=True)
        cli.cleanup("DEV-7", force=True)
        self.assert_disposed()
        self.assertFalse(self.isolated.parent.exists())

    def retained_loop(self):
        env = SimpleNamespace(issue=self.issue, repo=self.repo, registry=self.registry,
            endpoint="/tmp/test-herdr.sock", local=self.local.return_value,
            project=SimpleNamespace(base_branch="main"), base=self.command(self.repo, "rev-parse", "HEAD"),
            workspace=Workspace(self.branch, self.path, self.workspace_id, "t1", "p1", "fixture"))
        with patch("task_start.loop.implementation_target", return_value=(self.registry.get("DEV-7-I1"),)), \
                patch("task_start.loop.idle_reviewers"):
            state = new_state(env, AgentOptions("codex", "model", "high"), 3, 6, 10)
        store = LoopStore(self.path)
        store.create(state); store.begin(state)
        def persist(context_id, pass_id):
            state["active_pass"].update(context_id=context_id, pass_id=pass_id)
            store.save(state)
        output = PassDelivery(state, persist, env).create("DEV-7-I1", str(uuid4()))
        output.write_text('{"partial":')
        self.addCleanup(shutil.rmtree, output.parent, True)
        return output

    def test_forced_disposal_removes_retained_loop_output_with_journal(self):
        output = self.retained_loop()
        cli.cleanup("DEV-7", force=True)
        self.assert_disposed()
        self.assertFalse(output.parent.exists())
        retained = self.journal()["loop"]
        self.assertEqual(set(retained["files"]), {"complete.py", "result.json"})
        self.assertIn("discarded", cli.cleanup("DEV-7", force=True))

    def test_forced_disposal_refuses_unrelated_loop_artifacts(self):
        output = self.retained_loop()
        (output.parent / "unrelated.txt").write_text("preserve")
        with self.assertRaisesRegex(TaskError, "unsafe"):
            cli.cleanup("DEV-7", force=True)
        self.assertTrue(output.exists())
        self.assert_preserved()

    def test_abandoned_archive_and_new_uncertain_attempt_are_both_disposed(self):
        record = self.retained_integration()
        first_path, first_output = self.isolated, self.output
        context = self.registry.get(record["context"]["context_id"])
        timestamp = now()
        record.update(state="abandoned", abandonment=dict(at=timestamp, context=context,
            proof=dict(provider="codex", checkout=record["checkout"], history="absent",
                       process=dict(pid=None, started=None))))
        store = IntegrationStore(self.path)
        store.write(record)
        store.archive(record, create=True)
        self.registry.abandon_integration(context, timestamp)
        self.retained_integration()
        cli.cleanup("DEV-7", force=True)
        self.assert_disposed()
        for path in (first_path.parent, first_output.parent, self.isolated.parent, self.output.parent):
            self.assertFalse(path.exists())
        self.assertEqual(len(self.journal()["integrations"]), 2)

    def test_no_force_still_requires_completed(self):
        with self.assertRaisesRegex(TaskError, "not completed"):
            cli.cleanup("DEV-7")
        self.assert_preserved()

    def test_branch_selector_never_promotes_unpublished_disposal(self):
        with self.assertRaisesRegex(TaskError, "No verified merged PR"):
            cli.cleanup("DEV-7", force=True, branch=self.branch)
        self.assert_preserved()
        self.assertTrue(self.workspace_active)
        self.workspace_close_error = TaskError("close interrupted")
        with self.assertRaisesRegex(TaskError, "close interrupted"):
            cli.cleanup("DEV-7", force=True)
        before = self.journal()
        with self.assertRaisesRegex(TaskError, "Pending unpublished disposal must resume without --branch"):
            cli.cleanup("DEV-7", force=True, branch=self.branch)
        self.assertEqual(self.journal(), before)
        self.assert_preserved()
        self.workspace_close_error = None
        cli.cleanup("DEV-7", force=True)
        self.assert_disposed()

    def test_publication_evidence_refuses_before_workspace_close(self):
        saved = self.publication.read()
        for evidence in ("remote", "pr", "intent", "history", "cached", "tracking"):
            with self.subTest(evidence=evidence):
                if evidence == "remote":
                    self.remote_branches.return_value = [self.branch]
                if evidence == "pr":
                    self.pulls.return_value = [dict(number=1)]
                changed = dict(saved)
                if evidence == "intent":
                    changed["intent"] = dict(publishing_head=self.other_head)
                if evidence == "history":
                    changed["publication_history"] = "unknown"
                self.publication.write(changed)
                if evidence == "cached":
                    self.command(self.repo, "update-ref", f"refs/remotes/origin/{self.branch}", self.other_head)
                if evidence == "tracking":
                    self.command(self.repo, "config", f"branch.{self.branch}.remote", "origin")
                with self.assertRaisesRegex(TaskError, "[Pp]ublication|PR history"):
                    cli.cleanup("DEV-7", force=True)
                self.assert_preserved()
                self.assertTrue(self.workspace_active)
                self.remote_branches.return_value, self.pulls.return_value = [], []
                self.publication.write(saved)
                if evidence == "cached":
                    self.command(self.repo, "update-ref", "-d", f"refs/remotes/origin/{self.branch}")
                if evidence == "tracking":
                    self.command(self.repo, "config", "--unset", f"branch.{self.branch}.remote")

    def assert_cached_publication_refused(self, message):
        refs = self.command(self.repo, "show-ref")
        contexts = self.registry.list(include_retired=True)
        conflict = (self.isolated / "conflict.txt").read_bytes()
        output = self.output.read_bytes()
        with patch("task_start.cleanup.shutil.rmtree") as remove, self.assertRaisesRegex(TaskError, message):
            cli.cleanup("DEV-7", force=True)
        remove.assert_not_called()
        self.assert_preserved()
        self.assertTrue(self.workspace_active)
        self.assertFalse(any(c.args[0] == "close" for c in self.herdr_workspace.call_args_list))
        self.assertEqual(self.command(self.repo, "show-ref"), refs)
        self.assertEqual(self.registry.list(include_retired=True), contexts)
        self.assertTrue(self.publication.directory.is_dir())
        self.assertEqual((self.isolated / "conflict.txt").read_bytes(), conflict)
        self.assertEqual(self.output.read_bytes(), output)

    def test_cached_task_refs_under_slash_remote_names_preserve_execution(self):
        self.retained_integration()
        self.command(self.repo, "config", "extensions.worktreeConfig", "true")
        for remote in ("team/origin", "team/nested/origin"):
            ref = f"refs/remotes/{remote}/{self.branch}"
            for checkout, scope in ((self.repo, "--local"), (self.repo, "--worktree"),
                                    (self.path, "--worktree")):
                with self.subTest(remote=remote, checkout=checkout, scope=scope):
                    self.command(checkout, "config", scope, f"remote.{remote}.url", "git@github.com:example/project.git")
                    self.command(checkout, "config", scope, f"remote.{remote}.fetch",
                                 f"+refs/heads/*:refs/remotes/{remote}/*")
                    self.command(self.repo, "update-ref", ref, self.other_head)
                    self.assert_cached_publication_refused("Retained remote-tracking task refs are publication evidence")
                    self.command(self.repo, "update-ref", "-d", ref)
                    self.command(checkout, "config", scope, "--remove-section", f"remote.{remote}")

    def test_custom_or_ambiguous_fetch_mappings_preserve_cached_publication(self):
        self.retained_integration()
        self.command(self.repo, "config", "extensions.worktreeConfig", "true")
        for checkout in (self.repo, self.path):
            for spec, ref in (
                    (f"+refs/heads/{self.branch}:refs/remotes/team/origin/published-alias",
                     "refs/remotes/team/origin/published-alias"),
                    ("+refs/heads/dev-7-*:refs/task-cache/*", "refs/task-cache/original-title"),
                    (f"^refs/heads/{self.branch}", f"refs/remotes/origin/{self.branch}"),
                    ("", f"refs/remotes/origin/{self.branch}"),
                    ("not a refspec", f"refs/remotes/origin/{self.branch}")):
                with self.subTest(checkout=checkout, spec=spec):
                    # Common config still supplies the default mapping; an
                    # additional effective value must never hide retained refs.
                    self.command(checkout, "config", "--worktree", "remote.origin.fetch", spec)
                    self.command(self.repo, "update-ref", ref, self.other_head)
                    try:
                        # Git itself rejects malformed syntax before namespace
                        # classification. Both refusals must preserve everything.
                        message = "git remote failed" if spec == "not a refspec" else "fetch refspecs.*cached publication"
                        self.assert_cached_publication_refused(message)
                    finally:
                        self.command(self.repo, "update-ref", "-d", ref)
                        self.command(checkout, "config", "--worktree", "--unset", "remote.origin.fetch")

    def test_orphaned_or_unmapped_cached_remote_refs_refuse(self):
        self.retained_integration()
        ref = f"refs/remotes/team/origin/{self.branch}"
        self.command(self.repo, "update-ref", ref, self.other_head)
        self.assert_cached_publication_refused("publication namespace is unknown or ambiguous")
        self.command(self.repo, "config", "remote.team/origin.url", "git@github.com:example/project.git")
        self.assert_cached_publication_refused("publication namespace is unknown or ambiguous")

    def test_overlapping_cached_remote_namespaces_refuse(self):
        self.retained_integration()
        for remote in ("team", "team/origin"):
            self.command(self.repo, "remote", "add", remote, "git@github.com:example/project.git")
        self.command(self.repo, "update-ref", "refs/remotes/team/origin/main", self.other_head)
        self.assert_cached_publication_refused("publication namespace is unknown or ambiguous")

    def test_unrelated_cached_refs_under_slash_remote_name_allow_cleanup(self):
        self.command(self.repo, "remote", "add", "team/origin", "git@github.com:example/project.git")
        refs = [f"refs/remotes/team/origin/{branch}" for branch in ("main", "dev-70-other", "topic/dev-7-other")]
        for ref in refs:
            self.command(self.repo, "update-ref", ref, self.other_head)
        cli.cleanup("DEV-7", force=True)
        self.assert_disposed()
        for ref in refs:
            self.assertEqual(self.command(self.repo, "rev-parse", ref), self.other_head)

    def test_cached_slash_remote_task_ref_appearing_during_preflight_stops_cleanup(self):
        self.retained_integration()
        self.command(self.repo, "remote", "add", "team/origin", "git@github.com:example/project.git")
        ref = f"refs/remotes/team/origin/{self.branch}"
        contexts = self.registry.list(include_retired=True)
        conflict = (self.isolated / "conflict.txt").read_bytes()
        output = self.output.read_bytes()
        def pulls(*args):
            self.command(self.repo, "update-ref", ref, self.other_head)
            return []
        self.pulls.side_effect = pulls
        with patch("task_start.cleanup.shutil.rmtree") as remove, \
                self.assertRaisesRegex(TaskError, "Retained remote-tracking task refs are publication evidence"):
            cli.cleanup("DEV-7", force=True)
        remove.assert_not_called()
        self.assert_preserved()
        self.assertTrue(self.workspace_active)
        self.assertFalse(any(c.args[0] == "close" for c in self.herdr_workspace.call_args_list))
        self.assertEqual(self.command(self.repo, "rev-parse", ref), self.other_head)
        self.assertEqual(self.registry.list(include_retired=True), contexts)
        self.assertEqual((self.isolated / "conflict.txt").read_bytes(), conflict)
        self.assertEqual(self.output.read_bytes(), output)

    def test_unrelated_pane_or_context_moved_terminal_and_live_process_refuse(self):
        panes = copy.deepcopy(self.panes)
        for fault in ("unrelated", "moved", "replacement", "foreground", "background", "endpoint"):
            with self.subTest(fault=fault):
                self.panes[:] = copy.deepcopy(panes)
                self.identities.command.side_effect = self.process_info
                self.process.side_effect = lambda paths, shells, **kw: {str(pid): 1234 for pid in shells}
                self.identities.endpoint.return_value = "/tmp/test-herdr.sock"
                if fault == "unrelated":
                    self.panes[-1]["workspace_id"] = self.workspace_id
                if fault == "moved":
                    self.panes[0]["workspace_id"] = "other"
                if fault == "replacement":
                    self.panes[0]["terminal_id"] = "replacement"
                if fault == "foreground":
                    self.identities.command.side_effect = lambda *a: dict(process_info=dict(
                        pane_id=a[-1], shell_pid=101, foreground_process_group_id=999,
                        foreground_processes=[dict(pid=999)]))
                if fault == "background":
                    self.process.side_effect = TaskError("background execution remains")
                if fault == "endpoint":
                    self.identities.endpoint.return_value = "/tmp/other-herdr.sock"
                with self.assertRaises(TaskError):
                    cli.cleanup("DEV-7", force=True)
                self.assert_preserved()
                self.assertTrue(self.workspace_active)

    def test_exec_replaced_shell_pid_never_authorizes_workspace_close(self):
        for argv in (["ssh", "host"], ["codex"], ["bash", "-c", "read input"],
                     ["bash", "script.sh"], [], None):
            with self.subTest(argv=argv):
                def replaced(*args):
                    result = self.process_info(*args)
                    result["process_info"]["foreground_processes"][0]["argv"] = argv
                    return result
                self.identities.command.side_effect = replaced
                with self.assertRaisesRegex(TaskError, "shell"):
                    cli.cleanup("DEV-7", force=True)
                self.assert_preserved()
                self.assertTrue(self.workspace_active)
                self.assertFalse(any(c.args[0] == "close" for c in self.herdr_workspace.call_args_list))

    def detached_process_evidence(self):
        self.proc = self.repo.parent / "proc"
        self.proc.mkdir()
        process_mounts(self.proc)
        self.workspace_active = False
        self.panes[:] = [p for p in self.panes if p["workspace_id"] != self.workspace_id]
        self.process.side_effect = lambda paths, shells, **kw: stopped_execution(paths, shells, proc=self.proc, **kw)

    def test_unmanaged_host_references_and_external_aliases_are_outside_v1(self):
        self.retained_integration()
        self.detached_process_evidence()
        alias = self.repo.parent / "unmanaged-view"
        self.mountinfo.write_bytes(mount_table() + mount_record(21, self.isolated, alias))
        DisposalProcessTests.process(self, 125, 1, alias, argv="worker\0",
                                     executable=str(self.isolated / "conflict.txt"))
        (self.proc / "125" / "maps").unlink()
        with foreign_process_owners(self.proc, 125):
            cli.cleanup("DEV-7", force=True)
        self.assert_disposed()
        self.assertFalse(self.isolated.exists())

    def test_final_process_guard_refuses_new_job_in_closed_execution_session(self):
        self.retained_integration()
        self.proc = self.repo.parent / "proc"
        self.proc.mkdir()
        process_mounts(self.proc)
        pids = []
        for pane in self.panes:
            if pane["workspace_id"] == self.workspace_id:
                pid = self.process_info("pane", "process-info", "--pane", pane["pane_id"])["process_info"]["shell_pid"]
                DisposalProcessTests.process(self, pid, 1, Path(pane["cwd"]))
                pids.append(pid)
        self.process.side_effect = lambda paths, shells, **kw: stopped_execution(paths, shells, proc=self.proc, **kw)
        def close_shells(operation, *args):
            result = self.workspace_commands(operation, *args)
            if operation == "close":
                for pid in pids:
                    shutil.rmtree(self.proc / str(pid))
            return result
        self.herdr_workspace.side_effect = close_shells
        injected = False
        def final_mounts(paths):
            nonlocal injected
            check_mounts(paths)
            if len(paths) > 1 and not injected and any(r["state"] == "removing" for r in self.journal()["roots"]):
                DisposalProcessTests.process(self, 999, 1, self.other, argv="worker\0")
                stat = self.proc / "999" / "stat"
                fields = stat.read_text().rsplit(")", 1)[1].split()
                fields[3] = str(pids[0])
                stat.write_text("999 (worker) " + " ".join(fields))
                injected = True
        with patch("task_start.cleanup.check_mounts", side_effect=final_mounts), \
                self.assertRaisesRegex(TaskError, "Process 999"):
            cli.cleanup("DEV-7", force=True)
        self.assertTrue(injected)
        self.assert_preserved()
        self.assertTrue(self.isolated.exists())

    def test_stopped_execution_is_disposed_with_unrelated_kernel_and_userspace_tasks(self):
        self.retained_integration()
        self.detached_process_evidence()
        DisposalProcessTests.kernel_task(self, 73)
        DisposalProcessTests.kernel_task(self, 74, flags=0x00200020, comm="kworker/0:1")
        DisposalProcessTests.process(self, 125, 1, self.other, argv="unrelated\0")
        with foreign_process_owners(self.proc, 73, 74, 125):
            cli.cleanup("DEV-7", force=True)
        self.assert_disposed()
        self.assertFalse(self.isolated.parent.exists())
        self.assertFalse(self.output.parent.exists())

    def test_unrelated_same_device_mount_and_unused_alias_do_not_block_cleanup(self):
        self.detached_process_evidence()
        alias, unrelated = self.repo.parent / "task-view", self.repo.parent / "other-view"
        self.mountinfo.write_bytes(mount_table() + mount_record(21, self.path, alias)
                                  + mount_record(22, self.other, unrelated))
        DisposalProcessTests.process(self, 125, 1, unrelated, argv="unrelated\0")
        cli.cleanup("DEV-7", force=True)
        self.assert_disposed()

    def test_no_alias_and_no_live_execution_still_cleans_all_artifacts(self):
        self.retained_integration()
        self.detached_process_evidence()
        cli.cleanup("DEV-7", force=True)
        self.assert_disposed()
        self.assertFalse(self.isolated.parent.exists())
        self.assertFalse(self.output.parent.exists())

    def test_foreign_descendant_of_task_or_integration_shell_preserves_execution(self):
        self.retained_integration()
        self.proc = self.repo.parent / "proc"
        self.proc.mkdir()
        process_mounts(self.proc)
        self.process.side_effect = lambda paths, shells, **kw: stopped_execution(paths, shells, proc=self.proc, **kw)
        task_shells = []
        for pane in self.panes:
            if pane["workspace_id"] == self.workspace_id:
                pid = self.process_info("pane", "process-info", "--pane", pane["pane_id"])["process_info"]["shell_pid"]
                DisposalProcessTests.process(self, pid, 1, Path(pane["cwd"]))
                task_shells.append(pid)
        self.assertTrue(any(p["cwd"] == str(self.isolated) for p in self.panes))
        for parent in task_shells:
            with self.subTest(parent=parent):
                DisposalProcessTests.process(self, 999, parent, self.other, argv="unrelated-looking-worker\0")
                with foreign_process_owners(self.proc, 999), patch("task_start.cleanup.shutil.rmtree") as remove, \
                        self.assertRaisesRegex(TaskError, "Process 999"):
                    cli.cleanup("DEV-7", force=True)
                remove.assert_not_called()
                self.assertTrue(self.workspace_active)
                self.assert_preserved()
                self.assertTrue(self.isolated.is_dir())
                self.assertTrue(self.output.is_file())
                shutil.rmtree(self.proc / "999")

    def test_retry_cannot_assume_legacy_claim_proved_closed_shell_sessions(self):
        self.retained_integration()
        self.workspace_close_error = TaskError("interrupted close")
        with self.assertRaisesRegex(TaskError, "interrupted close"):
            cli.cleanup("DEV-7", force=True)
        claim = self.journal()
        claim["runtime"].pop("process_proof")
        self.journal_path().write_text(json.dumps(claim))
        self.workspace_close_error = None
        with patch("task_start.cleanup.shutil.rmtree") as remove, \
                self.assertRaisesRegex(TaskError, "predates the registered-shell process proof"):
            cli.cleanup("DEV-7", force=True)
        remove.assert_not_called()
        self.assert_preserved()
        self.assertTrue(self.isolated.is_dir())
        self.assertTrue(self.output.is_file())

    def test_effective_branch_config_in_each_checkout_refuses_disposal(self):
        self.command(self.repo, "config", "extensions.worktreeConfig", "true")
        for checkout in (self.repo, self.path):
            for key, value in (("remote", "origin"), ("merge", "refs/heads/main"),
                               ("pushRemote", "origin"), ("remote", "")):
                with self.subTest(checkout=checkout, key=key, value=value):
                    option = f"branch.{self.branch}.{key}"
                    self.command(checkout, "config", "--worktree", option, value)
                    with self.assertRaisesRegex(TaskError, "tracking configuration is publication evidence"):
                        cli.cleanup("DEV-7", force=True)
                    self.assert_preserved()
                    self.assertTrue(self.workspace_active)
                    self.assertFalse(any(c.args[0] == "close" for c in self.herdr_workspace.call_args_list))
                    self.command(checkout, "config", "--worktree", "--unset", option)

    def test_worktree_branch_config_added_during_cleanup_stops_before_close(self):
        self.command(self.repo, "config", "extensions.worktreeConfig", "true")
        self.pulls.side_effect = lambda *args: self.command(
            self.path, "config", "--worktree", f"branch.{self.branch}.merge", "refs/heads/main") or []
        with self.assertRaisesRegex(TaskError, "tracking configuration is publication evidence"):
            cli.cleanup("DEV-7", force=True)
        self.assert_preserved()
        self.assertTrue(self.workspace_active)

    def test_push_refspecs_preserve_mapped_remote_refs_and_pr_history(self):
        self.retained_integration()
        self.command(self.repo, "config", "extensions.worktreeConfig", "true")
        contexts = self.registry.list(include_retired=True)
        conflict = (self.isolated / "conflict.txt").read_bytes()
        output = self.output.read_bytes()
        alias = "published-alias"
        option = "remote.origin.push"
        original = Git.command
        self.remote_branches.side_effect = lambda identifier: self.remote_branch_lookup(self.git, identifier)
        for checkout, scope in ((self.repo, "--local"), (self.repo, "--worktree"),
                                (self.path, "--worktree")):
            self.command(checkout, "config", scope, option, f"refs/heads/{self.branch}:refs/heads/{alias}")
            for evidence in ("remote", "cached", "open", "closed"):
                with self.subTest(checkout=checkout, scope=scope, evidence=evidence):
                    def command(git, *args):
                        if args == ("ls-remote", "--heads", "--", "origin"):
                            return f"{self.other_head}\trefs/heads/{alias}\n" if evidence == "remote" else ""
                        return original(git, *args)
                    # Only the differently named destination has publication
                    # evidence; querying the original task branch finds no PR.
                    self.pulls.side_effect = lambda repository, branch: (
                        [dict(number=1, state=evidence)]
                        if branch == alias and evidence in {"open", "closed"} else [])
                    if evidence == "cached":
                        self.command(self.repo, "update-ref", f"refs/remotes/origin/{alias}", self.other_head)
                    with patch.object(Git, "command", command), \
                            patch("task_start.cleanup.shutil.rmtree") as remove, \
                            self.assertRaisesRegex(TaskError, "push refspecs.*publication exclusion ambiguous"):
                        cli.cleanup("DEV-7", force=True)
                    remove.assert_not_called()
                    self.assert_preserved()
                    self.assertTrue(self.workspace_active)
                    self.assertFalse(any(c.args[0] == "close" for c in self.herdr_workspace.call_args_list))
                    self.assertEqual(self.registry.list(include_retired=True), contexts)
                    self.assertTrue(self.publication.directory.is_dir())
                    self.assertEqual((self.isolated / "conflict.txt").read_bytes(), conflict)
                    self.assertEqual(self.output.read_bytes(), output)
                    if evidence == "cached":
                        self.assertEqual(self.command(self.repo, "rev-parse", f"refs/remotes/origin/{alias}"),
                                         self.other_head)
                        self.command(self.repo, "update-ref", "-d", f"refs/remotes/origin/{alias}")
            self.command(checkout, "config", scope, "--unset-all", option)

    def test_ambiguous_and_multivalued_push_refspecs_in_each_checkout_refuse(self):
        self.command(self.repo, "config", "extensions.worktreeConfig", "true")
        for checkout in (self.repo, self.path):
            for remote in ("origin", "secondary.remote"):
                option = f"remote.{remote}.push"
                for specs in ((f"{self.branch}:published-alias",), ("+HEAD:refs/heads/published-alias",),
                              ("refs/heads/dev-7-*:refs/heads/published-*",), (":",), ("+:",),
                              (f"{self.other_head}:refs/heads/published-alias",),
                              (":refs/heads/published-alias",), ("",), ("not a refspec",),
                              (f"refs/heads/{self.branch}:refs/heads/published-alias",
                               "refs/heads/main:refs/heads/main")):
                    with self.subTest(checkout=checkout, remote=remote, specs=specs):
                        for spec in specs:
                            self.command(checkout, "config", "--worktree", "--add", option, spec)
                        with self.assertRaisesRegex(TaskError, "push refspecs.*publication exclusion ambiguous"):
                            cli.cleanup("DEV-7", force=True)
                        self.assert_preserved()
                        self.assertTrue(self.workspace_active)
                        self.command(checkout, "config", "--worktree", "--unset-all", option)
        self.assertFalse(any(c.args[0] == "close" for c in self.herdr_workspace.call_args_list))

    def test_push_refspec_from_task_conditional_include_refuses(self):
        included = self.repo.parent / "task-push.inc"
        included.write_text(f'[remote "origin"]\n\tpush = refs/heads/{self.branch}:refs/heads/published-alias\n')
        self.command(self.repo, "config", f"includeIf.onbranch:{self.branch}.path", str(included))
        with self.assertRaisesRegex(TaskError, "push refspecs.*publication exclusion ambiguous"):
            cli.cleanup("DEV-7", force=True)
        self.assert_preserved()
        self.assertTrue(self.workspace_active)
        self.assertFalse(any(c.args[0] == "close" for c in self.herdr_workspace.call_args_list))

    def test_push_refspec_added_after_workspace_close_stops_content_removal(self):
        self.retained_integration()
        self.command(self.repo, "config", "extensions.worktreeConfig", "true")
        contexts = self.registry.list(include_retired=True)
        conflict = (self.isolated / "conflict.txt").read_bytes()
        output = self.output.read_bytes()
        def workspace(operation, *args):
            result = self.workspace_commands(operation, *args)
            if operation == "close":
                self.command(self.path, "config", "--worktree", "remote.origin.push",
                             f"refs/heads/{self.branch}:refs/heads/published-alias")
            return result
        self.herdr_workspace.side_effect = workspace
        with patch("task_start.cleanup.shutil.rmtree") as remove, \
                self.assertRaisesRegex(TaskError, "push refspecs.*publication exclusion ambiguous"):
            cli.cleanup("DEV-7", force=True)
        remove.assert_not_called()
        self.assertFalse(self.workspace_active)
        self.assert_preserved()
        self.assertEqual(self.registry.list(include_retired=True), contexts)
        self.assertTrue(self.publication.directory.is_dir())
        self.assertEqual((self.isolated / "conflict.txt").read_bytes(), conflict)
        self.assertEqual(self.output.read_bytes(), output)
        self.assertEqual(self.journal()["state"], "pending")

    def test_unrelated_registry_claim_refuses_even_if_its_terminal_is_absent(self):
        self.registry.allocate("DEV-9", "review", agent="codex", worktree=str(self.path),
                               endpoint="/tmp/test-herdr.sock", workspace_id=self.workspace_id)
        with self.assertRaisesRegex(TaskError, "Another task context"):
            cli.cleanup("DEV-7", force=True)
        self.assert_preserved()
        self.assertTrue(self.workspace_active)

    def test_foreign_abandoned_g_claim_blocks_even_with_retirement_timestamp(self):
        self.retained_integration()
        foreign = self.registry.allocate("DEV-8", "integration", agent="codex",
            repository=str(self.repo), worktree=str(self.isolated), endpoint="/tmp/test-herdr.sock",
            workspace_id="absent-old-workspace")
        self.registry.update(foreign, state="uncertain")
        self.registry.abandon_integration(self.registry.get(foreign), now())
        with self.assertRaisesRegex(TaskError, "Another task context claims"):
            cli.cleanup("DEV-7", force=True)
        self.assert_preserved()
        self.assertTrue(self.isolated.exists())
        self.assertTrue(self.registry.get(foreign)["retired_at"])

    def test_runtime_retirement_alone_does_not_release_foreign_g_artifacts(self):
        self.retained_integration()
        foreign = self.registry.allocate("DEV-8", "integration", agent="codex",
            repository=str(self.repo), worktree=str(self.isolated), endpoint="/other", workspace_id="gone")
        self.registry.retire("DEV-8", self.repo, self.isolated, endpoint="/other", workspace_id="gone")
        self.assertEqual(self.registry.get(foreign)["state"], "retired")
        with self.assertRaisesRegex(TaskError, "Another task context claims"):
            cli.cleanup("DEV-7", force=True)
        self.assert_preserved()

    def test_own_retired_g_runtime_artifacts_are_released_only_by_disposal(self):
        integration = self.retained_integration()
        context = self.registry.get(integration["context"]["context_id"])
        self.registry.retire("DEV-7", self.repo, self.isolated,
                             endpoint=context["endpoint"], workspace_id=context["workspace_id"])
        self.panes[:] = [p for p in self.panes if p["terminal_id"] != context["terminal_id"]]
        self.assertTrue(self.isolated.exists())
        cli.cleanup("DEV-7", force=True)
        self.assert_disposed()
        self.assertFalse(self.isolated.exists())
        self.assertEqual(self.journal()["integrations"][0]["context_id"], context["context_id"])


    def test_foreign_claim_on_private_git_metadata_blocks(self):
        self.registry.allocate("DEV-8", "review", agent="codex", repository=str(self.repo),
                               worktree=str(self.publication.directory / "objects"))
        with self.assertRaisesRegex(TaskError, "Another task context claims"):
            cli.cleanup("DEV-7", force=True)
        self.assert_preserved()

    def test_preparing_foreign_integration_provenance_blocks_without_g_context(self):
        own = self.retained_integration()
        foreign = copy.deepcopy(own)
        foreign.update(pass_id=str(uuid4()), state="preparing", context=None)
        foreign["binding"].update(issue="DEV-8", worktree=str(self.other), branch="dev-8-unrelated",
                                  workspace_id="other")
        IntegrationStore(self.other).write(foreign)
        with self.assertRaisesRegex(TaskError, "Another integration provenance claim"):
            cli.cleanup("DEV-7", force=True)
        self.assert_preserved()
        self.assertTrue(self.isolated.exists())

    def test_foreign_registered_alias_claim_blocks_without_host_process_scanning(self):
        self.retained_integration()
        alias = self.repo.parent / "registered-view"
        self.mountinfo.write_bytes(mount_table() + mount_record(21, self.isolated, alias))
        self.registry.allocate("DEV-8", "integration", agent="codex", repository=str(self.repo),
                               worktree=str(alias / "nested"))
        with self.assertRaisesRegex(TaskError, "Another task context claims"):
            cli.cleanup("DEV-7", force=True)
        self.assert_preserved()

    def test_completed_runtime_tombstone_does_not_claim_reused_task_path(self):
        foreign = self.registry.allocate("DEV-8", "review", agent="codex", repository=str(self.repo),
            worktree=str(self.path), endpoint="/other", workspace_id="old")
        self.registry.retire("DEV-8", self.repo, self.path, endpoint="/other", workspace_id="old")
        before = self.registry.get(foreign)
        cli.cleanup("DEV-7", force=True)
        self.assert_disposed()
        self.assertEqual(self.registry.get(foreign), before)

    def test_pending_disposal_reservation_survives_controller_exit(self):
        self.retained_integration()
        self.workspace_close_error = TaskError("interrupted close")
        with self.assertRaisesRegex(TaskError, "interrupted close"):
            cli.cleanup("DEV-7", force=True)
        for path in (self.path, self.isolated, self.publication.directory):
            with self.subTest(path=path), self.assertRaisesRegex(TaskError, "Pending cleanup reserves"):
                self.registry.allocate("DEV-8", "integration", agent="codex",
                                       repository=str(self.repo), worktree=str(path))
        self.assert_preserved()

    def test_overlapping_foreign_pending_journal_blocks_retry(self):
        self.workspace_close_error = TaskError("interrupted close")
        with self.assertRaisesRegex(TaskError, "interrupted close"):
            cli.cleanup("DEV-7", force=True)
        foreign = copy.deepcopy(self.journal())
        foreign.update(issue="DEV-8", execution_id=str(uuid4()), branch="dev-8-unrelated",
                       contexts=[], integrations=[], roots=[], workspace_id="other")
        journal = DisposalStore(self.git, "DEV-8", foreign["execution_id"])
        journal.write(foreign)
        before = journal.path.read_bytes()
        self.workspace_close_error = None
        with self.assertRaisesRegex(TaskError, "Another pending disposal reserves"):
            cli.cleanup("DEV-7", force=True)
        self.assert_preserved()
        self.assertEqual(journal.path.read_bytes(), before)

    def test_new_g_claim_at_reused_path_is_not_released_by_old_completed_journal(self):
        self.retained_integration()
        old_checkout = self.isolated
        cli.cleanup("DEV-7", force=True)
        completed = self.journal()
        self.restart_execution()
        foreign = self.registry.allocate("DEV-8", "integration", agent="codex", repository=str(self.repo),
            worktree=str(old_checkout), endpoint="/other", workspace_id="gone")
        self.registry.retire("DEV-8", self.repo, old_checkout, endpoint="/other", workspace_id="gone")
        from task_start.cleanup import context_claimed
        self.assertTrue(context_claimed(self.registry.get(foreign), [completed]))
        old = next(c for c in completed["contexts"] if c["role"] == "integration")
        self.assertFalse(context_claimed(old, [completed]))

    def test_workspace_lock_excludes_foreign_allocation_during_final_preflight(self):
        injected = False
        def final_mounts(paths):
            nonlocal injected
            check_mounts(paths)
            if len(paths) > 1 and not injected:
                with ThreadPoolExecutor(max_workers=1) as pool:
                    future = pool.submit(self.registry.allocate, "DEV-8", "integration", agent="codex",
                                         repository=str(self.repo), worktree=str(self.path))
                    with self.assertRaisesRegex(TaskError, "owns this worktree|Pending cleanup"):
                        future.result()
                injected = True
        with patch("task_start.cleanup.check_mounts", side_effect=final_mounts):
            cli.cleanup("DEV-7", force=True)
        self.assertTrue(injected)
        self.assert_disposed()

    def test_older_absent_workspace_context_is_retired(self):
        context = self.registry.allocate("DEV-7", "review", agent="codex", repository=str(self.repo),
            worktree=str(self.path), endpoint="/tmp/test-herdr.sock", workspace_id="old-absent",
            pane_id="old-pane", terminal_id="old-terminal")
        cli.cleanup("DEV-7", force=True)
        self.assert_disposed()
        self.assertEqual(self.registry.get(context)["state"], "retired")

    def test_publication_lock_refuses_concurrent_controller(self):
        with self.publication.locked(), self.assertRaisesRegex(TaskError, "owns this worktree"):
            cli.cleanup("DEV-7", force=True)
        self.assert_preserved()

    def test_alternate_push_destination_refuses(self):
        self.command(self.repo, "remote", "set-url", "--push", "origin", "git@github.com:another/project.git")
        with self.assertRaisesRegex(TaskError, "fetch/push destination"):
            cli.cleanup("DEV-7", force=True)
        self.assert_preserved()
        self.assertTrue(self.workspace_active)

    def test_durable_archive_failure_precedes_all_destructive_actions(self):
        self.retained_integration()
        with patch.object(DisposalStore, "write", side_effect=TaskError("fsync failed")), \
                self.assertRaisesRegex(TaskError, "fsync failed"):
            cli.cleanup("DEV-7", force=True)
        self.assert_preserved()
        self.assertTrue(self.workspace_active)
        self.assertTrue(self.isolated.exists())

    def test_missing_integration_evidence_preserves_everything(self):
        self.retained_integration()
        IntegrationStore(self.path).path.unlink()
        contexts = self.registry.list(include_retired=True)
        conflict = (self.isolated / "conflict.txt").read_bytes()
        output = self.output.read_bytes()
        # Without matching provenance, the G context is not selected for this
        # disposal. Its pane's checkout is therefore outside the authorized
        # workspace scope, and that guard refuses before context reconciliation.
        with patch("task_start.cleanup.shutil.rmtree") as remove, \
                self.assertRaisesRegex(TaskError,
                    "Task terminal moved or unrelated work occupies the workspace; disposal refused"):
            cli.cleanup("DEV-7", force=True)
        remove.assert_not_called()
        self.assert_preserved()
        self.assertTrue(self.workspace_active)
        self.assertFalse(any(c.args[0] == "close" for c in self.herdr_workspace.call_args_list))
        self.assertEqual(self.registry.list(include_retired=True), contexts)
        self.assertTrue(self.publication.directory.is_dir())
        self.assertTrue(self.isolated.exists())
        self.assertEqual((self.isolated / "conflict.txt").read_bytes(), conflict)
        self.assertEqual(self.output.read_bytes(), output)

    def test_replaced_isolated_history_is_not_disposable(self):
        self.retained_integration()
        self.command(self.isolated, "config", "user.name", "Unrelated replacement")
        with self.assertRaisesRegex(TaskError, "Git identity changed"):
            cli.cleanup("DEV-7", force=True)
        self.assert_preserved()
        self.assertTrue(self.isolated.exists())

    def test_symlinked_integration_checkout_is_never_followed(self):
        self.retained_integration()
        moved = self.isolated.with_name("moved")
        self.isolated.rename(moved)
        self.isolated.symlink_to(self.other, target_is_directory=True)
        with self.assertRaises(TaskError):
            cli.cleanup("DEV-7", force=True)
        self.assert_preserved()
        self.assertTrue(moved.is_dir())

    def test_same_filesystem_mount_in_any_disposal_tree_preserves_all_contents(self):
        self.retained_integration()
        nested = self.path / "bind space\\name\nline"
        nested.mkdir()
        valuable = nested / "valuable"
        valuable.write_text("unrelated mounted data")
        self.assertEqual(nested.stat().st_dev, self.path.stat().st_dev)
        for point in (nested, self.path, self.isolated / ".git" / "objects", self.isolated.parent,
                      self.output, self.output.parent, self.publication.directory / "logs",
                      self.publication.directory):
            with self.subTest(point=point), patch.object(Path, "is_mount", return_value=False), \
                    patch("task_start.cleanup.shutil.rmtree") as remove:
                self.mountinfo.write_bytes(mount_table(point))
                with self.assertRaisesRegex(TaskError, "mount boundary"):
                    cli.cleanup("DEV-7", force=True)
                remove.assert_not_called()
                self.assert_preserved()
                self.assertTrue(self.workspace_active)
                self.assertTrue(self.isolated.is_dir())
                self.assertTrue(self.output.is_file())
                self.assertEqual(valuable.read_text(), "unrelated mounted data")
                self.assertFalse(any(c.args[0] == "close" for c in self.herdr_workspace.call_args_list))

    def test_nested_bare_repositories_preserve_execution_before_workspace_close(self):
        for location in ("task", "integration", "integration_git", "task_git"):
            with self.subTest(location=location), ForcedCleanupTests() as case:
                case.retained_integration()
                roots = dict(task=case.path, integration=case.isolated,
                             integration_git=case.isolated / ".git", task_git=case.publication.directory)
                nested = roots[location] / "vendor" / "retained-repository"
                nested.parent.mkdir()
                # An ordinary bare repository with real objects/refs, no .git
                # entry, and no filename suffix on which detection could depend.
                case.command(case.repo, "clone", "--bare", "--no-local", str(case.other), str(nested))
                case.assertFalse((nested / ".git").exists())
                case.assertEqual(case.command(nested, "rev-parse", "--is-bare-repository"), "true")
                before = {p.relative_to(nested): p.read_bytes() for p in nested.rglob("*") if p.is_file()}
                contexts = case.registry.list(include_retired=True)
                with patch("task_start.cleanup.shutil.rmtree") as remove, \
                        case.assertRaisesRegex(TaskError, "Nested bare repository"):
                    cli.cleanup("DEV-7", force=True)
                remove.assert_not_called()
                case.assert_preserved()
                case.assertTrue(case.workspace_active)
                case.assertFalse(any(c.args[0] == "close" for c in case.herdr_workspace.call_args_list))
                case.assertFalse(any(c.args[0][3:5] == ["worktree", "remove"] for c in case.runner.call_args_list))
                case.assertEqual(case.registry.list(include_retired=True), contexts)
                case.assertTrue(case.isolated.is_dir())
                case.assertTrue(case.output.is_file())
                case.assertEqual({p.relative_to(nested): p.read_bytes()
                                  for p in nested.rglob("*") if p.is_file()}, before)
                case.assertEqual(case.command(nested, "rev-parse", "HEAD"), case.other_head)

    def test_owned_integration_git_directory_remains_disposable(self):
        self.retained_integration()
        owned = self.isolated / ".git"
        self.assertTrue((owned / "HEAD").is_file())
        self.assertTrue((owned / "objects").is_dir())
        self.assertTrue((owned / "refs").is_dir())
        check_contents(self.isolated, git_root=self.isolated)
        cli.cleanup("DEV-7", force=True)
        self.assert_disposed()
        self.assertFalse(self.isolated.parent.exists())

    def test_ordinary_nested_directory_remains_disposable(self):
        self.retained_integration()
        for root in (self.path, self.isolated):
            nested = root / "ordinary" / "nested"
            nested.mkdir(parents=True)
            (nested / "discard").write_text("ordinary task contents")
        # Ancestor and similarly named sibling mounts are outside the deleted trees.
        self.mountinfo.write_bytes(mount_table(self.repo.parent) + mount_record(
            22, "/unrelated", self.path.with_name(self.path.name + "-other"), parent=21))
        cli.cleanup("DEV-7", force=True)
        self.assert_disposed()
        self.assertFalse(self.isolated.parent.exists())
        self.assertFalse(self.output.parent.exists())

    def test_missing_or_malformed_mount_metadata_preserves_all_contents(self):
        self.retained_integration()
        for data in (None, b"", b"malformed\n", mount_table() + b"incomplete"):
            with self.subTest(data=data), patch("task_start.cleanup.shutil.rmtree") as remove:
                if data is None:
                    self.mountinfo.unlink()
                else:
                    self.mountinfo.write_bytes(data)
                with self.assertRaisesRegex(TaskError, "Cannot read or parse Linux mount information"):
                    cli.cleanup("DEV-7", force=True)
                remove.assert_not_called()
                self.assert_preserved()
                self.assertTrue(self.workspace_active)
                self.assertTrue(self.isolated.is_dir())
                self.assertTrue(self.output.is_file())

    def test_mount_appearing_after_workspace_close_precedes_any_content_deletion(self):
        self.retained_integration()
        nested = self.path / "late-bind"
        nested.mkdir()
        original = self.workspace_commands
        def close_then_mount(operation, *args):
            result = original(operation, *args)
            if operation == "close":
                self.mountinfo.write_bytes(mount_table(nested))
            return result
        with patch.object(Herdr, "workspace_command", side_effect=close_then_mount), \
                patch("task_start.cleanup.shutil.rmtree") as remove, \
                self.assertRaisesRegex(TaskError, "mount boundary"):
            cli.cleanup("DEV-7", force=True)
        remove.assert_not_called()
        self.assert_preserved()
        self.assertTrue(self.isolated.is_dir())
        self.assertTrue(self.output.is_file())
        # Removing the boundary allows the same pending claim to finish.
        self.mountinfo.write_bytes(mount_table())
        cli.cleanup("DEV-7", force=True)
        self.assert_disposed()

    def test_multiple_issue_worktrees_refuse_before_closing_workspace(self):
        extra = self.repo.parent / "extra-slice"
        self.command(self.repo, "worktree", "add", "-b", "dev-7-extra-slice", str(extra))
        with self.assertRaisesRegex(TaskError, "Ambiguous"):
            cli.cleanup("DEV-7", force=True)
        self.assert_preserved()
        self.assertTrue(extra.exists())
        self.assertTrue(self.workspace_active)

    def test_unexpected_close_response_with_verified_closure_finishes_once(self):
        self.retained_loop()
        original = self.workspace_commands
        def uncertain(operation, *args):
            result = original(operation, *args)
            if operation == "close":
                raise HerdrResponseError("Unexpected Herdr workspace close response")
            return result
        self.herdr_workspace.side_effect = uncertain
        cli.cleanup("DEV-7", force=True)
        self.assert_disposed()
        before = self.journal_path().read_bytes()
        self.assertIn("already discarded", cli.cleanup("DEV-7", force=True))
        self.assertEqual(self.journal_path().read_bytes(), before)
        self.assertEqual(sum(c.args[0] == "close" for c in self.herdr_workspace.call_args_list), 1)

    def test_close_response_missing_identity_requires_authoritative_closure(self):
        original = self.workspace_commands
        def incomplete(operation, *args):
            result = original(operation, *args)
            if operation == "close":
                result.pop("workspace_id")
            return result
        self.herdr_workspace.side_effect = incomplete
        cli.cleanup("DEV-7", force=True)
        self.assert_disposed()
        self.assertEqual(sum(c.args[0] == "close" for c in self.herdr_workspace.call_args_list), 1)

    def test_unexpected_close_response_without_closure_keeps_pending_claim(self):
        self.workspace_close_error = HerdrResponseError("Unexpected Herdr workspace close response")
        with self.assertRaisesRegex(TaskError, "exact workspace closure is unconfirmed"):
            cli.cleanup("DEV-7", force=True)
        self.assert_preserved()
        self.assertEqual((self.journal()["state"], self.journal()["workspace_state"]), ("pending", "closing"))
        self.assertEqual(sum(c.args[0] == "close" for c in self.herdr_workspace.call_args_list), 1)
        self.workspace_close_error = None
        cli.cleanup("DEV-7", force=True)
        self.assert_disposed()

    def test_uncertain_closure_rejects_replacement_endpoint_git_and_terminal_evidence(self):
        for kind in ("workspace", "endpoint", "git", "terminal", "wrong_result", "unreadable"):
            with self.subTest(kind=kind), ForcedCleanupTests() as case:
                original = case.workspace_commands
                def uncertain(operation, *args):
                    result = original(operation, *args)
                    if operation != "close":
                        if kind == "unreadable" and not case.workspace_active:
                            raise TaskError("Workspace listing unavailable")
                        return result
                    if kind == "workspace":
                        case.extra_workspaces.append(case.workspace_entry("replacement"))
                    elif kind == "endpoint":
                        case.identities.endpoint.return_value = "/different.sock"
                    elif kind == "git":
                        case.command(case.path, "branch", "-m", "dev-7-replaced")
                    elif kind == "terminal":
                        case.panes.append(dict(workspace_id=case.workspace_id, pane_id="p1", terminal_id="term1",
                                               cwd=str(case.path), tab_id="t1"))
                    elif kind == "wrong_result":
                        return dict(type="workspace_closed", workspace_id="other")
                    raise HerdrResponseError("Unexpected Herdr workspace close response")
                case.herdr_workspace.side_effect = uncertain
                with case.assertRaises(TaskError):
                    cli.cleanup("DEV-7", force=True)
                case.assertTrue(case.path.exists())
                case.assertEqual(case.journal()["state"], "pending")
                case.assertEqual(case.journal()["workspace_state"], "closing")
                case.assertTrue(case.registry.list("DEV-7"))

    def test_close_result_lost_is_recovered_without_recreating_execution(self):
        original = self.workspace_commands
        def uncertain(operation, *args):
            result = original(operation, *args)
            if operation == "close":
                raise TaskError("close acknowledgement lost")
            return result
        with patch.object(Herdr, "workspace_command", side_effect=uncertain), \
                self.assertRaisesRegex(TaskError, "acknowledgement"):
            cli.cleanup("DEV-7", force=True)
        self.assertEqual(self.journal()["workspace_state"], "closing")
        self.assert_preserved()
        with self.assertRaisesRegex(TaskError, "pending or uncertain"):
            Herdr(self.repo).resolve_task(self.git, "DEV-7", include_remotes=False)
        cli.cleanup("DEV-7", force=True)
        self.assert_disposed()

    def test_interrupted_artifact_removal_can_continue_from_same_directory(self):
        self.retained_integration()
        rmtree = shutil.rmtree
        def interrupted(path, *args, **kwargs):
            if Path(path) == self.isolated.parent:
                (self.isolated / "conflict.txt").unlink()
                raise OSError("interrupted")
            return rmtree(path, *args, **kwargs)
        with patch("task_start.cleanup.shutil.rmtree", side_effect=interrupted), self.assertRaises(TaskError):
            cli.cleanup("DEV-7", force=True)
        self.assertEqual(self.journal()["roots"][0]["state"], "removing")
        self.assertTrue(self.path.exists())
        cli.cleanup("DEV-7", force=True)
        self.assert_disposed()

    def test_git_removal_and_branch_deletion_acknowledgement_loss_are_rerunnable(self):
        original = Git.command
        for operation in ("worktree", "update-ref"):
            with self.subTest(operation=operation), ForcedCleanupTests() as case:
                def uncertain(git, *args):
                    result = original(git, *args)
                    if (args[:3] == ("worktree", "remove", "--force") and operation == "worktree"
                            or args[:3] == ("update-ref", "--no-deref", "-d") and operation == "update-ref"):
                        raise TaskError("acknowledgement lost")
                    return result
                with patch.object(Git, "command", uncertain), self.assertRaisesRegex(TaskError, "acknowledgement"):
                    cli.cleanup("DEV-7", force=True)
                self.assertFalse(case.path.exists())
                cli.cleanup("DEV-7", force=True)
                case.assert_disposed()

    def __enter__(self):
        self.setUp()
        return self

    def __exit__(self, *args):
        self.doCleanups()

    def test_reused_path_after_partial_git_cleanup_refuses(self):
        original = Git.command
        def fail_branch(git, *args):
            if args[:3] == ("update-ref", "--no-deref", "-d"):
                raise TaskError("branch deletion failed")
            return original(git, *args)
        with patch.object(Git, "command", fail_branch), self.assertRaisesRegex(TaskError, "branch deletion"):
            cli.cleanup("DEV-7", force=True)
        self.path.mkdir()
        (self.path / "valuable").write_text("unrelated replacement")
        with self.assertRaisesRegex(TaskError, "reused|replaced"):
            cli.cleanup("DEV-7", force=True)
        self.assertTrue((self.path / "valuable").exists())
        self.assertIn(self.branch, self.git.branches("DEV-7"))

    def test_worktree_removal_failure_keeps_branch_and_contexts_for_retry(self):
        original = Git.command
        def fail_remove(git, *args):
            if args[:3] == ("worktree", "remove", "--force"):
                raise TaskError("worktree removal failed")
            return original(git, *args)
        with patch.object(Git, "command", fail_remove), self.assertRaisesRegex(TaskError, "removal failed"):
            cli.cleanup("DEV-7", force=True)
        self.assert_preserved()
        self.assertTrue(self.registry.list("DEV-7"))
        cli.cleanup("DEV-7", force=True)
        self.assert_disposed()

    def test_new_publication_evidence_on_retry_stops_removal(self):
        self.workspace_close_error = TaskError("close failed")
        with self.assertRaisesRegex(TaskError, "close failed"):
            cli.cleanup("DEV-7", force=True)
        self.workspace_close_error = None
        self.remote_branches.return_value = [self.branch]
        with self.assertRaisesRegex(TaskError, "publication evidence"):
            cli.cleanup("DEV-7", force=True)
        self.assert_preserved()
        self.assertTrue(self.workspace_active)

    def test_corrupt_retry_journal_fails_closed(self):
        self.workspace_close_error = TaskError("close failed")
        with self.assertRaisesRegex(TaskError, "close failed"):
            cli.cleanup("DEV-7", force=True)
        self.workspace_close_error = None
        path = self.journal_path()
        record = self.journal()
        del record["path"]
        path.write_text(json.dumps(record))
        with self.assertRaisesRegex(TaskError, "journal identity"):
            cli.cleanup("DEV-7", force=True)
        self.assert_preserved()
        self.assertTrue(self.workspace_active)

    def test_context_retirement_interruption_keeps_ordinals_and_recovers(self):
        original = ContextRegistry.retire_execution
        def uncertain(registry, *args):
            original(registry, *args)
            raise TaskError("retirement acknowledgement lost")
        with patch.object(ContextRegistry, "retire_execution", uncertain), self.assertRaisesRegex(TaskError, "acknowledgement"):
            cli.cleanup("DEV-7", force=True)
        self.assertEqual(self.registry.list("DEV-7"), [])
        cli.cleanup("DEV-7", force=True)
        self.assert_disposed()

    def test_retired_integration_history_is_not_a_live_integration_claim(self):
        self.retained_integration()
        cli.cleanup("DEV-7", force=True)
        self.command(self.repo, "worktree", "add", "-b", "dev-7-new-execution", str(self.path))
        store = PublicationStore(self.path)
        self.assertIsNone(check_integration(store, store.read(), self.registry, self.issue, None, self.repo, None))

    def test_completed_disposal_does_not_reserve_historical_artifact_paths(self):
        self.retained_integration()
        old_checkout = self.isolated
        cli.cleanup("DEV-7", force=True)
        before = self.journal_path().read_bytes()
        old_checkout.mkdir(parents=True)
        unrelated = old_checkout / "unrelated.txt"
        unrelated.write_text("new resource")
        with patch("task_start.cleanup.shutil.rmtree") as remove:
            self.assertIn("discarded", cli.cleanup("DEV-7", force=True))
        remove.assert_not_called()
        self.assertEqual(unrelated.read_text(), "new resource")
        self.assertEqual(self.journal_path().read_bytes(), before)


    def restart_execution(self, *, same_target=True):
        self.git.check_disposal("DEV-7")
        if not same_target:
            self.branch = "dev-7-later-execution"
            self.path = self.repo.parent / "later-execution"
        self.command(self.repo, "worktree", "add", "-b", self.branch, str(self.path))
        self.git.save_scope(self.path, self.branch, "DEV-7", None)
        self.workspace_id = "later-workspace"
        self.workspace_active = True
        self.workspace_path = self.path
        self.context("implementation", self.path, "active")
        self.context("review", self.path, "active")
        self.publication = PublicationStore(self.path)
        (self.path / "tracked.txt").write_text("dirty reviewed implementation\n")

    def test_later_execution_at_same_branch_and_path_gets_separate_disposal_journal(self):
        self.retained_integration()
        cli.cleanup("DEV-7", force=True)
        first_path, first_record = self.journal_path(), self.journal()
        first_bytes = first_path.read_bytes()
        self.restart_execution()
        later_integration = self.retained_integration()
        cli.cleanup("DEV-7", force=True)
        self.assert_disposed()
        second_path, second_record = self.journal_path(), self.journal()
        self.assertNotEqual(first_path, second_path)
        self.assertNotEqual(first_record["execution_id"], second_record["execution_id"])
        self.assertEqual(first_path.read_bytes(), first_bytes)
        self.assertEqual(second_record["integrations"][0]["pass_id"], later_integration["pass_id"])
        self.assertTrue({c["context_id"] for c in first_record["contexts"]}.isdisjoint(
            c["context_id"] for c in second_record["contexts"]))
        for context in first_record["contexts"]:
            self.assertEqual(self.registry.get(context["context_id"]), context)
        self.assertEqual(len(self.git.disposal_files("DEV-7")), 2)
        self.assertIn("discarded", cli.cleanup("DEV-7", force=True))
        self.assertEqual(len(self.git.disposal_files("DEV-7")), 2)
        self.assertEqual(first_path.read_bytes(), first_bytes)
        self.assertEqual(self.registry.allocate("DEV-7", "implementation", agent="codex"), "DEV-7-I3")

    def test_later_interrupted_disposal_resumes_its_claim_and_preserves_legacy_history(self):
        cli.cleanup("DEV-7", force=True)
        first_path, first_record = self.journal_path(), self.journal()
        first_record.pop("execution_id")
        first_record["version"] = 1
        legacy_path = self.git.disposal_file("DEV-7")
        legacy_path.write_text(json.dumps(first_record))
        first_path.unlink()
        legacy_bytes = legacy_path.read_bytes()
        historical_checkout = self.path
        self.restart_execution(same_target=False)
        historical_checkout.symlink_to(self.other, target_is_directory=True)
        self.workspace_close_error = TaskError("later close failed")
        with self.assertRaisesRegex(TaskError, "later close failed"):
            cli.cleanup("DEV-7", force=True)
        later_path = self.journal_path()
        self.assertEqual(self.journal()["state"], "pending")
        self.assertEqual(len(self.git.disposal_files("DEV-7")), 2)
        with self.assertRaisesRegex(TaskError, "pending or uncertain"):
            self.git.check_disposal("DEV-7")
        self.assert_preserved()
        self.workspace_close_error = None
        cli.cleanup("DEV-7", force=True)
        self.assert_disposed()
        self.assertEqual(self.journal_path(), later_path)
        self.assertEqual(legacy_path.read_bytes(), legacy_bytes)
        self.assertTrue(historical_checkout.is_symlink())
        self.assertEqual(len(self.git.disposal_files("DEV-7")), 2)
        self.git.check_disposal("DEV-7")

    def test_cli_force_option_is_explicit(self):
        self.assertFalse(cli.parser().parse_args(["cleanup", "DEV-7"]).force)
        self.assertTrue(cli.parser().parse_args(["cleanup", "DEV-7", "--force"]).force)


class DisposalMountTests(unittest.TestCase):
    def setUp(self):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        self.directory = Path(directory)
        self.checkout = self.directory / "checkout"
        self.checkout.mkdir()
        self.mountinfo = self.directory / "mountinfo"
        self.enterContext(patch("task_start.cleanup.MOUNTINFO", self.mountinfo))

    def test_mount_identity_options_and_escaped_coordinates_are_preserved(self):
        root = self.checkout / "space tab\tnewline\nbackslash\\ café"
        point = self.directory / "external view"
        self.mountinfo.write_bytes(mount_table() + mount_record(
            21, root, point, options="ro,nosuid", optional="shared:2 future:field"))
        mount = read_mounts()[1]
        device = self.checkout.stat().st_dev
        self.assertEqual((mount.mount_id, mount.parent, mount.device),
                         (21, 20, (os.major(device), os.minor(device))))
        self.assertEqual((mount.root, mount.point), (root, point))
        self.assertEqual(mount.options, (b"ro", b"nosuid"))
        self.assertEqual(mount.optional, (b"shared:2", b"future:field"))
        self.assertEqual((mount.filesystem, mount.source, mount.super_options),
                         (b"ext4", b"/dev/example", (b"rw",)))
        self.assertTrue(overlapping(root, point, read_mounts()))

    def test_incomplete_parent_graph_and_malformed_mount_options_refuse(self):
        for extra in (mount_record(21, self.checkout, self.directory / "view", parent=999),
                      mount_record(21, self.checkout, self.directory / "view", parent=21),
                      mount_record(21, self.checkout, self.directory / "view", options="rw,ro"),
                      mount_record(21, self.checkout, self.directory / "view", options="rw,,nosuid"),
                      mount_record(21, self.checkout, self.directory / "view", super_options="unknown")):
            self.mountinfo.write_bytes(mount_table() + extra)
            with self.assertRaisesRegex(TaskError, "Cannot read or parse Linux mount information"):
                read_mounts()

    def test_escaped_bind_mount_is_detected_before_walking_the_tree(self):
        nested = self.checkout / "space tab\tnewline\nbackslash\\134 café"
        nested.mkdir()
        self.assertEqual(nested.stat().st_dev, self.checkout.stat().st_dev)
        self.mountinfo.write_bytes(mount_table(nested))
        self.assertIn(nested, mount_points())
        with patch.object(Path, "is_mount", return_value=False), patch("task_start.cleanup.os.walk") as walk, \
                self.assertRaisesRegex(TaskError, "mount boundary"):
            check_contents(self.checkout)
        walk.assert_not_called()

    def test_malformed_mount_table_is_never_partially_accepted(self):
        valid = mount_table(self.checkout / "ordinary")
        bad = (b"", b"\n", valid.rstrip(b"\n"), valid + b"malformed\n", valid + valid,
               valid.replace(valid.split(b" ")[2], b"device"), valid.replace(b"20 1", b"id 1"),
               valid.replace(b" - ", b" "), valid.replace(b"/outside", b"relative"),
               valid.replace(b"/outside", b"/bad\\999"), valid.replace(b"/outside", b"/bad\\"),
               valid.replace(b"/outside", b"/bad\0"), valid.replace(b"/outside", b"/bad\tpath"),
               valid.replace(b"/outside", b"/bad/../path"), valid.replace(b"/outside", b"//bad"))
        for data in bad:
            with self.subTest(data=data):
                self.mountinfo.write_bytes(data)
                with self.assertRaisesRegex(TaskError, "Cannot read or parse Linux mount information"):
                    check_contents(self.checkout)

    def test_unreadable_mount_metadata_refuses_before_traversal(self):
        with patch.object(Path, "read_bytes", side_effect=PermissionError("mountinfo denied")), \
                patch("task_start.cleanup.os.walk") as walk, \
                self.assertRaisesRegex(TaskError, "Cannot read or parse Linux mount information"):
            check_contents(self.checkout)
        walk.assert_not_called()


class DisposalProcessTests(unittest.TestCase):
    def setUp(self):
        abandonment.ProcessEvidenceTests.setUp(self)
        process_mounts(self.proc)

    def process(self, pid, parent, cwd, *, argv="bash\0", executable="/bin/bash", state="S",
                flags=0, comm="bash", **kwargs):
        abandonment.ProcessEvidenceTests.process(self, pid, parent, cwd, argv=argv, **kwargs)
        entry = self.proc / str(pid)
        (entry / "fd").mkdir()
        (entry / "exe").symlink_to(executable)
        (entry / "root").symlink_to("/")
        (entry / "maps").write_bytes(b"1000-2000 rw-p 00000000 00:00 0 [heap]\n")
        (entry / "ns").mkdir()
        (entry / "ns" / "mnt").symlink_to(os.readlink("/proc/self/ns/mnt"))
        (entry / "task").mkdir()
        (entry / "task" / str(pid)).symlink_to("..", target_is_directory=True)
        # Sleeping shell in its own foreground group with a controlling terminal.
        fields = (entry / "stat").read_text().rsplit(")", 1)[1].split()
        fields[0], fields[2], fields[3], fields[4], fields[5] = state, str(pid), str(pid), "34816", str(pid)
        fields[6] = str(flags)
        (entry / "stat").write_text(f"{pid} ({comm}) " + " ".join(fields))

    def kernel_task(self, pid, parent=2, *, flags=0x00200000, comm="kernel task", state="I"):
        # PF_KTHREAD is stat field 9. Intentionally supply no userspace links,
        # descriptors or mappings: their absence is normal for a kernel task.
        entry = self.proc / str(pid)
        entry.mkdir()
        fields = ["0"] * 50
        fields[0], fields[1], fields[5], fields[6] = state, str(parent), "-1", str(flags)
        fields[17], fields[19] = "1", "7"
        (entry / "stat").write_text(f"{pid} ({comm}) " + " ".join(fields))
        (entry / "task").mkdir()
        (entry / "task" / str(pid)).symlink_to("..", target_is_directory=True)

    def shells(self, *pids, argv=None):
        return {pid: dict(cwd=self.checkout, argv=argv or ["bash"]) for pid in pids}

    def test_multiple_empty_shells_are_allowed_and_pid_start_times_are_pinned(self):
        self.process(123, 1, self.checkout)
        self.process(124, 1, self.checkout, started=8)
        self.assertEqual(stopped_execution([self.checkout], self.shells(123, 124),
                                          proc=self.proc), {"123": 7, "124": 8})

    def test_kernel_tasks_need_neither_special_pids_names_nor_userspace_references(self):
        self.process(123, 1, self.checkout)
        self.kernel_task(2, 0, comm="kthreadd", state="S")
        self.kernel_task(73, comm="arbitrary (worker) name")
        with foreign_process_owners(self.proc, 2, 73):
            self.assertEqual(stopped_execution([self.checkout], self.shells(123), proc=self.proc), {"123": 7})

    def test_unrelated_kernel_worker_thread_groups_need_no_private_inspection(self):
        self.process(123, 1, self.checkout)
        for leader_state, worker_state in (("I", "R"), ("S", "D"), ("Z", "P")):
            with self.subTest(leader=leader_state, worker=worker_state):
                self.kernel_task(73, state=leader_state)
                # Workqueue and other flags may coexist with PF_KTHREAD.
                self.kernel_task(74, flags=0x00208020, comm="kworker/u8:0", state=worker_state)
                (self.proc / "74").rename(self.proc / "73" / "task" / "74")
                self.assertEqual(stopped_execution([self.checkout], self.shells(123), proc=self.proc), {"123": 7})
                shutil.rmtree(self.proc / "73")

    def test_kernel_flag_cannot_exempt_shells_or_execution_descendants(self):
        self.kernel_task(123)
        with self.assertRaisesRegex(TaskError, "shell cannot be a kernel task"):
            stopped_execution([self.checkout], self.shells(123), proc=self.proc)
        shutil.rmtree(self.proc / "123")
        self.process(123, 1, self.checkout)
        self.kernel_task(124, parent=123)
        with self.assertRaisesRegex(TaskError, "Process 124"):
            stopped_execution([self.checkout], self.shells(123), proc=self.proc)
        (self.proc / "124").rename(self.proc / "123" / "task" / "124")
        with self.assertRaisesRegex(TaskError, "Process 124"):
            stopped_execution([self.checkout], self.shells(123), proc=self.proc)

    def test_exec_replacement_cannot_hide_behind_shell_pid_or_spoofed_argv(self):
        replacement = self.proc / "replacement-binary"
        replacement.write_bytes(b"unrelated executable")
        for argv, executable in (("ssh\0host\0", replacement), ("codex\0", replacement),
                                 ("bash\0", replacement), ("bash\0-c\0read input\0", "/bin/bash"),
                                 ("bash\0script.sh\0", "/bin/bash")):
            with self.subTest(argv=argv, executable=executable):
                self.process(123, 1, self.checkout, argv=argv, executable=executable)
                with self.assertRaisesRegex(TaskError, "shell executable, argv"):
                    stopped_execution([self.checkout], self.shells(123), proc=self.proc)
                shutil.rmtree(self.proc / "123")

    def test_script_argv_matching_herdr_is_still_not_an_idle_shell(self):
        self.process(123, 1, self.checkout, argv="bash\0-c\0read input\0")
        with self.assertRaisesRegex(TaskError, "argv does not prove an idle shell"):
            stopped_execution([self.checkout], self.shells(123, argv=["bash", "-c", "read input"]), proc=self.proc)

    def test_login_shell_is_supported_but_running_or_nonterminal_shells_refuse(self):
        self.process(123, 1, self.checkout, argv="-bash\0")
        expected = self.shells(123, argv=["-bash"])
        self.assertEqual(stopped_execution([self.checkout], expected, proc=self.proc), {"123": 7})
        entry = self.proc / "123" / "stat"
        original = entry.read_text()
        fields = original.rsplit(")", 1)[1].split()
        for index, value in ((0, "R"), (0, "T"), (2, "999"), (3, "999"), (4, "0"), (5, "999")):
            with self.subTest(index=index, value=value):
                changed = fields.copy()
                changed[index] = value
                entry.write_text("123 (bash) " + " ".join(changed))
                with self.assertRaisesRegex(TaskError, "idle terminal identity changed"):
                    stopped_execution([self.checkout], expected, proc=self.proc)
        entry.write_text(original)
        (self.proc / "123" / "exe").unlink()
        with self.assertRaisesRegex(TaskError, "Cannot prove"):
            stopped_execution([self.checkout], expected, proc=self.proc)

    def test_background_children_fail_even_after_changing_cwd(self):
        self.process(123, 1, self.checkout)
        self.process(124, 123, self.proc)
        with self.assertRaisesRegex(TaskError, "Process 124"):
            stopped_execution([self.checkout], self.shells(123), proc=self.proc)

    def test_foreign_uid_descendant_cannot_disappear_from_ancestry(self):
        self.process(123, 1, self.checkout)
        self.process(124, 123, self.proc, state="Z")
        self.process(125, 124, self.proc, argv="worker\0")
        # Preserve a dead, foreign-owned intermediate node as ancestry evidence.
        with foreign_process_owners(self.proc, 124, 125), self.assertRaisesRegex(TaskError, "Process 125"):
            stopped_execution([self.checkout], self.shells(123), proc=self.proc)

    def test_unreadable_foreign_descendant_is_never_assumed_unrelated(self):
        self.process(123, 1, self.checkout)
        self.process(124, 123, self.proc)
        entry = self.proc / "124"
        readlink = os.readlink
        def denied(path, *args, **kwargs):
            if Path(path).is_relative_to(entry):
                raise PermissionError("foreign process references denied")
            return readlink(path, *args, **kwargs)
        with foreign_process_owners(self.proc, 124), \
                patch("task_start.integration_process.os.readlink", side_effect=denied), \
                self.assertRaisesRegex(TaskError, "Process 124"):
            stopped_execution([self.checkout], self.shells(123), proc=self.proc)
        read_text = Path.read_text
        def denied_stat(path, *args, **kwargs):
            if path.is_relative_to(entry):
                raise PermissionError("foreign process ancestry denied")
            return read_text(path, *args, **kwargs)
        with foreign_process_owners(self.proc, 124), patch.object(Path, "read_text", denied_stat), \
                self.assertRaisesRegex(TaskError, "Cannot prove"):
            stopped_execution([self.checkout], self.shells(123), proc=self.proc)

    def test_an_allowed_shell_cannot_exempt_a_descendant_shell(self):
        self.process(123, 1, self.checkout)
        self.process(124, 123, self.checkout)
        with self.assertRaisesRegex(TaskError, "Process 124"):
            stopped_execution([self.checkout], self.shells(123, 124), proc=self.proc)

    def test_reparented_job_in_closed_shell_session_blocks_cleanup(self):
        self.process(125, 1, self.proc)
        entry = self.proc / "125" / "stat"
        fields = entry.read_text().rsplit(")", 1)[1].split()
        fields[3] = "123"
        entry.write_text("125 (worker) " + " ".join(fields))
        with foreign_process_owners(self.proc, 125), self.assertRaisesRegex(TaskError, "Process 125"):
            stopped_execution([self.checkout], {}, closed_shells={"123": 7}, proc=self.proc)

    def test_absent_execution_and_unrelated_live_process_are_safe(self):
        self.process(125, 1, self.proc, argv="unrelated\0")
        self.assertEqual(stopped_execution([self.checkout], {}, proc=self.proc), {})
        shutil.rmtree(self.proc / "125")
        self.assertEqual(stopped_execution([self.checkout], {}, proc=self.proc), {})

    def test_closed_shell_must_actually_exit_even_after_changing_cwd(self):
        self.process(123, 1, self.proc)
        with self.assertRaisesRegex(TaskError, "still running"):
            stopped_execution([self.checkout], {}, closed_shells={"123": 7}, proc=self.proc)
        self.assertEqual(stopped_execution([self.checkout], {}, closed_shells={"123": 6}, proc=self.proc), {})

    def test_unrelated_private_references_and_namespaces_need_not_be_inspectable(self):
        self.process(123, 1, self.checkout)
        self.process(125, 1, self.checkout, executable="/missing", argv="worker\0")
        entry = self.proc / "125"
        (entry / "maps").unlink()
        (entry / "ns" / "mnt").unlink()
        (entry / "fd" / "7").symlink_to(self.checkout / "artifact")
        readlink = os.readlink
        def denied(path, *args, **kwargs):
            if Path(path).is_relative_to(entry):
                raise PermissionError("unrelated userspace references are private")
            return readlink(path, *args, **kwargs)
        with foreign_process_owners(self.proc, 125), patch("os.readlink", side_effect=denied):
            self.assertEqual(stopped_execution([self.checkout], self.shells(123), proc=self.proc), {"123": 7})

    def test_unrelated_process_churn_does_not_invalidate_execution_proof(self):
        self.process(123, 1, self.checkout)
        readlink = os.readlink
        def unrelated_start(path, *args, **kwargs):
            if Path(path) == self.proc / "123" / "cwd" and not (self.proc / "125").exists():
                self.process(125, 1, self.proc)
            return readlink(path, *args, **kwargs)
        with patch("os.readlink", side_effect=unrelated_start):
            self.assertEqual(stopped_execution([self.checkout], self.shells(123), proc=self.proc), {"123": 7})

    def test_descendant_appearing_during_final_shell_inspection_refuses(self):
        self.process(123, 1, self.checkout)
        readlink = os.readlink
        calls = 0
        def child_start(path, *args, **kwargs):
            nonlocal calls
            if Path(path) == self.proc / "123" / "cwd":
                calls += 1
                if calls == 2:
                    self.process(125, 123, self.proc, argv="worker\0")
            return readlink(path, *args, **kwargs)
        with patch("os.readlink", side_effect=child_start), self.assertRaisesRegex(TaskError, "Process 125"):
            stopped_execution([self.checkout], self.shells(123), proc=self.proc)

    def test_dead_registered_leader_does_not_hide_live_thread(self):
        self.process(123, 1, self.proc, state="Z")
        self.process(124, 1, self.proc)
        (self.proc / "124").rename(self.proc / "123" / "task" / "124")
        with self.assertRaisesRegex(TaskError, "Process 124"):
            stopped_execution([self.checkout], {}, closed_shells={"123": 7}, proc=self.proc)

    def test_registered_shell_metadata_and_ancestry_visibility_fail_closed(self):
        self.process(123, 1, self.checkout)
        stat = self.proc / "123" / "stat"
        original = stat.read_text()
        stat.write_text("malformed")
        with self.assertRaisesRegex(TaskError, "Cannot prove"):
            stopped_execution([self.checkout], self.shells(123), proc=self.proc)
        stat.write_text(original)
        (self.proc / "123" / "cmdline").unlink()
        with self.assertRaisesRegex(TaskError, "Cannot prove"):
            stopped_execution([self.checkout], self.shells(123), proc=self.proc)
        table = self.proc / "self" / "mounts"
        table.write_bytes(table.read_bytes().replace(b"rw,", b"rw,hidepid=2,"))
        with self.assertRaisesRegex(TaskError, "Cannot prove"):
            stopped_execution([self.checkout], self.shells(123), proc=self.proc)
