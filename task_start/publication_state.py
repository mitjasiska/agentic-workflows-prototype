"""Private acceptance, intent and irreversible publication evidence, not reports."""

from contextlib import contextmanager, ExitStack
import json
import os
from pathlib import Path
import re
import tempfile
import threading
import time

from . import TaskError
from .command import command_name
from .ownership import ownership_gate
from .review_result import unique_object
from .workspace import Git


_owned = threading.local()


def verify_publication_history(history, binding, identity):
    if history in (None, "unknown"):
        return
    try:
        fields = {"version", "binding", "identity", "state", "head"}
        if history["version"] == 2:
            fields |= {"base_commit", "pull_number", "cycles", "prior"}
        if (set(history) != fields or history["version"] not in {1, 2}
                or history["binding"] != binding
                or history["identity"]["remote"] != identity["remote"]
                or history["identity"]["repository"].casefold() != identity["repository"].casefold()
                or history["state"] not in {"pending", "published"}
                or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", history["head"])):
            raise ValueError("invalid publication history")
        if history["version"] == 2:
            cycles = history["cycles"]
            if (not isinstance(cycles, list) or not cycles
                    or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", history["base_commit"])
                    or (history["pull_number"] is not None
                        and (type(history["pull_number"]) is not int or history["pull_number"] < 1))):
                raise ValueError("invalid publication lineage")
            prior = history["prior"]
            if prior not in (None, "unknown"):
                if not isinstance(prior, dict) or prior["version"] != 1:
                    raise ValueError("invalid prior publication evidence")
                verify_publication_history(prior, binding, identity)
            prior_head = prior["head"] if isinstance(prior, dict) and prior["state"] == "published" else None
            previous, passes, published = None, set(), prior_head
            for cycle in cycles:
                accepted, intent = cycle["acceptance"], cycle["intent"]
                head = intent["publishing_head"]
                if (set(cycle) != {"acceptance", "intent", "state"}
                        or cycle["state"] not in {"pending", "published", "complete"}
                        or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", head)
                        or accepted["version"] != 1 or intent["version"] != 1
                        or accepted["review_state"]["version"] != 2
                        or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", accepted["review_state"]["head"])
                        or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", intent["tree"])
                        or not all(isinstance(intent[k], str) and intent[k] for k in ("index", "title", "body", "message"))
                        or not isinstance(accepted["pass_id"], str) or not accepted["pass_id"]
                        or accepted["verdict"] != "clean" or accepted["pass_id"] in passes
                        or intent["pass_id"] != accepted["pass_id"]
                        or intent["remote"] != identity["remote"]
                        or intent["repository"].casefold() != identity["repository"].casefold()
                        or (intent.get("reuse_head") is not None and intent["reuse_head"] != head)
                        or any(accepted[k] != v for k, v in binding.items())
                        or accepted["review_state"]["base_commit"] != history["base_commit"]
                        or (previous is None and prior_head is not None
                            and prior_head not in {head, accepted["review_state"]["head"]})
                        or (previous is not None and (previous["state"] != "complete"
                            or accepted["review_state"]["head"] != previous["intent"]["publishing_head"]
                            or intent.get("reuse_head") is not None))
                        or (cycle["state"] == "complete" and history["pull_number"] is None)):
                    raise ValueError("invalid publication cycle")
                if cycle["state"] in {"published", "complete"}:
                    published = head
                passes.add(accepted["pass_id"])
                previous = cycle
            if (history["head"] != (published or cycles[-1]["intent"]["publishing_head"])
                    or history["state"] != ("published" if published else "pending")):
                raise ValueError("invalid latest publication")
    except (KeyError, TypeError, ValueError, AttributeError):
        raise TaskError("Publication history conflicts with task/repository identity; inspect private Git metadata") from None


def prepare_review_continuation(saved, git, binding, base):
    """A new pass may replace only a completed publication, never an uncertain one."""
    history, intent = saved["publication_history"], saved["intent"]
    if isinstance(history, dict):
        verify_publication_history(history, binding, history.get("identity"))
    lineage = history if isinstance(history, dict) and history["version"] == 2 else None
    head = git.command("rev-parse", "HEAD").strip()
    if intent is not None and (intent.get("publishing_head") or head != saved["acceptance"]["review_state"]["head"]):
        if lineage is None or lineage["cycles"][-1]["intent"] != intent:
            raise TaskError(f"Publication is unfinished; rerun {command_name()} pr before a new review so its frozen commit/PR evidence is preserved")
    if lineage is not None:
        if lineage["cycles"][-1]["state"] != "complete":
            raise TaskError(f"Publication is unfinished; rerun {command_name()} pr to reconcile its frozen commit and PR before a new review")
        if head != lineage["head"]:
            raise TaskError("Published history changed: follow-up review requires HEAD at the latest published SHA "
                            f"{lineage['head']}. Inspect local history; keep follow-up edits uncommitted")
        if base != lineage["base_commit"]:
            raise TaskError("Published task base advanced or changed; review continuity requires the pinned published base. "
                            "Automatic rebase is forbidden; integration against an advanced base needs manual review")
    if lineage is None and history is not None:
        raise TaskError(f"Prior publication lacks completed review/PR lineage; rerun {command_name()} pr with its original acceptance/intent "
                        "or inspect legacy history manually before starting follow-up review")


class PublicationStore:
    def __init__(self, worktree):
        self.worktree = Path(worktree)
        self.directory = Path(Git(worktree).command("rev-parse", "--absolute-git-dir").strip())
        self.path = self.directory / "agentic-workflows-publication.json"
        self.identity = self._identity()

    def _identity(self):
        try:
            paths = (self.worktree, self.directory)
            if any(p.resolve() != p for p in paths):
                raise OSError("aliased workspace")
            return tuple((p.stat().st_dev, p.stat().st_ino) for p in paths)
        except OSError:
            raise TaskError("Workspace identity is missing or aliased; resolve the exact checkout again") from None

    @contextmanager
    def locked(self, *, allow_owned=False, registry=None):
        with ExitStack() as locks:
            with ownership_gate(registry=registry):
                if hasattr(self, "worktree"):
                    if (self._identity() != self.identity or
                            Path(Git(self.worktree).command("rev-parse", "--absolute-git-dir").strip()) != self.directory):
                        raise TaskError("Workspace identity changed before locking; resolve the exact checkout again")
                key = (os.getpid(), str(self.directory))
                owned = getattr(_owned, "locks", set())
                if not (allow_owned and key in owned):
                    locks.enter_context(self._locked())
            yield self

    @contextmanager
    def repository_locked(self):
        """Permanent-checkout administration may wait, always OUTSIDE the gate.

        No holder may wait for a task lock while holding this repository lock.
        The permanent checkout is never a disposal target.
        """
        with self._locked(timeout=30):
            if self._identity() != self.identity:
                raise TaskError("Permanent repository identity changed during Git administration")
            yield self

    @contextmanager
    def _locked(self, *, timeout=0):
        """An OS lock survives neither process exit nor interruption; no stale claims."""
        path = self.directory / "agentic-workflows-publication.lock"
        try:
            fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        except OSError:
            raise TaskError("Cannot lock review/publication state in the private Git directory; check filesystem access") from None
        try:
            try:
                if os.name == "nt":
                    import msvcrt
                    if os.fstat(fd).st_size == 0:
                        os.write(fd, b"\0")
                    os.lseek(fd, 0, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    deadline = time.monotonic() + timeout
                    while True:
                        try:
                            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                            break
                        except BlockingIOError:
                            if time.monotonic() >= deadline:
                                raise
                            time.sleep(0.01)
            except OSError:
                if timeout:
                    raise TaskError("Shared repository Git administration is busy; retry shortly after its "
                                    "fetch/base update or worktree removal finishes") from None
                raise TaskError(f"Another controller owns this worktree: {getattr(self, 'worktree', self.directory)} "
                                "(loop, review, publication, cleanup or Git administration); "
                                "finish or stop that controller before retrying this workspace") from None
            key = (os.getpid(), str(self.directory))
            if not hasattr(_owned, "locks"):
                _owned.locks = set()
            _owned.locks.add(key)
            try:
                yield self
            finally:
                _owned.locks.remove(key)
        finally:
            os.close(fd)

    def read(self):
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"), object_pairs_hook=unique_object)
            if isinstance(value, dict) and value.get("version") == 1 and set(value) == {"version", "acceptance", "intent"}:
                value.update(version=2, rebase=None)
            if isinstance(value, dict) and value.get("version") == 2 and set(value) == {"version", "acceptance", "intent", "rebase"}:
                # A legacy intent cannot prove that its push was never attempted.
                value.update(version=3, publication_history="unknown" if value["intent"] is not None else None)
            if (not isinstance(value, dict) or set(value) != {"version", "acceptance", "intent", "rebase", "publication_history"}
                    or value["version"] != 3
                    or any(value[k] is not None and not isinstance(value[k], dict) for k in ("acceptance", "intent", "rebase"))
                    or (value["publication_history"] not in (None, "unknown") and not isinstance(value["publication_history"], dict))):
                raise ValueError("invalid publication state")
            return value
        except FileNotFoundError:
            return dict(version=3, acceptance=None, intent=None, rebase=None, publication_history=None)
        except (OSError, ValueError, UnicodeError):
            raise TaskError("Cannot read publication evidence; inspect private Git metadata, never manufacture acceptance") from None

    def write(self, value, *, before_replace=None):
        """Persist atomically, checking any guard after serialization/fsync."""
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=self.directory, mode="w", encoding="utf-8", delete=False) as output:
                temporary = Path(output.name)
                json.dump(value, output, ensure_ascii=True, sort_keys=True)
                output.flush()
                os.fsync(output.fileno())
            if before_replace is not None:
                before_replace()
            os.replace(temporary, self.path)
            if os.name != "nt":
                fd = os.open(self.directory, os.O_RDONLY)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
        except OSError:
            raise TaskError("Cannot persist publication evidence; inspect state before retrying") from None
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
