import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from pushmore.app import create_app
from pushmore.config import parse_config

API = "https://api.pushover.net/1/messages.json"

TOKENS = {
    "slack": "slack-token-0123456789",
    "generic": "generic-token-0123456789",
    "apprise": "apprise-key-0123456789",
    "apprise_json": "apprise-json-0123456789",
    "pushover": "pushover-local-0123456789",
    "admin": "admin-token-0123456789",
}
GITHUB_SECRET = "gh-secret"


def base_config(**source_overrides):
    sources = {
        "gh": {"type": "github", "secret": GITHUB_SECRET},
        "ci": {"type": "github", "secret": GITHUB_SECRET, "only_failures": True},
        "kuma": {"type": "slack", "token": TOKENS["slack"]},
        "misc": {
            "type": "generic",
            "token": TOKENS["generic"],
            "title_template": "{{ alert.name }}",
            "message_template": "{{ alert.name }} is {{ status }}",
            "priority_template": "{% if status == 'firing' %}1{% else %}-1{% endif %}",
        },
        "ha": {"type": "apprise", "token": TOKENS["apprise"]},
        "lib": {"type": "apprise_json", "token": TOKENS["apprise_json"]},
        "legacy": {"type": "pushover", "token": TOKENS["pushover"]},
    }
    for sid, override in source_overrides.items():
        sources.setdefault(sid, {}).update(override)
    return parse_config(
        {
            "pushover": {"user_key": "u-real", "app_token": "a-real"},
            "log": {"db_path": ":memory:"},
            "server": {"admin_token": TOKENS["admin"]},
            "sources": sources,
        }
    )


@pytest.fixture
def pushover_mock():
    with respx.mock(assert_all_called=False) as mock:
        route = mock.post(API).mock(
            return_value=httpx.Response(200, json={"status": 1, "request": "req-123"})
        )
        yield route


def make_client(config):
    app = create_app(config, http_client=httpx.AsyncClient())
    return TestClient(app)


@pytest.fixture
def client(pushover_mock):
    with make_client(base_config()) as c:
        yield c


def sent_form(route, index=-1) -> dict[str, str]:
    """Decode the form (urlencoded or multipart) of a captured Pushover request."""
    request = route.calls[index].request
    ctype = request.headers["content-type"]
    body = request.content
    if ctype.startswith("application/x-www-form-urlencoded"):
        from urllib.parse import parse_qsl

        return dict(parse_qsl(body.decode()))
    from email.parser import BytesParser
    from email.policy import default

    msg = BytesParser(policy=default).parsebytes(b"Content-Type: " + ctype.encode() + b"\r\n\r\n" + body)
    out = {}
    for part in msg.iter_parts():
        name = part.get_param("name", header="content-disposition")
        if part.get_filename():
            out[name] = part.get_content()
        else:
            out[name] = part.get_content().strip()
    return out
