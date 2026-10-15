#!/usr/bin/env python3
"""Guided Linux browser setup: prerequisites, safe helper upgrades, and PATH diagnostics."""

import argparse
import hashlib
import json
import shutil
import subprocess
import os
from pathlib import Path
import shlex
import sys

from ab_worktree_session import SUPPORTED_VERSION


ROOT = Path(__file__).resolve().parents[1]
BEGIN = b"<!-- AWL browser helper -->"
END = b"<!-- /AWL browser helper -->"


def user_path(value: str) -> Path:
    return Path(os.path.abspath(Path(value).expanduser()))


def regular_bytes(path: Path) -> bytes | None:
    if any(parent.is_symlink() for parent in path.parents):
        raise ValueError(f"Refusing symlinked destination directory: {path}; integrate manually")
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



def chrome_path():
    selected = os.environ.get("AGENT_BROWSER_EXECUTABLE_PATH")
    if selected:
        path = Path(selected).expanduser()
        if not path.is_file() or not os.access(path, os.X_OK):
            raise ValueError("AGENT_BROWSER_EXECUTABLE_PATH is not an executable file; correct it before setup")
        return path
    for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser"):
        if found := shutil.which(name):
            return Path(found)
    home = Path.home()
    for pattern in (".agent-browser/browsers/**/chrome", ".cache/agent-browser/**/chrome", ".cache/puppeteer/chrome/**/chrome",
                    ".cache/ms-playwright/chromium-*/chrome-*/chrome"):
        for path in sorted(home.glob(pattern), reverse=True):
            if path.is_file() and os.access(path, os.X_OK):
                return path
    return None


def consent(args, description):
    if args.install_deps:
        return True
    if not args.check and sys.stdin.isatty():
        return input(description + " [y/N] ").strip().lower() in {"y", "yes"}
    return False


