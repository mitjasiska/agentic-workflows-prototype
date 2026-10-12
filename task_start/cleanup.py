"""Forced unpublished or proven merged execution disposal using the retirement model.

The common Git directory retains a bounded journal after linked-worktree metadata
is removed. Every destructive step is claimed durably before it starts; retries
accept absence only for a claimed step and never reconstruct execution state.
"""

from contextlib import nullcontext
from dataclasses import asdict, dataclass
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import shutil
import shlex
import stat
import sys
import time
from uuid import UUID, uuid4
from types import SimpleNamespace

from . import HerdrResponseError, TaskError
from .contexts import ContextRegistry, HerdrContexts, context_reference, now
from .agent import AgentOptions, adapter_for
from .ownership import ownership_gate, ownership_operation
from .github import pull_requests, repository_name
from .integration_process import owned_pane_processes, shell_executable, stopped_execution
from .integration_state import IntegrationStore, abandoned_context, evidence_digest, integration_tombstone
from .integrate import history_identity
from .publication_state import PublicationStore, verify_publication_history
from .loop_state import LoopStore
from .pass_delivery import FILES as PASS_FILES, check_directory, read_private, validate_delivery
from .publish import remote_identity, verify_remote_identity
from .review_result import unique_object
from .workspace import Git, Herdr, HerdrRetirement, TaskWorktree, Workspace, belongs_to_issue


MOUNTINFO = Path("/proc/self/mountinfo")


@dataclass(frozen=True)
class Mount:
    mount_id: int
    parent: int
    device: tuple[int, int]
    root: Path
    point: Path
    options: tuple[bytes, ...]
    optional: tuple[bytes, ...]
    filesystem: bytes
    source: bytes
    super_options: tuple[bytes, ...]


def read_mounts():
    """Read the deleting process's mount namespace, including same-device binds.

    Mount IDs and mount paths, unlike st_dev or ismount(), distinguish bind
    mounts. Decode the kernel's path escapes before comparing path components.
    Never treat an unreadable, empty or malformed table as absence of mounts.
    """
    try:
        if not sys.platform.startswith("linux"):
            raise ValueError("Linux mount information is required")
        data = MOUNTINFO.read_bytes()
        if not data or not data.endswith(b"\n"):
            raise ValueError("empty or incomplete mount table")
        mounts, ids = [], set()
        for line in data[:-1].split(b"\n"):
            fields = line.split(b" ")
            separator = fields.index(b"-", 6)
            if (len(fields) != separator + 4 or any(not f or b"\0" in f or b"\t" in f for f in fields)
                    or re.fullmatch(rb"[1-9][0-9]*", fields[0]) is None
                    or re.fullmatch(rb"[0-9]+", fields[1]) is None
                    or re.fullmatch(rb"[0-9]+:[0-9]+", fields[2]) is None
                    or fields[0] in ids):
                raise ValueError("invalid mount record")
            ids.add(fields[0])
            # Unknown optional fields before '-' are permitted by mountinfo.
            paths = []
            for raw in fields[3:5]:
                if re.search(rb"\\(?!040|011|012|134)", raw):
                    raise ValueError("invalid mount path escape")
                decoded = os.fsdecode(re.sub(rb"\\(040|011|012|134)",
                    lambda match: bytes([int(match[1], 8)]), raw))
                path = Path(decoded)
                if (not path.is_absolute() or decoded.startswith("//")
                        or str(path) != decoded or ".." in path.parts):
                    raise ValueError("invalid mount path")
                paths.append(path)
            options, super_options = fields[5].split(b","), fields[separator + 3].split(b",")
            for values in (options, super_options):
                if (any(not value for value in values) or len(set(values)) != len(values)
                        or len({b"ro", b"rw"} & set(values)) != 1):
                    raise ValueError("invalid mount options")
            mounts.append(Mount(int(fields[0]), int(fields[1]), tuple(map(int, fields[2].split(b":"))),
                                paths[0], paths[1], tuple(options), tuple(fields[6:separator]),
                                fields[separator + 1], fields[separator + 2], tuple(super_options)))
        by_id = {m.mount_id: m for m in mounts}
        if not any(m.point == Path("/") for m in mounts):
            raise ValueError("missing mount namespace root")
        for mount in mounts:
            if (mount.point == Path("/") and mount.parent in by_id
                    and by_id[mount.parent].point != Path("/")):
                raise ValueError("inconsistent namespace root parent")
            seen, current = set(), mount
            while current.point != Path("/"):
                if current.mount_id in seen or current.parent not in by_id:
                    raise ValueError("incomplete or cyclic mount topology")
                seen.add(current.mount_id)
                parent = by_id[current.parent]
                if not current.point.is_relative_to(parent.point):
                    raise ValueError("inconsistent mount parent")
                current = parent
        return tuple(sorted(mounts, key=lambda m: m.mount_id))
    except (OSError, ValueError, IndexError):
        raise TaskError("Cannot read or parse Linux mount information; forced cleanup refused") from None


def mount_points():
    return [mount.point for mount in read_mounts()]


def check_mounts(paths):
    """Refuse boundaries at or below a deletion root, including file bind mounts."""
    for point in mount_points():
        if any(point.is_relative_to(path) for path in paths):
            raise TaskError(f"Cleanup target contains a mount boundary: {point}; nothing further was removed")


def directory_identity(path):
    """No-follow root identity; a dangling symlink is never absence."""
    if not path.is_absolute() or path.resolve() != path:
        raise TaskError(f"Cleanup path is aliased or not absolute: {path}")
    try:
        entry = path.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISDIR(entry.st_mode) or path.is_mount():
        raise TaskError(f"Cleanup path is not an ordinary directory: {path}")
    return [entry.st_dev, entry.st_ino]


def check_contents(path, *, git_root=None):
    """Do not cross mounts or consume another nested repository/worktree."""
    check_mounts([path])
    owned_git_dir = git_root / ".git" if git_root is not None else None
    def failed(error):
        raise error
    for root, dirs, files in os.walk(path, followlinks=False, onerror=failed):
        parent = Path(root)
        if ".git" in dirs + files and parent != git_root:
            raise TaskError(f"Nested repository in cleanup target: {parent}; inspect it manually")
        # Bare repositories have no .git entry. Refuse their metadata layout
        # without loading nested Git configuration or following directory links.
        # Only the exact, separately verified integration Git directory is owned;
        # keep walking below it so another repository inside it is still refused.
        if parent != owned_git_dir and "HEAD" in files and {"objects", "refs"} <= set(dirs):
            raise TaskError(f"Nested bare repository in cleanup target: {parent}; inspect it manually")
        for name in dirs:
            if (parent / name).is_mount():
                raise TaskError("Cleanup target contains a mount; nothing further was removed")


class DisposalStore(PublicationStore):
    def __init__(self, git, identifier, execution_id=None):
        self.path = git.disposal_file(identifier, execution_id)
        self.directory = self.path.parent
        if self.directory.resolve() != self.directory:
            raise TaskError("Forced cleanup metadata path is aliased")
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        if os.name != "nt":
            # The retry record must survive creation of either new parent too.
            for directory in (self.directory.parent, self.directory.parent.parent):
                fd = os.open(directory, os.O_RDONLY)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)

    def read(self):
        return self.read_path(self.path)

    @staticmethod
    def read_path(path):
        try:
            if path.is_symlink() or path.stat().st_size > 4 * 1024 * 1024:
                raise ValueError("invalid journal")
            record = json.loads(path.read_text(), object_pairs_hook=unique_object)
            if record["version"] == 1:
                if path.name != "state.json":
                    raise ValueError("invalid legacy journal identity")
            elif record["version"] == 2:
                if (str(UUID(record["execution_id"])) != record["execution_id"]
                        or path.name != f"{record['execution_id']}.json"):
                    raise ValueError("invalid journal identity")
            else:
                raise ValueError("invalid journal version")
            if record["state"] not in {"pending", "complete"}:
                raise ValueError("invalid journal")
            return record
        except FileNotFoundError:
            return None
        except (OSError, ValueError, KeyError, TypeError, AttributeError, UnicodeError, RecursionError):
            raise TaskError("Cannot read forced cleanup journal; preserve it and inspect manually") from None

    @ownership_operation
    def write(self, value, **kwargs):
        if len(json.dumps(value, ensure_ascii=True).encode()) > 4 * 1024 * 1024:
            raise TaskError("Forced cleanup exceeds bounded audit storage; nothing further was removed")
        super().write(value, **kwargs)


def cached_publication_namespaces(checkout, keys):
    """Prove name-preserving fetch namespaces; custom mappings require inspection.

    Current refspecs cannot reconstruct historical fetch mappings. In particular,
    do not discard negative, empty or additional values as though they erased
    publication evidence. Unrecognized mappings fail closed even outside
    refs/remotes, where a renamed task branch could otherwise be missed entirely.
    """
    namespaces = set()
    remotes = set(checkout.command("remote").splitlines())
    for key in set(keys):
        if not (key.startswith("remote.") and key.endswith(".fetch")):
            continue
        remote = key[len("remote."):-len(".fetch")]
        namespace = f"refs/remotes/{remote}/"
        expected = f"refs/heads/*:{namespace}*"
        values = checkout.command("config", "--null", "--get-all", key)
        if (remote not in remotes or not values.endswith("\0")
                or any(value.removeprefix("+") != expected for value in values[:-1].split("\0"))):
            raise TaskError("Custom or ambiguous Git fetch refspecs prevent excluding cached publication; disposal refused")
        checkout.command("check-ref-format", "--refspec-pattern", namespace + "*")
        namespaces.add(namespace)
    return namespaces


