# Create Linear Task plugin

Create Linear Task is the task-authoring plugin for Agentic Workflows Prototype (AWP),
whose first public release is a **Developer Preview**.

The stable plugin name `create-linear-task` and marketplace identifier
`agentic-workflows-lite` are retained across the repository rename. Import the
canonical prototype URL below; existing imports need operator verification after
the rename. See the [migration runbook](../../docs/migration.md) for operator
approval, host migration, and live acceptance requirements.

This directory is the ChatGPT distribution layer for the canonical
[`skills/create-linear-task`](../../skills/create-linear-task/SKILL.md) agent skill. The
standalone skill remains the source of all workflow instructions, configuration, scripts, and
agent metadata. Do not edit the generated copy under `skills/create-linear-task`.

The package uses the current portable Agent Plugins layout: `plugin.json` is the root manifest and
hosts discover bundled skills under `skills/`. `.codex-plugin/plugin.json` is a generated
compatibility fallback. The plugin intentionally has no MCP manifest or server; the skill uses a
Linear integration already available to the host, and it returns a draft when Linear is not
available.

The package shape follows the official OpenAI documentation for
[packaging plugins](https://developers.openai.com/plugins/build/plugins),
[skills-only plugins](https://developers.openai.com/plugins/concepts/skills), and
[connecting and testing a plugin](https://developers.openai.com/plugins/deploy/connect-chatgpt).

## Synchronize, validate, and package

From the repository root, regenerate the bundled snapshot whenever the canonical skill changes:

```sh
python3.12 skills/create-linear-task/scripts/sync_config.py --check
python3.12 plugins/create-linear-task/scripts/sync_plugin.py
python3.12 plugins/create-linear-task/scripts/sync_plugin.py --check
PYTHONPATH=tests:. python3.12 -m unittest test_create_linear_task_plugin -v
```

The check compares every selected bundled skill file byte-for-byte with the canonical directory and
also checks the generated compatibility manifest. Synchronization and ZIP packaging share that
selection logic, so transient files such as `__pycache__/`, `*.pyc`, and `*.pyo` are ignored by
both. The repository test suite runs the same drift check and verifies that these files cannot
change the archive. The explicit public file allowlist excludes private
`config.local.toml`; unknown files (including the old populated `config.toml`) are
refused. Both trees must contain the exact generated `config.example.toml`, even
if a populated file was accidentally copied into both. Synchronization and
packaging never read the runtime registry. Regenerate public examples with the
canonical skill synchronizer before bundling.

Install private target mappings separately using the canonical skill's
[refresh instructions](../../skills/create-linear-task/SKILL.md#establish-context-and-target).
They stay outside Git and archives. The installed renderer requires private
`config.local.toml` beside the skill or an explicit `--config`; public examples
cannot authorize issue creation. A cloud host must have accessible private target
configuration before functional acceptance. No private configuration is inherited
from the packaging machine. Historical archives/Git history remain separate
exposures and are not rewritten by packaging.

Create a deterministic submission archive and optionally verify it later:

```sh
python3.12 plugins/create-linear-task/scripts/package_plugin.py \
  --output /tmp/create-linear-task-plugin.zip
python3.12 plugins/create-linear-task/scripts/package_plugin.py \
  --output /tmp/create-linear-task-plugin.zip --check
```

## Distribution paths

- For workspace/private distribution, this repository includes
  [`.agents/plugins/marketplace.json`](../../.agents/plugins/marketplace.json). It exposes
  `plugins/create-linear-task` as the local source `./plugins/create-linear-task`. A workspace admin
  imports [agentic-workflows-prototype](https://github.com/mitjasiska/agentic-workflows-prototype)
  through **Admin → Plugins → Add → Import marketplace**, leaves
  **Path** empty because the marketplace is at the repository root, selects the branch, tag, or
  commit containing the package, reviews the import, and configures installation policy for the
  intended roles. Follow the official
  [workspace plugin management guide](https://learn.chatgpt.com/docs/enterprise/plugin-management).
- For public distribution, create a **Skills only** draft in the OpenAI plugin submission portal,
  upload the generated ZIP, complete the public review requirements, and publish only after
  approval. Follow the official
  [public submission guide](https://developers.openai.com/plugins/deploy/submission).
- For a local desktop preflight only, expose `plugins/create-linear-task` from a local marketplace,
  restart the ChatGPT desktop app, and install **Create Linear Task** from the Plugins Directory.
  A local preflight does not establish web/mobile availability.

This repository packages the skill but does not claim that a workspace marketplace has imported it
or that a public listing has been reviewed and published.

## Manual ChatGPT acceptance

Run the synchronization, validation, and packaging commands above, distribute the plugin through
one of the appropriate paths above, install it from the Plugins tab, and confirm that installation
does not request an MCP connection.

### Activation acceptance

Use this to prove only that the installed plugin and bundled skill are discovered and invoked:

1. Start a fresh ChatGPT web or mobile chat with **Create Linear Task** enabled.
2. Explicitly select the plugin/skill with `@`, then prompt:

   > I want to prepare a development issue. Do not draft or create it yet. Tell me which configured
   > repository and Linear target context this workflow needs before it can proceed.

3. Confirm that ChatGPT identifies the `create-linear-task` workflow, asks for missing
   repository/target context rather than inventing it, and performs no Linear mutation. Repeat in a
   fresh mobile chat when mobile availability is part of the acceptance target.

Passing this procedure verifies activation only. ChatGPT web/mobile does not implicitly inherit a
checkout or filesystem from the machine or VPS where this repository was packaged.

### Functional acceptance

Run this separately only when the ChatGPT session can genuinely access all of the following:

- a repository checkout whose directory identity matches a project in accessible private configuration;
- the relevant repository instructions and implementation files;
- a Linear integration authorized for the configured team/project and its complete label catalog.

In a fresh chat, explicitly invoke **Create Linear Task** and ask it to create or refine a designated
test issue using that accessible repository. Verify the rendered human sections, collapsed agent
instructions, classification, exact configured target, and intended labels. After the mutation,
verify that the workflow rereads the issue and confirms its exact title, team, project, description,
and label IDs.

If the repository or Linear context is unavailable, stop after activation acceptance. A draft or
context request in that environment does not constitute functional acceptance of rendering,
classification, mutation, or final reread.
