"""Reviewed publication: preflight -> metadata -> commit -> push -> PR -> report."""

from contextlib import contextmanager
from copy import deepcopy
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
from urllib.parse import urlsplit

from . import TaskError
from .command import command_name
from .config import load_local, load_projects, repository_path, resolve_project
from .contexts import ContextRegistry, HerdrContexts, context_reference
from .github import api_credential, publication_pull, publish_pull, pull_requests, repository_name
from .linear import Linear
from .loop_state import LoopStore
from .publication_state import PublicationStore, verify_publication_history
from .integration_state import check_integration
from .publication_rebase import continue_rebase, integration_plan, validate_commits, validate_record
from .review import resolve_review_workspace
from .review_handoff_state import ReviewHandoffStore, standalone_current, verify_review_evidence
from .review_result import publication_fingerprint, publication_metadata
from .review_state import digest, snapshot, tree_content
from .sessions import merge_session
from .workspace import Git, run


CHANGE_TYPES = {"Feature": "feat", "Bug": "fix", "Chore": "chore", "Docs": "docs", "Refactor": "refactor"}


def change_type(issue):
    types = {CHANGE_TYPES[label] for label in issue.labels if label in CHANGE_TYPES}
    if len(types) != 1:
        raise TaskError("Publishing requires exactly one canonical Linear category label: Feature, Bug, Chore, Docs, or Refactor")
    return types.pop()


def tracking_url(issue):
    try:
        url = urlsplit(issue.url)
    except ValueError:
        raise TaskError("Linear supplied an invalid tracking URL; inspect the issue identity") from None
    match = re.fullmatch(r"/([A-Za-z0-9_-]+)/issue/" + re.escape(issue.identifier) + r"(?:/[^/]+)?", url.path)
    if (url.scheme != "https" or url.netloc != "linear.app" or not match or url.query or url.fragment):
        raise TaskError("Linear did not supply an unambiguous issue URL; fix its tracking identity before publishing")
    return f"https://linear.app/{match[1]}/issue/{issue.identifier}"


def native_git(path, *args, env=None, refs=False):
    """Credential/signing tools inherit stdin/stderr and the controlling terminal.

    Only ref/object-ID protocol stdout is read. No stderr or prompt input is
    captured, included in exceptions, or written to workflow artifacts.
    """
    try:
        result = subprocess.run(["git", "-C", str(path), *args], env=env,
                                stdout=subprocess.PIPE if refs else None, timeout=300, check=False)
    except (OSError, subprocess.TimeoutExpired):
        raise TaskError(f"Git publishing operation could not complete; check terminal authentication/signing and rerun {command_name()} pr") from None
    if result.returncode:
        raise TaskError(f"Git publishing operation failed; resolve the native authentication/signing or Git error above, then rerun {command_name()} pr")
    return os.fsdecode(result.stdout) if refs else None


def remote_identity(git, base, branch):
    remote = git.command("config", "--default", "", "--get", f"branch.{base}.remote").strip()
    upstream = git.command("config", "--default", "", "--get", f"branch.{base}.merge").strip()
    if not remote or remote == "." or remote.startswith("-") or upstream != f"refs/heads/{base}":
        raise TaskError("Publishing base must track a same-named branch on one remote")
    url = git.history_remote(base, branch)
    fetch = git.command("remote", "get-url", "--all", remote).splitlines()
    push = git.command("remote", "get-url", "--push", "--all", remote).splitlines()
    repository = repository_name(url)
    push_repository = repository_name(push[0]) if len(push) == 1 else None
    if (repository is None or len(fetch) != 1 or len(push) != 1
            or push_repository is None or push_repository.casefold() != repository.casefold()):
        raise TaskError("Publishing requires one fetch/push destination in the same github.com repository")
    config_keys = git.command("config", "--null", "--name-only", "--list").split("\0")
    if (git.command("config", "--type=bool", "--default", "false", "--get", f"remote.{remote}.mirror").strip() != "false"
            or f"remotes.{remote}".casefold() in {key.casefold() for key in config_keys}):
        raise TaskError("Publishing requires one non-mirror remote, not a remote group; inspect Git configuration")
    return dict(remote=remote, repository=repository)


def same_remote_identity(actual, expected):
    return (actual["remote"] == expected["remote"] and isinstance(actual["repository"], str)
            and actual["repository"].casefold() == expected["repository"].casefold())


