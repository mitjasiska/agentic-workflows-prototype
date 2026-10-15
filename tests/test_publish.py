import copy
from dataclasses import replace
import io
import json
import os
from pathlib import Path
import subprocess
import select
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from urllib.parse import urlsplit

from task_start import TaskError, cli
from task_start.github import publication_pull, publish_pull, request as github_request
from task_start.publish import (change_type, native_git, prepare_metadata, publish, remote_identity, tracking_url,
                                verify_remote_identity)
from task_start.publication_state import PublicationStore
from task_start.review import review
from task_start.review_result import publication_fingerprint, publication_metadata, publication_summary_limit
from task_start.review_state import snapshot
from task_start.workspace import Git
import test_review as reviews
import test_task_start as baseline


PUBLIC = dict(summary="publish the reviewed result", description="Publish the reviewed task with safe retries.",
              validation="Offline checks passed. Live GitHub access was not tested.")


class PublishingTests(unittest.TestCase):
    # Reuse the real Git/context/Herdr fixture without inheriting its test methods.
    setUp = reviews.ReviewTests.setUp
    command = reviews.ReviewTests.command
    worktrees = reviews.ReviewTests.worktrees
    pane_command = reviews.ReviewTests.pane_command
    select_adapter = reviews.ReviewTests.select_adapter

    def prepare(self):
        self.linear.get_issue.return_value = replace(self.linear.get_issue.return_value,
            labels=("Feature", "Research"), url="https://linear.app/workspace/issue/DEV-7/old-title")
        (self.path / "tracked.txt").write_text("implemented after steering\n")
        (self.path / "new.txt").write_text("new implementation\n")
        self.verdict_overrides = dict(publication=PUBLIC)
        self.accepted_result = review("DEV-7")
        self.assertEqual(self.accepted_result.state, "clean")
        self.store = PublicationStore(self.path)
        self.enterContext(patch("task_start.publish.load_local", return_value=self.local))
        for symbol in ("load_projects", "Linear", "ContextRegistry", "HerdrContexts"):
            import task_start.review as review_module
            self.enterContext(patch(f"task_start.publish.{symbol}", getattr(review_module, symbol)))
        self.enterContext(patch.dict(os.environ, {"GH_TOKEN": "test-token"}))
        self.destination = self.enterContext(patch("task_start.publish.remote_identity",
            return_value=dict(remote="origin", repository="owner/project")))
        self.calls = []
        self.pulls = []
        self.github_failure = None
        self.enterContext(patch("task_start.github.request", side_effect=self.api))
        self.native = self.enterContext(patch("task_start.publish.native_git", wraps=native_git))

    def api(self, path, branch, *, method="GET", data=None):
        self.calls.append((method, path, data))
        if method == "POST":
            self.pulls.append(dict(number=1, state="open", merged_at=None,
                head=dict(ref=self.branch, sha=self.command(self.remote, "rev-parse", self.branch),
                          repo=dict(full_name="owner/project")),
                base=dict(ref="main", repo=dict(full_name="owner/project")),
                html_url="https://github.com/owner/project/pull/1", title=data["title"], body=data["body"]))
            if self.github_failure:
                raise self.github_failure
            return copy.deepcopy(self.pulls[0])
        if method == "PATCH":
            self.pulls[0].update(data)
            return copy.deepcopy(self.pulls[0])
        # GitHub resolves current branch SHA on every observation.
        for pull in self.pulls:
            if pull.get("follow_remote", True):
                pull["head"]["sha"] = self.command(self.remote, "rev-parse", self.branch)
        return copy.deepcopy(self.pulls if "?" in path else self.pulls[0])

    def operations(self, operation):
        return [c for c in self.native.call_args_list if c.args[1] == operation]

    def assert_published(self):
        head = self.command(self.path, "rev-parse", "HEAD")
        self.assertEqual(self.command(self.remote, "rev-parse", self.branch), head)
        self.assertEqual(self.command(self.path, "status", "--porcelain"), "")
        self.assertEqual(self.command(self.path, "show", "-s", "--format=%s"), "feat: publish the reviewed result (DEV-7)")
        self.assertEqual(self.command(self.path, "rev-list", "--count", f"{self.base}..HEAD"), "1")
        self.assertEqual(len(self.pulls), 1)
        self.assertEqual(self.pulls[0]["title"], "feat: publish the reviewed result (DEV-7)")
        self.assertIn("Linear: [DEV-7](https://linear.app/workspace/issue/DEV-7)", self.pulls[0]["body"])
        self.assertEqual([line for line in self.pulls[0]["body"].splitlines() if line.startswith("##")],
                         ["## Summary", "## Tracking", "## Validation"])
        self.assertNotIn("Current requirements", self.pulls[0]["body"])
        self.linear.start.assert_not_called()
        return head

    def test_happy_path_and_completed_retry_are_exactly_once(self):
        self.prepare()
        self.assertFalse(Path(self.adapter.output).exists())
        accepted = self.store.read()["acceptance"]
        self.assertEqual(accepted["pass_id"], self.accepted_result.pass_id)
        self.assertEqual(accepted["review_state"], self.accepted_result.review_state)
        self.assertEqual(accepted["execution"], self.accepted_result.execution)
        self.assertTrue(accepted["completed_at"])
        url = publish("DEV-7")
        self.assertEqual(len(self.operations("ls-remote")), 4)
        head = self.assert_published()
        self.assertEqual(self.store.read()["intent"]["publishing_head"], head)
        history = self.store.read()["publication_history"]
        self.assertEqual((history["state"], history["head"]), ("published", head))
        self.assertEqual(publish("DEV-7"), url)
        self.assertEqual(len(self.operations("ls-remote")), 6)  # Two fresh checks for a completed retry.
        self.assertEqual(self.assert_published(), head)
        self.assertEqual(self.store.read()["publication_history"], history)
        self.assertEqual(len(self.operations("commit")), 1)
        self.assertEqual(len(self.operations("push")), 1)
        self.assertEqual(sum(c[0] == "POST" for c in self.calls), 1)

    def test_commit_success_push_failure_continues_without_another_commit(self):
        self.prepare()
        def fail_push(path, operation, *args, **kwargs):
            if operation == "push":
                raise TaskError("push failed")
            return native_git(path, operation, *args, **kwargs)
        self.native.side_effect = fail_push
        with self.assertRaisesRegex(TaskError, "push failed"):
            publish("DEV-7")
        head = self.command(self.path, "rev-parse", "HEAD")
        self.native.side_effect = None
        publish("DEV-7")
        self.assertEqual(self.assert_published(), head)
        self.assertEqual(len(self.operations("commit")), 1)

    def test_commit_succeeded_but_acknowledgement_lost_recovers_original_index(self):
        self.prepare()
        def fail_commit(path, operation, *args, **kwargs):
            result = native_git(path, operation, *args, **kwargs)
            if operation == "commit":
                raise TaskError("commit acknowledgement lost")
            return result
        self.native.side_effect = fail_commit
        with self.assertRaisesRegex(TaskError, "acknowledgement lost"):
            publish("DEV-7")
        self.assertNotEqual(self.command(self.path, "status", "--porcelain"), "")
        self.native.side_effect = None
        publish("DEV-7")
        self.assert_published()
        self.assertEqual(len(self.operations("commit")), 1)

    def test_push_succeeded_but_acknowledgement_lost_does_not_push_again(self):
        self.prepare()
        def fail_push(path, operation, *args, **kwargs):
            result = native_git(path, operation, *args, **kwargs)
            if operation == "push":
                raise TaskError("push acknowledgement lost")
            return result
        self.native.side_effect = fail_push
        with self.assertRaisesRegex(TaskError, "acknowledgement lost"):
            publish("DEV-7")
        self.native.side_effect = None
        publish("DEV-7")
        self.assert_published()
        self.assertEqual(len(self.operations("push")), 1)

    def test_push_success_pr_failure_and_uncertain_creation_reuse(self):
        self.prepare()
        real_api = self.api
        def fail_before_create(path, branch, **kwargs):
            if kwargs.get("method") == "POST":
                raise TaskError("GitHub unavailable")
            return real_api(path, branch, **kwargs)
        with patch("task_start.github.request", side_effect=fail_before_create):
            with self.assertRaisesRegex(TaskError, "unavailable"):
                publish("DEV-7")
        self.github_failure = TaskError("PR acknowledgement lost")
        with self.assertRaisesRegex(TaskError, "acknowledgement lost"):
            publish("DEV-7")
        self.github_failure = None
        publish("DEV-7")
        self.assert_published()
        self.assertEqual(len(self.operations("commit")), 1)
        self.assertEqual(len(self.operations("push")), 1)
        self.assertEqual(sum(c[0] == "POST" for c in self.calls), 1)

    def api_transport(self, request, **kwargs):
        url = urlsplit(request.full_url)
        self.assertEqual(url.netloc, "api.github.com")
        path = url.path.removeprefix("/repos/") + ("?" + url.query if url.query else "")
        result = self.api(path, self.branch, method=request.get_method(),
                          data=json.loads(request.data) if request.data is not None else None)
        return io.BytesIO(json.dumps(result).encode())

    def test_invalid_api_credentials_refuse_before_publication_side_effects(self):
        self.prepare()
        for value in ("", "invalid\ncredential"):
            with patch.dict(os.environ, {"GH_TOKEN": value, "GITHUB_TOKEN": ""}), \
                    self.assertRaisesRegex(TaskError, "authentication is missing|credential GH_TOKEN"):
                publish("DEV-7")
            self.assertEqual(self.operations("commit"), [])
            self.assertEqual(self.operations("push"), [])
            self.assertEqual(self.calls, [])
            self.assertIsNone(self.store.read()["intent"])

    def test_api_permission_failure_reuses_published_commit_after_credential_repair(self):
        self.prepare()
        def denied(request, **kwargs):
            if request.get_method() == "POST":
                raise HTTPError(request.full_url, 403, "private diagnostic", {
                    "X-Accepted-GitHub-Permissions": "pull_requests=write"}, io.BytesIO(json.dumps({
                        "message": "Resource not accessible by personal access token"}).encode()))
            return self.api_transport(request, **kwargs)
        with patch("task_start.github.request", new=github_request), \
                patch("task_start.github.urlopen", side_effect=denied) as transport:
            for _ in range(2):
                with self.assertRaisesRegex(TaskError, "HTTP 403.*insufficient token permissions.*Pull requests: write"):
                    publish("DEV-7")
                self.assertEqual(len(self.operations("commit")), 1)
                self.assertEqual(len(self.operations("push")), 1)
                self.assertEqual(self.pulls, [])
                self.assertEqual(self.store.read()["publication_history"]["state"], "published")
            head = self.command(self.path, "rev-parse", "HEAD")
            self.assertEqual(self.command(self.remote, "rev-parse", self.branch), head)
            transport.side_effect = self.api_transport
            publish("DEV-7")
        self.assertEqual(self.assert_published(), head)
        self.assertEqual(len(self.operations("commit")), 1)
        self.assertEqual(len(self.operations("push")), 1)

    def test_api_service_error_after_creation_discovers_existing_pr_without_another_write(self):
        self.prepare()
        writes = []
        def lost_response(request, **kwargs):
            response = self.api_transport(request, **kwargs)
            if request.get_method() == "POST":
                writes.append(request)
                response.close()
                raise HTTPError(request.full_url, 503, "private diagnostic", {}, io.BytesIO(b"private proxy response"))
            return response
        with patch("task_start.github.request", new=github_request), \
                patch("task_start.github.urlopen", side_effect=lost_response):
            with self.assertRaisesRegex(TaskError, "HTTP 503.*service failure.*discover and reuse"):
                publish("DEV-7")
            head = self.assert_published()
            self.assertEqual(publish("DEV-7"), self.pulls[0]["html_url"])
        self.assertEqual(self.assert_published(), head)
        self.assertEqual(len(writes), 1)
        self.assertEqual(len(self.operations("commit")), 1)
        self.assertEqual(len(self.operations("push")), 1)
        self.assertEqual(sum(c[0] == "POST" for c in self.calls), 1)

    def test_matching_pr_metadata_is_updated_not_duplicated(self):
        self.prepare()
        publish("DEV-7")
        before = len(self.operations("ls-remote"))
        self.pulls[0].update(title="Old title", body="Old body")
        publish("DEV-7")
        self.assertEqual(len(self.operations("ls-remote")) - before, 3)
        self.assert_published()
        self.assertEqual(sum(c[0] == "POST" for c in self.calls), 1)
        self.assertEqual(sum(c[0] == "PATCH" for c in self.calls), 1)

    def test_verified_push_confirmation_survives_pr_lookup_failure_without_duplicate_transport(self):
        self.prepare()
        def failed_lookup(path, branch, **kwargs):
            if self.operations("push"):
                raise TaskError("GitHub HTTP 503: service failure")
            return self.api(path, branch, **kwargs)
        with patch("task_start.github.request", side_effect=failed_lookup):
            with self.assertRaisesRegex(TaskError, "GitHub HTTP 503: service failure"):
                publish("DEV-7")
        head = self.command(self.path, "rev-parse", "HEAD")
        self.assertEqual(self.store.read()["publication_history"]["state"], "published")
        self.assertEqual(self.store.read()["publication_history"]["head"], head)
        self.assertEqual(len(self.operations("ls-remote")), 3)
        self.assertFalse(self.pulls)
        publish("DEV-7")
        self.assertEqual(self.assert_published(), head)
        self.assertEqual(len(self.operations("ls-remote")), 6)
        self.assertEqual(len(self.operations("commit")), 1)
        self.assertEqual(len(self.operations("push")), 1)

    def test_pr_lookup_failure_with_advanced_base_preserves_confirmed_push(self):
        self.prepare()
        def failed_lookup(path, branch, **kwargs):
            if self.operations("push"):
                self.advance_remote()
                raise TaskError("GitHub HTTP 503: service failure")
            return self.api(path, branch, **kwargs)
        with patch("task_start.github.request", side_effect=failed_lookup):
            with self.assertRaisesRegex(TaskError, "HTTP 503: service failure$"):
                publish("DEV-7")
        self.assertEqual(len(self.operations("ls-remote")), 3)
        self.assertEqual(self.store.read()["publication_history"]["state"], "published")
        self.assertFalse(self.pulls)

    def test_base_advancement_after_push_does_not_make_publication_uncertain(self):
        self.prepare()
        advanced = None
        def advance_after_push(path, operation, *args, **kwargs):
            nonlocal advanced
            result = native_git(path, operation, *args, **kwargs)
            if operation == "push":
                advanced = self.advance_remote()
                self.command(self.repo, "fetch", "origin", "main")
                self.command(self.repo, "merge", "--ff-only", advanced)
            return result
        self.native.side_effect = advance_after_push
        url = publish("DEV-7")
        head = self.assert_published()
        history = self.store.read()["publication_history"]
        self.assertEqual(history["base_commit"], self.base)
        self.assertNotEqual(advanced, self.base)
        self.assertEqual(history["state"], "published")
        self.assertEqual(history["cycles"][-1]["state"], "complete")
        self.assertEqual(publish("DEV-7"), url)
        self.assertEqual(self.assert_published(), head)
        self.assertEqual(len(self.operations("push")), 1)
        self.assertFalse(self.operations("commit-tree"))

    def test_lost_push_acknowledgement_can_confirm_exact_ref_despite_new_base(self):
        self.prepare()
        def lose_ack(path, operation, *args, **kwargs):
            result = native_git(path, operation, *args, **kwargs)
            if operation == "push":
                self.advance_remote()
                raise TaskError("push acknowledgement lost")
            return result
        self.native.side_effect = lose_ack
        with self.assertRaisesRegex(TaskError, "acknowledgement lost"):
            publish("DEV-7")
        self.assertEqual(self.store.read()["publication_history"]["state"], "pending")
        self.native.side_effect = None
        publish("DEV-7")
        self.assert_published()
        history = self.store.read()["publication_history"]
        self.assertEqual(history["state"], "published")
        self.assertEqual(history["cycles"][-1]["state"], "complete")
        self.assertEqual(len(self.operations("push")), 1)

    def test_pr_lookup_failure_and_task_ref_drift_preserve_failure(self):
        self.prepare()
        def failed_lookup(path, branch, **kwargs):
            if self.operations("push"):
                self.command(self.remote, "update-ref", "-d", f"refs/heads/{self.branch}")
                raise TaskError("GitHub HTTP 503: service failure")
            return self.api(path, branch, **kwargs)
        with patch("task_start.github.request", side_effect=failed_lookup):
            with self.assertRaisesRegex(TaskError, "HTTP 503.*confirmation also failed.*Remote task branch conflicts"):
                publish("DEV-7")
        self.assertEqual(self.store.read()["publication_history"]["state"], "pending")
        self.assertFalse(self.pulls)

    def test_published_retry_detects_remote_deletion_after_preflight_without_repairing_or_writing_pr(self):
        self.prepare()
        with patch("task_start.publish.publish_pull", side_effect=TaskError("stop before PR")):
            with self.assertRaisesRegex(TaskError, "stop before PR"):
                publish("DEV-7")
        head = self.command(self.path, "rev-parse", "HEAD")
        lookups = 0
        def deleted_ref(path, branch, **kwargs):
            nonlocal lookups
            result = self.api(path, branch, **kwargs)
            lookups += 1
            if lookups == 1:  # After preflight observed the exact published SHA.
                self.command(self.remote, "update-ref", "-d", f"refs/heads/{self.branch}")
            return result
        before = len(self.operations("ls-remote"))
        with patch("task_start.github.request", side_effect=deleted_ref):
            with self.assertRaisesRegex(TaskError, "Remote task branch conflicts"):
                publish("DEV-7")
        self.assertEqual(len(self.operations("ls-remote")) - before, 2)
        self.assertEqual(self.command(self.path, "rev-parse", "HEAD"), head)
        self.assertEqual(len(self.operations("commit")), 1)
        self.assertEqual(len(self.operations("push")), 1)
        self.assertFalse(self.pulls)

    def test_remote_drift_from_commit_hook_still_refuses_before_push(self):
        self.prepare()
        def advance_on_commit(path, operation, *args, **kwargs):
            result = native_git(path, operation, *args, **kwargs)
            if operation == "commit":
                self.advance_remote()
            return result
        self.native.side_effect = advance_on_commit
        with self.assertRaisesRegex(TaskError, "Remote base differs"):
            publish("DEV-7")
        self.assertEqual(len(self.operations("ls-remote")), 2)
        self.assertFalse(self.operations("push"))
        self.assertFalse(self.pulls)

    def test_concise_title_uses_reviewed_result_and_preserves_pr_acronym(self):
        self.prepare()
        public = dict(PUBLIC, summary="add reviewed task PR publishing")
        self.verdict_overrides = dict(publication=public)
        self.assertEqual(review("DEV-7").state, "clean")
        publish("DEV-7")
        title = "feat: add reviewed task PR publishing (DEV-7)"
        self.assertEqual(self.command(self.path, "show", "-s", "--format=%s"), title)
        self.assertEqual(self.pulls[0]["title"], title)
        self.assertNotIn(self.linear.get_issue.return_value.title, title)

    def test_nonconforming_review_summary_cannot_authorize_publication(self):
        self.prepare()
        self.verdict_overrides = dict(publication=dict(PUBLIC, summary="Publish The Reviewed Task"))
        result = review("DEV-7")
        self.assertEqual(result.state, "failed")
        self.assertIn("lower-case action phrase", result.summary)
        self.assertIsNone(self.store.read()["acceptance"])
        self.assertFalse(self.operations("commit"))

    def test_new_title_policy_never_rewrites_frozen_legacy_rebase_metadata(self):
        self.prepare()
        legacy = "feat: Publish reviewed task changes with recoverable Git and GitHub stages (DEV-7)"
        _, body = prepare_metadata(self.linear.get_issue.return_value, self.store.read()["acceptance"])
        base = self.advance_remote()
        with patch("task_start.publish.prepare_metadata", return_value=(legacy, body)):
            head = self.assert_rebased(base)
        frozen = self.store.read()["rebase"]["publication"]
        self.verdict_overrides = dict(summary="The rebased implementation is correct.",
                                     publication=dict(PUBLIC, summary="A Different Free-form Summary"))
        self.assertEqual(review("DEV-7").state, "clean")
        with patch("task_start.publish.prepare_metadata", side_effect=AssertionError("Never regenerate frozen metadata")):
            publish("DEV-7")
            publish("DEV-7")
        self.assertEqual(self.command(self.path, "rev-parse", "HEAD"), head)
        self.assertEqual(self.command(self.path, "show", "-s", "--format=%s"), legacy)
        self.assertEqual(self.pulls[0]["title"], legacy)
        self.assertEqual(self.store.read()["rebase"]["publication"], frozen)
        self.assertFalse(self.operations("commit"))
        self.assertEqual(len(self.operations("push")), 1)

    def test_pr_create_rechecks_task_branch_after_lookup(self):
        self.assert_remote_drift_before_pr_write("head", update=False)

    def test_pr_create_allows_base_advancement_after_push(self):
        self.assert_remote_drift_before_pr_write("base", update=False)

    def test_pr_update_rechecks_task_branch_after_lookup(self):
        self.assert_remote_drift_before_pr_write("head", update=True)

    def test_pr_update_allows_base_advancement_after_push(self):
        self.assert_remote_drift_before_pr_write("base", update=True)

    def test_pr_write_rechecks_effective_destination_after_lookup(self):
        self.assert_remote_drift_before_pr_write("destination", update=False)

    def assert_remote_drift_before_pr_write(self, drift, *, update):
        self.prepare()
        if update:
            publish("DEV-7")
            self.pulls[0].update(title="Old title", body="Old body")
        prior_writes = [call for call in self.calls if call[0] in {"POST", "PATCH"}]
        changed = False
        def drift_after_lookup(path, branch, **kwargs):
            nonlocal changed
            result = self.api(path, branch, **kwargs)
            # Return a consistent GitHub snapshot, then change authoritative
            # state after the last lookup and before the attempted write.
            if not changed and kwargs.get("method", "GET") == "GET" and (not update or path.endswith("/pulls/1")):
                changed = True
                if drift == "head":
                    other = self.command(self.remote, "commit-tree", "HEAD^{tree}", "-p", self.base, "-m", "other task update")
                    self.command(self.remote, "update-ref", f"refs/heads/{self.branch}", other)
                elif drift == "base":
                    self.advance_remote()
                else:
                    self.destination.return_value = dict(remote="origin", repository="other/repository")
            return result
        def guarded_pr(*args, **kwargs):
            with patch("task_start.github.request", side_effect=drift_after_lookup):
                return publish_pull(*args, **kwargs)
        with patch("task_start.publish.publish_pull", side_effect=guarded_pr), \
                patch("sys.stdout", new=io.StringIO()) as output, patch("sys.stderr", new=io.StringIO()) as errors:
            self.assertEqual(cli.main(["pr", "DEV-7"]), 0 if drift == "base" else 1)
        self.assertTrue(changed)
        if drift == "base":
            self.assertIn(self.pulls[0]["html_url"], output.getvalue())
            self.assertEqual(errors.getvalue(), "")
            self.assertEqual(len([c for c in self.calls if c[0] in {"POST", "PATCH"}]), len(prior_writes) + 1)
            history = self.store.read()["publication_history"]
            self.assertEqual(history["state"], "published")
            self.assertEqual(history["cycles"][-1]["state"], "complete")
            self.assert_published()
            return
        self.assertEqual(output.getvalue(), "")
        self.assertRegex(errors.getvalue(), "Remote task branch conflicts|Remote base differs|destination differs")
        self.assertEqual([call for call in self.calls if call[0] in {"POST", "PATCH"}], prior_writes)
        if update:
            self.assertEqual((self.pulls[0]["title"], self.pulls[0]["body"]), ("Old title", "Old body"))
        else:
            self.assertFalse(self.pulls)
        self.assertEqual(self.command(self.path, "rev-parse", "HEAD"), self.store.read()["intent"]["publishing_head"])
        self.assertEqual(len(self.operations("commit")), 1)
        self.assertEqual(len(self.operations("push")), 1)

    def test_deleted_published_branch_never_becomes_eligible_for_automatic_rebase(self):
        self.prepare()
        def fail_creation(path, branch, **kwargs):
            if kwargs.get("method") == "POST":
                raise TaskError("GitHub creation unavailable")
            return self.api(path, branch, **kwargs)
        with patch("task_start.github.request", side_effect=fail_creation):
            with self.assertRaisesRegex(TaskError, "creation unavailable"):
                publish("DEV-7")
        head = self.command(self.path, "rev-parse", "HEAD")
        self.assertEqual(self.command(self.remote, "rev-parse", self.branch), head)
        before = snapshot(self.path, self.base, self.branch)
        self.command(self.remote, "update-ref", "-d", f"refs/heads/{self.branch}")
        self.advance_remote()
        with self.assertRaisesRegex(TaskError, "already published"):
            publish("DEV-7")
        self.assertEqual(snapshot(self.path, self.base, self.branch), before)
        self.assertEqual(self.store.read()["publication_history"]["state"], "published")
        self.assertEqual(self.store.read()["publication_history"]["head"], head)
        self.assertFalse(self.operations("fetch"))
        self.assertFalse(self.operations("commit-tree"))
        self.assertFalse(self.pulls)
        self.assertEqual(len(self.operations("commit")), 1)
        self.assertEqual(len(self.operations("push")), 1)

    def test_publication_evidence_survives_failed_and_fresh_reviews(self):
        self.prepare()
        publish("DEV-7")
        history = self.store.read()["publication_history"]
        self.assertEqual(history["state"], "published")
        (self.path / "new.txt").write_text("follow-up implementation\n")
        for verdict in ("blocked", "clean"):
            self.verdict_overrides = dict(state=verdict, publication=PUBLIC)
            self.assertEqual(review("DEV-7").state, verdict)
            self.assertIsNone(self.store.read()["intent"])
            self.assertEqual(self.store.read()["publication_history"], history)
        before = snapshot(self.path, self.base, self.branch)
        self.command(self.remote, "update-ref", "-d", f"refs/heads/{self.branch}")
        self.advance_remote()
        with self.assertRaisesRegex(TaskError, "already published"):
            publish("DEV-7")
        self.assertEqual(snapshot(self.path, self.base, self.branch), before)
        self.assertFalse(self.operations("fetch"))
        self.assertEqual(len(self.operations("commit")), 1)

    def test_uncertain_push_then_deleted_ref_cannot_rebase(self):
        self.prepare()
        def lose_push_ack(path, operation, *args, **kwargs):
            result = native_git(path, operation, *args, **kwargs)
            if operation == "push":
                raise TaskError("push acknowledgement lost")
            return result
        self.native.side_effect = lose_push_ack
        with self.assertRaisesRegex(TaskError, "acknowledgement lost"):
            publish("DEV-7")
        self.native.side_effect = None
        self.assertEqual(self.store.read()["publication_history"]["state"], "pending")
        before = snapshot(self.path, self.base, self.branch)
        self.command(self.remote, "update-ref", "-d", f"refs/heads/{self.branch}")
        self.advance_remote()
        with self.assertRaisesRegex(TaskError, "prior push may have published"):
            publish("DEV-7")
        self.assertEqual(snapshot(self.path, self.base, self.branch), before)
        self.assertFalse(self.operations("fetch"))
        self.assertFalse(self.pulls)
        self.assertEqual(len(self.operations("push")), 1)

    def test_failed_publication_confirmation_write_retains_pending_evidence(self):
        self.prepare()
        write = PublicationStore.write
        def fail_confirmation(store, value):
            history = value["publication_history"]
            if isinstance(history, dict) and history["state"] == "published":
                raise TaskError("confirmation storage failed")
            return write(store, value)
        with patch.object(PublicationStore, "write", new=fail_confirmation):
            with self.assertRaisesRegex(TaskError, "confirmation storage failed"):
                publish("DEV-7")
        self.assertEqual(self.store.read()["publication_history"]["state"], "pending")
        before = snapshot(self.path, self.base, self.branch)
        self.assertEqual(self.command(self.remote, "rev-parse", self.branch), before.head)
        self.command(self.remote, "update-ref", "-d", f"refs/heads/{self.branch}")
        self.advance_remote()
        with self.assertRaisesRegex(TaskError, "prior push may have published"):
            publish("DEV-7")
        self.assertEqual(snapshot(self.path, self.base, self.branch), before)
        self.assertFalse(self.operations("fetch"))
        self.assertFalse(self.pulls)

    def test_pending_publication_write_failure_prevents_push(self):
        self.prepare()
        write = PublicationStore.write
        def fail_pending(store, value):
            if value["publication_history"] is not None:
                raise TaskError("pending storage failed")
            return write(store, value)
        with patch.object(PublicationStore, "write", new=fail_pending):
            with self.assertRaisesRegex(TaskError, "pending storage failed"):
                publish("DEV-7")
        self.assertIsNone(self.store.read()["publication_history"])
        self.assertFalse(self.operations("push"))
        self.assertFalse(self.pulls)
        publish("DEV-7")
        self.assert_published()
        self.assertEqual(len(self.operations("commit")), 1)
        self.assertEqual(len(self.operations("push")), 1)

    def test_legacy_intent_with_unknown_push_history_forbids_rebase(self):
        self.prepare()
        with patch("task_start.publish.push", side_effect=TaskError("stopped after commit")):
            with self.assertRaisesRegex(TaskError, "stopped after commit"):
                publish("DEV-7")
        legacy = self.store.read()
        legacy.pop("publication_history")
        legacy["version"] = 2
        self.store.write(legacy)
        self.assertEqual(self.store.read()["publication_history"], "unknown")
        before = snapshot(self.path, self.base, self.branch)
        self.advance_remote()
        with self.assertRaisesRegex(TaskError, "legacy publication history"):
            publish("DEV-7")
        self.assertEqual(snapshot(self.path, self.base, self.branch), before)
        self.assertFalse(self.operations("fetch"))
        self.assertFalse(self.operations("push"))

    def test_legacy_review_without_intent_can_still_rebase_before_first_publication(self):
        self.prepare()
        legacy = self.store.read()
        legacy.pop("publication_history")
        legacy["version"] = 2
        self.store.write(legacy)
        self.assertIsNone(self.store.read()["publication_history"])
        self.assert_rebased(self.advance_remote())

    def test_publication_evidence_is_bound_to_the_task_and_repository(self):
        self.prepare()
        publish("DEV-7")
        saved = self.store.read()
        calls = self.native.call_count
        for field in ("binding", "identity", "head", "state"):
            changed = copy.deepcopy(saved)
            changed["publication_history"][field] = "invalid"
            self.store.write(changed)
            with self.subTest(field=field), self.assertRaisesRegex(TaskError, "Publication history conflicts"):
                publish("DEV-7")
        self.assertEqual(self.native.call_count, calls)

    def test_case_equivalent_pr_url_reuses_the_same_publication(self):
        self.prepare()
        publish("DEV-7")
        self.pulls[0]["html_url"] = "https://github.com/Owner/Project/pull/1"
        for side in ("head", "base"):
            self.pulls[0][side]["repo"]["full_name"] = "Owner/Project"
        self.assertEqual(publish("DEV-7"), self.pulls[0]["html_url"])
        self.assertEqual(len(self.operations("commit")), 1)
        self.assertEqual(len(self.operations("push")), 1)
        self.assertEqual(sum(c[0] == "POST" for c in self.calls), 1)

    def test_worktree_rewrites_refuse_before_commit_or_transport(self):
        self.prepare()
        self.destination.side_effect = remote_identity
        approved = "git@github.com:owner/project.git"
        self.command(self.repo, "remote", "set-url", "origin", approved)
        self.command(self.repo, "config", "extensions.worktreeConfig", "true")
        for rule in ("insteadOf", "pushInsteadOf"):
            key = f"url.git@github.com:unapproved/project.git.{rule}"
            self.command(self.path, "config", "--worktree", key, approved)
            self.assertEqual(remote_identity(Git(self.repo), "main", self.branch)["repository"], "owner/project")
            with self.subTest(rule=rule), self.assertRaisesRegex(TaskError, "destination differs|same github.com"):
                publish("DEV-7")
            self.native.assert_not_called()
            self.assertIsNone(self.store.read()["intent"])
            self.assertEqual(self.command(self.path, "rev-parse", "HEAD"), self.base)
            self.command(self.path, "config", "--worktree", "--unset", key)

    def test_conditional_worktree_rewrite_refuses_before_transport(self):
        self.prepare()
        self.destination.side_effect = remote_identity
        approved = "git@github.com:owner/project.git"
        self.command(self.repo, "remote", "set-url", "origin", approved)
        included = self.repo.parent / "branch-config"
        self.command(self.repo, "config", "--file", str(included),
                     "url.git@github.com:unapproved/project.git.insteadOf", approved)
        self.command(self.repo, "config", f"includeIf.onbranch:{self.branch}.path", str(included))
        self.assertEqual(remote_identity(Git(self.repo), "main", self.branch)["repository"], "owner/project")
        with self.assertRaisesRegex(TaskError, "destination differs"):
            publish("DEV-7")
        self.native.assert_not_called()

    def test_remote_names_avoid_a_second_rewrite_and_keep_retries_exactly_once(self):
        self.prepare()
        self.destination.side_effect = remote_identity
        self.command(self.repo, "remote", "set-url", "origin", "approved:owner/project.git")
        self.command(self.repo, "config", "url.git@github.com:.insteadOf", "approved:")
        self.command(self.repo, "config", "extensions.worktreeConfig", "true")
        self.command(self.path, "config", "--worktree", "url.git@github.com:unapproved/.insteadOf", "git@github.com:")
        # Emulate GitHub's SSH endpoint with real upload-pack/receive-pack on the
        # disposable upstream. Any request to the rewritten repository fails.
        helper = self.repo.parent / "ssh-fixture"
        helper.write_text(f"#!{sys.executable}\n" + "import os, shlex, sys\n"
            "operation, repository = shlex.split(sys.argv[-1])\n"
            "assert operation in {'git-upload-pack', 'git-receive-pack'}\n"
            "assert repository == 'owner/project.git', repository\n"
            f"os.execvp(operation, [operation, {str(self.remote)!r}])\n")
        helper.chmod(0o755)
        with patch.dict(os.environ, {"GIT_SSH_COMMAND": str(helper), "GIT_SSH_VARIANT": "ssh"}):
            publish("DEV-7")
            publish("DEV-7")
        self.assert_published()
        self.assertEqual(len(self.operations("commit")), 1)
        self.assertEqual(len(self.operations("push")), 1)
        self.assertEqual(sum(c[0] == "POST" for c in self.calls), 1)
        for call in self.operations("push") + self.operations("ls-remote"):
            self.assertEqual(call.args[call.args.index("--") + 1], "origin")

    def test_worktree_rewrite_after_commit_stops_before_push_and_can_retry(self):
        self.prepare()
        self.command(self.repo, "remote", "set-url", "origin", "git@github.com:owner/project.git")
        self.command(self.repo, "config", "extensions.worktreeConfig", "true")
        # Preflight uses the existing local transport fixture. After commit,
        # re-enable real identity resolution to model a hook/config change.
        def change_config(path, operation, *args, **kwargs):
            if operation == "ls-remote":
                return native_git(path, operation, "--heads", "--", str(self.remote),
                                  "refs/heads/main", f"refs/heads/{self.branch}", refs=True)
            result = native_git(path, operation, *args, **kwargs)
            if operation == "commit":
                self.command(self.path, "config", "--worktree",
                    "url.git@github.com:unapproved/project.git.insteadOf", "git@github.com:owner/project.git")
                self.destination.side_effect = remote_identity
            return result
        self.native.side_effect = change_config
        with self.assertRaisesRegex(TaskError, "destination differs"):
            publish("DEV-7")
        self.assertEqual(len(self.operations("commit")), 1)
        self.assertFalse(self.operations("push"))
        self.destination.side_effect = None
        self.native.side_effect = None
        self.command(self.path, "config", "--worktree", "--unset",
                     "url.git@github.com:unapproved/project.git.insteadOf")
        self.command(self.repo, "remote", "set-url", "origin", str(self.remote))
        publish("DEV-7")
        self.assert_published()
        self.assertEqual(len(self.operations("commit")), 1)

    def test_drift_before_publish_and_after_commit_is_never_pushed(self):
        self.prepare()
        reviewed = (self.path / "tracked.txt").read_text()
        (self.path / "tracked.txt").write_text("unreviewed")
        with self.assertRaisesRegex(TaskError, "drifted"):
            publish("DEV-7")
        self.assertEqual(len(self.operations("commit")), 0)
        (self.path / "tracked.txt").write_text(reviewed)
        with patch("task_start.publish.push", side_effect=TaskError("stop after commit")):
            with self.assertRaises(TaskError):
                publish("DEV-7")
        (self.path / "late.txt").write_text("unreviewed")
        with self.assertRaisesRegex(TaskError, "drifted"):
            publish("DEV-7")
        self.assertEqual(len(self.operations("push")), 0)
        self.assertEqual(len(self.operations("commit")), 1)

    def test_index_drift_and_replacement_commit_refuse(self):
        self.prepare()
        self.command(self.path, "add", "tracked.txt")
        with self.assertRaisesRegex(TaskError, "drifted"):
            publish("DEV-7")
        self.assertFalse(self.operations("commit"))

    def test_conflicting_remote_fails_before_commit(self):
        self.prepare()
        self.command(self.remote, "branch", self.branch)
        self.command(self.remote, "checkout", self.branch)
        (self.remote / "unrelated").write_text("conflict")
        self.command(self.remote, "add", ".")
        self.command(self.remote, "commit", "-m", "conflict")
        with self.assertRaisesRegex(TaskError, "Remote task branch conflicts"):
            publish("DEV-7")
        self.assertFalse(self.operations("commit"))
        self.assertFalse(self.operations("push"))

    def test_conflicting_and_ambiguous_prs_refuse_on_retry(self):
        self.prepare()
        publish("DEV-7")
        original = copy.deepcopy(self.pulls)
        for mutation in ("base", "head", "closed", "repo", "duplicate"):
            self.pulls = copy.deepcopy(original)
            if mutation == "base":
                self.pulls[0]["base"]["ref"] = "other"
            elif mutation == "head":
                self.pulls[0]["follow_remote"] = False
                self.pulls[0]["head"]["sha"] = self.base
            elif mutation == "closed":
                self.pulls[0]["state"] = "closed"
            elif mutation == "repo":
                self.pulls[0]["head"]["repo"]["full_name"] = "other/fork"
            else:
                self.pulls.append(copy.deepcopy(self.pulls[0]))
            with self.subTest(mutation=mutation), self.assertRaises(TaskError):
                publish("DEV-7")
        self.assertEqual(len(self.operations("commit")), 1)
        self.assertEqual(len(self.operations("push")), 1)

    def test_new_failed_or_interrupted_review_revokes_old_acceptance(self):
        self.prepare()
        self.verdict_overrides = dict(state="blocked")
        result = review("DEV-7")
        self.assertEqual(result.state, "blocked")
        self.assertIsNone(self.store.read()["acceptance"])
        with self.assertRaisesRegex(TaskError, "No current clean"):
            publish("DEV-7")

    def test_missing_or_ambiguous_type_has_no_side_effects(self):
        self.prepare()
        for labels in ((), ("Research",), ("Bug", "Feature")):
            self.linear.get_issue.return_value = replace(self.linear.get_issue.return_value, labels=labels)
            with self.subTest(labels=labels), self.assertRaisesRegex(TaskError, "canonical"):
                publish("DEV-7")
        self.assertFalse(self.operations("commit"))
        self.assertIsNone(self.store.read()["intent"])

    def test_repository_context_or_base_drift_refuses(self):
        self.prepare()
        saved = self.store.read()
        for key in ("repository", "context_id", "branch", "endpoint", "base_branch"):
            changed = copy.deepcopy(saved)
            changed["acceptance"][key] = "other"
            self.store.write(changed)
            with self.subTest(key=key), self.assertRaises(TaskError):
                publish("DEV-7")
        self.store.write(saved)
        with self.store.locked(), self.assertRaisesRegex(TaskError, "Another controller"):
            publish("DEV-7")
        self.assertFalse(self.operations("commit"))

    def test_modes_symlinks_binary_deletions_and_staged_changes_are_preserved(self):
        self.prepare()
        (self.path / "tracked.txt").unlink()
        (self.path / "binary").write_bytes(b"\x00\xffcontent")
        (self.path / "new.txt").chmod(0o755)
        (self.path / "link").symlink_to("new.txt")
        self.command(self.path, "add", ".")
        (self.path / "new.txt").write_text("final unstaged result\n")
        self.assertEqual(review("DEV-7").state, "clean")
        before = snapshot(self.path, self.base, self.branch).content
        publish("DEV-7")
        self.assert_published()
        self.assertEqual(snapshot(self.path, self.base, self.branch).content, before)
        self.assertEqual(self.command(self.path, "show", "HEAD:new.txt"), "final unstaged result")

    def test_hook_mutation_never_reaches_remote(self):
        self.prepare()
        hooks = self.repo / ".git" / "hooks"
        hook = hooks / "pre-commit"
        hook.write_text("#!/bin/sh\nprintf 'hook mutation' > new.txt\ngit add new.txt\n")
        hook.chmod(0o755)
        with self.assertRaisesRegex(TaskError, "drifted|exact recorded"):
            publish("DEV-7")
        self.assertFalse(self.operations("push"))

    def test_pre_push_amend_stops_publication_and_retry_despite_equivalent_commit(self):
        self.assert_pre_push_amend_refuses(fail_push=False)

    def test_failed_pre_push_amend_keeps_frozen_sha_for_retry_even_without_remote_branch(self):
        self.assert_pre_push_amend_refuses(fail_push=True)

    def assert_pre_push_amend_refuses(self, *, fail_push):
        self.prepare()
        hook = self.repo / ".git" / "hooks" / "pre-push"
        # Change only author identity: parent, tree and message remain equal,
        # but the explicit push refspec still names the pre-hook commit SHA.
        hook.write_text("#!/bin/sh\ngit -c commit.gpgSign=false commit --amend --no-edit "
                        "--author='Hook Author <hook@example.test>' || exit 1\n"
                        f"exit {1 if fail_push else 0}\n")
        hook.chmod(0o755)
        for attempt in range(2):
            with patch("sys.stdout", new=io.StringIO()) as output, patch("sys.stderr", new=io.StringIO()) as errors:
                self.assertEqual(cli.main(["pr", "DEV-7"]), 1)
            frozen = self.store.read()["intent"]["publishing_head"]
            changed = self.command(self.path, "rev-parse", "HEAD")
            self.assertNotEqual(changed, frozen)
            self.assertIn(f"Local HEAD {changed} differs from frozen publishing SHA {frozen}", errors.getvalue())
            self.assertEqual(output.getvalue(), "")  # No PR URL is reported.
            for fmt in ("%P", "%T", "%B"):
                self.assertEqual(self.command(self.path, "show", "-s", f"--format={fmt}", frozen),
                                 self.command(self.path, "show", "-s", f"--format={fmt}", changed))
            remote = self.command(self.remote, "for-each-ref", "--format=%(objectname)", f"refs/heads/{self.branch}")
            self.assertEqual(remote, "" if fail_push else frozen)
            self.assertFalse(self.pulls)
            self.assertFalse(any(call[0] in {"POST", "PATCH"} for call in self.calls))
            self.assertEqual(len(self.operations("commit")), 1)
            self.assertEqual(len(self.operations("push")), 1)
            self.assertIn(f"{frozen}:refs/heads/{self.branch}", self.operations("push")[0].args)
            if attempt == 0:
                hook.unlink()
                transports = self.native.call_count
            else:
                self.assertEqual(self.native.call_count, transports)

    def amend_publication(self):
        self.command(self.path, "-c", "commit.gpgSign=false", "commit", "--amend", "--no-edit",
                     "--author=Changed Author <changed@example.test>")

    def test_head_changed_during_pr_lookup_refuses_before_creation(self):
        self.prepare()
        def mutate_on_lookup(path, branch, **kwargs):
            result = self.api(path, branch, **kwargs)
            if self.operations("push"):
                self.amend_publication()
            return result
        with patch("task_start.github.request", side_effect=mutate_on_lookup):
            with self.assertRaisesRegex(TaskError, "differs from frozen publishing SHA"):
                publish("DEV-7")
        self.assertFalse(self.pulls)
        self.assertFalse(any(call[0] in {"POST", "PATCH"} for call in self.calls))
        self.assertEqual(len(self.operations("push")), 1)

    def test_head_changed_during_final_remote_check_refuses_to_report_pr(self):
        self.prepare()
        publish("DEV-7")
        reporting = False
        def ready_to_report(*args, **kwargs):
            nonlocal reporting
            result = publish_pull(*args, **kwargs)
            reporting = True
            return result
        def mutate_after_remote(path, operation, *args, **kwargs):
            result = native_git(path, operation, *args, **kwargs)
            if operation == "ls-remote" and reporting:
                self.amend_publication()
            return result
        self.native.reset_mock()
        self.native.side_effect = mutate_after_remote
        with patch("task_start.publish.publish_pull", side_effect=ready_to_report), \
                patch("sys.stdout", new=io.StringIO()) as output, patch("sys.stderr", new=io.StringIO()) as errors:
            self.assertEqual(cli.main(["pr", "DEV-7"]), 1)
        self.assertEqual(output.getvalue(), "")
        self.assertIn("differs from frozen publishing SHA", errors.getvalue())
        with self.assertRaisesRegex(TaskError, "differs from frozen publishing SHA"):
            publish("DEV-7")
        self.assertFalse(self.operations("commit"))
        self.assertFalse(self.operations("push"))
        self.assertEqual(sum(call[0] == "POST" for call in self.calls), 1)

    def test_selected_rebased_commit_is_frozen_before_transport(self):
        self.prepare()
        self.assert_rebased(self.advance_remote())
        self.assertEqual(review("DEV-7").state, "clean")
        head = self.command(self.path, "rev-parse", "HEAD")
        def mutate_after_remote(path, operation, *args, **kwargs):
            result = native_git(path, operation, *args, **kwargs)
            if operation == "ls-remote":
                self.assertEqual(self.store.read()["intent"]["publishing_head"], head)
                self.amend_publication()
            return result
        self.native.side_effect = mutate_after_remote
        with self.assertRaisesRegex(TaskError, "differs from frozen publishing SHA"):
            publish("DEV-7")
        self.assertFalse(self.operations("commit"))
        self.assertFalse(self.operations("push"))
        self.assertFalse(self.pulls)

    def advance_remote(self, *, conflict=False):
        (self.remote / ("tracked.txt" if conflict else "upstream.txt")).write_text("new base\n")
        self.command(self.remote, "add", ".")
        self.command(self.remote, "commit", "-m", "advance base")
        return self.command(self.remote, "rev-parse", "main")

    def assert_rebased(self, base):
        with self.assertRaisesRegex(TaskError, "new independent task review"):
            publish("DEV-7")
        self.assertEqual(self.command(self.repo, "rev-parse", "HEAD"), base)
        self.assertEqual(self.command(self.path, "rev-parse", "HEAD^"), base)
        self.assertEqual(self.command(self.path, "status", "--porcelain"), "")
        self.assertEqual((self.path / "upstream.txt").read_text(), "new base\n")
        self.assertEqual((self.path / "tracked.txt").read_text(), "implemented after steering\n")
        self.assertIsNone(self.store.read()["acceptance"])
        self.assertFalse(self.operations("push"))
        self.assertFalse(self.pulls)
        return self.command(self.path, "rev-parse", "HEAD")

    def test_unpublished_rebase_requires_review_then_reuses_the_commit(self):
        self.prepare()
        base = self.advance_remote()
        head = self.assert_rebased(base)
        with self.assertRaisesRegex(TaskError, "No current clean"):
            publish("DEV-7")
        self.verdict_overrides = dict(state="blocked", publication=PUBLIC)
        self.assertEqual(review("DEV-7").state, "blocked")
        with self.assertRaisesRegex(TaskError, "No current clean"):
            publish("DEV-7")
        self.verdict_overrides = dict(publication=PUBLIC)
        fresh = review("DEV-7")
        self.assertEqual(fresh.state, "clean")
        self.assertNotEqual(fresh.pass_id, self.accepted_result.pass_id)
        self.assertEqual(fresh.review_state["base_commit"], base)
        publish("DEV-7")
        publish("DEV-7")
        self.base = base
        self.assertEqual(self.assert_published(), head)
        self.assertEqual(len(self.operations("commit-tree")), 1)
        self.assertFalse(self.operations("commit"))
        self.assertEqual(len(self.operations("push")), 1)
        self.assertEqual(sum(c[0] == "POST" for c in self.calls), 1)

    def test_fresh_review_wording_cannot_change_frozen_rebase_publication(self):
        self.prepare()
        base = self.advance_remote()
        head = self.assert_rebased(base)
        frozen = self.store.read()["rebase"]["publication"]
        # Both the ordinary review conclusion and optional publication prose may
        # differ. The explicit approval is for the original workflow metadata.
        self.verdict_overrides = dict(summary="The rebased publication workflow is correct.",
            publication=dict(summary="safely publish completed tasks", description="Publish accepted task changes with reliable retries.",
                             validation="The relevant offline checks still pass on the new base."))
        fresh = review("DEV-7")
        self.assertEqual(fresh.state, "clean")
        self.assertEqual(fresh.summary, self.verdict_overrides["summary"])
        prompt = self.prompts[-1]
        metadata = json.loads(prompt.split("RESOLVED REVIEW METADATA\n", 1)[1]
                              .split("\n\nLATEST LINEAR REQUIREMENTS", 1)[0])
        self.assertEqual(metadata["frozen_publication"], dict(**frozen, fingerprint=publication_fingerprint(frozen)))
        self.assertIn("Independently validate their accuracy against the rebased result", prompt)
        self.assertIn("Do not rewrite or regenerate", prompt)
        accepted = self.store.read()["acceptance"]
        self.assertEqual(accepted["publication_approval"], publication_fingerprint(frozen))
        self.assertEqual(accepted["review_state"], self.store.read()["rebase"]["result"])
        self.github_failure = TaskError("PR acknowledgement lost")
        with patch("task_start.publish.prepare_metadata", side_effect=AssertionError("Do not regenerate frozen metadata")):
            with self.assertRaisesRegex(TaskError, "PR acknowledgement lost"):
                publish("DEV-7")
            self.github_failure = None
            publish("DEV-7")
            publish("DEV-7")
        self.base = base
        self.assertEqual(self.assert_published(), head)
        self.assertEqual({key: self.pulls[0][key] for key in frozen}, frozen)
        self.assertEqual(len(self.operations("commit-tree")), 1)
        self.assertFalse(self.operations("commit"))
        self.assertEqual(len(self.operations("push")), 1)
        self.assertEqual(sum(c[0] == "POST" for c in self.calls), 1)
        self.assertFalse(any(c[0] == "PATCH" for c in self.calls))

    def test_rebased_clean_review_requires_explicit_matching_metadata_approval(self):
        self.prepare()
        self.assert_rebased(self.advance_remote())
        self.approve_frozen_publication = False
        for approval in (None, "0" * 64):
            self.verdict_overrides = {} if approval is None else dict(publication_approval=approval)
            with self.subTest(approval=approval):
                result = review("DEV-7")
                self.assertEqual(result.state, "failed")
                self.assertIn("frozen publication metadata", result.summary)
                self.assertIsNone(self.store.read()["acceptance"])
                with self.assertRaisesRegex(TaskError, "No current clean"):
                    publish("DEV-7")
        self.assertFalse(self.operations("push"))
        self.approve_frozen_publication = True
        self.verdict_overrides = dict(summary="The original public metadata remains accurate after rebase.")
        self.assertEqual(review("DEV-7").state, "clean")
        # Fresh approval needs no replacement publication summary at all.
        self.assertIsNone(self.store.read()["acceptance"]["publication"])
        publish("DEV-7")
        self.assertFalse(self.operations("commit"))

    def test_frozen_metadata_and_intent_drift_refuse_even_on_committed_retry(self):
        self.prepare()
        self.assert_rebased(self.advance_remote())
        self.assertEqual(review("DEV-7").state, "clean")
        with patch("task_start.publish.push", side_effect=TaskError("offline")):
            with self.assertRaisesRegex(TaskError, "offline"):
                publish("DEV-7")
        saved = self.store.read()
        for mutation in ("frozen", "approval", "intent", "fingerprint"):
            changed = copy.deepcopy(saved)
            if mutation == "frozen":
                changed["rebase"]["publication"]["body"] += "\nUnreviewed text.\n"
            elif mutation == "approval":
                changed["acceptance"].pop("publication_approval")
            elif mutation == "intent":
                changed["intent"]["body"] += "\nUnreviewed text.\n"
            else:
                changed["acceptance"]["review_state"]["fingerprint"] = "0" * 64
            self.store.write(changed)
            with self.subTest(mutation=mutation), self.assertRaisesRegex(TaskError, "[Ff]rozen|rebased"):
                publish("DEV-7")
        self.store.write(saved)
        self.assertFalse(self.operations("push"))
        publish("DEV-7")
        self.assertFalse(self.operations("commit"))
        self.assertEqual(len(self.operations("commit-tree")), 1)

    def test_frozen_metadata_change_during_review_invalidates_the_pass(self):
        self.prepare()
        self.assert_rebased(self.advance_remote())
        def change_metadata():
            changed = self.store.read()
            changed["rebase"]["publication"]["body"] += "\nChanged during review.\n"
            self.store.write(changed)
        self.mutation = change_metadata
        result = review("DEV-7")
        self.assertEqual(result.state, "blocked")
        self.assertTrue(result.invalidated)
        self.assertIsNone(self.store.read()["acceptance"])
        self.assertIn("Changed during review", self.store.read()["rebase"]["publication"]["body"])
        with self.assertRaisesRegex(TaskError, "Frozen publication metadata changed"):
            publish("DEV-7")
        self.assertFalse(self.operations("push"))

    def test_legacy_rebase_without_frozen_metadata_refuses_instead_of_regenerating(self):
        self.prepare()
        self.assert_rebased(self.advance_remote())
        saved = self.store.read()
        saved["rebase"].pop("publication")
        saved["rebase"].pop("publication_fingerprint")
        self.store.write(saved)
        with patch("task_start.publish.prepare_metadata", side_effect=AssertionError("Cannot invent lost metadata")):
            with self.assertRaisesRegex(TaskError, "frozen publication metadata"):
                publish("DEV-7")
        with self.assertRaisesRegex(TaskError, "frozen publication metadata"):
            review("DEV-7")
        self.assertFalse(self.operations("push"))

    def test_disposable_rebase_conflict_leaves_task_and_acceptance_untouched(self):
        self.prepare()
        # Preserve a partially staged reviewed index too.
        self.command(self.path, "add", "tracked.txt")
        self.assertEqual(review("DEV-7").state, "clean")
        before = snapshot(self.path, self.base, self.branch)
        index = Path(self.command(self.path, "rev-parse", "--path-format=absolute", "--git-path", "index"))
        original_index = index.read_bytes()
        saved = self.store.read()
        base = self.advance_remote(conflict=True)
        for attempt in range(2):
            with self.subTest(attempt=attempt), self.assertRaisesRegex(TaskError, "Disposable rebase conflicts"):
                publish("DEV-7")
            self.assertEqual(snapshot(self.path, self.base, self.branch), before)
            self.assertEqual(index.read_bytes(), original_index)
            self.assertEqual(self.store.read(), saved)
        self.assertEqual(self.command(self.repo, "rev-parse", "HEAD"), base)
        self.assertFalse(self.operations("commit"))
        self.assertFalse(self.operations("commit-tree"))
        self.assertFalse(self.operations("push"))

    def test_base_already_updated_locally_still_rebases_unpublished_review(self):
        self.prepare()
        base = self.advance_remote()
        self.command(self.repo, "fetch", "origin", "main")
        self.command(self.repo, "merge", "--ff-only", base)
        self.assert_rebased(base)

    def advance_existing_upstream_file(self):
        # The live failure changed a tracked upstream file, unlike the earlier
        # fixtures which only added upstream.txt. The task does not edit it.
        (self.remote / ".gitignore").write_text("ignored.txt\nupstream-ignore\n")
        return self.advance_remote()

    def test_base_advance_updates_existing_file_without_staging_real_index_during_probe(self):
        self.prepare()
        self.command(self.path, "add", "tracked.txt")
        self.assertEqual(review("DEV-7").state, "clean")
        before = snapshot(self.path, self.base, self.branch)
        index = Path(self.command(self.path, "rev-parse", "--path-format=absolute", "--git-path", "index"))
        original_index = index.read_bytes()
        base = self.advance_existing_upstream_file()
        def assert_untouched_before_install(path, operation, *args, **kwargs):
            if operation == "commit-tree":
                self.assertEqual(self.command(self.repo, "rev-parse", "HEAD"), base)
                self.assertEqual(snapshot(self.path, self.base, self.branch), before)
                self.assertEqual(index.read_bytes(), original_index)
            return native_git(path, operation, *args, **kwargs)
        self.native.side_effect = assert_untouched_before_install
        self.assert_rebased(base)
        self.assertEqual((self.path / ".gitignore").read_text(), "ignored.txt\nupstream-ignore\n")
        self.assertEqual(len(self.operations("commit-tree")), 1)

    def test_base_advance_checkout_setup_failure_preserves_exact_reviewed_index(self):
        self.prepare()
        self.command(self.path, "add", "tracked.txt")
        self.assertEqual(review("DEV-7").state, "clean")
        before = snapshot(self.path, self.base, self.branch)
        index = Path(self.command(self.path, "rev-parse", "--path-format=absolute", "--git-path", "index"))
        original_index = index.read_bytes()
        saved = self.store.read()
        base = self.advance_existing_upstream_file()
        from task_start.publication_rebase import run
        for phase in ("init", "checkout", "update-index", "--dry-run"):
            def refuse_checkout_setup(args, **kwargs):
                if phase in args:
                    raise TaskError("fixture checkout setup failure")
                return run(args, **kwargs)
            with patch("task_start.publication_rebase.run", side_effect=refuse_checkout_setup):
                for attempt in range(2):
                    with self.subTest(phase=phase, attempt=attempt), self.assertRaisesRegex(TaskError, "checkout preflight|Disposable rebase setup"):
                        publish("DEV-7")
                    self.assertEqual(self.command(self.repo, "rev-parse", "HEAD"), base)
                    self.assertEqual(snapshot(self.path, self.base, self.branch), before)
                    self.assertEqual(index.read_bytes(), original_index)
                    self.assertEqual(self.store.read(), saved)
        self.assertFalse(self.operations("commit-tree"))
        self.assertFalse(self.operations("commit"))
        self.assertFalse(self.operations("push"))
        self.assertFalse(self.pulls)

    def test_rebase_checkout_failure_keeps_original_index_and_reuses_planned_commit(self):
        self.prepare()
        before = snapshot(self.path, self.base, self.branch)
        index = Path(self.command(self.path, "rev-parse", "--path-format=absolute", "--git-path", "index"))
        original_index = index.read_bytes()
        base = self.advance_existing_upstream_file()
        from task_start.publication_rebase import run
        def fail_checkout(args, **kwargs):
            if args[2] == str(self.path) and "read-tree" in args and "-u" in args and "--dry-run" not in args:
                raise TaskError("fixture checkout failed (exit 128)")
            return run(args, **kwargs)
        with patch("task_start.publication_rebase.run", side_effect=fail_checkout):
            with self.assertRaisesRegex(TaskError, "checkout installation failed"):
                publish("DEV-7")
        self.assertEqual(snapshot(self.path, self.base, self.branch), before)
        self.assertEqual(index.read_bytes(), original_index)
        self.assertFalse(self.pulls)
        planned = self.store.read()["rebase"]["commits"][-1]
        self.assertEqual(self.assert_rebased(base), planned)
        self.assertEqual(len(self.operations("commit-tree")), 1)

    def test_rebase_checkout_honors_existing_real_index_lock_without_mutation(self):
        self.prepare()
        before = snapshot(self.path, self.base, self.branch)
        index = Path(self.command(self.path, "rev-parse", "--path-format=absolute", "--git-path", "index"))
        original_index = index.read_bytes()
        base = self.advance_existing_upstream_file()
        lock = index.with_name(index.name + ".lock")
        lock.write_bytes(b"another Git operation")
        with self.assertRaisesRegex(TaskError, "index is locked"):
            publish("DEV-7")
        self.assertEqual(lock.read_bytes(), b"another Git operation")
        self.assertEqual(snapshot(self.path, self.base, self.branch), before)
        self.assertEqual(index.read_bytes(), original_index)
        lock.unlink()
        self.assert_rebased(base)
        self.assertEqual(len(self.operations("commit-tree")), 1)

    def test_rebase_checkout_finished_before_index_replacement_recovers_without_replaying(self):
        self.prepare()
        index = Path(self.command(self.path, "rev-parse", "--path-format=absolute", "--git-path", "index"))
        original_index = index.read_bytes()
        base = self.advance_existing_upstream_file()
        replace_index = os.replace
        def fail_index_replace(source, target):
            if target == index:
                raise OSError("fixture index replacement failure")
            return replace_index(source, target)
        with patch("task_start.publication_rebase.os.replace", side_effect=fail_index_replace):
            with self.assertRaisesRegex(TaskError, "installation was not confirmed"):
                publish("DEV-7")
        self.assertEqual(index.read_bytes(), original_index)
        self.assertEqual(self.command(self.path, "rev-parse", "HEAD"), self.base)
        self.assertEqual((self.path / ".gitignore").read_text(), "ignored.txt\nupstream-ignore\n")
        self.assertEqual((self.path / "upstream.txt").read_text(), "new base\n")
        self.assertFalse(index.with_name(index.name + ".lock").exists())
        planned = self.store.read()["rebase"]["commits"][-1]
        self.assertEqual(self.assert_rebased(base), planned)
        self.assertEqual(len(self.operations("commit-tree")), 1)

    def test_base_advance_after_commit_before_push_rebases_without_extra_commit(self):
        self.prepare()
        with patch("task_start.publish.push", side_effect=TaskError("offline")):
            with self.assertRaisesRegex(TaskError, "offline"):
                publish("DEV-7")
        old_head = self.command(self.path, "rev-parse", "HEAD")
        base = self.advance_remote()
        head = self.assert_rebased(base)
        self.assertNotEqual(head, old_head)
        self.assertEqual(review("DEV-7").state, "clean")
        publish("DEV-7")
        self.base = base
        self.assertEqual(self.assert_published(), head)
        self.assertEqual(len(self.operations("commit")), 1)

    def test_confirmed_publication_retry_allows_new_base_without_rebase(self):
        self.prepare()
        url = publish("DEV-7")
        before = snapshot(self.path, self.base, self.branch)
        self.advance_remote()
        self.assertEqual(publish("DEV-7"), url)
        self.assertEqual(snapshot(self.path, self.base, self.branch), before)
        # A surviving PR also prevents rewriting if its remote branch vanished.
        self.pulls[0]["follow_remote"] = False
        self.command(self.remote, "update-ref", "-d", f"refs/heads/{self.branch}")
        with self.assertRaisesRegex(TaskError, "already published"):
            publish("DEV-7")
        self.assertEqual(snapshot(self.path, self.base, self.branch), before)
        self.assertFalse(self.operations("fetch"))
        self.assertFalse(self.operations("commit-tree"))
        self.assertEqual(len(self.operations("push")), 1)

    def test_rebase_install_interruption_recovers_without_duplicate_commits(self):
        self.prepare()
        base = self.advance_remote()
        real = Git.command
        def lose_ack(git, *args):
            result = real(git, *args)
            if git.repo == self.path and args[0] == "update-ref":
                raise TaskError("lost branch update acknowledgement")
            return result
        with patch.object(Git, "command", lose_ack):
            with self.assertRaisesRegex(TaskError, "lost branch update"):
                publish("DEV-7")
        self.assertIsNone(self.store.read()["acceptance"])
        self.assertIsNone(self.store.read()["rebase"]["result"])
        with self.assertRaisesRegex(TaskError, "rebase is pending"):
            review("DEV-7")
        head = self.assert_rebased(base)
        self.assertEqual(len(self.operations("commit-tree")), 1)
        self.assertEqual(review("DEV-7").state, "clean")
        publish("DEV-7")
        self.base = base
        self.assertEqual(self.assert_published(), head)

    def test_rebase_preserves_linear_implementation_commits(self):
        self.prepare()
        self.command(self.path, "add", "tracked.txt")
        self.command(self.path, "commit", "-m", "intermediate implementation")
        self.assertEqual(review("DEV-7").state, "clean")
        base = self.advance_remote()
        with self.assertRaisesRegex(TaskError, "new independent task review"):
            publish("DEV-7")
        head = self.command(self.path, "rev-parse", "HEAD")
        self.assertEqual(self.command(self.path, "rev-list", "--count", f"{base}..HEAD"), "2")
        self.assertEqual(self.command(self.path, "show", "-s", "--format=%s", "HEAD^"), "intermediate implementation")
        self.assertEqual(review("DEV-7").state, "clean")
        publish("DEV-7")
        self.assertEqual(self.command(self.path, "rev-parse", "HEAD"), head)
        self.assertEqual(len(self.operations("commit-tree")), 2)

    def test_rebase_respects_signing_failure_and_retry(self):
        self.prepare()
        base = self.advance_remote()
        helper = self.repo.parent / "gpg-fixture"
        helper.write_text("#!/bin/sh\necho 'fixture signing refusal' >&2\nexit 1\n")
        helper.chmod(0o755)
        self.command(self.path, "config", "commit.gpgSign", "true")
        self.command(self.path, "config", "gpg.program", str(helper))
        before = snapshot(self.path, self.base, self.branch)
        with self.assertRaisesRegex(TaskError, "authentication/signing"):
            publish("DEV-7")
        self.assertEqual(snapshot(self.path, self.base, self.branch), before)
        self.assertIsNone(self.store.read()["acceptance"])
        self.command(self.path, "config", "commit.gpgSign", "false")
        self.assert_rebased(base)

    def test_reviewed_state_drift_never_starts_base_update_or_rebase(self):
        self.prepare()
        self.advance_remote()
        (self.path / "new.txt").write_text("unreviewed")
        with self.assertRaisesRegex(TaskError, "drifted"):
            publish("DEV-7")
        self.assertFalse(self.operations("fetch"))
        self.assertIsNone(self.store.read()["rebase"])

    def test_rebase_preserves_ignored_files_obstructing_upstream_paths(self):
        self.prepare()
        exclude = self.repo / ".git" / "info" / "exclude"
        exclude.write_text(exclude.read_text() + "\nupstream.txt\n")
        ignored = self.path / "upstream.txt"
        ignored.write_text("private ignored content")
        self.advance_remote()
        with self.assertRaises(TaskError):
            publish("DEV-7")
        self.assertEqual(ignored.read_text(), "private ignored content")
        self.assertEqual(self.command(self.path, "rev-parse", "HEAD"), self.base)
        self.assertFalse(self.operations("push"))

    def test_checkout_completed_before_ref_update_can_resume(self):
        self.prepare()
        base = self.advance_remote()
        real = Git.command
        def stop_before_ref(git, *args):
            if git.repo == self.path and args[0] == "update-ref":
                raise TaskError("interrupted before branch update")
            return real(git, *args)
        with patch.object(Git, "command", stop_before_ref):
            with self.assertRaisesRegex(TaskError, "interrupted before branch"):
                publish("DEV-7")
        self.assertEqual(self.command(self.path, "rev-parse", "HEAD"), self.base)
        self.assertEqual((self.path / "upstream.txt").read_text(), "new base\n")
        self.assert_rebased(base)
        self.assertEqual(len(self.operations("commit-tree")), 1)

    def test_divergent_local_base_refuses_without_changing_task(self):
        self.prepare()
        self.advance_remote()
        (self.repo / "local.txt").write_text("local-only base change")
        self.command(self.repo, "add", ".")
        self.command(self.repo, "commit", "-m", "local base")
        before = snapshot(self.path, self.base, self.branch)
        with self.assertRaisesRegex(TaskError, "diverged|local-only"):
            publish("DEV-7")
        self.assertEqual(snapshot(self.path, self.base, self.branch), before)
        self.assertIsNone(self.store.read()["rebase"])

    def test_remote_branch_appearing_during_probe_prevents_installation(self):
        self.prepare()
        self.advance_remote()
        from task_start.publication_rebase import integration_plan
        def race(*args):
            plan = integration_plan(*args)
            self.command(self.remote, "branch", self.branch, self.base)
            return plan
        before = snapshot(self.path, self.base, self.branch)
        with patch("task_start.publish.integration_plan", side_effect=race):
            with self.assertRaisesRegex(TaskError, "already published"):
                publish("DEV-7")
        self.assertEqual(snapshot(self.path, self.base, self.branch), before)
        self.assertFalse(self.operations("commit-tree"))
        self.assertFalse(self.operations("push"))

    def test_intent_write_failure_never_commits(self):
        self.prepare()
        with patch.object(PublicationStore, "write", side_effect=TaskError("disk unavailable")):
            with self.assertRaisesRegex(TaskError, "disk unavailable"):
                publish("DEV-7")
        self.assertFalse(self.operations("commit"))
        self.assertFalse(self.operations("push"))

    def test_frozen_sha_write_failure_stops_before_push_and_recovers_the_same_commit(self):
        self.prepare()
        write = PublicationStore.write
        def fail_freeze(store, value):
            if value["intent"].get("publishing_head"):
                raise TaskError("cannot persist frozen SHA")
            return write(store, value)
        with patch.object(PublicationStore, "write", new=fail_freeze):
            with self.assertRaisesRegex(TaskError, "cannot persist frozen SHA"):
                publish("DEV-7")
        head = self.command(self.path, "rev-parse", "HEAD")
        self.assertFalse(self.operations("push"))
        self.assertFalse(self.pulls)
        publish("DEV-7")
        self.assertEqual(self.assert_published(), head)
        self.assertEqual(self.store.read()["intent"]["publishing_head"], head)
        self.assertEqual(len(self.operations("commit")), 1)

    def test_reviewed_ignored_remnant_of_staged_deletion_refuses_before_commit(self):
        self.prepare()
        self.command(self.path, "rm", "--cached", "tracked.txt")
        with (self.path / ".gitignore").open("a") as ignore:
            ignore.write("tracked.txt\n")
        self.assertEqual(review("DEV-7").state, "clean")
        with self.assertRaisesRegex(TaskError, "cannot represent"):
            publish("DEV-7")
        self.assertFalse(self.operations("commit"))

    def test_unrepresentable_mode_refuses_before_commit(self):
        self.prepare()
        self.command(self.path, "config", "core.filemode", "false")
        (self.path / "tracked.txt").chmod(0o755)
        self.assertEqual(review("DEV-7").state, "clean")
        with self.assertRaisesRegex(TaskError, "cannot represent"):
            publish("DEV-7")
        self.assertFalse(self.operations("commit"))

    def test_clean_filter_cannot_publish_different_bytes_than_reviewed(self):
        self.prepare()
        (self.path / ".gitattributes").write_text("tracked.txt filter=transform\n")
        self.command(self.path, "config", "filter.transform.clean", "sed s/implemented/unreviewed/")
        self.assertEqual(review("DEV-7").state, "clean")
        with self.assertRaisesRegex(TaskError, "cannot represent"):
            publish("DEV-7")
        self.assertFalse(self.operations("commit"))

    def test_review_started_after_publication_explicitly_establishes_new_contract(self):
        self.prepare()
        publish("DEV-7")
        first = self.command(self.path, "rev-parse", "HEAD")
        (self.path / "new.txt").write_text("follow-up change")
        self.assertEqual(review("DEV-7").state, "clean")
        self.assertIsNone(self.store.read()["intent"])
        publish("DEV-7")
        self.assertEqual(self.command(self.path, "rev-parse", "HEAD^"), first)
        self.assertEqual(sum(c[0] == "POST" for c in self.calls), 1)

    def followup_review(self, number=2):
        (self.path / "new.txt").write_text(f"reviewed follow-up {number}\n")
        self.verdict_overrides = dict(publication=dict(PUBLIC, summary=f"add follow-up {number}"))
        self.assertEqual(review("DEV-7").state, "clean")
        return self.store.read()["acceptance"]

    def propagation_api(self, previous, pairs):
        """Override only observed PR heads after the follow-up push, not Git refs."""
        observation = 0
        def api(path, branch, **kwargs):
            nonlocal observation
            result = self.api(path, branch, **kwargs)
            if kwargs.get("method", "GET") == "GET" and len(self.operations("push")) == 2:
                listed = "?" in path
                if listed:
                    observation += 1
                if observation <= len(pairs) and pairs[observation - 1][0 if listed else 1] == "old":
                    (result[0] if listed else result)["head"]["sha"] = previous
            return result
        return api

    def test_followup_pr_propagation_converges_before_update_and_reporting(self):
        self.prepare()
        url = publish("DEV-7")
        previous = self.command(self.path, "rev-parse", "HEAD")
        self.followup_review()
        pairs = [("old", "new"), ("new", "old"), ("old", "old"), ("new", "new"),
                 ("old", "new"), ("new", "new")]
        with patch("task_start.github.request", side_effect=self.propagation_api(previous, pairs)), \
                patch("task_start.github.sleep") as sleep:
            self.assertEqual(publish("DEV-7"), url)
        self.assertEqual(sleep.call_count, 4)
        self.assertEqual(self.command(self.path, "rev-parse", "HEAD^"), previous)
        self.assertEqual(self.store.read()["publication_history"]["cycles"][-1]["state"], "complete")
        self.assertEqual(len(self.operations("commit")), 2)
        self.assertEqual(len(self.operations("push")), 2)
        self.assertEqual(sum(c[0] == "POST" for c in self.calls), 1)
        self.assertEqual(sum(c[0] == "PATCH" for c in self.calls), 1)

    def test_followup_pr_propagation_stops_then_retry_rereads_without_duplicate_transport(self):
        self.prepare()
        url = publish("DEV-7")
        previous = self.command(self.path, "rev-parse", "HEAD")
        self.followup_review()
        with patch("task_start.github.request", side_effect=self.propagation_api(previous, [("old", "old")] * 5)), \
                patch("task_start.github.sleep") as sleep:
            with self.assertRaisesRegex(TaskError, "Conflicting PR repository/head/base/state"):
                publish("DEV-7")
        self.assertEqual(sleep.call_count, 4)
        head = self.command(self.path, "rev-parse", "HEAD")
        self.assertEqual(self.command(self.remote, "rev-parse", self.branch), head)
        self.assertEqual(self.store.read()["intent"]["publishing_head"], head)
        self.assertEqual(self.store.read()["publication_history"]["head"], head)
        self.assertEqual(self.store.read()["publication_history"]["cycles"][-1]["state"], "published")
        self.assertFalse(any(c[0] == "PATCH" for c in self.calls))
        with patch("task_start.github.request", side_effect=self.propagation_api(previous, [("old", "new")])), \
                patch("task_start.github.sleep") as sleep:
            self.assertEqual(publish("DEV-7"), url)
        self.assertEqual(sleep.call_count, 1)  # Recovery preflight also waits for the same PR.
        self.assertEqual(len(self.operations("commit")), 2)
        self.assertEqual(len(self.operations("push")), 2)
        self.assertEqual(sum(c[0] == "POST" for c in self.calls), 1)
        self.assertEqual(sum(c[0] == "PATCH" for c in self.calls), 1)

    def test_followup_pr_propagation_requires_remote_still_at_frozen_sha(self):
        self.prepare()
        publish("DEV-7")
        previous = self.command(self.path, "rev-parse", "HEAD")
        self.followup_review()
        stale = self.propagation_api(previous, [("old", "new")])
        def moved_remote(path, branch, **kwargs):
            result = stale(path, branch, **kwargs)
            if len(self.operations("push")) == 2 and kwargs.get("method", "GET") == "GET" and "?" not in path:
                self.command(self.remote, "update-ref", f"refs/heads/{self.branch}", previous)
            return result
        with patch("task_start.github.request", side_effect=moved_remote), \
                patch("task_start.github.sleep") as sleep:
            with self.assertRaisesRegex(TaskError, "Remote task branch conflicts"):
                publish("DEV-7")
        sleep.assert_not_called()
        self.assertFalse(any(c[0] == "PATCH" for c in self.calls))
        self.assertEqual(len(self.operations("push")), 2)

    def test_multiple_followups_preserve_every_review_commit_and_pr(self):
        self.prepare()
        url = publish("DEV-7")
        for number in (2, 3, 4):
            previous = self.store.read()["publication_history"]
            accepted = self.followup_review(number)
            self.assertEqual(publish("DEV-7"), url)
            history = self.store.read()["publication_history"]
            head = self.command(self.path, "rev-parse", "HEAD")
            self.assertEqual(self.command(self.path, "rev-parse", "HEAD^"), previous["head"])
            self.assertEqual(self.command(self.remote, "rev-parse", self.branch), head)
            self.assertEqual(history["head"], head)
            self.assertEqual(history["cycles"][:-1], previous["cycles"])
            self.assertEqual(history["cycles"][-1]["acceptance"], accepted)
            self.assertEqual(history["cycles"][-1]["intent"], self.store.read()["intent"])
            self.assertEqual(history["cycles"][-1]["state"], "complete")
            self.assertEqual(snapshot(self.path, self.base, self.branch).content, accepted["review_state"]["content"])
            self.assertIn(accepted["pass_id"], self.command(self.path, "show", "-s", "--format=%B"))
            self.assertEqual(self.pulls[0]["title"], f"feat: add follow-up {number} (DEV-7)")
            self.assertEqual(publish("DEV-7"), url)
            self.assertEqual(self.store.read()["publication_history"], history)
        self.assertEqual(self.command(self.path, "rev-list", "--count", f"{self.base}..HEAD"), "4")
        self.assertEqual(len(self.operations("commit")), 4)
        self.assertEqual(len(self.operations("push")), 4)
        self.assertEqual(sum(c[0] == "POST" for c in self.calls), 1)
        self.assertEqual(sum(c[0] == "PATCH" for c in self.calls), 3)
        for call in self.operations("push"):
            self.assertFalse(any(str(arg).startswith(("+", "--force")) for arg in call.args))
        self.assertFalse(self.operations("fetch"))
        self.assertFalse(self.operations("commit-tree"))

    def assert_followup_retry(self, failure):
        self.prepare()
        url = publish("DEV-7")
        previous = self.store.read()["publication_history"]
        self.followup_review()
        def interrupted_native(path, operation, *args, **kwargs):
            result = native_git(path, operation, *args, **kwargs)
            if operation == failure:
                raise TaskError("lost acknowledgement")
            return result
        def interrupted_api(path, branch, **kwargs):
            result = self.api(path, branch, **kwargs)
            if kwargs.get("method") == failure:
                raise TaskError("lost acknowledgement")
            return result
        self.native.side_effect = interrupted_native
        with patch("task_start.github.request", side_effect=interrupted_api):
            with self.assertRaisesRegex(TaskError, "lost acknowledgement"):
                publish("DEV-7")
        self.native.side_effect = None
        frozen = self.command(self.path, "rev-parse", "HEAD")
        saved = self.store.read()
        with self.assertRaisesRegex(TaskError, "unfinished.*rerun task pr"):
            review("DEV-7")
        self.assertEqual(self.store.read(), saved)
        if failure == "push":
            self.assertEqual(saved["publication_history"]["head"], previous["head"])
            self.assertEqual(saved["publication_history"]["cycles"][-1]["state"], "pending")
        self.assertEqual(publish("DEV-7"), url)
        self.assertEqual(self.command(self.path, "rev-parse", "HEAD"), frozen)
        self.assertEqual(self.command(self.path, "rev-parse", "HEAD^"), previous["head"])
        self.assertEqual(self.store.read()["publication_history"]["cycles"][:-1], previous["cycles"])
        self.assertEqual(self.store.read()["publication_history"]["head"], frozen)
        self.assertEqual(len(self.operations("commit")), 2)
        self.assertEqual(len(self.operations("push")), 2)
        self.assertEqual(sum(c[0] == "POST" for c in self.calls), 1)
        self.assertEqual(sum(c[0] == "PATCH" for c in self.calls), 1)

    def test_followup_recovers_commit_with_lost_acknowledgement(self):
        self.assert_followup_retry("commit")

    def test_followup_recovers_push_with_lost_acknowledgement(self):
        self.assert_followup_retry("push")

    def test_followup_recovers_pr_update_with_lost_acknowledgement(self):
        self.assert_followup_retry("PATCH")

    def test_followup_confirmation_write_failure_preserves_previous_publication(self):
        self.prepare()
        publish("DEV-7")
        previous = self.store.read()["publication_history"]
        self.followup_review()
        write = PublicationStore.write
        def fail_confirmation(store, value):
            history = value["publication_history"]
            if len(history["cycles"]) == 2 and history["cycles"][-1]["state"] == "published":
                raise TaskError("confirmation storage failed")
            return write(store, value)
        with patch.object(PublicationStore, "write", new=fail_confirmation):
            with self.assertRaisesRegex(TaskError, "confirmation storage failed"):
                publish("DEV-7")
        saved = self.store.read()["publication_history"]
        self.assertEqual(saved["head"], previous["head"])
        self.assertEqual(saved["cycles"][:-1], previous["cycles"])
        self.assertEqual(saved["cycles"][-1]["state"], "pending")
        publish("DEV-7")
        self.assertEqual(len(self.operations("commit")), 2)
        self.assertEqual(len(self.operations("push")), 2)

    def test_followup_freeze_failure_recovers_one_commit(self):
        self.prepare()
        publish("DEV-7")
        previous = self.store.read()["publication_history"]
        self.followup_review()
        write = PublicationStore.write
        def fail_freeze(store, value):
            if value["intent"].get("publishing_head"):
                raise TaskError("freeze storage failed")
            return write(store, value)
        with patch.object(PublicationStore, "write", new=fail_freeze):
            with self.assertRaisesRegex(TaskError, "freeze storage failed"):
                publish("DEV-7")
        head = self.command(self.path, "rev-parse", "HEAD")
        self.assertEqual(self.store.read()["publication_history"], previous)
        self.assertEqual(len(self.operations("push")), 1)
        publish("DEV-7")
        self.assertEqual(self.store.read()["publication_history"]["head"], head)
        self.assertEqual(len(self.operations("commit")), 2)
        self.assertEqual(len(self.operations("push")), 2)

    def test_followup_completion_write_failure_reuses_commit_push_and_pr_update(self):
        self.prepare()
        publish("DEV-7")
        self.followup_review()
        write = PublicationStore.write
        def fail_completion(store, value):
            cycles = value["publication_history"]["cycles"]
            if len(cycles) == 2 and cycles[-1]["state"] == "complete":
                raise TaskError("completion storage failed")
            return write(store, value)
        with patch.object(PublicationStore, "write", new=fail_completion):
            with self.assertRaisesRegex(TaskError, "completion storage failed"):
                publish("DEV-7")
        self.assertEqual(self.store.read()["publication_history"]["cycles"][-1]["state"], "published")
        publish("DEV-7")
        self.assertEqual(len(self.operations("commit")), 2)
        self.assertEqual(len(self.operations("push")), 2)
        self.assertEqual(sum(c[0] == "PATCH" for c in self.calls), 1)

    def test_legacy_published_intent_can_recover_lineage_before_followup(self):
        self.prepare()
        publish("DEV-7")
        saved = self.store.read()
        legacy = {k: v for k, v in saved["publication_history"].items()
                  if k in {"binding", "identity", "state", "head"}}
        saved["publication_history"] = dict(version=1, **legacy)
        self.store.write(saved)
        with self.assertRaisesRegex(TaskError, "unfinished.*rerun task pr"):
            review("DEV-7")
        publish("DEV-7")
        self.assertEqual(self.store.read()["publication_history"]["version"], 2)
        self.assertEqual(self.store.read()["publication_history"]["head"], legacy["head"])
        self.followup_review()
        publish("DEV-7")
        self.assertEqual(self.command(self.path, "rev-parse", "HEAD^"), legacy["head"])
        self.assertEqual(len(self.operations("commit")), 2)

    def test_legacy_publication_without_original_intent_cannot_authorize_followup(self):
        self.prepare()
        publish("DEV-7")
        self.followup_review()
        saved = self.store.read()
        saved["publication_history"] = dict(version=1, **{k: v for k, v in saved["publication_history"].items()
            if k in {"binding", "identity", "state", "head"}})
        self.store.write(saved)
        with self.assertRaisesRegex(TaskError, "original review/intent lineage"):
            publish("DEV-7")
        self.assertEqual(self.store.read(), saved)
        self.assertEqual(len(self.operations("commit")), 1)

    def test_observed_prepublication_head_survives_uncertain_initial_push(self):
        self.prepare()
        self.command(self.remote, "branch", self.branch, self.base)
        def lost_ack(path, operation, *args, **kwargs):
            result = native_git(path, operation, *args, **kwargs)
            if operation == "push":
                raise TaskError("lost push acknowledgement")
            return result
        self.native.side_effect = lost_ack
        with self.assertRaisesRegex(TaskError, "lost push acknowledgement"):
            publish("DEV-7")
        history = self.store.read()["publication_history"]
        self.assertEqual((history["state"], history["head"]), ("published", self.base))
        self.assertEqual(history["prior"]["head"], self.base)
        self.assertEqual(history["cycles"][-1]["state"], "pending")
        self.native.side_effect = None
        publish("DEV-7")
        self.assertEqual(self.store.read()["publication_history"]["prior"], history["prior"])
        self.assertEqual(self.store.read()["publication_history"]["head"], self.command(self.path, "rev-parse", "HEAD"))
        self.assertEqual(len(self.operations("commit")), 1)
        self.assertEqual(len(self.operations("push")), 1)

    def test_followup_remote_drift_or_deletion_never_changes_lineage(self):
        self.prepare()
        publish("DEV-7")
        self.followup_review()
        saved = self.store.read()
        first = saved["publication_history"]["head"]
        other = self.command(self.remote, "commit-tree", f"{first}^{{tree}}", "-p", first, "-m", "outside update")
        for remote_head in (other, self.base, None):
            if remote_head is None:
                self.command(self.remote, "update-ref", "-d", f"refs/heads/{self.branch}")
            else:
                self.command(self.remote, "update-ref", f"refs/heads/{self.branch}", remote_head)
            with self.subTest(remote_head=remote_head), self.assertRaisesRegex(TaskError, "conflicts|missing"):
                publish("DEV-7")
            self.assertEqual(self.store.read(), saved)
            self.assertEqual(len(self.operations("commit")), 1)
            self.assertEqual(len(self.operations("push")), 1)

    def test_followup_requires_the_recorded_pr_number_even_if_all_other_fields_match(self):
        self.prepare()
        publish("DEV-7")
        self.followup_review()
        original = copy.deepcopy(self.pulls)
        for missing in (False, True):
            self.pulls = copy.deepcopy(original)
            if missing:
                self.pulls.clear()
            else:
                self.pulls[0].update(number=2, html_url="https://github.com/owner/project/pull/2")
            with self.subTest(missing=missing), self.assertRaisesRegex(TaskError, "PR identity changed|PR is missing"):
                publish("DEV-7")
        self.assertEqual(len(self.operations("commit")), 1)
        self.assertEqual(len(self.operations("push")), 1)
        self.assertEqual(sum(c[0] == "POST" for c in self.calls), 1)

    def test_followup_detects_pr_replacement_after_push_before_metadata_update(self):
        self.prepare()
        publish("DEV-7")
        self.followup_review()
        def replace_after_push(path, operation, *args, **kwargs):
            result = native_git(path, operation, *args, **kwargs)
            if operation == "push":
                self.pulls[0].update(number=2, html_url="https://github.com/owner/project/pull/2")
            return result
        self.native.side_effect = replace_after_push
        with self.assertRaisesRegex(TaskError, "PR identity changed"):
            publish("DEV-7")
        self.assertEqual(sum(c[0] == "PATCH" for c in self.calls), 0)
        self.assertEqual(self.store.read()["publication_history"]["pull_number"], 1)

    def test_published_history_rewrite_refuses_review_and_publication(self):
        self.prepare()
        publish("DEV-7")
        self.followup_review()
        first = self.command(self.path, "rev-parse", "HEAD")
        replacement = self.command(self.path, "commit-tree", f"{first}^{{tree}}", "-p", self.base, "-m", "replacement")
        self.command(self.path, "update-ref", f"refs/heads/{self.branch}", replacement, first)
        saved = self.store.read()
        with self.assertRaisesRegex(TaskError, "Published history changed"):
            review("DEV-7")
        with self.assertRaisesRegex(TaskError, "Published history was rewritten"):
            publish("DEV-7")
        self.assertEqual(self.store.read(), saved)
        self.assertEqual(len(self.operations("commit")), 1)
        self.assertEqual(len(self.operations("push")), 1)

    def test_committed_followup_requires_manual_inspection_instead_of_extra_publishing_parent(self):
        self.prepare()
        publish("DEV-7")
        (self.path / "new.txt").write_text("local commit\n")
        self.command(self.path, "add", ".")
        self.command(self.path, "commit", "-m", "local follow-up")
        with self.assertRaisesRegex(TaskError, "keep follow-up edits uncommitted"):
            review("DEV-7")

    def test_followup_requires_new_review_and_nonempty_changes(self):
        self.prepare()
        publish("DEV-7")
        self.assertEqual(review("DEV-7").state, "clean")
        with self.assertRaisesRegex(TaskError, "No new reviewed changes"):
            publish("DEV-7")
        (self.path / "new.txt").write_text("unreviewed\n")
        with self.assertRaisesRegex(TaskError, "drifted"):
            publish("DEV-7")
        self.assertEqual(len(self.operations("commit")), 1)

    def test_followup_push_confirmation_ignores_later_base_advancement(self):
        self.prepare()
        publish("DEV-7")
        self.followup_review()
        def advance_after_push(path, operation, *args, **kwargs):
            result = native_git(path, operation, *args, **kwargs)
            if operation == "push":
                self.advance_remote()
            return result
        self.native.side_effect = advance_after_push
        url = publish("DEV-7")
        history = self.store.read()["publication_history"]
        self.assertEqual(len(history["cycles"]), 2)
        self.assertTrue(all(c["state"] == "complete" for c in history["cycles"]))
        self.assertEqual(history["base_commit"], self.base)
        self.assertEqual(self.command(self.remote, "rev-parse", self.branch), history["head"])
        self.assertEqual(publish("DEV-7"), url)
        self.assertEqual(len(self.operations("push")), 2)
        self.assertFalse(self.operations("commit-tree"))

    def test_followup_base_advance_fails_closed_without_rewriting_or_fetching(self):
        self.prepare()
        publish("DEV-7")
        self.followup_review()
        saved = self.store.read()
        before = snapshot(self.path, self.base, self.branch)
        advanced = self.advance_remote()
        with self.assertRaisesRegex(TaskError, "already published.*automatic rebase is forbidden"):
            publish("DEV-7")
        self.assertEqual(snapshot(self.path, self.base, self.branch), before)
        self.assertEqual(self.store.read(), saved)
        self.command(self.repo, "fetch", "origin", "main")
        self.command(self.repo, "merge", "--ff-only", advanced)
        with self.assertRaisesRegex(TaskError, "base advanced.*pinned published base"):
            review("DEV-7")
        with self.assertRaisesRegex(TaskError, "base advanced.*automatic rebase is forbidden"):
            publish("DEV-7")
        self.assertEqual(snapshot(self.path, self.base, self.branch), before)
        self.assertFalse(self.operations("fetch"))
        self.assertFalse(self.operations("commit-tree"))
        self.assertEqual(len(self.operations("commit")), 1)
        self.assertEqual(len(self.operations("push")), 1)

    def test_stale_or_missing_public_metadata_does_not_fall_back_to_task_title(self):
        self.prepare()
        saved = self.store.read()
        saved["acceptance"]["publication"] = None
        self.store.write(saved)
        with self.assertRaisesRegex(TaskError, "run task review again"):
            publish("DEV-7")
        self.assertFalse(self.operations("commit"))


class PublishingBoundaryTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "posix", "POSIX terminal fixture")
    def test_native_key_prompt_is_visible_and_input_usable_in_callers_tty(self):
        self.assert_native_prompt(protocol=False)

    @unittest.skipUnless(os.name == "posix", "POSIX terminal fixture")
    def test_rebase_object_id_capture_keeps_signing_prompt_interactive(self):
        self.assert_native_prompt(protocol=True)

    def assert_native_prompt(self, *, protocol):
        import pty
        with tempfile.TemporaryDirectory() as directory:
            program = Path(directory) / "git"
            program.write_text(f"#!{sys.executable}\n" + '''import sys, termios
protocol = sys.argv[-1] == "commit-tree"
assert sys.stdin.isatty() and sys.stderr.isatty()
assert sys.stdout.isatty() != protocol
settings = termios.tcgetattr(0)
hidden = termios.tcgetattr(0)
hidden[3] &= ~termios.ECHO
termios.tcsetattr(0, termios.TCSANOW, hidden)
try:
    sys.stderr.write("Key passphrase: ")
    sys.stderr.flush()
    entered = sys.stdin.readline().strip()
finally:
    termios.tcsetattr(0, termios.TCSANOW, settings)
if entered != "synthetic-test-passphrase":
    sys.exit(2)
if protocol:
    print("a" * 40)
''')
            program.chmod(0o755)
            # A real PTY child, with the workflow's ordinary subprocess boundary.
            pid, master = pty.fork()
            if pid == 0:
                try:
                    result = native_git(Path(directory), "commit-tree" if protocol else "commit",
                                        refs=protocol, env={**os.environ, "PATH": directory})
                    assert result == ("a" * 40 + "\n" if protocol else None)
                except BaseException:
                    os._exit(1)
                os._exit(0)
            output = b""
            sent = False
            status = None
            deadline = time.monotonic() + 10
            try:
                while time.monotonic() < deadline:
                    if select.select([master], [], [], 0.1)[0]:
                        try:
                            output += os.read(master, 4096)
                        except OSError:
                            pass  # PTY EOF after child exit.
                    if b"Key passphrase:" in output and not sent:
                        os.write(master, b"synthetic-test-passphrase\n")
                        sent = True
                    waited, value = os.waitpid(pid, os.WNOHANG)
                    if waited:
                        status = value
                        break
                self.assertTrue(sent, output)
                self.assertEqual(status, 0, output)
                self.assertNotIn(b"synthetic-test-passphrase", output)
            finally:
                os.close(master)
                if status is None:
                    os.kill(pid, 9)
                    os.waitpid(pid, 0)

    def test_native_git_preserves_terminal_streams_and_never_captures_secrets(self):
        with patch("task_start.publish.subprocess.run", return_value=subprocess.CompletedProcess([], 0, b"refs")) as run:
            for operation in ("commit", "push", "fetch", "ls-remote", "commit-tree"):
                protocol = operation in {"ls-remote", "commit-tree"}
                native_git(Path("/repo"), operation, refs=protocol)
                kwargs = run.call_args.kwargs
                self.assertNotIn("stdin", kwargs)
                self.assertNotIn("stderr", kwargs)
                self.assertNotIn("input", kwargs)
                self.assertNotIn("capture_output", kwargs)
                self.assertIs(kwargs["stdout"], subprocess.PIPE if protocol else None)
        with patch("task_start.publish.subprocess.run", return_value=subprocess.CompletedProcess([], 1, "secret", "secret")):
            with self.assertRaises(TaskError) as caught:
                native_git(Path("/repo"), "commit")
            self.assertNotIn("secret", str(caught.exception))

    def test_publication_schema_does_not_accept_side_effect_instructions(self):
        for value in (dict(PUBLIC, branch="evil"), dict(PUBLIC, summary="two\nlines"),
                      dict(PUBLIC, description="## Tracking\nevil")):
            with self.assertRaises(TaskError):
                publication_metadata(value)

    def test_new_subjects_are_bounded_action_summaries_with_case_preserved(self):
        issue = replace(baseline.ISSUE, identifier="DEV-18", labels=("Feature",),
                        url="https://linear.app/team/issue/DEV-18")
        for summary in ("add reviewed task PR publishing", "reduce SSH passphrase prompts", "fix GitHub API diagnostics"):
            with self.subTest(summary=summary):
                title, _ = prepare_metadata(issue, {"publication": dict(PUBLIC, summary=summary)})
                self.assertEqual(title, f"feat: {summary} (DEV-18)")
                self.assertLessEqual(len(title), 72)
        for identifier in ("DEV-18", "DEV-12345678"):
            limit = publication_summary_limit(identifier)
            summary = "add " + "x" * (limit - 4)
            issue = replace(issue, identifier=identifier, labels=("Refactor",),
                            url=f"https://linear.app/team/issue/{identifier}")
            title, _ = prepare_metadata(issue, {"publication": dict(PUBLIC, summary=summary)})
            self.assertEqual(len(title), 72)
            with self.assertRaisesRegex(TaskError, "at most"):
                prepare_metadata(issue, {"publication": dict(PUBLIC, summary=summary + "x")})

    def test_new_subjects_refuse_capitalized_verbs_prefixes_suffixes_and_sentence_prose(self):
        for summary in ("Publish reviewed task changes", "Add Reviewed Task PR Publishing", "feat: add task publishing",
                        "add task publishing (DEV-18)", "add task publishing.", "add task publishing!",
                        "add task publishing?", "add  task publishing", "Publish reviewed task changes with recoverable Git and GitHub stages"):
            with self.subTest(summary=summary), self.assertRaises(TaskError):
                publication_metadata(dict(PUBLIC, summary=summary), identifier="DEV-18")

    def test_cli_has_one_simple_authorizing_command(self):
        with patch("task_start.cli.publish", return_value="https://github.com/o/r/pull/1") as publisher:
            with patch("sys.stdout", new=io.StringIO()) as output:
                self.assertEqual(cli.main(["pr", "dev-7"]), 0)
            publisher.assert_called_once_with("DEV-7")
            self.assertEqual(output.getvalue().strip(), "https://github.com/o/r/pull/1")
        for command in ("commit", "push"):
            with patch("sys.stderr", new=io.StringIO()), self.assertRaises(SystemExit):
                cli.parser().parse_args([command, "DEV-7"])


