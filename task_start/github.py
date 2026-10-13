"""Conservative PR history checks, with optional local GitHub CLI authentication."""

import json
from dataclasses import dataclass
from http.client import HTTPException, InvalidURL
import os
import re
import socket
import ssl
import subprocess
from time import monotonic, sleep
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen

from . import TaskError


@dataclass(frozen=True)
class MergedPull:
    repository: str
    number: int
    head_commit: str
    merge_commit: str


def repository_name(remote: str) -> str | None:
    if remote.startswith("git@github.com:"):
        path = remote.removeprefix("git@github.com:")
    else:
        try:
            url = urlsplit(remote)
            if url.hostname != "github.com" or url.scheme not in {"https", "ssh"}:
                return None
            path = url.path.lstrip("/")
        except ValueError:
            return None
    path = path.removesuffix(".git")
    return path if re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", path) else None


def validate_credential(source, token):
    if not token or any(not 33 <= ord(char) <= 126 for char in token):
        raise TaskError(f"GitHub API credential {source} contains whitespace, control, or non-ASCII characters "
                        "or is empty; correct that credential without printing its value")
    return source, token


def cli_credential():
    """Read the active github.com credential privately; never prompt or persist it."""
    try:
        result = subprocess.run(
            ["gh", "auth", "token", "--hostname", "github.com"],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            env={**os.environ, "GH_PROMPT_DISABLED": "1"}, timeout=10, check=False)
    except FileNotFoundError:
        return None
    except (OSError, subprocess.TimeoutExpired):
        raise TaskError("GitHub CLI credential lookup could not complete; check gh auth status --hostname github.com "
                        "and unlock the local credential store before retrying") from None
    if result.returncode:
        return None
    try:
        token = result.stdout.decode("ascii")
        token = token[:-2] if token.endswith("\r\n") else token.removesuffix("\n")
    except UnicodeError:
        raise TaskError("GitHub CLI returned an invalid API credential; repair gh authentication without printing its token") from None
    return validate_credential("gh auth token", token)


def api_credential(*, required=False):
    """Explicit overrides retain precedence; otherwise use gh's local store."""
    for source in ("GH_TOKEN", "GITHUB_TOKEN"):
        token = os.environ.get(source)
        if token:
            return validate_credential(source, token)
    credential = cli_credential()
    if credential is not None:
        return credential
    if required:
        raise TaskError("GitHub API authentication is missing: use gh auth login --hostname github.com "
                        "or a command-scoped GH_TOKEN/GITHUB_TOKEN with repository Pull requests: write access; "
                        "Git SSH credentials are separate")
    return "unauthenticated", None


def http_failure(error, *, writing):
    """Classify bounded evidence; never echo response text, headers, or URLs."""
    permission = "Pull requests: write" if writing else "Pull requests: read"
    headers = error.headers or {}
    try:
        payload = json.loads(error.read(16384))
    except (OSError, HTTPException, ValueError):
        payload = None
    finally:
        error.close()
    message = payload.get("message") if isinstance(payload, dict) else None
    message = message if isinstance(message, str) else ""
    code = error.code if type(error.code) is int and 100 <= error.code <= 599 else "unknown"
    if code == 429 or (code in (403, 422) and (
            headers.get("X-RateLimit-Remaining") == "0" or headers.get("Retry-After")
            or message.startswith(("API rate limit exceeded", "You have exceeded a secondary rate limit",
                                   "You have triggered an abuse detection mechanism")))):
        detail = "rate limit exceeded; wait for the API limit to recover before retrying"
    elif code == 401:
        detail = "authentication rejected; check the selected token's validity, expiration, and revocation"
    elif code == 403:
        if str(headers.get("X-GitHub-SSO", "")).startswith("required"):
            detail = "SSO authorization required; authorize the selected token for the repository's organization"
        elif message in ("Resource not accessible by personal access token", "Resource not accessible by integration"):
            detail = f"insufficient token permissions; grant the selected token access to this repository and {permission}"
        else:
            detail = (f"forbidden; check the selected token's repository access, {permission}, "
                      "and organization or GitHub Actions restrictions")
    elif code == 404:
        detail = "repository or endpoint unavailable; verify repository identity and the selected token's repository access"
    elif code == 422:
        detail = "request validation failed; inspect the PR head/base, title/body, and commits between the branches"
        # GitHub validation messages may repeat private input. Only known field
        # names and error codes can cross the diagnostic boundary.
        errors = payload.get("errors") if isinstance(payload, dict) else None
        fields = set()
        for item in errors[:20] if isinstance(errors, list) else []:
            if not isinstance(item, dict):
                continue
            field, reason = item.get("field"), item.get("code")
            if (field in ("head", "base", "title", "body", "draft", "maintainer_can_modify")
                    and reason in ("missing", "missing_field", "invalid", "already_exists", "unprocessable")):
                fields.add(f"{field}: {reason}")
            text = item.get("message")
            if isinstance(text, str) and text.startswith("A pull request already exists"):
                detail = "a PR already exists for this head; discover and verify the matching PR on retry"
            elif isinstance(text, str) and text.startswith("No commits between"):
                detail = "no commits between the PR base and head; inspect the branch comparison"
        if fields:
            detail += " (" + ", ".join(sorted(fields)) + ")"
    elif code in (400, 405, 415):
        detail = "malformed or unsupported API request; inspect the request fields, method, content type, and API version"
    elif code == 409:
        detail = "repository state conflict; inspect the current head/base and PR state before retrying"
    elif isinstance(code, int) and code >= 500:
        detail = "GitHub service failure; check API availability before retrying"
    else:
        detail = "unexpected API response; inspect API availability, repository access, and request configuration"
    return f"HTTP {code}: {detail}"


