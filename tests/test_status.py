"""Offline status contract against real Git, checkpoint and context persistence."""

import copy
import io
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import unittest
from uuid import uuid4

from task_start import TaskError, cli
from task_start.config import Project, status_projects_root
from task_start.loop import LoopRuntime
from task_start.publication_state import PublicationStore
from task_start.review import review
from task_start.review_handoff_state import ReviewHandoffStore
from task_start.status import task_loop_report, task_status, HISTORY_LIMIT
from task_start.workspace import Git, Herdr
import test_loop as loops
import test_publish as publishing
import test_review as reviews
import test_integrate as integrations
import test_canceled_cleanup as cleanup_fixture
import test_loop_abort as abort_fixture


class StatusTests(unittest.TestCase):
    command = loops.LoopIntegrationTests.command
    pane_command = loops.LoopIntegrationTests.pane_command
    worktrees = loops.LoopIntegrationTests.worktrees
    select_adapter = loops.LoopIntegrationTests.select_adapter
    run_loop = loops.LoopIntegrationTests.run_loop
    pause = loops.LoopIntegrationTests.pause
    continue_loop = loops.LoopIntegrationTests.continue_loop
    pause_before_first_review = loops.LoopIntegrationTests.pause_before_first_review

    def setUp(self):
        loops.LoopIntegrationTests.setUp(self)
        self.projects = [Project(reviews.ISSUE.project, self.repo.name, 'main')]

    def report(self, issue='DEV-7', **kwargs):
        return task_status(issue, registry=self.registry, identities=self.identities,
                           projects=kwargs.pop('projects', self.projects), projects_root=self.repo.parent, **kwargs)

    def detail(self):
        result = self.report().as_dict()
        self.assertEqual(len(result['tasks']), 1, result)
        return result['tasks'][0]

    def test_implementation_and_project_identity_ignore_labels(self):
        task = self.detail()
        self.assertEqual(task['state'], 'implementation', task)
        self.assertEqual(task['project']['linear_project'], reviews.ISSUE.project)
        self.assertEqual(task['workspace']['registration'], 'registered')
        self.assertEqual(task['contexts']['implementation']['context_id'], self.implementation)
        self.assertFalse(task['guidance']['human_action_required'])

    def test_complete_findings_fix_rereview_history(self):
        self.review_results = [loops.findings(loops.finding()), {}]
        self.assertEqual(self.run_loop().state, 'clean')
        task = self.detail()
        self.assertEqual(task['state'], 'clean', task)
        self.assertEqual(task['review']['acceptance'], 'valid')
        passes = [e for e in task['history']['entries'] if e['source'] == 'loop' and e['pass_id']]
        self.assertEqual([(e['phase'], e['state']) for e in passes],
                         [('implementation', 'completed'), ('review', 'findings'),
                          ('fixes', 'completed'), ('rereview', 'clean')])
        self.assertEqual(passes[1]['findings_count'], 1)
        self.assertEqual(passes[2]['resolutions_count'], 1)
        self.assertTrue(all(e['timestamp'] is None for e in passes))
        self.assertLessEqual(len(task['history']['entries']), HISTORY_LIMIT)
        self.assertEqual(len(self.report(None).as_dict()['tasks']), 1)
        self.assertFalse(task['guidance']['human_action_required'])

    def test_successful_empty_discovery_removes_clean_task_only_from_overview(self):
        self.run_loop()
        with patch.object(Herdr, 'workspaces', return_value=[]):
            overview = self.report(None).as_dict()
            self.assertEqual(overview['discovery']['state'], 'available')
            self.assertEqual(overview['discovery']['registered_workspaces'], 0)
            self.assertEqual(overview['tasks'], [])
            task = self.detail()
            self.assertEqual(task['workspace']['registration'], 'absent')
            self.assertEqual(task['loop']['state'], 'clean')
            self.assertEqual(task['state'], 'clean')

    def test_failed_discovery_is_not_empty(self):
        for operation in ('workspaces',):
            with patch.object(Herdr, operation, side_effect=TaskError('unavailable')):
                overview = self.report(None).as_dict()
                self.assertEqual(overview['discovery']['state'], 'unavailable')
                self.assertIsNone(overview['discovery']['registered_workspaces'])
                self.assertIsNone(overview['tasks'])
                self.assertEqual(self.detail()['state'], 'uncertain')
        with patch.object(self.identities, 'endpoint', side_effect=TaskError('no endpoint')):
            self.assertIsNone(self.report(None).as_dict()['tasks'])

    def test_worktree_listing_failure_is_partial_uncertainty(self):
        with patch.object(Herdr, 'command', side_effect=TaskError('transient')):
            result = self.report(None).as_dict()
            self.assertEqual(result['discovery']['state'], 'uncertain')
            self.assertEqual(result['discovery']['registered_workspaces'], 1)
            self.assertEqual(result['tasks'], [])
            self.assertIn('exact registered workspace', result['discovery']['uncertainties'][0]['message'])

    def test_workspace_repository_checkout_and_endpoint_conflicts(self):
        original = Herdr(self.repo).workspaces()
        for key, value in [('repo_root', '/different'), ('repo_key', '/different/.git'),
                           ('checkout_path', '/different/task'), ('is_linked_worktree', False)]:
            changed = copy.deepcopy(original)
            changed[0]['worktree'][key] = value
            with self.subTest(key=key), patch.object(Herdr, 'workspaces', return_value=changed):
                self.assertEqual(self.report(None).as_dict()['discovery']['state'], 'uncertain')
                self.assertEqual(self.report(None).as_dict()['tasks'], [])
        with patch.object(self.identities, 'endpoint', return_value='/different.sock'):
            self.assertEqual(self.detail()['workspace']['registration'], 'unknown')

    def test_duplicate_workspaces_are_ambiguous(self):
        original = Herdr(self.repo).workspaces()
        changed = original + [dict(original[0], workspace_id='w2')]
        with patch.object(Herdr, 'workspaces', return_value=changed):
            self.assertEqual(self.report(None).as_dict()['tasks'], [])
            self.assertEqual(self.detail()['state'], 'uncertain')

    def test_ambiguous_project_mapping_and_repository_name_guess_are_unverified(self):
        ambiguous = self.projects + [Project('Other Linear project', self.repo.name, 'main')]
        result = self.report(None, projects=ambiguous).as_dict()
        self.assertEqual(result['projects'], [])
        self.assertEqual(result['unverified_project_tasks'], [0])
        self.assertEqual(result['tasks'][0]['project']['state'], 'unverified')
        result = task_status('DEV-7', registry=self.registry, identities=self.identities,
                            projects=self.projects, projects_root=Path('/other/root')).as_dict()
        self.assertEqual(result['tasks'][0]['project']['state'], 'unverified')

    def test_base_evidence_disambiguates_configured_projects(self):
        self.run_loop()
        projects = self.projects + [Project('Other base', self.repo.name, 'develop')]
        self.assertEqual(self.report(projects=projects).as_dict()['tasks'][0]['project']['linear_project'], reviews.ISSUE.project)

    def test_pause_and_escalation_require_different_guidance(self):
        self.pause_before_first_review()
        task = self.detail()
        self.assertEqual(task['state'], 'paused', task)
        self.assertEqual(task['guidance']['kind'], 'continue')
        self.assertEqual(task['guidance']['command'], 'task loop DEV-7 --continue')
        self.review_results = [loops.findings(loops.finding(category='architecture'))]
        self.continue_loop()
        task = self.detail()
        self.assertEqual(task['state'], 'escalated')
        self.assertEqual(task['guidance']['kind'], 'inspect')
        self.assertIsNone(task['guidance']['command'])
        self.assertTrue(task['guidance']['options'][0]['conditional'])

    def test_running_and_ready_controller_are_not_human_intervention(self):
        seen = []
        def during(execution, items):
            seen.append(self.detail())
        self.on_implementation = during
        original = LoopRuntime.prepare
        observed = []
        def prepare(runtime, checkpoint):
            observed.append(self.detail())
            return original(runtime, checkpoint)
        with patch.object(LoopRuntime, 'prepare', prepare):
            self.run_loop()
        self.assertEqual(seen[0]['state'], 'running', seen)
        self.assertEqual(seen[0]['controller'], 'held')
        self.assertFalse(seen[0]['guidance']['human_action_required'])
        self.assertEqual(observed[0]['loop']['state'], 'ready')
        self.assertFalse(observed[0]['guidance']['human_action_required'], observed[0])

    def test_interrupted_original_pass_can_only_observe(self):
        def interrupted(execution, items):
            raise KeyboardInterrupt()
        self.on_implementation = interrupted
        self.assertEqual(self.run_loop().state, 'interrupted')
        task = self.detail()
        self.assertEqual(task['state'], 'interrupted', task)
        self.assertEqual(task['guidance']['kind'], 'observe')
        self.assertEqual(task['guidance']['command'], 'task loop DEV-7')
        self.assertIn('never resends', task['guidance']['message'])
        self.assertEqual(task['guidance']['options'][0]['command'], 'task loop DEV-7 --abort')

    def assert_interrupted_drift_requires_inspection(self):
        before = self.store.read()
        task = self.detail()
        self.assertEqual(task['state'], 'uncertain', task)
        self.assertEqual(task['guidance']['kind'], 'inspect')
        self.assertIsNone(task['guidance']['command'])
        self.assertTrue(task['uncertainties'])
        self.assertEqual(self.store.read(), before, 'Inspection must not invalidate or rewrite the pass')

    def test_known_review_content_drift_does_not_suggest_observation(self):
        self.on_review = lambda *_: (_ for _ in ()).throw(KeyboardInterrupt())
        self.assertEqual(self.run_loop().state, 'interrupted')
        self.assertEqual(self.detail()['guidance']['kind'], 'observe')
        (self.path / 'tracked.txt').write_text('Changed after interruption\n')
        self.assert_interrupted_drift_requires_inspection()

    def test_interrupted_implementation_allows_uncommitted_edits(self):
        self.on_implementation = lambda *_: (_ for _ in ()).throw(KeyboardInterrupt())
        self.run_loop()
        (self.path / 'tracked.txt').write_text('Implementation progress\n')
        (self.path / 'additional.txt').write_text('More progress\n')
        self.command(self.path, 'add', 'tracked.txt')
        before = self.store.read()
        task = self.detail()
        self.assertEqual(task['guidance']['kind'], 'observe', task)
        self.assertEqual(task['uncertainties'], [])
        self.assertEqual(self.store.read(), before)

    def test_interrupted_implementation_head_drift_requires_inspection(self):
        self.on_implementation = lambda *_: (_ for _ in ()).throw(KeyboardInterrupt())
        self.run_loop()
        self.command(self.path, 'commit', '--allow-empty', '-m', 'Unexpected new HEAD')
        self.assert_interrupted_drift_requires_inspection()

    def test_interrupted_base_drift_requires_inspection(self):
        self.on_implementation = lambda *_: (_ for _ in ()).throw(KeyboardInterrupt())
        self.run_loop()
        self.command(self.repo, 'commit', '--allow-empty', '-m', 'Base advanced')
        self.assert_interrupted_drift_requires_inspection()

    def test_interrupted_scope_drift_requires_inspection(self):
        self.on_implementation = lambda *_: (_ for _ in ()).throw(KeyboardInterrupt())
        self.run_loop()
        scope_file = Git(self.repo).scope_file(self.path)
        saved = json.loads(scope_file.read_text())
        saved['slice'] = 'original-title'  # Valid for this branch, but different from the claimed default scope.
        scope_file.write_text(json.dumps(saved))
        self.assert_interrupted_drift_requires_inspection()

    def test_stale_running_flag_with_durable_receipt_and_unowned_lock(self):
        self.on_implementation = lambda *args: (_ for _ in ()).throw(KeyboardInterrupt())
        self.run_loop()
        state, _ = self.store.read()
        state['status'] = 'running'
        self.store.save(state)
        self.assertEqual(self.detail()['guidance']['kind'], 'observe')
        state['delivery']['receipt'] = None
        self.store.save(state)
        task = self.detail()
        self.assertEqual(task['state'], 'uncertain')
        self.assertEqual(task['guidance']['kind'], 'inspect')
        self.assertIsNone(task['guidance']['command'])

    def test_missing_receipt_or_changed_session_never_offers_recovery(self):
        self.on_implementation = lambda *args: (_ for _ in ()).throw(KeyboardInterrupt())
        self.run_loop()
        self.registry.update(self.implementation, session_id='/different.jsonl')
        task = self.detail()
        self.assertEqual(task['state'], 'uncertain')
        self.assertEqual(task['guidance']['kind'], 'inspect')
        self.assertIsNone(task['guidance']['command'])

    def test_awaiting_user_requires_original_live_controller(self):
        self.registry.update(self.implementation, state='awaiting_user')
        with PublicationStore(self.path).locked():
            task = self.detail()
            self.assertEqual(task['state'], 'awaiting_user')
            self.assertEqual(task['guidance']['kind'], 'human_action')
        self.assertEqual(self.detail()['guidance']['kind'], 'inspect')

    def test_stale_acceptance_from_content_or_base_drift(self):
        self.run_loop()
        (self.path / 'new-content.txt').write_text('changed')
        self.assertEqual(self.detail()['review']['acceptance'], 'invalid')
        self.assertEqual(self.detail()['state'], 'stale_review')
        (self.path / 'new-content.txt').unlink()
        self.command(self.repo, 'commit', '--allow-empty', '-m', 'advance base')
        self.assertEqual(self.detail()['review']['acceptance'], 'invalid')

    def test_unavailable_review_snapshot_is_unverified_instead_of_valid_or_absent(self):
        self.run_loop()
        with patch('task_start.status.snapshot', side_effect=TaskError('unreadable content')):
            task = self.detail()
        self.assertEqual(task['state'], 'uncertain')
        self.assertEqual(task['review']['acceptance'], 'unverified')
        self.assertTrue(any(e['code'] == 'acceptance' for e in task['uncertainties']))

    def test_status_does_not_create_missing_checkpoint_or_lock_files(self):
        directory = PublicationStore(self.path).directory
        lock = directory / 'agentic-workflows-publication.lock'
        lock.unlink(missing_ok=True)
        before = set(directory.iterdir())
        self.assertEqual(self.detail()['state'], 'implementation')
        self.assertEqual(set(directory.iterdir()), before)
        self.assertFalse(lock.exists())

    def test_clean_checkpoint_without_acceptance_is_not_clean(self):
        self.run_loop()
        publication = PublicationStore(self.path)
        saved = publication.read()
        saved['acceptance'] = None
        publication.write(saved)
        self.assertEqual(self.detail()['state'], 'stale_review')
        self.assertEqual(self.detail()['review']['acceptance'], 'absent')

    def test_corrupt_stores_and_binding_conflicts_fail_closed(self):
        self.run_loop()
        directory = PublicationStore(self.path).directory
        for filename in ('agentic-workflows-publication.json', 'agentic-workflows-integration.json',
                         'agentic-workflows-review-handoff.json', 'agentic-workflows-loop.sqlite3'):
            path = directory / filename
            old = path.read_bytes() if path.exists() else None
            path.write_bytes(b'corrupt')
            try:
                task = self.detail()
                self.assertEqual(task['state'], 'uncertain', filename)
                self.assertNotEqual(task['review']['acceptance'], 'valid')
            finally:
                path.write_bytes(old) if old is not None else path.unlink()
        state, _ = self.store.read()
        state['binding']['workspace_id'] = 'wrong'
        self.store.save(state)
        self.assertEqual(self.detail()['state'], 'uncertain')
        self.assertEqual(self.detail()['project']['state'], 'unverified')

    def test_standalone_review_findings_and_clean(self):
        self.review_results = [loops.findings(loops.finding())]
        review('DEV-7')
        task = self.detail()
        self.assertEqual(task['state'], 'findings')
        self.assertEqual(task['guidance']['kind'], 'inspect')
        self.verdict_overrides = {}
        review('DEV-7')
        self.assertEqual(self.detail()['review']['acceptance'], 'valid')

    def test_new_standalone_review_overrides_old_clean_loop_boundary(self):
        self.run_loop()
        seen = []
        self.on_review = lambda execution: seen.append(self.detail())
        self.review_results = [loops.findings(loops.finding())]
        review('DEV-7')
        self.assertEqual(seen[0]['loop']['state'], 'clean')
        self.assertEqual(seen[0]['state'], 'running', seen)
        self.assertFalse(seen[0]['guidance']['human_action_required'])
        self.assertEqual(self.detail()['state'], 'findings')

    def test_consumed_standalone_does_not_replace_newer_loop_reviewer(self):
        original = review('DEV-7')
        self.assertEqual(self.run_loop().state, 'clean')
        self.assertIsNotNone(ReviewHandoffStore(self.path).read()['consumed'])
        self.review_results = [loops.findings(loops.finding(category='human_decision'))]
        self.assertEqual(self.run_loop(action='new', from_review=True).state, 'escalated')
        state, _ = self.store.read()
        task = self.detail()
        self.assertNotEqual(state['reviewer']['context_id'], original.context_id)
        self.assertEqual(task['contexts']['review']['context_id'], state['reviewer']['context_id'])
        self.assertEqual(task['review']['context_id'], state['reviewer']['context_id'])
        self.assertEqual(task['review']['pass_id'], state['records'][-1]['pass_id'])
        self.assertEqual(task['review']['state'], 'findings')
        self.assertEqual(task['state'], 'escalated')
        self.assertEqual(task['uncertainties'], [])
        self.assertTrue(any(e['source'] == 'review' and e['pass_id'] == original.pass_id
                            and e['state'] == 'clean' for e in task['history']['entries']))

    def test_adopted_findings_do_not_replace_clean_rereview(self):
        self.review_results = [loops.findings(loops.finding())]
        original = review('DEV-7')
        self.assertEqual(self.run_loop().state, 'clean')
        task = self.detail()
        self.assertEqual(task['state'], 'clean', task)
        self.assertEqual(task['review']['acceptance'], 'valid')
        self.assertNotEqual(task['review']['pass_id'], original.pass_id)
        self.assertEqual(task['review']['state'], 'clean')
        self.assertEqual(task['uncertainties'], [])
        # Checkpoint-first adoption is authoritative even if its subsequent
        # consumption bookkeeping write was lost.
        handoff = ReviewHandoffStore(self.path)
        saved = handoff.read()
        saved['consumed'] = None
        handoff.write(saved)
        self.assertEqual(self.detail(), task)

    def test_conflicting_same_pass_standalone_outcome_or_provenance_withholds_acceptance(self):
        review('DEV-7')
        store = ReviewHandoffStore(self.path)
        original = store.read()
        changes = [dict(state='findings', findings=[loops.finding()]),
                   dict(pass_kind='resumed'), dict(post_fingerprint='0' * 64),
                   dict(execution=dict(original['result']['execution'], model='different-model')),
                   dict(review_state=dict(original['result']['review_state'], content='0' * 64))]
        for change in changes:
            with self.subTest(change=change):
                saved = copy.deepcopy(original)
                saved['result'].update(change)
                store.write(saved)
                task = self.detail()
                self.assertEqual(task['state'], 'uncertain', task)
                self.assertNotEqual(task['review']['acceptance'], 'valid')
                self.assertTrue(task['uncertainties'])
                self.assertEqual(store.read(), saved)
                if change.get('state') == 'findings':
                    self.assertEqual(task['review']['state'], 'findings')

    def test_conflicting_same_pass_loop_outcome_withholds_acceptance(self):
        self.run_loop()
        state, _ = self.store.read()
        state['records'][-1].update(state='findings', findings=[loops.finding()])
        state['status'] = 'escalated'
        self.store.save(state)
        task = self.detail()
        self.assertEqual(task['state'], 'uncertain', task)
        self.assertNotEqual(task['review']['acceptance'], 'valid')
        self.assertEqual(task['review']['state'], 'findings')
        self.assertTrue(task['uncertainties'])
        self.assertEqual(self.store.read()[0], state)

    def test_adopted_review_and_loop_outcomes_must_agree(self):
        review('DEV-7')
        self.run_loop()
        state, _ = self.store.read()
        state['records'][-1]['checks'] = [dict(name='unit', result='failed', details='Contradictory result')]
        self.store.save(state)
        task = self.detail()
        self.assertEqual(task['state'], 'uncertain', task)
        self.assertNotEqual(task['review']['acceptance'], 'valid')
        self.assertTrue(task['uncertainties'])

    def test_matching_loop_review_snapshot_must_agree_with_acceptance(self):
        self.run_loop()
        state, _ = self.store.read()
        state['snapshot']['fingerprint'] = '0' * 64
        self.store.save(state)
        task = self.detail()
        self.assertEqual(task['state'], 'uncertain', task)
        self.assertNotEqual(task['review']['acceptance'], 'valid')
        self.assertTrue(task['uncertainties'])
        self.assertEqual(self.store.read()[0], state)

    def test_json_deterministic_allowlisted_and_no_mutations(self):
        self.run_loop()
        self.linear.reset_mock()
        before = {p: p.read_bytes() for root in (self.repo / '.git', self.path) for p in root.rglob('*') if p.is_file()}
        before[self.registry.path] = self.registry.path.read_bytes()
        with patch('task_start.config.load_local', side_effect=AssertionError('credential loading')):
            first = self.report().as_dict()
            second = self.report().as_dict()
        self.assertEqual(first, second)
        text = json.dumps(first, sort_keys=True)
        self.assertEqual(first['version'], 1)
        self.assertEqual(first['selection'], 'issue')
        for forbidden in ('Current requirements', 'prompt_sha256', 'result.json', 'Checked requirements', 'api_key'):
            self.assertNotIn(forbidden, text)
        for path, content in before.items():
            self.assertEqual(path.read_bytes(), content, str(path))
        after = {p for root in (self.repo / '.git', self.path) for p in root.rglob('*') if p.is_file()}
        self.assertEqual(after, set(before) - {self.registry.path})
        self.linear.get_issue.assert_not_called()

    def test_cli_and_invalid_selection(self):
        with patch('task_start.cli.task_status', side_effect=lambda issue: self.report(issue)), patch('sys.stdout', new_callable=io.StringIO) as out:
            self.assertEqual(cli.main(['status', 'dev-7', '--json']), 0)
            self.assertEqual(json.loads(out.getvalue())['issue'], 'DEV-7')
            out.seek(0)
            out.truncate()
            self.assertEqual(cli.main(['status', '--all', '--json']), 0)
            self.assertEqual(json.loads(out.getvalue())['selection'], 'all')
            out.seek(0)
            out.truncate()
            self.assertEqual(cli.main(['status', 'DEV-7']), 0)
            self.assertIn('DEV-7: implementation', out.getvalue())
            self.assertIn('Project: ' + reviews.ISSUE.project, out.getvalue())
            self.assertIn('No intervention', out.getvalue())
            self.assertIn('Saved history', out.getvalue())
        for args in (['status'], ['status', 'DEV-7', '--all']):
            with patch('sys.stderr', new_callable=io.StringIO):
                self.assertEqual(cli.main(args), 1)

    def test_project_root_reader_needs_no_linear_or_agent_settings(self):
        config = self.repo.parent / 'status-config.toml'
        config.write_text('projects_root = ' + json.dumps(str(self.repo.parent)))
        self.assertEqual(status_projects_root(config), self.repo.parent)

    def test_conflicting_context_claim_cannot_enter_overview(self):
        self.registry.allocate('DEV-8', 'implementation', agent='codex', repository=str(self.repo),
            worktree=str(self.path), endpoint='/server.sock', workspace_id='w1')
        result = self.report(None).as_dict()
        self.assertEqual(result['discovery']['state'], 'uncertain')
        self.assertEqual(result['tasks'], [])
        self.assertEqual(self.detail()['workspace']['registration'], 'unknown')

    def test_unmanaged_worktree_is_not_an_execution(self):
        with patch.object(self.registry, 'list', return_value=[]):
            result = self.report(None).as_dict()
        self.assertEqual(result['discovery']['state'], 'available')
        self.assertEqual(result['tasks'], [])
        self.assertEqual(result['discovery']['registered_workspaces'], 1)

    def test_registry_failure_and_incomplete_context_are_not_empty_discovery(self):
        with patch.object(self.registry, 'list', side_effect=TaskError('unreadable registry')):
            result = self.report(None).as_dict()
            self.assertEqual(result['discovery']['state'], 'unavailable')
            self.assertIsNone(result['tasks'])
        contexts = self.registry.list(include_retired=True)
        contexts[0]['worktree'] = None
        with patch.object(self.registry, 'list', return_value=contexts):
            self.assertEqual(self.report(None).as_dict()['discovery']['state'], 'uncertain')

    def test_transient_registration_change_marks_snapshot_uncertain(self):
        before = Herdr(self.repo).workspaces()
        with patch.object(Herdr, 'workspaces', side_effect=[before, []]):
            result = self.report(None).as_dict()
        self.assertEqual(result['discovery']['state'], 'uncertain')
        self.assertEqual(len(result['tasks']), 1)
        self.assertEqual(result['tasks'][0]['guidance']['kind'], 'inspect')

    def test_record_change_during_read_never_offers_recovery(self):
        self.pause_before_first_review()
        original = self.store.read()
        changed = copy.deepcopy(original)
        changed[0]['reason'] = 'Changed boundary'
        with patch.object(type(self.store), 'read', side_effect=[original, changed]):
            task = self.detail()
        self.assertEqual(task['state'], 'uncertain')
        self.assertIsNone(task['guidance']['command'])

    def test_malformed_nested_record_never_crashes_or_certifies_clean(self):
        review('DEV-7')
        store = PublicationStore(self.path)
        source = store.directory / 'agentic-workflows-review-handoff.json'
        saved = json.loads(source.read_text())
        for change in ({'binding': []}, {'reviewer': {'context_id': 'DEV-8-R1'}},
                       {'result': dict(saved['result'], state='imaginary')}):
            source.write_text(json.dumps(dict(saved, **change)))
            task = self.detail()
            self.assertEqual(task['state'], 'uncertain', change)
            self.assertNotEqual(task['review']['acceptance'], 'valid')
        source.write_text(json.dumps(saved))
        saved = store.read()
        saved['publication_history'] = dict(version=2, cycles=7)
        store.write(saved)
        self.assertEqual(self.detail()['publication']['state'], 'unknown')

    def test_missing_controller_lock_cannot_prove_interruption(self):
        self.on_implementation = lambda *args: (_ for _ in ()).throw(KeyboardInterrupt())
        self.run_loop()
        (PublicationStore(self.path).directory / 'agentic-workflows-publication.lock').unlink()
        task = self.detail()
        self.assertEqual(task['controller'], 'unknown')
        self.assertEqual(task['guidance']['kind'], 'inspect')
        self.assertIsNone(task['guidance']['command'])

    def test_blocked_assessment_is_retained_as_escalation(self):
        self.impl_overrides = dict(state='blocked', task_assessment=dict(state='blocked', summary='Decision required',
                                                                      questions=['Choose required behavior?']))
        self.run_loop()
        task = self.detail()
        self.assertEqual(task['state'], 'escalated')
        self.assertEqual(task['guidance']['kind'], 'inspect')
        self.assertTrue(any(e.get('assessment_state') == 'blocked' for e in task['history']['entries']))

    def test_unknown_project_config_preserves_registered_execution(self):
        with patch('task_start.status.load_projects', side_effect=TaskError('missing registry')):
            result = task_status(None, registry=self.registry, identities=self.identities,
                                 projects_root=self.repo.parent).as_dict()
        self.assertEqual(result['discovery']['state'], 'uncertain')
        self.assertEqual(len(result['tasks']), 1)
        self.assertEqual(result['tasks'][0]['project']['state'], 'unverified')

    def test_maximum_checkpoint_history_stays_bounded_and_omits_report_prose(self):
        self.review_results = [loops.findings(loops.finding()), {}]
        self.run_loop()
        state, _ = self.store.read()
        implementation, reviewer = copy.deepcopy(state['records'][0]), copy.deepcopy(state['records'][-1])
        records = []
        for index in range(20):
            impl = dict(implementation, phase='implementation' if index == 0 else 'fixes', pass_id=str(uuid4()))
            rev = dict(reviewer, phase='review' if index == 0 else 'rereview',
                       pass_id=reviewer['pass_id'] if index == 19 else str(uuid4()),
                       state='clean' if index == 19 else 'findings',
                       findings=[] if index == 19 else [loops.finding(index + 1)])
            records.extend([impl, rev])
        state.update(records=records, pass_count=40, review_count=20, max_passes=40, max_reviews=20)
        self.store.save(state)
        task = self.detail()
        self.assertEqual(task['state'], 'clean', task)
        history = task['history']
        self.assertEqual(len([e for e in history['entries'] if e['source'] == 'loop' and e['pass_id']]), 40)
        self.assertLessEqual(len(history['entries']), history['limit'])
        self.assertFalse(history['truncated'])
        self.assertNotIn(implementation['summary'], json.dumps(history))

    def test_overview_groups_multiple_exact_repositories_by_configured_project(self):
        second = self.repo.parent / 'second-repository'
        self.command(self.repo.parent, 'clone', '--no-hardlinks', str(self.repo), str(second))
        checkout = self.repo.parent / 'second-task'
        self.command(second, 'worktree', 'add', '-b', 'dev-8-second', str(checkout))
        Git(second).save_scope(checkout, 'dev-8-second', 'DEV-8', None)
        self.registry.allocate('DEV-8', 'implementation', agent='codex', repository=str(second),
            worktree=str(checkout), endpoint='/server.sock', workspace_id='w2')
        entries = Herdr(self.repo).workspaces() + [dict(workspace_id='w2', label='misleading',
            worktree=dict(repo_root=str(second), repo_key=str(second / '.git'), checkout_path=str(checkout),
                          is_linked_worktree=True))]
        def factory(repo):
            def trees(operation):
                self.assertEqual(operation, 'list')
                return dict(source=dict(repo_root=str(repo)), worktrees=[dict(path=t['worktree'],
                    branch=t.get('branch', '').removeprefix('refs/heads/'), is_linked_worktree=Path(t['worktree']) != repo,
                    is_bare=False, is_detached=False, is_prunable=False,
                    open_workspace_id='w2' if repo == second else 'w1') for t in Git(repo).worktrees()
                    if Path(t['worktree']) != repo])
            return SimpleNamespace(workspaces=lambda: entries, command=trees)
        result = self.report(None, projects=self.projects + [Project('Second Linear project', second.name, 'main')],
                             herdr_factory=factory).as_dict()
        self.assertEqual(result['discovery']['state'], 'available', result)
        self.assertEqual(len(result['tasks']), 2)
        self.assertEqual({p['linear_project'] for p in result['projects']}, {reviews.ISSUE.project, 'Second Linear project'})
        for group in result['projects']:
            self.assertEqual(len(group['tasks']), 1)
            self.assertEqual(result['tasks'][group['tasks'][0]]['project']['linear_project'], group['linear_project'])


