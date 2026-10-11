import copy
from dataclasses import replace
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError

from task_start import TaskError
from task_start import cli
from task_start.agent import LaunchResult
from task_start.config import (AgentConfig, IssueStructureConfig, LocalConfig, Project,
                              load_local, load_projects, repository_path, resolve_project)
from task_start.linear import Issue, Linear
from task_start.contexts import ContextRegistry
from task_start.metrics import MetricsStore
from task_start.preparation import check_issue_structure
from task_start.workspace import Git, Herdr, Workspace, branch_name, run


PROJECT = Project("Example Project", "example-repo", "main")
ISSUE = Issue("issue-id", "DEV-7", "Add ingestion CLI", "Example Project", "todo", "Todo", "started",
              description="+++ Agent instructions\n\nImplement the ingestion CLI and validate it.\n\n+++",
              state_type="unstarted")
# Deliberately not an API key; no real credentials are used by these tests.
LOCAL = LocalConfig(Path("/projects"), "test-placeholder", AgentConfig("codex", "gpt-6-astra", "high"))


def issue_data():
    return {"issue": {
        "id": "issue-id", "identifier": "DEV-7", "title": "Add ingestion CLI",
        "description": ISSUE.description,
        "project": {"id": "project-id", "name": "Example Project"},
        "state": {"id": "todo", "name": "Todo", "type": "unstarted"},
        "team": {"id": "team-id", "states": {
            "nodes": [{"id": "todo", "name": "Todo"}, {"id": "started", "name": "In Progress"}],
            "pageInfo": {"hasNextPage": False},
        }},
    }}


class ParsingTests(unittest.TestCase):
    def test_start(self):
        args = cli.parser().parse_args(["start", "DEV-7"])
        self.assertEqual((args.command, args.issue), ("start", "DEV-7"))
        self.assertEqual(cli.parser().parse_args(["start", "dev-7"]).issue, "DEV-7")

    def test_invalid_identifiers_and_commands(self):
        for args in (["start", value] for value in ["DEV", "DEV-0", "DEV-07", "-7", "../DEV-7", "DEV-7;ls", "DEV-7\n"]):
            with self.subTest(args=args), patch("sys.stderr", new=io.StringIO()), self.assertRaises(SystemExit):
                cli.parser().parse_args(args)
        for command in ["unknown"]:
            with patch("sys.stderr", new=io.StringIO()), self.assertRaises(SystemExit):
                cli.parser().parse_args([command, "DEV-7"])

    def test_branch_names(self):
        for title, expected in [
            ("Add unified cross-platform source ingestion CLI", "add-unified-cross-platform-source-ingestion-cli"),
            ("  Fix: café / path..lock @{x} ", "fix-cafe-path-lock-x"),
            ("💡", ""), ("A" * 200, "a" * 100),
        ]:
            with self.subTest(title=title):
                self.assertEqual(branch_name("DEV-7", title), "dev-7" + ("-" + expected if expected else ""))


