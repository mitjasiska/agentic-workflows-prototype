import json
from dataclasses import dataclass, replace
import os
from pathlib import Path
import re
import shlex
import stat
import subprocess
import tempfile
import unicodedata
from uuid import UUID

from . import AgentNotReady, HerdrResponseError, TaskError
from .github import MergedPull, check_history, merged_pull, repository_name
from .review_result import unique_object

_UNSELECTED = object()


def branch_name(identifier: str, title: str) -> str:
    ascii_title = unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode()
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_title.lower()).strip("-")[:100].rstrip("-")
    return identifier.lower() + (f"-{slug}" if slug else "")


def slice_slug(value: str) -> str:
    # Normalize words and accents, but reject path/ref syntax and shell controls.
    if not value.strip() or any(not (c.isalnum() or c in " _-") for c in value):
        raise TaskError("slice must contain words, spaces, hyphens or underscores")
    ascii_value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    untrimmed = re.sub(r"[^a-z0-9]+", "-", ascii_value.lower()).strip("-")
    if not untrimmed or len(untrimmed) > 100:
        raise TaskError("slice must normalize to 1–100 ASCII letters, digits or hyphens")
    return untrimmed


@dataclass(frozen=True)
class Workspace:
    branch: str
    path: Path
    workspace_id: str
    tab_id: str
    pane_id: str
    action: str
    slice: str | None = None


@dataclass(frozen=True)
class TaskWorktree:
    branch: str
    path: Path
    open_workspace_id: str | None = None


@dataclass(frozen=True)
class CleanupState:
    branch_commit: str
    base_commit: str
    pull: MergedPull | None = None


@dataclass(frozen=True)
class HerdrRetirement:
    identifier: str
    base: str
    branch: str
    path: Path
    workspace_id: str | None
    git_dir: Path | None = None
    claims: tuple[str, ...] = ()


def run(args: list[str], *, input: bytes | None = None, env: dict[str, str] | None = None,
        timeout: float = 120) -> str:
    try:
        result = subprocess.run(args, input=input, env=env, capture_output=True, timeout=timeout, check=False)
    except FileNotFoundError:
        raise TaskError(f"{args[0]} is not installed or not on PATH") from None
    except (OSError, subprocess.TimeoutExpired):
        raise TaskError(f"{args[0]} could not complete; inspect its state before retrying") from None
    if result.returncode:
        # Commands can include authenticated remote URLs in stderr; don't echo them.
        operation = (args[5] if args[3] == "-c" else args[3]) if args[0] == "git" else " ".join(args[1:3])
        hint = "inspect it manually"
        error_type = TaskError
        if args[0] == "herdr":
            hint = "check the running Herdr session, repository trust, and worktree state"
            try:
                code = json.loads(result.stderr)["error"]["code"]
                if isinstance(code, str) and re.fullmatch(r"[a-z_]+", code):
                    hint = f"{code}; {hint}"
                    if args[1:3] == ["agent", "start"] and code == "agent_not_ready":
                        error_type = AgentNotReady
            except (ValueError, KeyError, TypeError):
                pass
        elif operation in {"fetch", "ls-remote"}:
            hint = ("check remote access and whether the upstream base branch exists; for SSH, "
                    "check SSH_AUTH_SOCK and ssh-add -l, load the Git key with ssh-add, "
                    "and verify host trust/access manually (docs/configuration.md#ssh-agent-on-linux)")
        elif operation == "merge":
            hint = "base could not be updated safely; inspect the checkout before retrying"
        raise error_type(f"{args[0]} {operation} failed (exit {result.returncode}); {hint}")
    try:
        # Git emits filesystem bytes for unquoted paths. Preserve undecodable
        # bytes with the filesystem codec's error handler (surrogateescape on POSIX).
        return os.fsdecode(result.stdout) if args[0] == "git" else result.stdout.decode("utf-8")
    except UnicodeError:
        contract = "filesystem encoding" if args[0] == "git" else "UTF-8 JSON"
        raise TaskError(f"{args[0]} output could not be decoded as {contract}; inspect its state before retrying") from None


