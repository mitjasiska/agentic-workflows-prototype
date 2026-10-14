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
from .review_result import finding_signature, parse_verdict, pending_review_checks, unique_object
from .sessions import has_immutable_identity
from .task_assessment import validate_assessment
from .pass_delivery import validate_delivery
from .workspace import Git


PHASES = {"initial_implementation", "implementation", "review", "fixes", "rereview"}
STATES = {"ready", "running", "paused", "clean", "escalated", "interrupted", "aborting", "aborted"}
BUDGET_STOP_REASON = "Autonomous pass/review limit reached; human inspection required"


def _budget_exhausted_boundary(state):
    """Prove pending work at a budget stop, without authorizing its continuation.

    Legacy checkpoints can prove pending corrections without the full continuity
    evidence needed to extend. Their findings must still survive review-first
    replacement attempts.
    """
    phase = state["next_phase"]
    if (state["status"] != "escalated" or state["reason"] != BUDGET_STOP_REASON
            or phase not in {"review", "fixes", "rereview"} or not state["records"]
            or state["active_pass"] is not None):
        raise TaskError("--extend requires a proven budget-exhaustion boundary in the current run; inspect --status")
    validate_checkpoint(state)
    if state.get("delivery") is not None:
        raise TaskError("Budget extension requires completed delivery evidence; reconcile retained delivery with the ordinary loop command")
    if state["pass_count"] < state["max_passes"] and state["review_count"] < state["max_reviews"]:
        raise TaskError("Saved pass/review budget is not exhausted; use the ordinary loop command for authorized recovery")
    # The schema checks ordered phases, counts and context IDs. Also validate the
    # structured outcomes, pending feedback and non-progress/human-stop rules.
    from .implementation_pass import parse_implementation
    findings, seen, previous = [], [], []
    for record in state["records"]:
        if record["phase"] in {"review", "rereview"}:
            verdict = parse_verdict(json.dumps({k: record[k] for k in
                ("pass_id", "state", "summary", "findings", "checks")}), record["pass_id"],
                routing=True, required_checks=pending_review_checks(previous))
            if verdict["state"] != "findings" or record["resolutions"]:
                # A previously authorized blocked review is retained as history;
                # its external checks must be cleared by subsequent review.
                if record.get("continuation") is None or record is state["records"][-1]:
                    raise TaskError("Saved review is blocked or terminal, not solely budget-exhausted; inspect --retry-review and human blockers")
            if any(f["category"] != "implementation" for f in verdict["findings"]):
                raise TaskError("Saved review requires a human decision; --extend cannot authorize it")
            ids = sorted(f["id"] for f in verdict["findings"])
            signature = finding_signature(verdict["findings"])
            if verdict["state"] == "findings" and (
                    any(s["ids"] == ids or s["signature"] == signature for s in seen)
                    or findings and {f["id"] for f in findings} <= set(ids)):
                raise TaskError("Review findings made no structured progress; this is not a budget-only stop")
            if ids:
                seen.append(dict(ids=ids, signature=signature))
            findings = verdict["findings"]
        else:
            fields = ("pass_id", "state", "summary", "checks", "resolutions")
            value = {k: record[k] for k in fields}
            if "task_assessment" in record:
                value["task_assessment"] = record["task_assessment"]
            parse_implementation(json.dumps(value), record["pass_id"], findings,
                                 assessment_enabled="task_assessment" in record)
            if record["state"] != "completed" or record["findings"]:
                raise TaskError("Implementation did not complete; --extend cannot bypass its blocker")
        previous.append(record)
    if state["findings"] != findings or state["seen"] != seen:
        raise TaskError("Saved findings/progress evidence conflicts; no budget extension is safe")
    return phase


def budget_exhausted_phase(state):
    """Require saved continuity evidence before offering or authorizing extension.

    Runtime, requirements and Git identity still need controller verification.
    Never reconstruct missing delivery/session history or a replaced predecessor.
    """
    phase = _budget_exhausted_boundary(state)
    if "delivery" not in state:
        raise TaskError("Budget extension requires completed delivery evidence; reconcile retained delivery with the ordinary loop command")
    if state["reviewer"] is not None and set(state["reviewer"]) == {"context_id"}:
        raise TaskError("Budget extension lacks the saved reviewer conversation; no replacement or historical reconstruction is allowed")
    if ((state["reviewer"] is None) != (state["review_count"] == 0)
            or state["reviewer"] and any(state["reviewer"][k] != state["reviewer_options"][o]
                for k, o in (("agent", "kind"), ("model", "model"), ("mode", "mode")))):
        raise TaskError("Saved reviewer history/settings conflict; no replacement is allowed")
    return phase


