"""Non-destructive aborts with real Git/checkpoints and disposable processes."""

import copy
from contextlib import contextmanager
import fcntl
import io
import json
import multiprocessing
import os
from pathlib import Path
import pty
import select
import signal
import shutil
import subprocess
import sys
import termios
import time
import unittest
from unittest.mock import patch

from task_start import TaskError, cli
from task_start.agent import Codex, Pi, AgentOptions
from task_start.contexts import ContextRegistry
from task_start.integration_process import stopped_loop_execution
from task_start.loop import loop, environment
from task_start.loop_state import LoopStore
from task_start.pass_delivery import PassDelivery, seal
from task_start.publication_state import PublicationStore
from task_start.review_state import snapshot
from task_start.review import accept_review
import test_loop_recovery as recovery
import test_loop_bootstrap as bootstrap
import test_canceled_cleanup as cleanup
import test_pass_delivery as provider


class AbortTests(unittest.TestCase):
    command = recovery.RecoveryTests.command
    pane_command = recovery.RecoveryTests.pane_command
    worktrees = recovery.RecoveryTests.worktrees
    select_adapter = recovery.RecoveryTests.select_adapter
    interrupt = recovery.RecoveryTests.interrupt

    def setUp(self):
        recovery.RecoveryTests.setUp(self)
        self.enterContext(patch("task_start.loop_abort.ContextRegistry", return_value=self.registry))
        self.enterContext(patch("task_start.loop_abort.HerdrContexts", return_value=self.identities))
        self.enterContext(patch("task_start.loop_abort.adapter_for", side_effect=lambda options:
                                self.implementer if options.kind == "pi" else self.adapter))
        self.implementer.verify_stopped_session = lambda workspace, reference, receipt=None: self.implementer.verify_session(workspace, reference)
        self.adapter.verify_stopped_session = lambda workspace, reference, receipt=None: self.adapter.verify_session(workspace, reference)
        self.proof = self.enterContext(patch("task_start.loop_abort.stopped_loop_execution", return_value={"100": 42}))
        original_command = self.pane_command
        def command(group, operation, *args):
            if operation == "process-info":
                pane = args[-1]
                pid = 100 + int(pane[1:])
                return dict(process_info=dict(pane_id=pane, shell_pid=pid, foreground_process_group_id=pid,
                            foreground_processes=[dict(pid=pid, argv=["bash"])]))
            return original_command(group, operation, *args)
        self.identity_command.side_effect = command

    def stop_agents(self):
        for pane in self.panes:
            pane.update(agent=None, agent_session=None, agent_status="unknown")

    def resume_implementation(self):
        self.panes[0].update(agent="pi", agent_session=dict(agent="pi", kind="path", value="/implementation.jsonl"),
                             agent_status="idle")

    def abort(self):
        return loop("DEV-7", action="abort")

    def content(self):
        return snapshot(self.path, self.base, self.branch).as_dict()

    def test_review_abort_preserves_staged_untracked_and_history_then_fresh_reviewer(self):
        (self.path / "tracked.txt").write_text("staged implementation")
        self.command(self.path, "add", "tracked.txt")
        (self.path / "tracked.txt").write_text("unstaged implementation")
        (self.path / "untracked.txt").write_bytes(b"precious\0work")
        original = self.interrupt("review")
        before, rows = self.content(), self.registry.list()
        self.stop_agents()
        # Abort/status controls must not load config or contact Linear.
        with patch("task_start.loop.load_local", side_effect=AssertionError("Local config accessed")):
            result = self.abort()
            self.assertEqual(result.state, "aborted", result.render())
            self.assertEqual(result.data["implementation_session"], original["implementation"]["session"])
            self.assertIn("/implementation.jsonl", result.render())
            self.assertEqual(self.abort().data, result.data)
            self.assertEqual(loop("DEV-7", action="status").data, result.data)
        self.assertEqual(self.content(), before)
        self.assertEqual(self.command(self.path, "branch", "--show-current"), self.branch)
        self.assertTrue(Path(original["delivery"]["directory"]).is_dir())
        archived = self.store.abort_journal(original["run_id"])
        self.assertEqual(archived["original"], original)
        self.assertEqual(archived["contexts"], rows)
        self.assertIsNone(self.store.read()[0]["active_pass"])
        self.assertIsNone(self.store.read()[0]["delivery"])
        retired = self.registry.get("DEV-7-R1")
        self.assertEqual(retired["state"], "abandoned")
        self.assertTrue(retired["retired_at"])
        for key in ("session_id", "herdr_session", "terminal_id", "pane_id"):
            self.assertEqual(retired[key], rows[-1][key])
        self.assertEqual(self.registry.get("DEV-7-I1"), rows[0])
        self.resume_implementation()
        result = loop("DEV-7", action="new", from_review=True, timeout=1)
        self.assertEqual(result.state, "clean", result.render())
        self.assertEqual(result.data["reviewer_context"], "DEV-7-R2")
        self.assertEqual(len(self.impl_prompts), 1)
        self.assertEqual(len(self.prompts), 2)
        self.assertEqual(self.content(), before)
        self.assertEqual(self.store.abort_journal(original["run_id"]), archived)
        prior, = self.store.iterations()
        self.assertEqual(prior['checkpoint']['run_id'], original['run_id'])
        self.assertEqual(prior['checkpoint']['status'], 'aborted')

    def test_interrupted_implementation_and_fixes_require_explicit_continuation(self):
        for phase in ("implementation", "fixes"):
            with self.subTest(phase=phase):
                case = AbortTests(); case.setUp()
                try:
                    original = case.interrupt(phase)
                    (case.path / "partial.txt").write_text("unfinished work")
                    case.command(case.path, "add", "partial.txt")
                    (case.path / "new.txt").write_text("keep untracked")
                    before = case.content()
                    case.stop_agents()
                    result = case.abort()
                    case.assertFalse(result.data["abandonment"]["review_ready"])
                    case.assertEqual(case.content(), before)
                    case.assertEqual(loop("DEV-7").state, "aborted")
                    case.resume_implementation()
                    with case.assertRaisesRegex(TaskError, "Implementation was interrupted"):
                        loop("DEV-7", action="new", from_review=True)
                    count = len(case.impl_prompts)
                    result = loop("DEV-7", action="new", timeout=1)
                    case.assertEqual(result.state, "clean", result.render())
                    case.assertEqual(len(case.impl_prompts), count + 1)
                    case.assertNotEqual(case.store.read()[0]["run_id"], original["run_id"])
                finally:
                    case.doCleanups()

    def test_pre_delivery_claim_released_without_replaying_original_prompt(self):
        with patch.object(self.implementer, "resume", side_effect=KeyboardInterrupt):
            self.assertEqual(loop("DEV-7", timeout=1).state, "interrupted")
        original = self.store.read()[0]
        self.assertIsNone(original["delivery"]["receipt"])
        self.stop_agents()
        self.assertEqual(self.abort().state, "aborted")
        self.assertEqual(self.impl_prompts, [])
        self.resume_implementation()
        self.assertEqual(loop("DEV-7", action="new", timeout=1).state, "clean")
        self.assertEqual(len(self.impl_prompts), 1)
        self.assertNotIn(original["active_pass"]["pass_id"], self.impl_prompts[0])

    def test_repeated_new_abort_preserves_prior_journals_and_retired_runtime_history(self):
        first = self.interrupt("review")
        self.stop_agents()
        self.abort()
        self.resume_implementation()
        self.on_review = lambda execution: (_ for _ in ()).throw(KeyboardInterrupt())
        self.assertEqual(loop("DEV-7", action="new", from_review=True, timeout=1).state, "interrupted")
        second = self.store.read()[0]
        self.stop_agents()
        self.assertEqual(self.abort().state, "aborted")
        self.assertEqual(self.store.abort_journal(first["run_id"])["original"], first)
        self.assertEqual(self.store.abort_journal(second["run_id"])["original"], second)
        self.assertEqual([c["state"] for c in self.registry.list(include_retired=True)],
                         ["active", "abandoned", "abandoned"])
        self.assertEqual([e['summary']['run_id'] for e in self.store.iterations()], [first['run_id']])

    def test_interrupted_rereview_preserves_fixes_and_starts_fresh_review(self):
        original = self.interrupt("rereview")
        before = self.content()
        self.stop_agents()
        self.assertEqual(self.abort().state, "aborted")
        self.assertEqual(self.store.read()[0]["records"], original["records"])
        self.assertTrue(self.store.read()[0]["abandonment"]["review_ready"])
        self.resume_implementation()
        count = len(self.impl_prompts)
        result = loop("DEV-7", action="new", from_review=True, timeout=1)
        self.assertEqual(result.state, "clean", result.render())
        self.assertEqual(result.data["reviewer_context"], "DEV-7-R2")
        self.assertEqual(len(self.impl_prompts), count)
        self.assertEqual(self.content(), before)

    def test_git_change_during_retry_keeps_revocation_and_refuses_reconciliation(self):
        self.interrupt("implementation")
        self.stop_agents()
        with patch.object(ContextRegistry, "reconcile_loop_abort", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.abort()
        (self.path / "after-abort.txt").write_text("new external edits")
        before = self.content()
        with self.assertRaisesRegex(TaskError, "changed during abort"):
            self.abort()
        self.assertEqual(self.content(), before)
        self.assertEqual(self.store.read()[0]["status"], "aborting")

    def test_abort_revokes_only_its_unconsumed_acceptance_and_refuses_conflicting_publication(self):
        def accepted_then_interrupted(*args, **kwargs):
            accept_review(*args, **kwargs)
            raise KeyboardInterrupt()
        with patch("task_start.review.accept_review", side_effect=accepted_then_interrupted):
            self.assertEqual(loop("DEV-7", timeout=1).state, "interrupted")
        state = self.store.read()[0]
        publication = PublicationStore(self.path)
        saved = publication.read()
        self.assertEqual(saved["acceptance"]["pass_id"], state["active_pass"]["pass_id"])
        self.stop_agents()
        conflict = copy.deepcopy(saved)
        conflict["acceptance"]["pass_id"] = "another-review"
        publication.write(conflict)
        with self.assertRaisesRegex(TaskError, "Publication state conflicts"):
            self.abort()
        self.assertEqual(publication.read(), conflict)
        self.assertIsNone(self.store.abort_journal(state["run_id"]))
        conflict = dict(saved, publication_history=dict(intervening="publication"))
        publication.write(conflict)
        with self.assertRaisesRegex(TaskError, "Publication provenance changed"):
            self.abort()
        self.assertEqual(publication.read(), conflict)
        self.assertIsNone(self.store.abort_journal(state["run_id"]))
        publication.write(saved)
        self.assertEqual(self.abort().state, "aborted")
        self.assertEqual(publication.read(), dict(saved, acceptance=None))

    def test_historical_claim_without_delivery_bookkeeping_is_not_assumed_undelivered(self):
        state = self.interrupt("implementation")
        del state["delivery"]
        self.store.save(state)
        self.stop_agents()
        with self.assertRaisesRegex(TaskError, "Historical claim"):
            self.abort()
        self.assertEqual(self.store.read()[0], state)

    def test_review_eligibility_cannot_survive_post_abort_drift_or_skip_implementation(self):
        self.interrupt("review")
        self.stop_agents()
        self.abort()
        self.resume_implementation()
        (self.path / "later.txt").write_text("new implementation work")
        count = len(self.impl_prompts), len(self.prompts)
        with self.assertRaisesRegex(TaskError, "boundary changed since abort"):
            loop("DEV-7", action="new", from_review=True)
        self.assertEqual((len(self.impl_prompts), len(self.prompts)), count)

    def test_new_after_abort_cannot_replace_original_implementation_identity(self):
        self.interrupt("review")
        self.stop_agents()
        self.abort()
        self.resume_implementation()
        self.registry.update("DEV-7-I1", session_id="/replacement.jsonl", herdr_session=None)
        self.panes[0]["agent_session"]["value"] = "/replacement.jsonl"
        count = len(self.impl_prompts), len(self.prompts)
        for from_review in (False, True):
            with self.assertRaisesRegex(TaskError, "Exact implementation context/session/settings changed"):
                loop("DEV-7", action="new", from_review=from_review)
        self.assertEqual((len(self.impl_prompts), len(self.prompts)), count)

    def test_live_idle_missing_moved_session_provider_git_and_stale_claims_refuse(self):
        for change in ("live", "missing", "moved", "session", "provider", "queued", "git", "head", "base", "context", "process", "receipt", "disposal", "integration"):
            with self.subTest(change=change):
                case = AbortTests(); case.setUp()
                try:
                    state = case.interrupt("review")
                    case.stop_agents()
                    if change == "live":
                        case.panes[-1].update(agent="codex", agent_status="idle")
                    elif change == "missing":
                        case.panes.pop()
                    elif change == "moved":
                        case.panes[-1]["terminal_id"] = "replaced"
                    elif change == "session":
                        case.registry.update("DEV-7-R1", session_id="replaced")
                    elif change == "provider":
                        case.adapter.verify_session = lambda *a: (_ for _ in ()).throw(TaskError("Unavailable provider"))
                    elif change == "queued":
                        case.adapter.verify_stopped_session = lambda *a: (_ for _ in ()).throw(TaskError("Pending input"))
                    elif change == "git":
                        (case.path / "tracked.txt").write_text("review drift")
                    elif change in {"head", "base"}:
                        case.command(case.path if change == "head" else case.repo, "commit", "--allow-empty", "-m", "drift")
                    elif change == "context":
                        case.registry.allocate("DEV-7", "review", agent="codex", model="x", mode="high",
                            repository=str(case.repo), worktree=str(case.path), endpoint="/server.sock",
                            workspace_id="w1", tab_id="t1", pane_id="extra", terminal_id="extra")
                    elif change == "receipt":
                        state["delivery"]["context"]["session"]["value"] = "conflicting"
                        case.store.save(state)
                    elif change == "disposal":
                        case.enterContext(patch("task_start.loop_abort.check_unreserved",
                                                side_effect=TaskError("Pending cleanup reserves this resource")))
                    elif change == "integration":
                        case.enterContext(patch("task_start.loop_abort.IntegrationStore.read",
                                                return_value=dict(state="preparing")))
                    else:
                        case.proof.side_effect = TaskError("Background task is live")
                    state = case.store.read()[0]
                    before, rows = case.content(), case.registry.list(include_retired=True)
                    count = len(case.impl_prompts), len(case.prompts)
                    with case.assertRaises(TaskError):
                        case.abort()
                    if change in {"git", "head", "base"}:
                        state["delivery"]["invalidated"] = True
                    case.assertEqual(case.store.read()[0], state)
                    case.assertEqual(case.registry.list(include_retired=True), rows)
                    case.assertEqual(case.content(), before)
                    case.assertEqual((len(case.impl_prompts), len(case.prompts)), count)
                    case.assertIsNone(case.store.abort_journal(state["run_id"]))
                finally:
                    case.doCleanups()

    def test_partial_abort_retries_and_late_results_cannot_be_consumed(self):
        for stage in ("before_journal", "contexts", "checkpoint"):
            with self.subTest(stage=stage):
                case = AbortTests(); case.setUp()
                try:
                    original = case.interrupt("review")
                    case.stop_agents()
                    target = {"before_journal": (LoopStore, "begin_abort"),
                              "contexts": (ContextRegistry, "reconcile_loop_abort"),
                              "checkpoint": (LoopStore, "finish_abort")}[stage]
                    with patch.object(*target, side_effect=KeyboardInterrupt):
                        with case.assertRaises(KeyboardInterrupt):
                            case.abort()
                    case.assertEqual(case.store.read()[0]["status"], "interrupted" if stage == "before_journal" else "aborting")
                    if stage != "before_journal":
                        with case.assertRaisesRegex(TaskError, "Abort reconciliation"):
                            loop("DEV-7")
                        with case.assertRaises(TaskError):
                            case.store.save(original)
                        with case.assertRaises(TaskError):
                            seal(original["delivery"]["directory"], original["run_id"],
                                 original["active_pass"]["pass_id"], str(case.path), case.base, case.branch)
                    case.assertEqual(case.abort().state, "aborted")
                    case.assertEqual(case.abort().state, "aborted")
                    env = environment("DEV-7")
                    delivery = PassDelivery(original, lambda *_: case.store.save(original), env)
                    with case.assertRaisesRegex(TaskError, "abandoned or superseded"):
                        delivery.verify(case.adapter, None)
                    case.assertIsNone(PublicationStore(case.path).read()["acceptance"])
                    case.resume_implementation()
                    case.assertEqual(loop("DEV-7", action="new", from_review=True, timeout=1).state, "clean")
                    saved = PublicationStore(case.path).read()
                    with case.assertRaises(TaskError):
                        delivery.runtime()
                    case.assertEqual(PublicationStore(case.path).read(), saved)
                finally:
                    case.doCleanups()

    def test_active_controller_lock_refuses_with_stop_sequence(self):
        self.interrupt("implementation")
        self.stop_agents()
        ready, finish = multiprocessing.Event(), multiprocessing.Event()
        def owner():
            with PublicationStore(self.path).locked():
                ready.set(); finish.wait(10)
        process = multiprocessing.get_context("fork").Process(target=owner)
        process.start()
        try:
            self.assertTrue(ready.wait(5))
            with self.assertRaisesRegex(TaskError, "Ctrl\\+C in its terminal first"):
                self.abort()
            self.assertEqual(self.store.read()[0]["status"], "interrupted")
        finally:
            finish.set(); process.join(5)

    def test_cli_abort_controls_and_help(self):
        self.interrupt("review")
        self.stop_agents()
        with patch("sys.stdout", io.StringIO()) as output:
            self.assertEqual(cli.main(["loop", "DEV-7", "--abort", "--json"]), 0)
        self.assertEqual(json.loads(output.getvalue())["state"], "aborted")
        for extra in (["--from-review"], ["--r-model", "different"], ["--timeout", "5"]):
            with patch("sys.stderr", io.StringIO()):
                self.assertEqual(cli.main(["loop", "DEV-7", "--abort", *extra]), 1)
        with patch("sys.stdout", io.StringIO()) as output, self.assertRaises(SystemExit):
            cli.parser().parse_args(["loop", "--help"])
        help_text = " ".join(output.getvalue().split())
        self.assertIn("Ctrl+C first", help_text)
        self.assertIn("Herdr's Linux host", help_text)
        self.assertIn("not machine-wide checkout non-use", help_text)
        self.assertIn("Stop manually detached or external helpers", help_text)
        with patch("task_start.cli.loop", side_effect=KeyboardInterrupt), patch("sys.stderr", io.StringIO()) as error:
            self.assertEqual(cli.main(["loop", "DEV-7", "--abort"]), 130)
        self.assertIn("--abort to finish reconciliation", error.getvalue())


class LoopProcessTests(unittest.TestCase):
    setUp = cleanup.DisposalProcessTests.setUp
    process = cleanup.DisposalProcessTests.process
    shells = cleanup.DisposalProcessTests.shells

    def stat_fields(self, pid, **changes):
        stat = self.proc / str(pid) / "stat"
        fields = stat.read_text().rpartition(") ")[2].split()
        for index, value in changes.items():
            fields[int(index)] = str(value)
        stat.write_text(f"{pid} (fixture) " + " ".join(fields))

    def deny_private_references(self, *pids, denied=None):
        """Model denied references and record any attempt to inspect them."""
        original_link, original_iter, original_bytes = os.readlink, Path.iterdir, Path.read_bytes
        entries = {self.proc / str(pid) for pid in pids}
        reads = []
        def deny(path):
            if Path(path).parent in entries:
                reads.append(str(path))
                if denied is None or Path(path).name in denied:
                    raise PermissionError("private process references")
        def readlink(path, *args, **kwargs):
            deny(path)
            return original_link(path, *args, **kwargs)
        def iterdir(path):
            deny(path)
            return original_iter(path)
        def read_bytes(path):
            deny(path)
            return original_bytes(path)
        self.enterContext(patch("os.readlink", side_effect=readlink))
        self.enterContext(patch.object(Path, "iterdir", iterdir))
        self.enterContext(patch.object(Path, "read_bytes", read_bytes))
        return reads

    def test_unrelated_references_are_out_of_scope_regardless_of_age_or_ancestry(self):
        for started in (6, 7, 8):
            for kind in ("private", "cwd", "argv", "fd", "ancestor"):
                with self.subTest(started=started, kind=kind):
                    case = LoopProcessTests(); case.setUp()
                    try:
                        case.process(123, 938 if kind == "ancestor" else 1, case.checkout)
                        case.process(938, 1, case.checkout if kind == "cwd" else case.proc,
                                     started=started, comm="systemd",
                                     argv=f"worker\0{case.checkout}\0" if kind == "argv" else "systemd\0")
                        if kind in {"fd", "private", "ancestor"}:
                            (case.proc / "938" / "fd" / "8").symlink_to(case.checkout / "work.txt")
                        reads = case.deny_private_references(938) if kind in {"private", "ancestor"} else []
                        self.assertEqual(stopped_loop_execution(case.checkout, case.shells(123), proc=case.proc),
                                         {"123": 7})
                        self.assertEqual(reads, [], "Unmanaged references must not be inspected")
                    finally:
                        case.doCleanups()

    def test_attributable_workers_refuse_even_without_checkout_references(self):
        for kind in ("child", "grandchild", "reparented_session", "reparented_group", "thread"):
            with self.subTest(kind=kind):
                case = LoopProcessTests(); case.setUp()
                try:
                    case.process(123, 1, case.checkout)
                    case.process(124, 123 if kind in {"child", "grandchild"} else 1, case.proc,
                                 argv="worker\0", state="Z" if kind == "grandchild" else "S")
                    if kind == "grandchild":
                        case.process(125, 124, case.proc, argv="worker\0")
                    if kind in {"reparented_session", "reparented_group"}:
                        case.stat_fields(124, **{"2" if kind == "reparented_group" else "3": 123})
                    if kind == "thread":
                        (case.proc / "124").rename(case.proc / "123" / "task" / "124")
                    with self.assertRaisesRegex(TaskError, "registered task execution stopped"):
                        stopped_loop_execution(case.checkout, case.shells(123), proc=case.proc)
                finally:
                    case.doCleanups()

    def test_registered_execution_evidence_must_be_complete(self):
        for kind in ("missing", "exe", "argv", "cwd", "running", "session", "foreground", "no_tty",
                     "task_list", "thread_stat", "stat", "visibility", "private"):
            with self.subTest(kind=kind):
                case = LoopProcessTests(); case.setUp()
                try:
                    case.process(123, 1, case.checkout)
                    root = case.proc / "123"
                    if kind == "missing":
                        shutil.rmtree(root)
                    elif kind == "exe":
                        (root / "exe").unlink()
                        (root / "exe").symlink_to(sys.executable)
                    elif kind == "argv":
                        (root / "cmdline").write_bytes(b"bash\0-c\0worker\0")
                    elif kind == "cwd":
                        (root / "cwd").unlink()
                        (root / "cwd").symlink_to(case.proc)
                    elif kind in {"running", "session", "foreground", "no_tty"}:
                        index, value = {"running": (0, "R"), "session": (3, 999),
                                        "foreground": (5, 999), "no_tty": (4, 0)}[kind]
                        case.stat_fields(123, **{str(index): value})
                    elif kind == "task_list":
                        (root / "task" / "123").unlink()
                    elif kind == "thread_stat":
                        (root / "task" / "124").mkdir()
                    elif kind == "stat":
                        (root / "stat").write_text("malformed")
                    elif kind == "visibility":
                        table = case.proc / "self" / "mounts"
                        table.write_bytes(table.read_bytes().replace(b"rw,", b"rw,hidepid=2,"))
                    else:
                        case.deny_private_references(123)
                    with self.assertRaisesRegex(TaskError, "--abort refused"):
                        stopped_loop_execution(case.checkout, case.shells(123), proc=case.proc)
                finally:
                    case.doCleanups()
        with self.assertRaisesRegex(TaskError, "Missing original task shells"):
            stopped_loop_execution(self.checkout, {}, proc=self.proc)
        for platform in ("darwin", "win32"):
            with patch("task_start.integration_process.sys.platform", platform), self.assertRaisesRegex(TaskError, "Linux"):
                stopped_loop_execution(self.checkout, self.shells(123), proc=self.proc)

    def test_graph_disappearance_only_tolerates_genuinely_vanished_unrelated_entries(self):
        for kind in ("unrelated_exit", "missing_stat", "denied_stat", "shell_exit", "shell_reuse"):
            with self.subTest(kind=kind):
                case = LoopProcessTests(); case.setUp()
                try:
                    case.process(123, 1, case.checkout)
                    case.process(938, 1, case.proc)
                    read_text = Path.read_text
                    changed = False
                    target = case.proc / ("123" if kind.startswith("shell") else "938")
                    def racing(path, *args, **kwargs):
                        nonlocal changed
                        if path == target / "stat" and not changed:
                            changed = True
                            if kind == "denied_stat":
                                raise PermissionError("ancestry unavailable")
                            if kind == "shell_reuse":
                                observed = read_text(path, *args, **kwargs)
                                case.stat_fields(123, **{"19": 8})
                                return observed
                            if kind == "missing_stat":
                                path.unlink()
                            else:
                                shutil.rmtree(target)
                            raise FileNotFoundError("process observation vanished")
                        return read_text(path, *args, **kwargs)
                    with patch.object(Path, "read_text", racing):
                        if kind == "unrelated_exit":
                            self.assertEqual(stopped_loop_execution(case.checkout, case.shells(123), proc=case.proc),
                                             {"123": 7})
                        else:
                            with self.assertRaises(TaskError):
                                stopped_loop_execution(case.checkout, case.shells(123), proc=case.proc)
                    self.assertTrue(changed)
                finally:
                    case.doCleanups()

    def test_shell_reference_races_and_worker_appearing_at_final_inspection_refuse(self):
        for kind in ("vanished_cwd", "reused_pid", "new_child"):
            with self.subTest(kind=kind):
                case = LoopProcessTests(); case.setUp()
                try:
                    case.process(123, 1, case.checkout)
                    readlink, calls = os.readlink, 0
                    def racing(path, *args, **kwargs):
                        nonlocal calls
                        if Path(path) == case.proc / "123" / "cwd":
                            calls += 1
                            if kind == "vanished_cwd":
                                raise FileNotFoundError("missing registered cwd")
                            if calls == 2:
                                if kind == "reused_pid":
                                    case.stat_fields(123, **{"19": 8})
                                else:
                                    case.process(124, 123, case.proc, argv="worker\0")
                        return readlink(path, *args, **kwargs)
                    with patch("os.readlink", side_effect=racing), self.assertRaises(TaskError):
                        stopped_loop_execution(case.checkout, case.shells(123), proc=case.proc)
                    self.assertLessEqual(calls, 2, "Uncertain registered evidence must not retry indefinitely")
                finally:
                    case.doCleanups()


class StoppedProviderTests(unittest.TestCase):
    setUp = provider.ProviderCompletionTests.setUp

    def test_codex_requires_terminal_exact_turn_and_empty_queue(self):
        for status in ("completed", "interrupted", "failed"):
            self.turn["status"] = status
            self.adapter.verify_stopped_session(self.execution.workspace, self.reference, self.receipt)
        for change in ("working", "queue", "missing_queue", "partial", "prompt", "newer", "fork"):
            with self.subTest(change=change):
                turn, queue, thread = copy.deepcopy(self.turn), copy.deepcopy(self.queue), copy.deepcopy(self.thread)
                if change == "working":
                    self.turn["status"] = "inProgress"
                elif change == "queue":
                    self.queue["data"] = [dict(id="pending")]
                elif change == "missing_queue":
                    self.queue.pop("nextCursor")
                elif change == "partial":
                    self.turn["itemsView"] = "summary"
                elif change == "prompt":
                    self.turn["items"][0]["content"][0]["text"] = "different input"
                elif change == "newer":
                    self.turn["items"][0]["clientId"] = "different-turn"
                else:
                    self.thread["parentThreadId"] = "parent"
                with self.assertRaises(TaskError):
                    self.adapter.verify_stopped_session(self.execution.workspace, self.reference, self.receipt)
                self.turn, self.queue, self.thread = turn, queue, thread
        self.assertTrue(all(call.args[0] in {"thread/read", "thread/turns/list", "thread/queue/list"}
                            for call in self.rpc.request.call_args_list))

    def test_pi_accepts_interrupted_chain_but_refuses_replaced_or_newer_history(self):
        adapter = Pi(AgentOptions("pi", "model", "high"))
        path = self.path / "pi.jsonl"
        reference = dict(agent="pi", kind="path", value=str(path), conversation_id="original")
        entries = [dict(type="session", id="original", cwd=str(self.path)),
                   dict(type="message", id="input", parentId=None,
                        message=dict(role="user", content="exact nonce-bound prompt")),
                   dict(type="message", id="aborted", parentId="input",
                        message=dict(role="assistant", stopReason="aborted", content=[]))]
        def write(value):
            path.write_text("".join(json.dumps(v) + "\n" for v in value))
        write(entries)
        adapter.verify_stopped_session(self.execution.workspace, reference, self.receipt)
        self.assertFalse(adapter.observe_delivery(self.execution, reference, self.receipt, None))
        for change in ("replaced", "newer", "branched", "partial"):
            with self.subTest(change=change):
                bad = copy.deepcopy(entries)
                if change == "replaced":
                    bad[0]["id"] = "replacement"
                elif change == "newer":
                    bad.append(dict(type="message", id="next", parentId="aborted",
                                    message=dict(role="user", content="new work")))
                elif change == "branched":
                    bad[-1]["parentId"] = "unrelated"
                write(bad)
                if change == "partial":
                    with path.open("a") as output:
                        output.write('{"partial":')
                with self.assertRaises(TaskError):
                    adapter.verify_stopped_session(self.execution.workspace, reference, self.receipt)


class InitialAbortTests(unittest.TestCase):
    command = bootstrap.BootstrapLoopTests.command
    pane_command = bootstrap.BootstrapLoopTests.pane_command
    worktrees = bootstrap.BootstrapLoopTests.worktrees
    select_adapter = bootstrap.BootstrapLoopTests.select_adapter
    implementation_adapter = bootstrap.BootstrapLoopTests.implementation_adapter
    update_base = bootstrap.BootstrapLoopTests.update_base
    setUp = bootstrap.BootstrapLoopTests.setUp

    def test_sessionless_initial_reservation_aborts_and_reuses_only_undelivered_allocation(self):
        with patch.object(PassDelivery, "attach", side_effect=KeyboardInterrupt):
            self.assertEqual(loop("DEV-7", timeout=1).state, "interrupted")
        original = self.store.read()[0]
        self.assertIsNone(original["delivery"]["receipt"])
        self.assertEqual(self.launches, [])
        before = snapshot(self.path, self.base, self.branch)
        with patch("task_start.loop_abort.ContextRegistry", return_value=self.registry), \
                patch("task_start.loop_abort.HerdrContexts", return_value=self.identities), \
                patch("task_start.loop_abort.stopped_loop_execution", return_value={"123": 7}):
            self.assertEqual(loop("DEV-7", action="abort").state, "aborted")
        self.assertEqual(snapshot(self.path, self.base, self.branch), before)
        preparation = list(self.preparation)
        result = loop("DEV-7", action="new", timeout=1)
        self.assertEqual(result.state, "clean", result.render())
        self.assertEqual(self.preparation, preparation)
        self.assertEqual(len(self.launches), 1)
        self.assertEqual(self.store.read()[0]["implementation"]["context_id"], "DEV-7-I1")
        self.assertNotIn(original["active_pass"]["pass_id"], self.impl_prompts[0])


class InterruptedControllerTests(unittest.TestCase):
    """Independent original agent process and signalled controller processes."""
    command = recovery.ProcessRecoveryTests.command
    pane_command = recovery.ProcessRecoveryTests.pane_command
    worktrees = recovery.ProcessRecoveryTests.worktrees
    select_adapter = recovery.ProcessRecoveryTests.select_adapter
    implementation_adapter = recovery.ProcessRecoveryTests.implementation_adapter
    update_base = recovery.ProcessRecoveryTests.update_base
    read_provider = recovery.ProcessRecoveryTests.read_provider
    save_provider = recovery.ProcessRecoveryTests.save_provider
    pane_snapshot = recovery.ProcessRecoveryTests.pane_snapshot
    herdr = recovery.ProcessRecoveryTests.herdr
    controller = recovery.ProcessRecoveryTests.controller
    wait_until = recovery.ProcessRecoveryTests.wait_until
    stop_children = recovery.ProcessRecoveryTests.stop_children

    def setUp(self):
        recovery.ProcessRecoveryTests.setUp(self)
        self.enterContext(patch("task_start.loop_abort.ContextRegistry", return_value=self.registry))
        self.enterContext(patch("task_start.loop_abort.HerdrContexts", return_value=self.identities))
        self.enterContext(patch("task_start.loop_abort.stopped_loop_execution", return_value={"123": 7}))
        self.enterContext(patch("task_start.loop_abort.adapter_for", side_effect=Codex))

    def request(self, method, params, **kwargs):
        response = recovery.ProcessRecoveryTests.request(self, method, params, **kwargs)
        if method == "thread/turns/list" and self.read_provider().get("interrupted"):
            response["data"][0]["status"] = "interrupted"
        return response

    def scenario(self, interruption):
        first, _ = self.controller("original")
        self.wait_until(lambda: self.read_provider() and self.read_provider()["prompts"])
        self.wait_until(lambda: self.registry.get("DEV-7-I1")["state"] == "active")
        self.wait_until(lambda: (self.root / "worker.pid").exists())
        self.store = LoopStore(self.path)
        original = self.store.read()[0]
        import shutil
        self.addCleanup(shutil.rmtree, original["delivery"]["directory"], True)
        if interruption in {"controller", "both"}:
            os.kill(first.pid, signal.SIGINT)
            first.join(5)
        if interruption == "controller":
            with self.assertRaises(TaskError):
                loop("DEV-7", action="abort")
        worker = int((self.root / "worker.pid").read_text())
        os.kill(worker, signal.SIGTERM)
        value = self.read_provider()
        value["interrupted"] = True
        value["pane"]["agent_status"] = "idle"  # Session remains open after interrupt.
        self.save_provider(value)
        if interruption == "agent":
            self.assertTrue(first.is_alive())
            with self.assertRaisesRegex(TaskError, "Ctrl\\+C"):
                loop("DEV-7", action="abort")
            os.kill(first.pid, signal.SIGINT)
            first.join(5)
        self.assertFalse(first.is_alive())
        with self.assertRaisesRegex(TaskError, "Quit its agent|Quit|live, missing, or uncertain"):
            loop("DEV-7", action="abort")
        # The fixture's provider history locator survives the runtime exit.
        value["pane"].update(agent=None, agent_status="unknown")
        self.save_provider(value)
        (self.path / "partial.txt").write_text("interrupted implementation")
        self.command(self.path, "add", "partial.txt")
        (self.path / "untracked.txt").write_text("preserve")
        before = snapshot(self.path, self.base, self.branch)
        self.assertEqual(loop("DEV-7", action="abort").state, "aborted")
        self.assertEqual(snapshot(self.path, self.base, self.branch), before)
        self.assertEqual(len(self.read_provider()["prompts"]), 1)
        self.assertEqual(self.store.abort_journal(original["run_id"])["original"]["active_pass"], original["active_pass"])
        self.assertFalse(self.store.read()[0]["abandonment"]["review_ready"])
        self.assertEqual(loop("DEV-7").state, "aborted")

    def test_controller_only_interrupt_does_not_authorize_abort_of_live_agent(self):
        self.scenario("controller")

    def test_agent_only_interrupt_requires_stopping_waiting_controller_then_quitting_session(self):
        self.scenario("agent")

    def test_both_interrupted_preserve_partial_implementation(self):
        self.scenario("both")


class RealShellTests(unittest.TestCase):
    """PTY shells, live background jobs and detached workers; no provider binaries."""
    def setUp(self):
        import tempfile
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name)
        self.start_shell()
        # Sandboxes may stack /proc mounts. The live test controls visibility;
        # synthetic fixtures separately exercise unreadable/hidden mount evidence.
        self.enterContext(patch("task_start.integration_process._process_visibility", return_value=b"controlled mounts"))

    def start_shell(self):
        self.master, slave = pty.openpty()
        def terminal():
            os.setsid()
            fcntl.ioctl(slave, termios.TIOCSCTTY, 0)
        self.argv = ["/bin/bash", "--noprofile", "--norc", "-i"]
        self.shell = subprocess.Popen(self.argv, cwd=self.path, stdin=slave, stdout=slave, stderr=slave,
                                      preexec_fn=terminal)
        os.close(slave)
        self.addCleanup(self.stop_shell)
        self.read_prompt()

    def stop_shell(self):
        self.shell.terminate()
        try:
            self.shell.wait(timeout=2)
        except subprocess.TimeoutExpired:
            self.shell.kill(); self.shell.wait()
        os.close(self.master)

    def read_prompt(self):
        deadline = time.monotonic() + 5
        data = b""
        while time.monotonic() < deadline:
            if select.select([self.master], [], [], 0.05)[0]:
                data += os.read(self.master, 65536)
                if data.endswith((b"# ", b"$ ")):
                    return
        self.fail("Disposable shell did not reach prompt")

    def proof(self):
        return stopped_loop_execution(self.path, {self.shell.pid: dict(cwd=self.path, argv=self.argv)})

    def test_background_job_refuses_but_unmanaged_detached_worker_is_out_of_scope(self):
        self.assertIn(str(self.shell.pid), self.proof())
        os.write(self.master, b"sleep 30 &\n")
        self.read_prompt()
        with self.assertRaises(TaskError):
            self.proof()
        os.write(self.master, b"kill %1; wait %1\n")
        self.read_prompt()
        self.assertIn(str(self.shell.pid), self.proof())
        worker = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], cwd=self.path,
                                  start_new_session=True)
        try:
            # This worker can access the checkout but has no registered family
            # or session evidence. Passing is a scope decision, not non-use proof.
            self.assertIn(str(self.shell.pid), self.proof())
        finally:
            worker.terminate(); worker.wait()
        self.assertIn(str(self.shell.pid), self.proof())


