---
name: create-linear-task
description: Create or refine a Linear development issue from a rough idea, with a concise human specification, separate implementation-agent guidance, and conservative configured classification. Use when a user wants an agent-ready Linear task rather than only prose or a local TODO.
---

# Create Linear Task

Create one independently understandable, implementable, testable, reviewable, and mergeable issue. Do not split small work merely to give each activity its own issue. Split only when outcomes can genuinely be implemented and reviewed independently; if that decision changes scope materially, ask first.

This is the task-creation skill for Agentic Workflows Prototype (AWP), whose first public release is a **Developer Preview**.

This optional skill produces a recommended, opinionated authoring convention. Its sections and collapsed `Agent instructions` block are not prerequisites for using Agentic Workflows Prototype. Users may organize issues however they choose: execution receives the complete description unchanged and follows task-specific requirements and guidance wherever they appear. The runtime defaults to a non-blocking warning when its configured block is absent; users may choose `ignore` or explicitly opt into `required` for one named block. That presence check gives block contents no additional authority over other task text or workflow-owned lifecycle and safety instructions. Keep this skill's generated `Agent instructions` name unchanged even when runtime validation uses a custom name.

## Establish context and target

1. Inspect the relevant repository instructions and enough existing implementation to make the issue actionable. Do not turn investigation into implementation.
2. For a refinement, read the current issue including its exact team ID/name, project ID/name, description, and complete label IDs. Its current team and project are authoritative; do not include a team or project change in the mutation.
3. Use the private generated `config.local.toml` installed beside this `SKILL.md` (or the explicit `--config` override) to establish the expected workflow target:
   - An explicit user choice must match one configured project entry.
   - Otherwise use the single entry whose `repo_name` matches the current repository.
   - `linear_project` and `linear_team` are exact selectors; resolve each to exactly one existing Linear object.
   - For a new issue, create it in that configured target.
   - For a refinement, require the issue's current team/project names to exactly match that configured target, preserve the current IDs, and stop if they differ. A user-requested move has no automatic mechanism in this skill.
   - If the target is missing or ambiguous, ask. Never choose a similarly named team or project.

Public distribution contains only `config.example.toml`, marked `example_only = true`.
It is documentation, never an issue-creation target. The renderer refuses marked
examples even through `--config`. If private configuration is missing, stop and
request setup; do not silently substitute the examples.

The private installed config is a snapshot, not a second configuration owner.
Repository developers copy `config/projects.example.toml` to the Git-ignored
`config/projects.toml`, configure their exact targets, and remove `example_only`.
After installing or refreshing the skill scripts, run from the source repository:

```sh
python3.12 skills/create-linear-task/scripts/sync_config.py --private \
  --output "$HOME/.codex/skills/create-linear-task/config.local.toml"
python3.12 skills/create-linear-task/scripts/sync_config.py --private \
  --output "$HOME/.codex/skills/create-linear-task/config.local.toml" --check
```

Use your actual installed skill path (including a custom `CODEX_HOME`) or a private
`config.local.toml` outside Git and pass it explicitly with `--config`. Private
synchronization requires an explicit destination outside all Git checkouts; it
never overwrites public configuration. Refresh mappings from the private registry,
not by editing an installed snapshot. Refreshing public skill files preserves the
separate installed local file. Hosts without local filesystem access must request
accessible private target configuration; they do not inherit the packager's files.

For maintainers, `python3.12 skills/create-linear-task/scripts/sync_config.py`
regenerates only the public example snapshot; `--check` detects drift. Never copy
private config into a public skill/plugin or archive. Historical Git and previously
distributed mappings remain a separate exposure; these commands do not rewrite
history or clean earlier installations.

Do not create, rename, or delete teams, projects, or labels. Do not read credentials to compensate for a missing Linear integration; if Linear cannot be accessed, return a prepared draft and say it was not created.

## Write and classify the issue

Use a concise, human-readable title that can produce a descriptive branch name. For this skill's output, draft only the human sections that materially help:

- `Context / why` is optional; explain why the work exists, not its execution plan.
- `Scope / outcome` is required and must stand on its own.
- `Done when` is required and expresses testable behavior and relevant regression coverage as practical, verifiable completion conditions.
- `Boundaries` is optional; include it when preventing adjacent changes matters.

For behavioral changes, expect thorough automated coverage of requirements, regressions, risk-driven edge cases, failure paths, and invariants, with meaningful assertions. Focused validation limits repeated execution, not the number of valuable tests to create; do not reduce coverage to save execution time or set arbitrary test-count targets or caps. Avoid artificial tests for non-testable work.

Default to relevant focused test execution and proportionate cheap checks. Use test-first development when useful; do not automatically insert strict red–green–refactor, blanket full-suite, or "all tests pass locally" obligations. Preserve explicit user and project requirements, including stricter test methodology or local/per-pass execution instructions. Distinguish "full regression must pass before merge" from "this agent must run the full suite locally": merge-time regression may be satisfied externally where permitted, while explicit local obligations still apply. External repository CI / GitHub Actions is the preferred broad-regression owner, but do not assume it exists. Require an honest report of actual commands/results and outstanding checks; without an established external gate, surface pending final validation for human verification. A clean code review does not establish that unrun regression passed. AWP ends at a PR for human review and manual merge.

Classify these separate facts:

- `task_kind`: how the work is performed (`implementation`, `research`, or `experiment` by default).
- `primary_category`: at most one of `feature`, `bug`, `chore`, `docs`, or `refactor`, or `null` when none clearly applies.
- `research_modifier`: `true` when research is part of the work, otherwise `false`.

