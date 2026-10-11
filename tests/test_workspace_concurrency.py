"""Independent Linux controllers with real Git, SQLite, journals and flock.

Service transports and the long agent turn are controlled; no production state
or installed agent is used. Pipes order races without scheduler-dependent sleeps.
"""

from contextlib import contextmanager
import multiprocessing
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from task_start import cli
from task_start.loop import loop
import test_canceled_cleanup as forced


def slow_loop(path, pipe):
    def drive(*args, **kwargs):
        pipe.send("running")
        while True:
            if not pipe.poll(30):
                raise AssertionError("controller was never released")
            message = pipe.recv()
            from task_start.contexts import ContextRegistry
            if isinstance(message, tuple) and message[0] == "claim":
                pipe.send(ContextRegistry().allocate(message[2], "review", agent="codex",
                    repository=str(message[1]), worktree=str(path)))
            elif message == "probe":
                with ContextRegistry().connection(write=True) as db:
                    db.execute("SELECT 1")
                pipe.send("progress")
            else:
                break
        return "finished"
    store = SimpleNamespace(continue_paused=lambda: {"next_phase": "implementation"})
    try:
        with patch("task_start.loop.control_store", return_value=(store, path)), \
                patch("task_start.loop.drive", side_effect=drive):
            loop("DEV-8", action="continue")
        pipe.send("finished")
    except BaseException as error:
        pipe.send(f"{type(error).__name__}: {error}")


@contextmanager
def active_loop(test, path):
    parent, child = multiprocessing.Pipe()
    process = multiprocessing.get_context("fork").Process(target=slow_loop, args=(path, child))
    process.start()
    child.close()
    try:
        test.assertTrue(parent.poll(10), "loop did not reach its agent turn")
        test.assertEqual(parent.recv(), "running")
        process.control = parent
        yield process
        test.assertTrue(process.is_alive(), "independent loop exited during cleanup")
        parent.send("release")
        test.assertTrue(parent.poll(10), "loop did not finish after release")
        test.assertEqual(parent.recv(), "finished")
    finally:
        if process.is_alive():
            process.terminate()
        process.join(10)
        parent.close()


class WorkspaceConcurrencyTests(unittest.TestCase):
    def setUp(self):
        self.fixture = forced.ForcedCleanupTests()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.setUp()

    def test_independent_loop_allows_forced_cleanup(self):
        f = self.fixture
        with active_loop(self, f.other):
            self.assertIn("discarded", cli.cleanup("DEV-7", force=True))
            f.assert_disposed()

    def test_forced_removal_rechecks_processes_after_repository_admission(self):
        from task_start import TaskError
        from task_start.publication_state import PublicationStore
        f = self.fixture
        original = PublicationStore.repository_locked
        @contextmanager
        def admitted(store):
            with original(store):
                f.process.side_effect = TaskError("Managed execution became live while awaiting repository admission")
                yield store
        with patch.object(PublicationStore, "repository_locked", admitted):
            with self.assertRaisesRegex(TaskError, "Managed execution became live"):
                cli.cleanup("DEV-7", force=True)
        f.assert_preserved()
        self.assertEqual(f.journal()["state"], "pending")


class AbortConcurrencyTests(unittest.TestCase):
    def setUp(self):
        import test_loop_abort as abort
        self.fixture = abort.AbortTests()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.setUp()
        self.enterContext(patch("task_start.contexts.registry_path", return_value=self.fixture.registry.path))

    def test_independent_abort_while_loop_is_active(self):
        f = self.fixture
        f.interrupt("implementation")
        f.stop_agents()
        other = f.repo.parent / "peer"
        f.command(f.repo, "worktree", "add", "-b", "dev-8-peer", str(other))
        before = f.content()
        with active_loop(self, other):
            self.assertEqual(f.abort().state, "aborted")
            self.assertEqual(f.content(), before)

    def test_independent_abort_different_repository(self):
        f = self.fixture
        f.interrupt("review")
        f.stop_agents()
        other_repo = f.repo.parent / "other-project"
        f.command(f.repo.parent, "clone", str(f.repo), str(other_repo))
        other = f.repo.parent / "other-project-task"
        f.command(other_repo, "worktree", "add", "-b", "dev-8-peer", str(other))
        before = f.content()
        with active_loop(self, other):
            self.assertEqual(f.abort().state, "aborted")
            self.assertEqual(f.content(), before)


