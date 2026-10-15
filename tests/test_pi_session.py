from dataclasses import replace
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from task_start import TaskError
from task_start.agent import AgentExecution, AgentOptions, Pi
from task_start.contexts import ContextRegistry, context_observer, context_reference
from task_start.review import finalize_reviewer
from task_start.workspace import Workspace
from test_task_start import ISSUE


class PiSessionDiscoveryTests(unittest.TestCase):
    def test_review_launch_and_restart_explicitly_load_session_reporter(self):
        workspace = Workspace("dev-7-task", Path("/checkout"), "w1", "t1", "p1", "existing")
        options = AgentOptions("pi", "model", "high")
        adapter = Pi(options)
        execution = AgentExecution(ISSUE, Path("/repo"), workspace, options, "Review", purpose="review")
        agent = dict(agent="pi", agent_status="idle", workspace_id="w1", tab_id="t1", pane_id="p1",
                     terminal_id="terminal", cwd="/checkout", foreground_cwd="/checkout")
        # These session fixtures mock shell readiness alongside the Herdr launch.
        with patch.object(adapter, "validate_model_mode"), patch.object(adapter, "check_target"), \
                patch.object(adapter, "clear_shell_input") as clear_shell_input, \
                patch.object(adapter, "start_agent", return_value=agent) as start, \
                patch.object(adapter, "command", return_value=dict(agent=agent)) as command:
            result = adapter.launch(execution)
        clear_shell_input.assert_called_once_with(workspace)
        start.assert_called_once()
        self.assertEqual([c.args for c in command.call_args_list if c.args[1] == "prompt"],
                         [("agent", "prompt", "p1", "Review")])
        args = start.call_args.args[1]
        extension = Path(args[args.index("--extension") + 1])
        self.assertEqual(extension, Path(__file__).resolve().parents[1] / "task_start/pi_session.mjs")
        self.assertTrue(extension.is_file())
        self.assertIn(str(extension), adapter.resume_args(execution, dict(value="/persisted/pi.jsonl")))
        self.assertNotIn("--extension", adapter.launch_args())  # Headless capability check has no hook.
        self.assertIsNone(result.session_id)  # An unreported identity is never fabricated.

    @unittest.skipUnless(shutil.which("node"), "Node required to exercise the Pi extension transport")
    def test_real_extension_emits_missing_herdr_metadata_and_registry_retains_verified_session(self):
        with tempfile.TemporaryDirectory(prefix="pi-capture-") as directory:
            root = Path(__file__).resolve().parents[1]
            probe = subprocess.run([shutil.which("node"), str(root / "tests/pi_session_probe.mjs"),
                str(root / "task_start/pi_session.mjs"), directory], capture_output=True, text=True, timeout=15)
            self.assertEqual(probe.returncode, 0, probe.stderr)
            data = json.loads(probe.stdout)
            self.assertFalse(data["existedAtStartup"])
            self.assertEqual(data["events"], ["session_start", "agent_start", "agent_settled", "session_shutdown"])
            reports = data["retainedReports"]
            self.assertEqual(len(reports), 4)
            for report in reports:
                self.assertEqual(report["method"], "pane.report_agent_session")
                self.assertEqual(report["params"]["agent_session_path"], data["sessionFile"])
                self.assertEqual(report["params"]["pane_id"], "review-pane")
                self.assertNotIn("state", report["params"])
            self.assertEqual(sorted(r["params"]["seq"] for r in reports), [r["params"]["seq"] for r in reports])

            workspace = Workspace("dev-7-task", Path(data["cwd"]), "w1", "t1", "review-pane", "existing")
            options = AgentOptions("pi", "model", "high")
            adapter = Pi(options)
            registry = ContextRegistry(Path(directory) / "contexts.sqlite3")
            # Registry claims inspect Git ownership evidence before admitting a
            # runtime binding, so the probe's checkout must be a real repository.
            subprocess.run(["git", "-C", data["cwd"], "init", "--quiet", "--template=", "--initial-branch=main"],
                           check=True, capture_output=True)
            context_id = registry.allocate("DEV-7", "review", agent="pi", model="model", mode="high",
                repository=data["cwd"], worktree=data["cwd"], endpoint="/server.sock", workspace_id="w1",
                tab_id="t1", pane_id="review-pane", terminal_id="terminal")
            execution = AgentExecution(ISSUE, workspace.path, workspace, options, "Review", purpose="review",
                                       runtime_observer=context_observer(registry, context_id))
            # Exact shape observed in DEV-20-R6: process detection is idle but has
            # NO agent_session field. Only the extension's IPC report supplies it.
            agent = dict(agent="pi", agent_status="idle", workspace_id="w1", tab_id="t1", pane_id="review-pane",
                terminal_id="terminal", cwd=data["cwd"], foreground_cwd=data["cwd"], label=context_id)
            observed = adapter.observe_agent(execution, agent)
            self.assertIsNone(registry.get(context_id)["session_id"])
            params = reports[0]["params"]
            agent["agent_session"] = dict(agent=params["agent"], kind="path", value=params["agent_session_path"], source=params["source"])
            path = Path(data["sessionFile"])
            history = path.read_bytes()
            path.unlink()  # Startup path allocated in memory, not yet persisted.
            observed = adapter.observe_agent(execution, agent, observed)
            row = registry.get(context_id)
            self.assertEqual((row["session_id"], row["resumability"]), (str(path), "unknown"))
            with self.assertRaises(TaskError):
                adapter.verify_review_session(workspace, context_reference(row))
            path.write_bytes(history)  # Pi flushes on its first assistant response.

            identities = MagicMock()
            identities.endpoint.return_value = "/server.sock"
            identities.snapshot.return_value = [agent]
            finalize_reviewer(context_id, workspace, adapter, registry, identities)
            row = registry.get(context_id)
            reference = context_reference(row)
            self.assertEqual(reference["conversation_id"], data["sessionId"])
            self.assertEqual((row["state"], row["resumability"]), ("active", "yes"))
            identities.snapshot.return_value = [dict(agent, agent=None, agent_session=None)]
            finalize_reviewer(context_id, workspace, adapter, registry, identities)
            self.assertEqual(context_reference(registry.get(context_id)), reference)
            self.assertEqual(registry.get(context_id)["resumability"], "yes")

            resumed = replace(execution, handoff="Focused follow-up", runtime_observer=context_observer(registry, context_id))
            with patch.object(adapter, "check_target"), \
                    patch.object(adapter, "clear_shell_input") as clear_shell_input, \
                    patch.object(adapter, "start_agent", return_value=agent) as start, \
                    patch.object(adapter, "model_mode_capabilities", return_value=("model", "high", ("high",))), \
                    patch.object(adapter, "command", return_value=dict(agent=agent)) as command:
                result = adapter.resume_review(resumed, reference, recreate=True)
            clear_shell_input.assert_called_once_with(workspace)
            start.assert_called_once_with(workspace, ["--model", "model", "--thinking", "high", "--extension",
                str(root / "task_start/pi_session.mjs"), "--session", str(path)], review=True)
            self.assertEqual((result.session_id, result.session_kind, result.resumability), (str(path), "path", "yes"))
            self.assertEqual([c.args for c in command.call_args_list if c.args[1] == "prompt"],
                             [("agent", "prompt", "review-pane", "Focused follow-up")])
            changed = data["switchedReport"]["params"]
            changed_agent = dict(agent, agent_session=dict(agent="pi", kind="path", value=changed["agent_session_path"]))
            with self.assertRaisesRegex(TaskError, "session changed"):
                adapter.observe_agent(resumed, changed_agent, observed)
            self.assertEqual(context_reference(registry.get(context_id)), reference)