class Git:
    def __init__(self, repo: Path):
        self.repo = repo

    def command(self, *args: str) -> str:
        return run(["git", "-C", str(self.repo), *args])

    def check_repository(self) -> Path:
        """Validate the permanent repository identity without requiring an idle base."""
        if not self.repo.is_dir():
            raise TaskError(f"Repository does not exist: {self.repo}")
        if not (self.repo / ".git").exists():
            raise TaskError(f"Configured path is not a Git checkout root: {self.repo}")
        if (self.command("rev-parse", "--is-inside-work-tree").strip() != "true"
                or Path(self.command("rev-parse", "--show-toplevel").strip()).resolve() != self.repo):
            raise TaskError("Configured repository must be a Git checkout root")
        git_dir = self.command("rev-parse", "--absolute-git-dir").strip()
        common = self.command("rev-parse", "--path-format=absolute", "--git-common-dir").strip()
        if Path(git_dir).resolve() != Path(common).resolve():
            raise TaskError("Configured repository must be the permanent checkout, not a linked worktree")
        return Path(git_dir)

    def check_base(self, base: str) -> None:
        git_dir = self.check_repository()
        self.command("check-ref-format", f"refs/heads/{base}")
        refs = self.command("for-each-ref", "--format=%(refname)", "refs/heads").splitlines()
        if f"refs/heads/{base}" not in refs:
            raise TaskError(f"Configured base branch {base!r} does not exist")
        if self.command("rev-parse", "--symbolic-full-name", "HEAD").strip() != f"refs/heads/{base}":
            raise TaskError(f"Permanent checkout must already be on {base!r}; switch it manually")
        if self.command("status", "--porcelain", "--untracked-files=all", "--ignore-submodules=none").strip():
            raise TaskError("Permanent checkout has local modifications or untracked files; resolve them first")
        for marker in ("MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "rebase-merge", "rebase-apply", "sequencer", "BISECT_LOG"):
            if (Path(git_dir) / marker).exists():
                raise TaskError("Permanent checkout has an unfinished Git operation; finish it first")

    def update_base(self, base: str) -> None:
        from .publication_state import PublicationStore
        with PublicationStore(self.repo).repository_locked():
            self._update_base(base)

    def _update_base(self, base: str) -> None:
        self.check_base(base)
        remote = self.command("config", "--default", "", "--get", f"branch.{base}.remote").strip()
        upstream = self.command("config", "--default", "", "--get", f"branch.{base}.merge").strip()
        if remote == "." or remote.startswith("-") or not remote:
            raise TaskError("Base branch must track a remote branch")
        if upstream != f"refs/heads/{base}":
            raise TaskError("Base upstream must have the same branch name as the configured base")
        # Fetch only into FETCH_HEAD; configured forced refspecs cannot move branches.
        self.command("fetch", "--no-tags", "--no-recurse-submodules", "--refmap=", remote, upstream)
        fetched = self.command("rev-parse", "--verify", "FETCH_HEAD^{commit}").strip()
        counts = self.command("rev-list", "--left-right", "--count", f"HEAD...{fetched}").split()
        if len(counts) != 2 or counts[0] != "0":
            raise TaskError("Base branch has local-only commits or diverges from its upstream; resolve it manually")
        self.check_base(base)
        # Override branch-local merge options only for this invocation. In
        # particular, --squash can otherwise report success without moving HEAD.
        self.command("-c", f"branch.{base}.mergeOptions=", "merge", "--ff-only",
                     "--no-squash", "--no-autostash", "--no-overwrite-ignore", fetched)
        if self.command("rev-parse", "--verify", "HEAD^{commit}").strip() != fetched:
            raise TaskError("Base update did not reach the fetched upstream commit; inspect the checkout before retrying")
        self.check_base(base)

    def branches(self, identifier: str) -> list[str]:
        branches = self.command("for-each-ref", "--format=%(refname:strip=2)", "refs/heads").splitlines()
        return [branch for branch in branches if belongs_to_issue(branch, identifier)]

    def remote_branches(self, identifier: str) -> list[str]:
        branches = set()
        for remote in self.command("remote").splitlines():
            if not remote or remote.startswith("-"):
                raise TaskError("Invalid Git remote name; inspect repository configuration")
            # Read the server's refs. Cached refs/remotes/* (and forced fetch
            # refspecs) never participate in workspace selection.
            output = self.command("ls-remote", "--heads", "--", remote)
            for line in output.splitlines():
                parts = line.split("\t")
                if (len(parts) != 2 or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", parts[0])
                        or not parts[1].startswith("refs/heads/")):
                    raise TaskError("Unexpected Git remote branch response")
                branch = parts[1].removeprefix("refs/heads/")
                if belongs_to_issue(branch, identifier):
                    branches.add(branch)
        return sorted(branches)

    def history_remote(self, base: str, branch: str) -> str:
        remote = self.command("config", "--default", "", "--get", f"branch.{base}.remote").strip()
        url = self.command("remote", "get-url", remote).strip()
        repo_name = repository_name(url)
        for other in self.command("remote").splitlines():
            other_url = self.command("remote", "get-url", other).strip()
            other_repo = repository_name(other_url)
            if other_url != url and (repo_name is None or other_repo is None
                                     or other_repo.casefold() != repo_name.casefold()):
                raise TaskError(f"Cannot establish complete PR history for {branch}: remotes point to "
                                "different repositories. Inspect cross-repository PR/merge state manually "
                                "or choose a new --slice; no workspace was opened or deleted")
        return url

    def check_history(self, base: str, branch: str, *, existing: bool) -> None:
        if existing:
            url = self.history_remote(base, branch)
        else:
            remote = self.command("config", "--default", "", "--get", f"branch.{base}.remote").strip()
            url = self.command("remote", "get-url", remote).strip()
        check_history(url, branch, existing=existing)

    def cleanup_merge(self, base: str, branch: str, identifier: str, *, head: str | None = None,
                      require_pull: bool = False) -> CleanupState:
        # Disposal retries retain the exact tip after the local ref is removed.
        if head is None:
            head = self.command("rev-parse", "--verify", f"refs/heads/{branch}^{{commit}}").strip()
        base_commit = self.command("rev-parse", "--verify", f"refs/heads/{base}^{{commit}}").strip()
        # A journaled squash head may have been pruned after ref deletion. Its
        # SHA still binds the authoritative PR; only the merge result must exist.
        if not require_pull and self.command("rev-list", "--count", f"{base_commit}..{head}").strip() == "0":
            return CleanupState(head, base_commit)
        try:
            if self.command("config", "--default", "", "--get", f"branch.{base}.merge").strip() != f"refs/heads/{base}":
                raise TaskError("Base upstream must match the configured base branch")
            pull = merged_pull(self.history_remote(base, branch), branch, base, head)
            # A merged PR is insufficient if the checkout's base is stale or
            # the resulting commit belongs to some other line of development.
            # Missing objects must not trigger an implicit partial-clone fetch.
            local_env = {**os.environ, "GIT_NO_LAZY_FETCH": "1"}
            try:
                count = run(["git", "-C", str(self.repo), "rev-list", "--count",
                             f"{base_commit}..{pull.merge_commit}"], env=local_env).strip()
            except TaskError:
                # Batch mode reports a missing object explicitly, without
                # conflating it with a failed Git command or parsing stderr.
                try:
                    missing = run(["git", "-C", str(self.repo), "cat-file",
                                   "--batch-check=%(objectname) %(objecttype)"],
                                  input=f"{pull.merge_commit}\n".encode("ascii"),
                                  env=local_env).strip() == f"{pull.merge_commit} missing"
                except TaskError:
                    missing = False
                if not missing:
                    raise
                merge_in_base = False
            else:
                if not re.fullmatch(r"[0-9]+", count):
                    raise TaskError("Unexpected Git merge reachability response; inspect it manually")
                merge_in_base = count == "0"
        except TaskError as error:
            raise TaskError(f"Task branch {branch!r} is not fully merged into {base!r} by ancestry, "
                            f"and its PR merge could not be verified: {error}; nothing was removed") from None
        if not merge_in_base:
            raise TaskError(f"GitHub confirms PR #{pull.number} is merged, but its merge commit is not present "
                            f"in the expected local base {base!r} in the permanent repository checkout.\n\n"
                            f"{self._cleanup_base_recovery(identifier)}\n\nNothing was removed.")
        return CleanupState(head, base_commit, pull)

    def _cleanup_base_recovery(self, identifier: str) -> str:
        # Location only tailors recovery after merge evidence has established
        # the stale base. Failed location inspection must not hide that result.
        checkout = None
        try:
            cwd = Path.cwd().resolve()
            if cwd == self.repo:
                checkout = self.repo
            else:
                paths = [Path(tree["worktree"]).resolve() for tree in self.worktrees()]
                # A linked worktree may itself be nested in the permanent one.
                checkout = max((path for path in paths if cwd.is_relative_to(path)),
                               key=lambda path: len(path.parts), default=None)
        except (OSError, RuntimeError, TaskError):
            pass
        repo = shlex.quote(str(self.repo))
        location = ""
        if checkout is not None and checkout != self.repo:
            location = ("You are currently running this command from a linked worktree.\n"
                        "Do not update the base branch here.\n\n")
        navigation = "" if checkout == self.repo else f"  cd {repo}\n"
        return (f"Permanent checkout:\n  {repo}\n\n{location}"
                "Update the permanent checkout, then retry:\n\n"
                f"{navigation}  git pull --ff-only\n  task cleanup {identifier}")

    def worktrees(self) -> list[dict]:
        output = self.command("worktree", "list", "--porcelain", "-z")
        entries = []
        for record in output.split("\0\0"):
            fields = dict(field.partition(" ")[::2] for field in record.split("\0") if field)
            if fields:
                if (not fields.get("worktree") or not Path(fields["worktree"]).is_absolute()
                        or ("branch" in fields and not fields["branch"].startswith("refs/heads/"))):
                    raise TaskError("Unexpected Git worktree response; inspect workspace state")
                entries.append(fields)
        return entries

    def check_cleanup_target(self, base: str, target: TaskWorktree, identifier: str,
                             *, require_clean: bool = True, require_base: bool = True,
                             allow_missing_scope: bool = False) -> None:
        """Revalidate all local target identity and safety checks without external lookups."""
        if require_base:
            self.check_base(base)
        else:
            self.check_repository()
        path, branch = target.path, target.branch
        common = Path(self.command("rev-parse", "--path-format=absolute", "--git-common-dir").strip()).resolve()
        if (branch == base or not belongs_to_issue(branch, identifier)
                or path == self.repo or self.repo.is_relative_to(path)
                or common.is_relative_to(path) or path.is_relative_to(common)):
            raise TaskError("Cleanup target overlaps the permanent checkout or Git metadata; nothing was removed")
        trees = self.worktrees()
        matches = [tree for tree in trees if tree.get("branch") == f"refs/heads/{branch}"
                   or Path(tree["worktree"]).resolve() == path]
        if (len(matches) != 1 or matches[0].get("branch") != f"refs/heads/{branch}"
                or Path(matches[0]["worktree"]).resolve() != path
                or any(flag in matches[0] for flag in ("locked", "prunable", "bare", "detached"))):
            raise TaskError("Git task worktree registration changed or is unusable; nothing was removed")
        if any(Path(tree["worktree"]).resolve().is_relative_to(path)
               for tree in trees if tree is not matches[0]):
            raise TaskError("Another registered worktree is inside the cleanup target; nothing was removed")
        checkout = Git(path)
        git_dir = Path(checkout.command("rev-parse", "--absolute-git-dir").strip()).resolve()
        if (not (path / ".git").is_file() or (path / ".git").is_symlink() or git_dir == common
                or Path(checkout.command("rev-parse", "--show-toplevel").strip()).resolve() != path
                or Path(checkout.command("rev-parse", "--path-format=absolute", "--git-common-dir").strip()).resolve() != common
                or checkout.command("symbolic-ref", "HEAD").strip() != f"refs/heads/{branch}"):
            raise TaskError("Cleanup checkout does not match its registered repository/branch; nothing was removed")
        try:
            if Path(os.fsdecode((git_dir / "gitdir").read_bytes()).rstrip("\n")).resolve() != path / ".git":
                raise TaskError("Cleanup checkout metadata points to another worktree; nothing was removed")
        except (OSError, UnicodeError):
            raise TaskError("Cannot verify cleanup checkout metadata; nothing was removed") from None
        scope = self.scope_file(path)
        if not os.path.lexists(scope):
            if not allow_missing_scope:
                raise TaskError(f"Cleanup task identity is unknown for {branch!r}: workspace scope metadata is missing; "
                                f"quit its agents and retry task cleanup {identifier} --force for verified merged "
                                "recovery; nothing was removed")
        else:
            if scope.is_symlink() or not scope.is_file():
                raise TaskError("Workspace scope metadata is not a regular file; nothing was removed")
            self.resolve_scope(path, branch, identifier, None)
        if require_clean:
            self.check_task_clean(path, git_dir)

    def discard_cleanup_artifacts(self, base: str, target: TaskWorktree, identifier: str, *, before_discard=None) -> bool:
        """Remove only a complete, positively classified Python cache dirty state."""
        self.check_cleanup_target(base, target, identifier, require_clean=False)
        git_dir = Path(Git(target.path).command("rev-parse", "--absolute-git-dir").strip()).resolve()
        removed = self.check_task_clean(target.path, git_dir, discard_disposable=True, before_discard=before_discard)
        # Re-run the complete target validation after the only permitted
        # mutation. New or changed content must block worktree removal.
        self.check_cleanup_target(base, target, identifier)
        return removed

    def check_cleanup(self, base: str, target: TaskWorktree, identifier: str) -> CleanupState:
        """Check local prerequisites and collect merge evidence without changing task state."""
        self.check_cleanup_target(base, target, identifier)
        branch = target.branch
        state = self.cleanup_merge(base, branch, identifier)
        if state.pull is None and self.command("-c", f"branch.{branch}.remote=", "for-each-ref",
                                              "--format=%(upstream)", f"refs/heads/{branch}").strip():
            raise TaskError("Cannot bind safe branch deletion to the permanent base checkout; nothing was removed")
        return state

    def check_task_clean(self, path: Path, git_dir: Path, *, discard_disposable: bool = False, before_discard=None) -> bool:
        checkout = Git(path)
        hidden_index_state = any(entry and (entry[0].islower() or entry[0] == "S")
                                 for entry in checkout.command("ls-files", "-v", "-z").split("\0"))
        # Unlike normal status, include ignored files: worktree remove would
        # otherwise discard ignored notes, build output, or local configuration.
        # Do not let permissive repository settings hide mode/type changes.
        status_output = checkout.command("-c", "core.fileMode=true", "-c", "core.symlinks=true",
                                         "status", "--porcelain=v1", "-z", "--untracked-files=all",
                                         "--ignored", "--ignore-submodules=none")
        unfinished = any((git_dir / marker).exists() for marker in (
            "MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "rebase-merge",
            "rebase-apply", "sequencer", "BISECT_LOG"))
        has_submodules = any(entry.startswith("160000 ")
                             for entry in checkout.command("ls-files", "--stage", "-z").split("\0"))
        # Inspect every safety input before deleting even an allowlisted cache.
        if hidden_index_state:
            raise TaskError("Task worktree has assume-unchanged or skip-worktree files; "
                            "cleanliness cannot be verified; nothing was removed")
        if unfinished:
            raise TaskError("Task worktree has an unfinished Git operation; nothing was removed")
        if has_submodules:
            # Git refuses non-force removal of worktrees containing submodules.
            raise TaskError("Task worktree contains submodules; inspect it manually; nothing was removed")
        if not status_output:
            return False
        if discard_disposable and self._discard_python_cache(path, status_output, before_discard=before_discard):
            return True
        if status_output:
            raise TaskError(f"Task worktree is dirty (modified, untracked or ignored files): {path}; nothing was removed")
        return False

    def _discard_python_cache(self, path: Path, status_output: str, *, before_discard=None) -> bool:
        records = status_output.split("\0")
        if records and not records[-1]:
            records.pop()
        cache_files: dict[Path, set[str]] = {}
        for record in records:
            # Only Git-confirmed ignored entries may be disposable. In
            # particular, an untracked .pyc remains valuable unknown content.
            if not record.startswith("!! "):
                return False
            relative = Path(record[3:])
            if (relative.is_absolute() or not relative.parts or ".." in relative.parts
                    or relative.parent.name != "__pycache__"
                    or relative.parent.parts.count("__pycache__") != 1
                    or relative.suffix not in {".pyc", ".pyo"}):
                return False
            cache_files.setdefault(relative.parent, set()).add(relative.name)
        if not cache_files:
            return False

        # Validate every directory and every entry before changing anything.
        # This also protects clean tracked files or ignored notes that happen to
        # live beside generated bytecode in a __pycache__ directory.
        try:
            for directory, expected_names in cache_files.items():
                current = path
                for component in directory.parts:
                    current /= component
                    if not stat.S_ISDIR(current.lstat().st_mode):
                        return False
                actual_names = set(os.listdir(current))
                if actual_names != expected_names:
                    return False
                for name in actual_names:
                    if (Path(name).suffix not in {".pyc", ".pyo"}
                            or not stat.S_ISREG((current / name).lstat().st_mode)):
                        return False
            if before_discard is not None:
                before_discard()
            for directory, names in cache_files.items():
                cache = path / directory
                for name in sorted(names):
                    (cache / name).unlink()
            for directory in sorted(cache_files, key=lambda item: len(item.parts), reverse=True):
                (path / directory).rmdir()
        except OSError as error:
            raise TaskError("Disposable Python cache cleanup could not be completed safely; "
                            "inspect the task worktree before retrying; Git cleanup was not started") from error
        return True

    def retirement_file(self, identifier: str) -> Path:
        if not re.fullmatch(r"[A-Z][A-Z0-9]*-[1-9][0-9]*", identifier):
            raise TaskError("Cannot store cleanup state for an invalid task identifier")
        common = Path(self.command("rev-parse", "--path-format=absolute", "--git-common-dir").strip())
        if not common.is_absolute():
            raise TaskError("Git did not identify its private metadata directory")
        return common / "agentic-workflows-cleanup" / f"{identifier}.json"

    def disposal_file(self, identifier: str, execution_id: str | None = None) -> Path:
        try:
            if execution_id is not None and str(UUID(execution_id)) != execution_id:
                raise ValueError("invalid execution ID")
        except (ValueError, TypeError, AttributeError):
            raise TaskError("Invalid forced cleanup execution identity") from None
        name = "state.json" if execution_id is None else f"{execution_id}.json"
        return self.retirement_file(identifier).with_suffix(".discard") / name

    def disposal_files(self, identifier: str) -> list[Path]:
        directory = self.disposal_file(identifier).parent
        if directory.resolve() != directory:
            raise TaskError("Forced cleanup metadata path is aliased")
        try:
            return sorted(p for p in directory.iterdir() if p.suffix == ".json")
        except FileNotFoundError:
            return []

    def check_disposal(self, identifier: str) -> None:
        """Interrupted disposal must never become permission to recreate execution."""
        try:
            for path in self.disposal_files(identifier):
                if path.resolve() != path or path.is_symlink() or path.stat().st_size > 4 * 1024 * 1024:
                    raise ValueError("invalid disposal record")
                record = json.loads(path.read_text(), object_pairs_hook=unique_object)
                if record["version"] == 1:
                    if path.name != "state.json":
                        raise ValueError("invalid legacy disposal identity")
                elif record["version"] == 2:
                    if (not isinstance(record["execution_id"], str)
                            or path != self.disposal_file(identifier, record["execution_id"])):
                        raise ValueError("invalid disposal identity")
                else:
                    raise ValueError("invalid disposal version")
                if (record["issue"] != identifier
                        or record["repository"] != str(self.repo) or record["state"] != "complete"
                        or record["workspace_state"] != "closed" or record["runtime"] is not None
                        or any(record[k] != "removed" for k in ("worktree_state", "branch_state"))
                        or any(r["state"] != "removed" for r in record["roots"])
                        or any(c["state"] != "retired" or not c["retired_at"] for c in record["contexts"])):
                    raise ValueError("pending disposal")
        except (OSError, ValueError, KeyError, TypeError, RecursionError):
            raise TaskError(f"Forced cleanup for {identifier} is pending or uncertain; "
                            f"inspect and rerun task cleanup {identifier} --force before other lifecycle work") from None

    def load_retirement(self, identifier: str, base: str) -> HerdrRetirement | None:
        record = self.retirement_file(identifier)
        try:
            if record.is_symlink() or record.stat().st_size > 1024 * 1024:
                raise ValueError("invalid retirement file")
            data = json.loads(record.read_text(encoding="utf-8"), object_pairs_hook=unique_object)
        except FileNotFoundError:
            return None
        except (OSError, ValueError):
            raise TaskError(f"Cannot read pending Herdr retirement state: {record}; inspect it manually") from None
        try:
            if (not isinstance(data, dict) or type(data.get("version")) is not int
                    or any(not isinstance(data.get(key), str) for key in (
                        "identifier", "base", "branch", "path"))):
                raise ValueError("invalid retirement fields")
            path = Path(data["path"])
            state = HerdrRetirement(data["identifier"], data["base"], data["branch"],
                                    path.resolve(), data["workspace_id"],
                                    Path(data["git_dir"]) if data.get("git_dir") else None,
                                    tuple(data.get("claims", ())))
            if (data != self.retirement_data(state)
                    or state.identifier != identifier or state.base != base
                    or not belongs_to_issue(state.branch, identifier) or not path.is_absolute()
                    or state.workspace_id is not None and (
                        not isinstance(state.workspace_id, str) or not state.workspace_id.strip())
                    or state.git_dir is None and state.workspace_id is None):
                raise ValueError("retirement identity mismatch")
            if state.git_dir is not None:
                common = record.parent.parent
                if (state.git_dir.parent != common / "worktrees" or state.git_dir.resolve() != state.git_dir
                        or path.resolve() != path or path == self.repo or self.repo.is_relative_to(path)
                        or path.is_relative_to(common) or len(state.claims) > 2048
                        or any(not isinstance(p, str) or not Path(p).is_absolute()
                               or Path(p).resolve() != Path(p) for p in state.claims)):
                    raise ValueError("invalid removal reservation")
        except (KeyError, TypeError, ValueError, OSError):
            raise TaskError(f"Invalid pending Herdr retirement state: {record}; inspect it manually") from None
        return state

    @staticmethod
    def retirement_data(state):
        data = {"version": 1, "identifier": state.identifier, "base": state.base,
                "branch": state.branch, "path": str(state.path), "workspace_id": state.workspace_id}
        if state.git_dir is not None:
            data.update(version=2, git_dir=str(state.git_dir), claims=list(state.claims))
        return data

    def save_retirement(self, state: HerdrRetirement) -> None:
        """Fsync the reservation before releasing exclusive claim admission."""
        record = self.retirement_file(state.identifier)
        data = self.retirement_data(state)
        temporary = None
        try:
            record.parent.mkdir(mode=0o700, exist_ok=True)
            previous = self.load_retirement(state.identifier, state.base)
            if previous is not None and previous not in (state, replace(state, git_dir=None, claims=())):
                raise TaskError("Pending Herdr retirement state identifies a different task workspace; nothing was removed")
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=record.parent, delete=False) as output:
                temporary = Path(output.name)
                json.dump(data, output)
                output.write("\n")
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, record)
            for directory in (record.parent, record.parent.parent):
                fd = os.open(directory, os.O_RDONLY)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
        except OSError:
            raise TaskError(f"Could not save exact Herdr retirement state in {record}; nothing was removed") from None
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def clear_retirement(self, state: HerdrRetirement) -> None:
        if self.load_retirement(state.identifier, state.base) != state:
            raise TaskError("Pending Herdr retirement state changed; it was not cleared")
        try:
            self.retirement_file(state.identifier).unlink()
        except OSError:
            raise TaskError("Herdr workspace retirement was confirmed, but its private retry state "
                            "could not be cleared; inspect Git metadata before retrying") from None

    def describe_worktree_removal(self, path: Path) -> str:
        # A failed/timed-out command may already have removed the worktree.
        # Inspect both sources even when one cannot be read. lstat also sees
        # dangling symlinks, which must not count as an absent checkout path.
        try:
            path.lstat()
            present = True
        except FileNotFoundError:
            present = False
        except OSError:
            present = None
        try:
            registered = any(Path(tree["worktree"]).resolve() == path for tree in self.worktrees())
        except (TaskError, OSError, RuntimeError):
            registered = None
        if present is False and registered is False:
            return f"Confirmed removed worktree: {path}"
        if present is True and registered is True:
            return f"Worktree path is still present and registered (contents may be incomplete): {path}"
        return f"Worktree removal state is unknown or inconsistent: {path}"

    def remove_task(self, base: str, target: TaskWorktree, identifier: str,
                    expected: CleanupState, *, before_remove=None, before_delete=None) -> None:
        if self.check_cleanup(base, target, identifier) != expected:
            raise TaskError("Task, base branch or merge evidence changed during cleanup; nothing was removed")
        from .ownership import ownership_gate
        from .publication_state import PublicationStore
        with PublicationStore(self.repo).repository_locked():
            with ownership_gate(exclusive=True):
                if before_remove is not None:
                    before_remove()
                reservation = self.load_retirement(identifier, base)
                if (reservation is None or reservation.path != target.path or reservation.git_dir is None):
                    raise TaskError("Worktree removal requires its durable exact resource reservation")
            # Recursive deletion may be slow. The task/repository locks and
            # fsynced reservation exclude overlapping work without a global wait.
            self._remove_task(base, target, identifier, expected, before_delete=before_delete)

    def _remove_task(self, base, target, identifier, expected, *, before_delete=None):
        if (self.command("rev-parse", "--verify", f"refs/heads/{target.branch}^{{commit}}").strip(),
                self.command("rev-parse", "--verify", f"refs/heads/{base}^{{commit}}").strip()) != (
                    expected.branch_commit, expected.base_commit):
            raise TaskError("Task or base branch changed during merge verification; nothing was removed")
        # External evidence can take time: re-read the entire local target,
        # including registration, symbolic HEAD and scope, immediately before
        # removal. A clean checkout alone is not proof of its identity.
        self.check_cleanup_target(base, target, identifier)
        if before_delete is not None:
            before_delete()
        try:
            self.command("worktree", "remove", "--", str(target.path))
        except TaskError as error:
            state = self.describe_worktree_removal(target.path)
            raise TaskError(f"Worktree removal command failed or was uncertain: {error}. {state}. "
                            f"Local branch {target.branch!r} was not deleted; inspect Git state before retrying") from None
        removal_confirmed = False
        try:
            if (target.path.exists() or any(Path(tree["worktree"]).resolve() == target.path
                                           for tree in self.worktrees())):
                raise TaskError("Git did not confirm worktree removal")
            removal_confirmed = True
            current = (self.command("rev-parse", "--verify", f"refs/heads/{target.branch}^{{commit}}").strip(),
                       self.command("rev-parse", "--verify", f"refs/heads/{base}^{{commit}}").strip())
            if current != (expected.branch_commit, expected.base_commit):
                raise TaskError("Task or base branch changed after worktree removal")
            # With no upstream, branch -d checks HEAD (the validated base).
            # Disable tracking only for this invocation; merge is multivalued
            # and cannot be safely replaced by appending a -c override.
            if self.command("symbolic-ref", "HEAD").strip() != f"refs/heads/{base}":
                raise TaskError("Permanent checkout changed branches after worktree removal")
            if expected.pull is None:
                self.command("-c", f"branch.{target.branch}.remote=",
                             "branch", "--delete", "--", target.branch)
            else:
                # Squashed commits cannot pass branch -d's ancestry guard.
                # Use the verified PR proof and atomically require the exact
                # original head. Never follow a symbolic ref or force-delete.
                if any(tree.get("branch") == f"refs/heads/{target.branch}" for tree in self.worktrees()):
                    raise TaskError("Task branch is still checked out in a registered worktree")
                self.command("update-ref", "--no-deref", "-d", f"refs/heads/{target.branch}", expected.branch_commit)
        except TaskError as error:
            worktree_state = (f"Removed worktree: {target.path}" if removal_confirmed else
                              f"Worktree removal could not be confirmed: {target.path}")
            raise TaskError(f"Task cleanup is incomplete. {worktree_state}. "
                            f"Local branch deletion could not be confirmed for {target.branch!r}: {error}. "
                            "Inspect local state before retrying") from None

    def scope_file(self, path: Path) -> Path:
        directory = Git(path).command("rev-parse", "--absolute-git-dir").strip()
        if not directory or not Path(directory).is_absolute():
            raise TaskError("Git did not identify the worktree metadata directory")
        return Path(directory) / "agentic-workflows-scope.json"

    def resolve_scope(self, path: Path, branch: str, identifier: str,
                      requested: str | None) -> str | None:
        record = self.scope_file(path)
        try:
            data = json.loads(record.read_text(encoding="utf-8"))
        except FileNotFoundError:
            if requested is None:
                raise TaskError(f"Workspace scope is unknown for {branch}; rerun with explicit --slice "
                                "<branch suffix> to establish its scope. Branch names and titles cannot "
                                "distinguish legacy default workspaces from slices") from None
            # Selection already matched the explicitly requested slice branch.
            return requested
        except (OSError, ValueError):
            raise TaskError(f"Cannot read workspace scope metadata: {record}; inspect it before retrying") from None
        try:
            scope = data["slice"]
            if (data != {"version": 1, "identifier": identifier, "branch": branch, "slice": scope}
                    or type(data["version"]) is not int
                    or (scope is not None and (not isinstance(scope, str) or slice_slug(scope) != scope
                                              or branch != branch_name(identifier, scope)))):
                raise ValueError("scope identity mismatch")
        except (KeyError, TypeError, ValueError, TaskError):
            raise TaskError(f"Invalid workspace scope metadata: {record}; inspect it before retrying") from None
        if requested is not None and scope != requested:
            raise TaskError(f"Requested slice {requested!r} conflicts with the recorded scope for {branch}; "
                            "choose a different slice or inspect the workspace metadata")
        return scope

    def save_scope(self, path: Path, branch: str, identifier: str, scope: str | None) -> None:
        record = self.scope_file(path)
        data = {"version": 1, "identifier": identifier, "branch": branch, "slice": scope}
        try:
            # Never overwrite existing identity. Metadata belongs to the Git
            # worktree lifetime, outside tracked files and Herdr display labels.
            with record.open("x", encoding="utf-8") as output:
                json.dump(data, output)
                output.write("\n")
        except FileExistsError:
            if self.resolve_scope(path, branch, identifier, scope) != scope:
                raise TaskError(f"Workspace scope changed for {branch}; inspect it before retrying") from None
        except OSError:
            raise TaskError(f"Workspace ready, but scope metadata could not be saved: {record}; "
                            "inspect it before retrying. Workspace left intact") from None


