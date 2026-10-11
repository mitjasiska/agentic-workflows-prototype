"""One private orchestration checkpoint, not a run-report archive.

Short SQLite transactions serialize pause requests with handoff claims. The
existing publication lock owns the controller; pause/status never take that lock.
"""

from contextlib import contextmanager, nullcontext
import json
import os
from pathlib import Path
import re
import sqlite3
from uuid import UUID

from . import TaskError
from .ownership import ownership_gate
from .review_result import unique_object
from .sessions import has_immutable_identity
from .task_assessment import validate_assessment
from .pass_delivery import validate_delivery
from .workspace import Git


PHASES = {"initial_implementation", "implementation", "review", "fixes", "rereview"}
STATES = {"ready", "running", "paused", "clean", "escalated", "interrupted", "aborting", "aborted"}


def startup_recovery_blocker(state):
    """Structural delivery evidence, including legacy v3 checkpoints, never error prose."""
    if state["active_pass"] is not None or state["pass_count"]:
        return "a handoff was already claimed; delivery/completion may be uncertain"
    if state["status"] != "escalated":
        return f"the saved loop is {state['status']}; only an escalated pre-handoff startup can retry"
    if (state["version"] != 3 or state.get("initial_phase") != "initial_implementation"
            or state["next_phase"] != "initial_implementation"):
        return "the checkpoint is not at its original initial implementation boundary"
    if (state["review_count"] or state["reviewer"] is not None or state["records"]
            or state["findings"] or state["seen"]):
        return "review/completion history exists; startup recovery cannot adopt it"
    if set(state["implementation"]) != {"context_id"}:
        return "an implementation provider session is already recorded"
    return None


