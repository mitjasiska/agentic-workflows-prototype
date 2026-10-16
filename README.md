# Agentic Workflows Prototype (AWP)

**From a development task to independently reviewed code.**

Agentic Workflows Prototype (AWP) is an open-source Python **agent orchestration framework** for AI-assisted software development. It coordinates coding agents through a structured workflow—from Linear issue intake and isolated workspaces to implementation, independent code review, automated fixes, and GitHub pull-request publication. Developers retain control over decisions, approvals, and merging.

AWP is a working prototype exploring the foundations of an **agentic software factory**. It supports real development workflows but remains experimental, with Linux as its initial execution platform.

The next-generation project, **[Agentic Workflows Lite (AWL)](https://github.com/mitjasiska/agentic-workflows-lite)**, will be a separate C#/.NET implementation designed to become the primary product. Rather than porting AWP's architecture directly, AWL will build on its practical lessons with a new design.

AWP will remain available as a development tool, behavioral reference, and bootstrap environment for AWL. Future Python development will focus primarily on compatibility, essential bug fixes, and maintenance.

## Core concepts

Examples use the illustrative Linear issue ID `TEAM-123`; substitute your actual ID.

| Concept | What it means for you |
| --- | --- |
| Issue | The Linear task holds the requirements. Start and review use its latest contents. |
| Project repository | A Linear project maps to a permanent local Git checkout and a base branch. Keep that checkout clean and on its configured base. |
| Task workspace | A separate Git worktree and branch, opened in Herdr. Implementation and review share this checkout. |
| Slice | An explicitly named part of an issue, with its own branch and workspace. The default is one issue, one branch, one PR. |
| Context | An implementation or reviewer conversation, labeled in its pane, such as `TEAM-123-I1` or `TEAM-123-R1`. Several contexts can share a checkout. |
| Review pass | A review of the current checkout against the local base. A fresh pass starts an independent conversation; an explicit resume keeps a particular reviewer's history. |

The normal path is: prepare an issue, start implementation, review the result,
address any findings, publish with `awp pr`, inspect and merge the PR, then clean
up. Implementation stops ready for review. Review reports findings without
applying fixes. The optional loop routes implementation defects back for fixes
and resumes the same reviewer. You own product and architecture decisions,
publication, merging, and marking the Linear issue complete.

Agent executions automatically emit bounded, experimental [local run metrics](docs/architecture.md#experimental-local-run-metrics)
for inspecting selected settings, observed durations, and outcomes.

## Setup

V1 supports **Linux only**; Windows and macOS are unsupported. You need Python
3.12 or newer, Git, and Herdr on `PATH`, with a running Herdr session. Agent
execution needs an authenticated Codex or Pi CLI on `PATH`; Codex is the default
implementation agent. Install
[uv](https://docs.astral.sh/uv/getting-started/installation/) for the user-scoped
tool commands below. AWP's Python runtime has no third-party dependencies.

### Install, upgrade, or uninstall

Python AWP uses the `agentic-workflows-prototype` repository and `awp` command.
The historical distribution name `agentic-workflows-lite`, Python modules,
`./task`, and private `~/.agentic-workflows-lite/` state remain unchanged.
`awl` is reserved for the future .NET AWL implementation; Python provides no alias.
Existing installations must follow the [operator migration runbook](docs/migration.md)
before moving a checkout or reusing the old repository name. Git-source installation
below requires the operator's GitHub rename to have completed.
A **Developer Preview** is the intended first public release, not a claim that
a formal release has shipped.

Install directly from Git; no retained AWP source clone is needed:

```sh
uv tool install --python 3.12 git+https://github.com/mitjasiska/agentic-workflows-prototype.git
awp --help
```

Ensure uv's tool executable directory (normally `~/.local/bin`) is on `PATH`.
For the current shell, if needed, run `export PATH="$HOME/.local/bin:$PATH"`.
AWP installs only the `awp` command. Installation does not change shell startup
files, Codex/Herdr settings, or private configuration, and does not install the
separate task-creation skill.

```sh
uv tool upgrade agentic-workflows-lite
uv tool uninstall agentic-workflows-lite
```

Uninstall removes the tool and command; private configuration and workflow history
remain under `~/.agentic-workflows-lite/`.

### Develop from a checkout

Maintainers can keep a checkout and install its current Python source in editable
mode (the checkout must remain available):

```sh
git clone https://github.com/mitjasiska/agentic-workflows-prototype.git ~/projects/agentic-workflows-prototype
uv tool install --python 3.12 --editable ~/projects/agentic-workflows-prototype
awp --help
~/projects/agentic-workflows-prototype/task --help
```

If another AWP installation already occupies the tool environment, uninstall it
first with `uv tool uninstall agentic-workflows-lite`. Update an editable checkout
through Git; Python source edits take effect without reinstalling. Reinstall after
changing package metadata or entry points. The repository's `./task` still works
without package installation and keeps its legacy help/usage identity. It requires
`python3.12` on `PATH`.

**Existing `./task` users:** editable `awp` and `./task` share the checkout's existing
`config/projects.toml` and machine settings, with no migration or duplication.
Normal, non-editable `awp` uses the user-private registry below instead. Switching
to that mode requires configuring that file explicitly; nothing is silently copied.

### Configure and start your first task

1. Keep permanent Git checkouts of the project repositories you want to work on.
2. Create `~/.agentic-workflows-lite/` privately (for example,
   `mkdir -p -m 700 ~/.agentic-workflows-lite`). Use
   [`config/local.example.toml`](config/local.example.toml) as the template for
   `~/.agentic-workflows-lite/config.toml`. Set `projects_root`, your Linear personal
   API key, and `[agent]`; keep the file private. The key needs issue/project/team-status
   read access and permission to update issues.
3. Use [`config/projects.example.toml`](config/projects.example.toml) as the template
   for the private project registry at the location for your installation:

   | Invocation | Private project registry |
   | --- | --- |
   | Normal installed `awp` | `~/.agentic-workflows-lite/projects.toml` |
   | Git-source `./task` or editable `awp` | `config/projects.toml` in the permanent AWP checkout |

   Replace the synthetic mappings and remove `example_only = true`.
   `linear_project` must match the Linear project name; `repo_name` and
   `base_branch` identify its local checkout and base. The base must track a
   same-named remote branch. Missing or ambiguous mappings stop the workflow.
   The caller's working directory never selects a registry. Linked AWP worktrees
   use the verified permanent checkout's registry; see
   [registry discovery](docs/configuration.md#installation-and-project-mapping).
4. For private GitHub PR-history checks and `awp pr`, use an existing `gh` login
   or follow the [local authentication setup](docs/configuration.md#local-github-cli-authentication).
   Explicit tokens can be scoped to one controller command. Agent launches omit
   GitHub token variables. For encrypted Git keys, configure a shared
   [SSH agent](docs/configuration.md#ssh-agent-on-linux) before starting Herdr.
5. From any directory, start an existing Linear issue:

   ```sh
   awp start TEAM-123
   ```

Both installation modes use the same `~/.agentic-workflows-lite/config.toml` for
machine settings and credentials. No registry belongs in site-packages.

Configure `[reviewer]` separately before using review; fresh reviews require an
explicit model and mode in that section or as command flags. The
[configuration guide](docs/configuration.md) includes examples, option precedence,
agent compatibility, and optional repository-specific Codex permission profiles.
The [Herdr Codex session hook](docs/configuration.md#herdr-codex-session-hook) is
optional for ordinary prompt delivery. Interrupted-pass recovery and automatic
closure of live agents require matching native session reports; install status
alone does not prove those reports work. Validate reporting before relying on
these features.
For web tasks, optional [on-demand browser setup](docs/browser.md) installs one
user-level helper for Codex, Pi, and custom shell agents. Explicit `--browser`
task opt-in prepares isolated actor sessions on the host for connect-only sandbox
access across worktrees.
Optional shared and personal
[project defaults](docs/configuration.md#project-defaults) apply per field from the
trusted permanent checkout. The old global namespace has no fallback; see the
[manual setup change](docs/configuration.md#manual-namespace-change).

## Using the workflow

### Create or refine a task

Start with a Linear issue, or use the
[`create-linear-task` skill](skills/create-linear-task/SKILL.md) to turn a rough
idea into one. It supports implementation, research, and experiment tasks, keeping
the human specification visible and execution guidance in a collapsed
`Agent instructions` section. This is the optional skill's recommended, opinionated
authoring convention. You can organize Linear issues however your team prefers;
the complete description supplies task context. Task creation is separate from the `awp` CLI.

By default, `awp start` and from-scratch `awp loop` warn and continue when that
block is absent. Configure one block name and a `required`, `warn`, or `ignore`
policy in [Linear issue structure settings](docs/configuration.md#linear-issue-structure).
Only an explicit `required` policy makes the configured block a prerequisite.

The standalone skill is validated with Codex; other skill hosts have not been
verified. This optional source-checkout installation is separate from the runtime
package. From an AWP checkout, install the whole directory, then generate its private
mappings from the checkout's configured `config/projects.toml`:

```sh
skill_dest="${CODEX_HOME:-$HOME/.codex}/skills/create-linear-task"
mkdir -p "$skill_dest"
cp -R skills/create-linear-task/. "$skill_dest/"
python3.12 skills/create-linear-task/scripts/sync_config.py --private \
  --output "$skill_dest/config.local.toml"
```

Public skill/plugin files contain examples only and cannot select a real target.
Keep the private installed configuration outside Git. Start a new Codex session
after installation or refresh. When project mappings change, follow the skill's [configuration refresh instructions](skills/create-linear-task/SKILL.md#establish-context-and-target).
Its own instructions also cover [classification](skills/create-linear-task/SKILL.md#write-and-classify-the-issue)
and [safe issue refinement](skills/create-linear-task/SKILL.md#render-deterministic-fields).

A separate [ChatGPT plugin package](plugins/create-linear-task/README.md) distributes
this same skill for web/mobile. See its README for distribution options, packaging,
and acceptance requirements. The standalone installation above remains independent.

### Start, reopen, or prepare a workspace

```sh
awp start TEAM-123
```

`start` updates the clean permanent base, creates or focuses the task workspace,
sets Linear to the team's `In Progress` status, and starts an implementation
agent with the current issue and checkout. Renaming an issue does not change its
existing workspace's identity. Uncommitted work in a reused task checkout is
preserved.

By default, the implementation agent briefly assesses task readiness before edits,
using the complete issue and relevant repository context. A ready task proceeds
automatically in the same session; a blocked task returns specific questions.
Ordinary issues need no template or `Agent instructions` block for this assessment.
Set `enabled = false` under `[implementation.task_assessment]` to skip it; the separate
issue-structure policy and workflow safety checks still apply. See
[readiness configuration and recovery](docs/configuration.md#implementation-task-readiness).

Implementation and independent review default to `strict` scope discipline: prefer
the smallest coherent solution, including necessary tests, documentation, and
safety fixes. Set `policy = "balanced"` under `[implementation.scope]` to permit
justified small adjacent improvements. Both are behavioral guidance without
diff-size or file-count limits. See [scope configuration](docs/configuration.md#implementation-scope-discipline).

Use `--no-agent` to prepare or focus the workspace without launching an agent.
It still updates Linear and works without an agent installation or `[agent]`
configuration. For option combinations, see the [CLI reference](#cli-reference).
If an agent is already running, continue in its pane, use `--no-agent` to focus the workspace,
or exit that session before requesting a fresh implementation handoff.

For separate implementation slices, select a stable scope name:

```sh
awp start TEAM-123 --slice importer
awp start TEAM-123 --slice exporter
```

Each slice has its own branch and workspace. Without `--slice`, start can reuse
exactly one candidate and retains its recorded scope. Multiple candidates require
an explicit slice; ambiguous or historical workspaces are refused. Review and
ordinary cleanup refuse multiple task worktrees/slices; forced cleanup can select
a proven merged execution with `--branch`. See
[workspace reuse and slices](docs/lifecycle.md#workspace-lifecycle-and-slices)
for legacy workspaces, remote branches, and PR-history restrictions.

### Review and re-review

When implementation and validation are ready, leave the task workspace open and
run an independent review:

```sh
awp review TEAM-123
awp review TEAM-123 --resume TEAM-123-R1
```

Fresh review opens a new reviewer pane in the task tab. It examines committed,
staged, unstaged, and untracked task changes against the local base, using the
latest Linear requirements. It needs a verified implementation context in that
workspace; a workspace prepared only with `--no-agent` is not sufficient. Review
does not advance the base or change Linear. Keep the checkout stable during a
pass: observed changes invalidate the result.

Agents create thorough behavioral and regression tests, use focused validation
on implementation and review passes, and batch review findings. TDD is optional.
External repository CI is the preferred owner of broad regression; its existence
and results are never assumed. Explicit local/per-pass checks remain required,
and unrun merge-time checks stay visible for CI or human verification. A clean
code review is not CI approval. AWP ends at a PR for human review and manual merge.
For development of AWP itself, ordinary merges require independent review/approval
and passing fast PR CI. Full regression runs nightly and gates the final release
candidate; it is not required for every ordinary merge. See the canonical
[validation policy](docs/lifecycle.md#validation-guidance) and
[CI setup and evidence](docs/architecture.md#github-actions). Configure
[`review.validation.strategy`](docs/configuration.md#review-validation) as
`exhaustive` to request full validation on every pass.

After addressing findings yourself or in the implementation session, choose a
fresh review for a new independent assessment, or `--resume` with an exact review
context ID for a follow-up in that reviewer's conversation. It stops if the
recorded conversation cannot be verified; see the [CLI reference](#cli-reference)
for selection and timeout options.

Results are `clean`, `findings`, `blocked`, or `failed`; a blocked result can report
that the implementation changed during review. Findings and limitations need
human follow-up. See the
[review reference](docs/lifecycle.md#task-review) for guarantees, exit codes, and
resume requirements.

### Automate implementation, fixes, and review

Start implementation and review of a Linear issue with one command:

```sh
awp loop TEAM-123
```

With no implementation context, the loop performs the same default workspace
preparation and Linear `In Progress` transition as `awp start`, then launches the
initial implementation with a structured completion contract. A validated result
automatically starts fresh independent review. This startup supports unsliced
workspaces only.

If Linear remains unavailable before the first handoff, repeat `awp loop TEAM-123`
after it recovers. The loop can reuse its original reservation only after proving
that no handoff was claimed and the requirements, checkout, and empty shell still
match. Saved selections and bounds stay in effect. After Ctrl+C, controller loss,
or a terminal disconnect, repeat the same command to observe the original pass
and collect its verified retained result without sending another prompt. Missing
or conflicting evidence stops for inspection. See
[recovery](docs/lifecycle.md#checkpoint-and-interruption-recovery).

If an implementation context already exists, plain `awp loop TEAM-123` continues
from its verified completion, or resumes that same conversation to finish work
and collect structured completion. It can also adopt a completed standalone
`awp review` with matching private evidence: findings go to the original
implementer, and re-review returns to that same reviewer. A valid clean review
finishes immediately. `--from-review` remains an explicit review entry option.
All paths route fixes to the original implementation context and resume the same
reviewer for focused follow-ups. Clean review finishes with one combined report.
Product/design/scope decisions, uncertain results, repeated findings, and pass
limits stop for human action.

Configured use needs only `awp loop TEAM-123`: `[agent]` supplies initial
implementation settings and `[reviewer]` supplies review settings. Both roles
require explicit resolved model and mode. Existing implementation contexts retain
their recorded settings and reject implementation overrides, except identical
selections during startup recovery.

The [CLI reference](#cli-reference) lists role selectors, bounds, and controls for
pausing, continuing, retrying a blocked review, extending an exhausted run, or
abandoning a stopped execution. Ctrl+C stops the waiting controller, not necessarily
the agent turn; ordinary `awp loop TEAM-123` reconciles the original pass.
See the [loop reference](docs/lifecycle.md#task-loop) for checkpoints and recovery,
[blocked review continuation](docs/lifecycle.md#blocked-review-continuation) for
retained findings and validation limitations,
[budget extension](docs/lifecycle.md#budget-exhaustion-continuation) for preserving
conversations and pending fixes, and the
[abort sequence](docs/lifecycle.md#explicit-abort-without-discarding-work) before
abandoning an interrupted pass. Stop manually detached helpers before reuse;
abort verifies registered executions, not machine-wide checkout non-use.

### Inspect local task status

```sh
awp status TEAM-123
awp status --all
```

Status combines saved loop, review, integration, publication, and cleanup evidence
with exact checkout/context identities. It is read-only and does not contact
Linear, GitHub, or agents. The overview groups registered AWP task workspaces by
configured Linear project; CLEAN and published tasks stay visible until workspace
cleanup. Unavailable discovery and ambiguous project mappings are reported explicitly.
Issue lookup also shows bounded saved history for diagnostic inspection. See the
[status contract](docs/lifecycle.md#task-status) for JSON, evidence limits, and operator hints.

To keep that view open with automatic updates:

```sh
awp dashboard
```

Open the printed localhost URL, choose a configured Linear project, then inspect
a task's current phase, progress, and attention guidance. Expand contexts,
workspace details, and history when needed. Use **Task report** to inspect and
copy the full saved loop report, or available diagnostics. The dashboard is
read-only, requires no frontend build or Node runtime, and stops with Ctrl+C.
For optional private Tailscale Serve access, see
[dashboard access and background setup](docs/lifecycle.md#task-dashboard) for
stale-data behavior, SSH forwarding, and setup-once Tailscale/systemd instructions.

### Find your conversations

```sh
awp contexts TEAM-123
```

This read-only view shows context IDs, locations, selected agents, status, and
whether a conversation can be resumed. It does not contact Linear or load workflow
configuration. An `unknown` or stale entry needs inspection; listing it does not repair or resume it.

### Publish the reviewed result

```sh
awp pr TEAM-123
```

This command authorizes committing the exact clean-reviewed task state, pushing
its branch, and creating or updating the matching GitHub PR. It prints the PR URL
and stops for your inspection. It never merges, approves, or completes the issue.
Keep the task workspace open and unchanged after review. Run review again if the
files, index, or task history have changed. If the remote base advances before
first publication, `pr` checks the rebase in disposable Git state. A conflict
leaves your task unchanged; a clean rebase updates it and stops for a fresh
independent review of the rebased result and preserved PR title/body. After that
review, rerun `pr` to publish the same rebased commit with that metadata.
Already published branches are never automatically rebased.

For a conflict while the reviewed implementation is still uncommitted, run
`awp integrate TEAM-123`. It uses `[agent]` and requires an explicit resolved model
and mode. The agent resolves and validates in a retained isolated checkout. A proven result is
installed locally against its frozen base and requires a fresh `awp review TEAM-123`.
Local installation can finish if main advances; `awp pr` separately checks whether
the reviewed result is still eligible for publication. Later main advancement does
not make an already confirmed push uncertain.
Human decisions or uncertain execution stop for inspection without installation.
For a stopped uncertain Codex attempt with no recorded session or task result, explicit
`awp integrate TEAM-123 --abandon TEAM-123-G1` can retain its evidence and permit a
separate fresh attempt after strict process/session absence checks.
See [isolated integration recovery](docs/lifecycle.md#task-integrate) for supported
states and interruption handling.

For feedback on a published PR, leave the additional edits uncommitted, run a
new `awp review TEAM-123`, then `awp pr TEAM-123`. Each cycle appends one reviewed
commit and updates the same PR. Finish any interrupted publication with `pr`
before starting another review. Follow-ups currently require the original
reviewed base to remain unchanged; base advancement stops for manual inspection.
See [follow-up publication](docs/lifecycle.md#follow-up-publication) for the
lineage and recovery guarantees.

The commit subject and PR title use `<type>: <summary> (TEAM-123)`. The type comes
from one canonical Linear category label; the reviewer supplies a public-safe
summary of the actual implemented result and observed validation. Titles use a
concise lower-case action summary, preserving names and acronyms, for example
`feat: add reviewed task PR publishing (DEV-18)`. The
[GitHub API setup](docs/configuration.md#github-publishing) covers token permissions
and safe verification. Keep your normal Git push authentication/signing setup;
native passphrase prompts stay in your terminal.

Rerun the same command after a failure: it verifies and reuses completed commits,
pushes, and PRs. Conflicting state stops for inspection. See the
[publishing reference](docs/lifecycle.md#task-pr) for the exact review contract,
credentials, supported states, and recovery guidance.

### Finish and clean up

After inspecting and merging the PR, mark the Linear issue completed and
ensure the merge is present in the configured local base. Exit the task's
agent/shell sessions and run cleanup from outside its workspace:

```sh
awp cleanup TEAM-123
```

Cleanup verifies completion and merge evidence, then removes the local task
worktree and branch, closes its Herdr workspace, and retires the associated
contexts. It does not update the base, remove remote branches, or change PRs or
Linear. Dirty, ambiguous, or unproven state is preserved. GitHub squash/rebase
merges are supported when their evidence matches the local task and base. See
[cleanup requirements and retries](docs/lifecycle.md#task-cleanup).

To discard an unpublished or proven merged local execution without changing its Linear status,
stop its controller, then run `awp cleanup TEAM-123 --force` from outside the task
workspace. `--force` is explicit authorization to delete dirty task contents and
retained integration checkouts/output, close the proven task workspace, and retire
contexts while preserving bounded provenance and context history. It may stop the
selected implementation/reviewer sessions after proving their exact identity and
provider inactivity. Working turns must finish or be explicitly stopped first.
Remaining workers, another execution's resource claim, unrelated panes, unmerged publication,
or uncertain identity cause refusal. Merged recovery also supports a manually
registered worktree without scope metadata when exact Git/Herdr identity and merge
evidence agree on one local execution. Abandoned integrations still own
their retained artifacts. If multiple local executions remain, select a proven
merged one with `awp cleanup TEAM-123 --force --branch <exact-local-branch>`.
V1 protects workflow ownership; stop unmanaged processes
using the files yourself. Ordinary cleanup without `--force` still requires a
completed, merged task. See
[forced local-execution disposal](docs/lifecycle.md#forced-local-execution-disposal).

### Handle a stopped or failed command

Read the reported state and inspect the named pane before retrying: an agent or
prompt may already have started. A handoff failure leaves the workspace and
Linear's `In Progress` status intact. Authentication or repository trust may need
action in the agent's pane. Do not run simultaneous starts for the same repository
or modify a workspace during review, publication, or cleanup. The
[failure and retry reference](docs/lifecycle.md#failure-and-retry-behavior)
explains which portions may already have completed.

## CLI reference

`ISSUE` is required unless shown as `[ISSUE]`; `dashboard` takes no issue.
Use `awp --help` or `awp <command> --help`; `-h` is also available at both levels.
Options below apply only to the commands listed.

| Command | Purpose and command-specific options |
| --- | --- |
| `awp start ISSUE` | Create or reopen an implementation workspace. `--slice NAME`: select a stable implementation slice. `--no-agent`: prepare/focus without launching an agent. `--agent KIND`, `--model MODEL`, `--mode MODE`: override implementation selection. `--browser`: prepare an isolated host browser before handoff. |
| `awp review ISSUE` | Run independent review. `--resume REVIEW_CONTEXT_ID`: return to that exact reviewer. `--agent KIND`, `--model MODEL`, `--mode MODE`: select a fresh reviewer. `--timeout SECONDS`: bound the completion wait (default 1800 after delivery). `--json`: structured review result. `--browser`: prepare the review actor's browser. |
| `awp loop ISSUE` | Run bounded implementation, review, and fixes. `--i-agent KIND`, `--i-model MODEL`, `--i-mode MODE`: initial implementation selection; `--r-agent KIND`, `--r-model MODEL`, `--r-mode MODE`: fresh reviewer selection. `--browser`: prepare isolated browsers for task actors. `--from-review`: explicitly enter review of completed implementation, adopting exact standalone review evidence when available. `--max-passes N`: total pass limit (default 6, range 1–40); `--max-reviews N`: review limit (default 3, range 1–20). `--timeout SECONDS`: advisory warning per pass (configured `[loop] timeout` or 7200, range 1–86400). `--json`: combined lifecycle result. Controls are listed below. |
| `awp status [ISSUE]` | Read local task state and bounded history. Supply either `ISSUE` or `--all` (required when omitting `ISSUE`): list registered workspaces by configured project. `--json`: canonical status projection. |
| `awp contexts [ISSUE]` | Inspect local conversations, optionally filtered by issue. `--all`: include retired contexts. |
| `awp dashboard` | Serve the read-only dashboard on `127.0.0.1`. `--port PORT`: local port (default 8765; 0 chooses a free port). `--trusted-origin HTTPS_ORIGIN`: allow one exact HTTPS origin through a local proxy. |
| `awp pr ISSUE` | Commit and publish the exact clean-reviewed task as a GitHub PR; stop for human inspection and manual merge. No additional command-specific options. |
| `awp integrate ISSUE` | Resolve unpublished integration conflicts in an isolated checkout, then require fresh review. `--agent KIND`, `--model MODEL`, `--mode MODE`: override integration selection. `--timeout SECONDS`: bound the completion wait (default 1800 after delivery). `--abandon CONTEXT`: abandon a stopped uncertain sessionless Codex integration context, retain evidence, and launch nothing; cannot accompany selection or timeout flags. |
| `awp cleanup ISSUE` | Remove a completed, proven merged local task. `--force`: authorize destructive disposal of one unpublished or proven merged execution, including dirty files and retained artifacts, without changing Linear status. `--branch BRANCH`: select an exact proven merged local execution; requires `--force`. Run outside the task checkouts after stopping active work. |

### Selection and timeout behavior

`--agent` selects `codex` or `pi`; `--model` selects the model; `--mode` selects
reasoning/thinking effort. Each overrides configuration independently: changing
only the agent retains the configured model and mode, with no fallback agent.
Start and integrate use `[agent]`; fresh review uses `[reviewer]`. Review,
integrate, and both loop roles require explicit resolved model and mode.
`loop` accepts only separate `--i-*`/`--r-*` selectors, not general
`--agent`/`--model`/`--mode` flags; established contexts keep their saved selection.

`start --no-agent` cannot accompany selection flags or `--browser`.
`review --resume` retains the recorded reviewer agent/model/mode and rejects
selection flags. For example: `awp review TEAM-123 --resume TEAM-123-R1`.

Loop timeout is advisory: it warns and keeps observing the original agent,
including late results. It is not a token budget. Review and integrate timeouts
bound the completion wait; they leave evidence for inspection and do not prove
that the agent stopped. Initial implementation counts as one loop pass; review
counting begins when review runs.

### Loop controls

Use one control at a time with `awp loop ISSUE` (optionally `--json`).

| Control | Meaning |
| --- | --- |
| `--pause-after-current` | Request a sticky pause before the next handoff; run from another terminal while the current pass finishes. |
| `--continue` | Resume the exact saved pause, not arbitrary failures or interrupted agent turns. |
| `--retry-review` | After resolving external blockers, continue a completed blocked review using its saved findings and checks; passing review evidence is still required. |
| `--extend` | Increase exhausted saved totals with `--max-passes` and/or `--max-reviews`, preserving conversations, counted passes, and unresolved findings. |
| `--status` | Inspect the checkpoint and warning threshold without resuming or contacting Linear. |
| `--new` | Explicitly replace an eligible stopped checkpoint with a new bounded run and fresh reviewer. It is not a retry shortcut: unresolved execution must be reconciled or aborted first; use `--extend` to preserve exhausted pending fixes (`--new --from-review` refuses to skip them), and `--retry-review` for blocked reviews. |
| `--abort` | Abandon an unfinished pass only after verified stopped execution on the Linux host. Stop the controller, quit task agents to their original shells, keep panes open, stop child/background jobs, and run outside the checkout. Preserves task files and session history; Ctrl+C alone does not abort. |

Pause, continue, retry, status, and abort reject selection, timeout, limit, and
browser flags. `--extend` accepts increased limits only. `--new` can select new bounds
and a fresh reviewer; `--from-review` can accompany `--new`, but not the other
controls. See [lifecycle details](docs/lifecycle.md#task-loop) for replacement
eligibility and recovery, and [configuration](docs/configuration.md#agent-and-reviewer-selection)
for selection precedence and supported modes.

## Project documentation

- [Configuration](docs/configuration.md): installation, project mapping, agent/reviewer settings, and permissions.
- [Task lifecycle reference](docs/lifecycle.md): workspace safety, review guarantees, cleanup, and retry cases.
- [Architecture and validation](docs/architecture.md): execution adapters, prompt delivery, context registry, tests, and recorded live-check limits.
- [Successor project](https://github.com/mitjasiska/agentic-workflows-lite): the separate future C#/.NET Agentic Workflows Lite (AWL).
- [AWP migration](docs/migration.md): inventory, operator approval gates, installation replacement, and recovery.
- [Repository instructions for agents](AGENTS.md): maintenance guidance and documentation ownership.

## License

Agentic Workflows Prototype is licensed under the [MIT License](LICENSE).