class RemoteIdentityTests(unittest.TestCase):
    setUp = baseline.LocalGitIntegrationTests.setUp
    command = baseline.LocalGitIntegrationTests.command

    def test_prototype_rename_requires_new_destination_and_rejects_old_worktree_override(self):
        old = 'git@github.com:mitjasiska/agentic-workflows-lite.git'
        new = 'git@github.com:mitjasiska/agentic-workflows-prototype.git'
        self.command(self.repo, 'remote', 'set-url', 'origin', old)
        prior = remote_identity(self.git, 'main', 'dev-114')
        self.command(self.repo, 'remote', 'set-url', 'origin', new)
        approved = remote_identity(self.git, 'main', 'dev-114')
        self.assertEqual(approved, dict(remote='origin', repository='mitjasiska/agentic-workflows-prototype'))
        with self.assertRaisesRegex(TaskError, 'differs from the approved'):
            verify_remote_identity(self.git, 'main', 'dev-114', prior)
        worktree = self.repo.parent / 'prototype-task'
        self.command(self.repo, 'worktree', 'add', '-b', 'dev-114', str(worktree))
        verify_remote_identity(Git(worktree), 'main', 'dev-114', approved)
        self.command(self.repo, 'config', 'extensions.worktreeConfig', 'true')
        self.command(worktree, 'config', '--worktree', 'remote.origin.url', old)
        # Worktree URL entries accumulate with shared config: refuse both URLs.
        with self.assertRaisesRegex(TaskError, 'one fetch/push'):
            verify_remote_identity(Git(worktree), 'main', 'dev-114', approved)
        self.command(worktree, 'config', '--worktree', '--unset-all', 'remote.origin.url')
        # A rewrite leaves one consistent destination, but it is the old identity.
        self.command(worktree, 'config', '--worktree', f'url.{old}.insteadOf', new)
        with self.assertRaisesRegex(TaskError, 'differs from the approved'):
            verify_remote_identity(Git(worktree), 'main', 'dev-114', approved)
        self.assertEqual(remote_identity(self.git, 'main', 'dev-114'), approved)

    def test_named_transport_refuses_mirror_and_group_configuration(self):
        self.command(self.repo, "remote", "set-url", "origin", "git@github.com:owner/project.git")
        for key, value in (("remote.origin.mirror", "true"), ("remotes.origin", "origin other")):
            self.command(self.repo, "config", key, value)
            if key == "remotes.origin":
                self.command(self.repo, "config", "--add", key, "")
            with self.subTest(key=key), self.assertRaisesRegex(TaskError, "non-mirror remote"):
                remote_identity(self.git, "main", "dev-7-task")
            self.command(self.repo, "config", "--unset-all", key)

    def test_remote_push_destination_must_be_unique_and_match_fetch_repository(self):
        self.command(self.repo, "remote", "set-url", "origin", "git@github.com:owner/project.git")
        identity = remote_identity(self.git, "main", "dev-7-task")
        self.assertEqual(identity, dict(remote="origin", repository="owner/project"))
        self.command(self.repo, "config", "remote.origin.pushurl", "git@github.com:other/project.git")
        with self.assertRaisesRegex(TaskError, "same github.com"):
            remote_identity(self.git, "main", "dev-7-task")
        self.command(self.repo, "config", "remote.origin.pushurl", "https://github.com/owner/project.git")
        self.command(self.repo, "config", "--add", "remote.origin.pushurl", "git@github.com:owner/project.git")
        with self.assertRaisesRegex(TaskError, "one fetch/push"):
            remote_identity(self.git, "main", "dev-7-task")

    def test_foreign_remote_and_nonmatching_upstream_base_refuse(self):
        self.command(self.repo, "remote", "set-url", "origin", "git@github.com:owner/project.git")
        self.command(self.repo, "config", "branch.main.merge", "refs/heads/other")
        with self.assertRaisesRegex(TaskError, "same-named"):
            remote_identity(self.git, "main", "dev-7-task")
        self.command(self.repo, "config", "branch.main.merge", "refs/heads/main")
        self.command(self.repo, "remote", "add", "fork", "git@github.com:fork/project.git")
        with self.assertRaisesRegex(TaskError, "different repositories"):
            remote_identity(self.git, "main", "dev-7-task")

    def test_canonical_types_and_stable_link_are_deterministic(self):
        for label, expected in (("Feature", "feat"), ("Bug", "fix"), ("Chore", "chore"),
                                ("Docs", "docs"), ("Refactor", "refactor")):
            self.assertEqual(change_type(replace(baseline.ISSUE, labels=(label, "Research"))), expected)
        self.assertEqual(tracking_url(replace(baseline.ISSUE, url="https://linear.app/team/issue/DEV-7/old-title")),
                         "https://linear.app/team/issue/DEV-7")
        for url in ("https://evil/issue/DEV-7", "https://linear.app/team/issue/DEV-8", "https://linear.app/team/issue/DEV-7?x=y"):
            with self.assertRaises(TaskError):
                tracking_url(replace(baseline.ISSUE, url=url))