class TaskLoopReportTests(unittest.TestCase):
    def setUp(self):
        self.fixture = StatusTests()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.setUp()

    def read(self, selected=None):
        f = self.fixture
        return task_loop_report(selected or f.detail(), registry=f.registry, identities=f.identities,
                                projects=f.projects, projects_root=f.repo.parent)

    def write_checkpoint(self, state):
        with sqlite3.connect(self.fixture.store.path) as db:
            db.execute('UPDATE checkpoint SET payload=? WHERE id=1', (json.dumps(state),))

    def test_complete_cli_report_with_findings_fixes_and_every_check_without_overview_prose(self):
        f = self.fixture
        checks = [dict(name=f'check {i}', result='not_run' if i % 2 else 'passed',
                       details=f'Limitation {i}: ' + 'long recorded detail ' * 100) for i in range(80)]
        f.impl_overrides = dict(checks=checks, summary='Full implementation summary')
        f.review_results = [loops.findings(loops.finding()), {}]
        f.run_loop()
        before = {p: p.read_bytes() for p in (f.store.path, f.registry.path)}
        result = self.read()
        self.assertEqual(result['kind'], 'report', result)
        self.assertEqual(result['text'], loops.loop('DEV-7', action='status').render())
        for check in checks:
            self.assertIn(f"Check {check['name']}: {check['result']} — {check['details']}", result['text'])
        for text in ('Implementation (implementation): Full implementation summary', 'Review 1: findings',
                     'F1 [implementation/high]', 'Fix F1: Fixed F1', 'Review 2: clean', 'human action required: no'):
            self.assertIn(text, result['text'])
        overview = json.dumps(f.report(None).as_dict())
        for text in ('Full implementation summary', 'Limitation 79', 'Defect 1', 'Fixed F1', 'Current requirements'):
            self.assertNotIn(text, overview)
        for path, content in before.items():
            self.assertEqual(path.read_bytes(), content, 'Report/status reads must not mutate persistence')

    def test_paused_partial_report_and_sticky_pause_match_cli(self):
        f = self.fixture
        f.on_implementation = lambda *_: f.pause()
        f.run_loop()
        result = self.read()
        self.assertEqual(result['kind'], 'report', result)
        self.assertEqual(result['text'], loops.loop('DEV-7', action='status').render())
        self.assertIn('loop: paused', result['text'])
        self.assertIn('Implementation (implementation)', result['text'])
        self.assertIn('Final review: not_run; human action required: yes', result['text'])
        self.assertIn('Pause-after-current requested', result['text'])

    def test_interrupted_partial_report_does_not_infer_success(self):
        f = self.fixture
        f.on_review = lambda *_: (_ for _ in ()).throw(KeyboardInterrupt())
        f.run_loop()
        result = self.read()
        self.assertEqual(result['kind'], 'report', result)
        self.assertEqual(result['text'], loops.loop('DEV-7', action='status').render())
        self.assertIn('loop: interrupted', result['text'])
        self.assertIn('original agent may still be working', result['text'])
        self.assertIn('human action required: yes', result['text'])

    def test_blocked_assessment_reason_questions_and_failed_check_are_retained(self):
        f = self.fixture
        f.impl_overrides = dict(state='blocked', summary='Choose the required behavior',
            checks=[dict(name='browser', result='failed', details='Socket unavailable')],
            task_assessment=dict(state='blocked', summary='Product decision required', questions=['Which behavior?']))
        f.run_loop()
        result = self.read()
        self.assertEqual(result['kind'], 'report', result)
        for text in ('loop: escalated', 'Choose the required behavior', 'Question: Which behavior?',
                     'Check browser: failed — Socket unavailable', 'human action required: yes'):
            self.assertIn(text, result['text'])

    def test_missing_checkpoint_is_honest_diagnostics_and_does_not_create_storage(self):
        f = self.fixture
        result = self.read()
        self.assertEqual(result['kind'], 'diagnostics')
        self.assertEqual(result['message'], 'No saved loop report exists for this task.')
        self.assertIn(f.detail()['guidance']['message'], result['text'])
        self.assertFalse(f.store.path.exists())

    def test_unreadable_and_malformed_records_never_expose_exception_or_nested_prose(self):
        f = self.fixture
        f.run_loop()
        selected = f.detail()
        with patch.object(type(f.store), 'read', side_effect=TaskError('PRIVATE provider transcript')):
            result = self.read(selected)
        self.assertNotEqual(result['kind'], 'report')
        self.assertNotIn('PRIVATE', json.dumps(result))
        state, _ = f.store.read()
        for index, changes in ((0, {'checks': [dict(name='unit', result='imaginary', details='PRIVATE')]}),
                               (0, {'checks': [dict(name='unit', result='passed', details={'transcript': 'PRIVATE'})]}),
                               (0, {'resolutions': [dict(finding_id='F1', summary={'raw': 'PRIVATE'})]}),
                               (1, {'resolutions': [dict(finding_id='F1', summary={'raw': 'PRIVATE'})]})):
            changed = copy.deepcopy(state)
            changed['records'][index].update(changes)
            self.write_checkpoint(changed)
            result = self.read(selected)
            self.assertNotEqual(result['kind'], 'report', changes)
            self.assertNotIn('PRIVATE', json.dumps(result))

    def test_review_only_report_does_not_invent_implementation_results(self):
        f = self.fixture
        expected = f.run_loop(from_review=True).render()
        result = self.read()
        self.assertEqual(result['kind'], 'report', result)
        self.assertEqual(result['text'], expected)
        self.assertNotIn('Implementation (', result['text'])
        self.assertIn('1 passes, 1 reviews', result['text'])

    def test_raw_persisted_controller_exception_is_diagnostics_only(self):
        f = self.fixture
        f.on_implementation = lambda *_: (_ for _ in ()).throw(RuntimeError('PRIVATE provider transcript'))
        f.run_loop()
        result = self.read()
        self.assertEqual(result['kind'], 'diagnostics', result)
        self.assertNotIn('PRIVATE', json.dumps(result))

    def test_raw_persisted_failed_review_exception_is_diagnostics_only(self):
        f = self.fixture
        # Legacy failed-review summaries do not distinguish structured outcomes
        # from the review primitive's caught exception strings.
        f.review_results = [dict(state='failed', summary='PRIVATE review transport output', checks=[], findings=[])]
        f.run_loop()
        self.assertIn('PRIVATE', f.store.read()[0]['records'][-1]['summary'])
        result = self.read()
        self.assertEqual(result['kind'], 'diagnostics', result)
        self.assertNotIn('PRIVATE', json.dumps(result))

    def test_stale_clean_acceptance_withholds_saved_success(self):
        f = self.fixture
        f.run_loop()
        selected = f.detail()
        (f.path / 'tracked.txt').write_text('unreviewed follow-up')
        result = self.read(selected)
        self.assertEqual(result['kind'], 'diagnostics')
        self.assertIn('stale_review', result['text'])
        self.assertNotIn('human action required: no', result['text'])

    def test_identity_changes_and_ambiguity_withhold_other_execution_report(self):
        f = self.fixture
        f.run_loop()
        selected = f.detail()
        for group, key, value in [('workspace', 'path', '/tmp/not-a-task'), ('workspace', 'workspace_id', 'wrong'),
                                  ('workspace', 'endpoint', '/wrong.sock'), ('workspace', 'branch', 'dev-7-other'),
                                  ('repository', 'path', '/tmp/other-repo')]:
            changed = copy.deepcopy(selected)
            changed[group][key] = value
            self.assertNotEqual(self.read(changed)['kind'], 'report', (group, key))
        state, _ = f.store.read()
        for key, value in [('issue', 'DEV-8'), ('worktree', '/other'), ('repository', '/other'),
                           ('workspace_id', 'w2'), ('endpoint', '/other.sock'), ('branch', 'dev-7-other')]:
            changed = copy.deepcopy(state)
            changed['binding'][key] = value
            self.write_checkpoint(changed)
            self.assertNotEqual(self.read(selected)['kind'], 'report', key)
        self.write_checkpoint(state)
        entries = Herdr(f.repo).workspaces()
        with patch.object(Herdr, 'workspaces', return_value=entries + entries):
            self.assertNotEqual(self.read(selected)['kind'], 'report')
        contexts = f.registry.list(include_retired=True)
        next(c for c in contexts if c['context_id'] == f.implementation)['model'] = 'different-model'
        with patch.object(f.registry, 'list', return_value=contexts):
            self.assertNotEqual(self.read(selected)['kind'], 'report')

    def test_checkpoint_or_discovery_change_during_read_discards_report(self):
        f = self.fixture
        f.run_loop()
        selected = f.detail()
        original = f.store.read()
        changed = copy.deepcopy(original)
        changed[0]['reason'] = 'Changed report'
        with patch.object(type(f.store), 'read', side_effect=[original, changed]):
            self.assertNotEqual(self.read(selected)['kind'], 'report')
        entries = Herdr(f.repo).workspaces()
        with patch.object(Herdr, 'workspaces', side_effect=[entries, []]):
            self.assertNotEqual(self.read(selected)['kind'], 'report')
        with patch.object(Herdr, 'workspaces', side_effect=TaskError('PRIVATE outage')):
            result = self.read(selected)
            self.assertNotEqual(result['kind'], 'report')
            self.assertNotIn('PRIVATE', json.dumps(result))


