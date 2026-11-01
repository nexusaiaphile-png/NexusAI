-- NexusAI HPP/POS correlation: additive snapshot reference column.
-- Apply only after reviewing the production schema and approving this migration.
ALTER TABLE nexusai_pos_exception_video_links
    ADD COLUMN IF NOT EXISTS snapshot_url TEXT;
