# Setup — getting an Instagram access token + account id

The official Instagram Graph API needs (1) an Instagram **Professional** account connected to a Facebook Page, (2) a Meta app, (3) a long-lived **access token**, and (4) your **Instagram Business Account id**. This walks each step.

## 1. Convert to a Professional account

In the Instagram app → Settings → **Account type and tools** → switch to **Business** or **Creator**. Connect it to a Facebook Page when prompted (create a Page if you don't have one). Free and reversible.

## 2. Create a Meta app

1. Go to [developers.facebook.com](https://developers.facebook.com/) → **My Apps** → **Create App**.
2. Choose the **Business** type.
3. Add the **Instagram** product (or **Instagram Graph API** / **Facebook Login for Business**, depending on the current console).

## 3. Get an access token

The quickest path for a single account:

1. Open the [Graph API Explorer](https://developers.facebook.com/tools/explorer/).
2. Select your app, then **Generate Access Token**.
3. Grant these permissions: `instagram_basic`, `instagram_content_publish`, `instagram_manage_comments`, `instagram_manage_insights`, `pages_show_list`, `pages_read_engagement`, `business_management`.
4. This gives a **short-lived** token (1 hour). Exchange it for a **long-lived** token (~60 days):

```bash
curl -s "https://graph.facebook.com/v21.0/oauth/access_token?grant_type=fb_exchange_token&client_id=APP_ID&client_secret=APP_SECRET&fb_exchange_token=SHORT_LIVED_TOKEN"
```

For production / multi-account, implement the standard Facebook Login token flow so each account owner authorizes their own token.

## 4. Find your Instagram Business Account id

```bash
# a) list the Pages you manage
curl -s "https://graph.facebook.com/v21.0/me/accounts?access_token=LONG_LIVED_TOKEN"

# b) for your Page id, read its connected Instagram account
curl -s "https://graph.facebook.com/v21.0/PAGE_ID?fields=instagram_business_account&access_token=LONG_LIVED_TOKEN"
```

The `instagram_business_account.id` value is your `INSTAGRAM_MCP_IG_USER_ID`.

## 5. Configure the server

Either set env vars (single-account, zero-config):

```bash
export INSTAGRAM_MCP_ACCESS_TOKEN="EAA...long-lived..."
export INSTAGRAM_MCP_IG_USER_ID="17841400000000000"
# optional: enables appsecret_proof call signing
export INSTAGRAM_MCP_APP_SECRET="your-app-secret"
```

…or call `add_account` from your MCP client (token is stored in the OS keychain):

```
add_account(label="main", access_token="EAA...", ig_user_id="17841400000000000", app_secret="...", make_default=True)
```

Verify with `healthcheck` — it does a live profile read and reports your username on success.

## 6. (Optional) Enable DMs

DM tools need `instagram_manage_messages`, granted only via **Meta App Review**. Submit your app for review with a screencast of the DM use case and opt-out handling. Once approved and your token carries the scope, set `INSTAGRAM_MCP_DM_ENABLED=1`.

See **[docs/APP_REVIEW.md](docs/APP_REVIEW.md)** for the full submission walkthrough — screencast shot list, reviewer test-instructions template, privacy-policy + data-deletion requirements, business verification, the 24-hour-window / message-tag caveat, and the post-approval flip.

## Token refresh

Long-lived tokens last ~60 days. Refresh before expiry:

```bash
curl -s "https://graph.facebook.com/v21.0/oauth/access_token?grant_type=fb_exchange_token&client_id=APP_ID&client_secret=APP_SECRET&fb_exchange_token=CURRENT_LONG_LIVED_TOKEN"
```

Then `add_account` again with the same label to overwrite the stored token.

## Troubleshooting

- **`error_class: oauth`** — token expired or missing a scope. Regenerate with the permissions in step 3.
- **`error_class: permission`** — the account isn't Professional, or the token lacks a scope for that tool.
- **`error_class: rate_limited`** — Instagram's API rate limit; back off and retry.
- **`error_class: needs_app_review`** — a DM tool was called before `instagram_manage_messages` was approved + `INSTAGRAM_MCP_DM_ENABLED=1`.
- **demographics return an error** — `get_audience_insights` requires the account to have ≥100 followers (Meta privacy floor).
