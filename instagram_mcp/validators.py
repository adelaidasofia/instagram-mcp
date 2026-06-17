"""INPUT rail for instagram-mcp.

Every tool validates its inputs through this module BEFORE any Graph API call,
per the MCP Build Runbook §"INPUT rail" + llm-rails-taxonomy.md §INPUT stage.

Validation is deliberately strict + allocation-free: it raises ValidationError
(mapped to error_class="validation" by audit.classify_error) rather than letting
a malformed id reach graph.facebook.com and bounce back as an opaque OAuthException.
"""

from __future__ import annotations

import re
from typing import Any

# Instagram caption hard limit (Meta-documented).
CAPTION_MAX = 2200
# Instagram allows up to 30 hashtags per post; we cap-validate the count elsewhere.
HASHTAG_MAX_LEN = 100

_GRAPH_ID_RE = re.compile(r"^[0-9A-Za-z_]{1,64}$")
_NUMERIC_ID_RE = re.compile(r"^[0-9]{1,32}$")
_HASHTAG_RE = re.compile(r"^[0-9A-Za-z_]{1,100}$")
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9\-]{0,62}[a-z0-9]$")
_ACCOUNT_LABEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_\-.]{0,62}$")


class ValidationError(ValueError):
    """Raised when a tool input fails the INPUT rail. Maps to error_class=validation."""


def truncate(text: Any, limit: int = 200) -> str:
    """Shorten a string for the audit io field (never the real payload)."""
    s = str(text)
    return s if len(s) <= limit else s[: limit - 1] + "…"


def validate_ig_user_id(value: str, *, field: str = "ig_user_id") -> str:
    """An Instagram Business/Creator account id is a numeric Graph node id."""
    if value is None or not isinstance(value, str):
        raise ValidationError(f"{field} must be a string")
    v = value.strip()
    if not _NUMERIC_ID_RE.match(v):
        raise ValidationError(f"{field} must be a numeric Instagram account id (got {truncate(v, 40)!r})")
    return v


def validate_graph_id(value: str, *, field: str = "id") -> str:
    """A generic Graph object id (media / comment / container / page)."""
    if value is None or not isinstance(value, str):
        raise ValidationError(f"{field} must be a string")
    v = value.strip()
    if not _GRAPH_ID_RE.match(v):
        raise ValidationError(f"{field} must be a Graph object id (alphanumeric/underscore, <=64; got {truncate(v, 40)!r})")
    return v


def validate_caption(value: str | None, *, field: str = "caption") -> str | None:
    """Instagram caption: <= 2200 chars. None passes through (caption is optional)."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValidationError(f"{field} must be a string")
    if len(value) > CAPTION_MAX:
        raise ValidationError(f"{field} exceeds Instagram's {CAPTION_MAX}-char limit (got {len(value)})")
    return value


def validate_hashtag(value: str, *, field: str = "hashtag") -> str:
    """A hashtag query: leading '#' optional, alphanumeric/underscore, <=100 chars."""
    if value is None or not isinstance(value, str):
        raise ValidationError(f"{field} must be a string")
    v = value.strip().lstrip("#")
    if not _HASHTAG_RE.match(v):
        raise ValidationError(f"{field} must be alphanumeric/underscore, <=100 chars, no spaces (got {truncate(v, 40)!r})")
    return v


def validate_username(value: str, *, field: str = "username") -> str:
    """An Instagram username for business_discovery: 1-30 chars, IG's charset."""
    if value is None or not isinstance(value, str):
        raise ValidationError(f"{field} must be a string")
    v = value.strip().lstrip("@")
    if not re.match(r"^[A-Za-z0-9._]{1,30}$", v):
        raise ValidationError(f"{field} must be a valid Instagram username (<=30, letters/digits/._; got {truncate(v, 40)!r})")
    return v


def validate_slug(value: str, *, field: str = "slug") -> str:
    """A lowercase kebab slug for account labels / saved-config keys."""
    if value is None or not isinstance(value, str):
        raise ValidationError(f"{field} must be a string")
    v = value.strip().lower()
    if not _SLUG_RE.match(v):
        raise ValidationError(f"{field} must be a lowercase kebab slug (3-64 chars; got {truncate(v, 40)!r})")
    return v


def validate_account_label(value: str, *, field: str = "account") -> str:
    """A human-friendly account label key (e.g. 'onde', 'mycelium', 'client-acme')."""
    if value is None or not isinstance(value, str):
        raise ValidationError(f"{field} must be a string")
    v = value.strip()
    if not _ACCOUNT_LABEL_RE.match(v):
        raise ValidationError(f"{field} must be 1-63 chars of letters/digits/._- (got {truncate(v, 40)!r})")
    return v


def validate_public_https_url(value: str, *, field: str = "media_url") -> str:
    """A media URL handed to Instagram for ingestion.

    Instagram (not this MCP) fetches the bytes, so the SSRF blast radius is on
    Meta's side; we still enforce https + reject obvious non-public hosts so a
    malformed/localhost URL fails fast at the INPUT rail instead of as an opaque
    container error. The authoritative SSRF guard for URLs WE fetch lives in
    graph_client.assert_safe_host().
    """
    if value is None or not isinstance(value, str):
        raise ValidationError(f"{field} must be a string")
    v = value.strip()
    if not v.lower().startswith("https://"):
        raise ValidationError(f"{field} must be an https:// URL (Instagram refuses non-https media)")
    host = re.sub(r"^https://", "", v, flags=re.I).split("/", 1)[0].split(":", 1)[0].lower()
    if host in {"localhost", "127.0.0.1", "0.0.0.0", "::1"} or host.endswith(".local"):
        raise ValidationError(f"{field} host {host!r} is not publicly reachable; Instagram cannot fetch it")
    return v


def validate_limit(value: int | None, *, field: str = "limit", default: int = 25, ceiling: int = 100) -> int:
    """Pagination page size: 1..ceiling, default when None."""
    if value is None:
        return default
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValidationError(f"{field} must be an integer")
    if value < 1 or value > ceiling:
        raise ValidationError(f"{field} must be between 1 and {ceiling}")
    return value


def validate_enum(value: str | None, allowed: set[str], *, field: str, default: str | None = None) -> str | None:
    """Validate a string against a fixed allow-list (e.g. media_type, metric period)."""
    if value is None:
        return default
    if not isinstance(value, str):
        raise ValidationError(f"{field} must be a string")
    v = value.strip().lower()
    if v not in allowed:
        raise ValidationError(f"{field} must be one of {sorted(allowed)} (got {truncate(v, 40)!r})")
    return v
