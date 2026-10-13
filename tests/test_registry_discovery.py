"""Private registry discovery uses disposable Git identities, never user config."""

import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from task_start import TaskError, config


ROOT = Path(__file__).resolve().parents[1]
MAPPING = '[projects.synthetic]\nlinear_project="Synthetic Project"\nrepo_name="example-repo"\nbase_branch="main"\n'


class RegistryDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / 'CLI café'
        self.repo.mkdir()
        self.git(self.repo, 'init', '-q', '-b', 'main')
        self.git(self.repo, 'config', 'user.email', 'test@example.invalid')
        self.git(self.repo, 'config', 'user.name', 'Test')
        package = self.repo / 'task_start'
        package.mkdir()
        for name in ('__init__.py', 'config.py'):
            shutil.copyfile(ROOT / 'task_start' / name, package / name)
        (self.repo / '.gitignore').write_text('config/projects.toml\n__pycache__/\n')
        self.git(self.repo, 'add', '.')
        self.git(self.repo, 'commit', '-qm', 'Fixture')
        self.worktree = self.root / 'linked task'
        self.git(self.repo, 'worktree', 'add', '-qb', 'dev-7', str(self.worktree))
        (self.repo / 'config').mkdir()
        self.registry = self.repo / 'config/projects.toml'
        self.registry.write_text(MAPPING)
        self.enterContext(patch.object(config, 'REGISTRY', self.worktree / 'config/projects.toml'))

    def git(self, root, *args):
        result = subprocess.run(['git', '-C', str(root), *args], capture_output=True, check=True,
                                env={k: v for k, v in os.environ.items() if not k.startswith('GIT_')})
        return os.fsdecode(result.stdout)

    def test_permanent_and_linked_source_resolve_same_registry_from_any_cwd(self):
        for source in (self.repo, self.worktree):
            with self.subTest(source=source), patch.object(config, 'REGISTRY', source / 'config/projects.toml'):
                self.assertEqual(config.default_project_registry(), self.registry)
                self.assertEqual(config.load_projects(), [config.Project('Synthetic Project', 'example-repo', 'main')])
        for source, cwd in ((self.repo, self.worktree), (self.worktree, self.root)):
            env = dict(os.environ, PYTHONPATH=str(source))
            subprocess.run([sys.executable, '-c', 'from task_start.config import load_projects; '
                            'assert load_projects()[0].linear_project == "Synthetic Project"'],
                           cwd=cwd, env=env, check=True, capture_output=True)
        self.assertFalse((self.worktree / 'config/projects.toml').exists())

    def test_inherited_git_selectors_cannot_select_another_repository(self):
        with patch.dict(os.environ, {'GIT_DIR': str(self.root / 'wrong'), 'GIT_WORK_TREE': '/wrong',
                                   'GIT_COMMON_DIR': '/wrong', 'GIT_CONFIG_COUNT': '1',
                                   'GIT_CONFIG_KEY_0': 'core.worktree', 'GIT_CONFIG_VALUE_0': '/wrong'}):
            self.assertEqual(config.default_project_registry(), self.registry)
            self.assertEqual(config.load_projects()[0].linear_project, 'Synthetic Project')

    def test_missing_permanent_registry_does_not_adopt_worktree_or_example(self):
        self.registry.unlink()
        (self.repo / 'config/projects.example.toml').write_text(MAPPING)
        with self.assertRaisesRegex(TaskError, 'Config missing'):
            config.load_projects()
        (self.worktree / 'config').mkdir()
        (self.worktree / 'config/projects.toml').write_text(MAPPING)
        with self.assertRaisesRegex(TaskError, 'competing'):
            config.load_projects()

    def test_competing_worktree_registry_is_uncertain_even_when_identical(self):
        (self.worktree / 'config').mkdir()
        copy = self.worktree / 'config/projects.toml'
        for contents in (MAPPING, MAPPING.replace('Synthetic Project', 'Wrong project')):
            copy.write_text(contents)
            with self.assertRaisesRegex(TaskError, 'competing'):
                config.load_projects()

    def test_malformed_duplicate_and_example_registry_are_not_fallback_candidates(self):
        for contents in ('invalid = [', MAPPING + '\n[projects.other]\nlinear_project="Synthetic Project"\nrepo_name="other"\nbase_branch="main"', 'example_only=true\n' + MAPPING):
            self.registry.write_text(contents)
            with self.assertRaises(TaskError):
                config.load_projects()

    def test_symlinked_registry_is_refused(self):
        self.registry.unlink()
        outside = self.root / 'outside.toml'
        outside.write_text(MAPPING)
        self.registry.symlink_to(outside)
        with self.assertRaisesRegex(TaskError, 'redirect'):
            config.load_projects()
        self.registry.unlink()
        self.registry.symlink_to(self.registry.name)
        with self.assertRaises(TaskError):
            config.load_projects()

    def test_broken_or_unregistered_worktree_identity_is_not_guessed(self):
        marker = self.worktree / '.git'
        original = marker.read_text()
        marker.write_text('gitdir: /nonexistent/awl-registry-test\n')
        with self.assertRaisesRegex(TaskError, 'verify'):
            config.load_projects()
        marker.write_text(original)
        private = Path(self.git(self.worktree, 'rev-parse', '--absolute-git-dir').strip())
        (private / 'gitdir').write_text(str(self.root / 'different/.git'))
        with self.assertRaisesRegex(TaskError, 'registration'):
            config.load_projects()

    def test_duplicate_registration_or_foreign_main_checkout_is_refused(self):
        other = self.root / 'unrelated'
        subprocess.run(['git', 'clone', '-q', str(self.repo), str(other)], check=True, capture_output=True)
        original_run = subprocess.run
        for conflicting_main in (False, True):
            def run(args, **kwargs):
                result = original_run(args, **kwargs)
                if 'worktree' in args and 'list' in args:
                    records = result.stdout.split(b'\0\0')
                    if conflicting_main:
                        records[0] = records[0].replace(os.fsencode(self.repo), os.fsencode(other))
                    else:
                        records.append(next(row for row in records if os.fsencode(self.worktree) in row))
                    result.stdout = b'\0\0'.join(records)
                return result
            with self.subTest(conflicting_main=conflicting_main), patch.object(config.subprocess, 'run', side_effect=run), \
                    patch.object(config, 'read_toml', side_effect=AssertionError('Untrusted registry must not be read')):
                with self.assertRaises(TaskError):
                    config.load_projects()

    def test_separate_git_directory_without_proven_main_identity_is_refused(self):
        other = self.root / 'separate'
        metadata = self.root / 'metadata'
        subprocess.run(['git', 'clone', '-q', '--separate-git-dir', str(metadata), str(self.repo), str(other)], check=True, capture_output=True)
        linked = self.root / 'separate-linked'
        self.git(other, 'worktree', 'add', '-qb', 'dev-8', str(linked))
        (other / 'config').mkdir()
        (other / 'config/projects.toml').write_text(MAPPING)
        with patch.object(config, 'REGISTRY', linked / 'config/projects.toml'):
            # Without a main-worktree location in Git's metadata, common-dir's
            # parent is not proof of the real checkout. Do not guess it.
            with self.assertRaises(TaskError):
                config.default_project_registry()

    def test_non_git_installation_never_searches_parent_or_cwd(self):
        installation = self.repo / 'nested-installation'
        installation.mkdir()
        with patch.object(config, 'REGISTRY', installation / 'config/projects.toml'):
            self.assertEqual(config.default_project_registry(), installation / 'config/projects.toml')
            with self.assertRaisesRegex(TaskError, 'Config missing'):
                config.load_projects()

    def test_explicit_registry_path_remains_supported(self):
        explicit = self.root / 'explicit.toml'
        explicit.write_text(MAPPING)
        with patch.object(config, 'default_project_registry', side_effect=AssertionError('Must honor explicit path')):
            self.assertEqual(config.load_projects(explicit)[0].linear_project, 'Synthetic Project')

    def test_discovery_failures_are_safe_and_do_not_echo_command_output(self):
        for failure in (OSError('secret'), subprocess.TimeoutExpired('git', 10)):
            with patch.object(config.subprocess, 'run', side_effect=failure), self.assertRaises(TaskError) as caught:
                config.load_projects()
            self.assertNotIn('secret', str(caught.exception))
        with patch.object(config.subprocess, 'run', return_value=subprocess.CompletedProcess([], 1, b'', b'private content')), self.assertRaises(TaskError) as caught:
            config.load_projects()
        self.assertNotIn('private content', str(caught.exception))


if __name__ == '__main__':
    unittest.main()
