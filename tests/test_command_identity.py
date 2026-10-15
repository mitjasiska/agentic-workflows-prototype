"""Command identity is scoped to one invocation; user/recorded text stays literal."""

import io
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
import unittest
from unittest.mock import patch

from task_start import TaskError, cli
from task_start.cleanup import cleanup_recovery
from task_start.command import bind_command, command_name, command_scope
from task_start.publication_rebase import review_required
from task_start.publish import prepare_metadata
from task_start.status import render_loop_report


class CommandIdentityTests(unittest.TestCase):
    def test_nested_scope_restores_identity_even_on_error(self):
        self.assertEqual(command_name(), 'task')
        with command_scope('awp'):
            self.assertEqual(command_name(), 'awp')
            with self.assertRaises(TaskError), command_scope('task'):
                self.assertEqual(command_name(), 'task')
                raise TaskError('synthetic')
            self.assertEqual(command_name(), 'awp')
        self.assertEqual(command_name(), 'task')

    def test_cli_restores_identity_after_success_error_and_parser_exit(self):
        for name in ('awp', 'task', 'awp'):
            with patch('task_start.cli.inspect_contexts', side_effect=lambda *a, **kw: command_name()), \
                    patch('sys.stdout', new_callable=io.StringIO) as output:
                self.assertEqual(cli.main(['contexts'], prog=name), 0)
                self.assertEqual(output.getvalue(), name + '\n')
            with patch('task_start.cli.publish', side_effect=lambda *a: prepare_metadata(None, {})), \
                    patch('sys.stderr', new_callable=io.StringIO) as output:
                self.assertEqual(cli.main(['pr', 'DEV-16'], prog=name), 1)
                self.assertIn(f'{name}: Clean review lacks public publishing metadata; run {name} review again',
                              output.getvalue())
            with patch('sys.stderr', new_callable=io.StringIO), self.assertRaises(SystemExit):
                cli.main(['unknown'], prog=name)
            self.assertEqual(command_name(), 'task')

    def test_arbitrary_error_and_path_text_are_not_rewritten(self):
        literal = 'task review remains literal in user data and /tmp/task loop/paths'
        with patch('task_start.cli.publish', side_effect=TaskError(literal)), \
                patch('sys.stderr', new_callable=io.StringIO) as output:
            self.assertEqual(cli.main(['pr', 'DEV-16'], prog='awp'), 1)
        self.assertEqual(output.getvalue(), 'awp: ' + literal + '\n')
        with command_scope('awp'):
            message = cleanup_recovery('DEV-16', Path('/fixture/task review/repo'))
        self.assertIn('/fixture/task review/repo', message)
        self.assertIn('awp cleanup DEV-16 --force', message)

    def test_bound_readers_preserve_separate_identities_across_threads(self):
        readers = []
        for name in ('awp', 'task'):
            with command_scope(name):
                readers.append(bind_command(review_required))
        def read(reader):
            with command_scope('outer'):
                result = reader()
                self.assertEqual(command_name(), 'outer')
                return result
        with ThreadPoolExecutor(max_workers=2) as pool:
            values = list(pool.map(read, readers * 4))
        for name, result in zip(('awp', 'task') * 4, values):
            self.assertIn(f'independent {name} review before rerunning {name} pr', result)
        with command_scope('awp'):
            def fail():
                raise TaskError(command_name())
            reader = bind_command(fail)
        with self.assertRaisesRegex(TaskError, 'awp'):
            reader()
        self.assertEqual(command_name(), 'task')

    def test_failure_report_filter_accepts_legacy_and_current_stored_command_spellings(self):
        for saved_name in ('task', 'awl', 'awp'):
            for current_name in ('task', 'awp'):
                reason = (f'PRIVATE exception. No initial handoff was claimed; rerun {saved_name} loop '
                          'DEV-16 to revalidate and reuse the original reservation')
                with command_scope(current_name), self.assertRaisesRegex(TaskError, 'Unfiltered controller failure'):
                    render_loop_report(dict(reason=reason), False)


if __name__ == '__main__':
    unittest.main()
