# instagram-mcp

An [MCP](https://modelcontextprotocol.io) server for the **official Instagram Graph API**. Read, publish, comment, and pull analytics across one or many Instagram **Business/Creator** accounts from Claude (or any MCP client) — ToS-safe, no private/reverse-engineered API.

29 tools across accounts, media, publishing, insights, comments, discovery, and (review-gated) direct messages.

```
npx-style stdio MCP · Python · FastMCP · MIT
```

## Why this one

Most "Instagram automation" tools either (a) wrap the **unofficial** private API (username + password) — which violates Instagram's Terms and risks a ban — or (b) only **post**, with no way to read insights, comments, or mentions back. This server is built entirely on the **official Graph API**, is **multi-account** from day one, and covers the full read + write surface. The only thing it cannot do without Meta's approval is DMs (see [Direct messages](#direct-messages)).

## Requirements

- An Instagram **Professional account** (Business or Creator). Personal accounts cannot use the Graph API. Converting is free and reversible (Instagram app → Settings → Account type).
- The Instagram account connected to a Facebook Page.
- A long-lived **access token** with `instagram_basic` + `instagram_content_publish` + `instagram_manage_comments` + `instagram_manage_insights`, and the account's numeric **Instagram Business Account id**. See [SETUP.md](SETUP.md) for the exact token + id steps.
- Python 3.10+.

## Install

```bash
git clone https://github.com/adelaidasofia/instagram-mcp
cd instagram-mcp
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
```

Register it with your MCP client (Claude Desktop / Claude Code) — single-account zero-config path:

```json
{
  "mcpServers": {
    "instagram": {
      "command": "/absolute/path/instagram-mcp/.venv/bin/python",
      "args": ["/absolute/path/instagram-mcp/server.py"],
      "env": {
        "INSTAGRAM_MCP_ACCESS_TOKEN": "EAA...your-long-lived-token...",
        "INSTAGRAM_MCP_IG_USER_ID": "17841400000000000"
      }
    }
  }
}
```

Or skip the env vars and call `add_account` at runtime (token goes to your OS keychain). See [Multiple accounts](#multiple-accounts).

## Tools

**Accounts & health** — `healthcheck`, `list_accounts`, `add_account`, `set_default_account`, `remove_account`, `account_info`

**Profile & media** — `get_profile`, `list_media`, `get_media`

**Insights** — `get_account_insights` (reach, impressions, profile views, follower count), `get_media_insights` (per-post reach, saves, shares, interactions), `get_audience_insights` (follower demographics: age, gender, country, city)

**Publishing** — `publish_image`, `publish_video`, `publish_reel`, `publish_carousel` (2–10 images), `publish_story`, `publishing_limit` (remaining 24h quota). Video/reel containers process asynchronously; the server polls to completion before publishing.

**Comments** — `get_comments`, `reply_to_comment`, `hide_comment`, `delete_comment`

**Discovery** — `search_hashtag`, `get_hashtag_media`, `get_mentions`, `business_discovery` (read any public Professional account by username)

**Direct messages** — `list_conversations`, `get_messages`, `send_message` (see below)

> Publishing takes **public https media URLs** — Instagram fetches the bytes itself, so the image/video must be reachable on the open web (an S3/Cloudflare/any-CDN URL works).

## Multiple accounts

Run one server for all your accounts (yours, a brand's, a client's). Each account authorizes its own token:

```
add_account(label="brand-a", access_token="EAA...", ig_user_id="178414...", make_default=True)
add_account(label="brand-b", access_token="EAA...", ig_user_id="178414...")
list_media(account="brand-b")
get_account_insights(account="brand-a")
```

Tokens are stored in the **macOS keychain** (or a `chmod 600` file on other platforms), never in the metadata file and never returned by any tool. Omit `account` on any tool to use the default.

## Direct messages

The DM tools require the `instagram_manage_messages` permission, which Meta grants **only through App Review** (typically weeks, and stricter in 2025–2026). Until then, the DM tools fail loud with that instruction rather than silently no-op. Once your app is approved and the token carries the scope, set `INSTAGRAM_MCP_DM_ENABLED=1`. Note Instagram's 24-hour standard-messaging window applies.

## Safety

- **Egress is pinned** to the Meta host allow-list (`graph.facebook.com` / `graph.instagram.com`). A general SSRF guard (RFC1918 / loopback / link-local / CGNAT / cloud-metadata, fail-closed DNS) backs any URL handling.
- **Credentials never leak**: every result and error passes a scrubber that strips access tokens (incl. Meta `EAA…` / `IGQV…`), bearer headers, app secrets, and API keys before it reaches the model.
- **Observability**: every call appends a 4-field JSONL audit line (`execution_time_ms`, `io`, `token_usage`, `error_class`) under `~/.claude/instagram-mcp/audit.log.jsonl`.
- **Input validation** runs before every Graph call (ids, caption length, hashtag/username charset, https media URLs).

## License

MIT — see [LICENSE](LICENSE).

Built by [Adelaida Diaz-Roa](https://diazroa.com).
