import base64
import hashlib
import hmac
import json

import httpx

from .conftest import GITHUB_SECRET, TOKENS, base_config, make_client, sent_form

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
)


def gh_post(client, source, event, payload, secret=GITHUB_SECRET):
    body = json.dumps(payload).encode()
    sig = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return client.post(
        f"/hooks/github/{source}",
        content=body,
        headers={"X-GitHub-Event": event, "X-Hub-Signature-256": sig, "Content-Type": "application/json"},
    )


# ---- health / auth ---------------------------------------------------------


def test_healthz(client):
    assert client.get("/healthz").json()["status"] == "ok"


def test_events_requires_admin(client):
    assert client.get("/api/events").status_code == 401
    r = client.get("/api/events", headers={"Authorization": f"Bearer {TOKENS['admin']}"})
    assert r.status_code == 200


def test_wrong_token_and_unknown_source_look_identical(client, pushover_mock):
    a = client.post("/hooks/slack/kuma?token=wrong", json={"text": "x"})
    b = client.post(f"/hooks/slack/nope?token={TOKENS['slack']}", json={"text": "x"})
    assert a.status_code == b.status_code == 401
    assert a.json() == b.json()
    assert not pushover_mock.called


def test_token_of_other_type_rejected(client):
    r = client.post(f"/hooks/slack/misc?token={TOKENS['generic']}", json={"text": "x"})
    assert r.status_code == 401


def test_body_limit(pushover_mock):
    cfg = base_config()
    cfg.max_body_bytes = 100
    with make_client(cfg) as c:
        r = c.post(f"/hooks/slack/kuma?token={TOKENS['slack']}", json={"text": "x" * 500})
        assert r.status_code == 413


# ---- GitHub ----------------------------------------------------------------


def test_github_bad_signature(client, pushover_mock):
    r = gh_post(client, "gh", "push", {"commits": []}, secret="wrong")
    assert r.status_code == 401
    assert not pushover_mock.called


def test_github_ping(client, pushover_mock):
    r = gh_post(client, "gh", "ping", {"zen": "x"})
    assert r.json()["result"] == "pong"
    assert not pushover_mock.called


def test_github_push(client, pushover_mock):
    payload = {
        "ref": "refs/heads/main",
        "compare": "https://github.com/o/r/compare/a...b",
        "repository": {"full_name": "o/r"},
        "sender": {"login": "rikard"},
        "commits": [{"message": "Fix thing\n\nlong body", "author": {"name": "Rikard"}}],
    }
    r = gh_post(client, "gh", "push", payload)
    assert r.json()["result"] == "sent"
    form = sent_form(pushover_mock)
    assert form["title"] == "o/r: 1 commit to main"
    assert "Fix thing (Rikard)" in form["message"]
    assert form["url"].endswith("compare/a...b")
    assert form["user"] == "u-real" and form["token"] == "a-real"


def _run(conclusion):
    return {
        "action": "completed",
        "repository": {"full_name": "o/r"},
        "workflow_run": {
            "name": "CI",
            "conclusion": conclusion,
            "head_branch": "main",
            "head_sha": "abcdef1234",
            "html_url": "https://github.com/o/r/actions/runs/1",
            "head_commit": {"message": "Bump"},
        },
    }


def test_github_workflow_failure_priority(client, pushover_mock):
    gh_post(client, "ci", "workflow_run", _run("failure"))
    form = sent_form(pushover_mock)
    assert form["priority"] == "1"
    assert "CI failure" in form["title"]


def test_github_only_failures_ignores_success(client, pushover_mock):
    r = gh_post(client, "ci", "workflow_run", _run("success"))
    assert r.json()["result"] == "ignored"
    assert not pushover_mock.called


def test_github_unknown_event_ignored(client, pushover_mock):
    assert gh_post(client, "gh", "label", {"action": "created"}).json()["result"] == "ignored"


def test_github_events_filter(pushover_mock):
    with make_client(base_config(gh={"events": ["release"]})) as c:
        r = gh_post(c, "gh", "push", {"commits": [{"message": "x"}], "ref": "refs/heads/main"})
        assert r.json()["result"] == "ignored"


def test_github_unsigned_rejected_at_config():
    import pytest

    from pushmore.config import ConfigError, parse_config

    with pytest.raises(ConfigError):
        parse_config({"pushover": {"user_key": "u", "app_token": "a"}, "sources": {"x": {"type": "github"}}})


# ---- Slack -----------------------------------------------------------------


def test_slack_text(client, pushover_mock):
    r = client.post(
        f"/hooks/slack/kuma?token={TOKENS['slack']}",
        json={"text": "*Down*: <https://ex.com|Example> & <@U123>"},
    )
    assert r.text == "ok"
    form = sent_form(pushover_mock)
    assert form["html"] == "1"
    assert form["message"] == '<b>Down</b>: <a href="https://ex.com">Example</a> &amp; @U123'


