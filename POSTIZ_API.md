# Postiz Public API — verified integration reference (2026-07-06)

Source: docs.postiz.com (public-api/*), github.com/gitroomhq/postiz-app issues #717, #1147.

## Base + auth
- Self-hosted base: `{POSTIZ_API_URL}/public/v1` (POSTIZ_API_URL = backend origin, no trailing slash). If 404, try `/api/public/v1` (doc discrepancy).
- Auth header: `Authorization: <raw api key>` — **no "Bearer" prefix**.
- Key generated in Postiz UI: Settings → Developers → Public API.

## Endpoints
| Purpose | Method | Path |
|---|---|---|
| List channels | GET | `/integrations` |
| Upload media | POST | `/upload` (multipart, field `file`, max 50MB, video MP4 only) |
| Create/schedule/draft post | POST | `/posts` |
| List posts | GET | `/posts?startDate=<ISO>&endDate=<ISO>` |
| Delete post | DELETE | `/posts/:id` |
| Platform analytics | GET | `/analytics/:integrationId?date=30` |

- Do NOT use `/upload-from-url` — known bug (#1147): returned path lacks extension and post creation validation rejects it.
- Upload response: `{id, path, name, organizationId, createdAt, updatedAt}` — pass BOTH `id` and `path` when attaching.

## GET /integrations response item
```json
{"id": "cm4e...", "name": "Display Name", "identifier": "x", "picture": "...", "disabled": false, "profile": "handle", "customer": {...}}
```
- `identifier` is the platform slug: `x`, `tiktok`, `instagram`, `youtube`, `linkedin`, `threads`, ...
- `disabled: true` = OAuth expired; posting will fail.
- Map campaign channel names against `name`/`profile`.

## POST /posts body (all four top-level fields required, even for drafts)
```json
{
  "type": "schedule",            // "now" | "schedule" | "draft"
  "date": "2026-07-10T14:00:00.000Z",   // ISO UTC; required even for draft
  "shortLink": false,
  "tags": [],
  "posts": [
    {
      "integration": {"id": "<uuid from /integrations>"},
      "value": [{"content": "caption text", "image": [{"id": "<upload id>", "path": "<upload path>"}]}],
      "settings": { ...platform settings, must include "__type": "<identifier>" }
    }
  ]
}
```
Response: `[{"postId": "...", "integration": "..."}]`

### Platform settings objects
```json
// x
{"__type": "x", "who_can_reply_post": "everyone", "community": ""}
// tiktok
{"__type": "tiktok", "privacy_level": "PUBLIC_TO_EVERYONE", "duet": false, "stitch": false,
 "comment": true, "autoAddMusic": "no", "brand_content_toggle": false,
 "brand_organic_toggle": false, "video_made_with_ai": false, "content_posting_method": "DIRECT_POST"}
// instagram
{"__type": "instagram", "post_type": "reel", "is_trial_reel": false, "collaborators": []}
// youtube
{"__type": "youtube", "title": "...", "type": "public", "selfDeclaredMadeForKids": "no", "tags": []}
// threads/bluesky/etc: just {"__type": "<identifier>"}
```

## Post status / permalink (for analytics matching)
`GET /posts` items include: `id`, `content`, `publishDate`, `releaseURL` (permalink once `state=PUBLISHED`), `state` (`QUEUE|PUBLISHED|ERROR|DRAFT`), `integration.{id, providerIdentifier, name}`.
→ Store `releaseURL` into `clips.posted_permalinks` and flip status to `posted` when `state=PUBLISHED`.

## Rate limits / Railway
- Create-post: 90 req/hour self-hosted (override with `API_LIMIT` env), 100/hour cloud. Other endpoints possibly ~30/hour — throttle politely.
- Railway: set `NEXT_PUBLIC_BACKEND_URL` on the Postiz service; `IS_GENERAL=true` for single-tenant. TikTok requires the uploaded media URL to be publicly reachable over HTTPS (matters for storage provider choice).
