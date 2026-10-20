"""Shared pipeline: per-source overrides → filter → dedup → rate limit → send → log to stdout."""

from __future__ import annotations

import hashlib
import logging
import time
from collections import defaultdict, deque

from .config import Config, Source
from .models import Notification, Result
from .pushover import PushoverClient, PushoverError
from .text import html_to_text

log = logging.getLogger(__name__)

PAYLOAD_LOG_MAX = 4096


class Throttle:
    """In-memory dedup and sliding-window rate limit. Assumes a single replica."""

    def __init__(self) -> None:
        self._seen: dict[str, float] = {}
        self._windows: dict[str, deque[float]] = defaultdict(deque)

    def forget(self, keep: set[str]) -> None:
        for source_id in list(self._windows):
            if source_id not in keep:
                del self._windows[source_id]

    def is_duplicate(self, source: Source, n: Notification, now: float) -> bool:
        if source.dedup_seconds <= 0:
            return False
        key = hashlib.sha256(f"{source.id}\0{n.title}\0{n.message}".encode()).hexdigest()
        if len(self._seen) > 10_000:
            self._seen = {k: v for k, v in self._seen.items() if v > now}
        expires = self._seen.get(key)
        if expires and expires > now:
            return True
        self._seen[key] = now + source.dedup_seconds
        return False

    def is_limited(self, source: Source, now: float) -> bool:
        rl = source.rate_limit
        if not rl:
            return False
        window = self._windows[source.id]
        while window and window[0] <= now - rl.seconds:
            window.popleft()
        if len(window) >= rl.count:
            return True
        window.append(now)
        return False


class Dispatcher:
    def __init__(self, config: Config, client: PushoverClient):
        self.config = config
        self.client = client
        self.throttle = Throttle()

    def reconfigure(self, config: Config, client: PushoverClient) -> None:
        """Swap config and client; keep throttle state for sources that still exist."""
        self.config = config
        self.client = client
        self.throttle.forget(set(config.sources))

    def apply_source(self, source: Source, n: Notification) -> Notification:
        if n.priority is None:
            n.priority = source.priority if source.priority is not None else 0
        if source.sound and not n.sound:
            n.sound = source.sound
        if source.device and not n.device:
            n.device = source.device
        if source.pushover_token and not n.token:
            n.token = source.pushover_token
        if source.pushover_user and not n.user:
            n.user = source.pushover_user
        if source.title_prefix:
            n.title = f"{source.title_prefix} {n.title}" if n.title else source.title_prefix
        return n

    async def dispatch(
        self,
        source: Source,
        n: Notification | None,
        *,
        endpoint: str,
        payload: bytes | None = None,
    ) -> Result:
        if n is None:
            result = Result("ignored", "event not mapped or filtered by source rules")
            self._log(source, endpoint, result, None, payload)
            return result

        n = self.apply_source(source, n)

        now = time.time()
        plain = html_to_text(n.message) if n.html else n.message
        if source.min_priority is not None and n.priority < source.min_priority:
            result = Result("filtered", f"priority {n.priority} < min_priority {source.min_priority}")
        elif source.is_excluded(f"{n.title or ''}\n{plain}"):
            result = Result("filtered", "matched exclude pattern")
        elif self.throttle.is_duplicate(source, n, now):
            result = Result("deduped")
        elif self.throttle.is_limited(source, now):
            result = Result("ratelimited")
        elif self.config.dry_run:
            result = Result("dry_run", str(self.client.build_form(n) | {"token": "***", "user": "***"}))
        else:
            try:
                request_id = await self.client.send(n)
                result = Result("sent", request_id=request_id)
            except PushoverError as exc:
                result = Result("error", str(exc))

        self._log(source, endpoint, result, n, payload)
        return result

    def _log(
        self, source: Source, endpoint: str, result: Result, n: Notification | None, payload: bytes | None
    ) -> None:
        level = logging.WARNING if result.status == "error" else logging.INFO
        log.log(
            level,
            "source=%s endpoint=%s status=%s title=%r%s",
            source.id,
            endpoint,
            result.status,
            n.title if n else None,
            f" detail={result.detail}" if result.detail else "",
        )
        if payload and log.isEnabledFor(logging.DEBUG):
            log.debug("source=%s payload=%s", source.id, payload[:PAYLOAD_LOG_MAX].decode("utf-8", "replace"))