def transport_failure(error):
    reason = error.reason if isinstance(error, URLError) else error
    if isinstance(reason, ssl.SSLCertVerificationError):
        return "TLS certificate verification failed; check the system CA trust and proxy configuration"
    if isinstance(reason, ssl.SSLError):
        return "TLS connection failed; check the network, proxy, and TLS configuration"
    if isinstance(reason, socket.gaierror):
        return "DNS lookup failed; check network/DNS access to api.github.com"
    if isinstance(reason, TimeoutError):
        return "API request timed out; check network/proxy access to api.github.com"
    if isinstance(reason, InvalidURL):
        return "invalid API request configuration; inspect the repository/endpoint parameters"
    return "API connection or response transport failed; check network/proxy access to api.github.com"


def request(path: str, branch: str, *, method: str = "GET", data: dict | None = None):
    source, token = api_credential(required=method != "GET")
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "agentic-workflows",
               "X-GitHub-Api-Version": "2022-11-28"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if data is not None:
        headers["Content-Type"] = "application/json"
    try:
        request = Request(f"https://api.github.com/repos/{path}", headers=headers, method=method,
                          data=json.dumps(data).encode() if data is not None else None)
    except (ValueError, TypeError):
        raise TaskError("Cannot prepare GitHub API request; inspect the repository/endpoint and JSON request fields") from None
    received = False
    try:
        with urlopen(request, timeout=30) as response:
            received = True
            return json.load(response)
    except HTTPError as error:
        cause = http_failure(error, writing=method != "GET")
    except (URLError, OSError, HTTPException) as error:
        cause = transport_failure(error)
    except ValueError:
        cause = ("malformed JSON API response; check GitHub/proxy response handling" if received else
                 "invalid API request encoding; inspect request and credential configuration")
    if method != "GET":
        raise TaskError(f"GitHub PR write was not confirmed ({source}): {cause}. "
                        "A write may already have reached GitHub; rerun task pr to discover and reuse "
                        "any matching PR before another write") from None
    authentication = ("Use gh auth login --hostname github.com or a command-scoped GH_TOKEN/GITHUB_TOKEN "
                      "with repository Pull requests: read access. " if token is None else "")
    raise TaskError(f"Cannot establish GitHub PR history for {branch} ({source}): {cause}. "
                    f"{authentication}Inspect its PR/merge state before retrying. Workspace left intact.") from None


class _StalePullHead(TaskError):
    """Both PR identities are valid; only the proven previous head is stale."""


