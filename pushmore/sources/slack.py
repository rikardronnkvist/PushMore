"""Slack incoming-webhook format → Notification.

Handles ``text``, legacy ``attachments`` and the common Block Kit blocks
(header, section, context, rich_text is reduced to its plain text).
"""

from __future__ import annotations

from typing import Any

from ..config import Source
from ..models import Notification
from ..text import html_to_text, slack_mrkdwn_to_html

DANGER_COLORS = {"danger", "#ff0000", "#d00000", "#e01e5a", "#dc3545", "#f44336", "#a30200", "#e74c3c"}


def _text_of(obj: Any) -> str:
    if isinstance(obj, str):
        return obj
    if isinstance(obj, dict):
        if "text" in obj and isinstance(obj["text"], (str, dict)):
            return _text_of(obj["text"])
        if "elements" in obj:
            return "".join(_text_of(e) for e in obj["elements"])
    if isinstance(obj, list):
        return "".join(_text_of(e) for e in obj)
    return ""


def _blocks(blocks: list[dict[str, Any]]) -> tuple[str | None, list[str], str | None]:
    title, lines, url = None, [], None
    for block in blocks:
        btype = block.get("type")
        if btype == "header":
            title = title or _text_of(block.get("text"))
        elif btype == "section":
            if block.get("text"):
                lines.append(_text_of(block["text"]))
            for f in block.get("fields") or []:
                lines.append(_text_of(f))
            acc = block.get("accessory") or {}
            url = url or acc.get("url")
        elif btype in ("context", "rich_text"):
            text = " ".join(_text_of(e) for e in block.get("elements") or [])
            if text.strip():
                lines.append(text)
        elif btype == "actions":
            for el in block.get("elements") or []:
                url = url or el.get("url")
    return title, lines, url


def _attachments(atts: list[dict[str, Any]]) -> tuple[str | None, list[str], str | None, bool]:
    title, lines, url, danger = None, [], None, False
    for a in atts:
        if str(a.get("color", "")).lower() in DANGER_COLORS:
            danger = True
        if a.get("pretext"):
            lines.append(a["pretext"])
        if a.get("title"):
            title = title or a["title"]
            url = url or a.get("title_link")
        if a.get("text"):
            lines.append(a["text"])
        elif not a.get("blocks") and a.get("fallback") and not a.get("fields"):
            lines.append(a["fallback"])
        for f in a.get("fields") or []:
            if f.get("title") or f.get("value"):
                lines.append(f"*{f.get('title', '')}*: {f.get('value', '')}")
        if a.get("blocks"):
            _, blines, burl = _blocks(a["blocks"])
            lines.extend(blines)
            url = url or burl
        if a.get("footer"):
            lines.append(a["footer"])
    return title, lines, url, danger


def to_notification(payload: dict[str, Any], src: Source) -> Notification | None:
    title: str | None = None
    lines: list[str] = []
    url: str | None = None
    danger = False

    if payload.get("blocks"):
        title, lines, url = _blocks(payload["blocks"])
    if payload.get("text") and not lines:
        lines.insert(0, payload["text"])
    elif payload.get("text") and payload["text"] not in lines:
        # With blocks, `text` is the notification fallback; use it as title if nothing else.
        title = title or payload["text"]
    if payload.get("attachments"):
        atitle, alines, aurl, danger = _attachments(payload["attachments"])
        title = title or atitle
        lines.extend(alines)
        url = url or aurl

    title = title or payload.get("username")
    body = "\n".join(line for line in lines if line and line.strip())
    if not body and not title:
        return None
    if not body:
        body, title = title or "", payload.get("username")

    return Notification(
        title=slack_plain(title) if title else None,
        message=slack_mrkdwn_to_html(body),
        html=True,
        url=url,
        priority=src.danger_priority if danger else None,
    )


def slack_plain(text: str) -> str:
    return html_to_text(slack_mrkdwn_to_html(text))
