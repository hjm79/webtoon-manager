# Unified webtoon integration

The browser uses one service origin. Collection and the library live in the manager, while the existing viewer is reached through `/viewer/` on that same origin.

Both components are maintained in this repository: the manager at the root and the viewer under `viewer/`. `compose.integrated.yml` builds both without checking out a separate viewer fork. See [repository layout and upgrade preservation](REPOSITORY_LAYOUT.md).

## Addresses

| Purpose | Address or configuration |
| --- | --- |
| Browser application | The manager's public origin, for example `http://192.168.1.26:8230` |
| Browser viewer paths | `VIEWER_PUBLIC_PATH=/viewer` on the manager and `BASE_PATH=/viewer` on the viewer |
| Manager-to-viewer transport | `VIEWER_INTERNAL_URL=http://webtoon-server:8000` on the private Docker network |
| Links produced by the viewer | Its `PUBLIC_BASE_URL`, including `/viewer` |

The viewer needs no published host port. Keep the existing library mounts and database volumes in place: series and chapter identities depend on the library references and filenames, not the public origin.

## Subscription and download queue

The scope dialog offers `구독만 저장` separately from `구독하고 다운로드 대기열 추가`. Saving a new subscription is allowed while another download is running and does not add it to that running batch. An existing subscription's `다운로드 추가` button queues its saved scope without changing it.

`POST /api/workflow/subscribe` uses `start_now: false` for subscription-only storage and `start_now: true` for durable FIFO enqueueing. `POST /api/workflow/queue` enqueues an active subscription by `platform` and `title_id`. The status endpoint returns `queue` and per-subscription `queue_position`. The worker waits for current automatic or manual downloads, executes one queued title at a time, and continues after a failed title. Already-downloaded episodes remain in place and are skipped by the existing downloader.

The `workflow_download_queue` setting persists pending and running items. Startup recovers interrupted running items as queued. Duplicate queue entries and attempts to change a queued title's scope are rejected with 409. Unsubscribing a pending title cancels its queue entry atomically; unsubscribing a title already running from the queue is rejected. Normal and wait-free automatic batches exclude queued titles, and each batch fixes its target subscription snapshot before execution.

Search titles open the provider's official work page in a new tab. Covers and the separate `회차 미리보기` button open an in-page episode preview. The separate `읽기` button opens downloaded files in the embedded reader. Previewing does not subscribe or download.

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

Library cards also consume `summary` (plain text, up to 1,000 characters), `latest_chapter` (`id`, `label`, `date`, `date_source`), and `recently_read_chapter` (`id`, `label`). Chapter fields are null when unavailable. The latest date is the highest held chapter file's modification time, not its publication date; `file_modified` is displayed as `파일 갱신` in Korean time, and unknown dates remain unknown. The last-read chapter follows the saved cursor, including completed series. These values reuse cached library metadata without additional file, archive, or remote reads during the listing request.

For server-side My Supporter data fetches, the viewer also exposes `GET /viewer/api/supporter/shelf?limit=12` and `GET /viewer/api/supporter/health`. These require the dedicated `SUPPORTER_API_TOKEN` as a Bearer header. An unset token returns 503; a wrong token returns 401. Shared-profile paths cannot call these endpoints. Keep this token on the dashboard server; do not put it in browser URLs or use the scan integration token.

Shelf `coverUrl` values use `GET /viewer/api/supporter/covers/{id}`. Fetch these on the dashboard server with the same Bearer header and serve them through the dashboard's image proxy. Direct browser image tags cannot attach this header. Do not append credentials to URLs. The normal viewer cover endpoint retains its browser authentication. The dedicated endpoint returns `no-store`, rejects unknown or excluded series with 404, and bounds the wait for a single-series cache miss; it does not scan the library.

The shelf returns `items` with `id`, `title`, `platform`, `coverUrl`, `href`, `lastReadAt`, `pageIndex`, `pageOffset`, `chapterCount`, `unreadCount`, `lastChapter`, `latestChapter`, `isNew`, `readingNow`, and `latestUpdatedAt`. Results sort by last-read time, then local file update time; `limit` accepts 1 through 50. `isNew` describes unread local files, not a remote provider's release schedule. The endpoint does not scan archives or change reading state. Public links use the configured viewer `PUBLIC_BASE_URL`; the reader link contains only the series ID so opening it resolves the latest saved position.

When only a legacy reading cursor exists, unread counts include the chapters before that cursor without creating or migrating read-history rows.

## Authentication boundary

The proxy preserves the viewer's normal browser authentication, profile permissions, and device/time restrictions. It does not add the server-side scan integration token to browser requests. A `401` or `403` response is an authentication or authorization result, not an empty library.

A future dashboard should keep its data-fetch credentials on its server and keep the browser's public link origin separate from its server-side transport address. The existing scan `INTEGRATION_TOKEN` is for scan coordination and must not be put into browser links or dashboard client bundles.

Same-origin reader embedding works with the application's current origin. Cross-origin embedding still requires the existing exact-origin allowlist. If My Supporter later embeds the reader from a separate HTTPS origin, configure a reachable HTTPS viewer origin and the exact allowed parent origin at that time.

This contract prepares the webtoon application for a later connection; it does not install a My Supporter widget, change its deployment, or introduce SSO.

## Kakao authentication recovery

Cookie imports are validated and checked against the provider before replacing the encrypted active credential set. Replacement and server-issued updates use a credential revision; an older worker cannot save over a newer import. Cookie expiry, deletion, domain and path attributes are retained. Authentication health distinguishes verified login, rejection, an inconclusive connection, missing credentials and unreadable storage; the exported cookie expiry remains an estimate.

`GET /api/workflow/status` includes a non-secret `kakao_auth` summary. A rejected queued title remains `blocked_auth`; temporary connection failures remain `retry_wait` and retry after five minutes. Other queued Naver titles can continue. Successful cookie registration or an explicit successful login check releases these entries with their original scopes. Startup preserves blocked entries and recovers interrupted running entries. Existing completed files and reading history are not reset.

The application banner and Kakao settings show the last authentication check and preserved queue count independently of Discord. Notification suppression is recorded only after a successful send. Cookie revisions and health are preserved with the existing local credentials during backup restoration and are excluded from portable backups.

Saved web sessions are renewed using KakaoPage's web contract: a bodyless `POST https://bff-page.kakao.com/api/refresh_token`, with domain/path-scoped cookies, the page origin/referrer, a five-second timeout, and redirects disabled. The returned access token is not copied into settings. Server-issued cookies are first held in an isolated candidate and verified with `get_profile`; only a verified candidate replaces the encrypted cookie set. Its promotion, healthy status and blocked-queue release commit together. Credential generations remain unchanged while the storage revision advances, so a concurrent manual cookie replacement cannot be overwritten.

An internal check runs at startup and every minute to see whether the thirty-minute refresh is due. Temporary refresh failures retry after five minutes and preserve the last confirmed login result. If both refresh and the original profile are rejected, the app reports that a new login is needed and waits thirty minutes before its next automatic attempt. If the original profile is still valid, the app keeps using that session and can attempt an earlier refresh after a later authentication rejection. The original response must succeed before any of its cookies can be saved. Each refresh attempt owns its result, so a late response cannot replace a newer attempt or override a session whose cookie attributes have changed. Only read-only GET requests can be replayed once; after a ticket-use POST is rejected, the durable queue rechecks episode availability.
