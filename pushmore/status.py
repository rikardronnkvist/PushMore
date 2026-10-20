"""Read-only HTML page showing the running configuration.

Values that come from defaults are shown and marked as such. Secrets
(tokens, HMAC secrets, Pushover credentials) are masked.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from jinja2 import Environment, PackageLoader, select_autoescape

from . import __version__
from .config import DEFAULT_APPRISE_PRIORITY, Config, Source
from .sources.github import HANDLERS as GITHUB_EVENTS

_env = Environment(loader=PackageLoader("pushmore", "templates"), autoescape=select_autoescape(["html"]))

SECRET_KEYS = {"token", "secret", "pushover_token", "pushover_user"}

COMMON_KEYS = [
    "enabled",
    "pushover_token",
    "pushover_user",
    "priority",
    "sound",
    "device",
    "title_prefix",
    "min_priority",
    "exclude",
    "dedup_seconds",
    "rate_limit",
]

TYPE_KEYS = {
    "github": ["secret", "allow_unsigned", "events", "only_failures"],
    "slack": ["token", "danger_priority"],
    "apprise": ["token", "type_priority"],
    "apprise_json": ["token", "type_priority"],
    "generic": ["token", "title_template", "message_template", "url_template", "priority_template"],
    "pushover": ["token", "allow_user_override"],
}

# How a default is described when the default itself is "not set".
DEFAULT_TEXT = {
    "pushover_token": "pushover.app_token",
    "pushover_user": "pushover.user_key",
    "priority": "from mapping, else 0",
    "sound": "Pushover app default",
    "device": "all devices",
    "title_prefix": "none",
    "min_priority": "none",
    "exclude": "none",
    "dedup_seconds": "0 (off)",
    "rate_limit": "unlimited",
    "events": "all: " + ", ".join(GITHUB_EVENTS),
    "title_template": "payload title",
    "message_template": "payload message/text/body",
    "url_template": "none",
    "priority_template": "from source priority",
    "secret": "none",
}

ENDPOINTS = {
    "github": "POST /hooks/github/{id}",
    "slack": "POST /hooks/slack/{id}?token=…",
    "apprise": "POST /notify/<token>",
    "apprise_json": "POST /hooks/apprise-json/{id}?token=…",
    "generic": "POST /hooks/generic/{id}",
    "pushover": "POST /1/messages.json",
}

MULTILINE_KEYS = {"title_template", "message_template", "url_template", "priority_template"}


@dataclass
class Row:
    key: str
    value: str
    is_default: bool
    secret: bool = False
    multiline: bool = False


def mask(value: Any) -> str:
    text = str(value or "")
    if not text:
        return ""
    return "••••" + text[-4:] if len(text) >= 12 else "••••"


def _fmt(key: str, value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if key == "rate_limit":
        unit = {1: "s", 60: "min", 3600: "h"}.get(value.seconds, f"{value.seconds}s")
        return f"{value.count}/{unit}"
    if isinstance(value, (list, tuple)):
        return ", ".join(str(v) for v in value)
    if isinstance(value, dict):
        return ", ".join(f"{k}: {v}" for k, v in value.items())
    return str(value)


def source_rows(src: Source) -> list[Row]:
    rows: list[Row] = []
    for key in TYPE_KEYS.get(src.type, []) + COMMON_KEYS:
        value = getattr(src, key)
        explicit = key in src.explicit
        if key == "type_priority":
            parts = []
            for level in ("info", "success", "warning", "failure"):
                mark = "" if level in src.explicit_type_priority else " (default)"
                parts.append(f"{level}: {value.get(level, DEFAULT_APPRISE_PRIORITY[level])}{mark}")
            extra = [f"{k}: {v}" for k, v in value.items() if k not in DEFAULT_APPRISE_PRIORITY]
            rows.append(Row(key, ", ".join(parts + extra), not explicit))
            continue
        if key in SECRET_KEYS:
            shown = mask(value) if value else DEFAULT_TEXT.get(key, "")
            rows.append(Row(key, shown, not explicit, secret=bool(value)))
            continue
        if explicit:
            shown = _fmt(key, value) or '""'
        else:
            shown = DEFAULT_TEXT.get(key) or _fmt(key, value) or "none"
        rows.append(Row(key, shown, not explicit, multiline=key in MULTILINE_KEYS and explicit))
    return rows


def global_rows(cfg: Config) -> list[Row]:
    def row(key: str, value: Any, secret: bool = False) -> Row:
        return Row(key, mask(value) if secret else _fmt(key, value), key not in cfg.explicit, secret)

    return [
        row("pushover.user_key", cfg.pushover_user, secret=True),
        row("pushover.app_token", cfg.pushover_token, secret=True),
        row("pushover.api_url", cfg.pushover_api_url),
        row("pushover.max_attachment_bytes", cfg.max_attachment_bytes),
        row("server.max_body_bytes", cfg.max_body_bytes),
        row("server.dry_run", cfg.dry_run),
    ]


def runtime_rows(config_file: str | None, reload_interval: float) -> list[Row]:
    def env(name: str, effective: str) -> Row:
        return Row(name, effective, name not in os.environ)

    interval = f"{reload_interval:g}s" if reload_interval > 0 else "disabled"
    return [
        Row("version", __version__, False),
        env("PUSHMORE_CONFIG", config_file or "(provided programmatically)"),
        env("PUSHMORE_RELOAD_INTERVAL", interval),
        env("LOG_LEVEL", os.environ.get("LOG_LEVEL", "INFO").upper()),
        env("FORWARDED_ALLOW_IPS", os.environ.get("FORWARDED_ALLOW_IPS", "127.0.0.1")),
    ]


def render(
    cfg: Config,
    *,
    loaded_at: datetime,
    config_file: str | None,
    reload_interval: float,
    reload_error: str | None,
) -> str:
    sources = [
        {
            "id": src.id,
            "type": src.type,
            "enabled": src.enabled,
            "endpoint": ENDPOINTS[src.type].replace("{id}", src.id),
            "rows": source_rows(src),
        }
        for src in sorted(cfg.sources.values(), key=lambda s: s.id)
    ]
    return _env.get_template("config.html").render(
        version=__version__,
        loaded_at=loaded_at.astimezone().strftime("%Y-%m-%d %H:%M:%S %Z"),
        now=datetime.now(UTC).astimezone().strftime("%H:%M:%S"),
        reload_error=reload_error,
        dry_run=cfg.dry_run,
        global_rows=global_rows(cfg),
        runtime_rows=runtime_rows(config_file, reload_interval),
        sources=sources,
    )