class PublicationStatusTests(unittest.TestCase):
    setUp = publishing.PublishingTests.setUp
    command = publishing.PublishingTests.command
    worktrees = publishing.PublishingTests.worktrees
    pane_command = publishing.PublishingTests.pane_command
    select_adapter = publishing.PublishingTests.select_adapter
    prepare = publishing.PublishingTests.prepare
    api = publishing.PublishingTests.api

    def report(self):
        return task_status('DEV-7', registry=self.registry, identities=self.identities,
            projects=[Project(reviews.ISSUE.project, self.repo.name, 'main')],
            projects_root=self.repo.parent)

    def detail(self):
        return self.report().as_dict()['tasks'][0]

    def test_published_evidence_and_registered_workspace_survive_local_base_advance(self):
        self.prepare()
        publishing.publish('DEV-7')
        self.command(self.repo, 'commit', '--allow-empty', '-m', 'base advances')
        task = self.detail()
        self.assertEqual(task['publication']['state'], 'complete', task)
        self.assertEqual(task['publication']['pull_number'], 1)
        self.assertEqual(task['publication']['repository'], 'owner/project')
        self.assertEqual((task['state'], task['phase']), ('published', 'publication'))
        self.assertEqual(task['review']['acceptance'], 'valid')
        self.assertEqual(task['workspace']['registration'], 'registered')

    def test_newly_accepted_followup_is_not_already_published(self):
        self.prepare()
        publishing.publish('DEV-7')
        first = self.store.read()['publication_history']['cycles'][-1]
        (self.path / 'tracked.txt').write_text('Follow-up implementation\n')
        accepted = review('DEV-7')
        self.assertNotEqual(accepted.pass_id, first['acceptance']['pass_id'])
        task = self.detail()
        self.assertEqual((task['state'], task['phase']), ('clean', 'review'), task)
        self.assertEqual(task['review']['acceptance'], 'valid')
        self.assertEqual(task['publication']['state'], 'complete')
        self.assertFalse(task['publication']['current_acceptance_published'])
        self.assertEqual(task['publication']['pull_number'], 1)
        self.assertEqual(task['publication']['head'], first['intent']['publishing_head'])
        self.assertIn('DEV-7: clean (review)', self.report().render())
        self.assertIn('saved cycle; current acceptance delivery unconfirmed', self.report().render())
        self.assertTrue(self.command(self.path, 'diff'))
        publishing.publish('DEV-7')
        task = self.detail()
        self.assertEqual(task['state'], 'published', task)
        self.assertTrue(task['publication']['current_acceptance_published'])
        self.assertEqual(task['review']['pass_id'], accepted.pass_id)
        cycles = [e for e in task['history']['entries'] if e['source'] == 'publication']
        self.assertEqual([e['pass_id'] for e in cycles], [first['acceptance']['pass_id'], accepted.pass_id])

    def test_published_content_drift_withholds_current_delivery(self):
        self.prepare()
        publishing.publish('DEV-7')
        self.assertTrue(self.detail()['publication']['current_acceptance_published'])
        (self.path / 'tracked.txt').write_text('Unreviewed follow-up\n')
        task = self.detail()
        self.assertNotEqual(task['state'], 'published')
        self.assertFalse(task['publication']['current_acceptance_published'])
        self.assertEqual(task['publication']['pull_number'], 1)

    def test_push_evidence_without_pr_confirmation_is_pending(self):
        self.prepare()
        self.github_failure = TaskError('Uncertain PR creation response')
        with self.assertRaises(TaskError):
            publishing.publish('DEV-7')
        task = self.detail()
        self.assertEqual(task['state'], 'publication_pending', task)
        self.assertEqual(task['publication']['state'], 'published')
        self.assertIsNone(task['publication']['pull_number'])
        self.assertEqual(task['guidance']['kind'], 'inspect')
        self.assertEqual(task['history']['entries'][-2]['state'], 'published')

    def test_conflicting_publication_intent_is_not_authorization(self):
        self.prepare()
        publishing.publish('DEV-7')
        saved = self.store.read()
        saved['intent']['title'] = 'Conflicting title'
        self.store.write(saved)
        task = self.detail()
        self.assertEqual(task['state'], 'uncertain')
        self.assertEqual(task['publication']['state'], 'unknown')
        self.assertNotEqual(task['review']['acceptance'], 'valid')