def check_cached_publication(git, identifier, namespaces):
    for ref in git.command("for-each-ref", "--format=%(refname)", "refs/remotes").splitlines():
        branches = [ref.removeprefix(namespace) for namespace in namespaces if ref.startswith(namespace)]
        if any(belongs_to_issue(branch, identifier) for branch in branches):
            raise TaskError("Retained remote-tracking task refs are publication evidence; disposal refused")
        if len(branches) != 1:
            raise TaskError("Retained remote-tracking publication namespace is unknown or ambiguous; disposal refused")


def never_published(git, base, identifier, branch, publication=None, *, worktree=None):
    if publication is not None and (publication["publication_history"] is not None
                                    or publication["intent"] is not None):
        raise TaskError("Publication history or intent makes forced disposal ambiguous")
    if publication is not None:
        expected = dict(issue=identifier, repository=str(git.repo), worktree=str(worktree),
                        branch=branch, base_branch=base)
        for key in ("acceptance", "rebase"):
            evidence = publication[key]
            if evidence is not None:
                binding = evidence.get("binding") if key == "rebase" else evidence
                if not isinstance(binding, dict) or any(binding.get(k) != v for k, v in expected.items()):
                    raise TaskError("Private review/integration identity differs from the selected execution")
    if git.remote_branches(identifier):
        raise TaskError("A live remote task branch is publication evidence; forced disposal refused")
    checkouts = (git, Git(worktree)) if worktree is not None else (git,)
    namespaces = set()
    for checkout in checkouts:
        # Check key presence, including empty/multiple values, in each effective
        # config: worktreeConfig and conditional includes can differ from main.
        keys = checkout.command("config", "--null", "--name-only", "--list").split("\0")
        if any(f"branch.{branch}.{key}" in keys for key in ("remote", "merge", "pushremote")):
            raise TaskError("Task branch tracking configuration is publication evidence; disposal refused")
        # Custom push refspecs can publish under names absent from the issue-ref
        # and same-branch PR queries. Do not infer historical source/destination
        # relationships from today's refs. Require default push naming instead,
        # refusing key presence even for empty, malformed or multivalued specs.
        if any(key.startswith("remote.") and key.endswith(".push") for key in keys):
            raise TaskError("Configured push refspecs make publication exclusion ambiguous; forced disposal refused")
        namespaces.update(cached_publication_namespaces(checkout, keys))
    check_cached_publication(git, identifier, namespaces)
    identity = remote_identity(git, base, branch)
    repository = identity["repository"]
    # Include worktree-specific configuration and every possible push destination.
    for checkout in checkouts:
        verify_remote_identity(checkout, base, branch, identity)
        for remote in checkout.command("remote").splitlines():
            for flags in (("--all",), ("--push", "--all")):
                urls = checkout.command("remote", "get-url", *flags, remote).splitlines()
                if len(urls) != 1 or (repository_name(urls[0]) or "").casefold() != repository.casefold():
                    raise TaskError("Cannot exclude publication through another remote destination; disposal refused")
    if list(pull_requests(repository, branch)):
        raise TaskError("Task PR history is publication evidence; forced disposal refused")
    return identity


def scope_digest(git, path):
    scope = git.scope_file(path)
    if not os.path.lexists(scope):
        return None
    if scope.is_symlink() or not scope.is_file():
        raise TaskError("Workspace scope metadata is not a regular file")
    return sha256(scope.read_bytes()).hexdigest()


def merged_publication(git, base, identifier, target, saved):
    """A separate positive proof, never an exemption from never_published()."""
    identity = remote_identity(git, base, target.branch)
    verify_remote_identity(Git(target.path), base, target.branch, identity)
    binding = dict(issue=identifier, repository=str(git.repo), worktree=str(target.path),
                   branch=target.branch, base_branch=base)
    for key in ("acceptance", "rebase"):
        evidence = saved[key]
        if evidence is not None:
            actual = evidence.get("binding") if key == "rebase" else evidence
            if not isinstance(actual, dict) or any(actual.get(k) != v for k, v in binding.items()):
                raise TaskError("Private review/integration identity differs from the selected execution")
    history = saved["publication_history"]
    if history == "unknown":
        raise TaskError("Legacy publication intent lacks exact execution identity; finish task pr reconciliation first")
    verify_publication_history(history, binding, identity)
    if saved["intent"] is not None:
        intent = saved["intent"]
        if (saved["acceptance"] is None or intent.get("remote") != identity["remote"]
                or intent.get("repository") != identity["repository"]
                or intent.get("pass_id") != saved["acceptance"].get("pass_id")):
            raise TaskError("Private publication intent differs from the selected execution")
    proof = asdict(git.cleanup_merge(base, target.branch, identifier))
    if (history is not None and history["head"] != proof["branch_commit"]
            or saved["intent"] is not None and saved["intent"].get("publishing_head") != proof["branch_commit"]):
        raise TaskError("Private publication head differs from the proven merged tip; reconcile that publication before cleanup")
    return identity, dict(proof=proof, scope_sha256=scope_digest(git, target.path))


def cleanup_recovery(identifier, repo):
    return (f"Run task contexts {identifier} --all, git -C {shlex.quote(str(repo))} worktree list, "
            f"and herdr worktree list --cwd {shlex.quote(str(repo))} to locate the conflicting execution. "
            "Quit its agents and resolve the reported Git/Herdr identity or merge conflict before retrying "
            f"task cleanup {identifier} --force. For multiple candidates, select a merged execution with "
            f"task cleanup {identifier} --force --branch <exact-local-branch>, then clean the remaining execution. "
            "The selector still requires one exact Git/Herdr match and positive merge proof; "
            "it does not infer slice scope or accept a path or label as authorization. "
            "Keep private evidence intact; do not create scope metadata to bypass recovery checks.")


def runtime_released(context):
    """Retirement ends a runtime binding, not necessarily its artifact claim."""
    return (context["state"] == "retired" and bool(context["retired_at"])
            and all(context[k] is None for k in ("terminal_id", "session_id", "session_kind", "herdr_session"))
            and context["resumability"] == "unknown")


def resource_paths(record):
    return [Path(record["path"]), Path(record["git_dir"]), *(Path(r["path"]) for r in record["roots"])]


def normal_retirements(git):
    home = git.check_repository() / "agentic-workflows-cleanup"
    for path in sorted(home.glob("*.json")):
        try:
            data = json.loads(path.read_text(), object_pairs_hook=unique_object)
            pending = git.load_retirement(path.stem, data["base"])
            if pending is None:
                raise ValueError("missing retirement")
        except (OSError, ValueError, KeyError, TypeError):
            raise TaskError("Cannot verify pending workspace retirement; preserve its evidence") from None
        yield pending


def retirement_paths(pending):
    return [pending.path, *([pending.git_dir] if pending.git_dir else []), *(Path(p) for p in pending.claims)]


@ownership_operation
def ownership_inventory(registry, repository, *, contexts=None):
    """Discover existing workflow evidence, never arbitrary host files/processes.

    Registry repositories locate private Git stores, including preparing passes
    without a G row and pending journals whose worktree has already disappeared.
    Missing old repositories cannot release their retained context claims.
    Inventory snapshots take the short gate; admission must hold it exclusively
    through the durable reservation write.
    """
    contexts = registry.list(include_retired=True) if contexts is None else contexts
    repositories = {Path(repository), *(Path(p) for p in registry.repositories()),
                    *(Path(c["repository"]) for c in contexts if c["repository"])}
    journals, stores, trees = [], [], []
    for repo in sorted(repositories):
        if not repo.exists():
            continue
        git = Git(repo)
        common = git.check_repository()
        home = common / "agentic-workflows-cleanup"
        if home.resolve() != home:
            raise TaskError("Workflow disposal metadata is aliased")
        removing_paths, removing_dirs = set(), set()
        for pending in normal_retirements(git):
            trees.extend((p, None) for p in retirement_paths(pending))
            if pending.git_dir is not None:
                removing_paths.add(pending.path)
                removing_dirs.add(pending.git_dir)
        for directory in sorted(home.glob("*.discard")):
            identifier = directory.name.removesuffix(".discard")
            if not re.fullmatch(r"[A-Z][A-Z0-9]*-[1-9][0-9]*", identifier) or directory.resolve() != directory:
                raise TaskError("Workflow disposal claim identity is invalid")
            for path in git.disposal_files(identifier):
                saved = DisposalStore.read_path(path)
                if saved is None:
                    raise TaskError("Workflow disposal claim disappeared")
                validate_record(saved, SimpleNamespace(identifier=identifier),
                                SimpleNamespace(base_branch=saved.get("base")), repo, registry)
                journals.append(saved)
                if saved["state"] == "pending" and saved["worktree_state"] in {"removing", "removed"}:
                    removing_paths.add(Path(saved["path"]))
                    removing_dirs.add(Path(saved["git_dir"]))
                    trees.extend((Path(saved[k]), None) for k in ("path", "git_dir"))
        # A published reservation replaces metadata that Git may currently be
        # deleting. Keep its resource claims, but never inspect half-deleted
        # checkpoints, integration records, .git pointers or private directories.
        directories = [common]
        private = common / "worktrees"
        if private.exists():
            if private.resolve() != private:
                raise TaskError("Workflow private Git metadata is aliased")
            try:
                private_directories = sorted(private.iterdir())
            except FileNotFoundError:
                # Git may remove this container with the final linked worktree.
                # Its in-flight resources remain in the journal claims above.
                private_directories = []
            for directory in private_directories:
                if directory in removing_dirs:
                    continue
                if directory.is_symlink() or not directory.is_dir():
                    raise TaskError("Workflow private Git metadata is ambiguous")
                directories.append(directory)
                trees.append((directory, None))
        for tree in git.worktrees():
            path = Path(tree["worktree"])
            trees.append((path, None if path in removing_paths else str(repo)))
        stores.extend(directories)
    return contexts, journals, stores, trees


