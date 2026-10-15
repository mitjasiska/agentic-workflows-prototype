# Configuration

[Project overview](../README.md) · [Lifecycle reference](lifecycle.md)

Installation, project mapping, issue structure, task readiness, implementation
scope, agent selection, review validation, repository permission profiles, and
GitHub API credentials and SSH-agent setup for the Agentic Workflows Prototype (AWP) `awp` CLI.
The first public release is a **Developer Preview** for local, single-machine use.

## Installation and project mapping

V1 is Linux-only; Windows and macOS are unsupported. See the
[README setup](../README.md#setup) for prerequisites, Git-source installation with
uv, upgrade/uninstall commands, editable checkout installation, and first-task setup.
The distribution is `agentic-workflows-lite`, and its only installed command is
`awp`. The checkout's `./task` remains a development/compatibility entry point.
Commands shown as `task` in the deeper guides can be invoked as `awp` with the same
arguments.

The existing transport compatibility evidence covers Herdr 0.9.1, Codex CLI
0.157.1, and Pi CLI 0.87.1; it is not new live validation of packaging.

### Herdr Codex session hook

**Prototype setup decision:** the native Herdr Codex hook is an optional integration,
not an unconditional AWP installation prerequisite. Fresh Codex start, review,
integration, and loop delivery use the exact readiness marker and Codex provider
history to establish a thread ID. Explicit follow-ups can use that saved, verified
ID even when Herdr omits `agent_session`. Neither a pane title nor `READY` alone
establishes identity or delivery. See [receipt confirmation](architecture.md#codex-receipt-confirmation).

Some features require **matching live Herdr session evidence**:

| Feature | When native `agent_session` is missing |
| --- | --- |
| Fresh delivery and exact saved-thread follow-ups | May proceed after provider verification; AWP warns before queuing the task. |
| Context inspection | Reports `session unknown/stale`; this does not erase the saved provider identity. |
| Interrupted-pass recovery | Refuses uncertain runtime evidence even with a saved thread ID and result. No replay or replacement is permitted. |
| Automatic closure of a live agent during forced cleanup | Refuses without matching session metadata and independent provider/process/ownership evidence. Quitting the agent and jobs to the original shell remains the manual path. |

Install and **validate reporting in a fresh disposable session** before relying on
the last two features. The hook can provide this evidence, but installation alone
is insufficient: the live probe below found different results for Codex's shared
background server and `--no-daemon` mode. AWP does not install or enable hooks,
change launch mode, or relax recovery/cleanup checks. A conflicting or malformed
report still fails; a previously observed report disappearing during handoff is
also an error, not merely a warning. The relevant boundaries are
[`agent.py`](../task_start/agent.py), [`contexts.py`](../task_start/contexts.py),
and [`PassDelivery.runtime`](../task_start/pass_delivery.py).

#### Install and approve

These commands were confirmed against installed **Herdr 0.9.1** help:

```sh
herdr --version
herdr integration --help
herdr integration install --help
herdr integration install codex
herdr integration status
```

Before installation or upgrade, privately back up any existing affected files
(and note which do not yet exist). The v8 installation uses:

| User file | Purpose |
| --- | --- |
| `~/.codex/herdr-agent-state.sh` | Herdr-managed shell/Python session reporter, marked integration version 8. |
| `~/.codex/hooks.json` | Registers a `SessionStart` command invoking `bash '<absolute script path>' session`, with a 10-second timeout. Preserve other hooks. |
| `~/.codex/config.toml` | Ensures Codex hook configuration; the observed installation has `[features] hooks = true`. Preserve unrelated settings. |

`codex: current (v8)` from `herdr integration status` describes the installed
integration. It does **not** prove hook trust, execution, socket delivery, a
provider thread ID, or AWP resumability. Recheck version/help after upgrading;
these paths describe the observed default user configuration, not every custom
`CODEX_HOME` or managed configuration.

Review the script and registered command yourself before approving the Codex
hook trust prompt. This is separate from folder trust and tool approvals. In
Codex 0.162.1, `/hooks` shows the `SessionStart` source, command, enabled state and
trust state. Approval is normally a one-time decision for the current definition;
changed definitions require review again. Hooks execute commands, so inspect the
script as well as the displayed command. Do not bypass hook trust or sandbox
protections to make reporting work. See the
[official hook trust documentation](https://learn.chatgpt.com/docs/hooks#review-and-trust-hooks).

The inspected v8 script consumes `SessionStart` input and sends
`pane.report_agent_session` with source `herdr:codex` to the inherited Herdr Unix
socket. It requires `HERDR_ENV=1`, `HERDR_SOCKET_PATH`, `HERDR_PANE_ID`, `python3`,
a nonempty session ID and transcript path. A conflicting inherited
`CODEX_THREAD_ID` suppresses reporting. It catches socket errors silently;
successful script exit is not proof that Herdr received the report.

#### Verify and diagnose

Use an explicitly identified disposable Herdr pane with an authenticated Codex
CLI. Give it a unique harmless prompt asking only for `READY`, without tools or
file changes. Inspect that exact pane, never the most recent conversation:

```sh
herdr agent get <probe-name-or-pane-id>
herdr agent read <probe-name-or-pane-id> --source recent-unwrapped --lines 80
```

In the probe's Codex UI, use `/status` to obtain its provider session ID and
`/hooks` to inspect trust without toggling settings. Expected native evidence is
`agent_session: {agent: "codex", kind: "id", source: "herdr:codex", value: "<same ID>"}`
on the same pane, terminal and checkout. `agent: codex`, `idle`, and
`interactive_ready: true` only establish detection/readiness, not session identity.
AWP separately verifies provider history and exact delivery receipts.

If the field is absent, check the three integration files, enabled/trusted hook
state, startup warnings, Herdr environment in the launched process, Python
availability and socket accessibility. Check whether `/status` says **Local
background server**: the dated probe below did not report identity in that mode.
On a version whose `codex --help` supports it, a separate disposable
`codex --no-daemon` invocation is a diagnostic comparison, not a verified repair
for an active AWP task or a new AWP default. Do not restart a shared server or
change another task to investigate this.

Installing/approving the hook after a conversation starts does not backfill its
startup event. Attaching a UI to that existing conversation is not a fresh launch.
Whether a particular explicit resume emits a usable new report must be checked
against its exact saved ID; do not assume it repairs missing metadata. AWP's
automatic startup continuation recognizes folder-trust/sign-in menus, not every
hook-trust screen. Complete hook setup in the disposable session before starting
task work. If a workflow has already stopped, retain its context/checkpoint and
inspect the original pane; never replay a prompt or edit identity fields to pass
recovery. See [startup limitations](lifecycle.md#codex-first-use-trust-and-setup)
and [interruption recovery](lifecycle.md#checkpoint-and-interruption-recovery).

#### Remove or roll back

Herdr 0.9.1 exposes an explicit uninstall command (confirmed via installed help):

```sh
herdr integration uninstall --help
herdr integration uninstall codex
herdr integration status
```

Finish affected sessions first and make a fresh private backup of the three files.
Use the version's supported uninstall, then inspect the files and status for
remaining Herdr entries. Do not assume uninstall restores the previous whole
`hooks.json` or `config.toml`, or revokes Codex's saved trust. For exact rollback,
compare with the pre-install backup and restore only Herdr's changes: its script,
its `SessionStart` handler, and any hook feature setting introduced by installation.
Preserve subsequently added settings and other hooks; remove a newly created file
only if no unrelated content uses it. Do not turn off all Codex hooks as a blanket
uninstall. Review any remaining trust entry through Codex's hook UI. If the local
version lacks this uninstall interface, stop and consult its help before changing
files. Uninstall was not executed against the developer's installation in DEV-85.

#### Validation evidence and limits

**Historical observation, 2026-10-10 (DEV-83 issue snapshot):** manual installation
on Aquila ensured the three files, reported `current (v8)`, and prompted for hook
trust, which the user approved after inspecting the script. It did not restore
`agent_session` for the already-running/resumed reviewer. This is not a recovery
success claim; DEV-83's retained worktree, database and checkpoint were not touched.

**Fresh probes, 2026-10-15 (DEV-85):** Herdr client/server 0.9.1, integration v8,
Codex CLI 0.162.1; disposable workspace `w4J`, pane `w4J:p1`, terminal
`term_65de09bf75375156`, cwd `/tmp/dev85-codex-hook-vjbqgtcm`. Each invocation used
`--sandbox read-only --ask-for-approval never`, a distinct no-tools readiness
prompt, and the same on-disk configuration. The hook was already trusted; only
the control disabled it for that invocation. Each reached `READY` and `idle`.

| Fresh invocation | Codex `/status` session ID | Herdr `agent_session` |
| --- | --- | --- |
| Default (Local background server) | `01a13fa5-3f54-7a21-8d28-d16bee1aa376` | Absent, although `/hooks` showed SessionStart installed/active 1/1 and Trusted. |
| `--no-daemon` | `01a13fab-8ccc-78e0-81ac-e01db77dfbbb` | Matching ID, kind `id`, source `herdr:codex`, in both `agent start` and `agent get`. |
| `--no-daemon --disable hooks` (invocation-only control) | `01a13fac-8550-7121-a3a5-87cbe5147d46` | Absent; Codex detection and readiness still worked. |

The control changed no saved configuration. All probe agents exited normally;
only their disposable workspace was closed. Hashes of the three user integration
files were unchanged. Sanitized observations were retained under the probe's
temporary cwd; the table records the durable summary. The difference implicates
launch mode, but does not establish why shared-server reporting was absent.
No pre-install conversation was resumed, and no end-to-end live AWL recovery or
full Codex 0.162.1 compatibility claim is made. A local stdio provider-history probe
could not initialize inside the implementation sandbox; the independent ID
comparison above used Codex's `/status` UI.

Focused mocked tests cover provider-based delivery with missing native reports,
warning timing, matching/conflicting/malformed reports, loss of an established
report, exact saved-thread follow-ups, recovery refusal without replay, and cleanup
refusal without closure. These are contract tests, not live-hook evidence. Broad
regression and release-candidate acceptance remain subject to the
[validation policy](lifecycle.md#validation-guidance).

### Optional browser setup

Run `python3.12 scripts/install_browser.py` from a stable AWP checkout for guided
user-level prerequisites and helper installation. Downloads require consent;
custom shortcuts, personal instructions and browser configurations are preserved.
Use `awp start ISSUE --browser`, `awp loop ISSUE --browser`, or
`awp review ISSUE --browser` only for explicitly browser-enabled tasks. The host
prepares each allocated actor's exact instance before handoff; agents use the
supplied connect-only command. This capability is per execution, not inferred
from project mappings or enabled by repository-wide defaults. Normal AWP users
need no Node, Chrome, or browser daemon. See the [browser guide](browser.md) for
PATH checks, supported versions, subagent identity, runtime permissions and recovery.

### Private registry selection

| Mode | Registry location |
| --- | --- |
| Non-editable install (wheel or Git URL) | `~/.agentic-workflows-lite/projects.toml` |
| `./task` in a Git checkout, or `awp` editable from it | Permanent AWP checkout's `config/projects.toml` |

Selection starts at the loaded Python module, never the caller's working
directory. If its source root has no `.git` entry, AWP uses the user-private path;
it does not search parent directories, read a sibling `config/projects.toml`, or
write into site-packages. This also applies to unpacked sources without Git
metadata. A normal installation remains usable after its build checkout is removed.
The dashboard's HTML/CSS/JS and Pi helper are bundled with the installed Python
package and need no source checkout or frontend build.

Editable installations load Python directly from the selected Git checkout, so
source edits are visible without reinstalling. Both entry points use its existing
registry without duplication. When that source is in a linked worktree, discovery
verifies its Git identity and worktree registration, then verifies the permanent
checkout shares that exact Git common directory. It reads the registry there;
do not copy the registry into linked worktrees. A competing worktree registry,
aliased registry path, missing permanent checkout, or ambiguous Git identity is
refused. A broken `.git` entry fails safely rather than falling back to the home
registry. Inherited Git selector variables cannot redirect discovery.

Create the registry explicitly using the
[synthetic template](../config/projects.example.toml); no private configuration is
bundled or migrated. Missing files identify the expected path, malformed TOML
reports that path without echoing contents, and invalid mappings use the existing
validation errors. Status and dashboard surface configuration uncertainty through
their read model rather than guessing a project.
Explicit Python `load_projects(path)` calls retain their specified path.
Only synthetic
[examples](../config/projects.example.toml) are tracked. Match `linear_project`
exactly to the Linear project name and set `repo_name` and `base_branch`. Missing,
duplicate, unmatched, or still-marked example mappings fail before task preparation.
This remains the only authority for Linear-project → repository/base resolution. Machine-specific paths
and the Linear API key live in `~/.agentic-workflows-lite/config.toml`;
GitHub API access uses [local GitHub CLI authentication](#local-github-cli-authentication)
or an explicit command-scoped token as described below:

```toml
projects_root = "~/projects"

[linear]
api_key = "" # Fill in your personal API key locally

[agent]
kind = "codex"
model = "gpt-6-astra"
mode = "high"
```

## Manual namespace change

This section describes the historical migration from `.agentic-workflows` to
`.agentic-workflows-lite`. It is separate from the
[AWP repository and CLI migration](migration.md), which preserves the current
runtime namespace and existing Linear project/team names.

`~/.agentic-workflows-lite/config.toml` is the only default global configuration
path. The historical `~/.agentic-workflows/config.toml` is not read, copied, or
migrated. Stop controllers and finish active tasks before switching,
create the new private directory, and manually copy/reconfigure your global TOML
there (or start from the example). If retaining a token override, copy/recreate
`secrets.env` privately and switch automatic shell startup sourcing to
[command-scoped loading](#persistent-github-token-on-posix). Do not put either file in Git.

Machine-local context storage and retained integration directories also use
`~/.agentic-workflows-lite/`. Old execution evidence is not automatically adopted;
retain it for inspection. Do not rewrite pinned checkout/session paths to pretend
an old execution is a new one. No compatibility alias or migration script is
provided.

When upgrading a checkout from before the private-registry separation, save a
private copy of its populated `config/projects.toml` outside Git first, then
restore it to the ignored runtime path after updating. Fresh clones start from
the synthetic example.

Earlier Git history and previously installed/distributed snapshots may still
contain real mappings. Removing them from the current tree does not erase that
exposure. Inspect historical distribution separately; this change does not
rewrite history. Refresh installed private mappings using the
[skill instructions](../skills/create-linear-task/SKILL.md#establish-context-and-target);
public packages carry only examples.

## Project defaults

The resolved trusted permanent checkout may contain
`.agentic-workflows-lite/config.toml` for shared, Git-tracked defaults and
`.agentic-workflows-lite/config.local.toml` for personal overrides. Start from
[`config/project.example.toml`](../config/project.example.toml). Add
`.agentic-workflows-lite/config.local.toml` to that repository's `.gitignore`
**before** creating a local override. The loader refuses tracked or non-ignored
local files. The controller never copies these local files into task worktrees,
handoffs, checkpoints, or distribution artifacts; checkpoints retain only the
resolved execution values needed for recovery.

Only these tables and fields are supported:

```toml
[agent]
model = "implementation-model" # kind/model/mode are independently optional
[reviewer]
mode = "high"                  # independent of implementation
[loop]
timeout = 7200                  # warning threshold per loop pass, > 0 and <= 86400
```

Both roles accept `kind`, `model`, and `mode`, matching the global schema;
`reasoning` remains a legacy alias for `mode`, and both cannot occur in one table.
`[loop] timeout` is also allowed globally. The default is 7200 seconds (two hours)
per delivered pass. It is an advisory warning threshold: the controller keeps
observing the original agent after warning. `task loop --timeout SECONDS` overrides
it for a new loop. Standalone review/integration timeouts retain their command defaults.

Fields resolve independently: **CLI → project local → project shared → global →
existing built-in behavior**. An absent file, table, or field inherits. An empty
string is invalid rather than a reset. Changing agent kind does not reset model
or mode; the selected adapter validates the final combination. Model names retain
the existing syntax validation and provider-dependent availability. Missing agent
kind still requires configuration or a CLI selection; start may leave model/mode
to its agent, while loop, review, and integration require explicit resolved values.

Project overrides cannot contain credentials, Linear identity, mappings, base
branches, repository locations, trusted paths, Codex profiles, controller policies,
or test commands. Unknown keys, wrong types, malformed TOML, invalid selections,
and out-of-range timeouts fail before a fresh handoff. Project directories/files
must be real paths within the permanent checkout; symlinks, hard-linked files,
special files, and linked task worktrees are refused. No parent-directory search
or current-directory discovery is added.

Fresh start, review, integration, and loop initialization resolve these layers.
Start, from-scratch loop, and fresh integration also revalidate them after advancing
the permanent base, before preparing task workspaces or integration artifacts.
Invalid configuration blocks preparation; changed effective defaults require
inspecting the updated permanent checkout and retrying the command. The base update
remains in place, and the retry selects from its current configuration. This check
also applies to workspace-only start so a newly tracked local override cannot be
copied into a task checkout.

The active loop checkpoint pins resolved implementation/reviewer selections and
its warning threshold. Interrupt recovery, continuation, fixes, and re-review retain them;
same-reviewer standalone resume also retains its context settings. Editing project
files in either checkout does not hot-reload an active execution. Existing CLI
conflict checks on recovery remain in force. Workspace-only start validates project
files but skips agent selection; cleanup skips execution defaults as before. Keep
repository test policy in `AGENTS.md` and CI.

## Linear issue structure

Linear descriptions are user-defined. The optional
[`create-linear-task` skill](../skills/create-linear-task/SKILL.md) recommends an
opinionated structure; using that skill or its template is not required by AWP.
Configure the single collapsed-block convention in the existing machine-local TOML:

```toml
[linear.issue_structure]
mode = "warn" # required | warn | ignore
block_name = "Agent instructions"
```

Both fields and the table are optional, with the defaults shown above.
`task start` with an agent and from-scratch `task loop` share this policy:

| Mode | Missing configured block |
| --- | --- |
| `warn` (default) | Print a non-blocking warning to stderr and continue. |
| `required` | Fail before Git updates, workspace preparation, Linear status changes, or context allocation/agent launch. |
| `ignore` | Skip the structural check entirely. |

The check recognizes only a line beginning with `+++` or `>>>`, followed by the
configured block name, matched literally and case-sensitively. Spaces/tabs around
the name in the marker and CRLF line endings are accepted. It checks marker
presence only, without parsing block contents or requiring any other heading,
closing delimiter, or template. For example, `block_name = "Implementation notes"`
recognizes `+++ Implementation notes` rather than the default name.

Unsupported modes, a non-table policy, unknown policy fields, and non-string,
empty, multiline, or nonprintable block names are configuration errors, including
in `ignore` mode. Surrounding spaces in configured names are trimmed.
Workspace-only preparation (`task start --no-agent`) and cleanup skip these
execution settings, just as they skip agent selection.

In every mode, the complete current description reaches the implementation agent
unchanged. Scope, acceptance criteria, constraints, and task-specific guidance may
appear anywhere; a collapsed block gives its contents no additional authority.
Separate workflow-owned instructions govern lifecycle and safety behavior.
The skill continues to generate its default `Agent instructions` block even if
you configure another name for runtime validation.

## Implementation task readiness

The implementation agent performs a lightweight semantic assessment before edits
by default. Configure it in the same machine-local TOML as agent selection:

```toml
[implementation.task_assessment]
enabled = true
```

Omitting the table or `enabled` keeps assessment enabled; set `enabled = false`
to skip the explicit assessment and retain ordinary implementation behavior.
The value must be a TOML boolean. Non-table settings and unknown fields are
configuration errors. Workspace-only preparation and cleanup skip this execution
setting. No new configuration format or project-registry mapping is required.

`task start` and loop-owned implementation use the same guidance and unchanged
complete issue description. The agent reads just enough repository context to
assess the outcome, scope and constraints, verification, feasibility, and blocking
ambiguity. It does not require a template, headings, explicit acceptance criteria,
or an `Agent instructions` block. It must not invent product decisions, expand
scope, edit Linear, rewrite the description, create a persistent plan, or run broad
validation just for readiness. A `ready` outcome continues in the same agent turn
without human approval or another model invocation. A `blocked` outcome stops
before implementation edits and contains specific questions.

This is independent of [issue structure](#linear-issue-structure): `required`
still refuses a missing configured block before preparation, even with assessment
disabled. `warn` and `ignore` permit ordinary issues; missing structure alone
cannot block readiness. Credentials, workspace, lifecycle, and exact session
checks remain mandatory in either setting. Assessment occurs after normal workspace
preparation and the workflow-owned Linear status transition.

For interactive start, answer a blocked assessment in the existing implementation
conversation. For a loop, inspect the stopped result and clarify the task, then
use `task loop ISSUE --new` to deliver current requirements to the same verified,
idle implementation session. `--continue` remains for graceful pauses, and an
uncertain delivery cannot be retried automatically. See the
[readiness result contract](lifecycle.md#implementation-readiness-outcomes) and
[loop recovery](lifecycle.md#checkpoint-and-interruption-recovery).

## Implementation scope discipline

Configure scope guidance in the machine-local TOML:

```toml
[implementation.scope]
policy = "strict" # strict | balanced
```

Omitting the table or `policy` defaults to `strict`. Only `strict` and `balanced`
are accepted, with surrounding whitespace trimmed. Unsupported values, non-table
settings, and unknown fields are configuration errors. Workspace-only preparation
(`task start --no-agent`) and cleanup skip this execution setting.

| Policy | Guidance |
| --- | --- |
| `strict` (default) | Prefer the smallest coherent change that satisfies the issue. Avoid unrelated refactors, formatting, renames, speculative documentation clarification, and opportunistic improvements. |
| `balanced` | Permit small, clearly relevant adjacent improvements when justified by the requested outcome. No unrelated feature work or broad cleanup. |

Both modes allow necessary supporting changes, regression tests, documentation,
and safety fixes for a complete, safe solution. `task start`, loop-owned
implementation (including completion and fixes), and independent review use the
same resolved policy. Fresh review and same-reviewer re-review examine each
substantive change against the complete issue and report unnecessary expansion
as actionable findings with concrete evidence and a justified correction, without
nitpicking necessary supporting work.

This is behavioral guidance, with no automatic edits, pruning, or rejection based
solely on diff size, file counts, or changed filenames. User-defined issue content
supplies task scope wherever it appears; the policy requires no template, heading,
or `Agent instructions` block. Workflow-owned lifecycle and safety instructions
remain authoritative. The separate issue-structure, task-readiness, and review
validation settings keep their existing behavior.

## Agent and reviewer selection

`kind` may be `codex` (the default implementation) or `pi`. For `task start`,
`model` and `mode` are optional; when omitted, the selected agent's own local
default is used. The legacy `reasoning = "high"` field remains accepted as an alias
for `mode` so
existing Codex installations continue to work. Do not set both fields.

Model names are passed unchanged to the selected CLI. Workflow `mode` means
reasoning/thinking effort, not Pi's unrelated `--mode text|json|rpc` output flag.
Codex receives it as `--config 'model_reasoning_effort="high"'`; Pi receives it
as `--thinking high`. `none` and `off` are mapped to the spelling used by each
agent. Codex supports `none`/`off`, `minimal`, `low`, `medium`, `high`, `xhigh`,
`max`, and `ultra`; Pi supports `off`/`none` through `max` but not `ultra`.
Unknown mode names fail before workspace or Linear mutation. When a Pi mode is
requested, the adapter uses Pi's local RPC mode in the exact worktree before task
submission to resolve the selected model, supported thinking levels, and effective
level. It rejects a model/mode pair if Pi would silently clamp it. Model/account
support is ultimately determined by the selected CLI. Login, repository trust,
and each agent's own permission settings remain under your control. Although Pi
itself accepts thinking suffixes such as `model:high`, Agentic Workflows Prototype rejects
that syntax because Pi may silently clamp it. Keep `model` plain and put every
thinking choice in workflow `mode`/`--mode` so it follows one validated path.

Per-command choices override the configuration layers without modifying them:

```sh
task start DEV-7 --agent pi
task start DEV-7 --agent codex --model gpt-6-astra --mode high
```

`--agent`, `--model`, and `--mode` resolve independently. For example,
`--agent pi` retains the configured model and mode; it does not silently choose
Pi-specific values. Precedence is command override, project local, project shared, then global
`[agent]`; an omitted model or mode retains the selected CLI's own local default. An unavailable or
unknown requested agent fails; there is no fallback to Codex.

Configure review separately from implementation in the machine-local config:

```toml
[reviewer]
kind = "codex"
model = "gpt-6-astra"
mode = "high"
```

Fresh review requires an explicit model and mode in the resolved `[reviewer]` layers or command flags.
Resuming a review retains its saved agent/model/mode and rejects selection flags.
See [review lifecycle](lifecycle.md#task-review) for exact resume requirements.

Configured loop use needs only `task loop DEV-7`. It resolves initial implementation
from `[agent]` and fresh review from `[reviewer]`, requiring explicit model and mode
for both roles. Optional `--i-agent`, `--i-model`, and `--i-mode` override implementation;
`--r-agent`, `--r-model`, and `--r-mode` override review. Each override resolves
independently over its role's configuration. Established implementation contexts
keep their recorded settings and reject `--i-*`. Generic `--agent`, `--model`, and
`--mode` remain available on `task start` and `task review`, but are not accepted by
`task loop`. See [loop lifecycle](lifecycle.md#task-loop) for startup and controls.

`task integrate DEV-7` also resolves its single execution role from `[agent]`.
Optional `--agent`, `--model`, and `--mode` override those fields independently;
model and mode must both be explicit before integration mutation or launch.
It always creates a fresh integration context; it does not inherit or change the
implementation context's settings. See [integration recovery](lifecycle.md#task-integrate).

## Review validation

The optional machine-local setting applies to standalone and loop-owned reviews,
including same-reviewer re-review:

```toml
[review.validation]
strategy = "focused_first" # focused_first | exhaustive
```

`focused_first` is the default: inspect the full diff, batch actionable findings,
and use targeted tests on both findings and clean passes without automatically
escalating to a full suite. Focused execution does not limit valuable test creation.
`exhaustive` requests full validation on every review pass, even with findings.
Both preserve explicit Linear issue and repository test requirements and read-only
review safety. Invalid tables or strategy values are rejected before execution
mutation. Workspace-only start (`--no-agent`) and cleanup skip this execution setting.
The implementation handoff calls for thorough tests, focused execution, and optional
TDD; there is no configurable methodology setting. Explicit local/per-pass checks
remain required; merge-time regression may be satisfied externally where permitted
and remains pending until evidenced. See [validation guidance](lifecycle.md#validation-guidance)
for CI ownership, human verification, and honest reporting.

## Codex permissions for trusted repositories

Agentic Workflows Prototype does not relax Codex permissions globally. With no repository
override, the adapter passes no profile, sandbox, approval, or network option, so
Codex's active defaults and machine configuration continue to apply. Model and
reasoning choices are independent of permissions.

A repository that needs unattended implementation can opt into one
named Codex configuration profile in the machine-local
`~/.agentic-workflows-lite/config.toml`. The key is the exact `repo_name` from
`config/projects.toml`, not a checkout or worktree path. For this repository:

```toml
[codex.repositories."agentic-workflows-prototype"]
profile = "agentic-workflows-lite-trusted"
```

Create the selected profile beside Codex's user config. With the usual
`CODEX_HOME`, the example above names
`~/.codex/agentic-workflows-lite-trusted.config.toml`:

```toml
approval_policy = "never"
sandbox_mode = "workspace-write"

[sandbox_workspace_write]
network_access = true
```

The profile flags were historically verified with Codex CLI 0.157.1 under the old
repository selector; this is not live verification of the renamed checkout. AWP
launches it as `codex --cd WORKTREE --profile agentic-workflows-lite-trusted ...`;
Codex loads and enforces the profile. `workspace-write` limits writes to the task
workspace and Codex's temporary roots while keeping protected paths such as
`.git` and `.codex` read-only. Network access permits commands inside that sandbox
to use outbound networking. `approval_policy = "never"` suppresses approval
prompts; an operation outside the sandbox fails and is returned to the agent
instead of escaping the sandbox. This setup does not use `danger-full-access` or
the bypass flag. See Codex's focused documentation for
[configuration profiles](https://developers.openai.com/codex/config-basic) and
[sandbox/approval behavior](https://developers.openai.com/codex/security).

Treat the override as a trust decision: the selected profile is layered on the
machine's Codex configuration and can expose repository content to processes and
network destinations used by the task. Keep the selection and profile file local;
do not commit populated local configuration, credentials, or approval state. An
unlisted repository receives no override, even if it uses the same agent or model.

Codex enforces filesystem, network, and approval boundaries. The task handoff's
rules—no commit, push, merge, pull request, `sudo`, or destructive Git
operations—are behavioral instructions, not hard sandbox rules. Implementation
and review commands may use the same trusted-repository profile; they stay
separate through fresh sessions, `AgentExecution.purpose`, and purpose-specific
handoffs, not through separate permission profiles.

Integration uses this same profile. Its agent only edits files in the isolated
checkout and writes structured completion in a retained private directory under
Python's temporary root (normally `/tmp`, or `TMPDIR`). The controller owns all
staging and Git metadata writes after verified completion. No additional sandbox
roots, approval override, or broader profile are required; see
[integration recovery](lifecycle.md#task-integrate).

This is deliberately a small Codex-only setup. Pi enforcement, cross-agent
capability mapping, stronger credential isolation, containers or external
sandboxing, a dedicated review profile, generalized command rules, and a broader
security framework remain out of scope.

## GitHub publishing

The workflow's GitHub REST client needs repository access and **Pull requests:
read** for private PR-history checks. `task pr` also needs **Pull requests: write**
to create/update PRs. Git transport separately uses your SSH key or HTTPS
credential helper; a working SSH key does not authenticate API requests.

### Local GitHub CLI authentication

Prefer an existing authenticated GitHub CLI (`gh`) and its local credential store.
With no token override, the controller privately reads the active `github.com`
account using `gh auth token --hostname github.com`. It never prints the token,
exports it into its environment, or saves it in workflow state. `gh` is optional:
public reads can run anonymously within API limits, and explicit tokens remain
supported for manual private-repository workflows.

For one-time interactive setup on the development host:

```sh
unset GH_TOKEN GITHUB_TOKEN GH_ENTERPRISE_TOKEN GITHUB_ENTERPRISE_TOKEN
gh auth login --hostname github.com --web --git-protocol ssh --skip-ssh-key
gh auth status --hostname github.com
```

An existing working login needs no replacement. The login command leaves existing
SSH keys to your own setup. GitHub CLI normally uses the system credential store,
but can fall back to a plaintext file when that store is unavailable; check its
reported storage location and configure/unlock the host's credential store for
secure storage. Do not use `--insecure-storage` for this setup. See
[GitHub CLI login](https://cli.github.com/manual/gh_auth_login).

Remove token exports and automatic sourcing of `secrets.env` from shell startup
files such as `~/.bashrc`. Unset old values in existing terminals, and restart old
agent/Herdr processes from the updated environment when practical: editing startup
files does not change running processes. Keep secrets, private keys and populated
configuration outside repositories, synced dotfiles, logs, and agent handoffs.

### Persistent GitHub token on POSIX

If `gh` is unavailable or you need a specific fine-grained token, retain a private
`~/.agentic-workflows-lite/secrets.env` with mode `600`, inside a directory with
mode `700`. Create it without truncating an existing file:

```sh
(
    umask 077
    mkdir -p "$HOME/.agentic-workflows-lite"
    chmod 700 "$HOME/.agentic-workflows-lite"
    touch "$HOME/.agentic-workflows-lite/secrets.env"
    chmod 600 "$HOME/.agentic-workflows-lite/secrets.env"
)
```

Use a trusted local editor and the placeholder-only
[`config/secrets.example.env`](../config/secrets.example.env) template. Never enter
a real token in a shell command, chat, or tracked file. Source this file only
inside the subshell for the intended controller command, with tracing disabled:

```sh
(
    set +vx
    unset GH_TOKEN GITHUB_TOKEN
    . "$HOME/.agentic-workflows-lite/secrets.env"
    task start DEV-7
)
```

Use the same subshell pattern for `task start DEV-7 --no-agent`, `task review`,
`task loop`, cleanup, or `task pr` when they need private API access. The parent
shell retains its previous environment. Only source files you own and trust:
sourcing executes shell code. Keep mode `600` after editor saves, and keep backups
private. Do not source the file from shell startup files or dump the environment
for diagnosis. The workflow does not load `secrets.env` itself.

For a fine-grained token, select only the required repositories and grant **Pull
requests: Read-only**, or **Read and write** if using `task pr`. Keep automatic
Metadata read access. The API token does not need **Contents: write**: publishing
uses separate Git transport and does not merge PRs through the API. See
[GitHub pull request endpoints](https://docs.github.com/en/rest/pulls/pulls).

### Credential selection and safe verification

Explicit `GH_TOKEN` takes precedence, then `GITHUB_TOKEN`, then the active `gh`
credential for `github.com`. Empty variables are skipped. An invalid, revoked,
or expired selected token is never silently replaced by another identity after
an API failure. Whitespace/control/non-ASCII token data is rejected without
printing it. GitHub CLI lookup is noninteractive and bounded to ten seconds;
unavailable `gh`/login permits anonymous reads, but missing required write auth
refuses before any write. A lookup timeout or transport failure stops with local
credential-store guidance. HTTP failures, including private-repository 404s,
never count as empty PR history.

The REST client uses Python HTTPS transport to `api.github.com`, honoring the
invoking environment's proxy and certificate settings. For the recommended `gh`
setup, this read-only check verifies private PR-list access without displaying
PR data or credentials. Replace the synthetic repository name:

```sh
gh api --hostname github.com 'repos/OWNER/REPO/pulls?state=all&per_page=1' --silent
```

A successful check establishes read access, not PR-write permission. Check the
selected account/token's repository access, expiration and organization/SSO
approval on 401/403/404 failures. Diagnose with fixed workflow messages rather
than printing tokens, headers, or raw exception bodies. See
[GitHub API troubleshooting](https://docs.github.com/en/rest/using-the-rest-api/troubleshooting-the-rest-api).

### Agent credential boundary

Implementation, review and integration agents do not need the controller's GitHub
API credential for ordinary file work. Fresh and recreated Codex/Pi launches,
Codex session API helpers, and Pi capability probes omit `GH_TOKEN`,
`GITHUB_TOKEN`, `GH_ENTERPRISE_TOKEN`, and `GITHUB_ENTERPRISE_TOKEN`. Herdr starts
agents inside an existing pane shell, so the adapter clears stale input at a
confirmed shell prompt and prefixes the launch with `env -u` for those variables.
This also covers values loaded by the pane's startup files. Controller API access,
model-provider authentication, `SSH_AUTH_SOCK`, and Codex permission profiles
remain unchanged. Already-running agent processes retain their original
environment; restart them to apply this boundary.

This prevents automatic token inheritance, not access by every process running
as your account. Local `gh` credentials, files, SSH agents and credential helpers
remain governed by host permissions and the existing agent sandbox. Do not ask
agents to read controller secrets during ordinary implementation/review. If a
task explicitly needs GitHub API access inside an agent, authorize that operation
separately with the least access needed using local setup; the workflow does not
forward raw tokens into agent prompts or provide an inheritance bypass. Manual
Git operations still use your normal SSH agent or HTTPS helper.

### Publishing prerequisites

The configured base must track a same-named remote branch on `github.com`. Fetch
and push URLs must each identify a single destination in the same repository.
The task worktree's effective Git configuration must resolve to the permanent
checkout's approved remote/repository. Worktree-specific URL rewrites or
conditional includes that redirect it are refused, as are mirror remotes and
remote groups. Cross-repository/fork publication is not supported. Existing Git
hooks, commit signing, credential helpers, SSH configuration, and terminal
environment (including
`GPG_TTY` when required by the user's signing setup) remain under user control.
Run from your normal terminal for interactive passphrases. `task pr` does not
choose an execution agent or need agent CLI authentication; its public prose was
already generated during the accepted independent review.

See [publishing lifecycle and retries](lifecycle.md#task-pr) for the frozen review
contract and conservative refusal rules.

## SSH agent on Linux

Use one SSH agent for the development login/session and load your encrypted Git
key once. Prefer your desktop/session-managed agent if `ssh-add -l` can reach it.
On a shell-only host without an agent, run the following once in the shell that
will start Herdr and the workflow (substitute your actual key path):

```sh
eval "$(ssh-agent -s)"
ssh-add -t 8h "$HOME/.ssh/id_ed25519"
ssh-add -l
```

Enter the key passphrase at `ssh-add`'s local prompt. `SSH_AUTH_SOCK` identifies the
agent; all terminals running `task` and the Herdr process must inherit the same
working socket. Start Herdr from that configured shell, or configure your login
session to provide the socket to both. An existing Herdr process will not acquire
new environment values from another terminal. Do not start a fresh agent in every
shell's startup file, disable private-key encryption, or put the passphrase in
configuration. Keys need reloading after the chosen lifetime, reboot, or agent
restart. See [ssh-agent](https://man.openbsd.org/ssh-agent) and
[ssh-add](https://man.openbsd.org/ssh-add).

From the same shell as `task`, verify the setup before a private-repository start:

```sh
ssh-add -l
git -C /absolute/path/to/repository ls-remote --heads origin
git -C /absolute/path/to/repository ls-remote --heads origin
```

Both Git calls should complete without another key-passphrase prompt. Verify new
host fingerprints through your normal trusted process; do not disable host-key
checking. `ssh-add -l` reporting no identities means load the correct key; inability
to contact the agent means repair the socket/session environment. If the key is
loaded but Git fails, inspect key selection, repository access and host trust.
AWP preserves Git's SSH configuration and reports failed fetch/remote lookup with
SSH-agent recovery guidance. It does not start an agent or manage passphrases.