class IndependentCleanupTests(unittest.TestCase):
    """Minimal stopped workspaces, shared registry, service-boundary fixtures."""

    def setUp(self):
        import tempfile
        from task_start.contexts import ContextRegistry
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.registry = ContextRegistry(self.root / "contexts.sqlite3")
        self.enterContext(patch("task_start.contexts.registry_path", return_value=self.registry.path))
        self.targets = {}
        from task_start.config import LocalConfig
        self.enterContext(patch("task_start.cli.load_local", return_value=LocalConfig(self.root, "placeholder")))
        self.enterContext(patch("task_start.cli.load_projects", side_effect=lambda: [t.project for t in self.targets.values()]))
        self.enterContext(patch("task_start.cli.resolve_project", side_effect=lambda projects, name:
                                next(t.project for t in self.targets.values() if t.issue.project == name)))
        self.enterContext(patch("task_start.cli.Linear")).return_value.get_issue.side_effect = lambda key: self.targets[key].issue
        from task_start.workspace import Git, Herdr
        self.enterContext(patch.object(Git, "remote_branches", return_value=[]))
        self.enterContext(patch.object(Git, "history_remote", return_value="git@github.com:example/project.git"))
        self.enterContext(patch("task_start.cleanup.pull_requests", return_value=[]))
        self.enterContext(patch.object(Herdr, "workspaces", return_value=[]))
        self.enterContext(patch.object(Herdr, "command", autospec=True, side_effect=self.worktrees))
        identities = self.enterContext(patch("task_start.cleanup.HerdrContexts")).return_value
        identities.endpoint.return_value = "/test/socket"
        identities.snapshot.return_value = []
        self.enterContext(patch("task_start.cleanup.stopped_execution", return_value={}))
        self.enterContext(patch("task_start.github.urlopen", side_effect=AssertionError("Unexpected network")))

    def command(self, repo, *args):
        import subprocess
        return subprocess.check_output(["git", "-C", str(repo), "-c", "user.name=Test", "-c", "user.email=test@example.test",
                                        *args], text=True, stderr=subprocess.PIPE).strip()

    def target(self, number, *, repo=None, history=True):
        from dataclasses import replace
        from task_start.config import Project
        from task_start.workspace import Git
        from test_task_start import ISSUE
        repo = repo or self.root / f"project-{number}"
        if not repo.exists():
            repo.mkdir()
            self.command(repo, "init", "--initial-branch=main")
            (repo / "tracked").write_text("shared base\n")
            self.command(repo, "add", "tracked")
            self.command(repo, "commit", "-m", "base")
            self.command(repo, "remote", "add", "origin", "git@github.com:example/project.git")
            self.command(repo, "config", "branch.main.remote", "origin")
            self.command(repo, "config", "branch.main.merge", "refs/heads/main")
        issue = replace(ISSUE, identifier=f"DEV-{number}", project=repo.name, state_name="Done", state_type="completed")
        branch = f"dev-{number}-task"
        path = self.root / branch
        self.command(repo, "worktree", "add", "-b", branch, str(path))
        Git(repo).save_scope(path, branch, issue.identifier, None)
        target = SimpleNamespace(repo=repo, path=path, branch=branch, issue=issue, project=Project(repo.name, repo.name, "main"))
        self.targets[issue.identifier] = target
        # Retained history discovers both repositories even before a disposal.
        if history:
            self.registry.allocate(issue.identifier, "implementation", agent="codex", repository=str(repo),
                                   worktree=str(path), endpoint="/test/socket")
            self.registry.retire(issue.identifier, repo, path, endpoint="/test/socket", workspace_id=None)
        return target

    def worktrees(self, herdr, operation, *args):
        from task_start.workspace import Git
        self.assertEqual((operation, args), ("list", ()))
        return dict(source=dict(repo_root=str(herdr.repo)), worktrees=[dict(path=t["worktree"],
            branch=t.get("branch", "").removeprefix("refs/heads/"), label="Display only",
            is_linked_worktree=Path(t["worktree"]) != herdr.repo, is_bare=False, is_detached=False,
            is_prunable=False, open_workspace_id=None) for t in Git(herdr.repo).worktrees()])

    def fingerprint(self, target):
        from task_start.publication_state import PublicationStore
        directory = PublicationStore(target.path).directory
        return ((target.path / "tracked").read_bytes(), (directory / "index").read_bytes(),
                self.command(target.path, "rev-parse", "HEAD"), self.registry.list(include_retired=True))

    def test_forced_and_normal_cleanup_same_and_different_repositories(self):
        for same_repo in (True, False):
            for force in (True, False):
                with self.subTest(same_repo=same_repo, force=force):
                    n = 10 + len(self.targets)
                    a = self.target(n)
                    b = self.target(n + 1, repo=a.repo if same_repo else None)
                    before = self.fingerprint(a)
                    with active_loop(self, a.path):
                        self.assertIn("discarded" if force else "cleanup complete", cli.cleanup(b.issue.identifier, force=force))
                        self.assertFalse(b.path.exists())
                        self.assertEqual(self.fingerprint(a), before)

    def cleanup_process(self, target, pipe, *, rendezvous=True):
        from task_start.cleanup import finish
        def paused(*args):
            pipe.send("reserved")
            if not pipe.poll(30):
                raise AssertionError("cleanup was never released")
            pipe.recv()
            return finish(*args)
        try:
            if rendezvous:
                with patch("task_start.cleanup.finish", side_effect=paused):
                    result = cli.cleanup(target.issue.identifier, force=True)
            else:
                result = cli.cleanup(target.issue.identifier, force=True)
            pipe.send(result)
        except BaseException as error:
            pipe.send(f"{type(error).__name__}: {error}")

    def start_cleanup(self, target):
        parent, child = multiprocessing.Pipe()
        process = multiprocessing.get_context("fork").Process(target=self.cleanup_process, args=(target, child))
        process.start()
        child.close()
        def stop():
            if process.is_alive():
                process.terminate()
            process.join(10)
            parent.close()
        self.addCleanup(stop)
        self.assertTrue(parent.poll(15), "cleanup did not reserve its workspace")
        self.assertEqual(parent.recv(), "reserved")
        return process, parent

    def test_disjoint_cleanups_reserve_and_finish_concurrently(self):
        for same_repo in (True, False):
            with self.subTest(same_repo=same_repo):
                n = 40 + len(self.targets)
                a = self.target(n)
                b = self.target(n + 1, repo=a.repo if same_repo else None)
                pa, ca = self.start_cleanup(a)
                pb, cb = self.start_cleanup(b)
                ca.send("finish"); cb.send("finish")
                for process, pipe in ((pa, ca), (pb, cb)):
                    self.assertTrue(pipe.poll(30), "disjoint cleanups deadlocked")
                    self.assertIn("local execution discarded", pipe.recv())
                    process.join(5)
                    self.assertEqual(process.exitcode, 0)
                self.assertFalse(a.path.exists())
                self.assertFalse(b.path.exists())

    def test_claim_during_disposal_and_interrupted_cleanup_retry(self):
        from task_start import TaskError
        from task_start.workspace import Git
        from task_start.publication_state import PublicationStore
        a = self.target(60)
        b = self.target(61, repo=a.repo)
        before = self.fingerprint(b)
        process, pipe = self.start_cleanup(a)
        with self.assertRaisesRegex(TaskError, "owns this worktree|Pending cleanup"):
            self.registry.allocate("DEV-62", "review", agent="codex", repository=str(a.repo), worktree=str(a.path))
        self.assertIn("DEV-63-R1", self.registry.allocate("DEV-63", "review", agent="codex"))
        process.terminate(); process.join(5)
        with self.assertRaisesRegex(TaskError, "Pending cleanup"):
            self.registry.allocate("DEV-62", "review", agent="codex", repository=str(a.repo), worktree=str(a.path))
        with PublicationStore(a.path).locked():
            pass  # The dead controller cannot leak its flock.
        journals = Git(a.repo).disposal_files(a.issue.identifier)
        self.assertEqual(len(journals), 1)
        self.assertIn("discarded", cli.cleanup(a.issue.identifier, force=True))
        self.assertEqual(Git(a.repo).disposal_files(a.issue.identifier), journals)
        after = self.fingerprint(b)
        self.assertEqual(before[:3], after[:3])
        self.assertTrue(all(c in after[3] for c in before[3]))
        self.assertEqual(self.registry.list("DEV-62", include_retired=True), [])

    def test_same_workspace_refuses_cleanup_and_claims_before_mutation(self):
        from task_start import TaskError
        a = self.target(70)
        before = self.fingerprint(a)
        with active_loop(self, a.path):
            for force in (False, True):
                with self.assertRaisesRegex(TaskError, "owns this worktree"):
                    cli.cleanup(a.issue.identifier, force=force)
            with self.assertRaisesRegex(TaskError, "owns this worktree"):
                self.registry.allocate("DEV-71", "implementation", agent="codex",
                                       repository=str(a.repo), worktree=str(a.path))
            self.assertEqual(self.fingerprint(a), before)

    def test_claim_committed_before_disposal_admission_is_not_lost(self):
        from task_start.cleanup import prepare_record
        from task_start.workspace import Git
        a = self.target(75)
        parent, child = multiprocessing.Pipe()
        def claimant():
            if not child.poll(10):
                return
            child.recv()
            result = self.registry.allocate("DEV-76", "integration", agent="codex",
                        repository=str(a.repo), worktree=str(a.path.parent))
            child.send(result)
        process = multiprocessing.get_context("fork").Process(target=claimant)
        process.start()
        child.close()
        def prepared(*args, **kwargs):
            record = prepare_record(*args, **kwargs)
            parent.send("claim")
            self.assertTrue(parent.poll(10))
            self.assertEqual(parent.recv(), "DEV-76-G1")
            return record
        from task_start import TaskError
        try:
            with patch("task_start.cleanup.prepare_record", side_effect=prepared):
                with self.assertRaisesRegex(TaskError, "Another task context claims"):
                    cli.cleanup(a.issue.identifier, force=True)
            self.assertTrue(a.path.is_dir())
            self.assertEqual(Git(a.repo).disposal_files(a.issue.identifier), [])
            self.assertEqual(self.registry.get("DEV-76-G1")["worktree"], str(a.path.parent))
        finally:
            process.join(5)
            if process.is_alive():
                process.terminate(); process.join(5)
            parent.close()

    def test_normal_cleanup_refuses_preexisting_nested_claim(self):
        from task_start import TaskError
        a = self.target(77)
        claim = self.registry.allocate("DEV-78", "integration", agent="codex",
                                       repository=str(a.repo), worktree=str(a.path / "nested"))
        before = self.fingerprint(a)
        with self.assertRaisesRegex(TaskError, "Another task context claims"):
            cli.cleanup(a.issue.identifier)
        self.assertEqual(self.fingerprint(a), before)
        self.assertEqual(self.registry.get(claim)["state"], "launching")

    def test_normal_removal_rechecks_claims_after_remote_proof(self):
        from task_start import TaskError
        from task_start.workspace import Git
        a = self.target(79)
        original = Git.remove_task
        def claim_then_remove(git, *args, **kwargs):
            parent, child = multiprocessing.Pipe()
            def claimant():
                child.send(self.registry.allocate("DEV-80", "integration", agent="codex",
                                                  repository=str(a.repo), worktree=str(a.path.parent)))
            process = multiprocessing.get_context("fork").Process(target=claimant)
            process.start(); child.close()
            try:
                self.assertTrue(parent.poll(10)); self.assertEqual(parent.recv(), "DEV-80-G1")
            finally:
                process.join(5)
                if process.is_alive():
                    process.terminate(); process.join(5)
                parent.close()
            return original(git, *args, **kwargs)
        with patch.object(Git, "remove_task", claim_then_remove):
            with self.assertRaisesRegex(TaskError, "Another task context claims"):
                cli.cleanup(a.issue.identifier)
        self.assertTrue(a.path.is_dir())
        self.assertEqual(self.registry.get("DEV-80-G1")["worktree"], str(a.path.parent))

    def test_registry_writer_contention_preserves_history_and_ordinals(self):
        import sqlite3
        a = self.target(80)
        before = self.registry.list(include_retired=True)
        parent, child = multiprocessing.Pipe()
        def claimant():
            child.send("started")
            if not child.poll(10):
                return
            child.recv()
            child.send(self.registry.allocate("DEV-81", "review", agent="codex"))
        # Fork before opening the contended connection; inherited SQLite handles
        # are not a valid second-controller fixture.
        process = multiprocessing.get_context("fork").Process(target=claimant)
        process.start()
        child.close()
        try:
            self.assertTrue(parent.poll(5)); self.assertEqual(parent.recv(), "started")
            with sqlite3.connect(self.registry.path, isolation_level=None) as db:
                db.execute("BEGIN IMMEDIATE")
                parent.send("allocate")
                self.assertFalse(parent.poll(0.1), "writer bypassed the SQLite transaction")
                db.commit()
            self.assertTrue(parent.poll(10)); self.assertEqual(parent.recv(), "DEV-81-R1")
        finally:
            process.join(5)
            if process.is_alive():
                process.terminate(); process.join(5)
            parent.close()
        self.assertTrue(all(c in self.registry.list(include_retired=True) for c in before))
        self.assertEqual(self.registry.allocate("DEV-81", "review", agent="codex"), "DEV-81-R2")
        self.assertTrue(a.path.exists())

    def test_nested_linked_workspace_claim_ignores_permanent_administration_lock(self):
        from task_start import TaskError
        from task_start.publication_state import PublicationStore
        a = self.target(82)
        nested = a.repo / "linked" / a.path.name
        nested.parent.mkdir()
        self.command(a.repo, "worktree", "move", str(a.path), str(nested))
        a.path = nested
        context = self.registry.allocate("DEV-82", "review", agent="codex",
                                         repository=str(a.repo), worktree=str(nested))
        parent, child = multiprocessing.Pipe()
        def administration():
            with PublicationStore(a.repo).repository_locked():
                child.send("administering")
                if not child.poll(10):
                    raise AssertionError("administration was never released")
                child.recv()
        process = multiprocessing.get_context("fork").Process(target=administration)
        process.start(); child.close()
        try:
            self.assertTrue(parent.poll(10)); self.assertEqual(parent.recv(), "administering")
            self.registry.update(context, state="active")
            self.assertEqual(self.registry.get(context)["state"], "active")
            parent.send("finish")
            process.join(5)
            self.assertEqual(process.exitcode, 0)
            with active_loop(self, nested):
                with self.assertRaisesRegex(TaskError, "owns this worktree"):
                    self.registry.update(context, state="uncertain")
            self.assertEqual(self.registry.get(context)["state"], "active")
        finally:
            if process.is_alive():
                process.terminate()
            process.join(5)
            parent.close()

    def test_stale_selected_git_identity_cannot_lock_replacement(self):
        from task_start import TaskError
        from task_start.publication_state import PublicationStore
        a = self.target(85)
        store = PublicationStore(a.path)
        moved = self.root / "preserved-original"
        self.command(a.repo, "worktree", "move", str(a.path), str(moved))
        self.command(a.repo, "worktree", "add", "-b", "dev-86-replacement", str(a.path))
        with self.assertRaisesRegex(TaskError, "identity changed"):
            with store.locked():
                self.fail("stale store locked the replacement")
        self.assertTrue(moved.exists())
        self.assertTrue(a.path.exists())

    def test_contextless_disposal_is_discoverable_after_worktree_removal(self):
        import sqlite3
        from task_start import TaskError
        from task_start.contexts import ContextRegistry
        a = self.target(87, history=False)
        b = self.target(88)
        parent, child = multiprocessing.Pipe()
        original = ContextRegistry.retire_execution
        def cleanup(pipe):
            def before_retirement(registry, *args):
                pipe.send("removed")
                if not pipe.poll(20):
                    raise AssertionError("retirement was never released")
                pipe.recv()
                return original(registry, *args)
            try:
                with patch.object(ContextRegistry, "retire_execution", before_retirement):
                    pipe.send(cli.cleanup(a.issue.identifier, force=True))
            except BaseException as error:
                pipe.send(f"{type(error).__name__}: {error}")
        process = multiprocessing.get_context("fork").Process(target=cleanup, args=(child,))
        process.start()
        child.close()
        try:
            self.assertTrue(parent.poll(15)); self.assertEqual(parent.recv(), "removed")
            self.assertFalse(a.path.exists())
            self.assertIn(str(a.repo), self.registry.repositories())
            self.assertEqual(self.registry.list(a.issue.identifier, include_retired=True), [])
            for stopped in (False, True):
                if stopped:
                    process.terminate(); process.join(5)
                with self.assertRaisesRegex(TaskError, "Pending cleanup"):
                    self.registry.allocate("DEV-89", "integration", agent="codex", repository=str(b.repo),
                                           worktree=str(a.path / "nested-checkout"))
            # Recreate a v2 registry accompanying an old pending contextless
            # journal. Retry must publish discovery before resuming disposal.
            with sqlite3.connect(self.registry.path) as db:
                db.execute("DROP TABLE ownership_repositories")
                db.execute("PRAGMA user_version = 2")
            self.assertEqual(self.registry.repositories(), [])
            parent.close()
            parent, child = multiprocessing.Pipe()
            process = multiprocessing.get_context("fork").Process(target=cleanup, args=(child,))
            process.start(); child.close()
            self.assertTrue(parent.poll(15)); self.assertEqual(parent.recv(), "removed")
            self.assertIn(str(a.repo), self.registry.repositories())
            with self.assertRaisesRegex(TaskError, "Pending cleanup"):
                self.registry.allocate("DEV-89", "integration", agent="codex", repository=str(b.repo),
                                       worktree=str(a.path / "nested-checkout"))
            parent.send("finish")
            self.assertTrue(parent.poll(15)); self.assertIn("discarded", parent.recv())
            process.join(5)
            self.assertEqual(process.exitcode, 0)
            self.assertTrue(b.path.exists())
            self.assertEqual(self.registry.list("DEV-89", include_retired=True), [])
        finally:
            if process.is_alive():
                process.terminate()
            process.join(5)
            parent.close()


