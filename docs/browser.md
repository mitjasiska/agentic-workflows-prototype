# On-demand worktree browser

AWL's optional Linux browser integration uses one `ab-worktree` interface for
Codex, Pi, and custom shell-capable agents. Browser access is an explicit task
capability. A repository name or task description never enables it automatically.
Tasks without opt-in start no Chrome process and need no Node installation or
browser daemon.

## One-time user setup

From a stable AWL checkout, run:

```sh
python3.12 scripts/install_browser.py
```

The guided setup detects Git, Python, `sha256sum`, `readlink`, `agent-browser`,
and Chrome. At an interactive terminal it asks before downloading missing optional
dependencies. For explicit unattended consent to those downloads, use:

```sh
python3.12 scripts/install_browser.py --install-deps
```

Missing `agent-browser` is installed with npm into a private user prefix under
`~/.local/share/awl-browser-npm`, with a shortcut in `~/.local/bin`. This requires
Node.js 24+ and npm; absent prerequisites produce a concrete diagnostic. Chrome
is downloaded using `agent-browser install`, without system package installation.
Existing supported installations are reused. Setup itself launches no browser.

Managed preparation/connect-only transport supports **agent-browser 0.39.0**.
An incompatible existing installation is preserved and reported for explicit
operator upgrade/downgrade. The version pin bounds the local socket protocol and
upstream launch behavior used by this integration. See the upstream
[installation guide](https://agent-browser.dev/installation) and
[versioned source](https://github.com/vercel-labs/agent-browser/tree/v0.39.0/cli/src).
Legacy lazy calls continue to use the installed CLI's own command surface.

Setup installs `ab-worktree` and its Python companion `ab_worktree_session.py`
in `~/.local/bin`, and this guide in `~/.local/share/awl-browser/browser.md`.
It creates `ab-pi` and `ab-codex` only when those names are free. Custom wrappers,
browser configurations, shell startup files, and personal instruction text are
preserved. An install manifest permits updates only when installed managed files
still match their recorded hashes. Unmodified DEV-94 helper/guide copies are also
recognized for upgrade. Edited files, malformed managed instruction pointers,
symlinks/hard links at file destinations, and conflicting destinations fail safely
for manual reconciliation. Repeat setup leaves identical files untouched.

Setup appends a short pointer to each native agent's selected instruction file:

- Codex: first nonempty `AGENTS.override.md` or `AGENTS.md` in
  `${CODEX_HOME:-~/.codex}`; otherwise creates `AGENTS.md`.
- Pi: first existing regular file among `AGENTS.override.md`, `AGENTS.md`,
  `AGENTS.MD`, `CLAUDE.md`, and `CLAUDE.MD` in
  `${PI_CODING_AGENT_DIR:-~/.pi/agent}`, including an empty file.

These discovery rules preserve existing instruction precedence. Explicit
`--codex-home`, `--pi-home`, `--bin-dir`, and `--data-dir` override destinations.
`--skip-instructions` installs only the generic commands and guide, useful for
custom agents. Rerun setup when instruction precedence or agent homes change.
Pi's `--no-context-files` disables discovery; Codex's instruction size limit can
truncate large files. The installed guide is a snapshot of this canonical guide.

Ensure the controller and agent shells can find `~/.local/bin`. An absolute
helper path works too; the handoff supplies one. Restart existing terminal/Herdr
and agent processes after changing PATH. To check installation without writes:

```sh
python3.12 scripts/install_browser.py --check
# Supply the PATH actually obtained from the target agent shell:
python3.12 scripts/install_browser.py --check --agent-path '/path/to/agent/bin:/usr/bin:/bin'
```

`--agent-path` checks executable visibility, not sandbox socket access. Connect-only
agents need the helper, its companion, Python 3, Git, `sha256sum`, and `readlink`;
only the host needs `agent-browser` and Chrome. Setup never installs system
libraries, uses `sudo`, changes AppArmor, or edits third-party agent settings.

## AWL task opt-in and identity handoff

Run the controller on the host, outside the coding agent's sandbox:

```sh
awl start DEV-7 --browser
awl loop DEV-7 --browser
awl review DEV-7 --browser
awl review DEV-7 --resume DEV-7-R1
```

These are alternative lifecycle entry points, not a sequence to run together.
`--browser` conflicts with `start --no-agent`. Loop controls preserve the saved
capability; omit the flag for `--continue`, `--retry-review`, `--extend`, etc.
An existing loop without browser opt-in cannot acquire it through recovery;
starting a new loop requires the existing lifecycle's explicit authorization.
Browser opt-in is per task execution, never a project-wide configuration default.
Project agent/model/profile defaults still use the [existing configuration](configuration.md).

Before each browser-dependent handoff, AWL uses the allocated context ID as both
actor ID and instance ID: for example `DEV-7-I1`, `DEV-7-R1`, and `DEV-7-R2`.
The controller prepares or reuses that exact session on the host and verifies a
snapshot. It then supplies the absolute helper path, exact identity, private
runtime directory, and a complete `env ... --connect-only` command prefix in the
handoff. Use that prefix on **every** call. Fresh and resumed reviewer handoffs
use the same persisted reviewer ID, including after pane recreation. A default
session or a session called `dev7-review` cannot stand in for `DEV-7-R2`.

Opt-in is retained on each actor and in the loop checkpoint. Each actor's runtime
directory is also retained, so changed controller environment variables cannot
create a second session during resume. Browser
preflight errors stop delivery before dependent agent work; the agent must not
try to compensate by starting Chrome in its sandbox. An agent-side `snapshot -i`
must succeed before reporting browser access/acceptance passed. A successful host
snapshot alone is not that evidence.

## Generic host and agent contract

Custom shell-capable agents need no plugin. From the **target checkout**, a host
controller or parent integration prepares each actor independently:

```sh
ab-worktree custom --instance task7-main --actor task7-main --prepare
ab-worktree custom --instance task7-sub1 --actor task7-sub1 --prepare
ab-worktree custom --instance task7-sub2 --actor task7-sub2 --prepare
```

Then give each actor only its own assignment. For example, this actor's calls are:

```sh
AWL_BROWSER_INSTANCE=task7-sub1 AWL_BROWSER_ACTOR=task7-sub1 \
AWL_BROWSER_CONNECT_ONLY=1 AWL_BROWSER_CONCURRENT=1 \
ab-worktree custom --instance task7-sub1 --actor task7-sub1 --connect-only snapshot -i
```

For a different runtime directory, propagate the exact absolute
`AWL_BROWSER_RUNTIME_DIR` printed by preparation. AWL also supplies
`AWL_BROWSER_AGENT` and `AWL_BROWSER_ROOT` to reject an agent/worktree mismatch.
`AWL_BROWSER_INSTANCE` and `AWL_BROWSER_ACTOR` reject conflicting explicit IDs;
assignments imply connect-only use even if the option is omitted. An environment
marked `AWL_BROWSER_CONNECT_ONLY=1` cannot call `--prepare`.

Each simultaneous actor needs a **distinct stable instance and actor ID**, even
when it has the same agent type and worktree as its parent. Tabs in one browser
are not actor isolation. A parent must assign and retain each child's IDs and
arrange host preparation before giving it browser work. Set
`AWL_BROWSER_CONCURRENT=1` when concurrency is known: missing instance/actor IDs
then fail. Reusing an instance with a different actor, or assigning a second instance to
the same actor in that runtime directory, is refused, even after close. Repeated preparation with the same actor/instance means reuse, not a new
actor; a framework must never assign both IDs to two different actors.

AWL allocates IDs for actors it launches through its context registry. It does
not discover or schedule third-party internal subagents. A framework unable to
supply distinct stable IDs and host preparation must report that limitation and
block those subagents' browser use. Do not inherit the parent's assignment.

Agent names are 1–24 lowercase ASCII letters/digits/underscores/hyphens, starting
with a letter. Instance/actor IDs are 1–64 ASCII letters/digits/dots/underscores/
hyphens, starting with a letter or digit. Options precede the browser command.
Physical Git worktree root, agent, and instance are NUL-separated and hashed with
SHA-256; the first 128 bits produce `awl-<agent>-<digest>`. Subdirectories and
symlink aliases reuse identity. Different worktrees, agents, or instances do not.
Moving a checkout changes its browser identity.

## Connect-only development loop

Append commands to the **exact prefix from your handoff**. For example:

```text
open http://localhost:3000
snapshot -i
find role button click --name Submit
get text main
eval document.title
console
errors
network requests
close
```

Start the app server with its normal project command. Prefer snapshots, semantic
and DOM queries, console errors, and network diagnostics. Refresh snapshots after
interactions. Screenshots are opt-in for visual/layout checks.

Connect-only supports: `open`/`goto`, `back`, `forward`, `reload`, `snapshot`
(`-i`, `-c`, `-C`, `--urls`, `-d DEPTH`, `-s SELECTOR`), `eval` (including
`--stdin`), `click`, `dblclick`, `hover`, `focus`, `check`, `uncheck`, `fill`, `type`,
`press`, `get` (`url`, `title`, `text`, `html`, `value`, `count`, `box`, `attr`),
`find` (`role`, `text`, `label`, `placeholder`, `testid`, with `--name`/`--exact`),
`console`/`errors` (`--clear`), `network requests` (`--clear`), `cookies`
(get/clear/set NAME VALUE with optional `--url`), `storage local|session`
(get/set/clear), `tab` (list/new/TAB_ID/close), `screenshot PATH [--full]`, and `close`.
Use the stable `tabId` returned by `tab list`: for example `tab t1` switches to
that tab, and `tab close t2` closes that specific tab. `tab close` closes the
active tab. IDs start at `t1` and remain stable when other tabs close; positional
integers such as `tab 1` are not supported by the pinned protocol.
Non-snapshot results are JSON data. Unsupported commands/options fail; the helper
never falls back to the launching CLI. Use the bounded command surface or report
the limitation, rather than calling raw `agent-browser` from the sandbox.

For legacy **single-actor, non-concurrent** host use, existing invocations remain:

```sh
ab-worktree codex open about:blank
ab-worktree pi snapshot -i
ab-worktree codex --instance personal-check snapshot -i
ab-worktree codex close
```

Without an assignment/managed mode these calls retain lazy startup and forward
arguments to `agent-browser`. `--session` overrides are always refused. Omitted
instance means one session per agent type/worktree. This compatibility path is
not the sandboxed or concurrent actor contract.

## Lifecycle, permissions, and recovery

Prepared sessions use private local Unix sockets under
`${XDG_RUNTIME_DIR:-/run/user/UID}/agent-browser/awl`, or an explicitly supplied
`AWL_BROWSER_RUNTIME_DIR`. This directory must be physical, owned by the user,
and mode 700. Records and locks are private and live outside Git. The socket path
must fit Linux's 108-byte bound. A per-instance lock prevents duplicate startup.
The client verifies socket ownership and continuity, daemon identity, and the exact
recorded worktree/agent/instance/actor before sending commands. Visible peers retain
PID/start-time checks; hidden PIDs require the
[host proof below](#pid-namespace-identity). It never spawns a CLI or daemon in connect-only mode, including after socket loss. Before browser
actions it sends the pinned protocol's non-launching `stream_status` probe, which
checks the Chrome connection without enabling streaming. A surviving daemon with
a lost browser is insufficient: unavailable Chrome blocks the action and requires
host recovery. If Chrome disappears between the probe and the action and the
daemon reports an automatic launch/restart, the helper rejects the response before
printing any snapshot or command result. Do not treat that interaction as passed;
rerun host preparation. Closing an owned daemon remains possible after browser loss.

Host startup uses an empty managed browser config and fresh isolated browser
state, preserving existing user configuration. Browser environment overrides
are ignored except `AGENT_BROWSER_EXECUTABLE_PATH`; user profiles, restore names,
remote providers, CDP attachment, and auto-connect cannot select another browser.
Separate actor sessions isolate navigation, tabs, cookies and storage. Closing
one actor affects only its recorded daemon. No automatic cleanup closes unrelated
or user-owned browsers. Existing unowned/stale sockets are refused, not adopted.
After a normal close, host preparation can restart that same recorded actor.
If a new actor requests an instance already owned by another actor, preparation
refuses without reserving the new actor against that instance. Retry the new
actor with a distinct stable instance ID; successful assignments remain fixed.

Missing tools, failed startup, inaccessible sockets, or identity mismatches print
the exact actor and a host recovery command. Run it in the same checkout on the
host, then retry the workflow's safe continuation. Do not invent a replacement ID.
For an unowned/stale socket or a failed daemon, inspect that exact session on the
host before any manual cleanup; never use bulk session cleanup. A helper upgrade
or agent restart may be necessary if installed commands or PATH differ.

For Chrome startup failures check its actual executable path, missing shared
libraries, and kernel/AppArmor user-namespace diagnostics. Downloaded Chrome
versions can change the executable path. An administrator can diagnose the
specific attachment using [Ubuntu's user-namespace guidance](https://documentation.ubuntu.com/release-notes/24.04/#unprivileged-user-namespace-restrictions).
AWL applies no policy changes. It refuses root/container/CI environments where
the supported upstream launcher would automatically add `--no-sandbox`.
Never disable provider/Chrome sandboxing, widen global permissions, or expose a
public browser/CDP endpoint to make a check pass.

### PID namespace identity

Linux translates `SO_PEERCRED` into the caller's PID namespace. A daemon outside
that namespace can report PID **0** while its Unix socket remains usable; this
is not evidence that Chrome is missing. See the kernel's
[credential translation](https://github.com/torvalds/linux/blob/v6.8/net/core/sock.c)
and [PID translation](https://github.com/torvalds/linux/blob/v6.8/kernel/pid.c).
The helper never reads `/proc/0/stat` or substitutes a supplied daemon PID.

Host preparation records a versioned continuity proof alongside the original
PID/start time: kernel boot ID, preparing PID namespace, peer UID/GID, and the
socket's device, inode, nanosecond change time, owner, group and mode, plus its
kernel listener inode and 64-bit socket cookie. For an existing record, only
successful verification of its original visible PID/start
time and inode can establish that proof. Connect-only never writes or upgrades
ownership records. Missing, malformed, duplicate or inconsistent proof fails
closed for a hidden PID.

Every connection checks the private runtime directory and exact persisted actor
and session assignments. It compares the socket metadata and peer credentials
with the host proof and rechecks the socket pathname after connecting, before
sending protocol bytes. Rebinding a pathname requires unlinking the old socket;
the filesystem metadata and kernel listener cookie checks reject a replacement,
even if the filesystem inode and timestamp are recycled. The helper resolves one
exact listener in `/proc/net/unix` and uses `NETLINK_SOCK_DIAG` to verify its name,
listening state and filesystem device/inode before comparing the cookie. The
diagnostic ABI exposes only the low 32 bits of the filesystem inode; the full
inode remains stored and checked in the separate socket continuity proof. Missing
or ambiguous listeners and denied/malformed kernel replies refuse access. See the
kernel [Unix socket diagnostics](https://github.com/torvalds/linux/blob/v6.8/net/unix/diag.c).
A changed boot or incompatible credential/socket metadata from a different mount
view also refuses access. UID/GID numbers must agree with the host proof; a
separate user namespace with unchanged mappings is supported.

A positive peer PID must still pass `/proc/PID/stat` start-time verification.
Within the preparing namespace its numeric PID must also match. Across namespaces
Linux can translate a visible PID to a different number; the start time and socket
proof must still agree. Missing/denied or malformed procfs evidence for a positive
PID is an identity constraint, never permission to skip a failed check.

PID 0 is accepted only with a complete matching host proof and a different caller
PID namespace. The helper also obtains **SO_PEERPIDFD** directly from the connected
socket and polls it for process exit before sending commands. This requires Linux
6.5+ and a sandbox policy permitting that socket option. On non-generic Linux
socket ABIs, Python must expose the `SO_PEERPIDFD` constant. A stopped peer, inherited
listener whose original daemon has exited, unsupported kernel or denied pidfd
fails closed. A pidfd is used for liveness, not its inode as a persistent process
identifier: older kernels share an anonymous inode for pidfds. See
[pidfd polling](https://man7.org/linux/man-pages/man2/pidfd_open.2.html).
The existing Chrome liveness probe and unexpected-relaunch refusal still apply.

This proof trusts the host preparer, kernel, filesystem metadata and private
ownership records. It assumes the user and privileged host administrators do not
maliciously rewrite those records, substitute mounts or transfer a live daemon's
listener to another process. It authenticates continuity of the host-assigned
socket under that ownership boundary; it is not a cryptographic attestation or
protection against a malicious process with the same user's unrestricted file
access. Actor checks prevent conflicting assignments and accidental cross-session
attachment; sandbox policy supplies isolation from hostile same-user actors.

A sandbox must permit the exact private runtime mount, ownership files/locks and
Unix socket. Its network namespace must expose that listener through
`/proc/net/unix` and permit `NETLINK_SOCK_DIAG`; a separate network namespace that
hides it is unsupported. A denied mount/socket, incompatible UID/GID mapping or
inaccessible identity evidence produces a diagnostic with the assigned actor and host recovery
command. Report that constraint through the operator's normal permissions process;
never start Chrome in the sandbox, change sandbox policy globally, expose TCP/CDP,
or silently select another directory/session.

For an existing managed installation, run `python3.12 scripts/install_browser.py`
on the host from the updated AWL checkout. Its manifest checks preserve personal
wrappers and edited files. Then run the diagnostic's exact `--prepare` command in
the original browser worktree on the host. This adds proof to the existing owned
session after verifying its legacy identity; it neither needs checkout edits nor
relaunches a healthy browser. Run the same actor's `--connect-only snapshot -i`
on the host and in the sandbox. A legacy record remains usable with visible
PID/start-time verification, but cannot authenticate PID 0 until host preparation
has added the proof. Do not manufacture proof from sandbox observations.

## Validation

Focused offline tests use disposable repositories and fake local daemons:

```sh
python3.12 -m unittest discover -s tests -p 'test_browser*.py' -v
```

Workflow review/loop tests also cover exact fresh/resumed handoffs. These tests
are not live browser acceptance. On a supported host, prepare three distinct
actors in one worktree, then have the sandboxed agents open a local app, snapshot,
set distinct cookies/local storage, navigate independently, and close one while
the others remain usable. Repeat across worktrees and on fresh/resumed review.
Record actual agent/Chrome versions and report unavailable Codex, Pi, custom-agent,
or multi-instance checks explicitly. Nightly and release-candidate full regression
follow the [canonical validation policy](lifecycle.md#validation-guidance).

### Dated transport evidence

2026-10-14 (DEV-108 implementation): the existing DEV-104-R1 socket reported
`SO_PEERCRED=(0, 1001, 1001)` inside Codex on Linux 6.8.0-142-generic. A
kernel-provided peer pidfd reported `Pid: 0` / `NSpid: 0`; the installed legacy
helper reproduced `/proc/0/stat` from the exact R1 worktree. No R1 metadata,
checkout or conversation was changed. The previously reported host success is
issue evidence, not a host check performed in this pass.

Two disposable fake daemons were prepared in one Codex tool PID namespace and
queried from another. With actual kernel peer PID 0, the updated helper returned
each actor's separate `snapshot -i`, rejected a conflicting actor assignment, and
closed one while the other stayed usable. Both ownership proofs, including kernel
listener cookies, stayed unchanged; the launch log contained exactly two daemons.
This is transport evidence, not live Chrome acceptance.
Direct `unshare` was denied. Paired DEV-104-R1 host/sandbox acceptance and an actual
user-level helper upgrade are **not_run**: this pass cannot write the host helper
installation or run outside Codex. The managed upgrade is covered in temporary-home
tests. Full offline regression was not run for this historical check; current
nightly and release-candidate obligations follow the canonical validation policy.
