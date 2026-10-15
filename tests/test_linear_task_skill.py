import importlib.util
from dataclasses import replace
import io
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import textwrap
import tomllib
import unittest
from unittest.mock import patch

from task_start.config import IssueStructureConfig
from task_start.handoff import implementation_handoff
from task_start.linear import Issue
from task_start.preparation import check_issue_structure
from task_start.workspace import Workspace


ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "skills" / "create-linear-task" / "scripts" / "prepare_issue.py"
SPEC = importlib.util.spec_from_file_location("linear_task_preparer", SCRIPT)
preparer = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = preparer
SPEC.loader.exec_module(preparer)
SYNC_SCRIPT = ROOT / "skills" / "create-linear-task" / "scripts" / "sync_config.py"
SYNC_SPEC = importlib.util.spec_from_file_location("linear_task_config_sync", SYNC_SCRIPT)
synchronizer = importlib.util.module_from_spec(SYNC_SPEC)
sys.modules[SYNC_SPEC.name] = synchronizer
SYNC_SPEC.loader.exec_module(synchronizer)

PRIMARY = {
    "feature": ("Feature", "feat"),
    "bug": ("Bug", "fix"),
    "chore": ("Chore", "chore"),
    "docs": ("Docs", "docs"),
    "refactor": ("Refactor", "refactor"),
}
WORKFLOW_NAMES = [label for label, _ in PRIMARY.values()] + ["Research"]


def label(name):
    return {"id": name.casefold() + "-id", "name": name}


def catalog(*extra):
    return [label(name) for name in WORKFLOW_NAMES] + [label(name) for name in extra]


def existing_target(**changes):
    value = {
        "team_id": "dev-team-id",
        "team_name": "DEV",
        "project_id": "example-project-id",
        "project_name": "Example Project",
    }
    value.update(changes)
    return value


def config_text(*, replace=False, team="DEV"):
    team_line = "" if team is None else f'linear_team = {json.dumps(team)}\n'
    return textwrap.dedent(
        f'''\
        [task_creation]
        task_kinds = ["implementation", "research", "experiment"]
        default_stop_condition = "Finish and validate, then stop ready for review. Do not commit."

        [task_creation.primary_categories.feature]
        linear_label = "Feature"
        change_type = "feat"

        [task_creation.primary_categories.bug]
        linear_label = "Bug"
        change_type = "fix"

        [task_creation.primary_categories.chore]
        linear_label = "Chore"
        change_type = "chore"

        [task_creation.primary_categories.docs]
        linear_label = "Docs"
        change_type = "docs"

        [task_creation.primary_categories.refactor]
        linear_label = "Refactor"
        change_type = "refactor"

        [task_creation.research_modifier]
        linear_label = "Research"

        [projects.example]
        linear_project = "Example Project"
        {team_line}repo_name = "example-repo"
        base_branch = "main"
        replace_workflow_labels = {str(replace).lower()}
        '''
    )


def draft(**changes):
    value = {
        "title": "Add export retry limits",
        "context": None,
        "outcome": "Export retries stop after the configured limit.",
        "done_when": ["The retry limit is enforced.", "Focused tests pass."],
        "boundaries": [],
        "task_kind": "implementation",
        "primary_category": "feature",
        "research_modifier": False,
        "agent": {
            "repository_context": ["Inspect the existing export worker."],
            "guidance": ["Reuse the current retry policy."],
            "constraints": [],
            "validation": ["Run the focused worker tests."],
        },
        "existing_target": None,
        "existing_labels": [],
        "workspace_labels": catalog("Improvement", "Priority"),
    }
    value.update(changes)
    return value


