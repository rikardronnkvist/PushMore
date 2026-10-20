"""Generic JSON webhook → Notification via sandboxed Jinja templates.

Templates get the whole payload as ``payload`` and its top-level keys directly,
e.g. ``title_template: "{{ alert.name }} is {{ status }}"``.
"""

from __future__ import annotations

import json
from typing import Any

from jinja2 import StrictUndefined, TemplateError
from jinja2.sandbox import SandboxedEnvironment

from ..config import Source
from ..models import Notification

_env = SandboxedEnvironment(autoescape=False, undefined=StrictUndefined)
_env.filters["tojson"] = lambda v: json.dumps(v, ensure_ascii=False)


class TemplateRenderError(ValueError):
    pass


def _render(template: str | None, payload: Any) -> str | None:
    if not template:
        return None
    context = dict(payload) if isinstance(payload, dict) else {}
    context["payload"] = payload
    try:
        return _env.from_string(template).render(**context).strip()
    except TemplateError as exc:
        raise TemplateRenderError(f"Template error: {exc}") from exc


def to_notification(payload: Any, src: Source) -> Notification | None:
    title = _render(src.title_template, payload)
    message = _render(src.message_template, payload)
    url = _render(src.url_template, payload)
    prio = _render(src.priority_template, payload)

    if message is None and isinstance(payload, dict):
        for key in ("message", "text", "body", "msg", "description", "content"):
            if isinstance(payload.get(key), str) and payload[key].strip():
                message = payload[key]
                break
        if title is None and isinstance(payload.get("title"), str):
            title = payload["title"]
    if message is None:
        message = json.dumps(payload, ensure_ascii=False, indent=1)[:1000]
    if not message.strip():
        return None

    priority = None
    if prio:
        try:
            priority = int(float(prio))
        except ValueError as exc:
            raise TemplateRenderError(f"priority_template rendered non-number '{prio}'") from exc

    return Notification(title=title or None, message=message, url=url or None, priority=priority)
