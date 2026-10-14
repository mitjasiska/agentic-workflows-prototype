"""Disposable integration and recoverable installation of an unpublished rebase.

Only unreachable objects are created during integration. The real branch, index
and files are untouched until the complete replay is known to be conflict-free.
The bounded journal is provenance for commit reuse, never review acceptance.
"""

from contextlib import contextmanager
import os
from pathlib import Path
import re
import shutil
import tempfile

from . import TaskError
from .command import command_name
from .review_result import publication_fingerprint
from .review_state import digest, snapshot, tree_content
from .workspace import run


def review_required():
    return (f"Task rebased onto the updated base; run a new independent {command_name()} review "
            f"before rerunning {command_name()} pr. Nothing was pushed.")


class IntegrationConflict(TaskError):
    """A deterministic replay stopped with actual unmerged paths."""


def replay(command, base, old_base):
    try:
        command("rebase", "--onto", base, old_base, "--no-rebase-merges",
                "--no-autosquash", "--no-autostash", "--no-update-refs", "--reapply-cherry-picks",
                "--empty=keep", "--keep-empty", "--no-fork-point", "--no-verify", "--strategy=ort")
    except TaskError:
        error = IntegrationConflict if command("ls-files", "--unmerged", "-z") else TaskError
        raise error("Disposable rebase conflicts or cannot be completed safely; task branch, index and files are unchanged. "
                    f"Use {command_name()} integrate for reviewed uncommitted conflicts, or resolve/rebase manually and review again") from None