def check_unreserved(registry, repository, worktree, *, endpoint=None, workspace_id=None, terminal_id=None, contexts=None):
    """A crashed disposal still reserves its resources after its OS lock exits."""
    if repository is None:
        # Unbound allocations have no filesystem resource to acquire.
        repository = registry.path.parent / "absent-repository"
    contexts, journals, _, _ = ownership_inventory(registry, repository, contexts=contexts)
    # Ordinary merged cleanup retains its existing exact Herdr retirement
    # record. It too reserves the path until context retirement is complete.
    repositories = {Path(repository), *(Path(p) for p in registry.repositories()),
                    *(Path(c["repository"]) for c in contexts if c["repository"])}
    for repo in sorted(repositories):
        if not repo.exists():
            continue
        git = Git(repo)
        for pending in normal_retirements(git):
            if (worktree is not None and any(overlapping(Path(worktree), p, read_mounts()) for p in retirement_paths(pending))
                    or workspace_id is not None and workspace_id == pending.workspace_id):
                raise TaskError("Pending workspace retirement reserves this resource; finish task cleanup before acquiring a claim")
    pending = [j for j in journals if j["state"] == "pending"]
    if not pending:
        return
    mounts = read_mounts()
    for journal in pending:
        if (worktree is not None and any(overlapping(Path(worktree), path, mounts)
                                         for path in resource_paths(journal))
                or endpoint is not None and endpoint == journal["endpoint"]
                and (workspace_id is not None and workspace_id == journal["workspace_id"]
                     or terminal_id is not None and any(terminal_id == c["terminal_id"]
                                                       for c in journal["contexts"]))):
            raise TaskError("Pending cleanup reserves this resource; finish its exact disposal before acquiring a claim")


def context_claimed(context, journals):
    if not runtime_released(context):
        return True
    if context["role"] != "integration":
        return False
    # A retired G runtime continues to own its artifacts until an exact frozen
    # disposal releases them. A timestamp, issue ID or matching path is not enough.
    return not any(j["state"] == "complete" and context in j["contexts"]
                   and any(i["context_id"] == context["context_id"]
                           and i["checkout"] == context["worktree"] for i in j["integrations"])
                   for j in journals)


def selected_contexts(registry, identifier, journals=()):
    return [c for c in registry.list(identifier, include_retired=True)
            if context_claimed(c, journals)]


def disposed_context_history(registry, identifier, repository, *, excluding=()):
    """Admit history only when exact completed disposal tombstones release it.

    Retirement alone does not prove disposal. Use the validated inventory so
    malformed, pending, foreign, or changed claims cannot become restart proof.
    Completed paths are historical selectors, not claims on a new checkout.
    """
    history = [c for c in registry.list(identifier, include_retired=True) if c["context_id"] not in excluding]
    if not history:
        return history
    _, journals, _, _ = ownership_inventory(registry, repository)
    selected = [j for j in journals if j["issue"] == identifier]
    if (any(j["state"] != "complete" or j["repository"] != str(repository) for j in selected)
            or any(c["repository"] != str(repository) or not runtime_released(c)
                   or sum(c in j["contexts"] for j in selected) != 1 for c in history)):
        raise TaskError("Context history lacks exact completed disposal; finish pending cleanup or inspect conflicting claims")
    return history


def overlapping(left, right, mounts):
    """Compare registered claims, including two registered views of one subtree.

    No external alias enumeration or host process reference search is performed.
    """
    left, right = left.resolve(), right.resolve()
    if left.is_relative_to(right) or right.is_relative_to(left):
        return True
    def location(path):
        matches = [m for m in mounts if path.is_relative_to(m.point)]
        depth = max(len(m.point.parts) for m in matches)
        closest = [m for m in matches if len(m.point.parts) == depth]
        if len(closest) != 1:
            raise TaskError("Registered resource mount identity is ambiguous")
        mount = closest[0]
        return (mount.device, mount.filesystem), mount.root / path.relative_to(mount.point)
    a, ap = location(left)
    b, bp = location(right)
    return a == b and (ap.is_relative_to(bp) or bp.is_relative_to(ap))


def integration_evidence(store, registry, binding, contexts):
    records = IntegrationStore(publication_store=store).disposal_records()
    roots, tombstones, claimed = [], [], set()
    for record in records:
        if record.get("context") is not None and not isinstance(record["context"], dict):
            raise TaskError("Retained integration context evidence is malformed")
        if any(record["binding"][k] != v for k, v in binding.items() if k != "workspace_id"):
            raise TaskError("Retained integration belongs to a different task execution")
        checkout = Path(record["checkout"])
        expected = registry.path.parent.resolve() / "integrations" / record["pass_id"] / "checkout"
        if checkout != expected or checkout.resolve() != checkout:
            raise TaskError("Retained integration is outside its exact workflow-owned directory")
        context_id = (record.get("context") or {}).get("context_id")
        matching = [c for c in contexts if c["context_id"] == context_id]
        if context_id is not None:
            if (len(matching) != 1 or matching[0]["role"] != "integration"
                    or matching[0]["worktree"] != str(checkout) or context_id in claimed
                    or matching[0]["workspace_id"] != record["binding"]["workspace_id"]
                    or any(matching[0][key] != record["options"][option] for key, option in
                           (("agent", "kind"), ("model", "model"), ("mode", "mode")))
                    or record["state"] == "abandoned" and matching[0] not in (
                        abandoned_context(record), registry.disposal_tombstone(
                            abandoned_context(record), matching[0]["retired_at"]))):
                raise TaskError("Integration provenance and context identity disagree")
            claimed.add(context_id)
        elif (record["state"] not in {"preparing", "uncertain"}
              or record["binding"]["workspace_id"] != binding["workspace_id"]):
            raise TaskError("Retained integration context identity is missing")
        parent = checkout.parent
        if parent.exists() and set(p.name for p in parent.iterdir()) - {"checkout", "result.json"}:
            raise TaskError("Unexpected files beside retained integration checkout; disposal refused")
        checkout_identity = directory_identity(checkout)
        git_identity = None
        if checkout_identity is not None:
            isolated = Git(checkout)
            if (Path(isolated.command("rev-parse", "--absolute-git-dir").strip()) != checkout / ".git"
                    or (checkout / ".git").is_symlink()
                    or isolated.command("remote").strip()
                    or len(isolated.worktrees()) != 1
                    or history_identity(checkout) != record.get("history")):
                raise TaskError("Retained integration Git identity changed; disposal refused")
            git_identity = directory_identity(checkout / ".git")
        roots.append(dict(path=str(parent), identity=directory_identity(parent),
                          checkout=str(checkout), checkout_identity=checkout_identity,
                          git_identity=git_identity, history_sha256=evidence_digest(record.get("history")),
                          state="pending"))
        tombstone = integration_tombstone(record)
        output = Path(record.get("output", parent / "result.json"))
        if "output" in record:
            if (output.name != "result.json" or output.parent == parent
                    or not re.fullmatch(re.escape(f"task-integration-{record['pass_id']}-") + r"[A-Za-z0-9_-]+",
                                        output.parent.name)):
                raise TaskError("Integration output directory identity is ambiguous")
            if output.parent.exists() and set(p.name for p in output.parent.iterdir()) - {"result.json"}:
                raise TaskError("Integration output directory contains unrelated artifacts")
            roots.append(dict(path=str(output.parent), identity=directory_identity(output.parent),
                              checkout=None, state="pending"))
        if output.is_symlink():
            raise TaskError("Integration completion output is a symlink")
        if output.exists():
            if not output.is_file() or output.stat().st_size > 1024 * 1024:
                raise TaskError("Integration completion output is not bounded")
            tombstone["output_sha256"] = sha256(output.read_bytes()).hexdigest()
        tombstones.append(tombstone)
    if claimed != {c["context_id"] for c in contexts if c["role"] == "integration"}:
        raise TaskError("Integration context lacks durable provenance; disposal refused")
    return roots, tombstones


def separate_workspace(record, herdr, entry):
    """An explicit selection may leave another independently bound slice open."""
    git = Git(herdr.repo)
    location = entry.get("worktree") or {}
    trees = [tree for tree in git.worktrees() if tree["worktree"] == location.get("checkout_path")]
    if len(trees) != 1:
        return False
    branch = trees[0].get("branch", "").removeprefix("refs/heads/")
    if branch == record["branch"] or not belongs_to_issue(branch, record["issue"]):
        return False
    target = herdr.resolve_task(git, record["issue"], branch=branch, include_remotes=False, disposing=True)
    if (target is None or str(target.path) != location.get("checkout_path")
            or target.open_workspace_id != entry["workspace_id"]):
        return False
    # A different registered path alone cannot prove disjoint ownership: its
    # checkout might point back into the selected execution's private Git dir.
    git.check_cleanup_target(record["base"], target, record["issue"], require_clean=False,
                             allow_missing_scope=True)
    herdr.retirement(target, record["issue"], record["base"])
    return True


