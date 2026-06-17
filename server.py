"""instagram-mcp v0.1.0 — official Instagram Graph API MCP.

A self-contained, multi-account wrapper over the OFFICIAL Instagram Graph API
(graph.facebook.com). Read, publish, comment, and pull analytics for any
Instagram Professional (Business/Creator) account. ToS-safe — no private API.

Tool surface (29):

  HEALTH + ACCOUNTS (6)  healthcheck, list_accounts, add_account,
                         set_default_account, remove_account, account_info
  PROFILE + MEDIA (3)    get_profile, list_media, get_media
  INSIGHTS (3)           get_account_insights, get_media_insights,
                         get_audience_insights
  PUBLISHING (6)         publish_image, publish_video, publish_reel,
                         publish_carousel, publish_story, publishing_limit
  COMMENTS (4)           get_comments, reply_to_comment, hide_comment,
                         delete_comment
  DISCOVERY (4)          search_hashtag, get_hashtag_media, get_mentions,
                         business_discovery
  DIRECT MESSAGES (3)    list_conversations, get_messages, send_message
                         (gated behind Meta App Review — see _require_dm)

Safety model:
  - Egress pinned to the Meta host allow-list (graph_client.ALLOWED_HOSTS).
  - sanitize_error() on every result + error before the seam (tokens never leak).
  - 4-field observability JSONL on every call (audit.py).
  - INPUT rail validation (validators.py) before any Graph call.
  - Tokens live in the OS keychain (auth.py); never an argument echoed back.

Multi-account: pass account="<label>" to any tool, or omit to use the default.
Configure with add_account(), or set INSTAGRAM_MCP_ACCESS_TOKEN +
INSTAGRAM_MCP_IG_USER_ID for a single zero-config account labelled "env".
"""

from __future__ import annotations

import os
import time
from typing import Any

from fastmcp import FastMCP

import audit
import auth
import graph_client
import validators as V

mcp = FastMCP("instagram-mcp")

# ----------------------------- field selections ----------------------------- #
PROFILE_FIELDS = ("id,username,name,biography,followers_count,follows_count,"
                  "media_count,profile_picture_url,website")
MEDIA_FIELDS = ("id,caption,media_type,media_product_type,media_url,permalink,"
                "thumbnail_url,timestamp,username,like_count,comments_count")
COMMENT_FIELDS = "id,text,username,timestamp,like_count,hidden"

# Insight metric defaults vary by Graph API version; these are sensible defaults
# and every insights tool accepts a `metrics`/`period` override so the caller can
# adapt to whatever the live API wants. A version mismatch returns Meta's own
# error (which names the right metric) rather than failing opaquely.
DEFAULT_ACCOUNT_METRICS = "reach,impressions,profile_views,follower_count"
DEFAULT_MEDIA_METRICS = "reach,saved,likes,comments,shares,total_interactions"
DEFAULT_AUDIENCE_METRIC = "follower_demographics"


# --------------------------------- plumbing --------------------------------- #

def _ms(start: float) -> int:
    return int((time.perf_counter() - start) * 1000)


def _summarize(result: Any) -> Any:
    """Compact a tool result for the audit io.output field (counts, not bodies)."""
    if isinstance(result, dict):
        summ: dict[str, Any] = {"ok": result.get("ok")}
        for k in ("count", "account", "media_id", "container_id", "comment_id", "id"):
            if k in result:
                summ[k] = result[k]
        return summ
    return {"type": type(result).__name__}


def _guard(tool: str, input_payload: dict[str, Any], impl: Any) -> dict[str, Any]:
    """Run a tool body with audit + fail-safe sanitized error shaping.

    Returns the impl() result on success, or a sanitized
    {ok: False, error, error_class} on any exception. No exception (and so no
    raw token in an exception message) escapes to the MCP seam.
    """
    start = time.perf_counter()
    safe_in = audit.sanitize_payload(input_payload)
    try:
        result = impl()
        audit.record(tool, execution_time_ms=_ms(start),
                     io={"input": safe_in, "output": _summarize(result)})
        return result
    except Exception as exc:  # noqa: BLE001 — boundary: classify + sanitize, never re-raise raw
        error_class = audit.classify_error(exc)
        message = audit.sanitize_error(str(exc))
        audit.record(tool, execution_time_ms=_ms(start),
                     io={"input": safe_in, "output": {"error": message}},
                     error_class=error_class)
        return {"ok": False, "error": message, "error_class": error_class}


