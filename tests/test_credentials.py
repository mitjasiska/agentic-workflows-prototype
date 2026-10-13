"""Synthetic credentials only; no live gh, credential stores, SSH or Herdr."""

import io
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import traceback
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit

from task_start import TaskError
from task_start.agent import AgentOptions, Codex, Pi
from task_start.credentials import GITHUB_TOKEN_VARIABLES, agent_environment
from task_start.github import api_credential, check_history, cli_credential, request
from task_start.workspace import Workspace, run


SECRET = "synthetic-controller-token"
TOKENS = {name: SECRET + "-" + name for name in GITHUB_TOKEN_VARIABLES}
RETAINED = {"PATH": "/usr/bin:/bin", "SSH_AUTH_SOCK": "/tmp/test-ssh-agent.sock",
            "OPENAI_API_KEY": "synthetic-provider-key", "ANTHROPIC_API_KEY": "synthetic-pi-key",
            "GH_CONFIG_DIR": "/tmp/synthetic-gh-config", "GPG_TTY": "/dev/pts/test"}


class LocalCredentialTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.dict(os.environ, RETAINED, clear=True))
        self.lookup = self.enterContext(patch("task_start.github.subprocess.run",
            return_value=subprocess.CompletedProcess([], 0, (SECRET + "\n").encode())))

    def test_lookup_is_host_scoped_private_bounded_and_does_not_export_token(self):
        before = dict(os.environ)
        self.assertEqual(api_credential(required=True), ("gh auth token", SECRET))
        args, options = self.lookup.call_args
        self.assertEqual(args[0], ["gh", "auth", "token", "--hostname", "github.com"])
        self.assertEqual(options["stdin"], subprocess.DEVNULL)
        self.assertEqual(options["stdout"], subprocess.PIPE)
        self.assertEqual(options["stderr"], subprocess.DEVNULL)
        self.assertEqual(options["timeout"], 10)
        self.assertEqual(options["env"], dict(RETAINED, GH_PROMPT_DISABLED="1"))
        self.assertEqual(dict(os.environ), before)
        self.assertNotIn(SECRET, repr(self.lookup.call_args))

    def test_explicit_overrides_keep_precedence_and_do_not_call_gh(self):
        for primary, secondary, source in (("primary", "secondary", "GH_TOKEN"),
                                            ("", "secondary", "GITHUB_TOKEN")):
            with self.subTest(source=source), patch.dict(os.environ, GH_TOKEN=primary, GITHUB_TOKEN=secondary):
                self.assertEqual(api_credential(), (source, primary or secondary))
        self.lookup.assert_not_called()

    def test_missing_gh_and_missing_login_allow_public_reads_but_refuse_required_auth(self):
        for result in (FileNotFoundError(SECRET), subprocess.CompletedProcess([], 1, SECRET.encode())):
            self.lookup.side_effect = result if isinstance(result, Exception) else None
            self.lookup.return_value = result
            with self.subTest(result=type(result).__name__):
                self.assertEqual(api_credential(), ("unauthenticated", None))
                with patch("task_start.github.urlopen", return_value=io.BytesIO(b"[]")) as transport:
                    request("owner/repo/pulls", "dev-7")
                self.assertIsNone(transport.call_args.args[0].get_header("Authorization"))
                with self.assertRaisesRegex(TaskError, "authentication is missing.*gh auth login"):
                    api_credential(required=True)

    def test_lookup_errors_never_disclose_output_or_fallback(self):
        for error in (OSError(SECRET), subprocess.TimeoutExpired("gh", 10, output=SECRET, stderr=SECRET)):
            self.lookup.side_effect = error
            with self.subTest(error=type(error).__name__), patch("task_start.github.urlopen") as transport:
                with self.assertRaisesRegex(TaskError, "credential lookup could not complete") as caught:
                    request("owner/repo/pulls", "dev-7")
                self.assertNotIn(SECRET, "".join(traceback.format_exception(caught.exception)))
                transport.assert_not_called()

    def test_malformed_cli_output_is_refused_without_disclosure(self):
        for value in (b"", b"\n", (SECRET + " \n").encode(), (SECRET + "\nextra\n").encode(),
                      b"\xff", (SECRET + "\t\n").encode(), (SECRET + "\x00\n").encode(),
                      (SECRET + "\r").encode(), (SECRET + "\n\r\n").encode()):
            self.lookup.return_value = subprocess.CompletedProcess([], 0, value)
            with self.subTest(value=value), self.assertRaises(TaskError) as caught:
                cli_credential()
            self.assertNotIn(SECRET, "".join(traceback.format_exception(caught.exception)))
        for suffix in ("", "\n", "\r\n"):
            self.lookup.return_value = subprocess.CompletedProcess([], 0, (SECRET + suffix).encode())
            self.assertEqual(cli_credential(), ("gh auth token", SECRET))

    def history(self, *pages):
        return patch("task_start.github.urlopen", side_effect=[io.BytesIO(json.dumps(p).encode()) for p in pages])

    def pull(self, *, state="open", merged=None):
        return dict(number=1, state=state, merged_at=merged,
                    head=dict(ref="dev-7", repo=dict(full_name="owner/private")),
                    base=dict(repo=dict(full_name="owner/private")))

    def test_private_history_uses_local_token_for_empty_and_open_results(self):
        for existing in (False, True):
            for page in ([], [self.pull()]):
                with self.subTest(existing=existing, page=page), self.history(page) as transport:
                    check_history("git@github.com:owner/private.git", "dev-7", existing=existing)
                outgoing = transport.call_args.args[0]
                self.assertEqual(outgoing.get_header("Authorization"), "Bearer " + SECRET)
                self.assertEqual(urlsplit(outgoing.full_url).netloc, "api.github.com")
                self.assertEqual(parse_qs(urlsplit(outgoing.full_url).query)["state"], ["all"])

    def test_private_pagination_retains_merged_and_closed_refusals(self):
        for merged in (None, "2026-01-01T00:00:00Z"):
            for existing in (False, True):
                with self.history([self.pull()] * 100, [self.pull(state="closed", merged=merged)]) as transport:
                    with self.assertRaisesRegex(TaskError, "Historical workspace"):
                        check_history("git@github.com:owner/private.git", "dev-7", existing=existing)
                self.assertEqual(transport.call_count, 2)
                self.assertTrue(all(c.args[0].get_header("Authorization") == "Bearer " + SECRET
                                    for c in transport.call_args_list))

    def test_private_unauthorized_history_stops_without_retry_or_secret_disclosure(self):
        for code in (401, 403, 404):
            error = HTTPError("https://private.invalid/" + SECRET, code, SECRET, {}, io.BytesIO(SECRET.encode()))
            with patch("task_start.github.urlopen", side_effect=error) as transport:
                with self.assertRaisesRegex(TaskError, "Cannot establish GitHub PR history") as caught:
                    check_history("git@github.com:owner/private.git", "dev-7", existing=True)
            self.assertEqual(transport.call_count, 1)
            self.assertNotIn(SECRET, "".join(traceback.format_exception(caught.exception)))
            self.assertIn("gh auth token", str(caught.exception))


class AgentCredentialTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.dict(os.environ, {**RETAINED, **TOKENS}, clear=True))
        self.workspace = Workspace("dev-7", Path("/tmp"), "w1", "t1", "p1", "ready")

    def test_direct_environment_preserves_provider_and_ssh_auth_without_mutating_controller(self):
        self.assertEqual(agent_environment(), RETAINED)
        self.assertEqual({key: os.environ[key] for key in TOKENS}, TOKENS)

    def test_terminal_launch_filters_the_pane_environment_for_both_agents_and_resume(self):
        real_run = subprocess.run
        for adapter in (Codex(AgentOptions("codex")), Pi(AgentOptions("pi"))):
            for args in ([], ["resume", "session-id"]):
                pane_input = ""
                observed = None

                def pane_send(command, **kwargs):
                    nonlocal pane_input
                    self.assertEqual(command[:4], ["herdr", "pane", "send-text", "p1"])
                    self.assertEqual(kwargs["env"], RETAINED)
                    pane_input += command[4]

                def start(*command):
                    nonlocal observed
                    self.assertEqual(command[:2], ("agent", "start"))
                    # Herdr appends its canonical argv to existing shell input.
                    # Substitute a local Python probe for the real provider CLI.
                    probe = [sys.executable, "-c", "import json, os; print(json.dumps(dict(os.environ)))"]
                    result = real_run(["bash", "--noprofile", "--norc", "-c", pane_input + shlex.join(probe)],
                                      capture_output=True, text=True, check=True, env={**RETAINED, **TOKENS})
                    observed = json.loads(result.stdout)
                    return dict(argv=[adapter.kind, *args], agent=dict(agent=adapter.kind, agent_status="idle",
                        workspace_id="w1", tab_id="t1", pane_id="p1", terminal_id="terminal",
                        cwd="/tmp", foreground_cwd="/tmp"))

                # A recreated pane can retain tokens even when the new controller
                # uses gh's local store and has no exported GitHub credentials.
                controller = RETAINED if args else {**RETAINED, **TOKENS}
                with self.subTest(agent=adapter.kind, args=args), patch.dict(os.environ, controller, clear=True), \
                        patch("task_start.agent.run", side_effect=pane_send), \
                        patch.object(adapter, "command", side_effect=start):
                    adapter.start_agent(self.workspace, args, review=bool(args))
                self.assertTrue(all(key not in observed for key in TOKENS))
                self.assertEqual({key: observed[key] for key in RETAINED}, RETAINED)
                self.assertTrue(all(os.environ[key] == value for key, value in TOKENS.items()))
                self.assertNotIn(SECRET, pane_input)
                self.assertNotIn("\n", pane_input)

    def test_prefix_delivery_failure_never_launches_agent(self):
        for adapter in (Codex(AgentOptions("codex")), Pi(AgentOptions("pi"))):
            with patch("task_start.agent.run", side_effect=TaskError("prefix unavailable")), \
                    patch.object(adapter, "command") as start, self.assertRaises(TaskError):
                adapter.start_agent(self.workspace, [])
            start.assert_not_called()

    def test_git_failures_offer_ssh_agent_recovery_without_echoing_stderr(self):
        for operation in ("fetch", "ls-remote"):
            with patch("task_start.workspace.subprocess.run", return_value=subprocess.CompletedProcess(
                    [], 128, b"", ("Permission denied (publickey) " + SECRET).encode())):
                with self.assertRaises(TaskError) as caught:
                    run(["git", "-C", "/tmp", operation, "origin"])
            self.assertIn("SSH_AUTH_SOCK", str(caught.exception))
            self.assertIn("ssh-add -l", str(caught.exception))
            self.assertNotIn(SECRET, str(caught.exception))

    def test_git_transport_inherits_the_configured_ssh_agent(self):
        with patch("task_start.workspace.subprocess.run", return_value=subprocess.CompletedProcess([], 0, b"", b"")) as command:
            run(["git", "-C", "/tmp", "ls-remote", "origin"])
        self.assertIsNone(command.call_args.kwargs["env"])
        self.assertEqual(os.environ["SSH_AUTH_SOCK"], RETAINED["SSH_AUTH_SOCK"])


