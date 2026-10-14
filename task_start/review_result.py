"""Validated verdict and workflow-owned provenance for review and loop adoption."""

from dataclasses import asdict, dataclass, field
import hashlib
import json
import re

from . import TaskError
from .command import command_name


def publication_summary_limit(identifier):
    # Reserve the longest supported type and the workflow-owned issue suffix.
    # The reviewer supplies prose only and never needs to choose that type.
    return 72 - len(f"refactor:  ({identifier})")


def publication_metadata(value, *, identifier=None):
    """Only public prose crosses the semantic boundary; never Git instructions."""
    if not isinstance(value, dict) or set(value) != {"summary", "description", "validation"}:
        raise TaskError("Review publication metadata requires summary, description, and validation")
    for key, limit in (("summary", 100), ("description", 2000), ("validation", 2000)):
        item = value[key]
        if (not isinstance(item, str) or not item.strip() or item != item.strip() or len(item) > limit
                or any(not c.isprintable() and c != "\n" for c in item)
                or any(line.lstrip().startswith("#") for line in item.splitlines())):
            raise TaskError("Invalid public review publication metadata")
    if "\n" in value["summary"]:
        raise TaskError("Publication summary must be a single line")
    if identifier is not None:
        summary = value["summary"]
        limit = publication_summary_limit(identifier)
        if (len(summary) > limit or re.fullmatch(r"[a-z]+(?:-[a-z]+)* .+", summary) is None
                or " ".join(summary.split()) != summary or summary.endswith((".", "!", "?"))
                or re.search(r"\([A-Z][A-Z0-9]*-[0-9]+\)$", summary)):
            raise TaskError(f"Publication summary must be a concise lower-case action phrase of at most {limit} characters, "
                            f"without a type prefix, issue suffix, or sentence punctuation; run {command_name()} review again")
    return value


def publication_fingerprint(value):
    """Identity of workflow-generated title/body, separately approved on rebase."""
    if (not isinstance(value, dict) or set(value) != {"title", "body"}
            or any(not isinstance(value[k], str) or not value[k].strip()
                   or any(not c.isprintable() and c != "\n" for c in value[k]) for k in value)
            or "\n" in value["title"]):
        raise TaskError("Missing or invalid frozen publication metadata; inspect rebase provenance before retrying")
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":")).encode()).hexdigest()


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def pending_review_checks(records):
    """Carry blocked validation and deferred merge checks as bounded review context."""
    pending = {}
    for record in records:
        if record["phase"] not in {"review", "rereview"}:
            continue
        unresolved = {c["name"] for c in record["checks"] if c["result"] != "passed"}
        required = {c["name"] for c in record["checks"] if c.get("scope") == "review" or c["result"] == "failed"}
        for check in record["checks"]:
            if check["result"] == "passed" and check["name"] not in unresolved:
                pending.pop(check["name"], None)
            elif check["result"] != "passed" and (record["state"] == "blocked" or check["name"] in pending
                    or record["state"] == "findings" and check["result"] == "failed"):
                # A later omission/change of scope cannot demote an explicitly
                # mandatory review check before it has independent passing evidence.
                previous = pending.get(check["name"], {})
                if check["name"] in required or previous.get("scope") == "review" or previous.get("result") == "failed":
                    check = dict(check, scope="review")
                pending[check["name"]] = check
    return list(pending.values())


def unresolved_review_checks(pending, checks, *, allow_failed=False):
    """Executed failures permit fixes, but only passing evidence permits clean review."""
    unresolved = []
    executed = {"passed", "failed"} if allow_failed else {"passed"}
    required = {c["name"]: c for c in pending}
    # A current review requirement is authoritative on this pass too. Include
    # passing declarations so conflicting evidence under the same name still blocks.
    required.update((c["name"], c) for c in checks if c.get("scope") == "review")
    for saved in required.values():
        current = [c for c in checks if c["name"] == saved["name"]]
        if (saved.get("scope") == "merge" and saved["result"] == "not_run"
                and not any(c.get("scope") == "review" for c in current)):
            continue
        # Legacy checkpoints had no scope. The independent reviewer may classify
        # an unrun legacy check as merge-time under the existing validation policy.
        if ("scope" not in saved and saved["result"] == "not_run" and current
                and all(c.get("scope") == "merge" and c["result"] == "not_run" for c in current)):
            continue
        if not current or any(c["result"] not in executed for c in current):
            unresolved.append(saved)
    return unresolved


def finding_signature(findings):
    value = sorted(({k: v for k, v in f.items() if k != "id"} for f in findings),
                   key=lambda f: json.dumps(f, sort_keys=True))
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=True).encode()).hexdigest()


