# Unlocking the DM tools — Meta App Review

The three DM tools (`list_conversations`, `get_messages`, `send_message`) are gated
behind the `instagram_manage_messages` permission. Meta grants it **only through App
Review** (typically weeks, stricter in 2025-26). Until then the DM tools fail loud with
`error_class: needs_app_review` rather than silently no-op. The other ~22 tools (profile,
media, insights, publishing, comments, hashtags, mentions) need no review.

This doc is the practical path to approval and the post-approval flip.

## Login path

This server uses the **Instagram API with Facebook Login** path: an Instagram
Professional account connected to a Facebook Page, permission named
`instagram_manage_messages`. (Meta's newer **Instagram API with Instagram Login** path
names the equivalent permission `instagram_business_manage_messages` and skips the Page.
Both require App Review + Business Verification. This server is built for the
Facebook-Login path.)

## Before you submit

1. **Instagram Professional account** (Business or Creator) connected to a Facebook Page.
2. **Meta app** (Business type) with the Instagram product added.
3. **Business Verification** — required for *all* Advanced Access requests. Start it early;
   Meta's queue can take days to weeks. This is usually the schedule bottleneck.
4. **App settings**: app icon, **Privacy Policy URL**, **Data Deletion** instructions URL,
   category, and a Website platform URL.
5. A working non-DM setup (`healthcheck` returns your username) — see [SETUP.md](../SETUP.md).

## Privacy policy must cover Instagram data

Your privacy policy has to describe how the app handles data obtained via these
permissions. At minimum state: which permissions you request, that the data is used only
to operate the messaging/comment/insight features, that tokens are stored encrypted, that
you do not sell or ad-target the data, and how a user requests deletion / revokes access
(Instagram → *Settings → Apps and websites → Remove*). Set a Data Deletion Instructions URL.

## What the reviewer wants

Meta weighs the **screencast** heavily — it must show the permission *in use*, end to end,
with narration or captions. For an operator/first-party tool like this one, frame it
honestly as a business-owned tool to manage the business's own (and explicitly-authorized
managed clients') Instagram DMs. You do not message users who have not contacted the
business first, and you reply within Meta's 24-hour window.

**Screencast shot list (2-4 min):**

1. Intro caption + show the connected account username (`healthcheck`).
2. From a second account, DM your business test account; show it arrive.
3. Run `list_conversations` — show the new conversation in the result.
4. Run `get_messages` — show the inbound message text.
5. Run `send_message` — reply within the 24h window.
6. **Cut to the recipient's Instagram and show the reply arrived in real time** (the key shot).
7. State the opt-out / data-deletion path; briefly show the privacy page.

**Reviewer test-instructions template (paste into the request):**

> This is an operator tool (an MCP server) run by the business owner; there is no public
> end-user signup. The flow is in the attached screencast. To reproduce: configure the
> tool with the connected account's long-lived token; from a second Instagram account, DM
> the test business account; the operator calls `list_conversations` (returns the new
> conversation), `get_messages` (reads it), and `send_message` (replies). The reply appears
> in the sender's Instagram inbox in real time. All actions are by the business owner on the
> business's own account.

Common rejection reasons: screencast doesn't actually show the permission used; use case
unclear; missing/weak privacy policy; app setup incomplete. Most rejections are fixed by
re-recording the specific shot, not rebuilding the submission.

## After approval — flip the flag

1. **Refresh the token to carry the new scope.** Re-run your login flow granting
   `instagram_manage_messages`, exchange for a long-lived token, and re-store it:
   ```
   add_account(label="<label>", access_token="EAA...new...", ig_user_id="<id>", make_default=True)
   ```
2. **Enable the tools:** set `INSTAGRAM_MCP_DM_ENABLED=1` in the server environment.
3. **Verify the gate is gone:** `healthcheck` reports `"dm_enabled": true`.
4. **Live read:** `list_conversations` returns real conversations (no `needs_app_review`);
   `get_messages` returns real messages.
5. **Live round-trip:** reply via `send_message` to a conversation that messaged you in the
   last 24h; confirm it lands in the recipient's Instagram.

## 24-hour window + message tags (important for client use)

You can reply freely for **24 hours** after a user's last message (standard messaging).
Outside that window you need a **message tag** (e.g. `HUMAN_AGENT`, which needs its own
approval) or a paid path. `send_message` does **not** attach a tag, so out-of-window sends
are rejected by Meta. Reply inside 24h, or extend the server to pass a tag if your use case
needs it.
