"""Offline builds and real installs; all sources, homes and environments are disposable.

Provision setuptools>=77 and Python venv/ensurepip before running these checks.
No live workflow configuration, credentials, agents or Herdr instances are used.
"""

import configparser
from email.parser import BytesParser
import http.client
import importlib.metadata
import json
import os
from pathlib import Path
import selectors
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import unittest
from urllib.parse import urlsplit
import zipfile


ROOT = Path(__file__).resolve().parents[1]
MAPPING = '[projects.fixture]\nlinear_project="Synthetic Project"\nrepo_name="example-repo"\nbase_branch="main"\n'
PRIVATE = 'PRIVATE_PACKAGING_SENTINEL_9a86c1'
COMMANDS = ('start', 'loop', 'status', 'dashboard', 'pr', 'review', 'integrate', 'cleanup', 'contexts')


class PackagingTests(unittest.TestCase):
    @classmethod
    def run_command(cls, argv, *, cwd=None, env=None):
        result = subprocess.run(list(map(str, argv)), cwd=cwd or cls.outside,
                                env=env or cls.build_env, capture_output=True, text=True, timeout=120)
        if result.returncode:
            raise AssertionError(f'{argv!r} exited {result.returncode}\n{result.stdout}\n{result.stderr}')
        return result.stdout

    @classmethod
    def git(cls, source, *args):
        return cls.run_command(['git', '-C', source, *args])

    @classmethod
    def copy_source(cls, target):
        target.mkdir()
        for name in ('pyproject.toml', 'MANIFEST.in', 'README.md', 'LICENSE', 'task'):
            shutil.copy2(ROOT / name, target / name)
        shutil.copytree(ROOT / 'task_start', target / 'task_start',
                        ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
        (target / '.gitignore').write_text('config/\n__pycache__/\n*.egg-info/\nbuild/\n')
        cls.git(target, 'init', '-q', '-b', 'main')
        cls.git(target, 'add', '.')
        cls.git(target, '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid',
                '-c', 'commit.gpgsign=false', 'commit', '-qm', 'Disposable source')
        return target

    @classmethod
    def install(cls, target, requirement, *, editable=False):
        cls.run_command([sys.executable, '-m', 'venv', target])
        # Expose only the already-provisioned build backend during installation.
        # The installed CLI is always exercised without PYTHONPATH afterwards.
        cls.run_command([target / 'bin/python', '-m', 'pip', 'install', '--no-index',
                         '--no-deps', '--no-build-isolation',
                         *(['--editable'] if editable else []), requirement])
        return target

    @classmethod
    def setUpClass(cls):
        try:
            if int(importlib.metadata.version('setuptools').split('.')[0]) < 77:
                raise unittest.SkipTest('Packaging checks require setuptools>=77; see docs/architecture.md')
        except importlib.metadata.PackageNotFoundError:
            raise unittest.SkipTest('Packaging checks require setuptools>=77; see docs/architecture.md')
        import setuptools
        cls.temp = tempfile.TemporaryDirectory(prefix='awl-packaging-')
        cls.addClassCleanup(cls.temp.cleanup)
        cls.root = Path(cls.temp.name)
        cls.outside = cls.root / 'unrelated-cwd'
        cls.outside.mkdir()
        cls.home = cls.root / 'build-home'
        cls.home.mkdir()
        cls.host_files = {}
        for relative in ('.bashrc', '.profile', '.codex/config.toml', '.herdr/config.json',
                         '.agentic-workflows-lite/config.toml', '.agentic-workflows-lite/projects.toml'):
            path = cls.home / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(PRIVATE)
            cls.host_files[path] = path.read_bytes()
        # An allowlist avoids inherited credentials, Python/Git selectors and host settings.
        cls.build_env = dict(PATH='/usr/bin:/bin', HOME=str(cls.home), LANG='C.UTF-8',
                             PYTHONPATH=str(Path(setuptools.__file__).resolve().parent.parent),
                             PIP_CONFIG_FILE=os.devnull, PIP_DISABLE_PIP_VERSION_CHECK='1',
                             PIP_NO_CACHE_DIR='1', GIT_CONFIG_NOSYSTEM='1')
        cls.artifacts = cls.root / 'artifacts'
        cls.artifacts.mkdir()
        source = cls.copy_source(cls.root / 'build-source')
        # Deliberately dirty/private files must not be swept into either artifact.
        for relative in ('config/projects.toml', 'config/local.toml', 'secrets.env',
                         '.env', '.agentic-workflows-lite/config.local.toml',
                         'task_start/config.local.toml', 'task_start/secrets.env',
                         'task_start/web/private.txt', 'skills/create-linear-task/SKILL.md',
                         'plugins/create-linear-task/private.toml', 'tests/test_private.py'):
            path = source / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(PRIVATE + '\n' + str(cls.root / 'private-host-path'))
        cls.run_command([sys.executable, '-c', 'from setuptools.build_meta import build_sdist; '
                         f'build_sdist({str(cls.artifacts)!r})'], cwd=source)
        cls.sdist, = cls.artifacts.glob('*.tar.gz')
        extracted = cls.root / 'sdist-build'
        with tarfile.open(cls.sdist) as archive:
            archive.extractall(extracted, filter='data')
        unpacked, = extracted.iterdir()
        cls.run_command([sys.executable, '-c', 'from setuptools.build_meta import build_wheel; '
                         f'build_wheel({str(cls.artifacts)!r})'], cwd=unpacked)
        cls.wheel, = cls.artifacts.glob('*.whl')
        cls.normal = cls.install(cls.root / 'normal', cls.wheel)
        # Prove normal installs have no dependency on either build source tree.
        shutil.rmtree(source)
        shutil.rmtree(extracted)
        cls.source = cls.copy_source(cls.root / 'editable-source')
        (cls.source / 'config').mkdir()
        (cls.source / 'config/projects.toml').write_text(MAPPING)
        cls.editable = cls.install(cls.root / 'editable', cls.source, editable=True)

    def setUp(self):
        self.home = Path(self.enterContext(tempfile.TemporaryDirectory(dir=self.root)))
        self.private = self.home / '.agentic-workflows-lite'
        self.private.mkdir()
        self.registry = self.private / 'projects.toml'
        self.settings = self.private / 'config.toml'
        self.settings.write_text(f'projects_root={json.dumps(str(self.home / "projects"))}\n'
                                 '[linear]\napi_key="synthetic-placeholder"\n')
        self.bin = self.home / 'bin'
        self.bin.mkdir()
        herdr = self.bin / 'herdr'
        herdr.write_text('#!/bin/sh\nexit 1\n')
        herdr.chmod(0o755)
        self.env = dict(PATH=f'{self.bin}:/usr/bin:/bin', HOME=str(self.home), LANG='C.UTF-8',
                        PYTHONDONTWRITEBYTECODE='1', GIT_CONFIG_NOSYSTEM='1')
        # Neither a caller registry nor caller module may shadow the installed package.
        self.cwd = self.home / 'unrelated'
        (self.cwd / 'config').mkdir(parents=True)
        (self.cwd / 'config/projects.toml').write_text('invalid caller registry')
        (self.cwd / 'task_start.py').write_text('raise AssertionError("caller module imported")')

    def invoke(self, environment, *args):
        return subprocess.run([str(environment / 'bin/awl'), *args], cwd=self.cwd,
                              env=self.env, capture_output=True, text=True, timeout=15)

    def python(self, environment, code):
        # -I also keeps the -c probe's caller directory out of its module search.
        return self.run_command([environment / 'bin/python', '-I', '-c', code],
                                cwd=self.cwd, env=self.env)

    def inject_output_fixture(self):
        fixture = self.home / 'output-fixture'
        fixture.mkdir()
        shutil.copyfile(ROOT / 'tests/command_output_fixture.py', fixture / 'sitecustomize.py')
        self.env['PYTHONPATH'] = str(fixture)

    def output_launchers(self):
        return ((self.normal / 'bin/awl', 'awl'), (self.editable / 'bin/awl', 'awl'),
                (self.source / 'task', 'task'))

    def output_environment(self, name):
        # sitecustomize runs before the source launcher inserts its own directory.
        # Only legacy task gets this source path; installed awl must use its install.
        return (dict(self.env, PYTHONPATH=self.env['PYTHONPATH'] + os.pathsep + str(self.source))
                if name == 'task' else self.env)

    def test_installed_recovery_output_and_legacy_identity_beyond_help(self):
        self.inject_output_fixture()
        for launcher, name in self.output_launchers():
            def invoke(*args):
                return subprocess.run([launcher, *args], cwd=self.cwd, env=self.output_environment(name),
                                      capture_output=True, text=True, timeout=15)
            with self.subTest(launcher=launcher):
                for args in (('status', '--all'), ('status', '--all', '--json')):
                    result = invoke(*args)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertIn(f'{name} loop DEV-16 --continue', result.stdout)
                    self.assertIn(f'{name} loop DEV-17 --abort', result.stdout)
                    self.assertIn(f'{name} cleanup DEV-18 --force', result.stdout)
                    self.assertIn('/fixture/task review/repo', result.stdout)
                    self.assertIn('/fixture/task loop/worktree', result.stdout)
                    if '--json' in args:
                        tasks = json.loads(result.stdout)['tasks']
                        self.assertEqual(tasks[1]['guidance']['command'], f'{name} loop DEV-17')
                        self.assertIn(f'repeating {name} loop.', tasks[1]['guidance']['message'])
                        self.assertEqual(tasks[3]['guidance']['options'][1]['command'],
                                         f'{name} cleanup DEV-19 --force')
                refusal = invoke('pr', 'DEV-16')
                self.assertEqual(refusal.returncode, 1)
                self.assertEqual(refusal.stderr,
                    f'{name}: Clean review lacks public publishing metadata; run {name} review again\n')
                cleanup = invoke('cleanup', 'DEV-16', '--force')
                self.assertEqual(cleanup.returncode, 0, cleanup.stderr)
                self.assertIn(f'Run {name} contexts DEV-16 --all', cleanup.stdout)
                self.assertIn(f'{name} cleanup DEV-16 --force --branch', cleanup.stdout)
                self.assertIn('/fixture/task review/repo', cleanup.stdout)
                warning = invoke('start', 'DEV-16')
                self.assertEqual(warning.returncode, 0, warning.stderr)
                self.assertTrue(warning.stderr.startswith(f'{name}: warning:'))
                self.assertIn('task review is literal user text', warning.stderr)
                self.assertEqual(warning.stdout, 'task review is literal user text\n')
                for args in (('loop', 'DEV-16'), ('loop', 'DEV-16', '--abort')):
                    interrupted = invoke(*args)
                    self.assertEqual(interrupted.returncode, 130)
                    self.assertIn(f'Repeat {name} loop DEV-16', interrupted.stderr)

    def test_installed_dashboard_background_guidance_and_request_report_identity(self):
        self.inject_output_fixture()
        for launcher, name in self.output_launchers():
            with self.subTest(launcher=launcher):
                process = subprocess.Popen([launcher, 'dashboard', '--port', '0'], cwd=self.cwd,
                                           env=self.output_environment(name), stdout=subprocess.PIPE,
                                           stderr=subprocess.PIPE, text=True)
                try:
                    with selectors.DefaultSelector() as selector:
                        selector.register(process.stdout, selectors.EVENT_READ)
                        self.assertTrue(selector.select(timeout=10), 'Dashboard startup timed out')
                    line = process.stdout.readline()
                    self.assertTrue(line.startswith('Dashboard: http://127.0.0.1:'), line)
                    port = urlsplit(line.split()[1]).port
                    def read(route):
                        connection = http.client.HTTPConnection('127.0.0.1', port, timeout=5)
                        try:
                            connection.request('GET', route)
                            response = connection.getresponse()
                            self.assertEqual(response.status, 200)
                            return json.loads(response.read())
                        finally:
                            connection.close()
                    deadline = time.monotonic() + 5
                    while True:
                        snapshot = read('/api/snapshot')
                        if snapshot['projection'] is not None or time.monotonic() >= deadline:
                            break
                        time.sleep(.01)
                    self.assertFalse(snapshot['stale'], snapshot)
                    tasks = snapshot['projection']['tasks']
                    self.assertEqual(tasks[0]['guidance']['command'], f'{name} loop DEV-16 --continue')
                    self.assertEqual(tasks[1]['guidance']['command'], f'{name} loop DEV-17')
                    self.assertEqual(tasks[2]['guidance']['options'][1]['command'],
                                     f'{name} cleanup DEV-18 --force')
                    report = read(f"/api/report?revision={snapshot['revision']}&task=0")
                    self.assertIn(f'independent {name} review before rerunning {name} pr', report['text'])
                finally:
                    process.terminate()
                    try:
                        _, stderr = process.communicate(timeout=10)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        _, stderr = process.communicate()
                        self.fail(f'Dashboard did not stop: {stderr}')
                self.assertEqual(process.returncode, 0, stderr)

    def test_public_help_and_all_subcommands_in_both_modes(self):
        for environment in (self.normal, self.editable):
            for command in ((), *((name,) for name in COMMANDS)):
                with self.subTest(mode=environment.name, command=command):
                    result = self.invoke(environment, *command, '--help')
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertTrue(result.stdout.startswith('usage: awl ' + ' '.join(command)))
                    self.assertNotIn('task loop ISSUE', result.stdout)
                    self.assertNotIn('usage: task', result.stdout)
            result = self.invoke(environment, 'unknown')
            self.assertEqual(result.returncode, 2)
            self.assertIn('awl: error:', result.stderr)
            self.assertFalse((environment / 'bin/task').exists())

    def test_installation_preserves_existing_private_and_shell_settings(self):
        for path, original in self.host_files.items():
            self.assertEqual(path.read_bytes(), original, path)
        self.assertEqual({path for path in (self.root / 'build-home').rglob('*') if path.is_file()},
                         self.host_files.keys())

    def test_normal_imports_and_resources_survive_source_removal(self):
        self.assertFalse((self.root / 'build-source').exists())
        self.assertFalse((self.root / 'sdist-build').exists())
        result = json.loads(self.python(self.normal, '''
import importlib, json, pkgutil
from pathlib import Path
import task_start
from task_start.dashboard_server import load_assets
for module in pkgutil.iter_modules(task_start.__path__):
    importlib.import_module('task_start.' + module.name)
root = Path(task_start.__file__).parent
print(json.dumps(dict(root=str(root), assets=sorted(load_assets()),
                     pi=(root / 'pi_session.mjs').read_text())))
'''))
        self.assertTrue(Path(result['root']).is_relative_to(self.normal))
        self.assertEqual(result['assets'], ['/', '/app.js', '/index.html', '/styles.css'])
        self.assertEqual(result['pi'], (ROOT / 'task_start/pi_session.mjs').read_text())

    def test_registry_and_global_settings_are_deterministic_per_mode(self):
        self.registry.write_text(MAPPING.replace('Synthetic Project', 'Home Project'))
        for environment, expected, project in ((self.normal, self.registry, 'Home Project'),
                (self.editable, self.source / 'config/projects.toml', 'Synthetic Project')):
            with self.subTest(mode=environment.name):
                result = json.loads(self.python(environment, '''
import json
from task_start.config import default_project_registry, load_projects, load_local
local = load_local()
print(json.dumps([str(default_project_registry()), load_projects()[0].linear_project,
                  str(local.projects_root), local.api_key]))
'''))
                self.assertEqual(result, [str(expected), project, str(self.home / 'projects'), 'synthetic-placeholder'])
        self.settings.unlink()
        for environment in (self.normal, self.editable):
            result = self.invoke(environment, 'start', 'DEV-16', '--no-agent')
            self.assertEqual(result.returncode, 1)
            self.assertIn(f'awl: Config missing: {self.settings}', result.stderr)
            self.assertNotIn('Traceback', result.stderr)

    def test_missing_and_invalid_registries_fail_without_fallback_or_leaks(self):
        source_registry = self.source / 'config/projects.toml'
        original = source_registry.read_bytes()
        self.addCleanup(source_registry.write_bytes, original)
        for environment, path in ((self.normal, self.registry), (self.editable, source_registry)):
            with self.subTest(mode=environment.name):
                # For editable mode the home file exists, but must never be a fallback.
                self.registry.write_text(MAPPING)
                path.unlink()
                result = self.invoke(environment, 'start', 'DEV-16', '--no-agent')
                self.assertEqual(result.returncode, 1)
                self.assertIn(f'awl: Config missing: {path}', result.stderr)
                for content, message in ((f'secret="{PRIVATE}"\nbroken=[', 'Cannot read valid TOML'),
                                         ('', 'requires a [projects] table'),
                                         ('example_only=true\n' + MAPPING, 'examples are not runtime mappings'),
                                         (MAPPING.replace('example-repo', '../private'), 'repo_name'),
                                         (MAPPING + MAPPING.replace('projects.fixture', 'projects.duplicate'),
                                          'Duplicate linear_project')):
                    path.write_text(content)
                    result = self.invoke(environment, 'start', 'DEV-16', '--no-agent')
                    self.assertEqual(result.returncode, 1, result.stderr)
                    self.assertIn(message, result.stderr)
                    self.assertNotIn(PRIVATE, result.stderr)
                    self.assertNotIn('Traceback', result.stderr)
                path.write_text(MAPPING)
        # No installed-site registry is created by failed or successful reads.
        self.assertFalse(list(self.normal.rglob('projects.toml')))

    def test_editable_source_changes_and_legacy_launcher_share_current_code(self):
        module = self.source / 'task_start/cli.py'
        original = module.read_text()
        self.addCleanup(module.write_text, original)
        module.write_text(original.replace('Manage Linear task workspaces in Herdr',
                                           'EDITABLE_SOURCE_CHANGE is visible immediately'))
        result = self.invoke(self.editable, '--help')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('EDITABLE_SOURCE_CHANGE', result.stdout)
        self.assertNotIn('EDITABLE_SOURCE_CHANGE', self.invoke(self.normal, '--help').stdout)
        for command in ((), ('loop',), ('status',), ('dashboard',)):
            result = self.run_command([self.source / 'task', *command, '--help'], cwd=self.cwd, env=self.env)
            self.assertTrue(result.startswith('usage: task ' + ' '.join(command)))
        result = self.run_command([self.source / 'task', '--help'], cwd=self.cwd, env=self.env)
        self.assertIn('EDITABLE_SOURCE_CHANGE', result)
        source_registry = self.source / 'config/projects.toml'
        original_registry = source_registry.read_bytes()
        self.addCleanup(source_registry.write_bytes, original_registry)
        source_registry.unlink()
        result = subprocess.run([self.source / 'task', 'start', 'DEV-16', '--no-agent'],
                                cwd=self.cwd, env=self.env, capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 1)
        self.assertIn(f'task: Config missing: {source_registry}', result.stderr)

    def test_editable_linked_worktree_preserves_git_discovery_and_refusals(self):
        linked = self.home / 'linked'
        self.git(self.source, 'worktree', 'add', '-qb', 'linked-packaging', linked)
        environment = self.install(self.home / 'linked-env', linked, editable=True)
        self.registry.write_text(MAPPING.replace('Synthetic Project', 'Wrong Home Project'))
        self.env.update(GIT_DIR='/invalid', GIT_WORK_TREE=str(self.cwd), GIT_CONFIG_COUNT='1',
                        GIT_CONFIG_KEY_0='core.worktree', GIT_CONFIG_VALUE_0=str(self.cwd))
        result = self.python(environment, 'from task_start.config import default_project_registry; '
                             'print(default_project_registry())')
        self.assertEqual(result.strip(), str(self.source / 'config/projects.toml'))
        (linked / 'config').mkdir()
        (linked / 'config/projects.toml').write_text(MAPPING)
        result = self.invoke(environment, 'start', 'DEV-16', '--no-agent')
        self.assertEqual(result.returncode, 1)
        self.assertIn('competing private project registry', result.stderr)
        (linked / 'config/projects.toml').unlink()
        marker = linked / '.git'
        original = marker.read_text()
        try:
            marker.write_text('gitdir: /nonexistent/awl-packaging-fixture\n')
            result = self.invoke(environment, 'start', 'DEV-16', '--no-agent')
            self.assertEqual(result.returncode, 1)
            self.assertIn('Cannot verify the CLI checkout', result.stderr)
        finally:
            marker.write_text(original)

    def test_installed_dashboard_serves_bundled_assets_from_unrelated_directory(self):
        for environment in (self.normal, self.editable):
            with self.subTest(mode=environment.name):
                process = subprocess.Popen([environment / 'bin/awl', 'dashboard', '--port', '0'],
                                           cwd=self.cwd, env=self.env, stdout=subprocess.PIPE,
                                           stderr=subprocess.PIPE, text=True)
                try:
                    with selectors.DefaultSelector() as selector:
                        selector.register(process.stdout, selectors.EVENT_READ)
                        self.assertTrue(selector.select(timeout=10), 'Dashboard startup timed out')
                    line = process.stdout.readline()
                    self.assertTrue(line.startswith('Dashboard: http://127.0.0.1:'), line)
                    port = urlsplit(line.split()[1]).port
                    for route, name, content_type in (('/', 'index.html', 'text/html'),
                            ('/index.html', 'index.html', 'text/html'), ('/app.js', 'app.js', 'text/javascript'),
                            ('/styles.css', 'styles.css', 'text/css')):
                        connection = http.client.HTTPConnection('127.0.0.1', port, timeout=5)
                        try:
                            connection.request('GET', route)
                            response = connection.getresponse()
                            self.assertEqual(response.status, 200)
                            self.assertTrue(response.getheader('Content-Type').startswith(content_type))
                            self.assertEqual(response.read(), (ROOT / 'task_start/web' / name).read_bytes())
                        finally:
                            connection.close()
                finally:
                    process.terminate()
                    try:
                        stdout, stderr = process.communicate(timeout=10)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        stdout, stderr = process.communicate()
                        self.fail(f'Dashboard did not stop: {stderr}')
                self.assertEqual(process.returncode, 0, stderr)

    def test_distribution_metadata_allowlist_and_privacy(self):
        with zipfile.ZipFile(self.wheel) as archive:
            wheel = {name: archive.read(name) for name in archive.namelist()}
        with tarfile.open(self.sdist) as archive:
            sdist = {str(Path(member.name).relative_to(Path(member.name).parts[0])):
                     archive.extractfile(member).read() for member in archive if member.isfile()}
        runtime = {str(path.relative_to(ROOT)) for path in (ROOT / 'task_start').glob('*.py')}
        runtime.update({'task_start/pi_session.mjs', 'task_start/web/index.html',
                        'task_start/web/app.js', 'task_start/web/styles.css'})
        for artifact, entries in (('wheel', wheel), ('sdist', sdist)):
            with self.subTest(artifact=artifact):
                self.assertTrue(runtime <= entries.keys(), runtime - entries.keys())
                for name, data in entries.items():
                    self.assertNotIn(PRIVATE.encode(), data, name)
                    self.assertNotIn(str(self.root).encode(), data, name)
                    self.assertNotIn(str(ROOT).encode(), data, name)
                    self.assertNotIn(str(Path.home()).encode(), data, name)
                    self.assertNotIn('create-linear-task/', name)
                    self.assertNotIn('projects.toml', name)
                    self.assertNotIn('secrets.env', name)
                    self.assertNotIn('__pycache__', name)
                    if name not in runtime:
                        if artifact == 'wheel':
                            self.assertIn('.dist-info/', name)
                        else:
                            self.assertTrue(name in {'pyproject.toml', 'MANIFEST.in', 'README.md', 'LICENSE',
                                                     'PKG-INFO', 'setup.cfg'} or '.egg-info/' in name, name)
        entry_points, = (data for name, data in wheel.items() if name.endswith('/entry_points.txt'))
        entries = configparser.ConfigParser()
        entries.read_string(entry_points.decode())
        self.assertEqual(dict(entries['console_scripts']), {'awl': 'task_start.cli:awl'})
        metadata, = (data for name, data in wheel.items() if name.endswith('/METADATA'))
        metadata = BytesParser().parsebytes(metadata)
        self.assertEqual(metadata['Name'], 'agentic-workflows-lite')
        self.assertEqual(metadata['Requires-Python'], '>=3.12')
        self.assertIsNone(metadata['Requires-Dist'])


if __name__ == '__main__':
    unittest.main()
