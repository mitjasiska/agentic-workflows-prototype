"""Synthetic repositories/configuration only; no machine settings or network."""
import json
import os
import shutil
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from task_start import TaskError, cli
from task_start.agent import AgentOptions, AgentOverrides, adapter_for, resolve_agent_options
from task_start.config import (AgentConfig, CONFIG_DIRECTORY, REGISTRY, LocalConfig, Project, load_local,
                               load_projects, project_settings, repository_path, resolve_project)
from task_start.contexts import registry_path


class LayeredConfigTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.repo = self.root / "example-repo"
        self.repo.mkdir()
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.email", "fixture@example.invalid")
        self.git("config", "user.name", "Fixture")
        (self.repo / ".gitignore").write_text(f"{CONFIG_DIRECTORY}/config.local.toml\n")
        self.git("add", ".gitignore")
        self.git("commit", "-qm", "Fixture")
        self.directory = self.repo / CONFIG_DIRECTORY
        self.directory.mkdir()
        self.local = LocalConfig(self.root, "placeholder", AgentConfig("codex", "global-i", "low"),
                                 {"example-repo": "trusted"}, AgentConfig("pi", "global-r", "medium"))

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.repo), *args], check=True,
                              capture_output=True, text=True).stdout

    def write(self, content, name="config.toml"):
        path = self.directory / name
        path.write_text(content)
        return path

    def test_per_field_layers_and_cli_roles_do_not_bleed(self):
        self.write('[agent]\nmodel="shared-i"\n[reviewer]\nmode="high"\n[loop]\ntimeout=450\n')
        self.write('[agent]\nmode="max"\n[reviewer]\nmodel="local-r"\n[loop]\ntimeout=125\n', "config.local.toml")
        resolved = project_settings(self.local, self.repo)
        self.assertEqual(resolved.agent, AgentConfig("codex", "shared-i", "max"))
        self.assertEqual(resolved.reviewer, AgentConfig("pi", "local-r", "high"))
        self.assertEqual(resolved.loop_timeout, 125)
        self.assertEqual(resolve_agent_options(resolved.agent, AgentOverrides(model="cli-i")),
                         AgentOptions("codex", "cli-i", "max"))
        self.assertEqual(resolve_agent_options(resolved.reviewer, AgentOverrides(mode="low")),
                         AgentOptions("pi", "local-r", "low"))
        for key in ("projects_root", "api_key", "codex_repository_profiles", "issue_structure",
                    "review_validation", "task_assessment", "implementation_scope"):
            self.assertEqual(getattr(resolved, key), getattr(self.local, key))
        self.assertEqual(self.local.agent.model, "global-i")
        self.assertEqual(self.local.loop_timeout, 1800)

    def test_missing_layers_and_empty_tables_preserve_defaults(self):
        self.directory.rmdir()
        self.assertEqual(project_settings(self.local, self.repo), self.local)
        self.directory.mkdir()
        self.assertEqual(project_settings(self.local, self.repo), self.local)
        self.write('[agent]\n[reviewer]\n[loop]\n')
        self.assertEqual(project_settings(self.local, self.repo), self.local)
        self.write('[reviewer]\nmodel="local-only"\n', "config.local.toml")
        self.assertEqual(project_settings(self.local, self.repo).reviewer,
                         AgentConfig("pi", "local-only", "medium"))
        (self.directory / 'config.toml').unlink()
        self.assertEqual(project_settings(self.local, self.repo).reviewer.model, "local-only")

    def test_new_global_namespace_only_and_auxiliary_registry(self):
        content = f'projects_root={json.dumps(str(self.root))}\n[linear]\napi_key="placeholder"\n'
        old = self.root / ".agentic-workflows"
        old.mkdir()
        (old / "config.toml").write_text(content)
        with patch("pathlib.Path.home", return_value=self.root):
            with self.assertRaisesRegex(TaskError, "Config missing.*agentic-workflows-lite"):
                load_local()
            new = self.root / CONFIG_DIRECTORY
            new.mkdir()
            (new / "config.toml").write_text(content + '[loop]\ntimeout=222\n[agent]\nmodel="partial"\n')
            loaded = load_local()
            self.assertEqual(loaded.loop_timeout, 222)
            self.assertEqual(project_settings(loaded, self.repo), loaded)
            self.assertEqual(loaded.agent, AgentConfig(None, "partial", None))
            self.assertEqual(registry_path(), new / "contexts.sqlite3")
            self.write('[agent]\nkind="codex"\n')
            self.assertEqual(project_settings(loaded, self.repo).agent, AgentConfig("codex", "partial"))
            (new / "config.toml").write_text("broken = [")
            with self.assertRaisesRegex(TaskError, "valid TOML"):
                load_local()  # A bad new file must not trigger the legacy fallback either.

    def test_invalid_layers_refused_even_if_overridden_later(self):
        invalid = [
            'projects_root="/escape"', '[linear]\napi_key="never-echo-this"',
            '[projects.x]\nrepo_name="escape"', '[codex.repositories.x]\nprofile="unsafe"',
            '[implementation.scope]\npolicy="balanced"', '[review.validation]\nstrategy="exhaustive"',
            '[agent]\nextra="ignored?"', 'agent="codex"', 'reviewer=[]', 'loop=8',
            '[agent]\nkind=3', '[agent]\nkind="unknown"', '[reviewer]\nmodel=42',
            '[agent]\nmodel="--unsafe"', '[reviewer]\nmodel="two words"',
            '[agent]\nmode="turbo"', '[agent]\nmode=true',
            '[agent]\nkind="pi"\nmode="ultra"',
            '[reviewer]\nkind="pi"\nmodel="fixture:high"',
            '[agent]\nmode="high"\nreasoning="low"',
            '[loop]\nmax_passes=20', '[loop]\ntimeout=true', '[loop]\ntimeout="120"',
            '[loop]\ntimeout=0', '[loop]\ntimeout=-1', '[loop]\ntimeout=86401',
            '[loop]\ntimeout=nan', '[loop]\ntimeout=inf', 'malformed = [',
        ]
        self.write('[agent]\nkind="codex"\nmodel="valid"\nmode="high"\n[loop]\ntimeout=50', "config.local.toml")
        for content in invalid:
            with self.subTest(content=content):
                self.write(content)
                with self.assertRaises(TaskError) as caught:
                    project_settings(self.local, self.repo)
                self.assertNotIn("never-echo-this", str(caught.exception))
        self.write("")
        self.write('[reviewer]\nmode="bad"', 'config.local.toml')
        with self.assertRaises(TaskError):
            project_settings(self.local, self.repo)

    def test_legacy_reasoning_merges_as_mode_and_adapter_checks_final_pair(self):
        self.write('[agent]\nreasoning="ultra"\n')
        resolved = project_settings(self.local, self.repo)
        self.assertEqual(resolved.agent.mode, "ultra")
        with self.assertRaisesRegex(TaskError, "Pi mode"):
            adapter_for(resolve_agent_options(resolved.agent, AgentOverrides(kind="pi")))
        self.assertEqual(adapter_for(resolve_agent_options(resolved.agent, AgentOverrides(kind="pi", mode="low"))).kind, "pi")

    def test_invalid_global_loop_and_unknown_agent_fields(self):
        path = self.root / 'global.toml'
        base = f'projects_root={json.dumps(str(self.root))}\n[linear]\napi_key="placeholder"\n'
        for content in ('[loop]\ntimeout=true', '[loop]\ntimeout=86401', '[loop]\nunknown=3',
                        '[agent]\nkind="codex"\nunknown="oops"'):
            with self.subTest(content=content):
                path.write_text(base + content)
                with self.assertRaises(TaskError):
                    load_local(path)
                self.assertEqual(load_local(path, no_agent=True).loop_timeout, 1800)

    def test_unsafe_file_kinds_and_links_fail_closed(self):
        outside = self.root / "outside.toml"
        outside.write_text('[agent]\nmode="high"')
        path = self.directory / "config.toml"
        for kind in ('symlink', 'dangling', 'directory', 'hardlink', 'fifo'):
            with self.subTest(kind=kind):
                if kind == 'symlink':
                    path.symlink_to(outside)
                elif kind == 'dangling':
                    path.symlink_to(self.root / 'missing')
                elif kind == 'directory':
                    path.mkdir()
                elif kind == 'hardlink':
                    os.link(outside, path)
                else:
                    os.mkfifo(path)
                with self.assertRaisesRegex(TaskError, "regular, unaliased"):
                    project_settings(self.local, self.repo)
                path.rmdir() if kind == 'directory' else path.unlink()
        self.directory.rmdir()
        self.directory.symlink_to(self.root, target_is_directory=True)
        with self.assertRaisesRegex(TaskError, "real directory"):
            project_settings(self.local, self.repo)

    def test_local_override_must_be_ignored_and_untracked(self):
        self.write('[agent]\nmode="high"', 'config.local.toml')
        (self.repo / '.gitignore').write_text('')
        with self.assertRaisesRegex(TaskError, 'Ignore'):
            project_settings(self.local, self.repo)
        (self.repo / '.gitignore').write_text(f'{CONFIG_DIRECTORY}/config.local.toml\n')
        self.assertEqual(project_settings(self.local, self.repo).agent.mode, 'high')
        self.git('add', '-f', f'{CONFIG_DIRECTORY}/config.local.toml')
        with self.assertRaisesRegex(TaskError, 'must not be tracked'):
            project_settings(self.local, self.repo)

    def test_worktree_and_cwd_cannot_supply_defaults(self):
        worktree = self.root / 'task-worktree'
        self.git('worktree', 'add', '-qb', 'task', str(worktree))
        (worktree / CONFIG_DIRECTORY).mkdir()
        (worktree / CONFIG_DIRECTORY / 'config.toml').write_text('[agent]\nmodel="untrusted"')
        self.write('[agent]\nmodel="trusted"')
        with patch('pathlib.Path.cwd', return_value=worktree):
            self.assertEqual(project_settings(self.local, self.repo).agent.model, 'trusted')
        with self.assertRaisesRegex(TaskError, 'permanent checkout'):
            project_settings(self.local, worktree)
        self.assertFalse((worktree / CONFIG_DIRECTORY / 'config.local.toml').exists())

    def test_private_registry_any_cwd_missing_examples_and_ambiguity(self):
        self.assertTrue(REGISTRY.is_absolute())
        self.assertEqual(REGISTRY.parent, Path(__file__).resolve().parents[1] / 'config')
        runtime = self.root / 'projects.toml'
        with self.assertRaisesRegex(TaskError, 'Config missing'):
            load_projects(runtime)
        example = Path(__file__).resolve().parents[1] / 'config/projects.example.toml'
        with self.assertRaisesRegex(TaskError, 'examples are not runtime'):
            load_projects(example)
        runtime.write_text('[projects.fixture]\nlinear_project="Synthetic Project"\nrepo_name="example-repo"\nbase_branch="main"')
        # A disposable CLI installation exercises the actual source-relative
        # default without ever reading or writing a developer's runtime registry.
        installation = self.root / 'cli-source'
        package = installation / 'task_start'
        package.mkdir(parents=True)
        for name in ('__init__.py', 'config.py'):
            shutil.copyfile(REGISTRY.parent.parent / 'task_start' / name, package / name)
        (installation / 'config').mkdir()
        shutil.copyfile(runtime, installation / 'config/projects.toml')
        alias = self.root / 'cli-alias'
        alias.symlink_to(installation, target_is_directory=True)
        program = ('from pathlib import Path; from task_start.config import load_projects, REGISTRY; '
                   f'assert REGISTRY == Path({str(installation / "config/projects.toml")!r}); '
                   'assert load_projects()[0].repo_name == "example-repo"')
        for source, cwd in ((installation, self.root), (alias, self.repo)):
            subprocess.run(['python3.12', '-c', program], cwd=cwd,
                           env=dict(os.environ, PYTHONPATH=str(source)), check=True)
        project = resolve_project(load_projects(runtime), 'Synthetic Project')
        self.assertEqual(repository_path(self.local, project), self.repo)
        runtime.write_text(runtime.read_text() + '\n[projects.duplicate]\nlinear_project="Synthetic Project"\nrepo_name="another"\nbase_branch="main"')
        with self.assertRaisesRegex(TaskError, 'Duplicate'):
            load_projects(runtime)

    def test_start_refuses_invalid_project_or_mapping_before_preparation(self):
        from test_task_start import ISSUE
        self.enterContext(patch('task_start.contexts.registry_path', return_value=self.root / 'contexts.sqlite3'))
        self.write('[codex]\nprofile="forbidden"')
        with patch('task_start.cli.load_local', return_value=self.local), \
                patch('task_start.cli.load_projects', return_value=[Project(ISSUE.project, self.repo.name, 'main')]), \
                patch('task_start.cli.Linear') as linear, patch('task_start.cli.prepare_task') as prepare, \
                patch('task_start.cli.launch_registered') as launch:
            linear.return_value.get_issue.return_value = ISSUE
            with self.assertRaisesRegex(TaskError, 'only agent, reviewer, and loop'):
                cli.start(ISSUE.identifier)
            prepare.assert_not_called()
            launch.assert_not_called()
            linear.return_value.start.assert_not_called()
            with patch('task_start.cli.load_projects', side_effect=TaskError('Config missing')):
                with self.assertRaisesRegex(TaskError, 'Config missing'):
                    cli.start(ISSUE.identifier)
            prepare.assert_not_called()
            with patch('task_start.cli.load_projects', return_value=[
                    Project(ISSUE.project, self.repo.name, 'main'), Project(ISSUE.project, 'other', 'main')]):
                with self.assertRaisesRegex(TaskError, 'exactly one'):
                    cli.start(ISSUE.identifier)
            prepare.assert_not_called()
            launch.assert_not_called()

    def test_start_resolves_permanent_layers_and_cli_before_launch(self):
        from test_task_start import ISSUE
        from task_start.metrics import MetricsStore
        from task_start.workspace import Workspace
        from types import SimpleNamespace
        self.enterContext(patch('task_start.contexts.registry_path', return_value=self.root / 'contexts.sqlite3'))
        self.write('[agent]\nmodel="project-i"\nmode="medium"')
        workspace = Workspace('dev-7-fixture', self.root / 'task', 'w1', 't1', 'p1', 'created')
        with patch('task_start.cli.load_local', return_value=self.local), \
                patch('task_start.cli.load_projects', return_value=[Project(ISSUE.project, self.repo.name, 'main')]), \
                patch('task_start.cli.Linear') as linear, \
                patch('task_start.cli.prepare_task', return_value=workspace), \
                patch('task_start.cli.adapter_for') as adapter, \
                patch('task_start.cli.launch_registered', return_value=SimpleNamespace(summary='launched')) as launch:
            linear.return_value.get_issue.return_value = ISSUE
            cli.start(ISSUE.identifier, mode='high')
            adapter.assert_called_once_with(AgentOptions('codex', 'project-i', 'high'))
            execution = launch.call_args.args[1]
            self.assertEqual(execution.repository, self.repo)
            self.assertEqual(execution.policy, {'codex_profile': 'trusted'})
            self.assertNotIn('placeholder', execution.handoff)
            metric, = MetricsStore(self.root / 'contexts.sqlite3').read()
            self.assertEqual(metric['status'], 'dispatched')
            self.assertIsNone(metric['context_id'])


def install_lifecycle_layers(case):
    """Track shared defaults in the fixture's local upstream, never a real repo."""
    relative = '.agentic-workflows-lite'
    shared = case.remote / relative
    shared.mkdir()
    (shared / 'config.toml').write_text('[agent]\nmodel="shared-i"\n[reviewer]\nmodel="shared-r"\n[loop]\ntimeout=41\n')
    ignore = case.remote / '.gitignore'
    ignore.write_text(ignore.read_text() + f'{relative}/config.local.toml\n')
    case.command(case.remote, 'add', relative, '.gitignore')
    case.command(case.remote, 'commit', '-m', 'Synthetic project defaults')
    case.command(case.repo, 'fetch', 'origin')
    case.command(case.repo, 'merge', '--ff-only', 'origin/main')
    local = case.repo / relative / 'config.local.toml'
    local.write_text('[agent]\nmode="high"\n[reviewer]\nmode="medium"\n[loop]\ntimeout=23\n')
    return local