def verify_remote_identity(git, base, branch, expected):
    # Resolve with precisely the checkout/configuration used by transport. In
    # particular, worktree config and conditional includes may differ from main.
    if not same_remote_identity(remote_identity(git, base, branch), expected):
        raise TaskError("Task-worktree publishing destination differs from the approved repository/remote; "
                        "inspect worktree Git configuration and URL rewrites before retrying")


def remote_heads(path, identity, base, branch):
    verify_remote_identity(Git(path), base, branch, identity)
    refs = [f"refs/heads/{base}", f"refs/heads/{branch}"]
    # get-url already expands insteadOf/pushInsteadOf. Passing the remote name
    # preserves that single resolution, rather than rewriting an expanded URL.
    output = native_git(path, "ls-remote", "--heads", "--", identity["remote"], *refs, refs=True)
    result = {}
    for line in output.splitlines():
        fields = line.split("\t")
        if (len(fields) != 2 or fields[1] not in refs or fields[1] in result
                or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", fields[0])):
            raise TaskError("Unexpected authoritative remote refs; inspect the configured remote")
        result[fields[1]] = fields[0]
    return result.get(refs[0]), result.get(refs[1])


def verify_remote(path, identity, base, branch, accepted_base, allowed_heads, *, observe_head=None,
                  require_base=True):
    base_head, head = remote_heads(path, identity, base, branch)
    if head is not None and observe_head is not None:
        observe_head(head)
    if require_base and base_head != accepted_base:
        raise TaskError("Remote base differs from the reviewed base; inspect/update the base and obtain a new review before publishing")
    if head not in allowed_heads:
        raise TaskError("Remote task branch conflicts with the reviewed/publishing commit; inspect it manually; no force-push is allowed")
    return head


def check_publishable_index(git):
    if any(entry and not entry.startswith("H ") for entry in git.command("ls-files", "-v", "-z").split("\0")):
        raise TaskError("Publishing refuses skip-worktree/assume-unchanged index entries; clear them and review again")
    if any(line.startswith("160000 ") for line in git.command("ls-files", "--stage", "-z").split("\0")):
        raise TaskError("Publishing submodules is not supported in v1; publish them manually")


@contextmanager
def prepared_index(git):
    """Stage final reviewed worktree contents without touching the user's index."""
    directory = Path(git.command("rev-parse", "--absolute-git-dir").strip())
    with tempfile.TemporaryDirectory(prefix="task-pr-index-", dir=directory) as temporary:
        index = Path(temporary) / "index"
        original = Path(git.command("rev-parse", "--path-format=absolute", "--git-path", "index").strip())
        try:
            shutil.copyfile(original, index)
        except OSError:
            raise TaskError("Cannot prepare a private publishing index; check Git metadata/filesystem access") from None
        env = {**os.environ, "GIT_INDEX_FILE": str(index)}
        def command(*args):
            return run(["git", "-C", str(git.repo), *args], env=env)
        command("add", "--all", "--", ".")
        tree = command("write-tree").strip()
        index_digest = digest(os.fsencode(command("ls-files", "--stage", "-z")))
        yield env, tree, index_digest


def verify_iteration_acceptance(accepted, worktree, *, evidence=None):
    """Replacement commits before a new pass can revoke the old acceptance.

    Admit only a checkpointed review of this run or an independently completed
    standalone review originating in it. Superseded evidence is never consulted.
    The publication lock also serializes replacement and review controllers.
    """
    try:
        if evidence is None:
            store = LoopStore(worktree)
            if not store.path.exists() and not store.path.is_symlink():
                return  # Standalone/legacy publication keeps its contract.
            current, _ = store.read()
            review = None
        else:
            # Canonical status supplies its before/after-checked read snapshot.
            current, review = evidence
            if current is None:
                return
        if any(accepted[k] != v for k, v in current['binding'].items()):
            raise ValueError('Different loop binding')
        if any(r['pass_id'] == accepted['pass_id'] for r in current['records']):
            verify_review_evidence(accepted, current, None)
            return
        if evidence is None:
            review = ReviewHandoffStore(worktree).read()
        if (not standalone_current(review, current) or not review['result']
                or review['result']['pass_id'] != accepted['pass_id']
                or review['binding'] != current['binding']):
            raise ValueError('Acceptance is not from this iteration')
        verify_review_evidence(accepted, current, review)
    except (TaskError, KeyError, ValueError, TypeError, AttributeError):
        raise TaskError(f"No current clean acceptance belongs to the active loop iteration; run {command_name()} review again") from None


def verify_acceptance(accepted, issue, project, repo, workspace, endpoint, registry, *, iteration_evidence=None):
    try:
        if (accepted["version"] != 1 or accepted["verdict"] != "clean"
                or accepted["issue"] != issue.identifier or accepted["repository"] != str(repo)
                or accepted["worktree"] != str(workspace.path) or accepted["branch"] != workspace.branch
                or accepted["workspace_id"] != workspace.workspace_id or accepted["endpoint"] != endpoint
                or accepted["base_branch"] != project.base_branch
                or accepted["review_state"]["branch"] != workspace.branch or accepted["review_state"]["version"] != 2
                or not accepted["completed_at"] or not accepted["pass_id"]):
            raise ValueError("mismatched acceptance")
        verify_iteration_acceptance(accepted, workspace.path, evidence=iteration_evidence)
        context = registry.get(accepted["context_id"])
        if (context["state"] != "active" or context["role"] != "review" or context["issue"] != issue.identifier
                or context["repository"] != str(repo) or context["worktree"] != str(workspace.path)
                or context["workspace_id"] != workspace.workspace_id or context["endpoint"] != endpoint
                or merge_session(None, context_reference(context), context["agent"]) != accepted["session"]
                or {"kind": context["agent"], "model": context["model"], "mode": context["mode"]} != accepted["execution"]):
            raise ValueError("mismatched reviewer")
    except (KeyError, TypeError, ValueError):
        raise TaskError(f"No current clean acceptance matches this exact task/context/base; run {command_name()} review again") from None


def prepare_metadata(issue, accepted):
    if accepted.get("publication") is None:
        raise TaskError(f"Clean review lacks public publishing metadata; run {command_name()} review again")
    public = publication_metadata(accepted.get("publication"), identifier=issue.identifier)
    title = f"{change_type(issue)}: {public['summary']} ({issue.identifier})"
    body = (f"## Summary\n\n{public['description']}\n\n## Tracking\n\n"
            f"Linear: [{issue.identifier}]({tracking_url(issue)})\n\n## Validation\n\n"
            f"{public['validation']}\n\nIndependent task review accepted this exact change as clean.\n")
    return title, body


def verify_commit(git, accepted, intent, *, expected_head=None):
    frozen_head = intent.get("publishing_head")
    if ("publishing_head" in intent and (not isinstance(frozen_head, str)
            or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", frozen_head))):
        raise TaskError("Invalid frozen publishing SHA; inspect private Git metadata before retrying")
    if expected_head is not None and frozen_head != expected_head:
        raise TaskError("Publishing SHA differs from the frozen intent; inspect private Git metadata before retrying")
    check_publishable_index(git)
    state = snapshot(git.repo, accepted["review_state"]["base_commit"], accepted["branch"])
    if frozen_head is not None and state.head != frozen_head:
        raise TaskError(f"Local HEAD {state.head} differs from frozen publishing SHA {frozen_head}; "
                        "the remote may already contain the frozen SHA. Inspect local/remote history and hooks "
                        "before retrying; no automatic repair is allowed")
    expected = accepted["review_state"]
    if state.content != expected["content"]:
        raise TaskError("Task content drifted from the accepted review; restore the reviewed state or obtain a new review")
    if state.head == expected["head"]:
        if state.as_dict() != expected:
            raise TaskError("Task index/state drifted from the accepted review; a new review is required")
        if intent.get("reuse_head") == state.head:
            if (git.command("rev-parse", f"{state.head}^{{tree}}").strip() != intent["tree"]
                    or git.command("show", "-s", "--format=%B", state.head).rstrip("\n") != intent["message"]):
                raise TaskError("Reviewed rebased commit differs from publishing intent; inspect it manually")
            return state.head
        if frozen_head is not None:
            raise TaskError("Frozen SHA does not identify a publishing commit; inspect private Git metadata")
        return None
    parents = git.command("rev-list", "--parents", "-n", "1", state.head).split()
    tree = git.command("rev-parse", f"{state.head}^{{tree}}").strip()
    message = git.command("show", "-s", "--format=%B", state.head).rstrip("\n")
    if (parents != [state.head, expected["head"]] or tree != intent["tree"]
            or message != intent["message"] or state.index not in {expected["index"], intent["index"]}):
        raise TaskError("HEAD/index is not the exact recorded publishing commit; inspect it manually, do not replay or rewrite it")
    return state.head


def commit(git, accepted, intent, *, freeze):
    head = verify_commit(git, accepted, intent)
    if head is None:
        with prepared_index(git) as (env, tree, index):
            if tree != intent["tree"] or index != intent["index"]:
                raise TaskError("Prepared Git tree changed since publication began; inspect filters/files and review again")
            if tree_content(git.repo, tree) != accepted["review_state"]["content"]:
                raise TaskError("Prepared tree cannot represent all reviewed bytes/modes; inspect Git ignore/filter/mode rules and review again")
            verify_commit(git, accepted, intent)
            native_git(git.repo, "commit", "--allow-empty", "--cleanup=verbatim", "-m", intent["message"], env=env)
        head = verify_commit(git, accepted, intent)
        if head is None:
            raise TaskError("Git did not create the publishing commit; inspect its hooks and retry")
    freeze(head)
    # The real index was never staged by us. Recover this step after an interrupted
    # successful commit only if its semantic entries still match the reviewed index.
    git.command("read-tree", intent["tree"])
    if git.command("status", "--porcelain=v1", "--untracked-files=all", "--ignore-submodules=none").strip():
        raise TaskError("Task worktree is not clean after the verified commit; inspect it before retrying")
    verify_commit(git, accepted, intent, expected_head=head)
    return head


def push(git, accepted, intent, identity, head, *, record_publication, defer_confirmation=False, require_existing=False):
    """Push once; optionally share confirmation with the next PR-stage check.

    Standalone callers confirm here. The publishing coordinator can defer the
    remote confirmation until after PR lookup, but must confirm on lookup failure
    too. Pending evidence already prevents rebasing if that caller is interrupted.
    """
    base = accepted["base_branch"]
    def observed(head):
        record_publication("published", head)
    verify_commit(git, accepted, intent, expected_head=head)
    remote_head = verify_remote(git.repo, identity, base, accepted["branch"], accepted["review_state"]["base_commit"],
                                {accepted["review_state"]["head"], head} | (set() if require_existing else {None}),
                                observe_head=observed)
    if remote_head != head:
        verify_remote_identity(git, base, accepted["branch"], identity)
        verify_commit(git, accepted, intent, expected_head=head)
        # Keep uncertainty durable too: an interrupted/failed push or a failed
        # confirmation write cannot make the branch safe to rewrite later.
        record_publication("pending", head)
        # The explicit SHA/refspec bypasses branch push defaults. The checked
        # remote has one destination and neither mirror nor group semantics.
        try:
            native_git(git.repo, "push", "--no-follow-tags", "--recurse-submodules=no", "--", identity["remote"],
                       f"{head}:refs/heads/{accepted['branch']}")
        finally:
            # A pre-push hook can amend HEAD while the explicit refspec still
            # sends the frozen commit, even when Git reports success or fails.
            verify_commit(git, accepted, intent, expected_head=head)
        if defer_confirmation:
            return True
        verify_remote(git.repo, identity, base, accepted["branch"], accepted["review_state"]["base_commit"], {head},
                      observe_head=observed, require_base=False)
    verify_commit(git, accepted, intent, expected_head=head)
    return False


def require_never_published(history):
    if history is None:
        return
    if isinstance(history, dict) and history["state"] == "published":
        raise TaskError("Task branch was already published; automatic rebase is forbidden even if the remote ref was deleted. "
                        "Follow-up publication requires the pinned published base; inspect advanced-base integration manually. "
                        "No force-push is allowed")
    raise TaskError("A prior push may have published this task branch; automatic rebase is forbidden. "
                    "Inspect the uncertain push/legacy publication history before retrying")


def require_unpublished(git, permanent, identity, base_branch, branch, base, *, record_published):
    permanent.check_base(base_branch)
    if permanent.command("rev-parse", "HEAD").strip() != base:
        raise TaskError("Permanent base changed during rebase; inspect it before retrying")
    verify_remote_identity(permanent, base_branch, branch, identity)
    remote_base, remote_head = remote_heads(git.repo, identity, base_branch, branch)
    if remote_head is not None:
        record_published(remote_head)
    if remote_base != base:
        raise TaskError(f"Remote base changed during rebase preparation; rerun {command_name()} pr after inspecting base state")
    pulls = list(pull_requests(identity["repository"], branch)) if remote_head is None else []
    if pulls:
        record_published(pulls[0]["head"].get("sha"))
    if remote_head is not None or pulls:
        raise TaskError("Task branch or PR is already published; automatic rebase is forbidden. Inspect history manually; no force-push is allowed")


def advance_base(git, permanent, identity, base_branch, branch, reviewed_base, remote_base):
    with PublicationStore(permanent.repo).repository_locked():
        return _advance_base(git, permanent, identity, base_branch, branch, reviewed_base, remote_base)


def _advance_base(git, permanent, identity, base_branch, branch, reviewed_base, remote_base):
    if remote_base is None:
        raise TaskError("Configured remote base is missing; inspect the upstream before publishing")
    verify_remote_identity(git, base_branch, branch, identity)
    native_git(git.repo, "fetch", "--no-tags", "--no-recurse-submodules", "--refmap=", "--",
               identity["remote"], f"refs/heads/{base_branch}")
    fetched = git.command("rev-parse", "--verify", "FETCH_HEAD^{commit}").strip()
    if fetched != remote_base:
        raise TaskError(f"Remote base changed while fetching; rerun {command_name()} pr to verify the latest base")
    permanent.check_base(base_branch)
    local_base = permanent.command("rev-parse", "HEAD").strip()
    for ancestor in (reviewed_base, local_base):
        if git.command("rev-list", "--count", f"{fetched}..{ancestor}").strip() != "0":
            raise TaskError("Base has diverged or contains local-only commits; reconcile it manually and review again")
    verify_remote_identity(permanent, base_branch, branch, identity)
    if local_base != fetched:
        native_git(permanent.repo, "-c", f"branch.{base_branch}.mergeOptions=", "merge", "--ff-only",
                   "--no-squash", "--no-autostash", "--no-overwrite-ignore", fetched)
    permanent.check_base(base_branch)
    if permanent.command("rev-parse", "HEAD").strip() != fetched:
        raise TaskError("Permanent base did not reach the verified upstream; inspect it before retrying")


def publish(identifier):
    if any(k in os.environ for k in ("GIT_INDEX_FILE", "GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR")):
        raise TaskError(f"Unset Git repository/index routing environment overrides before {command_name()} pr")
    local = load_local(no_agent=True)
    issue = Linear(local.api_key).get_issue(identifier)
    project = resolve_project(load_projects(), issue.project)
    repo = repository_path(local, project)
    registry, identities = ContextRegistry(), HerdrContexts()
    workspace, _, _, endpoint = resolve_review_workspace(issue, project, repo, registry, identities, local_only=True)
    with PublicationStore(workspace.path).locked() as store:
        saved = store.read()
        integration_guard = check_integration(store, saved, registry, issue, project, repo, identities)
        if integration_guard is not None:
            # Finish only the durable local plan. Remote eligibility and API
            # credentials are irrelevant until a fresh review authorizes publish.
            try:
                continue_rebase(Git(workspace.path), saved, store, native_git, integration_guard,
                                verify_identity=integration_guard)
            finally:
                if saved["rebase"]["result"] is not None:
                    integration_guard.finish()
        if (saved["rebase"] is not None and saved["rebase"].get("integration_id")
                and saved["rebase"]["result"] is not None and saved["acceptance"] is None):
            raise TaskError(f"No current clean acceptance; run a new independent {command_name()} review of the installed integration")
        accepted, intent = saved["acceptance"], saved["intent"]
        git = Git(workspace.path)
        permanent = Git(repo)
        permanent.check_base(project.base_branch)
        base = permanent.command("rev-parse", "--verify", f"refs/heads/{project.base_branch}^{{commit}}").strip()
        identity = remote_identity(permanent, project.base_branch, workspace.branch)
        verify_remote_identity(git, project.base_branch, workspace.branch, identity)
        api_credential(required=True)
        binding = dict(issue=issue.identifier, repository=str(repo), worktree=str(workspace.path),
                       branch=workspace.branch, base_branch=project.base_branch, endpoint=endpoint,
                       workspace_id=workspace.workspace_id)
        # Publication is a branch-lifetime fact, independent of review passes,
        # intents, rebase journals and mutable Herdr context locations.
        history_binding = {key: binding[key] for key in ("issue", "repository", "worktree", "branch", "base_branch")}
        verify_publication_history(saved["publication_history"], history_binding, identity)

        def record_publication(state, head):
            previous = saved["publication_history"]
            if not isinstance(head, str) or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", head):
                raise TaskError("Cannot establish a published branch SHA; inspect authoritative remote/PR history")
            lineage = isinstance(previous, dict) and previous["version"] == 2
            if lineage and previous["state"] == "published" and head == previous["head"]:
                return
            if intent is not None and head == intent.get("publishing_head"):
                if not lineage:
                    if (isinstance(previous, dict) and previous["head"] not in {head, accepted["review_state"]["head"]}):
                        raise TaskError("Recorded publication conflicts with the frozen commit; inspect published history manually")
                    previous = dict(version=2, binding=history_binding, identity=identity,
                                    state=previous["state"] if isinstance(previous, dict) else "pending",
                                    head=previous["head"] if isinstance(previous, dict) else head,
                                    prior=deepcopy(previous), base_commit=accepted["review_state"]["base_commit"],
                                    pull_number=intent.get("pull_number"), cycles=[])
                history = deepcopy(previous)
                cycles = history["cycles"]
                if not cycles or cycles[-1]["intent"]["publishing_head"] != head:
                    cycles.append(dict(acceptance=deepcopy(accepted), intent=deepcopy(intent), state=state))
                elif state == "published" and cycles[-1]["state"] == "pending":
                    cycles[-1]["state"] = state
                if state == "published" or history["state"] != "published":
                    history.update(state=state, head=head)
                verify_publication_history(history, history_binding, identity)
                if history != saved["publication_history"]:
                    saved["publication_history"] = history
                    store.write(saved)
                return
            if lineage:
                raise TaskError("Remote task branch conflicts with durable published history; inspect it manually; no force-push is allowed")
            if isinstance(previous, dict) and (previous["state"] == "published" or state == "pending"):
                return
            # External/legacy publication evidence still forbids any rebase, but
            # is not promoted into reviewed lineage without a proven frozen SHA.
            saved["publication_history"] = dict(version=1, binding=history_binding, identity=identity, state=state, head=head)
            store.write(saved)

        def record_published(head):
            record_publication("published", head)

        def verify_unpublished(expected_base):
            require_never_published(saved["publication_history"])
            require_unpublished(git, permanent, identity, project.base_branch, workspace.branch, expected_base,
                                record_published=record_published)

        rebase = saved["rebase"]
        if rebase is not None:
            validate_record(rebase)
            if (rebase.get("binding") != binding or not same_remote_identity(rebase["identity"], identity)):
                raise TaskError("Rebase provenance conflicts with task/repository identity; inspect private Git metadata")
            if rebase.get("result") is None:
                continue_rebase(git, saved, store, native_git, lambda: verify_unpublished(rebase["base"]),
                                verify_identity=integration_guard)
        verify_acceptance(accepted, issue, project, repo, workspace, endpoint, registry)
        reviewed_base = accepted["review_state"]["base_commit"]
        history = saved["publication_history"]
        lineage = history if isinstance(history, dict) and history["version"] == 2 else None
        followup = False
        previous_published_head = None
        if lineage is not None:
            last = lineage["cycles"][-1]
            if accepted["pass_id"] == last["acceptance"]["pass_id"]:
                if accepted != last["acceptance"] or intent != last["intent"]:
                    raise TaskError("Current review/intent differs from durable publication lineage; inspect private Git metadata")
                verify_commit(git, accepted, intent)
                if len(lineage["cycles"]) > 1 and last["state"] != "complete":
                    previous_published_head = lineage["cycles"][-2]["intent"]["publishing_head"]
            else:
                followup = True
                if last["state"] != "complete" or lineage["pull_number"] is None:
                    raise TaskError("Prior publication is unfinished; reconcile its original frozen commit and PR before follow-up publication")
                if accepted["review_state"]["head"] != lineage["head"]:
                    raise TaskError("Published history changed: follow-up HEAD must equal the latest published SHA; "
                                    "inspect history and keep follow-up edits uncommitted before review")
                previous_published_head = lineage["head"]
            if reviewed_base != lineage["base_commit"] or (followup and base != lineage["base_commit"]):
                raise TaskError("Published task base advanced or changed; automatic rebase is forbidden. "
                                "Follow-up review requires the pinned published base; advanced-base integration needs manual review")
            # Immutable object identity and every sole-parent link are checked,
            # including completed cycles no longer held by current acceptance.
            for cycle in lineage["cycles"]:
                original, frozen_intent = cycle["acceptance"], cycle["intent"]
                published_head = frozen_intent["publishing_head"]
                parents = git.command("rev-list", "--parents", "-n", "1", published_head).split()
                if ((frozen_intent.get("reuse_head") != published_head
                        and parents != [published_head, original["review_state"]["head"]])
                        or git.command("rev-parse", f"{published_head}^{{tree}}").strip() != frozen_intent["tree"]
                        or git.command("show", "-s", "--format=%B", published_head).rstrip("\n") != frozen_intent["message"]
                        or git.command("rev-list", "--count", f"HEAD..{published_head}").strip() != "0"):
                    raise TaskError("Published history was rewritten or no longer belongs to local HEAD; inspect ancestry manually")
        elif history is not None and intent is None:
            raise TaskError("Prior publication lacks its original review/intent lineage; inspect legacy history manually "
                            "before publishing follow-up changes")
        if git.command("rev-list", "--count", f"{accepted['review_state']['head']}..{reviewed_base}").strip() != "0":
            raise TaskError("Task does not contain the reviewed base; reconcile its history and review again")
        frozen = None
        if rebase is not None:
            frozen = rebase["publication"]
            if (rebase["result"] != accepted["review_state"]
                    or validate_commits(git, rebase) != accepted["review_state"]["head"]
                    or accepted.get("publication_approval") != publication_fingerprint(frozen)):
                raise TaskError(f"Clean review does not approve this rebased state and frozen publication metadata; run {command_name()} review again")
        if intent is None:
            before = snapshot(workspace.path, reviewed_base, workspace.branch)
            if before.as_dict() != accepted["review_state"]:
                raise TaskError(f"Task state drifted since its clean review; run {command_name()} review again")
            check_publishable_index(git)
            title, body = ((frozen["title"], frozen["body"]) if frozen is not None else prepare_metadata(issue, accepted))
            with prepared_index(git) as (_, tree, index):
                if tree_content(git.repo, tree) != before.content:
                    raise TaskError("Prepared tree cannot represent all reviewed bytes/modes; inspect Git ignore/filter/mode rules and review again")
                if not followup and tree == git.command("rev-parse", f"{reviewed_base}^{{tree}}").strip():
                    raise TaskError("No reviewed change against the base to publish")
                if followup and tree == git.command("rev-parse", f"{lineage['head']}^{{tree}}").strip():
                    raise TaskError("No new reviewed changes since the latest publication; no follow-up commit is needed")
                intent = dict(version=1, pass_id=accepted["pass_id"], **identity, tree=tree, index=index,
                              title=title, body=body, message=(rebase["steps"][-1]["message"] if frozen is not None
                                  else f"{title}\n\nTask-Review: {accepted['pass_id']}"))
            if frozen is not None:
                intent["reuse_head"] = before.head
            if snapshot(workspace.path, reviewed_base, workspace.branch) != before:
                raise TaskError(f"Task changed while preparing publication; run {command_name()} review again")
        else:
            try:
                if (intent["version"] != 1 or intent["pass_id"] != accepted["pass_id"]
                        or not same_remote_identity(intent, identity)
                        or not all(isinstance(intent[k], str) and intent[k] for k in ("tree", "index", "title", "body", "message"))):
                    raise ValueError("mismatched intent")
            except (KeyError, TypeError, ValueError):
                raise TaskError("Publishing intent conflicts with task/repository/review identity; inspect private Git metadata") from None
        if frozen is not None:
            expected = dict(**frozen, reuse_head=accepted["review_state"]["head"],
                            message=rebase["steps"][-1]["message"], tree=rebase["tree"], index=rebase["index"])
            if any(intent.get(key) != value for key, value in expected.items()):
                raise TaskError("Publishing intent differs from the approved frozen metadata/rebased commit; inspect private Git metadata")

        def freeze(head):
            if intent.get("publishing_head") == head:
                return
            if "publishing_head" in intent:
                raise TaskError("Cannot replace the frozen publishing SHA; inspect private Git metadata")
            intent["publishing_head"] = head
            saved["intent"] = intent
            store.write(saved)

        head = verify_commit(git, accepted, intent)
        if head is not None:
            # Pin recovered/rebased commits before any transport or PR lookup.
            freeze(head)
        allowed = {accepted["review_state"]["head"]} | ({head} if head else set())
        remote_base, remote_head = remote_heads(workspace.path, identity, project.base_branch, workspace.branch)
        if remote_head is not None:
            record_published(remote_head)
        already_published = head is not None and remote_head == head
        if remote_base != reviewed_base and not already_published:
            require_never_published(saved["publication_history"])
        if (remote_head is None and isinstance(saved["publication_history"], dict)
                and saved["publication_history"]["state"] == "published"):
            raise TaskError("Previously published remote task branch is missing; inspect remote history manually before retrying")
        if remote_head not in allowed | {None}:
            raise TaskError("Remote task branch conflicts with the reviewed/publishing commit; inspect it manually; no force-push is allowed")

        def verify_published():
            verify_commit(git, accepted, intent, expected_head=head)
            verify_remote(workspace.path, identity, project.base_branch, workspace.branch, reviewed_base, {head},
                          observe_head=record_published, require_base=False)
            verify_commit(git, accepted, intent, expected_head=head)

        def expected_pull_number():
            history = saved["publication_history"]
            return (history["pull_number"] if isinstance(history, dict) and history["version"] == 2
                    else intent.get("pull_number"))

        def record_pull(pull):
            number = pull["number"]
            expected = expected_pull_number()
            if expected is not None and number != expected:
                raise TaskError("Task PR identity changed; inspect the recorded PR before retrying")
            if expected is None:
                history = saved["publication_history"]
                if isinstance(history, dict) and history["version"] == 2:
                    history["pull_number"] = number
                else:
                    intent["pull_number"] = number
                    saved["intent"] = intent
                store.write(saved)

        # A retry may already have pushed this frozen follow-up. In that case,
        # the preflight PR lookup needs the same bounded propagation check too.
        propagating = previous_published_head if head is not None and remote_head == head else None
        pull = publication_pull(identity["repository"], workspace.branch, project.base_branch,
                                {head} if propagating else allowed, expected_number=expected_pull_number(),
                                previous_head=propagating, verify_published=verify_published)
        if pull is not None:
            record_published(pull["head"]["sha"])
            record_pull(pull)
        if remote_base != reviewed_base and not already_published:
            require_never_published(saved["publication_history"])
            if remote_head is not None or pull is not None:
                raise TaskError("Base advanced but the task branch or PR is already published; automatic rebase is forbidden. Inspect history manually; no force-push is allowed")
            advance_base(git, permanent, identity, project.base_branch, workspace.branch, reviewed_base, remote_base)
            verify_unpublished(remote_base)
            verify_commit(git, accepted, intent)
            plan = integration_plan(git, accepted, intent, remote_base)
            verify_commit(git, accepted, intent)
            source = snapshot(workspace.path, reviewed_base, workspace.branch).as_dict()
            plan.update(binding=binding, identity=identity, source=source,
                        source_tree=intent["tree"], source_index=intent["index"], result=None,
                        publication=dict(title=intent["title"], body=intent["body"]))
            plan["publication_fingerprint"] = publication_fingerprint(plan["publication"])
            # Invalidate BEFORE any signed commit or real worktree mutation. A
            # pending journal can only finish integration and demand new review.
            saved.update(acceptance=None, intent=None, rebase=plan)
            store.write(saved)
            continue_rebase(git, saved, store, native_git, lambda: verify_unpublished(remote_base))
        if base != reviewed_base and not already_published:
            raise TaskError("Local base differs from the reviewed/remote base; reconcile it manually and review again")
        if saved["intent"] is None:
            saved["intent"] = intent
            store.write(saved)  # Durable BEFORE commit; no guessed completion flags.
        head = commit(git, accepted, intent, freeze=freeze)
        # A ref already observed at this exact SHA needs no push. This observation
        # only skips transport; it never authorizes a PR write or URL reporting.
        confirmation_pending = False
        if remote_head != head:
            confirmation_pending = push(git, accepted, intent, identity, head,
                                        record_publication=record_publication, defer_confirmation=True,
                                        require_existing=isinstance(saved["publication_history"], dict)
                                            and saved["publication_history"]["state"] == "published")
        def verify_local():
            verify_commit(git, accepted, intent, expected_head=head)
        def before_write():
            nonlocal confirmation_pending
            # A failed confirmation already stops this invocation. Do not repeat
            # authentication while unwinding that same failure.
            confirmation_pending = False
            # Confirm the exact published task ref; main can advance after push
            # without making that publication uncertain. GitHub lookups are
            # observations, not a lock on task refs or PR identity.
            # This fresh lookup also confirms any just-completed push and saves
            # positive evidence before GitHub writes. Never reuse it across an
            # API call when authorizing another write or reporting the URL.
            verify_published()
        verify_local()
        try:
            result = publish_pull(identity["repository"], workspace.branch, project.base_branch, head,
                                  intent["title"], intent["body"], verify_local=verify_local, before_write=before_write,
                                  expected_number=expected_pull_number(), observe_pull=record_pull,
                                  previous_head=previous_published_head)
        except TaskError as error:
            if confirmation_pending:
                # A GitHub lookup failure must not discard evidence of a push
                # that succeeded. Preserve the original API diagnostic if Git
                # confirms the expected state; conflicting Git state still stops.
                try:
                    before_write()
                except TaskError as confirmation_error:
                    raise TaskError(f"{error}. Publishing confirmation also failed: {confirmation_error}") from None
            raise
        before_write()  # Fresh remote + local proof after the last API lookup.
        cycle = saved["publication_history"]["cycles"][-1]
        if cycle["state"] != "complete":
            cycle["state"] = "complete"
            store.write(saved)
        return result
