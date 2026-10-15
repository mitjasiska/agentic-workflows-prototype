# Task lifecycle reference

[Project overview](../README.md) · [Configuration](configuration.md) · [Architecture](architecture.md)

Command behavior, invariants, and refusal/retry cases for Agentic Workflows Lite
(AWL), whose first public release is a **Developer Preview**. Start with the
[README](../README.md#using-the-workflow) for the human workflow and command choices.

- [Start and status transition](#task-start)
- [Implementation readiness outcomes](#implementation-readiness-outcomes)
- [Workspace reuse and slices](#workspace-lifecycle-and-slices)
- [Failure and retry behavior](#failure-and-retry-behavior)
- [Fresh review and exact resume](#task-review)
- [Automatic implementation/review loop](#task-loop)
- [Read-only task status and history](#task-status)
- [Live local dashboard](#task-dashboard)
- [Reviewed PR publishing](#task-pr)
- [Isolated conflict recovery](#task-integrate)
- [Cleanup and partial failures](#task-cleanup)

## Task status

`task status ISSUE` and `task status ISSUE --json` inspect local evidence without
changing workflow state. `task status --all` (also with `--json`) is the dashboard
discovery seam. These commands use the same canonical projection in
[`status.py`](../task_start/status.py); text rendering contains no separate workflow
interpretation. They do not fetch Linear/GitHub, poll provider history, take
controller ownership, launch agents, repair records, or create state files. Reading
Git-visible content for review validity can take time on large checkouts.

The overview starts with AWL implementation/review context bindings and requires
an exact, currently registered Herdr workspace, matching endpoint, repository
common directory, checkout, branch, and Herdr/Git worktree registration. Labels
are not identity evidence. Unmanaged workspaces and retired-only contexts are not
overview tasks. A CLEAN loop or published/merged branch is not a removal signal:
registration keeps an otherwise matching execution visible. Successful workspace
cleanup removes it. Explicit issue lookup includes saved retired bindings and
available cleanup evidence, even when the checkout no longer exists. Multiple
executions are separate entries; no newest-workspace heuristic selects one.

Grouping uses the private project registry and the configured `projects_root` to
match the exact permanent repository path, together with the saved base branch
when available. The configured Linear project name is the project identity. A
unique matching mapping is `verified`; missing configuration, multiple mappings,
or conflicts leave membership `unverified`. This is local configured membership,
not a claim that Linear has been refreshed. Repository basename/display-label
guesses are never used. Status only needs the repository location from machine
configuration, not a Linear credential or agent configuration.
CLI source in a linked worktree uses the same verified permanent-checkout
[registry discovery](configuration.md#installation-and-project-mapping) as other
config consumers; it does not need a private registry copied into the worktree.

JSON version 1 has `selection`, `issue`, `discovery`, `projects`,
`unverified_project_tasks`, and `tasks`. Project groups contain indexes into the
deterministically ordered task array. Each task contains project/repository and
workspace identity, selected role contexts, `state`/`phase`, observed controller
ownership, loop and review evidence, acceptance validity, integration/publication,
cleanup, uncertainties, guidance, and history. Unknown scalar identities are
`null`; missing acceptance is `absent`, distinct from `invalid` or `unverified`.
`valid` acceptance means its exact local context, content/index/history and pinned
base still match. It is not authorization to publish or evidence of passing CI.
Publication reflects persisted delivery/PR evidence; remote PR state and merges
are not refreshed or inferred from ancestry. A retained `publication.state=complete`
can describe an older cycle. `publication.current_acceptance_published` is true
only when the current valid acceptance and content match the completed cycle;
a newly accepted follow-up remains in review until its own publication is proven.
Consumed standalone reviews remain history; the current loop owns its reviewer
and outcome. Conflicting outcomes or provenance for the accepted pass make its
acceptance unverified. History and status omit prompts, transcripts, finding prose,
publication bodies, and credentials.

`discovery.state=available` with `tasks=[]` is a successful empty overview.
`unavailable` uses `tasks=null` for the overview and an unknown workspace count
when Herdr cannot be inspected. `uncertain` can contain a partial set with explicit
excluded-execution diagnostics. Consumers should preserve their previous view on
unavailable/uncertain discovery rather than treat omissions as cleanup. Explicit
issue lookup can still return saved diagnostics during a Herdr outage. A valid
status invocation exits zero even when its model reports uncertainty; syntax or
selector errors exit nonzero. Consumers must inspect the model's state.

Operator hints are read-only. A running/ready loop with an observed owning
controller does not require intervention. An unowned controller lock plus a
validated active claim, retained delivery coordinates/receipt and matching context
identity, HEAD, base and scope can suggest repeating `task loop ISSUE` to **try
observation of the original pass**. This never resends it, and later provider/runtime verification
can still refuse recovery. Review additionally requires the entire pinned snapshot;
uncommitted implementation edits are allowed. Known drift calls for inspection
without invalidating or rewriting the checkpoint. A missing lock, receipt, `idle`
agent, timestamp, or saved `running` flag alone cannot establish that path. Deliberate `paused` state
suggests `--continue`. A completed blocked review with sufficient saved identity,
no human-decision findings, and remaining pass limits suggests `--retry-review`
after the operator resolves its external blocker. The command still verifies
runtime/provider identity before authorizing any new pass. Uncertain/conflicting
evidence calls for manual inspection.
At a proven budget-exhaustion boundary, loop reports and canonical status suggest
an explicit `--extend` command with usable increased totals, preserving the original
implementation/reviewer and unresolved findings. This is conditional guidance:
the command must still verify current requirements, Git/base and idle/resumable
sessions. At the supported ceilings, guidance explains that extension cannot
provide enough capacity. The dashboard consumes this same projection.
Trust/setup requires action in the original pane/controller. Abort/discard hints
are explicitly conditional on deliberate abandonment and the commands' own
ownership/publication checks; they do not certify that cleanup is safe.

History is an allowlisted view of the current checkpoint's ordered passes, the
latest standalone review, selected context allocation timestamps, current
acceptance, integration/rebase, the last 12 saved publication cycles, and matching
cleanup evidence. At most 64 entries are returned, with `truncated` indicating
omission. `ordering=source_order` preserves order **within each source**; it does
not fabricate a cross-source chronology. Missing timestamps remain `null`.
Interrupted/pause/abort states describe retained boundaries, not every controller
transition that ever occurred. Current checkpoint events remain separate from
`earlier_iterations`, which exposes up to three superseded runs in oldest-first
replacement order, with run IDs, context IDs, counts, outcomes and report availability.
Its `state` is `available`, `partial` or `unavailable`; invalid records have unknown
outcomes. Missing pre-feature history cannot be reconstructed. These counts are
not lifetime totals and never change current-loop limits, status or guidance.
No journal or global task-history database is introduced: task Git metadata goes
away with its worktree; existing registry retirement and cleanup tombstone policies
remain owned by their lifecycle components.

## Task dashboard

Run `task dashboard` and open its printed URL (normally
`http://127.0.0.1:8765`). `--port PORT` selects another port, and `--port 0`
asks the OS for a free port. The server always binds to `127.0.0.1`; it has no
public-listen option. Ctrl+C, SIGTERM, or SIGHUP stops the dashboard without
changing workflow state. Missing bundled assets or a busy port produce a startup
error. No additional runtime or frontend build is required.

The navigation is **configured Linear project → registered task execution →
agent contexts and bounded history**. Repository names are secondary labels.
Overview counts, state badges, current phase, saved pass/review counts, and the
canonical attention hint are prominent. Context identities, workspace paths,
detailed evidence, and bounded history expand on demand; open sections and
keyboard focus are retained when the same task refreshes. Context cards show
saved context states separately from canonical task state, without inferring
agent liveness from an `active` context flag.
Unverified project membership appears separately, without guessing a mapping.
The browser displays the [canonical status projection](#task-status), including
its exact conservative guidance and conditional command text. Those commands
are informational; there are no workflow action controls. CLEAN and locally
recorded merged executions remain visible while registered. Confirmed retirement
removes them, including a selected task's detail. There is no task archive or
remote Linear/GitHub refresh.

The selected task's **Task report** area offers **Copy report** and an expandable,
selectable plain-text view. Verified saved loops use the same complete rendering
as `task loop ISSUE --status`, including findings, fixes, every recorded check
(also `not_run`), final state, and human-intervention reasons. Paused, interrupted,
and escalated loops retain available partial outcomes without implying success.
Missing, unreadable, stale, or ambiguous evidence produces an unavailable state;
safe canonical guidance is offered as **Copy diagnostics** when available. Saved
controller exceptions and failed review results never expose their unsafe prose.
For the current iteration, preceding validated pass summaries, checks, findings
and resolutions remain reportable after a later failure. The failed boundary uses
canonical attention guidance and withholds failed-review prose and checks; saved
pass/review counts remain unchanged. A claimed review without a collected result
is explicitly uncertain; `Final review` describes only completed records and
remains `not_run` when none exists. Malformed or unverified pass evidence still
uses diagnostics, as do unsafe historical reports. No prompts or provider
transcripts are exposed.
Copy success is shown only after the clipboard write succeeds; if clipboard
access fails, use **Select text** and copy manually. Reports are not archived
after workspace cleanup.

**Current iteration** shows the active checkpoint's counts and guidance.
**Earlier iterations** shows retained outcomes separately, including the original
implementation/reviewer IDs. **Inspect report** selects a verified prior report
for the same copy/select controls; **Show current report** returns to the active
run. Old report guidance and clean acceptance are historical only. For example,
a permitted replacement of a clean R1 with six passes/three reviews by R2 with
one pass/one review shows both sets of counts; only R2 supplies current status.
Unavailable or partial evidence disables historical report access without making
it appear clean or complete.

One shared collector reads persisted state at startup and waits five seconds
after each completed projection before the next check. All browser clients read
its cache. SSE notifications trigger full HTTP snapshot reads; opening or
reconnecting also fetches the current snapshot, without replaying old events.
The browser additionally reads the cache every 15 seconds for freshness and as
a fallback if the event stream is unavailable. These snapshot reads do not poll
Herdr. Report text is fetched separately only for the selected execution, with
fresh canonical identity/evidence checks. Selection changes discard old report
responses, and refresh checks reports even when overview counts are unchanged.

Uncertain/unavailable discovery retains the last complete projection with a
visible stale/unverified notice and the latest discovery diagnostics. Before
the first complete discovery, any retained partial projection is also labeled
unverified. Omissions during uncertainty never establish cleanup. A task whose
evidence cannot be decoded retains the canonical uncertainty when it is included
in the projection. A genuinely empty, successful discovery clears the old view.
Last-check, projection-change, and last-complete-discovery times describe
dashboard observations, not agent progress or liveness. Slow reads and server
disconnects are visible; an unchanged timestamp is not proof of a stuck agent.
History keeps the canonical source ordering and unknown timestamps.

To view a dashboard running on another machine, create your own SSH tunnel:

```sh
# On the workflow machine:
task dashboard --port 8765
# On the machine running your browser:
ssh -N -L 8765:127.0.0.1:8765 user@workflow-host
```

Open `http://127.0.0.1:8765` on the browser machine. The first port in `-L` may
be changed if it is busy locally. You can use the workflow machine's private
Tailscale address/name as the SSH destination with your own SSH/Tailscale setup.
Lite still listens only on that machine's loopback address; tunneling and private
network access are operator-owned, with no remote hosting, accounts, or network
configuration added by Lite.

### Optional private Tailscale Serve access

Tailscale and background operation are optional; ordinary `task dashboard` still
works in the foreground with only localhost HTTP access. To use a private HTTPS
URL without SSH forwarding, use your own Linux Tailscale installation and one
explicit AWL option:

```text
https://<device>.<tailnet>.ts.net/
    -> Tailscale Serve (private HTTPS)
    -> http://127.0.0.1:8765/ (AWL)
```

Access is limited by **tailnet policy/ACLs**, with no AWL authentication layer.
Permitted tailnet members can read workflow metadata, including task identities,
workspace paths, bounded history, and selected task reports. Use private Serve,
with no Funnel enabled for this endpoint. See [Tailscale Serve prerequisites and access control](https://tailscale.com/docs/features/tailscale-serve).

On the workflow machine, check `tailscale serve --help` for the installed version
and complete Tailscale's one-time HTTPS setup if prompted. Use a version supporting
`--bg` for persistence. Check existing Serve routes before configuring the root
HTTPS endpoint so you do not replace another application:

```sh
tailscale serve status
tailscale serve --bg 8765
tailscale serve status
```

The status should show your private HTTPS URL proxying `/` to
`http://127.0.0.1:8765`. Use that URL's hostname in place of the placeholders below.
Serve's supported `--bg` mode persists after the terminal closes and resumes after
device or Tailscale restarts; it needs no additional supervisor. See the
[Serve CLI reference](https://tailscale.com/docs/reference/tailscale-cli/serve).

For an initial foreground check, run from your permanent AWL checkout:

```sh
task dashboard --port 8765 --trusted-origin 'https://<device>.<tailnet>.ts.net'
```

AWL still listens only on `127.0.0.1`; local HTTP access remains available. The
option accepts one HTTPS hostname and optional port, with no credentials,
wildcard, path, query, or fragment. A trailing `/`, hostname case, and default
HTTPS port `:443` are normalized. Other ports must match exactly. Without this
option the tailnet hostname receives 403. With it, the preserved Host must match
that authority, and any Origin must equal its normalized HTTPS origin. Browser
navigation and same-origin GETs can omit Origin. Cross-site requests and unrelated
or ambiguous headers are refused; forwarded headers do not authorize requests.

### Optional dashboard user service

To keep the dashboard available after logout and reboot, stop the foreground
check with Ctrl+C and create `~/.config/systemd/user/awl-dashboard.service`
(`mkdir -p ~/.config/systemd/user` if needed). This example assumes the permanent
checkout is `~/projects/agentic-workflows-lite`; adjust both paths and the trusted
origin. **Never point the service at a temporary Herdr worktree.** If using an
installed CLI instead, set `ExecStart` to its absolute executable path followed
by `dashboard` and these options, and keep a stable working directory.

```ini
[Unit]
Description=AWL read-only task dashboard

[Service]
Type=simple
WorkingDirectory=%h/projects/agentic-workflows-lite
Environment="PATH=%h/.local/bin:/usr/local/bin:/usr/bin:/bin"
ExecStart=/usr/bin/python3.12 %h/projects/agentic-workflows-lite/task dashboard --port 8765 --trusted-origin https://<device>.<tailnet>.ts.net
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
```

`%h` expands to the service user's home. Check `command -v python3.12 git herdr`
and adjust the Python path and `Environment` PATH for your installation; a user
service does not source interactive shell startup files. Use the same account
and permanent AWL configuration as your normal CLI. Herdr discovery can be
temporarily unavailable after startup; the dashboard labels this uncertainty
and retries as usual.

Enable lingering once for that account, then enable and inspect the service:

```sh
loginctl enable-linger "$USER"
loginctl show-user "$USER" -p Linger
systemctl --user daemon-reload
systemctl --user enable --now awl-dashboard.service
systemctl --user status awl-dashboard.service
journalctl --user -u awl-dashboard.service -n 50 --no-pager
```

Confirm `Linger=yes`: [lingering](https://www.freedesktop.org/software/systemd/man/latest/loginctl.html)
starts the user manager at boot and keeps it after logout. Local policy may require
administrator authorization for this one-time step; have the administrator
enable lingering for your account if it is refused. The dashboard itself runs
as your regular user. It stays a foreground Python process managed by systemd.

Serve and the dashboard can start in either order. Serve's saved proxy route can
be active while AWL is unavailable; requests recover when AWL is ready, and an
open dashboard reconnects and fetches a fresh snapshot. There is no cross-service
health gate. After editing the unit, run `systemctl --user daemon-reload` and
`systemctl --user restart awl-dashboard.service`. To stop it now and disable future
automatic starts:

```sh
systemctl --user disable --now awl-dashboard.service
```

To turn off this Serve HTTPS listener, inspect status then run
`tailscale serve --https=443 off`. To reconfigure it, turn it off and rerun
`tailscale serve --bg 8765` with the desired fixed local port; update AWL's `--port`
and restart its service to match. If the external hostname or HTTPS port changes,
update `--trusted-origin` too. Check `tailscale serve status` afterward.
`tailscale serve reset` clears **all** Serve configuration on the device, so use
it only when removing every Serve route is intended. Disabling AWL does not
disable lingering for other user services.

### Manual Tailscale and reboot acceptance check

Automated dashboard tests exercise HTTP headers, static assets, JSON, SSE, and
reconnects over loopback; they do not verify a real tailnet, systemd, or reboot.
After the one-time setup above, check your installation:

1. Confirm `tailscale serve status` shows the intended private URL and loopback
   target. Confirm `ss -ltn 'sport = :8765'` shows AWL listening on `127.0.0.1`,
   and local `http://127.0.0.1:8765/` still opens.
2. On another permitted tailnet device, open `https://<device>.<tailnet>.ts.net/`
   with no SSH tunnel. In browser network tools, verify HTML, `app.js`,
   `styles.css`, and `/api/snapshot` return 200 and `/api/events` remains a live
   event stream. Check normal workflow changes refresh the view. Restart only
   `awl-dashboard.service` and confirm the open page reconnects and refreshes.
3. Close the setup shells and log out of the workflow machine; verify the URL
   still works. Reboot at an operator-chosen time and, **before logging into that
   machine**, verify the same URL from the other tailnet device. No dashboard or
   Serve startup command should be needed. If it fails, inspect user-service
   status/journal, `Linger`, and Serve status after login.

## Task start

A repository is resolved as `projects_root / repo_name`. Each must be its permanent Git
checkout, already on the configured base branch, with a clean working tree (including
untracked files) and no unfinished Git operation. The base must track a same-named
branch on a remote. `task start` fetches that upstream into `FETCH_HEAD` and updates
using `merge --ff-only`; it refuses local-only commits or divergence. It does not
switch, stash, reset, or force-update branches.

The command loads the current issue through
[Linear's GraphQL API](https://linear.app/developers/graphql), resolves the project,
updates the base, and asks Herdr to create or focus a worktree. Herdr chooses its location. New default
branches start with the lowercase issue identifier and a title slug (up to 100
characters). The identifier is the stable identity: renaming a Linear title never
renames an existing branch or creates a replacement workspace.

After Git and Herdr confirm the exact checkout and focus, the command sets the
issue to its team's exact `In Progress` status (unless it is already there). It
then starts the selected adapter in the returned pane and sends the same workflow-
owned handoff: current identifier, title, exact description, resolved checkout,
branch, optional slice, and standard implementation instructions. Both agents
are told to read repository instructions, inspect before editing, implement the
requested scope, create thorough behavioral and regression tests with meaningful
assertions, and use focused subsets (including new or changed tests) plus
proportionate inexpensive syntax/lint/diff checks during development and before
handoff. Test-first development is useful when appropriate; TDD is optional.
Focused execution does not mean reducing coverage or adding artificial tests for
non-testable work. Ending implementation does not routinely require a full suite.
Explicit local/per-pass requirements remain authoritative; pending merge-time
validation is reported for CI or human verification. Agents stop for independent review without
committing, pushing, merging, or opening a PR. The Python workflow owns the Linear
lookup; the execution agent is told not to contact Linear or read its credentials.
No task description file is written into the worktree.

The complete Linear issue is the task source of truth, with user-defined structure.
Requested scope, acceptance criteria, constraints, and task-specific implementation
guidance apply wherever they appear. The separate workflow-owned implementation
instructions remain authoritative for lifecycle and safety behavior regardless of
issue formatting.

Agent-backed starts and from-scratch loops share the
[issue structure policy](configuration.md#linear-issue-structure). The default
`warn` mode prints a non-blocking warning if the configured collapsed marker
(default `Agent instructions`) is absent; `ignore` skips the check. Only explicit
`required` mode rejects absence, before any Git update, worktree creation/reuse,
Linear status change, or context allocation/agent launch. This checks only the
configured marker's presence; it does not parse, extract, or privilege its contents,
require other headings, or validate publication labels. The complete description
is passed unchanged in all three modes.

`task start ISSUE --no-agent` performs the same workspace preparation and Linear
transition without starting an agent. It does not require an `Agent instructions`
block, `[agent]`, or an installed agent and cannot be combined with `--agent`,
`--model`, or `--mode`.

### Implementation readiness outcomes

With [task assessment enabled](configuration.md#implementation-task-readiness),
the shared implementation handoff asks the current agent to assess readiness
before implementation mutations using the full issue and bounded read-only
repository inspection. The assessment is semantic agent guidance, not a new
issue parser, separate agent, planning phase, or controller boundary.

The agent emits a small structured outcome in its conversation before edits:

```json
{"task_assessment":{"state":"ready","summary":"Outcome and verification are clear","questions":[]}}
```

`ready` proceeds automatically in the same turn. `blocked` has a concise summary
and a nonempty `questions` list of specific blockers, and ends the turn before
implementation mutations. Interactive `task start` remains a launch command:
its exit status confirms handoff delivery, not assessment or implementation
completion. Read the structured outcome in the implementation pane and supply
clarification there; the agent reassesses and continues outstanding work in that
conversation. It must not rewrite the issue or record a separate plan.

Automated implementation passes include `task_assessment` in their existing
temporary result JSON, alongside `pass_id`, `state`, `summary`, `checks`, and
`resolutions`. This includes from-scratch launches and later completion/fix
handoffs; each assesses the current outstanding work without replaying completed
work. `ready` requires an empty question list. An assessment block requires
overall `state=blocked`; it cannot be accepted as completed. If implementation
later blocks after a ready assessment, the overall state is still `blocked`.
Only an execution failure before assessment may omit the field with `state=failed`.
With assessment disabled, the original result fields are used.

The loop validates this contract and retains the small outcome with the existing
pass record, displaying its questions in text and JSON status reports. A blocked
assessment escalates the run with no review or automatic retry. The controller
compares the existing before/after checkout snapshots and flags changes across a
blocked assessment for inspection; this detects Git-visible changes, not transient
edits or all possible side effects. The agent's pre-mutation instruction remains
the assessment gate. Normal workspace preparation and Linear status transition
have already occurred before the agent receives the task.

After the user resolves the questions (in the existing conversation or by updating
Linear), inspect the idle context and use `task loop ISSUE --new`. The workflow
fetches the current issue and resumes the same verified implementation conversation;
it does not relaunch the initial agent. A ready reassessment proceeds to ordinary
implementation and independent review. `--continue` cannot resume an escalated
block: it remains exclusive to a saved graceful pause. Session/checkout checks,
interruption refusal, pass limits, and stop behavior are unchanged. See
[checkpoint recovery](#checkpoint-and-interruption-recovery).

## Workspace lifecycle and slices

An existing workspace is reused only when Git and Herdr agree on one usable branch
and checkout. Dirty task worktrees are preserved; the permanent base checkout
must remain clean. Branch-only, locked, prunable, inconsistent, or ambiguous state
stops with an error. `task start` never deletes a branch, worktree, workspace, or
session. Scope is recorded in `agentic-workflows-scope.json` in the worktree's
private Git directory (`git rev-parse --absolute-git-dir`), outside tracked files.
The record contains only its version, issue identifier, branch and slice name
(or explicit `null` for a default workspace), never the Linear description.

The default remains one issue, one branch, one PR. For multiple implementation
slices, select a stable name explicitly:

```sh
task start DEV-13 --slice codex-handoff
task start DEV-13 --slice workspace-lifecycle
```

These create/reuse `dev-13-codex-handoff` and `dev-13-workspace-lifecycle`.
Slice names normalize accents, case, spaces and underscores to an ASCII branch
suffix; empty names, path/ref syntax, control characters and suffixes over 100
characters are rejected. The prompt names the slice and instructs the selected
agent to ask if its scope is unclear. Other slices remain untouched.

Without `--slice`, exactly one candidate can be reused, including one originally
created as a slice. The resolved workspace's recorded scope is used in the agent
prompt, so an `importer` slice retains its restriction when reopened without
`--slice`, even after a Linear title or Herdr label changes.

Legacy workspaces without scope metadata are refused until an explicit `--slice`
matching their branch suffix establishes their scope. A branch name matching the
current title is not proof of default scope. Invalid/mismatched records and
explicit selectors conflicting with recorded scope are refused; existing scope
is never overwritten. If a metadata write fails, the workspace remains intact
and neither the Linear transition nor agent startup proceeds.

Multiple candidate branches/worktrees (including live remote
branches) are listed and refused. Use `--slice` with the desired branch's suffix
after the issue ID to disambiguate a slice. For a legacy title-derived branch,
this explicitly adopts that suffix as its scope. Historical candidates are not
silently filtered out to guess an active one.

### Remote branches and PR history

Remote branch discovery uses `git ls-remote --heads` on every configured remote.
Cached `refs/remotes/*` are never evidence of a live branch, so stale tracking refs
do not require manual pruning. A live remote-only branch stops creation for that
selection. Remote lookup failure stops safely; no fetch refspecs are added or
force-applied.

For a `github.com` base upstream, the workflow checks the
[GitHub pull request API](https://docs.github.com/en/rest/pulls/pulls#list-pull-requests)
for all PR states and pages for the selected branch in that repository. Any merged
or closed PR makes the branch historical, even after a squash merge and remote
branch deletion. This check also runs before creating a new branch, to avoid
recycling a historical name. A successful empty result or only open PRs permits
the selection. Git ancestry and `git branch --merged` are never used as a proxy.

Public repositories need no GitHub credentials within unauthenticated API limits.
Private repositories/rate limits need pull-request read access through the active
local `gh` login or an explicit `GH_TOKEN`/`GITHUB_TOKEN` override; see
[credential setup](configuration.md#local-github-cli-authentication). `gh` remains
optional. API/network errors or malformed
responses refuse the selection. PR history is checked in the configured base's
upstream repository; if configured remotes point to different repositories, reuse
is refused because cross-repository PR history is not supported. Hosts other
than `github.com` permit fresh workspaces but refuse existing-workspace reuse
because merge history cannot be established. Inspect PR state and retire old local
work manually, or choose a new slice; the command never cleans it up for you.

## Failure and retry behavior

Agent selection, option validation, and executable availability are checked before
Git, Herdr, or Linear mutation. If Git/Herdr preparation fails, Linear is not
updated and no agent is started. If the Linear update fails, the valid workspace
remains available and no agent is started. Check Linear and rerun.

Launch confirmation does not prove eventual implementation success; later model,
provider, or implementation failures remain visible in the selected agent. The
workspace and Linear's `In Progress` status remain intact on handoff failure.

The returned pane must be an available shell. An existing instance of the selected
agent anywhere in the workspace prevents a duplicate launch. Continue that session,
exit it before requesting a fresh handoff, or use `--no-agent` to focus it. After a
timeout, inspect the pane before retrying: the process or prompt may already have
started. There is no automatic resubmission, fallback agent/session, or killing of
a potentially working agent. Do not run simultaneous starts for the same repository
or edit its base checkout during a start.

See [handoff transport](architecture.md#handoff-transport) for agent-specific
readiness checks, delivery guarantees, and timeouts.

### Codex first-use trust and setup

For `task start`, initial `task loop` implementation, fresh `task review`, and
`task integrate`, a recognized Codex **Trust this folder?** menu or sign-in method menu pauses the
original workflow command before task delivery. The message identifies the context,
checkout, workspace, pane, terminal and any already observed provider session.
`task contexts ISSUE` shows `awaiting_user`.
This works with any checkout path, including newly created isolated checkouts.

1. Keep the original workflow command running. Inspect the identified Codex pane
   and make the trust or authentication decision there yourself.
2. Return to the terminal running the workflow command and press Enter. This
   acknowledges your action to the workflow; it sends no keystrokes to Codex.
3. The workflow reconciles that same process and terminal, finds the provider
   session using its original readiness marker, and verifies recorded history
   before attempting the first task delivery. It allows up to 30 seconds for
   transient post-trust readiness and provider/history verification. If the
   recognized menu remains, it asks again. Timeout, replacement, additional session
   input or conflicting evidence stops delivery. A session reported by startup is
   retained even when startup was non-ready and may continue through the human
   wait, provided every later runtime observation and provider discovery matches
   it. Missing or conflicting identity prevents delivery. Initial loop and
   integration startup also retain that identity while history is unreadable
   during setup; they must verify the recorded readiness turn after setup before
   sending the task.

Transient unrecognized Herdr observation responses are retried within that same
budget. An unrecognized launch response may also be reconciled by observing the
original nonce-bearing process; `agent start` is never repeated. Each continuation
still requires the allocated terminal, exact session and readiness-only provider
history. A visible `READY` never establishes task delivery. Persistent errors
retain the context with pane/session inspection and loop-abort guidance.

Do not rerun `start`, `review`, `loop`, or `integrate` to get past trust. The original
command retains the handoff and result paths. The execution timeout (or loop
warning threshold) starts after launch/delivery completes. It neither launches a replacement nor
replays a queued prompt. `loop --continue` is for saved loop pause boundaries;
it does not acknowledge a startup trust dialog.

An interactive workflow stdin is required for this continuation. Closed stdin,
interruption, unsupported/clipped menus, unavailable process evidence, or identity
changes fail closed. If the original command ends, inspect the retained pane and
context; a new command cannot reconstruct that in-memory launch or deliver its
handoff. An `awaiting_user` context continues to prevent replacement allocation
even after its owner exits; normal confirmed cleanup can retire the mapping.
Trust/setup after a queue attempt is outside this recovery path: delivery may
already have happened, so inspect the exact session and never blindly resend.

## Task review

Review the existing task checkout in a new independent reviewer pane:

```sh
task review DEV-20
task review DEV-20 --agent pi --model <provider/model> --mode high
task review DEV-20 --resume DEV-20-R2
task review DEV-20 --resume DEV-20-R2 --json
```

Configure `[reviewer]` separately from implementation; see
[agent and reviewer selection](configuration.md#agent-and-reviewer-selection).

The fresh form always allocates the next review context (`DEV-20-R1`,
`DEV-20-R2`, etc.). It splits a pane in the existing task tab, preserving focus,
and labels it with that exact context ID. It never prepares another worktree or
inherits implementation or previous reviewer conversation. Codex and Pi use the
shared execution adapters and repository permission profiles. Those permission
capabilities do not authorize task writes during review.

### Validation guidance

This is the canonical validation policy for development of AWL. The explicit
human decision on 2026-10-15 replaces the blanket full-regression-before-every-merge
rule with the following responsibilities:

- **Implementer:** create meaningful behavioral, regression, edge-case,
  failure-path, and invariant tests; run changed/relevant tests and additional
  checks justified by risk. Focused execution must not reduce valuable coverage.
- **Independent reviewer:** assess the full diff, requirements, and test quality;
  run targeted checks and adversarial probes justified by the change and available
  evidence. Do not blindly repeat long overlapping suites already evidenced by
  implementation. Batch all substantive findings and recheck fixes and regressions.
- **Human PR merge:** require independently reviewed/approved code and passing
  **Fast PR checks** for the current PR revision. Full regression is **not required
  for every ordinary merge**. The green-check requirement still applies when
  GitHub branch protection has not yet been configured. CI does not substitute
  for code review; review does not prove that CI passed.
- **Nightly regression:** run the complete offline suite on `main` once nightly.
  It is non-blocking for ordinary merges, but a failed, cancelled, or missing run
  remains visible and must be investigated. Record the run URL, tested SHA,
  conclusion, and logs; a passing nightly proves nothing about a different commit.
- **Final release candidate:** require one complete passing full regression on
  the **exact candidate revision**, plus relevant live installation/workflow
  acceptance. Rerun when the candidate changes materially; evidence must identify
  the candidate it actually tested. A failed or incomplete run is not a passing
  release gate. The offline suite alone does not prove live acceptance.

The [CI operation guide](architecture.md#github-actions) defines the checked-in
commands, nightly UTC schedule, evidence collection, and human protection setup.
Specifically requested tests, explicit local/per-pass requirements, and deliberately
requested `exhaustive` review remain authoritative under this policy.

Fresh reviews and same-reviewer re-reviews share the
[review validation strategy](configuration.md#review-validation), defaulting to
`focused_first`. Reviewers inspect the full diff and requirements, assess test
quality (meaningful assertions and missing behavioral, regression, edge-case,
failure-path, and invariant coverage proportionate to risk), and collect a complete
batch of substantive actionable findings. They continue inspecting after a defect
and run targeted, adversarial tests needed to investigate potential problems.
Focused execution does not limit the number of valuable tests to create: there
are no test-count targets or caps, and coverage must not be reduced to save runtime.

Under `focused_first`, both findings and clean passes use relevant focused checks;
an otherwise-clean review does not trigger a full-suite run. Checks explicitly
required locally or on every pass still apply.
The implementer fixes the batch and reruns relevant tests, then the same reviewer
rechecks fixes and regressions (`task review --resume` or the loop's automatic
re-review). `exhaustive` requests full validation even on findings passes, with
the same inspection and batching obligations.

For other repositories or tasks that explicitly require a full pre-merge suite,
that requirement does not by itself require the agent to execute it locally.
External repository CI / GitHub Actions is the
preferred broad-regression owner where requirements permit it; explicit local or
per-pass execution requirements cannot be replaced by CI. Agents never assume an
external gate exists or has passed. Without an established gate, pending final
validation is surfaced for human verification. Lite ends at a PR for human review
and manual merge; a clean independent code review is not CI approval.

Tests must not mutate tracked or untracked Git-visible task state. If a check
cannot run safely, reviewers record `not_run` and explain why; restoring test
mutations afterward does not make the test safe. Existing `checks` entries report
commands, observed results, failures, skipped checks and reasons, and evidence for
any separate CI gate. Independently evidenced results identify their source and
applicable revision without implying local execution. Unrun merge-time checks
remain `not_run` and appear as pending limitations in the summary and
`publication.validation` when present, including on clean reviews. A clean review
does not claim unrun regression passed. If explicit local/per-pass checks or
`exhaustive` validation cannot run safely, record the reason and report blocked
rather than clean.

Review checks can include structured `scope`: `review` for validation required on
this pass, or `merge` for checks the requirements and validation policy permit
deferring until merge. An unrun merge-time check remains a limitation, not a
requirement to run the full suite merely to finish a review retry. This does not
permit deferring explicit local/per-pass requirements or `exhaustive` validation.
Executed code checks that fail support `findings` for implementation corrections;
unavailable mandatory validation is `not_run` and requires `blocked`. A failed
check still needs independent passing evidence before a clean review.

This is prompt guidance, not controller-enforced proof of command execution. The
existing result/publication acceptance and Git snapshot safety contracts remain
unchanged.

### Workspace resolution and pinned state

Each pass fetches the latest Linear issue and resolves the configured repository,
local base branch, exact existing task branch/worktree, scope metadata, and
context registry mappings. No title-derived branch guesses, Git fetch, base
advancement, worktree repair, Linear mutation, PR operation, or automatic routing
of findings occurs.
Review requires exactly one active implementation context mapping with a verified
pane in the open task workspace. There is no review slice selector.
The permanent checkout must pass the existing base safety checks; the task checkout
may have staged, unstaged, and untracked work. Ambiguous slices/worktrees and
missing or inconsistent mappings stop before reviewer launch.

Review pins the current local base commit. A versioned SHA-256 fingerprint covers
that commit, task HEAD/branch, semantic index entries/flags, Git status, file names,
bytes, executable bits, symlink targets, and non-ignored untracked files. Initialized
submodule state is included recursively. Conflicted/unfinished Git operations and
unsupported file types (such as non-regular untracked files) fail closed. Index
stat-cache refreshes and mtimes do not affect the fingerprint. Ignored validation
artifacts are excluded. Two matching reads establish each snapshot. Checkpoints run
immediately after launch/resume returns, at result/status polls, and immediately
before accepting the final result. Any checkpoint that observes drift permanently
invalidates that pass, even if the original bytes are later restored. Final acceptance
also requires the ending fingerprint to match the pinned starting fingerprint.
A mutation perfectly restored between checkpoints is an accepted v1 limitation;
no worktree lock or filesystem watcher is installed.

### Exact resume and provider history

`--resume` selects only the exact registered review context. It checks role, issue,
repository, checkout, server, terminal, and persisted provider history. It retains
the actual conversation and the recorded agent/model/mode. Fresh review requires
explicit model and mode (config or flags) so provider defaults cannot drift between
passes. Resume rejects selection flags; use a fresh reviewer for different settings.
Unknown, retired, uncertain, busy, mismatched, or non-resumable IDs never fall back
to fresh review. A verified missing pane can be recreated in the task's currently verified tab
with the same context ID/session. A terminal uniquely relocated to another pane
within the task workspace retains its context and session; only the verified pane/tab
location is updated. Conflicting or ambiguous runtime/session evidence fails. Tab
rearrangement within the task workspace and manual pane renaming do not select a
different reviewer. Codex uses its exact thread queue and persisted prompt receipt;
Pi requires an exact persisted session-file reference from Herdr.
Pi review launches and restarts explicitly load the bundled session-reporting
extension, so discovery does not depend on a globally installed Herdr integration.
It reports Pi's native session path; a startup path may precede persisted history.
Pi resume rechecks the saved model/thinking mode through the same capability check
as fresh launch; unsupported or clamped settings fail before the follow-up prompt.
Pi verification retains the immutable conversation ID from the persisted header in
the existing context registry session reference. Resume checks that ID and actual history
again after startup, before delivering instructions; recreating an empty session at
the same path is rejected. Session references first reported during review polling
are recorded through the same registry observer as launch. Later conflicting
identities fail; a reported path alone never establishes resumability. Explicit
empty, blank, or malformed `--resume` selectors fail before allocation.

### Completion and review permissions

Herdr may infer `idle` while Pi is still working. Idle/done without a structured
result, startup gaps, and temporarily absent/unknown observations remain pending;
elapsed startup time or an earlier working observation does not establish completion.
Acceptance requires validated output and idle/done, plus final identity/state checks.
Without conclusive output or a verified failure, the standalone review timeout bounds
the wait, including when a stopped process is only reported as absent/unknown.
Loop-owned reviews instead use the advisory threshold described under [task loop](#task-loop).

The handoff separates authoritative read-only review instructions, resolved and
pinned metadata, and latest Linear requirements. Embedded implementation-agent
instructions remain task context. Reviewers may inspect and validate, but cannot
edit task files, fix, stage, commit, push, merge, modify Linear, or change PRs.
Their only output-file write is a private temporary JSON result outside the task
checkout. The workflow waits for the same terminal/session to settle, validates a
pass-specific nonce and strict result shape, then removes the temporary directory.
The controller also retains one bounded private handoff in the worktree's Git
metadata for [loop adoption](#task-loop): issue/repository/worktree/branch/base,
requirements digest, scope and reviewed snapshot, originating implementation
identity and checkpoint boundary, reviewer identity and prompt receipt, validated
`ReviewResult`, publication digest, and consumption marker. It contains no prompt
or conversation archive. A new standalone pass replaces this record with a pending
claim before delivery; an interrupted or incomplete record cannot be adopted.
Clean acceptance and bounded public publishing metadata remain owned by
[task pr](#task-pr).
A blocked pass, timeout, or interruption leaves
the pane intact. Context health and resumability are determined separately from the
pass verdict using live pane/terminal/session identity and provider history. A verified
reviewer remains resumable after a pause or tab move; release the pause before submitting
its next follow-up. If the agent exits back to its shell, explicit resume restarts it in
the same mapped pane and verifies the retained conversation before sending the handoff.
Closing a pane preserves verified session evidence for later exact resume and pane recreation.
Temporary verification failures retain last-known verified resumability; every resume
still rechecks provider history. Missing, ambiguous, or replaced identity remains uncertain and cannot
be resumed implicitly. `--timeout` defaults to 1800 seconds after prompt delivery.

### Structured output and exit status

`--json` emits a version-1 `ReviewResult`, independent of human CLI formatting:

- `state`: `clean`, `findings`, `blocked`, or `failed`; `invalidated` distinguishes
  implementation-state invalidation from other blocking conditions.
- `findings`: severity (`critical`, `high`, `medium`, `low`), explanation, concrete
  file/location or other evidence, and optional requirement/test linkage text.
- `checks`: name, result (`passed`, `failed`, `not_run`), details, and optional
  scope (`review` or `merge`).
- `context_id`, `pass_id`, `pass_kind` (`fresh`/`resumed`), `execution`
  (`kind`/`model`/`mode`), pinned `review_state`, and `post_fingerprint`.
- `summary`: a human-readable explanation, never an automation verdict source.

Exit status is 0 for clean, 2 for findings, 3 for blocked/invalidated, and 1 for
failure. Missing/malformed output cannot be clean; drift overrides any model verdict.
Preflight errors stop before a pass exists and are reported on stderr. Findings,
validation limitations, and stopped/blocked agents require human action when using
this single-pass command. Automatic findings routing is an explicit `task loop` action.

## Task loop

```sh
task loop DEV-20
task loop DEV-20 --from-review
task loop DEV-20 --r-agent codex --r-model gpt-6-astra --r-mode high --max-reviews 3 --max-passes 6
task loop DEV-20 --i-agent codex --i-model gpt-6-astra --i-mode high
task loop DEV-20 --pause-after-current
task loop DEV-20 --status --json
task loop DEV-20 --continue
task loop DEV-20 --abort
```

With no implementation context, `task loop ISSUE` prepares
and runs the initial implementation itself. It shares `task start`'s configurable
issue structure preflight, project/base resolution, base update, default workspace
create/reuse, and Linear `In Progress` transition. Both implementation and reviewer selections
are validated before preparation mutates Git, Herdr, or Linear. Preparation
failure prevents the Linear update; a failed Linear update leaves the prepared
workspace but allocates no context and launches no agent.

This entry supports only the unsliced default workspace, including a workspace
prepared earlier with `task start --no-agent`. Existing slice scope, ambiguous or
historical workspaces, and conflicting context history are refused. Retired contexts
permit a fresh start only when each exactly matches a validated completed forced
cleanup journal in this repository. Pending disposal, missing provenance, changed
identities and live claims still refuse. Fresh starts preserve all history and
journals and allocate the next implementation ordinal.
It does not adopt an unregistered running agent or repair a missing context.
The first implementation handoff already contains the loop's nonce-bound structured
result contract. Validated completion proceeds directly to fresh review, without
an extra implementation turn just to collect completion. Blocked, failed, malformed,
timed-out, or uncertain delivery stops before review. Idleness is never completion.

Normal configured use is `task loop ISSUE`, with initial implementation settings
from `[agent]` and fresh reviewer settings from `[reviewer]`. Optional `--i-agent`,
`--i-model`, and `--i-mode` override implementation; `--r-agent`, `--r-model`, and
`--r-mode` override review. Each option resolves independently over its role's
configuration. Both roles require explicit resolved model and mode before mutation.
The loop rejects generic `--agent`, `--model`, and `--mode`; those flags are unchanged
on `task start` and `task review`. Implementation overrides are refused when a
context already exists (except matching saved selections during pre-handoff
startup recovery), with `--from-review`, and with controls. Existing
implementation contexts always retain their recorded settings.

When an exact implementation context already exists, plain `task loop ISSUE`
continues from a current durably verified completion in its saved checkpoint
directly to fresh independent review. Without completion evidence (including an
ordinary `task start`), it resumes the same idle implementation conversation to
finish outstanding work and validation and collect structured completion. It then
starts review automatically. Idleness alone never establishes completion.

A completed fresh standalone `task review` with a retained handoff can supply the review boundary.
The loop verifies its private handoff against current requirements, Git/base/scope,
context history, original implementation session and exact reviewer session and
completed provider receipt. It rechecks live mappings, context health and idle
runtimes after provider verification. Actionable structured findings go to the
original implementation conversation; fixes return to that same reviewer. Valid clean
acceptance finishes without another pass. Human-decision findings and blockers
escalate with their reason. Adopted review counts toward the configured limits,
retains its reviewer settings, and rejects conflicting explicit selection flags.

The saved checkpoint remains authoritative. A verified late original I1 result
may account for an intervening standalone R1 only through that independently saved
handoff, matching the original claim and the entire context-set difference.
Original completion seals and provider receipts are still required and are never
rewritten. The loop records adoption before marking the handoff consumed; crashes
between these writes reconcile consumption without repeating prompts. For a terminal
adopted review, retrying even with `--new` first repairs any missing consumption
marker and reports the original checkpoint's verdict and reason without creating
a replacement run. Sticky pause intent survives collection and stops before fixes.
A running controller continues
all later passes automatically; there is no background supervisor after it exits.

Missing, stale, ambiguous or contradictory proof preserves the workspace and stops
with a diagnostic. Legacy review output cannot provide adoption evidence, and
findings are never reconstructed from terminal output or conversations. A fresh
review can be used when no unresolved or competing claim makes that unsafe; an
unproven intervening reviewer cannot bypass an existing I1 claim's context check.

When you know the initial implementation is complete, `--from-review` explicitly
starts a new loop at review, without sending an initial implementation completion
turn. With an eligible standalone handoff it adopts that review; otherwise it
starts fresh review. It still verifies and saves the exact single idle, resumable
implementation context for later fixes. The human selects the starting boundary;
idle status alone never establishes completion. Existing-context entries never
adopt an arbitrary running turn or create a replacement implementation context.
Let its current turn finish before starting. `--from-review` can accompany `--new`,
but cannot accompany `--continue`, `--retry-review`, `--extend`, `--status`, `--abort`, or `--pause-after-current`.
An exhausted checkpoint with actionable pending fixes refuses `--new --from-review`
before replacement; see [budget extension](#budget-exhaustion-continuation).

When no completed review is adopted, the first review uses the existing fresh review
primitive with a newly allocated independent reviewer. It receives current requirements
and actual repository state, without implementation conversation or completion prose. Implementation
findings return to the original implementation context. A focused re-review uses
the exact same reviewer context, previous findings, and structured fix claims.
Review remains read-only and retains the single-pass drift checks. Clean review
terminates; there is no publication, merge, cleanup, or Linear completion. Only
from-scratch preparation changes Linear to `In Progress`.

Implementation and reviewer contexts require recorded explicit model and mode,
provider-verifiable immutable session identity, and consistent registry/runtime
bindings. In particular, a Pi runtime without a persisted session reference is
insufficient. An established context that is missing, uncertain, or non-resumable
stops the loop; it never triggers bootstrap.
Reviewer resume may recreate a stopped/missing pane through the existing review
primitive after verifying the original conversation; it never allocates a
replacement conversation. No replacement implementation or reviewer is selected
automatically. Reviewer flags (`--r-agent`, `--r-model`, `--r-mode`) apply only to the
first fresh reviewer; later passes and explicit continuation preserve recorded
settings.

### Boundaries and graceful pause

The saved next phase is one of `initial_implementation` (fresh launch),
`implementation` (completion in an established conversation), `review` (fresh
review), `fixes`, or `rereview`. Fresh-launch and review-start checkpoints record
their explicit origin; existing version-1 completion checkpoints remain valid.
Each completed pass and its next boundary are checkpointed before any automatic
handoff. From another terminal, request
`--pause-after-current` while either agent is working. The controller lets that
pass finish, validates/collects its result, and stops before the next handoff.
It does not send cancellation or restart the agent. A clean terminal review
still finishes cleanly because no handoff remains; failure/human decisions still
escalate rather than becoming a resumable pause.

Pause is a sticky SQLite flag, checked between passes and atomically with each
handoff claim after preflight. The committed claim is the start of the current
pass: a request ordered before it prevents delivery, while one ordered after it
waits for that pass. Result writes cannot overwrite a concurrent pause request.
The controller holds the existing review/publication worktree lock throughout;
pause and status use separate short transactions and remain available while it
is running. Do not type additional agent prompts or edit the checkout during a run.

`--continue` accepts only a saved `paused` boundary. It explicitly clears that
pause, verifies the exact checkout/base, requirement fingerprint, contexts,
sessions, and idle runtimes, then executes the pending phase. Before an initial
launch, it instead verifies the reserved context and its exact empty shell; no
provider session exists yet. Every continuation awaiting the initial handoff
repeats the full [startup recovery proof](#checkpoint-and-interruption-recovery),
including related-pane session/activity checks and the final shell/base checks
before claiming delivery. Proof obtained before a pause is never reused.
It retains the saved implementation options without
rerunning workspace preparation or the Linear transition. It never repeats a
completed pass. Changes to requirements, checkout, identity, or resumability stop
for inspection. A later pause remains sticky. `--status`, agent idleness, and
repeating the ordinary `loop` command never continue a paused loop. Ordinary restart recovery for interrupted passes is described below. Pause/status find
the checkpoint through the context registry without loading credentials,
workflow configuration, or Linear; continuation fetches current requirements.
Loop controls resolve the task checkout from implementation/review contexts.
Retained integration (`G`) contexts keep their separate isolated checkout and
provenance; their paths do not participate in this lookup. Missing or conflicting
task checkout paths still stop the command.

### Blocked review continuation

A completed structured `blocked` review remains escalated until the operator
resolves its external prerequisite (for example, browser access) and explicitly
runs:

```sh
task loop DEV-20 --retry-review
```

The command retains the checkpoint, checkout, settings, limits, and original
implementation/reviewer conversations. With implementation findings, it sends
the exact saved findings and review checks to the implementer for corrections
and focused validation, then resumes independent review. Without findings, it
retries reviewer validation directly. Human-decision findings, including batches
mixed with implementation findings, refuse continuation and require inspection.
The controller never classifies reviewer prose as an implementation defect.

The review's original findings and checks stay in the bounded report. Failed or
`not_run` checks from blocked reviews are supplied as outstanding context in later
review handoffs. Required review checks need independent passing evidence under
their exact saved names; implementation checks and operator authorization do not
satisfy this requirement. Deferred `scope=merge` checks may remain `not_run`, with
limitations carried into the summary and publication metadata. For legacy checks
without scope, the independent reviewer explicitly classifies permitted deferrals
under the validation policy. An explicitly required `scope=review` check or a
failed check cannot be demoted to a deferral before passing.
New `scope=review` checks reported in the current verdict also require passing
evidence before clean acceptance, even when all previously saved checks have passed.

A re-review returning findings while required validation is still unavailable or
omitted is recorded as blocked, retaining its findings and checks. It requires
another explicit `--retry-review` after the prerequisite is resolved, rather than
automatically sending another fix pass. A purported clean result lacking required
passing evidence retains the claim for inspection and grants no acceptance.
Executed failing code checks allow the normal `findings` → fixes → re-review path
when no required validation remains unavailable or omitted. Those failures stay
in review feedback until independent passing evidence is recorded.
Normal review validation policy and publication authority remain unchanged.

Retry requires no active claim or retained delivery, sufficient remaining bounds
for the next pass and re-review, and verified checkout/base, requirements,
implementation and reviewer identities, and idle runtimes. Unresolved evidence
refuses without changing the checkpoint. Older completed blocked checkpoints
that saved only the reviewer context ID can restore its binding from that exact
verified registry context, after checking the provider session and saved settings.
Missing, conflicting, busy, or non-resumable context identity still refuses.
The existing reviewer may recreate its pane only after verifying the same
conversation; unresolved reviewer identity never selects a replacement.

Authorization is saved on the completed blocked record before any new handoff
claim. A public `--pause-after-current` request at an eligible blocked boundary
records sticky pause intent without authorizing continuation. It remains sticky
through retry preflight, including failed preflight; successful retry authorization
then stops at a paused boundary until `--continue`. Repeating `--retry-review`
cannot resend an authorized pass: after interruption, use the ordinary `task loop DEV-20` command
to reconcile the saved boundary or claim; use `--continue` for a resulting pause.
If result collection stopped before disposing retained delivery evidence, first
use the ordinary loop command to finish collection/disposal, then explicitly
retry the blocked review. A later completed blocked pass needs its own explicit
retry. The existing pass/review bounds are never reset.

`--new` preserves blocked-review evidence and any ensuing continuation until the
loop finishes cleanly or is explicitly aborted. A failed fix pass cannot discard
the earlier findings through checkpoint replacement. Explicitly aborted runs
retain their existing abandonment recovery path. At a completed blocked-review
boundary, use `--retry-review` after resolving its blocker; no Linear description
edit or copied chat prompt is needed.
This control does not handle clean-review follow-up work or stale reviewer claims.

### Budget exhaustion continuation

After inspecting a loop stopped solely because its autonomous pass/review budget
was exhausted, explicitly increase its saved **total** limits:

```sh
awl loop DEV-20 --extend --max-passes 10 --max-reviews 5
# The source entry point supports the same control:
./task loop DEV-20 --extend --max-passes 10 --max-reviews 5
```

For a six-pass/three-review stop with F4 pending at `next_phase=fixes`, this keeps
all six passes and three reviews counted, sends F4 to the original I1 exactly once,
then resumes the same R1 for focused re-review. Run ID, recorded results, findings,
resolutions, settings and conversation bindings remain in the current checkpoint.
Pending `review` and `rereview` boundaries also resume at that exact phase. If no
reviewer has run yet, the pending first review allocates its first reviewer normally.
No previous review is inferred clean and no missing finding is reconstructed.

At least one total must explicitly increase; neither may decrease. Omitted totals
retain their saved values. Limits remain at most 40 passes and 20 reviews, with
enough capacity for the pending handoff **and** its independent review (two passes
and one review for fixes). A larger total is authorization for that current run,
not a counter reset or automatic extension. Further exhaustion requires another
explicit increase. There is no extension beyond the supported ceilings.

Under the existing worktree publication/controller lock, extension checks the
saved limit-stop reason together with ordered completed pass/count/context evidence,
structured outcomes, actionable findings and progress history. A reason string or
an idle label alone is insufficient. It then verifies current issue/repository/
worktree identity, requirements, Git/base snapshot, exact saved sessions and idle
runtimes, with no unresolved active claim, delivery or competing standalone handoff.
For extension, an absent reviewer pane or missing agent reporting leaves inactivity
unverified, even when the saved conversation is resumable. Restore observation of
the original reviewer reporting idle/done before requesting more budget.
Unrelated escalation, non-progress, human decisions, real external-validation
blockers, changed requirements and uncertain/missing conversations refuse without
changing the checkpoint. Legacy checkpoints lacking durable delivery support or
the full saved reviewer binding cannot be extended; inspect them without guessing
history or substituting another conversation.

Authorization atomically updates only limits and control state before returning
to normal routing and receipt recovery. A racing pause stays sticky and requires
`--continue`. After controller interruption, repeat the ordinary `awl loop DEV-20`
to reconcile the authorized boundary or original claim; do not repeat `--extend`
to replay work. The command never resends a claimed pass.

`--extend` cannot accompany `--new`, `--from-review`, pause/continue/retry/abort/status
controls, agent/model/mode selections or `--timeout`. Use `--retry-review` separately
after resolving a genuinely blocked review's external prerequisite; extension
cannot authorize that blocker. Use `--new` only for intended checkpoint replacement
after inspection. In particular, `--new --from-review` creates a fresh reviewer
and omits the previous structured findings. A proven exhausted `fixes` boundary
with actionable findings rejects that combination before replacement and directs
the operator to `--extend`. This protection also applies to legacy checkpoints
whose pending fixes are proven but whose delivery or reviewer continuity evidence
is insufficient to authorize extension; inspect that evidence without replacing
the run. A completed implementation/fixes boundary remains
eligible for review-first starts. If a newer run already replaced the checkpoint,
extension examines only that current run and cannot resurrect the lost predecessor.

### Routing, bounds, and results

Review handoffs request each finding with a stable `F1`-style `id` and a `category`:
`implementation` or `human_decision`. The reviewer must preserve unresolved IDs
and report all remaining findings, including regressions. Product, architecture,
design, scope, planning, and ambiguous decisions belong to `human_decision` and
stop automatic routing, including when mixed with implementation defects.
Standalone review still accepts its original finding shape for compatibility, but
such findings cannot be adopted by a loop. The controller never classifies findings
from free-form explanations.

Fix results identify every supplied finding and describe the claimed resolution;
the reviewer independently checks those claims. Duplicate/missing/unknown IDs,
malformed output, failed checks claimed as completed work, blocked agents,
or delivery/identity failures stop for inspection. Absent output remains pending
while the original loop pass can still be observed. No transport or
uncertain pass is automatically redelivered; recovery only observes its original execution. An unchanged content snapshot after
fixes, repeated finding-ID sets, identical findings with renamed IDs, or retention
of every previous unresolved ID also stops as non-progress.

Defaults are at most three review passes, six total implementation/review passes,
and a two-hour (7200-second) **warning threshold** per delivered pass. Startup keeps
the adapters' own bounded receipt checks. `--max-reviews` accepts 1–20,
`--max-passes` accepts 1–40, and the compatible `--timeout` flag accepts 1–86400
seconds. `[loop] timeout` configures the same threshold. These settings persist
across pauses. The initial implementation consumes one total pass;
review count remains zero until review runs. Exhausting the review budget stops
before fixes that could not be re-reviewed.
Selection and timeout flags cannot accompany controls; only `--extend` accepts
explicit increases to saved limit flags.

Elapsed wall-clock time is advisory, not a token budget or an automatic stop.
Implementation, fresh review, fixes, and re-review keep observing the original
turn after the threshold. One informational warning per pass per uninterrupted
controller invocation goes to stderr, leaving `--json` stdout parseable. It names
the issue/pass, elapsed time, and operator choices. A restarted controller may
emit one reminder for that still-pending pass; it does not reset the timestamp.
`--status` reports whether the threshold is exceeded without changing lifecycle
state. `--status --json` includes `pass_timing` for an active delivered pass:
`threshold_seconds`, `warning_at` (Unix timestamp), and `overdue`. This is saved
pass timing, not proof of current agent activity; inspection does not poll or
resume an agent.

Leave a long-running agent working, request `--pause-after-current` from another
terminal, or inspect `--status`. Ctrl+C stops the waiting controller, **not
necessarily the Codex turn**. To stop/abandon explicitly, stop the controller,
quit the task agents, then follow [abort](#explicit-abort-without-discarding-work)
to preserve work or [forced cleanup](#forced-local-execution-disposal) to discard
it. A pause still waits for verified completion before the next handoff.

The compact final report includes implementation summaries, review iterations,
substantive findings, claimed fixes, validation and limitations, final review
state, context IDs, and whether human action is required. `--json` supplies the
versioned equivalent, including the pending boundary and any uncertain active
pass. Review-start reports include only passes actually run: a clean first review
counts as one pass, with no initial implementation summary or validation claim.
Run/continue exit codes are 0 for clean, 3 for pause/escalation, 130 for
caught interruption, and 1 for preflight/storage errors. Successful pause/status
requests exit 0; their reported state remains authoritative. Successful abort
also exits 0, with state `aborted`, never `clean`.

### Explicit abort without discarding work

Ctrl+C interrupts the controller; it does **not** authorize abandonment or stop
the terminal-owned agent. Repeating `task loop ISSUE` observes the original pass.
An interrupted agent turn may leave that controller waiting for a result that
will never arrive. To deliberately abandon that pass:

1. Press Ctrl+C in the **controller's terminal** if it is still waiting. There is
   no need to wait for the pass warning threshold. If only the controller stopped
   and the agent is still working, ordinary recovery remains available instead.
2. Quit the task's Codex/Pi runtimes, including an interrupted session still open
   at an idle prompt, back to their original shells. Keep those panes open and
   stop their child/background jobs. Quitting retains persisted conversations.
3. From outside the task checkout, on Herdr's host/PID namespace, run
   `task loop ISSUE --abort`. Inspect `task loop ISSUE --status --json` as needed.

Abort uses the selected worktree lock. Another controller on that worktree
refuses it; independent workspace loops may continue. It requires exact saved issue/checkout/branch, context,
provider, pane and terminal identities; positive idle-shell executable, argv,
TTY, PID/start-time and process-family evidence; and readable Linux `/proc`.
For the trusted single-user **Linux V1 Developer Preview**, this proof covers the
registered execution: original shells, live descendants and related threads,
background jobs, and process-group/session members, including observable
reparented workers. It uses host process enumeration/stat data to establish that
family, but does not read unrelated processes' private cwd, descriptors or command
lines. An unrelated process is outside this policy even if it is old, is a task
shell's ancestor, has an accessible checkout reference, or denies private reference
reads. Neither process names nor age exempt attributable workers. Incomplete
family/stat visibility still refuses because attribution cannot be established.

The process graph and original shell identities are checked repeatedly, and the
abort transaction rechecks them before writing its journal. Genuinely vanished
unrelated entries are tolerated when family evidence remains complete. Missing,
replaced, running or unverifiable registered shells, PID reuse, live attributable
workers and unresolved observation races refuse. On refusal, inspect
`task contexts ISSUE --all` and the original panes, stop remaining task jobs, and
retry `--abort` from outside the checkout on the same Linux host/PID namespace.
Keep the original shells open; do not replace panes or edit the claim to bypass
missing evidence. A missing pane or an `idle` label alone never proves stoppage.

This is a task-scoped workflow ownership guarantee, **not machine-wide checkout
non-use or sandbox isolation**. A trusted user's arbitrary external process or a
fully detached/untracked helper that escapes registered ancestry/session evidence
can still edit the checkout. The operator must stop such helpers before reuse;
a successful abort does not prove their absence. V1 does not support Windows or
macOS abort verification.

Codex additionally requires a terminal provider turn
and an empty queue; delivered claims must match the latest input. Pi verifies the
immutable persisted conversation and claimed prompt's history chain. Pending
input, provider uncertainty, changed identities and conflicting claims refuse.
If interruption occurred after saving the receipt but before submission, abort
requires complete, stable history proving the claimed input absent: every full
Codex turn plus an empty queue, or Pi's intact linear session file, including a
header-only conversation. Compacted, partial or ambiguous history refuses this
non-delivery proof. A Pi locator is reconciled only with the conversation identity
already pinned in that receipt and verified against the original session file.
No command sends cancellation, kills processes or deletes files for this flow.

Implementation edits may be incomplete, so abort preserves their current staged,
unstaged and untracked content without declaring completion. HEAD/base changes
refuse abort; an interrupted review also requires its entire pinned snapshot to
remain unchanged, with no recorded review drift. Verification is repeated before
revocation. Observed review drift durably invalidates the original pass and
revokes its matching acceptance even when abort refuses; restoring files cannot
make that review acceptable later. The private loop database retains an
abandonment journal with the original run/pass, delivery, contexts, Git snapshot
and process proof. Result directories and context identity history remain intact. Revocation precedes
context reconciliation; if interrupted, state `aborting` requires another
`--abort`. Repeated completed abort is a no-op. Late results cannot be sealed,
recovered or accepted, including after a replacement loop. No SQLite edits are
part of the operator sequence. Historical stale-reviewer reconciliation remains
separate; abort only retires the reviewer of its exact unfinished loop pass.

After abort, resume the saved implementation conversation in its original pane
using the provider's native session selector, without submitting new work yet.
The abort/status report prints that exact session ID or path; `task contexts ISSUE`
locates its pane and recorded settings, without requiring private database edits.
For an aborted review/re-review, `task loop ISSUE --new --from-review` starts a
fresh independent reviewer after rechecking the preserved completion boundary.
For interrupted implementation/fixes, inspect the preserved work and run
`task loop ISSUE --new` to explicitly continue implementation and validation;
`--from-review` refuses to infer completion. A proven undelivered, sessionless
initial reservation can likewise use `--new` in its original empty shell.
A Pi session containing only its startup header requires explicit native
implementation input first, before `--new` can verify reusable conversation
history. The loop does not infer completed implementation from that header.
An abort never publishes a PR or changes manual approval. `cleanup --force`
remains a separate, destructive choice that can discard implementation files.

### Checkpoint and interruption recovery

`agentic-workflows-loop.sqlite3` in the worktree's private Git directory retains
one current checkpoint: exact bindings/session IDs, requirement fingerprint,
snapshot, counters/limits, next phase, active pass nonce, routing findings, and
bounded result summaries needed after a pause. It contains no issue description,
transcripts, credentials, or publication approval.

A permitted `--new` atomically retains the stopped checkpoint's result evidence
in the same private SQLite transaction as replacement. Retention is limited to
three earlier runs, each with at most 256 KiB of checkpoint evidence and 16 KiB of
identity/count metadata. Oversized evidence retains only that metadata and is
reported as partial, without a copyable report. Oldest entries are pruned in the
replacement transaction; failed writes change neither the current checkpoint nor
history. The current checkpoint remains the sole controller authority. `--extend`
updates that same run and creates no history entry, and exhausted pending fixes
still refuse `--new --from-review`. Legacy or already discarded predecessors are
never reconstructed. Retention is task-local and disappears with normal worktree
cleanup; existing abandonment evidence and retirement policies are unchanged.

`--new` refuses unfinished publication before replacing or retaining a checkpoint,
including an intent saved before commit, a failed push/PR operation, or a pending
rebase. Rerun `task pr` to recover the original operation first; its checkpoint,
acceptance and intent remain available for that retry.

Publication admission checks acceptance against the current run's saved review
or a completed standalone review originating in that run. An interruption after
replacement but before the first new pass cannot reuse the predecessor's clean
acceptance, even when the checkout is unchanged. Missing or retired historical
reviewers affect earlier report availability, not current state or guidance.

For an existing implementation context, interruption before the first completion
or explicit review handoff leaves an unclaimed boundary. Plain `task loop ISSUE`
revalidates that exact saved context, requirements and Git snapshot before claiming
the pending pass. A pending pause remains sticky. This does not authorize replay
of any claimed prompt or replacement of a competing standalone review claim.

Before the initial handoff, the controller reserves the implementation context
and saves a version-3 checkpoint with its ID and explicit implementation settings.
This makes pause/status available even before launch. A pause before the handoff
claim leaves that exact shell reservation pending for `--continue`. The durable
claim precedes labeling, agent startup, and task delivery. Observed provider
identity is retained in the registry immediately and added to the checkpoint as
soon as it can be verified. Launch, provider verification, and polling share one
persistence observer, so later Pi path-only reports retain the verified immutable
conversation ID. The same session must serve later fixes. Pi initial
loop launches load the session reporter used by review; ordinary `task start`
launch arguments are unchanged.

Read-only Linear issue retrieval retries HTTP 408, 429, 500, 502, 503 and 504,
connection failures, and timeouts at most twice, waiting 0.5 then 1 second. Each
request retains its 30-second transport timeout. Diagnostics report only the
status/failure class and retry count, never credentials or server response bodies.
Authentication/access errors, malformed JSON/issue data, and GraphQL errors stop
without retries. Exhausted retries stop the command. Linear mutations are never
automatically replayed: after an uncertain status update, a later command retrieves
the issue again before deciding whether an update is still needed. A confirmed
`In Progress` state needs no write.

An availability failure after reservation but before the first handoff reports
that you can repeat the ordinary `task loop ISSUE` command. That command can
recover an **escalated version-3 initial checkpoint** only when it has zero passes
and reviews, no active claim, no provider identity, no reviewer or completed
records, and no findings/history. This includes checkpoints from before this
recovery support; diagnostic wording does not authorize a retry. Fresh validation
must establish the exact issue/repository/worktree/branch/base/Herdr binding,
original unsliced task scope, unchanged requirements and pinned Git-visible
snapshot, and the original
`launching` context with its original model/mode and terminal. The pane must have
no session evidence and a positively observed idle foreground shell. PID equality
alone is insufficient: Herdr's foreground argv must name an ordinary interactive
system shell and match that process's local executable and command line. This
requires readable `/proc` evidence on Herdr's host/PID namespace. Other panes in
the task workspace, or reporting a cwd/foreground cwd in the task checkout, must
also have no agent/session evidence and verified shell processes. Herdr may omit
`agent` and `agent_session` and report `agent_status: "unknown"` for an ordinary
shell. Missing/null metadata or an `unknown`, `idle`, or `done` status only permits
process verification; it never proves idleness. Every related pane must still pass
the shell/process checks above. A reported agent, retained provider session,
contradictory activity, or missing process evidence blocks recovery. Missing,
replaced, moved, ambiguous, busy, or conflicting contexts/panes refuse recovery.
Authentication failures, invalid requirements, and continued unavailability do
not bypass these checks or reset the saved checkpoint.

Recovery holds the same controller lock as review/publication. A short transaction
compares the entire validated checkpoint before making the initial boundary ready;
a concurrent retry cannot deliver another handoff. The configured base ref is
resolved again after shell preflight and compared with the pinned base before
claiming the handoff; checking only that the old commit exists is insufficient.
A pending pause is retained, including a request during the first recovery
preflight while this narrowly recoverable checkpoint still reports `escalated`,
and the normal state machine must still claim the pass atomically before startup
or delivery. Such a pause sets only the sticky flag: successful revalidation
returns `paused` without launching, and `--continue` still requires that paused
boundary. Other initial-launch checkpoints do not acquire this exception.
The same run ID and I context are used; workspace preparation and
the Linear status transition are not repeated. Recorded implementation/reviewer
selections, timeout and bounds survive changed local defaults. Explicit matching
options are accepted; conflicting overrides and `--from-review` are refused.

A handoff claim never authorizes another delivery. The pre-handoff recovery
exception above remains separate from collection of a delivered pass. A crash
between context reservation and checkpoint creation, or after a claim but before
provider delivery evidence was saved, still requires inspection. Historical
controller-lifetime temporary output cannot establish recovery evidence.

For new controller-owned implementation, fix, review, and re-review passes, repeat
`task loop DEV-20` after Ctrl+C, SIGTERM, SIGHUP/terminal disconnect, or controller
termination. The publication lock first excludes another controller. The command
then validates the saved requirements, issue/worktree/base, context, model/mode,
provider conversation and exact pane/terminal. It observes the same pass while
that agent is working, without sending input, restarting an agent, moving a pane,
or allocating a replacement. The original warning timestamp, configured threshold,
and pass/review limits remain in force. Conflicting supplied settings are refused;
matching settings are allowed.

Before delivery, the existing checkpoint retains the run/pass nonce, session and
location, prompt digest and provider receipt selector. It also names one private
0700 `task-loop-<run UUID>-<pass UUID>` directory under the temporary root. This
is an explicitly retained directory, not a controller-lifetime TemporaryDirectory.
The generated completion helper contains only selectors and pinned Git identity.
Agents write their structured result there, then run that helper as their final
tool to flush the result and seal its digest and final Git snapshot. Their final
answer must contain exactly the machine-readable completion receipt emitted by
the helper. No prompts, transcripts, credentials or broad run archive are retained
by the workflow.

Collection verifies the structured result with the existing parser. It also
requires that the provider's exact completed turn contains the recorded prompt
and matching completion receipt: Codex must report the latest full turn with the
saved client message ID and an empty queue; Pi must retain the exact prompt and
final receipt on an uninterrupted native history chain in the same conversation.
Additional input, replacement sessions, missing/partial history and unsupported
provider responses refuse collection. Agent idleness, ordinary chat prose, or a
JSON file alone never prove completion. The current Git state must equal the
sealed final state; implementation cannot change HEAD/base, and reviews must
preserve their entire pinned snapshot. Observed review invalidation stays sticky,
even if files are restored later. Frozen publication metadata is rechecked.

Only this proof can restore the exact context's active state and feed the existing
loop router. The completed result and next boundary are saved atomically before
another handoff. Recovery of implementation starts one fresh independent review;
fixes retain the original reviewer for re-review. A crash after result consumption
continues at the saved next boundary. Repeating a completed loop returns its saved
report. Validation limitations, including `not_run` checks, survive restart.

Caught interruption records `interrupted` and prints the repeat-command hint.
SIGKILL can leave `running`; neither state proves that the agent stopped. Neither
is a graceful pause. A sticky `--pause-after-current` request survives recovery:
collect the original pass, then stop at `paused` before the next handoff.
`--continue` still accepts only a paused boundary.

A valid completion sealed before or after the original threshold can be collected,
including from older hard-deadline checkpoints that escalated on timeout. The saved
`delivery.deadline` remains the original warning timestamp; no checkpoint or seal
needs editing, and the original run/pass IDs and actual completion time remain
authoritative. Restarting never resets elapsed time. Every receipt, checksum,
session, requirement, Git/base, review, and publication check still applies; base
advancement or other drift requires the existing explicit recovery boundaries.

Missing output remains pending; truncated, stale, conflicting, or forged completion
evidence refuses acceptance and retains the original claim. Partial files are
pending only while the exact provider turn is demonstrably still working. Unknown
output or an idle agent never implies success. Restore transient provider access or
the original runtime's observable identity, then repeat the command. Do not edit evidence, infer success
from a transcript, reset a checkpoint, or resend a claimed prompt. Use the
[explicit abort sequence](#explicit-abort-without-discarding-work) to abandon a
proven stopped pass while preserving work. If its required identity/process
evidence is unavailable, inspection is still required. Explicit
[forced local-execution disposal](#forced-local-execution-disposal) is the separate
destructive path for a never-published execution. `--new` refuses every unresolved
active claim, including escalated, interrupted or partially aborted ones.

Retention is bounded to one unresolved pass destination per task checkout, plus
explicitly abandoned destinations retained with their private journals. The
result is limited to 1 MiB, the completion seal to 16 KiB, and no unknown directory
entries are accepted. Accepted output is removed after its result is durably in
the checkpoint and before another pass; interrupted disposal is retried without
reaccepting the result. Forced disposal includes unresolved loop artifacts in its
existing journal and stopped-runtime checks. Temporary-root eviction or missing
artifacts fail closed; configure host cleanup accordingly for long-lived tasks.
The checkpoint stores the current bounded run (at most 40 passes); abandonment
journals survive its replacement. `--new` after a clean, explicitly paused or
fully aborted run requests a new loop with a fresh reviewer.

## Task pr

```sh
task pr DEV-20
```

Running this command authorizes commit, push, and PR creation/update without a
confirmation prompt. It prints the verified PR URL and stops. Human inspection,
approval, merge, and Linear completion remain separate. There are no commit-only
or push-only commands. A failed stage leaves its completed predecessors available
for verification and continuation on the next invocation.

### Review acceptance and public metadata

Publishing uses the same exact repository/worktree/branch/scope and open Herdr
workspace/context resolver as review. It requires the active implementation
mapping and the accepted reviewer's unchanged registry identity. The task must
contain the pinned reviewed base. Publication requires the local and live remote
base to match that review; an unpublished task can first take the rebase path
below when the remote base has advanced.

Review stores one current clean acceptance in
`agentic-workflows-publication.json` in the task worktree's private Git directory,
outside tracked files. It contains issue/repository/worktree/branch/base and
Herdr identities, review context/pass, verdict, timestamp, reviewer settings and
available session identity, the reviewed fingerprint, and bounded public
publication prose (or explicit approval of the frozen publication metadata after
rebase). It contains no full issue description, review findings, chat,
credentials, or general run archive. Failed/blocked/findings results never create
acceptance. Beginning another review explicitly revokes the current acceptance and
publishing intent, including if that new pass is interrupted. Outside the
unpublished rebase path, once a publication commit exists, publication must finish
through `task pr` before another review can replace that contract. Completed cycles
retain their own acceptance and intent. Completed rebase provenance survives a new
review only while its exact resulting Git state is unchanged. It authorizes commit
reuse, never publication without fresh clean
acceptance. Review and publish hold the same process lock, released automatically
on process exit.

The same private record keeps append-only publication lineage, independently of
the current review acceptance and intent. It binds the issue, checkout/branch,
configured base and remote repository to every published cycle, retaining each
accepted review, frozen intent/SHA, and completion state. It also pins the base
commit and PR number and retains the latest positively published SHA. A new
review does not erase this evidence; it prevents automatic rebasing even if the
remote branch and PR are later absent. Only publication contracts accumulate;
review findings and conversations are not archived.

The reviewer supplies public-safe summary, description, and validation prose from
the actual reviewed implementation. This is metadata generation within the
existing read-only review handoff; it cannot select Git/GitHub identities or
perform publishing actions. After automatic rebase, the handoff instead asks the
reviewer to validate the already-frozen title/body, as described below.
`task pr` has no second model invocation and never
falls back to copying a possibly stale task title or private review checks.
Initial clean verdicts without publishing metadata require a new review.

Exactly one canonical primary Linear category label resolves the type:
`Feature` → `feat`, `Bug` → `fix`, `Chore` → `chore`, `Docs` → `docs`,
`Refactor` → `refactor`. Other labels, including `Research`, do not select a type.
Missing or conflicting categories refuse publication. Both commit subject and PR
title use `<type>: <concise actual change> (DEV-20)`. The body contains `Summary`,
`Tracking`, and `Validation`, with a stable linked Linear issue reference in
`Tracking`. Only approved public prose is copied into it; private reviewer
findings and task instructions are not used as a fallback. Public-safety judgment
is supplied by the reviewer, not a deterministic secret-content classifier.

New summaries start with a lower-case imperative verb, usually use 3–8 words,
and preserve proper names and acronyms such as Codex, GitHub, SSH and PR. For
example: `feat: add reviewed task PR publishing (DEV-18)`. Supporting mechanics
belong in the description. The review handoff reserves room for the longest
supported type and the issue suffix so the complete subject fits within 72
characters. New metadata with an uppercase first word, excess length, a repeated
type/issue suffix or sentence-ending punctuation is refused; the workflow does
not silently rewrite accepted prose. Previously frozen titles and bodies remain
unchanged, including through rebase review and retries.

### Mapping the reviewed state to a commit

Before any commit, the full current fingerprint must equal the acceptance. This
includes the exact original HEAD, index, flags, file contents/modes and untracked
inventory. Publication includes final materialized tracked files and non-ignored
new files, with deletions, executable modes and symlinks. For partially staged
files, final working-copy content wins; staged-only intermediate versions are not
separate changes to publish. All intended files must be present during review.
Ignored untracked files remain excluded. Submodules and skip-worktree or
assume-unchanged entries are refused in v1.

A temporary index prepares Git's resulting tree. The tree's actual blob bytes,
paths and modes must exactly represent the reviewed materialized files. Content-
transforming clean filters (including LFS and line-ending conversion), ignored
remnants of staged deletions, and modes that Git ignores are refused in v1. The
real index stays untouched during preparation. The workflow atomically saves
and fsyncs an intent containing the accepted pass, repository/remote identity,
expected tree/index, title/body and exact commit message before invoking commit.
The message includes a `Task-Review` pass-ID trailer. A separate materialized
content digest, excluding absent deleted paths, connects pre-commit bytes/modes to
post-commit bytes/modes. The resulting commit must have exactly the reviewed HEAD
as its sole parent, the intended tree, and the intended message. The real index
is then advanced only from its accepted entries to that proven tree. As soon as
the workflow creates or selects a proven publication commit, it durably freezes
that exact SHA in the intent before any further transport. All later checks,
including retries, require local HEAD to equal this SHA; equivalent parent,
tree and message are insufficient.

Before first publication, an already committed reviewed change receives one
publication commit (possibly an empty tree delta) to establish the generated
subject and pass provenance. The exception is a proven rebased publishing commit:
fresh review authorizes reuse of that exact
commit. Its original trailer remains provenance; the new durable acceptance
authorizes publication. Before first publication, a tree identical to the base
is refused.

The workflow rechecks content, exact commit SHA, and index before push,
immediately after push (including failures), before PR writes, and before
reporting the URL. Hooks retain their normal behavior, but a hook that changes
the message/tree/content causes refusal before push. A pre-push hook that amends
HEAD also causes refusal, even if only author or committer metadata changes:
the remote may already contain the original frozen SHA while local HEAD differs.
The command reports both SHAs and stops before creating or reporting a PR; retries
continue to refuse the changed HEAD. Inspect local/remote history and the hook
before deciding how to recover; the workflow never resets or force-pushes it.
The lock serializes workflow commands, not editors or unrelated Git processes.
Keep the checkout stable throughout publication; changes perfectly restored
between observations cannot be detected.

### Follow-up publication

After a task PR is published, keep additional task edits uncommitted in the same
checkout until they pass a fresh independent `task review`. Then run `task pr`
to append one new publishing commit to the existing branch and reuse the same PR.
Already-published commits are never rewritten. Staged, unstaged and new files can
all participate. Each cycle
creates exactly one commit with the previous published commit as its sole parent,
the exact newly reviewed tree, and its own `Task-Review` pass trailer. A review
without new materialized changes cannot create an empty follow-up commit.
Locally committed follow-ups require manual inspection; the workflow does not
rewrite them to manufacture the required parent.

The previous completed cycles remain unchanged. Before transport, the new SHA is
frozen in the current intent; before push, its acceptance and frozen intent are
saved as a pending cycle. Positive remote confirmation advances the latest
published SHA, while final PR/ref verification marks the cycle complete. A failed
push or evidence write retains the previous published SHA and pending contract.
Retry recovers the same commit even if commit acknowledgement or the initial SHA
write was lost, and skips push if the remote already contains that exact SHA.
A lost PR update acknowledgement is reconciled against the same recorded PR.
New reviews refuse unfinished cycles instead of discarding their retry evidence.

Every later publication verifies the recorded commit objects and their ancestry,
as well as the current review/intent. The remote must be at the previous published
SHA or the current frozen SHA. A missing branch, rollback, unrelated or additional
remote commit, missing/replaced PR, or changed PR repository/head/base refuses the
operation. PR number is durable identity: matching branch names alone cannot
authorize a replacement PR. All pushes use the existing normal, explicit SHA
refspec; no amend, rebase, reset, or force-push extends published history.

The initial reviewed base commit remains pinned for this lineage. Local or remote
base movement stops follow-up publication without fetching or changing task
history. Updating the local base and requesting a fresh review cannot bypass this
restriction. Review currently uses a single base both for the diff and for the
required ancestor/live target identity. Supporting an advanced target safely would
require separate immutable diff-base and observed target-tip identities, with an
independent review of integration against that target and fresh target checks
before transport. That integration model is not implemented; inspect and review
advanced-base integration manually while preserving published commits.

Legacy publication evidence still forbids rewriting. A retry retaining the
original accepted review and provable publishing intent can establish a completed
cycle and pin the discovered matching PR. The older observation is preserved
separately, including a published parent observed before the first workflow
commit; it never becomes a fabricated review approval. Missing acceptance/intent
provenance requires manual inspection; a fresh review cannot reconstruct old
approvals or PR identity.

### Base advancement before first publication

If the live base differs from the accepted base, any remote task branch or PR
history prevents automatic rebasing, even if its SHA otherwise matches. Durable
publication evidence also forbids rebasing after the remote ref is deleted;
an uncertain prior push cannot be treated as proof of no publication. For an
unpublished task, `pr` fetches only the configured base into `FETCH_HEAD`, using
the verified task-worktree remote and native terminal authentication. Both the
reviewed base and permanent checkout must be ancestors of the fetched base.
The clean permanent checkout advances with a guarded fast-forward; divergent or
local-only base history refuses the operation.

Integration runs in a disposable repository sharing only Git objects, with its
own refs, index and files. It replays linear implementation commits followed by
the intended publication commit onto the new base. Probe hooks, signing and
rerere are disabled; the probe never touches the real task branch/index/files.
Checkout preparation also uses a private index: it loads the reviewed source
tree, refreshes file metadata, and dry-runs the two-tree checkout against the
actual files. Refreshing is necessary when upstream changes an existing tracked
file; loading a tree alone does not establish that file's current stat data.
Setup errors, checkout preflight failures, or rebase conflicts stop with the
exact reviewed task index, files and HEAD unchanged. The permanent
base may already have advanced. Task histories containing merge commits require
manual rebasing in v1. Ignored files that would obstruct upstream paths also
cause refusal; move them aside before retrying.

After a clean probe, the workflow saves a bounded rebase journal and revokes the
old acceptance before installing anything. The journal records source state,
target base, expected commit trees/messages/authors, created commit IDs, the
completed fingerprint, and the frozen publication title/body with their metadata
fingerprint. This is one current continuation record, not a run
archive. Real replacement commits preserve implementation authors and honor the
task worktree's normal signing settings. No hooks can modify this precomputed
chain. Before installation, remote refs/PR history and permanent/task identities
are checked again. Checkout preparation is repeated in a private index, then the
real Git index is locked and task state rechecked. Checkout updates files and the
private index; only a successful checkout allows atomic replacement of the real
index. The reviewed source tree is never staged into the real index as a setup
step. A compare-and-swap branch update completes installation without running a
rebase sequencer in the real task worktree.

The command then stops with an explicit requirement for a new independent
`task review`. It cannot push or create a PR in that invocation. A pending
installation must finish through `task pr` before review; retries verify saved
commit IDs and the allowed source/target checkout states. If files reached the
planned target but index replacement was interrupted, the original index and
HEAD must still match the source; retry installs the proven target index and
reuses the same commits. Partial file updates or other drift that do not match a
proven state require manual inspection.

The fresh review explicitly receives that exact title/body and their fingerprint.
It must validate their accuracy against the rebased result, including the linked
issue, validation claims and public safety. A clean verdict must include a
`publication_approval` matching that metadata fingerprint. Missing or mismatched
approval cannot create clean acceptance; inaccurate or unsafe metadata calls for
findings or a blocked review. Metadata drift observed during the pass invalidates
it just as Git-visible drift does.

The durable acceptance binds this approval to the new pass and reviewed Git
fingerprint. `pr` checks it alongside the rebased commit provenance and reuses the
frozen title/body on every continuation, including after commit/push/PR failures.
It never regenerates them from the fresh reviewer's summary. The reviewer's own
conclusion or optional replacement publication prose may use different wording;
neither changes the frozen metadata. Older rebase records that did not retain the
title/body cannot be reconstructed safely and require manual inspection.

### Remote, PR and retry checks

The configured base must track a same-named branch on one GitHub remote. Fetch
and push destinations must each resolve to a single URL for the same repository;
conflicting repositories or multiple destinations are refused. The effective
remote is resolved with the task worktree's Git configuration, including worktree
settings and conditional includes, and must match the permanent checkout's
approved remote/repository identity. It is rechecked before each ref lookup,
fetch and push. Transport uses that remote's name so Git expands URL rewrites
once; an already-expanded URL is never passed back through Git's rewrite rules. Mirror
remotes and remote groups are refused. Push uses an explicit commit SHA and task
branch refspec, without force, tags or submodule pushes. Live `ls-remote` refs are
authoritative; cached tracking refs and task branch push defaults are not used.

Before the first push attempt, a pending marker is saved and fsynced. Observing
the remote task ref records positive publication evidence; a verified successful
push must save that evidence before any GitHub write or success reporting. The
positive record for each cycle is retained for that branch's workflow state, including
across fresh/failed reviews. Later pending pushes do not downgrade it. If the
push acknowledgement or confirmation write fails, the pending marker survives:
retry can verify or push the same commit, but cannot automatically rebase after
the remote ref disappears. Failed evidence writes stop publication. Legacy
records with an existing intent but no push history are treated as uncertain,
rather than assumed never published.

Before first publication, the task branch may be absent, at the accepted
pre-publication HEAD, or already at the proven publication commit. Once positively
published, the branch must exist; follow-ups allow only the prior published SHA or
the current frozen SHA. Anything else conflicts.
After push it must equal the publication commit. All PR history for this exact
repository/head participates: there must be no PR, or exactly one open, unmerged
PR with the configured base and expected head SHA. A fork, another base, closed
or merged history, multiple PRs, or changing responses refuse publication. The
matching PR is reused and its title/body updated if necessary. The confirmed PR
and remote SHA are checked before reporting its URL. Immediately before every
PR POST/PATCH, after the preceding GitHub lookups, the workflow rechecks the
effective destination and authoritative remote task SHA against the frozen
publishing state. Task-ref or identity drift refuses the write. Local HEAD and
reviewed contents are checked again after that native ref lookup. These checks cannot lock GitHub
against unrelated changes between the final observation and the API write.
The remote base must match the reviewed base before a new push. Once the exact
published task SHA is confirmed, later main advancement does not make publication
uncertain or prevent completing/retrying that frozen PR operation. The PR may
subsequently need integration; v1 does not rewrite published history. Uncertainty
concerns unconfirmed push/ref/PR outcomes, not base freshness. The server's ref
update during a non-force push is the concurrency boundary for the task branch;
it does not atomically compare main or encompass PR API operations.
GitHub owner/repository
casing is equivalent, including in the reported PR URL. URL scheme, host, path,
PR number, head/base branch names and head SHA remain strictly validated.

After a follow-up push, including recovery of an unfinished publication, PR
verification permits at most five observations within a five-second retry window
when either GitHub response still names the previously published parent SHA.
The recorded PR number, repository, head branch, base and open/unmerged state must
match throughout. Before retrying, native refs and local state must prove the exact
frozen publishing SHA. Both PR responses must converge to that SHA before use;
an unexpected third SHA, changed identity or API error fails immediately. These
retries repeat reads only, never PR writes, commits or pushes. Normal API request
timeouts still apply; convergence observed after the retry window is refused.

To reduce repeated authentication, a preflight observation that the remote task
ref already equals the frozen publishing SHA skips the push stage. After a new
push, remote confirmation shares the next required check after PR lookup. If
that lookup fails, the workflow still checks the refs and saves positive push
evidence before stopping; a confirmation failure is reported alongside the API
failure. A fresh publish normally needs four `ls-remote` calls and one push. A
retry with the branch already published needs three ref lookups if it creates or
updates a PR, or two if the PR already matches. Checks immediately before writes
and after the final GitHub lookup remain fresh, and local state is checked after
native transport. These reductions do not cache credentials or change Git/SSH
configuration; native prompts remain in the invoking terminal.

| Interruption | Next `task pr` invocation |
| --- | --- |
| Before commit | Recheck acceptance and saved intent, then commit once. |
| Commit completed, including a lost command acknowledgement | Require the frozen SHA if present; otherwise prove parent/tree/message/content and freeze the recovered SHA. Finish the index update if needed and reuse that commit. |
| Push failed or its acknowledgement was lost | Inspect live refs; push only if the exact commit is absent. |
| Published ref was deleted, or an uncertain push can no longer be observed | Retain publication evidence and forbid automatic rebase; inspect history manually. |
| PR creation failed or its acknowledgement was lost | Enumerate all matching history first; reuse the unique matching PR or create if absent. |
| Metadata update/reporting failed | Revalidate and reconcile the same PR, then report its URL. |
| Unpublished base advanced | Probe integration, install only a conflict-free rebase, invalidate review and stop for a fresh independent pass. |
| Rebase installation interrupted | Verify and reuse the saved replacement commits, finish only a provable checkout/ref transition, then require review. |
| Fresh review after rebase | Require explicit approval of the frozen title/body for the reviewed state; reuse the exact rebased commit and metadata through push/PR. |
| New edits after completed publication | Obtain a new clean review at the latest published HEAD; append one commit and reuse the recorded PR. |
| New review during unfinished publication | Refuse before replacing acceptance/intent; finish the frozen cycle with `task pr`. |
| Files/index/history/identity conflicts, or base change after publication | Stop without force or repair; inspect the reported state. A new review cannot bypass published ancestry, PR identity or the pinned base. |

Do not delete or edit acceptance/intent records to bypass refusal. Rerunning `pr`
continues a frozen contract, while explicitly running `review` after completed
publication starts a new one.
Neither a fresh review nor deletion of a remote ref clears publication history.

### Authentication and signing

The REST API uses local `gh` authentication or an explicit token override; see
[GitHub publishing configuration](configuration.md#github-publishing).
API failures name the selected credential source (never its value), HTTP
status when available, and a safe category such as authentication, token
permissions, SSO, rate limiting, request validation, DNS/TLS, timeout, or invalid
JSON. Diagnostics use fixed guidance and allowlisted validation field/code names;
raw response bodies, arbitrary headers, exception text, and request payloads are
not echoed. Publishing refuses missing/malformed credentials before transport.
A failed write is never automatically replayed: a connection failure, service
error, or unreadable response may follow a successful write. Rerun `task pr`
after addressing the cause; it discovers and verifies any matching PR before
deciding whether another create/update request is necessary.

Commit/push/fetch inherit stdin, stdout and stderr. Ref lookup and rebased commit
creation read only protocol stdout (refs or an object ID) and inherit stdin/stderr.
Git's SSH/GPG/credential tools retain the caller's console and controlling
terminal; the workflow never reads passphrase
input, captures prompts, or writes secrets into artifacts.
Native failures and a five-minute operation timeout produce an actionable error
without echoing captured command output. Normal Git signing and authentication
configuration stays in effect. Non-interactive callers must provide already
usable credentials/signing access or handle a native failure; the workflow never
supplies credentials to a prompt or weakens authentication.

## Task integrate

```sh
task integrate DEV-20
task integrate DEV-20 --agent codex --model gpt-6-astra --mode high
task integrate DEV-20 --timeout 1800
```

Use this explicit recovery command when an unpublished, clean-reviewed task has
uncommitted implementation changes and the base has advanced into a conflict.
`task pr` remains the deterministic first attempt and never invokes an integration
agent. `integrate` independently repeats all prerequisites and the disposable
replay; no earlier failed `pr` or conflict journal is required. A conflict-free
probe directs the caller back to `pr`. Setup errors do not count as conflicts.

The single execution role resolves from `[agent]`; `--agent`, `--model`, and
`--mode` override those fields independently. Explicit resolved model and mode
are required before mutation or launch. `[reviewer]` and the implementation
context's saved settings do not select this agent. The default completion timeout
is 1800 seconds, starting after confirmed handoff delivery.

V1 requires reviewed task HEAD to equal the reviewed base: staged, unstaged,
deleted, and non-ignored untracked changes are materialized from the exact clean
acceptance using a private index. Committed task histories, pending/uncertain
publication evidence, any remote task branch or PR history, a previous integration
record, and ambiguous source identity require inspection/manual recovery. Index
flags, submodules, filter/mode transformations, ignored obstructions, and unsafe
checkout preflights retain the publisher's existing refusals. Neither this command
nor its agent uses stash or rebases the real dirty worktree.
The existing workspace's recorded slice is included in the handoff and retained
in integration provenance, as in task start/review. When a slice is recorded,
only that slice is integrated; other issue requirements remain context. Ambiguous
slice scope requires `human_decision`. Scope drift refuses installation, including
continuation of a proven installation through `task pr`.

The configured remote and authoritative base/task refs are checked, all PR
history participates, and the clean permanent checkout may fast-forward to the
verified latest base, as in `pr`. The real task branch, index, and files remain
unchanged during the disposable probe and agent pass. After an actual deterministic
conflict, the workflow reproduces it in a durable machine-local repository under
`~/.agentic-workflows-lite/integrations/<pass-id>/checkout`. It has its own object
storage, no configured remote, and workflow-pinned source/base refs. The workflow
quits the isolated rebase sequencer while retaining the conflicted index/files;
the agent resolves with ordinary file edits, creation, and deletion. It does not
stage or remove files through Git, write Git metadata, or create commits. Unmerged
index entries remain during the agent pass; staging belongs to the controller.

A fresh `DEV-20-G1` context binds that checkout and a separate pane, preserving
the original implementation context. The handoff includes current Linear
requirements, exact accepted source fingerprint and identity, updated base,
and conflicted index entries. Prompt delivery and provider verification use the
existing adapters and immutable session guards. A bounded private record in the
real task's Git metadata claims delivery before the prompt is sent. It also records
the completion file's location, outside every checkout in a private
`task-integration-<pass-id>-*` directory under Python's temporary root (normally
`/tmp`, or `TMPDIR`). This uses the documented Codex profile's existing temporary
write access without adding writable roots or granting Git metadata writes.
Neither the checkout nor the completion directory is automatically removed on
return or interruption. The record is not a review acceptance or authorization
to publish. A temporary root inside a source or isolated checkout is refused.

The agent may edit and validate only the isolated checkout and write its result
outside it. It must not commit, change history/configuration, push, create/update
PRs, merge, or modify Linear. Product, architecture, scope, design, or semantic
ambiguity must produce `human_decision` with evidence. Completion is structured:
`completed`, `human_decision`, `blocked`, or `failed`, bound to the pass, source
fingerprint, and base SHA, with a summary and observed validation checks. Successful
completion requires at least one passed validation check and no failed checks.

Only a verified idle/completed session with valid successful output can proceed.
The workflow first proves unchanged isolated history/configuration and index,
then stages the edited files and deletions itself. The staged result must pass
`git diff --cached --check`, including conflict-marker and whitespace checks.
It then proves no unmerged paths or unfinished operation, stable file content,
and a lossless resulting tree. Failed, blocked, or uncertain agent completions
never authorize staging.
It imports only the proven tree/blob objects and repeats checkout preflight,
source/acceptance/identity checks, and latest base/remote/PR checks. Removing
textual conflict markers alone is insufficient. These checks and the subsequent
independent review complement the integration agent's reported validation.

Before touching the task, the complete integration proof and installation plan
are durably saved. The existing publication record then revokes old clean
acceptance and intent and saves the rebase journal. The normal installer creates
workflow-owned local commit history, honoring normal signing, and uses a private
index, exclusive real-index lock, repeated local proof/source checks, and compare-and-swap
branch update. The command stops after local installation; it never publishes or
marks the Linear issue complete. A fresh independent `task review` of the installed
state against the frozen base SHA B, including the frozen public title/body, is
mandatory before `task pr` can publish. Review preserves B even if main has
advanced. Resuming a reviewer is still an independent pass against that exact state.

Initial installation and `task pr` recovery use one required installation guard.
It pins the proven integration record separately from evolving commit/result
bookkeeping and rechecks the live task identity, recorded scope, proven source,
frozen base SHA, tree/history, and durable provenance immediately before file checkout,
index replacement, branch/HEAD movement, and atomic replacement of either success
record. Unreachable commit objects and private indexes confer no installation
authority. Drift stops further mutations and preserves pending evidence, including any earlier guarded steps;
the workflow neither rolls those steps back nor records a stale installation as
successful. The guard validates local state, including the configured repository/
remote identity, without polling remote refs or requiring an idle permanent
checkout. The base object B remains immutable in the proof; a moving main ref
is not part of the installation identity.

`task integrate` owns local installation safety; `task pr` owns remote publication
eligibility. Remote observations establish eligibility before the proof is frozen.
After that, local installation and its recovery may complete against B even if
remote/permanent main advances or a remote task branch appears. Installation never
authorizes publication or overwrites a remote ref. `task pr` can recover a pending
local plan without remote Git/PR lookups or GitHub credentials and then stops for
review.
After review, it checks current remote refs/history: newer main requires the
existing deterministic unpublished integration and another review, or explicit
conflict recovery within the supported v1 shape. An unexpected remote task ref
is retained as publication evidence and refused under the normal publishing rules.
Repeated remote reads cannot synchronize GitHub state with local file mutations.

`integrate`, `review`, and `pr` hold the same task-level lock. After controller
interruption releases that lock, the retained integration claim also gates later
review/publication. Human escalation, failed/malformed output, timeout, uncertain
delivery, or failed source/base/publication eligibility before proof installs
nothing. During installation, local drift stops further mutation and retains any
journaled intermediate state; external ref movement does not invalidate the proof.
The checkout and context are retained, and rerunning `integrate` refuses
to replay a prompt. Missing controller state alongside known integration contexts
also refuses; it is never treated as permission for a fresh launch.

For an unrecoverable uncertain Codex attempt with no recorded provider session,
an explicit human recovery action can abandon that exact context:

```sh
task contexts DEV-30 --all
task integrate DEV-30 --abandon DEV-30-G1
task integrate DEV-30
```

`--abandon` launches nothing and does not install or infer a result. It only
supports an `uncertain` G context with `unknown` resumability, no recorded session identity,
no completion/output or installation proof, unchanged isolated Git history, and
remaining unmerged paths. The exact task/checkout/context identities must match.
Agent/model/mode/timeout overrides cannot accompany abandonment.

Absence of a recorded session or an idle status alone is insufficient. The
original Herdr pane must be absent or contain its exact idle shell; a moved or
replaced terminal refuses. Readable local Linux process evidence must confirm no
execution using that checkout, including background children of the retained
shell. Run this recovery on the same host/PID namespace as Herdr; invisible host
processes cannot prove absence. Ordinarily Codex must report no current or archived
sessions for the checkout across providers and supported source types.
Provider/process failures, incomplete results, known sessions, live execution,
or identity drift refuse abandonment.
Other agents/platforms and more complex recovery cases remain manual inspection.

An old pre-sandbox-fix attempt may instead have an unrecorded Codex conversation
that completed only the native handoff-readiness exchange. Herdr can report that
Codex process as `idle` while it is still running in the foreground. Abandonment
refuses and identifies the exact pane, terminal, and process IDs. Inspect and quit
Codex yourself in that pane, keeping the original shell/pane and evidence, then
repeat the same `--abandon` command. The workflow sends no terminal input and never
terminates or resumes that conversation for you.

Only legacy claims lacking the newer output/scope/index fields support this
exception, and the original shell must remain observable. After proving the
process has stopped, the controller requires exactly one unarchived CLI session
for the isolated checkout, no archived sessions, no fork or parent, exactly one completed full
turn containing the canonical readiness prompt and final `READY`, and an empty
durable input queue. It repeats the history/queue observations and process check.
Any task input, extra turn/item, tool activity, incomplete/paginated history,
pending delivery, or identity change refuses. A visible `READY` alone proves
nothing. The discovered provider identity, complete readiness turn, and empty
queue evidence are archived with G1; its startup history is retained but never
used to resume integration. G2 still requires a separate explicit command.

Under the task lock, the controller durably records the explicit abandonment and
an archive in private Git metadata before retiring G1 with its identity fields
intact. `task contexts --all` retains it as `abandoned`. Its checkout, conflict
index/files, output directory, original attempt evidence, and abandonment proof
are kept; nothing is deleted and review/publication authorization is unchanged.
If interrupted before archival/retirement finishes, normal commands still refuse;
repeat the exact `--abandon` action explicitly to recheck absence and finish.
Missing or conflicting provenance never authorizes a retry.

Only a separate normal `task integrate` after completed abandonment may allocate
G2. It repeats current acceptance, source/base, remote/publication, and deterministic
conflict checks, using a fresh checkout/context and leaving G1's evidence intact.
Successful installation still revokes the old review and requires a fresh
independent review. The abandoned checkout is never a source of an installable
result.

Once both the integration proof and revoked-acceptance rebase journal are durable,
a pending installation can continue through `task pr` using its existing proven
source/target recovery states and saved commit IDs. That invocation finishes
installation and demands review; it cannot publish. A crash between proof storage
and authorization revocation, or ambiguous partial file updates, requires manual
inspection. The original index survives a failed checkout/index replacement;
recovery never infers an arbitrary partial installation. New source, base, remote,
or identity drift refuses continuation. Older integration proofs without recorded
slice scope require inspection before installation can continue.

There is no automatic cleanup or resume of integration checkouts or completion
directories in v1, including after success. Explicit
[forced local-execution disposal](#forced-local-execution-disposal) can remove a never-published
execution and its retained artifacts. Otherwise inspect `task contexts DEV-20 --all`,
the retained pane/checkout, completion output, and private provenance before manual
reconciliation. Lost output is never permission to replay a prompt. Preserve
valuable resolutions
and establish that the agent has stopped before any manual cleanup. Do not delete
controller or approval records to bypass a refusal. Complex or published histories
remain manual recovery; force-push and merge-commit rewriting are not supported.

## Task cleanup

After a task is completed and its branch is merged into the configured local base:

```sh
task cleanup DEV-7
```

Cleanup uses the same Linear project/repository mapping and Git/Herdr workspace
resolver as `task start`. It requires Linear's `completed` state type, regardless
of the status display name. The issue identifier, registered branch, checkout,
and recorded task scope must agree; the current title and filesystem naming are
not used to locate the worktree. Exactly one local task branch and one matching
registered Herdr worktree are required. Multiple slices are refused.

### Merge evidence

The permanent checkout must be clean and on the configured base. Cleanup checks
the local base without fetching or updating it; update it separately if needed.
If the task tip is reachable from that base, Git ancestry proves the merge.
Otherwise cleanup uses the existing GitHub PR-history lookup for the base's
upstream repository. It requires exactly one PR for the task branch, confirmed
merged into the configured base, with a head SHA identical to the local task tip.
The PR's resulting merge/squash commit must also be reachable from the local base.
The complete PR listing and individual PR response must agree. Extra local commits,
missing/ambiguous evidence, another head/base/repository, unavailable API access,
or a merge commit absent from the local base all cause refusal. Linear completion,
matching titles/messages, and patch similarity alone never prove a merge.

Squash verification supports `github.com` and uses the same
[local GitHub credentials](configuration.md#local-github-cli-authentication) as the
existing history checks. It reads PR evidence without changing
GitHub state. Non-ancestor cleanup on other hosts is refused.

### Removal checks and Python caches

Quit the selected agents before ordinary cleanup too. It uses the same Linux
task-scoped process verification as forced cleanup, checks before deleting even
allowlisted caches, and rechecks immediately before worktree removal. A completed
Linear status or an idle agent display does not prove that its process stopped.
Cleanup never automatically terminates agents.

All removal preconditions are checked before deletion and checked again just
before removal. Cleanup normally refuses dirty task worktrees (including
untracked and ignored files), index flags that hide modifications, unfinished Git
operations, submodules, locked/prunable/detached or mismatched worktrees,
missing/invalid task scope metadata, and any target that could remove the
permanent checkout, Git metadata, or another registered worktree.

The sole dirty-state exception is ignored Python bytecode directly inside
`__pycache__` directories. Cleanup first inspects the complete tracked,
untracked, and ignored state. It removes those directories only when every dirty
entry is a regular `.pyc` or `.pyo` file in such a directory and every directory
entry was present in that Git snapshot. A source change, symlink, loose bytecode,
nested/unknown cache content, or any other ignored or untracked file preserves
everything and refuses cleanup. The complete target validation runs again after
cache removal.

### Git removal, Herdr retirement, and retries

On success, non-force Git commands remove the exact registered worktree and then
its local branch; the output names both. Ancestry-proven branches use `branch -d`.
For a verified squash/rebase PR, cleanup rechecks that no worktree uses the branch
and deletes its exact local ref with `update-ref --no-deref -d`, supplying the
verified old SHA so a changed branch cannot be deleted. It does not use `-D` or
rewrite the task branch to manufacture ancestry. Remote branches, PRs, and Linear
status are not changed.

After Git cleanup, the command closes the exact open Herdr workspace identified
by the stable ID returned for the validated worktree. It confirms the ID's
repository and checkout path before closing it and confirms the ID disappeared
afterward; labels are never used as retirement identity. The exact association is
saved in Git-private metadata before removal, so a retry can finish closing a
stale workspace after the worktree and branch are already gone. Conflicting,
missing, or ambiguous identity refuses retirement. A close failure reports Git's
completed portion without claiming Herdr success and retains the retry state.
Each retry first uses a no-follow filesystem check to prove the saved checkout
path has no object, confirms no Git worktree has reused it, and revalidates the
workspace's repository key and task identity. Reuse or inconsistency preserves
both the workspace and retry record.
For a legacy cleanup that predates this retry metadata, the command checks Herdr
before reporting that nothing remains. It accepts exactly one absent linked
checkout only when Herdr's stable workspace ID, exact repository root/key, exact
issue label, and issue-prefixed checkout basename all agree. A label or path match
alone is insufficient; partial or multiple matches are left untouched.

If worktree removal fails, branch deletion and Herdr retirement are not attempted.
If branch deletion fails afterward, cleanup reports the partial result; a
branch-only retry refuses and leaves that branch for manual inspection. Run
cleanup from outside the task workspace after exiting its agent/shell sessions,
and do not modify the repository, task worktree, or Herdr workspace concurrently
with cleanup.

### Forced local-execution disposal

```sh
task cleanup DEV-30 --force
```

`--force` is destructive authorization to discard one selected,
**never-published or proven merged local execution**, including modified, staged, untracked and
ignored task files and its exact owned idle implementation/reviewer agent sessions.
Stop any controller owning that workspace first. Working provider turns must finish
or be explicitly stopped by the operator; force does not cancel active turns.
This authorization is independent of the Linear issue state:
the issue may remain In Progress, and cleanup does not update its status. Without
`--force`, the completed-task contract above is unchanged.

Merged recovery uses the same [merge evidence](#merge-evidence) as ordinary
cleanup, including exact local tip, PR repository/head/base and reachable merge
result checks. It never treats Done, a pushed branch or a closed PR as merge
proof. The unpublished disposal path retains its publication exclusions; a
published execution must pass the separate positive merge proof.

A manually registered linked worktree can qualify even when
`agentic-workflows-scope.json` was never written. Git's worktree registration,
local issue-prefixed branch, checkout root, symbolic HEAD, common/private Git
directories and metadata backlink must agree with Herdr's worktree registration
and exact open workspace ID, repository key and checkout. No scope file is
created. Existing scope and private execution evidence must remain consistent.
Missing scope is not inferred as either default or sliced. Without an explicit
selector, discovery covers all local issue branches/worktrees and requires
exactly one candidate. A branch prefix, label or path alone never proves ownership.

If no workspace is open, use `herdr worktree open --cwd <permanent-repo> --path
<registered-checkout>`, quit its agents, and retry. For conflicting candidates,
`task contexts ISSUE --all`, `git -C <permanent-repo> worktree list` and
`herdr worktree list --cwd <permanent-repo>` identify the conflicting contexts,
branches, paths and workspace IDs. Select one proven merged execution using its
exact local branch name:

```sh
task cleanup DEV-7 --force --branch dev-7-api-slice
task cleanup DEV-7 --force
```

The first command removes only that execution; the second can remove the remaining
unique execution. Repeat explicit selection if more than one still remains.
`--branch` requires `--force` and positive merge proof, including for an unpublished
selected branch. It narrows discovery without assigning default/slice scope or
bypassing Git registration, exact Herdr identity, foreign resource claims, stopped
runtime or filesystem checks. Duplicate or inconsistent matches for the selected
branch still refuse. Other independently bound workspaces and contexts remain
intact, including a running agent in another execution. Cleanup checks the actual
Git directory used by every existing registered sibling checkout, even if its
Herdr workspace is renamed or closed. A sibling pointing into the selected
execution's metadata blocks removal, including on final revalidation and retries.
Keep private metadata intact. A stale base requires a separate update in the
permanent checkout, as described above.

Run this command from outside all task/integration checkouts. Empty shells and
verified idle implementation/reviewer agents in the exact task workspace may
remain: cleanup proves their identity and closes that workspace. An `idle` agent
status alone is insufficient. The shell-only path compares Herdr's shell argv with
the local Linux executable and argv and requires a sleeping shell with its own
foreground terminal and session. The agent path proves the original sleeping shell
and its provider foreground family. Only ordinary interactive/login invocations of
system shells under `/bin` or `/usr/bin` are supported; script/command arguments,
missing evidence, and an exec-replaced shell PID refuse disposal.

Ownership has three meanings, derived from the existing context registry,
integration provenance and per-execution disposal journals:

- **claimed**: an execution still owns a resource, even when its runtime stopped.
- **disposing**: a pending journal reserves its exact frozen resources through
  interruption and retry.
- **released**: confirmed cleanup/retirement released the relevant resource;
  retained records describe history and do not claim a later execution.

Runtime retirement is separate from artifact release. In particular, an
`abandoned` G context retains its integration checkout and provenance despite
having a retirement timestamp. A retired G runtime also retains its artifact
claim until an exact completed disposal covers it. Cleanup reads retained context
history, current and archived integration records (including preparing passes
without a G context), registered worktrees/private Git directories, and disposal
journals in registry-known repositories. Overlapping unreleased claims from
another execution refuse cleanup, including another issue's abandoned G context.
Normal implementation/review retirement ends the runtime claim; existing Git
registration and retained provenance independently continue to protect artifacts.

A completed journal releases only its frozen context/resource identities, not
all future occupants of an issue ID or path. Context ordinals, immutable disposal
journals and bounded integration tombstones remain historical evidence. They do
not block a later execution with new contexts and its own disposal journal.

Process safety is scoped to the **registered execution**. Cleanup checks the
exact Herdr terminals and positive shell identity, UID-independent ancestry,
related threads, and remaining jobs in recorded shell sessions/process groups.
It reads ancestry metadata to discover those jobs, but does not inspect unrelated
processes' private cwd, executable, root, descriptors, mappings or mount namespace.
Known descendants block even when their UID or working directory changed. Missing
required ancestry, thread or shell evidence fails closed. Kernel classification
uses Linux's `PF_KTHREAD` flag, never a name; it cannot exempt a claimed shell or
execution descendant. Unrelated process churn does not invalidate the proof.
These checks run again immediately before each destructive operation, and closed
registered shells must actually exit. Historical provider conversations remain
outside disposal. Older pending journals without recorded shell-session proof
still require inspection; they are not silently promoted to new evidence.

On Linux with the verified Herdr 0.9.1 server/protocol 22, force can close a
registered implementation/reviewer TUI that is idle or done at its provider prompt.
It checks the exact context, provider session, terminal, foreground argv/executable,
shell PID/start/TTY and process family. Codex additionally verifies its persisted
terminal turn and empty input queue through the provider API. An idle badge or CLEAN
result alone is insufficient. Unsupported Herdr versions require manually quitting
agents to their original shells until their close semantics have been validated.
Another provider invocation anywhere in that family, including a nested
`codex exec` or an unverified launcher/native-child pair, refuses closure: ancestry
does not prove that its separate turn is idle. Let it finish and stop that process
before retrying cleanup.

The existing journal reserves the execution and freezes all observed family PIDs
and start times before requesting exact workspace closure. This stop runs under the
selected workspace lock, outside the registry gate. Herdr's response alone never
proves termination: cleanup checks pane absence and waits up to three seconds for
the recorded processes and related workers to exit. Failed or interrupted closure
retains the claim; rerunning force reconciles those same identities. A reused PID,
replacement terminal, unknown worker or surviving descendant refuses deletion.
Completed journals retain bounded agent retirement evidence. Force sends no `/exit`
input or custom process signals and does not resume provider sessions.

V1 does not prove a global absence of host references. Unmanaged editors,
detached daemons, escaped/reparented jobs outside recorded execution families,
containers and arbitrary external bind aliases are outside this guarantee.
Stop unmanaged activity yourself before using force. Stronger guarantees need
runner-owned process groups/cgroups, durable membership or leases, and controlled
filesystem access; repeated host-wide snapshots cannot provide those guarantees.

It refuses active or unverified agent turns, unrelated panes, moved or replaced terminals, another
Herdr endpoint, inaccessible process evidence, and other registry contexts
claiming the workspace or disposable paths. Run on the same host/PID namespace
as Herdr. Historical provider conversations remain
outside this disposal operation.

The permanent checkout must be clean and on the configured base. Exact Git
registration, task scope, branch, checkout and stable Herdr workspace identity
must agree. Multiple branches/worktrees/slices require explicit `--branch`
selection for a proven merged execution. Ambiguous selected targets, unsafe or aliased paths, nested
repositories (including bare repositories), mounts, and identity drift refuse
disposal. Cleanup reads Linux
`/proc/self/mountinfo` before traversal and rechecks all deletion trees before
removal, including linked Git metadata and retained integration/output trees.
Mount boundaries at or below any deletion root refuse disposal, including
same-filesystem directory and file bind mounts. Mount metadata also resolves overlap between registered workflow resource
claims exposed through different mount views; it is not used to enumerate host
process references through arbitrary external aliases. Cleanup rechecks direct
mount boundaries before deletion. Unreadable or malformed mount information
refuses cleanup. Dirty task files
are allowed only on this explicit path. For **unpublished disposal**, live remote task refs, retained task tracking
refs/configuration, any GitHub PR history (including open or closed PRs), or private
publication history/intent also refuse. Cached refs are resolved against complete
remote names (including slashes) and effective fetch configuration in both
checkouts. Only name-preserving `refs/heads/*:refs/remotes/<remote>/*` fetch
mappings, optionally prefixed with `+`, are accepted automatically. Custom,
negative, empty, or malformed fetch mappings require manual inspection; orphaned
or overlapping cached namespaces also refuse disposal. Effective branch `remote`, `merge`, and
`pushRemote` settings are checked in both the permanent and exact task checkout,
including worktree-local configuration and conditional includes. Any effective
`remote.*.push` setting also refuses disposal, including empty or multivalued
settings: custom push refspecs can publish under another name, and current refs
cannot establish historical mapping identity. Remote/PR checks
require access and a GitHub repository with unambiguous remote identity; they never
fetch, publish, delete remote branches, or change Linear. Missing or conflicting integration
provenance refuses deletion of its retained artifacts.

The command supports a dirty reviewed worktree with implementation/review
contexts, an uncertain integration context and its isolated checkout, including
legacy claims without an external completion directory. It also includes earlier
explicitly abandoned integration attempts when their archives still agree.
Stale context workspace IDs are accepted only when those workspaces and terminals
are absent and their issue/repository/checkout/endpoint identities agree. Retained
integration checkouts must live in their exact workflow-owned pass directories;
external completion directories must match the recorded pass and contain only
the completion output. Unrelated execution state is left intact.

Before deletion, a bounded, atomically replaced and fsynced journal in the common
Git directory (`agentic-workflows-cleanup/DEV-30.discard/<execution-id>.json`)
records the exact selectors and provenance. Each disposal claim has a unique
execution ID and journal. Completed journals, including legacy `state.json`
tombstones, remain unchanged when a later execution of the same issue is cleaned
up, even if it reuses the branch or checkout path. Integration tombstones retain
pass/context identity, source/base identity, disposition and hashes of original
provenance, installation/completion/abandonment evidence, and any retained completion file.
They do not preserve the full conflicted files, result reports, or checkout.
The journal is limited to 4 MiB and at most 101 integration record inputs;
exceeding those bounds refuses automatic disposal. Review/loop/private task
metadata disappears with the task's linked Git directory.

Merged journals additionally retain the exact tip/base and PR merge proof (or
ancestry proof), plus a digest of the original scope file or its absence. Every
removal boundary revalidates this evidence, exact target identity, ownership,
filesystem roots and stopped runtime. The proof survives worktree and branch
removal, so `task cleanup ISSUE --force` can finish an interrupted workspace
close, worktree removal, branch deletion or context retirement without creating
scope metadata or repeating a confirmed deletion. An unrecognized Herdr close
response is reconciled against the exact endpoint/workspace, terminal snapshot,
process proof and Git target in the same invocation. Verified closure continues;
a still-open workspace, conflicting response identity or ambiguous evidence keeps
the pending journal and refuses further removal. New or changed proof, reused
paths, new claims and runtime uncertainty stop further removal. Remote refs,
GitHub PRs and Linear state are never changed.

The journal retains an explicit branch selection through retries. Resume with the
same command or `task cleanup ISSUE --force`; a different requested branch refuses
until the pending disposal finishes. After a squash head's local branch is removed,
retry uses its retained SHA to recheck the exact GitHub PR/head/merge proof and the
merge result's reachability from the configured base. It does not require the
now-unreferenced head object to survive Git garbage collection. Missing or changed
PR evidence still refuses retirement.

Cleanup and abort can finish while an independent workspace's loop remains
active, including separate worktrees in the same repository. A loop owns its
selected worktree's existing review/publication lock for the agent turn; it does
not hold the machine-wide registry gate. Cleanup holds that same worktree lock
and the repository-local disposal lock that survives worktree removal.

A **same-workspace conflict** names the owned checkout. Let that controller
finish, or stop it with Ctrl+C before retrying. Abort and ordinary cleanup also
require quitting the selected agents to their original shells. Forced cleanup can
retire verified idle implementation/reviewer sessions and then checks that their
process families stopped. Do not stop an unrelated loop. **Registry contention** waits up
to 30 seconds for a short ownership transaction, then asks you to retry shortly;
persistent contention calls for inspecting the controller doing registry/Git
administration. Shared repository fetch/base updates and worktree removal also
use bounded synchronization. Never delete lock files, journals, or context
history to bypass either diagnostic.

The global gate covers claim admission, inventory and local metadata changes.
A forced disposal saves its durable reservation before releasing the admission
gate; allocation, rebinding and provenance writes reject overlapping pending
reservations even after interruption. Ordinary cleanup records its worktree,
private Git directory and retained artifact claims even when no Herdr workspace
is open. Both paths release the global gate before recursive worktree deletion;
inventory uses the durable reservation while Git removes files and metadata.
That reservation persists until retirement finishes. Other cleanup operations can
proceed on disjoint resources. Locks release on process exit; context inspection
remains read-only. This coordinates cooperating workflow commands and does not
provide filesystem sandbox isolation from external processes.

It revalidates ownership,
paths/mounts and registered runtime immediately before destructive operations,
and saves a durable claim before each step:

1. Close and confirm absence of the exact Herdr workspace and its stopped shells.
2. Remove the archived integrations' isolated checkout directories and recorded
   disposable completion directories.
3. Revalidate and forcibly remove the exact task worktree, then delete only the
   recorded local branch ref with an atomic comparison to its saved SHA.
4. Retire the frozen context rows transactionally. Ordinals, allocation identity,
   former location and retirement time remain; terminal/provider/session bindings
   are cleared. Registry rows are never deleted or reset.

After interruption, rerun the same `--force` command; the Linear issue state
remains unchanged. A pending journal blocks other workspace lifecycle resolution
rather than reopening or recreating execution. A claimed operation whose result is
confirmed absent can finish on retry, including a branch-only remainder after
worktree removal. Partial artifact deletion may continue only in the original
directory. Reused paths, changed refs, new contexts, conflicting evidence, or
uncertain worktree registration stop for inspection. Do not prune registrations,
delete journals, or reset contexts to bypass a refusal. A successful retry leaves
no active task worktree, local task branch, workspace, context, or retained
integration checkout; its bounded tombstone remains for audit. Published-task
abandonment and general historical garbage collection remain out of scope.
