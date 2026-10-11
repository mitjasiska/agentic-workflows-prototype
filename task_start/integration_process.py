"""Read-only local process evidence for startup recovery and execution retirement."""

from dataclasses import dataclass
import os
from pathlib import Path
import re
import sys

from . import TaskError


def stopped_checkout(path, shell_pid=None, *, proc=Path("/proc")):
    """Require no checkout processes or shell children, including background jobs.

    Herdr foreground status alone cannot establish this. Unsupported platforms or
    inaccessible same-user process evidence refuse instead of assuming absence.
    """
    if not sys.platform.startswith("linux") or not proc.is_dir():
        raise TaskError("Integration abandonment requires readable local Linux process evidence")
    processes = {}
    try:
        for entry in proc.iterdir():
            if not entry.name.isdecimal():
                continue
            try:
                if entry.stat().st_uid != os.getuid():
                    continue
                fields = (entry / "stat").read_text().rsplit(")", 1)[1].split()
                if fields[0] == "Z":
                    continue
                parent, started = int(fields[1]), int(fields[19])
                cwd = Path(os.readlink(entry / "cwd"))
                argv = os.fsdecode((entry / "cmdline").read_bytes())
                processes[int(entry.name)] = (parent, started, cwd, argv)
            except FileNotFoundError:
                if entry.exists():
                    raise  # Missing evidence for a still-present process is uncertain.
        if shell_pid is not None:
            shell = processes.get(shell_pid)
            if shell is None or shell[2] != path:
                raise TaskError("Integration shell process is absent or changed; abandonment refused. "
                                "Run recovery on the same host/PID namespace as Herdr and inspect the exact shell")
        descendants = {shell_pid} if shell_pid is not None else set()
        while True:
            found = {pid for pid, value in processes.items() if value[0] in descendants}
            if found <= descendants:
                break
            descendants |= found
        for pid, (_, _, cwd, argv) in processes.items():
            if pid != shell_pid and (pid in descendants or cwd.is_relative_to(path) or str(path) in argv):
                raise TaskError("A process may still use the integration checkout; abandonment refused")
        return dict(pid=shell_pid, started=processes[shell_pid][1] if shell_pid is not None else None)
    except (OSError, ValueError, IndexError):
        raise TaskError("Cannot prove integration processes have stopped; abandonment refused") from None


def shell_executable(argv):
    """Recognize only ordinary interactive/login invocations of system shells."""
    if not isinstance(argv, list) or not argv or any(not isinstance(a, str) or not a for a in argv):
        raise TaskError("Missing shell argv evidence; forced cleanup refused")
    command = argv[0].removeprefix("-")  # Login shells commonly use argv[0] = -bash.
    name = Path(command).name
    candidates = [Path(directory) / name for directory in ("/bin", "/usr/bin")]
    if (name not in {"bash", "sh", "dash", "zsh", "fish", "ksh"}
            or command != name and Path(command) not in candidates
            or any(a not in {"--login", "--interactive", "--norc", "--noprofile"}
                   and re.fullmatch(r"-[il]+", a) is None for a in argv[1:])):
        raise TaskError("Process argv does not prove an idle shell; forced cleanup refused")
    executables = {p.resolve(strict=True) for p in candidates if p.is_file()}
    if len(executables) != 1:
        raise TaskError("System shell executable is absent or ambiguous; forced cleanup refused")
    return executables.pop()


def verify_shell_process(pid, argv, *, proc=Path("/proc")):
    """Verify the reported shell itself; an exec-replaced PID is not a shell.

    Read only this selected process, not unrelated host processes. Herdr argv and
    local executable/cmdline must agree, including ordinary interactive options.
    """
    try:
        executable = shell_executable(argv)
        if type(pid) is not int or pid <= 0:
            raise ValueError("invalid shell PID")
        entry = proc / str(pid)
        if (not os.path.samefile(entry / "exe", executable)
                or (entry / "cmdline").read_bytes() != os.fsencode("\0".join(argv) + "\0")):
            raise ValueError("shell process changed")
    except (TaskError, OSError, ValueError):
        raise TaskError("Shell executable/argv is missing, changed, or not an interactive shell; "
                        "recovery requires readable process evidence on Herdr's host") from None


# Linux include/linux/sched.h; exported as unsigned decimal stat field 9.
# PF_WQ_WORKER, PF_IO_WORKER, comm text and absence of an executable are not
# substitutes for this flag. In particular, user workers may use execution state.
_PF_KTHREAD = 0x00200000


