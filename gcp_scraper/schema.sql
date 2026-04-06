-- schema.sql — PostgreSQL schema for Cloud SQL
-- Run this ONCE before migration or VM startup.
-- psql -h <HOST> -U scraper -d domains -f schema.sql

-- ── Core tables ───────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS domains (
    domain       TEXT PRIMARY KEY,
    last_crawled TEXT,
    shard        TEXT,
    category     TEXT
);
CREATE INDEX IF NOT EXISTS idx_domains_category ON domains(category);

CREATE TABLE IF NOT EXISTS domain_metadata (
    domain          TEXT PRIMARY KEY,
    category        TEXT,
    status_code     INTEGER,
    title           TEXT,
    description     TEXT,
    language        TEXT,
    location        TEXT,
    redirected_url  TEXT,
    fetch_ms        INTEGER,
    fetched_at      TIMESTAMPTZ,
    error_msg       TEXT,
    search_vector   TSVECTOR,
    quality_score   INT NOT NULL DEFAULT 0,
    email_count     INT NOT NULL DEFAULT 0,
    phone_count     INT NOT NULL DEFAULT 0,
    linkedin_count  INT NOT NULL DEFAULT 0,
    social_count    INT NOT NULL DEFAULT 0,
    emp_count       INT NOT NULL DEFAULT 0,
    _quality_synced BOOLEAN DEFAULT FALSE
);
CREATE INDEX IF NOT EXISTS idx_meta_category        ON domain_metadata(category);
CREATE INDEX IF NOT EXISTS idx_meta_status          ON domain_metadata(status_code);
-- Index for fast sorted lead discovery
CREATE INDEX IF NOT EXISTS idx_meta_quality         ON domain_metadata(quality_score DESC);
-- GIN index for full-text search
CREATE INDEX IF NOT EXISTS idx_meta_search_vector   ON domain_metadata USING GIN(search_vector);

-- Covering index for fetch_batch LEFT JOIN: status_code + category filter + keyset domain
-- Allows index-only scan — no heap access. Stays O(log n) at 2M rows.
CREATE INDEX IF NOT EXISTS idx_meta_status_cat_domain ON domain_metadata(status_code, category, domain);

CREATE TABLE IF NOT EXISTS domain_contacts (
    id           SERIAL PRIMARY KEY,
    domain       TEXT NOT NULL,
    category     TEXT,
    page_url     TEXT,
    entity_type  TEXT NOT NULL,
    value        TEXT NOT NULL,
    label        TEXT,
    context      TEXT,
    fetched_at   TIMESTAMPTZ DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_contacts_dedup  ON domain_contacts(domain, entity_type, value);
CREATE INDEX        IF NOT EXISTS idx_contacts_domain ON domain_contacts(domain);
CREATE INDEX        IF NOT EXISTS idx_contacts_type   ON domain_contacts(entity_type);

CREATE TABLE IF NOT EXISTS domain_employees (
    id          SERIAL PRIMARY KEY,
    domain      TEXT,
    name        TEXT,
    title       TEXT,
    email       TEXT,
    phone       TEXT,
    linkedin    TEXT,
    fetched_at  TIMESTAMPTZ DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_employees_dedup ON domain_employees(domain, name);

CREATE TABLE IF NOT EXISTS domain_crawl_status (
    domain          TEXT PRIMARY KEY,    -- TEXT PK auto-creates B-tree on domain
    category        TEXT,
    status_code     INTEGER,
    crawled_at      TIMESTAMPTZ DEFAULT now(),
    pages_crawled   INTEGER,
    contacts_found  INTEGER,
    error_msg       TEXT
);

-- ── Shard progress (optional: monitor per-VM progress) ─────────NOT USED───────────────
CREATE TABLE IF NOT EXISTS progress (
    shard           TEXT PRIMARY KEY,
    status          TEXT,
    new_domains     INTEGER,
    finished_at     TIMESTAMPTZ,
    lines_processed INTEGER DEFAULT 0
);
