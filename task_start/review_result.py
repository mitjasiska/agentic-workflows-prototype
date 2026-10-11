"""Validated verdict and workflow-owned provenance for review and loop adoption."""

from dataclasses import asdict, dataclass, field
import hashlib
import json
import re

from . import TaskError


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
                            "without a type prefix, issue suffix, or sentence punctuation; run task review again")
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


def parse_verdict(raw: str, pass_id: str, *, frozen_fingerprint: str | None = None, identifier=None,
                  routing: bool = False) -> dict:
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
            if (not isinstance(check, dict) or set(check) != {"name", "result", "details"}
                    or check["result"] not in {"passed", "failed", "not_run"}
                    or any(not isinstance(check[k], str) or not check[k].strip() for k in ("name", "details"))):
                raise ValueError("invalid check")
        if ((value["state"] == "clean" and (value["findings"] or
                any(c["result"] == "failed" for c in value["checks"])))
                or (value["state"] == "findings" and not value["findings"])):
            raise ValueError("inconsistent verdict")
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