@unittest.skipUnless(all(shutil.which(command) for command in ("ssh-agent", "ssh-add", "ssh-keygen")),
                     "local OpenSSH tools are required for the isolated encrypted-key check")
class SSHAgentSetupTests(unittest.TestCase):
    def test_documented_agent_setup_unlocks_once_and_reuses_the_key(self):
        # Disposable encrypted key and agent only; no user ~/.ssh, network, or
        # real passphrase. The askpass double counts the one local unlock prompt.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            env = {"PATH": "/usr/bin:/bin", "HOME": directory}
            key = root / "git-key"
            subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "synthetic-passphrase", "-f", str(key)],
                           env=env, check=True, capture_output=True, timeout=10)
            agent = subprocess.run(["ssh-agent", "-s"], env=env, check=True, capture_output=True, text=True, timeout=10)
            for line in agent.stdout.splitlines():
                if line.startswith(("SSH_AUTH_SOCK=", "SSH_AGENT_PID=")):
                    name, value = line.split(";", 1)[0].split("=", 1)
                    env[name] = value
            try:
                prompts = root / "prompts"
                askpass = root / "askpass"
                askpass.write_text("#!/bin/sh\nprintf 'prompt\\n' >> " + shlex.quote(str(prompts)) +
                                   "\nprintf 'synthetic-passphrase\\n'\n")
                askpass.chmod(0o700)
                env.update(SSH_ASKPASS=str(askpass), SSH_ASKPASS_REQUIRE="force", DISPLAY=":synthetic")
                subprocess.run(["ssh-add", "-t", "8h", str(key)], env=env, stdin=subprocess.DEVNULL,
                               check=True, capture_output=True, timeout=10)
                subprocess.run(["ssh-add", "-l"], env=env, check=True, capture_output=True, timeout=10)
                for _ in range(2):
                    # Sign/verify through the agent, without accessing the key file.
                    subprocess.run(["ssh-add", "-T", str(key) + ".pub"], env=env,
                                   check=True, capture_output=True, timeout=10)
                self.assertEqual(prompts.read_text(), "prompt\n")
            finally:
                subprocess.run(["ssh-agent", "-k"], env=env, check=True, capture_output=True, timeout=10)


if __name__ == "__main__":
    unittest.main()
