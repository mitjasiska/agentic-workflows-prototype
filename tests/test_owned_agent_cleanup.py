"""Exact idle-agent retirement, using disposable Git and Linux process fixtures."""

from contextlib import contextmanager
import copy
import json
import os
from pathlib import Path
import pty
import shutil
import signal
import tempfile
import time
import unittest
from unittest.mock import patch
from uuid import uuid4

from task_start import HerdrResponseError, TaskError, cli
from task_start.agent import AgentOptions, CodexAdapter, PiAdapter
from task_start.cleanup import DisposalStore
from task_start.integration_process import owned_pane_processes
from task_start.workspace import Herdr, Workspace
import test_canceled_cleanup as fixtures


def change_stat(proc, pid, **changes):
    path = proc / str(pid) / "stat"
    prefix, rest = path.read_text().rsplit(")", 1)
    fields = rest.split()
    indexes = dict(state=0, parent=1, group=2, session=3, terminal=4, foreground=5, started=19)
    for key, value in changes.items():
        fields[indexes[key]] = str(value)
    path.write_text(prefix + ") " + " ".join(fields))


@contextmanager
def execution():
    with fixtures.ForcedCleanupTests() as case:
        case.proc = case.repo.parent / "proc"
        case.proc.mkdir()
        fixtures.process_mounts(case.proc)
        case.agent_pids = []
        case.infos = {}
        for number, pane in enumerate(case.panes[:2], 101):
            context = case.registry.get(f"DEV-7-{'I1' if number == 101 else 'R1'}")
            pane.update(agent="codex", foreground_cwd=pane["cwd"],
                        agent_session=dict(agent="codex", kind="id", value=context["session_id"]))
            fixtures.DisposalProcessTests.process(case, number, 1, case.path)
            agent = number + 100
            case.agent_pids.append(agent)
            fixtures.DisposalProcessTests.process(case, agent, number, case.path,
                                                 argv="/bin/sleep\000120\0", executable="/bin/sleep")
            change_stat(case.proc, number, foreground=agent)
            change_stat(case.proc, agent, group=agent, session=number, foreground=agent)
            case.infos[pane["pane_id"]] = dict(pane_id=pane["pane_id"], shell_pid=number,
                foreground_process_group_id=agent,
                foreground_processes=[dict(pid=agent, argv=["/bin/sleep", "120"])])
        case.identities.command.side_effect = lambda *args: dict(process_info=copy.deepcopy(case.infos[args[-1]]))
        case.enterContext(patch("task_start.cleanup.owned_pane_processes",
            side_effect=lambda panes, **kwargs: owned_pane_processes(panes, proc=case.proc, **kwargs)))
        case.provider = case.enterContext(patch.object(CodexAdapter, "cleanup_provider_idle",
                                                       side_effect=lambda workspace, reference: reference))
        case.capability = case.enterContext(patch.object(Herdr, "check_agent_retirement"))
        case.enterContext(patch("task_start.integration_process.shutil.which", return_value="/bin/sleep"))
        original_close = case.workspace_commands
        def close(operation, *args):
            result = original_close(operation, *args)
            if operation == "close":
                for pid in (101, 102, 201, 202):
                    if (case.proc / str(pid)).exists():
                        shutil.rmtree(case.proc / str(pid))
            return result
        case.close_agents = close
        case.herdr_workspace.side_effect = close
        yield case


