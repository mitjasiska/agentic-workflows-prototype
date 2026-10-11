"""Real OS exclusion, independent of Git/Herdr/provider transports."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from task_start import TaskError
from task_start.contexts import ContextRegistry
from task_start.ownership import ownership_gate


class OwnershipGateTests(unittest.TestCase):
    def setUp(self):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        self.path = Path(directory) / "contexts.sqlite3"
        self.enterContext(patch("task_start.contexts.registry_path", return_value=self.path))

    def child_gate(self, exclusive):
        script = """
import sys
from pathlib import Path
from task_start import TaskError
from task_start.ownership import ownership_gate
try:
    with ownership_gate(registry=Path(sys.argv[1]), exclusive=sys.argv[2] == 'True', timeout=0):
        print('acquired')
except TaskError:
    print('busy')
"""
        return subprocess.check_output([sys.executable, "-c", script, str(self.path), str(exclusive)],
                                       cwd=Path(__file__).resolve().parent.parent, text=True).strip()

    def test_controllers_share_but_disposal_requires_exclusion(self):
        with ownership_gate():
            self.assertEqual(self.child_gate(False), "acquired")
            self.assertEqual(self.child_gate(True), "busy")
        self.assertEqual(self.child_gate(True), "acquired")

    def test_admission_excludes_other_processes_and_waiting_writers_retry(self):
        with ThreadPoolExecutor(max_workers=1) as pool:
            with ownership_gate(exclusive=True):
                with ownership_gate():
                    ContextRegistry(self.path).allocate("DEV-1", "review", agent="codex")
                self.assertEqual(self.child_gate(False), "busy")
                self.assertEqual(self.child_gate(True), "busy")
                attempt = pool.submit(ContextRegistry(self.path).allocate, "DEV-2", "review", agent="codex")
                self.assertFalse(attempt.done())
            self.assertEqual(attempt.result(timeout=5), "DEV-2-R1")
        self.assertEqual(ContextRegistry(self.path).allocate("DEV-2", "review", agent="codex"), "DEV-2-R2")

    def test_exception_releases_gate_and_shared_holder_cannot_upgrade(self):
        with self.assertRaisesRegex(RuntimeError, "interrupted"):
            with ownership_gate(exclusive=True):
                raise RuntimeError("interrupted")
        self.assertEqual(self.child_gate(True), "acquired")
        with ownership_gate(), self.assertRaisesRegex(TaskError, "Cannot upgrade"):
            with ownership_gate(exclusive=True):
                self.fail("unsafe upgrade")

    def test_registry_contention_has_bounded_actionable_diagnostic(self):
        def attempt():
            with ownership_gate(timeout=0):
                self.fail("exclusive admission should exclude this reader")
        with ownership_gate(exclusive=True), ThreadPoolExecutor(max_workers=1) as pool:
            with self.assertRaisesRegex(TaskError, "registry is busy.*retry shortly"):
                pool.submit(attempt).result()
