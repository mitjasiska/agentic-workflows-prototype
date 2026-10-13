import copy
from dataclasses import replace
import io
import json
import os
from pathlib import Path
import unittest
from unittest.mock import Mock, call, patch
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit

from task_start import TaskError, cli
from task_start.agent import LaunchResult
from task_start.config import LocalConfig, Project
from task_start.github import check_history, repository_name
from task_start.preparation import prepare_task
from task_start.workspace import Git, Herdr
import test_task_start as baseline


class LifecycleTests(unittest.TestCase):
    setUp = baseline.HerdrTests.setUp
    existing = baseline.HerdrTests.existing
    result = baseline.HerdrTests.result
    prepare = baseline.HerdrTests.prepare

    def responses(self, action="opened"):
        # Prepare selects once with remote discovery, then rechecks local state
        # under admission before opening or creating the selected workspace.
        return patch.object(self.herdr, "command", side_effect=[self.listing, self.listing, self.result(action)])

    def test_historical_workspace_never_opened_or_deleted(self):
        self.existing()
        self.git.check_history.side_effect = TaskError("GitHub PR #1 is merged")
        with self.responses() as command, self.assertRaisesRegex(TaskError, "merged"):
            self.prepare()
        command.assert_called_once_with("list")
        self.git.check_history.assert_called_once_with("main", self.branch, existing=True)

    def test_unavailable_history_fails_before_open(self):
        self.existing()
        self.git.check_history.side_effect = TaskError("Cannot establish PR history")
        with self.responses() as command, self.assertRaisesRegex(TaskError, "history"):
            self.prepare()
        command.assert_called_once_with("list")

    def test_new_workspace_also_checks_branch_history(self):
        self.git.check_history.side_effect = TaskError("GitHub PR #1 is merged")
        with self.responses("created") as command, self.assertRaisesRegex(TaskError, "merged"):
            self.prepare()
        command.assert_called_once_with("list")
        self.git.check_history.assert_called_once_with("main", self.branch, existing=False)

    def test_two_slices_can_coexist_and_explicit_slice_is_reused(self):
        self.tree["branch"] = "dev-7-codex-handoff"
        self.existing()
        self.git.resolve_scope.return_value = "codex-handoff"
        other_path = self.repo / "other"
        other_path.mkdir()
        other = dict(self.tree, branch="dev-7-lifecycle", path=str(other_path), label="DEV-7 / lifecycle", open_workspace_id="w2")
        self.listing["worktrees"].append(other)
        self.git.branches.return_value.append(other["branch"])
        self.git.worktrees.return_value.append(dict(worktree=str(other_path), branch="refs/heads/" + other["branch"]))
        with self.responses() as command:
            workspace = self.herdr.prepare(self.git, "main", "dev-7-codex-handoff", "DEV-7", "codex-handoff")
        self.assertEqual(workspace.branch, "dev-7-codex-handoff")
        self.assertEqual(command.call_args_list, [call("list"), call("list"),
            call("open", "--path", str(self.tree_path), "--label", "DEV-7 / codex-handoff", "--focus")])
        self.git.save_scope.assert_called_once_with(self.tree_path, workspace.branch, "DEV-7", "codex-handoff")
        with self.responses() as command, self.assertRaisesRegex(TaskError, "Ambiguous.*codex-handoff.*lifecycle"):
            self.prepare()
        command.assert_called_once_with("list")

    def test_creating_second_slice_ignores_other_slice_remote_and_local(self):
        self.existing()
        old_tree = copy.deepcopy(self.tree)
        old_git = copy.deepcopy(self.git.worktrees.return_value)
        self.git.remote_branches.return_value = [self.branch]
        self.tree["branch"] = "dev-7-second"
        self.tree_path = self.repo / "second"
        self.tree_path.mkdir()
        self.tree["path"] = str(self.tree_path)
        self.listing["worktrees"] = [old_tree]
        self.git.worktrees.side_effect = [old_git, old_git,
            old_git + [dict(worktree=str(self.tree_path), branch="refs/heads/dev-7-second")]]
        with self.responses("created") as command:
            result = self.herdr.prepare(self.git, "main", "dev-7-second", "DEV-7", "second")
        self.assertEqual(result.branch, "dev-7-second")
        self.assertEqual(command.call_args_list, [call("list"), call("list"),
            call("create", "--base", "main", "--branch", "dev-7-second", "--label", "DEV-7 / second", "--focus")])
        self.git.save_scope.assert_called_once_with(self.tree_path, "dev-7-second", "DEV-7", "second")

    def test_single_slice_without_selector_is_unambiguous(self):
        self.tree["branch"] = "dev-7-slice"
        self.existing()
        self.git.resolve_scope.return_value = "slice"
        with self.responses() as command:
            selected = self.prepare()
        self.assertEqual(selected.branch, "dev-7-slice")
        self.assertEqual(selected.slice, "slice")
        self.assertEqual(command.call_args_list, [call("list"), call("list"),
            call("open", "--path", str(self.tree_path), "--label", "DEV-7 / slice", "--focus")])

    def test_unknown_scope_refuses_before_open_or_metadata_write(self):
        self.existing()
        self.git.resolve_scope.side_effect = TaskError("Workspace scope is unknown; rerun with explicit --slice")
        with self.responses() as command, self.assertRaisesRegex(TaskError, "explicit --slice"):
            self.prepare()
        self.assertEqual(command.call_args_list, [call("list"), call("list")])
        self.git.save_scope.assert_not_called()

    def test_scope_persistence_failure_never_returns_ready_workspace(self):
        self.existing()
        self.git.save_scope.side_effect = TaskError("Scope metadata could not be saved")
        linear = Mock()
        # Shared startup also claims the existing selection before updating base.
        with patch.object(self.herdr, "command", side_effect=[self.listing, self.listing, self.listing,
                          self.result("opened")]) as command, self.assertRaisesRegex(TaskError, "could not be saved"):
            prepare_task(baseline.ISSUE, baseline.PROJECT, linear, self.git, lambda: self.herdr,
                         check_configuration=Mock())
        self.assertEqual(command.call_args_list, [call("list"), call("list"), call("list"),
            call("open", "--path", str(self.tree_path), "--label", "DEV-7", "--focus")])
        self.git.save_scope.assert_called_once_with(self.tree_path, self.branch, "DEV-7", None)
        self.assertEqual(self.git.worktrees.call_count, 4)  # Claim, selection, admission, confirmation.
        linear.start.assert_not_called()

    def test_selection_changes_after_claim_or_before_admission_refuse_mutation(self):
        self.existing()
        changed = copy.deepcopy(self.listing)
        changed["worktrees"][0]["open_workspace_id"] = "replacement"
        for phase, responses in [
            ("after claim", [self.listing, changed]),
            ("before admission", [self.listing, self.listing, changed]),
        ]:
            with self.subTest(phase=phase):
                linear = Mock()
                with patch.object(self.herdr, "command", side_effect=responses) as command, \
                        self.assertRaisesRegex(TaskError, "Workspace selection changed during preparation"):
                    prepare_task(baseline.ISSUE, baseline.PROJECT, linear, self.git, lambda: self.herdr,
                                 check_configuration=Mock())
                self.assertEqual(command.call_args_list, [call("list")] * len(responses))
                self.git.resolve_scope.assert_not_called()
                self.git.save_scope.assert_not_called()
                linear.start.assert_not_called()

    def test_remote_other_branch_makes_no_slice_ambiguous(self):
        self.existing()
        self.git.remote_branches.return_value = [self.branch, "dev-7-other"]
        with self.responses() as command, self.assertRaisesRegex(TaskError, "Ambiguous.*dev-7-other"):
            self.prepare()
        command.assert_called_once_with("list")

    def test_same_live_remote_branch_does_not_prevent_reuse(self):
        self.existing()
        self.git.remote_branches.return_value = [self.branch]
        with self.responses() as command:
            self.assertEqual(self.prepare().branch, self.branch)
        self.assertEqual(command.call_args_list, [call("list"), call("list"),
            call("open", "--path", str(self.tree_path), "--label", "DEV-7", "--focus")])
        self.git.remote_branches.assert_called_once_with("DEV-7")

    def test_stable_herdr_ids_and_checkout_are_validated(self):
        self.existing()
        changes = [lambda r: r["workspace"].update(workspace_id="unexpected"),
                   lambda r: r["root_pane"].update(workspace_id="other"),
                   lambda r: r["root_pane"].update(tab_id="other"),
                   lambda r: r["root_pane"].update(pane_id=""),
                   lambda r: r["workspace"]["worktree"].update(checkout_path="/wrong"),
                   lambda r: r["workspace"]["worktree"].update(repo_root="/wrong"),
                   lambda r: r.pop("root_pane")]
        for index, change in enumerate(changes):
            with self.subTest(change=index):
                response = self.result("opened")
                change(response)
                with patch.object(self.herdr, "command", side_effect=[self.listing, self.listing, response]) as command, \
                        self.assertRaisesRegex(TaskError, "Herdr did not confirm the requested workspace and focus"):
                    self.prepare()
                self.assertEqual(command.call_args_list, [call("list"), call("list"),
                    call("open", "--path", str(self.tree_path), "--label", "DEV-7", "--focus")])
                self.git.save_scope.assert_not_called()

    def test_invalid_listed_workspace_id_refuses_before_open(self):
        self.existing()
        for workspace_id in ("", " ", 123):
            with self.subTest(workspace_id=workspace_id), patch.dict(self.tree, open_workspace_id=workspace_id):
                with self.responses() as command, self.assertRaisesRegex(TaskError, "Unexpected Herdr worktree list"):
                    self.prepare()
                command.assert_called_once_with("list")
                self.git.save_scope.assert_not_called()

    def test_existing_workspace_id_cannot_change(self):
        self.existing()
        self.tree["open_workspace_id"] = "previous"
        with self.responses() as command, self.assertRaisesRegex(TaskError, "different workspace ID"):
            self.prepare()
        self.assertEqual(command.call_args_list, [call("list"), call("list"),
            call("open", "--path", str(self.tree_path), "--label", "DEV-7", "--focus")])
        self.git.save_scope.assert_not_called()

    def test_git_must_confirm_new_worktree_before_ready(self):
        with self.responses("created") as command, self.assertRaisesRegex(TaskError, "Git did not confirm"):
            self.prepare()
        self.assertEqual(command.call_args_list, [call("list"), call("list"),
            call("create", "--base", "main", "--branch", self.branch, "--label", "DEV-7", "--focus")])
        self.git.save_scope.assert_not_called()


class GitHubHistoryTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.dict(os.environ, {"GH_TOKEN": "", "GITHUB_TOKEN": ""}))
        self.remote = "git@github.com:owner/repo.git"
        self.branch = "dev-7-example"

    def pull(self, **changes):
        return dict(number=1, state="open", merged_at=None,
                    head=dict(ref=self.branch, repo=dict(full_name="owner/repo")),
                    base=dict(repo=dict(full_name="owner/repo")), **changes)

    def response(self, *pages):
        return patch("task_start.github.urlopen", side_effect=[io.BytesIO(json.dumps(p).encode()) for p in pages])

    def check(self):
        check_history(self.remote, self.branch, existing=True)

    def test_github_remote_url_parsing(self):
        for url in [self.remote, "ssh://git@github.com/owner/repo.git", "https://github.com/owner/repo",
                    "https://username:secret@github.com/owner/repo.git"]:
            self.assertEqual(repository_name(url), "owner/repo")
        for url in ["https://github.com.evil/owner/repo", "git@elsewhere:owner/repo.git", "/tmp/remote", "https://github.com/owner/repo/extra"]:
            self.assertIsNone(repository_name(url))

    def test_no_pr_and_open_pr_allow_active_workspace(self):
        for pulls in [[], [self.pull()]]:
            with self.response(pulls) as request:
                self.check()
            url = urlsplit(request.call_args.args[0].full_url)
            self.assertEqual(url.netloc, "api.github.com")
            self.assertEqual(parse_qs(url.query)["state"], ["all"])
            self.assertEqual(parse_qs(url.query)["head"], ["owner:" + self.branch])

    def test_merged_and_closed_are_historical_regardless_of_ancestry(self):
        for state, merged in [("closed", "2026-01-01T00:00:00Z"), ("closed", None)]:
            pull = self.pull()
            pull.update(state=state, merged_at=merged)
            with self.response([pull]), self.assertRaisesRegex(TaskError, "Historical.*#1"):
                self.check()

    def test_pagination_cannot_hide_merged_pr(self):
        merged = self.pull()
        merged.update(state="closed", merged_at="2026-01-01T00:00:00Z")
        with self.response([self.pull()] * 100, [merged]) as request, self.assertRaisesRegex(TaskError, "merged"):
            self.check()
        self.assertEqual(parse_qs(urlsplit(request.call_args.args[0].full_url).query)["page"], ["2"])

    def test_api_failures_never_imply_active_or_leak_secrets(self):
        for error in [URLError("secret"), HTTPError("url", 404, "secret", {}, None), TimeoutError()]:
            with patch("task_start.github.urlopen", side_effect=error), self.assertRaisesRegex(TaskError, "Cannot establish") as caught:
                self.check()
            self.assertNotIn("secret", str(caught.exception))
        with patch("task_start.github.urlopen", return_value=io.BytesIO(b"not JSON")), self.assertRaises(TaskError):
            self.check()

    def test_malformed_or_unrelated_pr_response_refuses(self):
        wrong_head = self.pull()
        wrong_head["head"] = dict(ref="different", repo=dict(full_name="owner/repo"))
        for pulls in [None, {}, [None], [{}], [wrong_head]]:
            with self.response(pulls), self.assertRaisesRegex(TaskError, "unexpected GitHub response"):
                self.check()

    def test_optional_existing_token_only_sent_to_github_api(self):
        with patch.dict(os.environ, {"GH_TOKEN": "fake-test-token"}), self.response([]) as request:
            self.check()
        self.assertEqual(request.call_args.args[0].get_header("Authorization"), "Bearer fake-test-token")

    def test_unsupported_host_refuses_reuse_but_allows_new_branch(self):
        with patch("task_start.github.urlopen") as request:
            with self.assertRaisesRegex(TaskError, "Cannot establish"):
                check_history("/local/remote", self.branch, existing=True)
            check_history("/local/remote", self.branch, existing=False)
        request.assert_not_called()


