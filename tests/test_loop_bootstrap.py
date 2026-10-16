"""Initial loop launches with real disposable Git/checkpoint/registry boundaries."""

from dataclasses import replace
from concurrent.futures import ThreadPoolExecutor
import copy
import io
import json
import re
import threading
import time
from types import SimpleNamespace
from pathlib import Path
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from task_start import HerdrResponseError, TaskError, cli
from task_start.agent import AgentOptions, Codex, LaunchResult, Pi, adapter_for
from task_start.config import IssueStructureConfig, load_local
from task_start.contexts import ContextRegistry, context_reference, now
from task_start.handoff import IMPLEMENTATION_INSTRUCTIONS, REVIEW_VALIDATION_INSTRUCTIONS, implementation_handoff
from task_start.integration_process import verify_shell_process
from task_start.loop import loop, LoopRuntime
from task_start.loop_state import LoopStore
from task_start.metrics import MetricsStore
from task_start.linear import Linear
from task_start.workspace import Git, Herdr
from test_loop import ImplementationAdapter, finding, findings
import test_loop as loop_fixture
import test_review as fixture
from codex_startup_fixture import TRUST_SCREEN, CodexStartupTransport
import pass_delivery_fixture


UPDATE_BASE = Git.update_base
REMOTE_BRANCHES = Git.remote_branches


class PiInitialTransport:
    """Real Pi launch/status/history verification with controlled Herdr reports."""

    def __init__(self, test, *, history_at):
        self.test, self.history_at = test, history_at
        self.path = test.repo.parent / 'initial-pi.jsonl'
        self.reference = dict(agent='pi', kind='path', value=str(self.path), source='hook')
        self.now = self.gets = self.polls = 0
        self.on_get = lambda: None
        self.saved_identities = []
        self.prompt = self.completion = None
        test.enterContext(patch('task_start.implementation_pass.adapter_for', side_effect=Pi))
        test.enterContext(patch('task_start.agent.adapter_for', side_effect=lambda options:
                               Pi(options) if options.kind == 'pi' else test.adapter))
        test.enterContext(patch.object(Pi, 'check_available'))
        test.enterContext(patch.object(Pi, 'clear_shell_input'))
        test.enterContext(patch.object(Pi, 'filter_shell_credentials'))
        test.enterContext(patch.object(Pi, 'model_mode_capabilities', return_value=('implementer', 'low', ('low',))))
        self.command = test.enterContext(patch.object(Pi, 'command', side_effect=self.herdr))
        test.enterContext(patch('task_start.implementation_pass.time',
                               SimpleNamespace(monotonic=lambda: self.now, sleep=self.advance)))
        update = test.registry.update
        def record(context_id, **values):
            update(context_id, **values)
            if context_id == 'DEV-7-I1':
                self.saved_identities.append(context_reference(test.registry.get(context_id)))
        test.enterContext(patch.object(test.registry, 'update', side_effect=record))

    def advance(self, seconds):
        self.now += seconds

    def history(self, conversation_id='original-implementation', *, messages=True):
        entries = [dict(type='session', id=conversation_id, cwd=str(self.test.path))]
        if messages:
            entries.append(dict(type='message', id='input', parentId=None,
                                message=dict(role='user', content=self.prompt or 'Earlier input')))
            if self.completion:
                entries.append(dict(type='message', id='output', parentId='input',
                    message=dict(role='assistant', content=self.completion, stopReason='stop')))
        self.path.write_text(''.join(json.dumps(entry) + '\n' for entry in entries))

    def herdr(self, group, operation, *args):
        test = self.test
        pane = test.panes[0]
        if (group, operation) == ('pane', 'list'):
            return dict(panes=[dict(p) for p in test.panes])
        if (group, operation) == ('agent', 'start'):
            state, _ = test.store.read()
            test.assertEqual((state['status'], state['pass_count'], state['review_count']), ('running', 1, 0))
            test.assertEqual(state['active_pass']['context_id'], getattr(test, 'expected_implementation', 'DEV-7-I1'))
            test.assertIsNotNone(state['active_pass']['pass_id'])
            test.assertIn('--extension', args)
            self.history(messages=False)
            pane.update(agent='pi', agent_status='idle', foreground_cwd=str(test.path),
                        agent_session=dict(self.reference))
            return dict(agent=dict(pane), argv=['pi', *args[args.index('--') + 1:]])
        if (group, operation) == ('agent', 'prompt'):
            test.assertEqual(args[0], 'p1')
            prompt = args[1]
            self.prompt = prompt
            test.impl_prompts.append(prompt)
            test.assertIn('fresh conversation', prompt)
            if self.history_at == 'prompt':
                self.history()
            output = Path(re.search(r'Write your result to (.*?)\. This temporary', prompt).group(1))
            pass_id = re.search(r'Pass ID: ([^\n]+)', prompt).group(1)
            output.write_text(json.dumps(dict(pass_id=pass_id, state='completed', summary='Implemented',
                                              checks=[], resolutions=[], task_assessment=dict(
                                                  state='ready', summary='Controlled readiness outcome', questions=[]))))
            self.completion = pass_delivery_fixture.complete_prompt(prompt)
            if self.history_at == 'prompt':
                self.history()
            pane['agent_status'] = 'working'
            return dict(agent=dict(pane))
        if (group, operation) == ('agent', 'get'):
            self.gets += 1
            if self.gets >= 3:  # First two reads confirm startup and prompt delivery.
                self.polls += 1
                if self.polls == 1 and self.history_at == 'poll':
                    self.history()
                pane['agent_status'] = 'done' if self.polls >= 3 else 'working'
            self.on_get()
            return dict(agent=dict(pane))
        test.fail((group, operation, args))

    def assert_identity_retained(self):
        expected = dict(self.reference, conversation_id='original-implementation')
        context = self.test.registry.get('DEV-7-I1')
        self.test.assertEqual(context_reference(context), expected)
        self.test.assertEqual(context['resumability'], 'yes')
        state, _ = self.test.store.read()
        self.test.assertEqual(state['implementation']['session']['conversation_id'], expected['conversation_id'])
        verified = [i for i, ref in enumerate(self.saved_identities) if ref and 'conversation_id' in ref]
        self.test.assertTrue(verified, 'Pi history must be verified')
        for ref in self.saved_identities[verified[0]:]:
            self.test.assertEqual(ref, expected, 'No later persistence update may erase verified identity')
        effects = [call.args[:2] for call in self.command.call_args_list]
        self.test.assertEqual(effects.count(('agent', 'start')), 1)
        self.test.assertEqual(effects.count(('agent', 'prompt')), 1)


class InitialAdapter(ImplementationAdapter):
    def launch(self, execution):
        test = self.test
        state, _ = test.store.read()
        test.assertEqual((state['status'], state['pass_count'], state['review_count']), ('running', 1, 0))
        test.assertEqual(state['active_pass']['context_id'], getattr(test, 'expected_implementation', 'DEV-7-I1'))
        test.assertIsNotNone(state['active_pass']['pass_id'])
        test.assertIn('fresh conversation', execution.handoff)
        test.assertNotIn('Continue in YOUR existing', execution.handoff)
        test.launches.append(execution)
        pane = test.panes[0]
        pane.update(agent=execution.options.kind, agent_status='idle',
                    agent_session=dict(agent='pi', kind='path', value='/implementation.jsonl'))
        execution.runtime_observer(dict(herdr_session=json.dumps(pane['agent_session'])))
        # Session identity is retained before delivery can become uncertain.
        state, _ = test.store.read()
        test.assertEqual(state['implementation']['session']['conversation_id'], 'original-implementation')
        if test.launch_error:
            raise test.launch_error
        self.resume(execution, pane['agent_session'], recreate=False)
        return LaunchResult(execution.options.kind, pane['pane_id'], 'Initial task delivered',
                            '/implementation.jsonl', session_kind='path', resumability='yes')