class IntegrationStatusTests(unittest.TestCase):
    setUp = integrations.IntegrationTests.setUp
    command = integrations.IntegrationTests.command
    worktrees = integrations.IntegrationTests.worktrees
    pane_command = integrations.IntegrationTests.pane_command
    select_adapter = integrations.IntegrationTests.select_adapter
    prepare = integrations.IntegrationTests.prepare
    api = integrations.IntegrationTests.api
    operations = integrations.IntegrationTests.operations
    advance_remote = integrations.IntegrationTests.advance_remote
    prepare_integration = integrations.IntegrationTests.prepare_integration
    detail = PublicationStatusTests.detail
    report = PublicationStatusTests.report

    def test_integration_running_then_installed_requires_new_review(self):
        self.prepare_integration()
        self.advance_remote(conflict=True)
        seen = []
        self.integrator.mutation = lambda *args: seen.append(self.detail())
        integrations.integrate('DEV-7')
        self.assertEqual(seen[0]['phase'], 'integration', seen)
        self.assertEqual(seen[0]['controller'], 'held')
        task = self.detail()
        self.assertEqual(task['integration']['state'], 'installed', task)
        self.assertEqual(task['contexts']['integration']['context_id'], 'DEV-7-G1')
        self.assertEqual(task['state'], 'stale_review')
        self.assertEqual(task['review']['acceptance'], 'absent')
        self.assertEqual(review('DEV-7').state, 'clean')
        self.assertEqual(self.detail()['review']['acceptance'], 'valid')

    def test_integration_uncertain_delivery_requires_inspection(self):
        self.prepare_integration()
        self.advance_remote(conflict=True)
        self.integrator.error = TaskError('uncertain delivery')
        with self.assertRaises(TaskError):
            integrations.integrate('DEV-7')
        task = self.detail()
        self.assertEqual(task['integration']['state'], 'uncertain', task)
        self.assertEqual(task['guidance']['kind'], 'inspect')
        self.assertIsNone(task['guidance']['command'])


