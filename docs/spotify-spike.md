# Spotify integration — spike

2026-10-18 · Issue #8. **Spike; one small step prototyped** (Spotify link previews
in the prompt). Voice-channel playback is out of scope (separate issue).

## Summary

People in the server share Spotify links, and Kiki can't tell what they are: the
prompt holds only `open.spotify.com/track/<id>`. The obvious fix, looking links up
through Spotify's Web API and handing the result to the model, **is not allowed**:
Spotify's developer terms forbid feeding any Spotify Content into an AI model.
The Web API is also nearly closed to hobby apps since February 2026.

Recommendation: use what **Discord** already gives Kiki, and don't call Spotify.
The first step, rendering Discord's own preview of a shared Spotify link into the
prompt, is implemented here. Presence ("Listening to Spotify") is the next
option, if the server wants it. Anything built on the Spotify API must keep its
data away from the model.

## Use cases

| Use case | Value | Needs |
|---|---|---|
| Know what a shared link is ("rate this 🎵") | High: links are common, and replies about them are currently guesses | Track/artist/album names |
| "What's everyone listening to?" | Medium, fun | Discord presence |
| Music taste from history ("what does Sam like?") | Medium | Past link previews (already stored in `messages.raw`) |
| A server playlist collecting shared tracks | Medium | User OAuth, Web API writes |
| Recommendations ("songs like this") | Low-medium | Model knowledge; Spotify's recommendation endpoints are gone |

## What Spotify offers (October 2026)

**Auth**
- **Client Credentials** gives app-only access to the catalog: tracks, albums,
  artists, shows, episodes and public playlists. Tokens last an hour, with no
  refresh token.
- **Authorization Code with PKCE** is needed for anything user-specific: now
  playing, libraries, writing playlists.
- Implicit grant, HTTP redirect URIs and `localhost` aliases stopped working for
  every app on 2025-11-27 (for new apps, from 2025-04-09). HTTP loopback IP
  literals (`http://127.0.0.1`, `http://[::1]`) are still allowed.
- Refresh tokens expire six months after the user's original authorization. This
  applies to new apps from 2026-06-18 and to existing apps from 2026-07-20; after
  it, the user must re-authorize.

**Quota**
- **Development mode** (from 2026-02-11 for new apps, 2026-03-09 for existing
  ones):
  - At most **5 allowlisted users** per app (existing apps keep users they
    already had), and the owner needs Premium.
  - Quota is counted per developer account (since 2026-07-23).
- **Extended quota** has been open only to organizations since 2025-05-15, and
  requires about 250k MAU and a launched service. Kiki doesn't qualify, so
  per-user OAuth features are capped at five people.

**Rate limits**
- Limits apply over a rolling 30-second window; Spotify doesn't publish the
  numbers. Over the limit, you get a 429 with `Retry-After`.
- A dev-mode quota 429 carries `reason: QUOTA_EXCEEDED`.

**Removed endpoints and fields**

From 2024-11-27, for new and development-mode apps:
- Recommendations, related artists, audio features and audio analysis.
- Featured and category playlists, and Spotify-owned editorial and algorithmic
  playlists.
- 30-second preview URLs in multi-get responses.

From February 2026, for development mode:
- Batch gets, artist top tracks, new releases and categories are removed.
- Search returns at most 10 results.
- These fields are gone: `popularity` (tracks, albums, artists), `followers`, the
  album `label`, and the show `publisher`.
- Playlist contents are returned only for playlists the user owns or
  collaborates on.

**oEmbed** (`open.spotify.com/oembed?url=…`)
- Works without auth.
- Returns only `title`, a thumbnail and iframe HTML: no artist or album.
- It's part of the developer platform, so the restrictions below apply to it too.

**Link formats**
- `open.spotify.com/{track|album|artist|playlist|episode|show}/<22-char id>`,
  optionally prefixed with `/intl-xx/`, and usually with a `?si=` share
  parameter.
- `spotify:` URIs.
- Short links (`spotify.link`) need redirects, and sometimes HTML parsing, to
  resolve.

## Terms that decide the design

From the Developer Terms (v10, effective 2025-05-15) and the Developer Policy:

- **No AI.** Terms IV.2.a.1 and Policy III.14 forbid training an ML/AI model on
  Spotify Content, "or otherwise ingesting Spotify Content into a machine learning
  or AI model." Terms II.8 defines Spotify Content as "any content, data,
  information or material made available through the Spotify Platform", which
  explicitly includes metadata. Policy III.13 also forbids "analyz[ing] the Spotify Content
  … for any purpose."
- **No databases.** Terms IV.3 says not to "store, aggregate or create
  compilations or databases of Spotify Content, other than as strictly necessary
  to operate your SDA." Content may not be stored indefinitely, and may be
  cached only temporarily, where strictly necessary for performance.
- **Attribution.** Policy II.4 requires metadata and cover art to be attributed
  to Spotify, with a link back. II.4.3 says metadata and cover art may not be
  offered as a standalone service or product (II.5 adds, for streaming apps,
  that they may only be used in connection with the underlying content).
- **No group listening.** Policy III.4 forbids playing one source to several
  simultaneous listeners. That matters for the voice issue, not here.

So **a tool that calls the Web API or oEmbed and returns the result to the model
breaks IV.2.a.1**, whatever it's used for. The spike built that tool first (parse
link → oEmbed or client-credentials lookup → result to the model). It worked and
was discarded for this reason; it isn't on the branch.

## What Discord already gives Kiki