def test_slack_attachments_danger(client, pushover_mock):
    client.post(
        "/hooks/slack/kuma",
        headers={"Authorization": f"Bearer {TOKENS['slack']}"},
        json={
            "attachments": [
                {
                    "color": "danger",
                    "title": "Service down",
                    "title_link": "https://kuma/1",
                    "text": "nas01 unreachable",
                    "fields": [{"title": "Host", "value": "nas01"}],
                }
            ]
        },
    )
    form = sent_form(pushover_mock)
    assert form["title"] == "Service down"
    assert form["priority"] == "1"
    assert form["url"] == "https://kuma/1"
    assert "<b>Host</b>: nas01" in form["message"]


def test_slack_blocks(client, pushover_mock):
    client.post(
        f"/hooks/slack/kuma?token={TOKENS['slack']}",
        json={
            "text": "fallback",
            "blocks": [
                {"type": "header", "text": {"type": "plain_text", "text": "Deploy done"}},
                {"type": "section", "text": {"type": "mrkdwn", "text": "stack *web* updated"}},
            ],
        },
    )
    form = sent_form(pushover_mock)
    assert form["title"] == "Deploy done"
    assert form["message"] == "stack <b>web</b> updated"


def test_slack_legacy_form_payload(client, pushover_mock):
    r = client.post(
        f"/hooks/slack/kuma?token={TOKENS['slack']}",
        data={"payload": json.dumps({"text": "hello"})},
    )
    assert r.text == "ok"
    assert sent_form(pushover_mock)["message"] == "hello"


def test_slack_rejects_javascript_links(client, pushover_mock):
    client.post(f"/hooks/slack/kuma?token={TOKENS['slack']}", json={"text": "<javascript:alert(1)|x>"})
    assert "href" not in sent_form(pushover_mock)["message"]


# ---- Generic ---------------------------------------------------------------


def test_generic_templates(client, pushover_mock):
    r = client.post(
        f"/hooks/generic/misc?token={TOKENS['generic']}",
        json={"alert": {"name": "DiskFull"}, "status": "firing"},
    )
    assert r.json()["result"] == "sent"
    form = sent_form(pushover_mock)
    assert form["title"] == "DiskFull"
    assert form["message"] == "DiskFull is firing"
    assert form["priority"] == "1"


def test_generic_template_error_is_400(client):
    r = client.post(f"/hooks/generic/misc?token={TOKENS['generic']}", json={"status": "x"})
    assert r.status_code == 400


def test_generic_sandbox(pushover_mock):
    cfg = base_config(
        misc={
            "message_template": "{{ ''.__class__.__mro__ }}",
            "title_template": None,
            "priority_template": None,
        }
    )
    with make_client(cfg) as c:
        r = c.post(f"/hooks/generic/misc?token={TOKENS['generic']}", json={})
        assert r.status_code == 400


# ---- Apprise ---------------------------------------------------------------


def test_apprise_api_key_json(client, pushover_mock):
    r = client.post(
        f"/notify/{TOKENS['apprise']}",
        json={
            "title": "Backup",
            "body": "**ssb** failed",
            "type": "failure",
            "format": "markdown",
            "urls": "pover://attacker@token",
        },
    )
    assert r.status_code == 200
    form = sent_form(pushover_mock)
    assert form["priority"] == "1"
    assert form["html"] == "1"
    assert form["message"] == "<b>ssb</b> failed"
    assert form["user"] == "u-real"  # `urls` never redirects delivery


def test_apprise_api_form(client, pushover_mock):
    client.post(f"/notify/{TOKENS['apprise']}", data={"body": "hi", "type": "info"})
    form = sent_form(pushover_mock)
    assert form["message"] == "hi" and form["priority"] == "0"


def test_apprise_stateless_bearer(client, pushover_mock):
    r = client.post("/notify", headers={"Authorization": f"Bearer {TOKENS['apprise']}"}, json={"body": "x"})
    assert r.status_code == 200
    assert client.post("/notify", json={"body": "x"}).status_code == 401


def test_apprise_api_multipart_attachment(client, pushover_mock):
    client.post(
        f"/notify/{TOKENS['apprise']}",
        data={"body": "snapshot"},
        files=[("attach", ("cam.png", PNG, "image/png")), ("attach", ("log.txt", b"x", "text/plain"))],
    )
    form = sent_form(pushover_mock)
    assert form["attachment"] == PNG


def test_apprise_json_with_attachment(client, pushover_mock):
    r = client.post(
        f"/hooks/apprise-json/lib?token={TOKENS['apprise_json']}",
        json={
            "version": "1.0",
            "title": "Door",
            "message": "Front door opened",
            "type": "warning",
            "attachments": [
                {"filename": "a.png", "base64": base64.b64encode(PNG).decode(), "mimetype": "image/png"}
            ],
        },
    )
    assert r.json()["result"] == "sent"
    form = sent_form(pushover_mock)
    assert form["title"] == "Door"
    assert form["attachment"] == PNG


def test_apprise_json_oversize_attachment_dropped(pushover_mock):
    cfg = base_config()
    cfg.max_attachment_bytes = 10
    with make_client(cfg) as c:
        c.post(
            f"/hooks/apprise-json/lib?token={TOKENS['apprise_json']}",
            json={
                "message": "x",
                "attachments": [{"base64": base64.b64encode(PNG).decode(), "mimetype": "image/png"}],
            },
        )
        assert "attachment" not in sent_form(pushover_mock)