def parse_verdict(raw: str, pass_id: str, *, frozen_fingerprint: str | None = None, identifier=None,
                  routing: bool = False, required_checks=()) -> dict:
    try:
        value = json.loads(raw, object_pairs_hook=unique_object)
        # Initial publishing requires public prose; a rebased continuation needs
        # explicit approval of frozen metadata instead. Review-only stays valid.
        required = {"pass_id", "state", "summary", "findings", "checks"}
        if (not isinstance(value, dict) or not required <= set(value)
                or set(value) - required - {"publication", "publication_approval"}
                or value["pass_id"] != pass_id
                or value["state"] not in {"clean", "findings", "blocked", "failed"}
                or not isinstance(value["summary"], str) or not value["summary"].strip()):
            raise ValueError("invalid verdict")
        if not isinstance(value["findings"], list) or not isinstance(value["checks"], list):
            raise ValueError("invalid lists")
        finding_ids = set()
        for finding in value["findings"]:
            fields = {"severity", "explanation", "evidence", "requirement"}
            if (not isinstance(finding, dict)
                    or set(finding) not in (fields, fields | {"id", "category"})
                    or finding["severity"] not in {"critical", "high", "medium", "low"}
                    or any(not isinstance(finding[k], str) or not finding[k].strip()
                           for k in ("explanation", "evidence"))
                    or not isinstance(finding["requirement"], str)):
                raise ValueError("invalid finding")
            if routing or "id" in finding:
                if (not isinstance(finding.get("id"), str)
                        or not re.fullmatch(r"F[1-9][0-9]{0,5}", finding["id"])
                        or finding["id"] in finding_ids
                        or finding.get("category") not in {"implementation", "human_decision"}):
                    raise ValueError("unroutable finding")
                finding_ids.add(finding["id"])
        for check in value["checks"]:
            if (not isinstance(check, dict) or set(check) not in
                    ({"name", "result", "details"}, {"name", "result", "details", "scope"})
                    or "scope" in check and check["scope"] not in {"review", "merge"}
                    or check["result"] not in {"passed", "failed", "not_run"}
                    or any(not isinstance(check[k], str) or not check[k].strip() for k in ("name", "details"))):
                raise ValueError("invalid check")
        if ((value["state"] == "clean" and (value["findings"] or
                any(c["result"] == "failed" for c in value["checks"])))
                or (value["state"] == "findings" and not value["findings"])):
            raise ValueError("inconsistent verdict")
        if value["state"] in {"clean", "findings"}:
            unresolved = unresolved_review_checks(required_checks, value["checks"],
                                                  allow_failed=value["state"] == "findings")
            if unresolved and value["state"] == "clean":
                raise TaskError("Clean review lacks passing evidence for required review checks; preserve the claim and inspect reviewer output")
            if unresolved:
                # A completed findings result is still recoverable evidence, but
                # cannot authorize another automatic fix while validation is blocked.
                value["state"] = "blocked"
                value["summary"] += "; Required review validation remains outstanding: " + ", ".join(
                    c["name"] for c in unresolved)
        if "publication" in value:
            # Frozen continuation validates existing metadata, never regenerates
            # it. Optional replacement prose must not apply a new title policy
            # retroactively to a previously accepted publishing contract.
            publication_metadata(value["publication"], identifier=identifier if frozen_fingerprint is None else None)
        if "publication_approval" in value and (frozen_fingerprint is None
                or value["publication_approval"] != frozen_fingerprint):
            raise TaskError("Review approval does not match the frozen publication metadata; a new review is required")
        if frozen_fingerprint is not None and value["state"] == "clean" and value.get("publication_approval") != frozen_fingerprint:
            raise TaskError("Clean review must explicitly approve the frozen publication metadata; a new review is required")
        return value
    except (ValueError, TypeError, KeyError, RecursionError):
        raise TaskError("Malformed, missing, or ambiguous reviewer output; review cannot be accepted as clean") from None


@dataclass(frozen=True)
class ReviewResult:
    state: str
    summary: str
    context_id: str | None
    pass_kind: str
    execution: dict
    review_state: dict
    pass_id: str
    findings: list = field(default_factory=list)
    checks: list = field(default_factory=list)
    invalidated: bool = False
    post_fingerprint: str | None = None
    version: int = 1

    def as_dict(self) -> dict:
        return asdict(self)

    def render(self) -> str:
        lines = [f"{self.context_id or 'Review'} ({self.pass_kind}): {self.state}", self.summary]
        for finding in self.findings:
            lines.append(f"[{finding['severity']}] {finding['explanation']} — {finding['evidence']}")
        for check in self.checks:
            lines.append(f"Check {check['name']}: {check['result']} — {check['details']}")
        if self.review_state:
            lines.append(f"Pinned base: {self.review_state['base_commit']}")
            lines.append(f"Review state: {self.review_state['fingerprint']}")
        return "\n".join("".join(c if c.isprintable() else "?" for c in line) for line in lines)
