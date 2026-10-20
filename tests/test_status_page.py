import os
import re

import httpx
import yaml
from fastapi.testclient import TestClient

from pushmore.app import create_app
from pushmore.config import parse_config
from pushmore.status import source_rows

from .conftest import TOKENS, base_config, make_client


def rows_by_key(src):
    return {r.key: r for r in source_rows(src)}


def test_page_renders_all_sources(client):
    r = client.get("/")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    assert r.headers["cache-control"] == "no-store"
    for sid in ("gh", "ci", "kuma", "misc", "ha", "lib", "legacy"):
        assert f"<h3>{sid}</h3>" in r.text


def test_secrets_are_masked(client):
    text = client.get("/").text
    for token in TOKENS.values():
        assert token not in text
    assert "u-real" not in text and "a-real" not in text
    assert "gh-secret" not in text
    assert "••••6789" in text  # last 4 of a token


def test_defaults_marked_and_explicit_not():
    cfg = base_config(kuma={"rate_limit": "5/min", "sound": "bike"})
    rows = rows_by_key(cfg.sources["kuma"])
    assert rows["rate_limit"].value == "5/min" and not rows["rate_limit"].is_default
    assert rows["sound"].value == "bike" and not rows["sound"].is_default
    assert rows["danger_priority"].value == "1" and rows["danger_priority"].is_default
    assert rows["dedup_seconds"].is_default and rows["dedup_seconds"].value == "0 (off)"
    assert rows["pushover_token"].is_default and rows["pushover_token"].value == "pushover.app_token"
    assert rows["token"].secret and not rows["token"].is_default


def test_explicit_value_equal_to_default_is_not_marked_default():
    cfg = base_config(kuma={"danger_priority": 1, "enabled": True})
    rows = rows_by_key(cfg.sources["kuma"])
    assert not rows["danger_priority"].is_default
    assert not rows["enabled"].is_default


def test_type_priority_marks_each_level():
    cfg = base_config(ha={"type_priority": {"info": -1}})
    row = rows_by_key(cfg.sources["ha"])["type_priority"]
    assert "info: -1," in row.value
    assert "failure: 1 (default)" in row.value
    assert not row.is_default


def test_type_specific_keys_only():
    cfg = base_config()
    gh = rows_by_key(cfg.sources["gh"])
    assert "events" in gh and "only_failures" in gh and "token" not in gh
    assert gh["events"].value.startswith("all: push")
    kuma = rows_by_key(cfg.sources["kuma"])
    assert "events" not in kuma and "type_priority" not in kuma


def test_global_defaults():
    cfg = parse_config(
        {"pushover": {"user_key": "u" * 30, "app_token": "a" * 30}, "server": {"dry_run": True}}
    )
    with make_client(cfg) as c:
        text = c.get("/").text
    assert "Dry run" in text
    m = re.search(r"pushover\.api_url</td>\s*<td class=\"v\">([^<]+)<span class=\"tag\">default", text)
    assert m and m.group(1) == "https://api.pushover.net/1/messages.json"
    assert re.search(r"server\.dry_run</td>\s*<td class=\"v\">true</td>", text)


def test_html_is_escaped(pushover_mock):
    cfg = base_config(kuma={"title_prefix": "<script>alert(1)</script>"})
    with make_client(cfg) as c:
        text = c.get("/").text
    assert "<script>alert(1)</script>" not in text
    assert "&lt;script&gt;" in text


def test_reload_error_banner_and_recovery(tmp_path, pushover_mock):
    path = tmp_path / "config.yaml"
    good = {
        "pushover": {"user_key": "u", "app_token": "a"},
        "sources": {"kuma": {"type": "slack", "token": TOKENS["slack"]}},
    }
    path.write_text(yaml.safe_dump(good))
    app = create_app(config_file=path, http_client=httpx.AsyncClient(), reload_interval=3600)
    with TestClient(app) as c:
        assert "reload failed" not in c.get("/").text
        assert str(path) in c.get("/").text

        path.write_text("pushover: [broken")
        st = path.stat()
        os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 2_000_000_000))
        c.app.state.watcher.check()
        text = c.get("/").text
        assert "Last config reload failed" in text and "Invalid YAML" in text
        assert "<h3>kuma</h3>" in text  # old config still shown

        good["sources"]["kuma"]["sound"] = "bike"
        path.write_text(yaml.safe_dump(good))
        st = path.stat()
        os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 4_000_000_000))
        assert c.app.state.watcher.check()
        text = c.get("/").text
        assert "reload failed" not in text and "bike" in text