def validate_record(rebase):
    if publication_fingerprint(rebase.get("publication")) != rebase.get("publication_fingerprint"):
        raise TaskError("Frozen publication metadata changed; inspect rebase provenance before retrying")
    try:
        if (rebase["version"] != 1 or not isinstance(rebase["binding"], dict)
                or not isinstance(rebase["identity"], dict) or not isinstance(rebase["source"], dict)
                or not isinstance(rebase["steps"], list) or not rebase["steps"]
                or not isinstance(rebase["commits"], list)
                or (rebase["result"] is not None and not isinstance(rebase["result"], dict))
                or any(not isinstance(rebase[key], str) or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", rebase[key])
                       for key in ("base", "tree", "source_tree", "index", "source_index", "content"))):
            raise ValueError("invalid journal")
        for step in rebase["steps"]:
            if (not isinstance(step["tree"], str) or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", step["tree"])
                    or not isinstance(step["message"], str) or not step["message"]
                    or (step["author"] is not None and (not isinstance(step["author"], list)
                        or len(step["author"]) != 3 or not all(isinstance(v, str) for v in step["author"])))):
                raise ValueError("invalid step")
        if rebase["tree"] != rebase["steps"][-1]["tree"] or len(rebase["commits"]) > len(rebase["steps"]):
            raise ValueError("invalid chain")
        if rebase["publication"]["title"] != rebase["steps"][-1]["message"].split("\n", 1)[0]:
            raise ValueError("frozen title differs from rebased commit")
        if any(not isinstance(head, str) or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", head) for head in rebase["commits"]):
            raise ValueError("invalid commit")
        for key in ("remote", "repository"):
            if not isinstance(rebase["identity"][key], str) or not rebase["identity"][key]:
                raise ValueError("invalid remote identity")
        for key in ("base_commit", "branch", "head", "content", "index"):
            if not isinstance(rebase["source"][key], str) or not rebase["source"][key]:
                raise ValueError("invalid source")
    except (KeyError, TypeError, ValueError):
        raise TaskError("Invalid rebase continuation evidence; inspect private Git metadata, never manufacture acceptance") from None


def check_ignored_obstructions(git, tree):
    # read-tree -u can overwrite ignored files. They were excluded from review,
    # so preserve them even when upstream now tracks one of those paths.
    ignored = set(git.command("ls-files", "--others", "--ignored", "--exclude-standard", "-z").split("\0")) - {""}
    for name in git.command("ls-tree", "-r", "--name-only", "-z", tree).split("\0"):
        if name and any(name == other or name.startswith(other + "/") or other.startswith(name + "/") for other in ignored):
            raise TaskError("Ignored task files obstruct the rebased tree; move them aside before retrying. No task files were overwritten")


@contextmanager
def checkout_index(git, source, target):
    """Prove the checkout against real files using only a private index.

    read-tree alone leaves entries without stat data. Refresh them before the
    two-tree checkout, especially for existing files changed by the new base.
    Neither setup nor the dry run can stage the real reviewed index.
    """
    try:
        directory = tempfile.TemporaryDirectory(prefix="task-pr-checkout-")
    except OSError:
        raise TaskError("Rebase checkout preflight cannot create a private index; check temporary-directory access") from None
    with directory:
        if Path(directory.name).resolve().is_relative_to(git.repo):
            raise TaskError("Private checkout index must be outside the task worktree; check TMPDIR")
        index = Path(directory.name) / "index"
        env = {**os.environ, "GIT_INDEX_FILE": str(index)}

        def command(*args):
            return run(["git", "-C", str(git.repo), *args], env=env)

        try:
            command("read-tree", source)
            command("update-index", "--refresh")
            command("read-tree", "--dry-run", "-m", "-u", source, target)
        except TaskError:
            raise TaskError("Rebase checkout preflight failed; no task index or files were changed by preflight. "
                            "Inspect file/index state and Git checkout settings before retrying") from None
        yield command, index


def install_checkout(git, source, target, verify_source, *, verify_identity=None):
    with checkout_index(git, source, target) as (command, prepared):
        index = Path(git.command("rev-parse", "--path-format=absolute", "--git-path", "index").strip())
        lock = index.with_name(index.name + ".lock")
        try:
            fd = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, index.stat().st_mode & 0o777)
        except FileExistsError:
            raise TaskError("Task Git index is locked; wait for the other Git operation before retrying rebase. "
                            "Task index and files were not changed") from None
        except OSError:
            raise TaskError("Cannot lock the task Git index for rebase; check filesystem access before retrying") from None
        try:
            with os.fdopen(fd, "wb") as output:
                verify_source()
                # Git updates files and the private index. Publish that complete
                # index atomically only after checkout succeeds; never expose a
                # staged source tree in the real index on setup/checkout failure.
                if verify_identity is not None:
                    verify_identity()
                try:
                    command("read-tree", "-m", "-u", source, target)
                except TaskError:
                    raise TaskError("Rebase checkout installation failed; the original task index was preserved. "
                                    "Inspect task files and checkout settings before retrying") from None
                with prepared.open("rb") as source_index:
                    shutil.copyfileobj(source_index, output)
                output.flush()
                os.fsync(output.fileno())
            if verify_identity is not None:
                verify_identity()
            os.replace(lock, index)
        except OSError:
            raise TaskError("Rebase checkout/index installation was not confirmed; inspect task files/index "
                            f"and rerun {command_name()} pr to verify continuation") from None
        finally:
            lock.unlink(missing_ok=True)


def integration_plan(git, accepted, intent, base):
    try:
        return _integration_plan(git, accepted, intent, base)
    except OSError:
        raise TaskError("Disposable rebase setup failed; task branch, index and files are unchanged. "
                        "Check temporary-directory and Git object access before retrying") from None