class RemoteGitIntegrationTests(unittest.TestCase):
    setUp = baseline.LocalGitIntegrationTests.setUp
    command = baseline.LocalGitIntegrationTests.command

    def test_stale_tracking_ref_is_not_authoritative(self):
        self.command(self.remote, "branch", "dev-7-old")
        self.command(self.repo, "fetch", "origin")
        self.command(self.remote, "branch", "-D", "dev-7-old")
        self.assertEqual(self.git.remote_branches("DEV-7"), [])
        self.assertEqual(self.command(self.repo, "rev-parse", "refs/remotes/origin/dev-7-old"), self.before)
        # Stale cached state does not block Herdr creation. Its response is faked,
        # but both remote lookup and resulting Git checkout are real.
        path = self.repo.parent / "selected"
        branch = "dev-7-new-title"
        herdr = Herdr(self.repo)

        def respond(operation, *args):
            if operation == "list":
                return dict(source=dict(repo_root=str(self.repo)), worktrees=[])
            self.assertEqual(operation, "create")
            self.command(self.repo, "worktree", "add", "-b", branch, str(path), "main")
            return dict(worktree=dict(branch=branch, path=str(path), is_linked_worktree=True,
                                      is_bare=False, is_detached=False, is_prunable=False),
                        workspace=dict(workspace_id="w4", focused=True, worktree=dict(repo_root=str(self.repo), checkout_path=str(path))),
                        tab=dict(workspace_id="w4", tab_id="w4:t3"),
                        root_pane=dict(workspace_id="w4", tab_id="w4:t3", pane_id="w4:p9"))
        with patch.object(herdr, "command", side_effect=respond):
            self.assertEqual(herdr.prepare(self.git, "main", branch, "DEV-7").path, path)

    def test_live_remote_branch_is_discovered_without_tracking_ref(self):
        self.command(self.remote, "branch", "dev-7-live")
        self.command(self.remote, "branch", "dev-70-other")
        self.assertEqual(self.git.remote_branches("DEV-7"), ["dev-7-live"])
        with patch.object(Herdr, "command", return_value=dict(source=dict(repo_root=str(self.repo)), worktrees=[])) as command:
            with self.assertRaisesRegex(TaskError, "live remote branch.*dev-7-live"):
                Herdr(self.repo).prepare(self.git, "main", "dev-7-new", "DEV-7")
        command.assert_called_once_with("list")

    def test_remote_query_never_applies_destructive_fetch_refspec(self):
        self.command(self.remote, "branch", "dev-7-live")
        self.command(self.repo, "config", "remote.origin.fetch", "+refs/heads/*:refs/heads/*")
        self.assertEqual(self.git.remote_branches("DEV-7"), ["dev-7-live"])
        self.assertEqual(self.git.branches("DEV-7"), [])
        self.assertEqual(self.command(self.repo, "rev-parse", "HEAD"), self.before)

    def test_inaccessible_remote_does_not_allow_new_work(self):
        self.command(self.repo, "remote", "set-url", "origin", str(self.repo.parent / "missing"))
        with self.assertRaises(TaskError):
            self.git.remote_branches("DEV-7")

    def test_squash_merged_worktree_is_rejected_by_authoritative_history(self):
        branch = "dev-7-squashed"
        path = self.repo.parent / "squashed-task"
        self.command(self.repo, "worktree", "add", "-b", branch, str(path))
        (path / "feature").write_text("feature\n")
        self.command(path, "add", "feature")
        self.command(path, "commit", "-m", "feature one")
        self.command(path, "commit", "--allow-empty", "-m", "feature two")
        self.command(self.repo, "merge", "--squash", branch)
        self.command(self.repo, "commit", "-m", "squashed PR")
        self.assertNotIn(branch, self.command(self.repo, "branch", "--merged", "main"))
        self.command(self.repo, "remote", "set-url", "origin", "git@github.com:owner/repo.git")
        pull = dict(number=10, state="closed", merged_at="2026-01-01T00:00:00Z",
                    head=dict(ref=branch, repo=dict(full_name="owner/repo")),
                    base=dict(repo=dict(full_name="owner/repo")))
        with patch("task_start.github.urlopen", return_value=io.BytesIO(json.dumps([pull]).encode())):
            with self.assertRaisesRegex(TaskError, "Historical.*merged"):
                self.git.check_history("main", branch, existing=True)
        self.assertTrue(path.is_dir())
        self.assertEqual(self.git.branches("DEV-7"), [branch])

    def test_multiple_repository_remotes_refuse_incomplete_history(self):
        self.command(self.repo, "remote", "set-url", "origin", "git@github.com:owner/repo.git")
        self.command(self.repo, "remote", "add", "fork", "git@github.com:other/repo.git")
        with patch("task_start.github.urlopen") as request, self.assertRaisesRegex(TaskError, "different repositories"):
            self.git.check_history("main", "dev-7-example", existing=True)
        request.assert_not_called()

    def test_create_slice_then_start_without_slice_preserves_scope_and_prompt(self):
        path = self.repo.parent / "selected-importer"
        branch = "dev-7-importer"
        operations = []

        def respond(operation, *args):
            operations.append(operation)
            tree = dict(branch=branch, path=str(path), is_linked_worktree=True, label="mutable display label",
                        is_bare=False, is_detached=False, is_prunable=False, open_workspace_id="w4")
            if operation == "list":
                return dict(source=dict(repo_root=str(self.repo)), worktrees=[tree] if path.exists() else [])
            if operation == "create":
                self.command(self.repo, "worktree", "add", "-b", branch, str(path), "main")
            else:
                self.assertEqual(operation, "open")
                self.assertEqual(args, ("--path", str(path), "--label", "DEV-7 / importer", "--focus"))
            return dict(worktree=tree,
                        workspace=dict(workspace_id="w4", focused=True, worktree=dict(repo_root=str(self.repo), checkout_path=str(path))),
                        tab=dict(workspace_id="w4", tab_id="w4:t3"),
                        root_pane=dict(workspace_id="w4", tab_id="w4:t3", pane_id="w4:p9"))

        local = LocalConfig(self.repo.parent, "placeholder", baseline.LOCAL.agent)
        project = Project(baseline.ISSUE.project, self.repo.name, "main")
        with patch("task_start.cli.load_local", return_value=local), \
                patch("task_start.cli.load_projects", return_value=[project]), \
                patch("task_start.cli.launch_registered", side_effect=lambda agent, execution: agent.launch(execution)), \
                patch("task_start.cli.Linear") as linear, patch("task_start.cli.adapter_for") as factory, \
                patch.object(Herdr, "command", side_effect=respond), patch.object(Git, "check_history"):
            linear.return_value.get_issue.return_value = baseline.ISSUE
            agent = factory.return_value
            agent.launch.return_value = LaunchResult("codex", "w4:p9", "working")
            cli.start("DEV-7", slice="importer")
            linear.return_value.get_issue.return_value = replace(baseline.ISSUE, title="Completely renamed issue")
            cli.start("DEV-7")
        executions = [call.args[0] for call in agent.launch.call_args_list]
        selected = [execution.workspace for execution in executions]
        self.assertEqual(len(selected), 2)
        for execution in executions:
            workspace = execution.workspace
            self.assertEqual((workspace.path, workspace.branch, workspace.slice), (path, branch, "importer"))
            self.assertIn("Slice: importer\nImplement only this slice of the task.", execution.handoff)
        # Each start claims before the base update, resolves with remotes, and
        # revalidates under admission before the single workspace mutation.
        self.assertEqual(operations, ["list", "list", "list", "create", "list", "list", "list", "open"])
        self.assertEqual(selected[0].workspace_id, selected[1].workspace_id)
        self.assertEqual(len(self.git.worktrees()), 2)
        record = self.git.scope_file(path)
        self.assertEqual(record.parent.parent, self.repo / ".git" / "worktrees")
        self.assertEqual(json.loads(record.read_text())["slice"], "importer")
        self.assertEqual(self.command(path, "status", "--porcelain"), "")


