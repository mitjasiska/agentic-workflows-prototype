import hashlib
import json
import os
from pathlib import Path
import shutil
import shlex
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "scripts/ab-worktree"
INSTALLER = ROOT / "scripts/install_browser.py"


class BrowserFixture:
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="awl-browser-")
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.bin = self.base / "bin"
        self.bin.mkdir()
        self.log = self.base / "calls.jsonl"
        browser = self.bin / "agent-browser"
        browser.write_text(f"#!{sys.executable}\n"
                           "import json, os, sys\n"
                           "record = {'args': sys.argv[1:], 'cwd': os.getcwd()}\n"
                           "if os.environ.get('BROWSER_TEST_READ_STDIN'):\n"
                           "    record['stdin'] = sys.stdin.read()\n"
                           "with open(os.environ['BROWSER_TEST_LOG'], 'a') as output:\n"
                           "    output.write(json.dumps(record) + '\\n')\n"
                           "print('browser output')\n"
                           "print('browser diagnostic', file=sys.stderr)\n"
                           "sys.exit(int(os.environ.get('BROWSER_TEST_EXIT', '0')))\n")
        browser.chmod(0o755)
        # Avoid personal Git configuration and browser/session environment state.
        self.env = dict(PATH=f"{self.bin}:/usr/bin:/bin", HOME=str(self.base / "home"),
                        GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
                        BROWSER_TEST_LOG=str(self.log))
        self.repo = self.base / "repo with spaces"
        self.repo.mkdir()
        self.git("init", "-q", cwd=self.repo)
        self.git("-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                 "commit", "--allow-empty", "-qm", "initial", cwd=self.repo)
        self.worktree = self.base / "linked worktree"
        self.git("worktree", "add", "--detach", str(self.worktree), "HEAD", cwd=self.repo)

    def git(self, *args, cwd):
        result = subprocess.run(["git", *args], cwd=cwd, env=self.env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result

    def call(self, *args, cwd=None, env=None, helper=HELPER, input=None):
        return subprocess.run(["/bin/sh", str(helper), *args], cwd=cwd or self.repo,
                              env=env or self.env, input=input, capture_output=True, text=True, timeout=10)

    def records(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []

    def session(self, agent="codex", instance=None, cwd=None, env=None):
        args = [agent] + (["--instance", instance] if instance is not None else []) + ["snapshot", "-i"]
        result = self.call(*args, cwd=cwd, env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        record = self.records()[-1]
        self.assertEqual(record["args"][0], "--session")
        return record["args"][1]


class BrowserTests(BrowserFixture, unittest.TestCase):
    def test_default_identity_reuses_root_and_isolates_agents_and_worktrees(self):
        child = self.repo / "nested"
        child.mkdir()
        session = self.session()
        self.assertEqual(session, self.session(cwd=child))
        self.assertEqual(session, self.session())
        self.assertNotEqual(session, self.session("pi"))
        self.assertNotEqual(session, self.session(cwd=self.worktree))
        self.assertNotEqual(self.session("pi"), self.session("pi", cwd=self.worktree))
        identity = b"\0".join([os.fsencode(self.repo.resolve()), b"codex", b""]) + b"\0"
        self.assertEqual(session, "awl-codex-" + hashlib.sha256(identity).hexdigest()[:32])

    def test_instance_identity_is_stable_and_distinct_from_default(self):
        sessions = [self.session(instance=value) for value in (None, "reviewer-1", "reviewer-2", "default")]
        self.assertEqual(len(set(sessions)), 4)
        self.assertEqual(sessions[1], self.session(instance="reviewer-1"))
        self.assertNotEqual(sessions[1], self.session("pi", instance="reviewer-1"))
        self.assertNotEqual(sessions[1], self.session(instance="reviewer-1", cwd=self.worktree))

    def test_valid_instance_boundary_and_characters(self):
        for instance in ("a", "A.b_C-9", "0" * 64):
            with self.subTest(instance=instance):
                self.session(instance=instance)

    def test_symlink_alias_has_same_identity(self):
        alias = self.base / "alias"
        alias.symlink_to(self.worktree, target_is_directory=True)
        self.assertEqual(self.session(cwd=alias), self.session(cwd=self.worktree))

    def test_trailing_newline_worktree_roots_remain_distinct_and_stable(self):
        roots = [self.worktree]
        for suffix in ("\n", "\n\n"):
            worktree = self.base / (self.worktree.name + suffix)
            self.git("worktree", "add", "--detach", str(worktree), "HEAD", cwd=self.repo)
            roots.append(worktree)
        for agent in ("pi", "codex"):
            with self.subTest(agent=agent):
                sessions = [self.session(agent, cwd=root) for root in roots]
                self.assertEqual(len(set(sessions)), len(roots))
                for root, session in zip(roots, sessions):
                    identity = b"\0".join([os.fsencode(root.resolve()), agent.encode(), b""]) + b"\0"
                    self.assertEqual(session, f"awl-{agent}-" + hashlib.sha256(identity).hexdigest()[:32])
                    nested = root / "nested"
                    nested.mkdir(exist_ok=True)
                    self.assertEqual(session, self.session(agent, cwd=nested))
                    self.assertEqual(session, self.session(agent, cwd=root))
                    self.assertEqual(self.records()[-1]["cwd"], str(root))
                    alias = self.base / f"alias-{agent}-{len(root.name)}"
                    alias.symlink_to(root, target_is_directory=True)
                    self.assertEqual(session, self.session(agent, cwd=alias))

    def test_environment_cannot_redirect_worktree_or_session(self):
        expected = self.session(cwd=self.worktree)
        altered = dict(self.env, GIT_DIR=str(self.repo / ".git"), GIT_WORK_TREE=str(self.repo),
                       GIT_COMMON_DIR=str(self.repo / ".git"), AGENT_BROWSER_SESSION="shared-default")
        self.assertEqual(expected, self.session(cwd=self.worktree, env=altered))
        self.assertNotEqual(expected, self.session())

    def test_arguments_stdin_stdout_stderr_and_working_directory_are_preserved(self):
        args = ["eval", 'document.querySelector("main").textContent', "", "a b", "line\nbreak", "$(echo unsafe)"]
        result = self.call("pi", "--", *args, cwd=self.worktree)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.records()[-1]["args"][2:], args)
        self.assertEqual(self.records()[-1]["cwd"], str(self.worktree))
        self.assertEqual(result.stdout, "browser output\n")
        self.assertEqual(result.stderr, "browser diagnostic\n")
        result = self.call("pi", "eval", "--stdin", input="document.title\n",
                           env=dict(self.env, BROWSER_TEST_READ_STDIN="1"))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.records()[-1]["stdin"], "document.title\n")
        # Snapshot -s is a selector option, not a session override.
        result = self.call("codex", "snapshot", "-s", "#main", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.records()[-1]["args"][2:], ["snapshot", "-s", "#main", "--json"])

    def test_command_failure_is_explicit_and_preserves_status(self):
        result = self.call("pi", "open", "about:blank", env=dict(self.env, BROWSER_TEST_EXIT="37"))
        self.assertEqual(result.returncode, 37)
        self.assertIn("agent-browser failed for session awl-pi-", result.stderr)
        self.assertIn("browser diagnostic", result.stderr)
        self.assertEqual(len(self.records()), 1)

    def test_invalid_agent_instance_and_session_overrides_never_call_browser(self):
        cases = [[], ["other", "snapshot"], ["pi"], ["pi", "--instance"],
                 ["pi", "--instance", "ok"], ["pi", "snapshot", "--instance", "late"],
                 ["codex", "--instance=bad", "snapshot"],
                 ["pi", "--session", "shared", "snapshot"],
                 ["pi", "snapshot", "--session=shared"],
                 ["pi", "snapshot", "--", "--session", "shared"]]
        cases += [["codex", "--instance", value, "snapshot"] for value in
                  ("", "-option", ".dot", "_under", "a/b", "a b", "x\ny", "a" * 65, "é", "$(id)")]
        for args in cases:
            with self.subTest(args=args):
                result = self.call(*args)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn("ab-worktree:", result.stderr)
        self.assertEqual(self.records(), [])

    def test_outside_git_and_bare_and_git_metadata_fail_without_browser(self):
        bare = self.base / "bare"
        self.git("init", "--bare", "-q", str(bare), cwd=self.base)
        for cwd in (self.base, bare, self.repo / ".git"):
            with self.subTest(cwd=cwd):
                result = self.call("pi", "snapshot", cwd=cwd)
                self.assertEqual(result.returncode, 2)
                self.assertIn("Git worktree", result.stderr)
        self.assertEqual(self.records(), [])

    def test_missing_dependencies_and_invalid_hash_fail_without_browser(self):
        # Controlled PATH contains only the dependencies required for each case.
        path = self.base / "controlled-bin"
        path.mkdir()
        for name in ("git", "sha256sum"):
            (path / name).symlink_to(shutil.which(name))
        result = self.call("pi", "snapshot", env=dict(self.env, PATH=str(path)))
        self.assertEqual(result.returncode, 2)
        self.assertIn("missing from PATH: agent-browser", result.stderr)
        (path / "agent-browser").symlink_to(self.bin / "agent-browser")
        for name in ("git", "sha256sum"):
            target = path / name
            target.unlink()
            result = self.call("pi", "snapshot", env=dict(self.env, PATH=str(path)))
            self.assertEqual(result.returncode, 2)
            self.assertIn(f"missing from PATH: {name}", result.stderr)
            target.symlink_to(shutil.which(name))
        (path / "sha256sum").unlink()
        fake_hash = path / "sha256sum"
        for body in ("exit 9", "printf 'bad hash\\n'", "printf 'ab\\n'"):
            fake_hash.write_text("#!/bin/sh\n" + body + "\n")
            fake_hash.chmod(0o755)
            result = self.call("pi", "snapshot", env=dict(self.env, PATH=str(path)))
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertIn("ab-worktree:", result.stderr)
        self.assertEqual(self.records(), [])

    def test_unresolvable_worktree_root_fails_before_browser(self):
        git = self.bin / "git"
        for result in ("exit 7", "printf '\\n'", "printf '/missing/awl-browser-worktree\\n'"):
            with self.subTest(result=result):
                git.write_text("#!/bin/sh\n"
                               "if [ \"$2\" = --is-inside-work-tree ]; then printf 'true\\n'; "
                               "else " + result + "; fi\n")
                git.chmod(0o755)
                completed = self.call("codex", "snapshot")
                self.assertEqual(completed.returncode, 2)
                self.assertIn("worktree root", completed.stderr)
        self.assertEqual(self.records(), [])

    def test_help_does_not_resolve_context_or_invoke_browser(self):
        result = self.call("--help", cwd=self.base, env=dict(self.env, PATH="/usr/bin:/bin"))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--instance ID", result.stdout)
        self.assertEqual(self.records(), [])


class BrowserInstallationTests(BrowserFixture, unittest.TestCase):
    def install(self, *args, env=None):
        return subprocess.run([sys.executable, str(INSTALLER), *args], env=env or self.env,
                              cwd=ROOT, capture_output=True, text=True, timeout=10)

    @property
    def installed_bin(self):
        return Path(self.env["HOME"]) / ".local/bin"

    @property
    def codex_home(self):
        return Path(self.env["HOME"]) / ".codex"

    @property
    def pi_home(self):
        return Path(self.env["HOME"]) / ".pi/agent"

    def write(self, path, content):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)

    def test_setup_is_lazy_idempotent_and_installed_commands_work(self):
        result = self.install()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.records(), [])
        home = Path(self.env["HOME"])
        snapshot = {p: (p.read_bytes(), p.stat().st_mtime_ns, p.stat().st_mode)
                    for p in home.rglob("*") if p.is_file()}
        result = self.install()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(snapshot, {p: (p.read_bytes(), p.stat().st_mtime_ns, p.stat().st_mode)
                                   for p in home.rglob("*") if p.is_file()})
        self.assertEqual(self.records(), [])
        sessions = []
        for agent in ("pi", "codex"):
            shortcut = self.installed_bin / f"ab-{agent}"
            result = subprocess.run([str(shortcut), "snapshot", "-i"], cwd=self.worktree,
                                    env=self.env, capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            sessions.append(self.records()[-1]["args"][1])
            self.assertEqual(sessions[-1], self.session(agent, cwd=self.worktree))
        self.assertNotEqual(*sessions)
        result = subprocess.run(["ab-worktree", "codex", "--instance", "worker-2", "snapshot"],
                                cwd=self.worktree, env=dict(self.env, PATH=f"{self.installed_bin}:{self.env['PATH']}"),
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.records()[-1]["args"][1],
                         self.session("codex", instance="worker-2", cwd=self.worktree))
        self.assertEqual((home / ".local/share/awl-browser/browser.md").read_bytes(),
                         (ROOT / "docs/browser.md").read_bytes())

    def test_personal_wrappers_and_instruction_bytes_and_modes_are_preserved(self):
        wrapper = self.installed_bin / "ab-codex"
        personal = b"#!/bin/sh\nprintf personal\\n\n"
        self.write(wrapper, personal)
        wrapper.chmod(0o700)
        originals = {}
        for path in (self.codex_home / "AGENTS.md", self.pi_home / "AGENTS.md"):
            originals[path] = b"# Personal rules\r\nDo not edit these.\xff"
            self.write(path, originals[path])
            path.chmod(0o600)
        result = self.install()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Preserved shortcut:", result.stdout)
        self.assertEqual(wrapper.read_bytes(), personal)
        self.assertEqual(wrapper.stat().st_mode & 0o777, 0o700)
        for path, original in originals.items():
            self.assertTrue(path.read_bytes().startswith(original))
            self.assertEqual(path.read_bytes().count(b"<!-- AWL browser helper -->"), 1)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertIn(str(self.installed_bin / "ab-worktree").encode(), path.read_bytes())
        self.assertEqual(self.install().returncode, 0)
        self.assertEqual(wrapper.read_bytes(), personal)

    def test_codex_override_and_pi_fallback_are_integrated_without_shadowing(self):
        base = self.codex_home / "AGENTS.md"
        override = self.codex_home / "AGENTS.override.md"
        fallback = self.pi_home / "CLAUDE.md"
        self.write(base, b"base rules")
        self.write(override, b"override rules")
        self.write(fallback, b"Pi personal rules")
        result = self.install()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(base.read_bytes(), b"base rules")
        self.assertIn(b"<!-- AWL browser helper -->", override.read_bytes())
        self.assertIn(b"<!-- AWL browser helper -->", fallback.read_bytes())
        self.assertFalse((self.pi_home / "AGENTS.md").exists())

    def test_empty_codex_override_does_not_shadow_base_but_empty_pi_file_does(self):
        override = self.codex_home / "AGENTS.override.md"
        pi_upper = self.pi_home / "AGENTS.MD"
        self.write(override, b" \n")
        self.write(pi_upper, b"")
        self.write(self.pi_home / "CLAUDE.md", b"lower priority")
        result = self.install()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(override.read_bytes(), b" \n")
        self.assertTrue((self.codex_home / "AGENTS.md").exists())
        self.assertIn(b"<!-- AWL browser helper -->", pi_upper.read_bytes())
        self.assertFalse((self.pi_home / "AGENTS.md").exists())

    def test_custom_homes_and_destinations_and_absolute_shell_quoted_pointer(self):
        env = dict(self.env, CODEX_HOME=str(self.base / "custom codex"),
                   PI_CODING_AGENT_DIR=str(self.base / "custom pi"))
        custom_bin = self.base / "bin with 'quote"
        data = self.base / "custom data"
        result = self.install("--bin-dir", str(custom_bin), "--data-dir", str(data), env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        for agent, home in (("codex", env["CODEX_HOME"]), ("pi", env["PI_CODING_AGENT_DIR"])):
            text = (Path(home) / "AGENTS.md").read_text()
            self.assertIn(f"Use {shlex.quote(str(custom_bin / 'ab-worktree'))} {agent}", text)
            self.assertIn(str(data / "browser.md"), text)
        self.assertFalse(self.codex_home.exists())
        self.assertEqual(self.install("--bin-dir", str(custom_bin), "--data-dir", str(data), env=env).returncode, 0)

    def test_existing_managed_file_conflict_stops_before_instruction_writes(self):
        helper = self.installed_bin / "ab-worktree"
        self.write(helper, b"personal helper")
        result = self.install()
        self.assertEqual(result.returncode, 1)
        self.assertIn("Preserving different existing file", result.stderr)
        self.assertEqual(helper.read_bytes(), b"personal helper")
        self.assertFalse(self.codex_home.exists())
        self.assertFalse(self.pi_home.exists())
        self.assertFalse((self.installed_bin / "ab-pi").exists())

    def test_guide_conflict_and_nonexecutable_helper_are_not_overwritten(self):
        guide = Path(self.env["HOME"]) / ".local/share/awl-browser/browser.md"
        self.write(guide, b"personal guide")
        result = self.install()
        self.assertEqual(result.returncode, 1)
        self.assertIn("Preserving different existing file", result.stderr)
        self.assertEqual(guide.read_bytes(), b"personal guide")
        self.assertFalse(self.installed_bin.exists())
        guide.unlink()
        helper = self.installed_bin / "ab-worktree"
        self.write(helper, HELPER.read_bytes())
        helper.chmod(0o600)
        result = self.install()
        self.assertEqual(result.returncode, 1)
        self.assertIn("not executable", result.stderr)
        self.assertEqual(helper.stat().st_mode & 0o777, 0o600)
        self.assertFalse(self.codex_home.exists())

    def test_hardlinked_instruction_and_alias_of_shared_home_are_refused(self):
        original = self.base / "rules"
        original.write_bytes(b"personal rules")
        self.codex_home.mkdir(parents=True)
        os.link(original, self.codex_home / "AGENTS.md")
        result = self.install()
        self.assertEqual(result.returncode, 1)
        self.assertIn("Refusing non-regular file", result.stderr)
        self.assertEqual(original.read_bytes(), b"personal rules")
        self.assertFalse(self.installed_bin.exists())
        (self.codex_home / "AGENTS.md").unlink()
        alias = self.base / "aliased home"
        alias.symlink_to(self.codex_home, target_is_directory=True)
        result = self.install("--pi-home", str(alias))
        self.assertEqual(result.returncode, 1)
        self.assertIn("destinations must differ", result.stderr)
        self.assertFalse(self.installed_bin.exists())

    def test_pi_override_preserves_lower_priority_rules_and_explicit_homes_win(self):
        home = self.base / "explicit pi"
        override = home / "AGENTS.override.md"
        base = home / "AGENTS.md"
        self.write(override, b"Pi override")
        self.write(base, b"Pi base rules")
        result = self.install("--pi-home", str(home), "--codex-home", str(self.codex_home),
                              env=dict(self.env, PI_CODING_AGENT_DIR=str(self.base / "ignored pi"),
                                       CODEX_HOME=str(self.base / "ignored codex")))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(override.read_bytes().startswith(b"Pi override"))
        self.assertIn(b"<!-- AWL browser helper -->", override.read_bytes())
        self.assertEqual(base.read_bytes(), b"Pi base rules")
        self.assertFalse((self.base / "ignored pi").exists())
        self.assertFalse((self.base / "ignored codex").exists())

    def test_symlinked_instruction_and_dangling_shortcut_are_preserved(self):
        target = self.base / "external rules"
        target.write_bytes(b"unchanged")
        self.codex_home.mkdir(parents=True)
        (self.codex_home / "AGENTS.md").symlink_to(target)
        result = self.install()
        self.assertEqual(result.returncode, 1)
        self.assertIn("Refusing non-regular file", result.stderr)
        self.assertEqual(target.read_bytes(), b"unchanged")
        self.assertFalse(self.installed_bin.exists())
        (self.codex_home / "AGENTS.md").unlink()
        self.installed_bin.mkdir(parents=True)
        alias = self.installed_bin / "ab-codex"
        alias.symlink_to(self.base / "missing")
        result = self.install()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(os.readlink(alias), str(self.base / "missing"))

    def test_edited_or_malformed_instruction_marker_stops_all_writes(self):
        path = self.pi_home / "AGENTS.md"
        for content in (b"<!-- AWL browser helper -->\nedited",
                        b"<!-- /AWL browser helper -->", b"<!-- AWL browser helper -->" * 2):
            with self.subTest(content=content):
                self.write(path, content)
                result = self.install()
                self.assertEqual(result.returncode, 1)
                self.assertIn("instruction block differs", result.stderr)
                self.assertEqual(path.read_bytes(), content)
                self.assertFalse(self.installed_bin.exists())
                self.assertFalse(self.codex_home.exists())

    def test_shared_instruction_destination_is_refused(self):
        home = self.base / "shared-home"
        result = self.install("--codex-home", str(home), "--pi-home", str(home))
        self.assertEqual(result.returncode, 1)
        self.assertIn("destinations must differ", result.stderr)
        self.assertFalse(self.installed_bin.exists())
        self.assertFalse(home.exists())


if __name__ == "__main__":
    unittest.main()