class ConfigTests(unittest.TestCase):
    def test_portable_registry_and_resolution(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "projects.toml"
            path.write_text('[projects.fixture]\nlinear_project = "Example Project"\n'
                            'repo_name = "example-repo"\nbase_branch = "main"\n')
            projects = load_projects(path)
        self.assertEqual(resolve_project(projects, "Example Project"), PROJECT)
        self.assertEqual(repository_path(LOCAL, PROJECT), Path("/projects/example-repo").resolve())
        with self.assertRaisesRegex(TaskError, "exactly one"):
            resolve_project(projects, "Unknown")
        with self.assertRaises(TaskError):
            resolve_project([PROJECT, PROJECT], "Example Project")

    def test_local_config(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "config.toml"
            path.write_text(f'projects_root = {json.dumps(temp)}\n[linear]\napi_key = "test-placeholder"\n')
            self.assertEqual(load_local(path), LocalConfig(Path(temp).resolve(), "test-placeholder"))
            self.assertNotIn("test-placeholder", repr(load_local(path)))

    def test_missing_local_config(self):
        with tempfile.TemporaryDirectory() as temp:
            with patch("task_start.config.Path.home", return_value=Path(temp)), self.assertRaisesRegex(TaskError, "Config missing"):
                load_local()

    def test_tilde_root_and_invalid_utf8(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "config.toml"
            path.write_text('projects_root = "~/projects"\n[linear]\napi_key = "test-placeholder"')
            self.assertEqual(load_local(path).projects_root, (Path.home() / "projects").resolve())
            path.write_bytes(b"\xff")
            with self.assertRaisesRegex(TaskError, "valid TOML"):
                load_local(path)

    def test_invalid_local_config(self):
        for content, message in [
            ('projects_root = "/projects"', "api_key"),
            ('projects_root = "/projects"\n[linear]\napi_key = ""', "api_key"),
            ('projects_root = "relative"\n[linear]\napi_key = "test-placeholder"', "absolute"),
            ('projects_root = "/projects"\n[linear]\napi_key = 42', "api_key"),
            ('[linear]\napi_key = "test placeholder"', "whitespace"),
            ('[linear]\napi_key = "test-placeholder', "valid TOML"),
        ]:
            with self.subTest(content=content), tempfile.TemporaryDirectory() as temp:
                path = Path(temp) / "config.toml"
                path.write_text(content)
                with self.assertRaisesRegex(TaskError, message) as caught:
                    load_local(path)
                self.assertNotIn("test-placeholder", str(caught.exception))

    def test_invalid_registry(self):
        for name in ["../escape", "/absolute", "C:/projects", "a/b", "a\\b", ".."]:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temp:
                path = Path(temp) / "projects.toml"
                path.write_text('[projects.test]\nlinear_project = "Test"\nbase_branch = "main"\nrepo_name = ' + json.dumps(name))
                with self.assertRaises(TaskError):
                    load_projects(path)

    def test_issue_structure_toml_defaults_and_overrides(self):
        for section, expected in (
            ('', IssueStructureConfig()),
            ('[linear.issue_structure]', IssueStructureConfig()),
            ('[linear.issue_structure]\nmode = "required"', IssueStructureConfig('required')),
            ('[linear.issue_structure]\nmode = "ignore"', IssueStructureConfig('ignore')),
            ('[linear.issue_structure]\nblock_name = "  Build notes (α) [v2].*  "',
             IssueStructureConfig('warn', 'Build notes (α) [v2].*')),
        ):
            with self.subTest(section=section), tempfile.TemporaryDirectory() as temp:
                path = Path(temp) / 'config.toml'
                path.write_text(f'projects_root = {json.dumps(temp)}\n[linear]\napi_key = "test-placeholder"\n{section}\n')
                self.assertEqual(load_local(path).issue_structure, expected)

    def test_invalid_issue_structure_configuration(self):
        policies = [False, '', [], {'unknown': 'value'}]
        policies += [dict(mode=value) for value in ('', 'WARN', 'unknown', 1, False, [], {})]
        policies += [dict(mode=mode, block_name=value)
                     for mode in ('required', 'warn', 'ignore')
                     for value in ('', '  ', '\t', 'Name\n', 'Two\rlines', 'With\ttab', 'NUL\0', 'DEL\x7f', 1, False, [], {})]
        for policy in policies:
            with self.subTest(policy=policy), patch('task_start.config.read_toml', return_value=dict(
                    projects_root='/projects', linear=dict(api_key='test-placeholder', issue_structure=policy))):
                with self.assertRaisesRegex(TaskError, 'linear.issue_structure') as caught:
                    load_local()
                self.assertNotIn('test-placeholder', str(caught.exception))
                # Workspace-only preparation and cleanup skip execution settings.
                self.assertEqual(load_local(no_agent=True).issue_structure, IssueStructureConfig())


class IssueStructureTests(unittest.TestCase):
    def test_ignore_does_not_access_issue_or_search_structure(self):
        with patch('task_start.preparation.re.search') as search, patch('sys.stderr', new_callable=io.StringIO) as stderr:
            check_issue_structure(object(), IssueStructureConfig('ignore'))
        search.assert_not_called()
        self.assertEqual(stderr.getvalue(), '')

    def test_only_literal_configured_collapsed_marker_is_recognized(self):
        name = 'Build notes (α) [v2].*'
        for mode in ('required', 'warn'):
            for description in (f'+++ {name}\n', f'>>>\t{name} \t\r\n',
                                f'Unstructured guidance\n+++{name}\nContents need no parsing'):
                with self.subTest(mode=mode, description=description), patch('sys.stderr', new_callable=io.StringIO) as stderr:
                    check_issue_structure(replace(ISSUE, description=description), IssueStructureConfig(mode, name))
                    self.assertEqual(stderr.getvalue(), '')
        for description in (ISSUE.description, f'# {name}', f'Please use +++ {name}',
                            '+++ Build notes α v2abc', f'+++ {name} extra', f'+++ {name.lower()}'):
            with self.subTest(description=description), self.assertRaisesRegex(TaskError, 'Required by'):
                check_issue_structure(replace(ISSUE, description=description), IssueStructureConfig('required', name))


class LinearTests(unittest.TestCase):
    def setUp(self):
        self.linear = Linear("test-placeholder")
        self.sleep = self.enterContext(patch("task_start.linear.time.sleep"))

    def test_transient_reads_retry_with_bounded_sanitized_backoff(self):
        for code in (408, 429, 500, 502, 503, 504):
            with self.subTest(code=code):
                self.sleep.reset_mock()
                errors = [HTTPError('private-url', code, 'private-body', {}, None) for _ in range(2)]
                with patch('task_start.linear.urlopen', side_effect=[*errors,
                        io.BytesIO(json.dumps({'data': issue_data()}).encode())]) as transport, \
                        patch('sys.stderr', new_callable=io.StringIO) as stderr:
                    self.assertEqual(self.linear.get_issue('DEV-7'), ISSUE)
                self.assertEqual(transport.call_count, 3)
                self.assertEqual([c.args[0] for c in self.sleep.call_args_list], [0.5, 1.0])
                self.assertIn('retry 2/2', stderr.getvalue())
                self.assertNotIn('private', stderr.getvalue())

    def test_persistent_reads_stop_and_authentication_never_retries(self):
        for code, attempts in ((503, 3), (401, 1), (403, 1), (400, 1), (404, 1)):
            with self.subTest(code=code), patch('task_start.linear.urlopen',
                    side_effect=HTTPError('private-url', code, 'private-body', {}, None)) as transport, \
                    patch('sys.stderr', new_callable=io.StringIO), self.assertRaises(TaskError) as error:
                self.linear.get_issue('DEV-7')
            self.assertEqual(transport.call_count, attempts)
            self.assertNotIn('private', str(error.exception))

    def test_uncertain_writes_are_never_retried(self):
        for failure in (HTTPError('private-url', 503, 'private-body', {}, None), TimeoutError()):
            self.sleep.reset_mock()
            with patch('task_start.linear.urlopen', side_effect=failure) as transport, self.assertRaises(TaskError):
                self.linear.start(ISSUE)
            self.assertEqual(transport.call_count, 1)
            self.sleep.assert_not_called()

    def test_read_current_state_after_lost_update_avoids_duplicate_mutation(self):
        current = issue_data()
        current['issue']['state'] = dict(id='started', name='In Progress', type='started')
        with patch('task_start.linear.urlopen', side_effect=[
                HTTPError('private-url', 503, 'lost update response', {}, None),
                io.BytesIO(json.dumps({'data': current}).encode())]) as transport:
            with self.assertRaises(TaskError):
                self.linear.start(ISSUE)
            self.linear.start(self.linear.get_issue('DEV-7'))
        queries = [json.loads(c.args[0].data)['query'] for c in transport.call_args_list]
        self.assertEqual(len(queries), 2)
        self.assertTrue(queries[0].lstrip().startswith('mutation'))
        self.assertTrue(queries[1].lstrip().startswith('query'))
        self.sleep.assert_not_called()

    def response(self, payload):
        return patch("task_start.linear.urlopen", return_value=io.BytesIO(json.dumps(payload).encode()))

    def test_issue_retrieval_http_boundary(self):
        with self.response({"data": issue_data()}) as mocked:
            self.assertEqual(self.linear.get_issue("DEV-7"), ISSUE)
        request = mocked.call_args.args[0]
        self.assertEqual(request.full_url, "https://api.linear.app/graphql")
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.get_header("Authorization"), "test-placeholder")
        self.assertEqual(json.loads(request.data)["variables"], {"id": "DEV-7"})
        self.assertEqual(mocked.call_args.kwargs["timeout"], 30)

    def test_invalid_response(self):
        for payload in [[], {}, {"data": None}, {"errors": [{"message": "test-placeholder"}]},
                        {"data": issue_data(), "errors": [{"message": "partial failure"}]},
                        {"data": {}}, {"data": {"issue": []}}, {"data": {"issue": None}}]:
            with self.subTest(payload=payload), self.response(payload) as transport, self.assertRaises(TaskError) as caught:
                self.linear.get_issue("DEV-7")
            self.assertEqual(transport.call_count, 1)
            self.assertNotIn("test-placeholder", str(caught.exception))
        with patch("task_start.linear.urlopen", return_value=io.BytesIO(b"not json")), self.assertRaises(TaskError):
            self.linear.get_issue("DEV-7")

    def test_transport_errors_are_sanitized(self):
        for error in [HTTPError("url", 401, "test-placeholder", {}, None), URLError("test-placeholder"), TimeoutError()]:
            with patch("task_start.linear.urlopen", side_effect=error), \
                    patch('sys.stderr', new_callable=io.StringIO), self.assertRaises(TaskError) as caught:
                self.linear.get_issue("DEV-7")
            self.assertNotIn("test-placeholder", str(caught.exception))

    def test_missing_or_malformed_issue_fields(self):
        for key in ["project", "state", "team", "title", "identifier"]:
            data = issue_data()
            data["issue"][key] = None
            with self.subTest(key=key), self.response({"data": data}), self.assertRaises(TaskError):
                self.linear.get_issue("DEV-7")
        for nodes in [[], [{"id": "a", "name": "In Progress"}, {"id": "b", "name": "In Progress"}]]:
            data = issue_data()
            data["issue"]["team"]["states"]["nodes"] = nodes
            with self.response({"data": data}), self.assertRaisesRegex(TaskError, "exactly one"):
                self.linear.get_issue("DEV-7")
        data = issue_data()
        data["issue"]["team"]["states"]["pageInfo"]["hasNextPage"] = True
        with self.response({"data": data}), self.assertRaises(TaskError):
            self.linear.get_issue("DEV-7")

    def test_status_update_only_changes_state(self):
        update = {"issueUpdate": {"success": True, "issue": {"id": ISSUE.id, "state": {"id": "started", "name": "In Progress"}}}}
        with self.response({"data": update}) as mocked:
            self.linear.start(ISSUE)
        request = json.loads(mocked.call_args.args[0].data)
        self.assertEqual(request["variables"], {"id": ISSUE.id, "state": "started"})
        self.assertIn("input: {stateId: $state}", request["query"])
        with patch.object(self.linear, "request") as mocked:
            self.linear.start(replace(ISSUE, state_name="In Progress", state_id="started"))
            mocked.assert_not_called()
        for data in [{}, {"issueUpdate": {"success": False}}, {"issueUpdate": None}]:
            with self.response({"data": data}), self.assertRaises(TaskError):
                self.linear.start(ISSUE)


class GitTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name).resolve()
        self.enterContext(patch("task_start.contexts.registry_path", return_value=self.repo / "contexts.sqlite3"))
        (self.repo / ".git").mkdir()
        self.git = Git(self.repo)
        self.answers = {
            ("rev-parse", "--is-inside-work-tree"): "true\n",
            ("rev-parse", "--show-toplevel"): str(self.repo) + "\n",
            ("rev-parse", "--absolute-git-dir"): str(self.repo / ".git"),
            ("rev-parse", "--path-format=absolute", "--git-common-dir"): str(self.repo / ".git"),
            ("for-each-ref", "--format=%(refname)", "refs/heads"): "refs/heads/main\n",
            ("rev-parse", "--symbolic-full-name", "HEAD"): "refs/heads/main\n",
            ("config", "--default", "", "--get", "branch.main.remote"): "origin\n",
            ("config", "--default", "", "--get", "branch.main.merge"): "refs/heads/main\n",
            ("rev-parse", "--verify", "FETCH_HEAD^{commit}"): "abc123\n",
            ("rev-parse", "--verify", "HEAD^{commit}"): "abc123\n",
            ("rev-list", "--left-right", "--count", "HEAD...abc123"): "0\t2\n",
        }
        def fake(args):
            self.assertEqual(args[:3], ["git", "-C", str(self.repo)])
            return self.answers.get(tuple(args[3:]), "")
        self.runner = self.enterContext(patch("task_start.workspace.run", side_effect=fake))

    def test_fast_forward_update(self):
        self.git.update_base("main")
        calls = [call.args[0][3:] for call in self.runner.call_args_list]
        fetch = ["fetch", "--no-tags", "--no-recurse-submodules", "--refmap=", "origin", "refs/heads/main"]
        self.assertIn(fetch, calls)
        merge = ["-c", "branch.main.mergeOptions=", "merge", "--ff-only", "--no-squash",
                 "--no-autostash", "--no-overwrite-ignore", "abc123"]
        self.assertIn(merge, calls)
        self.assertLess(calls.index(fetch), calls.index(merge))
        self.assertGreater(calls.index(["rev-parse", "--verify", "HEAD^{commit}"]), calls.index(merge))
        self.assertEqual(calls[-1], ["status", "--porcelain", "--untracked-files=all", "--ignore-submodules=none"])

    def test_unsafe_base_stops(self):
        cases = [
            (("for-each-ref", "--format=%(refname)", "refs/heads"), "refs/heads/other\n"),
            (("rev-parse", "--symbolic-full-name", "HEAD"), "refs/heads/other\n"),
            (("rev-parse", "--symbolic-full-name", "HEAD"), "HEAD\n"),
            (("status", "--porcelain", "--untracked-files=all", "--ignore-submodules=none"), "?? notes\n"),
            (("rev-parse", "--path-format=absolute", "--git-common-dir"), str(self.repo / "other")),
            (("config", "--default", "", "--get", "branch.main.remote"), ""),
            (("config", "--default", "", "--get", "branch.main.remote"), "."),
            (("config", "--default", "", "--get", "branch.main.merge"), "refs/heads/other"),
            (("rev-list", "--left-right", "--count", "HEAD...abc123"), "1\t2"),
            (("rev-list", "--left-right", "--count", "HEAD...abc123"), "1\t0"),
        ]
        for key, answer in cases:
            with self.subTest(key=key, answer=answer), patch.dict(self.answers, {key: answer}), self.assertRaises(TaskError):
                self.git.update_base("main")
        self.assertFalse(any("merge" in call.args[0][3:] for call in self.runner.call_args_list))

    def test_wrong_head_after_merge_stops_before_herdr(self):
        self.answers[("rev-parse", "--verify", "HEAD^{commit}")] = "old-head\n"
        with patch("task_start.cli.load_local", return_value=LOCAL), \
                patch("task_start.cli.load_projects", return_value=[PROJECT]), \
                patch("task_start.cli.Linear") as linear, \
                patch("task_start.cli.Git", return_value=self.git), \
                patch("task_start.cli.Herdr") as herdr:
            linear.return_value.get_issue.return_value = ISSUE
            with self.assertRaisesRegex(TaskError, "did not reach"):
                cli.start("DEV-7", no_agent=True)
            herdr.assert_not_called()
            linear.return_value.start.assert_not_called()

    def test_dirty_checkout_after_merge_is_rejected(self):
        fake = self.runner.side_effect

        def dirty_merge(args):
            if "merge" in args[3:]:
                self.answers[("status", "--porcelain", "--untracked-files=all", "--ignore-submodules=none")] = "M  tracked.txt\n"
            return fake(args)

        self.runner.side_effect = dirty_merge
        with self.assertRaisesRegex(TaskError, "local modifications"):
            self.git.update_base("main")

    def test_unfinished_operation(self):
        (self.repo / ".git" / "MERGE_HEAD").touch()
        with self.assertRaisesRegex(TaskError, "unfinished"):
            self.git.update_base("main")

    def test_branch_identity_and_worktree_parsing(self):
        self.answers[("for-each-ref", "--format=%(refname:strip=2)", "refs/heads")] = "main\ndev-7-old-title\ndev-70-other\n"
        self.assertEqual(self.git.branches("DEV-7"), ["dev-7-old-title"])
        self.answers[("worktree", "list", "--porcelain", "-z")] = "worktree /path with spaces\0HEAD abc\0branch refs/heads/dev-7-old-title\0\0"
        self.assertEqual(self.git.worktrees()[0]["worktree"], "/path with spaces")


class HerdrTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name).resolve()
        self.tree_path = self.repo / "existing"
        self.tree_path.mkdir()
        self.herdr = Herdr(self.repo)
        self.git = Mock()
        self.git.branches.return_value = []
        self.git.remote_branches.return_value = []
        self.git.worktrees.return_value = []
        self.git.resolve_scope.return_value = None
        self.branch = "dev-7-add-ingestion-cli"
        self.tree = {"branch": self.branch, "path": str(self.tree_path), "label": "DEV-7",
                     "is_linked_worktree": True, "is_bare": False, "is_detached": False,
                     "is_prunable": False, "open_workspace_id": "w1"}
        self.listing = {"type": "worktree_list", "source": {"repo_root": str(self.repo)}, "worktrees": []}

    def result(self, operation):
        return {"type": "worktree_" + operation, "worktree": copy.deepcopy(self.tree),
                "workspace": {"focused": True, "workspace_id": "w1", "worktree": {
                    "repo_root": str(self.repo), "checkout_path": str(self.tree_path)}},
                "tab": {"workspace_id": "w1", "tab_id": "w1:t5"},
                "root_pane": {"workspace_id": "w1", "tab_id": "w1:t5", "pane_id": "w1:p9"}}

    def prepare(self):
        return self.herdr.prepare(self.git, "main", self.branch, "DEV-7")

    def existing(self):
        self.listing["worktrees"] = [self.tree]
        self.git.branches.return_value = [self.tree["branch"]]
        self.git.worktrees.return_value = [{"worktree": str(self.tree_path), "branch": "refs/heads/" + self.tree["branch"]}]

    def test_create_command(self):
        self.git.worktrees.side_effect = [[], [{"worktree": str(self.tree_path), "branch": "refs/heads/" + self.branch}]]
        with patch("task_start.workspace.run", side_effect=[json.dumps({"result": self.listing}), json.dumps({"result": self.result("created")})]) as runner:
            self.assertEqual(self.prepare(), Workspace(self.branch, self.tree_path, "w1", "w1:t5", "w1:p9", "workspace created and focused"))
        self.assertEqual(runner.call_args_list[0].args[0], ["herdr", "worktree", "list", "--cwd", str(self.repo)])
        self.assertEqual(runner.call_args_list[1].args[0], ["herdr", "worktree", "create", "--cwd", str(self.repo), "--base", "main", "--branch", self.branch, "--label", "DEV-7", "--focus"])

    def test_reopen_even_after_title_changed(self):
        self.tree["branch"] = "dev-7-previous-title"
        self.existing()
        with patch("task_start.workspace.run", side_effect=[json.dumps({"result": self.listing}), json.dumps({"result": self.result("opened")})]) as runner:
            self.assertEqual(self.prepare().branch, "dev-7-previous-title")
        self.assertEqual(runner.call_args.args[0], ["herdr", "worktree", "open", "--cwd", str(self.repo), "--path", str(self.tree_path), "--label", "DEV-7", "--focus"])

    def test_branch_only_and_remote_only_refused(self):
        for local, remote in [([self.branch], []), ([], [self.branch])]:
            self.git.branches.return_value = local
            self.git.remote_branches.return_value = remote
            with patch.object(self.herdr, "command", return_value=self.listing) as command, self.assertRaises(TaskError):
                self.prepare()
            command.assert_called_once_with("list")

    def test_ambiguous_or_unusable_worktrees_refused(self):
        for field, value in [("is_prunable", True), ("is_detached", True), ("is_linked_worktree", False), ("branch", "dev-8-other")]:
            self.existing()
            with patch.dict(self.tree, {field: value}), patch.object(self.herdr, "command", return_value=self.listing) as command, self.assertRaises(TaskError):
                self.prepare()
            command.assert_called_once_with("list")
        self.existing()
        self.listing["worktrees"].append(copy.deepcopy(self.tree))
        with patch.object(self.herdr, "command", return_value=self.listing), self.assertRaises(TaskError):
            self.prepare()

    def test_malformed_or_unfocused_herdr_response(self):
        for output in ["not json", "[]", '{"error": {}}', '{"result": {"type": "other"}}']:
            with patch("task_start.workspace.run", return_value=output), self.assertRaises(TaskError):
                self.prepare()
        result = self.result("created")
        result["workspace"]["focused"] = False
        with patch.object(self.herdr, "command", side_effect=[self.listing, result]), self.assertRaises(TaskError):
            self.prepare()

    def test_mismatched_or_locked_git_worktree_refused(self):
        for changes in [{"worktree": str(self.repo / "another")}, {"locked": "maintenance"}]:
            self.existing()
            self.git.worktrees.return_value[0].update(changes)
            with patch.object(self.herdr, "command", return_value=self.listing) as command, self.assertRaises(TaskError):
                self.prepare()
            command.assert_called_once_with("list")


