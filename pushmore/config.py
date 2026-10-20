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
    # Keys set in the config file (the rest are defaults); not part of equality
    explicit: frozenset[str] = field(default_factory=frozenset, compare=False, repr=False)
    explicit_type_priority: frozenset[str] = field(default_factory=frozenset, compare=False, repr=False)

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
    dry_run: bool = False
    sources: dict[str, Source] = field(default_factory=dict)
    # Dotted global keys set in the config file, e.g. "pushover.api_url"
    explicit: frozenset[str] = field(default_factory=frozenset, compare=False, repr=False)

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
    explicit = frozenset(raw)
    rate = RateLimit.parse(raw.pop("rate_limit", None))
    type_priority = dict(DEFAULT_APPRISE_PRIORITY)
    given_tp = raw.pop("type_priority", None) or {}
    type_priority.update(given_tp)
    internal = {"id", "type", "rate_limit", "type_priority", "explicit", "explicit_type_priority"}
    known = set(Source.__dataclass_fields__) - internal
    unknown = set(raw) - known
    if unknown:
        raise ConfigError(f"Source '{source_id}': unknown keys {sorted(unknown)}")
    src = Source(
        id=source_id,
        type=stype,
        rate_limit=rate,
        type_priority=type_priority,
        explicit=explicit,
        explicit_type_priority=frozenset(given_tp),
        **raw,
    )

    if stype == "github":
        if not src.secret and not src.allow_unsigned:
            raise ConfigError(f"Source '{source_id}': github requires 'secret' (or allow_unsigned: true)")
    elif not src.token:
        raise ConfigError(f"Source '{source_id}': 'token' is required")
    elif len(src.token) < 16:
        raise ConfigError(f"Source '{source_id}': token must be at least 16 characters")
    return src


def config_path() -> Path:
    return Path(os.environ.get("PUSHMORE_CONFIG", "/config/config.yaml"))


def load_config(path: str | os.PathLike[str] | None = None) -> Config:
    path = Path(path) if path else config_path()
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise ConfigError(f"Cannot read config {path}: {exc}") from exc
    return config_from_bytes(data)


def config_from_bytes(data: bytes) -> Config:
    try:
        raw = yaml.safe_load(data) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"Invalid YAML: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError("Config must be a YAML mapping")
    return parse_config(raw)


def parse_config(raw: dict[str, Any]) -> Config:
    raw = _expand(raw)
    po = raw.get("pushover") or {}
    if not po.get("user_key") or not po.get("app_token"):
        raise ConfigError("pushover.user_key and pushover.app_token are required")
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
        dry_run=bool(server.get("dry_run", False)),
        sources=sources,
        explicit=frozenset([f"pushover.{k}" for k in po] + [f"server.{k}" for k in server]),
    )