class ScopeMetadataTests(unittest.TestCase):
    setUp = baseline.LocalGitIntegrationTests.setUp
    command = baseline.LocalGitIntegrationTests.command

    def task_tree(self):
        path = self.repo.parent / "scope-task"
        self.command(self.repo, "worktree", "add", "-b", "dev-7-importer", str(path))
        return path

    def test_missing_scope_requires_explicit_slice_even_if_title_matches(self):
        path = self.task_tree()
        with self.assertRaisesRegex(TaskError, "explicit --slice"):
            self.git.resolve_scope(path, "dev-7-importer", "DEV-7", None)
        self.assertEqual(self.git.resolve_scope(path, "dev-7-importer", "DEV-7", "importer"), "importer")
        self.git.save_scope(path, "dev-7-importer", "DEV-7", "importer")
        self.assertEqual(self.git.resolve_scope(path, "dev-7-importer", "DEV-7", None), "importer")

    def test_explicit_default_metadata_never_infers_slice_from_branch(self):
        path = self.task_tree()
        self.git.save_scope(path, "dev-7-importer", "DEV-7", None)
        self.assertIsNone(self.git.resolve_scope(path, "dev-7-importer", "DEV-7", None))
        with self.assertRaisesRegex(TaskError, "conflicts with the recorded scope"):
            self.git.resolve_scope(path, "dev-7-importer", "DEV-7", "importer")
        self.assertIsNone(json.loads(self.git.scope_file(path).read_text())["slice"])

    def test_invalid_or_mismatched_metadata_never_drops_scope(self):
        path = self.task_tree()
        record = self.git.scope_file(path)
        valid = dict(version=1, identifier="DEV-7", branch="dev-7-importer", slice="importer")
        for value in ["not json", "null", "[]", "{}", json.dumps(dict(valid, version=True)),
                      json.dumps(dict(valid, identifier="DEV-8")), json.dumps(dict(valid, branch="other")),
                      json.dumps(dict(valid, slice="different")), json.dumps(dict(valid, slice="Importer")),
                      json.dumps(dict(valid, slice=42)), json.dumps(dict(valid, extra="unexpected"))]:
            record.write_text(value)
            for requested in [None, "importer"]:
                with self.subTest(value=value, requested=requested), self.assertRaises(TaskError):
                    self.git.resolve_scope(path, "dev-7-importer", "DEV-7", requested)
        record.write_bytes(b"\xff")
        with self.assertRaises(TaskError):
            self.git.resolve_scope(path, "dev-7-importer", "DEV-7", None)

    def test_scope_record_is_never_overwritten(self):
        path = self.task_tree()
        self.git.save_scope(path, "dev-7-importer", "DEV-7", "importer")
        record = self.git.scope_file(path)
        before = record.read_bytes()
        with self.assertRaisesRegex(TaskError, "scope changed"):
            self.git.save_scope(path, "dev-7-importer", "DEV-7", None)
        self.assertEqual(record.read_bytes(), before)

    def test_write_failure_preserves_worktree_and_reports_not_ready(self):
        path = self.task_tree()
        with patch.object(Path, "open", side_effect=PermissionError), self.assertRaisesRegex(TaskError, "could not be saved"):
            self.git.save_scope(path, "dev-7-importer", "DEV-7", "importer")
        self.assertTrue(path.is_dir())


class GitResponseTests(unittest.TestCase):
    def test_remote_ref_validation_and_safe_command(self):
        git = Git(Path("/repo"))
        for output in ["invalid", "not-an-oid\trefs/heads/dev-7-foo", "0" * 40 + "\trefs/tags/dev-7"]:
            with patch.object(git, "command", side_effect=["origin\n", output]) as command, self.assertRaisesRegex(TaskError, "Unexpected Git remote"):
                git.remote_branches("DEV-7")
            command.assert_called_with("ls-remote", "--heads", "--", "origin")

    def test_invalid_git_worktree_output_is_a_task_error(self):
        for output in ["branch refs/heads/dev-7\0\0", "worktree relative/path\0\0",
                       "worktree /checkout\0branch other\0\0"]:
            with patch.object(Git, "command", return_value=output), self.assertRaisesRegex(TaskError, "Unexpected Git worktree"):
                Git(Path("/repo")).worktrees()