def runtime(record, herdr, identities, *, stop_agents=False):
    """Exact task runtime; force may admit positively idle, registered agents."""
    if identities.endpoint() != record["endpoint"]:
        raise TaskError("Selected execution belongs to another Herdr endpoint")
    contexts = record["contexts"]
    allowed = {record["path"], *(i["checkout"] for i in record["integrations"])}
    terminals = {c["terminal_id"] for c in contexts if c["terminal_id"]}
    pane_ids = {c["pane_id"] for c in contexts if c["pane_id"]}
    panes, shells, processes = [], {}, {}
    previous = record.get("runtime") or {}
    owned = previous.get("process_proof") == 2
    for pane in identities.snapshot():
        paths = [pane.get(k) for k in ("cwd", "foreground_cwd") if pane.get(k)]
        related = (pane["workspace_id"] == record["workspace_id"]
                   or pane["terminal_id"] in terminals or pane["pane_id"] in pane_ids
                   or any(any(Path(p).is_relative_to(Path(root)) for root in allowed) for p in paths))
        if not related:
            continue
        if pane["workspace_id"] != record["workspace_id"] or pane.get("cwd") not in allowed:
            raise TaskError("Task terminal moved or unrelated work occupies the workspace; disposal refused")
        matches = [c for c in contexts if c["terminal_id"] == pane["terminal_id"]]
        if any(p["terminal_id"] == pane["terminal_id"] for p in panes):
            raise TaskError("Herdr terminal identity is ambiguous; disposal refused")
        if matches:
            if (len(matches) != 1 or matches[0]["pane_id"] != pane["pane_id"]
                    or matches[0]["worktree"] != pane["cwd"]):
                raise TaskError("Task context terminal identity changed; disposal refused")
        elif pane["pane_id"] in pane_ids or pane.get("agent") or pane["cwd"] != record["path"]:
            raise TaskError("Unregistered or replaced execution pane; disposal refused")
        try:
            info = identities.command("pane", "process-info", "--pane", pane["pane_id"])["process_info"]
            processes[pane["pane_id"]] = dict(info=info, cwd=Path(pane["cwd"]), agent=None)
            if stop_agents and pane.get("agent"):
                if (len(matches) != 1 or matches[0]["role"] not in {"implementation", "review"}
                        or matches[0]["workspace_id"] != pane["workspace_id"]
                        or matches[0]["tab_id"] != pane["tab_id"] or matches[0]["agent"] != pane["agent"]):
                    raise TaskError(f"Pane {pane['pane_id']} is not an exact registered implementation/reviewer agent")
                context = matches[0]
                workspace = Workspace(path=Path(pane["cwd"]), branch=record["branch"],
                    workspace_id=pane["workspace_id"], tab_id=pane["tab_id"], pane_id=pane["pane_id"], action="cleanup")
                adapter_for(AgentOptions(context["agent"], context["model"], context["mode"])).verify_cleanup_idle(
                    workspace, context_reference(context), pane)
                processes[pane["pane_id"]]["agent"] = context["agent"]
                owned = True
                panes.append({k: pane[k] for k in ("workspace_id", "pane_id", "terminal_id", "cwd")})
                continue
            pid = info["shell_pid"]
            if (info["pane_id"] != pane["pane_id"] or type(pid) is not int or pid <= 0
                    or type(info["foreground_process_group_id"]) is not int
                    or info["foreground_process_group_id"] != pid
                    or not isinstance(info["foreground_processes"], list)
                    or any(type(p["pid"]) is not int for p in info["foreground_processes"])
                    or [p["pid"] for p in info["foreground_processes"]] != [pid] or pid in shells):
                raise ValueError("not a unique stopped shell")
            argv = info["foreground_processes"][0]["argv"]
            shell_executable(argv)
        except (KeyError, ValueError, TypeError):
            raise TaskError(f"Pane {pane['pane_id']} is live or uncertain; quit its agent before cleanup. "
                            "An idle agent is not proof of a stopped process") from None
        shells[pid] = dict(cwd=Path(pane["cwd"]), argv=argv)
        panes.append(dict({k: pane[k] for k in ("workspace_id", "pane_id", "terminal_id", "cwd")}, shell_argv=argv))
    entries = herdr.workspaces()
    if record["workspace_id"] is not None:
        state = HerdrRetirement(record["issue"], record["base"], record["branch"],
                                Path(record["path"]), record["workspace_id"])
        common = Path(Git(herdr.repo).command("rev-parse", "--path-format=absolute", "--git-common-dir").strip())
        entry = herdr._retirement_workspace(state, entries, allow_absent=True, common=common)
        claimed = re.fullmatch(r"([A-Z][A-Z0-9]*-[1-9][0-9]*)(?:\s*/\s*.+)?", entry["label"],
                               re.IGNORECASE) if entry else None
        if claimed and claimed.group(1).upper() != record["issue"]:
            raise TaskError("Herdr workspace identifies another issue; disposal refused")
        if entry is not None and (type(entry.get("pane_count")) is not int or entry["pane_count"] != len(panes)
                or entry["label"] not in {record["issue"], record["workspace_label"]}):
            raise TaskError("Herdr workspace membership changed or snapshot is incomplete")
        if entry is not None and "merged" in record and any(
                entry["worktree"][key] != value for key, value in (
                    ("repo_root", record["repository"]), ("repo_key", str(common)),
                    ("checkout_path", record["path"]))):
            raise TaskError("Merged cleanup requires canonical Herdr repository and checkout paths")
        if entry is None and panes:
            raise TaskError("Herdr workspace disappeared but its terminals remain")
    else:
        entry = None
    for other in entries:
        if other["workspace_id"] == record["workspace_id"]:
            continue
        location = other.get("worktree") or {}
        issue_label = re.fullmatch(re.escape(record["issue"]) + r"(?:\s*/\s*.+)?", other["label"], re.IGNORECASE)
        label_claim = other["label"] == record["issue"] or "merged" in record and issue_label
        if label_claim and "selected_branch" in record and separate_workspace(record, herdr, other):
            label_claim = False
        if label_claim or location.get("checkout_path") in allowed:
            raise TaskError("Another Herdr workspace claims this execution; disposal refused")
    closed_shells = previous.get("shells") if entry is None else None
    paths = [Path(record["path"]), Path(record["git_dir"]), *(Path(r["path"]) for r in record["roots"])]
    if owned:
        if any(Path.cwd().is_relative_to(path) for path in paths):
            raise TaskError("Run forced cleanup from outside all execution deletion targets")
        if processes:
            herdr.check_agent_retirement()
        frozen = previous.get("families", {})
        if frozen:
            original = {p["pane_id"]: p for p in previous["panes"]}
            for pane in panes:
                saved = original.get(pane["pane_id"], {})
                if any(pane[k] != saved.get(k) for k in ("workspace_id", "pane_id", "terminal_id", "cwd")):
                    raise TaskError("Owned agent pane was replaced during retirement")
        families = owned_pane_processes(processes, expected=frozen,
                                        closing=record["workspace_state"] in {"closing", "closed"})
        for pane in panes:
            pane["shell_argv"] = families[pane["pane_id"]]["shell_argv"]
        return dict(process_proof=2, workspace=entry is not None, panes=sorted(panes, key=lambda p: p["pane_id"]),
                    shells={str(f["shell"]): f["members"][str(f["shell"])]["started"] for f in families.values()},
                    families=families)
    proof = stopped_execution(paths, shells, closed_shells=closed_shells)
    # Persist that these PIDs were proved to be session leaders. On a retry
    # after closure, their session/group IDs are still execution evidence.
    return dict(process_proof=1, workspace=entry is not None,
                panes=sorted(panes, key=lambda p: p["pane_id"]), shells=proof)