@dataclass(frozen=True)
class _Process:
    entry: Path
    leader: int
    parent: int
    started: int
    state: str
    group: int
    session: int
    terminal: int
    foreground: int
    kernel: bool

    @property
    def live(self):
        return self.state not in {"Z", "X", "x"}

    @property
    def identity(self):
        # Scheduling changes (R/S/D) are ordinary; ancestry, PID reuse, terminal
        # changes, kernel/userspace transitions and death invalidate observation.
        # Other task flags can change during normal kernel work.
        return (self.leader, self.parent, self.started, self.live, self.group,
                self.session, self.terminal, self.foreground, self.kernel)


def _process_stat(entry, tid, leader):
    text = (entry / "stat").read_text()
    prefix, separator, rest = text.rpartition(") ")
    fields = rest.split()
    if (not separator or not prefix.startswith(f"{tid} (") or len(fields) < 20
            or fields[0] not in {"R", "S", "D", "Z", "T", "t", "X", "x", "K", "W", "P", "I"}):
        raise ValueError("malformed process stat")
    parent, group, session, terminal, foreground = map(int, fields[1:6])
    if re.fullmatch(r"[0-9]{1,10}", fields[6]) is None or int(fields[6]) > 0xffffffff:
        raise ValueError("invalid process flags")
    kernel = bool(int(fields[6]) & _PF_KTHREAD)
    started = int(fields[19])
    if min(parent, group, session, started) < 0:
        raise ValueError("invalid process identity")
    return _Process(entry, leader, parent, started, fields[0], group, session, terminal, foreground, kernel)


def _process_visibility(proc):
    """An apparently complete directory listing is insufficient with hidepid.

    Require an unfiltered proc mount and no overmounts hiding PID evidence.
    Unknown/unreadable mount evidence cannot establish process absence.
    """
    data = (proc / "self" / "mounts").read_bytes()
    if not data or not data.endswith(b"\n"):
        raise ValueError("missing proc mount evidence")
    mounts = []
    for line in data[:-1].split(b"\n"):
        fields = line.split()
        if (len(fields) != 6 or not fields[4].isdigit() or not fields[5].isdigit()
                or re.search(rb"\\(?!040|011|012|134)", fields[1])):
            raise ValueError("malformed proc mount evidence")
        point = Path(os.fsdecode(re.sub(rb"\\(040|011|012|134)",
            lambda match: bytes([int(match[1], 8)]), fields[1])))
        if not point.is_absolute() or ".." in point.parts:
            raise ValueError("ambiguous proc mount path")
        if point == proc:
            mounts.append(fields)
        elif point.is_relative_to(proc):
            child = point.relative_to(proc).parts[0]
            if child.isdecimal() or child in {"self", "thread-self"}:
                raise ValueError("process evidence hidden by a mount")
    if len(mounts) != 1 or mounts[0][2] != b"proc":
        raise ValueError("cannot identify proc mount")
    options = mounts[0][3].split(b",")
    if any(option.startswith(b"hidepid=") and option not in {b"hidepid=0", b"hidepid=off"}
           for option in options):
        raise ValueError("filtered process visibility")
    return data


def _process_graph(proc, roots):
    """Read UID-independent ancestry, then inspect threads of related processes.

    Leader stat is needed to discover descendants and surviving session jobs.
    Unrelated processes' private references and thread metadata are not evidence
    required by this execution's ownership proof.
    """
    leaders = {}
    for entry in proc.iterdir():
        if not entry.name.isdecimal():
            continue
        pid = int(entry.name)
        try:
            leaders[pid] = _process_stat(entry, pid, pid)
        except FileNotFoundError:
            try:
                entry.stat()
            except FileNotFoundError:
                continue
            raise
    related = set(roots)
    while True:
        found = {pid for pid, p in leaders.items()
                 if p.parent in related or p.group in roots or p.session in roots}
        if found <= related:
            break
        related |= found
    graph = dict(leaders)
    for pid in related & leaders.keys():
        leader = leaders[pid]
        threads = list((leader.entry / "task").iterdir())
        if not threads or any(not thread.name.isdecimal() for thread in threads):
            raise ValueError("incomplete execution thread list")
        if pid not in {int(thread.name) for thread in threads}:
            raise ValueError("missing execution thread-group leader")
        for thread in threads:
            tid = int(thread.name)
            observed = _process_stat(thread, tid, pid)
            if tid == pid:
                if observed.identity != leader.identity:
                    raise ValueError("execution changed during graph collection")
            elif tid in graph:
                raise ValueError("duplicate execution thread identity")
            else:
                graph[tid] = observed
    return graph


