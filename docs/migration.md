# Python AWP operator migration

[Setup](../README.md#setup) · [Configuration](configuration.md) · [Lifecycle](lifecycle.md)

This runbook prepares the Python repository for **Agentic Workflows Prototype
(AWP)**. It does not authorize GitHub administration, a live checkout move,
installation changes, service stops, or publication. Run the operator stages in
a normal Aquila terminal, outside any managed task checkout, after independent
review and explicit approval of the migration window. Record approval, commands,
results, timestamps, and tested revision privately; never paste credentials or
populated configuration into a PR. Stop at any failed or uncertain check.

## Identities and retained contracts

| Resource | Python destination / decision |
| --- | --- |
| GitHub repository | `mitjasiska/agentic-workflows-prototype`, renamed from `mitjasiska/agentic-workflows-lite` |
| Permanent checkout | `~/projects/agentic-workflows-prototype` |
| Installed command | `awp`; no Python `awl` compatibility alias |
| Distribution, imports, source launcher | Keep `agentic-workflows-lite`, `task_start`, and `./task` |
| Global and per-project state/configuration | Keep `.agentic-workflows-lite`, including contexts, metrics, and integrations |
| Linear targets | Keep existing exact project/team names and IDs; change only the applicable `repo_name` |
| Skill/plugin identifiers | Keep `create-linear-task` and marketplace ID `agentic-workflows-lite` |
| Existing optional integrations | Keep `AWL_BROWSER_*`, browser session/record names and `awl-browser` paths, `awl-dashboard.service`, and named Codex profiles |

The future .NET AWL (development codename AWLN) may later own the old GitHub name,
`~/projects/agentic-workflows-lite`, and `awl`. It must use separate runtime state
and a separate dashboard port; Python keeps its current default, 8765. Creating
that repository, reserving its runtime, or reusing the old names is a separately
authorized task. Leave the unrelated `agentic-workflows-e2e` repository alone.

**Redirects are temporary protection, not migration completion.** Reusing the old
repository name disables its redirects to Python. Hosted action calls also do
not receive the usual rename redirects; update callers explicitly. Check Pages
URLs separately if enabled. See [GitHub's rename rules](https://docs.github.com/en/repositories/creating-and-managing-repositories/renaming-a-repository).

## Preparation evidence and outstanding inventory

Read-only preparation on **2026-10-15**, based on `4cee788`:

- Public GitHub API identified `mitjasiska/agentic-workflows-lite`, repository ID
  **1383422189**, default branch `main`. The destination returned unauthenticated
  HTTP 404. This does **not** establish availability: a private repository or
  account restriction may be invisible. The owner must verify availability in
  repository Settings immediately before rename; do not create a placeholder.
- Public API inventory returned 29 branches, no tags, no open PRs, no releases,
  two active workflows, and no repository rulesets, with no additional pages.
  These are dated observations, not a claim about all protection/settings or the
  future migration window. Re-inventory before approval. Sources:
  [repository](https://api.github.com/repos/mitjasiska/agentic-workflows-lite),
  [branches](https://api.github.com/repos/mitjasiska/agentic-workflows-lite/branches?per_page=100),
  [tags](https://api.github.com/repos/mitjasiska/agentic-workflows-lite/tags?per_page=100),
  [open PRs](https://api.github.com/repos/mitjasiska/agentic-workflows-lite/pulls?state=open&per_page=100),
  [releases](https://api.github.com/repos/mitjasiska/agentic-workflows-lite/releases?per_page=100),
  [workflows](https://api.github.com/repos/mitjasiska/agentic-workflows-lite/actions/workflows),
  [rulesets](https://api.github.com/repos/mitjasiska/agentic-workflows-lite/rulesets).
- Git listed the clean permanent checkout on `main` and one linked DEV-114
  implementation worktree at the same base revision. Its common directory is
  the old permanent checkout's `.git`. **This active task blocks a live move.**
  Finish independent review, publication, human merge, and normal task cleanup
  at the old paths before starting the migration window. Fetch and push URLs each
  resolved to the expected old Python repository; neither was changed.
- Bounded read-only `herdr worktree list --cwd` and `herdr workspace list` calls
  succeeded: two worktree entries and three workspaces. No panes were opened,
  closed, or inspected for provider/process inactivity. Counts alone do not
  establish migration readiness; the operator must inspect exact dependencies.
- Prior DEV-67 branding and DEV-82 layered/private mapping implementations were
  inspected through local history and their current code. Their completed
  namespace/configuration work is retained; Linear was not contacted.
- Both checked-in Actions workflows use the current repository dynamically;
  neither contains an old repository URL or reusable-workflow caller. PR checks
  and nightly regression remain unchanged. Public metadata, installer URLs,
  Codex repository selectors, canonical skill, and plugin URLs are audited in
  this change. Synthetic project examples remain synthetic.
- Private registries, installed skill mappings, machine configuration,
  credentials, live controllers, Herdr panes, installed launchers, GitHub admin
  settings, and external integrations were **not inspected or migrated** by the
  implementation agent. Complete the operator inventory below. No claim of
  live acceptance is made by offline tests.

The repository marketplace displays **Agentic Workflows Prototype**. Its stable
identifier `agentic-workflows-lite` and plugin identifier `create-linear-task`
remain unchanged. This metadata change is part of completed source preparation;
external marketplace imports still require operator verification after rename.

## 1. Inventory and dependency map

Use Bash. Keep the evidence directory outside Git, with private permissions.
Commands in later sections reuse these variables. Do not enable shell tracing.

```sh
umask 077
awp_old="$HOME/projects/agentic-workflows-lite"
awp_new="$HOME/projects/agentic-workflows-prototype"
awp_from=mitjasiska/agentic-workflows-lite
awp_to=mitjasiska/agentic-workflows-prototype
awp_evidence=$(mktemp -d "$HOME/awp-migration.XXXXXXXX")
git --version > "$awp_evidence/versions.txt"
uv --version >> "$awp_evidence/versions.txt"
gh --version >> "$awp_evidence/versions.txt"
gh api --hostname github.com "repos/$awp_from" --jq '{id,full_name,default_branch,visibility,archived}' > "$awp_evidence/repository.before.json"
gh api --hostname github.com --paginate "repos/$awp_from/branches?per_page=100" > "$awp_evidence/branches.before.json"
gh api --hostname github.com --paginate "repos/$awp_from/tags?per_page=100" > "$awp_evidence/tags.before.json"
gh api --hostname github.com --paginate "repos/$awp_from/pulls?state=all&per_page=100" --jq '.[] | {number,state,head:{ref:.head.ref,sha:.head.sha},base:{ref:.base.ref},html_url}' > "$awp_evidence/pulls.before.json"
gh api --hostname github.com --paginate "repos/$awp_from/issues?state=all&per_page=100" --jq '.[] | {number,state}' > "$awp_evidence/issues.before.json"
gh api --hostname github.com --paginate "repos/$awp_from/releases?per_page=100" --jq '.[] | {id,tag_name,draft,prerelease,assets:[.assets[] | {id,name,size}]}' > "$awp_evidence/releases.before.json"
gh api --hostname github.com "repos/$awp_from/actions/workflows" > "$awp_evidence/workflows.before.json"
gh api --hostname github.com "repos/$awp_from/rulesets" > "$awp_evidence/rulesets.before.json"
git -C "$awp_old" worktree list --porcelain > "$awp_evidence/worktrees.before.txt"
git -C "$awp_old" for-each-ref --format='%(refname) %(objectname)' > "$awp_evidence/refs.before.txt"
git -C "$awp_old" status --porcelain=v1 --untracked-files=all > "$awp_evidence/status.before.txt"
uv tool list > "$awp_evidence/tools.before.txt"
uv tool dir > "$awp_evidence/tool-dir.txt"
uv tool dir --bin > "$awp_evidence/tool-bin.txt"
type -a awl awp > "$awp_evidence/resolution.before.txt" 2>&1
herdr worktree list --cwd "$awp_old" > "$awp_evidence/herdr-worktrees.before.json"
herdr workspace list > "$awp_evidence/herdr-workspaces.before.json"
```

Inspect successful results privately; any authorization error leaves that item
pending, never empty. Enumerate branch protection/ruleset details, required check
names, default branch, Actions permissions/environments/secrets **names only**,
webhooks (IDs/events/active state, not secrets), GitHub Apps, deploy keys, Pages,
package/release consumers, badges, external reusable workflows/actions, marketplace
imports, and Linear/project repository links in Settings. Record owners and
required follow-up per integration. Do not print secret values or dump credentials.
Check organization/private references with their owners; a public search is not a
complete inbound-reference inventory. Verify all active PR head/base refs and tags
are accounted for and note in-flight CI runs.

For **every** listed checkout, record canonical top-level, branch, absolute Git
directory and common directory, dirty/ignored files, and unfinished Git operations.
`git -C PATH rev-parse --path-format=absolute --show-toplevel --absolute-git-dir
--git-common-dir` and `git -C PATH status --porcelain=v1 --untracked-files=all`
provide the Git evidence. Do not read uncommitted content into public logs.
In Herdr, inspect each affected workspace/pane and its original provider session;
use `herdr agent get EXACT_PANE` for the selected pane. Map controllers, agent
children, terminals, editor cwd, filesystem watchers, cron jobs, shell functions,
aliases, symlinks, user services, and Tailscale routes to the source path they use.
Use local process inspection without dumping process environments or secrets.
A quiet pane, `status` exit code zero, or missing process match alone is not proof
that the checkout is unused.

Review these dependencies privately (record existence and disposition, not values):

| Consumer | Inspection and required change |
| --- | --- |
| Git/uv ordinary installation | `uv tool list`, tool environment `direct_url.json` and entry-point metadata; replace old Git URL and pin the reviewed revision |
| Editable installation | Resolve Python module source and editable URL; reinstall after the permanent directory moves |
| Project registry | Source/editable: permanent `config/projects.toml`; ordinary: `~/.agentic-workflows-lite/projects.toml`. Change only Python's `repo_name`, retain exact Linear targets/base/other mappings |
| Machine settings | Keep `projects_root` if the parent remains `~/projects`; adjust only if actually necessary. Rename Python's exact `codex.repositories` selector, retain its profile |
| Create Linear Task | Source is canonical `skills/create-linear-task`; locate every installed standalone/cloud snapshot and its authoritative private registry |
| Absolute-path consumers | Update service `WorkingDirectory`/`ExecStart`, hooks, shortcuts, scripts, Codex trust/path entries and installer sources only where they refer to Python |
| Git identity | Inspect fetch/push URLs, `pushurl`, upstreams, URL rewrites, conditional includes, worktree config, and `gh repo set-default --view` in each checkout |

Do not read or edit generated mappings in place. Do not change unrelated `awl`
executables or global Git/Herdr/Codex policies. Existing named permission profiles
and the optional `awl-dashboard.service` unit need no identifier rename, but its
old checkout paths need correction.

## 2. Review, quiesce, and approve the window

Merge the reviewed repository preparation through the normal human
workflow: independent approval and passing fast PR checks. Verify the migration
revision contains the `awp` entry point; it still supports `./task` at the old
path. Record the exact revision and last known working installation source for
rollback. Full regression and relevant live acceptance gate the final release
candidate, as specified in [validation guidance](lifecycle.md#validation-guidance).

Before advancing an **editable** permanent source checkout to that revision,
finish controllers/agents loading it and arrange the approved metadata reinstall.
The old `awl` launcher imports the removed entry function and stops working as
soon as the editable code advances; Git updates do not regenerate launchers.
Use the checkout's `./task` for any remaining review/cleanup until reinstall, or
reinstall the reviewed editable metadata at the old path if the move is deferred.
Do not add an alias. Ordinary installed copies keep their old code until replaced.

Finish affected tasks and resolve uncommitted work with its owner. Complete
publication/merge/normal cleanup of Python-repository tasks before the path or
remote changes: their context, review, loop, and publication evidence binds exact
paths and repository identity. A paused loop is **not** migratable. Preserve
historical evidence; never rewrite SQLite/checkpoint identities or Git worktree
administrative files. Retired history retains its original paths for inspection;
it is not retargeted or eligible for automatic replay at the new path.
Other repositories' workspaces stay intact; wait for any
controllers/agents loading the Python source to exit before moving that source.

This runbook's normal path requires **only the permanent worktree remains**, no
active Python-repository bindings or unfinished publication, clean base, and no
unresolved absolute-path consumers. If any worktree or task must remain active,
postpone the move and name its owner and next action. Do not use forced cleanup,
worktree pruning, reset, or symlinks to make the checks pass.

After approval, stop only inventoried AWP controllers/services and confirm they
cannot auto-restart during migration. Make a verified private backup of the
permanent checkout including `.git`, ignored files, relevant user configuration,
installed skill config, and `~/.agentic-workflows-lite/` after writers stop. A Git
bundle alone omits uncommitted/configuration/history files. For example:

```sh
tar -C "$HOME/projects" -cpf "$awp_evidence/checkout.tar" agentic-workflows-lite
tar -C "$HOME" -cpf "$awp_evidence/runtime.tar" .agentic-workflows-lite
tar -tf "$awp_evidence/checkout.tar" > "$awp_evidence/checkout-files.txt"
tar -tf "$awp_evidence/runtime.tar" > "$awp_evidence/runtime-files.txt"
git -C "$awp_old" rev-parse HEAD > "$awp_evidence/migration-revision.txt"
```

Back up the other inventoried files separately, preserving permissions, and
verify restoration into a private temporary location before proceeding. Never
restore an entire old state database over later work.

## 3. Approved GitHub rename

**Authorization gate:** operator approves the exact repository ID, destination,
inventory, backups, and rollback plan. Confirm source ID **1383422189** and
destination availability in the owner's GitHub Settings immediately beforehand.
Abort if either differs or cannot be established. Rename the existing repository;
do not create/copy a new one or transfer ownership.

```sh
(
set -eu
test "$(gh api --hostname github.com "repos/$awp_from" --jq '.id')" = 1383422189
gh api --hostname github.com --method PATCH "repos/$awp_from" -f name=agentic-workflows-prototype --jq '{id,full_name,default_branch}'
test "$(gh api --hostname github.com "repos/$awp_to" --jq '.id')" = 1383422189
test "$(gh api --hostname github.com "repos/$awp_to" --jq '.full_name')" = "$awp_to"
)
```

Repeat the inventory API reads against `$awp_to` into `*.after.json`. Compare
branch/tag SHAs, issue/PR numbers and states, release/asset IDs, default branch,
rules/protection and integration settings. Expected URL changes are not losses.
Verify active PRs in the renamed repository. If preservation checks fail, stop
before local changes and investigate or use the approved rename-back procedure.

## 4. Approved local move and remotes

**Authorization gate:** operator reconfirms no active dependencies and authorizes
the checkout move, private mapping edits, and installation replacement. Work from
`$HOME`, not inside either checkout. Recheck the worktree inventory and backups.
These checks deliberately refuse a destination, including a dangling symlink.

```sh
(
set -euo pipefail
cd "$HOME"
test -d "$awp_old/.git"
test ! -L "$awp_old"
test ! -e "$awp_new"
test ! -L "$awp_new"
awp_move_worktree_count=$(git -C "$awp_old" worktree list --porcelain | rg -c '^worktree ')
test "$awp_move_worktree_count" = 1
awp_move_status=$(git -C "$awp_old" status --porcelain=v1 --untracked-files=all)
test -z "$awp_move_status"
test "$(git -C "$awp_old" symbolic-ref --short HEAD)" = main
test "$(git -C "$awp_old" rev-parse HEAD)" = "$(cat "$awp_evidence/migration-revision.txt")"
for marker in MERGE_HEAD CHERRY_PICK_HEAD REVERT_HEAD rebase-merge rebase-apply sequencer BISECT_LOG; do
  test ! -e "$awp_old/.git/$marker"
done
mv -T --no-clobber -- "$awp_old" "$awp_new"
test ! -e "$awp_old"
test ! -L "$awp_old"
test -d "$awp_new/.git"
test ! -L "$awp_new"
git -C "$awp_new" worktree repair
git -C "$awp_new" worktree list --porcelain
git -C "$awp_new" rev-parse --path-format=absolute --show-toplevel --git-common-dir
git -C "$awp_new" status --short
)
```

Run this block directly, never as an `if` condition or within an `&&`/`||` list,
which can disable Bash's `errexit`. Every guard is enforced separately. A failed
Git inventory command also aborts; empty output is not evidence of a clean
checkout. The post-move guards refuse to inspect or repair the destination if
`mv --no-clobber` returns success while leaving the source in place, including
when another destination appears during the move. Stop the runbook if the block
fails; do not continue to the remote commands below. `mv` is for the
**main** checkout: `git worktree move` cannot
move it. Supported [`git worktree repair`](https://git-scm.com/docs/git-worktree)
repairs Git links after a main-directory move. It does not repair workflow context
identities. Never edit `.git/worktrees/*`, linked `.git` files, or pinned records
manually. Retained Git-only worktrees require a separately reviewed plan; this
normal path refuses them even if they are idle.

Set the canonical remote using the existing authentication transport. Example
for an inventoried SSH `origin` with one fetch URL and no separate push URL:

```sh
git -C "$awp_new" remote set-url origin git@github.com:mitjasiska/agentic-workflows-prototype.git
git -C "$awp_new" remote get-url --all origin
git -C "$awp_new" remote get-url --push --all origin
cd "$awp_new"
gh repo set-default "$awp_to"
gh repo set-default --view
gh repo view --json nameWithOwner --jq .nameWithOwner
git ls-remote --heads --tags origin > "$awp_evidence/remote-refs.after.txt"
```

If an explicit `pushurl` exists, replace that exact URL with `git remote set-url
--push origin NEW_URL` only after verifying it is the single intended destination.
Use the HTTPS equivalent when applicable. Resolve duplicate URLs, URL rewrites,
conditional includes or non-`origin` upstreams explicitly; changing one stored URL
does not prove effective push identity. Inspect every remaining clone/consumer.
Both resolved transports and `gh` must target the prototype, with the original
GitHub numeric ID. Do not push through the old redirect.

Update the applicable private registry from the inventory, retaining its exact
Linear target and other fields. Change Python's `repo_name` to
`agentic-workflows-prototype`; keep `projects_root` if unchanged. Update the exact
Codex selector and identified path consumers. Preserve private file permissions.
No blanket replacement over the home directory is safe.

## 5. Replace the Python installation and refresh the skill

Before uninstalling, verify `uv tool list`, the recorded tool environment's
`agentic-workflows-lite` metadata/`direct_url.json`, and every resolved launcher.
Use `type -a awl awp`, `command -v`, `readlink -f` for symlinks and private local
inspection of regular scripts. The Python distribution's old entry-point metadata
and matching target/shebang establish ownership, not the filename alone. Record
the exact paths and hashes privately. An unknown, changed, or unrelated launcher
is a blocker to deleting that path; ask its owner. Never use `uv --force` to
overwrite it. If uv's recorded `awl` path was replaced by an unrelated executable,
stop before uninstall too; do not let old installer metadata delete the replacement.

For the verified Python-owned installation:

```sh
uv tool uninstall agentic-workflows-lite
hash -r
type -a awl awp
```

Missing commands at this boundary are expected. `"$awp_new/task" --help` remains
available even if reinstall fails. If a stale shim remains, remove only the exact
previously verified Python-owned file after rechecking it has not changed. Record
its bytes/target before using `unlink -- EXACT_VERIFIED_PATH`; never wildcard
delete or delete whatever `command -v awl` happens to return now. If ownership is
uncertain, leave it and stop acceptance. Inspect aliases, functions, shell hashes,
alternative pip/pipx environments and other users separately with their owners.

Choose the **same installation form** as before (do not run both):

```sh
# Ordinary Git-source install: pin the reviewed merged commit recorded above.
awp_revision=$(cat "$awp_evidence/migration-revision.txt")
uv tool install --python 3.12 "git+https://github.com/$awp_to.git@$awp_revision"

# OR editable install, after verifying the moved checkout is that revision:
uv tool install --python 3.12 --editable "$awp_new"
```

The [uv tool commands](https://docs.astral.sh/uv/reference/cli/#uv-tool-install)
operate on distribution `agentic-workflows-lite`, which now publishes only `awp`.
Do not install a second Python distribution or add an `awl` alias. Upgrade an
ordinary pinned installation by choosing a new reviewed revision explicitly.

```sh
hash -r
uv tool list
type -a awp awl
command -v awp
readlink -f "$(command -v awp)"
awp --help
"$awp_new/task" --help
```

Expected: tool list names `awp` only for Python; module/entry-point metadata names
`task_start.cli:awp` in the intended environment. There is no Python-owned `awl`
anywhere inventoried. An unrelated `awl` may remain with identical content and
ownership. Validate a fresh login shell too. Ordinary installs resolve their
registry in the private home; editable installs resolve the moved permanent
checkout, independent of the caller's directory.

Verify the environment directly, without loading credentials or printing mappings:

```sh
awp_tools=$(uv tool dir)
"$awp_tools/agentic-workflows-lite/bin/python" -I - <<'PY'
from importlib.metadata import distribution
from pathlib import Path
from task_start import cli, config
dist = distribution('agentic-workflows-lite')
entries = {e.name: e.value for e in dist.entry_points if e.group == 'console_scripts'}
assert entries == {'awp': 'task_start.cli:awp'}
assert not hasattr(cli, 'awl')
print('Python source:', Path(cli.__file__).resolve())
print('Registry location:', config.default_project_registry())
project = config.resolve_project(config.load_projects(), 'Agentic Workflows')
assert project.repo_name == 'agentic-workflows-prototype'
assert (config.status_projects_root() / project.repo_name).resolve() == Path.home() / 'projects/agentic-workflows-prototype'
print('Prototype checkout mapping verified; private values omitted')
PY
```

Confirm the recorded Linear team remains `Development Lab` in the authoritative
registry/skill comparison. Runtime project loading does not resolve team IDs or
prove live Linear access; do that through the operator's ordinary workflow.

Refresh installed skill files using the
[canonical refresh procedure](../skills/create-linear-task/SKILL.md#establish-context-and-target),
preserving its private `config.local.toml`. Run synchronization from the new source
checkout, choosing the authoritative registry for the actual installation form:

```sh
awp_skill="${CODEX_HOME:-$HOME/.codex}/skills/create-linear-task"
awp_registry="$awp_new/config/projects.toml" # editable/source mode
# For ordinary installs instead: awp_registry="$HOME/.agentic-workflows-lite/projects.toml"
python3.12 skills/create-linear-task/scripts/sync_config.py --private \
  --projects "$awp_registry" --output "$awp_skill/config.local.toml"
python3.12 skills/create-linear-task/scripts/sync_config.py --private \
  --projects "$awp_registry" --output "$awp_skill/config.local.toml" --check
```

Use the real installed destination, outside Git. Verify the selected repository
resolves to the unchanged exact Linear project/team without printing mappings or
creating an issue. Restart skill-host sessions after refresh; cloud imports need
their own accessible private snapshot and canonical repository URL. Follow the
[plugin's synchronization and acceptance instructions](../plugins/create-linear-task/README.md#synchronize-validate-and-package).

## 6. Acceptance and release of old names

Keep scheduling paused until every required row has evidence. Record `passed`,
`failed`, or `pending` with command, revision, and reason; mocked/offline tests do
not establish live acceptance.

| Check | Required evidence |
| --- | --- |
| GitHub preservation | Same numeric repository ID; default branch, branch/tag SHAs, issue/PR/release identities and settings compared with inventory |
| Exact destination | Canonical fetch and push URLs; `gh` default/view; active PR URLs and heads/bases point to Python |
| Installation | `uv tool list`, executable and module paths, ordinary/editable registry resolution, `awp --help`, `./task --help`, and absence of Python-owned `awl`; unrelated launchers unchanged |
| Mapping and skill | Runtime and generated installed snapshot select new checkout with identical Linear targets; private sync `--check` passes |
| Workspace/status | `awp contexts --all`, `awp status --all --json` from outside the source checkout; inspect discovery state and exact paths, not just exit code |
| Representative lifecycle | Operator-selected real task through start/review/publication and normal cleanup; correct new permanent checkout/common-dir, Herdr ownership, and preserved unrelated workspaces |
| Push and PR | Before `awp pr ISSUE`, verify new task checkout's effective fetch/push URL and GitHub ID. After authorized publication verify PR URL/base repository is `$awp_to`, head SHA equals the pushed branch, and original PR visibility is preserved |
| Integrations | Fast PR checks run on the canonical repo; rules/check names and external callers work. Marketplace, hooks, badges and project links use new URL; no important consumer relies on old redirects |
| Dashboard/services | Restart only the approved Python service after updating paths; same private state, correct localhost port and Herdr discovery; unrelated services unchanged |

`awp pr` commits/pushes/creates a PR; it is **not** a read-only smoke check. Use
only an explicitly selected reviewed task and operator-authorized publication.
The generic runtime checks consistency of repository names; it cannot detect that
every stale remote now consistently targets a different repository under the
reused old name. The numeric-ID check and inbound-reference audit are therefore
release requirements. Do not rewrite retained publication evidence to bypass a
refusal after remote drift.

Only after acceptance and owner sign-off may a later separately authorized task
reuse the old GitHub name/checkout and `awl`. Until then, keep them unused. Record
unrun CI/release gates as pending: independent review and fast PR CI for merge;
nightly full regression; exact-candidate full regression plus live acceptance for
release. No .NET repository or runtime is created by this migration.

## Interruption and rollback

| Boundary / failure | Recovery before resuming |
| --- | --- |
| Before approval or while tasks remain active | Leave names, installs and paths unchanged. Complete/retire the named task through its normal workflow; unresolved dirty work or identity blocks the window |
| Source preparation merged, rename not done | Continue using the old checkout's `./task`; reinstall its reviewed metadata if `awp` is needed. Canonical Git-source URLs remain unavailable until rename |
| GitHub renamed, local path still old | Keep scheduling paused. Verify original numeric ID at new URL, then continue remote/path work. If rollback is approved and old name is still free, rename that same repository back via Settings, verify ID/refs/PRs, restore inventoried remotes and integration URLs |
| Checkout moved, repair/config incomplete | Do not start workflows. Locate the one directory containing the original `.git` and compare refs to backup. If only new exists, finish `git -C "$awp_new" worktree repair` and mapping/install steps, or move it back with `mv -T --no-clobber -- "$awp_new" "$awp_old"` only when old is absent (including symlinks). Enforce separate guards as in the forward move: require new absent and old to contain the restored `.git` before `git -C "$awp_old" worktree repair`; a successful `mv` exit alone is insufficient. If both/neither exist, stop for operator investigation; never merge directories or prune metadata |
| Uninstall done, reinstall fails or editable source stale | Use the verified checkout's `./task`. Restore the last working reviewed installation from the recorded source/revision using the unchanged distribution name. A rollback to Python `awl` is allowed only while that name is still exclusively Python-owned; otherwise repair `awp` without overwriting another tool |
| Private mapping or absolute path missed | Keep affected workflows stopped. Compare privately with backup; fix only identified fields/consumers, rerun private skill sync, verify registry and source paths. Never edit stored task identities; preserve old history for inspection |
| Wrong-origin push suspected | Stop publication immediately. Record exact local/remote SHAs and PR repository; repair local effective URLs and `gh` default. Notify the operator for reviewed recovery of the wrong destination; do not force-push/delete branches or PRs automatically |
| CI, hooks, Pages or external imports fail | Keep old names unreused, update exact callers/settings with their owners, rerun the affected checks. Preserve required checks; do not disable protections to make acceptance green |
| Old GitHub name already reused | Redirects and rename-back are unavailable. Verify original numeric ID at prototype URL, repair every stale reference and install source explicitly. Do not touch, rename or delete the .NET repository; any rollback now needs a separate owner decision |

For rollback after configuration changes, restore only this migration's changes
from private backups, reinstall for the restored source path, refresh generated
skill mappings, recheck remotes/identity and services, then repeat acceptance.
Preserve newly created work and all unrelated resources. Do not restore a whole
home directory, database, or Git backup over concurrent work.

## Execution record

At repository-preparation handoff: source edits, including the marketplace
display name, and isolated checks only. GitHub rename, owner availability
confirmation, active-session inventory, checkout move,
live install replacement, private mappings/skill refresh, integration changes,
live acceptance, and old-name release are **pending operator approval/execution**.
Independent review is pending; this document is a reviewable runbook, not
evidence of approval.
Append actual migration outcomes to a private operator record and link only a
sanitized result in the eventual handoff.
