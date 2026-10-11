"""Bounded, best-effort observations. Never read these to authorize workflow work."""

from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
from functools import wraps
import json
import os
from pathlib import Path
import platform
import stat
from uuid import uuid4


VERSION = 1
MAX_RUNS = 256
MAX_RECORD_BYTES = 4096
MAX_BYTES = 2 * 1024 * 1024
ROLES = {"implementation", "fix", "review", "re-review", "integration", "research"}
STATUSES = {"running", "dispatched", "completed", "failed", "interrupted", "blocked", "timed_out"}
LOOP_STATUSES = {"ready", "running", "paused", "clean", "escalated", "interrupted", "aborting", "aborted"}


def best_effort(function):
    @wraps(function)
    def call(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except Exception:
            # Includes corrupt/future schemas, full disks, permissions and lock
            # contention. No metrics exception may mask a workflow result/error.
            return None
    return call


def timestamp():
    return datetime.now(timezone.utc).isoformat()


def validate(record):
    nullable_text = {"agent", "model", "mode", "context_id", "loop_id", "previous_run_id",
                     "implementation_context_id"}
    required = nullable_text | {"version", "run_id", "task_id", "role", "status", "review_round",
                                "started_at", "finished_at", "wall_clock_seconds", "runtime_versions"}
    optional = {"review_outcome", "findings_count", "loop_status", "result_observed_at"}

    def text(value):
        return isinstance(value, str) and bool(value.strip())

    def instant(value):
        if not isinstance(value, str):
            raise ValueError("Invalid metrics timestamp")
        parsed = datetime.fromisoformat(value)
        if parsed.utcoffset() is None:
            raise ValueError("Metrics timestamps require a timezone")
        return parsed

    if (not isinstance(record, dict) or not required <= record.keys()
            or record.keys() - required - optional
            or type(record["version"]) is not int or record["version"] != VERSION
            or any(not text(record[k]) for k in ("run_id", "task_id", "role", "status"))
            or record["role"] not in ROLES or record["status"] not in STATUSES
            or any(record[k] is not None and not text(record[k]) for k in nullable_text)):
        raise ValueError("Unsupported metrics record")
    for key, minimum in (("review_round", 1), ("findings_count", 0)):
        value = record.get(key)
        if value is not None and (type(value) is not int or value < minimum):
            raise ValueError("Invalid metrics count")
    for key, choices in (("review_outcome", {"clean", "findings"}), ("loop_status", LOOP_STATUSES)):
        value = record.get(key)
        if value is not None and (not text(value) or value not in choices):
            raise ValueError("Invalid metrics outcome")
    versions = record["runtime_versions"]
    if not isinstance(versions, dict) or any(not text(k) or not text(v) for k, v in versions.items()):
        raise ValueError("Invalid runtime versions")
    start = instant(record["started_at"])
    duration = record["wall_clock_seconds"]
    if record["finished_at"] is None:
        if duration is not None:
            raise ValueError("Duration requires an observed finish")
    elif (type(duration) not in {int, float}
          or duration != (instant(record["finished_at"]) - start).total_seconds()):
        # Negative wall-clock deltas are possible after clock adjustments. Exact
        # equality also rejects nonfinite values without estimating missing time.
        raise ValueError("Duration must equal finish minus start")
    if "result_observed_at" in record:
        instant(record["result_observed_at"])
    if len(json.dumps(record, allow_nan=False).encode()) > MAX_RECORD_BYTES:
        raise ValueError("Oversized metrics record")


class MetricsStore:
    """One private atomic JSON snapshot, with nonblocking serialized writers.

    The registry supplies only the local directory; its database is never read.
    Unknown/corrupt metrics are left intact until removed by the user.
    """

    def __init__(self, registry_path):
        self.directory = Path(registry_path).parent / "metrics"

    @staticmethod
    def private(fd, *, directory=False):
        info = os.fstat(fd)
        if (info.st_uid != os.getuid() or info.st_mode & 0o077
                or not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode))
                or not directory and info.st_nlink != 1):
            raise ValueError("Metrics storage must be private and owned by this user")

    @contextmanager
    def opened(self, *, write=False):
        if write:
            self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            self.private(fd, directory=True)
            yield fd
        finally:
            os.close(fd)

    def _read(self, directory):
        try:
            fd = os.open("runs.json", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        except FileNotFoundError:
            return []
        with os.fdopen(fd, "rb") as source:
            self.private(source.fileno())
            raw = source.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise ValueError("Oversized metrics")
        value = json.loads(raw)
        if (not isinstance(value, dict) or type(value.get("version")) is not int or value["version"] != VERSION
                or not isinstance(value.get("runs"), list) or len(value["runs"]) > MAX_RUNS):
            raise ValueError("Unsupported metrics schema")
        for record in value["runs"]:
            validate(record)
        if len({r["run_id"] for r in value["runs"]}) != len(value["runs"]):
            raise ValueError("Duplicate metrics IDs")
        return value["runs"]

    @best_effort
    def read(self):
        """Return v1 records for inspection/export, or None if unavailable."""
        with self.opened() as directory:
            return self._read(directory)

    @best_effort
    def write(self, record=None, *, run_id=None, loop_status=None, recovered_result=None):
        """Upsert a run, or annotate retained observations without rewriting time."""
        if record is not None:
            validate(record)
            run_id = record["run_id"]
        with self.opened(write=True) as directory:
            lock = os.open("writer.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK,
                           0o600, dir_fd=directory)
            try:
                self.private(lock)
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                records = self._read(directory)
                index = next((i for i, r in enumerate(records) if r["run_id"] == run_id), None)
                if record is None:
                    if index is None:
                        return
                    record = dict(records[index], loop_status=loop_status)
                    if recovered_result is not None:
                        if recovered_result["pass_id"] != run_id:
                            raise ValueError("Recovery observation belongs to another pass")
                        status = recovered_result["state"]
                        record.update(status="completed" if status in {"clean", "findings"} else status,
                                      result_observed_at=timestamp())
                        if record["role"] in {"review", "re-review"}:
                            if status in {"clean", "findings"}:
                                record["review_outcome"] = status
                            record["findings_count"] = len(recovered_result["findings"])
                    validate(record)
                if index is None:
                    records.append(record)
                else:
                    records[index] = record
                payload = json.dumps(dict(version=VERSION, runs=records[-MAX_RUNS:]),
                                     ensure_ascii=True, indent=2).encode() + b"\n"
                if len(payload) > MAX_BYTES:
                    raise ValueError("Oversized metrics")
                temporary = "pending.json"
                fd = os.open(temporary, os.O_CREAT | os.O_WRONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                             0o600, dir_fd=directory)
                try:
                    with os.fdopen(fd, "wb") as target:
                        self.private(target.fileno())
                        target.truncate(0)
                        target.write(payload)
                    os.replace(temporary, "runs.json", src_dir_fd=directory, dst_dir_fd=directory)
                finally:
                    try:
                        os.unlink(temporary, dir_fd=directory)
                    except FileNotFoundError:
                        pass
            finally:
                os.close(lock)


class RunMetrics:
    def __init__(self):
        self.record = None
        self.store = None

    @best_effort
    def start(self, registry, issue, options, role, *, run_id=None, context_id=None,
              loop_id=None, review_round=None, previous_run_id=None, implementation_context_id=None):
        self.store = MetricsStore(registry.path)
        self.record = dict(version=VERSION, run_id=run_id or str(uuid4()), task_id=issue.identifier,
                           role=role, agent=options.kind, model=options.model, mode=options.mode,
                           context_id=context_id, loop_id=loop_id, review_round=review_round,
                           previous_run_id=previous_run_id, implementation_context_id=implementation_context_id,
                           started_at=timestamp(), finished_at=None, wall_clock_seconds=None,
                           status="running", runtime_versions=dict(python=platform.python_version()))
        self.store.write(self.record)

    @best_effort
    def outcome(self, status, *, findings=None, context_id=None):
        if self.record is None:
            return
        if status in {"clean", "findings"}:
            self.record["review_outcome"] = status
            status = "completed"
        if findings is not None:
            self.record["findings_count"] = len(findings)
        if context_id is not None:
            self.record["context_id"] = context_id
        self.record["status"] = "blocked" if status == "human_decision" else status

    @best_effort
    def finish(self, error=None):
        if self.record is None:
            return
        if isinstance(error, (KeyboardInterrupt, SystemExit)):
            self.record["status"] = "interrupted"
        elif error is not None and self.record["status"] not in {"blocked", "timed_out"}:
            self.record["status"] = "failed"
        elif self.record["status"] == "running":
            self.record["status"] = "failed"
        # Interactive dispatch has no observed agent completion. All other
        # intervals end when the controller stops observing this pass, even if
        # the agent remains alive after a timeout or controller interruption.
        if self.record["status"] != "dispatched":
            finish = timestamp()
            self.record["finished_at"] = finish
            self.record["wall_clock_seconds"] = (
                datetime.fromisoformat(finish) - datetime.fromisoformat(self.record["started_at"])).total_seconds()
        self.store.write(self.record)


@contextmanager
def observe_run():
    metrics = RunMetrics()
    try:
        yield metrics
    except BaseException as error:
        metrics.finish(error)
        raise
    else:
        metrics.finish()


@best_effort
def observe_loop(env, state, *, recovered_result=None):
    """Observe routing and verified recovery; never feed metrics back into either."""
    latest = state["active_pass"] or (state["records"][-1] if state["records"] else None)
    if latest and latest.get("pass_id"):
        MetricsStore(env.registry.path).write(run_id=latest["pass_id"], loop_status=state["status"],
                                              recovered_result=recovered_result)