class PullIdentityTests(unittest.TestCase):
    def setUp(self):
        self.pull = dict(number=17, state="open", merged_at=None,
            head=dict(ref="dev-7-task", sha="a" * 40, repo=dict(full_name="Owner/Project")),
            base=dict(ref="main", repo=dict(full_name="Owner/Project")),
            html_url="https://github.com/Owner/Project/pull/17")

    def check(self, listed, detail, repository="owner/project"):
        with patch("task_start.github.pull_requests", return_value=[listed]), \
                patch("task_start.github.request", return_value=detail):
            return publication_pull(repository, "dev-7-task", "main", {"a" * 40})

    def read_propagating(self, *, verify=None):
        return publication_pull("owner/project", "dev-7-task", "main", {"a" * 40},
                                expected_number=17, previous_head="b" * 40, verify_published=verify or Mock())

    def test_pr_propagation_does_not_retry_other_conflicts_even_with_one_stale_head(self):
        old = copy.deepcopy(self.pull)
        old["head"]["sha"] = "b" * 40
        for field in ("number", "head_repo", "base_repo", "head_branch", "base", "closed", "merged", "url", "sha"):
            changed = copy.deepcopy(self.pull)
            if field == "number":
                changed["number"] = 18
            elif field in {"head_repo", "base_repo"}:
                changed[field.split("_")[0]]["repo"]["full_name"] = "another/repo"
            elif field == "head_branch":
                changed["head"]["ref"] = "another-branch"
            elif field == "base":
                changed["base"]["ref"] = "other"
            elif field == "closed":
                changed["state"] = "closed"
            elif field == "merged":
                changed["merged_at"] = "2026-10-01"
            elif field == "url":
                changed["html_url"] = "https://github.com/owner/project/pull/18"
            else:
                changed["head"]["sha"] = "c" * 40
            for listed, detail in ((old, changed), (changed, old)):
                verify = Mock()
                with self.subTest(field=field, listed=listed is changed), \
                        patch("task_start.github.pull_requests", return_value=[listed]) as listing, \
                        patch("task_start.github.request", return_value=detail), \
                        patch("task_start.github.sleep") as sleep:
                    with self.assertRaises(TaskError):
                        self.read_propagating(verify=verify)
                    self.assertEqual(listing.call_count, 1)
                    verify.assert_not_called()
                    sleep.assert_not_called()

    def test_pr_propagation_does_not_retry_missing_duplicate_or_unavailable_pr(self):
        for history in ([], [self.pull, self.pull], [self.pull]):
            verify = Mock()
            with self.subTest(count=len(history)), \
                    patch("task_start.github.pull_requests", return_value=history) as listing, \
                    patch("task_start.github.request", side_effect=TaskError("API unavailable")), \
                    patch("task_start.github.sleep") as sleep:
                with self.assertRaises(TaskError):
                    self.read_propagating(verify=verify)
                self.assertEqual(listing.call_count, 1)
                verify.assert_not_called()
                sleep.assert_not_called()

    def test_pr_propagation_deadline_rejects_late_convergence(self):
        old = copy.deepcopy(self.pull)
        old["head"]["sha"] = "b" * 40
        with patch("task_start.github.pull_requests", side_effect=[[old], [self.pull]]) as listing, \
                patch("task_start.github.request", side_effect=[self.pull, self.pull]), \
                patch("task_start.github.sleep"), \
                patch("task_start.github.monotonic", side_effect=[0, 0, 1, 6]):
            with self.assertRaisesRegex(TaskError, "Conflicting PR repository/head/base/state"):
                self.read_propagating()
        self.assertEqual(listing.call_count, 2)

    def test_pr_propagation_requires_recorded_identity_and_remote_verifier(self):
        for kwargs in ({}, dict(expected_number=17), dict(verify_published=Mock())):
            with self.subTest(kwargs=kwargs), patch("task_start.github.pull_requests") as listing:
                with self.assertRaisesRegex(TaskError, "require the recorded PR"):
                    publication_pull("owner/project", "dev-7-task", "main", {"a" * 40},
                                     previous_head="b" * 40, **kwargs)
                listing.assert_not_called()

    def test_only_owner_repository_casing_is_equivalent(self):
        for repository in ("owner/project", "Owner/Project", "OWNER/PROJECT"):
            self.assertEqual(self.check(self.pull, self.pull, repository)["html_url"], self.pull["html_url"])

    def test_both_listed_and_detail_url_identity_remain_strict(self):
        invalid = ["http://github.com/Owner/Project/pull/17", "https://GITHUB.COM/Owner/Project/pull/17",
                   "https://github.com.evil/Owner/Project/pull/17", "https://github.com:443/Owner/Project/pull/17",
                   "https://user@github.com/Owner/Project/pull/17", "https://github.com/Other/Project/pull/17",
                   "https://github.com/Owner/Other/pull/17", "https://github.com/Owner/Project/Pull/17",
                   "https://github.com/Owner/Project/pull/18", "https://github.com/Owner/Project/pull/017",
                   "https://github.com/Owner/Project/pull/17/", "https://github.com/Owner/Project/pull/17?x=y",
                   "https://github.com/Owner/Project/pull/17#fragment", "https://github.com/Owner/Project/pull/17\n"]
        for url in invalid:
            changed = dict(self.pull, html_url=url)
            for listed, detail in ((changed, self.pull), (self.pull, changed)):
                with self.subTest(url=url, listed=listed is changed), self.assertRaises(TaskError):
                    self.check(listed, detail)

    def test_case_equivalent_url_does_not_relax_head_base_or_sha(self):
        for field in ("head", "base", "sha", "head_repo", "base_repo", "number"):
            changed = copy.deepcopy(self.pull)
            if field == "head":
                changed["head"]["ref"] = "DEV-7-task"
            elif field == "base":
                changed["base"]["ref"] = "Main"
            elif field == "sha":
                changed["head"]["sha"] = "A" * 40
            elif field in {"head_repo", "base_repo"}:
                changed[field.split("_")[0]]["repo"]["full_name"] = "other/project"
            else:
                changed["number"] = 18
            for listed, detail in ((changed, self.pull), (self.pull, changed)):
                with self.subTest(field=field, listed=listed is changed), self.assertRaises(TaskError):
                    self.check(listed, detail)