def belongs_to_issue(branch: str, identifier: str) -> bool:
    return branch.lower() == identifier.lower() or branch.lower().startswith(identifier.lower() + "-")


class Herdr:
    def __init__(self, repo: Path):
        self.repo = repo

    def check_agent_retirement(self):
        """Linux close semantics validated against the installed V1 protocol.

        CLI help alone cannot establish server support. Unknown versions refuse
        automatic agent termination until their close semantics are validated.
        """
        status = run(["herdr", "status", "server"], timeout=10)
        facts = dict(re.findall(r"^([a-z_]+):\s*(\S+)\s*$", status, re.MULTILINE))
        if (facts.get("status") != "running" or facts.get("version") != "0.9.1"
                or facts.get("private_protocol") != "22" or facts.get("endpoint_compatible") != "yes"
                or facts.get("private_protocol_compatible") != "yes"):
            raise TaskError("Installed Herdr server lacks verified owned-agent close semantics; "
                            "quit the selected agents to their original shells before --force cleanup")
        help_text = run(["herdr", "workspace", "close", "--help"], timeout=10)
        if "herdr workspace close <workspace_id>" not in help_text:
            raise TaskError("Installed Herdr CLI lacks exact workspace close support")

    def command(self, operation: str, *args: str) -> dict:
        output = run(["herdr", "worktree", operation, "--cwd", str(self.repo), *args])
        try:
            payload = json.loads(output)
            result = payload["result"]
            expected = {"list": "worktree_list", "create": "worktree_created", "open": "worktree_opened"}
            if payload.get("error") or result["type"] != expected[operation]:
                raise ValueError("unexpected result")
            return result
        except (ValueError, KeyError, TypeError):
            raise TaskError(f"Unexpected Herdr {operation} response; inspect workspace state before retrying") from None

    def workspace_command(self, operation: str, *args: str) -> dict:
        output = run(["herdr", "workspace", operation, *args])
        try:
            payload = json.loads(output)
            result = payload["result"]
            expected = {"list": "workspace_list", "close": "workspace_closed"}
            if operation == "close" and isinstance(result, dict) and args:
                reported = [result.get("workspace_id")]
                if isinstance(result.get("workspace"), dict):
                    reported.append(result["workspace"].get("workspace_id"))
                if any(value is not None and value != args[0] for value in reported):
                    raise TaskError("Herdr close response identifies another workspace; inspect before retrying")
            if payload.get("error") or result["type"] != expected[operation]:
                raise ValueError("unexpected result")
            return result
        except (ValueError, KeyError, TypeError):
            raise HerdrResponseError(f"Unexpected Herdr workspace {operation} response; "
                            "inspect workspace state before retrying") from None

    def workspaces(self) -> list[dict]:
        result = self.workspace_command("list")
        try:
            entries = result["workspaces"]
            if not isinstance(entries, list):
                raise ValueError("invalid workspaces")
            ids = []
            for entry in entries:
                if not isinstance(entry, dict):
                    raise ValueError("invalid workspace")
                workspace_id = entry["workspace_id"]
                if (not isinstance(workspace_id, str) or not workspace_id.strip()
                        or not isinstance(entry.get("label"), str)):
                    raise ValueError("invalid workspace identity")
                ids.append(workspace_id)
            if len(set(ids)) != len(ids):
                raise ValueError("duplicate workspace ID")
            return entries
        except (KeyError, TypeError, ValueError):
            raise TaskError("Unexpected Herdr workspace list; inspect workspace state before retrying") from None

    def retirement(self, target: TaskWorktree, identifier: str,
                   base: str) -> HerdrRetirement | None:
        if target.open_workspace_id is None:
            return None
        state = HerdrRetirement(identifier, base, target.branch, target.path,
                                target.open_workspace_id)
        common = Path(Git(self.repo).command(
            "rev-parse", "--path-format=absolute", "--git-common-dir").strip()).resolve()
        self._retirement_workspace(state, self.workspaces(), allow_absent=False, common=common)
        return state

    def stale_retirement(self, git: Git, identifier: str,
                         base: str) -> HerdrRetirement | None:
        """Resolve a legacy stale workspace without trusting its display label alone."""
        common = Path(git.command(
            "rev-parse", "--path-format=absolute", "--git-common-dir").strip()).resolve()
        entries = self.workspaces()
        candidates = []
        for entry in entries:
            label_match = entry["label"] == identifier
            location = entry.get("worktree")
            checkout_value = location.get("checkout_path") if isinstance(location, dict) else None
            checkout = (Path(checkout_value) if isinstance(checkout_value, str)
                        and Path(checkout_value).is_absolute() else None)
            path_match = checkout is not None and belongs_to_issue(checkout.name, identifier)
            if not label_match and not path_match:
                continue
            # Either signal in isolation can be user-edited or coincidental.
            # Require both, then bind them to Herdr's exact repository identity.
            try:
                if not label_match or not path_match or checkout is None:
                    raise ValueError("partial task identity")
                try:
                    checkout.lstat()
                except FileNotFoundError:
                    pass
                except OSError:
                    raise ValueError("checkout absence cannot be confirmed") from None
                else:
                    raise ValueError("checkout path still exists")
                # Resolve only after lstat proved the original reported path is
                # absent. A dangling symlink is an existing object, not proof
                # that its target checkout disappeared.
                repo_key = Path(location["repo_key"])
                if (not isinstance(location["repo_root"], str)
                        or not isinstance(location["repo_name"], str)
                        or not isinstance(location["is_linked_worktree"], bool)
                        or not Path(location["repo_root"]).is_absolute()
                        or not repo_key.is_absolute()
                        or Path(location["repo_root"]).resolve() != self.repo
                        or repo_key.resolve() != common
                        or location["repo_name"] != self.repo.name
                        or location["is_linked_worktree"] is not True):
                    raise ValueError("repository identity mismatch")
                path = checkout.resolve()
                if (path == self.repo or self.repo.is_relative_to(path)
                        or common.is_relative_to(path) or path.is_relative_to(common)):
                    raise ValueError("unsafe checkout path")
                candidates.append(HerdrRetirement(
                    identifier, base, checkout.name, path, entry["workspace_id"]))
            except (KeyError, TypeError, ValueError, OSError):
                raise TaskError(f"Herdr workspace {entry['workspace_id']!r} partially matches {identifier}, "
                                "but its exact stale task identity cannot be proven; it was not retired") from None
        if len(candidates) > 1:
            ids = ", ".join(state.workspace_id for state in candidates)
            raise TaskError(f"Multiple stale Herdr workspaces match {identifier}: {ids}; none was retired")
        if not candidates:
            return None
        state = candidates[0]
        branches = git.branches(identifier)
        trees = [tree for tree in git.worktrees()
                 if (belongs_to_issue(tree.get("branch", "").removeprefix("refs/heads/"), identifier)
                     or Path(tree["worktree"]).resolve() == state.path)]
        if branches or trees:
            raise TaskError("Git task state reappeared while resolving its stale Herdr workspace; "
                            "nothing was retired")
        self._retirement_workspace(state, entries, allow_absent=False, common=common)
        return state

    def _retirement_workspace(self, state: HerdrRetirement, entries: list[dict],
                              *, allow_absent: bool, common: Path | None = None) -> dict | None:
        matches = [entry for entry in entries if entry.get("workspace_id") == state.workspace_id]
        if not matches and allow_absent:
            return None
        try:
            if len(matches) != 1:
                raise ValueError("workspace ID is absent or ambiguous")
            match = matches[0]
            location = match["worktree"]
            if (not isinstance(location, dict)
                    or not isinstance(location["repo_root"], str)
                    or not isinstance(location["repo_key"], str)
                    or not isinstance(location["repo_name"], str)
                    or not isinstance(location["checkout_path"], str)
                    or not Path(location["repo_root"]).is_absolute()
                    or not Path(location["repo_key"]).is_absolute()
                    or not Path(location["checkout_path"]).is_absolute()
                    or Path(location["repo_root"]).resolve() != self.repo
                    or (common is not None and Path(location["repo_key"]).resolve() != common)
                    or location["repo_name"] != self.repo.name
                    or Path(location["checkout_path"]).resolve() != state.path
                    or location["is_linked_worktree"] is not True):
                raise ValueError("workspace checkout mismatch")
            return match
        except (KeyError, TypeError, ValueError, OSError):
            raise TaskError(f"Herdr workspace {state.workspace_id!r} does not exactly match the cleaned "
                            "task checkout; it was not retired") from None

    def revalidate_retirement(self, git: Git, state: HerdrRetirement) -> list[dict]:
        """Prove saved retirement state is still stale immediately before close."""
        try:
            state.path.lstat()
        except FileNotFoundError:
            pass
        except OSError:
            raise TaskError(f"Cannot confirm saved checkout path is absent: {state.path}; "
                            "Herdr workspace was not retired") from None
        else:
            raise TaskError(f"Saved checkout path has been reused or still exists: {state.path}; "
                            "Herdr workspace was not retired")
        common = Path(git.command(
            "rev-parse", "--path-format=absolute", "--git-common-dir").strip()).resolve()
        if any(Path(tree["worktree"]).resolve() == state.path for tree in git.worktrees()):
            raise TaskError(f"A registered Git worktree occupies saved checkout path {state.path}; "
                            "Herdr workspace was not retired")
        entries = self.workspaces()
        match = self._retirement_workspace(
            state, entries, allow_absent=True, common=common)
        if match is not None:
            label = match["label"]
            claimed = re.fullmatch(
                r"([A-Z][A-Z0-9]*-[1-9][0-9]*)(?:\s*/\s*.+)?", label, re.IGNORECASE)
            if claimed is not None and claimed.group(1).upper() != state.identifier:
                raise TaskError(f"Herdr workspace {state.workspace_id!r} now identifies task "
                                f"{claimed.group(1).upper()}, not {state.identifier}; it was not retired")
        return entries

    def retire(self, git: Git, state: HerdrRetirement) -> bool:
        """Close one exact workspace and confirm it disappeared; False means already absent."""
        common = Path(git.command(
            "rev-parse", "--path-format=absolute", "--git-common-dir").strip()).resolve()
        before = self.revalidate_retirement(git, state)
        if self._retirement_workspace(
                state, before, allow_absent=True, common=common) is None:
            return False
        try:
            result = self.workspace_command("close", state.workspace_id)
            if result.get("workspace_id") != state.workspace_id:
                raise TaskError("Herdr close response identified a different workspace")
            closed = result.get("workspace")
            if closed is not None:
                self._retirement_workspace(
                    state, [closed], allow_absent=False, common=common)
        except TaskError as error:
            try:
                after = self.workspaces()
                if self._retirement_workspace(
                        state, after, allow_absent=True, common=common) is None:
                    return True
            except TaskError as inspection_error:
                raise TaskError(f"Herdr workspace retirement failed and its result could not be confirmed: "
                                f"{error}. Post-failure inspection also failed: {inspection_error}") from None
            raise TaskError(f"Herdr workspace {state.workspace_id!r} remains after retirement failed: "
                            f"{error}") from None
        after = self.workspaces()
        if self._retirement_workspace(
                state, after, allow_absent=True, common=common) is not None:
            raise TaskError(f"Herdr did not confirm retirement of workspace {state.workspace_id!r}")
        return True

    def resolve_task(self, git: Git, identifier: str, *, branch: str | None = None,
                     slice: str | None = None, include_remotes: bool = True,
                     disposing: bool = False) -> TaskWorktree | None:
        """Select existing state without opening, creating or changing a workspace."""
        if not disposing:
            git.check_disposal(identifier)
        label = identifier if slice is None else f"{identifier} / {slice}"
        exact_cleanup = disposing and branch is not None

        def selected(name: str) -> bool:
            return name == branch if slice is not None or exact_cleanup else belongs_to_issue(name, identifier)

        result = self.command("list")
        try:
            if Path(result["source"]["repo_root"]).resolve() != self.repo:
                raise ValueError("repository mismatch")
            entries = result["worktrees"]
            if not isinstance(entries, list):
                raise ValueError("invalid worktrees")
            for entry in entries:
                if not isinstance(entry["path"], str) or not Path(entry["path"]).is_absolute():
                    raise ValueError("invalid path")
                if entry.get("branch") is not None and not isinstance(entry["branch"], str):
                    raise ValueError("invalid branch")
                if not isinstance(entry["label"], str):
                    raise ValueError("invalid label")
                workspace_id = entry.get("open_workspace_id")
                if workspace_id is not None and (not isinstance(workspace_id, str)
                                                  or not workspace_id.strip()):
                    raise ValueError("invalid workspace ID")
            matches = [w for w in entries if selected(w.get("branch") or "")
                       or not exact_cleanup and w["label"].casefold() == label.casefold()]
        except (KeyError, TypeError, ValueError, AttributeError):
            raise TaskError("Unexpected Herdr worktree list") from None
        branches = [b for b in git.branches(identifier) if selected(b)]
        remotes = [b for b in git.remote_branches(identifier) if selected(b)] if include_remotes else []
        git_trees = git.worktrees()
        candidates = [w for w in git_trees if selected(w.get("branch", "").removeprefix("refs/heads/"))]
        names = set(branches + remotes + [w.get("branch") or "(detached)" for w in matches]
                    + [w["branch"].removeprefix("refs/heads/") for w in candidates])
        details = "; ".join(sorted(names))
        paths = "; ".join(w["path"] for w in matches)
        if len(names) > 1:
            hint = ("Use --slice <branch suffix after the issue ID> to select one, or "
                    "inspect/retire historical work manually." if include_remotes and not disposing else
                    f"For a proven merged execution, use task cleanup {identifier} --force "
                    "--branch <exact-local-branch> to select one.")
            raise TaskError(f"Ambiguous workspaces for {identifier}: {details}. Paths: {paths or '(none)'}. "
                            f"{hint} Nothing was deleted.")
        if not branches and not matches and not candidates:
            if remotes:
                raise TaskError(f"A live remote branch for {identifier} already exists: {details}; "
                                "inspect it manually or choose another --slice")
            return None
        if len(branches) != 1 or len(matches) != 1 or len(candidates) != 1:
            raise TaskError(f"Existing branch/worktree state for {identifier} is ambiguous or branch-only: "
                            f"{details}. Paths: {paths or '(none)'}. Inspect Git and Herdr; nothing was deleted")
        branch = branches[0]
        match, tree = matches[0], candidates[0]
        path = Path(match["path"]).resolve()
        if disposing and (Path(match["path"]) != path
                          or Path(tree["worktree"]).resolve() != Path(tree["worktree"])):
            raise TaskError("Git/Herdr cleanup paths are aliased; use the canonical registered checkout in Herdr")
        if path == self.repo or Path(tree["worktree"]).resolve() == self.repo:
            raise TaskError("The resolved task worktree is the permanent checkout; nothing was removed")
        if (match.get("branch") != branch or tree.get("branch") != f"refs/heads/{branch}"
                or path != Path(tree["worktree"]).resolve() or not path.is_dir()
                or sum(Path(w["path"]).resolve() == path for w in entries) != 1
                or sum(Path(w["worktree"]).resolve() == path for w in git_trees) != 1
                or match.get("is_linked_worktree") is not True
                or match.get("is_bare") is not False or match.get("is_detached") is not False
                or match.get("is_prunable") is not False or "prunable" in tree or "locked" in tree):
            raise TaskError("Git and Herdr do not identify a single usable task worktree; inspect them manually")
        return TaskWorktree(branch, path, match.get("open_workspace_id"))

    def claim_preparation(self, git, base, branch, identifier, slice, *, ownership):
        """Own an existing selection before fetch can change the shared base.

        Task locks are try-only under the gate; repository administration waits
        afterward, outside it. Keep this ownership through preparation/handoff.
        """
        from .ownership import ownership_gate
        from .publication_state import PublicationStore
        git.check_base(base)
        with ownership_gate(exclusive=True):
            target = self.resolve_task(git, identifier, branch=branch, slice=slice, include_remotes=False)
            if git.load_retirement(identifier, base) is not None:
                raise TaskError("Workspace retirement is pending; finish task cleanup before starting")
            if target is not None:
                ownership.enter_context(PublicationStore(target.path).locked())
            return target

    def prepare(self, git: Git, base: str, branch: str, identifier: str,
                slice: str | None = None, *, default_only: bool = False, ownership=None,
                selected=_UNSELECTED) -> Workspace:
        target = self.resolve_task(git, identifier, branch=branch, slice=slice)
        if selected is not _UNSELECTED and target != selected:
            raise TaskError("Workspace selection changed during preparation; retry exact selection")
        git.check_history(base, target.branch if target else branch, existing=target is not None)
        from contextlib import ExitStack
        from .ownership import ownership_gate
        from .publication_state import PublicationStore
        with ExitStack() as local_locks:
            locks = ownership if ownership is not None else local_locks
            with ownership_gate(exclusive=True):
                if self.resolve_task(git, identifier, branch=branch, slice=slice, include_remotes=False) != target:
                    raise TaskError("Workspace selection changed during preparation; retry exact selection")
                if git.load_retirement(identifier, base) is not None:
                    raise TaskError("Workspace retirement is pending; finish task cleanup before starting")
                if target is not None:
                    locks.enter_context(PublicationStore(target.path).locked(allow_owned=selected is not _UNSELECTED))
                workspace = self._prepare_selected(git, base, branch, identifier, slice, default_only, target)
                if target is None:
                    locks.enter_context(PublicationStore(workspace.path).locked())
                return workspace

    def _prepare_selected(self, git, base, branch, identifier, slice, default_only, target):
        if target is None:
            label = identifier if slice is None else f"{identifier} / {slice}"
            created = self.command("create", "--base", base, "--branch", branch,
                                   "--label", label, "--focus")
            workspace = self.confirm(created, branch, "workspace created and focused")
            self.confirm_git(git, workspace)
            git.save_scope(workspace.path, branch, identifier, slice)
            return replace(workspace, slice=slice)
        branch, path = target.branch, target.path
        scope = git.resolve_scope(path, branch, identifier, slice)
        if default_only and scope is not None:
            raise TaskError("From-scratch loop requires an unsliced default workspace")
        label = identifier if scope is None else f"{identifier} / {scope}"
        opened = self.command("open", "--path", str(path), "--label", label, "--focus")
        workspace = self.confirm(opened, branch, "workspace reopened and focused", path)
        if target.open_workspace_id is not None and workspace.workspace_id != target.open_workspace_id:
            raise TaskError("Herdr opened a different workspace ID; inspect it before retrying")
        self.confirm_git(git, workspace)
        git.save_scope(path, branch, identifier, scope)
        return replace(workspace, slice=scope)

    def confirm(self, result: dict, branch: str, action: str, path: Path | None = None) -> Workspace:
        try:
            tree = result["worktree"]
            workspace, tab, pane = result["workspace"], result["tab"], result["root_pane"]
            wid, tid, pid = workspace["workspace_id"], tab["tab_id"], pane["pane_id"]
            if (not all(isinstance(x, str) and x.strip() for x in (wid, tid, pid))
                    or tab["workspace_id"] != wid or pane["workspace_id"] != wid or pane["tab_id"] != tid
                    or tree["branch"] != branch or workspace["focused"] is not True
                    or not isinstance(tree["path"], str) or not Path(tree["path"]).is_absolute()
                    or not Path(tree["path"]).is_dir() or Path(tree["path"]).resolve() == self.repo
                    or tree.get("is_linked_worktree") is not True or tree.get("is_bare") is not False
                    or tree.get("is_detached") is not False or tree.get("is_prunable") is not False
                    or (path is not None and Path(tree["path"]).resolve() != path)):
                raise ValueError("workspace not confirmed")
            location = workspace["worktree"]
            if (not Path(location["repo_root"]).is_absolute() or not Path(location["checkout_path"]).is_absolute()
                    or Path(location["repo_root"]).resolve() != self.repo
                    or Path(location["checkout_path"]).resolve() != Path(tree["path"]).resolve()):
                raise ValueError("workspace checkout mismatch")
            return Workspace(branch, Path(tree["path"]).resolve(), wid, tid, pid, action)
        except (KeyError, TypeError, ValueError):
            raise TaskError("Herdr did not confirm the requested workspace and focus; inspect it before retrying") from None

    def confirm_git(self, git: Git, workspace: Workspace) -> None:
        trees = [w for w in git.worktrees() if w.get("branch") == f"refs/heads/{workspace.branch}"]
        if (len(trees) != 1 or Path(trees[0]["worktree"]).resolve() != workspace.path
                or "locked" in trees[0] or "prunable" in trees[0]):
            raise TaskError("Git did not confirm the prepared Herdr worktree; inspect it before retrying")
