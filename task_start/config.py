from dataclasses import dataclass, field, replace
import os
from pathlib import Path
import re
import stat
import subprocess
import tomllib
from typing import Mapping

from . import TaskError

REGISTRY = Path(__file__).resolve().parent.parent / "config" / "projects.toml"
CONFIG_DIRECTORY = ".agentic-workflows-lite"


@dataclass(frozen=True)
class Project:
    linear_project: str
    repo_name: str
    base_branch: str


@dataclass(frozen=True)
class AgentConfig:
    kind: str | None
    model: str | None = None
    mode: str | None = None

    @property
    def reasoning(self) -> str | None:
        """Compatibility name for configurations written before workflow modes."""
        return self.mode


@dataclass(frozen=True)
class IssueStructureConfig:
    mode: str = "warn"
    block_name: str = "Agent instructions"


@dataclass(frozen=True)
class ReviewValidationConfig:
    strategy: str = "focused_first"


@dataclass(frozen=True)
class TaskAssessmentConfig:
    enabled: bool = True


@dataclass(frozen=True)
class ImplementationScopeConfig:
    policy: str = "strict"


@dataclass(frozen=True)
class LocalConfig:
    projects_root: Path
    api_key: str = field(repr=False)
    agent: AgentConfig | None = None
    codex_repository_profiles: Mapping[str, str] = field(default_factory=dict)
    reviewer: AgentConfig | None = None
    issue_structure: IssueStructureConfig = field(default_factory=IssueStructureConfig)
    review_validation: ReviewValidationConfig = field(default_factory=ReviewValidationConfig)
    task_assessment: TaskAssessmentConfig = field(default_factory=TaskAssessmentConfig)
    implementation_scope: ImplementationScopeConfig = field(default_factory=ImplementationScopeConfig)
    loop_timeout: float = 7200


def read_toml(path: Path) -> dict:
    try:
        with path.open("rb") as source:
            return tomllib.load(source)
    except FileNotFoundError:
        raise TaskError(f"Config missing: {path}") from None
    except (OSError, UnicodeError, tomllib.TOMLDecodeError):
        # Never echo TOML contents: this file can contain credentials.
        raise TaskError(f"Cannot read valid TOML from {path}") from None


def required_text(data: dict, key: str) -> str:
    value = data.get(key) if isinstance(data, dict) else None
    if not isinstance(value, str) or not value.strip():
        raise TaskError(f"Config requires a nonempty {key}")
    return value.strip()


def load_local(path: Path | None = None, *, no_agent: bool = False) -> LocalConfig:
    data = read_toml(path or Path.home() / CONFIG_DIRECTORY / "config.toml")
    api_key = required_text(data.get("linear"), "api_key")
    if any(char.isspace() for char in api_key):
        raise TaskError("Linear api_key must not contain whitespace")
    structure = (IssueStructureConfig() if no_agent
                 else issue_structure_config(data["linear"].get("issue_structure", {})))
    validation = (ReviewValidationConfig() if no_agent
                  else review_validation_config(data.get("review", {})))
    assessment, scope = ((TaskAssessmentConfig(), ImplementationScopeConfig()) if no_agent
                         else implementation_config(data.get("implementation", {})))
    try:
        root = Path(required_text(data, "projects_root")).expanduser()
        if not root.is_absolute():
            raise TaskError("projects_root must be absolute (or start with ~)")
        agent = None if no_agent else agent_config(data.get("agent"), partial=True)
        profiles = {} if no_agent else codex_repository_profiles(data.get("codex"))
        reviewer = None if no_agent else agent_config(data.get("reviewer"), partial=True)
        timeout = 7200 if no_agent else loop_config(data.get("loop", {}))
        return LocalConfig(root.resolve(), api_key, agent, profiles, reviewer, structure, validation, assessment, scope, timeout)
    except (OSError, ValueError, RuntimeError):
        raise TaskError("projects_root could not be resolved") from None


def status_projects_root(path: Path | None = None) -> Path:
    """Read only the configured repository location; status needs no credentials.

    Do not use load_local here: missing agent/Linear settings must not prevent
    offline inspection, and no credential is returned to the status read model.
    """
    data = read_toml(path or Path.home() / CONFIG_DIRECTORY / "config.toml")
    root = Path(required_text(data, "projects_root")).expanduser()
    if not root.is_absolute():
        raise TaskError("projects_root must be absolute")
    return root.resolve()