def prerequisites(args, bin_dir):
    if sys.platform != "linux":
        raise ValueError("Only Linux is supported; no files were installed")
    for name in ("git", "sha256sum", "python3", "readlink"):
        if not shutil.which(name):
            raise ValueError(f"Required executable missing from controller PATH: {name}; install it using your host's normal package procedure")
    browser = shutil.which("agent-browser")
    if browser:
        version = subprocess.run([browser, "--version"], capture_output=True, text=True, timeout=10,
                                 env={k: v for k, v in os.environ.items() if not k.startswith("AGENT_BROWSER_")})
        if version.returncode or version.stdout.strip() != f"agent-browser {SUPPORTED_VERSION}":
            raise ValueError(f"Existing agent-browser preserved at {browser}; prepared sessions require version {SUPPORTED_VERSION}. Review/update that installation explicitly, then rerun setup")
        print(f"Reusing agent-browser {SUPPORTED_VERSION}: {browser}")
    else:
        target = bin_dir / "agent-browser"
        if os.path.lexists(target):
            raise ValueError(f"Preserving existing {target}; add its directory to controller PATH or reconcile it manually")
        if not consent(args, f"Install agent-browser {SUPPORTED_VERSION} in a private user npm prefix (network download)?"):
            raise ValueError("agent-browser is missing. Rerun with --install-deps to consent to user-level npm/Chrome downloads, or install it manually")
        if not shutil.which("node") or not shutil.which("npm"):
            raise ValueError("User installation needs Node.js 24+ and npm on controller PATH; install these prerequisites with your normal user-level Node setup, then rerun this command")
        version = subprocess.run(["node", "--version"], capture_output=True, text=True, check=True, timeout=10)
        if int(version.stdout.strip().lstrip("v").split(".")[0]) < 24:
            raise ValueError("Install Node.js 24+ before user-level agent-browser installation")
        prefix = bin_dir.parent / "share/awl-browser-npm"
        if prefix.exists():
            raise ValueError(f"Preserving existing private npm prefix: {prefix}; put its bin directory on PATH and rerun, or reconcile it manually")
        subprocess.run(["npm", "install", "--global", "--prefix", str(prefix), f"agent-browser@{SUPPORTED_VERSION}"], check=True, timeout=300)
        browser = str(prefix / "bin/agent-browser")
        bin_dir.mkdir(parents=True, exist_ok=True)
        target.symlink_to(browser)
    chrome = chrome_path()
    if chrome is None:
        if not consent(args, "Download Chrome for this user with agent-browser install (no system packages)?"):
            raise ValueError("Chrome is missing. Rerun with --install-deps to consent to its user-level download, or set AGENT_BROWSER_EXECUTABLE_PATH to an installed Chrome")
        subprocess.run([browser, "install"], check=True, timeout=300,
                       env={k: v for k, v in os.environ.items() if not k.startswith("AGENT_BROWSER_")})
        chrome = chrome_path()
        if chrome is None:
            raise ValueError("Chrome download did not produce a detected executable; set AGENT_BROWSER_EXECUTABLE_PATH and rerun setup")
    print(f"Chrome executable detected: {chrome} (launch is checked later by host --prepare)")
    if str(bin_dir) not in os.environ.get("PATH", "").split(os.pathsep):
        print(f"Controller PATH: add {bin_dir} and restart the controller; absolute helper invocation is also supported")
    if args.agent_path is not None:
        missing = [name for name in ("ab-worktree", "python3", "git", "sha256sum", "readlink")
                   if not shutil.which(name, path=args.agent_path)]
        if missing:
            raise ValueError("Agent PATH missing: " + ", ".join(missing) + "; add the installed helper and required tools, then restart that agent")
        print("Supplied agent PATH: required executables found; actual socket access still needs an agent-side snapshot")
    else:
        print("Agent PATH not inspected: after setup run this command with --check --agent-path PATH_FROM_AGENT_SHELL. Connect-only agents need no Node or Chrome installation")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bin-dir", default="~/.local/bin", help="user command directory")
    parser.add_argument("--data-dir", default="~/.local/share/awl-browser", help="installed guide directory")
    parser.add_argument("--codex-home", default=os.environ.get("CODEX_HOME") or "~/.codex")
    parser.add_argument("--pi-home", default=os.environ.get("PI_CODING_AGENT_DIR") or "~/.pi/agent")
    parser.add_argument("--install-deps", action="store_true", help="consent to missing user-level agent-browser/Chrome downloads; never installs system packages")
    parser.add_argument("--check", action="store_true", help="check prerequisites and installed files without changing anything")
    parser.add_argument("--agent-path", help="PATH obtained from the target agent shell, to check command visibility")
    parser.add_argument("--skip-instructions", action="store_true", help="install generic helper/guide without adding Codex/Pi instruction pointers")
    args = parser.parse_args()
    if args.check and args.install_deps:
        parser.error("--check cannot install dependencies")
    bin_dir, data_dir = user_path(args.bin_dir), user_path(args.data_dir)
    helper, guide = bin_dir / "ab-worktree", data_dir / "browser.md"
    manifest = data_dir / "install-manifest.json"
    try:
        old_manifest = regular_bytes(manifest)
        hashes = json.loads(old_manifest) if old_manifest is not None else {}
        if not isinstance(hashes, dict):
            raise ValueError("Invalid browser install manifest; inspect it manually")
        copies = [(helper, (ROOT / "scripts/ab-worktree").read_bytes(), 0o755),
                  (bin_dir / "ab_worktree_session.py", (ROOT / "scripts/ab_worktree_session.py").read_bytes(), 0o644),
                  (guide, (ROOT / "docs/browser.md").read_bytes(), 0o644)]
        # Recognize unchanged DEV-94 installations as well as later managed copies.
        legacy = {str(helper): "a29eca948fdac969b89bca5dc6638d9aab69337d9924547e994b20f2d1c5c87c",
                  str(guide): "c4ae350ae19a09159022cbc3c0c56a8bbf9a3e9e549c0e1ed3d8cf07624d2c03"}
        for path, content, mode in copies:
            previous = regular_bytes(path)
            if previous is not None and previous != content:
                digest = hashlib.sha256(previous).hexdigest()
                if digest not in {hashes.get(str(path)), legacy.get(str(path))}:
                    raise ValueError(f"Preserving different existing file: {path}; review before reinstalling")
            if previous is not None and path == helper and not os.access(path, os.X_OK):
                raise ValueError(f"Existing helper is not executable: {path}; review its permissions")
        instructions = []
        if not args.skip_instructions:
            for agent, home in [("codex", user_path(args.codex_home)), ("pi", user_path(args.pi_home))]:
                path = instruction_path(home, agent)
                instructions.append((path, instruction_addition(path, helper, guide, agent)))
            if instructions[0][0].resolve() == instructions[1][0].resolve():
                raise ValueError("Pi and Codex instruction destinations must differ")
        prerequisites(args, bin_dir)
        if args.check:
            if any(regular_bytes(path) != content for path, content, _ in copies):
                raise ValueError("Helper/guide are missing or need a managed update; rerun setup without --check")
            if any(addition for _, addition in instructions):
                raise ValueError("Browser instruction pointers are missing; rerun setup without --check")
            print("Setup preflight passed; no browser was launched")
            return 0
        for path, content, mode in copies:
            if regular_bytes(path) == content:
                print(f"Unchanged: {path}")
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                # All destinations were checked before dependency installation/writes.
                with path.open("wb" if path.exists() else "xb") as output:
                    output.write(content)
                path.chmod(mode)
                print(f"Installed/updated: {path}")
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
        content = (json.dumps({str(path): hashlib.sha256(content).hexdigest() for path, content, _ in copies}, sort_keys=True) + "\n").encode()
        if old_manifest != content:
            manifest.write_bytes(content)
        print("Setup complete; no browser was launched. For an opted-in task run awp start/loop/review ISSUE --browser from the host; see the installed guide")
        return 0
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        print(f"install_browser: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