def test_apprise_type_priority_override(pushover_mock):
    with make_client(base_config(ha={"type_priority": {"info": -1}})) as c:
        c.post(f"/notify/{TOKENS['apprise']}", json={"body": "x", "type": "info"})
        assert sent_form(pushover_mock)["priority"] == "-1"


# ---- Pushover compatible ---------------------------------------------------


def test_pushover_compat(client, pushover_mock):
    r = client.post(
        "/1/messages.json",
        data={
            "token": TOKENS["pushover"],
            "user": "client-user",
            "message": "hello",
            "priority": "1",
            "sound": "siren",
        },
    )
    assert r.json() == {"status": 1, "request": "req-123"}
    form = sent_form(pushover_mock)
    assert form["token"] == "a-real" and form["user"] == "u-real"
    assert form["priority"] == "1" and form["sound"] == "siren"


def test_pushover_compat_json_and_errors(client, pushover_mock):
    r = client.post("/1/messages.json", json={"token": "bad", "user": "x", "message": "m"})
    assert r.status_code == 400 and r.json()["token"] == "invalid"
    r = client.post("/1/messages.json", json={"token": TOKENS["pushover"], "message": ""})
    assert r.status_code == 400 and r.json()["status"] == 0


def test_pushover_compat_user_override(pushover_mock):
    with make_client(base_config(legacy={"allow_user_override": True})) as c:
        c.post("/1/messages.json", data={"token": TOKENS["pushover"], "user": "other", "message": "m"})
        assert sent_form(pushover_mock)["user"] == "other"


def test_pushover_emergency_gets_retry_expire(client, pushover_mock):
    client.post("/1/messages.json", data={"token": TOKENS["pushover"], "message": "m", "priority": "2"})
    form = sent_form(pushover_mock)
    assert form["retry"] == "60" and form["expire"] == "3600"


def test_upstream_error_propagates(client, pushover_mock):
    pushover_mock.mock(return_value=httpx.Response(400, json={"status": 0, "errors": ["user invalid"]}))
    r = client.post("/1/messages.json", data={"token": TOKENS["pushover"], "message": "m"})
    assert r.status_code == 502
    assert "user invalid" in r.json()["errors"][0]


# ---- pipeline --------------------------------------------------------------


def test_dedup(pushover_mock):
    with make_client(base_config(kuma={"dedup_seconds": 60})) as c:
        for _ in range(3):
            c.post(f"/hooks/slack/kuma?token={TOKENS['slack']}", json={"text": "same"})
        assert pushover_mock.call_count == 1


def test_rate_limit(pushover_mock):
    with make_client(base_config(kuma={"rate_limit": "2/min"})) as c:
        for i in range(4):
            c.post(f"/hooks/slack/kuma?token={TOKENS['slack']}", json={"text": f"msg {i}"})
        assert pushover_mock.call_count == 2
        events = c.get("/api/events", headers={"X-PushMore-Token": TOKENS["admin"]}).json()
        assert [e["status"] for e in events][:2] == ["ratelimited", "ratelimited"]


def test_exclude_and_min_priority(pushover_mock):
    cfg = base_config(kuma={"exclude": ["^maintenance"]}, ha={"min_priority": 1})
    with make_client(cfg) as c:
        c.post(f"/hooks/slack/kuma?token={TOKENS['slack']}", json={"text": "Maintenance window"})
        c.post(f"/notify/{TOKENS['apprise']}", json={"body": "info", "type": "info"})
        c.post(f"/notify/{TOKENS['apprise']}", json={"body": "boom", "type": "failure"})
        assert pushover_mock.call_count == 1


def test_source_overrides(pushover_mock):
    cfg = base_config(
        kuma={"pushover_token": "a-kuma", "sound": "bike", "title_prefix": "[Kuma]", "priority": -1}
    )
    with make_client(cfg) as c:
        c.post(f"/hooks/slack/kuma?token={TOKENS['slack']}", json={"text": "x", "username": "Bot"})
        form = sent_form(pushover_mock)
        assert form["token"] == "a-kuma"
        assert form["sound"] == "bike"
        assert form["priority"] == "-1"


def test_dry_run(pushover_mock):
    cfg = base_config()
    cfg.dry_run = True
    with make_client(cfg) as c:
        r = c.post(f"/hooks/slack/kuma?token={TOKENS['slack']}", json={"text": "x"})
        assert r.text == "ok"
        assert not pushover_mock.called


def test_event_log_redacts_tokens(client, pushover_mock):
    client.post("/1/messages.json", data={"token": TOKENS["pushover"], "user": "secret-user", "message": "m"})
    events = client.get(
        "/api/events?payload=true", headers={"Authorization": f"Bearer {TOKENS['admin']}"}
    ).json()
    dumped = json.dumps(events)
    assert TOKENS["pushover"] not in dumped and "secret-user" not in dumped
