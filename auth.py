"""Multi-account token store for instagram-mcp.

A long-lived Instagram Graph API token is a bearer credential. It is stored in
the macOS Keychain (primary) or a chmod-600 file (fallback on non-macOS / when
the `security` CLI is unavailable), NEVER in the plaintext metadata file and
NEVER returned through a tool output.

Layout:
  ~/.claude/instagram-mcp/accounts.json   metadata ONLY: {label, ig_user_id,
                                           base_host, app_secret_ref, default}.
                                           chmod 600. No tokens.
  Keychain service "instagram-mcp",        token per account label; app secret
  account "<label>" / "<label>::secret"    under the ::secret suffix.

Default-account resolution precedence:
  1. INSTAGRAM_MCP_ACCESS_TOKEN env (+ INSTAGRAM_MCP_IG_USER_ID) -> ephemeral
     single account labelled "env" (zero-config / CI path).
  2. The account flagged default in accounts.json.
  3. The sole account, if exactly one exists.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import graph_client

ACCOUNTS_PATH = Path(os.environ.get("INSTAGRAM_MCP_ACCOUNTS_PATH") or
                     os.path.expanduser("~/.claude/instagram-mcp/accounts.json"))
TOKENS_FALLBACK_PATH = ACCOUNTS_PATH.parent / "tokens.json"  # chmod 600, fallback only
KEYCHAIN_SERVICE = "instagram-mcp"

ENV_TOKEN = "INSTAGRAM_MCP_ACCESS_TOKEN"
ENV_IG_USER_ID = "INSTAGRAM_MCP_IG_USER_ID"
ENV_BASE_HOST = "INSTAGRAM_MCP_BASE_HOST"
ENV_APP_SECRET = "INSTAGRAM_MCP_APP_SECRET"

_DEFAULT_BASE = "graph.facebook.com"


class AuthError(RuntimeError):
    """Raised when an account / token cannot be resolved. Maps to error_class=auth."""

    error_class = "auth"


@dataclass
class ResolvedAccount:
    label: str
    ig_user_id: str
    base_host: str
    access_token: str
    app_secret: str | None

    def client(self) -> graph_client.GraphClient:
        return graph_client.GraphClient(
            self.access_token, base_host=self.base_host, app_secret=self.app_secret
        )


# --------------------------------------------------------------------------- #
# Keychain (macOS) with chmod-600 file fallback
# --------------------------------------------------------------------------- #

def _keychain_available() -> bool:
    return sys.platform == "darwin" and shutil.which("security") is not None


def _keychain_set(account: str, secret: str) -> None:
    subprocess.run(
        ["security", "add-generic-password", "-U", "-a", account, "-s", KEYCHAIN_SERVICE, "-w", secret],
        check=True, capture_output=True, text=True,
    )


def _keychain_get(account: str) -> str | None:
    res = subprocess.run(
        ["security", "find-generic-password", "-a", account, "-s", KEYCHAIN_SERVICE, "-w"],
        capture_output=True, text=True,
    )
    if res.returncode != 0:
        return None
    return res.stdout.strip() or None


def _keychain_delete(account: str) -> None:
    subprocess.run(
        ["security", "delete-generic-password", "-a", account, "-s", KEYCHAIN_SERVICE],
        capture_output=True, text=True,
    )


def _fallback_load() -> dict[str, str]:
    if not TOKENS_FALLBACK_PATH.exists():
        return {}
    try:
        return json.loads(TOKENS_FALLBACK_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _fallback_save(data: dict[str, str]) -> None:
    TOKENS_FALLBACK_PATH.parent.mkdir(parents=True, exist_ok=True)
    TOKENS_FALLBACK_PATH.write_text(json.dumps(data), encoding="utf-8")
    os.chmod(TOKENS_FALLBACK_PATH, stat.S_IRUSR | stat.S_IWUSR)  # 0600


def _secret_set(account: str, secret: str) -> None:
    if _keychain_available():
        _keychain_set(account, secret)
        return
    data = _fallback_load()
    data[account] = secret
    _fallback_save(data)


def _secret_get(account: str) -> str | None:
    if _keychain_available():
        return _keychain_get(account)
    return _fallback_load().get(account)


def _secret_delete(account: str) -> None:
    if _keychain_available():
        _keychain_delete(account)
        return
    data = _fallback_load()
    if account in data:
        del data[account]
        _fallback_save(data)


def secret_backend() -> str:
    return "macos-keychain" if _keychain_available() else "chmod600-file"


# --------------------------------------------------------------------------- #
# Metadata file (NO tokens)
# --------------------------------------------------------------------------- #

def _load_meta() -> dict[str, Any]:
    if not ACCOUNTS_PATH.exists():
        return {"accounts": {}, "default": None}
    try:
        data = json.loads(ACCOUNTS_PATH.read_text(encoding="utf-8"))
        data.setdefault("accounts", {})
        data.setdefault("default", None)
        return data
    except Exception:
        return {"accounts": {}, "default": None}


def _save_meta(meta: dict[str, Any]) -> None:
    ACCOUNTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    ACCOUNTS_PATH.write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
    os.chmod(ACCOUNTS_PATH, stat.S_IRUSR | stat.S_IWUSR)  # 0600


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #

def list_accounts() -> list[dict[str, Any]]:
    """Account metadata only — never tokens.

    Includes the ephemeral env account when INSTAGRAM_MCP_ACCESS_TOKEN is set.
    """
    meta = _load_meta()
    out: list[dict[str, Any]] = []
    if os.environ.get(ENV_TOKEN):
        out.append({
            "label": "env",
            "ig_user_id": os.environ.get(ENV_IG_USER_ID),
            "base_host": os.environ.get(ENV_BASE_HOST, _DEFAULT_BASE),
            "source": "env",
            "default": meta.get("default") in (None, "env"),
        })
    for label, info in meta["accounts"].items():
        out.append({
            "label": label,
            "ig_user_id": info.get("ig_user_id"),
            "base_host": info.get("base_host", _DEFAULT_BASE),
            "source": "stored",
            "has_app_secret": bool(info.get("app_secret")),
            "default": meta.get("default") == label,
        })
    return out


def add_account(
    label: str,
    access_token: str,
    ig_user_id: str,
    *,
    base_host: str = _DEFAULT_BASE,
    app_secret: str | None = None,
    make_default: bool = False,
) -> dict[str, Any]:
    """Store an account's token in the secret backend + metadata in accounts.json."""
    if base_host not in graph_client.ALLOWED_HOSTS:
        raise AuthError(f"base_host must be one of {sorted(graph_client.ALLOWED_HOSTS)}")
    _secret_set(label, access_token)
    if app_secret:
        _secret_set(f"{label}::secret", app_secret)
    meta = _load_meta()
    meta["accounts"][label] = {
        "ig_user_id": ig_user_id,
        "base_host": base_host,
        "app_secret": bool(app_secret),
    }
    if make_default or meta.get("default") is None:
        meta["default"] = label
    _save_meta(meta)
    return {"label": label, "ig_user_id": ig_user_id, "base_host": base_host,
            "default": meta["default"] == label, "secret_backend": secret_backend()}


