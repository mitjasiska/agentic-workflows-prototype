"""Exercise the documented checkout move using disposable repositories only."""

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
MOVE_BLOCK = re.search(
    r"## 4\. Approved local move and remotes\n.*?```sh\n(.*?)\n```",
    (ROOT / "docs/migration.md").read_text(),
    re.DOTALL,
).group(1)


class CheckoutMoveRunbookTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="awp-move-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.old = self.root / "old prototype café"
        self.new = self.root / "new prototype café"
        self.evidence = self.root / "evidence"
        self.evidence.mkdir()
        self.real_git = shutil.which("git")
        self.real_mv = shutil.which("mv")
        self.env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        self.env.update(
            HOME=str(self.root),
            GIT_CONFIG_GLOBAL=os.devnull,
            GIT_CONFIG_NOSYSTEM="1",
            awp_old=str(self.old),
            awp_new=str(self.new),
            awp_evidence=str(self.evidence),
        )
        self.create_repo(self.old, "preserve original checkout\n")
        self.revision = self.git(self.old, "rev-parse", "HEAD").stdout.strip()
        (self.evidence / "migration-revision.txt").write_text(self.revision + "\n")
        self.calls = self.root / "calls.jsonl"
        shims = self.root / "bin"
        shims.mkdir()
        wrapper = f"""#!{sys.executable}
import json
import os
from pathlib import Path
import sys

command = Path(sys.argv[0]).name
args = sys.argv[1:]
with open(os.environ['AWP_TEST_CALLS'], 'a') as log:
    log.write(json.dumps([command, *args]) + '\\n')
if command == 'mv':
    mode = os.environ.get('AWP_TEST_MV', '')
    if mode == 'destination_appeared':
        os.rename(os.environ['AWP_TEST_UNRELATED'], os.environ['awp_new'])
    if mode in ('skip', 'destination_appeared'):
        sys.exit(0)
if command == 'git' and os.environ.get('AWP_TEST_FAIL_STATUS') == '1':
    if 'status' in args and '--porcelain=v1' in args:
        sys.exit(128)
if command == 'git' and os.environ.get('AWP_TEST_FAIL_WORKTREE_LIST') == '1':
    if args[2:] == ['worktree', 'list', '--porcelain']:
        print('worktree ' + os.environ['awp_old'])
        sys.exit(128)
real = {{'git': {self.real_git!r}, 'mv': {self.real_mv!r}}}[command]
os.execv(real, [real, *args])
"""
        for command in ("git", "mv"):
            shim = shims / command
            shim.write_text(wrapper)
            shim.chmod(0o755)
        self.env["PATH"] = str(shims) + os.pathsep + self.env["PATH"]
        self.env["AWP_TEST_CALLS"] = str(self.calls)

    def git(self, root, *args):
        return subprocess.run(
            [self.real_git, "-C", str(root), *args],
            env=self.env, capture_output=True, text=True, check=True,
        )

    def create_repo(self, path, contents):
        path.mkdir()
        self.git(path, "init", "-q", "-b", "main")
        self.git(path, "config", "user.name", "Migration Test")
        self.git(path, "config", "user.email", "test@example.invalid")
        (path / "tracked.txt").write_text(contents)
        self.git(path, "add", "tracked.txt")
        self.git(path, "commit", "-qm", "Fixture")

    def run_move(self):
        return subprocess.run(
            ["bash", "-c", MOVE_BLOCK], cwd=self.root, env=self.env,
            capture_output=True, text=True, timeout=15,
        )

    def recorded_calls(self):
        if not self.calls.exists():
            return []
        return [json.loads(line) for line in self.calls.read_text().splitlines()]

    def assert_refused(self, result, *, before_move=True):
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        calls = self.recorded_calls()
        self.assertFalse(
            any(call[:3] == ["git", "-C", str(self.new)] for call in calls), calls
        )
        if before_move:
            self.assertFalse(any(call[0] == "mv" for call in calls), calls)
        else:
            self.assertEqual(sum(call[0] == "mv" for call in calls), 1, calls)

    def test_moves_clean_main_checkout_and_repairs_it(self):
        result = self.run_move()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(self.old.exists())
        self.assertEqual(self.git(self.new, "rev-parse", "HEAD").stdout.strip(), self.revision)
        self.assertEqual(self.git(self.new, "rev-parse", "--show-toplevel").stdout.strip(), str(self.new))
        self.assertEqual(self.git(self.new, "status", "--porcelain=v1").stdout, "")
        self.assertEqual((self.new / "tracked.txt").read_text(), "preserve original checkout\n")
        self.assertIn(["git", "-C", str(self.new), "worktree", "repair"], self.recorded_calls())

    def test_existing_destination_refuses_before_successful_skip_mv(self):
        self.create_repo(self.new, "preserve unrelated checkout\n")
        original = self.git(self.new, "rev-parse", "HEAD").stdout
        self.env["AWP_TEST_MV"] = "skip"
        self.assert_refused(self.run_move())
        self.assertEqual(self.git(self.new, "rev-parse", "HEAD").stdout, original)
        self.assertEqual((self.new / "tracked.txt").read_text(), "preserve unrelated checkout\n")
        self.assertTrue((self.old / ".git").is_dir())

    def test_existing_file_destination_is_refused(self):
        self.new.write_text("unrelated file\n")
        self.assert_refused(self.run_move())
        self.assertEqual(self.new.read_text(), "unrelated file\n")

    def test_dangling_symlink_destination_is_refused(self):
        self.new.symlink_to(self.root / "absent")
        self.assert_refused(self.run_move())
        self.assertEqual(self.new.readlink(), self.root / "absent")

    def test_missing_git_directory_refuses_before_move(self):
        (self.old / ".git").rename(self.root / "saved-git")
        self.assert_refused(self.run_move())
        self.assertEqual(self.recorded_calls(), [])
        self.assertTrue((self.old / "tracked.txt").is_file())
        self.assertFalse(self.new.exists())

    def test_symlink_source_is_refused(self):
        actual = self.root / "actual-source"
        self.old.rename(actual)
        self.old.symlink_to(actual, target_is_directory=True)
        self.assert_refused(self.run_move())
        self.assertEqual(self.recorded_calls(), [])
        self.assertTrue((actual / ".git").is_dir())

    def test_destination_appearing_during_successful_skip_is_not_inspected(self):
        unrelated = self.root / "unrelated"
        self.create_repo(unrelated, "preserve unrelated checkout\n")
        original = self.git(unrelated, "rev-parse", "HEAD").stdout
        self.env.update(AWP_TEST_MV="destination_appeared", AWP_TEST_UNRELATED=str(unrelated))
        self.assert_refused(self.run_move(), before_move=False)
        self.assertTrue((self.old / ".git").is_dir())
        self.assertEqual(self.git(self.new, "rev-parse", "HEAD").stdout, original)
        self.assertEqual((self.new / "tracked.txt").read_text(), "preserve unrelated checkout\n")

    def test_success_without_any_move_is_refused(self):
        self.env["AWP_TEST_MV"] = "skip"
        self.assert_refused(self.run_move(), before_move=False)
        self.assertTrue((self.old / ".git").is_dir())
        self.assertFalse(self.new.exists())

    def test_dirty_checkout_is_preserved(self):
        (self.old / "tracked.txt").write_text("uncommitted changes\n")
        (self.old / "untracked.txt").write_text("untracked work\n")
        self.assert_refused(self.run_move())
        self.assertEqual((self.old / "tracked.txt").read_text(), "uncommitted changes\n")
        self.assertEqual((self.old / "untracked.txt").read_text(), "untracked work\n")

    def test_failed_status_cannot_look_like_clean_checkout(self):
        self.env["AWP_TEST_FAIL_STATUS"] = "1"
        self.assert_refused(self.run_move())
        self.assertTrue((self.old / ".git").is_dir())

    def test_failed_worktree_inventory_cannot_look_like_single_checkout(self):
        self.env["AWP_TEST_FAIL_WORKTREE_LIST"] = "1"
        self.assert_refused(self.run_move())
        self.assertTrue((self.old / ".git").is_dir())

    def test_linked_worktree_is_preserved(self):
        linked = self.root / "linked-worktree"
        self.git(self.old, "worktree", "add", "-qb", "active-task", str(linked))
        (linked / "untracked.txt").write_text("active work\n")
        self.assert_refused(self.run_move())
        self.assertEqual(self.git(linked, "rev-parse", "HEAD").stdout.strip(), self.revision)
        self.assertEqual((linked / "untracked.txt").read_text(), "active work\n")

    def test_unapproved_revision_is_refused(self):
        (self.evidence / "migration-revision.txt").write_text("0" * 40 + "\n")
        self.assert_refused(self.run_move())

    def test_non_main_branch_is_refused(self):
        self.git(self.old, "switch", "-qc", "active-task")
        self.assert_refused(self.run_move())
        self.assertEqual(self.git(self.old, "branch", "--show-current").stdout.strip(), "active-task")

    def test_in_progress_operation_is_refused(self):
        marker = self.old / ".git" / "CHERRY_PICK_HEAD"
        marker.write_text(self.revision + "\n")
        self.assert_refused(self.run_move())
        self.assertEqual(marker.read_text(), self.revision + "\n")


if __name__ == "__main__":
    unittest.main()