class OwnedAgentCleanupTests(unittest.TestCase):
    def test_idle_and_done_foreground_agents_stop_and_disposal_is_idempotent(self):
        with execution() as case:
            case.panes[1]["agent_status"] = "done"
            output = case.retained_loop()
            cli.cleanup("DEV-7", force=True)
            case.assert_disposed()
            self.assertTrue(case.provider.called)
            proof = case.journal()["agent_retirement"]
            self.assertEqual(proof["process_proof"], 2)
            self.assertEqual(set(proof["families"]), {"p1", "p2"})
            self.assertEqual(set(proof["families"]["p1"]["members"]), {"101", "201"})
            before = case.journal_path().read_bytes()
            self.assertIn("already discarded", cli.cleanup("DEV-7", force=True))
            self.assertEqual(case.journal_path().read_bytes(), before)
            self.assertEqual(sum(c.args[0] == "close" for c in case.herdr_workspace.call_args_list), 1)
            self.assertFalse(output.exists())

    def test_working_blocked_unknown_launching_or_changed_session_refuse_before_close(self):
        for kind in ("working", "blocked", "unknown", "launching", "session", "missing_session",
                     "malformed_session", "terminal", "unregistered", "provider"):
            with self.subTest(kind=kind), execution() as case:
                if kind in {"working", "blocked", "unknown"}:
                    case.panes[0]["agent_status"] = kind
                elif kind == "launching":
                    case.panes[0]["launch_pending"] = True
                elif kind == "session":
                    case.panes[0]["agent_session"]["value"] = "replaced"
                elif kind == "missing_session":
                    case.panes[0]["agent_session"] = None
                elif kind == "malformed_session":
                    case.panes[0]["agent_session"] = {}
                elif kind == "terminal":
                    case.panes[0]["terminal_id"] = "replaced"
                elif kind == "unregistered":
                    case.panes.append(dict(case.panes[0], pane_id="unknown", terminal_id="unknown"))
                else:
                    case.provider.side_effect = TaskError("pending provider input")
                with self.assertRaises(TaskError):
                    cli.cleanup("DEV-7", force=True)
                case.assert_preserved()
                self.assertFalse(any(c.args[0] == "close" for c in case.herdr_workspace.call_args_list))

    def test_missing_or_unsupported_herdr_never_stops_agents(self):
        for reason in ("herdr is not installed", "server version unsupported"):
            with self.subTest(reason=reason), execution() as case:
                case.capability.side_effect = TaskError(reason)
                with self.assertRaisesRegex(TaskError, reason):
                    cli.cleanup("DEV-7", force=True)
                case.assert_preserved()
                self.assertFalse(any(c.args[0] == "close" for c in case.herdr_workspace.call_args_list))

    def test_same_workspace_controller_refuses_before_provider_or_close(self):
        with execution() as case:
            with case.publication.locked(), self.assertRaisesRegex(TaskError, "stop that controller"):
                cli.cleanup("DEV-7", force=True)
            case.provider.assert_not_called()
            case.assert_preserved()

    def test_background_shell_job_refuses_even_with_idle_provider(self):
        with execution() as case:
            fixtures.DisposalProcessTests.process(case, 301, 101, case.other, argv="worker\0")
            change_stat(case.proc, 301, session=101)
            with self.assertRaisesRegex(TaskError, "background worker"):
                cli.cleanup("DEV-7", force=True)
            case.assert_preserved()
            self.assertFalse(any(c.args[0] == "close" for c in case.herdr_workspace.call_args_list))

    def test_unregistered_foreground_sibling_is_not_an_owned_provider_worker(self):
        with execution() as case:
            fixtures.DisposalProcessTests.process(case, 301, 101, case.path, argv="worker\0")
            change_stat(case.proc, 301, group=201, session=101, foreground=201)
            case.infos["p1"]["foreground_processes"].append(dict(pid=301, argv=["worker"]))
            with self.assertRaisesRegex(TaskError, "background worker"):
                cli.cleanup("DEV-7", force=True)
            case.assert_preserved()
            self.assertFalse(any(c.args[0] == "close" for c in case.herdr_workspace.call_args_list))

    def test_second_provider_sibling_and_argv_impostor_refuse(self):
        for scenario in ("second_provider", "argv_impostor"):
            with self.subTest(scenario=scenario), execution() as case:
                if scenario == "second_provider":
                    fixtures.DisposalProcessTests.process(case, 301, 101, case.path,
                        argv="/bin/sleep\000120\0", executable="/bin/sleep")
                    change_stat(case.proc, 301, group=201, session=101, foreground=201)
                    case.infos["p1"]["foreground_processes"].append(dict(pid=301, argv=["/bin/sleep", "120"]))
                else:
                    (case.proc / "201" / "exe").unlink()
                    (case.proc / "201" / "exe").symlink_to("/bin/cat")
                    (case.proc / "201" / "cmdline").write_bytes(b"/bin/cat\0/bin/sleep\0")
                    case.infos["p1"]["foreground_processes"][0]["argv"] = ["/bin/cat", "/bin/sleep"]
                with self.assertRaisesRegex(TaskError, "multiple provider roots|foreground executable"):
                    cli.cleanup("DEV-7", force=True)
                case.assert_preserved()
                self.assertFalse(any(c.args[0] == "close" for c in case.herdr_workspace.call_args_list))

    def test_registered_provider_child_surviving_reparenting_blocks_all_deletion(self):
        with execution() as case:
            fixtures.DisposalProcessTests.process(case, 301, 201, case.other, argv="worker\0")
            change_stat(case.proc, 301, session=101)
            def close(operation, *args):
                result = case.close_agents(operation, *args)
                if operation == "close":
                    change_stat(case.proc, 301, parent=1, session=301)
                return result
            case.herdr_workspace.side_effect = close
            with self.assertRaisesRegex(TaskError, "301 still runs"):
                cli.cleanup("DEV-7", force=True)
            case.assert_preserved()
            self.assertEqual(case.journal()["workspace_state"], "closing")
            shutil.rmtree(case.proc / "301")
            cli.cleanup("DEV-7", force=True)
            case.assert_disposed()

    def test_nested_unregistered_provider_refuses_before_close_or_deletion(self):
        for scenario in ("foreground", "background", "grandchild", "other_provider", "interpreter", "relative_script"):
            with self.subTest(scenario=scenario), execution() as case:
                parent = 201
                if scenario == "grandchild":
                    fixtures.DisposalProcessTests.process(case, 300, 201, case.path, argv="worker\0")
                    change_stat(case.proc, 300, session=101)
                    parent = 300
                # Same executable identity as the registered fixture provider,
                # but a separate invocation whose turn has never been checked.
                executable, argv = "/bin/sleep", ["/bin/sleep", "exec", "unregistered-provider-turn"]
                if scenario == "other_provider":
                    executable, argv = "/bin/cat", ["codex", "exec", "unregistered-provider-turn"]
                elif scenario == "interpreter":
                    executable, argv = "/bin/cat", ["node", "--no-warnings", "/bin/sleep", "--print", "turn"]
                elif scenario == "relative_script":
                    (case.path / "provider.js").symlink_to("/bin/sleep")
                    executable, argv = "/bin/cat", ["node", "./provider.js", "--print", "turn"]
                fixtures.DisposalProcessTests.process(case, 301, parent, case.path,
                    argv="\0".join(argv) + "\0", executable=executable)
                change_stat(case.proc, 301, session=101)
                if scenario == "foreground":
                    change_stat(case.proc, 301, group=201, foreground=201)
                    case.infos["p1"]["foreground_processes"].append(dict(pid=301, argv=argv))
                with self.assertRaisesRegex(TaskError, "unregistered provider invocation.*301"):
                    cli.cleanup("DEV-7", force=True)
                case.assert_preserved()
                self.assertTrue((case.proc / "301").exists())
                self.assertFalse(any(c.args[0] == "close" for c in case.herdr_workspace.call_args_list))

    def test_nested_provider_appearing_after_failed_close_refuses_retry(self):
        with execution() as case:
            case.workspace_close_error = TaskError("close failed")
            with self.assertRaisesRegex(TaskError, "close failed"):
                cli.cleanup("DEV-7", force=True)
            before = case.journal_path().read_bytes()
            close_count = sum(c.args[0] == "close" for c in case.herdr_workspace.call_args_list)
            fixtures.DisposalProcessTests.process(case, 301, 201, case.path,
                argv="/bin/sleep\0exec\0unregistered-provider-turn\0", executable="/bin/sleep")
            change_stat(case.proc, 301, session=101)
            case.workspace_close_error = None
            with self.assertRaisesRegex(TaskError, "unregistered provider invocation.*301"):
                cli.cleanup("DEV-7", force=True)
            case.assert_preserved()
            self.assertEqual(case.journal_path().read_bytes(), before)
            self.assertEqual(sum(c.args[0] == "close" for c in case.herdr_workspace.call_args_list), close_count)

    def test_lost_ack_and_ctrl_c_reconcile_exact_original_resources(self):
        for interruption in (HerdrResponseError("lost response"), TaskError("lost acknowledgement"), KeyboardInterrupt()):
            with self.subTest(interruption=type(interruption).__name__), execution() as case:
                def close(operation, *args):
                    result = case.close_agents(operation, *args)
                    if operation == "close":
                        self.assertEqual(case.journal()["workspace_state"], "closing")
                        self.assertEqual(case.journal()["runtime"]["process_proof"], 2)
                        raise interruption
                    return result
                case.herdr_workspace.side_effect = close
                if not isinstance(interruption, HerdrResponseError):
                    with self.assertRaises(type(interruption)):
                        cli.cleanup("DEV-7", force=True)
                    case.assert_preserved()
                    cli.cleanup("DEV-7", force=True)
                else:
                    cli.cleanup("DEV-7", force=True)
                case.assert_disposed()
                self.assertEqual(sum(c.args[0] == "close" for c in case.herdr_workspace.call_args_list), 1)

    def test_failed_close_keeps_claim_and_retries(self):
        with execution() as case:
            case.workspace_close_error = TaskError("close failed")
            with self.assertRaisesRegex(TaskError, "close failed"):
                cli.cleanup("DEV-7", force=True)
            case.assert_preserved()
            self.assertEqual(case.journal()["workspace_state"], "closing")
            case.workspace_close_error = None
            cli.cleanup("DEV-7", force=True)
            case.assert_disposed()

    def test_conflicting_close_response_never_authorizes_final_deletion(self):
        with execution() as case:
            def close(operation, *args):
                result = case.close_agents(operation, *args)
                if operation == "close":
                    raise TaskError("Herdr close response identifies another workspace")
                return result
            case.herdr_workspace.side_effect = close
            with self.assertRaisesRegex(TaskError, "another workspace"):
                cli.cleanup("DEV-7", force=True)
            case.assert_preserved()
            self.assertEqual(case.journal()["workspace_state"], "closing")

    def test_partial_pane_retirement_retries_only_original_remaining_family(self):
        with execution() as case:
            original = case.workspace_commands
            def partial(operation, *args):
                if operation == "close":
                    case.panes[:] = [p for p in case.panes if p["pane_id"] != "p2"]
                    shutil.rmtree(case.proc / "102")
                    shutil.rmtree(case.proc / "202")
                    raise KeyboardInterrupt()
                return original(operation, *args)
            case.herdr_workspace.side_effect = partial
            with self.assertRaises(KeyboardInterrupt):
                cli.cleanup("DEV-7", force=True)
            frozen = case.journal()["runtime"]
            case.assert_preserved()
            case.herdr_workspace.side_effect = case.close_agents
            cli.cleanup("DEV-7", force=True)
            case.assert_disposed()
            self.assertEqual(case.journal()["agent_retirement"], frozen)

    def test_pid_reuse_after_panes_are_gone_refuses_without_targeting_replacement(self):
        with execution() as case:
            def close(operation, *args):
                result = case.close_agents(operation, *args)
                if operation == "close":
                    raise KeyboardInterrupt()
                return result
            case.herdr_workspace.side_effect = close
            with self.assertRaises(KeyboardInterrupt):
                cli.cleanup("DEV-7", force=True)
            fixtures.DisposalProcessTests.process(case, 201, 1, case.other, argv="unrelated\0", started=99)
            before = case.journal_path().read_bytes()
            with self.assertRaisesRegex(TaskError, "201 was reused"):
                cli.cleanup("DEV-7", force=True)
            case.assert_preserved()
            self.assertTrue((case.proc / "201").exists())
            self.assertEqual(case.journal_path().read_bytes(), before)

    def test_late_result_change_after_close_preserves_artifacts_and_claim(self):
        with execution() as case:
            output = case.retained_loop()
            def close(operation, *args):
                result = case.close_agents(operation, *args)
                if operation == "close":
                    output.write_text('{"late":true}')
                return result
            case.herdr_workspace.side_effect = close
            with self.assertRaisesRegex(TaskError, "output changed"):
                cli.cleanup("DEV-7", force=True)
            case.assert_preserved()
            self.assertTrue(output.exists())
            self.assertEqual(case.journal()["state"], "pending")

    def test_unrelated_workspace_can_take_its_lock_during_agent_stop(self):
        from concurrent.futures import ThreadPoolExecutor
        from task_start.ownership import ownership_gate
        from task_start.publication_state import PublicationStore
        with execution() as case:
            checked = []
            def independent():
                with PublicationStore(case.other).locked(), ownership_gate(exclusive=True):
                    checked.append(True)
            def close(operation, *args):
                if operation == "close":
                    with ThreadPoolExecutor(max_workers=1) as pool:
                        pool.submit(independent).result(timeout=5)
                return case.close_agents(operation, *args)
            case.herdr_workspace.side_effect = close
            cli.cleanup("DEV-7", force=True)
            case.assert_disposed()
            self.assertEqual(checked, [True])

    def test_reused_pid_shell_exec_or_replaced_pane_on_retry_refuse(self):
        for kind in ("pid", "shell", "pane", "new_worker", "argv"):
            with self.subTest(kind=kind), execution() as case:
                case.workspace_close_error = TaskError("stop before close")
                with self.assertRaises(TaskError):
                    cli.cleanup("DEV-7", force=True)
                before = case.journal_path().read_bytes()
                case.workspace_close_error = None
                if kind == "pid":
                    change_stat(case.proc, 201, started=500)
                elif kind == "shell":
                    (case.proc / "101" / "exe").unlink()
                    (case.proc / "101" / "exe").symlink_to("/bin/sleep")
                elif kind == "pane":
                    case.panes[0]["terminal_id"] = "replacement"
                elif kind == "new_worker":
                    fixtures.DisposalProcessTests.process(case, 301, 201, case.path, argv="worker\0")
                    change_stat(case.proc, 301, session=101)
                else:
                    (case.proc / "201" / "cmdline").write_bytes(b"/bin/sleep\0different\0")
                with self.assertRaises(TaskError):
                    cli.cleanup("DEV-7", force=True)
                case.assert_preserved()
                self.assertEqual(case.journal_path().read_bytes(), before)

    def test_fs_sync_failure_precedes_stop(self):
        with execution() as case, patch.object(DisposalStore, "write", side_effect=TaskError("fsync failed")):
            with self.assertRaisesRegex(TaskError, "fsync failed"):
                cli.cleanup("DEV-7", force=True)
            case.assert_preserved()
            self.assertFalse(any(c.args[0] == "close" for c in case.herdr_workspace.call_args_list))

    def test_ordinary_cleanup_does_not_admit_agents(self):
        with execution() as case:
            with self.assertRaisesRegex(TaskError, "completed"):
                cli.cleanup("DEV-7")
            case.provider.assert_not_called()
            case.assert_preserved()