class NormalRetirementConcurrencyTests(unittest.TestCase):
    def test_slow_herdr_close_does_not_hold_global_gate_or_repository_lock(self):
        import test_cleanup as completed
        from task_start.workspace import Herdr
        from task_start.publication_state import PublicationStore
        f = completed.CleanupTests()
        self.addCleanup(f.doCleanups)
        f.setUp()
        f.workspace_id = "w-task"
        peer = f.repo.parent / "independent"
        f.command(f.repo, "worktree", "add", "-b", "dev-8-peer", str(peer))
        parent, child = multiprocessing.Pipe()
        original = Herdr.retire
        def cleanup():
            def slow_close(herdr, *args, **kwargs):
                child.send("closing")
                if not child.poll(20):
                    raise AssertionError("close was never released")
                child.recv()
                return original(herdr, *args, **kwargs)
            try:
                with patch.object(Herdr, "retire", slow_close):
                    child.send(cli.cleanup("DEV-7"))
            except BaseException as error:
                child.send(str(error))
        with active_loop(self, peer) as loop_process:
            process = multiprocessing.get_context("fork").Process(target=cleanup)
            process.start()
            child.close()
            try:
                self.assertTrue(parent.poll(10)); self.assertEqual(parent.recv(), "closing")
                loop_process.control.send("probe")
                self.assertTrue(loop_process.control.poll(5), "B's close blocked A's registry progress")
                self.assertEqual(loop_process.control.recv(), "progress")
                with PublicationStore(f.repo).locked():
                    pass
                parent.send("finish")
                self.assertTrue(parent.poll(10)); self.assertIn("cleanup complete", parent.recv())
            finally:
                if process.is_alive():
                    process.terminate()
                process.join(5)
                parent.close()


