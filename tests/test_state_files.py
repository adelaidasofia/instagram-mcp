"""State files: a corrupt accounts/token file is an error (never silently replaced
with an empty one), and files are written atomically with 0600 from creation.
"""

from __future__ import annotations

import os
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


def test_corrupt_accounts_file_is_not_silently_overwritten(tmp_path):
    from instagram_mcp import auth
    auth.ACCOUNTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    auth.ACCOUNTS_PATH.write_text("{ not json", encoding="utf-8")
    with pytest.raises(auth.AuthError):
        auth.add_account("acme", TOKEN, "123")
    assert auth.ACCOUNTS_PATH.read_text(encoding="utf-8") == "{ not json"  # untouched


def test_corrupt_token_fallback_is_not_silently_overwritten():
    from instagram_mcp import auth
    auth.TOKENS_FALLBACK_PATH.parent.mkdir(parents=True, exist_ok=True)
    auth.TOKENS_FALLBACK_PATH.write_text("garbage", encoding="utf-8")
    if auth._keychain_available():
        pytest.skip("keychain backend in use")
    with pytest.raises(auth.AuthError):
        auth.add_account("acme", TOKEN, "123")
    assert auth.TOKENS_FALLBACK_PATH.read_text(encoding="utf-8") == "garbage"


@pytest.mark.skipif(os.name == "nt", reason="POSIX file modes")
def test_state_files_are_created_0600():
    from instagram_mcp import audit, auth
    if auth._keychain_available():
        pytest.skip("keychain backend in use")
    auth.add_account("acme", TOKEN, "123")
    audit.record("t", execution_time_ms=1, io={})
    for p in (auth.ACCOUNTS_PATH, auth.TOKENS_FALLBACK_PATH, audit.AUDIT_PATH):
        assert (p.stat().st_mode & 0o777) == 0o600, p


def test_add_account_roundtrip_and_no_tmp_left_behind():
    from instagram_mcp import auth
    if auth._keychain_available():
        pytest.skip("keychain backend in use")
    auth.add_account("acme", TOKEN, "123")
    assert auth.resolve("acme").access_token == TOKEN
    assert not list(auth.ACCOUNTS_PATH.parent.glob("*.tmp"))
    assert TOKEN not in auth.ACCOUNTS_PATH.read_text(encoding="utf-8")
