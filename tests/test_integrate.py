"""Real Git integration/install boundaries; external services and agents are fake."""

import copy
from contextlib import contextmanager
from dataclasses import replace
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import unittest
from unittest.mock import patch

from task_start import TaskError, cli
from task_start.agent import AgentOptions, Codex, LaunchResult
from task_start.config import AgentConfig
from task_start.contexts import context_reference
from task_start.integrate import integrate
from task_start.integration_state import IntegrationStore
from task_start.loop import loop
from task_start.loop_state import LoopStore
from task_start.metrics import MetricsStore
from task_start.publish import publish
from task_start.review import review
from task_start.review_state import snapshot
from codex_startup_fixture import CodexStartupTransport, TRUST_SCREEN
import test_publish as publishing
import test_loop as looping


@contextmanager
def readonly_agent_metadata(checkout):
    """Enforce the documented write denials with real OS permissions.

    Only the simulated agent runs here; the controller keeps metadata ownership.
    The parent models the non-writable integration directory outside the checkout.
    """
    paths = [checkout.parent, checkout / ".git", *(checkout / ".git").rglob("*")]
    modes = {p: stat.S_IMODE(p.stat().st_mode) for p in paths if not p.is_symlink()}
    try:
        for path, mode in modes.items():
            path.chmod(mode & ~0o222)
        yield
    finally:
        for path, mode in modes.items():
            path.chmod(mode)