class BootstrapLoopTests(unittest.TestCase):
    command = loop_fixture.LoopIntegrationTests.command
    run_loop = loop_fixture.LoopIntegrationTests.run_loop
    pause = loop_fixture.LoopIntegrationTests.pause
    continue_loop = loop_fixture.LoopIntegrationTests.continue_loop

    def pane_command(self, group, operation, *args):
        if (group, operation) == ('pane', 'process-info'):
            self.assertEqual(args[0], '--pane')
            pane_id = args[1]
            pid = {'p1': 123, 'p2': 124}[pane_id]
            return dict(process_info=dict(pane_id=pane_id, shell_pid=pid,
                foreground_process_group_id=pid, foreground_processes=[dict(pid=pid, argv=['/bin/bash'])]))
        return loop_fixture.LoopIntegrationTests.pane_command(self, group, operation, *args)

    def select_adapter(self, options):
        # Validate supported options even with controlled agent transport.
        adapter_for(options)
        self.options.append(options)
        return self.implementer if options.kind == 'pi' else self.adapter

    def setUp(self):
        loop_fixture.LoopIntegrationTests.setUp(self)
        # Use a clean registry, shell and untouched issue; the earlier fixture's
        # registry remains in its disposable directory and is never consulted.
        self.registry = ContextRegistry(self.repo.parent / 'initial-contexts.sqlite3')
        self.enterContext(patch('task_start.loop.ContextRegistry', return_value=self.registry))
        # Actual DEV-75 Herdr shell response: optional agent/session keys omitted,
        # status unknown. Only process evidence can establish an idle shell.
        self.panes[0].pop('agent', None)
        self.panes[0].pop('agent_session', None)
        self.panes[0].update(agent_status='unknown', label='Shell')
        self.proc = self.repo.parent / 'bootstrap-proc'
        for pid in (123, 124):
            entry = self.proc / str(pid)
            entry.mkdir(parents=True)
            (entry / 'exe').symlink_to('/bin/bash')
            (entry / 'cmdline').write_bytes(b'/bin/bash\0')
        self.enterContext(patch('task_start.contexts.verify_shell_process',
            side_effect=lambda pid, argv: verify_shell_process(pid, argv, proc=self.proc)))
        self.implementer = InitialAdapter(self)
        self.enterContext(patch('task_start.implementation_pass.adapter_for', side_effect=self.implementation_adapter))
        self.linear.get_issue.return_value = fixture.ISSUE
        self.launches, self.preparation = [], []
        self.launch_error = None
        self.base_error = self.workspace_error = False
        self.command(self.repo, 'worktree', 'remove', str(self.path))
        self.command(self.repo, 'branch', '-d', self.branch)
        self.branch = 'dev-7-add-ingestion-cli'
        self.enterContext(patch.object(Git, 'update_base', lambda git, branch: self.update_base(git, branch)))
        self.enterContext(patch.object(Git, 'remote_branches', REMOTE_BRANCHES))
        self.history = self.enterContext(patch.object(Git, 'check_history'))
        self.linear.start.side_effect = lambda _: self.preparation.append('linear')
        # LoopStore computes the private path only after Herdr creates the tree.
        self.store = None

    def implementation_adapter(self, options):
        adapter_for(options)
        return self.implementer

    def update_base(self, git, branch):
        self.preparation.append('base')
        if self.base_error:
            raise TaskError('Base preparation failed')
        UPDATE_BASE(git, branch)

    def worktrees(self, operation, *args):
        if operation == 'list':
            return loop_fixture.LoopIntegrationTests.worktrees(self, operation, *args)
        self.preparation.append(operation)
        if self.workspace_error:
            raise TaskError('Workspace preparation failed')
        if operation == 'create':
            self.assertEqual(args, ('--base', 'main', '--branch', self.branch, '--label', 'DEV-7', '--focus'))
            self.command(self.repo, 'worktree', 'add', '-b', self.branch, str(self.path), 'main')
        else:
            self.assertEqual(operation, 'open')
        self.store = LoopStore(self.path)
        tree = dict(path=str(self.path), branch=self.branch, is_linked_worktree=True,
                    is_bare=False, is_detached=False, is_prunable=False)
        return dict(worktree=tree,
                    workspace=dict(workspace_id='w1', focused=True,
                        worktree=dict(repo_root=str(self.repo), checkout_path=str(self.path))),
                    tab=dict(workspace_id='w1', tab_id='t1'),
                    root_pane=dict(workspace_id='w1', tab_id='t1', pane_id='p1'))

    def prepared_workspace(self, scope=None):
        self.command(self.repo, 'worktree', 'add', '-b', self.branch, str(self.path))
        self.git.save_scope(self.path, self.branch, 'DEV-7', scope)
        self.store = LoopStore(self.path)

    def pause_before_launch(self):
        original = LoopStore.begin
        def pause(store, state):
            store.pause()
            return original(store, state)
        with patch.object(LoopStore, 'begin', pause):
            result = self.run_loop()
        self.assertEqual(result.state, 'paused', result.render())
        self.assertEqual(result.data['next_phase'], 'initial_implementation')
        self.assertEqual((result.data['passes'], result.data['reviews']), (0, 0))
        self.assertEqual(self.launches, [])
        return result

    def fail_before_launch(self, **kwargs):
        read = Linear('test-placeholder').get_issue
        calls = 0
        def issue(identifier):
            nonlocal calls
            calls += 1
            return fixture.ISSUE if calls == 1 else read(identifier)
        with patch.object(self.linear, 'get_issue', side_effect=issue), \
                patch('task_start.linear.urlopen', side_effect=HTTPError(
                    'private-url', 503, 'private-body', {}, None)) as transport, \
                patch('task_start.linear.time.sleep'), patch('sys.stderr', new_callable=io.StringIO):
            result = self.run_loop(**kwargs)
        self.assertEqual(transport.call_count, 3)
        self.assertEqual(result.state, 'escalated', result.render())
        state, pause = self.store.read()
        self.assertFalse(pause)
        self.assertEqual((state['pass_count'], state['review_count'], state['active_pass']), (0, 0, None))
        self.assertEqual(state['next_phase'], 'initial_implementation')
        self.assertEqual(state['implementation'], dict(context_id='DEV-7-I1'))
        self.assertEqual(self.registry.get('DEV-7-I1')['state'], 'launching')
        self.assertIsNone(context_reference(self.registry.get('DEV-7-I1')))
        self.assertEqual(self.launches, [])
        return result

    def test_browser_from_scratch_prepares_implementation_and_reviewer_before_delivery(self):
        import browser_handoff_fixture
        calls = browser_handoff_fixture.install(self)
        result = self.run_loop(browser=True)
        self.assertEqual(result.state, 'clean', result.render())
        self.assertEqual([c[0] for c in calls], ['DEV-7-I1', 'DEV-7-R1'])
        self.assertTrue(self.store.read()[0]['browser'])
        self.assertIn('--instance DEV-7-I1 --actor DEV-7-I1 --connect-only', self.impl_prompts[0])
        self.assertIn('--instance DEV-7-R1 --actor DEV-7-R1 --connect-only', self.prompts[0])

    def test_dev75_shell_response_recovers_503_reservation_once_with_saved_settings(self):
        failed = self.fail_before_launch(max_reviews=2, max_passes=4, impl_model='saved-model')
        self.assertIn('task loop DEV-7', failed.render())
        before = self.store.read()[0]
        # Defaults/config changes must not override either role or saved bounds.
        self.local = replace(self.local, agent=None, reviewer=None)
        result = loop('DEV-7')
        self.assertEqual(result.state, 'clean', result.render())
        after = self.store.read()[0]
        for key in ('run_id', 'binding', 'requirements', 'implementation_options', 'reviewer_options',
                    'timeout', 'max_reviews', 'max_passes'):
            self.assertEqual(after[key], before[key], key)
        self.assertEqual(after['implementation']['context_id'], 'DEV-7-I1')
        self.assertEqual((len(self.launches), len(self.impl_prompts), len(self.prompts)), (1, 1, 1))
        self.assertEqual([r['phase'] for r in after['records']], ['initial_implementation', 'review'])
        self.assertEqual(self.preparation, ['base', 'create', 'linear'])
        self.linear.start.assert_called_once()
        self.assertEqual(loop('DEV-7').data, result.data)
        self.assertEqual(len(self.launches), 1)

    def test_recovery_optional_shell_metadata_always_requires_process_proof(self):
        self.fail_before_launch()
        before, context = self.store.read(), self.registry.get('DEV-7-I1')
        shell = dict(self.panes[0])
        del shell['agent_status']
        runtime = LoopRuntime('DEV-7', None, startup_recovery=True)
        for metadata in ({}, dict(agent_status=None), dict(agent_status='unknown'),
                         dict(agent=None), dict(agent_session=None),
                         dict(agent=None, agent_session=None, agent_status='idle'),
                         dict(agent=None, agent_session=None, agent_status='done')):
            for location in ({}, dict(workspace_id='w2', cwd=str(self.path)),
                             dict(workspace_id='w2', cwd=str(self.repo), foreground_cwd=str(self.path / 'src'))):
                with self.subTest(metadata=metadata, location=location):
                    self.panes[:] = [dict(shell, **metadata),
                                    dict(shell, pane_id='p2', terminal_id='term2', **metadata, **location)]
                    with patch.object(self.identities, 'command', side_effect=self.pane_command) as command:
                        runtime.prepare(before[0])
                    self.assertEqual([c.args for c in command.call_args_list],
                                     [('pane', 'process-info', '--pane', 'p1'),
                                      ('pane', 'process-info', '--pane', 'p2')])
                    for unverified in ('p1', 'p2'):
                        def observe(group, operation, *args):
                            if operation == 'process-info' and args[1] == unverified:
                                return dict(process_info={})
                            return self.pane_command(group, operation, *args)
                        with patch.object(self.identities, 'command', side_effect=observe), \
                                self.assertRaisesRegex(TaskError, f'Pane {unverified}.*unverified'):
                            runtime.prepare(before[0])
                    self.assertEqual(self.store.read(), before)
                    self.assertEqual(self.registry.get('DEV-7-I1'), context)
        self.assertEqual(self.launches, [])

    def test_startup_read_recovers_within_same_command_retry_budget(self):
        read = Linear('placeholder').get_issue
        calls = 0
        def issue(identifier):
            nonlocal calls
            calls += 1
            return read(identifier) if calls == 2 else fixture.ISSUE
        with patch.object(self.linear, 'get_issue', side_effect=issue), \
                patch('task_start.linear.urlopen', side_effect=[
                    HTTPError('private-url', 503, 'private-body', {}, None),
                    io.BytesIO(json.dumps({'data': fixture.baseline.issue_data()}).encode())]) as transport, \
                patch('task_start.linear.time.sleep'), patch('sys.stderr', new_callable=io.StringIO):
            result = self.run_loop()
        self.assertEqual(result.state, 'clean', result.render())
        self.assertEqual(transport.call_count, 2)
        self.assertEqual((len(self.launches), len(self.impl_prompts), len(self.prompts)), (1, 1, 1))

    def test_legacy_503_checkpoint_uses_evidence_not_diagnostic_wording(self):
        self.fail_before_launch()
        state, _ = self.store.read()
        state['reason'] = ('Linear HTTP error 503; check credentials/access or retry. '
                           'Inspect contexts before recovery; no handoff was retried')
        self.store.save(state)
        result = loop('DEV-7', impl_agent_kind='pi', impl_model='implementer', impl_mode='low',
                      agent_kind='codex', model='review-model', mode='high',
                      timeout=0.2, max_reviews=3, max_passes=6)
        self.assertEqual(result.state, 'clean', result.render())
        self.assertEqual(len(self.launches), 1)
        self.assertEqual(result.data['run_id'], state['run_id'])

    def test_persistent_or_invalid_linear_read_preserves_failed_reservation(self):
        self.fail_before_launch()
        before = self.store.read()
        context = self.registry.get('DEV-7-I1')
        for code, count in ((503, 3), (401, 1)):
            with self.subTest(code=code), patch.object(self.linear, 'get_issue',
                    side_effect=Linear('placeholder').get_issue), \
                    patch('task_start.linear.urlopen', side_effect=HTTPError(
                        'private-url', code, 'private-body', {}, None)) as transport, \
                    patch('task_start.linear.time.sleep'), patch('sys.stderr', new_callable=io.StringIO):
                with self.assertRaisesRegex(TaskError, 'reservation retained' if code == 503 else 'recovery blocked'):
                    loop('DEV-7')
                self.assertEqual(transport.call_count, count)
            self.assertEqual(self.store.read(), before)
            self.assertEqual(self.registry.get('DEV-7-I1'), context)
        self.assertEqual(self.launches, [])
        self.assertEqual(loop('DEV-7').state, 'clean')

    def test_recovery_rejects_conflicting_overrides_without_mutation(self):
        self.fail_before_launch()
        before, context = self.store.read(), self.registry.get('DEV-7-I1')
        for override in (dict(impl_agent_kind='codex'), dict(impl_model='other'), dict(impl_mode='high'),
                         dict(agent_kind='pi'), dict(model='other'), dict(mode='low'),
                         dict(timeout=12), dict(max_passes=4), dict(max_reviews=2), dict(from_review=True)):
            with self.subTest(override=override), self.assertRaisesRegex(TaskError, 'conflicts'):
                loop('DEV-7', **override)
            self.assertEqual(self.store.read(), before)
            self.assertEqual(self.registry.get('DEV-7-I1'), context)
        self.assertEqual(self.launches, [])

    def test_recovery_refuses_git_requirements_and_binding_drift(self):
        self.fail_before_launch()
        before = self.store.read()
        for changes in (dict(title='different'), dict(description='changed requirements'), dict(project='other')):
            with self.subTest(changes=changes), patch.object(self.linear, 'get_issue',
                    return_value=replace(fixture.ISSUE, **changes)), self.assertRaisesRegex(TaskError, 'recovery blocked'):
                loop('DEV-7')
            self.assertEqual(self.store.read(), before)
        # The binding must still match even when the changed checkpoint is valid JSON/schema.
        for key, value in (('repository', '/different/repo'), ('worktree', '/different/worktree'),
                           ('branch', 'other-branch'), ('base_branch', 'other-base'),
                           ('endpoint', '/other.sock'), ('workspace_id', 'other-workspace')):
            with self.subTest(binding=key):
                changed = copy.deepcopy(before[0])
                changed['binding'][key] = value
                if key == 'branch':
                    changed['snapshot']['branch'] = value
                self.store.save(changed)
                with self.assertRaises(TaskError):
                    loop('DEV-7')
                self.assertEqual(self.store.read()[0], changed)
                self.store.save(before[0])
        for edit in ('untracked', 'tracked', 'index', 'head', 'base'):
            with self.subTest(edit=edit):
                case = BootstrapLoopTests()
                case.setUp()
                try:
                    case.fail_before_launch()
                    checkpoint, context = case.store.read(), case.registry.get('DEV-7-I1')
                    if edit == 'untracked':
                        (case.path / 'unexpected').write_text('changed')
                    elif edit in ('tracked', 'index'):
                        tracked = case.command(case.path, 'ls-files').splitlines()[0]
                        (case.path / tracked).write_text('changed')
                        if edit == 'index':
                            case.command(case.path, 'add', tracked)
                    else:
                        case.command(case.repo if edit == 'base' else case.path,
                                     'commit', '--allow-empty', '-m', 'changed history')
                    with self.assertRaisesRegex(TaskError, 'Checkout/base changed'):
                        loop('DEV-7')
                    self.assertEqual(case.store.read(), checkpoint)
                    self.assertEqual(case.registry.get('DEV-7-I1'), context)
                    self.assertEqual(case.launches, [])
                finally:
                    case.doCleanups()

    def test_recovery_refuses_changed_or_ambiguous_reserved_runtime(self):
        self.fail_before_launch()
        before, original = self.store.read(), copy.deepcopy(self.panes)
        for change in (dict(terminal_id='replaced'), dict(pane_id='replaced'), dict(tab_id='moved'),
                       dict(workspace_id='moved'), dict(cwd=str(self.repo)),
                       dict(agent='pi'), dict(agent='codex'), dict(agent=False), dict(agent={}),
                       dict(agent_status='working'), dict(agent_status='blocked'), dict(agent_status={}),
                       dict(agent_session=False), dict(agent_session={}),
                       dict(agent_session=dict(agent='pi', kind='path', value='/existing.jsonl')),
                       dict(agent_session=dict(agent='codex', kind='id', value='conflicting-session'))):
            with self.subTest(change=change):
                self.panes = [dict(original[0], **change)]
                with self.assertRaisesRegex(TaskError, 'recovery blocked'):
                    loop('DEV-7')
                self.assertEqual(self.store.read(), before)
        for panes in ([], [original[0], dict(original[0], pane_id='p2')],
                      [original[0], dict(original[0], pane_id='p2', terminal_id='t2', agent='codex')]):
            self.panes = panes
            with self.subTest(panes=panes), self.assertRaisesRegex(TaskError, 'recovery blocked'):
                loop('DEV-7')
            self.assertEqual(self.store.read(), before)
        self.panes = original
        for info in ({}, dict(pane_id='other', shell_pid=123, foreground_process_group_id=123,
                             foreground_processes=[dict(pid=123)]),
                     dict(pane_id='p1', shell_pid=123, foreground_process_group_id=456,
                          foreground_processes=[dict(pid=456)])):
            with patch.object(self.identities, 'command', return_value=dict(process_info=info)), \
                    self.assertRaisesRegex(TaskError, 'busy or its process identity is unverified'):
                loop('DEV-7')
        self.assertEqual(self.store.read(), before)
        self.assertEqual(self.launches, [])

    def test_recovery_refuses_changed_reserved_context_and_foreign_claim(self):
        self.fail_before_launch()
        before, original = self.store.read(), self.registry.get('DEV-7-I1')
        changes = (dict(state='uncertain'), dict(state='active'), dict(state='awaiting_user'),
                   dict(session_id='/session.jsonl', session_kind='path'), dict(session_kind='path'),
                   dict(herdr_session='{}'), dict(resumability='yes'), dict(model='other'), dict(mode='high'),
                   dict(worktree=str(self.repo)), dict(endpoint='/other.sock'), dict(role='review'))
        for change in changes:
            with self.subTest(change=change):
                with self.registry.connection(write=True) as db:
                    db.execute('UPDATE contexts SET ' + ', '.join(k + '=?' for k in change), tuple(change.values()))
                with self.assertRaises(TaskError):
                    loop('DEV-7')
                self.assertEqual(self.store.read(), before)
                with self.registry.connection(write=True) as db:
                    db.execute('UPDATE contexts SET ' + ', '.join(k + '=?' for k in change),
                               tuple(original[k] for k in change))
        # Another issue can also claim this terminal/workspace; labels cannot exempt it.
        self.registry.allocate('DEV-8', 'review', agent='pi', repository=str(self.repo),
            worktree=str(self.path), endpoint='/server.sock', workspace_id='w1', terminal_id='term1')
        with self.assertRaisesRegex(TaskError, 'Another workflow context conflicts'):
            loop('DEV-7')
        self.assertEqual(self.store.read(), before)
        self.assertEqual(self.launches, [])

    def test_simultaneous_retries_launch_only_once(self):
        self.fail_before_launch()
        before = self.store.read()
        entered, release = threading.Event(), threading.Event()
        prepare = LoopRuntime.prepare
        def held(runtime, state):
            entered.set()
            if not release.wait(10):
                raise AssertionError('Retry test was not released')
            return prepare(runtime, state)
        with patch.object(LoopRuntime, 'prepare', held), ThreadPoolExecutor(max_workers=1) as executor:
            first = executor.submit(loop, 'DEV-7')
            try:
                self.assertTrue(entered.wait(10))
                with self.assertRaisesRegex(TaskError, 'Another controller'):
                    loop('DEV-7')
                self.assertEqual(self.store.read(), before)
            finally:
                release.set()
            result = first.result(timeout=20)
        self.assertEqual(result.state, 'clean', result.render())
        self.assertEqual((len(self.launches), len(self.impl_prompts), len(self.prompts)), (1, 1, 1))

    def test_restart_compare_and_swap_preserves_new_checkpoint_evidence(self):
        self.fail_before_launch()
        prepare = LoopRuntime.prepare
        changed = copy.deepcopy(self.store.read()[0])
        changed['reason'] = 'Changed by another observer'
        def change(runtime, state):
            prepare(runtime, state)
            self.store.save(changed)
        with patch.object(LoopRuntime, 'prepare', change), self.assertRaisesRegex(TaskError, 'checkpoint changed'):
            loop('DEV-7')
        self.assertEqual(self.store.read()[0], changed)
        self.assertEqual(self.launches, [])

    def test_recovery_keeps_sticky_pause_before_handoff(self):
        self.fail_before_launch()
        self.assertTrue(self.pause().data['pause_requested'])
        with self.assertRaisesRegex(TaskError, 'Only a paused'):
            self.continue_loop()
        result = loop('DEV-7')
        self.assertEqual(result.state, 'paused', result.render())
        self.assertEqual(self.launches, [])
        self.assertTrue(self.store.read()[1])
        self.assertEqual(self.continue_loop().state, 'clean')

    def pause_recovered_initial(self):
        self.fail_before_launch()
        self.assertTrue(self.pause().data['pause_requested'])
        result = loop('DEV-7')
        self.assertEqual(result.state, 'paused', result.render())
        self.assertEqual((result.data['passes'], result.data['active_pass']), (0, None))
        self.assertEqual(self.launches, [])

    def test_recovery_continue_rejects_exec_replaced_shell_after_pause(self):
        self.pause_recovered_initial()
        context = self.registry.get('DEV-7-I1')
        # The old proof had matching PID and argv; only the executable changes.
        (self.proc / '123' / 'exe').unlink()
        (self.proc / '123' / 'exe').symlink_to('/usr/bin/sleep')
        result = self.continue_loop()
        self.assertEqual(result.state, 'escalated', result.render())
        self.assertIn('Shell executable/argv', result.data['reason'])
        self.assertEqual((result.data['passes'], result.data['active_pass']), (0, None))
        self.assertEqual(self.registry.get('DEV-7-I1'), context)
        self.assertEqual(self.launches, [])

    def test_recovery_continue_rejects_competing_session_after_pause(self):
        self.pause_recovered_initial()
        context = self.registry.get('DEV-7-I1')
        self.panes.append(dict(self.panes[0], pane_id='p2', terminal_id='term2', agent=None,
            agent_status='working', agent_session=dict(agent='codex', kind='id', value='other-provider')))
        self.next_pane_number = 3
        result = self.continue_loop()
        self.assertEqual(result.state, 'escalated', result.render())
        self.assertIn('Pane p2', result.data['reason'])
        self.assertEqual((result.data['passes'], result.data['active_pass']), (0, None))
        self.assertEqual(self.registry.get('DEV-7-I1'), context)
        self.assertEqual(self.launches, [])

    def test_recovery_continue_rechecks_runtime_and_base_before_claim(self):
        for fault in ('shell', 'competing_session', 'base'):
            with self.subTest(fault=fault):
                case = BootstrapLoopTests()
                case.setUp()
                try:
                    case.pause_recovered_initial()
                    context = case.registry.get('DEV-7-I1')
                    observations = 0
                    original = case.pane_command
                    def observe(group, operation, *args):
                        nonlocal observations
                        result = original(group, operation, *args)
                        if operation == 'process-info':
                            observations += 1
                            if observations == 2 and fault == 'base':
                                case.command(case.repo, 'commit', '--allow-empty', '-m', 'Base moved during continuation')
                        return result
                    execute = LoopRuntime.execute
                    def changed(runtime, state, *args):
                        # After prepare, before the handoff's final claim checks.
                        if fault == 'shell':
                            (case.proc / '123' / 'exe').unlink()
                            (case.proc / '123' / 'exe').symlink_to('/usr/bin/sleep')
                        elif fault == 'competing_session':
                            case.panes.append(dict(case.panes[0], pane_id='p2', terminal_id='term2', agent=None,
                                agent_status='working', agent_session=dict(agent='codex', kind='id', value='competing')))
                            case.next_pane_number = 3
                        return execute(runtime, state, *args)
                    with patch.object(case.identities, 'command', side_effect=observe), \
                            patch.object(LoopRuntime, 'execute', changed):
                        result = case.continue_loop()
                    self.assertEqual(result.state, 'escalated', result.render())
                    expected = {'shell': 'Shell executable/argv', 'competing_session': 'Pane p2',
                                'base': 'Base changed before the handoff claim'}[fault]
                    self.assertIn(expected, result.data['reason'])
                    self.assertEqual((result.data['passes'], result.data['active_pass']), (0, None))
                    self.assertEqual(case.registry.get('DEV-7-I1'), context)
                    self.assertEqual(case.launches, [])
                finally:
                    case.doCleanups()

    def test_recovery_continue_reproves_each_pause_and_keeps_original_reservation(self):
        self.pause_recovered_initial()
        before = self.store.read()[0]
        begin = LoopStore.begin
        def paused(store, state):
            self.assertTrue(self.pause().data['pause_requested'])
            return begin(store, state)
        with patch.object(LoopStore, 'begin', paused):
            result = self.continue_loop()
        self.assertEqual(result.state, 'paused', result.render())
        self.assertEqual(self.launches, [])
        with patch.object(self.identities, 'command', side_effect=self.pane_command) as command:
            result = self.continue_loop()
        self.assertEqual(result.state, 'clean', result.render())
        self.assertEqual(sum(c.args[:2] == ('pane', 'process-info') for c in command.call_args_list), 2)
        after = self.store.read()[0]
        for key in ('run_id', 'binding', 'requirements', 'snapshot', 'implementation_options',
                    'reviewer_options', 'timeout', 'max_reviews', 'max_passes'):
            self.assertEqual(after[key], before[key], key)
        self.assertEqual(after['implementation']['context_id'], 'DEV-7-I1')
        self.assertEqual([r['phase'] for r in after['records']], ['initial_implementation', 'review'])
        self.assertEqual((len(self.launches), len(self.impl_prompts), len(self.prompts)), (1, 1, 1))
        self.linear.start.assert_called_once()

    def test_recovery_keeps_pause_requested_during_first_preflight(self):
        self.fail_before_launch()
        before = self.store.read()[0]
        entered, release = threading.Event(), threading.Event()
        prepare = LoopRuntime.prepare
        def held(runtime, state):
            entered.set()
            if not release.wait(10):
                raise AssertionError('Recovery preflight was not released')
            return prepare(runtime, state)
        with patch.object(LoopRuntime, 'prepare', held), ThreadPoolExecutor(max_workers=1) as executor:
            retry = executor.submit(loop, 'DEV-7')
            try:
                self.assertTrue(entered.wait(10))
                paused = self.pause()
                self.assertEqual(paused.state, 'escalated')
                self.assertTrue(paused.data['pause_requested'])
                self.assertEqual(self.store.read(), (before, True))
            finally:
                release.set()
            result = retry.result(timeout=20)
        self.assertEqual(result.state, 'paused', result.render())
        self.assertTrue(result.data['pause_requested'])
        self.assertEqual((result.data['passes'], result.data['active_pass']), (0, None))
        self.assertEqual(self.launches, [])
        self.assertEqual(self.continue_loop().state, 'clean')

    def test_recovery_rejects_exec_replaced_shell_with_same_pid(self):
        self.fail_before_launch()
        before, context = self.store.read(), self.registry.get('DEV-7-I1')
        info = dict(pane_id='p1', shell_pid=123, foreground_process_group_id=123,
                    foreground_processes=[dict(pid=123, argv=['/usr/bin/sleep', '600'])])
        with patch.object(self.identities, 'command', return_value=dict(process_info=info)), \
                self.assertRaisesRegex(TaskError, 'shell'):
            loop('DEV-7')
        self.assertEqual(self.store.read(), before)
        self.assertEqual(self.registry.get('DEV-7-I1'), context)
        self.assertEqual(self.launches, [])

    def test_recovery_requires_shell_argv_and_matching_local_executable(self):
        self.fail_before_launch()
        before = self.store.read()
        for process in (dict(pid=123), dict(pid=123, argv=None), dict(pid=123, argv=[]),
                        dict(pid=123, argv='bash'), dict(pid=123, argv=['/tmp/bash']),
                        dict(pid=123, argv=['bash', '-c', 'sleep 600']),
                        dict(pid=123, argv=['bash', 'script.sh'])):
            with self.subTest(process=process):
                info = dict(pane_id='p1', shell_pid=123, foreground_process_group_id=123,
                            foreground_processes=[process])
                with patch.object(self.identities, 'command', return_value=dict(process_info=info)), \
                        self.assertRaises(TaskError):
                    loop('DEV-7')
                self.assertEqual(self.store.read(), before)
        entry = self.proc / '123'
        for fault in ('exec_replaced', 'stale_argv', 'missing_exe', 'missing_cmdline'):
            with self.subTest(fault=fault):
                if fault == 'exec_replaced':
                    # Even spoofed/stale Herdr argv=['/bin/bash'] is insufficient.
                    (entry / 'exe').unlink()
                    (entry / 'exe').symlink_to('/usr/bin/sleep')
                elif fault == 'stale_argv':
                    (entry / 'cmdline').write_bytes(b'bash\0-c\0sleep 600\0')
                else:
                    (entry / fault.removeprefix('missing_')).unlink()
                with self.assertRaisesRegex(TaskError, 'Shell executable/argv'):
                    loop('DEV-7')
                self.assertEqual(self.store.read(), before)
                if not (entry / 'exe').is_symlink():
                    (entry / 'exe').symlink_to('/bin/bash')
                elif fault == 'exec_replaced':
                    (entry / 'exe').unlink()
                    (entry / 'exe').symlink_to('/bin/bash')
                (entry / 'cmdline').write_bytes(b'/bin/bash\0')
        self.assertEqual(self.launches, [])

    def test_recovery_rejects_competing_session_when_other_pane_has_no_agent(self):
        self.fail_before_launch()
        before, context = self.store.read(), self.registry.get('DEV-7-I1')
        self.panes.append(dict(self.panes[0], pane_id='p2', terminal_id='term2', agent=None,
            agent_status='working', agent_session=dict(agent='codex', kind='id', value='other-provider')))
        with self.assertRaisesRegex(TaskError, 'runtime evidence'):
            loop('DEV-7')
        self.assertEqual(self.store.read(), before)
        self.assertEqual(self.registry.get('DEV-7-I1'), context)
        self.assertEqual(self.launches, [])

    def test_recovery_refuses_competing_pane_appearing_before_claim(self):
        self.fail_before_launch()
        execute = LoopRuntime.execute
        def competing(runtime, state, *args):
            self.panes.append(dict(self.panes[0], pane_id='p2', terminal_id='term2',
                agent_status='working', agent_session=dict(agent='codex', kind='id', value='competing')))
            return execute(runtime, state, *args)
        with patch.object(LoopRuntime, 'execute', competing):
            result = loop('DEV-7')
        self.assertEqual(result.state, 'escalated', result.render())
        self.assertIn('Pane p2', result.data['reason'])
        self.assertEqual((result.data['passes'], result.data['active_pass']), (0, None))
        self.assertEqual(self.launches, [])

    def test_recovery_refuses_exec_replacement_during_final_shell_observation(self):
        self.fail_before_launch()
        original = self.pane_command
        observations = 0
        def replaced(group, operation, *args):
            nonlocal observations
            result = original(group, operation, *args)
            if operation == 'process-info':
                observations += 1
                if observations == 3:
                    # The PID and even reported argv can remain unchanged/stale.
                    (self.proc / '123' / 'exe').unlink()
                    (self.proc / '123' / 'exe').symlink_to('/usr/bin/sleep')
            return result
        with patch.object(self.identities, 'command', side_effect=replaced):
            result = loop('DEV-7')
        self.assertEqual(result.state, 'escalated', result.render())
        self.assertIn('Shell executable/argv', result.data['reason'])
        self.assertEqual((result.data['passes'], result.data['active_pass']), (0, None))
        self.assertEqual(self.launches, [])

    def test_recovery_checks_absence_evidence_in_all_related_panes(self):
        self.fail_before_launch()
        before = self.store.read()
        other = dict(self.panes[0], pane_id='p2', terminal_id='term2')
        for change in (dict(agent_status='working'), dict(agent_status='blocked'), dict(agent_status={}),
                       dict(agent='pi'), dict(agent='codex'), dict(agent=False), dict(agent={}),
                       dict(agent_session=dict(agent='pi', kind='path', value='/other.jsonl')),
                       dict(agent_session=dict(agent='codex', kind='id', value='other-provider')),
                       dict(agent_session=False), dict(agent_session={})):
            for location in ({}, dict(workspace_id='w2', cwd=str(self.repo), foreground_cwd=str(self.path / 'src'))):
                with self.subTest(change=change, location=location):
                    self.panes[:] = [self.panes[0], dict(other, **change, **location)]
                    with self.assertRaisesRegex(TaskError, 'recovery blocked'):
                        loop('DEV-7')
                    self.assertEqual(self.store.read(), before)
        # A busy unrecognized process in another related pane is also unsafe.
        self.panes[:] = [self.panes[0], other]
        (self.proc / '124' / 'exe').unlink()
        (self.proc / '124' / 'exe').symlink_to('/usr/bin/sleep')
        with self.assertRaisesRegex(TaskError, 'Shell executable/argv'):
            loop('DEV-7')
        self.assertEqual(self.store.read(), before)
        self.assertEqual(self.launches, [])

    def test_recovery_allows_proven_idle_shells_and_ignores_unrelated_workspaces(self):
        self.fail_before_launch()
        self.panes.extend([
            dict(self.panes[0], pane_id='p2', terminal_id='term2'),
            dict(self.panes[0], pane_id='p3', terminal_id='term3', workspace_id='w3',
                 cwd=str(self.repo), agent='codex', agent_status='working',
                 agent_session=dict(agent='codex', kind='id', value='unrelated-session')),
        ])
        # The review fake allocates its next pane after these preexisting shells.
        self.next_pane_number = 4
        result = loop('DEV-7')
        self.assertEqual(result.state, 'clean', result.render())
        self.assertEqual(len(self.launches), 1)

    def test_recovery_pause_survives_another_preflight_failure(self):
        self.fail_before_launch()
        before = self.store.read()[0]
        def paused_failure(runtime, state):
            self.assertTrue(self.pause().data['pause_requested'])
            raise TaskError('Temporary preflight failure')
        with patch.object(LoopRuntime, 'prepare', paused_failure), self.assertRaisesRegex(TaskError, 'preflight failure'):
            loop('DEV-7')
        self.assertEqual(self.store.read(), (before, True))
        self.assertEqual(loop('DEV-7').state, 'paused')
        self.assertEqual(self.launches, [])

    def test_pause_does_not_mark_unrecoverable_escalations(self):
        self.fail_before_launch()
        before = self.store.read()[0]
        for change in (dict(status='interrupted'), dict(findings=[dict(id='F1')]),
                       dict(pass_count=1, active_pass=dict(phase='initial_implementation',
                                                           context_id='DEV-7-I1', pass_id=None))):
            changed = dict(before, **change)
            self.store.save(changed)
            with self.subTest(change=change):
                self.assertFalse(self.pause().data['pause_requested'])
                self.assertEqual(self.store.read(), (changed, False))
        self.assertEqual(self.launches, [])

    def test_recovery_never_restarts_other_states_or_unfinished_claims(self):
        self.fail_before_launch()
        original = self.store.read()[0]
        for status in ('ready', 'interrupted', 'paused', 'running'):
            changed = dict(original, status=status)
            if status == 'running':
                changed.update(pass_count=1, active_pass=dict(phase='initial_implementation',
                                                             context_id=None, pass_id=None))
            self.store.save(changed)
            with self.subTest(status=status), self.assertRaisesRegex(TaskError, 'startup recovery blocked|no durable delivery proof'):
                loop('DEV-7')
            self.assertEqual(self.store.read()[0], changed)
        self.assertEqual(self.launches, [])

    def test_missing_reservation_never_allocates_a_replacement(self):
        self.fail_before_launch()
        before, preparation = self.store.read(), list(self.preparation)
        # Simulate lost registry evidence only in this disposable fixture.
        with self.registry.connection(write=True) as db:
            db.execute('DELETE FROM contexts')
        with self.assertRaisesRegex(TaskError, 'original implementation reservation is missing'):
            loop('DEV-7')
        self.assertEqual(self.registry.list(), [])
        self.assertEqual(self.store.read(), before)
        self.assertEqual(self.preparation, preparation)
        self.assertEqual(self.launches, [])

    def test_recovery_rechecks_busy_shell_immediately_before_claim(self):
        self.fail_before_launch()
        original = self.pane_command
        observations = 0
        def observe(group, operation, *args):
            nonlocal observations
            result = original(group, operation, *args)
            if operation == 'process-info':
                observations += 1
                if observations == 3:  # Validation, normal prepare, then handoff boundary.
                    result['process_info']['foreground_processes'].append(dict(pid=456))
            return result
        with patch.object(self.identities, 'command', side_effect=observe):
            result = loop('DEV-7')
        self.assertEqual(result.state, 'escalated', result.render())
        self.assertIn('busy', result.data['reason'])
        state, _ = self.store.read()
        self.assertEqual((state['pass_count'], state['active_pass']), (0, None))
        self.assertEqual(self.launches, [])

    def test_recovery_rechecks_git_after_shell_observation_before_claim(self):
        self.fail_before_launch()
        original = self.pane_command
        observations = 0
        def observe(group, operation, *args):
            nonlocal observations
            result = original(group, operation, *args)
            if operation == 'process-info':
                observations += 1
                if observations == 3:
                    (self.path / 'external-change').write_text('Changed during slow shell preflight')
            return result
        with patch.object(self.identities, 'command', side_effect=observe):
            result = loop('DEV-7')
        self.assertEqual(result.state, 'escalated', result.render())
        self.assertIn('Checkout changed before the handoff claim', result.data['reason'])
        state, _ = self.store.read()
        self.assertEqual((state['pass_count'], state['active_pass']), (0, None))
        self.assertEqual(self.launches, [])

    def test_recovery_rechecks_base_after_shell_observation_before_claim(self):
        self.fail_before_launch()
        original = self.pane_command
        observations = 0
        def observe(group, operation, *args):
            nonlocal observations
            result = original(group, operation, *args)
            if operation == 'process-info':
                observations += 1
                if observations == 3:
                    self.command(self.repo, 'commit', '--allow-empty', '-m', 'Base advanced during shell preflight')
            return result
        with patch.object(self.identities, 'command', side_effect=observe):
            result = loop('DEV-7')
        self.assertEqual(result.state, 'escalated', result.render())
        state, _ = self.store.read()
        self.assertEqual((state['pass_count'], state['active_pass']), (0, None))
        self.assertIn('Base changed before the handoff claim', result.data['reason'])
        self.assertEqual(self.launches, [])

    def test_recovery_refuses_changed_initial_slice_metadata(self):
        self.fail_before_launch()
        before = self.store.read()
        scope = self.git.scope_file(self.path)
        changed = json.loads(scope.read_text())
        changed['slice'] = 'add-ingestion-cli'
        scope.write_text(json.dumps(changed))
        with self.assertRaisesRegex(TaskError, 'unsliced'):
            loop('DEV-7')
        self.assertEqual(self.store.read(), before)
        self.assertEqual(self.launches, [])

    def test_untouched_issue_initial_handoff_then_fresh_review(self):
        self.local = replace(self.local, issue_structure=IssueStructureConfig('required'))
        result = self.run_loop()
        self.assertEqual(result.state, 'clean', result.render())
        self.assertEqual(self.preparation, ['base', 'create', 'linear'])
        self.assertEqual((len(self.launches), len(self.impl_prompts), len(self.prompts)), (1, 1, 1))
        self.assertEqual((result.data['passes'], result.data['reviews']), (2, 1))
        self.assertEqual((result.data['implementation_context'], result.data['reviewer_context']), ('DEV-7-I1', 'DEV-7-R1'))
        self.assertEqual(self.launches[0].options, AgentOptions('pi', 'implementer', 'low'))
        self.assertIn(fixture.ISSUE.description, self.impl_prompts[0])
        self.assertIn('fresh independent review', self.prompts[0])
        self.assertNotIn('Implementation completed and validated', self.prompts[0])
        state, _ = self.store.read()
        self.assertEqual(state['version'], 3)
        self.assertEqual([r['phase'] for r in state['records']], ['initial_implementation', 'review'])
        self.assertEqual(self.command(self.path, 'rev-parse', 'HEAD'), self.before)
        self.assertNotIn(fixture.ISSUE.description, self.store.path.read_bytes().decode(errors='ignore'))
        metrics = MetricsStore(self.registry.path).read()
        self.assertEqual([r['role'] for r in metrics], ['implementation', 'review'])
        self.assertEqual([r['run_id'] for r in metrics], [r['pass_id'] for r in state['records']])
        self.assertEqual((metrics[0]['agent'], metrics[0]['model'], metrics[0]['mode']), ('pi', 'implementer', 'low'))

    def assert_structure_handoff(self, description, *, warning):
        issue = replace(fixture.ISSUE, description=description)
        self.linear.get_issue.return_value = issue
        with patch('sys.stderr', new_callable=io.StringIO) as stderr:
            result = self.run_loop()
        self.assertEqual(result.state, 'clean', result.render())
        self.assertEqual(self.preparation, ['base', 'create', 'linear'])
        if warning:
            self.assertIn('warning: Linear issue DEV-7', stderr.getvalue())
            self.assertIn(repr(self.local.issue_structure.block_name), stderr.getvalue())
            self.assertIn('Continuing agent execution', stderr.getvalue())
            self.assertEqual(stderr.getvalue().count('warning:'), 1)
        else:
            self.assertEqual(stderr.getvalue(), '')
        execution = self.launches[0]
        shared_handoff = implementation_handoff(issue, execution.workspace)
        self.assertTrue(execution.handoff.startswith(shared_handoff))
        self.assertIn(f'Task:\n{description}\n\nWorkflow-owned implementation instructions:', shared_handoff)
        self.assertIn('Users may organize the issue description however they choose', shared_handoff)
        self.assertEqual(execution.issue.description, description)

    def test_default_warn_accepts_ordinary_issue_and_preserves_full_description(self):
        self.assert_structure_handoff('Build ingestion.\r\n  Constraints α anywhere.\r\n', warning=True)

    def test_ignore_accepts_empty_issue_without_inspecting_structure(self):
        self.local = replace(self.local, issue_structure=IssueStructureConfig('ignore'))
        self.assert_structure_handoff('', warning=False)

    def test_custom_required_block_preserves_complete_description(self):
        self.local = replace(self.local, issue_structure=IssueStructureConfig('required', 'Build notes (α)'))
        self.assert_structure_handoff('Requirements first.\r\n>>> Build notes (α) \t\r\nGuidance\r\n>>>\r\nMore constraints.',
                                      warning=False)

    def test_custom_warn_does_not_treat_default_block_as_configured_block(self):
        self.local = replace(self.local, issue_structure=IssueStructureConfig('warn', 'Build notes'))
        self.assert_structure_handoff(fixture.ISSUE.description, warning=True)

    def test_invalid_structure_configuration_fails_before_preparation(self):
        with patch('task_start.config.read_toml', return_value=dict(projects_root='/projects',
                linear=dict(api_key='test-placeholder', issue_structure=dict(mode='ignore', block_name='')))), \
                patch('task_start.loop.load_local', side_effect=load_local):
            with self.assertRaisesRegex(TaskError, 'linear.issue_structure.block_name'):
                self.run_loop()
        self.linear.get_issue.assert_not_called()
        self.assertEqual(self.preparation, [])
        self.linear.start.assert_not_called()
        self.assertEqual(self.registry.list(), [])
        self.assertFalse(self.path.exists())

    def test_invalid_validation_configuration_fails_before_preparation(self):
        with patch('task_start.config.read_toml', return_value=dict(projects_root='/projects',
                linear=dict(api_key='test-placeholder'), review=dict(validation=dict(strategy='invalid')))), \
                patch('task_start.loop.load_local', side_effect=load_local):
            with self.assertRaisesRegex(TaskError, 'review.validation.strategy'):
                self.run_loop()
        self.linear.get_issue.assert_not_called()
        self.assertEqual(self.preparation, [])
        self.linear.start.assert_not_called()
        self.assertEqual(self.registry.list(), [])
        self.assertFalse(self.path.exists())

    def codex_transport(self):
        self.local = replace(self.local, agent=fixture.AgentConfig('codex', 'initial-codex', 'high'))
        self.enterContext(patch('task_start.implementation_pass.adapter_for', side_effect=Codex))
        transport = CodexStartupTransport(self, lambda: self.panes)
        self.enterContext(patch('task_start.implementation_pass.time',
                                SimpleNamespace(monotonic=lambda: transport.now, sleep=transport.advance)))
        return transport

    def test_pi_path_only_confirmation_after_verification_reaches_fresh_review(self):
        transport = PiInitialTransport(self, history_at='prompt')
        result = loop('DEV-7', timeout=2)
        self.assertEqual(result.state, 'clean', result.render())
        transport.assert_identity_retained()
        self.assertGreaterEqual(transport.polls, 3)
        self.assertEqual((result.data['passes'], result.data['reviews']), (2, 1))
        self.assertEqual((len(self.impl_prompts), len(self.prompts)), (1, 1))
        self.assertIn('fresh independent review', self.prompts[0])

    def test_pi_path_only_polls_after_delayed_verification_reach_fresh_review(self):
        transport = PiInitialTransport(self, history_at='poll')
        result = loop('DEV-7', timeout=2)
        self.assertEqual(result.state, 'clean', result.render())
        transport.assert_identity_retained()
        self.assertGreaterEqual(transport.polls, 3)
        self.assertEqual((result.data['passes'], result.data['reviews']), (2, 1))
        self.assertEqual((len(self.impl_prompts), len(self.prompts)), (1, 1))
        self.assertIn('fresh independent review', self.prompts[0])

    def test_pi_uncertain_prompt_confirmation_keeps_verified_identity_without_replay(self):
        transport = PiInitialTransport(self, history_at='prompt')
        def uncertain():
            if transport.gets == 2:
                raise TaskError('Confirmation unavailable')
        transport.on_get = uncertain
        result = loop('DEV-7', timeout=2)
        self.assertEqual(result.state, 'escalated', result.render())
        self.assertIn('Confirmation unavailable', result.data['reason'])
        self.assertEqual(self.registry.get('DEV-7-I1')['state'], 'uncertain')
        self.assertEqual(self.prompts, [])
        self.assertEqual(loop('DEV-7').state, 'clean')
        with self.assertRaises(TaskError):
            loop('DEV-7', action='continue')
        transport.assert_identity_retained()

    def test_pi_interrupt_in_submission_uses_pinned_header_before_registry_enrichment(self):
        transport = PiInitialTransport(self, history_at='prompt')
        def interrupt(group, operation, *args):
            result = transport.herdr(group, operation, *args)
            if (group, operation) == ('agent', 'prompt'):
                raise KeyboardInterrupt()
            return result
        transport.command.side_effect = interrupt
        first = loop('DEV-7', timeout=2)
        self.assertEqual(first.state, 'interrupted', first.render())
        self.assertNotIn('conversation_id', context_reference(self.registry.get('DEV-7-I1')))
        self.assertEqual(self.store.read()[0]['delivery']['context']['session']['conversation_id'],
                         'original-implementation')
        transport.command.side_effect = transport.herdr
        result = loop('DEV-7')
        self.assertEqual(result.state, 'clean', result.render())
        self.assertEqual((len(self.impl_prompts), len(self.prompts)), (1, 1))
        transport.assert_identity_retained()

    def test_pi_replaced_history_after_path_only_polls_refuses_review(self):
        transport = PiInitialTransport(self, history_at='poll')
        def replace_history():
            if transport.polls == 3:
                transport.history('replacement')
        transport.on_get = replace_history
        result = loop('DEV-7', timeout=2)
        self.assertEqual(result.state, 'escalated', result.render())
        self.assertIn('matching conversation identity', result.data['reason'])
        self.assertEqual(self.prompts, [])
        transport.assert_identity_retained()

    def test_pi_conflicting_runtime_after_verification_preserves_original_binding(self):
        for changes in (dict(terminal_id='replacement'),
                        dict(agent_session=dict(agent='pi', kind='path', value='/replacement.jsonl')),
                        dict(conversation_id='replacement')):
            with self.subTest(changes=changes):
                case = BootstrapLoopTests()
                case.setUp()
                try:
                    transport = PiInitialTransport(case, history_at='poll')
                    def conflict():
                        if transport.polls == 2:
                            if 'conversation_id' in changes:
                                case.panes[0]['agent_session'].update(changes)
                            else:
                                case.panes[0].update(changes)
                    transport.on_get = conflict
                    result = loop('DEV-7', timeout=2)
                    self.assertEqual(result.state, 'escalated', result.render())
                    self.assertEqual(case.prompts, [])
                    transport.assert_identity_retained()
                finally:
                    case.doCleanups()

    def test_codex_initial_structured_task_is_queued_once_before_fresh_review(self):
        transport = self.codex_transport()
        def deliver(prompt):
            self.impl_prompts.append(prompt)
            state, _ = self.store.read()
            self.assertEqual(state['status'], 'running')
            self.assertEqual(state['implementation']['session']['value'], transport.thread_id)
            output = Path(re.search(r'Write your result to (.*?)\. This temporary', prompt).group(1))
            pass_id = re.search(r'Pass ID: ([^\n]+)', prompt).group(1)
            (self.path / 'implemented.txt').write_text('initial implementation')
            output.write_text(json.dumps(dict(pass_id=pass_id, state='completed', summary='Implemented',
                                              checks=[], resolutions=[], task_assessment=dict(
                                                  state='ready', summary='Controlled readiness outcome', questions=[]))))
        transport.on_queue = deliver
        result = self.run_loop()
        self.assertEqual(result.state, 'clean', result.render())
        self.assertEqual((len(self.impl_prompts), len(self.prompts)), (1, 1))
        self.assertIn('fresh conversation', self.impl_prompts[0])
        transport.assert_effects(1, 1)
        self.assertEqual(self.store.read()[0]['implementation']['session']['value'], transport.thread_id)

    def test_initial_warning_starts_after_bounded_startup_and_keeps_waiting(self):
        transport = self.codex_transport()
        polls = []
        def wait(seconds):
            polls.append(transport.now)
            if transport.now >= transport.queued_at + transport.receipt_delay + 1:
                raise KeyboardInterrupt()
            transport.advance(seconds)
        with patch('task_start.pass_delivery.time', SimpleNamespace(time=lambda: 2000000000 + transport.now)), \
                patch('task_start.implementation_pass.time',
                      SimpleNamespace(monotonic=lambda: transport.now, sleep=wait)), \
                patch('sys.stderr', new_callable=io.StringIO) as warning:
            result = loop('DEV-7', timeout=0.5)
        self.assertEqual(result.state, 'interrupted', result.render())
        self.assertEqual(polls[0], transport.queued_at + transport.receipt_delay)
        self.assertEqual(transport.now, transport.queued_at + transport.receipt_delay + 1)
        self.assertEqual(warning.getvalue().count('warning threshold'), 1)
        self.assertEqual(self.store.read()[0]['delivery']['deadline'], 2000000000 + polls[0] + 0.5)
        self.assertEqual((result.data['passes'], result.data['reviews']), (1, 0))
        transport.assert_effects(1, 1)
        self.assertEqual(self.prompts, [])

    def test_initial_codex_trust_wait_retains_claim_and_continues_same_pass(self):
        transport = self.codex_transport()
        transport.blocker = TRUST_SCREEN
        pending = []
        def accept():
            state, _ = self.store.read()
            pending.append(state['active_pass']['pass_id'])
            self.assertEqual(state['status'], 'running')
            self.assertEqual(state['implementation'], dict(context_id='DEV-7-I1'))
            self.assertEqual(self.registry.get('DEV-7-I1')['state'], 'awaiting_user')
            transport.assert_effects(1, 0)
            transport.advance(2000)
            transport.accept_setup()
            return '\n'
        def deliver(prompt):
            output = Path(re.search(r'Write your result to (.*?)\. This temporary', prompt).group(1))
            pass_id = re.search(r'Pass ID: ([^\n]+)', prompt).group(1)
            self.assertEqual(pending, [pass_id])
            self.assertTrue(output.parent.is_dir())
            output.write_text(json.dumps(dict(pass_id=pass_id, state='completed', summary='Implemented',
                                              checks=[], resolutions=[], task_assessment=dict(
                                                  state='ready', summary='Controlled readiness outcome', questions=[]))))
        transport.on_queue = deliver
        with patch('task_start.agent.sys.stdin') as stdin, patch('task_start.agent.sys.stderr', new_callable=io.StringIO):
            stdin.isatty.return_value = True
            stdin.readline.side_effect = accept
            result = self.run_loop()
        self.assertEqual(result.state, 'clean', result.render())
        self.assertEqual((result.data['passes'], result.data['reviews']), (2, 1))
        self.assertEqual(self.store.read()[0]['implementation']['session']['value'], transport.thread_id)
        transport.assert_effects(1, 1)

    def test_initial_ready_with_persistent_read_error_retains_claim_without_receipt_or_replay(self):
        transport = self.codex_transport()
        transport.blocker = TRUST_SCREEN
        def command(group, operation, *args, **kwargs):
            if (group, operation) == ("pane", "read") and transport.blocker is None:
                raise HerdrResponseError("Unexpected Herdr pane read response")
            return transport.herdr(group, operation, *args, **kwargs)
        transport.command.side_effect = command
        with patch('task_start.agent.sys.stdin') as stdin, \
                patch('task_start.agent.sys.stderr', new_callable=io.StringIO), \
                patch.object(Codex, "POST_TRUST_READY_TIMEOUT", 0.5):
            stdin.isatty.return_value = True
            stdin.readline.side_effect = lambda: (transport.accept_setup(), "\n")[1]
            result = self.run_loop()
        self.assertEqual(result.state, "escalated", result.render())
        state, _ = self.store.read()
        self.assertIsNotNone(state["active_pass"])
        self.assertIsNone(state["delivery"]["receipt"])
        self.assertIn("Unexpected Herdr pane read", result.data["reason"])
        self.assertIn("task loop DEV-7 --abort", result.data["reason"])
        self.assertEqual(self.prompts, [])
        with self.assertRaisesRegex(TaskError, "no durable delivery proof"):
            loop("DEV-7")
        self.assertEqual(self.store.read()[0], state)
        for action in ("continue", "new"):
            with self.assertRaises(TaskError):
                loop("DEV-7", action=action)
        transport.assert_effects(1, 0)

    def test_codex_uncertain_queue_preserves_observed_identity_and_never_requeues(self):
        transport = self.codex_transport()
        transport.queue_error = TaskError('Queue acknowledgement lost')
        result = self.run_loop()
        self.assertEqual(result.state, 'escalated', result.render())
        state, _ = self.store.read()
        self.assertEqual(state['implementation']['session']['value'], transport.thread_id)
        self.assertEqual(self.registry.get('DEV-7-I1')['state'], 'uncertain')
        self.assertEqual(self.prompts, [])
        def stopped_wait(seconds):
            raise KeyboardInterrupt()
        with patch('task_start.loop.time', SimpleNamespace(sleep=stopped_wait)):
            recovered = loop('DEV-7')
        self.assertEqual(recovered.state, 'interrupted', recovered.render())
        self.assertEqual(self.store.read()[0]['active_pass'], state['active_pass'])
        self.assertEqual(self.store.read()[0]['delivery'], state['delivery'])
        for kwargs in (dict(action='continue'), dict(action='new')):
            with self.assertRaises(TaskError):
                loop('DEV-7', **kwargs)
        transport.assert_effects(1, 1)

    def test_initial_codex_stable_session_waits_for_setup_before_reading_history(self):
        transport = self.codex_transport()
        first = dict(agent='codex', kind='id', value=transport.thread_id)
        transport.blocker, transport.start_not_ready = TRUST_SCREEN, False
        transport.start_changes = dict(agent_status='blocked', agent_session=first)
        transport.get_changes = dict(agent_status='blocked', agent_session=first)
        pending, premature_reads, verified_reads = [], [], []
        def request(method, params, **kwargs):
            if method == 'thread/read':
                if transport.blocker:
                    premature_reads.append(params['threadId'])
                    raise TaskError('Provider history is temporarily unreadable during setup')
                if transport.queued_at is None:
                    verified_reads.append((params['threadId'], kwargs.get('timeout')))
            return transport.request(method, params, **kwargs)
        transport.rpc.request.side_effect = request
        def accept():
            state, _ = self.store.read()
            pending.append(state['active_pass']['pass_id'])
            self.assertEqual(state['implementation'], dict(context_id='DEV-7-I1'))
            row = self.registry.get('DEV-7-I1')
            self.assertEqual(context_reference(row), first)
            self.assertEqual((row['state'], row['resumability']), ('awaiting_user', 'unknown'))
            self.assertEqual((row['pane_id'], row['terminal_id']), ('p1', self.panes[0]['terminal_id']))
            self.assertEqual(premature_reads, [])
            transport.assert_effects(1, 0)
            transport.accept_setup()
            transport.get_changes = dict(agent_session=first)
            return '\n'
        def deliver(prompt):
            output = Path(re.search(r'Write your result to (.*?)\. This temporary', prompt).group(1))
            pass_id = re.search(r'Pass ID: ([^\n]+)', prompt).group(1)
            self.assertEqual(pending, [pass_id])
            self.assertTrue(verified_reads)
            for session_id, timeout in verified_reads:
                self.assertEqual(session_id, first['value'])
                self.assertIsNotNone(timeout, 'Pre-delivery history reads must use the reconciliation budget')
                self.assertTrue(0 < timeout <= Codex.POST_TRUST_READY_TIMEOUT)
            state, _ = self.store.read()
            self.assertEqual(state['implementation']['session'], first)
            self.assertTrue(output.parent.is_dir())
            output.write_text(json.dumps(dict(pass_id=pass_id, state='completed', summary='Implemented',
                                              checks=[], resolutions=[], task_assessment=dict(
                                                  state='ready', summary='Controlled readiness outcome', questions=[]))))
        transport.on_queue = deliver
        with patch('task_start.agent.sys.stdin') as stdin, patch('task_start.agent.sys.stderr', new_callable=io.StringIO):
            stdin.isatty.return_value = True
            stdin.readline.side_effect = accept
            result = self.run_loop()
        self.assertEqual(result.state, 'clean', result.render())
        self.assertEqual((result.data['passes'], result.data['reviews']), (2, 1))
        self.assertEqual(premature_reads, [])
        self.assertEqual(len(pending), 1)
        self.assertEqual(self.store.read()[0]['implementation']['session'], first)
        self.assertEqual([c['context_id'] for c in self.registry.list() if c['role'] == 'implementation'], ['DEV-7-I1'])
        transport.assert_effects(1, 1)

    def test_initial_codex_unreadable_history_after_setup_never_delivers(self):
        transport = self.codex_transport()
        first = dict(agent='codex', kind='id', value=transport.thread_id)
        transport.blocker, transport.start_not_ready = TRUST_SCREEN, False
        transport.start_changes = dict(agent_status='blocked', agent_session=first)
        transport.get_changes = dict(agent_session=first)
        actions = []
        def request(method, params, **kwargs):
            if method == 'thread/read':
                raise TaskError('Provider history is temporarily unreadable')
            return transport.request(method, params, **kwargs)
        transport.rpc.request.side_effect = request
        def accept():
            actions.append(True)
            transport.accept_setup()
            return '\n'
        with patch('task_start.agent.sys.stdin') as stdin, patch('task_start.agent.sys.stderr', new_callable=io.StringIO):
            stdin.isatty.return_value = True
            stdin.readline.side_effect = accept
            result = self.run_loop()
        self.assertEqual(actions, [True])
        self.assertEqual(result.state, 'escalated', result.render())
        self.assertIn('Provider history is temporarily unreadable', result.data['reason'])
        self.assertEqual(context_reference(self.registry.get('DEV-7-I1')), first)
        self.assertEqual(self.registry.get('DEV-7-I1')['resumability'], 'unknown')
        self.assertEqual(self.prompts, [])
        transport.assert_effects(1, 0)

    def test_initial_codex_identity_conflict_after_setup_never_delivers(self):
        transport = self.codex_transport()
        first = dict(agent='codex', kind='id', value=transport.thread_id)
        transport.blocker, transport.start_not_ready = TRUST_SCREEN, False
        transport.start_changes = dict(agent_status='blocked', agent_session=first)
        transport.get_changes = dict(agent_session=first)
        actions = []
        def accept():
            actions.append(True)
            transport.accept_setup()
            transport.get_changes = dict(agent_session=dict(first, value='11a0d314-bd68-7203-8b68-f2520f892afa'))
            return '\n'
        with patch('task_start.agent.sys.stdin') as stdin, patch('task_start.agent.sys.stderr', new_callable=io.StringIO):
            stdin.isatty.return_value = True
            stdin.readline.side_effect = accept
            result = self.run_loop()
        self.assertEqual(actions, [True])
        self.assertEqual(result.state, 'escalated', result.render())
        self.assertIn('session changed', result.data['reason'])
        self.assertEqual(context_reference(self.registry.get('DEV-7-I1')), first)
        self.assertEqual(self.prompts, [])
        transport.assert_effects(1, 0)

    def test_findings_use_original_implementation_and_same_reviewer(self):
        self.review_results = [findings(finding()), {}]
        result = self.run_loop()
        self.assertEqual(result.state, 'clean', result.render())
        self.assertEqual((result.data['passes'], result.data['reviews']), (4, 2))
        self.assertEqual((len(self.launches), len(self.impl_prompts), len(self.prompts)), (1, 2, 2))
        state, _ = self.store.read()
        self.assertEqual([r['context_id'] for r in state['records']], ['DEV-7-I1', 'DEV-7-R1', 'DEV-7-I1', 'DEV-7-R1'])
        self.assertIn('Continue in YOUR existing', self.impl_prompts[1])
        self.assertIn('"finding_id": "F1"', self.prompts[1])
        self.assertEqual(self.recreated, [False])

        for prompt in self.prompts:
            self.assertIn('REVIEW VALIDATION POLICY: focused_first', prompt)
            self.assertIn(REVIEW_VALIDATION_INSTRUCTIONS, prompt)
        for prompt in self.impl_prompts:
            self.assertIn(IMPLEMENTATION_INSTRUCTIONS, prompt)
            self.assertIn('test-first / red-green-refactor', prompt)
            self.assertIn('fix the complete batch and rerun relevant tests', prompt)

    def test_existing_default_workspace_is_reused_and_work_preserved(self):
        self.prepared_workspace()
        (self.path / 'work.txt').write_text('existing work')
        result = self.run_loop()
        self.assertEqual(result.state, 'clean', result.render())
        self.assertEqual(self.preparation, ['base', 'open', 'linear'])
        self.assertEqual((self.path / 'work.txt').read_text(), 'existing work')
        self.history.assert_called_once_with('main', self.branch, existing=True)

    def test_base_advances_before_fresh_workspace(self):
        fixture.baseline.LocalGitIntegrationTests.advance_remote(self)
        result = self.run_loop()
        self.assertEqual(result.state, 'clean', result.render())
        upstream = self.command(self.remote, 'rev-parse', 'HEAD')
        self.assertEqual(self.command(self.repo, 'rev-parse', 'HEAD'), upstream)
        self.assertEqual(self.command(self.path, 'rev-parse', 'HEAD'), upstream)

    def upstream_project_config(self, content, name='config.toml'):
        directory = self.remote / '.agentic-workflows-lite'
        directory.mkdir(exist_ok=True)
        (directory / name).write_text(content)
        self.command(self.remote, 'add', str(directory / name))
        self.command(self.remote, 'commit', '-m', 'Upstream project configuration')

    def mock_start_handoff(self):
        self.enterContext(patch('task_start.cli.load_local', return_value=self.local))
        self.enterContext(patch('task_start.cli.load_projects', return_value=[
            fixture.Project(fixture.ISSUE.project, self.repo.name, 'main')]))
        self.enterContext(patch('task_start.cli.Linear', return_value=self.linear))
        self.enterContext(patch('task_start.cli.adapter_for', side_effect=self.select_adapter))
        return self.enterContext(patch('task_start.cli.launch_registered',
                                      return_value=SimpleNamespace(summary='launched')))

    def assert_config_refused_before_workspace(self):
        self.assertEqual(self.command(self.repo, 'rev-parse', 'HEAD'),
                         self.command(self.remote, 'rev-parse', 'HEAD'))
        self.assertEqual(self.preparation, ['base'])
        self.linear.start.assert_not_called()
        self.assertEqual(self.registry.list(), [])
        self.assertEqual(self.launches, [])
        self.assertEqual(self.prompts, [])
        self.assertIsNone(self.store)
        self.assertFalse(self.path.exists())
        self.assertEqual(self.git.branches('DEV-7'), [])
        self.assertEqual([tree['worktree'] for tree in self.git.worktrees()], [str(self.repo)])

    def test_upstream_defaults_require_retry_before_initial_loop_artifacts(self):
        self.upstream_project_config('[agent]\nmodel="upstream-i"\n[reviewer]\nmodel="upstream-r"\n'
                                     '[loop]\ntimeout=41\n')
        with self.assertRaisesRegex(TaskError, 'Project defaults changed.*retry'):
            loop('DEV-7')
        self.assert_config_refused_before_workspace()
        result = loop('DEV-7')
        self.assertEqual(result.state, 'clean', result.render())
        state = self.store.read()[0]
        self.assertEqual(state['implementation_options'], dict(kind='pi', model='upstream-i', mode='low'))
        self.assertEqual(state['reviewer_options'], dict(kind='codex', model='upstream-r', mode='high'))
        self.assertEqual(state['timeout'], 41)
        self.assertEqual(self.launches[0].options, AgentOptions('pi', 'upstream-i', 'low'))
        self.assertEqual(self.command(self.path, 'rev-parse', 'HEAD'),
                         self.command(self.remote, 'rev-parse', 'HEAD'))
        self.assertEqual(self.preparation, ['base', 'base', 'create', 'linear'])

    def test_upstream_defaults_require_retry_before_start_artifacts(self):
        launch = self.mock_start_handoff()
        self.upstream_project_config('[agent]\nmodel="upstream-i"\nmode="medium"\n')
        with self.assertRaisesRegex(TaskError, 'Project defaults changed.*retry'):
            cli.start('DEV-7', mode='high')
        self.assert_config_refused_before_workspace()
        launch.assert_not_called()
        cli.start('DEV-7', mode='high')
        launch.assert_called_once()
        execution = launch.call_args.args[1]
        self.assertEqual(execution.options, AgentOptions('pi', 'upstream-i', 'high'))
        self.assertEqual(execution.repository, self.repo)
        self.assertEqual(execution.workspace.path, self.path)
        self.assertEqual(self.command(self.path, 'rev-parse', 'HEAD'),
                         self.command(self.remote, 'rev-parse', 'HEAD'))

    def test_upstream_removal_does_not_keep_stale_project_defaults(self):
        from test_layered_config import install_lifecycle_layers
        install_lifecycle_layers(self)
        shared = self.remote / '.agentic-workflows-lite/config.toml'
        shared.unlink()
        self.command(self.remote, 'add', '-u')
        self.command(self.remote, 'commit', '-m', 'Remove shared defaults')
        with self.assertRaisesRegex(TaskError, 'Project defaults changed.*retry'):
            loop('DEV-7')
        self.assert_config_refused_before_workspace()
        result = loop('DEV-7')
        self.assertEqual(result.state, 'clean', result.render())
        state = self.store.read()[0]
        self.assertEqual(state['implementation_options'], dict(kind='pi', model='implementer', mode='high'))
        self.assertEqual(state['reviewer_options'], dict(kind='codex', model='review-model', mode='medium'))
        self.assertEqual(state['timeout'], 23)
        self.assertFalse((self.path / '.agentic-workflows-lite/config.local.toml').exists())

    def test_unsafe_upstream_configuration_never_reaches_start_or_loop_artifacts(self):
        for command in ('start', 'loop', 'no-agent'):
            for name, content, message in (
                ('config.toml', '[linear]\napi_key="synthetic-forbidden"', 'only agent, reviewer, and loop'),
                ('config.local.toml', '[agent]\nmodel="implementer"', 'must not be tracked'),
            ):
                with self.subTest(command=command, file=name):
                    case = BootstrapLoopTests(); case.setUp()
                    try:
                        launch = case.mock_start_handoff()
                        case.upstream_project_config(content, name)
                        with case.assertRaisesRegex(TaskError, message):
                            if command == 'loop':
                                loop('DEV-7')
                            else:
                                cli.start('DEV-7', no_agent=command == 'no-agent')
                        case.assert_config_refused_before_workspace()
                        launch.assert_not_called()
                        case.assertEqual((case.repo / '.agentic-workflows-lite' / name).read_text(), content)
                    finally:
                        case.doCleanups()

    def test_initial_outcomes_fail_closed_without_review(self):
        for outcome in ('blocked', 'failed', 'malformed', 'missing', 'failed_check', 'busy'):
            with self.subTest(outcome=outcome):
                case = BootstrapLoopTests()
                case.setUp()
                # Outcome/status comparisons inspect the same moment; advisory
                # timing transitions are covered by the threshold regressions.
                case.enterContext(patch('task_start.pass_delivery.time',
                                        SimpleNamespace(time=lambda: 2000000000.0)))
                try:
                    if outcome in ('blocked', 'failed'):
                        case.impl_overrides = dict(state=outcome)
                    elif outcome in ('malformed', 'missing'):
                        case.impl_raw = '{}' if outcome == 'malformed' else 'missing'
                    elif outcome == 'busy':
                        case.impl_status = 'working'
                    else:
                        case.impl_overrides = dict(checks=[dict(name='tests', result='failed', details='failure')])
                    def stopped_wait(seconds):
                        raise KeyboardInterrupt()
                    with patch('task_start.implementation_pass.time',
                               SimpleNamespace(monotonic=time.monotonic, sleep=stopped_wait)):
                        result = case.run_loop()
                    self.assertEqual(result.state, 'interrupted' if outcome in ('missing', 'busy') else 'escalated',
                                     result.render())
                    self.assertEqual((result.data['passes'], result.data['reviews']), (1, 0))
                    self.assertEqual(case.prompts, [])
                    self.assertEqual(len(case.launches), 1)
                    with self.assertRaisesRegex(TaskError, 'Only a paused'):
                        case.continue_loop()
                    self.assertEqual(loop('DEV-7', action='status').data, result.data)
                finally:
                    case.doCleanups()

    def test_preparation_failures_do_not_launch(self):
        for failure in ('base', 'workspace', 'linear'):
            with self.subTest(failure=failure):
                case = BootstrapLoopTests()
                case.setUp()
                try:
                    case.base_error = failure == 'base'
                    case.workspace_error = failure == 'workspace'
                    if failure == 'linear':
                        case.linear.start.side_effect = TaskError('Linear update failed')
                    with self.assertRaises(TaskError):
                        case.run_loop()
                    if failure != 'linear':
                        case.linear.start.assert_not_called()
                    else:
                        self.assertTrue(case.path.is_dir())
                    self.assertEqual(case.launches, [])
                    self.assertEqual(case.registry.list(), [])
                    if case.store:
                        self.assertFalse(case.store.path.exists())
                finally:
                    case.doCleanups()

    def test_preflight_refusals_before_base_workspace_linear_or_allocation(self):
        original = self.local
        for changes, overrides in (
            (dict(agent=fixture.AgentConfig('pi', None, 'low')), {}),
            (dict(agent=fixture.AgentConfig('pi', 'model', None)), {}),
            (dict(reviewer=fixture.AgentConfig('codex', None, 'high')), {}),
            ({}, dict(impl_model='bad model')),
            ({}, dict(impl_mode='unsupported')),
            ({}, dict(impl_agent_kind='unknown')),
        ):
            with self.subTest(changes=changes, overrides=overrides):
                self.local = replace(original, **changes)
                with self.assertRaises(TaskError):
                    self.run_loop(**overrides)
        self.local = original
        with patch.object(self.implementer, 'check_available', side_effect=TaskError('Agent unavailable')):
            with self.assertRaisesRegex(TaskError, 'unavailable'):
                self.run_loop()
        self.linear.get_issue.return_value = replace(fixture.ISSUE, description='No guidance')
        for name in ('Agent instructions', 'Build notes'):
            with self.subTest(block_name=name):
                self.local = replace(original, issue_structure=IssueStructureConfig('required', name))
                with self.assertRaisesRegex(TaskError, f'{name}.*mode=required'):
                    self.run_loop()
        self.assertEqual(self.preparation, [])
        self.linear.start.assert_not_called()
        self.assertEqual(self.registry.list(), [])
        self.assertFalse(self.path.exists())

    def test_layered_initial_settings_timeout_and_cli_are_pinned_through_fixes(self):
        from test_layered_config import install_lifecycle_layers
        local = install_lifecycle_layers(self)
        self.review_results = [findings(finding()), {}]
        def change_defaults(execution, items):
            local.write_text('[agent]\nkind="codex"\nmodel="changed"\nmode="low"\n'
                             '[reviewer]\nkind="pi"\nmodel="changed"\nmode="low"\n[loop]\ntimeout=999')
        self.on_implementation = change_defaults
        result = loop('DEV-7', impl_model='cli-i', mode='high')
        self.assertEqual(result.state, 'clean', result.render())
        state = self.store.read()[0]
        self.assertEqual(state['implementation_options'], dict(kind='pi', model='cli-i', mode='high'))
        self.assertEqual(state['reviewer_options'], dict(kind='codex', model='shared-r', mode='high'))
        self.assertEqual(state['timeout'], 23)
        self.assertEqual(self.launches[0].options, AgentOptions('pi', 'cli-i', 'high'))
        self.assertEqual([r['context_id'] for r in state['records']],
                         ['DEV-7-I1', 'DEV-7-R1', 'DEV-7-I1', 'DEV-7-R1'])
        self.assertEqual(state['reviewer']['model'], 'shared-r')
        self.assertEqual(self.recreated, [False])
        self.assertFalse((self.path / '.agentic-workflows-lite/config.local.toml').exists())
        self.assertFalse(any('config.local.toml' in prompt for prompt in self.impl_prompts + self.prompts))

    def test_project_settings_changes_do_not_reload_paused_initial_boundary(self):
        from test_layered_config import install_lifecycle_layers
        local = install_lifecycle_layers(self)
        original = LoopStore.begin
        def pause(store, state):
            store.pause()
            return original(store, state)
        with patch.object(LoopStore, 'begin', pause):
            result = loop('DEV-7', timeout=17)
        self.assertEqual(result.state, 'paused', result.render())
        before = self.store.read()[0]
        self.assertEqual(before['timeout'], 17)  # CLI wins over the local 23.
        local.write_text('invalid project settings = [')
        result = loop('DEV-7', action='continue')
        self.assertEqual(result.state, 'clean', result.render())
        after = self.store.read()[0]
        for key in ('implementation_options', 'reviewer_options', 'timeout', 'run_id'):
            self.assertEqual(after[key], before[key], key)
        self.assertEqual(len(self.launches), 1)

    def test_invalid_project_timeout_blocks_bootstrap_without_preparation(self):
        from test_layered_config import install_lifecycle_layers
        local = install_lifecycle_layers(self)
        local.write_text('[loop]\ntimeout=true')
        with self.assertRaisesRegex(TaskError, 'loop.timeout'):
            loop('DEV-7')
        self.assertEqual(self.preparation, [])
        self.assertEqual(self.registry.list(), [])
        self.assertEqual(self.launches, [])
        self.linear.start.assert_not_called()

    def test_plain_loop_cli_uses_both_configured_roles(self):
        with patch('sys.stdout', new_callable=io.StringIO) as output:
            self.assertEqual(cli.main(['loop', 'DEV-7']), 0)
        self.assertIn('DEV-7 loop: clean', output.getvalue())
        self.assertEqual(self.launches[0].options, AgentOptions('pi', 'implementer', 'low'))
        self.assertEqual(self.store.read()[0]['reviewer_options'],
                         dict(kind='codex', model='review-model', mode='high'))

    def test_cli_partial_overrides_inherit_remaining_role_settings(self):
        with patch('sys.stdout', new_callable=io.StringIO) as output:
            self.assertEqual(cli.main(['loop', 'DEV-7', '--i-model', 'initial-override',
                                       '--r-mode', 'medium']), 0)
        self.assertIn('DEV-7 loop: clean', output.getvalue())
        self.assertEqual(self.launches[0].options, AgentOptions('pi', 'initial-override', 'low'))
        self.assertEqual(self.store.read()[0]['reviewer_options'],
                         dict(kind='codex', model='review-model', mode='medium'))

    def test_cli_missing_role_config_names_available_overrides_before_mutation(self):
        configured = self.local
        for role, prefix in (('agent', 'i'), ('reviewer', 'r')):
            for config, hint in ((None, f'--{prefix}-agent'),
                                 (fixture.AgentConfig('codex', None, 'high'), f'--{prefix}-model'),
                                 (fixture.AgentConfig('codex', 'model', None), f'--{prefix}-mode')):
                with self.subTest(role=role, config=config):
                    self.local = replace(configured, **{role: config})
                    with patch('sys.stderr', new_callable=io.StringIO) as error:
                        self.assertEqual(cli.main(['loop', 'DEV-7']), 1)
                    self.assertIn(hint, error.getvalue())
        self.assertEqual(self.preparation, [])
        self.linear.start.assert_not_called()
        self.assertEqual(self.registry.list(), [])

    def test_cli_role_overrides_select_implementation_and_reviewer_independently(self):
        with patch('sys.stdout', new_callable=io.StringIO) as output:
            code = cli.main(['loop', 'DEV-7', '--i-agent', 'pi', '--i-model', 'initial-model',
                             '--i-mode', 'medium', '--r-agent', 'codex', '--r-model', 'review-override',
                             '--r-mode', 'low', '--json'])
        self.assertEqual(code, 0, output.getvalue())
        self.assertEqual(self.launches[0].options, AgentOptions('pi', 'initial-model', 'medium'))
        state, _ = self.store.read()
        self.assertEqual(state['reviewer_options'], dict(kind='codex', model='review-override', mode='low'))
        self.assertEqual(json.loads(output.getvalue())['state'], 'clean')

    def test_pause_before_initial_launch_status_continue_preserves_settings(self):
        result = self.pause_before_launch()
        with patch('task_start.loop.load_local', side_effect=AssertionError('No config for status')):
            self.assertEqual(loop('DEV-7', action='status').data, result.data)
            self.assertEqual(self.pause().state, 'paused')
        self.local = replace(self.local, agent=fixture.AgentConfig('pi', 'changed', 'max'))
        continued = self.continue_loop()
        self.assertEqual(continued.state, 'clean', continued.render())
        self.assertEqual(self.launches[0].options, AgentOptions('pi', 'implementer', 'low'))
        self.assertEqual(self.preparation, ['base', 'create', 'linear'])

    def test_pause_during_initial_pass_collects_then_continues_review_only(self):
        self.on_implementation = lambda *_: self.pause()
        result = self.run_loop()
        self.assertEqual(result.state, 'paused', result.render())
        self.assertEqual(result.data['next_phase'], 'review')
        self.assertEqual((result.data['passes'], result.data['reviews']), (1, 0))
        self.assertEqual(self.prompts, [])
        self.on_implementation = None
        result = self.continue_loop()
        self.assertEqual(result.state, 'clean', result.render())
        self.assertEqual((len(self.launches), len(self.impl_prompts), len(self.prompts)), (1, 1, 1))

    def test_initial_pass_counts_against_budget(self):
        result = self.run_loop(max_passes=1)
        self.assertEqual(result.state, 'escalated', result.render())
        self.assertIn('limit reached', result.data['reason'])
        self.assertEqual((result.data['passes'], result.data['reviews']), (1, 0))
        self.assertEqual(self.prompts, [])

    def test_interrupted_or_uncertain_launch_retains_claim_and_session_without_replay(self):
        for error in (KeyboardInterrupt(), TaskError('Uncertain delivery')):
            with self.subTest(error=type(error)):
                case = BootstrapLoopTests()
                case.setUp()
                try:
                    case.launch_error = error
                    result = case.run_loop()
                    self.assertEqual(result.state, 'interrupted' if isinstance(error, KeyboardInterrupt) else 'escalated')
                    state, _ = case.store.read()
                    self.assertEqual(state['active_pass']['context_id'], 'DEV-7-I1')
                    self.assertEqual(state['implementation']['session']['conversation_id'], 'original-implementation')
                    self.assertEqual(case.registry.get('DEV-7-I1')['state'], 'uncertain')
                    for kwargs in ({}, dict(action='new'), dict(action='continue')):
                        with self.assertRaises(TaskError):
                            loop('DEV-7', **kwargs)
                    self.assertEqual(len(case.launches), 1)
                    self.assertEqual(case.prompts, [])
                    self.assertEqual(loop('DEV-7', action='status').data, result.data)
                finally:
                    case.doCleanups()

    def test_failed_startup_before_session_observation_retains_unbound_claim(self):
        with patch.object(self.implementer, 'launch', side_effect=TaskError('Startup uncertain')) as launch:
            result = self.run_loop()
        self.assertEqual(result.state, 'escalated', result.render())
        launch.assert_called_once()
        state, _ = self.store.read()
        self.assertEqual(state['implementation'], dict(context_id='DEV-7-I1'))
        self.assertEqual(state['active_pass']['context_id'], 'DEV-7-I1')
        self.assertIsNotNone(state['active_pass']['pass_id'])
        self.assertEqual(self.registry.get('DEV-7-I1')['state'], 'uncertain')
        self.assertEqual(loop('DEV-7', action='status').data, result.data)
        for kwargs in ({}, dict(action='continue'), dict(action='new')):
            with self.assertRaises(TaskError):
                loop('DEV-7', **kwargs)
        self.assertEqual(self.prompts, [])

    def test_orphaned_initial_claim_cannot_continue_or_launch_replacement(self):
        self.pause_before_launch()
        state = self.store.continue_paused()
        self.store.begin(state)  # Simulate process death immediately after claim.
        for kwargs in ({}, dict(action='new'), dict(action='continue')):
            with self.assertRaises(TaskError):
                loop('DEV-7', **kwargs)
        self.assertEqual(loop('DEV-7', action='status').state, 'running')
        self.assertEqual(self.launches, [])
        self.assertEqual(len(self.registry.list()), 1)

    def test_interruption_after_reservation_before_checkpoint_cannot_relaunch(self):
        with patch.object(LoopStore, 'create', side_effect=KeyboardInterrupt()):
            with self.assertRaises(KeyboardInterrupt):
                self.run_loop()
        self.assertEqual(self.registry.get('DEV-7-I1')['state'], 'launching')
        with self.assertRaises(TaskError):
            self.run_loop()
        self.assertEqual(self.launches, [])

    def test_paused_initial_boundary_rechecks_shell_checkout_and_context(self):
        self.pause_before_launch()
        self.panes[0]['terminal_id'] = 'replacement'
        result = self.continue_loop()
        self.assertEqual(result.state, 'escalated', result.render())
        self.assertEqual(self.launches, [])

    def test_pause_before_launch_refuses_requirement_checkout_settings_and_session_drift(self):
        for fault in ('requirements', 'checkout', 'settings', 'session'):
            with self.subTest(fault=fault):
                case = BootstrapLoopTests()
                case.setUp()
                try:
                    case.pause_before_launch()
                    if fault == 'requirements':
                        case.linear.get_issue.return_value = replace(fixture.ISSUE, description='Changed scope')
                    elif fault == 'checkout':
                        (case.path / 'external.txt').write_text('out of band change')
                    elif fault == 'settings':
                        with case.registry.connection(write=True) as db:
                            db.execute('UPDATE contexts SET model=?', ('changed-model',))
                    else:
                        case.registry.update('DEV-7-I1', session_kind='path', session_id='/unexpected.jsonl')
                    result = case.continue_loop()
                    self.assertEqual(result.state, 'escalated', result.render())
                    self.assertEqual(case.launches, [])
                    self.assertEqual(case.prompts, [])
                finally:
                    case.doCleanups()

    def test_initial_checkpoint_rejects_missing_origin_settings_or_session(self):
        self.pause_before_launch()
        state, pause = self.store.read()
        variants = [dict(state, version=1), dict(state, initial_phase='implementation'),
                    dict(state, next_phase='review'), dict(state, implementation_options={}),
                    {k: v for k, v in state.items() if k != 'initial_phase'}]
        for value in variants:
            with self.assertRaisesRegex(TaskError, 'Malformed loop checkpoint'):
                LoopStore.decode((json.dumps(value), pause))
        self.assertEqual(self.continue_loop().state, 'clean')
        state, pause = self.store.read()
        state['implementation'] = dict(context_id='DEV-7-I1')
        with self.assertRaisesRegex(TaskError, 'Malformed loop checkpoint'):
            LoopStore.decode((json.dumps(state), pause))

    def test_sliced_historical_and_ambiguous_workspaces_are_refused(self):
        self.prepared_workspace(scope='add-ingestion-cli')
        with self.assertRaisesRegex(TaskError, 'unsliced'):
            self.run_loop()
        self.linear.start.assert_not_called()
        self.assertEqual(self.registry.list(), [])
        with patch.object(Git, 'check_history', side_effect=TaskError('Historical branch')):
            with self.assertRaisesRegex(TaskError, 'Historical'):
                self.run_loop()
        self.command(self.repo, 'branch', 'dev-7-other')
        with self.assertRaises(TaskError):
            self.run_loop()
        self.assertNotIn('open', self.preparation)
        self.assertEqual(self.launches, [])

    def dispose_initial(self, *, interrupted=False):
        """Real disposal journals/Git/registry, synthetic remote and Herdr proof."""
        from task_start.cleanup import discard_execution
        closed = False
        original_panes = copy.deepcopy(self.panes)
        def worktrees(operation, *args):
            value = self.worktrees(operation, *args)
            if closed and operation == "list":
                for tree in value["worktrees"]:
                    tree["open_workspace_id"] = None
            return value
        def workspaces():
            return [] if closed else [dict(workspace_id="w1", label="DEV-7", pane_count=len(self.panes),
                worktree=dict(repo_root=str(self.repo), repo_key=str(self.repo / ".git"), repo_name=self.repo.name,
                              checkout_path=str(self.path), is_linked_worktree=True))]
        def close(operation, workspace):
            nonlocal closed
            self.assertEqual((operation, workspace), ("close", "w1"))
            if interrupted:
                raise TaskError("Interrupted close")
            closed = True
            self.panes.clear()
            return dict(workspace_id="w1")
        with patch("task_start.cleanup.ContextRegistry", return_value=self.registry), \
                patch("task_start.cleanup.HerdrContexts", return_value=self.identities), \
                patch("task_start.cleanup.never_published", return_value={"repository": "example/test"}), \
                patch("task_start.cleanup.stopped_execution", return_value={}), \
                patch.object(Herdr, "command", side_effect=worktrees), \
                patch.object(Herdr, "workspaces", side_effect=workspaces), \
                patch.object(Herdr, "workspace_command", side_effect=close):
            discard_execution(fixture.ISSUE, fixture.Project(fixture.ISSUE.project, self.repo.name, "main"), self.repo)
        self.panes[:] = original_panes
        self.expected_implementation = "DEV-7-I2"
        return self.git.disposal_files("DEV-7")[0]

    def test_completed_disposal_allows_fresh_loop_and_preserves_history_and_ordinal(self):
        self.pause_before_launch()
        path = self.dispose_initial()
        journal = path.read_bytes()
        retired = self.registry.get("DEV-7-I1")
        self.assertEqual(retired["state"], "retired")
        self.assertFalse(self.path.exists())
        result = self.run_loop()
        self.assertEqual(result.state, "clean", result.render())
        self.assertEqual(result.data["implementation_context"], "DEV-7-I2")
        self.assertEqual(len(self.launches), 1)
        self.assertEqual(self.registry.get("DEV-7-I1"), retired)
        self.assertEqual(path.read_bytes(), journal)
        self.assertEqual([c["ordinal"] for c in self.registry.list("DEV-7", include_retired=True)
                          if c["role"] == "implementation"], [1, 2])

    def test_prelaunch_recovery_after_disposal_reuses_new_reservation_only(self):
        self.pause_before_launch()
        path = self.dispose_initial()
        before = path.read_bytes()
        # A second launch's pre-handoff interruption must tolerate the same
        # validated retired history when checking its reserved empty shell.
        self.pause_before_launch()
        self.assertEqual(self.registry.get("DEV-7-I2")["state"], "launching")
        result = self.continue_loop()
        self.assertEqual(result.state, "clean", result.render())
        self.assertEqual(result.data["implementation_context"], "DEV-7-I2")
        self.assertEqual(len(self.launches), 1)
        self.assertEqual(path.read_bytes(), before)

    def test_restart_refuses_pending_disposal_before_preparation(self):
        self.pause_before_launch()
        with self.assertRaisesRegex(TaskError, "Interrupted close"):
            self.dispose_initial(interrupted=True)
        before = list(self.preparation)
        with self.assertRaises(TaskError):
            self.run_loop()
        self.assertEqual(self.preparation, before)
        self.assertEqual(self.launches, [])
        self.assertTrue(self.path.exists())
        self.assertEqual(self.registry.get("DEV-7-I1")["state"], "launching")

    def test_restart_requires_exact_complete_journal_and_no_conflicting_history(self):
        for kind in ("missing", "pending", "wrong_context", "incomplete", "live_claim", "foreign_history"):
            with self.subTest(kind=kind):
                case = BootstrapLoopTests(); case.setUp()
                try:
                    case.pause_before_launch()
                    path = case.dispose_initial()
                    record = json.loads(path.read_text())
                    if kind == "missing":
                        path.unlink()
                    elif kind == "live_claim":
                        case.registry.allocate("DEV-7", "review", agent="codex", repository=str(case.repo),
                                               worktree=str(case.path))
                    elif kind == "foreign_history":
                        context = case.registry.allocate("DEV-7", "review", agent="codex")
                        case.registry.retire_execution([case.registry.get(context)], now())
                    else:
                        if kind == "pending":
                            record.update(state="pending", runtime=dict(process_proof=1, workspace=False, panes=[], shells={}))
                        elif kind == "wrong_context":
                            record["contexts"][0]["pane_id"] = "stale-pane"
                        else:
                            record["branch_state"] = "pending"
                        path.write_text(json.dumps(record))
                    before = list(case.preparation)
                    with case.assertRaises(TaskError):
                        case.run_loop()
                    case.assertEqual(case.preparation, before)
                    case.assertEqual(case.launches, [])
                    case.assertFalse(case.path.exists())
                finally:
                    case.doCleanups()

    def test_retired_context_history_and_from_review_do_not_bootstrap(self):
        self.prepared_workspace()
        context = self.registry.allocate('DEV-7', 'implementation', agent='pi', repository=str(self.repo),
            worktree=str(self.path), endpoint='/server.sock', workspace_id='w1')
        self.registry.retire('DEV-7', self.repo, self.path, endpoint='/server.sock', workspace_id='w1')
        with self.assertRaisesRegex(TaskError, 'history'):
            self.run_loop()
        with self.assertRaises(TaskError):
            self.run_loop(from_review=True)
        self.assertEqual(self.registry.get(context)['state'], 'retired')
        self.assertEqual(self.preparation, [])
        self.assertEqual(self.launches, [])

    def test_implementation_flags_rejected_for_existing_context_and_controls(self):
        self.assertEqual(self.run_loop().state, 'clean')
        for kwargs in (dict(action='new', impl_model='other'), dict(from_review=True, impl_mode='low'),
                       dict(action='status', impl_agent_kind='pi'), dict(action='continue', impl_model='other'),
                       dict(action='pause', impl_mode='low')):
            with self.subTest(kwargs=kwargs), self.assertRaises(TaskError):
                loop('DEV-7', **kwargs)
        before = self.registry.get('DEV-7-I1')
        checkpoint = self.store.read()
        for flag, value in (('--i-agent', 'codex'), ('--i-model', 'other'), ('--i-mode', 'high')):
            with self.subTest(flag=flag), patch('sys.stderr', new_callable=io.StringIO) as error:
                self.assertEqual(cli.main(['loop', 'DEV-7', '--new', flag, value]), 1)
                self.assertIn('existing implementation settings are preserved', error.getvalue())
        self.assertEqual(self.registry.get('DEV-7-I1'), before)
        self.assertEqual(self.store.read(), checkpoint)
        self.assertEqual(len(self.launches), 1)
