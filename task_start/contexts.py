"""Machine-local context identities. No task text or semantic run reports live here."""

from contextlib import contextmanager, nullcontext, ExitStack
from dataclasses import replace
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sqlite3

from . import TaskError
from .integration_process import verify_shell_process
from .sessions import merge_session, same_session
from .ownership import ownership_gate
from .workspace import run


def registry_path() -> Path:
    return Path.home() / ".agentic-workflows-lite" / "contexts.sqlite3"


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


class ContextRegistry:
    """SQLite serializes writers; retired runtimes can still own retained artifacts.

    Allocation commits before any external launch. No API deletes rows, and retired
    rows retain their ordinal while dropping provider/terminal handles. Identity,
    execution metadata, resumability and lifecycle are separate columns; the
    lifecycle observations here are not a workflow state-machine abstraction.
    """

    def __init__(self, path: Path | None = None):
        self.path = path if path is not None else registry_path()

    @contextmanager
    def connection(self, *, write: bool = False):
        with ownership_gate(registry=self.path) if write else nullcontext():
            with self._connection(write=write) as db:
                yield db

    @contextmanager
    def _connection(self, *, write: bool = False):
        db = None
        try:
            if write:
                self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                # O_EXCL establishes private permissions without changing an existing file.
                try:
                    fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                except FileExistsError:
                    pass
                else:
                    os.close(fd)
                db = sqlite3.connect(self.path, timeout=30, isolation_level=None)
                db.execute("BEGIN IMMEDIATE")
                version = db.execute("PRAGMA user_version").fetchone()[0]
                if version in {0, 1}:
                    if version == 1:
                        db.execute("ALTER TABLE contexts RENAME TO contexts_v1")
                    db.execute("""CREATE TABLE contexts (
                        context_id TEXT PRIMARY KEY, issue TEXT NOT NULL,
                        role TEXT NOT NULL CHECK(role IN ('implementation', 'review', 'integration')),
                        ordinal INTEGER NOT NULL, agent TEXT NOT NULL, model TEXT, mode TEXT,
                        repository TEXT, worktree TEXT, endpoint TEXT, workspace_id TEXT,
                        tab_id TEXT, pane_id TEXT, terminal_id TEXT, session_id TEXT,
                        session_kind TEXT, herdr_session TEXT,
                        resumability TEXT NOT NULL DEFAULT 'unknown',
                        state TEXT NOT NULL DEFAULT 'launching',
                        allocated_at TEXT NOT NULL, retired_at TEXT,
                        UNIQUE(issue, role, ordinal))""")
                    if version == 1:
                        db.execute("INSERT INTO contexts SELECT * FROM contexts_v1")
                        db.execute("DROP TABLE contexts_v1")
                    db.execute("PRAGMA user_version = 2")
                    # Admission can refuse a pending disposal. Keep a valid empty
                    # schema (or completed migration) even when that claim rolls back.
                    db.commit()
                    db.execute("BEGIN IMMEDIATE")
                elif version not in {2, 3, 4}:
                    raise TaskError("Unsupported workflow context registry version")
                if version in {0, 1, 2}:
                    # Contextless workspaces can still have disposal journals.
                    # Reuse the registry for discovery, never invent a context.
                    db.execute("CREATE TABLE IF NOT EXISTS ownership_repositories (repository TEXT PRIMARY KEY)")
                    db.execute("PRAGMA user_version = 3")
                    db.commit()
                    db.execute("BEGIN IMMEDIATE")
                if version in {0, 1, 2, 3}:
                    db.execute("CREATE TABLE IF NOT EXISTS browser_contexts (context_id TEXT PRIMARY KEY REFERENCES contexts(context_id), runtime_dir TEXT NOT NULL)")
                    db.execute("PRAGMA user_version = 4")
                    db.commit()
                    db.execute("BEGIN IMMEDIATE")
            else:
                # mode=ro neither creates the file nor migrates/repairs the registry.
                db = sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True, timeout=30)
                if db.execute("PRAGMA user_version").fetchone()[0] not in {1, 2, 3, 4}:
                    raise TaskError("Unsupported workflow context registry version")
            db.row_factory = sqlite3.Row
            yield db
            if write:
                db.commit()
        except (sqlite3.Error, OSError):
            raise TaskError(f"Cannot access workflow context registry: {self.path}; "
                            "history was not reset; inspect it before retrying") from None
        finally:
            if db is not None:
                db.close()  # Rolls back any incomplete transaction.

    def remember_repository(self, repository):
        """Discover durable journals even when no agent context was allocated.

        Caller holds exclusive admission through this commit and journal fsync.
        This table is discovery only; journals still own exact resource claims.
        """
        path = Path(repository)
        if not path.is_absolute() or path.resolve() != path:
            raise TaskError("Ownership discovery requires the exact repository path")
        with self.connection(write=True) as db:
            db.execute("INSERT OR IGNORE INTO ownership_repositories VALUES (?)", (str(path),))

    def repositories(self):
        if not self.path.exists():
            return []
        with self.connection() as db:
            if db.execute("PRAGMA user_version").fetchone()[0] < 3:
                return []
            return [row[0] for row in db.execute("SELECT repository FROM ownership_repositories ORDER BY repository")]

    @contextmanager
    def claim_connection(self, repository, worktree, *, endpoint=None, workspace_id=None, terminal_id=None):
        from .cleanup import check_unreserved
        from .publication_state import PublicationStore
        from .workspace import Git
        with ownership_gate(registry=self.path):
            # Another allocator may have created the SQLite file without yet
            # committing its schema. Initialize/snapshot in a short transaction,
            # then close it before trying resource locks (never invert the order).
            with self.connection(write=True) as db:
                history = [dict(row) for row in db.execute("SELECT * FROM contexts")]
                repositories = {row[0] for row in db.execute("SELECT repository FROM ownership_repositories")}
            with ExitStack() as locks:
                # Claim admission cannot race a selected controller, including a
                # different path claiming the same exact Herdr execution binding.
                paths = {worktree} if worktree else set()
                for context in history:
                    if (not context["retired_at"] and endpoint is not None and context["endpoint"] == endpoint
                            and (workspace_id is not None and context["workspace_id"] == workspace_id
                                 or terminal_id is not None and context["terminal_id"] == terminal_id)):
                        if context["worktree"]:
                            paths.add(context["worktree"])
                # Use trusted Git registrations, not arbitrary .git directories
                # found by walking upward through the host filesystem.
                repositories.update(c["repository"] for c in history if c["repository"])
                if repository:
                    repositories.add(str(repository))
                registered = set()
                for repo in sorted(repositories):
                    if (Path(repo) / ".git").exists():
                        registered.update(Path(t["worktree"]) for t in Git(Path(repo)).worktrees())
                checkouts = set()
                for path in paths:
                    candidate = Path(path)
                    enclosing = {p for p in registered if candidate.is_relative_to(p) and (p / ".git").exists()}
                    # The permanent checkout contains shared administration, not
                    # the linked task's lifetime ownership. Keep genuinely nested
                    # task locks, but do not contend with an unrelated base fetch.
                    checkouts.update(p for p in enclosing if str(p) not in repositories
                                     or not any(q != p and q.is_relative_to(p) for q in enclosing))
                    if (candidate / ".git").exists():
                        checkouts.add(candidate)
                for path in sorted(checkouts):
                    locks.enter_context(PublicationStore(path).locked(allow_owned=True, registry=self.path))
                with self.connection(write=True) as db:
                    contexts = [dict(row) for row in db.execute("SELECT * FROM contexts")]
                    check_unreserved(self, repository, worktree, endpoint=endpoint,
                                     workspace_id=workspace_id, terminal_id=terminal_id, contexts=contexts)
                    yield db

    def allocate(self, issue: str, role: str, *, agent: str, model: str | None = None,
                 mode: str | None = None, repository: str | None = None,
                 worktree: str | None = None, endpoint: str | None = None,
                 workspace_id: str | None = None, tab_id: str | None = None,
                 pane_id: str | None = None, terminal_id: str | None = None) -> str:
        if not re.fullmatch(r"[A-Z][A-Z0-9]*-[1-9][0-9]*", issue):
            raise TaskError("Context allocation requires the canonical issue identifier")
        if role not in {"implementation", "review", "integration"}:
            raise TaskError("Context role must be implementation, review, or integration")
        with self.claim_connection(repository, worktree, endpoint=endpoint,
                                   workspace_id=workspace_id, terminal_id=terminal_id) as db:
            pending = db.execute("""SELECT context_id FROM contexts WHERE issue=? AND role=?
                AND repository IS ? AND worktree IS ? AND state='awaiting_user'""",
                (issue, role, repository, worktree)).fetchone()
            if pending:
                raise TaskError(f"{pending['context_id']} is waiting for Codex trust/setup; "
                                "continue its original workflow command or inspect it; no replacement was allocated")
            if endpoint and pane_id:
                pending = db.execute("""SELECT context_id FROM contexts WHERE endpoint=?
                    AND pane_id=? AND state IN ('launching', 'awaiting_user')""", (endpoint, pane_id)).fetchone()
                if pending:
                    raise TaskError(f"{pending['context_id']} has an unfinished launch in {pane_id}; "
                                    "inspect it before retrying or cleaning up")
            ordinal = db.execute("SELECT COALESCE(MAX(ordinal), 0)+1 FROM contexts WHERE issue=? AND role=?",
                                 (issue, role)).fetchone()[0]
            prefix = {"implementation": "I", "review": "R", "integration": "G"}[role]
            context_id = f"{issue}-{prefix}{ordinal}"
            db.execute("""INSERT INTO contexts (context_id, issue, role, ordinal, agent, model, mode,
                repository, worktree, endpoint, workspace_id, tab_id, pane_id, terminal_id, allocated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                       (context_id, issue, role, ordinal, agent, model, mode, repository, worktree,
                        endpoint, workspace_id, tab_id, pane_id, terminal_id, now()))
        return context_id

    def update(self, context_id: str, **values) -> None:
        allowed = {"state", "session_id", "session_kind", "herdr_session", "resumability"}
        if not values or values.keys() - allowed:
            raise TaskError("Invalid workflow context update")
        context = self.get(context_id)
        with self.claim_connection(context["repository"], context["worktree"], endpoint=context["endpoint"],
                                   workspace_id=context["workspace_id"], terminal_id=context["terminal_id"]) as db:
            result = db.execute("UPDATE contexts SET " + ", ".join(f"{key}=?" for key in values)
                                + " WHERE context_id=? AND retired_at IS NULL",
                                (*values.values(), context_id))
            if result.rowcount != 1:
                raise TaskError(f"Context {context_id} is absent or retired; mapping was not changed")

    def browser_enabled(self, context_id: str) -> bool:
        return self.browser_runtime(context_id) is not None

    def browser_runtime(self, context_id: str) -> str | None:
        """Keep capability metadata separate from frozen context rows in journals."""
        if not self.path.exists():
            return None
        with self.connection() as db:
            if db.execute("PRAGMA user_version").fetchone()[0] < 4:
                return None
            row = db.execute("SELECT runtime_dir FROM browser_contexts WHERE context_id=?", (context_id,)).fetchone()
            return row[0] if row else None

    def enable_browser(self, context_id: str, runtime_dir: str) -> None:
        if not isinstance(runtime_dir, str) or not Path(runtime_dir).is_absolute():
            raise TaskError("Browser runtime requires an absolute path")
        context = self.get(context_id)
        with self.claim_connection(context["repository"], context["worktree"], endpoint=context["endpoint"],
                                   workspace_id=context["workspace_id"], terminal_id=context["terminal_id"]) as db:
            current = db.execute("SELECT * FROM contexts WHERE context_id=?", (context_id,)).fetchone()
            if current is None or dict(current) != context or context["retired_at"] is not None:
                raise TaskError("Browser actor changed or retired before opt-in")
            previous = db.execute("SELECT runtime_dir FROM browser_contexts WHERE context_id=?", (context_id,)).fetchone()
            if previous and previous[0] != runtime_dir:
                raise TaskError("Browser runtime directory changed for this actor; use its original assignment")
            db.execute("INSERT OR IGNORE INTO browser_contexts VALUES (?, ?)", (context_id, runtime_dir))

    def list(self, issue: str | None = None, *, include_retired: bool = False) -> list[dict]:
        if not self.path.exists():
            return []
        with self.connection() as db:
            return [dict(row) for row in db.execute("""SELECT * FROM contexts
                WHERE (? IS NULL OR issue=?) AND (? OR retired_at IS NULL)
                ORDER BY issue, role, ordinal""", (issue, issue, include_retired))]

    def get(self, context_id: str) -> dict:
        matches = [c for c in self.list(include_retired=True) if c["context_id"] == context_id]
        if len(matches) != 1:
            raise TaskError(f"Unknown workflow context {context_id}")
        return matches[0]

    def check_pending_startup(self, issue: str, repository: Path, worktree: Path, role: str) -> None:
        for context in self.list(issue):
            if (context["repository"] == str(repository) and context["worktree"] == str(worktree)
                    and context["role"] == role and context["state"] == "awaiting_user"):
                raise TaskError(f"{context['context_id']} is waiting for Codex trust/setup in {context['pane_id']}; "
                                "continue its original workflow command or inspect it; no replacement was allocated")

    def claim_review(self, context: dict) -> None:
        """Serialize passes in one reviewer, without a second identity store."""
        with self.claim_connection(context["repository"], context["worktree"], endpoint=context["endpoint"],
                                   workspace_id=context["workspace_id"], terminal_id=context["terminal_id"]) as db:
            current = db.execute("SELECT * FROM contexts WHERE context_id=?",
                                 (context["context_id"],)).fetchone()
            if (current is None or dict(current) != context or context["role"] != "review"
                    or context["state"] != "active" or context["retired_at"]):
                raise TaskError("Reviewer is stale, retired, busy, or uncertain; inspect its context")
            db.execute("UPDATE contexts SET state='reviewing' WHERE context_id=?", (context["context_id"],))

    def rebind_review_pane(self, context: dict, pane: dict) -> None:
        """Update a relocated terminal, or bind a safely resumed replacement pane.

        Relocation requires the same terminal and reconciled session evidence.
        A replacement requires missing-pane/provider evidence and a claimed pass.
        The compare-and-set prevents another pass or cleanup from changing the binding.
        """
        with self.claim_connection(context["repository"], context["worktree"], endpoint=context["endpoint"],
                                   workspace_id=pane["workspace_id"], terminal_id=pane["terminal_id"]) as db:
            current = db.execute("SELECT * FROM contexts WHERE context_id=?",
                                 (context["context_id"],)).fetchone()
            relocated = pane["terminal_id"] == context["terminal_id"]
            expected = context if relocated else dict(context, state="reviewing")
            if (current is None or dict(current) != expected
                    or context["role"] != "review" or context["state"] not in {"active", "reviewing"}
                    or context["retired_at"]
                    or pane["workspace_id"] != context["workspace_id"]):
                raise TaskError("Reviewer binding changed; replacement pane was not registered")
            db.execute("UPDATE contexts SET pane_id=?, terminal_id=?, tab_id=? WHERE context_id=?",
                       (pane["pane_id"], pane["terminal_id"], pane["tab_id"], context["context_id"]))

    def abandon_integration(self, context: dict, timestamp: str) -> None:
        """CAS-retire one proven stopped attempt without erasing identity evidence."""
        with self.connection(write=True) as db:
            current = db.execute("SELECT * FROM contexts WHERE context_id=?", (context["context_id"],)).fetchone()
            if (current is None or dict(current) != context or context["role"] != "integration"
                    or context["state"] != "uncertain" or context["retired_at"]
                    or context["resumability"] != "unknown" or context_reference(context) is not None):
                raise TaskError("Integration context changed; abandonment was not applied")
            db.execute("UPDATE contexts SET state='abandoned', retired_at=? WHERE context_id=?",
                       (timestamp, context["context_id"]))

    def reconcile_loop_abort(self, contexts, reconciled):
        """CAS the exact stopped claims; preserve provider and terminal history.

        Retry accepts only the original rows or the journal's exact resulting rows.
        Reviewer retirement is local to the explicitly abandoned loop pass, not
        general stale-reviewer reconciliation.
        """
        if len(contexts) != len(reconciled):
            raise TaskError("Abort context evidence is incomplete")
        with self.connection(write=True) as db:
            for before, after in zip(contexts, reconciled):
                current = db.execute("SELECT * FROM contexts WHERE context_id=?", (before["context_id"],)).fetchone()
                if current is None or dict(current) not in (before, after):
                    raise TaskError("Context changed during abort; no claim was released")
                if any(before[k] != after[k] for k in before if k not in {"state", "retired_at", "resumability", "herdr_session"}):
                    raise TaskError("Abort cannot replace a context identity")
                if before["herdr_session"] != after["herdr_session"]:
                    prior, refined = context_reference(before), context_reference(after)
                    try:
                        if (prior is None or refined is None or
                                merge_session(prior, refined, before["agent"]) != merge_session(None, refined, before["agent"])):
                            raise ValueError("not an identity refinement")
                    except ValueError:
                        raise TaskError("Abort cannot replace a context identity") from None
            for after in reconciled:
                db.execute("UPDATE contexts SET state=?, retired_at=?, resumability=?, herdr_session=? WHERE context_id=?",
                           (after["state"], after["retired_at"], after["resumability"], after["herdr_session"], after["context_id"]))

    def retire(self, issue: str, repository: Path, worktree: Path, *,
               endpoint: str, workspace_id: str | None) -> None:
        if not self.path.exists():
            return
        with self.connection(write=True) as db:
            db.execute("""UPDATE contexts SET state='retired', retired_at=?, terminal_id=NULL,
                session_id=NULL, session_kind=NULL, herdr_session=NULL, resumability='unknown'
                WHERE issue=? AND repository=? AND worktree=? AND endpoint=?
                AND workspace_id IS ? AND retired_at IS NULL""",
                       (now(), issue, str(repository), str(worktree), endpoint, workspace_id))

    @staticmethod
    def disposal_tombstone(context: dict, timestamp: str) -> dict:
        return dict(context, state="retired", retired_at=timestamp, terminal_id=None,
                    session_id=None, session_kind=None, herdr_session=None, resumability="unknown")

    def retire_execution(self, contexts: "list[dict]", timestamp: str) -> None:
        """Atomically retire the frozen execution, including abandoned G contexts.

        Exact rows or their exact resulting tombstones are the only retry states.
        No predicate can accidentally retire a newly allocated context.
        """
        if not contexts:
            return
        with self.connection(write=True) as db:
            for context in contexts:
                current = db.execute("SELECT * FROM contexts WHERE context_id=?",
                                     (context["context_id"],)).fetchone()
                tombstone = self.disposal_tombstone(context, timestamp)
                if current is None or dict(current) not in (context, tombstone):
                    raise TaskError("Context changed during forced cleanup; mappings were retained")
                db.execute("""UPDATE contexts SET state='retired', retired_at=?, terminal_id=NULL,
                    session_id=NULL, session_kind=NULL, herdr_session=NULL, resumability='unknown'
                    WHERE context_id=?""", (timestamp, context["context_id"]))


class HerdrContexts:
    """Read Herdr-owned facts and name panes using the installed 0.9.1 API."""

    def endpoint(self) -> str:
        # A pane ID is only unique inside its server. Never compare IDs across sockets.
        value = os.environ.get("HERDR_SOCKET_PATH")
        if not value:
            match = re.search(r"^\s*socket:\s*(.+)$", run(["herdr", "status"]), re.MULTILINE)
            value = match.group(1) if match else None
        if not value or not Path(value).is_absolute():
            raise TaskError("Cannot identify the current Herdr socket; context identity is unknown")
        return str(Path(value).resolve())

    def command(self, group: str, operation: str, *args: str) -> dict:
        try:
            payload = json.loads(run(["herdr", group, operation, *args]))
            result = payload["result"]
            expected = {("api", "snapshot"): "session_snapshot", ("pane", "get"): "pane_info",
                        ("pane", "rename"): "pane_info", ("pane", "split"): "pane_info",
                        ("pane", "process-info"): "pane_process_info"}
            if payload.get("error") or result["type"] != expected[group, operation]:
                raise ValueError("unexpected response")
            return result
        except (ValueError, KeyError, TypeError):
            raise TaskError(f"Unexpected Herdr {group} {operation} response") from None

    def snapshot(self) -> list[dict]:
        try:
            panes = self.command("api", "snapshot")["snapshot"]["panes"]
            if not isinstance(panes, list):
                raise ValueError("invalid panes")
            for pane in panes:
                self.validate_pane(pane)
            if len({p["pane_id"] for p in panes}) != len(panes):
                raise ValueError("ambiguous panes")
            return panes
        except (KeyError, ValueError, TypeError):
            raise TaskError("Unexpected Herdr context snapshot") from None

    @staticmethod
    def validate_pane(pane: dict) -> None:
        for key in ("pane_id", "workspace_id", "tab_id", "terminal_id"):
            if not isinstance(pane[key], str) or not pane[key]:
                raise ValueError("missing pane identity")

    def label(self, pane: dict, context_id: str) -> None:
        # Recheck the terminal before mutation; position/focus never select a target.
        current = self.command("pane", "get", pane["pane_id"])["pane"]
        if any(current.get(k) != pane.get(k) for k in
               ("pane_id", "workspace_id", "terminal_id", "agent", "label")):
            raise TaskError("Herdr pane changed before labeling; context was not rebound")
        self.command("pane", "rename", pane["pane_id"], context_id)
        current = self.command("pane", "get", pane["pane_id"])["pane"]
        if current.get("terminal_id") != pane["terminal_id"] or current.get("label") != context_id:
            raise TaskError(f"Herdr pane label {context_id} could not be confirmed")

    def split(self, anchor: dict, path: Path) -> dict:
        current = self.command("pane", "get", anchor["pane_id"])["pane"]
        if any(current.get(k) != anchor.get(k) for k in
               ("workspace_id", "tab_id", "pane_id", "terminal_id")):
            raise TaskError("Task pane changed before split; no reviewer was launched")
        try:
            pane = self.command("pane", "split", anchor["pane_id"], "--direction", "down",
                                "--cwd", str(path), "--no-focus")["pane"]
            self.validate_pane(pane)
            if (pane["workspace_id"] != anchor["workspace_id"] or pane["tab_id"] != anchor["tab_id"]
                    or pane["pane_id"] == anchor["pane_id"] or pane.get("agent")
                    or Path(pane["cwd"]).resolve() != path):
                raise ValueError("split target mismatch")
            return pane
        except (KeyError, TypeError, ValueError):
            raise TaskError("Herdr did not confirm a new pane in the exact task tab/worktree") from None


def context_reference(context: dict) -> dict | None:
    """Read all identity evidence from the existing DEV-41 mapping."""
    try:
        reference = (dict(agent=context["agent"], kind=context["session_kind"], value=context["session_id"])
                     if context["session_id"] else None)
        reported = json.loads(context["herdr_session"]) if context["herdr_session"] else None
        merged = merge_session(reference, reported, context["agent"])
        return dict(reported, **merged) if isinstance(reported, dict) else merged
    except (TypeError, ValueError):
        raise TaskError("Invalid or conflicting workflow session identity") from None


def context_observer(registry: ContextRegistry, context_id: str):
    """Guard observations against identity retained by this observer.

    Feed provider verification back through the active observer so later locator-only
    reports retain that evidence. A new observer reads the registry only at creation.
    """
    context = registry.get(context_id)
    established = context_reference(context)
    resumability = context["resumability"]

    def record(values):
        nonlocal established, resumability
        values = dict(values)
        terminal = values.pop("terminal_id", context["terminal_id"])
        if terminal != context["terminal_id"]:
            raise TaskError("Herdr terminal changed during observation; context was not rebound")
        candidate = established
        try:
            reported = json.loads(values["herdr_session"]) if values.get("herdr_session") is not None else None
            candidate = merge_session(candidate, reported, context["agent"])
            if values.get("session_id") is not None:
                candidate = merge_session(candidate, dict(agent=context["agent"], kind=values.get("session_kind"),
                                          value=values["session_id"]), context["agent"])
        except (TypeError, ValueError):
            raise TaskError("Agent session changed or is malformed during observation; context was not rebound") from None
        values.pop("session_id", None)
        values.pop("session_kind", None)
        if reported is None:
            values.pop("herdr_session", None)
        elif candidate and "conversation_id" in candidate:
            # Keep provider-authenticated evidence when a later Herdr observation
            # supplies only the locator (or changes reporting provenance).
            reported = dict(reported) if isinstance(reported, dict) else dict(candidate)
            reported["conversation_id"] = candidate["conversation_id"]
            values["herdr_session"] = json.dumps(reported)
        if candidate is not None:
            values.update(session_id=candidate["value"], session_kind=candidate["kind"])
        if values.get("resumability") == "unknown" and resumability != "unknown":
            values.pop("resumability")
        if values:
            registry.update(context_id, **values)
        established = candidate
        resumability = values.get("resumability", resumability)

    return record


def allocate_launch(execution, registry, herdr):
    """Reserve the exact shell/context before launch or loop checkpoint creation."""
    workspace = execution.workspace
    endpoint = herdr.endpoint()
    panes = herdr.snapshot()
    if execution.purpose == "implementation" and any(p.get("agent") == execution.options.kind and p["workspace_id"] == workspace.workspace_id
           for p in panes):
        raise TaskError("An implementation agent already occupies this workspace; inspect/continue it "
                        "or use --no-agent. Its context identity was preserved")
    matches = [p for p in panes if p["pane_id"] == workspace.pane_id]
    if (len(matches) != 1 or matches[0]["workspace_id"] != workspace.workspace_id
            or matches[0]["tab_id"] != workspace.tab_id or matches[0].get("agent")):
        raise TaskError("Confirmed Herdr shell pane is missing or occupied; no context was launched")
    pane = matches[0]
    return registry.allocate(execution.issue.identifier, execution.purpose,
        agent=execution.options.kind, model=execution.options.model, mode=execution.options.mode,
        repository=str(execution.repository), worktree=str(workspace.path), endpoint=endpoint,
        workspace_id=workspace.workspace_id, tab_id=workspace.tab_id, pane_id=workspace.pane_id,
        terminal_id=pane["terminal_id"])


def pending_launch(execution, context_id, registry, herdr, *, require_idle=False):
    """A reserved launch is usable only while its original shell is still empty."""
    context = registry.get(context_id)
    workspace = execution.workspace
    expected = dict(issue=execution.issue.identifier, role=execution.purpose, agent=execution.options.kind,
                    model=execution.options.model, mode=execution.options.mode,
                    repository=str(execution.repository), worktree=str(workspace.path),
                    endpoint=herdr.endpoint(), workspace_id=workspace.workspace_id,
                    tab_id=workspace.tab_id, pane_id=workspace.pane_id, state="launching")
    if (any(context[k] != v for k, v in expected.items()) or context["retired_at"]
            or context_reference(context) is not None):
        raise TaskError("Reserved implementation context changed or launch is uncertain; no replay is allowed")
    panes = herdr.snapshot()
    pane, _ = reconcile(context, panes)
    if (pane is None or pane.get("agent") is not None
            or Path(pane.get("cwd", "")).resolve() != workspace.path
            or any(p.get("agent") for p in panes if p["workspace_id"] == workspace.workspace_id)):
        raise TaskError("Reserved implementation shell is missing, occupied, or changed; no launch is safe")
    if require_idle:
        # Herdr may omit agent/session and report unknown for ordinary shells.
        # Metadata only rules out conflicts; every related pane still needs the
        # positive shell/process proof below. Never discover or adopt sessions.
        if (any(context[k] is not None for k in ("session_id", "session_kind", "herdr_session"))
                or context["resumability"] != "unknown"):
            raise TaskError("Reserved implementation has provider/session or uncertain runtime evidence")
        related = []
        for current in panes:
            paths = (current.get(k) for k in ("cwd", "foreground_cwd"))
            if (current["workspace_id"] != workspace.workspace_id
                    and not any(isinstance(p, str) and Path(p).is_absolute()
                                and Path(p).resolve().is_relative_to(workspace.path) for p in paths)):
                continue
            if (current.get("agent") is not None or current.get("agent_session") is not None
                    or current.get("agent_status") not in (None, "unknown", "idle", "done")):
                raise TaskError(f"Pane {current['pane_id']} has provider/session or uncertain runtime evidence")
            related.append(current)
        others = [c for c in registry.list() if c["context_id"] != context_id]
        if any(c["worktree"] == str(workspace.path) or c["endpoint"] == context["endpoint"] and (
                c["workspace_id"] == workspace.workspace_id or c["terminal_id"] == context["terminal_id"]
                or c["pane_id"] == workspace.pane_id) for c in others):
            raise TaskError("Another workflow context conflicts with the reserved implementation")
        from .cleanup import disposed_context_history
        disposed_context_history(registry, execution.issue.identifier, execution.repository, excluding={context_id})
        for current in related:
            try:
                process = herdr.command("pane", "process-info", "--pane", current["pane_id"])["process_info"]
                pid = process["shell_pid"]
                group, foreground = process["foreground_process_group_id"], process["foreground_processes"]
                if (process["pane_id"] != current["pane_id"] or type(pid) is not int or pid <= 0
                        or type(group) is not int or group != pid or not isinstance(foreground, list)
                        or any(not isinstance(p, dict) or type(p.get("pid")) is not int for p in foreground)
                        or [p["pid"] for p in foreground] != [pid]):
                    raise ValueError("not an idle shell")
                verify_shell_process(pid, foreground[0]["argv"])
            except (KeyError, TypeError, ValueError):
                raise TaskError(f"Pane {current['pane_id']} shell is busy or its process identity is unverified") from None
            except TaskError as error:
                raise TaskError(f"Pane {current['pane_id']}: {error}") from None
    return pane


def launch_registered(adapter, execution, *, registry: ContextRegistry | None = None,
                      herdr: HerdrContexts | None = None, handoff_factory=None):
    """Register a fresh launch; existing agents are never resumed or relabeled here."""
    registry, herdr = registry or ContextRegistry(), herdr or HerdrContexts()
    context_id = allocate_launch(execution, registry, herdr)
    return launch_allocated(adapter, execution, context_id, registry, herdr, handoff_factory=handoff_factory)


def launch_allocated(adapter, execution, context_id, registry, herdr, *, handoff_factory=None,
                     persist_observer=None):
    """Deliver once to an allocation owned by the caller (and its durable claim)."""
    workspace = execution.workspace
    context = registry.get(context_id)
    pane, _ = reconcile(context, herdr.snapshot())
    if pane is None or pane.get("agent") or context["state"] != "launching":
        raise TaskError("Allocated shell changed before launch; inspect before retrying")
    # Initial loop passes share this guard with provider verification and polling.
    persist = persist_observer or context_observer(registry, context_id)

    def record(values):
        persist(values)
        if execution.runtime_observer:
            execution.runtime_observer(values)

    try:
        if handoff_factory is not None:
            execution = replace(execution, handoff=handoff_factory(context_id))
        herdr.label(pane, context_id)
        from .browser import prepare_browser
        execution = prepare_browser(replace(execution, context_id=context_id), context, registry)
        result = adapter.launch(replace(execution, runtime_observer=record, context_id=context_id))
        if result.kind != execution.options.kind or result.pane_id != workspace.pane_id:
            raise TaskError("Execution result does not match the allocated workflow context")
        record(dict(state="reviewing" if execution.purpose == "review" else "active", session_id=result.session_id,
                    session_kind=result.session_kind, resumability=result.resumability))
        return replace(result, context_id=context_id, summary=f"{context_id}: {result.summary}")
    except BaseException as error:
        # Startup/prompt delivery may have succeeded before a timeout. Keep its identity.
        try:
            # Retain the human boundary even if stdin closes or the owner exits.
            # Another command must not allocate a replacement for that launch.
            if registry.get(context_id)["state"] != "awaiting_user":
                registry.update(context_id, state="uncertain")
        except TaskError:
            pass  # The committed allocation remains even if a later write fails.
        if isinstance(error, Exception):
            raise TaskError(f"{context_id}: {error}; inspect the context before retrying") from error
        raise


def reconcile(context: dict, panes: list[dict], *, allow_relocation: bool = False) -> tuple[dict | None, list[str]]:
    """Read-only identity reconciliation; callers explicitly persist verified placement."""
    matches = [p for p in panes if p["pane_id"] == context["pane_id"]]
    terminals = [p for p in panes if context["terminal_id"] and p["terminal_id"] == context["terminal_id"]]
    if len(matches) > 1 or len(terminals) > 1:
        return None, ["ambiguous pane/terminal identity; binding unchanged"]
    if not matches:
        if len(terminals) == 1 and allow_relocation:
            matches = terminals
        elif len(terminals) == 1:
            return None, [f"moved/stale: terminal now at {terminals[0]['pane_id']} (binding unchanged)"]
        else:
            return None, ["missing pane"]
    pane = matches[0]
    if pane["terminal_id"] != context["terminal_id"] or pane["workspace_id"] != context["workspace_id"]:
        return None, ["mismatched terminal/workspace; binding unchanged"]
    notes = []
    if pane["pane_id"] != context["pane_id"]:
        notes.append("pane changed")
    if pane["tab_id"] != context["tab_id"]:
        notes.append("tab changed")
    if pane.get("label") != context["context_id"]:
        notes.append(f"renamed/unlabeled: {pane.get('label') or 'unknown'}")
    if not pane.get("agent"):
        notes.append("agent absent; session unknown")
    elif pane["agent"] != context["agent"]:
        notes.append("agent mismatch")
    try:
        expected = context_reference(context)
    except TaskError:
        return pane, [*notes, "session identity invalid"]
    actual = pane.get("agent_session")
    if expected:
        try:
            same = same_session(expected, actual, context["agent"])
        except ValueError:
            same = False  # Malformed live evidence is divergence, not a new binding.
        if not same:
            notes.append("session mismatch" if actual is not None else "session unknown/stale")
    else:
        notes.append("session identity unknown")
    return pane, notes


def inspect_contexts(issue: str | None = None, *, include_retired: bool = False,
                     registry: ContextRegistry | None = None, herdr: HerdrContexts | None = None) -> str:
    registry, herdr = registry or ContextRegistry(), herdr or HerdrContexts()
    contexts = registry.list(issue, include_retired=include_retired)
    if not contexts:
        return "No known workflow contexts."
    endpoint, panes, error = None, [], None
    if any(not c["retired_at"] for c in contexts):
        try:
            endpoint, panes = herdr.endpoint(), herdr.snapshot()
        except TaskError as exc:
            error = str(exc)
    lines = ["CONTEXT | ISSUE | ROLE | AGENT | MODEL / MODE | STATE / LIVE | RESUMABILITY | HERDR | NOTES"]
    for context in contexts:
        live = None
        if context["retired_at"]:
            notes = [f"allocated {context['allocated_at']}; retired {context['retired_at']}"]
        elif error:
            notes = [f"unknown/stale: {error}"]
        elif context["endpoint"] != endpoint:
            notes = ["unknown/stale: different Herdr server"]
        else:
            live, notes = reconcile(context, panes)
        location = live or context
        lines.append(" | ".join([
            context["context_id"], context["issue"], context["role"],
            (live.get("agent") if live else None) or context["agent"],
            f"{context['model'] or 'unknown'} / {context['mode'] or 'unknown'}",
            f"{context['state']} / {(live.get('agent_status') if live else None) or 'unknown'}",
            context["resumability"],
            f"{context['endpoint'] or 'unknown'} {location['workspace_id'] or '?'} "
            f"{location['tab_id'] or '?'} {location['pane_id'] or '?'}",
            "; ".join(notes) or "matched",
        ]))
    # Labels and server error text are untrusted terminal display strings.
    return "\n".join("".join(c if c.isprintable() else "?" for c in line) for line in lines)
