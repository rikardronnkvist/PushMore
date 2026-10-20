"""Configuration loading.

The config is a YAML file. String values may reference environment variables
with ``${VAR}`` or read a file (e.g. a Docker secret) with ``${file:/run/secrets/x}``.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

SOURCE_TYPES = {"github", "slack", "apprise", "apprise_json", "generic", "pushover"}

DEFAULT_APPRISE_PRIORITY = {"info": 0, "success": 0, "warning": 0, "failure": 1}

_VAR_RE = re.compile(r"\$\{([^}]+)\}")


class ConfigError(ValueError):
    pass


def _expand(value: Any) -> Any:
    if isinstance(value, str):

        def repl(m: re.Match[str]) -> str:
            ref = m.group(1)
            if ref.startswith("file:"):
                path = Path(ref[5:])
                try:
                    return path.read_text().strip()
                except OSError as exc:
                    raise ConfigError(f"Cannot read {path}: {exc}") from exc
            if ref not in os.environ:
                raise ConfigError(f"Environment variable {ref} is not set")
            return os.environ[ref]

        return _VAR_RE.sub(repl, value)
    if isinstance(value, dict):
        return {k: _expand(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand(v) for v in value]
    return value


@dataclass
class RateLimit:
    count: int
    seconds: int

    @classmethod
    def parse(cls, raw: str | None) -> RateLimit | None:
        if not raw:
            return None
        m = re.fullmatch(r"\s*(\d+)\s*/\s*(s|sec|min|m|h|hour)\s*", str(raw))
        if not m:
            raise ConfigError(f"Invalid rate_limit '{raw}', use e.g. '10/min'")
        unit = {"s": 1, "sec": 1, "m": 60, "min": 60, "h": 3600, "hour": 3600}[m.group(2)]
        return cls(int(m.group(1)), unit)


@dataclass
class Source:
    id: str
    type: str
    token: str | None = None
    secret: str | None = None  # GitHub HMAC secret
    allow_unsigned: bool = False
    enabled: bool = True
    # Pushover overrides
    pushover_token: str | None = None
    pushover_user: str | None = None
    device: str | None = None
    priority: int | None = None
    sound: str | None = None
    title_prefix: str | None = None
    # Filtering / throttling
    min_priority: int | None = None
    exclude: list[str] = field(default_factory=list)
    dedup_seconds: int = 0
    rate_limit: RateLimit | None = None
    # GitHub
    events: list[str] | None = None
    only_failures: bool = False
    # Apprise
    type_priority: dict[str, int] = field(default_factory=lambda: dict(DEFAULT_APPRISE_PRIORITY))
    # Slack
    danger_priority: int | None = 1
    # Generic
    title_template: str | None = None
    message_template: str | None = None
    url_template: str | None = None
    priority_template: str | None = None
    # Pushover-compatible endpoint
    allow_user_override: bool = False

    def __post_init__(self) -> None:
        self._exclude_re = [re.compile(p, re.IGNORECASE | re.MULTILINE) for p in self.exclude]

    def is_excluded(self, text: str) -> bool:
        return any(r.search(text) for r in self._exclude_re)


@dataclass
class Config:
    pushover_user: str
    pushover_token: str
    pushover_api_url: str = "https://api.pushover.net/1/messages.json"
    max_attachment_bytes: int = 2_621_440  # 2.5 MiB
    max_body_bytes: int = 10 * 1024 * 1024
    db_path: str = "/data/pushmore.db"
    retention_days: int = 14
    store_payloads: bool = True
    admin_token: str | None = None
    dry_run: bool = False
    sources: dict[str, Source] = field(default_factory=dict)

    def source(self, source_id: str, expected_type: str | set[str]) -> Source | None:
        src = self.sources.get(source_id)
        types = {expected_type} if isinstance(expected_type, str) else expected_type
        if src is None or not src.enabled or src.type not in types:
            return None
        return src

    def sources_of_type(self, source_type: str) -> list[Source]:
        return [s for s in self.sources.values() if s.enabled and s.type == source_type]


def _build_source(source_id: str, raw: dict[str, Any]) -> Source:
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", source_id):
        raise ConfigError(f"Invalid source id '{source_id}'")
    raw = dict(raw or {})
    stype = raw.pop("type", None)
    if stype not in SOURCE_TYPES:
        raise ConfigError(f"Source '{source_id}': type must be one of {sorted(SOURCE_TYPES)}")
    rate = RateLimit.parse(raw.pop("rate_limit", None))
    type_priority = dict(DEFAULT_APPRISE_PRIORITY)
    type_priority.update(raw.pop("type_priority", None) or {})
    known = set(Source.__dataclass_fields__) - {"id", "type", "rate_limit", "type_priority"}
    unknown = set(raw) - known
    if unknown:
        raise ConfigError(f"Source '{source_id}': unknown keys {sorted(unknown)}")
    src = Source(id=source_id, type=stype, rate_limit=rate, type_priority=type_priority, **raw)

    if stype == "github":
        if not src.secret and not src.allow_unsigned:
            raise ConfigError(f"Source '{source_id}': github requires 'secret' (or allow_unsigned: true)")
    elif not src.token:
        raise ConfigError(f"Source '{source_id}': 'token' is required")
    elif len(src.token) < 16:
        raise ConfigError(f"Source '{source_id}': token must be at least 16 characters")
    return src


def load_config(path: str | os.PathLike[str] | None = None) -> Config:
    path = Path(path or os.environ.get("PUSHMORE_CONFIG", "/config/config.yaml"))
    try:
        raw = yaml.safe_load(path.read_text()) or {}
    except OSError as exc:
        raise ConfigError(f"Cannot read config {path}: {exc}") from exc
    return parse_config(raw)


def parse_config(raw: dict[str, Any]) -> Config:
    raw = _expand(raw)
    po = raw.get("pushover") or {}
    if not po.get("user_key") or not po.get("app_token"):
        raise ConfigError("pushover.user_key and pushover.app_token are required")
    log = raw.get("log") or {}
    server = raw.get("server") or {}
    sources = {sid: _build_source(sid, s) for sid, s in (raw.get("sources") or {}).items()}

    tokens = [s.token for s in sources.values() if s.token]
    if len(tokens) != len(set(tokens)):
        raise ConfigError("Source tokens must be unique")

    return Config(
        pushover_user=po["user_key"],
        pushover_token=po["app_token"],
        pushover_api_url=po.get("api_url", Config.pushover_api_url),
        max_attachment_bytes=int(po.get("max_attachment_bytes", Config.max_attachment_bytes)),
        max_body_bytes=int(server.get("max_body_bytes", Config.max_body_bytes)),
        db_path=log.get("db_path", Config.db_path),
        retention_days=int(log.get("retention_days", Config.retention_days)),
        store_payloads=bool(log.get("store_payloads", True)),
        admin_token=server.get("admin_token"),
        dry_run=bool(server.get("dry_run", False)),
        sources=sources,
    )