def validate_checkpoint(state):
    """Never turn missing/corrupt routing evidence into a fresh or replayed pass."""
    fields = {"version", "run_id", "binding", "requirements", "implementation", "reviewer",
              "reviewer_options", "status", "reason", "next_phase", "active_pass", "pass_count",
              "review_count", "max_reviews", "max_passes", "timeout", "snapshot", "findings", "seen", "records"}
    initial_phase = "implementation"  # Version 1 always starts with completion.
    abandoned = state.get("abandonment")
    if abandoned is not None:
        fields.add("abandonment")
        if (set(abandoned) != {"claim", "review_ready", "timestamp"}
                or state["status"] != "aborted" or state["active_pass"] is not None
                or state.get("delivery") is not None or state["next_phase"] is not None
                or type(abandoned["review_ready"]) is not bool
                or not isinstance(abandoned["timestamp"], str)
                or set(abandoned["claim"]) != {"phase", "context_id", "pass_id"}
                or abandoned["claim"]["phase"] not in PHASES
                or abandoned["review_ready"] != (abandoned["claim"]["phase"] in {"review", "rereview"})):
            raise ValueError("invalid abandonment boundary")
    elif state["status"] == "aborted":
        raise ValueError("missing abandonment evidence")
    if "delivery" in state:
        fields.add("delivery")
        validate_delivery(state["delivery"], state)
    if state["version"] == 2:
        fields.add("initial_phase")
        initial_phase = state["initial_phase"]
        if initial_phase != "review":
            raise ValueError("invalid initial boundary")
    if state["version"] == 3:
        fields.update({"initial_phase", "implementation_options"})
        initial_phase = state["initial_phase"]
        if initial_phase != "initial_implementation":
            raise ValueError("invalid initial launch boundary")
        options = state["implementation_options"]
        if (set(options) != {"kind", "model", "mode"}
                or any(not isinstance(v, str) or not v for v in options.values())):
            raise ValueError("invalid implementation settings")
    if set(state) != fields:
        raise ValueError("unknown checkpoint fields")
    target = state["binding"]
    if (set(target) != {"issue", "repository", "worktree", "branch", "workspace_id", "endpoint", "base_branch"}
            or any(not isinstance(v, str) or not v for v in target.values())
            or not re.fullmatch(r"[A-Z][A-Z0-9]*-[1-9][0-9]*", target["issue"])
            or any(not Path(target[k]).is_absolute() for k in ("repository", "worktree", "endpoint"))
            or not isinstance(state["reason"], str)
            or not re.fullmatch(r"[0-9a-f]{64}", state["requirements"])):
        raise ValueError("invalid binding")
    for key, role in (("implementation", "I"), ("reviewer", "R")):
        context = state[key]
        if context is None and key == "reviewer":
            continue
        if not re.fullmatch(re.escape(target["issue"]) + f"-{role}[1-9][0-9]*", context["context_id"]):
            raise ValueError("invalid context selector")
        if (key == "implementation" and state["version"] == 3 and set(context) == {"context_id"}):
            if (state["records"] or state["reviewer"] is not None or state["review_count"] != 0
                    or state["next_phase"] != "initial_implementation" and not abandoned
                    or state["status"] == "clean"):
                raise ValueError("missing established implementation")
            continue  # Reservation is durable before any provider/session exists.
        if (key == "reviewer" and set(context) == {"context_id"}
                and state["status"] in {"running", "escalated", "interrupted", "aborting", "aborted"}):
            continue  # Allocation is recorded even before provider receipt exists.
        if (set(context) != {"context_id", "agent", "model", "mode", "session"}
                or any(not isinstance(context[k], str) or not context[k] for k in ("agent", "model", "mode"))
                or not has_immutable_identity(context["session"], context["agent"])):
            raise ValueError("invalid saved session")
        if key == "implementation" and state["version"] == 3:
            options = state["implementation_options"]
            if any(context[k] != options[o] for k, o in (("agent", "kind"), ("model", "model"), ("mode", "mode"))):
                raise ValueError("implementation settings changed")
    pinned = state["snapshot"]
    if (set(pinned) != {"base_commit", "head", "branch", "index", "fingerprint", "content", "version"}
            or pinned["version"] != 2 or pinned["branch"] != target["branch"]
            or any(not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", pinned[k])
                   for k in ("base_commit", "head", "index", "fingerprint", "content"))):
        raise ValueError("invalid snapshot")
    options = state["reviewer_options"]
    if (set(options) != {"kind", "model", "mode"}
            or any(not isinstance(v, str) or not v for v in options.values())):
        raise ValueError("invalid reviewer settings")
    records, active = state["records"], state["active_pass"]
    if active is not None and (set(active) != {"phase", "context_id", "pass_id"}
            or active["phase"] != state["next_phase"]):
        raise ValueError("invalid active pass")
    if state["status"] == "running" and active is None:
        raise ValueError("missing handoff claim")
    if state["pass_count"] != len(records) + (active is not None) + bool(abandoned):
        raise ValueError("missing pass evidence")
    reviews = 0
    next_phase = initial_phase
    ids = set()
    for index, record in enumerate(records):
        record_fields = {"phase", "pass_id", "context_id", "state", "summary", "findings", "checks", "resolutions"}
        if "task_assessment" in record and next_phase in {"initial_implementation", "implementation", "fixes"}:
            record_fields.add("task_assessment")
            validate_assessment(record["task_assessment"], record["state"])
        if (set(record) != record_fields
                or record["phase"] != next_phase or record["pass_id"] in ids
                or not isinstance(record["summary"], str)
                or any(not isinstance(record[k], list) for k in ("findings", "checks", "resolutions"))):
            raise ValueError("invalid pass sequence")
        ids.add(record["pass_id"])
        if next_phase in {"initial_implementation", "implementation", "fixes"}:
            if record["context_id"] != state["implementation"]["context_id"]:
                raise ValueError("implementation changed")
            next_phase = "rereview" if next_phase == "fixes" else "review"
        else:
            reviews += 1
            if state["reviewer"] is None:
                # Fresh setup can fail (or be invalidated) before allocation.
                # This is a terminal outcome, never a resumable review boundary
                # or permission to omit an already allocated reviewer identity.
                if (next_phase != "review" or record["context_id"] is not None
                        or record["state"] not in {"failed", "blocked"}
                        or index != len(records) - 1
                        or state["status"] not in {"escalated", "interrupted"}
                        or state["next_phase"] is not None or active is not None
                        or any(record[k] for k in ("findings", "checks", "resolutions"))):
                    raise ValueError("missing reviewer outside terminal setup failure")
            elif record["context_id"] != state["reviewer"]["context_id"]:
                raise ValueError("reviewer changed")
            next_phase = "fixes" if record["state"] == "findings" else None
    counted = active or (abandoned["claim"] if abandoned else None)
    if state["review_count"] != reviews + bool(counted and counted["phase"] in {"review", "rereview"}):
        raise ValueError("missing review evidence")
    if abandoned and abandoned["claim"]["phase"] != next_phase:
        raise ValueError("abandoned pass sequence changed")
    if (state["status"] in {"ready", "paused", "running"} or active is not None
            or records and state["next_phase"] is not None):
        if state["next_phase"] != next_phase:
            raise ValueError("completed work would be replayed")
        if next_phase in {"fixes", "rereview"} and (not state["findings"] or state["reviewer"] is None):
            raise ValueError("missing feedback or reviewer")
    if state["status"] == "clean" and (active is not None or state["next_phase"] is not None
            or not records or records[-1]["state"] != "clean"):
        raise ValueError("unproven clean boundary")


class PauseRequested(Exception):
    """A safe boundary was reached without delivering the next handoff."""


