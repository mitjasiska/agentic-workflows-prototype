import importlib.util
import io
import json
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
import zipfile


ROOT = Path(__file__).resolve().parent.parent
PLUGIN = ROOT / "plugins" / "create-linear-task"
MARKETPLACE = ROOT / ".agents" / "plugins" / "marketplace.json"
SYNC_SCRIPT = PLUGIN / "scripts" / "sync_plugin.py"
PACKAGE_SCRIPT = PLUGIN / "scripts" / "package_plugin.py"
BUNDLED_PREPARE_SCRIPT = (
    PLUGIN / "skills" / "create-linear-task" / "scripts" / "prepare_issue.py"
)
SYNC_SPEC = importlib.util.spec_from_file_location("plugin_synchronizer", SYNC_SCRIPT)
synchronizer = importlib.util.module_from_spec(SYNC_SPEC)
sys.modules[SYNC_SPEC.name] = synchronizer
SYNC_SPEC.loader.exec_module(synchronizer)
sys.modules["sync_plugin"] = synchronizer
PACKAGE_SPEC = importlib.util.spec_from_file_location("plugin_packager", PACKAGE_SCRIPT)
packager = importlib.util.module_from_spec(PACKAGE_SPEC)
sys.modules[PACKAGE_SPEC.name] = packager
PACKAGE_SPEC.loader.exec_module(packager)