def _account_summary(acct: auth.ResolvedAccount) -> dict[str, Any]:
    return {"label": acct.label, "ig_user_id": acct.ig_user_id, "base_host": acct.base_host}


def _require_dm() -> None:
    """DM tools need instagram_manage_messages (Meta App Review). Fail loud, never stub."""
    enabled = os.environ.get("INSTAGRAM_MCP_DM_ENABLED", "").strip().lower() in {"1", "true", "yes"}
    if not enabled:
        raise graph_client.GraphAPIError(
            "Instagram DM tools require the instagram_manage_messages permission, granted only "
            "via Meta App Review (typically weeks). Once your app is approved AND the account token "
            "carries the scope, set INSTAGRAM_MCP_DM_ENABLED=1 to enable these tools.",
            error_class="needs_app_review",
        )


def _wait_container_ready(client: graph_client.GraphClient, container_id: str,
                          *, timeout_s: int = 90, interval_s: int = 4) -> None:
    """Poll a media container until status_code=FINISHED (video/reel/story-video are async)."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        status = client.get(container_id, fields="status_code,status")
        code = status.get("status_code")
        if code == "FINISHED":
            return
        if code == "ERROR":
            raise graph_client.GraphAPIError(
                f"media container {container_id} processing failed: {status.get('status')}",
                error_class="upstream_error")
        time.sleep(interval_s)
    raise graph_client.GraphAPIError(
        f"media container {container_id} not ready after {timeout_s}s", error_class="timeout")


def _publish_container(client: graph_client.GraphClient, ig_user_id: str, container_id: str) -> dict[str, Any]:
    published = client.post(f"{ig_user_id}/media_publish", creation_id=container_id)
    return {"ok": True, "media_id": published.get("id"), "container_id": container_id}


# ============================ HEALTH + ACCOUNTS ============================ #

@mcp.tool()
def healthcheck() -> dict[str, Any]:
    """Verify the MCP is configured + ready: accounts present, token backend, Graph reachability.

    Does a live GET on the default account's profile if one is configured. Safe to
    call anytime; never mutates. Returns ok=false with a hint when no account is set.
    """
    def _impl() -> dict[str, Any]:
        accounts = auth.list_accounts()
        out: dict[str, Any] = {
            "ok": True,
            "accounts_configured": len(accounts),
            "secret_backend": auth.secret_backend(),
            "graph_version": graph_client.GRAPH_VERSION,
            "dm_enabled": os.environ.get("INSTAGRAM_MCP_DM_ENABLED", "").lower() in {"1", "true", "yes"},
            "default_account": next((a["label"] for a in accounts if a.get("default")), None),
            "live_check": None,
        }
        if not accounts:
            out["ok"] = False
            out["hint"] = ("No account configured. Set INSTAGRAM_MCP_ACCESS_TOKEN + "
                           "INSTAGRAM_MCP_IG_USER_ID, or call add_account(label, access_token, ig_user_id).")
            return out
        try:
            client, acct = auth.client_for(None)
            me = client.get(acct.ig_user_id, fields="id,username")
            out["live_check"] = {"ok": True, "username": me.get("username"), "account": acct.label}
        except Exception as exc:  # noqa: BLE001
            out["ok"] = False
            out["live_check"] = {"ok": False, "error": audit.sanitize_error(str(exc)),
                                 "error_class": audit.classify_error(exc)}
        return out

    return _guard("healthcheck", {}, _impl)


@mcp.tool()
def list_accounts() -> dict[str, Any]:
    """List configured Instagram accounts (labels + ig_user_id + default flag). Never returns tokens."""
    return _guard("list_accounts", {}, lambda: {"ok": True, "accounts": auth.list_accounts(),
                                                 "count": len(auth.list_accounts())})


@mcp.tool()
def add_account(label: str, access_token: str, ig_user_id: str,
                *, app_secret: str | None = None, make_default: bool = False) -> dict[str, Any]:
    """Store an Instagram Business/Creator account for use by every tool.

    label        a short key you choose (e.g. "onde", "mycelium", "client-acme")
    access_token a long-lived Instagram Graph API token (stored in the OS keychain)
    ig_user_id   the Instagram Business Account id (numeric)
    app_secret   optional Meta app secret — enables appsecret_proof call hardening
    make_default set true to make this the account used when `account` is omitted

    The token is written to the macOS keychain (or a chmod-600 file fallback) and
    is NEVER echoed back or logged.
    """
    def _impl() -> dict[str, Any]:
        lbl = V.validate_account_label(label)
        uid = V.validate_ig_user_id(ig_user_id)
        if not access_token or not isinstance(access_token, str):
            raise V.ValidationError("access_token must be a non-empty string")
        info = auth.add_account(lbl, access_token, uid, app_secret=app_secret, make_default=make_default)
        return {"ok": True, **info}

    # Redact the token from the audit input — never persist it to the log.
    redacted = {"label": label, "access_token": "***", "token_len": len(access_token or ""),
                "ig_user_id": ig_user_id, "app_secret": "***" if app_secret else None,
                "make_default": make_default}
    return _guard("add_account", redacted, _impl)


@mcp.tool()
def set_default_account(label: str) -> dict[str, Any]:
    """Set which configured account is used when a tool's `account` arg is omitted."""
    return _guard("set_default_account", {"label": label},
                  lambda: {"ok": True, **auth.set_default(V.validate_account_label(label))})


