"""Pushover-compatible inbound API (``POST /1/messages.json``).

The incoming ``token`` selects a ``pushover`` source; the real Pushover credentials
stay in PushMore. The incoming ``user`` is ignored unless the source sets
``allow_user_override: true``.
"""

from __future__ import annotations

import base64
import binascii
from typing import Any

from ..config import Source
from ..models import Attachment, Notification


class PushoverRequestError(ValueError):
    pass


def _int(data: dict[str, Any], key: str) -> int | None:
    value = data.get(key)
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise PushoverRequestError(f"{key} must be an integer") from exc


def to_notification(
    data: dict[str, Any], src: Source, upload: Attachment | None, max_bytes: int
) -> Notification:
    message = str(data.get("message") or "").strip()
    if not message:
        raise PushoverRequestError("message cannot be blank")

    attachment = upload
    if attachment is None and data.get("attachment_base64"):
        b64 = str(data["attachment_base64"])
        if len(b64) > (max_bytes * 4 // 3) + 8:
            raise PushoverRequestError("attachment is too large")
        try:
            content = base64.b64decode(b64, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise PushoverRequestError("attachment_base64 is not valid base64") from exc
        attachment = Attachment("attachment", content, str(data.get("attachment_type") or "image/jpeg"))
    if attachment is not None and len(attachment.content) > max_bytes:
        raise PushoverRequestError("attachment is too large")

    priority = _int(data, "priority")
    if priority is not None and not -2 <= priority <= 2:
        raise PushoverRequestError("priority must be between -2 and 2")

    return Notification(
        message=message,
        title=data.get("title") or None,
        url=data.get("url") or None,
        url_title=data.get("url_title") or None,
        priority=priority,
        sound=data.get("sound") or None,
        html=str(data.get("html", "")) == "1",
        device=data.get("device") or None,
        timestamp=_int(data, "timestamp"),
        ttl=_int(data, "ttl"),
        retry=_int(data, "retry"),
        expire=_int(data, "expire"),
        attachment=attachment,
        user=(data.get("user") or None) if src.allow_user_override else None,
    )