def publication_pull(repo: str, branch: str, base: str, heads: set[str], *, expected_number=None,
                     previous_head=None, verify_published=None):
    if previous_head is None:
        return _publication_pull(repo, branch, base, heads, expected_number=expected_number)
    if expected_number is None or len(heads) != 1 or previous_head in heads or not callable(verify_published):
        raise TaskError("PR propagation checks require the recorded PR, frozen SHA and remote verification")
    # Retry observations only, never API failures or writes. Five observations
    # and a five-second window bound propagation retries independently of API
    # request timeouts. A response arriving after the window cannot authorize use.
    deadline, stale = None, None
    for attempt in range(5):
        if deadline is not None and monotonic() >= deadline:
            raise stale
        try:
            pull = _publication_pull(repo, branch, base, heads | {previous_head},
                                     expected_number=expected_number, stale_head=previous_head)
        except _StalePullHead as error:
            stale = error
            if deadline is None:
                deadline = monotonic() + 5
            # Native refs and local contents must still prove the frozen SHA
            # before tolerating even this one narrowly classified stale response.
            verify_published()
            remaining = deadline - monotonic()
            if attempt == 4 or remaining <= 0:
                raise
            sleep(min(1, remaining))
        else:
            if deadline is not None and monotonic() >= deadline:
                raise stale
            return pull


def _publication_pull(repo, branch, base, heads, *, expected_number, stale_head=None):
    """All history participates: a closed, different-base, or second PR conflicts."""
    pulls = list(pull_requests(repo, branch))
    if len(pulls) > 1:
        raise TaskError("Ambiguous PR history: multiple PRs use the task head; inspect them before retrying")
    if not pulls:
        if expected_number is not None:
            raise TaskError("Previously identified task PR is missing; inspect GitHub history before retrying; no replacement PR will be created")
        return None
    listed = pulls[0]
    if expected_number is not None and listed["number"] != expected_number:
        raise TaskError("Task PR identity changed; inspect the recorded PR number and GitHub history before retrying")
    pull = request(f"{repo}/pulls/{listed['number']}", branch)
    for item in (listed, pull):
        validate_pull(item, repo, branch)
        try:
            url = re.fullmatch(r"https://github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/pull/([1-9][0-9]*)",
                               item["html_url"])
            if (item["base"]["ref"] != base or item["head"]["sha"] not in heads
                    or item["state"] != "open" or item["merged_at"] is not None
                    or item.get("merged", False) is not False
                    or item["number"] != listed["number"]
                    or url is None or url[1].casefold() != repo.casefold()
                    or url[2] != str(item["number"])):
                raise ValueError("conflicting PR")
        except (KeyError, TypeError, ValueError):
            raise TaskError("Conflicting PR repository/head/base/state; inspect it before retrying task pr") from None
    if stale_head is not None and any(item["head"]["sha"] == stale_head for item in (listed, pull)):
        raise _StalePullHead("Conflicting PR repository/head/base/state; inspect it before retrying task pr")
    if pull["head"]["sha"] != listed["head"]["sha"]:
        raise TaskError("PR head changed during verification; retry after inspecting remote state")
    return pull


def publish_pull(repo: str, branch: str, base: str, head: str, title: str, body: str, *, verify_local, before_write,
                 expected_number=None, observe_pull=lambda pull: None, previous_head=None):
    pull = publication_pull(repo, branch, base, {head}, expected_number=expected_number,
                            previous_head=previous_head, verify_published=before_write)
    if pull is not None:
        observe_pull(pull)
        expected_number = pull["number"]
    # Lifecycle owns the frozen local SHA and reviewed-state checks. Recheck
    # after network lookups, immediately before any PR write and URL reporting.
    verify_local()
    if pull is None:
        # An uncertain POST is never replayed here. The next invocation enumerates
        # authoritative history first and reuses the unique matching result.
        before_write()
        created = request(f"{repo}/pulls", branch, method="POST",
                          data=dict(head=branch, base=base, title=title, body=body, draft=False))
        validate_pull(created, repo, branch)
        observe_pull(created)
        expected_number = created["number"]
    elif pull.get("title") != title or pull.get("body") != body:
        before_write()
        request(f"{repo}/pulls/{pull['number']}", branch, method="PATCH", data=dict(title=title, body=body))
    verified = publication_pull(repo, branch, base, {head}, expected_number=expected_number,
                                previous_head=previous_head, verify_published=before_write)
    if verified is None or verified.get("title") != title or verified.get("body") != body:
        raise TaskError("GitHub has not confirmed the expected PR metadata; rerun task pr to verify/reuse it")
    verify_local()
    observe_pull(verified)
    return verified["html_url"]