class CreateLinearTaskPluginTests(unittest.TestCase):
    def test_repository_marketplace_references_the_plugin_package(self):
        marketplace = json.loads(MARKETPLACE.read_text())

        self.assertEqual(marketplace["name"], "agentic-workflows-lite")
        self.assertEqual(
            marketplace["interface"], {"displayName": "Agentic Workflows Prototype"}
        )
        self.assertEqual(len(marketplace["plugins"]), 1)
        entry = marketplace["plugins"][0]
        self.assertEqual(entry["name"], "create-linear-task")
        self.assertEqual(
            entry["source"],
            {
                "source": "local",
                "path": "./plugins/create-linear-task",
            },
        )
        self.assertEqual(
            entry["policy"],
            {
                "installation": "AVAILABLE",
                "authentication": "ON_INSTALL",
            },
        )
        self.assertEqual(entry["category"], "Productivity")

        source_path = PurePosixPath(entry["source"]["path"])
        self.assertFalse(source_path.is_absolute())
        self.assertNotIn("..", source_path.parts)
        plugin_root = ROOT.joinpath(*source_path.parts)
        self.assertEqual(plugin_root, PLUGIN)
        self.assertTrue((plugin_root / "plugin.json").is_file())
        self.assertTrue((plugin_root / ".codex-plugin" / "plugin.json").is_file())
        self.assertTrue(
            (plugin_root / "skills" / "create-linear-task" / "SKILL.md").is_file()
        )
        plugin_manifest = json.loads((plugin_root / "plugin.json").read_text())
        self.assertEqual(plugin_manifest["name"], entry["name"])

    def test_portable_manifest_describes_a_skills_only_plugin(self):
        manifest = json.loads((PLUGIN / "plugin.json").read_text())

        self.assertEqual(
            manifest["$schema"],
            "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json",
        )
        self.assertEqual(manifest["name"], "create-linear-task")
        self.assertEqual(
            manifest["repository"],
            "https://github.com/mitjasiska/agentic-workflows-prototype",
        )
        self.assertEqual(
            manifest["homepage"],
            manifest["repository"] + "/tree/main/plugins/create-linear-task",
        )
        self.assertRegex(manifest["version"], r"^\d+\.\d+\.\d+$")
        self.assertIn("interface", manifest["extensions"]["com.openai"])
        self.assertNotIn("mcpServers", manifest)
        self.assertNotIn("apps", manifest["extensions"]["com.openai"])
        self.assertFalse((PLUGIN / "mcp.json").exists())
        self.assertFalse((PLUGIN / ".mcp.json").exists())

    def test_generated_plugin_files_match_the_canonical_skill(self):
        completed = subprocess.run(
            [sys.executable, "-B", str(SYNC_SCRIPT), "--check"],
            cwd=ROOT,
            text=True,
            capture_output=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)

        portable = json.loads((PLUGIN / "plugin.json").read_text())
        compatibility = json.loads(
            (PLUGIN / ".codex-plugin" / "plugin.json").read_text()
        )
        for field in ("name", "version", "description", "author", "homepage", "repository"):
            self.assertEqual(compatibility[field], portable[field])
        self.assertEqual(compatibility["skills"], "./skills/")
        self.assertEqual(
            compatibility["interface"],
            portable["extensions"]["com.openai"]["interface"],
        )

    def test_bundled_renderer_emits_linear_api_collapsible_syntax(self):
        workflow_labels = ["Feature", "Bug", "Chore", "Docs", "Refactor", "Research"]
        draft = {
            "title": "Add export retry limits",
            "context": None,
            "outcome": "Export retries stop after the configured limit.",
            "done_when": ["The retry limit is enforced."],
            "boundaries": [],
            "task_kind": "implementation",
            "primary_category": "feature",
            "research_modifier": False,
            "agent": {
                "repository_context": [],
                "guidance": [],
                "constraints": [],
                "validation": ["Run the focused worker tests."],
            },
            "existing_target": None,
            "existing_labels": [],
            "workspace_labels": [
                {"id": f"{name.casefold()}-id", "name": name}
                for name in workflow_labels
            ],
        }
        from test_linear_task_skill import config_text
        temporary = self.enterContext(tempfile.TemporaryDirectory())
        config = Path(temporary) / "config.local.toml"
        config.write_text(config_text())
        completed = subprocess.run(
            [
                sys.executable,
                "-B",
                str(BUNDLED_PREPARE_SCRIPT),
                "--config", str(config),
                "--repository",
                str(Path(temporary) / "example-repo"),
            ],
            input=json.dumps(draft),
            cwd=ROOT,
            text=True,
            capture_output=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        description = json.loads(completed.stdout)["issue"]["description"]

        self.assertIn("+++ Agent instructions\n", description)
        self.assertTrue(description.endswith("\n+++\n"))
        self.assertNotIn(">>> Agent instructions", description)

    def test_sync_check_detects_bundled_skill_drift(self):
        with tempfile.TemporaryDirectory() as directory:
            bundled = Path(directory) / "create-linear-task"
            shutil.copytree(PLUGIN / "skills" / "create-linear-task", bundled, ignore=shutil.ignore_patterns("config.local.toml"))
            skill = bundled / "SKILL.md"
            skill.write_text(skill.read_text() + "\nDrift.\n")

            with mock.patch.object(synchronizer, "BUNDLED_SKILL", bundled):
                differences = synchronizer.check_generated_files(
                    synchronizer.load_portable_manifest()
                )

        self.assertIn("bundled skill file differs: SKILL.md", differences)

    def test_package_archive_is_deterministic_and_contains_only_runtime_files(self):
        with tempfile.TemporaryDirectory() as directory:
            first = Path(directory) / "first.zip"
            second = Path(directory) / "second.zip"
            for output in (first, second):
                completed = subprocess.run(
                    [
                        sys.executable,
                        "-B",
                        str(PACKAGE_SCRIPT),
                        "--output",
                        str(output),
                    ],
                    cwd=ROOT,
                    text=True,
                    capture_output=True,
                )
                self.assertEqual(completed.returncode, 0, completed.stderr)

            self.assertEqual(first.read_bytes(), second.read_bytes())
            with zipfile.ZipFile(first) as archive:
                names = archive.namelist()
                self.assertEqual(names, sorted(names))
                self.assertIn("plugin.json", names)
                self.assertIn(".codex-plugin/plugin.json", names)
                self.assertIn("skills/create-linear-task/SKILL.md", names)
                self.assertIn(
                    "skills/create-linear-task/scripts/prepare_issue.py", names
                )
                self.assertNotIn("README.md", names)
                self.assertFalse(any(name.startswith("scripts/") for name in names))
                self.assertTrue(
                    all(item.date_time == (1980, 1, 1, 0, 0, 0) for item in archive.infolist())
                )

            completed = subprocess.run(
                [
                    sys.executable,
                    "-B",
                    str(PACKAGE_SCRIPT),
                    "--output",
                    str(first),
                    "--check",
                ],
                cwd=ROOT,
                text=True,
                capture_output=True,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_private_files_are_excluded_and_matching_private_snapshots_are_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            canonical, bundled = root / "canonical", root / "skills/bundled"
            shutil.copytree(synchronizer.CANONICAL_SKILL, canonical, ignore=shutil.ignore_patterns("config.local.toml"))
            shutil.copytree(synchronizer.BUNDLED_SKILL, bundled, ignore=shutil.ignore_patterns("config.local.toml"))
            with mock.patch.object(synchronizer, "CANONICAL_SKILL", canonical), \
                    mock.patch.object(synchronizer, "BUNDLED_SKILL", bundled):
                baseline = packager.build_archive()
                for skill in (canonical, bundled):
                    (skill / "config.local.toml").write_text('PRIVATE-MAPPING-SENTINEL')
                self.assertEqual(packager.build_archive(), baseline)
                self.assertNotIn(b'PRIVATE-MAPPING-SENTINEL', baseline)
                with zipfile.ZipFile(io.BytesIO(baseline)) as archive:
                    self.assertIn('skills/create-linear-task/config.example.toml', archive.namelist())
                    self.assertFalse(any(name.endswith(('config.local.toml', '/config.toml', '/projects.toml'))
                                         for name in archive.namelist()))
                for skill in (canonical, bundled):
                    public = skill / 'config.example.toml'
                    public.write_text(public.read_text().replace('Example Application', 'PRIVATE-MAPPING-SENTINEL'))
                with self.assertRaisesRegex(synchronizer.SyncError, 'private mappings cannot be packaged'):
                    packager.build_archive()
                with self.assertRaises(synchronizer.SyncError):
                    synchronizer.synchronize(synchronizer.load_portable_manifest())

    def test_old_populated_snapshot_cannot_be_distributed(self):
        with tempfile.TemporaryDirectory() as directory:
            bundled = Path(directory) / 'bundled'
            shutil.copytree(synchronizer.BUNDLED_SKILL, bundled, ignore=shutil.ignore_patterns("config.local.toml"))
            (bundled / 'config.toml').write_text('PRIVATE-MAPPING-SENTINEL')
            with mock.patch.object(synchronizer, 'BUNDLED_SKILL', bundled):
                with self.assertRaisesRegex(synchronizer.SyncError, 'unsupported public skill file'):
                    packager.build_archive()

    def test_ignored_skill_cache_files_cannot_affect_the_archive(self):
        with tempfile.TemporaryDirectory() as directory:
            bundled = Path(directory) / "create-linear-task"
            shutil.copytree(PLUGIN / "skills" / "create-linear-task", bundled, ignore=shutil.ignore_patterns("config.local.toml"))

            with mock.patch.object(synchronizer, "BUNDLED_SKILL", bundled):
                before = packager.build_archive()
                cache = bundled / "scripts" / "__pycache__" / "generated.pyc"
                cache.parent.mkdir()
                cache.write_bytes(b"host-specific cache contents")
                after = packager.build_archive()

        self.assertEqual(after, before)
        with zipfile.ZipFile(io.BytesIO(after)) as archive:
            self.assertFalse(
                any(
                    "__pycache__" in name or name.endswith((".pyc", ".pyo"))
                    for name in archive.namelist()
                )
            )


if __name__ == "__main__":
    unittest.main()
