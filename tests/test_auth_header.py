"""The access token is sent as an Authorization: Bearer header, never in the URL/params.
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


def test_token_sent_in_header_not_url_or_params(monkeypatch):
    from instagram_mcp import graph_client
    calls = _fake_http(monkeypatch, [_Resp(200, {"id": "1"})])
    c = graph_client.GraphClient(TOKEN, app_secret="appsecret")
    c.get("123", fields="id")
    c.post("123/media", caption="hi")
    reqs = [x for x in calls if "method" in x]
    assert len(reqs) == 2
    for r in reqs:
        assert r["headers"]["Authorization"] == f"Bearer {TOKEN}"
        assert TOKEN not in r["url"]
        assert "access_token" not in r["params"]
        assert "access_token" not in r["data"]
        assert TOKEN not in str(r["params"]) + str(r["data"])
        assert "appsecret_proof" in r["params"]  # proof still sent, as a param
