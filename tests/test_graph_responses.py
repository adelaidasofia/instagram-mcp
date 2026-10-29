"""Graph response handling: no redirect following, non-JSON 2xx / 3xx rejected,
pagination loop guards, Graph version env validation.
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


def test_client_does_not_follow_redirects(monkeypatch):
    from instagram_mcp import graph_client
    calls = _fake_http(monkeypatch, [_Resp(200, {"id": "1"})])
    graph_client.GraphClient(TOKEN).get("1")
    assert calls[0]["client_kwargs"]["follow_redirects"] is False


def test_non_json_2xx_is_an_error(monkeypatch):
    from instagram_mcp import graph_client
    _fake_http(monkeypatch, [_Resp(200, json_error=True)])
    with pytest.raises(graph_client.GraphAPIError) as ei:
        graph_client.GraphClient(TOKEN).get("1")
    assert ei.value.error_class == "upstream_error"


def test_redirect_response_is_an_error(monkeypatch):
    from instagram_mcp import graph_client
    _fake_http(monkeypatch, [_Resp(302, json_error=True)])
    with pytest.raises(graph_client.GraphAPIError):
        graph_client.GraphClient(TOKEN).get("1")


def test_pagination_stops_on_repeated_cursor(monkeypatch):
    from instagram_mcp import graph_client
    page = {"data": [{"id": "a"}], "paging": {"cursors": {"after": "SAME"}, "next": "x"}}
    calls = _fake_http(monkeypatch, [_Resp(200, page)])
    out = graph_client.GraphClient(TOKEN).get_paginated("1/media", limit=1, max_items=500)
    assert len(out) <= 3
    assert len([c for c in calls if "method" in c]) <= 3


def test_pagination_stops_on_empty_page(monkeypatch):
    from instagram_mcp import graph_client
    first = _Resp(200, {"data": [{"id": "a"}], "paging": {"cursors": {"after": "c1"}, "next": "x"}})
    empty = _Resp(200, {"data": [], "paging": {"cursors": {"after": "c2"}, "next": "x"}})
    calls = _fake_http(monkeypatch, [first, empty])
    out = graph_client.GraphClient(TOKEN).get_paginated("1/media", limit=1, max_items=500)
    assert out == [{"id": "a"}]
    assert len([c for c in calls if "method" in c]) == 2


def test_pagination_tolerates_null_cursors(monkeypatch):
    from instagram_mcp import graph_client
    _fake_http(monkeypatch, [_Resp(200, {"data": [{"id": "a"}], "paging": {"cursors": None, "next": "x"}})])
    assert graph_client.GraphClient(TOKEN).get_paginated("1/media") == [{"id": "a"}]


@pytest.mark.parametrize("bad", ["v21.0/../../x", "21.0", "latest", "v21.0 x", "v1.2.3"])
def test_graph_version_env_is_validated(monkeypatch, bad):
    monkeypatch.setenv("INSTAGRAM_MCP_GRAPH_VERSION", bad)
    with pytest.raises(ValueError):
        from instagram_mcp import graph_client  # noqa: F401
