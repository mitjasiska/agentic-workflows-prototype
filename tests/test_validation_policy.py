"""Validation policy parsing and semantic handoffs; no live agent execution."""

from dataclasses import replace
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from task_start import TaskError
from task_start.agent import AgentOptions
from task_start.config import ReviewValidationConfig, load_local
from task_start.handoff import implementation_handoff, review_handoff
from task_start.implementation_pass import parse_implementation
from task_start.review_result import parse_verdict, pending_review_checks
from task_start.workspace import Workspace
from test_task_start import ISSUE


class ValidationPolicyTests(unittest.TestCase):
    def test_toml_defaults_and_documented_strategies(self):
        for section, expected in (
            ('', 'focused_first'),
            ('[review]', 'focused_first'),
            ('[review.validation]', 'focused_first'),
            ('[review.validation]\nstrategy = "focused_first"', 'focused_first'),
            ('[review.validation]\nstrategy = "exhaustive"', 'exhaustive'),
        ):
            with self.subTest(section=section), tempfile.TemporaryDirectory() as temp:
                path = Path(temp) / 'config.toml'
                path.write_text(f'projects_root = {json.dumps(temp)}\n[linear]\napi_key = "test-placeholder"\n{section}\n')
                self.assertEqual(load_local(path).review_validation.strategy, expected)

    def test_invalid_policy_rejected_without_echoing_config(self):
        policies = [None, False, '', [], {'strategy': 'exhaustive'}, {'unknown': {}}]
        policies += [dict(validation=value) for value in (None, False, '', [], {'unknown': 'value'})]
        policies += [dict(validation=dict(strategy=value))
                     for value in ('', 'FOCUSED_FIRST', 'unknown', 1, False, [], {}, None)]
        for policy in policies:
            with self.subTest(policy=policy), patch('task_start.config.read_toml', return_value=dict(
                    projects_root='/projects', linear=dict(api_key='test-placeholder'), review=policy)):
                with self.assertRaisesRegex(TaskError, 'review') as caught:
                    load_local()
                self.assertNotIn('test-placeholder', str(caught.exception))
                # Match existing execution-setting bypass for workspace-only operations.
                self.assertEqual(load_local(no_agent=True).review_validation.strategy, 'focused_first')

    def test_implementation_preserves_requirements_with_thorough_tests_and_focused_execution(self):
        for requirement in self.requirements():
            with self.subTest(requirement=requirement):
                prompt = implementation_handoff(replace(ISSUE, description=requirement), self.workspace())
                self.assertIn(f'Task:\n{requirement}\n\nWorkflow-owned implementation instructions:', prompt)
                instructions = prompt.split('Workflow-owned implementation instructions:\n')[1]
                for text in ('thorough automated tests', 'meaningful assertions', 'failure paths, and invariants',
                             'do not reduce coverage', 'arbitrary test-count targets or caps',
                             'test-first / red-green-refactor when useful; TDD is optional',
                             'new or changed tests', 'syntax/lint/diff checks',
                             'Do not routinely run the full suite merely to end the implementation pass',
                             'Explicit Linear issue and repository validation requirements remain authoritative',
                             'Complete checks explicitly required locally or for this pass',
                             'Merge-time requirements may be satisfied externally where permitted',
                             'never assume CI exists', 'pending final validation to the human',
                             'fix the complete batch and rerun relevant tests',
                             'Never report an unrun check as passed', 'source and applicable revision'):
                    self.assertIn(text, instructions)
                self.assertNotIn('complete required final validation before reporting ready', instructions)

    @staticmethod
    def requirements():
        return ('Cover retry failures and the configured limit.',
                'All tests must pass. Full regression must pass before merge.',
                'This agent must run the full suite locally before handoff.\n'
                'Run the full suite on every review pass. Use strict red-green-refactor.')

    @staticmethod
    def workspace():
        return Workspace('dev-7-task', Path('/task'), 'workspace', 'tab', 'pane', 'existing')

    def test_fresh_and_resumed_review_policy_preserves_full_inspection_and_evidence(self):
        for strategy in ('focused_first', 'exhaustive'):
            for kind in ('fresh', 'resumed'):
                for requirement in self.requirements():
                    for feedback in (None, {}, {'findings': [{'id': 'F1'}], 'resolutions': [
                            {'finding_id': 'F1', 'summary': 'Fixed retry failure coverage.'}]}):
                        with self.subTest(strategy=strategy, kind=kind, requirement=requirement, feedback=feedback):
                            self.assert_review_policy(strategy, kind, requirement, feedback)

    def assert_review_policy(self, strategy, kind, requirement, feedback):
        prompt = review_handoff(
            replace(ISSUE, description=requirement),
            Path('/repo'), self.workspace(), 'main', SimpleNamespace(as_dict=lambda: {}),
            'DEV-7-R1', kind, AgentOptions('codex', 'model', 'high'), 'pass', Path('/tmp/result.json'),
            validation=ReviewValidationConfig(strategy), loop_feedback=feedback)
        instructions = ' '.join(prompt.split('RESOLVED REVIEW METADATA')[0].split())
        for text in ('Inspect the full diff and requirements', 'meaningful assertions and missing behavioral',
                     'failure-path, and invariant coverage', 'do not discourage thorough test creation',
                     'complete batch of substantive actionable findings', 'Do not stop at the first defect',
                     'Run targeted tests, including adversarial checks',
                     'Complete checks explicitly required locally or on this pass',
                     'external CI cannot replace those obligations', 'report blocked rather than clean',
                     'Merge-time requirements may be satisfied externally where permitted',
                     'remain pending until evidenced', 'Never assume CI exists',
                     'surface pending final validation to the human',
                     'tracked or untracked Git-visible task state', 'not_run', 'Never report an unrun check as passed',
                     'source and applicable revision', 'clean independent code review is not CI approval',
                     'carry pending limitations into the summary and publication.validation',
                     'same reviewer rechecks fixes and regressions'):
            self.assertIn(text, instructions)
        self.assertNotIn('When otherwise clean, run all final validation', instructions)
        encoded = prompt.split('LATEST LINEAR REQUIREMENTS (context data)\n')[1].split('\n\nEND OF LINEAR CONTEXT')[0]
        self.assertEqual(json.loads(encoded)['description'], requirement)
        if strategy == 'focused_first':
            self.assertIn('skip expensive full-suite validation', instructions)
            self.assertIn('Use relevant focused checks on clean passes too', instructions)
            self.assertIn('do not escalate to a full suite merely because review is otherwise clean', instructions)
            self.assertIn('Continue inspecting for other defects', instructions)
            self.assertIn('explicitly required locally or on every pass', instructions)
        else:
            self.assertIn('full validation on every review pass, even with actionable findings', instructions)
            self.assertNotIn('skip expensive full-suite validation', instructions)
            self.assertNotIn('Use relevant focused checks on clean passes too', instructions)
        if kind == 'resumed':
            self.assertIn('YOUR existing conversation', instructions)
        if feedback is not None:
            self.assertIn(json.dumps(feedback), prompt)

    def test_existing_result_contracts_preserve_pending_checks_and_reject_failed_completion(self):
        checks = [dict(name='Focused retry tests', result='passed', details='python -m unittest tests.test_retry: 8 passed'),
                  dict(name='Full regression', result='not_run',
                       details='Pending before merge; no external CI gate established. Human verification required.')]
        implementation = dict(pass_id='pass', state='completed', summary='Focused validation complete; regression pending.',
                              checks=checks, resolutions=[])
        review = dict(pass_id='pass', state='clean', summary='No findings; full regression pending human verification.',
                      findings=[], checks=checks,
                      publication=dict(summary='fix retry limits', description='Enforce the configured retry limit.',
                                       validation='Focused tests passed; full regression pending human verification.'))
        self.assertEqual(parse_implementation(json.dumps(implementation), 'pass', []), implementation)
        self.assertEqual(parse_verdict(json.dumps(review), 'pass'), review)
        for result in ('failed', 'unknown'):
            with self.subTest(result=result):
                checks[1]['result'] = result
                with self.assertRaises(TaskError):
                    parse_implementation(json.dumps(implementation), 'pass', [])
                with self.assertRaises(TaskError):
                    parse_verdict(json.dumps(review), 'pass')

    def test_review_check_scope_is_explicit_and_legacy_checks_still_parse(self):
        check = dict(name='validation', result='not_run', details='Evidence unavailable')
        verdict = dict(pass_id='pass', state='blocked', summary='Blocked', findings=[], checks=[check])
        for scope in (None, 'review', 'merge'):
            with self.subTest(scope=scope):
                verdict['checks'] = [dict(check, scope=scope) if scope else check]
                self.assertEqual(parse_verdict(json.dumps(verdict), 'pass'), verdict)
        for scope in ('unknown', '', False, [], {}, None):
            with self.subTest(invalid_scope=scope), self.assertRaises(TaskError):
                parse_verdict(json.dumps(dict(verdict, checks=[dict(check, scope=scope)])), 'pass')

    def test_only_structured_deferral_can_exempt_legacy_pending_check(self):
        check = dict(name='arbitrary check', result='not_run', details='Deferred before merge')
        verdict = dict(pass_id='pass', state='clean', summary='Clean, merge checks pending', findings=[],
                       checks=[dict(check, scope='merge')])
        self.assertEqual(parse_verdict(json.dumps(verdict), 'pass', required_checks=[check]), verdict)
        # Names and prose alone cannot determine which checks are optional this pass.
        for current in ([], [check], [dict(check, name='new name', scope='merge')]):
            with self.subTest(current=current), self.assertRaises(TaskError):
                parse_verdict(json.dumps(dict(verdict, checks=current)), 'pass', required_checks=[check])

    def test_explicit_review_scope_or_failed_check_cannot_be_demoted_until_passed(self):
        check = dict(name='browser', result='not_run', details='Browser unavailable')
        for saved in (dict(check, scope='review'), dict(check, result='failed'),
                      dict(check, result='failed', scope='merge')):
            with self.subTest(saved=saved):
                deferred = dict(check, scope='merge')
                verdict = dict(pass_id='pass', state='clean', summary='Clean', findings=[], checks=[deferred])
                with self.assertRaises(TaskError):
                    parse_verdict(json.dumps(verdict), 'pass', required_checks=[saved])
                history = [dict(phase='review', state='blocked', checks=[saved]),
                           dict(phase='rereview', state='blocked', checks=[deferred])]
                pending = pending_review_checks(history)
                self.assertEqual(pending, [dict(deferred, scope='review')])
                with self.assertRaises(TaskError):
                    parse_verdict(json.dumps(verdict), 'pass', required_checks=pending)
                passed = dict(check, result='passed', details='Independent validation passed')
                self.assertEqual(parse_verdict(json.dumps(dict(verdict, checks=[passed])), 'pass',
                                              required_checks=pending)['state'], 'clean')

    def test_deferred_checks_stay_visible_without_blocking_review_routing(self):
        deferred = dict(name='full regression', result='not_run', scope='merge', details='Pending human gate')
        history = [dict(phase='review', state='blocked', checks=[deferred])]
        pending = pending_review_checks(history)
        self.assertEqual(pending, [deferred])
        for state, findings in (('clean', []), ('findings', [dict(id='F2', category='implementation',
                severity='high', explanation='A concrete defect', evidence='file:1', requirement='Fix it')])):
            verdict = dict(pass_id='pass', state=state, summary='Regression pending', findings=findings,
                           checks=[deferred])
            with self.subTest(state=state):
                self.assertEqual(parse_verdict(json.dumps(verdict), 'pass', required_checks=pending), verdict)
                self.assertEqual(pending_review_checks(history + [dict(phase='rereview', **verdict)]), [deferred])

    def test_conflicting_scopes_cannot_hide_required_validation(self):
        deferred = dict(name='check', result='not_run', scope='merge', details='Pending')
        required = dict(deferred, scope='review')
        for checks in ([dict(required, result='passed'), deferred],
                       [deferred, dict(required, result='passed')]):
            history = [dict(phase='review', state='blocked', checks=checks)]
            with self.subTest(checks=checks):
                self.assertEqual(pending_review_checks(history), [required])
        verdict = dict(pass_id='pass', state='clean', summary='Clean', findings=[], checks=[required])
        with self.assertRaises(TaskError):
            parse_verdict(json.dumps(verdict), 'pass', required_checks=[deferred])

    def test_pending_required_checks_hold_findings_but_preserve_all_evidence(self):
        browser = dict(name='browser', result='not_run', scope='review', details='Unavailable')
        for category in ('implementation', 'human_decision'):
            for checks in ([], [browser],
                           [dict(browser, result='passed'), browser]):
                with self.subTest(category=category, checks=checks):
                    verdict = dict(pass_id='pass', state='findings', summary='New finding', checks=checks,
                        findings=[dict(id='F2', category=category, severity='high', explanation='Needs correction',
                                       evidence='file:2', requirement='Acceptance')])
                    result = parse_verdict(json.dumps(verdict), 'pass', routing=True, required_checks=[browser])
                    self.assertEqual(result['state'], 'blocked')
                    self.assertTrue(result['summary'].startswith(verdict['summary']))
                    self.assertIn('browser', result['summary'])
                    for key in ('pass_id', 'checks', 'findings'):
                        self.assertEqual(result[key], verdict[key])
        for state in ('blocked', 'failed'):
            verdict = dict(pass_id='pass', state=state, summary='Cannot complete', findings=[], checks=[browser])
            self.assertEqual(parse_verdict(json.dumps(verdict), 'pass', required_checks=[browser]), verdict)

    def test_current_required_checks_hold_clean_and_findings_without_saved_evidence(self):
        saved = dict(name='browser', result='not_run', details='Browser unavailable')
        required = dict(name='integration service', result='not_run', scope='review', details='Service unavailable')
        deferred = dict(name='full regression', result='not_run', scope='merge', details='Pending before merge')
        finding = dict(id='F2', category='implementation', severity='high', explanation='New defect',
                       evidence='file:2', requirement='Acceptance')
        for pending in ([], [saved]):
            for checks in ([required],
                           [dict(required, result='passed'), required], [required, dict(required, result='passed')],
                           [dict(required, result='passed'), dict(required, scope='merge')]):
                checks = [dict(saved, result='passed'), deferred, *checks]
                for state in ('clean', 'findings'):
                    verdict = dict(pass_id='pass', state=state, summary='Current review', checks=checks,
                                   findings=[finding] if state == 'findings' else [])
                    with self.subTest(pending=pending, checks=checks, state=state):
                        if state == 'clean':
                            with self.assertRaises(TaskError):
                                parse_verdict(json.dumps(verdict), 'pass', required_checks=pending)
                        else:
                            parsed = parse_verdict(json.dumps(verdict), 'pass', required_checks=pending)
                            self.assertEqual(parsed['state'], 'blocked')
                            self.assertIn(required['name'], parsed['summary'])
                            self.assertEqual(parsed['findings'], [finding])
                            self.assertEqual(parsed['checks'], checks)
        # The same check can pass independently while the merge deferral remains unrun.
        checks = [dict(saved, result='passed'), dict(required, result='passed'), deferred]
        for state in ('clean', 'findings'):
            verdict = dict(pass_id='pass', state=state, summary='Current review', checks=checks,
                           findings=[finding] if state == 'findings' else [])
            self.assertEqual(parse_verdict(json.dumps(verdict), 'pass', required_checks=[saved]), verdict)

    def test_executed_failures_allow_findings_but_require_passing_clean_review(self):
        finding = dict(id='F1', category='implementation', severity='high', explanation='Code defect',
                       evidence='code.py:1', requirement='Regression coverage')
        for scope in (None, 'review', 'merge'):
            failed = dict(name='focused tests', result='failed', details='Executed tests reproduced the code defect')
            if scope:
                failed['scope'] = scope
            verdict = dict(pass_id='pass', state='findings', summary='Code defect reproduced',
                           findings=[finding], checks=[failed])
            with self.subTest(scope=scope):
                self.assertEqual(parse_verdict(json.dumps(verdict), 'pass', routing=True), verdict)
                pending = pending_review_checks([dict(phase='review', **verdict)])
                self.assertEqual(pending, [dict(failed, scope='review')])
                clean = dict(verdict, state='clean', findings=[])
                for checks in ([], [failed], [dict(failed, result='not_run', scope='merge')]):
                    with self.subTest(clean_checks=checks), self.assertRaises(TaskError):
                        parse_verdict(json.dumps(dict(clean, checks=checks)), 'pass', required_checks=pending)
                passed = dict(failed, result='passed', details='Independent re-review tests passed')
                self.assertEqual(parse_verdict(json.dumps(dict(clean, checks=[passed])), 'pass',
                                              required_checks=pending)['state'], 'clean')
                self.assertEqual(pending_review_checks([dict(phase='review', **verdict),
                    dict(phase='rereview', **dict(clean, checks=[passed]))]), [])

    def test_executed_failure_can_continue_findings_after_unavailable_check_runs(self):
        unavailable = dict(name='browser', result='not_run', scope='review', details='Browser unavailable')
        failed = dict(unavailable, result='failed', details='Browser ran and reproduced an implementation defect')
        finding = dict(id='F2', category='implementation', severity='high', explanation='Browser assertion failed',
                       evidence='code.py:2', requirement='Browser acceptance')
        verdict = dict(pass_id='pass', state='findings', summary='Code defect reproduced',
                       findings=[finding], checks=[failed])
        self.assertEqual(parse_verdict(json.dumps(verdict), 'pass', required_checks=[unavailable]), verdict)
        history = [dict(phase='review', state='blocked', checks=[unavailable]), dict(phase='rereview', **verdict)]
        self.assertEqual(pending_review_checks(history), [failed])

    def test_failed_code_check_does_not_bypass_missing_validation_or_human_decisions(self):
        failed = dict(name='focused tests', result='failed', scope='review', details='Executed tests failed')
        browser = dict(name='browser', result='not_run', scope='review', details='Browser unavailable')
        finding = dict(id='F1', category='implementation', severity='high', explanation='Code defect',
                       evidence='code.py:1', requirement='Acceptance')
        for pending, checks in (([browser], [failed]), ([], [failed, browser]),
                                ([], [dict(failed, result='passed'), failed, dict(failed, result='not_run')])):
            verdict = dict(pass_id='pass', state='findings', summary='Code defect', findings=[finding], checks=checks)
            with self.subTest(pending=pending, checks=checks):
                parsed = parse_verdict(json.dumps(verdict), 'pass', required_checks=pending)
                self.assertEqual(parsed['state'], 'blocked')
                self.assertEqual(parsed['findings'], verdict['findings'])
                self.assertEqual(parsed['checks'], checks)
        verdict = dict(pass_id='pass', state='findings', summary='Human decision required',
                       findings=[dict(finding, category='human_decision')], checks=[failed])
        self.assertEqual(parse_verdict(json.dumps(verdict), 'pass', routing=True), verdict)

    def test_check_scopes_and_retry_limitations_are_in_review_handoff(self):
        prompt = review_handoff(ISSUE, Path('/repo'), self.workspace(), 'main',
            SimpleNamespace(as_dict=lambda: {}), 'DEV-7-R1', 'resumed', AgentOptions('codex', 'model', 'high'),
            'pass', Path('/tmp/result.json'), loop_feedback={'pending_checks': []})
        for text in ('scope to review', 'optional scope (review/merge)',
                     'even when new findings exist', 'For legacy checks without scope',
                     'never demote a saved scope=review check', 'summary and publication.validation',
                     'Executed failing code checks support findings for fixes',
                     'Unavailable mandatory', 'independent passing evidence is still required before clean review'):
            self.assertIn(text, prompt)


if __name__ == '__main__':
    unittest.main()