def remove_account(label: str) -> dict[str, Any]:
    meta = _load_meta()
    existed = label in meta["accounts"]
    _secret_delete(label)
    _secret_delete(f"{label}::secret")
    if existed:
        del meta["accounts"][label]
    if meta.get("default") == label:
        meta["default"] = next(iter(meta["accounts"]), None)
    _save_meta(meta)
    return {"label": label, "removed": existed, "default": meta.get("default")}


def set_default(label: str) -> dict[str, Any]:
    meta = _load_meta()
    if label not in meta["accounts"] and not (label == "env" and os.environ.get(ENV_TOKEN)):
        raise AuthError(f"unknown account {label!r}")
    meta["default"] = label
    _save_meta(meta)
    return {"default": label}


def resolve(label: str | None = None) -> ResolvedAccount:
    """Resolve label -> ResolvedAccount (with token). Internal use only."""
    meta = _load_meta()
    # Explicit env account, or default falling through to env.
    if label == "env" or (label is None and os.environ.get(ENV_TOKEN) and meta.get("default") in (None, "env")):
        token = os.environ.get(ENV_TOKEN)
        if not token:
            raise AuthError(f"{ENV_TOKEN} not set")
        uid = os.environ.get(ENV_IG_USER_ID)
        if not uid:
            raise AuthError(f"{ENV_IG_USER_ID} must be set alongside {ENV_TOKEN}")
        return ResolvedAccount("env", uid, os.environ.get(ENV_BASE_HOST, _DEFAULT_BASE),
                               token, os.environ.get(ENV_APP_SECRET))

    target = label or meta.get("default")
    if not target:
        if os.environ.get(ENV_TOKEN):
            return resolve("env")
        raise AuthError("no account configured — set INSTAGRAM_MCP_ACCESS_TOKEN or call add_account")
    info = meta["accounts"].get(target)
    if not info:
        raise AuthError(f"unknown account {target!r} — call list_accounts to see configured accounts")
    token = _secret_get(target)
    if not token:
        raise AuthError(f"no stored token for account {target!r} ({secret_backend()}) — re-run add_account")
    app_secret = _secret_get(f"{target}::secret") if info.get("app_secret") else None
    return ResolvedAccount(target, info["ig_user_id"], info.get("base_host", _DEFAULT_BASE),
                           token, app_secret)


def client_for(label: str | None = None) -> tuple[graph_client.GraphClient, ResolvedAccount]:
    acct = resolve(label)
    return acct.client(), acct
