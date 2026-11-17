"""Media URL validation and the egress IP blocklist (IPv4-mapped IPv6, scope ids, garbage).
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


@pytest.mark.parametrize("ip", ["::ffff:10.0.0.1", "::ffff:127.0.0.1", "::ffff:169.254.169.254",
                                "fe80::1%eth0", "not-an-ip"])
def test_ip_blocklist_covers_mapped_scoped_and_garbage(ip):
    from instagram_mcp import graph_client
    assert graph_client._ip_is_blocked(ip) is True


def test_ip_blocklist_allows_public():
    from instagram_mcp import graph_client
    assert graph_client._ip_is_blocked("8.8.8.8") is False


@pytest.mark.parametrize("url", [
    "https://good.example.com@127.0.0.1/x.jpg",
    "https://user:pw@example.com/x.jpg",
    "https://10.0.0.5/x.jpg",
    "https://192.168.0.1/x.jpg",
    "https://169.254.169.254/latest/meta-data/",
    "https://[::1]/x.jpg",
    "https://localhost./x.jpg",
    "https://foo.internal/x.jpg",
    "https://example.com/a b.jpg",
    "https://example.com/x.jpg\r\nHost: evil",
    "https://example.com:notaport/x.jpg",
    "https:///nohost.jpg",
])
def test_media_url_rejects_unsafe(url):
    from instagram_mcp import validators as V
    with pytest.raises(V.ValidationError):
        V.validate_public_https_url(url)


def test_media_url_accepts_normal():
    from instagram_mcp import validators as V
    assert V.validate_public_https_url("https://cdn.example.com/a/b.jpg?x=1") == "https://cdn.example.com/a/b.jpg?x=1"
    assert V.validate_public_https_url("https://93.184.216.34:8443/x.jpg")
