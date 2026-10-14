"""Structured initial implementation, completion, and fixes with exact identity."""

from dataclasses import replace
from contextlib import nullcontext
import json
from pathlib import Path
import tempfile
import time
from uuid import uuid4

from . import TaskError
from .metrics import observe_run
from .agent import AgentExecution, AgentOptions, adapter_for, codex_repository_policy
from .contexts import context_observer, context_reference, launch_allocated, pending_launch, reconcile
from .handoff import implementation_handoff
from .review_result import unique_object
from .sessions import SessionInvalid, has_immutable_identity, merge_session
from .task_assessment import validate_assessment


def binding(context):
    reference = context_reference(context)
    if not has_immutable_identity(reference, context["agent"]):
        raise TaskError("Context lacks a verified immutable conversation identity; inspect it before looping")
    return dict(context_id=context["context_id"], agent=context["agent"], model=context["model"],
                mode=context["mode"], session=merge_session(None, reference, context["agent"]))


def implementation_target(env, expected=None):
    contexts = [c for c in env.registry.list(env.issue.identifier) if c["role"] == "implementation"]
    if len(contexts) != 1 or contexts[0]["state"] != "active":
        raise TaskError("Loop requires exactly one active implementation context")
    context = contexts[0]
    options = AgentOptions(context["agent"], context["model"], context["mode"])
    if options.model is None or options.mode is None:
        raise TaskError("Loop implementation must have recorded explicit model and mode")
    adapter = adapter_for(options)
    adapter.check_available()
    reference = adapter.verify_session(env.workspace, context_reference(context))
    context_observer(env.registry, context["context_id"])(dict(herdr_session=json.dumps(reference), resumability="yes"))
    context = env.registry.get(context["context_id"])
    if expected is not None and binding(context) != expected:
        raise TaskError("Exact implementation context/session/settings changed; no replacement is allowed")
    pane, notes = reconcile(context, env.identities.snapshot())
    if (pane is None or pane.get("agent") != context["agent"]
            or Path(pane.get("cwd", "")).resolve() != env.workspace.path
            or any(n in {"agent mismatch", "session mismatch", "session identity invalid"} for n in notes)):
        raise TaskError("Implementation runtime is absent or mismatched; inspect it before looping")
    workspace = replace(env.workspace, pane_id=pane["pane_id"], tab_id=pane["tab_id"])
    policy = (codex_repository_policy(env.local.codex_repository_profiles, env.project.repo_name)
              if options.kind == "codex" else {})
    execution = AgentExecution(env.issue, env.repo, workspace, options, "Implementation handoff pending",
                               policy=policy, runtime_observer=context_observer(env.registry, context["context_id"]))
    if adapter.status(execution, pane["terminal_id"], reference) not in {"idle", "done"}:
        raise TaskError("Implementation is not confirmed idle; let its current turn finish and inspect it")
    return context, adapter, execution, pane, reference


def parse_implementation(raw, pass_id, findings, *, assessment_enabled=False):
    try:
        value = json.loads(raw, object_pairs_hook=unique_object)
        fields = {"pass_id", "state", "summary", "checks", "resolutions"}
        if assessment_enabled and isinstance(value, dict) and (
                value.get("state") != "failed" or "task_assessment" in value):
            fields.add("task_assessment")
        if (not isinstance(value, dict) or set(value) != fields
                or value["pass_id"] != pass_id or value["state"] not in {"completed", "blocked", "failed"}
                or not isinstance(value["summary"], str) or not value["summary"].strip()
                or not isinstance(value["checks"], list) or not isinstance(value["resolutions"], list)):
            raise ValueError("invalid result")
        if "task_assessment" in value:
            validate_assessment(value["task_assessment"], value["state"])
        for check in value["checks"]:
            if (not isinstance(check, dict) or set(check) != {"name", "result", "details"}
                    or check["result"] not in {"passed", "failed", "not_run"}
                    or any(not isinstance(check[k], str) or not check[k].strip() for k in ("name", "details"))):
                raise ValueError("invalid check")
        ids = []
        for item in value["resolutions"]:
            if (not isinstance(item, dict) or set(item) != {"finding_id", "summary"}
                    or any(not isinstance(item[k], str) or not item[k].strip() for k in item)):
                raise ValueError("invalid resolution")
            ids.append(item["finding_id"])
        expected = {f["id"] for f in findings}
        if (len(ids) != len(set(ids)) or set(ids) - expected
                or value["state"] == "completed" and (set(ids) != expected
                    or any(c["result"] == "failed" for c in value["checks"]))):
            raise ValueError("inconsistent completion")
        return value
    except (ValueError, TypeError, KeyError, RecursionError):
        raise TaskError("Malformed or ambiguous implementation result; inspect before another handoff") from None