**Link previews**
- Discord unfurls Spotify links itself. The embed arrives in a partial
  `MESSAGE_UPDATE` shortly after the message, and `store::update_message`
  already merges it into `messages.raw`.
- The embed comes from Spotify's page metadata, which as of 2026-10-18 looks like
  this (`og:title` / `og:description`, checked on a live track page):
  `Never Gonna Give You Up` / `Rick Astley · Whenever You Need Somebody · Song · 1987`.
- No Spotify API call is involved, and Kiki accepts no Spotify terms.

**Presence**
- A member listening on Spotify has an activity with `type: 2` and
  `name: "Spotify"`:
  - `details` is the track and `state` the artists.
  - `assets.large_text` is the album.
  - `sync_id` is the track ID.
  - `timestamps` holds the start and end.
- Discord documents only the generic fields; `sync_id` and the `spotify:` image
  prefix are de facto, from client libraries.
- It needs the privileged `GUILD_PRESENCES` intent: a portal toggle for bots in
  fewer than 100 servers, review above that. Kiki currently asks for
  `GUILDS | GUILD_MESSAGES | MESSAGE_CONTENT` only.

## Options

| Option | Pros | Cons |
|---|---|---|
| **A. Render Discord's Spotify previews in the prompt** (built) | No credentials, cost or new service; no Spotify terms accepted; past previews are already stored, so conversation history and topic summaries get them too (search still matches message text only) | Preview may arrive after the reply starts; title/artist/album/year only; Discord's embed schema isn't formally documented |
| B. Spotify lookup tool (Web API / oEmbed → model) | Richer and on demand; reliable timing | **Breaks Terms IV.2.a.1**; credentials; quota |
| C. Presence: store or show what members are listening to | Fun "now playing" and taste features; data comes from Discord | Privileged intent; privacy (people don't expect a bot to log their listening); a new event stream and table; some fields undocumented |
| D. Server playlist via OAuth (deterministic code, never the model) | Real Spotify feature; one owner account fits the five-user cap | Owner's refresh token expires every six months; terms questions around aggregation; maintenance for modest value |
| E. Spotify for AI / MCP | — | Spotify's ChatGPT (2025-10) and Claude (2026-04) integrations are first-party consumer connectors. There's no public developer agent API or official MCP server. |

## Recommendation and plan

1. **Ship A** (this branch). `context.rs` selects `raw->'embeds'` whose
   `provider.name` is `Spotify`, as `title — description`.
   - `Roster::text` appends ` [Spotify: …]` after the message text.
   - The preview is flattened to one line, and brackets become parentheses so it
     can't close the marker or forge a `[u2 Bob]` speaker line.
   - It's capped at 200 characters. Playlist descriptions are user-written, and
     the prompt already tells the model that found text is evidence, not
     instructions.
   - Verified with a unit test and the sim scenario `spotify-link-preview`, where
     fakecord delivers previews in a follow-up `MESSAGE_UPDATE` as Discord does.
2. **Watch it in production.** Check how often a reply's trigger message has no
   preview yet. If it's often, either wait briefly for an embed when the
   triggering message contains an `open.spotify.com` link, or re-read the
   trigger rows just before building the prompt.
3. **Presence (C), if the server wants it.**
   - Opt in per server (a `/kiki config` key), and keep only the current activity
     in memory, not a history table, unless members agree to more.
   - Expose it as a "who's listening now" line in run context or a tool.
4. **Don't build B.** Revisit only with written permission from Spotify.
   **Build D only if asked**, and keep Spotify responses out of the model: the
   model may propose "artist – title" strings, and code resolves them and writes
   the playlist.
5. **Generalize A to other link previews** (YouTube, TikTok, articles) as a
   separate change. It's the same mechanism, but it widens the prompt-injection
   surface, so it deserves its own review.

## Open questions

- Are Discord-originated previews of Spotify pages acceptable? Kiki has no
  agreement with Spotify on this path. It's Spotify's public page metadata
  relayed by Discord, which is lower risk than B but not zero.
- Does the server want presence-based features, given the privacy trade-off?
- Discord's Spotify embed fields are inferred from Spotify's Open Graph tags,
  not from a captured Discord payload. Confirm against a real message in
  production (`raw->'embeds'`).

## Sources

- Spotify:
  [Developer Terms](https://developer.spotify.com/terms),
  [Developer Policy](https://developer.spotify.com/policy),
  [quota modes](https://developer.spotify.com/documentation/web-api/concepts/quota-modes),
  [February 2026 migration guide](https://developer.spotify.com/documentation/web-api/tutorials/february-2026-migration-guide),
  [2026-07-23 quota update](https://developer.spotify.com/blog/2026-07-23-web-api-quota-updates),
  [refresh token expiry](https://developer.spotify.com/blog/2026-06-18-refresh-token-expiration),
  [2024-11-27 changes](https://developer.spotify.com/blog/2024-11-27-changes-to-the-web-api),
  [OAuth security changes](https://developer.spotify.com/blog/2025-02-12-increasing-the-security-requirements-for-integrating-with-spotify),
  [OAuth migration reminder](https://developer.spotify.com/blog/2025-10-14-reminder-oauth-migration-27-nov-2025),
  [rate limits](https://developer.spotify.com/documentation/web-api/concepts/rate-limits),
  [oEmbed](https://developer.spotify.com/documentation/embeds/reference/oembed).
- Discord:
  [Gateway events](https://docs.discord.com/developers/events/gateway-events),
  [Gateway intents](https://docs.discord.com/developers/events/gateway).
- Client library:
  [discord.py `Spotify` activity](https://github.com/Rapptz/discord.py/blob/master/discord/activity.py).
