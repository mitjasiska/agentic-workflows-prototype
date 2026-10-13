# Agentic Workflows Lite (AWL)

Agentic Workflows Lite (AWL) is a focused Python development controller for local,
single-machine workflows. Its first public release is a **Developer Preview**.
It takes a development task from a Linear issue to an agent implementation,
independent review, PR publication, and workspace cleanup, combining a task-creation
skill with a `task` command that coordinates your repositories, Git worktrees, and
interactive coding agents in Herdr.

You choose the task and scope, the workflow prepares the checkout and hands over
the current requirements, and you choose manual review or a bounded automatic
fix/review loop. You retain decisions about scope and publication.
Codex and Pi are supported execution agents; Codex is the default
implementation.

## Core concepts

| Concept | What it means for you |
| --- | --- |
| Issue | The Linear task holds the requirements. Start and review use its latest contents. |
| Project repository | A Linear project maps to a permanent local Git checkout and a base branch. Keep that checkout clean and on its configured base. |
| Task workspace | A separate Git worktree and branch, opened in Herdr. Implementation and review share this checkout. |
| Slice | An explicitly named part of an issue, with its own branch and workspace. The default is one issue, one branch, one PR. |
| Context | An implementation or reviewer conversation, labeled in its pane, such as `DEV-7-I1` or `DEV-7-R1`. Several contexts can share a checkout. |
| Review pass | A review of the current checkout against the local base. A fresh pass starts an independent conversation; an explicit resume keeps a particular reviewer's history. |

The normal path is: prepare an issue, start implementation, review the result,
address any findings, publish with `task pr`, inspect and merge the PR, then clean
up. Implementation stops ready for review. Review reports findings without
applying fixes. The optional loop routes implementation defects back for fixes
and resumes the same reviewer. You own product and architecture decisions,
publication, merging, and marking the Linear issue complete.

