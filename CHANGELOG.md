# Changelog

All notable changes to instagram-mcp are documented here. Format follows
[Keep a Changelog](https://keepachangelog.com/); versioning is [SemVer](https://semver.org/).

## [0.1.0] — 2026-06-16

Initial release.

### Added
- 29 tools over the official Instagram Graph API:
  - **Accounts & health** (6): healthcheck, list_accounts, add_account, set_default_account, remove_account, account_info.
  - **Profile & media** (3): get_profile, list_media, get_media.
  - **Insights** (3): get_account_insights, get_media_insights, get_audience_insights.
  - **Publishing** (6): publish_image, publish_video, publish_reel, publish_carousel, publish_story, publishing_limit — with async container-status polling for video/reel/story.
  - **Comments** (4): get_comments, reply_to_comment, hide_comment, delete_comment.
  - **Discovery** (4): search_hashtag, get_hashtag_media, get_mentions, business_discovery.
  - **Direct messages** (3): list_conversations, get_messages, send_message — gated behind Meta App Review (`instagram_manage_messages`), fail-loud until `INSTAGRAM_MCP_DM_ENABLED=1`.
- Multi-account support with OS-keychain token storage (chmod-600 file fallback); tokens never echoed or logged.
- Safety: Meta-host egress allow-list + fail-closed SSRF guard; `sanitize_error` scrubber (Meta `EAA…`/`IGQV…` tokens, bearer headers, app secrets, API keys); 4-field JSONL audit; INPUT-rail validation.

[0.1.0]: https://github.com/adelaidasofia/instagram-mcp/releases/tag/v0.1.0