class PreparationConcurrencyTests(unittest.TestCase):
    setUp = IndependentCleanupTests.setUp
    command = IndependentCleanupTests.command
    target = IndependentCleanupTests.target
    worktrees = IndependentCleanupTests.worktrees

    def test_start_and_loop_keep_selected_lock_during_status_update(self):
        from contextlib import ExitStack
        from task_start.agent import AgentOptions
        from task_start.config import LocalConfig
        from task_start.loop import bootstrap
        from task_start.publication_state import PublicationStore
        from task_start.workspace import Git, Herdr, Workspace
        from task_start import TaskError
        for kind in ("start", "bootstrap"):
            with self.subTest(kind=kind):
                a = self.target(90 + len(self.targets))
                parent, child = multiprocessing.Pipe()
                def prepare():
                    def status_update(*args):
                        child.send("updating-status")
                        if not child.poll(15):
                            raise AssertionError("status was never released")
                        child.recv()
                    workspace = Workspace(a.branch, a.path, "w-task", "t1", "p1", "prepared")
                    try:
                        with patch.object(Git, "update_base"), patch.object(Git, "check_history"), \
                                patch.object(Herdr, "_prepare_selected", return_value=workspace), \
                                patch("task_start.preparation.check_issue_structure"), \
                                patch("task_start.loop.adapter_for"):
                            if kind == "start":
                                with patch("task_start.cli.Linear") as linear:
                                    linear.return_value.get_issue.return_value = a.issue
                                    linear.return_value.start.side_effect = status_update
                                    cli.start(a.issue.identifier, no_agent=True)
                            else:
                                local = LocalConfig(self.root, "placeholder")
                                env = SimpleNamespace(issue=a.issue, repo=a.repo, project=a.project, local=local,
                                                      identities=SimpleNamespace(endpoint=lambda: "/test/socket"))
                                with ExitStack() as ownership, patch("task_start.loop.Linear") as linear:
                                    linear.return_value.start.side_effect = status_update
                                    bootstrap(env, AgentOptions("codex", "model", "high"), local, ownership=ownership)
                                    # The returned preparation still belongs to this loop.
                                    child.send("prepared")
                                    if not child.poll(15):
                                        raise AssertionError("handoff was never released")
                                    child.recv()
                            child.send("finished")
                    except BaseException as error:
                        child.send(f"{type(error).__name__}: {error}")
                process = multiprocessing.get_context("fork").Process(target=prepare)
                process.start(); child.close()
                try:
                    self.assertTrue(parent.poll(10)); self.assertEqual(parent.recv(), "updating-status")
                    with self.assertRaisesRegex(TaskError, "owns this worktree"):
                        cli.cleanup(a.issue.identifier, force=True)
                    parent.send("finish-status")
                    if kind == "bootstrap":
                        self.assertTrue(parent.poll(10)); self.assertEqual(parent.recv(), "prepared")
                        with self.assertRaisesRegex(TaskError, "owns this worktree"):
                            with PublicationStore(a.path).locked():
                                self.fail("bootstrap lost its handoff lock")
                        parent.send("finish-handoff")
                    self.assertTrue(parent.poll(10)); self.assertEqual(parent.recv(), "finished")
                    self.assertTrue(a.path.is_dir())
                finally:
                    if process.is_alive():
                        process.terminate()
                    process.join(5)
                    parent.close()