@mcp.tool()
def remove_account(label: str) -> dict[str, Any]:
    """Remove a configured account + delete its stored token from the keychain."""
    return _guard("remove_account", {"label": label},
                  lambda: {"ok": True, **auth.remove_account(V.validate_account_label(label))})


@mcp.tool()
def account_info(account: str | None = None) -> dict[str, Any]:
    """Live profile snapshot for one account: username, name, followers, media count, etc."""
    def _impl() -> dict[str, Any]:
        client, acct = auth.client_for(account)
        prof = client.get(acct.ig_user_id, fields=PROFILE_FIELDS)
        return {"ok": True, "account": acct.label, "profile": prof}
    return _guard("account_info", {"account": account}, _impl)


# ============================ PROFILE + MEDIA ============================ #

@mcp.tool()
def get_profile(account: str | None = None) -> dict[str, Any]:
    """Return the account's public profile fields (followers, follows, media count, bio, website)."""
    def _impl() -> dict[str, Any]:
        client, acct = auth.client_for(account)
        return {"ok": True, "account": acct.label, "profile": client.get(acct.ig_user_id, fields=PROFILE_FIELDS)}
    return _guard("get_profile", {"account": account}, _impl)


@mcp.tool()
def list_media(account: str | None = None, *, limit: int = 25) -> dict[str, Any]:
    """List recent media (posts/reels) for the account, newest first. limit 1-100 (default 25)."""
    def _impl() -> dict[str, Any]:
        client, acct = auth.client_for(account)
        lim = V.validate_limit(limit)
        items = client.get_paginated(f"{acct.ig_user_id}/media", fields=MEDIA_FIELDS,
                                      limit=lim, max_items=lim)
        return {"ok": True, "account": acct.label, "count": len(items), "media": items}
    return _guard("list_media", {"account": account, "limit": limit}, _impl)


@mcp.tool()
def get_media(media_id: str, account: str | None = None) -> dict[str, Any]:
    """Return full fields for one media object by id (caption, type, permalink, like/comment counts)."""
    def _impl() -> dict[str, Any]:
        client, acct = auth.client_for(account)
        mid = V.validate_graph_id(media_id, field="media_id")
        return {"ok": True, "account": acct.label, "media": client.get(mid, fields=MEDIA_FIELDS)}
    return _guard("get_media", {"media_id": media_id, "account": account}, _impl)


# ================================ INSIGHTS ================================ #

@mcp.tool()
def get_account_insights(account: str | None = None, *, metrics: str | None = None,
                         period: str = "day") -> dict[str, Any]:
    """Account-level analytics (reach, impressions, profile views, follower count).

    metrics: comma-separated Graph metric names (default reach,impressions,profile_views,
    follower_count). period: day|week|days_28|lifetime. Exact available metrics vary by
    Graph API version; on a mismatch the Meta error names the correct metric.
    """
    def _impl() -> dict[str, Any]:
        client, acct = auth.client_for(account)
        m = metrics or DEFAULT_ACCOUNT_METRICS
        per = V.validate_enum(period, {"day", "week", "days_28", "lifetime"}, field="period", default="day")
        data = client.get(f"{acct.ig_user_id}/insights", metric=m, period=per)
        return {"ok": True, "account": acct.label, "period": per, "insights": data.get("data", [])}
    return _guard("get_account_insights", {"account": account, "metrics": metrics, "period": period}, _impl)


