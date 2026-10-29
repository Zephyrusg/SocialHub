# Connect a YouTube channel

YouTube uses an independent Google Web Application OAuth client. Authelia still
protects SocialHub administration and ChatGPT access; it does not issue Google
API credentials. Existing Meta settings and authorizations are unchanged.

## Google Cloud setup

1. In [Google Cloud Console](https://console.cloud.google.com/), create/select a
   dedicated SocialHub project. Enable **YouTube Data API v3** in APIs & Services
   → Library. A public API key is not a substitute for channel OAuth.
2. In Google Auth Platform, configure Branding (app name, support/developer
   contact and the real application homepage/privacy policy), Audience and Data
   Access. For personal Gmail use External; Internal is only appropriate for
   an eligible Workspace organization. Initially use Testing and add the owning
   Google account as a test user. Do not invent public legal/support URLs.
3. Add only these scopes:
   `https://www.googleapis.com/auth/youtube.readonly` and
   `https://www.googleapis.com/auth/youtube.upload`.
4. Create a **Web application** OAuth client. Register the exact redirect URI
   formed by SocialHub's configured `BROWSER_ORIGIN` plus:
   **`/youtube/oauth/callback`**. This is the implemented callback, not the
   Authelia callback, Meta callback or ChatGPT connector callback. It uses HTTPS
   and must match exactly, including the absence of a trailing slash. No
   JavaScript origin or browser-side client secret is needed.
5. Obtain the real channel ID from YouTube Studio → Settings → Channel →
   Advanced settings (or YouTube account advanced settings). It starts `UC` and
   has 24 characters. A handle, display name or Google email is insufficient.
   Configure deployment `YOUTUBE_CHANNEL_IDS` with only that ID. Keep
   `YOUTUBE_ENABLED=true` for connection/read testing, and all YouTube publishing
   ceilings/preferences false. Do not expand upload quotas or storage silently.
6. After an approved deployment, use SocialHub **Connections → YouTube** to
   enter the Client ID and write-only Client Secret in your normal authenticated
   administrator browser. Never send either a secret or an OAuth link/code/token
   through chat, screenshots or Git. Blank secret preserves its saved value.
7. Choose Connect and continue to Google. Select the correct owning Google
   account and then the correct YouTube/Brand channel. Grant the two requested
   scopes. SocialHub requires one returned channel matching the exact configured
   ID; ambiguous, wrong or non-allowlisted channels are refused. If Google does
   not offer the intended Brand channel, cancel, switch YouTube's active channel
   and reconnect. Do not authorize a similarly named personal channel instead.
8. Return to Connections. Only a successful token exchange and real channel
   verification marks it connected. Reconnect requests offline consent. Local
   Disconnect removes SocialHub's stored channel credential and disables local
   account access; it does not remotely revoke other grants for the Google
   project. You may separately revoke SocialHub in Google account permissions.

The production-specific exact URI and deployment values are recorded in the
ignored `private/phase3f/` owner handoff. Public examples use your configured
origin and contain no private infrastructure or identity details.

## Consent, refresh and verification

Google Testing authorizations for these non-basic scopes can have refresh tokens
that expire after seven days. Production consent, sensitive-scope verification
and YouTube API compliance/audit are different processes. Publishing the consent
screen does not itself certify YouTube API compliance. Refresh tokens can also
expire or be revoked independently; SocialHub reports reconnection requirements,
never attempts password automation and never exposes Google's raw error body.
See [Google token expiration](https://developers.google.com/identity/protocols/oauth2#expiration)
and [web server authorization](https://developers.google.com/identity/protocols/oauth2/web-server).

As checked on 2026-10-29, Google's public references have conflicting guidance
about unverified API project public-upload restrictions. Treat actual visibility
as unverified until a separately approved upload is inspected. Follow the
[YouTube compliance/audit route](https://support.google.com/youtube/contact/yt_api_form)
if required; do not bypass a private restriction by another update request.
SocialHub sends explicit requested privacy and reports the returned actual
privacy independently. Initial previews default to Private. No live upload is
part of development verification and no existing release may be edited.

## Current quota and Shorts behavior

Current [official quota documentation](https://developers.google.com/youtube/v3/determine_quota_cost)
lists default daily buckets of 100 video insert calls, 100 search calls and
10,000 combined units for other endpoints. Project Console values govern your
actual allocation; avoid outdated 1,600-unit upload assumptions. Library listing
uses the channel uploads playlist, not search. Quota exhaustion requires waiting
or an approved quota request, not blind retries.

Shorts use the ordinary videos.insert endpoint. Current
[Shorts rules](https://support.google.com/youtube/answer/15424877) describe square
or vertical videos up to three minutes, including new Official Artist Channel
uploads. The preview reports expected eligibility, not guaranteed classification
or freedom from copyright restrictions. Standard videos can also be classified
as Shorts by YouTube. Hashtags belong in the structured description; tags are a
separate array. Explicit Made for Kids and synthetic-media declarations are
required; fantasy artwork/music does not decide audience automatically.