class CleanupCapabilityTests(unittest.TestCase):
    def test_server_and_cli_close_capability_are_both_required(self):
        good = "status: running\nversion: 0.9.1\nprivate_protocol: 22\nendpoint_compatible: yes\nprivate_protocol_compatible: yes\n"
        for status, help_text, allowed in (
                (good, "Usage: herdr workspace close <workspace_id>", True),
                (good.replace("0.9.1", "0.9.0"), "Usage: herdr workspace close <workspace_id>", False),
                (good.replace("22", "21"), "Usage: herdr workspace close <workspace_id>", False),
                (good, "unknown operation", False)):
            with self.subTest(status=status, help=help_text), patch("task_start.workspace.run", side_effect=[status, help_text]):
                if allowed:
                    Herdr(Path("/tmp")).check_agent_retirement()
                else:
                    with self.assertRaises(TaskError):
                        Herdr(Path("/tmp")).check_agent_retirement()


class CleanupProviderTests(unittest.TestCase):
    def test_pi_requires_latest_terminal_message_and_immutable_linear_history(self):
        agent = PiAdapter(AgentOptions("pi"))
        with tempfile.TemporaryDirectory(prefix="awl-pi-cleanup-") as directory:
            path = Path(directory) / "session.jsonl"
            workspace = Workspace("dev-7-test", Path(directory), "w", "t", "p", "cleanup")
            reference = dict(agent="pi", kind="path", value=str(path), conversation_id="fixture")
            header = dict(type="session", id="fixture", cwd=directory)
            user = dict(type="message", id="1", parentId=None, message=dict(role="user", content="fixture"))
            assistant = dict(type="message", id="2", parentId="1",
                             message=dict(role="assistant", content="done", stopReason="stop"))
            for scenario in ("empty", "complete", "pending", "tool", "branch", "replaced"):
                with self.subTest(scenario=scenario):
                    entries = [header] if scenario == "empty" else [header, user, assistant]
                    if scenario == "pending":
                        entries.append(dict(user, id="3", parentId="2"))
                    elif scenario == "tool":
                        entries.append(dict(type="message", id="3", parentId="2", message=dict(role="toolResult")))
                    elif scenario == "branch":
                        entries.append(dict(assistant, id="3", parentId="1"))
                    elif scenario == "replaced":
                        entries[0] = dict(header, id="other")
                    path.write_text("".join(json.dumps(entry) + "\n" for entry in entries))
                    if scenario in {"empty", "complete"}:
                        agent.cleanup_provider_idle(workspace, reference)
                    else:
                        with self.assertRaises(TaskError):
                            agent.cleanup_provider_idle(workspace, reference)

    def test_codex_inactivity_requires_current_terminal_turn_and_empty_queue(self):
        workspace = Workspace("dev-7-test", Path("/tmp"), "w", "t", "p", "cleanup")
        reference = dict(agent="codex", kind="id", value=str(uuid4()))
        agent = CodexAdapter(AgentOptions("codex"))
        for scenario in ("complete", "empty", "interrupted", "failed", "working", "queue", "active",
                         "wrong_session", "wrong_path", "fork", "partial", "malformed"):
            with self.subTest(scenario=scenario), patch("task_start.agent.CodexRPC") as rpc:
                thread = dict(id=reference["value"], cwd="/tmp", status=dict(type="idle"))
                page = dict(data=[dict(status="completed", itemsView="full")], nextCursor=None)
                queue = dict(data=[], nextCursor=None)
                if scenario == "empty":
                    page["data"] = []
                elif scenario in {"interrupted", "failed", "working"}:
                    page["data"][0]["status"] = "inProgress" if scenario == "working" else scenario
                elif scenario == "queue":
                    queue["data"] = [dict(id="queued")]
                elif scenario == "active":
                    thread["status"] = dict(type="active")
                elif scenario == "wrong_session":
                    thread["id"] = str(uuid4())
                elif scenario == "wrong_path":
                    thread["cwd"] = "/"
                elif scenario == "fork":
                    thread["forkedFromId"] = str(uuid4())
                elif scenario == "partial":
                    page["data"][0]["itemsView"] = "summary"
                elif scenario == "malformed":
                    page = {}
                rpc.return_value.__enter__.return_value.request.side_effect = [dict(thread=thread), page, queue]
                if scenario in {"complete", "empty", "interrupted", "failed"}:
                    agent.cleanup_provider_idle(workspace, reference)
                else:
                    with self.assertRaises(TaskError):
                        agent.cleanup_provider_idle(workspace, reference)