class SlowDeletionTests(unittest.TestCase):
    setUp = IndependentCleanupTests.setUp
    command = IndependentCleanupTests.command
    target = IndependentCleanupTests.target
    worktrees = IndependentCleanupTests.worktrees
    fingerprint = IndependentCleanupTests.fingerprint

    def paused_deletion(self, target, force):
        from task_start.workspace import Git
        parent, child = multiprocessing.Pipe()
        command = Git.command
        def cleanup():
            def slow_remove(git, *args, **kwargs):
                if args[:2] == ("worktree", "remove"):
                    # Git removes files and private metadata in separate steps.
                    # Discovery must tolerate an in-flight, unusable checkout.
                    from task_start.publication_state import PublicationStore
                    private = PublicationStore(target.path).directory
                    broken_checkpoint = private / "agentic-workflows-loop.sqlite3"
                    assert not broken_checkpoint.exists()
                    pointer = target.path / ".git"
                    original = pointer.read_bytes()
                    pointer.unlink()
                    broken_checkpoint.write_bytes(b"partially deleted metadata")
                    child.send("deleting")
                    try:
                        if not child.poll(30):
                            raise AssertionError("deletion was never released")
                        child.recv()
                    finally:
                        broken_checkpoint.unlink()
                        pointer.write_bytes(original)
                return command(git, *args, **kwargs)
            try:
                with patch.object(Git, "command", slow_remove):
                    child.send(cli.cleanup(target.issue.identifier, force=force))
            except BaseException as error:
                child.send(f"{type(error).__name__}: {error}")
        process = multiprocessing.get_context("fork").Process(target=cleanup)
        process.start(); child.close()
        def stop():
            if process.is_alive():
                process.terminate()
            process.join(5)
            parent.close()
        self.addCleanup(stop)
        self.assertTrue(parent.poll(15), "cleanup did not reach recursive deletion")
        self.assertEqual(parent.recv(), "deleting")
        return process, parent

    def test_registry_progress_and_claim_exclusion_during_slow_deletion(self):
        from task_start import TaskError
        for same_repo in (True, False):
            for force in (True, False):
                with self.subTest(same_repo=same_repo, force=force):
                    n = 100 + len(self.targets)
                    a = self.target(n)
                    b = self.target(n + 1, repo=a.repo if same_repo else None, history=False)
                    before = self.fingerprint(a)
                    with active_loop(self, a.path) as controller:
                        process, pipe = self.paused_deletion(b, force)
                        controller.control.send("probe")
                        self.assertTrue(controller.control.poll(5), "B deletion blocked A registry write")
                        self.assertEqual(controller.control.recv(), "progress")
                        controller.control.send(("claim", a.repo, a.issue.identifier))
                        self.assertTrue(controller.control.poll(5), "B deletion blocked A context claim")
                        self.assertEqual(controller.control.recv(), a.issue.identifier + "-R1")
                        # This requires complete inventory, including partially
                        # removed B, rather than merely opening the registry DB.
                        context = self.registry.allocate("DEV-150", "review", agent="codex")
                        self.assertTrue(context.startswith("DEV-150-R"))
                        with self.assertRaises(TaskError):
                            self.registry.allocate("DEV-151", "implementation", agent="codex",
                                repository=str(b.repo), worktree=str(b.path))
                        self.assertEqual(self.registry.list("DEV-151", include_retired=True), [])
                        self.assertEqual(self.fingerprint(a)[:3], before[:3])
                        self.assertTrue(all(c in self.registry.list(include_retired=True) for c in before[3]))
                        pipe.send("finish")
                        self.assertTrue(pipe.poll(15))
                        self.assertIn("discarded" if force else "cleanup complete", pipe.recv())
                        process.join(5)
                        self.assertEqual(process.exitcode, 0)
                        self.assertFalse(b.path.exists())

    def test_disjoint_cleanup_uses_reservation_while_git_metadata_is_partially_removed(self):
        for force in (False, True):
            with self.subTest(force=force):
                a = self.target(180 + len(self.targets), history=False)
                b = self.target(181 + len(self.targets))
                process, pipe = self.paused_deletion(a, force)
                self.assertIn("cleanup complete", cli.cleanup(b.issue.identifier))
                self.assertFalse(b.path.exists())
                self.assertTrue(process.is_alive())
                pipe.send("finish")
                self.assertTrue(pipe.poll(15))
                self.assertIn("discarded" if force else "cleanup complete", pipe.recv())
                process.join(5)
                self.assertEqual(process.exitcode, 0)

    def test_interrupted_normal_deletion_keeps_reservation_and_allows_retry(self):
        from task_start import TaskError
        from task_start.workspace import Git
        from task_start.publication_state import PublicationStore
        a = self.target(160, history=False)
        pointer = (a.path / ".git").read_bytes()
        process, pipe = self.paused_deletion(a, False)
        process.terminate(); process.join(5)
        reservation = Git(a.repo).load_retirement(a.issue.identifier, "main")
        self.assertIsNotNone(reservation.git_dir)
        self.assertIn(str(a.repo), self.registry.repositories())
        with self.assertRaises(TaskError):
            self.registry.allocate("DEV-161", "review", agent="codex",
                                   repository=str(a.repo), worktree=str(a.path))
        (reservation.git_dir / "agentic-workflows-loop.sqlite3").unlink()
        (a.path / ".git").write_bytes(pointer)  # Complete the simulated Git step.
        with PublicationStore(a.path).locked():
            pass  # Kernel released the interrupted controller's lock.
        self.assertIn("cleanup complete", cli.cleanup(a.issue.identifier))
        self.assertFalse(a.path.exists())
        self.assertFalse(Git(a.repo).retirement_file(a.issue.identifier).exists())


