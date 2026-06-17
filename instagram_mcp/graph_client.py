"""SSRF-safe Instagram Graph API client for instagram-mcp.

Two layers of egress safety:
  1. The client only ever talks to a FIXED allow-list of Meta hosts
     (graph.facebook.com / graph.instagram.com). An allow-list beats a blocklist
     for a fixed-upstream client — there is no code path that fetches a
     user-supplied host. (Media URLs handed to publish_* are fetched by
     Instagram, not by us.)
  2. assert_safe_host() is the general RFC1918/loopback/link-local/CGNAT/metadata
     blocklist (fail-closed DNS) per url-input-safety.md, exercised by the
     negative-control test and available if a future tool ever fetches a user URL.

Graph API errors are mapped to a stable `error_class` (oauth / rate_limited /
permission / not_found / invalid_param / ssrf_blocked / upstream_error) so the
audit taxonomy and the tool callers can branch without string-matching Meta's
prose. The raw token is never echoed: GraphAPIError messages pass through
audit.sanitize_error at the seam.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import os
import socket
from typing import Any
from urllib.parse import urlparse

import httpx

GRAPH_VERSION = os.environ.get("INSTAGRAM_MCP_GRAPH_VERSION", "v21.0")
DEFAULT_TIMEOUT = float(os.environ.get("INSTAGRAM_MCP_TIMEOUT", "30"))

# The ONLY hosts this client will ever contact.
ALLOWED_HOSTS: frozenset[str] = frozenset({"graph.facebook.com", "graph.instagram.com"})

# Cloud-metadata endpoints that are not caught by the private/link-local checks
# on every platform.
_METADATA_IPS: frozenset[str] = frozenset({"169.254.169.254", "fd00:ec2::254"})
_CGNAT = ipaddress.ip_network("100.64.0.0/10")


class GraphAPIError(RuntimeError):
    """A Graph API (or egress-safety) failure with a stable error_class."""

    def __init__(
        self,
        message: str,
        *,
        error_class: str = "upstream_error",
        code: int | None = None,
        subcode: int | None = None,
        type_: str | None = None,
        http_status: int | None = None,
    ) -> None:
        super().__init__(message)
        self.error_class = error_class
        self.code = code
        self.subcode = subcode
        self.type = type_
        self.http_status = http_status


def _ip_is_blocked(ip_str: str) -> bool:
    """True if an IP is private / loopback / link-local / reserved / CGNAT / metadata."""
    addr = ipaddress.ip_address(ip_str)
    if (
        addr.is_private
        or addr.is_loopback
        or addr.is_link_local
        or addr.is_reserved
        or addr.is_multicast
        or addr.is_unspecified
    ):
        return True
    if addr.version == 4 and addr in _CGNAT:
        return True
    return str(addr) in _METADATA_IPS


def assert_safe_host(url: str) -> str:
    """Resolve the URL's host and refuse any non-public address. Fail-closed DNS.

    Returns the hostname on success; raises GraphAPIError(error_class='ssrf_blocked'
    | 'validation' | 'network') otherwise. This is the general guard for arbitrary
    URLs — the GraphClient itself additionally pins to ALLOWED_HOSTS.
    """
    host = urlparse(url).hostname
    if not host:
        raise GraphAPIError(f"URL has no host: {url!r}", error_class="validation")
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as exc:
        # Fail closed: an unresolvable host is refused, never optimistically fetched.
        raise GraphAPIError(f"DNS resolution failed for host {host!r}", error_class="network") from exc
    for info in infos:
        ip_str = info[4][0]
        if _ip_is_blocked(ip_str):
            raise GraphAPIError(
                f"host {host!r} resolves to a non-public address ({ip_str}) — refused",
                error_class="ssrf_blocked",
            )
    return host


def _map_graph_error(payload: dict[str, Any], http_status: int) -> GraphAPIError:
    """Translate a Graph API error envelope into a typed GraphAPIError."""
    err = payload.get("error", {}) if isinstance(payload, dict) else {}
    message = err.get("message") or f"Graph API error (HTTP {http_status})"
    code = err.get("code")
    subcode = err.get("error_subcode")
    etype = err.get("type")

    error_class = "upstream_error"
    if code == 190 or etype == "OAuthException":
        error_class = "oauth"
    if code in {4, 17, 32, 613} or (isinstance(message, str) and "rate limit" in message.lower()):
        error_class = "rate_limited"
    if code in {10, 200, 803} or (isinstance(code, int) and 200 <= code <= 299):
        error_class = "permission"
    if code == 100:
        # 100 is "invalid parameter" UNLESS it's the missing-permission subcode family.
        error_class = "invalid_param"
    if http_status == 404:
        error_class = "not_found"

    return GraphAPIError(
        message if isinstance(message, str) else str(message),
        error_class=error_class,
        code=code if isinstance(code, int) else None,
        subcode=subcode if isinstance(subcode, int) else None,
        type_=etype if isinstance(etype, str) else None,
        http_status=http_status,
    )


class GraphClient:
    """Thin, SSRF-pinned wrapper over the Instagram Graph API.

    One client == one account token. Construct via auth.client_for(account) so the
    token is resolved from the keychain/env, never passed around by tool callers.
    """

    def __init__(
        self,
        access_token: str,
        *,
        base_host: str = "graph.facebook.com",
        app_secret: str | None = None,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        if base_host not in ALLOWED_HOSTS:
            raise GraphAPIError(
                f"base_host {base_host!r} is not in the Meta allow-list {sorted(ALLOWED_HOSTS)}",
                error_class="ssrf_blocked",
            )
        if not access_token:
            raise GraphAPIError("missing access token", error_class="oauth")
        self._token = access_token
        self._base = base_host
        self._app_secret = app_secret
        self._timeout = timeout

    def _appsecret_proof(self) -> str | None:
        """HMAC-SHA256(token) keyed by app_secret — Meta's recommended call hardening."""
        if not self._app_secret:
            return None
        return hmac.new(
            self._app_secret.encode("utf-8"),
            self._token.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    def _auth_params(self) -> dict[str, str]:
        params = {"access_token": self._token}
        proof = self._appsecret_proof()
        if proof:
            params["appsecret_proof"] = proof
        return params

    def _url(self, path: str) -> str:
        return f"https://{self._base}/{GRAPH_VERSION}/{path.lstrip('/')}"

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        url = self._url(path)
        # Defense in depth: even though path is internal, confirm the base host
        # never drifted out of the allow-list.
        if urlparse(url).hostname not in ALLOWED_HOSTS:
            raise GraphAPIError(f"refusing egress to non-Meta host in {url!r}", error_class="ssrf_blocked")
        query = dict(params or {})
        query.update(self._auth_params())
        try:
            with httpx.Client(timeout=self._timeout) as client:
                resp = client.request(method, url, params=query, data=data)
        except httpx.TimeoutException as exc:
            raise GraphAPIError(f"Graph API request timed out after {self._timeout}s", error_class="timeout") from exc
        except httpx.HTTPError as exc:
            raise GraphAPIError(f"Graph API transport error: {exc}", error_class="network") from exc

        try:
            body = resp.json()
        except ValueError:
            body = {}
        if resp.status_code >= 400 or (isinstance(body, dict) and "error" in body):
            raise _map_graph_error(body if isinstance(body, dict) else {}, resp.status_code)
        return body if isinstance(body, dict) else {"data": body}

    def get(self, path: str, **params: Any) -> dict[str, Any]:
        clean = {k: v for k, v in params.items() if v is not None}
        return self._request("GET", path, params=clean)

    def post(self, path: str, **data: Any) -> dict[str, Any]:
        clean = {k: v for k, v in data.items() if v is not None}
        return self._request("POST", path, data=clean)

    def delete(self, path: str, **params: Any) -> dict[str, Any]:
        clean = {k: v for k, v in params.items() if v is not None}
        return self._request("DELETE", path, params=clean)

    def get_paginated(
        self,
        path: str,
        *,
        limit: int = 25,
        max_items: int = 200,
        **params: Any,
    ) -> list[dict[str, Any]]:
        """Follow `paging.cursors`/`paging.next` until max_items collected.

        max_items is a hard ceiling so a tool can never walk an unbounded feed.
        """
        out: list[dict[str, Any]] = []
        page = self.get(path, limit=min(limit, max_items), **params)
        while True:
            data = page.get("data") or []
            out.extend(data)
            if len(out) >= max_items:
                return out[:max_items]
            after = (page.get("paging") or {}).get("cursors", {}).get("after")
            has_next = (page.get("paging") or {}).get("next")
            if not after or not has_next:
                return out
            page = self.get(path, limit=min(limit, max_items - len(out)), after=after, **params)
