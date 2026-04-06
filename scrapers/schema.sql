-- schema.sql -- Database schema for domains.db
-- Location:  E:\domains.db
-- Updated:   2026-03-24

-- =====================================================
-- 1. Core table: all unique domains crawled
-- =====================================================
CREATE TABLE IF NOT EXISTS domains (
    domain       TEXT PRIMARY KEY,   -- e.g. "bestbaylogistics.com"
    last_crawled TEXT,               -- CDX timestamp "20260313180021"
    shard        TEXT,               -- source shard "cdx-00042.gz"
    category     TEXT                -- "logistics" | "exim" | "commodity" | NULL
);

CREATE INDEX IF NOT EXISTS idx_category ON domains(category);

-- =====================================================
-- 2. Shard download progress
-- =====================================================
CREATE TABLE IF NOT EXISTS progress (
    shard           TEXT PRIMARY KEY,
    status          TEXT,               -- "done" | "in_progress"
    new_domains     INTEGER,
    finished_at     TEXT,
    lines_processed INTEGER DEFAULT 0
);

-- =====================================================
-- 3. Domain metadata (HTML <head> fetch results)
-- =====================================================
CREATE TABLE IF NOT EXISTS domain_metadata (
    domain         TEXT PRIMARY KEY,     -- FK to domains.domain
    category       TEXT,                 -- copied from domains.category
    status_code    INTEGER,             -- HTTP status or negative error code
    title          TEXT,                 -- <title> tag content
    description    TEXT,                 -- meta description
    language       TEXT,                 -- html lang attribute
    location       TEXT,                 -- geo.region / geo.placename
    company_address TEXT,                -- scraped footer/contact address
    redirected_url TEXT,                 -- final URL after redirects
    fetch_ms       INTEGER,             -- fetch duration in milliseconds
    fetched_at     TEXT,                 -- UTC timestamp
    error_msg      TEXT                  -- error details or NULL
);

-- Status codes:  200=OK, 301/302=redirect, 403=forbidden, 404=not found
-- Negative codes: -1=timeout, -2=dns_fail, -3=conn_error, -4=ssl, -5=redirects, -6=other

CREATE INDEX IF NOT EXISTS idx_meta_category ON domain_metadata(category);
CREATE INDEX IF NOT EXISTS idx_meta_status   ON domain_metadata(status_code);

-- =====================================================
-- PRAGMAs (set at runtime)
-- =====================================================
-- PRAGMA journal_mode=WAL;
-- PRAGMA synchronous=NORMAL;
-- PRAGMA cache_size=-131072;  -- 128MB

-- =====================================================
-- 4. Domain Contacts (fast parsed data)
-- =====================================================
CREATE TABLE IF NOT EXISTS domain_contacts (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    domain       TEXT NOT NULL,
    category     TEXT,
    page_url     TEXT,
    entity_type  TEXT NOT NULL,
    value        TEXT NOT NULL,
    label        TEXT,
    context      TEXT,
    fetched_at   TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_contacts_dedup ON domain_contacts(domain, entity_type, value);
CREATE INDEX IF NOT EXISTS idx_contacts_domain ON domain_contacts(domain);
CREATE INDEX IF NOT EXISTS idx_contacts_type ON domain_contacts(entity_type);

-- =====================================================
-- 5. Domain Employees (extracted corporate team data)
-- =====================================================
CREATE TABLE IF NOT EXISTS domain_employees (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    domain      TEXT,
    name        TEXT,
    title       TEXT,
    email       TEXT,
    phone       TEXT,
    linkedin    TEXT,
    fetched_at  TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_employees_dedup ON domain_employees(domain, name);

-- =====================================================
-- 6. Crawl Status (Playwright fallback tracking)
-- =====================================================
CREATE TABLE IF NOT EXISTS domain_crawl_status (
    domain      TEXT PRIMARY KEY,
    category    TEXT,
    status_code INTEGER,
    crawled_at  TEXT,
    pages_crawled INTEGER,
    contacts_found INTEGER,
    used_playwright INTEGER DEFAULT 0,
    error_msg   TEXT
);