def prepare_record(issue, project, repo, git, herdr, registry, identities, target, publication, execution_id,
                   *, branch=None):
    saved = publication.read()
    merged = None
    missing_scope = scope_digest(git, target.path) is None
    if missing_scope or branch is not None:
        if missing_scope and target.open_workspace_id is None:
            raise TaskError("Merged orphan recovery requires an exact open Herdr workspace ID. "
                            f"Open the registered checkout with herdr worktree open --cwd {shlex.quote(str(repo))} "
                            f"--path {shlex.quote(str(target.path))}, quit its agents, then retry --force")
        remote, merged = merged_publication(git, project.base_branch, issue.identifier, target, saved)
    else:
        try:
            remote = never_published(git, project.base_branch, issue.identifier, target.branch, saved, worktree=target.path)
        except TaskError as unpublished_error:
            try:
                remote, merged = merged_publication(git, project.base_branch, issue.identifier, target, saved)
            except TaskError as merged_error:
                raise TaskError(f"{unpublished_error}. Merged recovery also refused: {merged_error}") from None
    git.check_cleanup_target(project.base_branch, target, issue.identifier, require_clean=False,
                             allow_missing_scope=merged is not None)
    _, journals, _, _ = ownership_inventory(registry, repo)
    integration_ids = {(i.get("context") or {}).get("context_id")
                       for i in IntegrationStore(publication_store=publication).disposal_records()}
    contexts = [c for c in selected_contexts(registry, issue.identifier, journals)
                if c["role"] != "integration" and c["worktree"] == str(target.path)
                or c["context_id"] in integration_ids]
    endpoint = identities.endpoint()
    live_workspaces = {w["workspace_id"] for w in herdr.workspaces()}
    for context in contexts:
        if (context["repository"] != str(repo) or context["endpoint"] != endpoint
                or context["workspace_id"] != target.open_workspace_id and context["workspace_id"] in live_workspaces
                or context["role"] != "integration" and context["worktree"] != str(target.path)):
            raise TaskError("Issue contexts do not identify a single local task execution")
    binding = dict(issue=issue.identifier, repository=str(repo), worktree=str(target.path), branch=target.branch,
                   base_branch=project.base_branch, endpoint=endpoint, workspace_id=target.open_workspace_id)
    roots, integrations = integration_evidence(publication, registry, binding, contexts)
    loop_evidence = None
    loop_store = LoopStore(target.path)
    if loop_store.path.exists():
        checkpoint, pause = loop_store.read()
        if checkpoint.get("delivery"):
            delivery = checkpoint["delivery"]
            directory = Path(delivery["directory"])
            present = directory_identity(directory) is not None
            if present:
                check_directory(delivery)
            loop_evidence = dict(checkpoint_sha256=evidence_digest([checkpoint, pause]), delivery=delivery,
                                 files={p.name: sha256(read_private(p)).hexdigest() for p in directory.iterdir()} if present else {})
            roots.append(dict(path=str(directory), identity=directory_identity(directory), checkout=None, state="pending"))
    retirement = herdr.retirement(target, issue.identifier, project.base_branch)
    if git.load_retirement(issue.identifier, project.base_branch) is not None:
        raise TaskError("Completed-task cleanup has pending retirement evidence; inspect it before forced disposal")
    record = dict(version=2, execution_id=execution_id, state="pending", issue=issue.identifier,
                  repository=str(repo), base=project.base_branch,
                  branch=target.branch, path=str(target.path), identity=directory_identity(target.path),
                  head=git.command("rev-parse", f"refs/heads/{target.branch}").strip(),
                  git_dir=str(publication.directory), git_identity=directory_identity(publication.directory),
                  remote=remote, endpoint=endpoint,
                  workspace_id=target.open_workspace_id, workspace_label=None,
                  contexts=contexts, integrations=integrations, roots=roots,
                  publication_sha256=evidence_digest(saved), at=now(),
                  workspace_state="pending", worktree_state="pending", branch_state="pending")
    if merged is not None:
        record["merged"] = merged
    if branch is not None:
        record["selected_branch"] = branch
    if loop_evidence is not None:
        record["loop"] = loop_evidence
    if retirement:
        record["workspace_label"] = next(w["label"] for w in herdr.workspaces()
                                          if w["workspace_id"] == retirement.workspace_id)
    record["runtime"] = runtime(record, herdr, identities, stop_agents=True)
    validate_record(record, issue, project, repo, registry)
    verify_contexts(record, registry)
    check_task(record, git, herdr, closed=target.open_workspace_id is None)
    check_roots(record, git)
    return record


def normal_stopped(record, herdr, identities, base, branch):
    """Apply the existing selected Linux execution policy to ordinary cleanup."""
    if record["workspace_id"] is None and not record["contexts"]:
        # Exact Herdr selection found no open workspace or registered execution.
        return stopped_execution(resource_paths(record), {})
    if "base" not in record:
        record.update(base=base, branch=branch, endpoint=identities.endpoint(), workspace_label="")
        if any(c["endpoint"] != record["endpoint"] for c in record["contexts"]):
            raise TaskError("Selected execution belongs to another Herdr endpoint")
        matches = [w for w in herdr.workspaces() if w["workspace_id"] == record["workspace_id"]]
        if len(matches) == 1:
            record["workspace_label"] = matches[0]["label"]
    return runtime(record, herdr, identities)


def removal_retirement(target, identifier, base):
    """Preserve private-store claims while ordinary Git removal is in flight."""
    store = PublicationStore(target.path)
    claims = set()
    for record in IntegrationStore(publication_store=store).disposal_records():
        claims.add(str(Path(record["checkout"]).parent))
        if record.get("output"):
            claims.add(str(Path(record["output"]).parent))
    loop = LoopStore(target.path)
    if loop.path.exists():
        delivery = loop.read()[0].get("delivery")
        if delivery:
            claims.add(delivery["directory"])
    return HerdrRetirement(identifier, base, target.branch, target.path, target.open_workspace_id,
                           store.directory, tuple(sorted(claims)))


