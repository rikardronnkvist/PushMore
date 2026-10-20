import logging
import os

import httpx
import yaml
from fastapi.testclient import TestClient

from pushmore.app import create_app

from .conftest import sent_form

TOKEN_A = "kuma-token-0123456789"
TOKEN_B = "new-token-0123456789ab"


def write(path, sources, **pushover):
    cfg = {"pushover": {"user_key": "u-real", "app_token": "a-real", **pushover}, "sources": sources}
    path.write_text(yaml.safe_dump(cfg))
    # Make sure the mtime differs even on filesystems with coarse timestamps.
    st = path.stat()
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))


def client_for(path):
    app = create_app(config_file=path, http_client=httpx.AsyncClient(), reload_interval=3600)
    return TestClient(app)


def slack(c, token, text="x", source="kuma"):
    return c.post(f"/hooks/slack/{source}?token={token}", json={"text": text})


def test_reload_adds_and_removes_sources(tmp_path, pushover_mock, caplog):
    caplog.set_level(logging.INFO, logger="pushmore")
    path = tmp_path / "config.yaml"
    write(path, {"kuma": {"type": "slack", "token": TOKEN_A}})
    with client_for(path) as c:
        watcher = c.app.state.watcher
        assert slack(c, TOKEN_A).status_code == 200
        assert slack(c, TOKEN_B, source="other").status_code == 401

        write(path, {"other": {"type": "slack", "token": TOKEN_B}})
        assert watcher.check() is True
        assert "added=other removed=kuma" in caplog.text

        assert slack(c, TOKEN_A).status_code == 401
        assert slack(c, TOKEN_B, source="other").status_code == 200


def test_reload_changes_pushover_credentials(tmp_path, pushover_mock):
    path = tmp_path / "config.yaml"
    write(path, {"kuma": {"type": "slack", "token": TOKEN_A}})
    with client_for(path) as c:
        write(path, {"kuma": {"type": "slack", "token": TOKEN_A}}, app_token="a-new")
        assert c.app.state.watcher.check() is True
        slack(c, TOKEN_A)
        assert sent_form(pushover_mock)["token"] == "a-new"


def test_invalid_config_keeps_running_config(tmp_path, pushover_mock, caplog):
    path = tmp_path / "config.yaml"
    write(path, {"kuma": {"type": "slack", "token": TOKEN_A}})
    with client_for(path) as c:
        watcher = c.app.state.watcher

        path.write_text("pushover: [this is: not valid")
        os.utime(path, ns=(path.stat().st_atime_ns, path.stat().st_mtime_ns + 2_000_000_000))
        assert watcher.check() is False
        assert "Config reload failed" in caplog.text
        assert slack(c, TOKEN_A).status_code == 200

        # Valid YAML but invalid config (duplicate tokens) is rejected too
        write(path, {"a": {"type": "slack", "token": TOKEN_A}, "b": {"type": "apprise", "token": TOKEN_A}})
        assert watcher.check() is False
        assert slack(c, TOKEN_A).status_code == 200

        # Broken file is reported once, not on every poll
        caplog.clear()
        assert watcher.check() is False
        assert "Config reload failed" not in caplog.text


def test_touch_without_change_is_noop(tmp_path, pushover_mock, caplog):
    caplog.set_level(logging.INFO, logger="pushmore")
    path = tmp_path / "config.yaml"
    write(path, {"kuma": {"type": "slack", "token": TOKEN_A}})
    with client_for(path) as c:
        os.utime(path, ns=(path.stat().st_atime_ns, path.stat().st_mtime_ns + 3_000_000_000))
        assert c.app.state.watcher.check() is False
        assert "Config reloaded" not in caplog.text


def test_missing_file_keeps_config(tmp_path, pushover_mock):
    path = tmp_path / "config.yaml"
    write(path, {"kuma": {"type": "slack", "token": TOKEN_A}})
    with client_for(path) as c:
        path.unlink()
        assert c.app.state.watcher.check() is False
        assert slack(c, TOKEN_A).status_code == 200
        # Comes back unchanged: nothing to apply
        write(path, {"kuma": {"type": "slack", "token": TOKEN_A}})
        assert c.app.state.watcher.check() is False


def test_rate_limit_state_survives_reload(tmp_path, pushover_mock):
    path = tmp_path / "config.yaml"
    write(path, {"kuma": {"type": "slack", "token": TOKEN_A, "rate_limit": "2/min"}})
    with client_for(path) as c:
        slack(c, TOKEN_A, "1")
        slack(c, TOKEN_A, "2")
        write(path, {"kuma": {"type": "slack", "token": TOKEN_A, "rate_limit": "2/min", "sound": "bike"}})
        assert c.app.state.watcher.check() is True
        slack(c, TOKEN_A, "3")
        assert pushover_mock.call_count == 2


def test_no_watcher_for_fixed_config_or_zero_interval(tmp_path, pushover_mock):
    path = tmp_path / "config.yaml"
    write(path, {"kuma": {"type": "slack", "token": TOKEN_A}})
    app = create_app(config_file=path, http_client=httpx.AsyncClient(), reload_interval=0)
    with TestClient(app) as c:
        assert not hasattr(c.app.state, "watcher")