class CleanupStatusTests(unittest.TestCase):
    setUp = cleanup_fixture.ForcedCleanupTests.setUp
    command = cleanup_fixture.ForcedCleanupTests.command
    workspace_entry = cleanup_fixture.ForcedCleanupTests.workspace_entry
    list_worktrees = cleanup_fixture.ForcedCleanupTests.list_worktrees
    workspace_commands = cleanup_fixture.ForcedCleanupTests.workspace_commands
    process_info = cleanup_fixture.ForcedCleanupTests.process_info
    context = cleanup_fixture.ForcedCleanupTests.context

    def report(self, issue=None):
        return task_status(issue, registry=self.registry, identities=self.identities,
            projects=[Project(self.issue.project, self.repo.name, 'main')], projects_root=self.repo.parent).as_dict()

    def test_cleanup_removes_execution_from_overview_keeps_diagnostic_tombstone(self):
        saved = self.publication.read()
        saved['acceptance'] = None
        self.publication.write(saved)
        self.assertIn('DEV-7', [t['issue'] for t in self.report()['tasks']])
        cli.cleanup('DEV-7', force=True)
        self.assertNotIn('DEV-7', [t['issue'] for t in self.report()['tasks']])
        task = self.report('DEV-7')['tasks'][0]
        self.assertEqual(task['cleanup'][0]['state'], 'complete', task)
        self.assertEqual(task['workspace']['registration'], 'absent')
        self.assertEqual(task['history']['entries'][-1]['source'], 'cleanup')
        self.assertEqual(task['state'], 'cleaned')

    def test_prior_cleanup_tombstone_cannot_classify_reused_checkout_as_cleaned(self):
        cli.cleanup('DEV-7', force=True)
        self.command(self.repo, 'worktree', 'add', '-b', self.branch, str(self.path))
        self.git.save_scope(self.path, self.branch, 'DEV-7', None)
        self.workspace_active = True
        self.context('implementation', self.path, 'active')
        task = self.report('DEV-7')['tasks'][0]
        self.assertEqual(task['state'], 'implementation', task)
        self.assertEqual(task['cleanup'], [])
        self.assertIn('DEV-7', [t['issue'] for t in self.report()['tasks']])


class AbortStatusTests(unittest.TestCase):
    setUp = abort_fixture.AbortTests.setUp
    command = abort_fixture.AbortTests.command
    pane_command = abort_fixture.AbortTests.pane_command
    worktrees = abort_fixture.AbortTests.worktrees
    select_adapter = abort_fixture.AbortTests.select_adapter
    interrupt = abort_fixture.AbortTests.interrupt
    stop_agents = abort_fixture.AbortTests.stop_agents
    abort = abort_fixture.AbortTests.abort
    detail = PublicationStatusTests.detail
    report = PublicationStatusTests.report

    def test_aborted_review_keeps_history_and_explicit_discard_option(self):
        self.interrupt('review')
        self.stop_agents()
        self.abort()
        task = self.detail()
        self.assertEqual(task['state'], 'aborted', task)
        self.assertEqual(task['review']['acceptance'], 'absent')
        self.assertTrue(any(e['state'] == 'aborted' for e in task['history']['entries']))
        self.assertEqual(task['guidance']['kind'], 'inspect')
        self.assertEqual(task['guidance']['options'][0]['command'], 'task cleanup DEV-7 --force')
        self.assertTrue(task['guidance']['options'][0]['conditional'])