def issue_structure_config(data: dict) -> IssueStructureConfig:
    if not isinstance(data, dict) or set(data) - {"mode", "block_name"}:
        raise TaskError("linear.issue_structure must be a table supporting only mode and block_name")
    mode = data.get("mode", "warn")
    if not isinstance(mode, str) or mode.strip() not in {"required", "warn", "ignore"}:
        raise TaskError("linear.issue_structure.mode must be required, warn, or ignore")
    name = data.get("block_name", "Agent instructions")
    if not isinstance(name, str) or not name.strip() or not name.isprintable():
        raise TaskError("linear.issue_structure.block_name must be nonempty printable text on one line")
    return IssueStructureConfig(mode.strip(), name.strip())


def review_validation_config(data: dict) -> ReviewValidationConfig:
    if not isinstance(data, dict) or set(data) - {"validation"}:
        raise TaskError("review must be a table supporting only validation")
    validation = data.get("validation", {})
    if not isinstance(validation, dict) or set(validation) - {"strategy"}:
        raise TaskError("review.validation must be a table supporting only strategy")
    strategy = validation.get("strategy", "focused_first")
    if not isinstance(strategy, str) or strategy.strip() not in {"focused_first", "exhaustive"}:
        raise TaskError("review.validation.strategy must be focused_first or exhaustive")
    return ReviewValidationConfig(strategy.strip())


def implementation_config(data: dict) -> tuple[TaskAssessmentConfig, ImplementationScopeConfig]:
    if not isinstance(data, dict) or set(data) - {"task_assessment", "scope"}:
        raise TaskError("implementation must be a table supporting only task_assessment and scope")
    assessment = data.get("task_assessment", {})
    if not isinstance(assessment, dict) or set(assessment) - {"enabled"}:
        raise TaskError("implementation.task_assessment must be a table supporting only enabled")
    enabled = assessment.get("enabled", True)
    if type(enabled) is not bool:
        raise TaskError("implementation.task_assessment.enabled must be a boolean")
    scope = data.get("scope", {})
    if not isinstance(scope, dict) or set(scope) - {"policy"}:
        raise TaskError("implementation.scope must be a table supporting only policy")
    policy = scope.get("policy", "strict")
    if not isinstance(policy, str) or policy.strip() not in {"strict", "balanced"}:
        raise TaskError("implementation.scope.policy must be strict or balanced")
    return TaskAssessmentConfig(enabled), ImplementationScopeConfig(policy.strip())


def agent_config(data: dict | None, *, partial: bool = False) -> AgentConfig | None:
    # Older configurations remain useful for --no-agent.
    if data is None:
        return None
    if not isinstance(data, dict) or set(data) - {"kind", "model", "mode", "reasoning"}:
        raise TaskError("agent/reviewer supports only kind, model, and mode (or legacy reasoning)")
    kind = None if partial and "kind" not in data else required_text(data, "kind")
    if kind is not None and not re.fullmatch(r"[a-z][a-z0-9_-]*", kind):
        raise TaskError("agent.kind must be a lowercase agent name such as codex or pi")

    model = data.get("model")
    if model is not None:
        if (not isinstance(model, str) or not model.strip()
                or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/@*+-]*", model.strip())):
            raise TaskError("agent.model must be a model name without whitespace or control characters")
        model = model.strip()

    # `reasoning` was the original Codex-shaped field. `mode` is the
    # agent-independent spelling; accepting exactly one makes migration explicit.
    if "mode" in data and "reasoning" in data:
        raise TaskError("[agent] must use either mode or legacy reasoning, not both")
    mode_key = "mode" if "mode" in data else "reasoning"
    mode = data.get(mode_key)
    if mode is not None:
        if (not isinstance(mode, str) or not mode.strip()
                or not re.fullmatch(r"[a-z][a-z0-9_-]*", mode.strip())):
            raise TaskError(f"agent.{mode_key} must be a lowercase execution mode without whitespace")
        mode = mode.strip()
    return AgentConfig(kind, model, mode)


def loop_config(data: dict, default: float = 7200) -> float:
    if not isinstance(data, dict) or set(data) - {"timeout"}:
        raise TaskError("loop must be a table supporting only timeout")
    timeout = data.get("timeout", default)
    if type(timeout) not in {int, float} or not 0 < timeout <= 86400:
        raise TaskError("loop.timeout must be positive and at most 86400 seconds")
    return timeout


