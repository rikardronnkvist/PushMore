import pytest

from pushmore.config import ConfigError, RateLimit, parse_config
from pushmore.text import markdown_to_html, slack_mrkdwn_to_html, truncate


def test_markdown_basic():
    out = markdown_to_html("# Head\n**bold** and *it* [link](https://x.se) <script>")
    assert "<b>Head</b>" in out
    assert "<b>bold</b>" in out and "<i>it</i>" in out
    assert '<a href="https://x.se">link</a>' in out
    assert "&lt;script&gt;" in out


def test_markdown_rejects_non_http_links():
    assert "href" not in markdown_to_html("[x](javascript:alert(1))")


def test_slack_literal_entities_preserved():
    assert slack_mrkdwn_to_html("a &lt;b&gt; c") == "a &lt;b&gt; c"


def test_truncate():
    assert truncate("abcdef", 4) == "abc…"
    assert truncate("abc", 4) == "abc"


def test_env_expansion(monkeypatch, tmp_path):
    monkeypatch.setenv("PO_USER", "u-env")
    secret = tmp_path / "tok"
    secret.write_text("a-file\n")
    cfg = parse_config({"pushover": {"user_key": "${PO_USER}", "app_token": f"${{file:{secret}}}"}})
    assert cfg.pushover_user == "u-env" and cfg.pushover_token == "a-file"


def test_missing_env_fails(monkeypatch):
    monkeypatch.delenv("NOPE", raising=False)
    with pytest.raises(ConfigError):
        parse_config({"pushover": {"user_key": "${NOPE}", "app_token": "a"}})


@pytest.mark.parametrize(
    "src",
    [
        {"type": "slack"},  # no token
        {"type": "slack", "token": "short"},
        {"type": "nope", "token": "x" * 20},
        {"type": "slack", "token": "x" * 20, "typo_key": 1},
    ],
)
def test_invalid_sources(src):
    with pytest.raises(ConfigError):
        parse_config({"pushover": {"user_key": "u", "app_token": "a"}, "sources": {"s": src}})


def test_duplicate_tokens():
    tok = "x" * 20
    with pytest.raises(ConfigError):
        parse_config(
            {
                "pushover": {"user_key": "u", "app_token": "a"},
                "sources": {"a": {"type": "slack", "token": tok}, "b": {"type": "apprise", "token": tok}},
            }
        )


def test_rate_limit_parse():
    assert RateLimit.parse("10/min").seconds == 60
    with pytest.raises(ConfigError):
        RateLimit.parse("ten per minute")