def validate_record(record, issue, project, repo, registry):
    """Validate durable selectors even when their original checkout is absent."""
    try:
        fields = {"version", "state", "issue", "repository", "base", "branch", "path", "identity", "head",
                  "git_dir", "git_identity", "remote", "endpoint", "workspace_id", "workspace_label",
                  "contexts", "integrations", "roots", "publication_sha256", "at", "workspace_state",
                  "worktree_state", "branch_state", "runtime"}
        if record["version"] == 2:
            fields.add("execution_id")
            if str(UUID(record["execution_id"])) != record["execution_id"]:
                raise ValueError("invalid execution identity")
        if "selected_branch" in record:
            fields.add("selected_branch")
            if "merged" not in record or record["selected_branch"] != record["branch"]:
                raise ValueError("invalid cleanup selection")
        if "merged" in record:
            fields.add("merged")
            merged = record["merged"]
            proof = merged["proof"]
            if (record["version"] != 2 or set(merged) != {"proof", "scope_sha256"}
                    or set(proof) != {"branch_commit", "base_commit", "pull"}
                    or proof["branch_commit"] != record["head"]
                    or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", proof["base_commit"])
                    or merged["scope_sha256"] is not None
                    and not re.fullmatch(r"[0-9a-f]{64}", merged["scope_sha256"])):
                raise ValueError("invalid merged disposal proof")
            pull = proof["pull"]
            if pull is not None and (set(pull) != {"repository", "number", "head_commit", "merge_commit"}
                    or pull["repository"] != record["remote"]["repository"]
                    or type(pull["number"]) is not int or pull["number"] <= 0
                    or pull["head_commit"] != record["head"]
                    or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", pull["merge_commit"])):
                raise ValueError("invalid retained PR proof")
        if "loop" in record:
            fields.add("loop")
            retained = record["loop"]
            delivery = retained["delivery"]
            if (set(retained) != {"checkpoint_sha256", "delivery", "files"}
                    or set(retained["files"]) - PASS_FILES
                    or any(not re.fullmatch(r"[0-9a-f]{64}", v) for v in
                           [retained["checkpoint_sha256"], *retained["files"].values()])):
                raise ValueError("invalid loop disposal evidence")
            context = delivery["context"]
            claim = dict(pass_id=delivery["pass_id"], phase=delivery["phase"],
                         context_id=context["context_id"] if context else None)
            validate_delivery(delivery, dict(run_id=delivery["run_id"], active_pass=claim,
                                             binding=dict(worktree=record["path"])))
            if context and not any(c["context_id"] == context["context_id"] for c in record["contexts"]):
                raise ValueError("unowned loop output")
        if "agent_retirement" in record:
            fields.add("agent_retirement")
            if record["state"] != "complete":
                raise ValueError("premature agent retirement evidence")
            validate_agent_runtime(record["agent_retirement"])
        if set(record) != fields or not isinstance(record["contexts"], list):
            raise ValueError("invalid journal fields")
        if any(not isinstance(record[k], str) or not record[k]
               for k in ("issue", "repository", "base", "branch", "path", "head", "git_dir", "endpoint", "at")):
            raise ValueError("invalid identity fields")
        for context in record["contexts"]:
            if (not isinstance(context, dict) or set(context) != set(registry.get(context["context_id"]))
                    or context["issue"] != issue.identifier or context["repository"] != str(repo)
                    or context["endpoint"] != record["endpoint"]):
                raise ValueError("invalid context identity")
        if record["state"] == "pending":
            proof = record["runtime"]
            if isinstance(proof, dict) and "process_proof" not in proof:
                raise TaskError("Cleanup claim predates the registered-shell process proof; preserve it for inspection")
            expected_fields = {"process_proof", "workspace", "panes", "shells"}
            if proof.get("process_proof") == 2:
                expected_fields.add("families")
                validate_agent_runtime(proof)
            if (set(proof) != expected_fields
                    or type(proof["process_proof"]) is not int or proof["process_proof"] not in {1, 2}
                    or type(proof["workspace"]) is not bool
                    or not isinstance(proof["panes"], list) or not isinstance(proof["shells"], dict)
                    or any(not pid.isdecimal() or type(started) is not int or started <= 0
                           for pid, started in proof["shells"].items())):
                raise ValueError("invalid process proof")
        if (record["issue"] != issue.identifier or record["repository"] != str(repo)
                or record["state"] == "pending" and record["base"] != project.base_branch
                or record["branch"] == record["base"]
                or not belongs_to_issue(record["branch"], issue.identifier)
                or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", record["head"])
                or any(record[k] not in {"pending", "removing", "removed"}
                       for k in ("worktree_state", "branch_state"))
                or record["workspace_state"] not in {"pending", "closing", "closed"}):
            raise ValueError("identity or step mismatch")
        common = Git(repo).check_repository()
        path = Path(record["path"])
        roots = [path, *(Path(r["path"]) for r in record["roots"])]
        for root in roots:
            # Completed selectors describe history, not the current occupants
            # of those paths. Only a pending claim may bind live directories.
            if (not root.is_absolute() or ".." in root.parts
                    or record["state"] == "pending" and root.resolve() != root
                    or any(root.is_relative_to(p) or p.is_relative_to(root) for p in (repo, common))):
                raise ValueError("unsafe root")
        if any(a.is_relative_to(b) or b.is_relative_to(a) for n, a in enumerate(roots) for b in roots[:n]):
            raise ValueError("overlapping roots")
        if not Path(record["git_dir"]).is_relative_to(common / "worktrees"):
            raise ValueError("invalid worktree metadata")
        for integration in record["integrations"]:
            binding = integration["binding"]
            if any(binding[k] != record[v] for k, v in (
                    ("issue", "issue"), ("repository", "repository"), ("worktree", "path"),
                    ("branch", "branch"), ("base_branch", "base"), ("endpoint", "endpoint"))):
                raise ValueError("integration execution binding mismatch")
            if not re.fullmatch(r"[0-9a-f]{64}", integration["record_sha256"]):
                raise ValueError("invalid retained integration digest")
            if integration["context_id"] is not None:
                matches = [c for c in record["contexts"] if c["context_id"] == integration["context_id"]]
                if (len(matches) != 1 or matches[0]["role"] != "integration"
                        or matches[0]["worktree"] != integration["checkout"]
                        or matches[0]["workspace_id"] != binding["workspace_id"]):
                    raise ValueError("integration release context mismatch")
            if str(UUID(integration["pass_id"])) != integration["pass_id"]:
                raise ValueError("invalid integration ID")
            expected = registry.path.parent.resolve() / "integrations" / integration["pass_id"] / "checkout"
            if Path(integration["checkout"]) != expected:
                raise ValueError("invalid retained checkout")
            if integration["output"] is not None:
                output = Path(integration["output"])
                if (output.name != "result.json" or not re.fullmatch(
                        re.escape(f"task-integration-{integration['pass_id']}-") + r"[A-Za-z0-9_-]+", output.parent.name)):
                    raise ValueError("invalid retained output")
        permitted = {str(Path(i["checkout"]).parent) for i in record["integrations"]}
        permitted |= {str(Path(i["output"]).parent) for i in record["integrations"] if i["output"]}
        if "loop" in record:
            permitted.add(record["loop"]["delivery"]["directory"])
        if {r["path"] for r in record["roots"]} != permitted:
            raise ValueError("invalid artifact selectors")
        for root in record["roots"]:
            required = {"path", "identity", "checkout", "state"}
            if root["checkout"] is not None:
                required |= {"checkout_identity", "git_identity", "history_sha256"}
            if set(root) != required:
                raise ValueError("invalid artifact fields")
            if (root["state"] not in {"pending", "removing", "removed"}
                    or root["checkout"] is not None and Path(root["checkout"]).parent != Path(root["path"])):
                raise ValueError("invalid artifact state")
        if record["state"] == "complete" and (record["workspace_state"] != "closed"
                or record["runtime"] is not None
                or any(record[k] != "removed" for k in ("worktree_state", "branch_state"))
                or any(r["state"] != "removed" for r in record["roots"])
                or any(c != registry.disposal_tombstone(c, record["at"]) for c in record["contexts"])):
            raise ValueError("incomplete tombstone")
    except (KeyError, TypeError, ValueError, AttributeError):
        raise TaskError("Forced cleanup journal identity is invalid; inspect without deleting its evidence") from None


def check_roots(record, git):
    trees = git.worktrees()
    for root in record["roots"]:
        path = Path(root["path"])
        identity = directory_identity(path)
        if root["state"] == "removed":
            if identity is not None:
                raise TaskError("Disposed artifact path was reused; cleanup refused")
            continue
        if identity is None:
            if root["identity"] is not None and root["state"] != "removing":
                raise TaskError("Retained artifact disappeared without a disposal claim")
            continue
        if identity != root["identity"]:
            raise TaskError("Retained artifact directory was replaced; cleanup refused")
        loop_output = record.get("loop")
        loop_output = loop_output if loop_output and str(path) == loop_output["delivery"]["directory"] else None
        allowed = PASS_FILES if loop_output else {"checkout", "result.json"} if root["checkout"] else {"result.json"}
        if set(p.name for p in path.iterdir()) - allowed:
            raise TaskError("Unrelated files appeared in a disposable artifact directory")
        if loop_output:
            check_directory(loop_output["delivery"])
            files = {p.name: sha256(read_private(p)).hexdigest() for p in path.iterdir()}
            if (root["state"] == "pending" and files != loop_output["files"]
                    or any(loop_output["files"].get(k) != v for k, v in files.items())):
                raise TaskError("Retained loop output changed during disposal")
        if any(Path(t["worktree"]).is_relative_to(path) for t in trees):
            raise TaskError("A registered worktree occupies a disposable artifact directory")
        check_contents(path, git_root=Path(root["checkout"]) if root["checkout"] else None)
        if root["checkout"]:
            checkout = Path(root["checkout"])
            for location, key in ((checkout, "checkout_identity"), (checkout / ".git", "git_identity")):
                current = directory_identity(location)
                if current != root[key] and not (current is None and root["state"] == "removing"):
                    raise TaskError("Isolated checkout identity changed; disposal refused")
            # A registered linked checkout outside this root must remain usable.
            registrations = checkout / ".git" / "worktrees"
            if registrations.exists() and any(registrations.iterdir()):
                raise TaskError("Retained integration has other registered worktrees; disposal refused")
            if root["state"] == "pending" and root["checkout_identity"] is not None:
                if evidence_digest(history_identity(checkout)) != root["history_sha256"]:
                    raise TaskError("Retained integration history changed; disposal refused")
        for integration in record["integrations"]:
            output = Path(integration["output"] or Path(integration["checkout"]).parent / "result.json")
            if output.parent != path:
                continue
            if output.is_symlink():
                raise TaskError("Integration completion output changed to a symlink")
            if output.exists():
                if (not output.is_file() or output.stat().st_size > 1024 * 1024
                        or sha256(output.read_bytes()).hexdigest() != integration.get("output_sha256")):
                    raise TaskError("Integration completion evidence changed before disposal")
            elif "output_sha256" in integration and root["state"] == "pending":
                raise TaskError("Integration completion evidence disappeared before disposal")


@ownership_operation
def check_normal_ownership(identifier, repo, target, registry, *, endpoint=None):
    """Normal cleanup shares the forced path's exact overlap exclusions.

    No disposal authorization is inferred here: the caller still needs its
    normal clean/merged proof. Repeat under exclusive admission before removal.
    """
    _, journals, _, _ = ownership_inventory(registry, repo)
    contexts = [c for c in selected_contexts(registry, identifier, journals)
                if c["repository"] == str(repo) and c["worktree"] == str(target.path)]
    record = dict(issue=identifier, repository=str(repo), path=str(target.path),
                  git_dir=str(PublicationStore(target.path).directory), contexts=contexts,
                  integrations=[], roots=[], endpoint=endpoint,
                  workspace_id=target.open_workspace_id, at=now())
    verify_contexts(record, registry)
    return record


@ownership_operation
def verify_contexts(record, registry):
    contexts, journals, stores, trees = ownership_inventory(registry, record["repository"])
    saved = {c["context_id"]: c for c in record["contexts"]}
    integration_ids = {i["context_id"] for i in record["integrations"]}
    for context in selected_contexts(registry, record["issue"], journals):
        if context["worktree"] != record["path"] and context["context_id"] not in integration_ids:
            continue
        original = saved.get(context["context_id"])
        if original is None or context not in (original, registry.disposal_tombstone(original, record["at"])):
            raise TaskError("New or changed task context appeared during cleanup")
    for context in saved.values():
        if registry.get(context["context_id"]) not in (
                context, registry.disposal_tombstone(context, record["at"])):
            raise TaskError("Selected task context changed during cleanup")
    paths, mounts = resource_paths(record), read_mounts()
    terminals = {c["terminal_id"] for c in saved.values() if c["terminal_id"]}
    def conflicts(claims):
        return any(overlapping(Path(claim), path, mounts) for claim in claims for path in paths)
    for context in contexts:
        if context["context_id"] in saved or not context_claimed(context, journals):
            continue
        if (context["worktree"] and conflicts([context["worktree"]])
                or context["endpoint"] == record["endpoint"]
                and (context["terminal_id"] in terminals or record["workspace_id"] is not None
                     and context["workspace_id"] == record["workspace_id"])):
            raise TaskError("Another task context claims this execution; disposal refused")
    for other in journals:
        if other["state"] == "pending" and other != record and conflicts(resource_paths(other)):
            raise TaskError("Another pending disposal reserves an execution resource")
    for path, repo in trees:
        if path in {Path(record["path"]), Path(record["git_dir"])}:
            continue
        # The permanent checkout/common store contains shared metadata by design;
        # validate_record independently prevents deleting those shared roots.
        if repo is not None and path == Path(repo):
            continue
        if conflicts([path]):
            raise TaskError("Another registered worktree/private Git directory claims a cleanup target")
        if repo is not None and path.exists():
            # Registration lists the expected private directory, but the
            # checkout's .git pointer may actually use another execution's.
            # Check every existing registered checkout, independent of issue,
            # Herdr label or open state, on each disposal guard/retry.
            try:
                git_dir = Path(Git(path).command("rev-parse", "--absolute-git-dir").strip())
                if not git_dir.is_absolute():
                    raise TaskError("Git directory is not absolute")
            except TaskError:
                raise TaskError(f"Cannot verify Git metadata used by registered checkout {path}; "
                                "restore its Git checkout binding before retrying --force") from None
            if conflicts([git_dir]):
                raise TaskError(f"Another registered checkout uses cleanup Git metadata: {path}; "
                                "restore its independent Git checkout binding before retrying --force")
    for directory in stores:
        if directory == Path(record["git_dir"]):
            continue  # Own provenance is frozen and checked by the disposal guard.
        loop_store = LoopStore.at_directory(directory)
        if loop_store.path.exists():
            checkpoint, _ = loop_store.read()
            if checkpoint.get("delivery") and conflicts([checkpoint["delivery"]["directory"]]):
                raise TaskError("Another loop delivery claim owns a cleanup target")
        for integration in IntegrationStore(publication_store=SimpleNamespace(directory=directory)).disposal_records():
            tombstone = integration_tombstone(integration)
            released = any(j["state"] == "complete" and any(
                {k: v for k, v in i.items() if k != "output_sha256"} == tombstone
                for i in j["integrations"]) for j in journals)
            if released:
                continue
            claims = [integration["binding"]["worktree"], str(directory), str(Path(integration["checkout"]).parent)]
            if integration.get("output"):
                claims.append(str(Path(integration["output"]).parent))
            if conflicts(claims):
                raise TaskError("Another integration provenance claim owns a cleanup target")


def disposal_branches(record, git):
    branches = git.branches(record["issue"])
    return [b for b in branches if b == record["selected_branch"]] if "selected_branch" in record else branches


def check_task(record, git, herdr, *, closed):
    if "merged" in record:
        base = git.command("rev-parse", "--verify", f"refs/heads/{record['base']}^{{commit}}").strip()
        if base != record["merged"]["proof"]["base_commit"]:
            raise TaskError("Base branch changed during merged cleanup; preserve the journal and stop concurrent base updates")
    path = Path(record["path"])
    present = directory_identity(path)
    trees = [t for t in git.worktrees() if Path(t["worktree"]).resolve() == path
             or t.get("branch") == f"refs/heads/{record['branch']}"]
    branches = disposal_branches(record, git)
    if branches not in ([], [record["branch"]]):
        raise TaskError("Another task branch appeared; forced cleanup refused")
    if present is None:
        if trees or record["worktree_state"] == "pending":
            raise TaskError("Task removal is uncertain; retained Git registration needs manual inspection")
        if os.path.lexists(record["git_dir"]):
            raise TaskError("Task Git metadata remains after removal; inspect the uncertain result")
    else:
        if present != record["identity"] or record["worktree_state"] == "removed":
            raise TaskError("Task checkout path was reused or replaced; cleanup refused")
        target = herdr.resolve_task(git, record["issue"], include_remotes=False, disposing=True,
                                    branch=record.get("selected_branch"))
        if target != TaskWorktree(record["branch"], path, None if closed else record["workspace_id"]):
            raise TaskError("Exact Git/Herdr task target changed; cleanup refused")
        git.check_cleanup_target(record["base"], target, record["issue"], require_clean=False,
                                 allow_missing_scope="merged" in record)
        if "merged" in record and scope_digest(git, path) != record["merged"]["scope_sha256"]:
            raise TaskError("Workspace scope changed during merged cleanup")
        if (str(PublicationStore(path).directory) != record["git_dir"]
                or directory_identity(Path(record["git_dir"])) != record["git_identity"]):
            raise TaskError("Task Git metadata identity changed")
        # `git worktree remove` also recursively removes this linked metadata.
        check_contents(Path(record["git_dir"]))
        check_contents(path, git_root=path)
    if branches:
        ref = f"refs/heads/{record['branch']}"
        refs = git.command("for-each-ref", "--format=%(refname) %(objectname) %(symref)", ref).splitlines()
        if refs != [f"{ref} {record['head']} "] or record["branch_state"] == "removed":
            raise TaskError("Task branch identity changed; forced cleanup refused")
    elif record["branch_state"] == "pending":
        raise TaskError("Task branch disappeared without a disposal claim")
    return present is not None, bool(branches)


def validate_agent_runtime(proof):
    """Strict bounded identities retained even after the execution is removed."""
    if (set(proof) != {"process_proof", "workspace", "panes", "shells", "families"}
            or proof["process_proof"] != 2 or proof["workspace"] is not True
            or not isinstance(proof["families"], dict) or not proof["families"]
            or len(proof["families"]) > 256
            or {p["pane_id"] for p in proof["panes"]} != set(proof["families"])):
        raise ValueError("invalid owned agent runtime")
    seen = set()
    for pane in proof["panes"]:
        if (set(pane) != {"workspace_id", "pane_id", "terminal_id", "cwd", "shell_argv"}
                or any(not isinstance(pane[k], str) or not pane[k] for k in
                       ("workspace_id", "pane_id", "terminal_id", "cwd"))):
            raise ValueError("invalid owned pane")
        family = proof["families"][pane["pane_id"]]
        if (set(family) != {"shell", "foreground", "shell_argv", "members"}
                or any(type(family[k]) is not int or family[k] <= 0 for k in ("shell", "foreground"))
                or not isinstance(family["members"], dict) or not 1 <= len(family["members"]) <= 256
                or str(family["shell"]) not in family["members"]
                or str(family["foreground"]) not in family["members"]
                or family["shell_argv"] != pane["shell_argv"] or seen & family["members"].keys()):
            raise ValueError("invalid owned family")
        shell_executable(family["shell_argv"])
        seen.update(family["members"])
        for pid, member in family["members"].items():
            if (not pid.isdecimal() or int(pid) <= 0
                    or set(member) != {"started", "parent", "group", "session", "terminal", "exe", "argv"}
                    or any(type(member[k]) is not int or member[k] < 0
                           for k in ("parent", "group", "session", "terminal"))
                    or type(member["started"]) is not int or member["started"] <= 0
                    or not isinstance(member["exe"], str) or not Path(member["exe"]).is_absolute()
                    or not isinstance(member["argv"], str) or not member["argv"].endswith("\0")):
                raise ValueError("invalid retained process identity")
    if proof["shells"] != {str(f["shell"]): f["members"][str(f["shell"])]["started"]
                           for f in proof["families"].values()}:
        raise ValueError("inconsistent retained shells")


def finish(record, journal, git, herdr, registry, identities):
    owned_agents = record["runtime"].get("process_proof") == 2
    stop_requested = False

    def observe_runtime():
        deadline = time.monotonic() + (3 if owned_agents and stop_requested else 0)
        while True:
            try:
                return runtime(record, herdr, identities, stop_agents=True)
            except TaskError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.05)

    def guard():
        if journal.read() != record:
            raise TaskError("Forced cleanup journal changed during disposal")
        git.check_base(record["base"])
        verify_contexts(record, registry)
        check_roots(record, git)
        observed = observe_runtime()
        if record["workspace_state"] == "closed":
            if observed["workspace"] or observed["panes"]:
                raise TaskError("Selected workspace reappeared after closure")
        elif observed != record["runtime"]:
            if (record["workspace_state"] != "closing"
                    or not owned_agents and (observed["workspace"] or observed["panes"])):
                raise TaskError("Selected workspace runtime changed; cleanup refused")
        present, branch = check_task(record, git, herdr, closed=not observed["workspace"])
        saved = PublicationStore(Path(record["path"])).read() if present else None
        if saved is not None and evidence_digest(saved) != record["publication_sha256"]:
            raise TaskError("Publication evidence changed during forced cleanup")
        if present:
            if "loop" in record and evidence_digest(list(LoopStore(Path(record["path"])).read())) != record["loop"]["checkpoint_sha256"]:
                raise TaskError("Loop checkpoint changed during forced cleanup")
            evidence = [integration_tombstone(r) for r in IntegrationStore(Path(record["path"])).disposal_records()]
            if evidence != [{k: v for k, v in i.items() if k != "output_sha256"} for i in record["integrations"]]:
                raise TaskError("Integration provenance changed during forced cleanup")
        if "merged" in record:
            verify_remote_identity(git, record["base"], record["branch"], record["remote"])
            if present:
                verify_remote_identity(Git(Path(record["path"])), record["base"], record["branch"], record["remote"])
            proof = git.cleanup_merge(record["base"], record["branch"], record["issue"], head=record["head"],
                                      require_pull=not branch and record["merged"]["proof"]["pull"] is not None)
            if asdict(proof) != record["merged"]["proof"]:
                raise TaskError("Task, base branch or merge evidence changed during merged cleanup")
        elif never_published(git, record["base"], record["issue"], record["branch"], saved,
                             worktree=Path(record["path"]) if present else None) != record["remote"]:
            raise TaskError("Remote repository identity changed during forced cleanup")
        verify_contexts(record, registry)
        check_roots(record, git)
        if check_task(record, git, herdr, closed=not observed["workspace"]) != (present, branch):
            raise TaskError("Selected Git target changed before removal")
        # Refresh all deletion roots together after the other, potentially slow
        # guards, before allowing any task or integration contents to be removed.
        check_mounts([Path(record["path"]), Path(record["git_dir"]),
                      *(Path(root["path"]) for root in record["roots"])])
        # OS evidence must be the last guard, after remote reads, Git commands,
        # directory walks and mount checks, immediately before the mutation.
        if observe_runtime() != observed:
            raise TaskError("Selected execution runtime changed before removal")
        return present, branch, observed

    present, _, observed = guard()
    if record["workspace_state"] != "closed":
        record["workspace_state"] = "closing"
        journal.write(record)
        _, _, observed = guard()
        if observed["workspace"]:
            try:
                stop_requested = True
                result = herdr.workspace_command("close", record["workspace_id"])
                if result.get("workspace_id") is None:
                    raise HerdrResponseError("Herdr close response omitted the workspace identity")
            except HerdrResponseError as error:
                # The mutation may have succeeded. Never repeat close here:
                # reconcile the frozen endpoint/workspace, terminals and Git.
                _, _, observed = guard()
                if observed["workspace"] or observed["panes"]:
                    raise TaskError(f"{error}; exact workspace closure is unconfirmed. "
                                    "Journal retained; inspect and rerun --force") from None
            else:
                if result.get("workspace_id") != record["workspace_id"]:
                    raise TaskError("Herdr close result is uncertain; rerun --force after inspection")
        _, _, observed = guard()
        if observed["workspace"] or observed["panes"]:
            raise TaskError("Herdr workspace closure was not confirmed; rerun --force after inspection")
        record["workspace_state"] = "closed"
        journal.write(record)
    for root in record["roots"]:
        if root["state"] == "removed":
            continue
        guard()
        root["state"] = "removing"
        journal.write(record)  # Includes bounded provenance before deleting files.
        guard()
        path = Path(root["path"])
        if directory_identity(path) is not None:
            shutil.rmtree(path)  # No symlink following; exact root inode checked above.
        if directory_identity(path) is not None:
            raise TaskError("Integration artifact removal is uncertain")
        root["state"] = "removed"
        journal.write(record)
    present, _, _ = guard()
    if record["worktree_state"] != "removed":
        record["worktree_state"] = "removing"
        journal.write(record)
        present, _, observed = guard()
        if present:
            # The fsynced removing reservation replaces metadata in inventory;
            # recursive Git deletion must not hold machine-wide admission.
            with PublicationStore(git.repo).repository_locked():
                with ownership_gate(exclusive=True):
                    verify_contexts(record, registry)
                    check_task(record, git, herdr, closed=True)
                check_mounts([Path(record["path"]), Path(record["git_dir"])])
                # Repository admission can wait; refresh the unchanged Linux
                # stopped-execution proof after that wait, last before removal.
                if observe_runtime() != observed:
                    raise TaskError("Selected execution runtime changed before removal")
                git.command("worktree", "remove", "--force", "--", record["path"])
        present, _ = check_task(record, git, herdr, closed=True)
        if present:
            raise TaskError("Selected task worktree removal is uncertain")
        record["worktree_state"] = "removed"
        journal.write(record)
    _, branch, _ = guard()
    if record["branch_state"] != "removed":
        record["branch_state"] = "removing"
        journal.write(record)
        _, branch, _ = guard()
        if branch:
            git.command("update-ref", "--no-deref", "-d", f"refs/heads/{record['branch']}", record["head"])
        if disposal_branches(record, git):
            raise TaskError("Selected task branch removal is uncertain")
        record["branch_state"] = "removed"
        journal.write(record)
    guard()
    registry.retire_execution(record["contexts"], record["at"])
    guard()
    record["contexts"] = [registry.disposal_tombstone(c, record["at"]) for c in record["contexts"]]
    if owned_agents:
        record["agent_retirement"] = record["runtime"]
    record.update(state="complete", runtime=None)
    journal.write(record)