Agent executions automatically emit bounded, experimental [local run metrics](docs/architecture.md#experimental-local-run-metrics)
for inspecting selected settings, observed durations, and outcomes.

## Setup

You need Python 3.12, Git, and Herdr on `PATH`, with a running Herdr session.
Agent execution also needs an authenticated Codex or Pi CLI on `PATH`.

1. Clone [agentic-workflows-lite](https://github.com/mitjasiska/agentic-workflows-lite)
   and the project repositories you want to work on.
2. Copy [`config/projects.example.toml`](config/projects.example.toml) to
   `config/projects.toml` (Git-ignored), replace the synthetic mappings, and remove
   `example_only = true`.
   `linear_project` must match the Linear project name; `repo_name` and
   `base_branch` identify its local checkout and base. The base must track a
   same-named remote branch. Missing or ambiguous mappings stop the workflow.
   Keep the private registry in the permanent CLI checkout; linked CLI worktrees
   resolve that shared registry through Git. See [registry discovery](docs/configuration.md#installation-and-project-mapping).
3. Copy [`config/local.example.toml`](config/local.example.toml) to
   `~/.agentic-workflows-lite/config.toml`, creating the directory first. Set
   `projects_root`, your Linear personal API key, and your `[agent]` selection.
   Keep this populated file private. The key needs issue/project/team-status
   read access and permission to update issues.
4. For GitHub API access (`task pr` and PR-history checks), follow the
   [persistent token setup](docs/configuration.md#persistent-github-token-on-posix).
   Use [`config/secrets.example.env`](config/secrets.example.env) as the placeholder
   template.
   Store `GH_TOKEN` in `~/.agentic-workflows-lite/secrets.env` with mode `600`, and
   source that file from `~/.bashrc`. Reload it in existing shells after token
   changes. Git push still uses your separate SSH/HTTPS authentication.
5. Add this repository to `PATH`, then start a task:

   ```sh
   export PATH="/absolute/path/to/agentic-workflows-lite:$PATH"
   task start DEV-7
   ```

No package installation is required. The command works from any directory or
through a symlink on `PATH`. On Windows, use
`py -3.12 C:/path/to/agentic-workflows-lite/task start DEV-7`.

Configure `[reviewer]` separately before using review; fresh reviews require an
explicit model and mode in that section or as command flags. The
[configuration guide](docs/configuration.md) includes examples, option precedence,
agent compatibility, and optional repository-specific Codex permission profiles.
For web tasks, optional [on-demand browser setup](docs/browser.md) installs one
user-level helper for Pi and Codex across worktrees, with lazy browser startup
and isolated worktree sessions.
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
the complete description supplies task context. Task creation is separate from the `task` CLI.

By default, `task start` and from-scratch `task loop` warn and continue when that
block is absent. Configure one block name and a `required`, `warn`, or `ignore`
policy in [Linear issue structure settings](docs/configuration.md#linear-issue-structure).
Only an explicit `required` policy makes the configured block a prerequisite.

The standalone skill is validated with Codex; other skill hosts have not been
verified. Install the whole directory, then generate its private mappings from
your configured `config/projects.toml`:

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
task start DEV-7
task start DEV-7 --no-agent
task start DEV-7 --agent pi --model <provider/model> --mode high
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
configuration. It cannot be combined with agent selection flags. If an agent is
already running, continue in its pane, use `--no-agent` to focus the workspace,
or exit that session before requesting a fresh implementation handoff.

`--agent`, `--model`, and `--mode` override configuration independently for one
command. Changing only `--agent` retains the configured model and mode, so choose
values supported by that agent. There is no automatic fallback agent.

For separate implementation slices, select a stable scope name:

```sh
task start DEV-7 --slice importer
task start DEV-7 --slice exporter
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
task review DEV-7
task review DEV-7 --agent codex --model gpt-6-astra --mode high
task review DEV-7 --resume DEV-7-R1
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
code review is not CI approval. Lite ends at a PR for human review and manual merge.
See [validation guidance](docs/lifecycle.md#validation-guidance). Configure
[`review.validation.strategy`](docs/configuration.md#review-validation) as
`exhaustive` to request full validation on every pass.

After addressing findings yourself or in the implementation session, choose a
fresh review for a new independent assessment, or `--resume` with an exact review
context ID for a follow-up in that reviewer's conversation. Resume retains the
original agent/model/mode and cannot be combined with selection flags. It stops
if the recorded conversation cannot be verified.

Results are `clean`, `findings`, `blocked`, or `failed`; a blocked result can report
that the implementation changed during review. Findings and limitations need
human follow-up. Use `--json` for structured output or `--timeout SECONDS` to
change the default 1800-second wait after delivery. See the
[review reference](docs/lifecycle.md#task-review) for guarantees, exit codes, and
resume requirements.

### Automate implementation, fixes, and review

Start implementation and review of a Linear issue with one command:

```sh
task loop DEV-7
task loop DEV-7 --pause-after-current
task loop DEV-7 --status
task loop DEV-7 --continue
```

With no implementation context, the loop performs the same default workspace
preparation and Linear `In Progress` transition as `task start`, then launches the
initial implementation with a structured completion contract. A validated result
automatically starts fresh independent review. This startup supports unsliced
workspaces only.

If Linear remains unavailable before the first handoff, repeat `task loop DEV-7`
after it recovers. The loop can reuse its original reservation only after proving
that no handoff was claimed and the requirements, checkout, and empty shell still
match. Saved selections and bounds stay in effect. After Ctrl+C, controller loss,
or a terminal disconnect, repeat the same command to observe the original pass
and collect its verified retained result without sending another prompt. Missing
or conflicting evidence stops for inspection. See
[recovery](docs/lifecycle.md#checkpoint-and-interruption-recovery).

If an implementation context already exists, plain `task loop DEV-7` continues
from its verified completion, or resumes that same conversation to finish work
and collect structured completion. It can also adopt a completed standalone
`task review` with matching private evidence: findings go to the original
implementer, and re-review returns to that same reviewer. A valid clean review
finishes immediately. `--from-review` remains an explicit review entry option.
All paths route fixes to the original implementation context and resume the same
reviewer for focused follow-ups. Clean review finishes with one combined report.
Product/design/scope decisions, uncertain results, repeated findings, and pass
limits stop for human action.

Configured use needs only `task loop DEV-7`: `[agent]` supplies initial
implementation settings and `[reviewer]` supplies review settings. Optional
`--i-agent`, `--i-model`, and `--i-mode` override implementation; `--r-agent`,
`--r-model`, and `--r-mode` override the fresh reviewer. Both roles require explicit
resolved model and mode. Existing implementation contexts retain their recorded
settings and reject `--i-*` overrides, except identical selections during startup
recovery. Generic `--agent`, `--model`, and `--mode`
remain available on `task start` and `task review`; `task loop` uses only the
role-prefixed overrides.

Run `--pause-after-current` from another terminal while the command is working.
The current pass finishes; the next handoff waits for explicit `--continue`.
`--status` only inspects, including whether the pass warning threshold is exceeded.
Ctrl+C stops the waiting controller, not necessarily the agent turn; ordinary
`task loop DEV-7` reconciles it, while `--continue` remains exclusive to pauses.
To deliberately abandon an interrupted pass, stop the waiting controller with
Ctrl+C, quit task agents to their original shells while keeping panes open, then
run `task loop DEV-7 --abort` from outside the checkout. On Linux V1 it verifies
stopped registered executions and preserves implementation files and session
history. Stop manually detached helpers before reuse; abort does not prove
machine-wide checkout non-use. See the [abort sequence](docs/lifecycle.md#explicit-abort-without-discarding-work) for
fresh review and incomplete-implementation continuation.
Defaults are three reviews, six total passes, and a two-hour (7200-second)
warning threshold per delivered pass. Use `--max-reviews`, `--max-passes`, and
`--timeout` when starting the loop; `[loop] timeout` also configures the threshold.
At the threshold the controller warns once and keeps observing the original agent.
You can leave it working or use the controls above; verified late results still
proceed to review. This wall-clock threshold is not a token budget.
The initial implementation counts as one pass; review counts begin when review runs.
Use `--json` for the combined structured report. See the
[loop reference](docs/lifecycle.md#task-loop) for checkpoints, bounds, and recovery.

### Inspect local task status

```sh
task status DEV-7
task status DEV-7 --json
task status --all
task status --all --json
```

Status combines saved loop, review, integration, publication, and cleanup evidence
with exact checkout/context identities. It is read-only and does not contact
Linear, GitHub, or agents. The overview groups registered AWL task workspaces by
configured Linear project; CLEAN and published tasks stay visible until workspace
cleanup. Unavailable discovery and ambiguous project mappings are reported explicitly.
Issue lookup also shows bounded saved history for diagnostic inspection. See the
[status contract](docs/lifecycle.md#task-status) for JSON, evidence limits, and operator hints.

To keep that view open with automatic updates:

```sh
task dashboard
task dashboard --port 8766
```

Open the printed localhost URL, choose a configured Linear project, then inspect
a task's current phase, progress, and attention guidance. Expand contexts,
workspace details, and history when needed. The dashboard is read-only,
requires no frontend build or Node runtime, and stops with Ctrl+C. Use `--port 0`
to select a free port. See [local dashboard and SSH access](docs/lifecycle.md#task-dashboard)
for stale-data behavior and access from another machine.

### Find your conversations

```sh
task contexts
task contexts DEV-7
task contexts DEV-7 --all
```

This read-only view shows context IDs, locations, selected agents, status, and
whether a conversation can be resumed. `--all` includes retired history. It does
not contact Linear or load workflow configuration. An `unknown` or stale entry
needs inspection; listing it does not repair or resume it.

### Publish the reviewed result

```sh
task pr DEV-7
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
`task integrate DEV-7`. It uses `[agent]` (optionally overridden with `--agent`,
`--model`, and `--mode`) and requires an explicit resolved model and mode. The
agent resolves and validates in a retained isolated checkout. A proven result is
installed locally against its frozen base and requires a fresh `task review DEV-7`.
Local installation can finish if main advances; `task pr` separately checks whether
the reviewed result is still eligible for publication. Later main advancement does
not make an already confirmed push uncertain.
Human decisions or uncertain execution stop for inspection without installation.
For a stopped uncertain Codex attempt with no recorded session or task result, explicit
`task integrate DEV-7 --abandon DEV-7-G1` can retain its evidence and permit a
separate fresh attempt after strict process/session absence checks.
See [isolated integration recovery](docs/lifecycle.md#task-integrate) for supported
states and interruption handling.

For feedback on a published PR, leave the additional edits uncommitted, run a
new `task review DEV-7`, then `task pr DEV-7`. Each cycle appends one reviewed
commit and updates the same PR. Finish any interrupted publication with `pr`
before starting another review. Follow-ups currently require the original
reviewed base to remain unchanged; base advancement stops for manual inspection.
See [follow-up publication](docs/lifecycle.md#follow-up-publication) for the
lineage and recovery guarantees.

The commit subject and PR title use `<type>: <summary> (DEV-7)`. The type comes
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
task cleanup DEV-7
```

Cleanup verifies completion and merge evidence, then removes the local task
worktree and branch, closes its Herdr workspace, and retires the associated
contexts. It does not update the base, remove remote branches, or change PRs or
Linear. Dirty, ambiguous, or unproven state is preserved. GitHub squash/rebase
merges are supported when their evidence matches the local task and base. See
[cleanup requirements and retries](docs/lifecycle.md#task-cleanup).

To discard an unpublished or proven merged local execution without changing its Linear status,
stop its controller, then run `task cleanup DEV-7 --force` from outside the task
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
merged one with `task cleanup DEV-7 --force --branch <exact-local-branch>`.
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

## Project documentation

- [Configuration](docs/configuration.md): installation, project mapping, agent/reviewer settings, and permissions.
- [Task lifecycle reference](docs/lifecycle.md): workspace safety, review guarantees, cleanup, and retry cases.
- [Architecture and validation](docs/architecture.md): execution adapters, prompt delivery, context registry, tests, and recorded live-check limits.
- [Repository instructions for agents](AGENTS.md): maintenance guidance and documentation ownership.

Use `task --help` or `task <command> --help` for the available flags.

## License

Agentic Workflows Lite is licensed under the [MIT License](LICENSE).