class OrchestrationTests(unittest.TestCase):
    def setUp(self):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        self.enterContext(patch("task_start.contexts.registry_path", return_value=Path(directory) / "contexts.sqlite3"))
        self.enterContext(patch("task_start.cli.load_local", return_value=LOCAL))
        # This suite tests orchestration; registry/Herdr effects have their own tests.
        self.enterContext(patch("task_start.cli.launch_registered",
                                side_effect=lambda agent, execution: agent.launch(execution)))
        self.enterContext(patch("task_start.cli.load_projects", return_value=[PROJECT]))
        self.linear = self.enterContext(patch("task_start.cli.Linear")).return_value
        self.linear.get_issue.return_value = ISSUE
        self.git = self.enterContext(patch("task_start.cli.Git")).return_value
        self.herdr = self.enterContext(patch("task_start.cli.Herdr")).return_value
        self.workspace = Workspace("dev-7-add-ingestion-cli", Path("/selected/worktree"), "w7", "w7:t5", "w7:p8", "workspace created and focused")
        self.herdr.prepare.return_value = self.workspace
        self.agent = self.enterContext(patch("task_start.cli.adapter_for")).return_value
        self.agent.launch.return_value = LaunchResult("codex", "w7:p8", "Codex working in w7:p8")

    def test_order_and_concise_output(self):
        operations = Mock()
        operations.attach_mock(self.git.update_base, "base")
        operations.attach_mock(self.herdr.prepare, "workspace")
        operations.attach_mock(self.linear.start, "status")
        output = cli.start("DEV-7")
        self.assertEqual([call[0] for call in operations.mock_calls], ["base", "workspace", "status"])
        self.assertEqual(len(output.splitlines()), 7)
        self.assertIn("Linear: In Progress", output)
        self.assertNotIn("test-placeholder", output)
        metric, = MetricsStore(ContextRegistry().path).read()
        self.assertEqual((metric["role"], metric["status"]), ("implementation", "dispatched"))
        self.assertEqual((metric["agent"], metric["model"], metric["mode"]), ("codex", "gpt-6-astra", "high"))
        self.assertIsNone(metric["finished_at"])
        self.assertIsNone(metric["wall_clock_seconds"])

    def test_metrics_failure_does_not_block_interactive_dispatch(self):
        with patch("task_start.metrics.MetricsStore.write", side_effect=OSError("full disk")):
            output = cli.start("DEV-7")
        self.agent.launch.assert_called_once()
        self.assertIn("Codex working", output)

    def test_required_missing_block_refuses_before_any_workflow_mutation(self):
        self.enterContext(patch('task_start.cli.load_local', return_value=replace(
            LOCAL, issue_structure=IssueStructureConfig('required'))))
        for kind in ("codex", "pi"):
            for description in (None, "", "Implement the ingestion CLI.",
                                "Mention Agent instructions in the documentation.",
                                "## Agent instructions\nImplement it.",
                                "+++ Reviewer instructions\nReview the changes.\n+++",
                                "+++ Agent instructions for reviewers\nReview the changes.\n+++"):
                with self.subTest(kind=kind, description=description):
                    data = issue_data()
                    data["issue"]["description"] = description
                    with patch.object(Linear, "request", return_value=data):
                        self.linear.get_issue.return_value = Linear("placeholder").get_issue("DEV-7")
                    with patch("task_start.cli.Git") as git, patch("task_start.cli.Herdr") as herdr, \
                            patch("task_start.cli.launch_registered") as launch, \
                            self.assertRaisesRegex(TaskError, "DEV-7.*Agent instructions.*mode=required"):
                        cli.start("DEV-7", agent_kind=kind)
                    git.assert_not_called()
                    herdr.assert_not_called()
                    self.linear.start.assert_not_called()
                    launch.assert_not_called()
                    self.agent.launch.assert_not_called()

    def test_agent_instructions_representations_preserve_exact_handoff(self):
        for description in (
                ISSUE.description,
                "## Outcome\n\nAdd ingestion.\n\n>>> Agent instructions\n\nImplement and validate.\n\n>>>",
                "+++Agent instructions\nImplement and validate.\n+++",
                "Fresh task α\r\n\r\n>>> Agent instructions \t\r\n\r\n  Exact whitespace.\r\n\r\n>>>\r\n"):
            for mode in ('required', 'warn', 'ignore'):
                with self.subTest(description=description, mode=mode), patch('task_start.cli.load_local', return_value=replace(
                        LOCAL, issue_structure=IssueStructureConfig(mode))), patch('sys.stderr', new_callable=io.StringIO) as stderr:
                    data = issue_data()
                    data["issue"]["description"] = description
                    with patch.object(Linear, "request", return_value=data):
                        issue = Linear("placeholder").get_issue("DEV-7")
                    self.linear.get_issue.return_value = issue
                    self.agent.reset_mock()
                    output = cli.start("DEV-7")
                    self.agent.launch.assert_called_once()
                    execution = self.agent.launch.call_args.args[0]
                    self.assertEqual(execution.issue, issue)
                    self.assertIn(f"Task:\n{description}\n\nWorkflow-owned implementation instructions:", execution.handoff)
                    self.assertIn('complete Linear issue above as the task source of truth', execution.handoff)
                    self.assertIn('Users may organize the issue description however they choose', execution.handoff)
                    self.assertIn('guidance wherever they appear in the issue', execution.handoff)
                    self.assertIn('take precedence over conflicting task content regardless of its formatting', execution.handoff)
                    self.assertEqual(stderr.getvalue(), '')
                    self.assertEqual(issue.labels, ())  # Publication classification is not a start prerequisite.
                    self.assertIn("Linear: In Progress", output)

    def test_required_missing_block_cli_error_explains_policy(self):
        self.enterContext(patch('task_start.cli.load_local', return_value=replace(
            LOCAL, issue_structure=IssueStructureConfig('required'))))
        self.linear.get_issue.return_value = replace(ISSUE, description="")
        with patch("sys.stderr", new=io.StringIO()) as stderr, patch("sys.stdout", new=io.StringIO()) as stdout:
            self.assertEqual(cli.main(["start", "DEV-7"]), 1)
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(stderr.getvalue(),
                         "task: Linear issue DEV-7 has no recognizable collapsed 'Agent instructions' block. "
                         "Required by linear.issue_structure.mode=required; add the configured block in Linear "
                         "or change the issue_structure policy before starting agent execution\n")

    def test_ordinary_issues_continue_with_default_warn_or_ignore_and_exact_handoff(self):
        for policy in (LOCAL.issue_structure, IssueStructureConfig('ignore')):
            for description in ('', 'Implement ingestion.\r\n  Preserve α whitespace.\n',
                                '## Agent instructions\nPlain headings work too.'):
                with self.subTest(policy=policy, description=description), patch('task_start.cli.load_local',
                        return_value=replace(LOCAL, issue_structure=policy)):
                    self.linear.get_issue.return_value = replace(ISSUE, description=description)
                    with patch('sys.stderr', new_callable=io.StringIO) as stderr, patch('sys.stdout', new_callable=io.StringIO) as stdout:
                        self.assertEqual(cli.main(['start', 'DEV-7']), 0)
                    self.assertIn('Linear: In Progress', stdout.getvalue())
                    if policy.mode == 'warn':
                        self.assertIn("warning: Linear issue DEV-7", stderr.getvalue())
                        self.assertIn("'Agent instructions'", stderr.getvalue())
                        self.assertIn('Continuing agent execution', stderr.getvalue())
                    else:
                        self.assertEqual(stderr.getvalue(), '')
                    self.assertIn(f'Task:\n{description}\n\nWorkflow-owned implementation instructions:',
                                  self.agent.launch.call_args.args[0].handoff)

    def test_custom_block_name_controls_start_check_without_rewriting_handoff(self):
        description = 'Requirements outside the block.\n+++ Build notes (α)\nGuidance\n+++\nMore constraints.'
        self.linear.get_issue.return_value = replace(ISSUE, description=description)
        with patch('task_start.cli.load_local', return_value=replace(LOCAL,
                issue_structure=IssueStructureConfig('required', 'Build notes (α)'))):
            cli.start('DEV-7')
        self.assertIn(f'Task:\n{description}\n\nWorkflow-owned implementation instructions:',
                      self.agent.launch.call_args.args[0].handoff)

    def test_invalid_configuration_and_unavailable_agent_fail_before_mutation(self):
        with patch('task_start.config.read_toml', return_value=dict(projects_root='/projects',
                linear=dict(api_key='test-placeholder', issue_structure=dict(mode='invalid')))), \
                patch('task_start.cli.load_local', side_effect=load_local):
            with self.assertRaisesRegex(TaskError, 'linear.issue_structure.mode'):
                cli.start('DEV-7')
        self.linear.get_issue.assert_not_called()
        with patch.object(self.agent, 'check_available', side_effect=TaskError('Agent unavailable')):
            with self.assertRaisesRegex(TaskError, 'unavailable'):
                cli.start('DEV-7')
        self.git.update_base.assert_not_called()
        self.herdr.prepare.assert_not_called()
        self.linear.start.assert_not_called()
        self.agent.launch.assert_not_called()

    def test_invalid_validation_config_fails_before_execution_mutation(self):
        with patch('task_start.config.read_toml', return_value=dict(projects_root='/projects',
                linear=dict(api_key='test-placeholder'), review=dict(validation=dict(strategy='invalid')))), \
                patch('task_start.cli.load_local', side_effect=load_local):
            with self.assertRaisesRegex(TaskError, 'review.validation.strategy'):
                cli.start('DEV-7')
        self.linear.get_issue.assert_not_called()
        self.git.update_base.assert_not_called()
        self.herdr.prepare.assert_not_called()
        self.linear.start.assert_not_called()
        self.agent.launch.assert_not_called()

    def test_no_agent_without_instructions_still_prepares_and_updates_status(self):
        self.enterContext(patch('task_start.cli.load_local', return_value=replace(
            LOCAL, issue_structure=IssueStructureConfig('required'))))
        issue = replace(ISSUE, description="")
        self.linear.get_issue.return_value = issue
        with patch("task_start.cli.adapter_for") as adapter, \
                patch("task_start.cli.launch_registered") as launch:
            output = cli.start("DEV-7", no_agent=True)
        self.git.update_base.assert_called_once_with(PROJECT.base_branch)
        self.herdr.prepare.assert_called_once()
        self.linear.start.assert_called_once_with(issue)
        adapter.assert_not_called()
        launch.assert_not_called()
        self.assertIn("skipped (--no-agent)", output)
        self.assertFalse(MetricsStore(ContextRegistry().path).directory.exists())

    def test_herdr_failure_does_not_update_linear(self):
        self.herdr.prepare.side_effect = TaskError("Herdr failed")
        with self.assertRaises(TaskError):
            cli.start("DEV-7")
        self.linear.start.assert_not_called()

    def test_git_failure_does_not_update_linear_or_call_herdr(self):
        self.git.update_base.side_effect = TaskError("dirty checkout")
        with self.assertRaises(TaskError):
            cli.start("DEV-7")
        self.linear.start.assert_not_called()
        self.herdr.prepare.assert_not_called()

    def test_status_failure_reports_workspace_ready(self):
        self.linear.start.side_effect = TaskError("Linear request failed")
        with self.assertRaisesRegex(TaskError, "Workspace ready"):
            cli.start("DEV-7")

    def test_expected_error_has_no_traceback(self):
        self.herdr.prepare.side_effect = TaskError("Herdr failed")
        with patch("sys.stderr", new=io.StringIO()) as stderr:
            self.assertEqual(cli.main(["start", "DEV-7"]), 1)
        self.assertEqual(stderr.getvalue(), "task: Herdr failed\n")