def _check_process_graph(graph, shells, closed_shells):
    roots = set(shells)
    for pid, started in closed_shells.items():
        pid = int(pid)
        observed = graph.get(pid)
        if observed is None or observed.started == started:
            roots.add(pid)  # Preserve ancestry after closure; don't follow reused PIDs.
            if observed is not None and observed.live:
                raise TaskError("A closed task shell is still running; wait for exit before retrying cleanup")
    descendants = set(roots)
    while True:
        found = {tid for tid, p in graph.items() if p.parent in descendants or p.leader in descendants}
        if found <= descendants:
            break
        descendants |= found
    for tid, process in graph.items():
        # Even another 'allowed' shell cannot exempt a live descendant. Reparented
        # jobs can retain a shell's session/group after the shell itself exits.
        child = tid in descendants and (tid not in roots or process.parent in descendants)
        session_job = tid not in shells and (process.group in roots or process.session in roots)
        if process.live and (child or session_job):
            raise TaskError(f"Process {tid} may still use the selected execution; quit it before cleanup")


def stopped_execution(paths, shells, *, closed_shells=None, own_lock=None, proc=Path("/proc")):
    """Prove the registered shells/families stopped, not global host non-use.

    Detached unmanaged processes, references, namespaces and external mount views
    are outside V1. Registered runtime identity and ancestry remain fail closed.
    The caller excludes workflow controllers and repeats this before deletion.
    """
    if not sys.platform.startswith("linux") or not proc.is_dir():
        raise TaskError("Forced cleanup requires readable local Linux process evidence")
    closed_shells = closed_shells or {}
    roots = set(shells) | {int(pid) for pid in closed_shells}
    try:
        if any(Path.cwd().is_relative_to(path) for path in paths):
            raise TaskError("Run forced cleanup from outside all execution deletion targets")
        if not roots:
            return {}  # Herdr proved all registered terminals absent; no known shell remains.
        _process_visibility(proc)
        graph = _process_graph(proc, roots)
        _check_process_graph(graph, shells, closed_shells)

        def inspect(pid, process):
            expected = shells[pid]
            shell = shell_executable(expected["argv"])
            current = _process_stat(process.entry, pid, pid)
            argv = (process.entry / "cmdline").read_bytes()
            cwd = Path(os.readlink(process.entry / "cwd"))
            if (not process.live or process.kernel
                    or not os.path.samefile(process.entry / "exe", shell)
                    or argv != os.fsencode("\0".join(expected["argv"]) + "\0")
                    or cwd != expected["cwd"] or current.identity != process.identity
                    or current.state != "S" or current.group != pid or current.session != pid
                    or current.terminal == 0 or current.foreground != pid):
                raise TaskError("Task shell executable, argv or idle terminal identity changed; cleanup refused")

        for pid in shells:
            if pid not in graph:
                raise TaskError("Task shell changed or is invisible; run cleanup on Herdr's host/PID namespace")
            if graph[pid].kernel:
                raise TaskError("Task shell cannot be a kernel task; forced cleanup refused")
            inspect(pid, graph[pid])
        after = _process_graph(proc, roots)
        _check_process_graph(after, shells, closed_shells)
        for pid in shells:
            if pid not in after or after[pid].identity != graph[pid].identity:
                raise ValueError("registered shell identity changed")
            inspect(pid, after[pid])
        # A fork during the second shell inspection must also refuse. Unrelated
        # host process churn does not invalidate this execution's observation.
        final = _process_graph(proc, roots)
        _check_process_graph(final, shells, closed_shells)
        if any(pid not in final or final[pid].identity != graph[pid].identity
               or final[pid].state != "S" for pid in shells):
            raise ValueError("registered shell changed before deletion")
        return {str(pid): graph[pid].started for pid in shells}
    except (OSError, ValueError, IndexError):
        raise TaskError("Cannot prove registered execution processes have stopped; nothing further was removed") from None


def stopped_loop_execution(path, shells, *, proc=Path("/proc")):
    """Prove original shells and attributable workers stopped for Linux V1 abort.

    Reuse the registered-execution ownership proof. Unrelated private checkout
    references and fully detached/untracked helpers are outside this policy;
    success does not establish machine-wide checkout non-use.
    """
    if not shells:
        raise TaskError("Missing original task shells cannot prove an interrupted turn stopped")
    try:
        return stopped_execution([path], shells, proc=proc)
    except TaskError as error:
        raise TaskError(
            "Cannot prove registered task execution stopped; --abort refused. "
            "Keep the original panes open, quit agents and child/background/session jobs, "
            "then retry from outside the checkout on Herdr's Linux host/PID namespace. "
            "Original shell executable/argv/cwd/TTY and PID/start identity, plus complete "
            "process-family evidence, must remain verifiable. Inspect task contexts and "
            "the original shells; do not replace panes or clear the claim to bypass this check"
        ) from error
