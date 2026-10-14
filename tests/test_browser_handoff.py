from dataclasses import replace
import json
from pathlib import Path
import subprocess
import unittest
from unittest.mock import Mock, patch

from task_start import TaskError
from task_start.agent import AgentExecution, AgentOptions
from task_start.browser import prepare_browser
from task_start.cli import parser, start
from task_start.loop import loop
from task_start.workspace import Workspace
from test_task_start import ISSUE
import browser_handoff_fixture


class BrowserHandoffTests(unittest.TestCase):
    def setUp(self):
        self.workspace = Workspace('dev-7-browser', Path('/tmp/checkout'), 'w', 't', 'p', 'ready')
        self.execution = AgentExecution(ISSUE, Path('/tmp/repo'), self.workspace,
                                        AgentOptions('codex', 'model', 'high'), 'Task', policy=dict(browser=True))
        self.context = dict(context_id='DEV-7-I1', agent='codex', worktree='/tmp/checkout', browser_enabled=False)
        self.registry = Mock()
        self.registry.browser_enabled.return_value = False
        self.registry.browser_runtime.return_value = "/tmp/original-runtime"

    def test_native_and_custom_actor_assignments_are_stable_and_unique(self):
        calls = browser_handoff_fixture.install(self)
        for kind in ('codex', 'pi', 'custom'):
            for actor in ('DEV-7-I1', 'DEV-7-I1-sub1', 'DEV-7-I1-sub2', 'DEV-7-R1'):
                context = dict(self.context, agent=kind, context_id=actor)
                execution = replace(self.execution, options=AgentOptions(kind))
                first = prepare_browser(execution, context, self.registry)
                resumed = prepare_browser(execution, dict(context, browser_enabled=True), self.registry)
                self.assertEqual(first.handoff, resumed.handoff)
                self.assertIn(f'AWL_BROWSER_INSTANCE={actor}', first.handoff)
                self.assertIn(f'--instance {actor} --actor {actor} --connect-only', first.handoff)
        self.assertEqual(len(set(calls)), 12)

    def test_no_browser_means_no_resolution_or_subprocess(self):
        execution = replace(self.execution, policy={})
        with patch('task_start.browser.which', side_effect=AssertionError('no PATH check')):
            self.assertIs(prepare_browser(execution, self.context, self.registry), execution)
        self.registry.enable_browser.assert_not_called()

    def test_saved_opt_in_is_used_on_resume(self):
        calls = browser_handoff_fixture.install(self)
        self.registry.browser_enabled.return_value = True
        result = prepare_browser(replace(self.execution, policy={}), self.context, self.registry)
        self.assertEqual(calls[0][0], self.context['context_id'])
        self.assertIn('--connect-only', result.handoff)
        self.registry.enable_browser.assert_not_called()

    def test_resumed_actor_pins_runtime_despite_changed_controller_environment(self):
        self.registry.browser_enabled.return_value = True
        calls = browser_handoff_fixture.install(self)
        with patch.dict('os.environ', AWL_BROWSER_RUNTIME_DIR='/tmp/changed-runtime'):
            result = prepare_browser(self.execution, self.context, self.registry)
        self.assertIn('AWL_BROWSER_RUNTIME_DIR=/tmp/original-runtime', result.handoff)
        self.assertNotIn('/tmp/changed-runtime', result.handoff)
        self.registry.enable_browser.assert_not_called()

    def test_context_identity_mismatch_fails_before_preparation(self):
        for changes in (dict(context_id='different'), dict(options=AgentOptions('pi')),
                        dict(workspace=replace(self.workspace, path=Path('/tmp/different')))):
            with patch('task_start.browser.which', side_effect=AssertionError('no preparation')), self.assertRaisesRegex(TaskError, 'mismatch'):
                prepare_browser(replace(self.execution, **changes), self.context, self.registry)
        self.registry.enable_browser.assert_not_called()

    def test_missing_helper_and_timeouts_are_actionable_and_opt_in_is_retained(self):
        with patch('task_start.browser.which', return_value=None), self.assertRaisesRegex(TaskError, 'controller PATH'):
            prepare_browser(self.execution, self.context, self.registry)
        self.assertEqual(self.registry.enable_browser.call_args.args[0], 'DEV-7-I1')
        self.assertTrue(Path(self.registry.enable_browser.call_args.args[1]).is_absolute())
        with patch('task_start.browser.which', return_value='/helper'), \
                patch('task_start.browser.run_browser', side_effect=subprocess.TimeoutExpired('helper', 75)), \
                self.assertRaisesRegex(TaskError, '--instance DEV-7-I1 --actor DEV-7-I1 --prepare'):
            prepare_browser(self.execution, self.context, self.registry)

    def test_invalid_runtime_is_rejected_before_retaining_assignment(self):
        for directory in ('relative', '/tmp/' + 'x' * 120):
            with patch.dict('os.environ', AWL_BROWSER_RUNTIME_DIR=directory), self.assertRaises(TaskError):
                prepare_browser(self.execution, self.context, self.registry)
        self.registry.enable_browser.assert_not_called()

    def test_missing_or_wrong_host_identity_cannot_form_handoff(self):
        with patch('task_start.browser.which', return_value='/helper'):
            for value in ('not JSON', '{}', json.dumps(dict(root='/tmp/checkout', agent='codex', instance='DEV-7-I1', actor='DEV-7-I1', session='default', runtime_dir='/tmp/rt'))):
                with patch('task_start.browser.run_browser', return_value=Mock(returncode=0, stdout=value)), self.assertRaisesRegex(TaskError, 'unverified identity'):
                    prepare_browser(self.execution, self.context, self.registry)

    def test_cli_opt_in_is_explicit_and_nonexecution_controls_cannot_launch(self):
        for command in ('start', 'loop', 'review'):
            self.assertFalse(parser().parse_args([command, 'DEV-7']).browser)
            self.assertTrue(parser().parse_args([command, 'DEV-7', '--browser']).browser)
        with self.assertRaisesRegex(TaskError, '--no-agent'):
            start('DEV-7', no_agent=True, browser=True)
        for action in ('status', 'pause', 'abort', 'continue'):
            with self.assertRaisesRegex(TaskError, 'controls preserve'):
                loop('DEV-7', action=action, browser=True)