def _integration_plan(git, accepted, intent, base):
    old = accepted["review_state"]
    if git.command("rev-list", "--merges", f"{old['base_commit']}..{old['head']}").strip():
        raise TaskError("Automatic unpublished rebase requires linear task history; rebase merge commits manually and review again")
    # An isolated repository shares only the object database. Its temporary
    # refs/index/worktree cannot move real branches or update other worktrees.
    with tempfile.TemporaryDirectory(prefix="task-pr-rebase-") as directory:
        path = Path(directory)
        if path.resolve().is_relative_to(git.repo):
            raise TaskError("Disposable integration must be outside the task worktree; check TMPDIR")
        hooks = path / "no-hooks"
        hooks.mkdir()
        env = {**os.environ, "GIT_OBJECT_DIRECTORY": git.command(
            "rev-parse", "--path-format=absolute", "--git-path", "objects").strip()}

        def command(*args):
            return run(["git", "-C", directory, "-c", f"core.hooksPath={hooks}",
                        "-c", "commit.gpgSign=false", "-c", "rerere.enabled=false",
                        "-c", "user.name=Task integration", "-c", "user.email=integration@localhost", *args], env=env)

        try:
            command("init", "--quiet", "--object-format=" + git.command("rev-parse", "--show-object-format").strip())
            # The final reviewed materialized change is the publication commit, even
            # when a previous invocation has already committed it in the real tree.
            tip = (intent.get("reuse_head") or
                   command("commit-tree", intent["tree"], "-p", old["head"], "-m", intent["message"]).strip())
            command("checkout", "--quiet", "--detach", tip)
        except TaskError:
            raise TaskError("Disposable rebase setup failed; task branch, index and files are unchanged. "
                            "Inspect Git object access and checkout settings before retrying") from None
        replay(command, base, old["base_commit"])
        steps = []
        for commit in command("rev-list", "--reverse", f"{base}..HEAD").splitlines():
            steps.append(dict(tree=command("rev-parse", f"{commit}^{{tree}}").strip(),
                              message=command("show", "-s", "--format=%B", commit).rstrip("\n"),
                              author=command("show", "-s", "--format=%an%x00%ae%x00%aI", commit).rstrip("\n").split("\0")))
        if not steps or steps[-1]["tree"] == git.command("rev-parse", f"{base}^{{tree}}").strip():
            raise TaskError("Rebased task has no change against the updated base; inspect it manually")
        # The newly generated publication commit uses the caller's normal author
        # identity; existing implementation commits keep their original authors.
        steps[-1]["author"] = None
        check_ignored_obstructions(git, steps[-1]["tree"])
        # Prove checkout safety as part of probing, before revoking acceptance
        # or signing replacement commits. The real index stays exactly reviewed.
        with checkout_index(git, intent["tree"], steps[-1]["tree"]):
            pass
        return dict(version=1, base=base, steps=steps, commits=[], tree=steps[-1]["tree"],
                    content=tree_content(git.repo, steps[-1]["tree"]),
                    index=digest(os.fsencode(command("ls-files", "--stage", "-z"))))


def validate_commits(git, rebase):
    parent = rebase["base"]
    if len(rebase["commits"]) > len(rebase["steps"]):
        raise TaskError("Invalid rebase commit evidence; inspect private Git metadata")
    for step, head in zip(rebase["steps"], rebase["commits"]):
        if (not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", head)
                or git.command("rev-list", "--parents", "-n", "1", head).split() != [head, parent]
                or git.command("rev-parse", f"{head}^{{tree}}").strip() != step["tree"]
                or git.command("show", "-s", "--format=%B", head).rstrip("\n") != step["message"]):
            raise TaskError("Rebased commit no longer matches its recorded parent/tree/message; inspect it manually")
        if step["author"] is not None and git.command("show", "-s", "--format=%an%x00%ae%x00%aI", head).rstrip("\n").split("\0") != step["author"]:
            raise TaskError("Rebased implementation author differs from its recorded identity; inspect it manually")
        parent = head
    return parent if rebase["commits"] else None


