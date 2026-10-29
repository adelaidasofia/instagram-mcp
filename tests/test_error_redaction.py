"""Errors must not carry secrets: httpx transport errors (which can embed the
request URL, incl. access_token) and keychain CalledProcessError (argv holds the secret).
"""

from __future__ import annotations

import subprocess
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


def test_transport_error_message_has_no_url(monkeypatch):
    import httpx

    from instagram_mcp import graph_client

    class _Boom:
        def __init__(self, *a, **k): ...
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def request(self, *a, **k):
            raise httpx.ConnectError("failed for https://graph.facebook.com/x?foo=SECRETVALUE")

    monkeypatch.setattr(graph_client.httpx, "Client", _Boom)
    with pytest.raises(graph_client.GraphAPIError) as ei:
        graph_client.GraphClient(TOKEN).get("1")
    assert "SECRETVALUE" not in str(ei.value)
    assert ei.value.error_class == "network"


def test_keychain_failure_does_not_leak_secret(monkeypatch):
    from instagram_mcp import auth

    def _fail(cmd, **kw):
        raise subprocess.CalledProcessError(1, cmd)

    monkeypatch.setattr(auth.subprocess, "run", _fail)
    with pytest.raises(auth.AuthError) as ei:
        auth._keychain_set("acme", TOKEN)
    assert TOKEN not in str(ei.value)
    assert ei.value.__cause__ is None and ei.value.__suppress_context__
