import copy
from dataclasses import replace
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from task_start import TaskError, cli
from task_start.agent import (AgentExecution, AgentOptions, AgentOverrides, Codex,
                              LaunchResult, Pi, adapter_for, codex_repository_policy,
                              resolve_agent_options)
from task_start.config import (AgentConfig, agent_config, codex_repository_profiles,
                               load_local)
from task_start.contexts import ContextRegistry, HerdrContexts, launch_registered
from task_start.handoff import IMPLEMENTATION_INSTRUCTIONS, implementation_handoff
from task_start.linear import Linear
from task_start.workspace import Workspace, slice_slug
import test_task_start as baseline
from test_task_start import ISSUE, LOCAL, issue_data


class ConfigurationAndInputTests(unittest.TestCase):
    def test_arbitrary_models_and_legacy_reasoning(self):
        for model in ["gpt-6-astra", "gpt-6-sol", "custom/model:v2"]:
            for reasoning in ["none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"]:
                self.assertEqual(agent_config(dict(kind="codex", model=model, reasoning=reasoning)),
                                 AgentConfig("codex", model, reasoning))

    def test_invalid_agent_config(self):
        for data in [[], "codex", {}, dict(kind="Codex", model="a", mode="high"),
                     dict(kind="codex", model=3, mode="high"),
                     dict(kind="codex", model="-config", reasoning="high"),
                     dict(kind="codex", model="a\nb", reasoning="high"),
                     dict(kind="codex", mode="high", reasoning="high")]:
            with self.subTest(data=data), self.assertRaises(TaskError):
                agent_config(data)

    def test_mode_field_and_optional_agent_defaults(self):
        self.assertEqual(agent_config(dict(kind="pi", model="anthropic/sonnet", mode="high")),
                         AgentConfig("pi", "anthropic/sonnet", "high"))
        self.assertEqual(agent_config(dict(kind="pi")), AgentConfig("pi"))

    def test_independent_cli_over_config_precedence(self):
        configured = AgentConfig("codex", "configured-model", "medium")
        cases = [
            (AgentOverrides(), AgentOptions("codex", "configured-model", "medium")),
            (AgentOverrides(kind="pi"), AgentOptions("pi", "configured-model", "medium")),
            (AgentOverrides(model="run-model"), AgentOptions("codex", "run-model", "medium")),
            (AgentOverrides(mode="high"), AgentOptions("codex", "configured-model", "high")),
            (AgentOverrides("pi", "run-model", "low"), AgentOptions("pi", "run-model", "low")),
        ]
        for overrides, expected in cases:
            with self.subTest(overrides=overrides):
                self.assertEqual(resolve_agent_options(configured, overrides), expected)
        self.assertEqual(
            resolve_agent_options(AgentConfig("pi", "pi-model", "low"),
                                  AgentOverrides(kind="codex")),
            AgentOptions("codex", "pi-model", "low"),
        )
        self.assertEqual(configured, AgentConfig("codex", "configured-model", "medium"))
        with self.assertRaisesRegex(TaskError, r"\[agent\].*--agent"):
            resolve_agent_options(None, AgentOverrides(model="model-only"))

    def test_adapter_selection_and_unknown_agent(self):
        self.assertIsInstance(adapter_for(AgentOptions("codex")), Codex)
        self.assertIsInstance(adapter_for(AgentOptions("pi")), Pi)
        with self.assertRaisesRegex(TaskError, "Unsupported agent.*codex, pi"):
            adapter_for(AgentOptions("unknown"))

    def test_load_agent_and_skip_invalid_config_for_no_agent(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            base = f'projects_root = {json.dumps(directory)}\n[linear]\napi_key = "placeholder"\n'
            path.write_text(base + '[agent]\nkind = "codex"\nmodel = "gpt-6-sol"\nreasoning = "max"\n')
            self.assertEqual(load_local(path).agent, AgentConfig("codex", "gpt-6-sol", "max"))
            path.write_text(base + '[agent]\nkind = "other"\n')
            self.assertIsNone(load_local(path, no_agent=True).agent)

    def test_codex_repository_profiles_are_explicit_and_exact(self):
        data = {"repositories": {
            "agentic-workflows-prototype": {"profile": "agentic-workflows-lite-trusted"},
            "other.repo": {"profile": "other_profile"},
        }}
        expected = {"agentic-workflows-prototype": "agentic-workflows-lite-trusted",
                    "other.repo": "other_profile"}
        self.assertEqual(codex_repository_profiles(data), expected)
        self.assertEqual(codex_repository_policy(expected, "agentic-workflows-prototype"),
                         {"codex_profile": "agentic-workflows-lite-trusted"})
        for repository in ["unlisted", "Agentic-workflows-prototype",
                           "/projects/agentic-workflows-prototype", "agentic-workflows-prototype-extra"]:
            with self.subTest(repository=repository):
                self.assertEqual(codex_repository_policy(expected, repository), {})

    def test_invalid_codex_repository_profiles_fail_closed(self):
        cases = [
            [],
            {"unknown": {}},
            {"repositories": []},
            {"repositories": {"../escape": {"profile": "safe"}}},
            {"repositories": {"repo": "profile"}},
            {"repositories": {"repo": {}}},
            {"repositories": {"repo": {"profile": 3}}},
            {"repositories": {"repo": {"profile": "../escape"}}},
            {"repositories": {"repo": {"profile": "-option"}}},
            {"repositories": {"repo": {"profile": "safe", "sandbox": "danger-full-access"}}},
        ]
        for data in cases:
            with self.subTest(data=data), self.assertRaises(TaskError):
                codex_repository_profiles(data)

    def test_local_config_loads_profile_but_no_agent_skips_launch_configuration(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            base = (f'projects_root = {json.dumps(directory)}\n[linear]\n'
                    'api_key = "placeholder"\n[agent]\nkind = "codex"\n')
            path.write_text(base + '[codex.repositories."agentic-workflows-prototype"]\n'
                            'profile = "agentic-workflows-lite-trusted"\n')
            self.assertEqual(load_local(path).codex_repository_profiles,
                             {"agentic-workflows-prototype": "agentic-workflows-lite-trusted"})
            path.write_text(base + '[codex.repositories.repo]\nprofile = "../invalid"\n')
            self.assertEqual(load_local(path, no_agent=True).codex_repository_profiles, {})

    def test_slice_normalization_and_flags(self):
        for value in ["codex-handoff", "Codex Handoff", "  Codéx__Handoff  "]:
            self.assertEqual(slice_slug(value), "codex-handoff")
        args = cli.parser().parse_args(["start", "DEV-13", "--slice", "Codex Handoff", "--no-agent"])
        self.assertEqual(args.slice, "codex-handoff")
        self.assertTrue(args.no_agent)
        args = cli.parser().parse_args(["start", "DEV-13", "--agent", "pi",
                                        "--model", "anthropic/sonnet", "--mode", "high"])
        self.assertEqual((args.agent_kind, args.model, args.mode),
                         ("pi", "anthropic/sonnet", "high"))
        for value in ["", " ", "---", "../escape", "foo/bar", "a.lock", "a\nb", "a;ls", "a" * 101, "字"]:
            with self.subTest(value=value), patch("sys.stderr", new=io.StringIO()), self.assertRaises(SystemExit):
                cli.parser().parse_args(["start", "DEV-13", "--slice", value])


class PromptTests(unittest.TestCase):
    def test_linear_description_is_exact_and_nullable(self):
        for description in [None, "", "\n  Current **task**\r\n`$(echo bad)`\nΔ\n"]:
            data = issue_data()
            data["issue"]["description"] = description
            with patch.object(Linear, "request", return_value=data):
                issue = Linear("placeholder").get_issue("DEV-7")
            self.assertEqual(issue.description, description or "")
        for description in [42, [], {}]:
            data["issue"]["description"] = description
            with patch.object(Linear, "request", return_value=data), self.assertRaises(TaskError):
                Linear("placeholder").get_issue("DEV-7")
        del data["issue"]["description"]
        with patch.object(Linear, "request", return_value=data), self.assertRaises(TaskError):
            Linear("placeholder").get_issue("DEV-7")

    def test_fresh_snapshot_and_instructions(self):
        workspace = Workspace("dev-7-original-title", Path("/exact/checkout"), "w2", "tab", "pane", "reopened")
        issue = replace(ISSUE, title="Renamed issue", description="\nExact  whitespace\r\n**Unicode α**\n")
        prompt = implementation_handoff(issue, workspace)
        self.assertIn(f"Implement Linear issue {issue.identifier}.", prompt)
        self.assertIn(f"Title:\n{issue.title}\n", prompt)
        self.assertIn(f"Task:\n{issue.description}\n\nWorkflow-owned implementation instructions:", prompt)
        self.assertIn(IMPLEMENTATION_INSTRUCTIONS, prompt)
        self.assertIn(str(workspace.path), prompt)
        self.assertIn(workspace.branch, prompt)
        self.assertNotIn(LOCAL.api_key, prompt)
        self.assertIn("Slice: handoff", implementation_handoff(
            issue, replace(workspace, slice="handoff")))
        with self.assertRaisesRegex(TaskError, "NUL"):
            implementation_handoff(replace(issue, description="\0"), workspace)

    def test_shared_execution_preserves_non_implementation_handoff(self):
        workspace = Workspace("dev-7-review", Path("/exact/checkout"),
                              "w2", "w2:t1", "w2:p1", "reopened")
        handoff = "Review DEV-7 in the prepared checkout. Report findings only."
        execution = AgentExecution(ISSUE, Path("/resolved/repository"), workspace,
                                   AgentOptions("pi"), handoff, purpose="review")
        self.assertEqual(execution.handoff, handoff)
        self.assertNotIn("Implement Linear issue", execution.handoff)
        self.assertNotIn("Implement only this slice", execution.handoff)


class AgentTests(unittest.TestCase):
    def setUp(self):
        self.message_id = "b67e58a0-3876-4e85-ab88-bc170e847327"
        self.bootstrap = (f"Handoff readiness {self.message_id}. Do not use tools or modify files. "
                          "Reply READY, then wait for the task prompt.")
        self.workspace = Workspace("dev-7-old", Path("/exact/checkout"), "w6", "w6:t8", "w6:p20", "ready")
        self.issue = ISSUE
        self.codex = Codex(AgentOptions("codex", "custom/model", "high"))
        self.filter_credentials = self.enterContext(patch.object(self.codex, "filter_shell_credentials"))
        clear_input_patcher = patch.object(self.codex, "clear_shell_input")
        self.clear_shell_input = clear_input_patcher.start()
        self.addCleanup(clear_input_patcher.stop)
        self.rpc = MagicMock()
        for target, value in [("CodexRPC", None), ("uuid4", self.message_id)]:
            patcher = patch("task_start.agent." + target, return_value=value)
            mocked = patcher.start()
            self.addCleanup(patcher.stop)
            if target == "CodexRPC":
                self.factory = mocked
                self.factory.return_value = MagicMock()
                self.factory.return_value.__enter__.return_value = self.rpc
        def advance(seconds):
            self.now += seconds
        for target, effect in [("monotonic", lambda: self.now), ("sleep", advance)]:
            patcher = patch("task_start.agent.time." + target, side_effect=effect)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.reset_fixture()

    def reset_fixture(self):
        self.now = 0
        self.queued = False
        self.prompt = implementation_handoff(self.issue, self.workspace)
        self.thread_id = "01a0d314-bd68-7203-8b68-f2520f892afa"
        self.turn_id = "01a0d315-ded5-7051-ac63-e46e9aa818fe"
        self.thread = dict(id=self.thread_id, cwd=str(self.workspace.path), preview=self.bootstrap)
        self.sessions = [self.thread]
        self.bootstrap_item = dict(turnId="readiness-turn", item=dict(type="userMessage", clientId="native",
                                    content=[dict(type="text", text=self.bootstrap)]))
        self.receipt = dict(turnId=self.turn_id, item=dict(type="userMessage", clientId=self.message_id,
                           content=[dict(type="text", text=self.prompt)]))
        self.task_items = [self.receipt]
        self.agent = dict(agent="codex", agent_status="idle", workspace_id="w6", tab_id="w6:t8",
                          pane_id="w6:p20", terminal_id="term_opaque", cwd="/exact/checkout", foreground_cwd="/exact/checkout")
        self.args = ["codex", "--cd", "/exact/checkout", "--model", "custom/model", "--config",
                     'model_reasoning_effort="high"', "--", self.bootstrap]
        self.results = [dict(type="pane_list", panes=[dict(self.agent, agent=None)]),
                        dict(type="agent_started", agent=self.agent, argv=self.args),
                        dict(type="agent_info", agent=dict(self.agent)),
                        dict(type="agent_info", agent=dict(self.agent))]
        self.rpc.reset_mock()
        self.rpc.request.side_effect = self.request

    def request(self, method, params, **kwargs):
        if method == "thread/list":
            return dict(data=self.sessions, nextCursor=None)
        if method == "thread/read":
            return dict(thread=self.thread)
        if method == "thread/items/list":
            return dict(data=[self.bootstrap_item] + (self.task_items if self.queued else []), nextCursor=None)
        if method == "thread/queue/add":
            self.queued = True
            return dict(queuedSubmission=dict(id="queue-id", clientUserMessageId=params["clientUserMessageId"], input=params["input"]))
        self.fail("Unexpected session operation " + method)

    def run_launch(self, *, policy=None, purpose="implementation", handoff=None, runtime_observer=None):
        prompt = self.prompt if handoff is None else handoff
        execution = AgentExecution(self.issue, Path("/resolved/repository"), self.workspace,
                                   self.codex.options, prompt, purpose=purpose,
                                   policy=policy or {}, runtime_observer=runtime_observer)
        return self.codex.launch(execution)

    def test_exact_workspace_model_reasoning_and_confirmed_task(self):
        with patch("task_start.agent.run", side_effect=[json.dumps(dict(result=r)) for r in self.results]) as run:
            result = self.run_launch()
        self.assertEqual(result.turn_id, self.turn_id)
        self.assertEqual(result.session_id, self.thread_id)
        self.clear_shell_input.assert_called_once_with(self.workspace)
        self.filter_credentials.assert_called_once_with(self.workspace)
        calls = [c.args[0] for c in run.call_args_list]
        self.assertEqual(calls[1][4:], ["--kind", "codex", "--pane", "w6:p20", "--timeout", "30000", "--", *self.args[1:]])
        self.assertEqual(calls[2:], [["herdr", "agent", "get", "w6:p20"]] * 2)
        self.factory.assert_called_once_with(self.workspace.path)
        self.rpc.request.assert_any_call("thread/queue/add", dict(threadId=self.thread_id,
            clientUserMessageId=self.message_id, input=[dict(type="text", text=self.prompt, text_elements=[])]))
        self.assertFalse(any(c.args[0] in {"thread/start", "thread/resume", "turn/start"} for c in self.rpc.request.call_args_list))

    def test_stale_pi_fragment_is_cleared_before_codex_launch(self):
        process = dict(type="pane_process_info", process_info=dict(pane_id="w6:p20",
                       shell_pid=123, foreground_process_group_id=123,
                       foreground_processes=[dict(pid=123, name="bash", argv=["/bin/bash"])]))
        shell_input = "pi"
        canceled_inputs = []
        launch_argvs = []

        def response(result):
            return json.dumps(dict(result=result))

        def herdr(args, *, timeout=120):
            nonlocal shell_input
            if args[:3] == ["herdr", "pane", "list"]:
                return response(self.results[0])
            if args[:3] == ["herdr", "pane", "process-info"]:
                return response(process)
            if args[:3] == ["herdr", "pane", "send-keys"]:
                self.assertEqual(args[3:], ["w6:p20", "ctrl+c"])
                canceled_inputs.append(shell_input)
                shell_input = ""
                return ""
            if args[:3] == ["herdr", "agent", "start"]:
                separator = args.index("--")
                executable = shell_input + args[args.index("--kind") + 1]
                shell_input = ""
                argv = [executable, *args[separator + 1:]]
                launch_argvs.append(argv)
                if executable != "codex":
                    raise TaskError(f"{executable}: command not found")
                return response(dict(self.results[1], argv=argv))
            if args[:3] == ["herdr", "agent", "get"]:
                return response(self.results[2])
            self.fail(f"Unexpected Herdr call: {args}")

        with patch("task_start.agent.run", side_effect=herdr):
            # Reproduce the regression: without the preflight/Ctrl+C, Herdr
            # appends canonical `codex` to the shell's unsubmitted `pi`.
            with self.assertRaisesRegex(TaskError, "picodex"):
                self.run_launch()
            self.assertEqual(launch_argvs[0], ["picodex", *self.args[1:]])
            self.assertEqual(canceled_inputs, [])
            self.assertFalse(self.queued)

            # Seed the same terminal state and exercise the production preflight.
            self.reset_fixture()
            shell_input = "pi"
            self.clear_shell_input.reset_mock()
            self.clear_shell_input.side_effect = lambda workspace: type(self.codex).clear_shell_input(
                self.codex, workspace)
            result = self.run_launch()

        self.assertEqual(result.turn_id, self.turn_id)
        self.assertEqual(canceled_inputs, ["pi"])
        self.assertEqual(launch_argvs[1], self.args)
        self.assertNotIn("picodex", launch_argvs[1])

    def test_trusted_repository_profile_is_the_only_permission_launch_override(self):
        policy = {"codex_profile": "agentic-workflows-lite-trusted"}
        self.results[1]["argv"] = ["codex", "--cd", "/exact/checkout", "--no-daemon", "--profile",
                                    "agentic-workflows-lite-trusted", "--model", "custom/model",
                                    "--config", 'model_reasoning_effort="high"', "--", self.bootstrap]
        with patch.object(self.codex, "command", side_effect=self.results) as command:
            self.run_launch(policy=policy)
        launch = command.call_args_list[1].args
        self.assertEqual(launch[-len(self.results[1]["argv"]) + 1:],
                         tuple(self.results[1]["argv"][1:]))
        self.assertNotIn("--sandbox", launch)
        self.assertNotIn("--ask-for-approval", launch)
        self.assertEqual(launch.count("--no-daemon"), 1)
        self.filter_credentials.assert_called_once_with(self.workspace)

    def test_implementation_and_review_share_repository_profile_not_handoff(self):
        implementation = AgentExecution(self.issue, Path("/resolved/repository"), self.workspace,
                                        self.codex.options, self.prompt)
        review = replace(implementation, purpose="review",
                         handoff="Read-only review: do not edit or stage files. Report findings only.")
        for profile in [None, "agentic-workflows-lite-trusted", "other_profile"]:
            for execution in [implementation, review]:
                with self.subTest(profile=profile, purpose=execution.purpose):
                    policy = {} if profile is None else {"codex_profile": profile}
                    execution = replace(execution, policy=policy)
                    expected = ["--cd", "/exact/checkout"]
                    if profile is not None:
                        expected += ["--no-daemon", "--profile", profile]
                    expected += ["--model", "custom/model", "--config", 'model_reasoning_effort="high"']
                    launch = self.codex.launch_args(execution, self.bootstrap)
                    resume = self.codex.resume_args(execution, {"kind": "id", "value": self.thread_id})
                    self.assertEqual(launch, [*expected, "--", self.bootstrap])
                    self.assertEqual(resume, ["resume", *expected, "--", self.thread_id])
                    self.assertEqual(launch.count("--no-daemon"), int(profile is not None))
                    self.assertEqual(resume.count("--no-daemon"), int(profile is not None))
                    self.assertNotIn(execution.handoff, launch)
                    self.assertNotIn(execution.handoff, resume)
                    self.assertNotIn(self.bootstrap, resume)
        self.assertNotEqual(implementation.handoff, review.handoff)

    def test_invalid_profile_policy_cannot_become_cli_syntax(self):
        for profile in ["../escape", "-option", "contains space", 3]:
            with self.subTest(profile=profile), self.assertRaisesRegex(TaskError, "portable name"):
                self.run_launch(policy={"codex_profile": profile})
        self.factory.assert_not_called()

    def test_no_task_goes_through_terminal_arguments(self):
        with patch.object(self.codex, "command", side_effect=self.results) as command:
            self.run_launch()
        self.assertFalse(any(self.prompt in c.args or c.args[1] == "prompt" for c in command.call_args_list))

    def test_startup_failure_does_not_queue_or_retry(self):
        with patch.object(self.codex, "command", side_effect=[self.results[0], TaskError("failed")]) as command, self.assertRaisesRegex(TaskError, "startup"):
            self.run_launch()
        self.assertEqual(command.call_count, 2)
        self.assertFalse(self.queued)

    def test_existing_codex_in_any_pane_refuses_duplicate(self):
        self.results[0]["panes"].append(dict(self.agent, pane_id="w6:p99"))
        with patch.object(self.codex, "command", side_effect=self.results) as command, self.assertRaisesRegex(TaskError, "already occupies.*w6:p99"):
            self.run_launch()
        command.assert_called_once_with("pane", "list", "--workspace", "w6")
        self.clear_shell_input.assert_not_called()
        self.factory.assert_not_called()

    def test_mismatched_start_response_prevents_queue(self):
        for field, value in [("workspace_id", "other"), ("pane_id", "other"), ("tab_id", "other"),
                             ("cwd", "/wrong"), ("foreground_cwd", "/wrong"), ("agent", "claude"), ("agent_status", "blocked")]:
            with self.subTest(field=field):
                self.reset_fixture()
                self.results[1]["agent"][field] = value
                with patch.object(self.codex, "command", side_effect=self.results), self.assertRaisesRegex(TaskError, "startup"):
                    self.run_launch()
                self.assertFalse(self.queued)

    def test_argv_mismatch_fails(self):
        self.results[1]["argv"] = ["codex"]
        with patch.object(self.codex, "command", side_effect=self.results), self.assertRaises(TaskError):
            self.run_launch()

    def test_working_without_task_receipt_fails_and_retry_cannot_duplicate(self):
        self.results[1]["agent"]["agent_status"] = "working"
        self.task_items = []
        with patch.object(self.codex, "command", side_effect=self.results) as command, self.assertRaisesRegex(TaskError, "did not confirm the exact task"):
            self.run_launch()
        self.assertEqual(command.call_count, 3)
        self.assertEqual(sum(c.args[0] == "thread/queue/add" for c in self.rpc.request.call_args_list), 1)
        with patch.object(self.codex, "command", return_value={"panes": [self.agent]}), self.assertRaisesRegex(TaskError, "already occupies"):
            self.run_launch()
        self.factory.assert_called_once()

    def test_first_use_trust_idle_is_not_readiness(self):
        self.results[1]["agent"].update(interactive_ready=True, agent_status="idle")
        self.sessions = []
        with patch.object(self.codex, "command", side_effect=self.results), self.assertRaisesRegex(TaskError, "trust"):
            self.run_launch()
        self.assertFalse(self.queued)

    def test_receipt_polling_never_repeats_queue_or_launch(self):
        pending = 2
        def request(method, params, **kwargs):
            nonlocal pending
            if self.queued and method == "thread/items/list" and pending:
                pending -= 1
                return dict(data=[self.bootstrap_item], nextCursor=None)
            return self.request(method, params, **kwargs)
        self.rpc.request.side_effect = request
        with patch.object(self.codex, "command", side_effect=self.results) as command:
            self.run_launch()
        self.assertEqual(self.now, 0.5)
        self.assertEqual(sum(c.args[1] == "start" for c in command.call_args_list), 1)
        self.assertEqual(sum(c.args[0] == "thread/queue/add" for c in self.rpc.request.call_args_list), 1)

    def test_quickly_completed_turn_is_confirmed(self):
        with patch.object(self.codex, "command", side_effect=self.results):
            self.assertIn("confirmed", self.run_launch().summary)

    def test_different_input_fails(self):
        self.receipt["item"]["content"][0]["text"] = self.prompt[:-1]
        with patch.object(self.codex, "command", side_effect=self.results), self.assertRaisesRegex(TaskError, "prompt delivery/start"):
            self.run_launch()

    def test_turn_identifier_is_required(self):
        self.receipt["turnId"] = ""
        with patch.object(self.codex, "command", side_effect=self.results), self.assertRaises(TaskError):
            self.run_launch()

    def test_other_message_id_does_not_confirm_task(self):
        self.receipt["item"]["clientId"] = "other-message"
        with patch.object(self.codex, "command", side_effect=self.results), self.assertRaises(TaskError):
            self.run_launch()

    def test_ambiguous_session_identity_fails(self):
        self.sessions.append(dict(self.thread, id="01a0d314-bd68-7203-8b68-f2520f892afb"))
        with patch.object(self.codex, "command", side_effect=self.results), self.assertRaises(TaskError):
            self.run_launch()
        self.assertFalse(self.queued)

    def test_unrelated_recent_session_is_never_selected(self):
        self.sessions = [dict(self.thread, preview="unrelated task")]
        with patch.object(self.codex, "command", side_effect=self.results), self.assertRaises(TaskError):
            self.run_launch()
        self.assertFalse(self.queued)

    def test_invalid_session_identity_fails(self):
        for field, value in [("id", "--last"), ("cwd", "/wrong")]:
            with self.subTest(field=field):
                self.reset_fixture()
                self.thread[field] = value
                with patch.object(self.codex, "command", side_effect=self.results), self.assertRaises(TaskError):
                    self.run_launch()
                self.assertFalse(self.queued)

    def test_queue_failure_is_clear(self):
        def request(method, params, **kwargs):
            if method == "thread/queue/add":
                raise TaskError("queue rejected")
            return self.request(method, params, **kwargs)
        self.rpc.request.side_effect = request
        with patch.object(self.codex, "command", side_effect=self.results), self.assertRaisesRegex(TaskError, "prompt queue"):
            self.run_launch()

    def test_queue_acknowledgement_must_match_input(self):
        def request(method, params, **kwargs):
            result = self.request(method, params, **kwargs)
            if method == "thread/queue/add":
                result["queuedSubmission"]["input"] = []
            return result
        self.rpc.request.side_effect = request
        with patch.object(self.codex, "command", side_effect=self.results), self.assertRaisesRegex(TaskError, "prompt queue"):
            self.run_launch()

    def test_session_api_failure_is_not_assumed_success(self):
        self.rpc.request.side_effect = TaskError("unsupported session API")
        with patch.object(self.codex, "command", side_effect=self.results), self.assertRaisesRegex(TaskError, "session API"):
            self.run_launch()

    def test_replaced_terminal_or_agent_session_fails(self):
        for field, value in [("terminal_id", "replacement"), ("pane_id", "other"), ("agent_session", "replacement")]:
            for index in [2, 3]:
                with self.subTest(field=field, index=index):
                    self.reset_fixture()
                    if field == "agent_session":
                        for result in self.results[1:]:
                            result["agent"][field] = self.thread_id
                    self.results[index]["agent"][field] = value
                    with patch.object(self.codex, "command", side_effect=self.results), self.assertRaises(TaskError):
                        self.run_launch()
                    if index == 2:
                        self.assertFalse(self.queued)

    def test_codex_late_session_metadata_and_source_changes_are_observed(self):
        observer = MagicMock()
        for index, result in enumerate(self.results[2:], 2):
            result["agent"]["agent_session"] = dict(agent="codex", kind="id",
                value=self.thread_id, source=f"source-{index}")
        with patch.object(self.codex, "command", side_effect=self.results):
            result = self.run_launch(runtime_observer=observer)
        self.assertEqual(result.session_id, self.thread_id)
        reports = [json.loads(call.args[0]["herdr_session"]) for call in observer.call_args_list
                   if "herdr_session" in call.args[0]]
        self.assertEqual([r["source"] for r in reports], ["source-2", "source-3"])

    def test_codex_late_reference_must_match_discovered_provider_session(self):
        for index in (2, 3):
            with self.subTest(index=index):
                self.reset_fixture()
                for result in self.results[2:]:
                    result["agent"]["agent_session"] = dict(agent="codex", kind="id", value=self.thread_id)
                self.results[index]["agent"]["agent_session"]["value"] = "replacement-session"
                with patch.object(self.codex, "command", side_effect=self.results):
                    with self.assertRaisesRegex(TaskError, "session.*handoff"):
                        self.run_launch()
                if index == 2:
                    self.assertFalse(self.queued)

    def test_codex_reference_alone_does_not_establish_resumability(self):
        observer = MagicMock()
        with patch.object(self.codex, "command", side_effect=self.results), \
                patch.object(self.codex, "confirm_prompt", side_effect=TaskError("history unavailable")):
            with self.assertRaisesRegex(TaskError, "history unavailable"):
                self.run_launch(runtime_observer=observer)
        self.assertTrue(any(call.args[0].get("session_id") == self.thread_id for call in observer.call_args_list))
        self.assertFalse(any(call.args[0].get("resumability") == "yes" for call in observer.call_args_list))

    def test_exact_linear_context_and_resolved_slice_reach_queue(self):
        self.workspace = replace(self.workspace, slice="importer")
        self.issue = replace(ISSUE, title="Current title", description="\nExact α\r\n  task\n")
        self.prompt = implementation_handoff(self.issue, self.workspace)
        self.receipt["item"]["content"][0]["text"] = self.prompt
        with patch.object(self.codex, "command", side_effect=self.results):
            self.run_launch()
        queued = [c.args[1]["input"][0]["text"] for c in self.rpc.request.call_args_list if c.args[0] == "thread/queue/add"]
        self.assertEqual(queued, [self.prompt])
        self.assertIn(f"Task:\n{self.issue.description}\n\nWorkflow-owned implementation instructions:", queued[0])
        self.assertIn("Slice: importer\nImplement only this slice", queued[0])

    def test_malformed_responses_fail_cleanly(self):
        for result in ["bad", "[]", "null", '{"result": {"type": "other"}}',
                       '{"result": {"type": "pane_list", "panes": null}}']:
            with patch("task_start.agent.run", return_value=result), self.assertRaises(TaskError):
                self.run_launch()

    def test_unavailable_codex(self):
        with patch("task_start.agent.shutil.which", return_value=None), self.assertRaisesRegex(TaskError, "not installed"):
            self.codex.check_available()


class PiAdapterTests(unittest.TestCase):
    def setUp(self):
        self.workspace = Workspace("dev-7-old", Path("/exact/checkout"),
                                   "w6", "w6:t8", "w6:p20", "ready")
        self.options = AgentOptions("pi", "anthropic/claude-sonnet", "high")
        self.pi = Pi(self.options)
        self.filter_credentials = self.enterContext(patch.object(self.pi, "filter_shell_credentials"))
        self.clear_shell_input = self.enterContext(patch.object(self.pi, "clear_shell_input"))
        self.handoff = implementation_handoff(ISSUE, self.workspace)
        self.execution = AgentExecution(ISSUE, Path("/resolved/repository"),
                                        self.workspace, self.options, self.handoff)
        capability_patcher = patch.object(
            self.pi, "model_mode_capabilities",
            return_value=("anthropic/claude-sonnet", "high",
                          ("off", "minimal", "low", "medium", "high")),
        )
        self.capabilities = capability_patcher.start()
        self.addCleanup(capability_patcher.stop)
        self.agent = dict(agent="pi", agent_status="idle", workspace_id="w6",
                          tab_id="w6:t8", pane_id="w6:p20", terminal_id="term_pi",
                          agent_session="pi-session", cwd="/exact/checkout",
                          foreground_cwd="/exact/checkout")
        self.args = ["--model", "anthropic/claude-sonnet", "--thinking", "high"]
        self.results = [dict(type="pane_list", panes=[dict(self.agent, agent=None)]),
                        dict(type="agent_started", agent=dict(self.agent), argv=["pi", *self.args]),
                        dict(type="agent_info", agent=dict(self.agent)),
                        dict(type="agent_prompted", agent=dict(self.agent)),
                        dict(type="agent_info", agent=dict(self.agent))]

    def test_normal_prompt_submission_uses_exact_workspace_model_and_mode(self):
        with patch.object(self.pi, "command", side_effect=self.results) as command:
            result = self.pi.launch(self.execution)
        self.assertEqual(result, LaunchResult("pi", "w6:p20",
                         "Pi prompt submitted in w6:p20 (session pi-session)", "pi-session"))
        self.capabilities.assert_called_once_with(self.workspace)
        self.clear_shell_input.assert_called_once_with(self.workspace)
        self.filter_credentials.assert_called_once_with(self.workspace)
        start = command.call_args_list[1].args
        self.assertEqual(start[3:], ("--kind", "pi", "--pane", "w6:p20",
                                    "--timeout", "30000", "--", *self.args))
        self.assertNotIn(self.execution.handoff, start)
        self.assertNotIn("--cd", self.args)
        self.assertEqual(command.call_args_list[2].args, ("agent", "get", "w6:p20"))
        self.assertEqual(command.call_args_list[3].args,
                         ("agent", "prompt", "w6:p20", self.handoff))
        self.assertEqual(command.call_args_list[4].args, ("agent", "get", "w6:p20"))

    def test_initial_loop_launch_loads_session_reporter_without_changing_start(self):
        extension = str(Path(__file__).resolve().parents[1] / "task_start" / "pi_session.mjs")
        args = [*self.args, "--extension", extension]
        self.results[1]["argv"] = ["pi", *args]
        execution = replace(self.execution, policy=dict(session_reporting=True))
        with patch.object(self.pi, "command", side_effect=self.results) as command:
            self.pi.launch(execution)
        self.assertEqual(command.call_args_list[1].args[-len(args):], tuple(args))
        self.assertEqual(command.call_args_list[3].args, ("agent", "prompt", "w6:p20", self.handoff))

    def registered_pi_launch(self, results):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        self.registry = ContextRegistry(Path(directory) / "contexts.sqlite3")
        herdr = MagicMock(spec=HerdrContexts)
        herdr.endpoint.return_value = "/test.sock"
        herdr.snapshot.return_value = [dict(self.agent, agent=None, agent_session=None)]
        with patch.object(self.pi, "command", side_effect=results):
            return launch_registered(self.pi, self.execution, registry=self.registry, herdr=herdr)

    def test_structured_session_reference_does_not_prove_resumability(self):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        empty_session = Path(directory) / "session.jsonl"
        empty_session.touch()  # Even an existing path need not contain persisted history.
        for kind, value in [("id", "pi-session-id"), ("path", str(empty_session))]:
            results = copy.deepcopy(self.results)
            session = dict(agent="pi", kind=kind, value=value, source="hook")
            for result in results[1:]:
                result["agent"]["agent_session"] = session
            result = self.registered_pi_launch(results)
            self.assertEqual((result.session_id, result.session_kind, result.resumability), (value, kind, "unknown"))
            row = self.registry.list()[0]
            self.assertEqual((row["session_id"], row["session_kind"], row["resumability"]),
                             (value, kind, "unknown"))
            self.assertEqual(json.loads(row["herdr_session"]), session)

    def test_session_first_reported_after_startup_is_saved(self):
        # Cover each later response: pre-prompt get, prompt receipt, final get.
        for first_report in (2, 3, 4):
            with self.subTest(first_report=first_report):
                results = copy.deepcopy(self.results)
                session = dict(agent="pi", kind="id", value="session-A", source="hook")
                for index, result in enumerate(results[1:], 1):
                    result["agent"]["agent_session"] = session if index >= first_report else None
                result = self.registered_pi_launch(results)
                self.assertEqual(result.session_id, "session-A")
                row = self.registry.list()[0]
                self.assertEqual(row["session_id"], "session-A")
                self.assertEqual(json.loads(row["herdr_session"]), session)
                self.assertEqual(row["resumability"], "unknown")

    def test_late_discovery_is_persisted_before_later_handoff_failure(self):
        results = copy.deepcopy(self.results)
        results[1]["agent"]["agent_session"] = None
        session = dict(agent="pi", kind="path", value="/sessions/A.jsonl", source="hook")
        results[2]["agent"]["agent_session"] = session
        results[3] = TaskError("prompt transport failed")
        with self.assertRaisesRegex(TaskError, "prompt transport failed"):
            self.registered_pi_launch(results)
        row = self.registry.list()[0]
        self.assertEqual((row["state"], row["session_id"]), ("uncertain", "/sessions/A.jsonl"))
        self.assertEqual(json.loads(row["herdr_session"]), session)

    def test_late_session_A_then_B_aborts_without_rebinding(self):
        for first_report, replacement_at in ((2, 3), (2, 4), (3, 4)):
            with self.subTest(first_report=first_report, replacement_at=replacement_at):
                results = copy.deepcopy(self.results)
                results[1]["agent"]["agent_session"] = None
                for index, result in enumerate(results[2:], 2):
                    result["agent"]["agent_session"] = (dict(agent="pi", kind="id", source="hook",
                        value="session-A" if index < replacement_at else "session-B")
                        if index >= first_report else None)
                with self.assertRaisesRegex(TaskError, "session changed"):
                    self.registered_pi_launch(results)
                row = self.registry.list()[0]
                self.assertEqual((row["state"], row["session_id"]), ("uncertain", "session-A"))
                self.assertEqual(json.loads(row["herdr_session"])["value"], "session-A")

    def test_known_session_kind_is_not_forgotten_after_a_legacy_report(self):
        results = copy.deepcopy(self.results)
        results[1]["agent"]["agent_session"] = dict(agent="pi", kind="id", value="A", source="hook")
        results[2]["agent"]["agent_session"] = "A"
        results[3]["agent"]["agent_session"] = dict(agent="pi", kind="path", value="A", source="hook")
        with patch.object(self.pi, "command", side_effect=results):
            with self.assertRaisesRegex(TaskError, "session changed"):
                self.pi.launch(self.execution)

    def test_provenance_changes_do_not_abort_pi_handoff(self):
        results = copy.deepcopy(self.results)
        for index, result in enumerate(results[1:], 1):
            result["agent"]["agent_session"] = dict(agent="pi", kind="id", value="session-A", source=f"source-{index}")
        self.registered_pi_launch(results)
        row = self.registry.list()[0]
        self.assertEqual((row["state"], row["session_id"]), ("active", "session-A"))
        self.assertEqual(json.loads(row["herdr_session"])["source"], "source-4")

    def test_absent_herdr_session_is_not_invented(self):
        results = copy.deepcopy(self.results)
        for result in results[1:]:
            result["agent"].pop("agent_session")
        with patch.object(self.pi, "command", side_effect=results):
            result = self.pi.launch(self.execution)
        self.assertEqual((result.session_id, result.session_kind, result.resumability), (None, None, "unknown"))

    def test_session_already_working_is_only_reported_as_submitted(self):
        results = copy.deepcopy(self.results)
        results[1]["agent"]["agent_status"] = "working"
        results[2]["agent"]["agent_status"] = "working"
        with patch.object(self.pi, "command", side_effect=results):
            summary = self.pi.launch(self.execution).summary
        self.assertIn("prompt submitted", summary)
        self.assertNotIn("task started", summary)
        self.assertNotIn("confirmed", summary)

    def test_unrelated_working_activity_cannot_satisfy_a_turn_wait(self):
        results = copy.deepcopy(self.results)
        results[2]["agent"]["agent_status"] = "working"
        results[3]["agent"]["agent_status"] = "working"
        with patch.object(self.pi, "command", side_effect=results) as command:
            summary = self.pi.launch(self.execution).summary
        prompt_call = command.call_args_list[3].args
        self.assertEqual(prompt_call, ("agent", "prompt", "w6:p20", self.handoff))
        self.assertNotIn("--wait", prompt_call)
        self.assertEqual(summary, "Pi prompt submitted in w6:p20 (session pi-session)")

    def test_agent_local_defaults_are_not_silently_replaced(self):
        pi = Pi(AgentOptions("pi"))
        self.assertEqual(pi.launch_args(), [])

    def test_none_mode_maps_to_pi_off(self):
        pi = Pi(AgentOptions("pi", None, "none"))
        self.assertEqual(pi.launch_args(), ["--thinking", "off"])

    def test_unsupported_or_conflicting_pi_options_are_clear(self):
        with self.assertRaisesRegex(TaskError, "Pi mode"):
            Pi(AgentOptions("pi", "model", "ultra"))
        with self.assertRaisesRegex(TaskError, r"model:mode syntax.*--mode"):
            Pi(AgentOptions("pi", "sonnet:high", "low"))
        self.assertEqual(Pi(AgentOptions("pi", "high", "low")).launch_args(),
                         ["--model", "high", "--thinking", "low"])

    def test_pi_model_thinking_suffixes_fail_before_submission(self):
        for model in ["openai/gpt-4o:high", "anthropic/claude-haiku-4-5:max"]:
            with self.subTest(model=model), \
                    patch.object(Pi, "command") as command, \
                    patch("task_start.agent.subprocess.run") as rpc, \
                    self.assertRaisesRegex(TaskError, r"model:mode syntax.*--mode"):
                Pi(AgentOptions("pi", model))
            command.assert_not_called()
            rpc.assert_not_called()

    def test_pi_plain_model_without_mode_remains_supported(self):
        pi = Pi(AgentOptions("pi", "openai/gpt-4o"))
        self.assertEqual(pi.launch_args(), ["--model", "openai/gpt-4o"])
        with patch.object(pi, "model_mode_capabilities") as capabilities:
            pi.validate_model_mode(self.workspace)
        capabilities.assert_not_called()

    def test_installed_pi_rpc_capability_probe_uses_target_worktree(self):
        pi = Pi(AgentOptions("pi", "openai-codex/gpt-6-luna", "max"))
        responses = [
            dict(id="state", type="response", command="get_state", success=True,
                 data=dict(model=dict(provider="openai-codex", id="gpt-6-luna"),
                           thinkingLevel="max")),
            dict(id="levels", type="response", command="get_available_thinking_levels",
                 success=True, data=dict(levels=["off", "minimal", "low", "medium",
                                                 "high", "xhigh", "max"])),
        ]
        completed = MagicMock(returncode=0,
                              stdout=("\n".join(json.dumps(r) for r in responses) + "\n").encode())
        with patch.dict(os.environ, {"GH_TOKEN": "synthetic-controller-key", "GITHUB_TOKEN": "synthetic-fallback",
                                     "SSH_AUTH_SOCK": "/tmp/test-agent.sock", "ANTHROPIC_API_KEY": "synthetic-provider"}), \
                patch("task_start.agent.shutil.which", return_value="/agents/pi"), \
                patch("task_start.agent.subprocess.run", return_value=completed) as run:
            capabilities = pi.model_mode_capabilities(self.workspace)
        self.assertEqual(capabilities, ("openai-codex/gpt-6-luna", "max",
                         ("off", "minimal", "low", "medium", "high", "xhigh", "max")))
        args = run.call_args.args[0]
        self.assertEqual(args[:5], ["/agents/pi", "--model", "openai-codex/gpt-6-luna",
                                    "--thinking", "max"])
        self.assertEqual(args[5:], ["--mode", "rpc", "--no-session", "--no-tools"])
        self.assertNotIn("GH_TOKEN", run.call_args.kwargs["env"])
        self.assertNotIn("GITHUB_TOKEN", run.call_args.kwargs["env"])
        self.assertEqual(run.call_args.kwargs["env"]["SSH_AUTH_SOCK"], "/tmp/test-agent.sock")
        self.assertEqual(run.call_args.kwargs["env"]["ANTHROPIC_API_KEY"], "synthetic-provider")
        self.assertEqual(run.call_args.kwargs["cwd"], self.workspace.path)
        requests = [json.loads(line) for line in run.call_args.kwargs["input"].decode().splitlines()]
        self.assertEqual(requests, [dict(id="state", type="get_state"),
                                    dict(id="levels", type="get_available_thinking_levels")])

    def test_pi_capability_probe_failure_is_clear(self):
        pi = Pi(AgentOptions("pi", "openai/gpt-4o", "high"))
        completed = MagicMock(returncode=1, stdout=b"")
        with patch("task_start.agent.shutil.which", return_value="/agents/pi"), \
                patch("task_start.agent.subprocess.run", return_value=completed), \
                self.assertRaisesRegex(TaskError, "model/mode preflight failed"):
            pi.model_mode_capabilities(self.workspace)

    def test_pi_rejects_silently_clamped_model_mode_combinations(self):
        cases = [
            (AgentOptions("pi", "openai/gpt-4o", "high"),
             ("openai/gpt-4o", "off", ("off",))),
            (AgentOptions("pi", "anthropic/claude-haiku-4-5", "max"),
             ("anthropic/claude-haiku-4-5", "high",
              ("off", "minimal", "low", "medium", "high"))),
        ]
        for options, capabilities in cases:
            with self.subTest(options=options):
                pi = Pi(options)
                execution = AgentExecution(ISSUE, Path("/resolved/repository"), self.workspace,
                                           options, self.handoff)
                with patch.object(pi, "model_mode_capabilities", return_value=capabilities), \
                        patch.object(pi, "command") as command, \
                        self.assertRaisesRegex(TaskError, r"does not support requested mode.*Pi would use"):
                    pi.launch(execution)
                command.assert_not_called()

    def test_pi_accepts_supported_model_mode_combinations(self):
        cases = [
            (AgentOptions("pi", "openai/gpt-4o", "off"),
             ("openai/gpt-4o", "off", ("off",))),
            (AgentOptions("pi", "anthropic/claude-haiku-4-5", "high"),
             ("anthropic/claude-haiku-4-5", "high",
              ("off", "minimal", "low", "medium", "high"))),
            (AgentOptions("pi", "openai-codex/gpt-6-luna", "max"),
             ("openai-codex/gpt-6-luna", "max",
              ("off", "minimal", "low", "medium", "high", "xhigh", "max"))),
        ]
        for options, capabilities in cases:
            with self.subTest(options=options):
                pi = Pi(options)
                with patch.object(pi, "model_mode_capabilities", return_value=capabilities):
                    pi.validate_model_mode(self.workspace)

    def test_mismatched_target_or_argv_is_not_success(self):
        for field, value in [("cwd", "/wrong"), ("agent", "codex")]:
            with self.subTest(field=field):
                results = copy.deepcopy(self.results)
                results[1]["agent"][field] = value
                with patch.object(self.pi, "command", side_effect=results) as command, \
                        self.assertRaisesRegex(TaskError, "not confirmed"):
                    self.pi.launch(self.execution)
                self.assertEqual(command.call_count, 2)
        results = copy.deepcopy(self.results)
        results[1]["argv"] = ["pi"]
        with patch.object(self.pi, "command", side_effect=results) as command, \
                self.assertRaisesRegex(TaskError, "not confirmed"):
            self.pi.launch(self.execution)
        self.assertEqual(command.call_count, 2)

    def test_changed_target_before_submission_does_not_send(self):
        results = copy.deepcopy(self.results)
        results[2]["agent"]["terminal_id"] = "replacement"
        with patch.object(self.pi, "command", side_effect=results) as command, \
                self.assertRaisesRegex(TaskError, "startup"):
            self.pi.launch(self.execution)
        self.assertEqual(command.call_count, 3)

    def test_prompt_response_type_must_be_agent_prompted(self):
        with patch("task_start.agent.run", return_value=json.dumps(dict(
                result=dict(type="agent_info", agent=self.agent)))), \
                self.assertRaisesRegex(TaskError, "Unexpected Herdr agent prompt response"):
            self.pi.command("agent", "prompt", "w6:p20", "probe")

    def test_prompt_failure_is_not_retried_or_mistaken_for_success(self):
        for response in [TaskError("agent_blocked"),
                         dict(type="agent_prompted", agent={}),
                         dict(type="agent_prompted", agent=dict(self.agent, terminal_id="replaced"))]:
            with self.subTest(response=response):
                results = copy.deepcopy(self.results)
                results[3] = response
                with patch.object(self.pi, "command", side_effect=results) as command, \
                        self.assertRaisesRegex(TaskError, "prompt submission"):
                    self.pi.launch(self.execution)
                self.assertEqual(command.call_count, 4)

    def test_exact_multiline_context_reaches_herdr_prompt_only(self):
        issue = replace(ISSUE, title="Current title", description="\nExact α\r\n  task\n")
        workspace = replace(self.workspace, slice="importer")
        handoff = implementation_handoff(issue, workspace)
        execution = replace(self.execution, issue=issue, workspace=workspace, handoff=handoff)
        with patch.object(self.pi, "command", side_effect=self.results) as command:
            self.pi.launch(execution)
        self.assertEqual(command.call_args_list[3].args[3], handoff)
        self.assertIn("Slice: importer", command.call_args_list[3].args[3])
        self.assertNotIn(issue.description, str(command.call_args_list[1].args))

    def test_review_handoff_is_delivered_without_implementation_framing(self):
        handoff = "Review DEV-7. Report findings only; do not implement changes."
        execution = replace(self.execution, purpose="review", handoff=handoff)
        results = copy.deepcopy(self.results)
        results[1]["argv"] = ["pi", *self.pi.review_args()]
        with patch.object(self.pi, "command", side_effect=results) as command:
            self.pi.launch(execution)
        submitted = command.call_args_list[3].args[3]
        self.assertEqual(submitted, handoff)
        self.assertNotIn("Implement Linear issue", submitted)
        self.assertNotIn("Implement only this slice", submitted)

    def test_unavailable_pi(self):
        with patch("task_start.agent.shutil.which", return_value=None), \
                self.assertRaisesRegex(TaskError, "pi is not installed"):
            self.pi.check_available()


class ControlledTaskStartAcceptanceTests(unittest.TestCase):
    """Drive the same mocked workflow boundary through both real adapters."""

    def setUp(self):
        self.enterContext(patch("task_start.agent.HerdrAgentAdapter.filter_shell_credentials"))
        self.enterContext(patch.object(Pi, "clear_shell_input"))
        directory = self.enterContext(tempfile.TemporaryDirectory())
        self.enterContext(patch("task_start.contexts.registry_path", return_value=Path(directory) / "contexts.sqlite3"))
        self.enterContext(patch("task_start.cli.launch_registered",
                                side_effect=lambda agent, execution: agent.launch(execution)))
        self.enterContext(patch("task_start.cli.load_local", return_value=LOCAL))
        self.enterContext(patch("task_start.cli.load_projects", return_value=[baseline.PROJECT]))
        linear = self.enterContext(patch("task_start.cli.Linear")).return_value
        linear.get_issue.return_value = ISSUE
        self.enterContext(patch("task_start.cli.Git"))
        herdr = self.enterContext(patch("task_start.cli.Herdr")).return_value
        self.workspace = Workspace("dev-7-add-ingestion-cli", Path("/selected/worktree"),
                                   "w7", "w7:t5", "w7:p8", "ready")
        herdr.prepare.return_value = self.workspace
        self.enterContext(patch("task_start.agent.shutil.which", return_value="/agents/executable"))

    def test_same_semantic_handoff_reaches_codex_and_pi(self):
        message_id = "b67e58a0-3876-4e85-ab88-bc170e847327"
        bootstrap = (f"Handoff readiness {message_id}. Do not use tools or modify files. "
                     "Reply READY, then wait for the task prompt.")
        prompt = implementation_handoff(ISSUE, self.workspace)
        codex_agent = dict(agent="codex", agent_status="idle", workspace_id="w7",
                           tab_id="w7:t5", pane_id="w7:p8", terminal_id="term_codex",
                           cwd=str(self.workspace.path), foreground_cwd=str(self.workspace.path))
        codex_args = ["--cd", str(self.workspace.path), "--model", "gpt-6-astra",
                      "--config", 'model_reasoning_effort="high"', "--", bootstrap]
        codex_results = [
            dict(type="pane_list", panes=[dict(codex_agent, agent=None)]),
            dict(type="agent_started", agent=codex_agent, argv=["codex", *codex_args]),
            dict(type="agent_info", agent=codex_agent),
            dict(type="agent_info", agent=codex_agent),
        ]
        rpc = MagicMock()
        queued = False

        def codex_request(method, params, **kwargs):
            nonlocal queued
            if method == "thread/list":
                return dict(data=[dict(id="01a0d314-bd68-7203-8b68-f2520f892afa",
                                       cwd=str(self.workspace.path), preview=bootstrap)])
            if method == "thread/read":
                return dict(thread=dict(id="01a0d314-bd68-7203-8b68-f2520f892afa",
                                        cwd=str(self.workspace.path)))
            if method == "thread/items/list":
                items = [dict(turnId="ready", item=dict(type="userMessage", clientId="native",
                              content=[dict(type="text", text=bootstrap)]))]
                if queued:
                    items.append(dict(turnId="task-turn", item=dict(type="userMessage", clientId=message_id,
                                      content=[dict(type="text", text=prompt)])))
                return dict(data=items, nextCursor=None)
            if method == "thread/queue/add":
                queued = True
                return dict(queuedSubmission=dict(id="queue", clientUserMessageId=message_id,
                                                   input=params["input"]))
            self.fail(method)

        rpc.request.side_effect = codex_request
        rpc_factory = MagicMock()
        rpc_factory.return_value.__enter__.return_value = rpc
        with patch("task_start.agent.uuid4", return_value=message_id), \
                patch("task_start.agent.CodexRPC", rpc_factory), \
                patch.object(Codex, "clear_shell_input") as clear_shell_input, \
                patch.object(Codex, "command", side_effect=codex_results):
            codex_output = cli.start("DEV-7")

        codex_handoff = next(call.args[1]["input"][0]["text"] for call in rpc.request.call_args_list
                              if call.args[0] == "thread/queue/add")
        pi_agent = dict(agent="pi", agent_status="working", workspace_id="w7",
                        tab_id="w7:t5", pane_id="w7:p8", terminal_id="term_pi",
                        cwd=str(self.workspace.path), foreground_cwd=str(self.workspace.path))
        pi_args = ["--model", "gpt-6-astra", "--thinking", "high"]
        pi_results = [dict(type="pane_list", panes=[dict(pi_agent, agent=None)]),
                      dict(type="agent_started", agent=pi_agent, argv=["pi", *pi_args]),
                      dict(type="agent_info", agent=pi_agent),
                      dict(type="agent_prompted", agent=pi_agent),
                      dict(type="agent_info", agent=pi_agent)]
        with patch.object(Pi, "model_mode_capabilities",
                          return_value=("openai-codex/gpt-6-astra", "high",
                                        ("off", "minimal", "low", "medium", "high"))), \
                patch.object(Pi, "command", side_effect=pi_results) as pi_command:
            pi_output = cli.start("DEV-7", agent_kind="pi")

        self.assertEqual(codex_handoff, prompt)
        clear_shell_input.assert_called_once_with(self.workspace)
        self.assertEqual(pi_command.call_args_list[1].args[-len(pi_args):], tuple(pi_args))
        self.assertNotIn(prompt, pi_command.call_args_list[1].args)
        self.assertEqual(pi_command.call_args_list[3].args,
                         ("agent", "prompt", "w7:p8", prompt))
        self.assertIn("Codex turn task-turn confirmed", codex_output)
        self.assertIn("Pi prompt submitted", pi_output)


class HandoffOrchestrationTests(unittest.TestCase):
    setUp = baseline.OrchestrationTests.setUp

    def test_config_defaults_and_independent_command_overrides_reach_factory(self):
        cases = [
            ({}, AgentOptions("codex", "gpt-6-astra", "high")),
            ({"agent_kind": "pi"}, AgentOptions("pi", "gpt-6-astra", "high")),
            ({"model": "run-model"}, AgentOptions("codex", "run-model", "high")),
            ({"mode": "low"}, AgentOptions("codex", "gpt-6-astra", "low")),
            ({"agent_kind": "pi", "model": "pi-model", "mode": "medium"},
             AgentOptions("pi", "pi-model", "medium")),
        ]
        for kwargs, expected in cases:
            with self.subTest(kwargs=kwargs):
                self.agent.reset_mock()
                self.agent.launch.return_value = LaunchResult(expected.kind, "w7:p8", "working")
                with patch("task_start.cli.adapter_for", return_value=self.agent) as factory:
                    cli.start("DEV-7", **kwargs)
                factory.assert_called_once_with(expected)
                execution = self.agent.launch.call_args.args[0]
                self.assertEqual(execution.options, expected)
                self.assertEqual(execution.repository, Path("/projects/example-repo").resolve())
                self.assertEqual(execution.purpose, "implementation")

    def test_only_codex_gets_exact_repository_profile_policy(self):
        local = replace(LOCAL, codex_repository_profiles={
            "example-repo": "example-repo-trusted",
            "other-repository": "other-trusted",
        })
        with patch("task_start.cli.load_local", return_value=local):
            cli.start("DEV-7")
        execution = self.agent.launch.call_args.args[0]
        self.assertEqual(execution.policy, {"codex_profile": "example-repo-trusted"})

        self.agent.reset_mock()
        self.agent.launch.return_value = LaunchResult("pi", "w7:p8", "working")
        with patch("task_start.cli.load_local", return_value=local):
            cli.start("DEV-7", agent_kind="pi")
        self.assertEqual(self.agent.launch.call_args.args[0].policy, {})

    def test_no_agent_conflicts_with_execution_overrides_before_mutation(self):
        for kwargs in [{"agent_kind": "pi"}, {"model": "model"}, {"mode": "high"}]:
            with self.subTest(kwargs=kwargs), self.assertRaisesRegex(TaskError, "conflicts"):
                cli.start("DEV-7", no_agent=True, **kwargs)
        self.git.update_base.assert_not_called()
        self.herdr.prepare.assert_not_called()
        self.linear.start.assert_not_called()

    def test_handoff_order_and_fresh_context(self):
        self.workspace = replace(self.workspace, slice="codex-handoff")
        order = []
        self.git.update_base.side_effect = lambda *a: order.append("base")
        self.herdr.prepare.side_effect = lambda *a, ownership, selected: order.append("workspace") or self.workspace
        self.linear.start.side_effect = lambda *a: order.append("linear")
        self.agent.launch.side_effect = lambda *a: order.append("agent") or LaunchResult(
            "codex", self.workspace.pane_id, "working")
        self.linear.get_issue.return_value = replace(ISSUE, title="New title",
                                                    description="Fresh task\n  exact\n" + ISSUE.description)
        cli.start("DEV-7", slice="Codex Handoff")
        self.assertEqual(order, ["base", "workspace", "linear", "agent"])
        execution = self.agent.launch.call_args.args[0]
        self.assertIs(execution.workspace, self.workspace)
        self.assertIn("New title", execution.handoff)
        self.assertIn("Fresh task\n  exact\n", execution.handoff)
        self.assertEqual(self.herdr.prepare.call_args.args[2:], ("dev-7-codex-handoff", "DEV-7", "codex-handoff"))
        self.assertIn("Slice: codex-handoff", execution.handoff)

    def test_no_requested_slice_uses_resolved_slice_in_prompt(self):
        selected = replace(self.workspace, branch="dev-7-importer", slice="importer")
        self.herdr.prepare.return_value = selected
        cli.start("DEV-7")
        self.assertIsNone(self.herdr.prepare.call_args.args[-1])
        execution = self.agent.launch.call_args.args[0]
        self.assertEqual(execution.workspace, selected)
        self.assertIn("Slice: importer\nImplement only this slice of the task.", execution.handoff)

    def test_no_agent_still_prepares_and_updates_status(self):
        with patch("task_start.cli.adapter_for") as adapter:
            output = cli.start("DEV-7", no_agent=True)
        self.herdr.prepare.assert_called_once()
        self.linear.start.assert_called_once_with(ISSUE)
        adapter.assert_not_called()
        self.assertIn("skipped (--no-agent)", output)

    def test_preparation_or_status_failure_never_launches(self):
        for boundary in [self.git.update_base, self.herdr.prepare, self.linear.start]:
            with self.subTest(boundary=boundary):
                boundary.side_effect = TaskError("failed")
                with self.assertRaises(TaskError):
                    cli.start("DEV-7")
                self.agent.launch.assert_not_called()
                boundary.side_effect = None

    def test_agent_failure_is_reported_without_undoing_ready_workspace(self):
        self.agent.launch.side_effect = TaskError("Codex startup failed")
        with patch("sys.stderr", new=io.StringIO()) as stderr, patch("sys.stdout", new=io.StringIO()) as stdout:
            self.assertEqual(cli.main(["start", "DEV-7"]), 1)
        self.assertIn("Codex startup failed", stderr.getvalue())
        self.assertEqual(stdout.getvalue(), "")
        self.linear.start.assert_called_once_with(ISSUE)

    def test_missing_agent_config_before_mutations(self):
        with patch("task_start.cli.load_local", return_value=replace(LOCAL, agent=None)), self.assertRaisesRegex(TaskError, r"\[agent\]"):
            cli.start("DEV-7")
        self.git.update_base.assert_not_called()
        self.herdr.prepare.assert_not_called()