def discard_execution(issue, project, repo, *, branch=None):
    if any(k in os.environ for k in ("GIT_INDEX_FILE", "GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR",
                                    "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES")):
        raise TaskError("Unset Git routing environment overrides before forced cleanup")
    git, herdr = Git(repo), Herdr(repo)
    registry, identities = ContextRegistry(), HerdrContexts()
    git.check_base(project.base_branch)
    try:
        if branch is not None:
            if not belongs_to_issue(branch, issue.identifier) or branch == project.base_branch:
                raise TaskError("Cleanup --branch must name an exact local branch belonging to this issue")
            git.command("check-ref-format", f"refs/heads/{branch}")
        # All execution journals share the issue directory's lock. Completed
        # journals are immutable history, never a claim on a later execution.
        lock = DisposalStore(git, issue.identifier)
        with lock.locked():
            # Include retries of journals created before registry discovery was
            # available, including contextless branch-only remainders.
            with ownership_gate(exclusive=True):
                registry.remember_repository(repo)
            journals = []
            for path in git.disposal_files(issue.identifier):
                journal = DisposalStore(git, issue.identifier, None if path.name == "state.json" else path.stem)
                record = journal.read()
                if record is None:
                    raise TaskError("Forced cleanup journal disappeared; preserve the remaining evidence")
                validate_record(record, issue, project, repo, registry)
                journals.append((journal, record))
            pending = [(j, r) for j, r in journals if r["state"] == "pending"]
            if len(pending) > 1:
                raise TaskError("Multiple forced-disposal execution claims are pending; inspect without deleting evidence")
            if pending:
                journal, record = pending[0]
                if branch is not None and branch != record["branch"]:
                    raise TaskError(f"Pending cleanup owns {record['branch']!r}; finish it with "
                                    f"task cleanup {issue.identifier} --force before selecting another branch")
                if branch is not None and "selected_branch" not in record:
                    if "merged" not in record:
                        raise TaskError("Pending unpublished disposal must resume without --branch; selection requires merge proof")
                    # New explicit authorization narrows discovery, never changes
                    # the frozen execution or its proof. All guards still run.
                    record["selected_branch"] = branch
                    journal.write(record)
            else:
                target = herdr.resolve_task(git, issue.identifier, include_remotes=False, disposing=True, branch=branch)
                matching_journals = [(j, r) for j, r in journals if branch is None or r["branch"] == branch]
                if target is not None:
                    journal = DisposalStore(git, issue.identifier, str(uuid4()))
                    if journal.read() is not None:
                        raise TaskError("Forced cleanup execution identity already exists")
                    record = None
                elif matching_journals:
                    journal, record = max(matching_journals, key=lambda item: (item[1]["at"], item[0].path.name))
                else:
                    if branch is not None:
                        raise TaskError(f"No registered local execution or completed disposal matches branch {branch!r}")
                    if selected_contexts(registry, issue.identifier) or herdr.stale_retirement(git, issue.identifier, project.base_branch):
                        raise TaskError("Selected execution lacks its exact Git provenance; inspect retained state manually")
                    return f"{issue.identifier}: no local execution to discard"
            if record is None:
                publication = PublicationStore(target.path)
            elif record["state"] == "complete":
                publication = None  # Released paths may now belong to another execution.
            else:
                identity = directory_identity(Path(record["path"]))
                if identity is not None and (identity != record["identity"] or record["worktree_state"] == "removed"):
                    raise TaskError("Task checkout path was reused or replaced; cleanup refused")
                publication = PublicationStore(Path(record["path"])) if identity else None
                if publication is not None and (str(publication.directory) != record["git_dir"]
                        or directory_identity(publication.directory) != record["git_identity"]):
                    raise TaskError("Task Git metadata identity changed before locking")
            with publication.locked() if publication else nullcontext():
                if record is None:
                    record = prepare_record(issue, project, repo, git, herdr, registry, identities, target,
                                            publication, journal.path.stem, branch=branch)
                    # Claim admission and reservation publication are indivisible.
                    # No slow provider, remote or filesystem deletion is inside.
                    with ownership_gate(exclusive=True):
                        registry.remember_repository(repo)
                        verify_contexts(record, registry)
                        check_task(record, git, herdr, closed=target.open_workspace_id is None)
                        journal.write(record)
                if record["state"] == "complete":
                    remaining = selected_contexts(registry, issue.identifier, [r for _, r in journals])
                    if any(branch is None or c["worktree"] == record["path"] for c in remaining):
                        raise TaskError("Unreleased task contexts lack their exact Git execution; inspect retained ownership")
                    # Completed selectors are release evidence, not continuing
                    # reservations on paths, panes or IDs that may have been reused.
                    return (f"{issue.identifier}: execution already discarded\n"
                            f"Completed disposal provenance: {journal.path}")
                else:
                    finish(record, journal, git, herdr, registry, identities)
        return (f"{issue.identifier}: local execution discarded\nRemoved task worktree and local branch; "
                "Herdr workspace and contexts retired; retained integration artifacts removed.\n"
                f"Bounded disposal provenance: {journal.path}")
    except OSError:
        raise TaskError(f"Forced cleanup was interrupted by a filesystem error; preserve its journal and "
                        f"rerun task cleanup {issue.identifier} --force after inspection") from None
    except TaskError as error:
        raise TaskError(f"{error}\n{cleanup_recovery(issue.identifier, repo)}") from None