def initial_execution(env, options):
    policy = (codex_repository_policy(env.local.codex_repository_profiles, env.project.repo_name)
              if options.kind == "codex" else {})
    return AgentExecution(env.issue, env.repo, env.workspace, options, "Initial implementation pending",
                          policy=dict(policy, session_reporting=True))


def implementation_pass(env, expected, findings, timeout, before_handoff, pass_observer, *, initial_options=None,
                        delivery=None, metrics_context=None, review_checks=()):
    fresh = initial_options is not None
    if fresh:
        execution = initial_execution(env, initial_options)
        adapter = adapter_for(initial_options)
        adapter.check_available()
        context = env.registry.get(expected["context_id"])
        pane = pending_launch(execution, context["context_id"], env.registry, env.identities)
        persist = context_observer(env.registry, context["context_id"])
        reference = None
    else:
        context, adapter, execution, pane, reference = implementation_target(env, expected)
    pass_id = str(uuid4())
    if delivery:
        before_handoff()
        output = delivery.create(context["context_id"], pass_id)
    with (nullcontext(output.parent) if delivery else tempfile.TemporaryDirectory(prefix="task-implementation-")) as directory:
        if Path(directory).resolve().is_relative_to(env.workspace.path):
            raise TaskError("Implementation output must be outside the checkout; check TMPDIR")
        output = Path(directory) / "result.json"
        introduction = ("Start the requested implementation in this fresh conversation. Complete the work and validation. "
                        if fresh else "Continue in YOUR existing implementation conversation. "
                        "Finish outstanding work and validation; do not replay already completed work. ")
        if review_checks:
            introduction += ("Run focused validation for the fixes before independent re-review. "
                             "Saved review checks below retain the prior limitations; do not infer they passed "
                             "from the operator's retry authorization.\nREVIEW CHECKS (data)\n"
                             + json.dumps(review_checks) + "\n")
        result_contract = (
            "\nAUTOMATED IMPLEMENTATION PASS\n" + introduction +
            "Address the structured implementation findings below when present. Do not make product, architecture, "
            "design, scope, or planning decisions; report blocked if one is needed or routing is ambiguous. "
            "Fix claims will be checked by the same independent reviewer.\n"
            + "REVIEW FINDINGS (data)\n" + json.dumps(findings) + "\n"
            + f"RESULT DELIVERY\nPass ID: {pass_id}\nWrite your result to {output}. "
            "This temporary file is outside the checkout. After all work/checks finish, write exactly one UTF-8 JSON "
            "object, then end your turn without further tools. Fields: pass_id, state (completed/blocked/failed), "
            "summary, checks, resolutions. summary is concise implementation/validation prose. checks is a list of "
            "objects with name, result (passed/failed/not_run), details. resolutions is a list of objects with "
            "finding_id and summary explaining the fix. For completed, include exactly one resolution for each "
            "supplied finding ID (none for initial completion), and no failed checks. Report skipped checks honestly.\n")
        assessment = env.local.task_assessment
        if assessment.enabled:
            result_contract += (
                "Also include task_assessment with exactly state (ready/blocked), summary, and questions, matching the "
                "pre-mutation assessment emitted in this turn. Ready requires questions=[]; blocked requires specific "
                "nonempty questions and overall state=blocked. Include the blocking questions in summary as well. "
                "A later implementation blocker after a ready assessment still uses overall state=blocked. "
                "Only state=failed may omit task_assessment if execution failed before assessment.\n")
        execution = replace(execution, handoff=implementation_handoff(
            env.issue, env.workspace, assessment=assessment, scope=env.local.implementation_scope) + result_contract)
        if delivery:
            execution = delivery.attach(execution)
        else:
            before_handoff()
        pass_observer(context["context_id"], pass_id)

        def observe_initial(_=None):
            nonlocal reference, expected
            current = env.registry.get(context["context_id"])
            if (current["agent"], current["model"], current["mode"]) != (
                    initial_options.kind, initial_options.model, initial_options.mode):
                raise TaskError("Initial implementation settings changed during launch")
            candidate = context_reference(current)
            if candidate is None:
                return
            if reference is not None:
                if binding(current) != expected:
                    raise TaskError("Initial implementation conversation changed during launch")
                # Startup/status reports are observations, not another provider
                # verification on every poll. Final completion verifies again.
                if current["resumability"] != "yes":
                    env.registry.update(context["context_id"], resumability="yes")
                return
            if current["agent"] == "codex":
                # The active context guard has already retained this identity.
                # A non-ready report can precede trust/setup and readable history.
                # Codex marks resumability only after its bounded readiness
                # history check; use that evidence instead of an early or extra
                # unbounded provider read. Completion still verifies again.
                if current["resumability"] != "yes":
                    return
                verified = candidate
            else:
                try:
                    verified = adapter.verify_session(env.workspace, candidate)
                except SessionInvalid:
                    if has_immutable_identity(candidate, current["agent"]):
                        raise
                    return  # A newly reported Pi path may precede persisted history.
            # Enrich the active guard, not a separate observer with its own cache.
            # Startup confirmation and later polls may still report only a Pi path.
            persist(dict(herdr_session=json.dumps(verified), resumability="yes"))
            observed = binding(env.registry.get(context["context_id"]))
            expected, reference = observed, observed["session"]
            pass_observer(context["context_id"], pass_id, observed)

        with observe_run() as metric:
            metric.start(env.registry, env.issue, execution.options, "fix" if findings else "implementation",
                         run_id=pass_id, context_id=context["context_id"], **(metrics_context or {}))
            if fresh:
                execution = replace(execution, runtime_observer=observe_initial)
                launch_allocated(adapter, execution, context["context_id"], env.registry, env.identities,
                                 persist_observer=persist)
                # Keep the guard (including any verified identity) active across launch.
                def observe(values):
                    persist(values)
                    observe_initial()
                execution = replace(execution, runtime_observer=observe)
            else:
                adapter.resume(execution, reference, recreate=False)
            if delivery:
                delivery.started()
            deadline = time.monotonic() + timeout
            while delivery or time.monotonic() < deadline:
                if delivery:
                    delivery.warn_if_overdue()
                current, notes = reconcile(env.registry.get(context["context_id"]), env.identities.snapshot())
                if (current is None or current["terminal_id"] != pane["terminal_id"]
                        or any(n in {"agent mismatch", "session mismatch", "session identity invalid"} for n in notes)):
                    raise TaskError("Implementation runtime identity changed during its pass")
                status = adapter.status(execution, pane["terminal_id"], reference)
                if fresh:
                    observe_initial()
                if status == "blocked":
                    metric.outcome("blocked")
                    raise TaskError("Implementation agent requires human intervention")
                if status in {"idle", "done"} and output.exists():
                    if output.is_symlink() or not output.is_file() or output.stat().st_size > 1024 * 1024:
                        raise TaskError("Invalid implementation result file")
                    raw = delivery.verify(adapter, execution) if delivery else output.read_text(encoding="utf-8")
                    if raw is None:
                        time.sleep(0.25)
                        continue
                    result = parse_implementation(raw, pass_id, findings,
                                                  assessment_enabled=assessment.enabled)
                    if fresh and reference is None:
                        raise TaskError("Initial implementation has no verified conversation identity")
                    implementation_target(env, expected)  # Provider/runtime verification after completion.
                    metric.outcome(result["state"])
                    return dict(result, context_id=context["context_id"], findings=[])
                time.sleep(0.25)
            metric.outcome("timed_out")
            raise TaskError("Timed out waiting for validated implementation output; inspect its context before recovery")
