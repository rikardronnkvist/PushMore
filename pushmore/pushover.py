"""Outbound client for the Pushover Messages API."""

from __future__ import annotations

import logging

import httpx

from .models import Notification
from .text import MESSAGE_MAX, TITLE_MAX, URL_MAX, URL_TITLE_MAX, truncate

log = logging.getLogger(__name__)


class PushoverError(RuntimeError):
    pass


class PushoverClient:
    def __init__(self, api_url: str, default_user: str, default_token: str, client: httpx.AsyncClient):
        self.api_url = api_url
        self.default_user = default_user
        self.default_token = default_token
        self.client = client

    def build_form(self, n: Notification) -> dict[str, str]:
        priority = max(-2, min(2, int(n.priority or 0)))
        form: dict[str, str | None] = {
            "token": n.token or self.default_token,
            "user": n.user or self.default_user,
            "message": truncate(n.message, MESSAGE_MAX) or "(empty)",
            "title": truncate(n.title, TITLE_MAX),
            "url": truncate(n.url, URL_MAX),
            "url_title": truncate(n.url_title, URL_TITLE_MAX),
            "priority": str(priority),
            "sound": n.sound,
            "device": n.device,
            "html": "1" if n.html else None,
            "timestamp": str(n.timestamp) if n.timestamp else None,
            "ttl": str(n.ttl) if n.ttl else None,
        }
        if priority == 2:
            form["retry"] = str(max(30, n.retry or 60))
            form["expire"] = str(min(10800, n.expire or 3600))
        return {k: v for k, v in form.items() if v not in (None, "")}

    async def send(self, n: Notification) -> str:
        form = self.build_form(n)
        files = None
        if n.attachment:
            files = {"attachment": (n.attachment.filename, n.attachment.content, n.attachment.mimetype)}
        try:
            resp = await self.client.post(self.api_url, data=form, files=files, timeout=15)
        except httpx.HTTPError as exc:
            raise PushoverError(f"Pushover request failed: {exc}") from exc
        try:
            body = resp.json()
        except ValueError:
            body = {}
        if resp.status_code != 200 or body.get("status") != 1:
            errors = body.get("errors") or [resp.text[:200]]
            raise PushoverError(f"Pushover rejected message ({resp.status_code}): {'; '.join(errors)}")
        return str(body.get("request", ""))