@mcp.tool()
def get_media_insights(media_id: str, account: str | None = None, *, metrics: str | None = None) -> dict[str, Any]:
    """Per-post analytics (reach, saves, likes, comments, shares, total interactions).

    metrics override is comma-separated; defaults suit feed posts. Reels/stories expose
    different metrics (e.g. plays, navigation) — pass them explicitly if needed.
    """
    def _impl() -> dict[str, Any]:
        client, acct = auth.client_for(account)
        mid = V.validate_graph_id(media_id, field="media_id")
        m = metrics or DEFAULT_MEDIA_METRICS
        data = client.get(f"{mid}/insights", metric=m)
        return {"ok": True, "account": acct.label, "media_id": mid, "insights": data.get("data", [])}
    return _guard("get_media_insights", {"media_id": media_id, "account": account, "metrics": metrics}, _impl)


@mcp.tool()
def get_audience_insights(account: str | None = None, *, metric: str | None = None,
                          period: str = "lifetime", metric_type: str = "total_value",
                          breakdown: str | None = "age,gender,country,city",
                          timeframe: str | None = None) -> dict[str, Any]:
    """Follower demographics (age, gender, country, city).

    Requires the account to have >=100 followers (Meta privacy floor) — below that the
    Graph returns an error, surfaced cleanly. Newer Graph versions use metric=
    follower_demographics with metric_type=total_value + breakdown=age|gender|country|city
    and a timeframe (last_14_days|last_30_days|last_90_days|prev_month) for engaged-audience
    metrics. All are override-able.
    """
    def _impl() -> dict[str, Any]:
        client, acct = auth.client_for(account)
        m = metric or DEFAULT_AUDIENCE_METRIC
        params: dict[str, Any] = {"metric": m, "period": period, "metric_type": metric_type}
        if breakdown:
            params["breakdown"] = breakdown
        if timeframe:
            params["timeframe"] = timeframe
        data = client.get(f"{acct.ig_user_id}/insights", **params)
        return {"ok": True, "account": acct.label, "metric": m, "insights": data.get("data", [])}
    return _guard("get_audience_insights",
                  {"account": account, "metric": metric, "period": period}, _impl)


# =============================== PUBLISHING =============================== #

@mcp.tool()
def publishing_limit(account: str | None = None) -> dict[str, Any]:
    """Remaining posts in the rolling 24h publishing quota (Instagram caps API posts/day)."""
    def _impl() -> dict[str, Any]:
        client, acct = auth.client_for(account)
        data = client.get(f"{acct.ig_user_id}/content_publishing_limit",
                           fields="config,quota_usage")
        return {"ok": True, "account": acct.label, "limit": data.get("data", [])}
    return _guard("publishing_limit", {"account": account}, _impl)


@mcp.tool()
def publish_image(image_url: str, caption: str | None = None, account: str | None = None) -> dict[str, Any]:
    """Publish a single image to the feed.

    image_url must be a PUBLIC https URL (Instagram fetches the bytes itself). caption
    <= 2200 chars. Two-step Graph flow (create container -> publish) handled internally.
    """
    def _impl() -> dict[str, Any]:
        client, acct = auth.client_for(account)
        url = V.validate_public_https_url(image_url, field="image_url")
        cap = V.validate_caption(caption)
        container = client.post(f"{acct.ig_user_id}/media", image_url=url, caption=cap)
        cid = container.get("id")
        return {"account": acct.label, **_publish_container(client, acct.ig_user_id, cid)}
    return _guard("publish_image", {"image_url": image_url, "caption": V.truncate(caption or ""),
                                    "account": account}, _impl)


@mcp.tool()
def publish_video(video_url: str, caption: str | None = None, account: str | None = None) -> dict[str, Any]:
    """Publish a video to the feed. video_url must be a PUBLIC https URL.

    Video containers process asynchronously; this polls status up to 90s before publishing.
    """
    def _impl() -> dict[str, Any]:
        client, acct = auth.client_for(account)
        url = V.validate_public_https_url(video_url, field="video_url")
        cap = V.validate_caption(caption)
        container = client.post(f"{acct.ig_user_id}/media", media_type="VIDEO", video_url=url, caption=cap)
        cid = container.get("id")
        _wait_container_ready(client, cid)
        return {"account": acct.label, **_publish_container(client, acct.ig_user_id, cid)}
    return _guard("publish_video", {"video_url": video_url, "caption": V.truncate(caption or ""),
                                    "account": account}, _impl)


