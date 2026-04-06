-- migration.sql — Add full-text search vector and quality score
-- Run ONCE against your Cloud SQL PostgreSQL database.
-- psql -h 34.93.217.19 -U scraper -d domains -f migration.sql

-- ── Step 1: Add search_vector column ─────────────────────────────────────
ALTER TABLE domain_metadata
    ADD COLUMN IF NOT EXISTS search_vector tsvector;

-- ── Step 2: Add quality_score column ─────────────────────────────────────
ALTER TABLE domain_metadata
    ADD COLUMN IF NOT EXISTS quality_score INTEGER DEFAULT 0;

-- ── Step 3: Backfill search_vector for all existing rows ─────────────────
-- This runs in batches to avoid locking the table. For 2M rows this may
-- take 10-30 minutes. You can run it in a tmux/screen session.
UPDATE domain_metadata
SET search_vector = to_tsvector(
    'english',
    coalesce(title, '') || ' ' ||
    coalesce(description, '') || ' ' ||
    coalesce(domain, '') || ' ' ||
    coalesce(location, '') || ' ' ||
    coalesce(category, '')
)
WHERE search_vector IS NULL;

-- ── Step 4: Create GIN index for fast full-text search ───────────────────
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_meta_search_vector
    ON domain_metadata USING GIN(search_vector);

-- ── Step 5: Create trigger to keep search_vector updated automatically ───
CREATE OR REPLACE FUNCTION update_search_vector()
RETURNS trigger AS $$
BEGIN
    NEW.search_vector := to_tsvector(
        'english',
        coalesce(NEW.title, '') || ' ' ||
        coalesce(NEW.description, '') || ' ' ||
        coalesce(NEW.domain, '') || ' ' ||
        coalesce(NEW.location, '') || ' ' ||
        coalesce(NEW.category, '')
    );
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trig_search_vector ON domain_metadata;
CREATE TRIGGER trig_search_vector
    BEFORE INSERT OR UPDATE OF title, description, domain, location, category
    ON domain_metadata
    FOR EACH ROW EXECUTE FUNCTION update_search_vector();

-- ── Step 6: Add composite index to speed up discover query ordering ───────
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_meta_status_category
    ON domain_metadata(status_code, category)
    WHERE status_code = 200;

-- ── Done — verify ────────────────────────────────────────────────────────
SELECT
    count(*) FILTER (WHERE search_vector IS NOT NULL) AS rows_with_vector,
    count(*) AS total_rows
FROM domain_metadata;