Task kind does not imply a primary category. A research task that only investigates a bug has no primary category; an experiment can be a feature, chore, or unclassified depending on its intended outcome. However, `task_kind = "research"` necessarily requires `research_modifier = true`; contradictory input must stop. If task kind or primary category is genuinely ambiguous, ask or explicitly use `null`; do not choose the nearest-looking value.

The canonical configuration is closed and maps primary categories as follows: `feature` → `Feature` / `feat`, `bug` → `Bug` / `fix`, `chore` → `Chore` / `chore`, `docs` → `Docs` / `docs`, and `refactor` → `Refactor` / `refactor`. Research independently adds the `Research` label. Pure research has no change type; primary + Research retains the primary change type. Do not add categories or substitute labels outside this taxonomy.

Classification alone does not justify another human-facing section. In particular, do not make the visible specification longer merely because the Research modifier is set; include research detail only where it helps explain the outcome or completion conditions.

## Keep agent guidance separate

Put only execution-relevant details in the collapsed `Agent instructions` block:

- repository or context to inspect;
- task-specific implementation, research, or experiment guidance;
- important constraints;
- validation expectations;
- the configured stop condition;
- task kind, primary category, Research modifier, intended workflow labels, and change type.

The block must state that it addresses the implementation agent executing the issue. Do not repeat the human specification there. The default stop condition is to finish the requested work and relevant focused validation, honor explicit local/per-pass requirements, and honestly report outstanding checks, then stop ready for independent review without committing, pushing, or opening a PR unless another workflow explicitly requests it. It does not automatically require a full-suite run before handoff or treat pending merge-time checks as satisfied.

Render the collapsed block with Linear's API Markdown delimiters: `+++ Agent instructions` to open and `+++` to close. Preserve this API representation. `>>>` is only the interactive-editor shortcut and must not be emitted in an API description.

## Render deterministic fields

Prepare a JSON object with this shape, using empty lists for inapplicable agent subsections:

```json
{
  "title": "Concise title",
  "context": "Optional context or null",
  "outcome": "Independently understandable result",
  "done_when": ["Verifiable condition"],
  "boundaries": [],
  "task_kind": "implementation",
  "primary_category": "feature",
  "research_modifier": false,
  "agent": {
    "repository_context": [],
    "guidance": [],
    "constraints": [],
    "validation": ["Relevant validation"]
  },
  "existing_target": null,
  "existing_labels": [],
  "workspace_labels": [
    {"id": "existing-label-id", "name": "Feature"}
  ]
}
```

Use `null` for a genuinely unclassified task kind or primary category; all three classification fields must still be present. For a new issue, set `existing_target` to `null` and use an empty `existing_labels` list. For a refinement, set it to `{"team_id": "…", "team_name": "…", "project_id": "…", "project_name": "…"}` from the current issue. The renderer preserves those IDs and refuses a team/project name that conflicts with configuration; the returned issue update contains no target fields.

Populate `workspace_labels` with the complete active catalog of labels applicable to the selected team as `{id, name}` objects. Do not pass a broader unscoped catalog. Existing unrelated labels need not appear there: an issue may retain archived labels omitted from active-catalog queries. Treat those existing IDs as opaque and preserve them without resolving, recreating, renaming, or mutating them. Run `scripts/prepare_issue.py --repository <repo-root>`, optionally adding `--project-key <configured-key>` or `--config <path>`, and provide the JSON on standard input. With no override, the script loads the configuration from the installed skill directory and does not depend on the source repository.

Before any mutation, the renderer exact-matches all six canonical label names in that selected-team catalog, including canonical labels not intended for the current task. Any missing or ambiguous canonical label stops preparation; never substitute or create one. It emits only `label_changes.add` and `label_changes.remove` for canonical workflow label IDs; it never emits a replacement set containing unrelated labels. With `replace_workflow_labels = false`, stale or mutually exclusive workflow labels stop instead of producing contradictory labels. Replacement is allowed only when that project explicitly sets the option to `true`; even then, only the six canonical workflow labels can be removed.

For a refinement, the issue and label state read while drafting are not authoritative. Immediately before mutation, re-read the issue's current target and complete labels plus the complete selected-team label catalog. Replace `existing_target`, `existing_labels`, and `workspace_labels` in the draft, then rerun the renderer. Apply only title, description, and the resulting workflow-label additions/removals; do not submit team/project fields or all label IDs as a replacement. This preserves the existing target and unrelated labels added concurrently. If the Linear integration cannot perform label-scoped additions/removals, stop rather than falling back to a stale full-label update. For a new issue, use only `label_changes.add` as its initial workflow labels.

This fresh-read strategy is not an atomic lock against another Linear actor. After mutation, keep the original pre-mutation `existing_target` unchanged. Re-read the issue and put its target in a separate `current_target` object with the same four ID/name fields; replace only `existing_labels` and `workspace_labels` with the post-mutation state, then run the renderer with `--verify-labels`. Verification requires the reread team and project IDs to exactly equal the original IDs—names alone are insufficient—and requires the canonical workflow labels to exactly match `intended_workflow_labels` while ignoring unrelated labels. If it reports a concurrent Linear change, fail clearly and report the mismatch; do not automatically remove, overwrite, or otherwise repair the concurrent change.

Create or update the issue through the available Linear integration, then re-read it and verify the exact title, team, project, description, and label IDs. Report its identifier and link. If the renderer or exact Linear resolution fails, stop before mutation and surface the ambiguity or configuration gap.