def installation_state(git, rebase):
    """Prove only the frozen source or a journaled installation intermediate."""
    source = rebase["source"]
    if any(entry and not entry.startswith("H ") for entry in git.command("ls-files", "-v", "-z").split("\0")):
        raise TaskError("Task index flags changed during rebase; inspect it manually")
    current = snapshot(git.repo, source["base_commit"], source["branch"])
    head = validate_commits(git, rebase)
    source_matches = (current.head == source["head"] and current.content == source["content"]
                      and current.index in {source["index"], rebase["source_index"]})
    if rebase.get("integration_id"):
        # Agent-assisted installation never stages its materialized source
        # into the real index. An unchanged source must still be EXACTLY the
        # accepted snapshot; only the proven target permits crash recovery.
        source_matches = current.as_dict() == source
    target_matches = (len(rebase["commits"]) == len(rebase["steps"])
                      and current.content == rebase["content"]
                      and ((current.index == rebase["index"] and current.head in {source["head"], head})
                           or (current.head == source["head"]
                               and current.index in ({source["index"]} if rebase.get("integration_id")
                                                     else {source["index"], rebase["source_index"]}))))
    if not source_matches and not target_matches:
        raise TaskError("Task changed during pending rebase; inspect branch/index/files manually before retrying")
    # Checkout may have completed before the atomic index replacement.
    return current, target_matches and current.index == rebase["index"]


def continue_rebase(git, saved, store, native, verify_installation, *, verify_identity=None):
    rebase = saved["rebase"]
    if rebase.get("integration_id") and not callable(verify_identity):
        raise TaskError("Integration installation requires a live identity/scope guard; inspect pending evidence")
    source = rebase["source"]

    def state():
        return installation_state(git, rebase)

    state()
    verify_installation()
    while len(rebase["commits"]) < len(rebase["steps"]):
        step = rebase["steps"][len(rebase["commits"])]
        parent = rebase["commits"][-1] if rebase["commits"] else rebase["base"]
        env = dict(os.environ)
        if step["author"] is not None:
            env.update(zip(("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_AUTHOR_DATE"), step["author"]))
        # Plumbing needs an explicit -S to honor commit.gpgSign. Git still owns
        # key selection and GPG/SSH transport; only the resulting object ID is
        # read while signing prompts/input retain the caller's TTY.
        signing = ["-S"] if git.command("config", "--type=bool", "--default", "false", "--get", "commit.gpgSign").strip() == "true" else []
        head = native(git.repo, "commit-tree", *signing, step["tree"], "-p", parent,
                      "-m", step["message"], env=env, refs=True).strip()
        if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", head):
            raise TaskError(f"Git did not return a rebased commit identity; rerun {command_name()} pr to inspect continuation")
        rebase["commits"].append(head)
        validate_commits(git, rebase)
        store.write(saved)
        state()
    # No real mutation until the entire signed chain is durable and the local
    # installation basis still matches. A crash before update-ref is provable
    # from the two allowed trees/indexes and the compare-and-swap branch update.
    verify_installation()
    current, at_target = state()
    head = rebase["commits"][-1]
    if not at_target:
        check_ignored_obstructions(git, rebase["tree"])
        checkout_source = rebase["tree"] if current.content == rebase["content"] else rebase["source_tree"]
        def verify_source():
            verify_installation()
            if state()[0] != current:
                raise TaskError("Task changed during rebase checkout preparation; inspect it before retrying")
        install_checkout(git, checkout_source, rebase["tree"], verify_source, verify_identity=verify_identity)
        _, at_target = state()
        if not at_target:
            raise TaskError("Rebased checkout differs from the planned tree; inspect filters/files manually")
    if current.head != head:
        if verify_identity is not None:
            verify_identity()
        git.command("update-ref", f"refs/heads/{source['branch']}", head, source["head"])
    state()
    if git.command("status", "--porcelain=v1", "--untracked-files=all", "--ignore-submodules=none").strip():
        raise TaskError("Rebased checkout is not clean; inspect it before obtaining a fresh review")
    result = snapshot(git.repo, rebase["base"], source["branch"]).as_dict()
    completed = dict(saved, rebase=dict(rebase, result=result))
    if verify_identity is None:
        store.write(completed)
    else:
        store.write(completed, before_replace=verify_identity)
    rebase["result"] = result
    raise TaskError(review_required())