class LinearTaskSkillTests(unittest.TestCase):
    def test_private_registry_rename_refreshes_skill_without_changing_linear_target(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry = root / 'projects.toml'
            installed = root / 'config.local.toml'
            original = ('[projects.prototype]\nlinear_project="Synthetic Project"\n'
                        'linear_team="SYNTHETIC"\nrepo_name="agentic-workflows-lite"\nbase_branch="main"\n')
            registry.write_text(original)
            installed.write_text(synchronizer.render(registry, private=True))
            before = preparer.resolve_target(preparer.load_settings(installed), root / 'agentic-workflows-lite', None)
            registry.write_text(original.replace('repo_name="agentic-workflows-lite"',
                                                 'repo_name="agentic-workflows-prototype"'))
            installed.write_text(synchronizer.render(registry, private=True))
            settings = preparer.load_settings(installed)
            after = preparer.resolve_target(settings, root / 'agentic-workflows-prototype', None)
            self.assertEqual(after, replace(before, repo_name='agentic-workflows-prototype'))
            with self.assertRaisesRegex(preparer.PreparationError, 'exactly one configured'):
                preparer.resolve_target(settings, root / 'agentic-workflows-lite', None)

    def settings(self, **options):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        path = Path(temporary.name) / "projects.toml"
        path.write_text(config_text(**options))
        return preparer.load_settings(path)

    def target(self, settings):
        return preparer.resolve_target(settings, Path("/work/example-repo"), None)

    def prepare(self, settings=None, **changes):
        settings = settings or self.settings()
        return preparer.prepare_issue(settings, self.target(settings), draft(**changes))

    def test_repository_configuration_selects_exact_target_and_canonical_taxonomy(self):
        settings = self.settings()
        target = self.target(settings)
        self.assertEqual(target.key, "example")
        self.assertEqual(target.linear_project, "Example Project")
        self.assertEqual(target.linear_team, "DEV")
        self.assertEqual(
            {
                key: (value.linear_label, value.change_type)
                for key, value in settings.primary_categories.items()
            },
            PRIMARY,
        )
        self.assertEqual(settings.research_label, "Research")
        with self.assertRaisesRegex(preparer.PreparationError, "no configured linear_team"):
            preparer.resolve_target(self.settings(team=None), Path("/work/example-repo"), None)

    def test_new_issue_uses_configured_target(self):
        result = self.prepare(existing_target=None)
        self.assertEqual(
            result["target"],
            {
                "source": "configuration",
                "project_key": "example",
                "team": "DEV",
                "project": "Example Project",
            },
        )

    def test_refinement_preserves_exact_existing_target_and_cannot_move(self):
        current = existing_target()
        result = self.prepare(existing_target=current)
        self.assertEqual(
            result["target"],
            {
                "source": "existing_issue",
                "project_key": "example",
                "team": "DEV",
                "project": "Example Project",
                "team_id": "dev-team-id",
                "project_id": "example-project-id",
            },
        )
        self.assertNotIn("team", result["issue"])
        self.assertNotIn("project", result["issue"])

        for conflicting in (
            existing_target(team_id="other-team-id", team_name="OTHER"),
            existing_target(project_id="other-project-id", project_name="Other Project"),
        ):
            with self.subTest(conflicting=conflicting), self.assertRaisesRegex(
                preparer.PreparationError,
                "existing issue target conflicts.*refusing to move",
            ):
                self.prepare(existing_target=conflicting)

        settings = self.settings()
        with self.assertRaisesRegex(
            preparer.PreparationError,
            "existing issue target conflicts.*refusing to move",
        ):
            preparer.verify_workflow_labels(
                settings,
                self.target(settings),
                draft(
                    existing_target=existing_target(),
                    current_target=existing_target(
                        project_id="moved-project-id", project_name="Moved Project"
                    ),
                    existing_labels=[label("Feature")],
                ),
            )

    def test_packaged_config_matches_canonical_project_registry(self):
        canonical = ROOT / "config" / "projects.example.toml"
        packaged = ROOT / "skills" / "create-linear-task" / "config.example.toml"
        self.assertEqual(packaged.read_text(), synchronizer.render(canonical))
        completed = subprocess.run(
            [sys.executable, "-B", str(SYNC_SCRIPT), "--check"],
            text=True,
            capture_output=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_public_tree_has_only_synthetic_mappings_and_ignores_private_paths(self):
        tracked = set(subprocess.check_output(['git', '-C', str(ROOT), 'ls-files'], text=True).splitlines())
        deleted = set(subprocess.check_output(['git', '-C', str(ROOT), 'ls-files', '--deleted'], text=True).splitlines())
        # Account for an unstaged implementation deletion without reading any
        # legitimate ignored runtime copy in a developer's configured checkout.
        self.assertNotIn('config/projects.toml', tracked - deleted)
        public = tomllib.loads((ROOT / 'config/projects.example.toml').read_text())
        self.assertIs(public['example_only'], True)
        for entry in public['projects'].values():
            self.assertTrue(entry['repo_name'].startswith('example-'))
            self.assertTrue(entry['linear_project'].startswith('Example '))
            self.assertEqual(entry['linear_team'], 'EXAMPLE')
        private_paths = ['config/projects.toml', '.agentic-workflows-lite/config.local.toml',
                         'skills/create-linear-task/config.local.toml',
                         'plugins/create-linear-task/skills/create-linear-task/config.local.toml']
        result = subprocess.run(['git', '-C', str(ROOT), 'check-ignore', '--no-index', '--', *private_paths],
                                check=True, capture_output=True, text=True)
        self.assertEqual(result.stdout.splitlines(), private_paths)

    def test_public_examples_are_never_runtime_targets(self):
        for skill in (ROOT / "skills/create-linear-task", ROOT / "plugins/create-linear-task/skills/create-linear-task"):
            with self.subTest(skill=skill):
                with self.assertRaisesRegex(preparer.PreparationError, "Example targets"):
                    preparer.load_settings(skill / "config.example.toml")
                self.assertFalse((skill / "config.toml").exists())
                temporary = self.enterContext(tempfile.TemporaryDirectory())
                installed = Path(temporary) / "installed"
                shutil.copytree(skill, installed, ignore=shutil.ignore_patterns("config.local.toml"))
                result = subprocess.run([sys.executable, str(installed / "scripts/prepare_issue.py"),
                                         "--repository", "/work/example-app"], input=json.dumps(draft()),
                                        capture_output=True, text=True)
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn('"target"', result.stdout)

    def test_private_sync_requires_explicit_safe_destination_and_never_changes_public(self):
        public = synchronizer.DEFAULT_OUTPUT.read_bytes()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            registry = root / "projects.toml"
            registry.write_text('[projects.private]\nlinear_project="Private Fixture 🦉"\n'
                                'linear_team="FIXTURE"\nrepo_name="private-fixture"\nbase_branch="main"\n'
                                '# Never copy this private note\n')
            installed = root / "installed/config.local.toml"
            with patch("sys.stderr", new=io.StringIO()):
                for arguments in (["--private"], ["--projects", str(registry)],
                                  ["--private", "--projects", str(registry), "--output", str(synchronizer.DEFAULT_OUTPUT)],
                                  ["--private", "--projects", str(registry), "--output", str(ROOT / "config.local.toml")]):
                    self.assertEqual(synchronizer.main(arguments), 2)
            arguments = ["--private", "--projects", str(registry), "--output", str(installed)]
            self.assertEqual(synchronizer.main(arguments), 0)
            self.assertEqual(synchronizer.main(arguments + ["--check"]), 0)
            self.assertEqual(installed.stat().st_mode & 0o777, 0o600)
            self.assertNotIn("private note", installed.read_text())
            settings = preparer.load_settings(installed)
            target = preparer.resolve_target(settings, root / "private-fixture", None)
            self.assertEqual((target.linear_project, target.linear_team), ("Private Fixture 🦉", "FIXTURE"))
            # Reject a separate checkout, not just this source repository.
            checkout = root / "another-repo"
            checkout.mkdir()
            subprocess.run(["git", "-C", str(checkout), "init", "-q"], check=True)
            with self.assertRaisesRegex(ValueError, "outside Git"):
                synchronizer.private_output(checkout / "config.local.toml")
            alias = root / "alias"
            alias.symlink_to(installed.parent, target_is_directory=True)
            with self.assertRaises(ValueError):
                synchronizer.private_output(alias / "config.local.toml")
            with self.assertRaisesRegex(ValueError, "marked examples"):
                synchronizer.render(registry)
            with self.assertRaisesRegex(ValueError, "unmarked runtime"):
                synchronizer.render(synchronizer.DEFAULT_PROJECTS, private=True)
            registry.write_text(registry.read_text() + 'api_key="must-not-install"\n')
            with self.assertRaisesRegex(ValueError, "unsupported project"):
                synchronizer.render(registry, private=True)
        self.assertEqual(synchronizer.DEFAULT_OUTPUT.read_bytes(), public)

    def test_renders_visible_spec_and_complete_collapsed_metadata(self):
        settings = self.settings()
        result = self.prepare(settings)
        description = result["issue"]["description"]
        self.assertTrue(description.startswith("## Scope / outcome\n"))
        self.assertNotIn("## Context / why", description)
        self.assertNotIn("## Boundaries", description)
        self.assertIn("+++ Agent instructions\n", description)
        self.assertTrue(description.endswith("\n+++\n"))
        self.assertNotIn(">>>", description)
        self.assertIn("addressed to the implementation agent executing this issue", description)
        self.assertEqual(description.count("Export retries stop after the configured limit."), 1)
        self.assertIn("- Task kind: `implementation`", description)
        self.assertIn("- Primary category: `feature`", description)
        self.assertIn("- Research modifier: `no`", description)
        self.assertIn("- Intended workflow labels: `Feature`", description)
        self.assertIn("- Change type: `feat`", description)
        self.assertIn(settings.stop_condition, description)

    def test_skill_guidance_separates_thorough_coverage_from_focused_execution(self):
        # These are instruction-contract checks, not a live model authoring evaluation.
        skill = (ROOT / "skills/create-linear-task/SKILL.md").read_text()
        for guidance in (
            'expresses testable behavior and relevant regression coverage',
            'thorough automated coverage', 'failure paths, and invariants, with meaningful assertions',
            'Focused validation limits repeated execution, not the number of valuable tests to create',
            'do not reduce coverage', 'arbitrary test-count targets or caps',
            'Avoid artificial tests for non-testable work',
            'do not automatically insert strict red–green–refactor, blanket full-suite, or "all tests pass locally" obligations',
            'Preserve explicit user and project requirements',
            'explicit local obligations still apply', 'do not assume it exists',
            'actual commands/results and outstanding checks',
            'pending final validation for human verification',
        ):
            with self.subTest(guidance=guidance):
                self.assertIn(guidance, skill)

    def test_distributed_default_stop_preserves_behavioral_outcomes_without_blanket_tests(self):
        for skill in (ROOT / 'skills/create-linear-task', ROOT / 'plugins/create-linear-task/skills/create-linear-task'):
            with self.subTest(skill=skill):
                public = tomllib.loads((skill / 'config.example.toml').read_text())
                settings = replace(self.settings(), stop_condition=public['task_creation']['default_stop_condition'])
                target = self.target(settings)
                for kind, outcomes, validation in (
                    ('feature', ['Retries stop at the configured limit.', 'Tests cover exhausted retries and worker failures.'],
                     'Run the focused retry tests.'),
                    ('docs', ['The setup guide links to the configuration reference.'], 'Verify the relative link target.'),
                ):
                    value = draft(primary_category=kind, done_when=outcomes,
                                  agent=dict(repository_context=[], guidance=[], constraints=[], validation=[validation]))
                    description = preparer.prepare_issue(settings, target, value)['issue']['description']
                    self.assertEqual(description.split('## Done when\n\n')[1].split('\n\n+++')[0],
                                     '\n'.join(f'- {outcome}' for outcome in outcomes))
                    self.assertIn(settings.stop_condition, description)
                    for guidance in ('relevant focused validation', 'explicit local/per-pass requirements',
                                     'Report actual commands/results and outstanding checks honestly',
                                     'pending merge-time validation for CI or human verification',
                                     'do not assume an external gate exists', 'ready for independent review'):
                        self.assertIn(guidance, description)
                    for blanket in ('full suite', 'full-suite', 'all tests pass locally', 'red-green-refactor'):
                        self.assertNotIn(blanket, description)

    def test_stricter_author_requirements_and_configured_stop_survive_rendering(self):
        requirement = 'Full regression must pass before merge.'
        local = 'This agent must run the full suite locally before handoff.'
        methodology = 'Use strict red-green-refactor for this fix.'
        stop = 'Run all tests locally, then stop ready for review. Do not commit.'
        settings = replace(self.settings(), stop_condition=stop)
        description = self.prepare(
            settings, done_when=[requirement],
            agent=dict(repository_context=[], guidance=[methodology], constraints=[], validation=[local]),
        )['issue']['description']
        for text in (requirement, local, methodology, stop):
            self.assertEqual(description.count(text), 1)

    def test_generated_issue_remains_valid_full_task_context_in_every_mode(self):
        description = self.prepare(self.settings())["issue"]["description"]
        issue = Issue('issue-id', 'DEV-7', 'Example task', 'Example Project', 'todo', 'Todo', 'started',
                      description=description)
        workspace = Workspace('dev-7-task', Path('/task'), 'w1', 't1', 'p1', 'prepared')
        for mode in ('required', 'warn', 'ignore'):
            with self.subTest(mode=mode), patch('sys.stderr', new_callable=io.StringIO) as stderr:
                check_issue_structure(issue, IssueStructureConfig(mode))
                self.assertEqual(stderr.getvalue(), '')
                self.assertIn(f'Task:\n{description}\n\nWorkflow-owned implementation instructions:',
                              implementation_handoff(issue, workspace))

    def test_each_primary_category_has_one_label_and_its_change_type(self):
        settings = self.settings()
        for category, (linear_label, change_type) in PRIMARY.items():
            with self.subTest(category=category):
                result = self.prepare(settings, primary_category=category)
                self.assertEqual(
                    result["classification"],
                    {
                        "task_kind": "implementation",
                        "primary_category": category,
                        "research_modifier": False,
                        "intended_workflow_labels": [linear_label],
                        "change_type": change_type,
                    },
                )
                self.assertEqual(
                    result["label_changes"],
                    {"add": [label(linear_label)["id"]], "remove": []},
                )

    def test_pure_research_has_only_research_label_and_no_change_type(self):
        result = self.prepare(
            task_kind="research",
            primary_category=None,
            research_modifier=True,
        )
        self.assertEqual(result["classification"]["primary_category"], None)
        self.assertEqual(result["classification"]["intended_workflow_labels"], ["Research"])
        self.assertIsNone(result["classification"]["change_type"])
        self.assertEqual(result["label_changes"], {"add": ["research-id"], "remove": []})
        self.assertIn("- Change type: `none`", result["issue"]["description"])

    def test_research_task_kind_requires_research_modifier(self):
        with self.assertRaisesRegex(
            preparer.PreparationError, "task_kind 'research' requires research_modifier true"
        ):
            self.prepare(
                task_kind="research",
                primary_category=None,
                research_modifier=False,
            )

    def test_each_primary_category_can_be_combined_with_research(self):
        settings = self.settings()
        for category, (linear_label, change_type) in PRIMARY.items():
            with self.subTest(category=category):
                result = self.prepare(
                    settings,
                    task_kind="research",
                    primary_category=category,
                    research_modifier=True,
                )
                self.assertEqual(
                    result["classification"]["intended_workflow_labels"],
                    [linear_label, "Research"],
                )
                self.assertEqual(result["classification"]["change_type"], change_type)
                self.assertEqual(result["label_changes"], {
                    "add": [label(linear_label)["id"], "research-id"],
                    "remove": [],
                })

    def test_multiple_primary_categories_and_arbitrary_categories_are_rejected(self):
        for category in [["feature", "bug"], "improvement", "research", {"bug": True}]:
            with self.subTest(category=category), self.assertRaises(preparer.PreparationError):
                self.prepare(primary_category=category)

    def test_canonical_configuration_rejects_implicit_extensions_or_remapping(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "projects.toml"
            extended = config_text().replace(
                "[task_creation.research_modifier]",
                "[task_creation.primary_categories.improvement]\n"
                'linear_label = "Improvement"\nchange_type = "feat"\n\n'
                "[task_creation.research_modifier]",
            )
            path.write_text(extended)
            with self.assertRaisesRegex(preparer.PreparationError, "exactly"):
                preparer.load_settings(path)
            remapped = config_text().replace('linear_label = "Feature"', 'linear_label = "Enhancement"')
            path.write_text(remapped)
            with self.assertRaisesRegex(preparer.PreparationError, "must map"):
                preparer.load_settings(path)

    def test_unrelated_labels_absent_from_active_catalog_are_preserved(self):
        existing = [
            {"id": "archived-improvement-id", "name": "Improvement"},
            {"id": "archived-priority-id", "name": "Priority"},
        ]
        result = self.prepare(existing_labels=existing, workspace_labels=catalog())
        self.assertNotIn("label_ids", result["issue"])
        self.assertEqual(result["label_changes"], {"add": ["feature-id"], "remove": []})
        self.assertNotIn("archived-improvement-id", result["label_changes"]["remove"])
        self.assertNotIn("archived-priority-id", result["label_changes"]["remove"])

    def test_fresh_reconciliation_preserves_concurrently_added_unrelated_label(self):
        stale_plan = self.prepare(existing_labels=[])
        self.assertEqual(stale_plan["label_changes"], {"add": ["feature-id"], "remove": []})

        fresh_labels = [label("Improvement")]
        fresh_plan = self.prepare(existing_labels=fresh_labels)
        changes = fresh_plan["label_changes"]
        current_ids = [item["id"] for item in fresh_labels]
        reconciled = [item for item in current_ids if item not in changes["remove"]]
        reconciled.extend(item for item in changes["add"] if item not in reconciled)

        self.assertEqual(changes, {"add": ["feature-id"], "remove": []})
        self.assertEqual(reconciled, ["improvement-id", "feature-id"])
        self.assertNotIn("label_ids", fresh_plan["issue"])

    def test_post_mutation_verification_reports_concurrent_workflow_conflicts(self):
        settings = self.settings()
        target = self.target(settings)
        verified = preparer.verify_workflow_labels(
            settings,
            target,
            draft(
                existing_target=existing_target(),
                current_target=existing_target(),
                existing_labels=[label("Feature"), label("Improvement")],
            ),
        )
        self.assertEqual(verified["workflow_labels"], ["Feature"])
        self.assertEqual(verified["target"]["source"], "existing_issue")
        self.assertEqual(verified["target"]["team_id"], "dev-team-id")
        self.assertTrue(verified["verified"])

        for post_labels in (
            [label("Feature"), label("Bug"), label("Improvement")],
            [label("Improvement")],
        ):
            with self.subTest(post_labels=post_labels), self.assertRaisesRegex(
                preparer.PreparationError,
                "concurrent Linear change detected.*do not auto-repair",
            ):
                preparer.verify_workflow_labels(
                    settings,
                    target,
                    draft(
                        existing_target=existing_target(),
                        current_target=existing_target(),
                        existing_labels=post_labels,
                    ),
                )

    def test_post_mutation_target_verification_uses_original_ids(self):
        settings = self.settings()
        original = existing_target()
        reread = existing_target(
            team_id="replacement-team-id",
            project_id="replacement-project-id",
        )
        with self.assertRaisesRegex(
            preparer.PreparationError,
            "concurrent Linear target change detected.*do not auto-repair",
        ):
            preparer.verify_workflow_labels(
                settings,
                self.target(settings),
                draft(
                    existing_target=original,
                    current_target=reread,
                    existing_labels=[label("Feature")],
                ),
            )

    def test_mutually_exclusive_primary_labels_stop_by_default(self):
        settings = self.settings()
        with self.assertRaisesRegex(preparer.PreparationError, "conflict"):
            self.prepare(
                settings,
                primary_category="feature",
                existing_labels=[label("Bug"), label("Improvement")],
            )
        with self.assertRaisesRegex(preparer.PreparationError, "conflict"):
            self.prepare(
                settings,
                primary_category=None,
                existing_labels=[label("Feature"), label("Bug")],
            )

    def test_explicit_replacement_only_removes_canonical_workflow_labels(self):
        settings = self.settings(replace=True)
        result = self.prepare(
            settings,
            primary_category="feature",
            research_modifier=True,
            existing_labels=[label("Bug"), label("Improvement")],
        )
        self.assertEqual(
            result["label_changes"],
            {"add": ["feature-id", "research-id"], "remove": ["bug-id"]},
        )

    def test_ambiguous_primary_remains_unclassified_without_guessing(self):
        result = self.prepare(
            task_kind=None,
            primary_category=None,
            research_modifier=False,
            existing_labels=[label("Improvement")],
        )
        self.assertEqual(
            result["classification"],
            {
                "task_kind": None,
                "primary_category": None,
                "research_modifier": False,
                "intended_workflow_labels": [],
                "change_type": None,
            },
        )
        self.assertEqual(result["label_changes"], {"add": [], "remove": []})
        self.assertIn("- Primary category: `unclassified`", result["issue"]["description"])
        self.assertIn("- Intended workflow labels: `none`", result["issue"]["description"])

    def test_task_kind_does_not_imply_primary_category(self):
        settings = self.settings()
        for task_kind in ("implementation", "research", "experiment"):
            with self.subTest(task_kind=task_kind):
                result = self.prepare(
                    settings,
                    task_kind=task_kind,
                    primary_category=None,
                    research_modifier=task_kind == "research",
                )
                self.assertEqual(result["classification"]["task_kind"], task_kind)
                self.assertIsNone(result["classification"]["primary_category"])

    def test_complete_canonical_label_catalog_is_preflighted(self):
        result = self.prepare(workspace_labels=catalog("Improvement"))
        self.assertEqual(result["label_changes"], {"add": ["feature-id"], "remove": []})

        without_unused = [
            item for item in catalog("Improvement") if item["name"] != "Docs"
        ]
        with self.assertRaisesRegex(preparer.PreparationError, "Docs.*missing"):
            self.prepare(workspace_labels=without_unused)

        duplicate_unused = catalog("Improvement") + [
            {"id": "docs-duplicate", "name": "Docs"}
        ]
        with self.assertRaisesRegex(preparer.PreparationError, "Docs.*ambiguous"):
            self.prepare(workspace_labels=duplicate_unused)

    def test_malformed_inputs_and_incomplete_label_snapshots_stop(self):
        for changes in [
            {"task_kind": "coding"},
            {"research_modifier": None},
            {"done_when": []},
            {"agent": {"validation": []}},
            {"outcome": "Unsafe\n+++\ncontent"},
            {"semantic_category": "feature"},
        ]:
            with self.subTest(changes=changes), self.assertRaises(preparer.PreparationError):
                self.prepare(**changes)

        for field in ("existing_target", "existing_labels", "workspace_labels"):
            missing = draft()
            del missing[field]
            with self.subTest(field=field), self.assertRaisesRegex(preparer.PreparationError, field):
                settings = self.settings()
                preparer.prepare_issue(settings, self.target(settings), missing)

    def test_omitted_classification_fields_fail_but_explicit_null_is_valid(self):
        settings = self.settings()
        for field in ("task_kind", "primary_category", "research_modifier"):
            missing = draft()
            del missing[field]
            with self.subTest(field=field), self.assertRaisesRegex(
                preparer.PreparationError, f"missing required classification fields.*{field}"
            ):
                preparer.prepare_issue(settings, self.target(settings), missing)

        explicit = self.prepare(task_kind=None, primary_category=None, research_modifier=False)
        self.assertIsNone(explicit["classification"]["task_kind"])
        self.assertIsNone(explicit["classification"]["primary_category"])

    def test_cli_emits_a_linear_ready_payload(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository = root / "example-repo"
            repository.mkdir()
            config = root / "projects.toml"
            config.write_text(config_text())
            completed = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--config",
                    str(config),
                    "--repository",
                    str(repository),
                ],
                input=json.dumps(draft(research_modifier=True)),
                text=True,
                capture_output=True,
                check=True,
            )
        payload = json.loads(completed.stdout)
        self.assertEqual(payload["target"]["team"], "DEV")
        self.assertEqual(payload["target"]["project"], "Example Project")
        self.assertEqual(
            payload["label_changes"],
            {"add": ["feature-id", "research-id"], "remove": []},
        )

    def test_installed_copy_uses_bundled_config_without_source_checkout(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            installed = root / "installed" / "create-linear-task"
            shutil.copytree(ROOT / "skills" / "create-linear-task", installed, ignore=shutil.ignore_patterns("config.local.toml"))
            repository = root / "example-repo"
            (installed / "config.local.toml").write_text(config_text())
            repository.mkdir()
            completed = subprocess.run(
                [
                    sys.executable,
                    "-B",
                    str(installed / "scripts" / "prepare_issue.py"),
                    "--repository",
                    str(repository),
                ],
                input=json.dumps(draft()),
                text=True,
                capture_output=True,
                check=True,
                cwd=root,
            )
        payload = json.loads(completed.stdout)
        self.assertEqual(payload["target"]["project_key"], "example")
        self.assertEqual(payload["target"]["source"], "configuration")
        self.assertEqual(payload["target"]["team"], "DEV")
        self.assertEqual(payload["label_changes"], {"add": ["feature-id"], "remove": []})


if __name__ == "__main__":
    unittest.main()