class LoopStore:
    def __init__(self, worktree):
        directory = Path(Git(worktree).command("rev-parse", "--absolute-git-dir").strip())
        self.path = directory / "agentic-workflows-loop.sqlite3"

    @classmethod
    def at_directory(cls, directory):
        """Inspect registry-known private metadata without requiring a live checkout."""
        store = cls.__new__(cls)
        store.path = Path(directory) / "agentic-workflows-loop.sqlite3"
        return store

    @contextmanager
    def connection(self, *, write=False):
        # Agent completion seals inspect checkpoints read-only, including in
        # sandboxes without registry write access. Controllers gate selection;
        # only checkpoint mutations need admission against disposal here.
        with ownership_gate() if write else nullcontext():
            if write and self.path.exists():
                from .cleanup import check_unreserved
                from .contexts import ContextRegistry
                binding = self.read()[0]["binding"]
                check_unreserved(ContextRegistry(), binding["repository"], binding["worktree"])
            with self._connection(write=write) as db:
                yield db

    @contextmanager
    def _connection(self, *, write=False):
        db = None
        try:
            if write:
                try:
                    fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                except FileExistsError:
                    pass
                else:
                    os.close(fd)
                db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
                db.execute("BEGIN IMMEDIATE")
                db.execute("CREATE TABLE IF NOT EXISTS checkpoint (id INTEGER PRIMARY KEY CHECK(id=1), "
                           "payload TEXT NOT NULL, pause INTEGER NOT NULL CHECK(pause IN (0,1)))")
            else:
                db = sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True, timeout=10)
            yield db
            if write:
                db.commit()
        except (sqlite3.Error, OSError):
            raise TaskError("Cannot access loop checkpoint; inspect private Git metadata before recovery") from None
        finally:
            if db is not None:
                db.close()

    @staticmethod
    def decode(row):
        if row is None:
            raise TaskError("No implementation-review loop checkpoint exists")
        try:
            state = json.loads(row[0], object_pairs_hook=unique_object)
            if (state["version"] not in {1, 2, 3} or str(UUID(state["run_id"])) != state["run_id"]
                    or state["status"] not in STATES
                    or state["next_phase"] not in PHASES | {None}
                    or type(state["pass_count"]) is not int or type(state["review_count"]) is not int
                    or not 0 <= state["pass_count"] <= state["max_passes"] <= 40
                    or not 0 <= state["review_count"] <= state["max_reviews"] <= 20
                    or not 0 < state["timeout"] <= 86400
                    or not isinstance(state["records"], list)
                    or len(state["records"]) > state["pass_count"]
                    or not isinstance(state["findings"], list) or not isinstance(state["seen"], list)
                    or not isinstance(state["binding"], dict) or not isinstance(state["implementation"], dict)
                    or state["status"] in {"ready", "running", "paused"} and state["next_phase"] is None
                    or state["status"] in {"ready", "paused"} and state["active_pass"] is not None):
                raise ValueError("invalid checkpoint")
            validate_checkpoint(state)
            return state, bool(row[1])
        except (KeyError, ValueError, TypeError, AttributeError, RecursionError):
            raise TaskError("Malformed loop checkpoint; inspect it, never infer a completed pass") from None

    def read(self):
        if not self.path.exists():
            raise TaskError("No implementation-review loop checkpoint exists")
        with self.connection() as db:
            return self.decode(db.execute("SELECT payload, pause FROM checkpoint WHERE id=1").fetchone())

    def create(self, state, *, replace=False):
        with self.connection(write=True) as db:
            row = db.execute("SELECT payload, pause FROM checkpoint WHERE id=1").fetchone()
            if row is not None:
                old, _ = self.decode(row)
                if not replace:
                    raise TaskError("A loop checkpoint already exists; use --status, --continue for a pause, "
                                    "or explicitly --new after inspecting the previous run")
                if old["status"] in {"ready", "running", "aborting"} or old["active_pass"] is not None or old.get("delivery"):
                    raise TaskError("An unfinished handoff exists; inspect its contexts before replacing the checkpoint")
                if old["status"] == "aborted" and self.abort_journal(old["run_id"]) is None:
                    raise TaskError("Abandonment archive is missing; no replacement loop is safe")
            db.execute("INSERT OR REPLACE INTO checkpoint VALUES (1, ?, 0)", (json.dumps(state),))

    def save(self, state):
        with self.connection(write=True) as db:
            current, _ = self.decode(db.execute("SELECT payload, pause FROM checkpoint WHERE id=1").fetchone())
            if current["run_id"] != state["run_id"] or current["status"] in {"aborting", "aborted"}:
                raise TaskError("Loop identity changed; no state was overwritten")
            db.execute("UPDATE checkpoint SET payload=? WHERE id=1", (json.dumps(state),))

    def begin_abort(self, expected, journal):
        """Commit revocation before releasing any operational context claim.

        The archive survives checkpoint replacement. An interrupted transaction
        changes neither; an interrupted reconciliation remains explicitly aborting.
        """
        with self.connection(write=True) as db:
            current, _ = self.decode(db.execute("SELECT payload, pause FROM checkpoint WHERE id=1").fetchone())
            if current != expected or current["active_pass"] is None:
                raise TaskError("Loop claim changed before abort; nothing was abandoned")
            db.execute("CREATE TABLE IF NOT EXISTS abandonments (run_id TEXT PRIMARY KEY, payload TEXT NOT NULL)")
            db.execute("INSERT INTO abandonments VALUES (?, ?)", (expected["run_id"], json.dumps(journal)))
            current.update(status="aborting", reason="Abort recorded; rerun --abort to finish reconciliation")
            db.execute("UPDATE checkpoint SET payload=? WHERE id=1", (json.dumps(current),))

    def abort_journal(self, run_id):
        with self.connection() as db:
            exists = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='abandonments'").fetchone()
            row = db.execute("SELECT payload FROM abandonments WHERE run_id=?", (run_id,)).fetchone() if exists else None
        if row is None:
            return None
        try:
            journal = json.loads(row[0], object_pairs_hook=unique_object)
            if (set(journal) != {"original", "contexts", "reconciled", "snapshot", "processes", "publication", "timestamp"}
                    or journal["original"]["run_id"] != run_id):
                raise ValueError("invalid archive")
            self.decode((json.dumps(journal["original"]), 0))
            return journal
        except (KeyError, TypeError, ValueError):
            raise TaskError("Malformed abandonment archive; preserve it for inspection") from None

    def finish_abort(self, expected, state):
        self.decode((json.dumps(state), 0))
        with self.connection(write=True) as db:
            current, _ = self.decode(db.execute("SELECT payload, pause FROM checkpoint WHERE id=1").fetchone())
            if current != expected or current["status"] != "aborting":
                raise TaskError("Abort checkpoint changed; no state was overwritten")
            db.execute("UPDATE checkpoint SET payload=?, pause=0 WHERE id=1", (json.dumps(state),))

    def pause(self):
        with self.connection(write=True) as db:
            state, _ = self.decode(db.execute("SELECT payload, pause FROM checkpoint WHERE id=1").fetchone())
            # Recovery retains escalated state during preflight. Record pause
            # intent there without making the boundary resumable before proof.
            if (state["status"] in {"ready", "running", "paused"} or state.get("delivery")
                    and state["active_pass"] is not None or startup_recovery_blocker(state) is None):
                db.execute("UPDATE checkpoint SET pause=1 WHERE id=1")
        return self.read()

    def continue_paused(self):
        with self.connection(write=True) as db:
            state, _ = self.decode(db.execute("SELECT payload, pause FROM checkpoint WHERE id=1").fetchone())
            if state["status"] != "paused":
                raise TaskError("Only a paused boundary can continue; interrupted/unfinished handoffs require inspection")
            state.update(status="ready", reason="Explicit continuation requested")
            db.execute("UPDATE checkpoint SET payload=?, pause=0 WHERE id=1", (json.dumps(state),))
        return state

    def restart_initial(self, expected):
        """Called under the controller lock after revalidating the original reservation.

        Compare the entire checkpoint in one transaction and retain a racing pause.
        A later handoff still needs begin(); this never clears delivery evidence.
        """
        with self.connection(write=True) as db:
            state, pause = self.decode(db.execute("SELECT payload, pause FROM checkpoint WHERE id=1").fetchone())
            if state != expected:
                raise TaskError("Startup checkpoint changed during recovery; no state was overwritten")
            blocker = startup_recovery_blocker(state)
            if blocker:
                raise TaskError("Startup recovery blocked: " + blocker)
            state.update(status="paused" if pause else "ready",
                         reason="Pause retained; use --continue explicitly" if pause else
                                "Original pre-handoff reservation revalidated; initial implementation pending")
            db.execute("UPDATE checkpoint SET payload=? WHERE id=1", (json.dumps(state),))
        return state

    def begin(self, state):
        """Linearization point: a pause before this transaction prevents the pass.

        A request after the claim belongs to this current pass and stays sticky.
        No delivery is retried after a persisted running claim.
        """
        with self.connection(write=True) as db:
            current, pause = self.decode(db.execute("SELECT payload, pause FROM checkpoint WHERE id=1").fetchone())
            if current != state or state["status"] != "ready":
                raise TaskError("Loop handoff boundary changed; inspect before proceeding")
            if pause:
                raise PauseRequested()
            state.update(status="running", pass_count=state["pass_count"] + 1,
                         active_pass=dict(phase=state["next_phase"], context_id=None, pass_id=None))
            if state["next_phase"] in {"review", "rereview"}:
                state["review_count"] += 1
            db.execute("UPDATE checkpoint SET payload=? WHERE id=1", (json.dumps(state),))
