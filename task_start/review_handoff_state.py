"""One bounded standalone-review handoff, never a transcript or another controller.

The publication lock owns writes. A pending record is deliberately not recoverable:
only the original standalone reviewer controller can attest its validated result.
Consumption is checkpoint-first, so a crash cannot cause a second adoption.
"""

from dataclasses import replace
import json
from uuid import UUID

from . import TaskError
from .agent import AgentExecution, AgentOptions
from .contexts import context_reference
from .implementation_pass import binding, implementation_target
from .loop_state import LoopStore
from .pass_delivery import LIMIT, digest, object_digest, read_private
from .publication_state import PublicationStore
from .review_result import ReviewResult, parse_verdict, unique_object
from .review_state import snapshot
from .sessions import merge_session
from .workspace import Git


def standalone_current(review, loop):
    """Consumption/checkpoint provenance orders reviews, never context labels or time."""
    if not review:
        return False
    if not loop:
        return review['consumed'] is None
    result = review['result']
    matches = [r for r in loop['records'] if result and r['pass_id'] == result['pass_id']]
    if matches:
        if (len(matches) != 1 or matches[0]['phase'] not in {'review', 'rereview'}
                or any(matches[0][k] != result[k] for k in ('state', 'summary', 'context_id', 'findings', 'checks'))
                or review['consumed'] not in {None, loop['run_id']}
                or review['origin'] is not None and review['origin']['run_id'] != loop['run_id']):
            raise TaskError('Standalone review conflicts with its checkpointed result')
        return False  # Includes checkpoint-first adoption before consumption was saved.
    if review['consumed'] == loop['run_id']:
        raise TaskError('Consumed review is missing from its owning checkpoint')
    return review['consumed'] is None and (review['origin'] or {}).get('run_id') == loop['run_id']


def verify_review_evidence(accepted, loop, review):
    """Reconcile an acceptance with any retained outcomes for that exact pass."""
    def clean(result):
        return (result['state'] == 'clean' and result['context_id'] == accepted['context_id']
                and not result['findings'] and not any(c['result'] == 'failed' for c in result['checks']))

    if loop:
        for record in loop['records']:
            if record['pass_id'] == accepted['pass_id']:
                reviewer = loop['reviewer']
                if (record['phase'] not in {'review', 'rereview'} or not clean(record) or not reviewer
                        or reviewer['context_id'] != accepted['context_id']
                        or reviewer['session'] != accepted['session']
                        or accepted['pass_kind'] != ('resumed' if record['phase'] == 'rereview' else 'fresh')
                        or loop['snapshot'] != accepted['review_state']
                        or dict(kind=reviewer['agent'], model=reviewer['model'], mode=reviewer['mode']) != accepted['execution']):
                    raise TaskError('Acceptance conflicts with its saved loop review')
    if review and review['result'] and review['result']['pass_id'] == accepted['pass_id']:
        result, reviewer = review['result'], review['reviewer']
        if (not clean(result) or result['invalidated'] or not reviewer
                or any(result[k] != accepted[k] for k in ('context_id', 'pass_kind', 'execution', 'review_state'))
                or review['snapshot'] != accepted['review_state']
                or result['post_fingerprint'] != accepted['review_state']['fingerprint']
                or reviewer['context_id'] != accepted['context_id'] or reviewer['session'] != accepted['session']
                or dict(kind=reviewer['agent'], model=reviewer['model'], mode=reviewer['mode']) != accepted['execution']):
            raise TaskError('Acceptance conflicts with its saved standalone review')


def task_binding(env):
    return dict(issue=env.issue.identifier, repository=str(env.repo), worktree=str(env.workspace.path),
                branch=env.workspace.branch, workspace_id=env.workspace.workspace_id, endpoint=env.endpoint,
                base_branch=env.project.base_branch)


def requirements(env):
    return object_digest(dict(title=env.issue.title, description=env.issue.description, project=env.issue.project))


def contexts_digest(env, *, excluding=()):
    return object_digest(sorted(c["context_id"] for c in
        env.registry.list(env.issue.identifier, include_retired=True) if c["context_id"] not in excluding))


