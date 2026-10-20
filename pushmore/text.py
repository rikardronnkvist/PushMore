"""Text helpers: limits and conversion to the HTML subset Pushover supports (b, i, u, a, font color)."""

from __future__ import annotations

import html
import re

TITLE_MAX = 250
MESSAGE_MAX = 1024
URL_MAX = 512
URL_TITLE_MAX = 100


def truncate(text: str | None, limit: int) -> str | None:
    if text is None:
        return None
    text = text.strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _safe_url(url: str) -> str | None:
    url = url.strip()
    return url if re.match(r"^https?://", url, re.IGNORECASE) else None


def _link(url: str, label: str) -> str:
    safe = _safe_url(html.unescape(url))
    if not safe:
        return label
    return f'<a href="{html.escape(safe, quote=True)}">{label}</a>'


def markdown_to_html(text: str) -> str:
    """Minimal Markdown → Pushover HTML. Input is escaped first, so it is safe."""
    out = html.escape(text, quote=False)
    out = re.sub(r"```.*?\n(.*?)```", r"\1", out, flags=re.DOTALL)
    out = re.sub(r"`([^`\n]+)`", r"\1", out)
    out = re.sub(r"^#{1,6}\s+(.+)$", r"<b>\1</b>", out, flags=re.MULTILINE)
    out = re.sub(r"\[([^\]\n]+)\]\(([^)\s]+)\)", lambda m: _link(m.group(2), m.group(1)), out)
    out = re.sub(r"\*\*(.+?)\*\*|__(.+?)__", lambda m: f"<b>{m.group(1) or m.group(2)}</b>", out)
    out = re.sub(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])", r"<i>\1</i>", out)
    out = re.sub(r"(?<![\w_])_(?!\s)(.+?)(?<!\s)_(?![\w_])", r"<i>\1</i>", out)
    out = re.sub(r"^\s*[-*]\s+", "• ", out, flags=re.MULTILINE)
    return out


def slack_mrkdwn_to_html(text: str) -> str:
    """Slack mrkdwn (``<url|label>``, ``*bold*``, ``_italic_``) → Pushover HTML."""
    out = html.escape(text, quote=False)

    def slack_link(m: re.Match[str]) -> str:
        target, _, label = m.group(1).partition("|")
        if target.startswith(("@", "#", "!")):
            return label or target
        return _link(target, label or target)

    # Raw <...> are Slack control sequences; Slack escapes literal < > & as entities,
    # which arrive here double-escaped and are restored after link handling.
    out = re.sub(r"&lt;([^\s&][^<>]*?)&gt;", slack_link, out)
    out = out.replace("&amp;lt;", "&lt;").replace("&amp;gt;", "&gt;").replace("&amp;amp;", "&amp;")
    out = re.sub(r"```(.*?)```", r"\1", out, flags=re.DOTALL)
    out = re.sub(r"`([^`\n]+)`", r"\1", out)
    out = re.sub(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])", r"<b>\1</b>", out)
    out = re.sub(r"(?<![\w_])_(?!\s)(.+?)(?<!\s)_(?![\w_])", r"<i>\1</i>", out)
    out = re.sub(r"(?<![\w~])~(?!\s)(.+?)(?<!\s)~(?![\w~])", r"\1", out)
    return out


def html_to_text(text: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", text))
