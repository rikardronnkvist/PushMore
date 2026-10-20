"""GitHub webhook → Notification."""

from __future__ import annotations

import hashlib
import hmac
from typing import Any

from ..config import Source
from ..models import Notification

FAILED_CONCLUSIONS = {"failure", "timed_out", "startup_failure", "action_required"}


def verify_signature(secret: str, body: bytes, header: str | None) -> bool:
    if not header or not header.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header)


def _repo(p: dict[str, Any]) -> str:
    return (p.get("repository") or {}).get("full_name") or "github"


def _actor(p: dict[str, Any]) -> str:
    return (p.get("sender") or {}).get("login") or "someone"


def _first_line(text: str | None) -> str:
    return (text or "").strip().splitlines()[0] if (text or "").strip() else ""


def _push(p: dict[str, Any], src: Source) -> Notification | None:
    if p.get("deleted"):
        return None
    ref = (p.get("ref") or "").removeprefix("refs/heads/").removeprefix("refs/tags/")
    commits = p.get("commits") or []
    if not commits:
        if not p.get("created"):
            return None
        return Notification(
            title=f"{_repo(p)}: {ref} created",
            message=f"{_actor(p)} created {ref}",
            url=(p.get("repository") or {}).get("html_url"),
        )
    lines = [
        f"• {_first_line(c.get('message'))} ({(c.get('author') or {}).get('name', '?')})" for c in commits[:5]
    ]
    if len(commits) > 5:
        lines.append(f"… and {len(commits) - 5} more")
    noun = "commit" if len(commits) == 1 else "commits"
    return Notification(
        title=f"{_repo(p)}: {len(commits)} {noun} to {ref}",
        message="\n".join(lines),
        url=p.get("compare"),
        url_title="Compare",
    )


def _release(p: dict[str, Any], src: Source) -> Notification | None:
    if p.get("action") not in ("published", "released"):
        return None
    rel = p.get("release") or {}
    tag = rel.get("tag_name") or "?"
    kind = "Pre-release" if rel.get("prerelease") else "Release"
    body = _first_line(rel.get("name")) or tag
    return Notification(
        title=f"{_repo(p)}: {kind} {tag}",
        message=f"{body} by {_actor(p)}",
        url=rel.get("html_url"),
        url_title="Release notes",
    )


def _workflow_run(p: dict[str, Any], src: Source) -> Notification | None:
    if p.get("action") != "completed":
        return None
    run = p.get("workflow_run") or {}
    conclusion = run.get("conclusion") or "unknown"
    failed = conclusion in FAILED_CONCLUSIONS
    if src.only_failures and not failed:
        return None
    if conclusion in ("skipped", "neutral") and not failed:
        return None
    icon = "❌" if failed else ("✅" if conclusion == "success" else "⚠️")
    return Notification(
        title=f"{icon} {_repo(p)}: {run.get('name', 'workflow')} {conclusion}",
        message=f"{run.get('head_branch', '?')} @ {(run.get('head_sha') or '')[:7]}: "
        f"{_first_line((run.get('head_commit') or {}).get('message'))}",
        url=run.get("html_url"),
        url_title="Run",
        priority=1 if failed else None,
    )


def _pull_request(p: dict[str, Any], src: Source) -> Notification | None:
    action = p.get("action")
    pr = p.get("pull_request") or {}
    if action == "closed":
        action = "merged" if pr.get("merged") else "closed"
    if action not in ("opened", "reopened", "merged", "closed", "ready_for_review"):
        return None
    if src.only_failures:
        return None
    return Notification(
        title=f"{_repo(p)}: PR #{pr.get('number')} {action.replace('_', ' ')}",
        message=f"{pr.get('title', '')} by {_actor(p)}",
        url=pr.get("html_url"),
        url_title="Pull request",
    )


def _issues(p: dict[str, Any], src: Source) -> Notification | None:
    if p.get("action") not in ("opened", "reopened", "closed") or src.only_failures:
        return None
    issue = p.get("issue") or {}
    return Notification(
        title=f"{_repo(p)}: Issue #{issue.get('number')} {p['action']}",
        message=f"{issue.get('title', '')} by {_actor(p)}",
        url=issue.get("html_url"),
        url_title="Issue",
    )


def _star(p: dict[str, Any], src: Source) -> Notification | None:
    if p.get("action") != "created" or src.only_failures:
        return None
    count = (p.get("repository") or {}).get("stargazers_count")
    return Notification(
        title=f"{_repo(p)}: ⭐ new star",
        message=f"{_actor(p)} starred ({count} total)",
        url=(p.get("sender") or {}).get("html_url"),
        priority=-1,
    )


def _fork(p: dict[str, Any], src: Source) -> Notification | None:
    if src.only_failures:
        return None
    forkee = p.get("forkee") or {}
    return Notification(
        title=f"{_repo(p)}: forked",
        message=f"{_actor(p)} → {forkee.get('full_name', '?')}",
        url=forkee.get("html_url"),
        priority=-1,
    )


def _dependabot(p: dict[str, Any], src: Source) -> Notification | None:
    if p.get("action") not in ("created", "reopened"):
        return None
    alert = p.get("alert") or {}
    adv = alert.get("security_advisory") or {}
    severity = (adv.get("severity") or "unknown").lower()
    pkg = ((alert.get("dependency") or {}).get("package") or {}).get("name", "?")
    return Notification(
        title=f"{_repo(p)}: Dependabot {severity}",
        message=f"{pkg}: {adv.get('summary', '')}",
        url=alert.get("html_url"),
        url_title="Alert",
        priority=1 if severity in ("high", "critical") else None,
    )


HANDLERS = {
    "push": _push,
    "release": _release,
    "workflow_run": _workflow_run,
    "pull_request": _pull_request,
    "issues": _issues,
    "star": _star,
    "fork": _fork,
    "dependabot_alert": _dependabot,
}


def to_notification(event: str, payload: dict[str, Any], src: Source) -> Notification | None:
    if src.events is not None and event not in src.events:
        return None
    handler = HANDLERS.get(event)
    if handler is None:
        return None
    return handler(payload, src)