def implementation_identity(env):
    """Optional adoption proof must not make standalone review depend on I1 availability."""
    from .agent import adapter_for
    candidates = [c for c in env.registry.list(env.issue.identifier) if c["role"] == "implementation"]
    if len(candidates) != 1:
        return None
    context = candidates[0]
    try:
        reference = adapter_for(AgentOptions(context["agent"], context["model"], context["mode"])).verify_session(
            env.workspace, context_reference(context))
        return binding(dict(context, herdr_session=json.dumps(reference)))
    except (TaskError, ValueError, OSError):
        return None


def origin(state):
    """Only an initial/completion implementation boundary can precede standalone R1."""
    claim = state["active_pass"] or (state["records"][-1] if state["records"] else {})
    if (state["reviewer"] is not None or state["review_count"] or
            claim.get("phase") not in {"initial_implementation", "implementation"} or
            not claim.get("pass_id") or
            not state["active_pass"] and (claim.get("state") != "completed" or state["next_phase"] != "review")):
        return None
    return dict(run_id=state["run_id"], pass_id=claim["pass_id"], phase=claim["phase"],
                snapshot=state["snapshot"], delivery=object_digest(state.get("delivery")))


class ReviewHandoffStore:
    def __init__(self, worktree):
        self.storage = PublicationStore(worktree)
        self.path = self.storage.path = self.storage.directory / "agentic-workflows-review-handoff.json"

    def read(self):
        if not self.path.exists() and not self.path.is_symlink():
            return None
        try:
            value = json.loads(read_private(self.path), object_pairs_hook=unique_object)
            fields = {"version", "binding", "requirements", "slice", "snapshot", "implementation", "origin",
                      "contexts_before", "contexts_after", "reviewer", "receipt", "result", "publication", "consumed"}
            if set(value) != fields or value["version"] != 1:
                raise ValueError("invalid record")
            if value["consumed"] is not None:
                UUID(value["consumed"])
            if value["result"] is not None:
                result = ReviewResult(**value["result"])
                UUID(result.pass_id)
                if result.version != 1 or set(result.execution) != {"kind", "model", "mode"}:
                    raise ValueError("invalid review result")
            if value["receipt"] is not None:
                receipt = value["receipt"]
                UUID(receipt["message_id"])
                if (set(receipt) != {"message_id", "prompt_sha256"} or len(receipt["prompt_sha256"]) != 64
                        or any(c not in "0123456789abcdef" for c in receipt["prompt_sha256"])):
                    raise ValueError("invalid review receipt")
            return value
        except (ValueError, TypeError, KeyError, UnicodeError, AttributeError, RecursionError):
            raise TaskError("Malformed standalone review handoff; preserve private evidence and inspect it") from None

    def write(self, value):
        if len(json.dumps(value).encode()) > LIMIT:
            raise TaskError("Standalone review handoff exceeds the bounded private record size")
        self.storage.write(value)

    def begin(self, env, before):
        loop = LoopStore(env.workspace.path)
        previous = loop.read()[0] if loop.path.exists() else None
        value = dict(version=1, binding=task_binding(env), requirements=requirements(env),
            slice=env.workspace.slice, snapshot=before, implementation=implementation_identity(env),
            origin=origin(previous) if previous else None, contexts_before=contexts_digest(env),
            contexts_after=None, reviewer=None, receipt=None, result=None, publication=None, consumed=None)
        # A checkpoint that cannot supply this origin remains authoritative.
        if previous is not None and value["origin"] is None:
            value["origin"] = dict(run_id=previous["run_id"], unsupported=True)
        self.write(value)
        return value

    def claim(self, env, value, options, context_id, reference, message_id, prompt):
        if value["receipt"] is not None:
            raise TaskError("Standalone review prompt already claimed; it cannot be replayed")
        context = env.registry.get(context_id)
        reference = merge_session(context_reference(context), reference, options.kind)
        value.update(reviewer=dict(context_id=context_id, agent=options.kind,
            model=options.model, mode=options.mode, session=reference),
            receipt=dict(message_id=message_id, prompt_sha256=digest(prompt.encode())))
        self.write(value)

    def finish(self, env, value, result, publication):
        if value["reviewer"] and result.context_id == value["reviewer"]["context_id"]:
            try:
                verified = binding(env.registry.get(result.context_id))
                reference = merge_session(value["reviewer"]["session"], verified["session"], verified["agent"])
                if dict(value["reviewer"], session=reference) == verified:
                    value["reviewer"] = verified
            except (TaskError, ValueError):
                pass  # Preserve inconclusive proof; ordinary review still reports its outcome.
        value.update(result=result.as_dict(), publication=object_digest(publication.read()),
                     contexts_after=contexts_digest(env))
        self.write(value)

    def consume(self, state):
        """Repair bookkeeping from an exact collected result; never route it again."""
        value = self.read()
        if value and value["result"] and any(r["pass_id"] == value["result"]["pass_id"] for r in state["records"]):
            result = value["result"]
            matches = [r for r in state["records"] if r["pass_id"] == result["pass_id"]]
            if (len(matches) != 1 or state["reviewer"] != value["reviewer"] or
                    any(matches[0][k] != result[k] for k in
                        ("state", "summary", "context_id", "findings", "checks"))):
                raise TaskError("Consumed standalone review conflicts with its checkpointed result")
            if value["consumed"] not in {None, state["run_id"]}:
                raise TaskError("Standalone review was already consumed by another loop")
            if value["consumed"] is None:
                value["consumed"] = state["run_id"]
                self.write(value)
            return True
        return False