def project_settings(local: LocalConfig, repository: Path) -> LocalConfig:
    """Merge optional defaults from the trusted permanent checkout only.

    Call only for a fresh selection. Contexts/checkpoints own active settings;
    recovery and same-session continuations must not reload these files.
    """
    from .agent import validate_agent_defaults
    from .workspace import Git

    directory = repository / CONFIG_DIRECTORY
    try:
        info = directory.lstat()
    except FileNotFoundError:
        return local
    except OSError:
        raise TaskError("Cannot inspect project configuration directory") from None
    if not stat.S_ISDIR(info.st_mode) or directory.resolve() != directory:
        raise TaskError("Project configuration directory must be a real directory in the permanent checkout")
    git = Git(repository)
    git.check_repository()
    for name in ("config.toml", "config.local.toml"):
        path = directory / name
        try:
            info = path.lstat()
        except FileNotFoundError:
            continue
        except OSError:
            raise TaskError(f"Cannot inspect project configuration: {path}") from None
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or path.resolve() != path:
            raise TaskError(f"Project configuration must be a regular, unaliased file: {path}")
        if name == "config.local.toml":
            relative = path.relative_to(repository).as_posix()
            if git.command("ls-files", "--", relative).strip():
                raise TaskError("Project config.local.toml must not be tracked by Git")
            try:
                git.command("check-ignore", "--", relative)
            except TaskError:
                raise TaskError("Ignore .agentic-workflows-lite/config.local.toml in Git before using it") from None
        # O_NOFOLLOW/O_NONBLOCK also refuse a final-component symlink/FIFO swap.
        try:
            fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
            with os.fdopen(fd, "rb") as source:
                current = os.fstat(source.fileno())
                if ((current.st_dev, current.st_ino) != (info.st_dev, info.st_ino)
                        or not stat.S_ISREG(current.st_mode) or current.st_nlink != 1
                        or directory.resolve() != directory):
                    raise TaskError("Project configuration path changed during inspection")
                data = tomllib.load(source)
        except (OSError, UnicodeError, tomllib.TOMLDecodeError):
            raise TaskError(f"Cannot read valid project TOML from {path}") from None
        if set(data) - {"agent", "reviewer", "loop"}:
            raise TaskError("Project configuration supports only agent, reviewer, and loop defaults")
        updates = {}
        for role in ("agent", "reviewer"):
            if role not in data:
                continue
            override = agent_config(data[role], partial=True)
            # Validate each provided value, even when a later layer overrides it.
            validate_agent_defaults(override)
            base = getattr(local, role)
            updates[role] = AgentConfig(*(getattr(override, key) if getattr(override, key) is not None
                                         else getattr(base, key) if base else None
                                         for key in ("kind", "model", "mode")))
        if "loop" in data:
            updates["loop_timeout"] = loop_config(data["loop"], local.loop_timeout)
        local = replace(local, **updates)
    return local


def check_project_settings_unchanged(global_settings: LocalConfig, repository: Path,
                                     selected: LocalConfig) -> None:
    """Revalidate the prepared base before creating execution artifacts.

    Merge from the original global defaults so removal of a project field is
    detected too. Invalid layers fail even if their effective values match.
    """
    if project_settings(global_settings, repository) != selected:
        raise TaskError("Project defaults changed during base preparation; inspect the updated "
                        "permanent checkout configuration and retry before starting execution")


def codex_repository_profiles(data: dict | None) -> dict[str, str]:
    """Read exact repository-to-profile selections from machine-local config."""
    if data is None:
        return {}
    if not isinstance(data, dict) or set(data) - {"repositories"}:
        raise TaskError("[codex] supports only the repositories table")
    repositories = data.get("repositories", {})
    if not isinstance(repositories, dict):
        raise TaskError("codex.repositories must be a table")

    profiles = {}
    for repository, override in repositories.items():
        if (not isinstance(repository, str)
                or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", repository)):
            raise TaskError("codex repository names must be portable directory names")
        if not isinstance(override, dict) or set(override) != {"profile"}:
            raise TaskError(f"codex.repositories.{repository} requires only profile")
        profile = required_text(override, "profile")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}", profile):
            raise TaskError("Codex profile must be a portable name without path or option syntax")
        profiles[repository] = profile
    return profiles


