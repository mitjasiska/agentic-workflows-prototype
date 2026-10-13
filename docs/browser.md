# On-demand worktree browser

Use the Linux-first browser helper for web tasks from any Git worktree, including
Herdr worktrees. It reuses installed `agent-browser` and Chrome. Setup and agent
startup launch no browser, daemon, or session resolver. The first browser command
starts Chrome when needed; later calls reuse that identity's session.

## One-time user setup

From a stable AWL checkout, run:

```sh
python3.12 scripts/install_browser.py
```

The installer copies the POSIX shell helper to `~/.local/bin/ab-worktree`, copies
this canonical guide to `~/.local/share/awl-browser/browser.md`, and creates
`ab-pi` and `ab-codex` symlinks only if those names are free. It appends a small
guide/command pointer to the global instruction file each harness actually reads.
Existing instruction bytes and personal shortcuts (including an existing
`~/.local/bin/ab-codex`) are preserved. Use `ab-worktree codex` when a personal
shortcut already exists; that shortcut's behavior remains operator-owned.

Ensure `~/.local/bin` and the installed `agent-browser` directory are on the PATH
used by agent shells. If needed, add this line to your shell startup file once,
then restart the terminal/Herdr session and Pi/Codex:

```sh
export PATH="$HOME/.local/bin:$PATH"
command -v ab-worktree agent-browser
```

The global pointers also give an absolute helper path for shells without
`~/.local/bin` on PATH. `agent-browser`, Git, and `sha256sum` must still be on PATH;
missing tools fail explicitly. With an npm/nvm installation, ensure its active
Node bin directory is available to both harnesses, including Codex shell commands.
No browser/session environment variable needs to be exported.

Instruction discovery:

- Codex reads the first non-empty `AGENTS.override.md` or `AGENTS.md` in
  `${CODEX_HOME:-~/.codex}`. The installer appends to that selected file, creating
  `AGENTS.md` if neither has content. See [official Codex instructions](https://developers.openai.com/codex/guides/agents-md).
- Pi 0.87.1 reads the first existing regular file among `AGENTS.override.md`,
  `AGENTS.md`, `AGENTS.MD`, `CLAUDE.md`, and `CLAUDE.MD` in
  `${PI_CODING_AGENT_DIR:-~/.pi/agent}`, even if empty. The installer appends to
  that file rather than creating a higher-priority file that shadows personal
  instructions. See [Pi's resource loader](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/src/core/resource-loader.ts).

The installer honors those home overrides at setup time. Its `--codex-home`,
`--pi-home`, `--bin-dir`, and `--data-dir` options allow explicit destinations;
run `python3.12 scripts/install_browser.py --help`. Use the same homes when
starting agents. Rerun setup if you later add/remove a higher-priority instruction
file or change agent homes. Pi's `--no-context-files` disables discovery; large
Codex instruction files may hit its instruction size limit.

Repeat setup leaves identical files and pointers untouched. Before writing, setup
checks all managed-file and instruction conflicts. Different existing helper or
guide contents, edited/malformed managed pointers, symlinked/hard-linked files,
and unsafe destinations stop with an explanation. For upgrades, review and move
only the two installed helper/guide files aside if their contents changed, then
rerun setup; reconcile an edited pointer manually. Setup never rewrites personal
instructions or shell startup files and never installs Chrome or AppArmor rules.
The installed copies survive removal of the source checkout. This installed guide
is a snapshot; the source of truth remains `docs/browser.md` in AWL.

## Invocation and identity

```sh
ab-worktree pi open http://localhost:3000
ab-worktree codex snapshot -i
ab-pi snapshot -i
ab-codex snapshot -i                  # If installed by this setup
ab-worktree codex --instance reviewer-1 open http://localhost:3000
ab-worktree codex --instance reviewer-1 close
```

Run from the target checkout or a subdirectory. Each call resolves the physical
Git worktree root and derives `awl-<agent>-<digest>` from the root, agent type,
and optional instance ID using NUL-separated fields and the first 128 bits of
SHA-256. Subdirectories and symlink aliases reuse the same identity; different
worktrees, Pi/Codex, and different instance IDs get different identities. Moving
a checkout changes its identity. This deliberately uses its own bounded session
derivation, independent of `agent-browser session id` versions/fallback behavior.

`--instance ID` must precede the browser command. IDs are 1–64 ASCII letters,
digits, dots, underscores, or hyphens, starting with a letter or digit. Omission
means one session per agent type per worktree. Reuse the same ID for every call,
including `close`. AWL does not yet inject unique browser-instance IDs into agent
or subagent launchers. Concurrent same-agent/same-worktree callers share a session
unless explicitly given distinct IDs; this helper does not schedule subagents.

Arguments are forwarded intact with an explicit `--session`; inherited
`AGENT_BROWSER_SESSION` is unnecessary and cannot select the session. A forwarded
`--session` override is rejected. The optional `--` after the helper options ends
helper parsing, as in `ab-worktree pi -- snapshot -i`. Browser command failures
retain their exit code and print a session-specific error; missing tools, invalid
IDs, and non-worktree/bare-repository contexts fail before any browser call.
There is no shared-default fallback or per-worktree configuration. Browser session
data stays in agent-browser's user storage; do not add it to a repository.

## Lightweight development loop

Start your application's development server using its normal project commands.
Use your harness's helper for each call (`ab-worktree codex` below; substitute
`pi` for Pi). Prefer accessibility snapshots and semantic/DOM queries:

```sh
ab-worktree codex open http://localhost:3000
ab-worktree codex snapshot -i
ab-worktree codex find role button click --name Submit
ab-worktree codex get text 'main'
ab-worktree codex eval 'document.querySelector("main").textContent'
ab-worktree codex console
ab-worktree codex errors
ab-worktree codex network requests
```

Inspect the updated snapshot after interactions; element references can change.
Check console errors and failed network requests while iterating. Request a
screenshot only when checking appearance or layout, for example
`ab-worktree codex screenshot /tmp/page-layout.png`; visual analysis is opt-in.
Close the session when the web work is finished: `ab-worktree codex close`.
For installed-version command details, use `agent-browser --help`.

## Ubuntu installation and sandbox troubleshooting

If the tools are already installed, reuse them. Otherwise the operator installs
them once (system dependency installation can require administrator privileges):

```sh
npm install -g agent-browser
agent-browser install --with-deps
```

See [agent-browser installation](https://agent-browser.dev/installation). To check
headless Chrome from a Git worktree after setup:

```sh
ab-worktree codex open about:blank
ab-worktree codex snapshot
ab-worktree codex close
```

If Chrome reports `No usable sandbox` or a user-namespace permission error on
Ubuntu, inspect kernel/AppArmor diagnostics for `userns_create` denial. Determine
the **actual installed Chrome executable path** from the launch error or the
browser installation, and resolve symlinks with `readlink -f /actual/path/to/chrome`.
Do not assume the downloaded Chrome lives at `/opt/google/chrome/chrome`.

An administrator can review an application-specific profile under
`/etc/apparmor.d/awl-agent-browser-chrome`, following
[Ubuntu's user-namespace exception guidance](https://documentation.ubuntu.com/release-notes/24.04/#unprivileged-user-namespace-restrictions).
Replace the quoted path below with that exact physical executable path:

```text
abi <abi/4.0>,
include <tunables/global>
profile awl-agent-browser-chrome "/absolute/installed/chrome/path" flags=(unconfined) {
  userns,
}
```

The administrator loads/reloads that single profile using `apparmor_parser -r`
on its file, then repeats launch/snapshot/close. This narrowly allows user
namespaces for the selected executable while keeping Chrome's sandbox enabled.
Downloaded Chrome upgrades often change the versioned path: recheck and update
the profile attachment after upgrades. Never disable Chrome's sandbox or global
AppArmor restrictions. The helper does not apply privileged changes or bypass
harness/network sandbox policies; resolve an actual harness denial through the
operator's normal permission configuration.

## Validation

Run the focused offline checks from the AWL checkout:

```sh
python3.12 -m unittest discover -s tests -p 'test_browser.py' -v
```

They use fake browser commands and disposable Git worktrees; they require no
network, Chrome, or privileges. For live acceptance after setup, ask Pi and Codex
in a worktree to open `about:blank`, take a snapshot, and close using their helper.
Repeat in a second worktree and with distinct instance IDs. Metadata-only session
checks do not establish Chrome launch or browser access under a harness sandbox.