class NormalStoppedExecutionTests(unittest.TestCase):
    def setUp(self):
        import test_cleanup as completed
        self.fixture = completed.CleanupTests()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.setUp()
        f = self.fixture
        f.workspace_id = "w-task"
        f.pane_count = 1
        from task_start.contexts import ContextRegistry
        registry = ContextRegistry(f.registry_path)
        self.context = registry.allocate("DEV-7", "implementation", agent="codex", repository=str(f.repo),
            worktree=str(f.path), endpoint="/test.sock", workspace_id="w-task", terminal_id="term", pane_id="w-task:p1")
        registry.update(self.context, state="active", session_id="saved-session")
        pane = dict(workspace_id="w-task", pane_id="w-task:p1", terminal_id="term", cwd=str(f.path), agent="codex")
        self.enterContext(patch("task_start.cli.HerdrContexts.snapshot", side_effect=lambda: [pane] if f.workspace_active else []))
        self.info = dict(pane_id="w-task:p1", shell_pid=1200, foreground_process_group_id=1200,
                         foreground_processes=[dict(pid=1200, argv=["/bin/bash"])])
        self.transport = self.enterContext(patch("task_start.cli.HerdrContexts.command",
                                                side_effect=lambda *args: dict(process_info=self.info)))
        self.proof = self.enterContext(patch("task_start.cleanup.stopped_execution", return_value={"1200": 1234}))

    def test_registered_live_agent_refuses_before_cache_or_worktree_removal(self):
        f = self.fixture
        f.ignore_python_cache()
        cache = f.path / "__pycache__"
        cache.mkdir()
        evidence = cache / "task.cpython-312.pyc"
        evidence.write_bytes(b"preserve until stopped")
        self.info.update(foreground_process_group_id=1300,
                         foreground_processes=[dict(pid=1300, argv=["codex"])])
        f.assert_refused("live or uncertain")
        self.assertEqual(evidence.read_bytes(), b"preserve until stopped")
        self.assertTrue(f.workspace_active)
        self.proof.assert_not_called()
        self.assertTrue(all(call.args[:2] == ("pane", "process-info") for call in self.transport.call_args_list))

    def test_task_scoped_process_verifier_can_refuse_even_with_idle_shell(self):
        from task_start import TaskError
        self.proof.side_effect = TaskError("Managed execution still has a live process")
        self.fixture.assert_refused("still has a live process")
        self.proof.assert_called_once()
        self.assertIn(self.fixture.path, self.proof.call_args.args[0])
        self.assertEqual(set(self.proof.call_args.args[1]), {1200})

    def test_agent_starting_after_admission_is_rechecked_before_git_remove(self):
        from task_start.workspace import Git
        original = Git._remove_task
        def late_agent(git, *args, **kwargs):
            self.info.update(foreground_process_group_id=1300,
                             foreground_processes=[dict(pid=1300, argv=["codex"])])
            return original(git, *args, **kwargs)
        with patch.object(Git, "_remove_task", late_agent):
            self.fixture.assert_refused("live or uncertain")
        self.assertTrue(self.fixture.git.retirement_file("DEV-7").exists())
        self.info.update(foreground_process_group_id=1200,
                         foreground_processes=[dict(pid=1200, argv=["/bin/bash"])])
        self.assertIn("cleanup complete", cli.cleanup("DEV-7"))
        self.assertFalse(self.fixture.path.exists())

    def test_changed_endpoint_after_admission_refuses(self):
        from task_start.workspace import Git
        original = Git._remove_task
        def replaced_endpoint(git, *args, **kwargs):
            with patch("task_start.cli.HerdrContexts.endpoint", return_value="/replacement.sock"):
                return original(git, *args, **kwargs)
        with patch.object(Git, "_remove_task", replaced_endpoint):
            self.fixture.assert_refused("another Herdr endpoint")