class ProcessTests(unittest.TestCase):
    def test_errors_do_not_expose_command_output(self):
        with patch("task_start.workspace.subprocess.run", return_value=subprocess.CompletedProcess([], 1, "", "credential-bearing URL")), self.assertRaises(TaskError) as caught:
            run(["git", "-C", "/repo", "fetch", "origin"])
        self.assertNotIn("credential-bearing", str(caught.exception))

    @unittest.skipUnless(os.name == "posix", "POSIX filesystem byte paths")
    def test_git_path_bytes_round_trip(self):
        raw_path = b"/repos/caf\xc3\xa9/undecodable-\xff\n"
        with patch("task_start.workspace.subprocess.run", return_value=subprocess.CompletedProcess([], 0, raw_path, b"")):
            self.assertEqual(os.fsencode(run(["git", "-C", "/repo", "rev-parse", "--show-toplevel"])), raw_path)

    def test_herdr_json_uses_utf8(self):
        payload = {"result": {"type": "worktree_list", "path": "/repos/café/naïve"}}
        encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        with patch("task_start.workspace.subprocess.run", return_value=subprocess.CompletedProcess([], 0, encoded, b"\xff")):
            self.assertEqual(Herdr(Path("/repo")).command("list"), payload["result"])

    def test_invalid_herdr_utf8_is_task_error(self):
        with patch("task_start.workspace.subprocess.run", return_value=subprocess.CompletedProcess([], 0, b"\xff", b"")):
            with self.assertRaisesRegex(TaskError, "UTF-8 JSON"):
                Herdr(Path("/repo")).command("list")

    def test_git_decoding_failure_is_task_error(self):
        with patch("task_start.workspace.subprocess.run", return_value=subprocess.CompletedProcess([], 0, b"\xff", b"")), \
                patch("task_start.workspace.os.fsdecode", side_effect=UnicodeDecodeError("ascii", b"\xff", 0, 1, "invalid byte")):
            with self.assertRaisesRegex(TaskError, "filesystem encoding"):
                run(["git", "-C", "/repo", "rev-parse", "--show-toplevel"])


