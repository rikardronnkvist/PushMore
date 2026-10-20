"""Inbound Apprise messages.

Two wire formats are supported:

* apprise-api ``/notify/<key>`` (what ``apprise://`` / ``apprises://`` URLs and
  "Apprise API" integrations send): ``title``, ``body``, ``type``, ``format``, ``tag``.
  JSON or form encoded, attachments as multipart files.
* Apprise's generic ``json://`` plugin: ``version``, ``title``, ``message``,
  ``type`` and ``attachments`` as ``[{filename, base64, mimetype}]``.

Remote attachment URLs are deliberately ignored (no server-side fetching).
"""

from __future__ import annotations

import base64
import binascii
from typing import Any

from ..config import Source
from ..models import Attachment, Notification
from ..text import html_to_text, markdown_to_html

IMAGE_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp", "image/bmp"}


def _format(title: str | None, body: str, fmt: str | None) -> tuple[str | None, str, bool]:
    fmt = (fmt or "text").lower()
    if fmt == "markdown":
        return (html_to_text(markdown_to_html(title)) if title else None), markdown_to_html(body), True
    if fmt == "html":
        # Pushover renders a small HTML subset itself and ignores the rest.
        return (html_to_text(title) if title else None), body, True
    return title, body, False


def _priority(src: Source, ntype: str | None) -> int | None:
    ntype = (ntype or "info").lower()
    return src.type_priority.get(ntype, src.type_priority.get("info"))


def pick_attachment(candidates: list[Attachment], max_bytes: int) -> Attachment | None:
    """Pushover accepts one image; use the first image that fits, drop the rest."""
    for att in candidates:
        if att.mimetype.lower() in IMAGE_TYPES and 0 < len(att.content) <= max_bytes:
            return att
    return None


def decode_json_attachments(raw: Any, max_bytes: int) -> list[Attachment]:
    out: list[Attachment] = []
    if not isinstance(raw, list):
        return out
    for item in raw[:10]:
        if not isinstance(item, dict) or not item.get("base64"):
            continue
        b64 = str(item["base64"])
        if len(b64) > (max_bytes * 4 // 3) + 8:
            continue  # too large; skip before decoding
        try:
            content = base64.b64decode(b64, validate=True)
        except (binascii.Error, ValueError):
            continue
        out.append(
            Attachment(
                filename=str(item.get("filename") or "attachment"),
                content=content,
                mimetype=str(item.get("mimetype") or "application/octet-stream"),
            )
        )
    return out


def from_apprise_api(
    data: dict[str, Any], src: Source, attachments: list[Attachment], max_bytes: int
) -> Notification | None:
    body = str(data.get("body") or "").strip()
    raw_title = data.get("title")
    title = str(raw_title).strip() if raw_title else None
    if not body and not title:
        return None
    title, message, is_html = _format(title, body or title or "", data.get("format"))
    return Notification(
        title=title if body else None,
        message=message,
        html=is_html,
        priority=_priority(src, data.get("type")),
        attachment=pick_attachment(attachments, max_bytes),
    )


def from_apprise_json(data: dict[str, Any], src: Source, max_bytes: int) -> Notification | None:
    message = str(data.get("message") or "").strip()
    raw_title = data.get("title")
    title = str(raw_title).strip() if raw_title else None
    if not message and not title:
        return None
    return Notification(
        title=title if message else None,
        message=message or title or "",
        priority=_priority(src, data.get("type")),
        attachment=pick_attachment(decode_json_attachments(data.get("attachments"), max_bytes), max_bytes),
    )