def default_project_registry() -> Path:
    """Use the CLI installation's permanent checkout, never the caller's cwd.

    Git proves the relationship when this code lives in a linked worktree.
    A non-Git installation retains the sibling config/ resource convention.
    No parent search, machine paths, or worktree-local fallback is permitted.
    """
    try:
        return _default_project_registry()
    except (OSError, RuntimeError):
        raise TaskError('Cannot resolve private project registry checkout identity safely') from None


def _default_project_registry() -> Path:
    source = REGISTRY.parent.parent
    if not os.path.lexists(source / '.git'):
        return REGISTRY

    # Inherited Git selectors/config injection must not redirect discovery to
    # the repository of the shell that happened to launch this CLI.
    env = {key: value for key, value in os.environ.items() if not key.startswith('GIT_')}

    def git(root, *args):
        try:
            result = subprocess.run(['git', '-C', str(root), *args], env=env,
                                    capture_output=True, timeout=10, check=False)
        except (OSError, subprocess.TimeoutExpired):
            raise TaskError('Cannot verify the CLI checkout for private project registry discovery') from None
        if result.returncode:
            raise TaskError('Cannot verify the CLI checkout for private project registry discovery')
        return os.fsdecode(result.stdout)

    def identity(root):
        values = git(root, 'rev-parse', '--path-format=absolute', '--show-toplevel',
                     '--absolute-git-dir', '--git-common-dir').splitlines()
        if len(values) != 3 or any(not Path(value).is_absolute() for value in values):
            raise TaskError('Ambiguous CLI checkout identity for project registry discovery')
        top, private, common = map(Path, values)
        if top != root or any(path.resolve() != path for path in (top, private, common)):
            raise TaskError('Conflicting CLI checkout identity for project registry discovery')
        return private, common

    private, common = identity(source)
    records = []
    for record in git(source, 'worktree', 'list', '--porcelain', '-z').split('\0\0'):
        fields = dict(field.partition(' ')[::2] for field in record.split('\0') if field)
        if fields:
            records.append(fields)
    # Git lists its main worktree first. Verify that candidate independently;
    # neither its display name nor common-dir.parent establishes authority.
    if (not records or not records[0].get('worktree')
            or sum(row.get('worktree') == str(source) for row in records) != 1
            or any('bare' in row or 'prunable' in row for row in records
                   if row.get('worktree') in {str(source), records[0]['worktree']})):
        raise TaskError('Missing or ambiguous CLI worktree registration for project registry discovery')
    permanent = Path(records[0]['worktree'])
    if (not permanent.is_absolute() or identity(permanent) != (common, common)
            or private == common and permanent != source
            or private != common and private.parent != common / 'worktrees'):
        raise TaskError('Private project registry requires a verified permanent CLI checkout')
    if source != permanent and os.path.lexists(REGISTRY):
        raise TaskError('A linked CLI worktree has a competing private project registry; use only the permanent checkout registry')
    path = permanent / 'config' / 'projects.toml'
    if path.resolve() != path:
        raise TaskError('Private project registry must not redirect outside its permanent checkout path')
    return path


def load_projects(path: Path | None = None) -> list[Project]:
    path = default_project_registry() if path is None else path
    data = read_toml(path)
    if "example_only" in data:
        raise TaskError("Configure private project targets and remove example_only; examples are not runtime mappings")
    entries = data.get("projects")
    if not isinstance(entries, dict) or not entries:
        raise TaskError("Project registry requires a [projects] table")
    projects = []
    for entry in entries.values():
        project = Project(*(required_text(entry, key) for key in (
            "linear_project", "repo_name", "base_branch"
        )))
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", project.repo_name):
            raise TaskError("repo_name must be a single portable directory name")
        projects.append(project)
    if len({p.linear_project for p in projects}) != len(projects):
        raise TaskError("Duplicate linear_project names in project registry")
    return projects


def resolve_project(projects: list[Project], name: str) -> Project:
    matches = [p for p in projects if p.linear_project == name]
    if len(matches) != 1:
        raise TaskError(f"Linear project {name!r} must match exactly one configured project")
    return matches[0]


def repository_path(local: LocalConfig, project: Project) -> Path:
    return (local.projects_root / project.repo_name).resolve()
