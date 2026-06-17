"""4-field observability + sanitize_error for instagram-mcp.

Per MCP Build Runbook §"Per-call observability — 4-field schema":
  execution_time_ms : int
  io                : dict {"input": ..., "output": ...}
  token_usage       : dict {"prompt", "completion", "cache_read", "cache_creation"}
  error_class       : str | None

Plus sanitize_error() per url-input-safety.md §"sanitize_error strip patterns":
  strips api keys / tokens / Bearer headers / passwords AND Meta/Instagram
  access tokens (EAA... / IGQV...) from any string crossing the user-visible /
  model-visible boundary.

Append-only JSONL at $HOME/.claude/instagram-mcp/audit.log.jsonl. One line per call.

This MCP makes NO LLM calls (pure Graph API wrapper) so token_usage is always
the zero-vector; the field is retained for cross-MCP audit-schema parity.
"""

from __future__ import annotations

import json
import os
import re
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

AUDIT_PATH = Path(os.environ.get("INSTAGRAM_MCP_AUDIT_PATH") or
                  os.path.expanduser("~/.claude/instagram-mcp/audit.log.jsonl"))

# Patterns scrubbed from any string before it crosses the seam.
# Ordered most-specific -> least-specific so longer prefixes shadow shorter ones.
# The Meta/Instagram token patterns (EAA / IGQV) are the load-bearing additions
# vs the shared base scrubber: a Graph API error body can echo the access token.
_SCRUB_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"EAA[A-Za-z0-9]{20,}"), "EAA***META_TOKEN_REDACTED***"),
    (re.compile(r"IGQV[A-Za-z0-9_\-]{20,}"), "IGQV***IG_TOKEN_REDACTED***"),
    (re.compile(r"sk-ant-[A-Za-z0-9_\-]{20,}"), "sk-ant-***REDACTED***"),
    (re.compile(r"sk-proj-[A-Za-z0-9_\-]{20,}"), "sk-proj-***REDACTED***"),
    (re.compile(r"ghp_[A-Za-z0-9_]{20,}"), "ghp_***REDACTED***"),
    (re.compile(r"github_pat_[A-Za-z0-9_]{20,}"), "github_pat_***REDACTED***"),
    (re.compile(r"AKIA[0-9A-Z]{16}"), "AKIA***REDACTED***"),
    (re.compile(r"npm_[A-Za-z0-9]{30,}"), "npm_***REDACTED***"),
    (re.compile(r"sk_live_[0-9a-zA-Z]{20,}"), "sk_live_***REDACTED***"),
    (re.compile(r"(?i)\baccess_token\s*[:=]\s*\S+"), "access_token=***REDACTED***"),
    (re.compile(r"(?i)\bclient_secret\s*[:=]\s*\S+"), "client_secret=***REDACTED***"),
    (re.compile(r"(?i)\bappsecret_proof\s*[:=]\s*\S+"), "appsecret_proof=***REDACTED***"),
    (re.compile(r"(?i)\bBearer\s+[A-Za-z0-9_\-.=]+"), "Bearer ***REDACTED***"),
    (re.compile(r"(?i)\bAuthorization\s*[:=]\s*\S+"), "Authorization: ***REDACTED***"),
    (re.compile(r"(?i)\bX-Api-Key\s*[:=]\s*\S+"), "X-Api-Key: ***REDACTED***"),
    (re.compile(r"(?i)\bapi[_\-]?key\s*[:=]\s*\S+"), "api_key=***REDACTED***"),
    (re.compile(r"(?i)\btoken\s*[:=]\s*\S+"), "token=***REDACTED***"),
    (re.compile(r"(?i)\bsecret\s*[:=]\s*\S+"), "secret=***REDACTED***"),
    (re.compile(r"(?i)\bpassword\s*[:=]\s*\S+"), "password=***REDACTED***"),
]


