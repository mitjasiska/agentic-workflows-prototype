#!/usr/bin/env python3
"""Install the lazy browser helper and short global instruction pointers."""

import argparse
import os
from pathlib import Path
import shlex
import sys


ROOT = Path(__file__).resolve().parents[1]
BEGIN = b"<!-- AWL browser helper -->"
END = b"<!-- /AWL browser helper -->"


def user_path(value: str) -> Path:
    return Path(os.path.abspath(Path(value).expanduser()))


def regular_bytes(path: Path) -> bytes | None:
    if path.is_symlink() or (path.exists() and (not path.is_file() or path.stat().st_nlink != 1)):
        raise ValueError(f"Refusing non-regular file: {path}; integrate manually")
    return path.read_bytes() if path.exists() else None


def instruction_path(home: Path, agent: str) -> Path:
    names = (["AGENTS.override.md", "AGENTS.md"] if agent == "codex" else
             ["AGENTS.override.md", "AGENTS.md", "AGENTS.MD", "CLAUDE.md", "CLAUDE.MD"])
    for name in names:
        path = home / name
        content = regular_bytes(path)
        if content is not None and (agent == "pi" or content.strip()):
            return path
    return home / "AGENTS.md"


def instruction_addition(path: Path, helper: Path, guide: Path, agent: str) -> bytes:
    block = (BEGIN + b"\n" +
             f"For web tasks only, read the browser guide at {guide}.\n"
             f"Use {shlex.quote(str(helper))} {agent} COMMAND [ARGS...] from the task worktree; "
             "it selects an explicit session. Do not start a browser for non-web tasks.\n"
             "Use snapshots, semantic/DOM queries, console and network diagnostics by default; "
             "screenshots are opt-in for visual/layout checks.\n".encode() + END + b"\n")
    content = regular_bytes(path) or b""
    if BEGIN in content or END in content:
        if content.count(BEGIN) != 1 or content.count(END) != 1 or block not in content:
            raise ValueError(f"Existing browser instruction block differs: {path}; review manually")
        return b""
    return (b"\n\n" if content else b"") + block


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bin-dir", default="~/.local/bin", help="user command directory")
    parser.add_argument("--data-dir", default="~/.local/share/awl-browser", help="installed guide directory")
    parser.add_argument("--codex-home", default=os.environ.get("CODEX_HOME") or "~/.codex")
    parser.add_argument("--pi-home", default=os.environ.get("PI_CODING_AGENT_DIR") or "~/.pi/agent")
    args = parser.parse_args()
    bin_dir, data_dir = user_path(args.bin_dir), user_path(args.data_dir)
    helper, guide = bin_dir / "ab-worktree", data_dir / "browser.md"
    try:
        # Preflight all conflicts before writing anything. Existing files are
        # never replaced, including older/different versions of our own files.
        copies = [(helper, (ROOT / "scripts/ab-worktree").read_bytes(), 0o755),
                  (guide, (ROOT / "docs/browser.md").read_bytes(), 0o644)]
        for path, content, mode in copies:
            previous = regular_bytes(path)
            if previous is not None and previous != content:
                raise ValueError(f"Preserving different existing file: {path}; review before reinstalling")
            if previous is not None and path == helper and not os.access(path, os.X_OK):
                raise ValueError(f"Existing helper is not executable: {path}; review its permissions")
        instructions = []
        for agent, home in [("codex", user_path(args.codex_home)), ("pi", user_path(args.pi_home))]:
            path = instruction_path(home, agent)
            instructions.append((path, instruction_addition(path, helper, guide, agent)))
        if instructions[0][0].resolve() == instructions[1][0].resolve():
            raise ValueError("Pi and Codex instruction destinations must differ")
        for path, content, mode in copies:
            if path.exists():
                print(f"Unchanged: {path}")
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open("xb") as output:
                    output.write(content)
                path.chmod(mode)
                print(f"Installed: {path}")
        for name in ("ab-pi", "ab-codex"):
            path = bin_dir / name
            if os.path.lexists(path):
                print(f"Preserved shortcut: {path}; use {helper} {name[3:]} for the managed helper")
            else:
                path.symlink_to(helper)
                print(f"Installed shortcut: {path}")
        for path, addition in instructions:
            if addition:
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open("ab") as output:
                    output.write(addition)
                print(f"Appended browser pointer: {path}")
            else:
                print(f"Unchanged instructions: {path}")
        print(f"Put {bin_dir} on your shell PATH once, then restart Pi/Codex. "
              "The instruction pointers also include the absolute helper path.")
        return 0
    except (OSError, ValueError) as error:
        print(f"install_browser: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