@mcp.tool()
def publish_reel(video_url: str, caption: str | None = None, account: str | None = None,
                 *, share_to_feed: bool = True, cover_url: str | None = None) -> dict[str, Any]:
    """Publish a Reel. video_url must be a PUBLIC https URL.

    share_to_feed also surfaces the reel in the main grid. cover_url (optional) sets the
    thumbnail. Reel containers process asynchronously (polled up to 90s).
    """
    def _impl() -> dict[str, Any]:
        client, acct = auth.client_for(account)
        url = V.validate_public_https_url(video_url, field="video_url")
        cap = V.validate_caption(caption)
        cover = V.validate_public_https_url(cover_url, field="cover_url") if cover_url else None
        container = client.post(f"{acct.ig_user_id}/media", media_type="REELS", video_url=url,
                                caption=cap, share_to_feed=share_to_feed, cover_url=cover)
        cid = container.get("id")
        _wait_container_ready(client, cid)
        return {"account": acct.label, **_publish_container(client, acct.ig_user_id, cid)}
    return _guard("publish_reel", {"video_url": video_url, "caption": V.truncate(caption or ""),
                                   "share_to_feed": share_to_feed, "account": account}, _impl)


@mcp.tool()
def publish_carousel(image_urls: list[str], caption: str | None = None,
                     account: str | None = None) -> dict[str, Any]:
    """Publish a multi-image carousel (2-10 images). Each image_url must be a PUBLIC https URL.

    Creates one child container per image (is_carousel_item) then a CAROUSEL parent, then publishes.
    """
    def _impl() -> dict[str, Any]:
        client, acct = auth.client_for(account)
        if not isinstance(image_urls, list) or not (2 <= len(image_urls) <= 10):
            raise V.ValidationError("image_urls must be a list of 2-10 public https URLs")
        urls = [V.validate_public_https_url(u, field="image_url") for u in image_urls]
        cap = V.validate_caption(caption)
        child_ids: list[str] = []
        for u in urls:
            child = client.post(f"{acct.ig_user_id}/media", image_url=u, is_carousel_item=True)
            child_ids.append(child.get("id"))
        parent = client.post(f"{acct.ig_user_id}/media", media_type="CAROUSEL",
                             children=",".join(child_ids), caption=cap)
        cid = parent.get("id")
        return {"account": acct.label, "child_count": len(child_ids),
                **_publish_container(client, acct.ig_user_id, cid)}
    return _guard("publish_carousel", {"image_count": len(image_urls or []),
                                       "caption": V.truncate(caption or ""), "account": account}, _impl)


@mcp.tool()
def publish_story(image_url: str | None = None, video_url: str | None = None,
                  account: str | None = None) -> dict[str, Any]:
    """Publish a Story (image OR video). Exactly one of image_url / video_url, PUBLIC https."""
    def _impl() -> dict[str, Any]:
        client, acct = auth.client_for(account)
        if bool(image_url) == bool(video_url):
            raise V.ValidationError("pass exactly one of image_url or video_url")
        if image_url:
            url = V.validate_public_https_url(image_url, field="image_url")
            container = client.post(f"{acct.ig_user_id}/media", media_type="STORIES", image_url=url)
            cid = container.get("id")
        else:
            url = V.validate_public_https_url(video_url, field="video_url")
            container = client.post(f"{acct.ig_user_id}/media", media_type="STORIES", video_url=url)
            cid = container.get("id")
            _wait_container_ready(client, cid)
        return {"account": acct.label, **_publish_container(client, acct.ig_user_id, cid)}
    return _guard("publish_story", {"image_url": image_url, "video_url": video_url, "account": account}, _impl)


# ================================ COMMENTS ================================ #

@mcp.tool()
def get_comments(media_id: str, account: str | None = None, *, limit: int = 25) -> dict[str, Any]:
    """List comments on a media object (text, username, timestamp, like_count, hidden)."""
    def _impl() -> dict[str, Any]:
        client, acct = auth.client_for(account)
        mid = V.validate_graph_id(media_id, field="media_id")
        lim = V.validate_limit(limit)
        items = client.get_paginated(f"{mid}/comments", fields=COMMENT_FIELDS, limit=lim, max_items=lim)
        return {"ok": True, "account": acct.label, "media_id": mid, "count": len(items), "comments": items}
    return _guard("get_comments", {"media_id": media_id, "account": account, "limit": limit}, _impl)


