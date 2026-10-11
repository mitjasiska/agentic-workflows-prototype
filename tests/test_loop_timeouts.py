"""Advisory thresholds with disposable checkpoints, clocks, and provider output."""

import copy
from dataclasses import replace
import io
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from task_start import cli
from task_start.loop import loop
from task_start.pass_delivery import PassDelivery
from task_start.publication_state import PublicationStore
import pass_delivery_fixture
import test_loop as loops
import test_loop_bootstrap as bootstrap
import test_loop_recovery as recovery


class AdvisoryTimeoutTests(unittest.TestCase):
    command = loops.LoopIntegrationTests.command
    pane_command = loops.LoopIntegrationTests.pane_command
    worktrees = loops.LoopIntegrationTests.worktrees
    select_adapter = loops.LoopIntegrationTests.select_adapter
    setUp = loops.LoopIntegrationTests.setUp
    interrupt = recovery.RecoveryTests.interrupt

    def clock(self, case=None):
        case = case or self
        clock = SimpleNamespace(now=2000000000.0)
        clock.time = clock.monotonic = lambda: clock.now
        def sleep(seconds):
            clock.now += 1
        clock.sleep = sleep
        for module in ('pass_delivery', 'loop', 'implementation_pass', 'review'):
            case.enterContext(patch(f'task_start.{module}.time', clock))
        return clock

    def check_late_flow(self, case, initial):
        clock = self.clock(case)
        case.local = replace(case.local, loop_timeout=2)
        case.review_results = [loops.findings(loops.finding())]
        started, collected, observed = [], [], []
        start = PassDelivery.started
        def record_start(delivery):
            start(delivery)
            started.append(copy.deepcopy(delivery.state))
        def provider(execution, reference, receipt, completion):
            state = case.store.read()[0]
            delivery = state['delivery']
            if clock.now < delivery['deadline'] + 2:
                status = loop('DEV-7', action='status')
                observed.append(status.data['pass_timing'])
                case.assertEqual(status.state, 'running')
                return False  # Even idle + JSON is not a completed provider turn.
            proof = Path(delivery['directory']) / 'completion.json'
            if not proof.exists():
                prompt = case.prompts[-1] if execution.purpose == 'review' else case.impl_prompts[-1]
                pass_delivery_fixture.complete_prompt(prompt)
            if completion is not None:
                case.assertEqual(completion.split()[1], state['active_pass']['pass_id'])
            return True
        def collect(result):
            state = case.store.read()[0]
            proof = json.loads((Path(state['delivery']['directory']) / 'completion.json').read_text())
            collected.append(result['pass_id'])
            case.assertEqual(proof['run_id'], state['run_id'])
            case.assertEqual(proof['pass_id'], result['pass_id'])
            case.assertGreater(proof['completed_at'], state['delivery']['deadline'])
        with patch.object(PassDelivery, 'started', record_start), \
                patch.object(pass_delivery_fixture, 'complete'), \
                patch.object(case.implementer, 'observe_delivery', side_effect=provider), \
                patch.object(case.adapter, 'observe_delivery', side_effect=provider), \
                patch('task_start.cli.loop', side_effect=lambda *args, **kwargs:
                      loop(*args, on_pass_result=collect, **kwargs)), \
                patch('sys.stderr', new_callable=io.StringIO) as warnings, \
                patch('sys.stdout', new_callable=io.StringIO) as output:
            # Config selection and machine-readable output use the real CLI.
            code = cli.main(['loop', 'DEV-7', '--json'])
            self.assertEqual(code, 0, output.getvalue())
            result = json.loads(output.getvalue())
        state = case.store.read()[0]
        phases = ['initial_implementation' if initial else 'implementation', 'review', 'fixes', 'rereview']
        case.assertEqual([r['phase'] for r in state['records']], phases)
        case.assertEqual(collected, [r['pass_id'] for r in state['records']])
        case.assertEqual(result['state'], 'clean')
        case.assertEqual(state['timeout'], 2)
        case.assertEqual((len(case.impl_prompts), len(case.prompts)), (2, 2))
        case.assertEqual(case.recreated, [False])
        case.assertEqual(len(case.registry.list()), 2)
        case.assertTrue(any(v['overdue'] for v in observed))
        case.assertTrue(any(not v['overdue'] for v in observed))
        notices = warnings.getvalue().splitlines()
        case.assertEqual(len(notices), 4, warnings.getvalue())
        for saved, notice in zip(started, notices):
            case.assertIn(saved['active_pass']['pass_id'], notice)
            case.assertIn(saved['active_pass']['phase'], notice)
            case.assertIn('DEV-7', notice)
            case.assertIn('elapsed 2s', notice)
            case.assertIn('original agent turn', notice)
            for choice in ('Leave it working', '--status', '--pause-after-current', 'Ctrl+C', '--abort', '--force'):
                case.assertIn(choice, notice)
        # No reacceptance or redelivery when the completed loop is inspected/repeated.
        case.assertEqual(loop('DEV-7', on_pass_result=lambda _: case.fail('replayed')).data, result)
        case.assertEqual(PublicationStore(case.path).read()['acceptance']['pass_id'], state['records'][-1]['pass_id'])

    def test_implementation_review_fixes_and_rereview_outlive_configured_threshold(self):
        self.check_late_flow(self, initial=False)

    def test_fresh_initial_implementation_outlives_threshold_and_routes_all_roles(self):
        case = bootstrap.BootstrapLoopTests()
        case.setUp()
        try:
            self.check_late_flow(case, initial=True)
        finally:
            case.doCleanups()

    def test_on_time_completion_has_no_warning_and_default_is_two_hours(self):
        with patch('sys.stderr', new_callable=io.StringIO) as warnings:
            result = loop('DEV-7')
        self.assertEqual(result.state, 'clean', result.render())
        self.assertEqual(self.store.read()[0]['timeout'], 7200)
        self.assertIsNone(result.data['pass_timing'])
        self.assertNotIn('warning threshold', warnings.getvalue())

    def test_dev90_shaped_late_seal_recovers_legacy_escalation_without_rewriting_proof(self):
        for initial in (False, True):
            with self.subTest(checkpoint_version=3 if initial else 1):
                case = bootstrap.BootstrapLoopTests() if initial else recovery.RecoveryTests()
                case.setUp()
                try:
                    clock = self.clock(case)
                    complete = pass_delivery_fixture.complete
                    def late_complete(execution):
                        clock.now += 1800 + 21 * 60 + 8
                        return complete(execution)
                    def stop(*args):
                        raise KeyboardInterrupt()
                    case.on_implementation = stop
                    with patch.object(pass_delivery_fixture, 'complete', side_effect=late_complete):
                        case.assertEqual(loop('DEV-7', timeout=1800).state, 'interrupted')
                    case.on_implementation = None
                    before = case.store.read()[0]
                    before.update(status='escalated', reason='Original hard timeout expired')
                    case.store.save(before)  # Synthetic older controller checkpoint only.
                    directory = Path(before['delivery']['directory'])
                    original = {name: (directory / name).read_bytes() for name in ('result.json', 'completion.json')}
                    proof = json.loads(original['completion.json'])
                    case.assertEqual(proof['completed_at'] - before['delivery']['deadline'], 1268)
                    seen = []
                    def collected(result):
                        seen.append(result['pass_id'])
                        if result['pass_id'] == before['active_pass']['pass_id']:
                            for name, raw in original.items():
                                case.assertEqual((directory / name).read_bytes(), raw)
                            case.assertEqual(case.store.read()[0]['delivery'], before['delivery'])
                    with patch('sys.stderr', new_callable=io.StringIO):
                        result = loop('DEV-7', on_pass_result=collected)
                    case.assertEqual(result.state, 'clean', result.render())
                    case.assertEqual(seen.count(before['active_pass']['pass_id']), 1)
                    case.assertEqual((len(case.impl_prompts), len(case.prompts)), (1, 1))
                    case.assertEqual(case.store.read()[0]['run_id'], before['run_id'])
                    case.assertFalse(directory.exists())
                    case.assertEqual(loop('DEV-7', on_pass_result=lambda _: case.fail('replayed')).data, result.data)
                finally:
                    case.doCleanups()

    def test_restart_keeps_original_threshold_warns_once_and_pause_collects_late_result(self):
        clock = self.clock()
        before = self.interrupt('implementation')
        directory = Path(before['delivery']['directory'])
        # Model an unfinished original turn, not a newly delivered pass.
        (directory / 'completion.json').unlink()
        clock.now = before['delivery']['deadline'] + 1268
        polls = []
        def stopped_wait(seconds):
            polls.append(seconds)
            clock.now += 1
            if len(polls) == 3:
                raise KeyboardInterrupt()
        clock.sleep = stopped_wait
        with patch.object(self.implementer, 'observe_delivery', return_value=False), \
                patch('sys.stderr', new_callable=io.StringIO) as warnings:
            result = loop('DEV-7')
        self.assertEqual(result.state, 'interrupted')
        self.assertEqual(len(polls), 3)
        self.assertEqual(warnings.getvalue().count('warning threshold'), 1)
        self.assertEqual(self.store.read()[0]['delivery'], before['delivery'])
        with patch('task_start.loop.load_local', side_effect=AssertionError('status must be read-only')), \
                patch('sys.stdout', new_callable=io.StringIO) as output:
            self.assertEqual(cli.main(['loop', 'DEV-7', '--status', '--json']), 0)
        timing = json.loads(output.getvalue())['pass_timing']
        self.assertEqual(timing, dict(threshold_seconds=10, warning_at=before['delivery']['deadline'], overdue=True))
        self.assertIn('exceeded; advisory only', loop('DEV-7', action='status').render())
        self.assertEqual(loop('DEV-7', action='pause').state, 'interrupted')
        pass_delivery_fixture.complete_prompt(self.impl_prompts[0])
        with patch('sys.stderr', new_callable=io.StringIO) as warnings:
            result = loop('DEV-7')
        self.assertEqual(result.state, 'paused', result.render())
        self.assertEqual(warnings.getvalue().count('warning threshold'), 1)
        self.assertEqual((len(self.impl_prompts), len(self.prompts)), (1, 0))
        self.assertEqual(self.store.read()[0]['records'][0]['pass_id'], before['active_pass']['pass_id'])
        self.assertEqual(loop('DEV-7', action='continue').state, 'clean')
        self.assertEqual((len(self.impl_prompts), len(self.prompts)), (1, 1))

    def test_missing_output_and_unknown_runtime_remain_pending_past_threshold(self):
        clock = self.clock()
        before = self.interrupt('implementation')
        directory = Path(before['delivery']['directory'])
        (directory / 'result.json').unlink()
        clock.now = before['delivery']['deadline'] + 1
        waits = []
        def stop(seconds):
            waits.append(seconds)
            clock.now += 1
            if len(waits) == 3:
                raise KeyboardInterrupt()
        clock.sleep = stop
        # idle without output and unknown status both remain pending.
        for status in ('idle', 'unknown'):
            waits.clear()
            with patch.object(self.implementer, 'status', return_value=status), \
                    patch('sys.stderr', new_callable=io.StringIO) as warnings:
                result = loop('DEV-7')
            self.assertEqual(result.state, 'interrupted', result.render())
            self.assertEqual(len(waits), 3)
            self.assertEqual(warnings.getvalue().count('warning threshold'), 1)
            self.assertEqual(result.data['active_pass'], before['active_pass'])
            self.assertEqual((len(self.impl_prompts), len(self.prompts)), (1, 0))
            self.assertIsNone(PublicationStore(self.path).read()['acceptance'])

    def test_invalid_completion_times_still_refuse_even_after_threshold(self):
        before = self.interrupt('implementation')
        proof_path = Path(before['delivery']['directory']) / 'completion.json'
        proof = json.loads(proof_path.read_text())
        for invalid in (0, -1, True, 'late', float('inf'), float('nan')):
            with self.subTest(completed_at=invalid):
                proof_path.write_text(json.dumps(dict(proof, completed_at=invalid)))
                with patch('task_start.pass_delivery.time.time', return_value=before['delivery']['deadline'] + 1268), \
                        patch('sys.stderr', new_callable=io.StringIO):
                    result = loop('DEV-7')
                self.assertEqual(result.state, 'escalated')
                self.assertIn('seal is invalid', result.data['reason'])
                self.assertEqual(result.data['active_pass'], before['active_pass'])
                self.assertEqual((len(self.impl_prompts), len(self.prompts)), (1, 0))