def eligible_review(env, publication, state=None, *, intervening=False):
    """Verify the exact completed review without delivery or inference from idleness."""
    from .agent import adapter_for
    from .review import resolve_reviewer, resolve_review_workspace
    value = ReviewHandoffStore(env.workspace.path).read()
    if value is None or value["consumed"] is not None:
        return None
    try:
        result = value["result"]
        if result is None or value["reviewer"] is None or value["receipt"] is None:
            raise TaskError("Standalone review has no finished, attributable handoff; inspect its original context")
        if (result["pass_kind"] != "fresh" or result["invalidated"] or
                result["review_state"] != value["snapshot"] or
                result["post_fingerprint"] != value["snapshot"]["fingerprint"] or
                value["binding"] != task_binding(env) or value["requirements"] != requirements(env) or
                value["slice"] != env.workspace.slice or
                snapshot(env.workspace.path, env.base, env.workspace.branch).as_dict() != value["snapshot"]):
            raise TaskError("Standalone review requirements, scope or Git/base evidence changed; adoption refused")
        if state is None:
            if value["origin"] is not None:
                raise TaskError("Standalone review belongs to a saved loop boundary; its checkpoint is required")
        else:
            expected = value["origin"]
            current = origin(state)
            if (not expected or not current or any(expected.get(k) != current[k] for k in
                    ("run_id", "pass_id", "phase")) or state["binding"] != value["binding"] or
                    state["requirements"] != value["requirements"] or
                    state["active_pass"] is not None and expected != current):
                raise TaskError("Standalone review does not match the authoritative implementation checkpoint")
        if (value["contexts_after"] != contexts_digest(env) or
                value["contexts_before"] != contexts_digest(env, excluding=(result["context_id"],))):
            raise TaskError("Standalone review context history is ambiguous or contains unrelated executions")
        if intervening and (not state["delivery"] or
                state["delivery"]["contexts_sha256"] != value["contexts_before"]):
            raise TaskError("Intervening reviewer does not account for the exact original context set")
        context = env.registry.get(value["implementation"]["context_id"])
        if binding(context) != value["implementation"]:
            raise TaskError("Original implementation session/settings changed since standalone review")
        reviewer, pane = resolve_reviewer(result["context_id"], env.issue, env.workspace, env.repo,
                                          env.endpoint, env.registry, env.identities)
        if (binding(reviewer) != value["reviewer"] or pane is None or
                result["execution"] != dict(kind=reviewer["agent"], model=reviewer["model"], mode=reviewer["mode"])):
            raise TaskError("Standalone reviewer session/settings or runtime changed")
        options = AgentOptions(**result["execution"])
        adapter = adapter_for(options)
        adapter.check_available()
        reference = adapter.verify_review_session(env.workspace, value["reviewer"]["session"])
        execution = AgentExecution(env.issue, env.repo,
            replace(env.workspace, pane_id=pane["pane_id"], tab_id=pane["tab_id"]), options,
            "Observe completed standalone review only", purpose="review", policy=dict(read_only=True))
        if (reference != value["reviewer"]["session"] or
                adapter.review_status(execution, pane["terminal_id"], reference) not in {"idle", "done"} or
                not adapter.observe_delivery(execution, reference, value["receipt"], None)):
            raise TaskError("Standalone reviewer has no exclusive completed provider receipt")
        try:
            verdict = parse_verdict(json.dumps({k: result[k] for k in
                ("pass_id", "state", "summary", "findings", "checks")}), result["pass_id"], routing=True)
        except TaskError:
            raise TaskError("Standalone review has no valid routable verdict/unique finding IDs; inspect it before retrying") from None
        saved = publication.read()
        if object_digest(saved) != value["publication"] or saved["intent"] is not None:
            raise TaskError("Standalone review publication authority changed; adoption refused")
        if result["state"] == "clean":
            acceptance = saved["acceptance"]
            if (not acceptance or acceptance["verdict"] != "clean" or
                    any(acceptance[k] != v for k, v in value["binding"].items()) or
                    any(acceptance[k] != result[k] for k in ("context_id", "pass_id", "pass_kind", "review_state", "execution")) or
                    acceptance["session"] != reference):
                raise TaskError("Standalone clean review lacks its exact valid acceptance")
        elif saved["acceptance"] is not None:
            raise TaskError("Standalone findings conflict with clean publication acceptance")
        # Provider history observation can be slow. Re-resolve the live mappings,
        # health and idle runtimes as well as the immutable bindings before adoption.
        workspace, _, base, endpoint = resolve_review_workspace(
            env.issue, env.project, env.repo, env.registry, env.identities,
            recovery_implementation=context["context_id"] if intervening else None)
        if (any(getattr(workspace, k) != getattr(env.workspace, k) for k in
                ("path", "branch", "workspace_id", "slice")) or base != env.base or endpoint != env.endpoint):
            raise TaskError("Standalone review workspace mapping changed during verification")
        if not intervening:
            implementation_target(env, value["implementation"])
        # Intervening mode only attributes the extra context during DEV-92 proof
        # verification; that caller verifies the original I1 runtime and receipt.
        # Actual adoption always checks active/idle I1 above after recovery.
        reviewer, pane = resolve_reviewer(result["context_id"], env.issue, env.workspace, env.repo,
                                          env.endpoint, env.registry, env.identities)
        if pane is None or binding(reviewer) != value["reviewer"]:
            raise TaskError("Standalone reviewer runtime changed during verification")
        execution = replace(execution, workspace=replace(env.workspace, pane_id=pane["pane_id"], tab_id=pane["tab_id"]))
        if (adapter.verify_review_session(env.workspace, value["reviewer"]["session"]) != reference or
                adapter.review_status(execution, pane["terminal_id"], reference) not in {"idle", "done"}):
            raise TaskError("Standalone reviewer is no longer confirmed idle in its original session")
        git = Git(env.repo)
        if (snapshot(env.workspace.path, env.base, env.workspace.branch).as_dict() != value["snapshot"] or
                contexts_digest(env) != value["contexts_after"] or
                git.resolve_scope(env.workspace.path, env.workspace.branch, env.issue.identifier, None) != value["slice"] or
                git.command("rev-parse", "--verify", f"refs/heads/{env.project.base_branch}^{{commit}}").strip() != env.base):
            raise TaskError("Standalone review evidence changed during verification")
        # Session verification can also be slow. Check health on the same final
        # rows as the bindings, with no further provider calls after these reads.
        context = env.registry.get(context["context_id"])
        reviewer = env.registry.get(reviewer["context_id"])
        implementation_states = {"active", "launching", "uncertain"} if intervening else {"active"}
        if (context["retired_at"] or context["state"] not in implementation_states or
                not intervening and context["resumability"] != "yes" or
                reviewer["retired_at"] or reviewer["state"] != "active" or reviewer["resumability"] != "yes"):
            raise TaskError("Standalone review context health changed during verification; adoption refused")
        if binding(context) != value["implementation"] or binding(reviewer) != value["reviewer"]:
            raise TaskError("Standalone review evidence changed during verification")
        return value, dict(verdict, context_id=result["context_id"], resolutions=[])
    except (KeyError, TypeError, ValueError, AttributeError):
        raise TaskError("Standalone review lacks exact routable provenance; no findings were reconstructed") from None