class PreparationConflictTests(unittest.TestCase):
    setUp = IndependentCleanupTests.setUp
    command = IndependentCleanupTests.command
    target = IndependentCleanupTests.target
    worktrees = IndependentCleanupTests.worktrees

    def test_existing_workspace_conflict_precedes_fetch_and_base_fast_forward(self):
        from contextlib import ExitStack
        from task_start import TaskError
        from task_start.agent import AgentOptions
        from task_start.config import LocalConfig
        from task_start.loop import bootstrap
        from task_start.workspace import Git
        for kind in ("start", "bootstrap"):
            with self.subTest(kind=kind):
                a = self.target(170 + len(self.targets))
                remote = self.root / f"upstream-{kind}"
                self.command(self.root, "clone", str(a.repo), str(remote))
                (remote / "tracked").write_text("upstream advanced\n")
                self.command(remote, "commit", "-am", "advance main")
                self.command(a.repo, "remote", "set-url", "origin", str(remote))
                before = (self.command(a.repo, "rev-parse", "main"), (a.repo / "tracked").read_bytes(),
                          (a.repo / ".git" / "index").read_bytes())
                self.assertNotEqual(before[0], self.command(remote, "rev-parse", "main"))
                actual = Git.command
                mutations = []
                def observe(git, *args, **kwargs):
                    if args[0] in {"fetch", "merge", "pull"}:
                        mutations.append(args)
                    return actual(git, *args, **kwargs)
                with active_loop(self, a.path), patch.object(Git, "command", observe), \
                        patch("task_start.loop.adapter_for"), patch("task_start.preparation.check_issue_structure"), \
                        patch("task_start.linear.Linear.start") as status:
                    with self.assertRaisesRegex(TaskError, "owns this worktree"):
                        if kind == "start":
                            cli.start(a.issue.identifier, no_agent=True)
                        else:
                            local = LocalConfig(self.root, "placeholder")
                            env = SimpleNamespace(issue=a.issue, repo=a.repo, project=a.project, local=local,
                                                  identities=SimpleNamespace(endpoint=lambda: "/test/socket"))
                            with ExitStack() as ownership:
                                bootstrap(env, AgentOptions("codex", "model", "high"), local, ownership=ownership)
                    status.assert_not_called()
                self.assertEqual(mutations, [])
                self.assertEqual(before, (self.command(a.repo, "rev-parse", "main"),
                                          (a.repo / "tracked").read_bytes(), (a.repo / ".git" / "index").read_bytes()))