def extension_limits(state, max_passes=None, max_reviews=None):
    phase = budget_exhausted_phase(state)
    passes = state["max_passes"] if max_passes is None else max_passes
    reviews = state["max_reviews"] if max_reviews is None else max_reviews
    if (type(passes) is not int or type(reviews) is not int
            or not state["max_passes"] <= passes <= 40 or not state["max_reviews"] <= reviews <= 20
            or (passes, reviews) == (state["max_passes"], state["max_reviews"])):
        raise TaskError("--extend requires an explicit increase to --max-passes (up to 40) and/or --max-reviews (up to 20); neither may decrease")
    if passes < state["pass_count"] + (2 if phase == "fixes" else 1) or reviews <= state["review_count"]:
        raise TaskError("Extended limits must cover the pending handoff and subsequent independent review")
    return passes, reviews


def extension_guidance(state):
    """Shared conditional report/status suggestion, not runtime authorization."""
    from .command import command_name
    budget_exhausted_phase(state)
    passes, reviews = min(40, state["max_passes"] + 4), min(20, state["max_reviews"] + 2)
    try:
        extension_limits(state, passes, reviews)
    except TaskError:
        return dict(command=None, message="Budget exhausted; --extend cannot provide enough capacity within 40 passes/20 reviews. Inspect the pending boundary and findings.")
    command = f"{command_name()} loop {state['binding']['issue']} --extend --max-passes {passes} --max-reviews {reviews}"
    reviewer = state["reviewer"]["context_id"] if state["reviewer"] else "the pending first review"
    return dict(command=command, message=(
        f"After inspection, try {command}. This preserves {state['implementation']['context_id']} / {reviewer}, "
        "counted passes and unresolved findings. Checkout, requirements and idle/resumable sessions must still verify. "
        "--new --from-review creates a fresh reviewer and omits prior findings; it cannot skip pending fixes."))


def guard_review_first_replacement(state):
    try:
        phase = _budget_exhausted_boundary(state)
    except TaskError:
        return
    if phase == "fixes":
        raise TaskError("--new --from-review would skip the required implementation correction and omit prior findings in a fresh reviewer; use --extend with increased bounds to preserve the original implementation/reviewer")


def preserve_blocked_review(state):
    """Do not discard outstanding blocked-review evidence through --new."""
    return state["status"] not in {"clean", "aborted"} and any(
        r["phase"] in {"review", "rereview"} and r["state"] == "blocked" for r in state["records"])


def blocked_review_phase(record):
    """Validate saved structured evidence before granting a new handoff boundary."""
    if record["phase"] not in {"review", "rereview"} or record["state"] != "blocked":
        raise TaskError("Only a completed blocked review can retry")
    parse_verdict(json.dumps({k: record[k] for k in ("pass_id", "state", "summary", "findings", "checks")}),
                  record["pass_id"], routing=True)
    if record["resolutions"] or not record["context_id"] or not record["pass_id"]:
        raise TaskError("Blocked review lacks a completed reviewer identity")
    if any(f["category"] != "implementation" for f in record["findings"]):
        raise TaskError("Blocked review requires a human decision; no implementation fixes can be routed")
    return "fixes" if record["findings"] else "rereview"


