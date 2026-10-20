from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Attachment:
    filename: str
    content: bytes
    mimetype: str


@dataclass
class Notification:
    message: str
    title: str | None = None
    url: str | None = None
    url_title: str | None = None
    priority: int | None = None  # None = no opinion, source default applies
    sound: str | None = None
    html: bool = False
    device: str | None = None
    timestamp: int | None = None
    ttl: int | None = None
    retry: int | None = None
    expire: int | None = None
    attachment: Attachment | None = None
    # Overrides of the global Pushover credentials (per source or per request)
    user: str | None = None
    token: str | None = None
    extra: dict[str, str] = field(default_factory=dict)


@dataclass
class Result:
    """Outcome of handling an inbound request."""

    status: str  # sent | filtered | deduped | ratelimited | ignored | error | dry_run
    detail: str | None = None
    request_id: str | None = None

    @property
    def ok(self) -> bool:
        return self.status != "error"
