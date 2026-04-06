-- v2 migration: Adding denormalized columns to avoid runtime JOINs and enable fast ORDER BY
ALTER TABLE domain_metadata ADD COLUMN IF NOT EXISTS email_count INT NOT NULL DEFAULT 0;
ALTER TABLE domain_metadata ADD COLUMN IF NOT EXISTS phone_count INT NOT NULL DEFAULT 0;
ALTER TABLE domain_metadata ADD COLUMN IF NOT EXISTS linkedin_count INT NOT NULL DEFAULT 0;
ALTER TABLE domain_metadata ADD COLUMN IF NOT EXISTS social_count INT NOT NULL DEFAULT 0;
ALTER TABLE domain_metadata ADD COLUMN IF NOT EXISTS emp_count INT NOT NULL DEFAULT 0;

-- Add index for fast sorted retrieval
CREATE INDEX IF NOT EXISTS idx_meta_quality ON domain_metadata (quality_score DESC);