def blocked_review_retry(state):
    """Pure eligibility check; runtime identity and checkout are rechecked by the controller."""
    if (state["status"] not in {"escalated", "interrupted"} or state["active_pass"] is not None
            or state["next_phase"] is not None or not state["records"]):
        raise TaskError("Only a completed blocked review can retry; inspect active or already continued work")
    if state.get("delivery"):
        raise TaskError("Retained delivery must be reconciled with the ordinary loop command before --retry-review")
    record = state["records"][-1]
    phase = blocked_review_phase(record)
    if "continuation" in record:
        raise TaskError("Blocked review was already continued; use the ordinary loop command to reconcile it")
    if ("delivery" not in state or state["reviewer"] is None
            or state["reviewer"]["context_id"] != record["context_id"]):
        raise TaskError("Blocked review lacks completed delivery/context evidence; inspect before recovery")
    if state["review_count"] >= state["max_reviews"] or state["pass_count"] + (2 if phase == "fixes" else 1) > state["max_passes"]:
        raise TaskError("Saved pass/review limits do not permit blocked review continuation and independent re-review")
    return phase


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
        continuation = record.get("continuation")
        if "continuation" in record:
            record_fields.add("continuation")
            if continuation != blocked_review_phase(record):
                raise ValueError("invalid blocked review continuation")
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
            next_phase = continuation or ("fixes" if record["state"] == "findings" else None)
    counted = active or (abandoned["claim"] if abandoned else None)
    if state["review_count"] != reviews + bool(counted and counted["phase"] in {"review", "rereview"}):
        raise ValueError("missing review evidence")
    if abandoned and abandoned["claim"]["phase"] != next_phase:
        raise ValueError("abandoned pass sequence changed")
    if (state["status"] in {"ready", "paused", "running"} or active is not None
            or records and state["next_phase"] is not None):
        if state["next_phase"] != next_phase:
            raise ValueError("completed work would be replayed")
        validation_retry = (records and records[-1].get("continuation") == "rereview")
        if next_phase in {"fixes", "rereview"} and (state["reviewer"] is None
                or not state["findings"] and not validation_retry):
            raise ValueError("missing feedback or reviewer")
        latest_review = next((r for r in reversed(records) if r["phase"] in {"review", "rereview"}), None)
        if (latest_review and latest_review.get("continuation")
                and state["findings"] != latest_review["findings"]):
            raise ValueError("blocked review feedback changed")
    if state["status"] == "clean" and (active is not None or state["next_phase"] is not None
            or not records or records[-1]["state"] != "clean"):
        raise ValueError("unproven clean boundary")


def unclaimed_existing_boundary(state):
    """A durable-capable existing-context checkpoint has not delivered any prompt."""
    return (state["version"] in {1, 2} and "delivery" in state and state["delivery"] is None
            and state["active_pass"] is None and not state["records"]
            and state["status"] in {"ready", "interrupted", "escalated"}
            and state["next_phase"] == {1: "implementation", 2: "review"}[state["version"]]
            and not state["findings"] and not state["seen"])


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
                if state.get("initial_phase") == "review":
                    guard_review_first_replacement(old)
                if preserve_blocked_review(old):
                    raise TaskError("Blocked review evidence cannot be replaced with --new; inspect --status, "
                                    "then use --retry-review only at a completed blocked review boundary")
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
            try:
                blocked_retry = blocked_review_retry(state)
            except TaskError:
                blocked_retry = None
            try:
                exhausted = budget_exhausted_phase(state)
            except TaskError:
                exhausted = None
            if (state["status"] in {"ready", "running", "paused"} or state.get("delivery")
                    and state["active_pass"] is not None or startup_recovery_blocker(state) is None
                    or unclaimed_existing_boundary(state) or blocked_retry or exhausted):
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

    def retry_review(self, expected, reviewer):
        """Authorize once under the controller lock, retaining a concurrent pause."""
        with self.connection(write=True) as db:
            state, pause = self.decode(db.execute("SELECT payload, pause FROM checkpoint WHERE id=1").fetchone())
            if state != expected:
                raise TaskError("Blocked review checkpoint changed; nothing was continued")
            phase = blocked_review_retry(state)
            if (reviewer["context_id"] != state["reviewer"]["context_id"]
                    or len(state["reviewer"]) > 1 and reviewer != state["reviewer"]):
                raise TaskError("Blocked reviewer identity changed; nothing was continued")
            state["reviewer"] = reviewer
            state["records"][-1]["continuation"] = phase
            if phase == "fixes":
                state["seen"].append(dict(ids=sorted(f["id"] for f in state["records"][-1]["findings"]),
                                          signature=finding_signature(state["records"][-1]["findings"])))
            state.update(status="paused" if pause else "ready", next_phase=phase,
                         findings=state["records"][-1]["findings"],
                         reason="Explicit blocked review retry authorized; prior validation limitations remain outstanding")
            validate_checkpoint(state)
            db.execute("UPDATE checkpoint SET payload=? WHERE id=1", (json.dumps(state),))
        return state

    def extend(self, expected, max_passes, max_reviews):
        """Compare and authorize under the controller lock; preserve racing pause intent."""
        with self.connection(write=True) as db:
            state, pause = self.decode(db.execute("SELECT payload, pause FROM checkpoint WHERE id=1").fetchone())
            if state != expected:
                raise TaskError("Budget-exhaustion checkpoint changed; nothing was extended")
            passes, reviews = extension_limits(state, max_passes, max_reviews)
            state.update(max_passes=passes, max_reviews=reviews, status="paused" if pause else "ready",
                         reason="Explicit budget extension authorized; original pending boundary retained")
            validate_checkpoint(state)
            db.execute("UPDATE checkpoint SET payload=? WHERE id=1", (json.dumps(state),))
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