class IntegrationTests(unittest.TestCase):
    setUp = publishing.PublishingTests.setUp
    command = publishing.PublishingTests.command
    worktrees = publishing.PublishingTests.worktrees
    select_adapter = publishing.PublishingTests.select_adapter
    prepare = publishing.PublishingTests.prepare
    api = publishing.PublishingTests.api
    operations = publishing.PublishingTests.operations
    advance_remote = publishing.PublishingTests.advance_remote

    def pane_command(self, group, operation, *args):
        if operation == "split" and args[4] != str(self.path):
            number = self.next_pane_number
            self.next_pane_number += 1
            pane = dict(self.panes[0], pane_id=f"p{number}", terminal_id=f"term{number}",
                        agent=None, agent_session=None, cwd=args[4], label="Shell")
            self.panes.append(pane)
            return dict(pane=copy.deepcopy(pane))
        return publishing.PublishingTests.pane_command(self, group, operation, *args)

    def prepare_integration(self):
        self.prepare()
        import task_start.review as module
        for name in ("load_projects", "Linear", "ContextRegistry", "HerdrContexts"):
            self.enterContext(patch(f"task_start.integrate.{name}", getattr(module, name)))
        self.enterContext(patch("task_start.integrate.load_local", side_effect=lambda: self.local))
        self.integrator = Integrator(self)
        self.enterContext(patch("task_start.integrate.adapter_for", return_value=self.integrator))
        self.integration_store = IntegrationStore(self.path)
        self.result_root = self.repo.parent / "codex-temporary-root"
        self.result_root.mkdir()
        self.enterContext(patch("tempfile.gettempdir", return_value=str(self.result_root)))
        self.before = snapshot(self.path, self.base, self.branch)
        self.index_path = Path(self.command(self.path, "rev-parse", "--path-format=absolute", "--git-path", "index"))
        self.index_bytes = self.index_path.read_bytes()
        self.original_context = self.registry.get(self.implementation)

    def assert_untouched(self):
        self.assertEqual(snapshot(self.path, self.base, self.branch), self.before)
        self.assertEqual(self.index_path.read_bytes(), self.index_bytes)
        self.assertEqual(self.registry.get(self.implementation), self.original_context)
        self.assertFalse(self.operations("push"))
        self.assertFalse(any(c[0] != "GET" for c in self.calls))

    def test_dev30_uncommitted_conflict_install_requires_fresh_review(self):
        self.prepare_integration()
        self.command(self.path, "add", "tracked.txt")
        (self.path / "tracked.txt").write_text("unstaged implementation\n")
        (self.path / "deleted.txt").write_text("temporary\n")
        self.command(self.path, "add", "deleted.txt")
        (self.path / "deleted.txt").unlink()
        self.assertEqual(review("DEV-7").state, "clean")
        accepted = self.store.read()["acceptance"]
        base = self.advance_remote(conflict=True)
        self.assertIn("independent task review", integrate("DEV-7"))
        metric = MetricsStore(self.registry.path).read()[-1]
        self.assertEqual((metric["role"], metric["status"], metric["context_id"]),
                         ("integration", "completed", "DEV-7-G1"))
        self.assertEqual(metric["run_id"], self.integration_store.read()["pass_id"])
        self.assertEqual(metric["previous_run_id"], accepted["pass_id"])
        self.assertIsNone(self.store.read()["acceptance"])
        self.assertEqual(self.command(self.path, "rev-parse", "HEAD^"), base)
        self.assertEqual(self.command(self.path, "status", "--porcelain"), "")
        self.assertEqual((self.path / "tracked.txt").read_text(), "integrated requirements\n")
        self.assertTrue((self.path / "new.txt").exists())
        self.assertFalse((self.path / "deleted.txt").exists())
        self.assertEqual(self.registry.get(self.implementation), self.original_context)
        context = self.registry.get("DEV-7-G1")
        self.assertEqual(context["role"], "integration")
        self.assertNotEqual(context["worktree"], str(self.path))
        self.assertTrue(Path(context["worktree"]).exists())
        self.assertEqual(self.integrator.execution.options, AgentOptions("pi", "implementer", "low"))
        self.assertFalse(self.operations("push"))
        with self.assertRaisesRegex(TaskError, "No current clean"):
            publish("DEV-7")
        fresh = review("DEV-7")
        self.assertEqual(fresh.state, "clean")
        self.assertNotEqual(fresh.pass_id, accepted["pass_id"])
        head = self.command(self.path, "rev-parse", "HEAD")
        publish("DEV-7")
        self.assertEqual(self.command(self.remote, "rev-parse", self.branch), head)

    def test_loop_controls_after_install_use_task_checkout_and_retain_integration_context(self):
        self.prepare_integration()
        base = self.advance_remote(conflict=True)
        integrate("DEV-7")
        self.assertEqual(self.integration_store.read()["state"], "installed")
        self.assertIsNone(self.store.read()["acceptance"])
        integrated = snapshot(self.path, base, self.branch)
        context = self.registry.get("DEV-7-G1")
        record = self.integration_store.read()
        isolated = Path(context["worktree"])
        self.assertNotEqual(isolated, self.path)
        prompts_before = len(self.prompts)

        import task_start.review as module
        for name in ("load_projects", "Linear", "ContextRegistry", "HerdrContexts", "load_local", "adapter_for"):
            self.enterContext(patch(f"task_start.loop.{name}", getattr(module, name)))
        self.impl_verification_error, self.impl_status = False, "done"
        self.enterContext(patch("task_start.implementation_pass.adapter_for",
                                return_value=looping.ImplementationAdapter(self)))
        store = LoopStore(self.path)

        def assert_retained():
            self.assertIn(context, self.registry.list("DEV-7"))
            self.assertEqual(self.registry.get(context["context_id"]), context)
            self.assertEqual(self.integration_store.read(), record)
            self.assertTrue(isolated.is_dir())
            self.assertEqual(snapshot(self.path, base, self.branch), integrated)

        def control(flag):
            with patch("sys.stdout", new_callable=io.StringIO) as output, \
                    patch("task_start.loop.LoopStore", wraps=LoopStore) as resolved:
                self.assertEqual(cli.main(["loop", "DEV-7", flag, "--json"]), 0)
            resolved.assert_called_once_with(self.path)
            value = json.loads(output.getvalue())
            self.assertEqual(store.read()[0]["binding"]["worktree"], str(self.path))
            assert_retained()
            return value

        create = LoopStore.create
        def pause_after_create(target, state, **kwargs):
            create(target, state, **kwargs)
            self.assertFalse(store.read()[1])
            with patch("task_start.loop.load_local", side_effect=AssertionError("No config during pause")), \
                    patch("task_start.loop.Linear", side_effect=AssertionError("No Linear during pause")):
                self.assertTrue(control("--pause-after-current")["pause_requested"])
            self.assertTrue(store.read()[1])

        with patch.object(LoopStore, "create", pause_after_create):
            paused = loop("DEV-7", from_review=True, timeout=0.02)
        self.assertEqual(paused.state, "paused", paused.render())
        self.assertEqual(len(self.prompts), prompts_before)
        with patch("task_start.loop.load_local", side_effect=AssertionError("No config during status")), \
                patch("task_start.loop.Linear", side_effect=AssertionError("No Linear during status")):
            self.assertEqual(control("--status"), paused.as_dict())
        completed = control("--continue")
        self.assertEqual(completed["state"], "clean")
        self.assertEqual((completed["passes"], completed["reviews"]), (1, 1))
        self.assertEqual(len(self.prompts), prompts_before + 1)
        self.assertEqual(self.registry.get(completed["reviewer_context"])["worktree"], str(self.path))
        self.assertNotEqual(completed["reviewer_context"], self.accepted_result.context_id)
        self.assertEqual(self.store.read()["acceptance"]["review_state"], integrated.as_dict())
        self.assertFalse(self.operations("push"))
        self.assertFalse(any(c[0] != "GET" for c in self.calls))

    def test_human_decision_and_agent_failures_preserve_source_and_never_replay(self):
        self.prepare_integration()
        self.advance_remote(conflict=True)
        self.integrator.state = "human_decision"
        with self.assertRaisesRegex(TaskError, "human_decision"):
            integrate("DEV-7")
        self.assert_untouched()
        self.assertIsNotNone(self.store.read()["acceptance"])
        self.assertTrue(self.integrator.execution.workspace.path.exists())
        for action in (integrate, review, publish):
            with self.assertRaisesRegex(TaskError, "[Ii]ntegration.*inspect|inspect.*[Ii]ntegration"):
                action("DEV-7")
        self.assertEqual(self.integrator.launches, 1)
        self.assertEqual(MetricsStore(self.registry.path).read()[-1]["status"], "blocked")

    def test_interrupted_delivery_retains_checkout_and_claim(self):
        self.prepare_integration()
        self.advance_remote(conflict=True)
        self.integrator.error = KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            integrate("DEV-7")
        self.assert_untouched()
        self.assertTrue(self.integrator.execution.workspace.path.exists())
        self.assertTrue(Path(self.integration_store.read()["output"]).parent.is_dir())
        with self.assertRaises(TaskError):
            integrate("DEV-7")
        self.assertEqual(self.integrator.launches, 1)

    def test_missing_explicit_settings_precedes_mutation(self):
        self.prepare_integration()
        self.local = replace(self.local, agent=AgentConfig("pi"))
        with self.assertRaisesRegex(TaskError, "explicit model and mode"):
            integrate("DEV-7")
        self.assert_untouched()
        self.assertIsNone(self.integration_store.read())
        self.assertEqual(self.integrator.launches, 0)

    def assert_project_configuration_refused(self, message, **overrides):
        from task_start.publication_rebase import integration_plan
        saved = self.store.read()
        contexts, panes = self.registry.list(), copy.deepcopy(self.panes)
        with patch('task_start.integrate.integration_plan', wraps=integration_plan) as probe:
            with self.assertRaisesRegex(TaskError, message):
                integrate('DEV-7', **overrides)
            probe.assert_not_called()
        self.assert_untouched()
        self.assertEqual(self.store.read(), saved)
        self.assertEqual(self.registry.list(), contexts)
        self.assertEqual(self.panes, panes)
        self.assertIsNone(self.integration_store.read())
        self.assertEqual(self.integrator.launches, 0)
        self.assertFalse((self.registry.path.parent / 'integrations').exists())
        self.assertEqual(list(self.result_root.iterdir()), [])
        self.assertEqual(self.command(self.repo, 'rev-parse', 'HEAD'),
                         self.command(self.remote, 'rev-parse', 'HEAD'))

    def test_upstream_project_defaults_require_retry_before_integration_artifacts(self):
        self.prepare_integration()
        directory = self.remote / '.agentic-workflows-lite'
        directory.mkdir()
        (directory / 'config.toml').write_text('[agent]\nmodel="upstream-i"\nmode="medium"\n')
        base = self.advance_remote(conflict=True)
        self.assert_project_configuration_refused('Project defaults changed.*retry', mode='high')
        self.assertIn('independent task review', integrate('DEV-7', mode='high'))
        self.assertEqual(self.integrator.execution.options, AgentOptions('pi', 'upstream-i', 'high'))
        record = self.integration_store.read()
        self.assertEqual(record['options'], dict(kind='pi', model='upstream-i', mode='high'))
        self.assertEqual(record['state'], 'installed')
        self.assertEqual(self.integrator.launches, 1)
        self.assertEqual(self.command(self.path, 'rev-parse', 'HEAD^'), base)

    def test_upstream_unsafe_configuration_precedes_integration_artifacts(self):
        for name, content, message in (
            ('config.toml', '[linear]\napi_key="synthetic-forbidden"', 'only agent, reviewer, and loop'),
            ('config.local.toml', '[agent]\nmodel="implementer"', 'must not be tracked'),
        ):
            with self.subTest(file=name):
                case = IntegrationTests(); case.setUp()
                try:
                    case.prepare_integration()
                    directory = case.remote / '.agentic-workflows-lite'
                    directory.mkdir()
                    (directory / name).write_text(content)
                    case.advance_remote(conflict=True)
                    case.assert_project_configuration_refused(message)
                    case.assertEqual((case.repo / '.agentic-workflows-lite' / name).read_text(), content)
                    case.assertFalse((case.path / '.agentic-workflows-lite' / name).exists())
                finally:
                    case.doCleanups()

    def test_upstream_shared_removal_does_not_keep_stale_integration_defaults(self):
        from test_layered_config import install_lifecycle_layers
        self.prepare_integration()
        install_lifecycle_layers(self)
        (self.remote / '.agentic-workflows-lite/config.toml').unlink()
        base = self.advance_remote(conflict=True)
        self.assert_project_configuration_refused('Project defaults changed.*retry')
        self.assertIn('independent task review', integrate('DEV-7'))
        self.assertEqual(self.integrator.execution.options, AgentOptions('pi', 'implementer', 'high'))
        self.assertEqual(self.integration_store.read()['state'], 'installed')
        self.assertEqual(self.command(self.path, 'rev-parse', 'HEAD^'), base)
        for checkout in (self.path, self.integrator.execution.workspace.path):
            self.assertFalse((checkout / '.agentic-workflows-lite/config.local.toml').exists())

    def test_project_changes_do_not_reload_proven_integration_recovery(self):
        from test_layered_config import install_lifecycle_layers
        self.prepare_integration()
        local = install_lifecycle_layers(self)
        self.advance_remote(conflict=True)
        with patch('task_start.publication_rebase.install_checkout', side_effect=KeyboardInterrupt()), \
                self.assertRaises(KeyboardInterrupt):
            integrate('DEV-7')
        before = self.integration_store.read()
        self.assertEqual(before['state'], 'proven')
        self.assertEqual(before['options'], dict(kind='pi', model='shared-i', mode='high'))
        local.write_text('[linear]\napi_key="synthetic-forbidden"')
        with self.assertRaisesRegex(TaskError, 'independent task review'):
            publish('DEV-7')
        after = self.integration_store.read()
        self.assertEqual(after['state'], 'installed')
        for field in ('pass_id', 'context', 'options', 'base'):
            self.assertEqual(after[field], before[field], field)
        self.assertEqual(self.integrator.launches, 1)
        for checkout in (self.path, self.integrator.execution.workspace.path):
            self.assertFalse((checkout / '.agentic-workflows-lite/config.local.toml').exists())
        self.assertFalse(self.operations('push'))

    def test_clean_deterministic_integration_does_not_launch_agent(self):
        self.prepare_integration()
        self.advance_remote()
        with self.assertRaisesRegex(TaskError, "task pr"):
            integrate("DEV-7")
        self.assert_untouched()
        self.assertEqual(self.integrator.launches, 0)

    def test_all_three_commands_share_lock(self):
        self.prepare_integration()
        with self.store.locked():
            for action in (integrate, review, publish):
                with self.assertRaisesRegex(TaskError, "owns this worktree"):
                    action("DEV-7")
        self.assert_untouched()

    def failed_resolution(self, *, state="completed", mutation=None, error=None, status="done", message="."):
        self.prepare_integration()
        self.advance_remote(conflict=True)
        self.integrator.state, self.integrator.mutation, self.integrator.error = state, mutation, error
        with patch.object(self.integrator, "status", return_value=status), self.assertRaisesRegex(TaskError, message):
            integrate("DEV-7", timeout=0.05 if status != "done" else 1800)
        self.assert_untouched()
        self.assertTrue(self.integrator.execution.workspace.path.exists())
        self.assertIsNotNone(self.store.read()["acceptance"])
        with self.assertRaisesRegex(TaskError, "inspect"):
            integrate("DEV-7")
        self.assertEqual(self.integrator.launches, 1)

    def test_failed_completion_preserves_source(self):
        self.failed_resolution(state="failed", message="reported failed")
        self.assertTrue(self.command(self.integrator.execution.workspace.path, "ls-files", "--unmerged"))
        self.assertTrue(Path(self.integration_store.read()["output"]).is_file())
        self.assertEqual(MetricsStore(self.registry.path).read()[-1]["status"], "failed")

    def test_blocked_completion_preserves_source(self):
        self.failed_resolution(state="blocked", message="reported blocked")

    def test_uncertain_delivery_preserves_source_and_checkout(self):
        self.failed_resolution(error=TaskError("delivery acknowledgement lost"), message="acknowledgement lost")

    def test_timeout_does_not_accept_early_result_or_delete_checkout(self):
        self.failed_resolution(status="working", message="Timed out")
        self.assertEqual(MetricsStore(self.registry.path).read()[-1]["status"], "timed_out")

    def test_malformed_result_preserves_source(self):
        self.failed_resolution(mutation=lambda execution, output: output.write_text("{}"), message="Malformed")

    def test_wrong_source_or_base_in_result_refuses(self):
        def mutate(execution, output):
            value = json.loads(output.read_text())
            value["source_fingerprint"] = "0" * 64
            value["base_commit"] = "0" * 40
            output.write_text(json.dumps(value))
        self.failed_resolution(mutation=mutate, message="Malformed")

    def test_result_without_validation_evidence_refuses(self):
        def mutate(execution, output):
            value = json.loads(output.read_text())
            value["checks"] = []
            output.write_text(json.dumps(value))
        self.failed_resolution(mutation=mutate, message="Malformed")

    def test_agent_owned_commit_refuses_even_when_reset_to_base(self):
        def mutate(execution, output):
            path = execution.workspace.path
            base = self.command(path, "rev-parse", "HEAD")
            self.command(path, "add", "--all")
            self.command(path, "commit", "-m", "agent commit is forbidden")
            self.command(path, "update-ref", "refs/heads/integration", base)
        self.failed_resolution(mutation=mutate, message="changed Git history")

    def test_unfinished_git_operation_refuses(self):
        def mutate(execution, output):
            (execution.workspace.path / ".git" / "CHERRY_PICK_HEAD").write_text(self.base)
        self.failed_resolution(mutation=mutate, message="unfinished Git operation")

    def test_agent_staging_refuses_structured_success(self):
        def mutate(execution, output):
            self.command(execution.workspace.path, "add", "--all")
        self.failed_resolution(mutation=mutate, message="changed.*index")

    def test_conflict_markers_refuse_structured_success(self):
        def mutate(execution, output):
            (execution.workspace.path / "tracked.txt").write_text("<<<<<<< HEAD\nbase\n=======\ntask\n>>>>>>> task\n")
        self.failed_resolution(mutation=mutate, message="conflict markers")

    def test_controller_staging_failure_preserves_source_and_acceptance(self):
        def mutate(execution, output):
            (execution.workspace.path / ".git" / "index.lock").write_text("another process owns this lock")
        self.failed_resolution(mutation=mutate, message="Workflow could not stage")

    def test_temporary_root_inside_task_refuses_before_launch(self):
        self.prepare_integration()
        self.advance_remote(conflict=True)
        entries = sorted(p.name for p in self.path.iterdir())
        with patch("tempfile.gettempdir", return_value=str(self.path)), \
                self.assertRaisesRegex(TaskError, "outside.*check TMPDIR"):
            integrate("DEV-7")
        self.assert_untouched()
        self.assertEqual(sorted(p.name for p in self.path.iterdir()), entries)
        self.assertEqual(self.integrator.launches, 0)

    def test_provider_identity_replacement_refuses(self):
        def mutate(execution, output):
            pane = next(p for p in self.panes if p["pane_id"] == execution.workspace.pane_id)
            pane["agent_session"]["conversation_id"] = "different-conversation"
        self.failed_resolution(mutation=mutate, message="runtime identity changed")

    def test_live_base_drift_preserves_task(self):
        self.failed_resolution(mutation=lambda execution, output: self.advance_remote(), message="Remote base changed")

    def test_local_base_drift_preserves_task(self):
        def mutate(execution, output):
            (self.repo / "local.txt").write_text("new local base\n")
            self.command(self.repo, "add", ".")
            self.command(self.repo, "commit", "-m", "local drift")
        self.failed_resolution(mutation=mutate, message="Permanent base changed")

    def test_source_drift_is_preserved_without_installation(self):
        self.prepare_integration()
        self.advance_remote(conflict=True)
        def mutate(execution, output):
            (self.path / "tracked.txt").write_text("human edit while agent works\n")
            self.drifted = snapshot(self.path, self.base, self.branch)
        self.integrator.mutation = mutate
        with self.assertRaisesRegex(TaskError, "state drifted"):
            integrate("DEV-7")
        self.assertEqual(snapshot(self.path, self.base, self.branch), self.drifted)
        self.assertEqual(self.index_path.read_bytes(), self.index_bytes)
        self.assertEqual(self.command(self.path, "rev-parse", "HEAD"), self.base)

    def test_publication_appearing_during_agent_is_retained_and_installs_nothing(self):
        def mutate(execution, output):
            self.command(self.remote, "update-ref", f"refs/heads/{self.branch}", self.base)
        self.failed_resolution(mutation=mutate, message="already published")
        self.command(self.remote, "update-ref", "-d", f"refs/heads/{self.branch}", self.base)
        self.assertEqual(self.store.read()["publication_history"]["state"], "published")

    def test_remote_identity_drift_installs_nothing(self):
        self.failed_resolution(mutation=lambda execution, output: setattr(self.destination, "return_value",
            dict(remote="elsewhere", repository="owner/other")), message="destination differs")

    def test_published_and_uncertain_history_refuse_before_launch(self):
        self.prepare_integration()
        self.advance_remote(conflict=True)
        saved = self.store.read()
        saved["publication_history"] = "unknown"
        self.store.write(saved)
        with self.assertRaisesRegex(TaskError, "prior push"):
            integrate("DEV-7")
        self.assertEqual(self.integrator.launches, 0)
        self.assert_untouched()

    def test_existing_remote_branch_refuses_before_launch(self):
        self.prepare_integration()
        self.advance_remote(conflict=True)
        self.command(self.remote, "update-ref", f"refs/heads/{self.branch}", self.base)
        with self.assertRaisesRegex(TaskError, "already published"):
            integrate("DEV-7")
        self.assertEqual(self.integrator.launches, 0)
        self.assert_untouched()

    def test_committed_task_history_is_explicitly_unsupported(self):
        self.prepare_integration()
        self.command(self.path, "add", ".")
        self.command(self.path, "commit", "-m", "local implementation")
        self.assertEqual(review("DEV-7").state, "clean")
        before = snapshot(self.path, self.base, self.branch)
        with self.assertRaisesRegex(TaskError, "HEAD equal.*recover other histories manually"):
            integrate("DEV-7")
        self.assertEqual(snapshot(self.path, self.base, self.branch), before)
        self.assertEqual(self.integrator.launches, 0)

    def test_missing_controller_state_never_replays_or_reauthorizes(self):
        self.failed_resolution(state="blocked")
        self.integration_store.path.unlink()
        for action in (integrate, review, publish):
            with self.assertRaisesRegex(TaskError, "[Ii]ntegration.*inspect"):
                action("DEV-7")

    def test_prior_failed_pr_is_optional_and_its_conflict_can_be_recovered(self):
        self.prepare_integration()
        self.advance_remote(conflict=True)
        with self.assertRaisesRegex(TaskError, "Disposable rebase conflicts"):
            publish("DEV-7")
        self.assert_untouched()
        self.assertIn("independent task review", integrate("DEV-7"))

    def test_reviewed_tracked_deletions_and_modes_survive_integration(self):
        # Establish extra base files before accepting a still-uncommitted task.
        for name in ("staged-delete.txt", "unstaged-delete.txt"):
            (self.remote / name).write_text("base file\n")
        self.command(self.remote, "add", ".")
        self.command(self.remote, "commit", "-m", "base files")
        self.command(self.repo, "fetch", "origin")
        self.command(self.repo, "merge", "--ff-only", "origin/main")
        self.command(self.path, "merge", "--ff-only", "origin/main")
        self.base = self.command(self.repo, "rev-parse", "HEAD")
        self.command(self.path, "rm", "staged-delete.txt")
        (self.path / "unstaged-delete.txt").unlink()
        (self.path / "executable.sh").write_text("#!/bin/sh\nexit 0\n")
        (self.path / "executable.sh").chmod(0o755)
        self.prepare_integration()
        self.advance_remote(conflict=True)
        integrate("DEV-7")
        self.assertFalse((self.path / "staged-delete.txt").exists())
        self.assertFalse((self.path / "unstaged-delete.txt").exists())
        self.assertTrue((self.path / "executable.sh").stat().st_mode & 0o111)

    def test_checkout_preflight_failure_preserves_original_acceptance_and_task(self):
        self.prepare_integration()
        self.advance_remote(conflict=True)
        with patch("task_start.integrate.checkout_index", side_effect=TaskError("checkout preflight failed")), \
                self.assertRaisesRegex(TaskError, "checkout preflight failed"):
            integrate("DEV-7")
        self.assert_untouched()
        self.assertIsNotNone(self.store.read()["acceptance"])

    def test_index_lock_failure_revokes_review_only_after_durable_plan(self):
        self.prepare_integration()
        self.advance_remote(conflict=True)
        lock = self.index_path.with_name("index.lock")
        self.integrator.mutation = lambda execution, output: lock.write_bytes(b"owned by another Git operation")
        with self.assertRaisesRegex(TaskError, "Git index is locked"):
            integrate("DEV-7")
        self.assert_untouched()
        self.assertIsNone(self.store.read()["acceptance"])
        self.assertEqual(self.integration_store.read()["state"], "proven")
        self.assertEqual(lock.read_bytes(), b"owned by another Git operation")
        lock.unlink()
        with self.assertRaisesRegex(TaskError, "independent task review"):
            publish("DEV-7")

    def test_durable_plan_without_revocation_cannot_publish_old_acceptance(self):
        self.prepare_integration()
        self.advance_remote(conflict=True)
        from task_start.publication_state import PublicationStore
        write = PublicationStore.write
        def fail(store, value):
            if type(store) is PublicationStore and value.get("rebase") is not None:
                raise TaskError("revocation write failed")
            return write(store, value)
        with patch.object(PublicationStore, "write", fail), self.assertRaisesRegex(TaskError, "revocation write failed"):
            integrate("DEV-7")
        self.assert_untouched()
        self.assertIsNotNone(self.store.read()["acceptance"])
        for action in (integrate, publish, review):
            with self.assertRaisesRegex(TaskError, "[Ii]ntegration.*inspect"):
                action("DEV-7")

    def test_proven_installation_interruption_recovers_through_pr_without_agent_replay(self):
        self.prepare_integration()
        self.advance_remote(conflict=True)
        from task_start.workspace import Git
        command = Git.command
        def interrupt(git, *args):
            if git.repo == self.path and args[:2] == ("update-ref", f"refs/heads/{self.branch}"):
                raise KeyboardInterrupt()
            return command(git, *args)
        with patch.object(Git, "command", interrupt), self.assertRaises(KeyboardInterrupt):
            integrate("DEV-7")
        saved = self.store.read()
        self.assertIsNone(saved["acceptance"])
        self.assertIsNone(saved["rebase"]["result"])
        planned = saved["rebase"]["commits"][-1]
        with self.assertRaisesRegex(TaskError, "rebase is pending"):
            review("DEV-7")
        with self.assertRaisesRegex(TaskError, "independent task review"):
            publish("DEV-7")
        self.assertEqual(self.command(self.path, "rev-parse", "HEAD"), planned)
        self.assertEqual(review("DEV-7").state, "clean")
        self.assertEqual(self.integrator.launches, 1)
        self.assertFalse(self.operations("push"))

    def test_provenance_write_failure_precedes_review_revocation(self):
        self.prepare_integration()
        self.advance_remote(conflict=True)
        write = IntegrationStore.write
        def fail(store, value):
            if value["state"] == "proven":
                raise TaskError("provenance write failed")
            return write(store, value)
        with patch.object(IntegrationStore, "write", fail), self.assertRaisesRegex(TaskError, "provenance write failed"):
            integrate("DEV-7")
        self.assert_untouched()
        self.assertIsNotNone(self.store.read()["acceptance"])

    def codex_transport(self):
        self.prepare_integration()
        self.advance_remote(conflict=True)
        transport = CodexStartupTransport(self, lambda: self.panes)
        def complete(prompt):
            metadata = json.loads(prompt.split("INTEGRATION METADATA\n")[1].split("\n\nLATEST LINEAR")[0])
            path = Path(metadata["checkout"])
            (path / "tracked.txt").write_text("integrated requirements\n")
            Path(metadata["output"]).write_text(json.dumps(dict(pass_id=metadata["pass_id"], state="completed",
                summary="Conflict resolved and validated", checks=[dict(name="inspection", result="passed", details="Verified")],
                source_fingerprint=metadata["source"]["fingerprint"], base_commit=metadata["base"])))
        transport.on_queue = complete
        return transport

    @unittest.skipIf(os.geteuid() == 0, "POSIX write-denial fixture requires an unprivileged process")
    def test_codex_workspace_write_contract_needs_no_agent_git_writes(self):
        transport = self.codex_transport()
        self.local = replace(self.local, codex_repository_profiles={self.repo.name: "agentic-workflows-lite-trusted"})
        temporary_root = self.result_root
        observed = []
        def deliver(prompt):
            metadata = json.loads(prompt.split("INTEGRATION METADATA\n")[1].split("\n\nLATEST LINEAR")[0])
            path, output = Path(metadata["checkout"]), Path(metadata["output"])
            self.assertTrue(output.is_relative_to(temporary_root))
            self.assertFalse(output.is_relative_to(path))
            self.assertEqual(self.integration_store.read()["output"], str(output))
            self.assertIn("Do not stage", prompt)
            index = (path / ".git" / "index").read_bytes()
            with readonly_agent_metadata(path):
                denied = subprocess.run(["git", "-C", str(path), "add", "--all"], capture_output=True)
                self.assertNotEqual(denied.returncode, 0)
                self.assertIn(b"Permission denied", denied.stderr)
                with self.assertRaises(PermissionError):
                    (path.parent / "result.json").write_text("unwritable sibling")
                # Resolution can delete a conflicted file and add a replacement,
                # using only ordinary worktree writes and the temporary result.
                (path / "tracked.txt").unlink()
                (path / "replacement.txt").write_text("integrated replacement\n")
                self.assertEqual((path / ".git" / "index").read_bytes(), index)
                self.assertTrue(self.command(path, "ls-files", "--unmerged"))
                self.assert_untouched()
                output.write_text(json.dumps(dict(pass_id=metadata["pass_id"], state="completed",
                    summary="Resolved by replacing the conflicted file", source_fingerprint=metadata["source"]["fingerprint"],
                    base_commit=metadata["base"], checks=[dict(name="replacement validation", result="passed",
                    details="Validated the replacement without Git metadata writes")])))
            observed.append((path, output))
        transport.on_queue = deliver
        with patch("task_start.integrate.adapter_for", side_effect=Codex):
            self.assertIn("independent task review", integrate("DEV-7", agent_kind="codex", model="integration-model", mode="high"))
        path, output = observed[0]
        self.assertTrue(output.is_file(), "Completion evidence must survive controller return")
        self.assertFalse((self.path / "tracked.txt").exists())
        self.assertEqual((self.path / "replacement.txt").read_text(), "integrated replacement\n")
        self.assertEqual(self.command(path, "ls-files", "--unmerged"), "")
        self.assertNotIn(output.name, self.command(self.path, "ls-files").splitlines())
        self.assertIsNone(self.store.read()["acceptance"])
        self.assertFalse(self.operations("push"))
        argv = transport.argv
        self.assertEqual(argv[argv.index("--profile") + 1], "agentic-workflows-lite-trusted")
        for flag in ("--sandbox", "--ask-for-approval", "--add-dir", "--dangerously-bypass-approvals-and-sandbox"):
            self.assertNotIn(flag, argv)
        transport.assert_effects(1, 1)

    def test_recorded_slice_reaches_integration_and_fresh_review(self):
        scope_file = self.git.scope_file(self.path)
        scope = json.loads(scope_file.read_text())
        scope["slice"] = "original-title"
        scope_file.write_text(json.dumps(scope))
        self.prepare_integration()
        self.advance_remote(conflict=True)
        self.assertIn("independent task review", integrate("DEV-7"))
        prompt = self.integrator.execution.handoff
        metadata = json.loads(prompt.split("INTEGRATION METADATA\n")[1].split("\n\nLATEST LINEAR")[0])
        self.assertEqual(metadata["slice"], "original-title")
        self.assertEqual(self.integration_store.read()["slice"], "original-title")
        self.assertIn("Integrate only the recorded slice", prompt)
        self.assertIn("human_decision", prompt)
        self.assertEqual(review("DEV-7").state, "clean")
        review_metadata = json.loads(self.prompts[-1].split("RESOLVED REVIEW METADATA\n")[1].split("\n\nLATEST LINEAR")[0])
        self.assertEqual(review_metadata["slice"], "original-title")

    def test_scope_drift_during_integration_installs_nothing(self):
        def mutate(execution, output):
            scope_file = self.git.scope_file(self.path)
            scope = json.loads(scope_file.read_text())
            scope["slice"] = "original-title"
            scope_file.write_text(json.dumps(scope))
        self.failed_resolution(mutation=mutate, message="Source task identity changed")

    def test_scope_drift_refuses_proven_installation_recovery(self):
        self.prepare_integration()
        self.advance_remote(conflict=True)
        lock = self.index_path.with_name("index.lock")
        self.integrator.mutation = lambda execution, output: lock.write_text("another process owns this lock")
        with self.assertRaisesRegex(TaskError, "Git index is locked"):
            integrate("DEV-7")
        lock.unlink()
        record = self.integration_store.read()
        self.assertEqual(record["state"], "proven")
        self.assertIsNone(record["slice"])
        scope_file = self.git.scope_file(self.path)
        scope = json.loads(scope_file.read_text())
        scope["slice"] = "original-title"
        scope_file.write_text(json.dumps(scope))
        with self.assertRaisesRegex(TaskError, "slice scope changed"):
            publish("DEV-7")
        scope["slice"] = None
        scope_file.write_text(json.dumps(scope))
        record.pop("slice")
        self.integration_store.write(record)
        with self.assertRaisesRegex(TaskError, "lacks recorded slice scope"):
            publish("DEV-7")
        self.assert_untouched()

    def test_scope_drift_during_pending_recovery_refuses_before_checkout(self):
        self.prepare_integration()
        self.advance_remote(conflict=True)
        with patch("task_start.publication_rebase.install_checkout", side_effect=KeyboardInterrupt()), \
                self.assertRaises(KeyboardInterrupt):
            integrate("DEV-7")
        pending = self.store.read()
        record = self.integration_store.read()
        context = self.registry.get("DEV-7-G1")
        evidence = [Path(record["checkout"]) / "tracked.txt", Path(record["checkout"]) / ".git" / "index",
                    Path(record["output"])]
        retained = {path: path.read_bytes() for path in evidence}
        self.assertEqual(record["state"], "proven")
        self.assertIsNone(pending["rebase"]["result"])
        scope_file = self.git.scope_file(self.path)
        scope = json.loads(scope_file.read_text())

        from task_start.integration_state import check_integration
        from task_start.publication_rebase import checkout_index, run
        @contextmanager
        def drift_after_preflight(git, source, target):
            with checkout_index(git, source, target) as prepared:
                initial.assert_called_once()
                scope["slice"] = "original-title"
                scope_file.write_text(json.dumps(scope))
                yield prepared

        with patch("task_start.publish.check_integration", wraps=check_integration) as initial, \
                patch("task_start.publication_rebase.checkout_index", drift_after_preflight), \
                patch("task_start.publication_rebase.run", wraps=run) as commands, \
                self.assertRaisesRegex(TaskError, "slice scope changed"):
            publish("DEV-7")
        self.assertFalse(any("read-tree" in call.args[0] and "-u" in call.args[0]
                             and "--dry-run" not in call.args[0] for call in commands.call_args_list))
        self.assert_untouched()
        self.assertFalse(self.index_path.with_name("index.lock").exists())
        self.assertEqual(self.store.read(), pending)
        self.assertEqual(self.integration_store.read(), record)
        self.assertEqual(self.registry.get("DEV-7-G1"), context)
        self.assertEqual({path: path.read_bytes() for path in evidence}, retained)
        self.assertEqual(self.integrator.launches, 1)

    def test_scope_drift_during_pending_head_recovery_refuses_before_ref_update(self):
        self.prepare_integration()
        self.advance_remote(conflict=True)
        from task_start.workspace import Git
        command = Git.command
        def interrupt(git, *args):
            if git.repo == self.path and args[:2] == ("update-ref", f"refs/heads/{self.branch}"):
                raise KeyboardInterrupt()
            return command(git, *args)
        with patch.object(Git, "command", interrupt), self.assertRaises(KeyboardInterrupt):
            integrate("DEV-7")
        pending, record = self.store.read(), self.integration_store.read()
        before = snapshot(self.path, self.base, self.branch)
        index = self.index_path.read_bytes()
        self.assertEqual(before.head, self.base)
        self.assertEqual(before.index, pending["rebase"]["index"])
        self.assertIsNone(pending["rebase"]["result"])
        scope_file = self.git.scope_file(self.path)
        scope = json.loads(scope_file.read_text())

        from task_start.publication_rebase import installation_state
        def drift_after_local_check(*args, **kwargs):
            result = installation_state(*args, **kwargs)
            scope["slice"] = "original-title"
            scope_file.write_text(json.dumps(scope))
            return result

        with patch("task_start.publication_rebase.installation_state", side_effect=drift_after_local_check), \
                self.assertRaisesRegex(TaskError, "slice scope changed"):
            publish("DEV-7")
        self.assertEqual(snapshot(self.path, self.base, self.branch), before)
        self.assertEqual(self.index_path.read_bytes(), index)
        self.assertEqual(self.store.read(), pending)
        self.assertEqual(self.integration_store.read(), record)
        self.assertEqual(self.integrator.launches, 1)
        self.assertFalse(self.operations("push"))

    def assert_installation_drift(self, boundary, *, drift="scope", recovery=False):
        """Local drift stops authority changes; external movement preserves the frozen plan."""
        self.prepare_integration()
        self.advance_remote(conflict=True)
        if recovery:
            stop = ("task_start.integration_state.IntegrationInstallation.finish" if boundary == "installed"
                    else "task_start.publication_rebase.install_checkout")
            with patch(stop, side_effect=KeyboardInterrupt()), self.assertRaises(KeyboardInterrupt):
                integrate("DEV-7")

        from task_start.publication_rebase import checkout_index, run
        from task_start.workspace import Git
        command, replace_file, dump = Git.command, os.replace, json.dump
        observed = {}
        external = drift in {"base", "remote_base", "remote_branch", "permanent_dirty"}
        snapshot_branch = "" if drift == "branch_identity" else self.branch

        def change_identity():
            if observed:
                return
            observed["triggered"] = True
            if drift in {"scope", "scope_identity"}:
                scope_file = self.git.scope_file(self.path)
                scope = json.loads(scope_file.read_text())
                scope["slice" if drift == "scope" else "identifier"] = (
                    "original-title" if drift == "scope" else "DEV-8")
                scope_file.write_text(json.dumps(scope))
            elif drift == "task_identity":
                self.panes[0]["terminal_id"] = "replacement-terminal"
            elif drift == "branch_identity":
                self.command(self.path, "symbolic-ref", "HEAD", "refs/heads/main")
            elif drift == "provenance":
                record = self.integration_store.read()
                record["completion"]["summary"] = "changed after proof was pinned"
                self.integration_store.write(record)
            elif drift in {"base", "remote_base"}:
                proven_base = self.integration_store.read()["plan"]["base"]
                advanced = self.advance_remote()
                if drift == "base":
                    self.command(self.repo, "fetch", "origin", "main")
                    self.command(self.repo, "merge", "--ff-only", advanced)
                    self.assertEqual(self.command(self.repo, "rev-parse", "HEAD"), advanced)
                self.assertNotEqual(advanced, proven_base)
            elif drift == "remote_branch":
                self.command(self.remote, "update-ref", f"refs/heads/{self.branch}", self.base)
            elif drift == "permanent_dirty":
                (self.repo / "concurrent-work.txt").write_text("another local operation\n")
            elif drift == "source":
                (self.path / "tracked.txt").write_text("concurrent task edit\n")
            elif drift == "frozen_base":
                record = self.integration_store.read()
                record["base"] = record["plan"]["base"] = self.base
                record["completion"]["base_commit"] = self.base
                self.integration_store.write(record)
            observed.update(task=snapshot(self.path, self.base, snapshot_branch), index=self.index_path.read_bytes(),
                            refs=self.command(self.path, "show-ref", "--heads"), publication=self.store.read(),
                            integration=self.integration_store.read(), context=self.registry.get("DEV-7-G1"),
                            lookups=len(self.operations("ls-remote")))
            record = observed["integration"]
            evidence = [Path(record["checkout"]) / "tracked.txt", Path(record["checkout"]) / ".git" / "index",
                        Path(record["output"])]
            observed["evidence"] = {path: path.read_bytes() for path in evidence}

        @contextmanager
        def checkout(git, source, target):
            with checkout_index(git, source, target) as prepared:
                if boundary == "checkout":
                    change_identity()  # After preparation, before checkout's local guards.
                yield prepared

        def files(args, **kwargs):
            result = run(args, **kwargs)
            if (boundary == "files" and args[:3] == ["git", "-C", str(self.path)]
                    and "read-tree" in args and "-u" in args and "--dry-run" not in args):
                change_identity()  # Files changed under a valid guard; index/ref still old.
            return result

        def replace_index(source, target):
            result = replace_file(source, target)
            if boundary == "index" and Path(target) == self.index_path:
                change_identity()  # Checkout/index complete; branch still at source.
            return result

        def move_head(git, *args):
            result = command(git, *args)
            if boundary == "head" and git.repo == self.path and args[:2] == ("update-ref", f"refs/heads/{self.branch}"):
                change_identity()
            return result

        def serialize(value, output, **kwargs):
            result = dump(value, output, **kwargs)
            if ((boundary == "journal" and value.get("rebase") and value["rebase"].get("result"))
                    or (boundary == "installed" and value.get("state") == "installed")):
                change_identity()  # Inside persistence, before its atomic replacement.
            return result

        with patch("task_start.publication_rebase.checkout_index", checkout), \
                patch("task_start.publication_rebase.run", files), \
                patch("task_start.publication_rebase.os.replace", replace_index), \
                patch.object(Git, "command", move_head), \
                patch("task_start.publication_state.json.dump", serialize):
            if external:
                if recovery:
                    with self.assertRaisesRegex(TaskError, "independent task review|No current clean"):
                        publish("DEV-7")
                else:
                    self.assertIn("independent task review", integrate("DEV-7"))
            else:
                with self.assertRaises(TaskError):
                    (publish if recovery else integrate)("DEV-7")
        self.assertTrue(observed, "The requested mutation boundary was not reached")
        if external:
            self.assertEqual(len(self.operations("ls-remote")), observed["lookups"])
            installed = self.integration_store.read()
            self.assertEqual(installed["state"], "installed")
            self.assertEqual(installed["base"], observed["integration"]["base"])
            self.assertEqual(self.command(self.path, "rev-parse", "HEAD^"), installed["base"])
            self.assertEqual(snapshot(self.path, installed["base"], self.branch).as_dict(), installed["plan"]["result"])
            self.assertIsNone(self.store.read()["acceptance"])
            self.assertEqual(self.command(self.path, "status", "--porcelain"), "")
            self.assertEqual(self.registry.get("DEV-7-G1"), observed["context"])
            self.assertEqual({p: p.read_bytes() for p in observed["evidence"]}, observed["evidence"])
            self.assertEqual(self.integrator.launches, 1)
            self.assertFalse(self.operations("push"))
            self.assertFalse(any(c[0] != "GET" for c in self.calls))
            if drift == "remote_branch":
                self.assertEqual(self.command(self.remote, "rev-parse", self.branch), self.base)
                with self.assertRaisesRegex(TaskError, "No current clean"):
                    publish("DEV-7")
                self.assertEqual(review("DEV-7").state, "clean")
                with self.assertRaisesRegex(TaskError, "Remote task branch conflicts"):
                    publish("DEV-7")
                self.assertEqual(self.store.read()["publication_history"]["state"], "published")
                self.assertFalse(self.operations("push"))
            elif drift in {"base", "remote_base"}:
                self.assertEqual(review("DEV-7").state, "clean")
                self.assertEqual(self.store.read()["acceptance"]["review_state"]["base_commit"], installed["base"])
                self.assertEqual(self.store.read()["rebase"], installed["plan"])
                with self.assertRaisesRegex(TaskError, "independent task review"):
                    publish("DEV-7")  # New base requires deterministic reintegration and review.
                self.assertIsNone(self.store.read()["acceptance"])
                self.assertFalse(self.operations("push"))
            return
        self.assertEqual(snapshot(self.path, self.base, snapshot_branch), observed["task"])
        self.assertEqual(self.index_path.read_bytes(), observed["index"])
        self.assertEqual(self.command(self.path, "show-ref", "--heads"), observed["refs"])
        self.assertEqual(self.store.read(), observed["publication"])
        self.assertEqual(self.integration_store.read(), observed["integration"])
        self.assertEqual(self.integration_store.read()["state"], "proven")
        self.assertEqual(self.registry.get("DEV-7-G1"), observed["context"])
        self.assertEqual({p: p.read_bytes() for p in observed["evidence"]}, observed["evidence"])
        self.assertFalse(self.index_path.with_name("index.lock").exists())
        self.assertEqual(self.integrator.launches, 1)
        self.assertFalse(self.operations("push"))
        self.assertFalse(any(c[0] != "GET" for c in self.calls))
        if boundary in {"checkout", "files", "index"}:
            self.assertEqual(self.command(self.path, "rev-parse", f"refs/heads/{self.branch}"), self.base)
        if boundary != "installed":
            self.assertIsNone(self.store.read()["rebase"]["result"])
        if boundary == "checkout":
            self.assert_untouched()

    def test_initial_install_scope_drift_before_checkout(self):
        self.assert_installation_drift("checkout")

    def test_initial_install_scope_identity_drift_before_checkout(self):
        self.assert_installation_drift("checkout", drift="scope_identity")

    def test_initial_install_task_identity_drift_before_checkout(self):
        self.assert_installation_drift("checkout", drift="task_identity")

    def test_initial_install_scope_drift_after_files_stops_index_and_head(self):
        self.assert_installation_drift("files")

    def test_initial_install_scope_drift_after_index_stops_head(self):
        self.assert_installation_drift("index")

    def test_initial_install_task_identity_drift_after_index_stops_head(self):
        self.assert_installation_drift("index", drift="task_identity")

    def test_initial_install_branch_identity_drift_after_index_stops_refs(self):
        self.assert_installation_drift("index", drift="branch_identity")

    def test_initial_install_provenance_drift_after_index_stops_head(self):
        self.assert_installation_drift("index", drift="provenance")

    def test_initial_install_scope_drift_after_head_stops_success(self):
        self.assert_installation_drift("head")

    def test_initial_install_scope_drift_inside_result_persistence(self):
        self.assert_installation_drift("journal")

    def test_initial_install_scope_drift_inside_installed_persistence(self):
        self.assert_installation_drift("installed")

    def test_recovery_scope_drift_after_files_stops_index_and_head(self):
        self.assert_installation_drift("files", recovery=True)

    def test_recovery_task_identity_drift_after_index_stops_head(self):
        self.assert_installation_drift("index", drift="task_identity", recovery=True)

    def test_recovery_scope_drift_inside_result_persistence(self):
        self.assert_installation_drift("journal", recovery=True)

    def test_recovery_scope_drift_inside_installed_persistence(self):
        self.assert_installation_drift("installed", recovery=True)

    def test_initial_install_base_drift_before_checkout(self):
        self.assert_installation_drift("checkout", drift="base")

    def test_recovery_base_drift_before_checkout(self):
        self.assert_installation_drift("checkout", drift="base", recovery=True)

    def test_initial_install_base_drift_after_files_preserves_frozen_installation(self):
        self.assert_installation_drift("files", drift="base")

    def test_recovery_base_drift_after_files_preserves_frozen_installation(self):
        self.assert_installation_drift("files", drift="base", recovery=True)

    def test_initial_install_base_drift_after_index_preserves_frozen_installation(self):
        self.assert_installation_drift("index", drift="base")

    def test_recovery_base_drift_after_index_preserves_frozen_installation(self):
        self.assert_installation_drift("index", drift="base", recovery=True)

    def test_initial_install_base_drift_after_head_preserves_frozen_installation(self):
        self.assert_installation_drift("head", drift="base")

    def test_recovery_base_drift_after_head_preserves_frozen_installation(self):
        self.assert_installation_drift("head", drift="base", recovery=True)

    def test_initial_install_base_drift_inside_result_persistence(self):
        self.assert_installation_drift("journal", drift="base")

    def test_recovery_base_drift_inside_result_persistence(self):
        self.assert_installation_drift("journal", drift="base", recovery=True)

    def test_initial_install_base_drift_inside_installed_persistence(self):
        self.assert_installation_drift("installed", drift="base")

    def test_recovery_base_drift_inside_installed_persistence(self):
        self.assert_installation_drift("installed", drift="base", recovery=True)

    def test_initial_install_remote_base_drift_is_publication_concern(self):
        self.assert_installation_drift("checkout", drift="remote_base")

    def test_recovery_remote_base_drift_is_publication_concern(self):
        self.assert_installation_drift("checkout", drift="remote_base", recovery=True)

    def test_initial_install_remote_branch_appearance_does_not_publish(self):
        self.assert_installation_drift("index", drift="remote_branch")

    def test_recovery_remote_branch_appearance_does_not_publish(self):
        self.assert_installation_drift("checkout", drift="remote_branch", recovery=True)

    def test_initial_install_does_not_require_idle_permanent_checkout(self):
        self.assert_installation_drift("checkout", drift="permanent_dirty")

    def test_recovery_does_not_require_idle_permanent_checkout(self):
        self.assert_installation_drift("checkout", drift="permanent_dirty", recovery=True)

    def test_initial_install_source_drift_after_files_stops_index_and_head(self):
        self.assert_installation_drift("files", drift="source")

    def test_recovery_source_drift_after_index_stops_head(self):
        self.assert_installation_drift("index", drift="source", recovery=True)

    def test_initial_install_frozen_base_tampering_still_refuses(self):
        self.assert_installation_drift("checkout", drift="frozen_base")

    def test_recovery_frozen_base_tampering_still_refuses(self):
        self.assert_installation_drift("checkout", drift="frozen_base", recovery=True)

    def test_pending_recovery_uses_only_local_proof_despite_existing_remote_changes(self):
        self.prepare_integration()
        base = self.advance_remote(conflict=True)
        with patch("task_start.publication_rebase.install_checkout", side_effect=KeyboardInterrupt()), \
                self.assertRaises(KeyboardInterrupt):
            integrate("DEV-7")
        record = self.integration_store.read()
        advanced = self.advance_remote()
        self.command(self.repo, "fetch", "origin", "main")
        self.command(self.repo, "merge", "--ff-only", advanced)
        self.command(self.remote, "update-ref", f"refs/heads/{self.branch}", self.base)
        (self.repo / "concurrent-work.txt").write_text("permanent checkout busy\n")
        with patch("task_start.publish.remote_heads", side_effect=AssertionError("No installation ref polling")), \
                patch("task_start.publish.api_credential", side_effect=AssertionError("Local recovery needs no GitHub token")), \
                patch("task_start.github.request", side_effect=AssertionError("No installation PR lookup")), \
                self.assertRaisesRegex(TaskError, "independent task review"):
            publish("DEV-7")
        installed = self.integration_store.read()
        self.assertEqual(installed["state"], "installed")
        self.assertEqual(installed["base"], base)
        self.assertEqual(installed["context"], record["context"])
        self.assertTrue(Path(record["checkout"]).exists())
        self.assertEqual(self.command(self.path, "rev-parse", "HEAD^"), base)
        self.assertEqual(self.command(self.remote, "rev-parse", self.branch), self.base)
        self.assertIsNone(self.store.read()["acceptance"])
        self.assertEqual(review("DEV-7").review_state["base_commit"], base)
        self.assertEqual(self.integrator.launches, 1)
        self.assertFalse(self.operations("push"))

    def test_integration_cannot_enter_installer_without_identity_guard(self):
        self.prepare_integration()
        self.advance_remote(conflict=True)
        with patch("task_start.integrate.check_integration", return_value=None), \
                self.assertRaisesRegex(TaskError, "requires a live identity/scope guard"):
            integrate("DEV-7")
        self.assert_untouched()
        self.assertEqual(self.integration_store.read()["state"], "proven")
        self.assertEqual(self.store.read()["rebase"]["commits"], [])
        self.assertIsNone(self.store.read()["rebase"]["result"])

    def test_codex_adapter_receipt_uses_fresh_isolated_context(self):
        transport = self.codex_transport()
        with patch("task_start.integrate.adapter_for", side_effect=Codex):
            integrate("DEV-7", agent_kind="codex", model="integration-model", mode="high")
        transport.assert_effects(1, 1)
        context = self.registry.get("DEV-7-G1")
        self.assertEqual(context["session_id"], transport.thread_id)
        self.assertEqual(context["model"], "integration-model")
        self.assertEqual(self.registry.get(self.implementation), self.original_context)

    def test_codex_stable_session_waits_for_setup_before_reading_history(self):
        transport = self.codex_transport()
        first = dict(agent="codex", kind="id", value=transport.thread_id)
        transport.blocker, transport.start_not_ready = TRUST_SCREEN, False
        transport.start_changes = dict(agent_status="blocked", agent_session=first)
        transport.get_changes = dict(agent_session=first)
        premature_reads, verified_reads, pending = [], [], []
        def request(method, params, **kwargs):
            if method == "thread/read":
                if transport.blocker:
                    premature_reads.append(params["threadId"])
                    raise TaskError("Provider history is temporarily unreadable during setup")
                if transport.queued_at is None:
                    verified_reads.append((params["threadId"], kwargs.get("timeout")))
            return transport.request(method, params, **kwargs)
        transport.rpc.request.side_effect = request
        def accept():
            context = self.registry.get("DEV-7-G1")
            pending.append(context)
            self.assertEqual(context_reference(context), first)
            self.assertEqual((context["state"], context["resumability"]), ("awaiting_user", "unknown"))
            self.assertEqual(self.integration_store.read()["context"], dict(context_id="DEV-7-G1"))
            self.assertTrue(Path(context["worktree"]).is_dir())
            self.assert_untouched()
            self.assertEqual(premature_reads, [])
            transport.assert_effects(1, 0)
            transport.accept_setup()
            return "\n"
        complete = transport.on_queue
        def deliver(prompt):
            self.assertEqual(len(pending), 1)
            self.assertTrue(verified_reads)
            for session_id, timeout in verified_reads:
                self.assertEqual(session_id, first["value"])
                self.assertIsNotNone(timeout)
                self.assertTrue(0 < timeout <= Codex.POST_TRUST_READY_TIMEOUT)
            self.assertEqual(self.integration_store.read()["context"]["session"], first)
            complete(prompt)
        transport.on_queue = deliver
        with patch("task_start.integrate.adapter_for", side_effect=Codex), \
                patch("task_start.agent.sys.stdin") as stdin, \
                patch("task_start.agent.sys.stderr", new_callable=io.StringIO):
            stdin.isatty.return_value = True
            stdin.readline.side_effect = accept
            self.assertIn("independent task review", integrate("DEV-7", agent_kind="codex", model="integration-model", mode="high"))
        self.assertEqual(premature_reads, [])
        context = self.registry.get("DEV-7-G1")
        for field in ("context_id", "session_id", "worktree", "pane_id", "tab_id", "terminal_id"):
            self.assertEqual(context[field], pending[0][field])
        self.assertEqual(self.registry.get(self.implementation), self.original_context)
        self.assertIsNone(self.store.read()["acceptance"])
        self.assertFalse(self.operations("push"))
        transport.assert_effects(1, 1)

    def test_codex_unreadable_history_after_setup_installs_nothing(self):
        transport = self.codex_transport()
        first = dict(agent="codex", kind="id", value=transport.thread_id)
        transport.blocker, transport.start_not_ready = TRUST_SCREEN, False
        transport.start_changes = dict(agent_status="blocked", agent_session=first)
        transport.get_changes = dict(agent_session=first)
        actions = []
        def request(method, params, **kwargs):
            if method == "thread/read":
                raise TaskError("Provider history is temporarily unreadable")
            return transport.request(method, params, **kwargs)
        transport.rpc.request.side_effect = request
        def accept():
            actions.append(True)
            transport.accept_setup()
            return "\n"
        with patch("task_start.integrate.adapter_for", side_effect=Codex), \
                patch("task_start.agent.sys.stdin") as stdin, \
                patch("task_start.agent.sys.stderr", new_callable=io.StringIO):
            stdin.isatty.return_value = True
            stdin.readline.side_effect = accept
            with self.assertRaisesRegex(TaskError, "Provider history is temporarily unreadable"):
                integrate("DEV-7", agent_kind="codex", model="integration-model", mode="high")
        self.assertEqual(actions, [True])
        self.assert_untouched()
        context = self.registry.get("DEV-7-G1")
        self.assertEqual(context_reference(context), first)
        self.assertEqual(context["resumability"], "unknown")
        self.assertTrue(Path(context["worktree"]).is_dir())
        self.assertIsNotNone(self.store.read()["acceptance"])
        with self.assertRaisesRegex(TaskError, "inspect"):
            integrate("DEV-7")
        transport.assert_effects(1, 0)

    def test_cli_wires_optional_role_overrides(self):
        with patch("task_start.cli.integrate", return_value="review required") as call, patch("builtins.print"):
            self.assertEqual(cli.main(["integrate", "dev-7", "--agent", "pi", "--model", "model", "--mode", "high"]), 0)
        call.assert_called_once_with("DEV-7", agent_kind="pi", model="model", mode="high", timeout=1800)