class LocalGitIntegrationTests(unittest.TestCase):
    """Real Git in disposable directories, using a local fake remote; no network."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name).resolve()
        self.enterContext(patch("task_start.contexts.registry_path", return_value=root / "contexts.sqlite3"))
        self.repo, self.remote = root / "checkout", root / "upstream"
        # Isolate tests from user Git configuration, hooks, signing and identity.
        self.enterContext(patch.dict(os.environ, {
            "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "test@example.invalid",
            "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "test@example.invalid",
        }))
        self.command(root, "init", "--initial-branch=main", str(self.remote))
        (self.remote / ".gitignore").write_text("ignored.txt\n")
        (self.remote / "tracked.txt").write_text("initial\n")
        self.command(self.remote, "add", ".")
        self.command(self.remote, "commit", "-m", "initial")
        self.command(root, "clone", str(self.remote), str(self.repo))
        self.git = Git(self.repo)
        self.before = self.command(self.repo, "rev-parse", "HEAD")

    def command(self, repo, *args):
        return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()

    def advance_remote(self, filename="tracked.txt"):
        (self.remote / filename).write_text("upstream change\n")
        self.command(self.remote, "add", "--force", filename)
        self.command(self.remote, "commit", "-m", "advance")

    def test_fast_forward_and_up_to_date(self):
        self.advance_remote()
        self.git.update_base("main")
        self.assertEqual(self.command(self.repo, "rev-parse", "HEAD"), self.command(self.remote, "rev-parse", "HEAD"))
        self.assertEqual((self.repo / "tracked.txt").read_text(), "upstream change\n")
        self.git.update_base("main")

    def test_branch_squash_options_cannot_change_fast_forward(self):
        self.command(self.repo, "config", "branch.main.mergeOptions", "--squash")
        self.advance_remote()
        self.git.update_base("main")
        fetched = self.command(self.repo, "rev-parse", "FETCH_HEAD")
        self.assertNotEqual(fetched, self.before)
        self.assertEqual(self.command(self.repo, "rev-parse", "HEAD"), fetched)
        self.assertEqual(self.command(self.repo, "status", "--porcelain"), "")
        self.assertEqual((self.repo / "tracked.txt").read_text(), "upstream change\n")
        self.assertEqual(self.command(self.repo, "config", "branch.main.mergeOptions"), "--squash")
        self.git.update_base("main")
        self.assertEqual(self.command(self.repo, "status", "--porcelain"), "")

    @unittest.skipUnless(sys.platform.startswith("linux"), "Uses Linux C.UTF-8 startup and C/ASCII runtime locales")
    def test_non_ascii_paths_under_ascii_text_locale(self):
        repo = self.repo.parent / "café"
        tree = self.repo.parent / "naïve-worktree"
        self.command(self.repo.parent, "clone", str(self.remote), str(repo))
        self.command(repo, "worktree", "add", "-b", "dev-7-example", str(tree))
        self.advance_remote()
        # Python's filesystem codec is fixed at startup. Switching the runtime
        # locale reproduces ASCII subprocess text decoding with UTF-8 path bytes.
        script = """
