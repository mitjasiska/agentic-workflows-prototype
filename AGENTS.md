# Repository instructions for agents

## Working scope

- Read the task and repository instructions, then inspect the relevant existing
  implementation before editing. Keep changes within the requested scope.
- Work in the prepared checkout and branch. Implementation handoffs stop after
  implementation and validation, ready for independent review; do not commit,
  push, merge, or open a PR unless the active workflow explicitly authorizes it.
- Review handoffs are read-only: inspect and validate without fixing or staging
  task files. Follow the handoff's result-delivery instructions.
- The Python workflow owns Linear retrieval. When executing a task handoff, use
  its supplied issue snapshot; do not contact Linear, read its credentials or the
  machine-local workflow configuration, or save the task description in the repo.
- Keep credentials, populated local configuration, and approval state out of
  tracked files. Do not use `sudo` or destructive Git operations.

## Documentation ownership

- The root `README.md` is the human-readable, high-level project entry point.
  Keep it focused on capabilities, core concepts, supported lifecycle paths,
  setup, and commands/options a human needs. It must make sense on its own.
- Root `docs/` owns deeper project-level architecture, lifecycle, configuration,
  invariants, edge cases, and implementation details. Link from the root README
  to these guides rather than growing it into an implementation reference.
- `AGENTS.md` owns repository-level agent behavior and maintenance guidance.
- Component-specific documentation stays with its owning component. In
  particular, `plugins/create-linear-task/README.md` owns plugin/package details,
  distribution, and acceptance procedures, while the canonical skill's
  `SKILL.md` owns its task-creation instructions. Root docs may briefly explain a
  component's role and link to its documentation; do not copy component internals
  into root `docs/` or rewrite component READMEs as part of project-doc cleanup.
- Preserve these boundaries in future changes. Move or intentionally consolidate
  useful material, update relative links and heading anchors, and prefer the
  existing small set of project guides over many narrowly split files. When
  component documentation already covers a detail, link to that owner.
- Describe implemented behavior. Keep historical validation evidence dated and
  separate from checks performed for the current change.

## Maintenance boundaries

- Lifecycle code owns issue/project resolution, Git/Herdr preparation, status
  transitions, and purpose-specific handoffs. Adapters own CLI arguments,
  capability validation, working-directory behavior, and transport.
- Reuse `add_agent_options`, `resolve_agent_options`, `AgentExecution`, and the
  adapter registry for new execution commands. Keep agent-specific capabilities
  in adapters and provide a handoff appropriate to each purpose.
- Preserve exact issue, checkout, context, and session identity checks and safe
  refusal on ambiguity. See [architecture](docs/architecture.md) and the
  [lifecycle reference](docs/lifecycle.md) before changing these boundaries.
- `config/projects.toml` is the private, Git-ignored human-edited project registry;
  only synthetic `config/projects.example.toml` mappings belong in Git. Follow the
  [canonical skill's refresh instructions](skills/create-linear-task/SKILL.md#establish-context-and-target)
  when mappings change; do not edit installed/generated mappings directly.
- Follow the [plugin's maintenance instructions](plugins/create-linear-task/README.md#synchronize-validate-and-package)
  for plugin changes. Its bundled skill is generated from the canonical
  `skills/create-linear-task/` directory; edit the source and synchronize it.

## Web tasks

- For explicitly browser-enabled tasks, follow the canonical [browser guide](docs/browser.md).
  The host controller prepares each actor's exact instance before handoff. Use the
  supplied `ab-worktree AGENT --instance ID --actor ID --connect-only` prefix on
  every call; never invent an ID or launch Chrome from a sandbox. Internally
  spawned subagents need distinct stable IDs and separate host preparation from
  their parent; block their browser use if the framework cannot provide that.
  Non-browser tasks start no browser. Prefer snapshots, semantic/DOM queries,
  console and network diagnostics; screenshots are opt-in for visual/layout checks.

## Validation and handoff

- Run focused checks appropriate to the change during implementation and default
  `focused_first` review, on both findings and clean passes. Preserve thorough test
  coverage, full-diff review, targeted/adversarial checks, and complete findings
  batches. Explicit local/per-pass requirements and `exhaustive` review remain
  authoritative; see [validation guidance](docs/lifecycle.md#validation-guidance).
- Follow the canonical [AWL validation policy](docs/lifecycle.md#validation-guidance):
  ordinary human merges require independent review/approval and passing fast PR
  checks; full regression runs nightly and gates the final release candidate,
  not every merge. Preserve explicit test requirements and report unrun checks
  as pending, never passed. See [CI operation](docs/architecture.md#github-actions)
  for commands, evidence, and human repository-settings setup.
- For project-mapping changes, check the generated skill snapshot with
  `python3.12 skills/create-linear-task/scripts/sync_config.py --check`.
  Component-specific validation remains documented with that component.
- For documentation changes, verify relative links and heading anchors, compare
  documented commands with CLI help, and run `git diff --check`. No dedicated
  Markdown/link checker is currently provided.
- Review the final diff for scope, information loss, unintended component-doc
  edits, and duplicated component internals in root docs. Report changes, checks,
  and limitations without claiming live verification from mocked tests or past
  evidence.
