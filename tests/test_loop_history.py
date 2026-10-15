"""Bounded superseded evidence on disposable tasks, never a routing source."""

import copy
from dataclasses import replace
import io
import json
import unittest
from unittest.mock import patch
from uuid import uuid4

from task_start import TaskError, cli
from task_start.loop import loop
from task_start.loop_state import LoopStore, ITERATION_LIMIT, ITERATION_BYTES
from task_start.publication_state import PublicationStore
from task_start.publish import native_git, publish
from task_start.review import review
from task_start.review_handoff_state import ReviewHandoffStore
from task_start.status import task_loop_report, task_status
from task_start.workspace import Herdr
import test_loop as loops
import test_status as status_fixture
import test_publish as publishing


class IterationTests(unittest.TestCase):
    command = loops.LoopIntegrationTests.command
    pane_command = loops.LoopIntegrationTests.pane_command
    worktrees = loops.LoopIntegrationTests.worktrees
    select_adapter = loops.LoopIntegrationTests.select_adapter
    run_loop = loops.LoopIntegrationTests.run_loop
    setUp = status_fixture.StatusTests.setUp
    report = status_fixture.StatusTests.report
    detail = status_fixture.StatusTests.detail

    def report_for(self, selected, run_id=None):
        return task_loop_report(selected, prior_run_id=run_id, registry=self.registry,
            identities=self.identities, projects=self.projects, projects_root=self.repo.parent)

    def prior_clean(self):
        self.review_results = [loops.findings(loops.finding(1)), loops.findings(loops.finding(2)),
            dict(checks=[dict(name='full regression', result='not_run', details='Merge-time CI pending', scope='merge')])]
        result = self.run_loop()
        self.assertEqual((result.state, result.data['passes'], result.data['reviews']), ('clean', 6, 3))
        return result

    def replace(self, **kwargs):
        return loop('DEV-7', action='new', from_review=True, **kwargs)

    def test_six_pass_clean_r1_and_one_pass_r2_keep_exact_reports_and_current_counts(self):
        first = self.prior_clean()
        before = self.store.read()[0]
        # An independently completed edit, followed by explicit review-first entry.
        (self.path / 'completed-followup.txt').write_text('completed follow-up implementation')
        self.assertNotEqual(self.detail()['review']['acceptance'], 'valid')
        second = self.replace()
        task = self.detail()
        self.assertEqual((second.data['passes'], second.data['reviews'], second.data['reviewer_context']), (1, 1, 'DEV-7-R2'))
        self.assertEqual((task['loop']['passes'], task['loop']['reviews']), (1, 1))
        self.assertEqual(task['contexts']['review']['context_id'], 'DEV-7-R2')
        self.assertEqual(task['review']['context_id'], 'DEV-7-R2')
        self.assertEqual(task['state'], 'clean')
        history = task['earlier_iterations']
        self.assertEqual(history['state'], 'available')
        old, = history['entries']
        self.assertEqual((old['state'], old['passes'], old['reviews'], old['reviewer_context']), ('clean', 6, 3, 'DEV-7-R1'))
        self.assertEqual(old['run_id'], before['run_id'])
        self.assertEqual(old['evidence'], 'verified')
        self.assertEqual(self.report_for(task, old['run_id'])['text'], first.render())
        self.assertEqual(self.report_for(task)['text'], second.render())
        self.assertNotIn('Defect 1', json.dumps(task))  # Report prose remains on demand.
        self.assertNotIn('Merge-time CI pending', json.dumps(task))
        self.assertIn('Earlier iterations:', self.report().render())
        self.assertIn('6 passes / 3 reviews', self.report().render())
        self.assertIn('Current iteration:', self.report().render())
        self.assertEqual(self.store.path.stat().st_mode & 0o777, 0o600)
        with patch('task_start.cli.task_status', side_effect=lambda issue: self.report(issue)), \
                patch('sys.stdout', new_callable=io.StringIO) as output:
            self.assertEqual(cli.main(['status', 'DEV-7', '--json']), 0)
            self.assertEqual(json.loads(output.getvalue())['tasks'][0]['earlier_iterations'], history)

    def test_current_findings_and_attention_never_inherit_old_clean_acceptance(self):
        self.prior_clean()
        self.review_results = [loops.findings(loops.finding(4))]
        done = self.replace(max_reviews=1)
        task = self.detail()
        self.assertEqual(done.state, 'escalated')
        self.assertEqual(task['state'], 'escalated')
        self.assertTrue(task['guidance']['human_action_required'])
        self.assertEqual(task['guidance']['kind'], 'extend')
        self.assertEqual(task['review']['state'], 'findings')
        self.assertNotEqual(task['review']['acceptance'], 'valid')
        self.assertFalse(task['publication']['current_acceptance_published'])
        self.assertEqual(task['earlier_iterations']['entries'][0]['state'], 'clean')
        self.assertIn('F4', self.report_for(task)['text'])
        self.assertNotIn('Defect 1', self.report_for(task)['text'])
        retained = self.store.iterations()
        run_id = task['loop']['run_id']
        self.review_results = [{}]
        self.assertEqual(loop('DEV-7', action='extend', max_reviews=3).state, 'clean')
        self.assertEqual(self.store.read()[0]['run_id'], run_id)
        self.assertEqual(self.store.iterations(), retained)

    def test_unclaimed_replacement_does_not_select_the_previous_reviewer_or_acceptance(self):
        self.run_loop()
        old = self.store.read()[0]
        # Stop directly after atomic replacement, before any new provider work.
        with patch('task_start.loop.drive', side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.replace()
        retained = self.store.iterations()
        with self.assertRaises(TaskError):
            self.replace()
        self.assertEqual(self.store.iterations(), retained)
        current = self.store.read()[0]
        current.update(status='paused', reason='Explicit pause before the first review')
        self.store.save(current)
        task = self.detail()
        self.assertEqual(task['state'], 'paused')
        self.assertIsNone(task['contexts']['review'])
        self.assertEqual(task['review']['state'], 'not_recorded')
        self.assertNotEqual(task['review']['acceptance'], 'valid')
        self.assertIsNone(task['review']['context_id'])
        self.assertFalse(task['publication']['current_acceptance_published'])
        self.assertEqual(task['earlier_iterations']['entries'][0]['reviewer_context'], 'DEV-7-R1')
        self.assertEqual(task['earlier_iterations']['entries'][0]['run_id'], old['run_id'])

    def test_missing_or_retired_historical_reviewer_does_not_change_current_guidance(self):
        self.run_loop()
        with patch('task_start.loop.drive', side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.replace()
        current = self.store.read()[0]
        current.update(status='paused', reason='Explicit pause before the first review')
        self.store.save(current)
        before = self.detail()
        self.assertEqual((before['state'], before['guidance']['kind']), ('paused', 'continue'))
        reviewer = self.registry.get('DEV-7-R1')
        self.registry.retire_execution([reviewer], '2026-10-14T00:00:00Z')
        for state in ('retired', 'missing'):
            with self.subTest(state=state):
                if state == 'missing':
                    with self.registry.connection(write=True) as db:
                        db.execute('DELETE FROM contexts WHERE context_id=?', ('DEV-7-R1',))
                after = self.detail()
                for key in ('state', 'guidance', 'contexts', 'review', 'uncertainties', 'loop'):
                    self.assertEqual(after[key], before[key], key)
                self.assertEqual(after['earlier_iterations']['state'], 'partial')
                self.assertFalse(after['earlier_iterations']['entries'][0]['report_available'])
                self.assertEqual(self.store.read()[0], current)

    def test_unsafe_inflight_checkpoint_is_not_retained(self):
        self.on_review = lambda _: (_ for _ in ()).throw(KeyboardInterrupt())
        self.assertEqual(self.run_loop().state, 'interrupted')
        before = self.store.read()
        with self.assertRaises(TaskError):
            self.replace()
        self.assertEqual(self.store.read(), before)
        self.assertIsNone(self.store.iterations())

    def test_pending_f4_refusal_and_extension_never_archive_or_allocate_r2(self):
        self.review_results = [loops.findings(loops.finding(n)) for n in (1, 2, 4)]
        self.run_loop()
        before = self.store.read()
        with self.assertRaisesRegex(TaskError, 'skip.*correction'):
            self.replace()
        self.assertEqual(self.store.read(), before)
        self.assertIsNone(self.store.iterations())
        self.assertEqual(len(self.registry.list()), 2)
        self.review_results = [{}]
        loop('DEV-7', action='extend', max_passes=10, max_reviews=5)
        self.assertEqual(self.store.read()[0]['run_id'], before[0]['run_id'])
        self.assertIsNone(self.store.iterations())
        self.assertEqual(len(self.registry.list()), 2)

    def test_sequential_replacements_bound_order_and_same_run_resume(self):
        self.run_loop()
        runs = []
        for _ in range(ITERATION_LIMIT + 2):
            runs.append(self.store.read()[0]['run_id'])
            self.replace()
        entries = self.store.iterations()
        self.assertEqual([e['summary']['run_id'] for e in entries], runs[-ITERATION_LIMIT:])
        self.assertEqual([e['order'] for e in entries], [3, 4, 5])
        before = copy.deepcopy(entries)
        loop('DEV-7')
        self.assertEqual(self.store.iterations(), before)
        self.assertEqual(len({e['summary']['run_id'] for e in entries}), ITERATION_LIMIT)

    def test_atomic_replacement_rollback_and_retry_preserve_exactly_one_predecessor(self):
        self.run_loop()
        old = self.store.read()
        new = copy.deepcopy(old[0])
        new['run_id'] = str(uuid4())
        with self.store.connection(write=True) as db:
            db.execute("CREATE TRIGGER fail_replacement BEFORE INSERT ON checkpoint BEGIN SELECT RAISE(ABORT, 'interrupted'); END")
        with self.assertRaises(TaskError):
            self.store.create(new, replace=True)
        self.assertEqual(self.store.read(), old)
        self.assertIsNone(self.store.iterations())  # DDL and retention rolled back too.
        with self.store.connection(write=True) as db:
            db.execute('DROP TRIGGER fail_replacement')
        self.store.create(new, replace=True)
        retained = self.store.iterations()
        self.assertEqual(len(retained), 1)
        self.assertEqual(retained[0]['checkpoint'], old[0])
        with self.assertRaisesRegex(TaskError, 'new run identity'):
            self.store.create(new, replace=True)
        with self.assertRaisesRegex(TaskError, 'retained run identity'):
            self.store.create(old[0], replace=True)
        self.assertEqual(self.store.iterations(), retained)
        self.assertEqual(self.store.read()[0], new)

    def test_pruning_and_replacement_rollback_together(self):
        self.run_loop()
        for _ in range(ITERATION_LIMIT):
            self.replace()
        before, entries = self.store.read(), self.store.iterations()
        new = copy.deepcopy(before[0])
        new['run_id'] = str(uuid4())
        with self.store.connection(write=True) as db:
            db.execute("CREATE TRIGGER fail_replacement BEFORE INSERT ON checkpoint BEGIN SELECT RAISE(ABORT, 'interrupted'); END")
        with self.assertRaises(TaskError):
            self.store.create(new, replace=True)
        self.assertEqual(self.store.read(), before)
        self.assertEqual(self.store.iterations(), entries)

    def test_oversized_evidence_is_partial_and_never_copyable(self):
        self.run_loop()
        old = self.store.read()[0]
        old['records'][0]['summary'] = 'large' * ITERATION_BYTES
        self.store.save(old)
        self.replace()
        task = self.detail()
        entry, = task['earlier_iterations']['entries']
        self.assertEqual(entry['evidence'], 'partial')
        self.assertEqual(entry['state'], 'unknown')
        self.assertEqual((entry['passes'], entry['reviews']), (2, 1))
        self.assertFalse(entry['report_available'])
        self.assertEqual(task['state'], 'clean')
        self.assertNotEqual(self.report_for(task, old['run_id'])['kind'], 'report')
        with self.store.connection() as db:
            summary, payload = db.execute('SELECT summary, payload FROM iterations').fetchone()
        self.assertIsNone(payload)
        self.assertLess(len(summary), 16000)

    def test_absent_legacy_history_is_unavailable_and_read_only(self):
        self.run_loop()
        before = self.store.path.read_bytes()
        task = self.detail()
        self.assertEqual(task['earlier_iterations']['state'], 'unavailable')
        self.assertIn('cannot be reconstructed', task['earlier_iterations']['message'])
        self.assertEqual(task['earlier_iterations']['entries'], [])
        self.assertEqual(before, self.store.path.read_bytes())

    def test_corrupt_missing_changed_or_foreign_history_cannot_change_current_state(self):
        self.run_loop()
        old = self.store.read()[0]
        self.replace()
        selected = self.detail()
        original = self.store.iterations()
        with self.store.connection() as db:
            row = db.execute('SELECT summary, payload FROM iterations').fetchone()
        for kind in ('corrupt', 'oversized', 'foreign', 'missing'):
            with self.subTest(kind=kind):
                with self.store.connection(write=True) as db:
                    db.execute('UPDATE iterations SET summary=?, payload=?', row)
                    if kind == 'corrupt':
                        db.execute("UPDATE iterations SET payload='broken'")
                    elif kind == 'oversized':
                        db.execute('UPDATE iterations SET payload=?', ('x' * (ITERATION_BYTES + 1),))
                    elif kind == 'foreign':
                        summary, state = map(json.loads, row)
                        for obj in (summary, state):
                            obj['binding']['worktree'] = '/another/worktree'
                        db.execute('UPDATE iterations SET summary=?, payload=?', (json.dumps(summary), json.dumps(state)))
                    else:
                        db.execute('DELETE FROM iterations')
                task = self.detail()
                self.assertEqual(task['state'], 'clean')
                self.assertEqual(task['review']['acceptance'], 'valid')
                self.assertEqual(task['guidance'], selected['guidance'])
                self.assertFalse(any(e['report_available'] for e in task['earlier_iterations']['entries']))
                self.assertNotEqual(self.report_for(selected, old['run_id'])['kind'], 'report')
                if kind == 'missing':
                    self.assertEqual(task['earlier_iterations']['state'], 'unavailable')
        self.assertNotEqual(self.store.iterations(), original)

    def test_historical_context_session_mismatch_withholds_report_only(self):
        self.run_loop()
        old = self.store.read()[0]
        self.replace()
        selected = self.detail()
        contexts = self.registry.list(include_retired=True)
        changed = copy.deepcopy(contexts)
        next(c for c in changed if c['context_id'] == 'DEV-7-R1')['session_id'] = 'different-session'
        with patch.object(self.registry, 'list', return_value=changed):
            task = self.detail()
            self.assertEqual(task['state'], 'clean')
            self.assertEqual(task['earlier_iterations']['entries'][0]['evidence'], 'unavailable')
            self.assertNotEqual(self.report_for(selected, old['run_id'])['kind'], 'report')

    def test_unfiltered_historical_exception_never_exposed(self):
        self.run_loop()
        old = self.store.read()[0]
        old.update(status='escalated', reason='SECRET exception. Inspect contexts before recovery; no handoff was retried')
        self.store.save(old)
        self.replace()
        task = self.detail()
        self.assertNotIn('SECRET', json.dumps(task))
        self.assertEqual(task['earlier_iterations']['state'], 'partial')
        self.assertNotIn('SECRET', json.dumps(self.report_for(task, old['run_id'])))

    def test_unsafe_and_cross_binding_replacement_preserve_store(self):
        self.run_loop()
        before = self.store.read()
        for key in ('issue', 'worktree', 'repository', 'workspace_id', 'endpoint', 'branch', 'base_branch'):
            new = copy.deepcopy(before[0])
            new['run_id'] = str(uuid4())
            new['binding'][key] += '-different'
            with self.subTest(key=key), self.assertRaises(TaskError):
                self.store.create(new, replace=True)
            self.assertEqual(self.store.read(), before)
            self.assertIsNone(self.store.iterations())
        self.assertFalse(self.detail()['earlier_iterations']['entries'])

    def test_stale_current_selection_never_returns_replacement_report(self):
        self.run_loop()
        selected = self.detail()
        self.replace()
        self.assertNotEqual(self.report_for(selected)['kind'], 'report')

    def test_plain_new_after_clean_retains_previous_report(self):
        first = self.run_loop()
        old = self.store.read()[0]
        second = self.run_loop(action='new')
        task = self.detail()
        self.assertNotEqual(second.data['run_id'], old['run_id'])
        self.assertEqual(second.data['reviewer_context'], 'DEV-7-R2')
        self.assertEqual(task['loop']['passes'], second.data['passes'])
        self.assertEqual(self.report_for(task, old['run_id'])['text'], first.render())

    def test_history_change_during_projection_discards_historical_report(self):
        self.run_loop()
        old = self.store.read()[0]
        self.replace()
        selected = self.detail()
        entries = self.store.iterations()
        with patch.object(LoopStore, 'iterations', side_effect=[entries, []]):
            self.assertNotEqual(self.report_for(selected, old['run_id'])['kind'], 'report')

    def test_unregistered_task_history_is_not_available_through_overview_or_report(self):
        self.run_loop()
        old = self.store.read()[0]
        self.replace()
        selected = self.detail()
        before = self.store.path.read_bytes()
        with patch.object(Herdr, 'workspaces', return_value=[]):
            self.assertEqual(self.report(None).as_dict()['tasks'], [])
            self.assertEqual(self.detail()['earlier_iterations']['state'], 'unavailable')
            self.assertNotEqual(self.report_for(selected, old['run_id'])['kind'], 'report')
        self.assertEqual(self.store.path.read_bytes(), before)

    def test_aborted_iteration_report_requires_original_abandonment_evidence(self):
        case = status_fixture.abort_fixture.AbortTests()
        case.setUp()
        try:
            old = case.interrupt('review')
            case.stop_agents()
            expected = case.abort().render()
            case.resume_implementation()
            self.assertEqual(loop('DEV-7', action='new', from_review=True).state, 'clean')
            options = dict(registry=case.registry, identities=case.identities,
                projects=[status_fixture.Project(status_fixture.reviews.ISSUE.project, case.repo.name, 'main')],
                projects_root=case.repo.parent)
            task = task_status('DEV-7', **options).as_dict()['tasks'][0]
            entry, = task['earlier_iterations']['entries']
            self.assertEqual((entry['state'], entry['evidence']), ('aborted', 'verified'))
            self.assertEqual(task_loop_report(task, prior_run_id=old['run_id'], **options)['text'], expected)
            with patch.object(LoopStore, 'abort_journal', return_value=None):
                changed = task_status('DEV-7', **options).as_dict()['tasks'][0]
                self.assertEqual(changed['state'], 'clean')
                self.assertEqual(changed['earlier_iterations']['state'], 'partial')
                self.assertNotEqual(task_loop_report(task, prior_run_id=old['run_id'], **options)['kind'], 'report')
        finally:
            case.doCleanups()


class IterationPublicationTests(unittest.TestCase):
    """Publication admission uses the active run, even before its first pass."""

    command = loops.LoopIntegrationTests.command
    pane_command = loops.LoopIntegrationTests.pane_command
    worktrees = loops.LoopIntegrationTests.worktrees
    select_adapter = loops.LoopIntegrationTests.select_adapter
    run_loop = loops.LoopIntegrationTests.run_loop
    report = status_fixture.StatusTests.report
    detail = status_fixture.StatusTests.detail
    api = publishing.PublishingTests.api
    operations = publishing.PublishingTests.operations
    assert_published = publishing.PublishingTests.assert_published
    advance_remote = publishing.PublishingTests.advance_remote

    def setUp(self):
        status_fixture.StatusTests.setUp(self)
        self.loop_store, self.store = self.store, PublicationStore(self.path)
        self.linear.get_issue.return_value = replace(self.linear.get_issue.return_value,
            labels=('Feature',), url='https://linear.app/workspace/issue/DEV-7/original-title')
        (self.path / 'tracked.txt').write_text('implemented after steering\n')
        self.enterContext(patch('task_start.publish.load_local', return_value=self.local))
        for symbol in ('load_projects', 'Linear', 'ContextRegistry', 'HerdrContexts'):
            import task_start.review as review_module
            self.enterContext(patch(f'task_start.publish.{symbol}', getattr(review_module, symbol)))
        self.enterContext(patch('task_start.publish.api_credential', return_value='synthetic-token'))
        self.enterContext(patch('task_start.publish.remote_identity',
            return_value=dict(remote='origin', repository='owner/project')))
        self.calls, self.pulls, self.github_failure = [], [], None
        self.enterContext(patch('task_start.github.request', side_effect=self.api))
        self.native = self.enterContext(patch('task_start.publish.native_git', wraps=native_git))

    def clean_loop(self):
        self.review_results = [dict(publication=publishing.PUBLIC)]
        self.assertEqual(self.run_loop().state, 'clean')

    def replace_before_drive(self, *, from_review=True):
        with patch('task_start.loop.drive', side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                loop('DEV-7', action='new', from_review=from_review)

    def assert_refused_before_publication(self):
        before = self.store.read()
        checkpoint = self.loop_store.read()
        with patch('task_start.publish.prepared_index', side_effect=AssertionError('Historical acceptance reached tree preparation')) as prepare:
            with self.assertRaisesRegex(TaskError, 'current.*acceptance'):
                publish('DEV-7')
        prepare.assert_not_called()
        self.assertFalse(self.native.called)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.store.read(), before)
        self.assertEqual(self.loop_store.read(), checkpoint)

    def replacement_rejects_old_acceptance(self, from_review):
        self.review_results = [loops.findings(loops.finding(1)), loops.findings(loops.finding(2)),
                               dict(publication=publishing.PUBLIC)]
        self.assertEqual(self.run_loop().state, 'clean')
        old = self.loop_store.read()[0]
        self.assertEqual((old['pass_count'], old['review_count']), (6, 3))
        accepted = self.store.read()['acceptance']
        self.replace_before_drive(from_review=from_review)
        current = self.loop_store.read()[0]
        self.assertNotEqual(current['run_id'], old['run_id'])
        self.assertEqual((current['pass_count'], current['review_count'], current['reviewer']), (0, 0, None))
        self.assertEqual(self.store.read()['acceptance'], accepted)
        self.assertEqual(self.loop_store.iterations()[0]['summary']['run_id'], old['run_id'])
        self.assert_refused_before_publication()
        self.assert_refused_before_publication()  # Retrying admission cannot consume old evidence.

    def test_interrupted_review_first_replacement_refuses_previous_clean_acceptance(self):
        self.replacement_rejects_old_acceptance(True)

    def test_interrupted_implementation_first_replacement_refuses_previous_clean_acceptance(self):
        self.replacement_rejects_old_acceptance(False)

    def test_current_loop_acceptance_can_publish_and_retry_after_commit(self):
        self.clean_loop()
        def fail_push(path, operation, *args, **kwargs):
            if operation == 'push':
                raise TaskError('synthetic push failure')
            return native_git(path, operation, *args, **kwargs)
        self.native.side_effect = fail_push
        with self.assertRaisesRegex(TaskError, 'synthetic push failure'):
            publish('DEV-7')
        frozen = self.store.read()['intent']['publishing_head']
        self.assert_new_preserves_unfinished_publication()
        self.native.side_effect = None
        url = publish('DEV-7')
        self.assertEqual(publish('DEV-7'), url)
        self.assertEqual(self.assert_published(), frozen)
        self.assertEqual(len(self.operations('commit')), 1)

    def assert_new_preserves_unfinished_publication(self):
        checkpoint = self.loop_store.read()
        history = self.loop_store.iterations()
        saved = self.store.read()
        contexts = self.registry.list(include_retired=True)
        calls = (list(self.native.call_args_list), copy.deepcopy(self.calls))
        for from_review in (False, True):
            with self.subTest(from_review=from_review), \
                    patch.object(LoopStore, 'create', wraps=self.loop_store.create) as create, \
                    patch('task_start.loop.drive', side_effect=AssertionError('Replacement reached drive')) as drive:
                with self.assertRaisesRegex(TaskError, '(unfinished|pending).*task pr'):
                    loop('DEV-7', action='new', from_review=from_review)
                create.assert_not_called()
                drive.assert_not_called()
                self.assertEqual(self.loop_store.read(), checkpoint)
                self.assertEqual(self.loop_store.iterations(), history)
                self.assertEqual(self.store.read(), saved)
                self.assertEqual(self.registry.list(include_retired=True), contexts)
                self.assertEqual((self.native.call_args_list, self.calls), calls)

    def test_new_refuses_precommit_intent_and_original_pr_still_completes(self):
        self.clean_loop()
        with patch('task_start.publish.commit', side_effect=TaskError('synthetic precommit interruption')):
            with self.assertRaisesRegex(TaskError, 'synthetic precommit interruption'):
                publish('DEV-7')
        self.assertIsNotNone(self.store.read()['intent'])
        self.assertNotIn('publishing_head', self.store.read()['intent'])
        self.assertIsNone(self.store.read()['publication_history'])
        self.assert_new_preserves_unfinished_publication()
        publish('DEV-7')
        self.assert_published()
        self.assertEqual(len(self.operations('commit')), 1)

    def test_new_refuses_commit_acknowledgement_gap_and_pr_reuses_commit(self):
        self.clean_loop()
        def lose_acknowledgement(path, operation, *args, **kwargs):
            result = native_git(path, operation, *args, **kwargs)
            if operation == 'commit':
                raise TaskError('synthetic lost commit acknowledgement')
            return result
        self.native.side_effect = lose_acknowledgement
        with self.assertRaisesRegex(TaskError, 'synthetic lost commit acknowledgement'):
            publish('DEV-7')
        head = self.command(self.path, 'rev-parse', 'HEAD')
        self.assertNotEqual(head, self.store.read()['acceptance']['review_state']['head'])
        self.assertNotIn('publishing_head', self.store.read()['intent'])
        self.assert_new_preserves_unfinished_publication()
        self.native.side_effect = None
        publish('DEV-7')
        self.assertEqual(self.assert_published(), head)
        self.assertEqual(len(self.operations('commit')), 1)

    def test_new_refuses_incomplete_pr_and_retry_reuses_publication(self):
        self.clean_loop()
        self.github_failure = TaskError('synthetic lost PR acknowledgement')
        with self.assertRaisesRegex(TaskError, 'synthetic lost PR acknowledgement'):
            publish('DEV-7')
        saved = self.store.read()
        self.assertEqual(saved['publication_history']['state'], 'published')
        self.assertNotEqual(saved['publication_history']['cycles'][-1]['state'], 'complete')
        self.assert_new_preserves_unfinished_publication()
        self.github_failure = None
        publish('DEV-7')
        self.assert_published()
        self.assertEqual(len(self.operations('commit')), 1)
        self.assertEqual(len(self.operations('push')), 1)
        self.assertEqual(sum(c[0] == 'POST' for c in self.calls), 1)

    def test_new_refuses_pending_rebase_and_original_pr_can_recover_it(self):
        self.clean_loop()
        base = self.advance_remote()
        with patch('task_start.publish.continue_rebase', side_effect=TaskError('synthetic rebase interruption')):
            with self.assertRaisesRegex(TaskError, 'synthetic rebase interruption'):
                publish('DEV-7')
        self.assertIsNone(self.store.read()['rebase']['result'])
        self.assert_new_preserves_unfinished_publication()
        with self.assertRaisesRegex(TaskError, 'new independent task review'):
            publish('DEV-7')
        self.review_results = [dict(publication=publishing.PUBLIC)]
        self.assertEqual(review('DEV-7').state, 'clean')
        publish('DEV-7')
        self.base = base
        self.assert_published()
        self.assertEqual(len(self.operations('commit-tree')), 1)
        self.assertFalse(self.operations('commit'))

    def test_completed_publication_allows_new_iteration_and_reviewed_followup(self):
        self.clean_loop()
        old = self.loop_store.read()[0]
        url = publish('DEV-7')
        self.assertEqual(self.store.read()['publication_history']['cycles'][-1]['state'], 'complete')
        (self.path / 'tracked.txt').write_text('independently completed follow-up\n')
        self.review_results = [dict(publication=publishing.PUBLIC)]
        result = loop('DEV-7', action='new', from_review=True)
        self.assertEqual(result.state, 'clean')
        current = self.loop_store.read()[0]
        self.assertNotEqual(current['run_id'], old['run_id'])
        self.assertEqual((current['pass_count'], current['review_count']), (1, 1))
        self.assertEqual(current['reviewer']['context_id'], 'DEV-7-R2')
        self.assertEqual(self.loop_store.iterations()[0]['summary']['run_id'], old['run_id'])
        self.assertEqual(publish('DEV-7'), url)
        self.assertEqual(len(self.store.read()['publication_history']['cycles']), 2)
        self.assertEqual(len(self.operations('commit')), 2)
        self.assertEqual(len(self.pulls), 1)

    def test_completed_standalone_review_of_new_run_can_publish_and_retry(self):
        self.standalone_review_publishes()

    def test_explicitly_resumed_standalone_review_of_new_run_can_publish_and_retry(self):
        self.standalone_review_publishes(resume='DEV-7-R1')

    def standalone_review_publishes(self, *, resume=None):
        self.clean_loop()
        old_pass = self.store.read()['acceptance']['pass_id']
        self.replace_before_drive()
        current = self.loop_store.read()[0]
        self.review_results = [dict(publication=publishing.PUBLIC)]
        result = review('DEV-7', resume=resume)
        self.assertEqual(result.state, 'clean')
        self.assertNotEqual(result.pass_id, old_pass)
        handoff = ReviewHandoffStore(self.path).read()
        self.assertEqual(handoff['origin']['run_id'], current['run_id'])
        self.assertIsNone(handoff['consumed'])
        url = publish('DEV-7')
        self.assertEqual(publish('DEV-7'), url)
        self.assert_published()
        self.assertEqual(self.loop_store.read()[0], current)

    def test_standalone_review_for_another_run_or_binding_cannot_authorize_current_run(self):
        self.clean_loop()
        self.replace_before_drive()
        self.review_results = [dict(publication=publishing.PUBLIC)]
        self.assertEqual(review('DEV-7').state, 'clean')
        handoffs = ReviewHandoffStore(self.path)
        good = handoffs.read()
        variants = []
        foreign_run = copy.deepcopy(good)
        foreign_run['origin']['run_id'] = str(uuid4())
        variants.append(foreign_run)
        foreign_binding = copy.deepcopy(good)
        foreign_binding['binding']['worktree'] += '-other'
        variants.append(foreign_binding)
        incomplete = copy.deepcopy(good)
        incomplete['result'] = None
        variants.append(incomplete)
        mismatched = copy.deepcopy(good)
        mismatched['result']['post_fingerprint'] = '0' * 64
        variants.append(mismatched)
        for value in variants:
            with self.subTest(origin=value['origin'], binding=value['binding'], result=value['result']):
                handoffs.write(value)
                self.assert_refused_before_publication()

    def test_fresh_standalone_review_after_loop_publication_rebase_still_authorizes_retry(self):
        self.clean_loop()
        current = self.loop_store.read()[0]
        base = self.advance_remote()
        with self.assertRaisesRegex(TaskError, 'new independent task review'):
            publish('DEV-7')
        self.assertIsNone(self.store.read()['acceptance'])
        self.review_results = [dict(publication=publishing.PUBLIC)]
        self.assertEqual(review('DEV-7').state, 'clean')
        url = publish('DEV-7')
        self.assertEqual(publish('DEV-7'), url)
        self.base = base
        self.assert_published()
        self.assertEqual(len(self.operations('commit-tree')), 1)
        self.assertFalse(self.operations('commit'))
        self.assertEqual(self.loop_store.read()[0], current)


if __name__ == '__main__':
    unittest.main()