import codecs
import json
import locale
from pathlib import Path
import subprocess
import sys
from unittest.mock import patch

sys.path.insert(0, sys.argv[1])
from task_start.workspace import Git, Herdr

locale.setlocale(locale.LC_CTYPE, "C")
assert sys.flags.utf8_mode == 0
assert codecs.lookup(locale.getencoding()).name == "ascii"
repo, tree = Path(sys.argv[2]), Path(sys.argv[3])
git = Git(repo)
git.update_base("main")
assert {Path(w["worktree"]) for w in git.worktrees()} == {repo, tree}
payload = {"result": {"type": "worktree_list", "path": str(tree)}}
encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
with patch("task_start.workspace.subprocess.run", return_value=subprocess.CompletedProcess([], 0, encoded, b"")):
    assert Herdr(repo).command("list")["path"] == str(tree)
"""
        env = dict(os.environ, LC_ALL="C.UTF-8", PYTHONUTF8="0", PYTHONCOERCECLOCALE="0")
        result = subprocess.run(
            [sys.executable, "-X", "utf8=0", "-c", script,
             str(Path(__file__).resolve().parent.parent), str(repo), str(tree)],
            capture_output=True, encoding="utf-8", errors="replace", env=env,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.command(repo, "rev-parse", "HEAD"), self.command(self.remote, "rev-parse", "HEAD"))
        self.assertEqual(self.command(repo, "status", "--porcelain"), "")

    def test_dirty_checkout_is_preserved(self):
        self.advance_remote()
        (self.repo / "tracked.txt").write_text("local work\n")
        with self.assertRaisesRegex(TaskError, "local modifications"):
            self.git.update_base("main")
        self.assertEqual(self.command(self.repo, "rev-parse", "HEAD"), self.before)
        self.assertEqual((self.repo / "tracked.txt").read_text(), "local work\n")

    def test_local_commits_and_divergence_are_preserved(self):
        self.command(self.repo, "commit", "--allow-empty", "-m", "local work")
        local = self.command(self.repo, "rev-parse", "HEAD")
        with self.assertRaisesRegex(TaskError, "local-only"):
            self.git.update_base("main")
        self.advance_remote()
        with self.assertRaisesRegex(TaskError, "diverges"):
            self.git.update_base("main")
        self.assertEqual(self.command(self.repo, "rev-parse", "HEAD"), local)

    def test_ignored_file_is_not_overwritten(self):
        (self.repo / "ignored.txt").write_text("local ignored work\n")
        self.advance_remote("ignored.txt")
        with self.assertRaises(TaskError):
            self.git.update_base("main")
        self.assertEqual(self.command(self.repo, "rev-parse", "HEAD"), self.before)
        self.assertEqual((self.repo / "ignored.txt").read_text(), "local ignored work\n")

    def test_entry_point_from_another_directory_and_symlink(self):
        entry = Path(__file__).resolve().parent.parent / "task"
        result = subprocess.run([sys.executable, str(entry), "--help"], cwd=self.repo, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        if os.name != "nt":
            link = self.repo.parent / "task-link"
            link.symlink_to(entry)
            result = subprocess.run([str(link), "--help"], cwd=self.repo, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