@mcp.tool()
def reply_to_comment(comment_id: str, message: str, account: str | None = None) -> dict[str, Any]:
    """Reply to a comment. message <= 2200 chars. Returns the new comment id."""
    def _impl() -> dict[str, Any]:
        client, acct = auth.client_for(account)
        cid = V.validate_graph_id(comment_id, field="comment_id")
        msg = V.validate_caption(message, field="message")
        if not msg:
            raise V.ValidationError("message must be non-empty")
        res = client.post(f"{cid}/replies", message=msg)
        return {"ok": True, "account": acct.label, "comment_id": res.get("id")}
    return _guard("reply_to_comment", {"comment_id": comment_id, "message": V.truncate(message),
                                       "account": account}, _impl)


@mcp.tool()
def hide_comment(comment_id: str, hide: bool = True, account: str | None = None) -> dict[str, Any]:
    """Hide (or unhide) a comment from public view. hide=false unhides."""
    def _impl() -> dict[str, Any]:
        client, acct = auth.client_for(account)
        cid = V.validate_graph_id(comment_id, field="comment_id")
        client.post(cid, hide=bool(hide))
        return {"ok": True, "account": acct.label, "comment_id": cid, "hidden": bool(hide)}
    return _guard("hide_comment", {"comment_id": comment_id, "hide": hide, "account": account}, _impl)


@mcp.tool()
def delete_comment(comment_id: str, account: str | None = None) -> dict[str, Any]:
    """Permanently delete a comment you own (or a comment on your media). Irreversible."""
    def _impl() -> dict[str, Any]:
        client, acct = auth.client_for(account)
        cid = V.validate_graph_id(comment_id, field="comment_id")
        client.delete(cid)
        return {"ok": True, "account": acct.label, "comment_id": cid, "deleted": True}
    return _guard("delete_comment", {"comment_id": comment_id, "account": account}, _impl)


# ================================ DISCOVERY ================================ #

@mcp.tool()
def search_hashtag(hashtag: str, account: str | None = None) -> dict[str, Any]:
    """Resolve a hashtag name to its Graph id (needed before get_hashtag_media)."""
    def _impl() -> dict[str, Any]:
        client, acct = auth.client_for(account)
        tag = V.validate_hashtag(hashtag)
        res = client.get("ig_hashtag_search", user_id=acct.ig_user_id, q=tag)
        return {"ok": True, "account": acct.label, "hashtag": tag, "results": res.get("data", [])}
    return _guard("search_hashtag", {"hashtag": hashtag, "account": account}, _impl)


@mcp.tool()
def get_hashtag_media(hashtag_id: str, account: str | None = None, *,
                      edge: str = "top_media", limit: int = 25) -> dict[str, Any]:
    """Recent or top media for a hashtag id. edge=top_media|recent_media. limit 1-100.

    Get the hashtag_id from search_hashtag first. Subject to Meta's 30-unique-hashtags
    per-7-days query limit per account.
    """
    def _impl() -> dict[str, Any]:
        client, acct = auth.client_for(account)
        hid = V.validate_graph_id(hashtag_id, field="hashtag_id")
        e = V.validate_enum(edge, {"top_media", "recent_media"}, field="edge", default="top_media")
        lim = V.validate_limit(limit)
        items = client.get_paginated(f"{hid}/{e}", user_id=acct.ig_user_id,
                                     fields="id,caption,media_type,permalink,like_count,comments_count,timestamp",
                                     limit=lim, max_items=lim)
        return {"ok": True, "account": acct.label, "hashtag_id": hid, "edge": e,
                "count": len(items), "media": items}
    return _guard("get_hashtag_media", {"hashtag_id": hashtag_id, "edge": edge, "limit": limit,
                                        "account": account}, _impl)


@mcp.tool()
def get_mentions(account: str | None = None, *, limit: int = 25) -> dict[str, Any]:
    """List recent media where the account is @-mentioned (tags edge)."""
    def _impl() -> dict[str, Any]:
        client, acct = auth.client_for(account)
        lim = V.validate_limit(limit)
        items = client.get_paginated(f"{acct.ig_user_id}/tags", fields=MEDIA_FIELDS, limit=lim, max_items=lim)
        return {"ok": True, "account": acct.label, "count": len(items), "media": items}
    return _guard("get_mentions", {"account": account, "limit": limit}, _impl)


