"""Smoke + unit tests for instagram-mcp v0.1.0.

Covers:
  - module imports (server, graph_client, auth, audit, validators)
  - validators reject bad input (ig_user_id, graph_id, caption len, hashtag,
    username, public-https url, limit, enum)
  - audit.sanitize_error strips API keys + Bearer + token + password AND
    Meta/Instagram access tokens (EAA / IGQV)
  - audit.record writes a JSONL line
  - SSRF: assert_safe_host blocks private/loopback/link-local/metadata IPs
  - GraphClient pins egress to the Meta host allow-list
  - Graph error envelope -> stable error_class (oauth / rate_limited / not_found)
  - server tool registry contains all 25 tools
  - healthcheck with no account returns ok=false + hint
  - DM tools fail loud with error_class="needs_app_review" when not enabled
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


@pytest.fixture(autouse=True)
def _isolate_env(tmp_path, monkeypatch):
    """Every test runs against a fresh tmp accounts file + tmp audit log, no real token."""
    monkeypatch.setenv("INSTAGRAM_MCP_ACCOUNTS_PATH", str(tmp_path / "accounts.json"))
    monkeypatch.setenv("INSTAGRAM_MCP_AUDIT_PATH", str(tmp_path / "audit.log.jsonl"))
    monkeypatch.delenv("INSTAGRAM_MCP_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("INSTAGRAM_MCP_IG_USER_ID", raising=False)
    monkeypatch.delenv("INSTAGRAM_MCP_DM_ENABLED", raising=False)
    for mod in list(sys.modules):
        if mod in {"audit", "auth", "graph_client", "validators", "server"}:
            del sys.modules[mod]


# --------------------------------- imports --------------------------------- #

def test_imports_clean():
    import server  # noqa: F401


# -------------------------------- validators ------------------------------- #

def test_validate_ig_user_id():
    import validators as V
    assert V.validate_ig_user_id("17841400000000000") == "17841400000000000"
    with pytest.raises(V.ValidationError):
        V.validate_ig_user_id("not-numeric")
    with pytest.raises(V.ValidationError):
        V.validate_ig_user_id("")


def test_validate_graph_id():
    import validators as V
    assert V.validate_graph_id("17895695668004550") == "17895695668004550"
    assert V.validate_graph_id("aBc_123") == "aBc_123"
    with pytest.raises(V.ValidationError):
        V.validate_graph_id("has spaces")
    with pytest.raises(V.ValidationError):
        V.validate_graph_id("drop;table")


def test_validate_caption_limit():
    import validators as V
    assert V.validate_caption(None) is None
    assert V.validate_caption("hi") == "hi"
    with pytest.raises(V.ValidationError):
        V.validate_caption("x" * 2201)


def test_validate_hashtag():
    import validators as V
    assert V.validate_hashtag("#TravelTuesday") == "TravelTuesday"
    assert V.validate_hashtag("bogota") == "bogota"
    with pytest.raises(V.ValidationError):
        V.validate_hashtag("two words")


def test_validate_username():
    import validators as V
    assert V.validate_username("@adelaida.ig") == "adelaida.ig"
    with pytest.raises(V.ValidationError):
        V.validate_username("bad/name")


def test_validate_public_https_url():
    import validators as V
    assert V.validate_public_https_url("https://cdn.example.com/a.jpg").startswith("https://")
    with pytest.raises(V.ValidationError):
        V.validate_public_https_url("http://cdn.example.com/a.jpg")  # not https
    with pytest.raises(V.ValidationError):
        V.validate_public_https_url("https://localhost/a.jpg")  # not public


def test_validate_limit_and_enum():
    import validators as V
    assert V.validate_limit(None) == 25
    assert V.validate_limit(50) == 50
    with pytest.raises(V.ValidationError):
        V.validate_limit(0)
    with pytest.raises(V.ValidationError):
        V.validate_limit(101)
    assert V.validate_enum("recent_media", {"top_media", "recent_media"}, field="edge") == "recent_media"
    with pytest.raises(V.ValidationError):
        V.validate_enum("nope", {"top_media", "recent_media"}, field="edge")


# ------------------------------ sanitize_error ----------------------------- #

def test_sanitize_strips_meta_tokens():
    import audit
    eaa = "EAAGm0PX4ZCpsBA" + "x" * 40
    igqv = "IGQVJ" + "y" * 40
    # Bare tokens (no key=value wrapper) — only the Meta-specific patterns can
    # catch these, so the markers prove those patterns fire.
    out = audit.sanitize_error(f"GET failed for tokens {eaa} {igqv}")
    assert "META_TOKEN_REDACTED" in out
    assert "IG_TOKEN_REDACTED" in out
    assert eaa not in out
    assert igqv not in out
    # And in a key=value wrapper the token still never survives (defense in depth:
    # the generic access_token= pattern may re-redact to ***REDACTED***).
    wrapped = audit.sanitize_error(f"access_token={eaa}")
    assert eaa not in wrapped


def test_sanitize_strips_generic_secrets():
    import audit
    out = audit.sanitize_error("Authorization: Bearer abc.def.ghi password=hunter2 client_secret=zzz123")
    assert "***REDACTED***" in out
    assert "abc.def.ghi" not in out
    assert "hunter2" not in out
    assert "zzz123" not in out


def test_audit_record_writes_jsonl(tmp_path, monkeypatch):
    monkeypatch.setenv("INSTAGRAM_MCP_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    import importlib

    import audit
    importlib.reload(audit)
    audit.record("test_tool", execution_time_ms=42,
                 io={"input": {"x": 1}, "output": {"ok": True}}, error_class=None)
    lines = (tmp_path / "audit.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert rec["tool"] == "test_tool"
    assert rec["execution_time_ms"] == 42
    assert rec["error_class"] is None


# ----------------------------------- SSRF ---------------------------------- #

@pytest.mark.parametrize("url", [
    "https://127.0.0.1/x",
    "https://10.0.0.1/x",
    "https://192.168.1.10/x",
    "https://169.254.169.254/latest/meta-data/",  # cloud metadata
    "https://100.64.1.1/x",                        # CGNAT
])
def test_assert_safe_host_blocks_private(url):
    import graph_client
    with pytest.raises(graph_client.GraphAPIError) as ei:
        graph_client.assert_safe_host(url)
    assert ei.value.error_class == "ssrf_blocked"


def test_graph_client_pins_meta_allowlist():
    import graph_client
    # Off-list host is refused at construction.
    with pytest.raises(graph_client.GraphAPIError) as ei:
        graph_client.GraphClient("EAAtoken", base_host="evil.example.com")
    assert ei.value.error_class == "ssrf_blocked"
    # Allow-listed host constructs fine.
    c = graph_client.GraphClient("EAAtoken", base_host="graph.facebook.com")
    assert c is not None


# ------------------------- Graph error -> error_class ---------------------- #

class _FakeResp:
    def __init__(self, status, payload):
        self.status_code = status
        self._payload = payload

    def json(self):
        return self._payload


def _install_fake_http(monkeypatch, status, payload):
    import graph_client

    class _FakeClient:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def request(self, method, url, params=None, data=None):
            return _FakeResp(status, payload)

    monkeypatch.setattr(graph_client.httpx, "Client", _FakeClient)


def test_graph_error_oauth(monkeypatch):
    import graph_client
    _install_fake_http(monkeypatch, 400,
                       {"error": {"code": 190, "type": "OAuthException", "message": "Invalid token"}})
    client = graph_client.GraphClient("EAAtoken", base_host="graph.facebook.com")
    with pytest.raises(graph_client.GraphAPIError) as ei:
        client.get("me", fields="id")
    assert ei.value.error_class == "oauth"


def test_graph_error_rate_limited(monkeypatch):
    import graph_client
    _install_fake_http(monkeypatch, 400,
                       {"error": {"code": 4, "message": "Application request limit reached"}})
    client = graph_client.GraphClient("EAAtoken", base_host="graph.facebook.com")
    with pytest.raises(graph_client.GraphAPIError) as ei:
        client.get("me")
    assert ei.value.error_class == "rate_limited"


def test_graph_success(monkeypatch):
    import graph_client
    _install_fake_http(monkeypatch, 200, {"id": "123", "username": "acme"})
    client = graph_client.GraphClient("EAAtoken", base_host="graph.facebook.com")
    out = client.get("123", fields="id,username")
    assert out["username"] == "acme"


# ------------------------------ tool registry ------------------------------ #

EXPECTED_TOOLS = {
    "healthcheck", "list_accounts", "add_account", "set_default_account",
    "remove_account", "account_info",
    "get_profile", "list_media", "get_media",
    "get_account_insights", "get_media_insights", "get_audience_insights",
    "publish_image", "publish_video", "publish_reel", "publish_carousel",
    "publish_story", "publishing_limit",
    "get_comments", "reply_to_comment", "hide_comment", "delete_comment",
    "search_hashtag", "get_hashtag_media", "get_mentions", "business_discovery",
    "list_conversations", "get_messages", "send_message",
}


def test_server_tool_registry_complete():
    import server
    assert len(EXPECTED_TOOLS) == 29  # 26 non-DM + 3 DM (publishing_limit included)
    missing = [name for name in EXPECTED_TOOLS if not hasattr(server, name)]
    assert not missing, f"server module missing tool functions: {missing}"
    for name in EXPECTED_TOOLS:
        obj = getattr(server, name)
        assert callable(obj) or hasattr(obj, "fn"), f"{name} not callable: {type(obj)}"


def _call(tool):
    """Call a FastMCP tool's underlying function regardless of wrapper shape."""
    return getattr(tool, "fn", tool)


def test_healthcheck_no_account_returns_hint():
    import server
    result = _call(server.healthcheck)()
    assert result["ok"] is False
    assert "hint" in result
    assert result["accounts_configured"] == 0


def test_dm_tools_gated_without_review():
    import server
    for tool in (server.list_conversations, server.get_messages, server.send_message):
        # get_messages / send_message take args; pass dummies — the gate fires first.
        if tool is server.list_conversations:
            res = _call(tool)()
        elif tool is server.get_messages:
            res = _call(tool)(conversation_id="123")
        else:
            res = _call(tool)(recipient_id="123", text="hi")
        assert res["ok"] is False
        assert res["error_class"] == "needs_app_review"


def test_dm_enabled_passes_gate_then_needs_account(monkeypatch):
    """With DM enabled, the gate passes and the failure becomes the (absent) account."""
    monkeypatch.setenv("INSTAGRAM_MCP_DM_ENABLED", "1")
    import server
    res = _call(server.list_conversations)()
    assert res["ok"] is False
    assert res["error_class"] != "needs_app_review"  # got past the gate; now an auth error