def validate_pull(pull: dict, repo: str, branch: str) -> None:
    try:
        if (pull["head"]["ref"] != branch
                or pull["head"]["repo"]["full_name"].casefold() != repo.casefold()
                or pull["base"]["repo"]["full_name"].casefold() != repo.casefold()
                or pull["state"] not in {"open", "closed"}
                or type(pull["number"]) is not int or pull["number"] < 1
                or (pull["merged_at"] is not None and not isinstance(pull["merged_at"], str))):
            raise ValueError("unexpected PR identity/state")
    except (KeyError, TypeError, ValueError, AttributeError):
        raise TaskError(f"Cannot establish PR history for {branch}: unexpected GitHub response; "
                        "inspect its PR/merge state manually") from None


def pull_requests(repo: str, branch: str):
    owner = repo.split("/")[0]
    # Query all states, not Git ancestry: squash merges do not preserve the head.
    # Generate each page URL ourselves rather than forwarding credentials to a Link URL.
    for page in range(1, 21):
        query = urlencode({"state": "all", "head": f"{owner}:{branch}",
                           "per_page": 100, "page": page})
        pulls = request(f"{repo}/pulls?{query}", branch)
        if not isinstance(pulls, list) or len(pulls) > 100:
            raise TaskError(f"Cannot establish PR history for {branch}: unexpected GitHub response; "
                            "inspect its PR/merge state manually")
        for pull in pulls:
            validate_pull(pull, repo, branch)
            yield pull
        if len(pulls) < 100:
            return
    raise TaskError(f"Cannot establish complete PR history for {branch}; inspect it manually")


def check_history(remote: str, branch: str, *, existing: bool) -> None:
    repo = repository_name(remote)
    if repo is None:
        if existing:
            raise TaskError(f"Cannot establish PR history for {branch}: the base remote is not github.com. "
                            "Inspect its PR/merge state and retire historical local work manually, "
                            "or choose a new --slice. No workspace was opened or deleted.")
        return
    for pull in pull_requests(repo, branch):
        if pull["merged_at"] is not None or pull["state"] == "closed":
            state = "merged" if pull["merged_at"] is not None else "closed"
            raise TaskError(f"Historical workspace {branch}: GitHub PR #{pull['number']} is {state}. "
                            "Inspect/retire it manually or choose a new --slice. "
                            "No branch or worktree was deleted.")


def merged_pull(remote: str, branch: str, base: str, head_commit: str) -> MergedPull:
    repo = repository_name(remote)
    if repo is None:
        raise TaskError("Cannot prove a squash/rebase merge: the base remote is not github.com")
    # Read all pages and all states: another PR for this head is ambiguous,
    # even if only one happens to claim a completed merge.
    pulls = list(pull_requests(repo, branch))
    if len(pulls) != 1:
        raise TaskError(f"Missing or ambiguous merge evidence for {branch}: expected exactly one GitHub PR, "
                        f"found {len(pulls)}")
    listed = pulls[0]
    pull = request(f"{repo}/pulls/{listed['number']}", branch)
    validate_pull(pull, repo, branch)
    try:
        if (pull["number"] != listed["number"] or pull["base"]["ref"] != base
                or pull["head"]["sha"] != head_commit
                or listed["base"]["ref"] != base or listed["head"]["sha"] != head_commit
                or any(pull[key] != listed[key] for key in ("state", "merged_at", "merge_commit_sha"))):
            raise ValueError("mismatched or changed PR")
        if pull["merged"] is not True or pull["state"] != "closed" or not pull["merged_at"]:
            raise TaskError(f"GitHub PR #{pull['number']} for {branch} is not confirmed merged")
        commit = pull["merge_commit_sha"]
        if not isinstance(commit, str) or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", commit):
            raise ValueError("invalid merge commit")
    except (KeyError, TypeError, ValueError):
        raise TaskError(f"GitHub merge evidence does not match the exact task head and base for {branch}; "
                        "workspace left intact") from None
    return MergedPull(repo, pull["number"], head_commit, commit)