@mcp.tool()
def business_discovery(username: str, account: str | None = None, *, with_media: bool = False,
                       media_limit: int = 12) -> dict[str, Any]:
    """Public profile + (optionally) recent media for ANY business/creator account by username.

    Read-only competitor/prospect research via the business_discovery edge. Only works for
    Professional accounts (not personal). with_media pulls up to media_limit recent posts.
    """
    def _impl() -> dict[str, Any]:
        client, acct = auth.client_for(account)
        uname = V.validate_username(username)
        fields = "business_discovery.username(" + uname + "){" + \
                 "username,name,biography,followers_count,follows_count,media_count,profile_picture_url,website"
        if with_media:
            lim = V.validate_limit(media_limit, ceiling=50)
            fields += (".media.limit(" + str(lim) + "){id,caption,media_type,permalink,like_count,"
                       "comments_count,timestamp}")
        fields += "}"
        res = client.get(acct.ig_user_id, fields=fields)
        return {"ok": True, "account": acct.label, "target": uname,
                "business_discovery": res.get("business_discovery", {})}
    return _guard("business_discovery", {"username": username, "with_media": with_media,
                                         "account": account}, _impl)


# ============================= DIRECT MESSAGES ============================= #
# Gated behind Meta App Review (instagram_manage_messages). Each tool fails loud
# with the app-review hint unless INSTAGRAM_MCP_DM_ENABLED=1 AND the token carries
# the scope. Scaffolded (not stubbed) so enabling is a one-flag flip post-approval.

@mcp.tool()
def list_conversations(account: str | None = None, *, limit: int = 25) -> dict[str, Any]:
    """List Instagram DM conversations. REQUIRES Meta App Review (instagram_manage_messages)."""
    def _impl() -> dict[str, Any]:
        _require_dm()
        client, acct = auth.client_for(account)
        lim = V.validate_limit(limit)
        items = client.get_paginated(f"{acct.ig_user_id}/conversations", platform="instagram",
                                      fields="id,updated_time,participants", limit=lim, max_items=lim)
        return {"ok": True, "account": acct.label, "count": len(items), "conversations": items}
    return _guard("list_conversations", {"account": account, "limit": limit}, _impl)


@mcp.tool()
def get_messages(conversation_id: str, account: str | None = None, *, limit: int = 25) -> dict[str, Any]:
    """Read messages in a DM conversation. REQUIRES Meta App Review (instagram_manage_messages)."""
    def _impl() -> dict[str, Any]:
        _require_dm()
        client, acct = auth.client_for(account)
        cid = V.validate_graph_id(conversation_id, field="conversation_id")
        lim = V.validate_limit(limit)
        res = client.get(cid, fields="messages.limit(" + str(lim) + "){id,created_time,from,to,message}")
        return {"ok": True, "account": acct.label, "conversation_id": cid,
                "messages": (res.get("messages") or {}).get("data", [])}
    return _guard("get_messages", {"conversation_id": conversation_id, "account": account, "limit": limit}, _impl)


@mcp.tool()
def send_message(recipient_id: str, text: str, account: str | None = None) -> dict[str, Any]:
    """Send a DM. REQUIRES Meta App Review + the 24-hour standard-messaging window.

    recipient_id is the IGSID (Instagram-scoped user id) of the recipient. Outside the
    24h customer-service window a paid message tag is required (not handled here).
    """
    def _impl() -> dict[str, Any]:
        _require_dm()
        client, acct = auth.client_for(account)
        rid = V.validate_graph_id(recipient_id, field="recipient_id")
        msg = V.validate_caption(text, field="text")
        if not msg:
            raise V.ValidationError("text must be non-empty")
        res = client.post(f"{acct.ig_user_id}/messages",
                          recipient='{"id":"' + rid + '"}', message='{"text":' + _json_str(msg) + '}')
        return {"ok": True, "account": acct.label, "recipient_id": rid, "result": res}
    return _guard("send_message", {"recipient_id": recipient_id, "text": V.truncate(text), "account": account}, _impl)


def _json_str(s: str) -> str:
    """Minimal JSON string encoder for the messages payload."""
    import json
    return json.dumps(s, ensure_ascii=False)


if __name__ == "__main__":
    mcp.run()