class Integrator:
    def __init__(self, test):
        self.test = test
        self.state, self.error, self.mutation = "completed", None, None
        self.launches = 0

    def check_available(self):
        pass

    def launch(self, execution):
        self.execution = execution
        self.launches += 1
        test = self.test
        pane = next(p for p in test.panes if p["pane_id"] == execution.workspace.pane_id)
        reference = dict(agent=execution.options.kind, kind="id", value="integration-session",
                         conversation_id="integration-session")
        pane.update(agent=execution.options.kind, agent_session=reference)
        execution.runtime_observer(dict(herdr_session=json.dumps(reference), resumability="yes"))
        if self.error:
            raise self.error
        path = execution.workspace.path
        self.test.assertNotEqual(path, test.path)
        (path / "tracked.txt").write_text("integrated requirements\n")
        metadata = json.loads(execution.handoff.split("INTEGRATION METADATA\n")[1].split("\n\nLATEST LINEAR")[0])
        output = Path(metadata["output"])
        output.write_text(json.dumps(dict(pass_id=metadata["pass_id"], state=self.state,
            summary="Resolved against current requirements", source_fingerprint=metadata["source"]["fingerprint"],
            base_commit=metadata["base"], checks=[dict(name="validation", result="passed", details="Fixture validated")])))
        if self.mutation:
            self.mutation(execution, output)
        return LaunchResult(execution.options.kind, pane["pane_id"], "delivered", reference["value"],
                            session_kind="id", resumability="yes")

    def verify_session(self, workspace, reference):
        return reference

    def status(self, execution, terminal, reference):
        return "done"
