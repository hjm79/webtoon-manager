# Unified webtoon integration

The browser uses one service origin. Collection and the library live in the manager, while the existing viewer is reached through `/viewer/` on that same origin.

## Addresses

| Purpose | Address or configuration |
| --- | --- |
| Browser application | The manager's public origin, for example `http://192.168.1.26:8230` |
| Browser viewer paths | `VIEWER_PUBLIC_PATH=/viewer` on the manager and `BASE_PATH=/viewer` on the viewer |
| Manager-to-viewer transport | `VIEWER_INTERNAL_URL=http://webtoon-server:8000` on the private Docker network |
| Links produced by the viewer | Its `PUBLIC_BASE_URL`, including `/viewer` |

The viewer needs no published host port. Keep the existing library mounts and database volumes in place: series and chapter identities depend on the library references and filenames, not the public origin.

## Read-only consumer contract

These paths are suitable for a later My Supporter connection:

| Request | Purpose |
| --- | --- |
| `GET /api/viewer/config` | Discover whether the integrated viewer is enabled and its public paths. Internal service addresses and tokens are not returned. |
| `GET /viewer/api/series` | List accessible series, covers, chapter counts, unread counts, and current reading summaries. |
| `GET /viewer/api/series/{id}/continue` | Resolve the saved `chapter_id`, zero-based `page_index`, and optional normalized `page_offset`. |
| `/viewer/reader.html?series={id}` | Open a series at the saved position. Omit `chapter` and `page` to follow the saved position. |
| `GET /api/workflow/status` | Read collection progress, subscriptions, scopes, and the next scheduled download. |

Series list fields include `id`, `title`, `platform`, `chapter_count`, `unread_count`, `progress_display`, `reading_now`, `latest_update`, `cover_url`, and `list`. Treat IDs as opaque strings. Resolve relative viewer and cover paths against the webtoon application's public origin, rather than the dashboard's origin or an internal Docker hostname.

For server-side My Supporter data fetches, the viewer also exposes `GET /viewer/api/supporter/shelf?limit=12` and `GET /viewer/api/supporter/health`. These require the dedicated `SUPPORTER_API_TOKEN` as a Bearer header. An unset token returns 503; a wrong token returns 401. Shared-profile paths cannot call these endpoints. Keep this token on the dashboard server; do not put it in browser URLs or use the scan integration token.

The shelf returns `items` with `id`, `title`, `platform`, `coverUrl`, `href`, `lastReadAt`, `pageIndex`, `pageOffset`, `chapterCount`, `unreadCount`, `lastChapter`, `latestChapter`, `isNew`, `readingNow`, and `latestUpdatedAt`. Results sort by last-read time, then local file update time; `limit` accepts 1 through 50. `isNew` describes unread local files, not a remote provider's release schedule. The endpoint does not scan archives or change reading state. Public links use the configured viewer `PUBLIC_BASE_URL`; the reader link contains only the series ID so opening it resolves the latest saved position.

## Authentication boundary

The proxy preserves the viewer's normal browser authentication, profile permissions, and device/time restrictions. It does not add the server-side scan integration token to browser requests. A `401` or `403` response is an authentication or authorization result, not an empty library.

A future dashboard should keep its data-fetch credentials on its server and keep the browser's public link origin separate from its server-side transport address. The existing scan `INTEGRATION_TOKEN` is for scan coordination and must not be put into browser links or dashboard client bundles.

Same-origin reader embedding works with the application's current origin. Cross-origin embedding still requires the existing exact-origin allowlist. If My Supporter later embeds the reader from a separate HTTPS origin, configure a reachable HTTPS viewer origin and the exact allowed parent origin at that time.

This contract prepares the webtoon application for a later connection; it does not install a My Supporter widget, change its deployment, or introduce SSO.