class CheckpointDisposalTests(unittest.TestCase):
    def test_pending_disposal_refuses_checkpoint_write_and_preserves_readable_history(self):
        from task_start import TaskError
        from task_start.cleanup import finish
        from task_start.loop_state import LoopStore
        f = forced.ForcedCleanupTests()
        self.addCleanup(f.doCleanups)
        f.setUp()
        f.retained_loop()
        store = LoopStore(f.path)
        before = store.read()
        parent, child = multiprocessing.Pipe()
        def cleanup():
            def paused(*args):
                child.send("reserved")
                if not child.poll(20):
                    raise AssertionError("cleanup was never released")
                child.recv()
                return finish(*args)
            try:
                with patch("task_start.cleanup.finish", side_effect=paused):
                    child.send(cli.cleanup("DEV-7", force=True))
            except BaseException as error:
                child.send(f"{type(error).__name__}: {error}")
        process = multiprocessing.get_context("fork").Process(target=cleanup)
        process.start(); child.close()
        try:
            self.assertTrue(parent.poll(15)); self.assertEqual(parent.recv(), "reserved")
            with self.assertRaisesRegex(TaskError, "Pending cleanup"):
                store.pause()
            self.assertEqual(store.read(), before)
            parent.send("finish")
            self.assertTrue(parent.poll(15)); self.assertIn("discarded", parent.recv())
            process.join(5)
            self.assertEqual(process.exitcode, 0)
        finally:
            if process.is_alive():
                process.terminate()
            process.join(5)
            parent.close()


class NormalReservationTests(unittest.TestCase):
    def test_reservation_fsync_failure_prevents_removal_and_retry_preserves_identity(self):
        import test_cleanup as completed
        from task_start.workspace import Git
        f = completed.CleanupTests()
        self.addCleanup(f.doCleanups)
        f.setUp()
        save = Git.save_retirement
        def unavailable(git, state):
            with patch("task_start.workspace.os.fsync", side_effect=OSError("storage unavailable")):
                return save(git, state)
        with patch.object(Git, "save_retirement", unavailable):
            f.assert_refused("Could not save exact Herdr retirement state")
        record = f.git.retirement_file("DEV-7")
        self.assertFalse(record.exists())
        self.assertFalse(any(path.is_file() for path in record.parent.iterdir()), "temporary reservation leaked")
        self.assertIn("cleanup complete", cli.cleanup("DEV-7"))
        self.assertFalse(f.path.exists())

    def test_malformed_removal_reservation_refuses_before_deletion(self):
        import json
        import test_cleanup as completed
        from task_start import TaskError
        from task_start.workspace import Git
        f = completed.CleanupTests()
        self.addCleanup(f.doCleanups)
        f.setUp()
        with patch.object(Git, "_remove_task", side_effect=TaskError("interrupted before deletion")):
            f.assert_refused("interrupted before deletion")
        record = f.git.retirement_file("DEV-7")
        saved = json.loads(record.read_text())
        for changes in ({"git_dir": str(f.repo / ".git")}, {"claims": ["relative-path"]}, {"version": 99}):
            with self.subTest(changes=changes):
                record.write_text(json.dumps(dict(saved, **changes)))
                f.assert_refused("Invalid pending Herdr retirement state")
        record.write_text(json.dumps(saved))
        self.assertIn("cleanup complete", cli.cleanup("DEV-7"))

    def test_inventory_keeps_claims_when_last_private_directory_disappears_during_scan(self):
        import test_cleanup as completed
        from task_start import TaskError
        from task_start.contexts import ContextRegistry
        from task_start.workspace import Git
        f = completed.CleanupTests()
        self.addCleanup(f.doCleanups)
        f.setUp()
        with patch.object(Git, "_remove_task", side_effect=TaskError("reserved")):
            f.assert_refused("reserved")
        private = f.repo / ".git" / "worktrees"
        scan = Path.iterdir
        removed = False
        def finish_deletion(path):
            nonlocal removed
            if path == private and not removed:
                removed = True
                f.command(f.repo, "worktree", "remove", "--", str(f.path))
                if private.exists():
                    private.rmdir()
            return scan(path)
        registry = ContextRegistry(f.registry_path)
        with patch.object(Path, "iterdir", finish_deletion):
            self.assertEqual(registry.allocate("DEV-8", "review", agent="codex"), "DEV-8-R1")
        self.assertTrue(removed)
        self.assertFalse(f.path.exists())
        with self.assertRaisesRegex(TaskError, "Pending workspace retirement"):
            registry.allocate("DEV-9", "review", agent="codex", repository=str(f.repo), worktree=str(f.path))
        self.assertEqual(registry.list("DEV-9", include_retired=True), [])