def sanitize_error(text: Any) -> str:
    """Strip credentials + Meta/IG access tokens from any string before emitting."""
    if not isinstance(text, str):
        text = str(text)
    cleaned = text
    for pattern, replacement in _SCRUB_PATTERNS:
        cleaned = pattern.sub(replacement, cleaned)
    return cleaned


def sanitize_payload(obj: Any) -> Any:
    """Recursively sanitize a dict/list/str payload for the audit io field."""
    if isinstance(obj, dict):
        return {k: sanitize_payload(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [sanitize_payload(v) for v in obj]
    if isinstance(obj, str):
        return sanitize_error(obj)
    return obj


def classify_error(exc: BaseException) -> str:
    """Map an exception to a stable error_class for downstream taxonomy.

    GraphAPIError (graph_client.py) carries an `error_class` attribute already
    resolved from the Graph API error payload (oauth / rate_limited / permission
    / not_found / upstream_error); honor it verbatim when present.
    """
    explicit = getattr(exc, "error_class", None)
    if isinstance(explicit, str) and explicit:
        return explicit
    name = type(exc).__name__
    if name in {"ValidationError", "ValueError"}:
        return "validation"
    if name in {"TimeoutError", "ReadTimeout", "ConnectTimeout", "PoolTimeout"}:
        return "timeout"
    if name in {"ConnectError", "ConnectionError"}:
        return "network"
    if name in {"PermissionError", "OSError", "FileNotFoundError", "IsADirectoryError"}:
        return "filesystem"
    if name == "RuntimeError" and "auth" in str(exc).lower():
        return "auth"
    if name == "RuntimeError":
        return "internal_error"
    return "upstream_error"


def _ensure_dir() -> None:
    AUDIT_PATH.parent.mkdir(parents=True, exist_ok=True)


def record(
    tool: str,
    *,
    execution_time_ms: int,
    io: dict[str, Any],
    token_usage: dict[str, int] | None = None,
    error_class: str | None = None,
    extra: dict[str, Any] | None = None,
) -> None:
    """Append a JSONL line. Best-effort: never raises into the caller."""
    payload = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime()),
        "tool": tool,
        "execution_time_ms": int(execution_time_ms),
        "io": sanitize_payload(io),
        "token_usage": token_usage or {
            "prompt": 0, "completion": 0, "cache_read": 0, "cache_creation": 0,
        },
        "error_class": error_class,
    }
    if extra:
        payload["extra"] = sanitize_payload(extra)
    try:
        _ensure_dir()
        with AUDIT_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False) + "\n")
    except Exception:
        # Audit must never break the tool call. Silent failure here is acceptable.
        pass


@contextmanager
def timed(tool: str, *, input_payload: dict[str, Any]) -> Iterator[dict[str, Any]]:
    """Context manager that records an audit line on __exit__.

    Usage:
        with timed("list_media", input_payload={...}) as ctx:
            result = ...
            ctx["output"] = result   # logged (sanitized)
            ctx["extra"] = {...}      # optional
    """
    start = time.perf_counter()
    ctx: dict[str, Any] = {
        "output": None,
        "token_usage": None,
        "extra": None,
        "error_class": None,
    }
    try:
        yield ctx
    except BaseException as exc:
        ctx["error_class"] = classify_error(exc)
        ctx["output"] = {"error": sanitize_error(str(exc))}
        elapsed_ms = int((time.perf_counter() - start) * 1000)
        record(
            tool,
            execution_time_ms=elapsed_ms,
            io={"input": input_payload, "output": ctx["output"]},
            token_usage=ctx["token_usage"],
            error_class=ctx["error_class"],
            extra=ctx["extra"],
        )
        raise
    elapsed_ms = int((time.perf_counter() - start) * 1000)
    record(
        tool,
        execution_time_ms=elapsed_ms,
        io={"input": input_payload, "output": ctx["output"]},
        token_usage=ctx["token_usage"],
        error_class=ctx["error_class"],
        extra=ctx["extra"],
    )
