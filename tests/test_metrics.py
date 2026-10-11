"""Experimental observations must stay bounded, private and non-authoritative."""

from datetime import datetime
import fcntl
import json
import os
from pathlib import Path
import stat
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from task_start.agent import AgentOptions
from task_start.metrics import MAX_BYTES, MAX_RUNS, MetricsStore, observe_loop, observe_run, validate


class MetricsTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.registry = SimpleNamespace(path=self.root / "contexts.sqlite3")
        self.issue = SimpleNamespace(identifier="DEV-25", description="private task prose")
        self.options = AgentOptions("pi", "selected-model", "low")
        self.store = MetricsStore(self.registry.path)
        self.path = self.store.directory / "runs.json"

    def record(self, run_id="run", *, role="implementation", status="completed", **kwargs):
        with observe_run() as metric:
            metric.start(self.registry, self.issue, self.options, role, run_id=run_id, **kwargs)
            metric.outcome(status)
        return self.store.read()

    def test_versioned_round_trip_timestamps_and_unknown_values(self):
        self.options = AgentOptions("codex")
        times = ["2026-10-10T23:59:59.125000+00:00", "2026-10-11T00:00:01.625000+00:00"]
        with patch("task_start.metrics.timestamp", side_effect=times):
            records = self.record(context_id="DEV-25-I1")
        self.assertEqual(json.loads(self.path.read_text())["version"], 1)
        record, = records
        self.assertEqual(record["version"], 1)
        self.assertEqual((record["started_at"], record["finished_at"], record["wall_clock_seconds"]), (*times, 2.5))
        self.assertIsNotNone(datetime.fromisoformat(record["started_at"]).utcoffset())
        self.assertEqual((record["task_id"], record["context_id"], record["agent"]), ("DEV-25", "DEV-25-I1", "codex"))
        for key in ("model", "mode", "loop_id", "review_round", "previous_run_id", "implementation_context_id"):
            self.assertIsNone(record[key])
        self.assertTrue(record["runtime_versions"]["python"])
        for key in ("tokens", "cost", "quota", "active_model_seconds", "summary", "description", "handoff", "session"):
            self.assertNotIn(key, record)
        self.assertNotIn(self.issue.description, self.path.read_text())
        self.assertFalse(self.registry.path.exists())

    def test_roles_results_and_multiple_runs_keep_relationships(self):
        for i, (role, status) in enumerate((("implementation", "completed"), ("fix", "failed"),
                ("review", "findings"), ("re-review", "clean"), ("integration", "human_decision"),
                ("research", "blocked"))):
            self.record(str(i), role=role, status=status, loop_id="loop", review_round=2,
                        previous_run_id=str(i-1) if i else None, implementation_context_id="DEV-25-I1")
        records = self.store.read()
        self.assertEqual([r["run_id"] for r in records], list(map(str, range(6))))
        self.assertEqual([r["status"] for r in records], ["completed", "failed", "completed", "completed", "blocked", "blocked"])
        self.assertEqual(records[2]["review_outcome"], "findings")
        self.assertEqual(records[3]["review_outcome"], "clean")
        self.assertEqual(records[3]["previous_run_id"], "2")
        self.assertTrue(all(r["loop_id"] == "loop" and r["review_round"] == 2 for r in records))
        self.assertTrue(all(r["model"] == "selected-model" and r["mode"] == "low" for r in records))

    def test_running_dispatch_and_findings_do_not_invent_completion(self):
        with observe_run() as metric:
            metric.start(self.registry, self.issue, self.options, "implementation", run_id="interactive")
            running, = self.store.read()
            self.assertEqual(running["status"], "running")
            self.assertIsNone(running["finished_at"])
            metric.outcome("dispatched", context_id="DEV-25-I1")
        dispatched, = self.store.read()
        self.assertEqual(dispatched["status"], "dispatched")
        self.assertEqual(dispatched["context_id"], "DEV-25-I1")
        self.assertIsNone(dispatched["finished_at"])
        self.assertIsNone(dispatched["wall_clock_seconds"])
        with observe_run() as metric:
            metric.start(self.registry, self.issue, self.options, "review")
            metric.outcome("findings", findings=[{"explanation": "private finding text"}])
        review = self.store.read()[-1]
        self.assertEqual(review["findings_count"], 1)
        self.assertNotIn("private finding text", self.path.read_text())

    def test_exceptions_interrupts_and_timeouts_preserve_original_errors(self):
        for i, error in enumerate((RuntimeError("original"), KeyboardInterrupt(), SystemExit(1))):
            with self.assertRaises(type(error)) as caught:
                with observe_run() as metric:
                    metric.start(self.registry, self.issue, self.options, "implementation", run_id=str(i))
                    raise error
            self.assertIs(caught.exception, error)
        for status in ("blocked", "timed_out"):
            with self.assertRaises(RuntimeError):
                with observe_run() as metric:
                    metric.start(self.registry, self.issue, self.options, "review", run_id=status)
                    metric.outcome(status)
                    raise RuntimeError("controller refusal")
        self.assertEqual([r["status"] for r in self.store.read()],
                         ["failed", "interrupted", "interrupted", "blocked", "timed_out"])
        self.assertTrue(all(r["finished_at"] is not None for r in self.store.read()))

    def test_writer_and_clock_failures_do_not_change_results_or_errors(self):
        for target in ("task_start.metrics.MetricsStore.write", "task_start.metrics.timestamp"):
            with patch(target, side_effect=OSError("unavailable")):
                with observe_run() as metric:
                    metric.start(self.registry, self.issue, self.options, "review")
                    metric.outcome("clean", findings=[])
                with self.assertRaisesRegex(RuntimeError, "workflow failure"):
                    with observe_run() as metric:
                        metric.start(self.registry, self.issue, self.options, "fix")
                        raise RuntimeError("workflow failure")

    def test_loop_observation_tolerates_pause_before_environment_preparation(self):
        for records in ([], [dict(pass_id="prior-pass")]):
            observe_loop(None, dict(active_pass=None, records=records, status="paused"))
        self.assertFalse(self.store.directory.exists())

    def test_corrupt_future_and_oversized_storage_is_ignored_and_preserved(self):
        self.record()
        for raw in (b"broken", b'{"version":2,"runs":[]}', b"x" * (MAX_BYTES + 1),
                    b'{"version":1,"runs":[{"version":2}]}'):
            self.path.write_bytes(raw)
            self.assertIsNone(self.record("later"))
            self.assertEqual(self.path.read_bytes(), raw)

    def test_required_fields_cannot_be_missing_even_when_nullable(self):
        template, = self.record()
        for key in template:
            with self.subTest(field=key):
                malformed = dict(template)
                del malformed[key]
                with self.assertRaises(ValueError):
                    validate(malformed)

    def test_invalid_field_values_are_rejected_on_read_and_write_without_replacing_storage(self):
        template, = self.record()
        original = self.path.read_bytes()
        cases = {
            "version": [True, 1.0, 2, None],
            "run_id": [None, "", " ", 25], "task_id": [None, "", " ", []],
            "role": [None, [], "unknown"], "status": [None, {}, "clean"],
            "agent": [False, "", []], "model": [1, ""], "mode": [False, {}],
            "context_id": [1, ""], "implementation_context_id": [[], ""],
            "loop_id": [True, ""], "previous_run_id": [{}, ""],
            "review_round": [True, 0, -1, 1.5, "1"],
            "started_at": [None, 0, "", "2026-10-11", "2026-10-11T00:00:00", "2026-02-30T00:00:00Z"],
            "finished_at": [None, False, "yesterday", "2026-10-11T00:00:00"],
            "wall_clock_seconds": [None, False, "1", [], 999999],
            "runtime_versions": [None, [], {"python": 3}, {"python": None}, {"": "3.12"}],
            "review_outcome": [False, "unknown", []],
            "findings_count": [True, -1, 0.5, "1"],
            "loop_status": [[], "unknown", False],
            "result_observed_at": [None, 1, "2026-10-11", "2026-10-11T00:00:00"],
            "summary": ["Unsupported prose"],
        }
        for key, values in cases.items():
            for value in values:
                with self.subTest(field=key, value=value):
                    malformed = dict(template, **{key: value})
                    with self.assertRaises(ValueError):
                        validate(malformed)
                    self.path.write_bytes(original)
                    self.store.write(malformed)
                    self.assertEqual(self.path.read_bytes(), original)
                    # Syntactically valid JSON is still corrupt metrics state.
                    raw = json.dumps(dict(version=1, runs=[malformed]), allow_nan=False).encode()
                    self.path.write_bytes(raw)
                    self.assertIsNone(self.store.read())
                    self.assertIsNone(self.record("later"))
                    self.assertEqual(self.path.read_bytes(), raw)

    def test_nonfinite_durations_and_noninteger_snapshot_versions_are_rejected(self):
        template, = self.record()
        for duration in (float("nan"), float("inf"), -float("inf")):
            with self.subTest(duration=duration), self.assertRaises(ValueError):
                validate(dict(template, wall_clock_seconds=duration))
        for version in (True, 1.0, None):
            raw = json.dumps(dict(version=version, runs=[template])).encode()
            self.path.write_bytes(raw)
            self.assertIsNone(self.record("later"))
            self.assertEqual(self.path.read_bytes(), raw)

    def test_nullable_fields_and_unknown_intervals_remain_valid(self):
        template, = self.record()
        unknown = dict(template, agent=None, model=None, mode=None, runtime_versions={},
                       finished_at=None, wall_clock_seconds=None, review_outcome=None,
                       findings_count=None, loop_status=None)
        self.store.write(unknown)
        self.assertEqual(self.store.read(), [unknown])

    def test_recovery_does_not_reconstruct_missing_runs_or_change_other_passes(self):
        original, = self.record()
        state = dict(active_pass=None, records=[dict(pass_id="missing")], status="ready")
        observe_loop(SimpleNamespace(registry=self.registry), state,
                     recovered_result=dict(pass_id="missing", state="completed", findings=[]))
        self.assertEqual(self.store.read(), [original])
        state["records"] = [dict(pass_id="run")]
        observe_loop(SimpleNamespace(registry=self.registry), state,
                     recovered_result=dict(pass_id="different", state="completed", findings=[]))
        self.assertEqual(self.store.read(), [original])

    def test_retention_updates_in_place_and_bounds_crash_scratch_file(self):
        template, = self.record()
        for i in range(MAX_RUNS + 2):
            self.store.write(dict(template, run_id=str(i)))
        records = self.store.read()
        self.assertEqual(len(records), MAX_RUNS)
        self.assertEqual(records[0]["run_id"], "2")
        self.assertEqual(records[-1]["run_id"], str(MAX_RUNS + 1))
        pending = self.store.directory / "pending.json"
        pending.write_bytes(b"interrupted previous write")
        pending.chmod(0o600)
        self.store.write(dict(template, run_id="2", status="blocked"))
        self.assertEqual(len(self.store.read()), MAX_RUNS)
        self.assertEqual(self.store.read()[0]["status"], "blocked")
        self.assertFalse(pending.exists())
        self.assertLessEqual(self.path.stat().st_size, MAX_BYTES)
        self.assertEqual({p.name for p in self.store.directory.iterdir()}, {"runs.json", "writer.lock"})

    def test_private_permissions_and_lock_contention_skip_observations(self):
        self.record()
        self.assertEqual(stat.S_IMODE(self.store.directory.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        with (self.store.directory / "writer.lock").open("rb") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.record("contended")
        self.assertEqual([r["run_id"] for r in self.store.read()], ["run"])
        self.path.chmod(0o644)
        original = self.path.read_bytes()
        self.record("public")
        self.assertEqual(self.path.read_bytes(), original)
        self.assertIsNone(self.store.read())

    def test_atomic_failure_keeps_prior_snapshot_and_removes_scratch(self):
        self.record()
        original = self.path.read_bytes()
        with patch("task_start.metrics.os.replace", side_effect=OSError("full disk")):
            self.record("lost")
        self.assertEqual(self.path.read_bytes(), original)
        self.assertFalse((self.store.directory / "pending.json").exists())

    def test_symlinks_hardlinks_and_special_files_cannot_redirect_storage(self):
        self.record()
        external = self.root / "external"
        external.write_text("untouched")
        external.chmod(0o600)
        for name in ("runs.json", "pending.json", "writer.lock"):
            path = self.store.directory / name
            original = path.read_bytes() if path.exists() else None
            if path.exists():
                path.unlink()
            for kind in ("symlink", "hardlink", "fifo"):
                if kind == "symlink":
                    path.symlink_to(external)
                elif kind == "hardlink":
                    os.link(external, path)
                else:
                    os.mkfifo(path, 0o600)
                self.record("unsafe")
                self.assertEqual(external.read_text(), "untouched")
                # A failed scratch write may remove its own directory entry.
                if path.exists() or path.is_symlink():
                    path.unlink()
            if original is not None:
                path.write_bytes(original)
                path.chmod(0o600)

    def test_clock_adjustment_is_not_replaced_with_active_or_monotonic_time(self):
        with patch("task_start.metrics.timestamp", side_effect=[
                "2026-10-10T10:00:01+00:00", "2026-10-10T10:00:00+00:00"]):
            record, = self.record()
        self.assertEqual(record["wall_clock_seconds"], -1)


if __name__ == "__main__":
    unittest.main()