@unittest.skipUnless(Path("/proc/self/stat").exists(), "Linux PTY/process evidence required")
class LivePTYTests(unittest.TestCase):
    def test_real_foreground_job_and_shell_stop_when_their_pty_is_retired(self):
        from task_start.integration_process import _process_visibility
        try:
            _process_visibility(Path("/proc"))
        except (OSError, ValueError) as error:
            self.skipTest(f"requires Herdr host's unfiltered proc mount: {error}")
        with tempfile.TemporaryDirectory(prefix="awl-owned-pty-") as root:
            child, terminal = pty.fork()
            if child == 0:
                os.chdir(root)
                os.execv("/bin/bash", ["bash", "--noprofile", "--norc", "-i"])
            try:
                os.write(terminal, b"/bin/sleep 120\n")
                deadline = time.monotonic() + 5
                while True:
                    fields = Path(f"/proc/{child}/stat").read_text().rsplit(")", 1)[1].split()
                    foreground = int(fields[5])
                    if foreground > 0 and foreground != child:
                        args = Path(f"/proc/{foreground}/cmdline").read_bytes()
                        if args == b"/bin/sleep\000120\0":
                            break
                    if time.monotonic() >= deadline:
                        self.fail("disposable foreground fixture did not become ready")
                    time.sleep(.02)
                panes = {"fixture": dict(cwd=Path(root), agent="sleep", info=dict(pane_id="fixture",
                    shell_pid=child, foreground_process_group_id=foreground,
                    foreground_processes=[dict(pid=foreground, argv=["/bin/sleep", "120"])]))}
                frozen = owned_pane_processes(panes)
                self.assertEqual(set(frozen["fixture"]["members"]), {str(child), str(foreground)})
                os.close(terminal)
                terminal = None
                deadline = time.monotonic() + 5
                while True:
                    try:
                        self.assertEqual(owned_pane_processes({}, expected=frozen, closing=True), {})
                        break
                    except TaskError:
                        if time.monotonic() >= deadline:
                            raise
                        time.sleep(.02)
            finally:
                if terminal is not None:
                    os.close(terminal)
                # This PID is our unreaped direct fixture child, never a runtime
                # PID read from a stale Herdr report.
                if os.waitpid(child, os.WNOHANG) == (0, 0):
                    os.kill(child, signal.SIGKILL)
                    os.waitpid(child, 0)