class ManagedAbortCliTests(unittest.TestCase):
    """Real CLI/Git/checkpoints with controlled Herdr/provider and Linux evidence."""

    @contextmanager
    def execution(self, *, real_shell=False):
        case, processes = AbortTests(), LoopProcessTests()
        case.setUp()
        processes.setUp()
        shell = None
        try:
            (case.path / "tracked.txt").write_text("staged work")
            case.command(case.path, "add", "tracked.txt")
            (case.path / "tracked.txt").write_text("unstaged work")
            (case.path / "untracked.txt").write_bytes(b"untracked\0work")
            if real_shell:
                # A real, disposable original PTY shell; only its /proc entry is
                # exposed alongside the synthetic service in this process graph.
                shell = RealShellTests()
                shell.path = case.path
                shell.start_shell()
                (processes.proc / str(shell.shell.pid)).symlink_to(Path("/proc") / str(shell.shell.pid))
                pane = case.panes[0]["pane_id"]
                case.identity_command.side_effect = lambda *args: dict(process_info=dict(
                    pane_id=pane, shell_pid=shell.shell.pid, foreground_process_group_id=shell.shell.pid,
                    foreground_processes=[dict(pid=shell.shell.pid, argv=shell.argv)]))
            else:
                processes.process(101, 1, case.path)
            original = case.interrupt("implementation")
            case.stop_agents()
            case.proof.side_effect = lambda path, shells: stopped_loop_execution(path, shells, proc=processes.proc)
            yield case, processes, original
        finally:
            if shell:
                shell.doCleanups()
            processes.doCleanups()
            case.doCleanups()

    def test_cli_real_stopped_shell_ignores_denied_pid_938_and_preserves_original_claim(self):
        with self.execution(real_shell=True) as (case, processes, original):
            processes.process(938, 1, case.path, comm="systemd", argv="systemd\0", started=1)
            (processes.proc / "938" / "fd" / "8").symlink_to(case.path / "tracked.txt")
            reads = processes.deny_private_references(938, denied={"cwd", "fd"})
            # The real shell's parent is also outside the managed family. Give
            # its synthetic entry accessible checkout references, like an editor.
            processes.process(os.getpid(), 1, case.path, argv=f"external-parent\0{case.path}\0", started=1)
            accessible_reads = processes.deny_private_references(os.getpid(), denied=set())
            before, rows = case.content(), case.registry.list(include_retired=True)
            delivery_files = {p.name: p.read_bytes() for p in Path(original["delivery"]["directory"]).iterdir()
                              if p.is_file()}
            with patch("sys.stdout", io.StringIO()) as output, \
                    patch("task_start.loop.load_local", side_effect=AssertionError("Local config accessed")):
                self.assertEqual(cli.main(["loop", "DEV-7", "--abort", "--json"]), 0)
            self.assertEqual(json.loads(output.getvalue())["state"], "aborted")
            journal = case.store.abort_journal(original["run_id"])
            self.assertEqual(journal["original"], original)
            self.assertEqual(journal["contexts"], rows)
            self.assertEqual(journal["snapshot"], before)
            self.assertEqual(len(journal["processes"]), 1)
            self.assertEqual(case.content(), before)
            self.assertEqual(case.command(case.path, "branch", "--show-current"), case.branch)
            self.assertEqual(reads, [], "Permission denial must not be used as unrelated-process evidence")
            self.assertEqual(accessible_reads, [], "An ancestor with checkout references is outside the proof")
            self.assertEqual(case.proof.call_count, 2, "The transaction must repeat its process proof before journaling")
            self.assertEqual(case.abort().state, "aborted")
            self.assertEqual(case.store.abort_journal(original["run_id"]), journal)
            self.assertEqual({p.name: p.read_bytes() for p in Path(original["delivery"]["directory"]).iterdir()
                              if p.is_file()}, delivery_files)
            with self.assertRaises(TaskError):
                seal(original["delivery"]["directory"], original["run_id"],
                     original["active_pass"]["pass_id"], str(case.path), case.base, case.branch)
            self.assertIsNone(PublicationStore(case.path).read()["acceptance"])

    def test_cli_refusal_creates_no_journal_or_replacement_handoff(self):
        for kind in ("child", "group", "reparented", "exe", "argv", "cwd", "missing_shell", "stat",
                     "pid_reuse_before_journal", "worker_before_journal", "live_pi", "live_codex",
                     "unknown_pane", "active_pane", "endpoint", "provider", "provider_session", "terminal"):
            with self.subTest(kind=kind), self.execution() as (case, processes, original):
                root = processes.proc / "101"
                if kind in {"child", "group", "reparented"}:
                    processes.process(124, 101 if kind == "child" else 1, processes.proc, argv="worker\0")
                    if kind != "child":
                        processes.stat_fields(124, **{"2" if kind == "group" else "3": 101})
                elif kind == "exe":
                    (root / "exe").unlink()
                    (root / "exe").symlink_to(sys.executable)
                elif kind == "argv":
                    (root / "cmdline").write_bytes(b"codex\0")
                elif kind == "cwd":
                    (root / "cwd").unlink()
                    (root / "cwd").symlink_to(processes.proc)
                elif kind == "missing_shell":
                    shutil.rmtree(root)
                elif kind == "stat":
                    (root / "stat").unlink()
                elif kind in {"pid_reuse_before_journal", "worker_before_journal"}:
                    def changed(path, shells):
                        if case.proof.call_count == 2:
                            if kind == "pid_reuse_before_journal":
                                processes.stat_fields(101, **{"19": 8})
                            else:
                                processes.process(124, 101, processes.proc, argv="worker\0")
                        return stopped_loop_execution(path, shells, proc=processes.proc)
                    case.proof.side_effect = changed
                elif kind.startswith("live_"):
                    case.panes[0].update(agent=kind.removeprefix("live_"), agent_status="working")
                elif kind in {"unknown_pane", "active_pane"}:
                    case.panes.append(dict(case.panes[0], pane_id="extra", terminal_id="extra",
                                           agent="codex" if kind == "active_pane" else None))
                elif kind == "endpoint":
                    case.enterContext(patch.object(case.identities, "endpoint", return_value="/different.sock"))
                elif kind == "provider":
                    case.implementer.verify_stopped_session = lambda *args: (_ for _ in ()).throw(TaskError("Pending turn"))
                elif kind == "provider_session":
                    case.registry.update("DEV-7-I1", session_id="replaced")
                else:
                    case.panes[0]["terminal_id"] = "replaced"
                before, rows = case.content(), case.registry.list(include_retired=True)
                counts = len(case.impl_prompts), len(case.prompts)
                with patch("sys.stderr", io.StringIO()) as error:
                    self.assertEqual(cli.main(["loop", "DEV-7", "--abort", "--json"]), 1)
                self.assertTrue(error.getvalue())
                self.assertIsNone(case.store.abort_journal(original["run_id"]))
                self.assertEqual(case.store.read()[0], original)
                self.assertEqual(case.registry.list(include_retired=True), rows)
                self.assertEqual(case.content(), before)
                self.assertEqual((len(case.impl_prompts), len(case.prompts)), counts)
                self.assertIsNone(PublicationStore(case.path).read()["acceptance"])

    def test_interrupted_retry_pins_process_identity_and_keeps_abandonment_evidence(self):
        with self.execution() as (case, processes, original):
            before = case.content()
            with patch.object(ContextRegistry, "reconcile_loop_abort", side_effect=KeyboardInterrupt):
                with self.assertRaises(KeyboardInterrupt):
                    case.abort()
            journal = case.store.abort_journal(original["run_id"])
            self.assertEqual(journal["original"], original)
            self.assertEqual(journal["processes"], {"101": 7})
            processes.stat_fields(101, **{"19": 8})
            with self.assertRaisesRegex(TaskError, "changed during abort"):
                case.abort()
            self.assertEqual(case.store.abort_journal(original["run_id"]), journal)
            self.assertEqual(case.store.read()[0]["status"], "aborting")
            self.assertEqual(case.content(), before)
            # Restore only the synthetic observation to model retry on the same
            # original shell, never accepting a real replacement shell.
            processes.stat_fields(101, **{"19": 7})
            self.assertEqual(case.abort().state, "aborted")
            self.assertEqual(case.abort().state, "aborted")
            self.assertEqual(case.store.abort_journal(original["run_id"]), journal)
            self.assertEqual(case.content(), before)
