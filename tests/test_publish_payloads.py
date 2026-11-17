"""Publish flow fails loud on a missing container/child id; DM payloads are real JSON.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

TOKEN = "EAAsecrettokensecrettoken123456"


@pytest.fixture(autouse=True)
def _isolate_env(tmp_path, monkeypatch):
    monkeypatch.setenv("INSTAGRAM_MCP_ACCOUNTS_PATH", str(tmp_path / "accounts.json"))
    monkeypatch.setenv("INSTAGRAM_MCP_AUDIT_PATH", str(tmp_path / "audit.log.jsonl"))
    monkeypatch.delenv("INSTAGRAM_MCP_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("INSTAGRAM_MCP_GRAPH_VERSION", raising=False)
    for mod in list(sys.modules):
        if mod.startswith("instagram_mcp"):
            del sys.modules[mod]


class _Resp:
    def __init__(self, status=200, payload=None, json_error=False):
        self.status_code = status
        self._payload = payload
        self._json_error = json_error

    def json(self):
        if self._json_error:
            raise ValueError("not json")
        return self._payload


def _fake_http(monkeypatch, responses):
    """Install a fake httpx.Client; returns the list that records every request."""
    from instagram_mcp import graph_client

    calls: list[dict] = []
    queue = list(responses)

    class _Client:
        def __init__(self, *a, **k):
            calls.append({"client_kwargs": k})

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def request(self, method, url, params=None, data=None, headers=None):
            calls.append({"method": method, "url": url, "params": params or {},
                          "data": data or {}, "headers": headers or {}})
            return queue.pop(0) if len(queue) > 1 else queue[0]

    monkeypatch.setattr(graph_client.httpx, "Client", _Client)
    return calls


def test_send_message_payload_is_valid_json(monkeypatch):
    import json

    from instagram_mcp import auth, server

    monkeypatch.setenv("INSTAGRAM_MCP_DM_ENABLED", "1")
    monkeypatch.setenv("INSTAGRAM_MCP_ACCESS_TOKEN", TOKEN)
    monkeypatch.setenv("INSTAGRAM_MCP_IG_USER_ID", "123")
    calls = _fake_http(monkeypatch, [_Resp(200, {"message_id": "m"})])
    msg = 'he said "hi" \\ and \n newline'
    res = server.send_message("456", msg)
    assert res["ok"] is True
    data = [c for c in calls if "method" in c][0]["data"]
    assert json.loads(data["recipient"]) == {"id": "456"}
    assert json.loads(data["message"]) == {"text": msg}
    assert auth  # silence unused

@pytest.mark.parametrize("cid", [None, ""])
def test_publish_container_without_id_does_not_publish(monkeypatch, cid):
    from instagram_mcp import graph_client, server
    calls = _fake_http(monkeypatch, [_Resp(200, {"id": "m"})])
    with pytest.raises(graph_client.GraphAPIError) as ei:
        server._publish_container(graph_client.GraphClient(TOKEN), "123", cid)
    assert ei.value.error_class == "upstream_error"
    assert not [c for c in calls if "method" in c]  # media_publish never called
