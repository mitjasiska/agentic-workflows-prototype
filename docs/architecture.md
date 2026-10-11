# Architecture and validation

[Project overview](../README.md) · [Lifecycle reference](lifecycle.md) · [Agent instructions](../AGENTS.md)

Agentic Workflows Lite (AWL) is a local, single-machine Python development
controller. Its first public release is a **Developer Preview**.

- [Execution contract and responsibilities](#agent-execution-architecture)
- [Handoff transport](#handoff-transport)
- [Context identity and registry](#workflow-context-identities)
- [Experimental local run metrics](#experimental-local-run-metrics)
- [Validation and historical live checks](#validation)

## Agent execution architecture

AWL supports multiple agent adapters. Codex is the current default
and most mature adapter; Pi is the second supported implementation. Additional
agents can be implemented as adapters without rewriting task lifecycle logic.

The shared contract in [`task_start/agent.py`](../task_start/agent.py) is deliberately small:

- `AgentOptions` is the resolved agent, model, and workflow mode.
- `AgentExecution` carries the fresh issue snapshot, resolved repository and exact
  worktree, execution purpose, an already-constructed semantic handoff, and an
  extensible workflow-policy mapping. It does not invent purpose-specific wording.
- `LaunchResult` contains the pane/session/turn information deterministic workflow
  code may report. `AgentAdapter` is the launch boundary.

Responsibility is split as follows:

- Core owns fresh Linear retrieval, project/repository resolution, deterministic
  Git and Herdr workspace preparation, status transition, option precedence, and
  construction of the semantic implementation handoff. Implementation and review
  have separate purpose-specific builders in [`task_start/handoff.py`](../task_start/handoff.py).
- An adapter owns executable availability, supported values and mappings, CLI
  arguments, working-directory behavior, prompt transport/receipt mechanics, and
  agent-specific launch validation. Agent-specific capabilities are handled at this boundary.
- Machine-local configuration owns credentials, paths, the default agent/model/
  mode, repository-to-Codex-profile selections, and each CLI's authentication,
  trust, provider, and permission settings.
- Linear content owns task-specific intent and constraints. It is fetched fresh,
  transported in memory, and is never copied into tracked repository files.

The repository entry point is [`task`](../task), with command orchestration in
[`task_start/cli.py`](../task_start/cli.py). The main lifecycle components are
[`workspace.py`](../task_start/workspace.py) for Git/Herdr operations,
[`contexts.py`](../task_start/contexts.py) for context allocation and inspection,
and [`review.py`](../task_start/review.py) for review passes. Review snapshots and
result validation live in [`review_state.py`](../task_start/review_state.py) and
[`review_result.py`](../task_start/review_result.py). The bounded
[`loop.py`](../task_start/loop.py) controller composes that review pass with
[`implementation_pass.py`](../task_start/implementation_pass.py) for initial launch,
completion, and fixes through the same adapters. Start and loop share workspace
and Linear preparation in [`preparation.py`](../task_start/preparation.py); the loop
owns its initial result contract and durable handoff claim.
The shared implementation handoff also carries optional
[task readiness guidance](lifecycle.md#implementation-readiness-outcomes).
The same agent assesses and implements in one turn; the controller validates and
reports the structured outcome through the existing implementation result and
checkpoint. It does not classify issue quality or create an assessment phase.
[`loop_state.py`](../task_start/loop_state.py) serializes private checkpoint and
pause updates; it does not store a run archive or choose models. The controller
holds the existing publication lock and calls the review primitive within that
ownership. [`pass_delivery.py`](../task_start/pass_delivery.py) retains one private
pass result destination and completion seal. Adapters persist a session-bound
prompt digest before delivery and verify the exact provider completion receipt.
Restart collection returns to the same loop router and review acceptance writer;
it never launches or resumes an agent. See [loop controls and recovery](lifecycle.md#task-loop).
Reviewed publishing stages
live in [`publish.py`](../task_start/publish.py),
with disposable integration and unpublished commit installation in
[`publication_rebase.py`](../task_start/publication_rebase.py),
and private acceptance/intent persistence in
[`publication_state.py`](../task_start/publication_state.py). See the
[publishing contract](lifecycle.md#task-pr). External API boundaries are
[`linear.py`](../task_start/linear.py) and [`github.py`](../task_start/github.py).
Explicit conflict recovery lives in [`integrate.py`](../task_start/integrate.py)
and its bounded delivery/proof record in
[`integration_state.py`](../task_start/integration_state.py). It reuses the same
deterministic replay, snapshots, task lock, rebase journal, and guarded installer.
Its durable checkout has separate refs, files, index, and object storage; only
proven tree/blob objects return to the source repository. Agents edit files while
the conflict index stays unchanged; the controller stages only verified successful
completion. The output directory is retained under the temporary root so the
documented Codex workspace-write profile needs no extra permissions. Recorded slice
scope is carried into the handoff. Initial installation and recovery share the
same frozen-provenance guard, required at every checkout/index/ref mutation and
atomic success-record replacement. It protects the local binding, scope, source,
base object, and proven tree/history without requiring moving base/task refs to
stay unchanged. Fresh review uses that installed base; `task pr` separately checks
remote publication eligibility. Confirming a pushed task SHA and PR remains valid
when main subsequently advances. See
[integration lifecycle and recovery](lifecycle.md#task-integrate).

## Handoff transport

Codex is launched with `--cd` and uses its local app-server API for a readiness
exchange, durable queue submission, and exact recorded-turn confirmation. Pi has
no working-directory flag: Herdr starts it in the already-confirmed task pane and
the adapter validates both reported working directories before submitting the full
handoff through `herdr agent prompt`. Herdr confirms prompt submission, but does
not identify a Pi turn for that prompt. Both remain interactive sessions.

### Codex receipt confirmation

Fresh implementation and reviewer contexts share the same launch boundary.
Pane creation and context allocation/labeling do not prove the shell is ready:
shell startup children can still occupy its foreground process group. Before
sending input, the Codex adapter polls `pane process-info` for up to 30 seconds
(`CodexAdapter.SHELL_READY_TIMEOUT`). The exact pane must report a positive shell
PID, that PID as the foreground group, and only that shell in the group. A changed
shell identity or malformed/mismatched observation fails immediately. A timeout
reports the last shell PID, foreground group, and foreground PIDs, without sending
Ctrl+C or launching Codex.
Each process observation receives only the remaining readiness budget as its
subprocess timeout. The adapter rechecks the deadline after the observation and
rejects even a ready shell reported at or after expiry, before sending input.

Once ready, the adapter clears stale shell input once and initializes the receipt
API. It issues `agent start` once; Herdr owns the subsequent runtime-readiness wait
(`--timeout 30000`). The adapter validates the returned runtime target, status and
exact argv. It never retries `agent start` after a failure or uncertain response.
If a validated non-ready launch report contains a provider session, the adapter
persists that identity before further startup inspection. Later omissions or
conflicting reports cannot erase or replace it, including during trust recovery.
Initial loop and integration launches retain that identity in the same context
guard immediately, without requiring readable provider history at the first
runtime observation. The caller adopts the verified session binding only after
the Codex adapter confirms the recorded readiness turn within its reconciliation
budget; completion verifies provider history again.
Session discovery and recorded-turn confirmation below poll observation only;
delayed visibility never repeats launch or queues another handoff. Errors identify
the failed phase; a session that has not been observed is not assumed absent.

Codex starts with a short native prompt containing a random readiness marker. It
asks only for `READY`, without tools or edits. The local stdio app-server API's
`thread/list` identifies the session by that exact marker and checkout;
`thread/read` and `thread/items/list` confirm the readiness turn. The workflow
never selects the latest session or infers its identity from a title.

`thread/queue/add` then delivers the unmodified task to that terminal-owned session
with a unique message ID. History polling must find the same session, checkout,
message ID, exact text, and a turn ID before success. The helper never creates,
resumes, or executes a model session and exits after confirmation. No shared daemon
or service is installed. These APIs (including the experimental queue endpoint)
were validated with Codex CLI 0.157.1; unsupported or malformed responses fail
clearly.

Herdr's `interactive_ready` and `working` states alone do not prove a Codex turn:
startup/trust dialogs can consume terminal input, and `agent prompt --wait` does
not track individual turns. Codex task text is therefore never pasted into the
terminal. Receipt polling has a 30-second deadline and never resends input. A blocked
startup normally fails closed, with pane and session IDs for inspection.

Before any queue attempt, a typed Herdr `agent_not_ready` response, an unrecognized
launch/observation response envelope, or a valid session-discovery timeout permits
read-only startup inspection. Recovery requires the exact Codex argv including the
native readiness nonce, foreground process identity and checkout, and the allocated
terminal. Human action additionally requires a recognized menu in the visible
viewport. The supported signatures are Codex's folder-access trust menu and its
ChatGPT/device-code/API-key sign-in method menu. Missing argv/cwd information or
clipped/unknown screens refuse this path. An already observed provider identity
permits this boundary, provided every subsequent runtime observation and provider
discovery matches it. It does not prove readiness or authorize delivery by itself.
The adapter never reads credentials, changes trust configuration, or answers a
Codex dialog.

The original workflow remains alive at an explicit human-action boundary. It saves
`awaiting_user` in the context registry while retaining the handoff, nonce and
caller result paths in memory. User acknowledgement rechecks the original process
and terminal; it does not establish readiness. A single 30-second budget covers
post-action runtime reconciliation, provider discovery and readiness/history checks.
Transient `blocked` or `unknown` reports and unrecognized observation envelopes
are polled on the same process and terminal; observations receive the remaining
budget, and late responses cannot authorize delivery. After a non-ready or
unrecognized launch response, an already-ready original runtime can proceed to
provider verification without requiring a menu that has disappeared. Launch and
queue mutations are never retried. Identity conflicts, replacement, unsupported evidence or timeout stop
recovery. Only the exact provider session and recorded readiness exchange permit
progress. A paginated history check rejects additional user input, an empty queue
check excludes pending provider input, and the adapter rechecks process identity before its first
queue attempt. If readiness regresses during verification, history must be checked
again after the same runtime becomes ready within that budget. Process observation,
provider identity, and recorded task delivery
remain separate facts. Queue errors and uncertain receipts never enter recovery.
Interrupted owners retain the pending mapping and cannot be replaced automatically.
Loop passes with durable delivery evidence can observe their exact original turn;
interrupted pre-delivery setup cannot reconstruct a lost launch. See the
[user-action flow](lifecycle.md#codex-first-use-trust-and-setup).

### Pi prompt submission

Pi starts with only supported `--model`/`--thinking` flags (or no arguments).
Although Pi CLI accepts an initial message, Herdr 0.9.1 rejects the multiline
handoff as an `agent start` argument (`invalid_agent_argument`). After confirming
the canonical `pi` process, exact argv, pane, and foreground/current directories,
the adapter submits the unchanged handoff once through `herdr agent prompt` and
validates the returned terminal, session, pane, and checkout. It deliberately does
not wait for Herdr's generic `working` state: unrelated Pi activity could satisfy
that state, and Herdr does not tie it to a particular prompt. Success therefore
means only that the prompt was submitted, not that a corresponding Pi turn started
or completed. A rejection, timeout, or target mismatch is not success; inspect the
pane before retrying because input may already have been sent. Authentication,
project trust, or model errors remain visible in the pane.

## Workflow context identities

Each new implementation launch receives a visible pane label such as `DEV-20-I1`.
The identifier is the canonical Linear issue ID, followed by `I` (implementation),
`R` (review), or `G` (integration), and a monotonic ordinal. These identify agent
contexts, not Git worktrees: contexts can share a checkout. `task review` consumes the same allocation,
session identity, and pane-labeling primitives for independent reviewers.

Inspect contexts without loading workflow configuration or contacting Linear:

```sh
task contexts
task contexts DEV-20
task contexts DEV-20 --all
```

The default view includes non-retired contexts, including uncertain launches and
stale mappings. `--all` adds retired history. Output includes selected agent/model/
mode, recorded lifecycle status, live Herdr status, resumability, and the socket,
workspace, tab and pane needed to locate the context. Unspecified model/mode or
unavailable session evidence is `unknown`. A session reference does not by itself
establish resumability: Pi references remain `unknown` without persistence evidence,
while Codex's receipt API confirms a persisted readiness turn before reporting
`yes`. `task review --resume` rechecks provider history; registry evidence does not
guarantee that the provider will retain that history indefinitely.
Integration contexts bind a distinct durable checkout in a separate pane of the
task's Herdr workspace. Implementation identity is never rebound. Registry schema
version 2 adds the explicit integration role through a transactional migration
that preserves existing rows and ordinals; read-only inspection accepts versions
1 and 2 without migrating them.

### Registry allocation and lifecycle observations

The machine-local registry is `~/.agentic-workflows-lite/contexts.sqlite3`, separate from
project files and Linear. Python's SQLite support supplies atomic transactions and
cross-process allocation locking without a service or dependency. The allocation
commits before labeling or launching; failed and interrupted attempts consume their
ordinal. Runtime retirement and artifact release are distinct. Ordinary retired rows
keep identity, allocation time,
retirement time and former location, but discard provider and terminal handles.
Abandoned integration rows retain their artifact bindings; even a retired G
runtime owns its retained checkout until exact disposal releases those artifacts.
Cleanup never resets ordinals. Deleting this database is a destructive registry
reset that discards allocation history; normal operations never do that.

Context identity, runtime/session references, resumability, and lifecycle status
are independent fields. The registry records `launching`, `awaiting_user`, `active`, `uncertain`,
and `retired` observations; it does not implement a state-machine framework or
semantic run reports. Adapter observers save real session handles as soon as they
become available in startup, confirmation, or prompt responses, including before
later handoff failures. Subsequent observations must match the established session;
reporting provenance such as `source` is retained separately and is not identity.
Review passes use `reviewing` while one caller owns that reviewer; an atomic registry
claim prevents concurrent follow-ups in the same conversation. Clean, findings,
blocked, and failed verdicts are not registry lifecycle states. Clean acceptance
is retained separately in private worktree Git metadata for publication.
A failed fresh review launch retains the `uncertain` allocation and any identity
already observed by the adapter. It does not run reviewer finalization against a
possibly unestablished runtime or append a secondary missing-identity error to the
launch failure. Its ordinal remains consumed, even if no session was observed.
A launch interrupted before its result can be recorded remains `launching`;
another concurrent launch cannot claim that pane until the uncertain context is
inspected and cleaned up, or its controller-owned loop pass is proven complete
through the recorded delivery and provider receipt.

### Live identity inspection and cleanup

Herdr owns the live layout. Inspection never renames, focuses, resumes, repairs, or
rebinds anything. It checks socket, pane and terminal identity, reports manual
renames, absent/replaced agents, missing panes and session mismatches, and uses the
live tab when a pane is rearranged. A move to another workspace can change Herdr's
pane ID; inspection reports a matching terminal at its new location while retaining
the original binding. Contexts belonging to another Herdr socket remain visible
with unknown/stale live state; inspect from that server to reconcile them. Focusing,
typing directly to, or stopping an agent does not change its workflow identity.

`task start` refuses a duplicate agent, and `--no-agent` prepares/focuses the
workspace without an agent; neither allocates or relabels an existing context.
A fresh launch gets the next ordinal. Cleanup retires mappings
only after confirming removal, scoped to the issue, repository, checkout and Herdr
workspace/server, including absent older workspace instances for that same cleaned
checkout. Normal completion and cleanup retries perform the same reconciliation.
Partial cleanup retains mappings and allocation history for a retry. A terminal
moved outside the cleaned workspace also retains its mapping
until its closure can be confirmed. Existing Git/Linear cleanup safety checks
still apply.

Explicit forced local-execution disposal is orchestrated by
[`cleanup.py`](../task_start/cleanup.py), using the same exact Git/Herdr selectors,
publication lock and context registry. It adds a bounded journal in common Git
metadata so identity and integration provenance survive removal of the linked
worktree's private directory. Frozen context rows are retired with a transactional
comparison, including abandoned integration contexts. Completed disposal journals
release only the exact frozen resources and contexts they covered. Cleanup reads
retained history and current/archived provenance to distinguish claimed resources,
pending disposal reservations and released history. Private Git metadata and
foreign pending journals participate in overlap checks.

[`ownership.py`](../task_start/ownership.py) supplies a short shared/exclusive
OS gate beside the registry. Agent turns hold only the selected worktree's
existing review/publication lock. Abort and both cleanup paths use that same
lock; lock identity includes the exact Git directory and checkout identity,
not just an issue number. A second controller for that worktree refuses.

Lock order is task/disposal locks (nonblocking), then the permanent checkout's
publication lock when shared Git administration is needed, then the registry
gate, then SQLite writes. Selection may try a task lock under the gate, but must
never wait for one. Repository administration may wait only outside the gate and
never waits for a task lock. Fetch/base update and local removal use this shared
repository lock, without holding it across agent turns. The gate permits bounded
waiting for short registry transactions, inventory snapshots, Herdr allocation,
and Git allocation changes; it never encloses an agent turn, remote cleanup
proof, or recursive worktree deletion. Existing task selection is locked before
fetch/base update, so a conflicting start cannot first advance the shared base.
SQLite continues to serialize context ordinals and compare-and-set writes.
Registry schema v3 also remembers repositories admitted for cleanup, so journals
for workspaces without agent contexts remain discoverable across projects and
after worktree removal. This stores repository locations, not duplicate claims;
older context rows and ordinals remain intact.

Forced cleanup rechecks all resource claims and fsyncs its reservation under one
exclusive admission transaction. Once that gate is released, its pending journal
continues to exclude overlapping context/provenance claims, including after a
crash. Ordinary cleanup fsyncs its exact worktree, private Git directory and
retained artifact claims before removal, even without an open Herdr workspace,
and retains that reservation until context retirement. Inventory uses these
reservations instead of reading metadata that Git may be deleting. Both cleanup
paths apply the existing task-scoped Linux stopped-process proof before removal.
Claim allocation also tries the selected checkout and exact
Herdr binding's existing worktree locks. Cleanup's repository-local disposal lock
survives removal of the worktree lock file. Checkpoint controls use short gate/
SQLite transactions, so pause and status remain available during an agent turn.
This is a cooperating-workflow ownership boundary, not a host-wide
process/reference lease. Process checks cover registered shells and their families;
filesystem checks prevent deletion across nested mount or symlink boundaries.
Pending disposal gates
workspace resolution and preparation; only the explicit cleanup command may
continue its journaled removals. See
[destructive semantics and recovery](lifecycle.md#forced-local-execution-disposal).

## Experimental local run metrics

[`metrics.py`](../task_start/metrics.py) writes experimental observations to
`~/.agentic-workflows-lite/metrics/runs.json`, beside the context registry.
It is a versioned JSON object with `version: 1` and a `runs` array; each record
also carries `version: 1`. Copy this file for export or future migration. It is
local only, independent of run reports, artifact stores, and remote services.

The writer retains up to 256 runs in insertion order, updating retained IDs in
place. A pruned in-flight run can reappear when its final observation arrives.
Records are limited to 4 KiB, the JSON snapshot to 2 MiB, and one bounded scratch
snapshot may remain after a crash. There is no archive. The directory is created
with mode `700`, files with `600`; non-private, foreign-owned, linked, corrupt,
oversized, or unsupported-version files are skipped. Nonblocking locking and
atomic replacement protect concurrent writes. Contention or storage failures can
drop observations. Removing only the `metrics` directory while controllers are
stopped resets the experiment without changing workflow state.

Fields describe workflow observations, not estimates:

| Fields | Meaning |
| --- | --- |
| `run_id`, `task_id`, `context_id` | Existing pass ID (a new UUID for interactive dispatch), Linear identifier, and context identity when known. |
| `role` | `implementation`, `fix`, `review`, `re-review`, or `integration`; the schema also permits `research` if a future execution path provides it. |
| `agent`, `model`, `mode` | Resolved workflow selections. Null means unknown/default; runtime model switching is not inferred. |
| `started_at`, `finished_at`, `wall_clock_seconds` | UTC controller observation interval around launch/resume and result collection. Duration is exactly finish minus start, including startup, waiting, human setup, and validation in that interval. Clock adjustments can make it negative. It is not active model time. |
| `status` | `running`, `dispatched`, `completed`, `failed`, `interrupted`, `blocked`, or `timed_out`. Integration `human_decision` maps to `blocked`. |
| `loop_id`, `review_round`, `previous_run_id`, `implementation_context_id` | Known loop/pass/context relationships. Review rounds are one-based; a fix refers to the round supplying its findings. Integration links to its source review pass. Unknown relationships remain null. |
| `review_outcome`, `findings_count` | High-level validated review verdict and count when available, without finding text. |
| `loop_status` | Last observed loop routing state for this pass, including escalation or interruption. |
| `result_observed_at` | When recovery collected a verified result for the original pass, if applicable. This is a late observation timestamp, not an execution finish. |
| `runtime_versions` | Python version already available in the controller; external CLI versions are not probed. |

Interactive `task start` records dispatch with null finish/duration because the
controller does not observe agent completion. `--no-agent` emits nothing. Loop
implementation/fix passes, fresh/resumed reviews, and integration agent passes
record their observed result. Integration completion describes the agent pass,
not installation or publication. On interruption or timeout, the interval ends
when the controller stops observing; the agent may still be running. A killed
controller can leave `running` with no finish. After verifying and collecting the
original result, recovery updates its retained metrics status and available review
outcome/count, with a separate `result_observed_at`. It preserves the original
observation interval, including an unknown finish/duration, and does not recreate
missing metrics records. Manual turns outside these controllers are not captured.

Token usage, subscription quota, active-model time, and monetary cost are absent.
No issue descriptions, prompts, transcripts, checks prose, findings text,
credentials, or semantic run reports are recorded. Missing/corrupt metrics never
authorize or block execution, review acceptance, publication, recovery, or cleanup;
only the existing workflow evidence governs those actions.

## Validation

Agent-local implementation and default `focused_first` review use relevant focused
checks and proportionate inexpensive validation. Both findings and clean review
passes retain targeted testing without automatically triggering the full suite.
Continue full-diff inspection and targeted/adversarial checks after finding a
defect, then report the complete findings batch. Focused execution does not reduce
thorough test coverage. Explicit issue/repository local or per-pass requirements
remain authoritative, and `exhaustive` requests full validation on every review
pass, even with findings. See [validation guidance](lifecycle.md#validation-guidance).

Tests mock Linear, GitHub, Herdr, Codex RPC, and process boundaries and exercise Git
safety and authoritative remote lookup in temporary repositories with a local fake
remote. Controlled acceptance coverage drives the normal task-start semantics and
exact handoff construction through both Codex and Pi adapters, including Pi's
separate startup and prompt submission. It also covers pre-existing Pi activity,
model-specific thinking-level clamping, purpose-specific handoffs, option
precedence, unsupported combinations, handoff ordering/failures, exact context,
mutable titles, ambiguity, slices, squash-merge history and stale tracking refs.
Cleanup tests exercise actual worktree/branch removal in disposable repositories,
narrow Python-cache disposal, exact Herdr workspace retirement, preservation on
refusal, repeat runs, and partial-failure reporting.
[`test_canceled_cleanup.py`](../tests/test_canceled_cleanup.py) adds In Progress
dirty reviewed task and uncertain/abandoned integration disposal, unrelated-state
preservation, publication and process refusals, archival ordering, interruption
recovery and path-reuse checks. Process checks use a synthetic Linux process filesystem.
[`test_merged_cleanup.py`](../tests/test_merged_cleanup.py) covers merged manual
worktrees without scope metadata, exact GitHub/ancestry proof, ambiguity and
ownership refusals, concurrent changes and recovery at each removal boundary.
Tests need no API key, network access, agent installation, or real Herdr workspaces.

Focused implementation/review checks for forced cleanup include cleanup and
integration-recovery coverage:

```sh
python3.12 -m unittest discover -s tests -p 'test*cleanup.py' -v
python3.12 -m unittest discover -s tests -p 'test_integration_abandon.py' -v
```

Final pre-merge gate: the full offline regression suite must pass before human
merge, separately from agent-local focused validation:

```sh
python3.12 -m unittest discover -s tests -v
```

Configured external CI is the preferred owner of this gate where requirements
permit it; count it only with evidence identifying the source and applicable
revision. Without an established external gate, report final regression as pending
human verification. Record unrun checks as `not_run`; a clean review does not mean
that unexecuted regression passed. CI does not replace explicit local/per-pass
checks.

Loop coverage uses real disposable Git worktrees, context registries, and private
checkpoint databases with controlled agent/config/Linear boundaries. It covers
from-scratch preparation/launch, initial structured outcomes, separate implementation
selection, clean-first-review, repeated fix/review passes, exact context reuse, all pause
boundaries, a pause from a second process, continuation without replay, cancellation,
orphaned claims, corrupt checkpoints, identity loss, malformed outcomes, drift,
non-progress, and bounded escalation. These are offline checks, not live model
or Herdr loop acceptance.

Recovery regressions also terminate real controller subprocesses with SIGINT,
SIGTERM, SIGKILL and SIGHUP while a separate controlled provider process retains
the original pass. They cover completion after exit, observation while still
working, concurrent restart refusal, and exactly one independent review. Provider
fixtures exercise nonce/digest/turn checks; disposable tests cover interrupted
review/fix/re-review, corrupt output, drift, timeout, retained validation limitations,
and journaled artifact disposal. They do not exercise installed provider binaries
or reuse any historical DEV-75 evidence.

Startup regressions inject Linear 503 responses after the original I reservation,
exercise bounded read retries and ordinary-command recovery, and verify refusal
on claims, session/runtime ambiguity, Git/requirements drift, conflicting options,
and concurrent retries. Recovery uses the existing controller lock and an atomic
checkpoint comparison before returning to the normal handoff claim. It neither
reconstructs missing state nor replays uncertain Linear mutations. The real DEV-75
checkpoint remains outside these disposable tests; no live recovery is claimed.

[`test_task_assessment.py`](../tests/test_task_assessment.py) covers readiness
configuration, full-description handoffs, enabled/disabled start/loop parity,
independent structure modes, controlled ready/blocked results, unchanged checkout
on a block, mutation detection, and same-session recovery after clarification.
These tests verify instructions and deterministic routing with simulated agent
outcomes; they do not establish a live model's semantic judgment or compliance.

For documentation changes, verify relative file links and heading anchors, compare
commands with CLI help, and run `git diff --check`. No dedicated Markdown/link
checker is provided in the repository.

### Recorded live-check limits

These are historical results, not checks rerun by a documentation update.

On 2026-09-29, a disposable Herdr workspace/split-pane probe observed `bash`,
`lesspipe`, and `basename` in a fresh pane's foreground group, followed 55 ms later
by only `bash`. Replaying that observed shape through the pre-fix `task start` and
`task review` paths reproduced the generic startup failure before `agent start`
or the receipt API was called, including review's secondary finalization error.
The probe workspace was closed. This establishes the pre-launch shell race; it
does not claim a live Codex launch or model review. A second disposable probe
exercised the corrected readiness check on one workspace pane and three split
panes. All converged to the shell-only predicate and cleared input once, including
two splits that needed multiple process observations; that workspace was also
closed. Sequenced offline regressions cover both callers, delayed session/receipt
visibility, bounded failures, conflicting identity, exactly-once side effects, and
retained allocation history.

Controlled fresh/resume acceptance tests use real disposable Git task worktrees and
the context SQLite registry, with deterministic Linear/Herdr/provider boundaries.
They verify distinct fresh sessions, exact resume, saved settings, stable state,
and mutation invalidation. The 2026-09-27 live check confirmed Herdr 0.9.1 pane split
identity and preserved task-tab placement, then closed that disposable pane. A live
model fresh/resume smoke could not run in the managed sandbox: Pi could not acquire
its settings/auth locks and Codex's receipt API could not initialize. No live review
verdict is claimed from that check.

The controlled Herdr 0.9.1 smoke test on 2026-09-27 used a temporary registry and
disposable Pi workspaces against the existing checkout, with a no-tools prompt.
It verified visible `DEV-41-I3` and then `DEV-41-I4` after closing the first workspace,
calling the cleanup retirement hook, and reopening the registry. Earlier failed
allocations `I1` and `I2` were also retained. The Git-removal part was simulated by
the retirement hook to preserve the implementation checkout; automated cleanup
tests exercise real disposable Git worktrees. Codex's local session API did not
initialize in the sandbox, so the successful live launch used Pi. All disposable
Herdr workspaces were closed.
