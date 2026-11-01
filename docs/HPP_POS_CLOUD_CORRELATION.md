# HPP cloud alarm → POS exception correlation

This feature extends the existing Hik-Partner Pro message-queue consumer. It does
not create an HTTP webhook receiver and does not start a second consumer.

## Processing path

1. The existing HPP worker long-polls `/api/hpcgw/v1/mq/messages`.
2. The HPP adapter preserves `happenTime`, device serial, and alarm payload.
3. The existing site/device mapping resolves the HPP device to a NexusAI site.
4. `hpp_pos_correlation.correlate_hpp_event` searches the existing
   `nexusai_pos_exceptions` table for a flagged exception on that site whose
   `camera_id` equals the HPP serial or starts with `<serial>-`, within the
   configured time window.
5. If a matching exception and vendor `filePath` exist, NexusAI calls the
   existing HPP `/api/hpcgw/v1/alarm/pictureurl` operation.
6. The returned HTTPS snapshot URL is saved in the additive
   `snapshot_url` column on `nexusai_pos_exception_video_links`. It is never
   written to `video_clip_url`, because a still image is not a playback clip.
7. The HPP batch is offset only after the event sink completes successfully.
   A database or HPP media error raises and leaves the batch available for retry.

## Configuration

- `DATABASE_URL`: PostgreSQL connection string used by the current application.
- `HPP_APP_KEY` and `HPP_SECRET_KEY`: existing server-side HPP credentials.
- `NEXUSAI_HPP_POS_MATCH_WINDOW_SECONDS`: match window, default 5, bounded to 1–30 seconds.
- `HPP_ALARM_WORKER_ENABLED`: existing HPP worker switch.

The code uses the repository's existing POS exception source
`nexusai_pos_exceptions`. The repository code currently defines that as a
PostgreSQL table with `occurred_at`, `camera_id`, and `flagged_for_review`;
it does not establish that this table is a TimescaleDB hypertable or identify a
separate Shoprite transaction hypertable. Confirm the production schema before
claiming TimescaleDB-specific query optimisation or switching the source table.

## Database migration

Review and apply `migrations/20261102_add_hpp_snapshot_url.sql` only after
confirming the production table and approving a migration. The application does
not run this migration automatically. Until it is applied, a matched snapshot
cannot be persisted and the HPP batch will remain retryable.

## Important limitations

- The current join is site + HPP device serial/camera_id + timestamp. It does not
  infer a till-to-camera relationship. POS records must already carry the
  matching HPP-derived camera identifier in `camera_id`.
- This feature retrieves an alarm snapshot reference only. It does not request
  recorded playback clips; the current documented pictureurl operation is not
  proof of a playback API capability.
- No new Render worker service is required: processing runs in the existing HPP
  queue consumer thread. Run only one active HPP consumer for the same account.
- A 5-second event-time match is a correlation candidate, not proof that a
  transaction caused the camera alarm.
