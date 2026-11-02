# HPP cloud alarm → POS exception correlation

This feature extends the existing Hik-Partner Pro message-queue consumer. It
does not create an HTTP webhook receiver or start a second consumer.

## Processing path

1. The existing HPP worker long-polls `/api/hpcgw/v1/mq/messages`.
2. The HPP adapter preserves `happenTime`, device serial, and alarm payload.
3. The existing site/device mapping resolves the HPP device to a NexusAI site.
4. `hpp_pos_correlation.correlate_hpp_event` reads flagged records from the
   production `public.nexusai_pos_exceptions` table using verified columns:
   `id`, `site_id`, `occurred_at`, `camera_id`, `source_event_id`, and
   `flagged_for_review`.
5. Since production stores `occurred_at` as TEXT, timestamps are parsed in
   Python. Invalid or timezone-naive values are skipped rather than causing a
   database cast failure. Candidate matching uses site, HPP device serial
   (exact match or `<serial>-` prefix), and the nearest event time within the
   configured window.
6. If a matching exception and vendor `filePath` exist, NexusAI calls the
   existing HPP `/api/hpcgw/v1/alarm/pictureurl` operation.
7. The returned HTTPS snapshot URL and HPP metadata are stored as JSON in the
   existing `source_ref_id` TEXT column of
   `public.nexusai_pos_exception_video_links`. No schema migration is needed.
   The snapshot is not written to `video_clip_url`, because a still image is
   not a playback clip.
8. The HPP batch is offset only after the event sink completes successfully.
   A database or HPP media error raises and leaves the batch available for retry.

## Production schema assumptions verified

- `nexusai_pos_exceptions.id` is the primary key.
- POS exception columns used by this feature exist; `occurred_at` and
  `camera_id` are TEXT, and `flagged_for_review` is BOOLEAN.
- `nexusai_pos_exception_video_links.exception_id` is NOT NULL, UNIQUE, and
  a foreign key to the POS exception table.
- The link table already contains `source_ref_id` TEXT and
  `match_status` TEXT NOT NULL. It has no `snapshot_url` column.
- `created_at` is DOUBLE PRECISION NOT NULL and is populated with epoch seconds.

## Configuration

- `DATABASE_URL`: PostgreSQL connection string used by the current application.
- `HPP_APP_KEY` and `HPP_SECRET_KEY`: existing server-side HPP credentials.
- `NEXUSAI_HPP_POS_MATCH_WINDOW_SECONDS`: match window, default 5, bounded to 1–30 seconds.
- `HPP_ALARM_WORKER_ENABLED`: existing HPP worker switch.

## Important limitations

- The current join is site + HPP device serial/camera_id + timestamp. It does not
  infer a till-to-camera relationship. POS records must already carry the
  matching HPP-derived camera identifier in `camera_id`.
- `source_event_id` is retained as the stable POS transaction reference in
  the exception record; the correlation link uses the POS primary key
  `exception_id` to satisfy the existing foreign key and unique constraint.
- This feature retrieves an alarm snapshot reference only. It does not request
  recorded playback clips; the pictureurl operation is not proof of a playback
  API capability.
- No new Render worker service is required: processing runs in the existing HPP
  queue consumer thread. Run only one active HPP consumer for the same account.
- A 5-second event-time match is a correlation candidate, not proof that a
  transaction caused the camera alarm.
