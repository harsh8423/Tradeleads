# db.py — PostgreSQL database layer (asyncpg)
# Uses a shared asyncpg connection pool — one pool per VM process.
# Optimized for 2M+ row scale:
#   - UNNEST bulk inserts (1 query regardless of batch size, not N queries)
#   - LEFT JOIN fetch_batch (hash join, not correlated subquery)
#   - Covering indexes on all hot query paths
#   - Each DDL isolated so a partial failure doesn't block all indexes
#   - pg_class reltuples estimate for COUNT(*) on large tables

import asyncio
import asyncpg
import logging
from config import (
    PG_HOST, PG_PORT, PG_DB, PG_USER, PG_PASSWORD,
    ALLOWED_CATEGORIES, SHARD_INDEX, SHARD_TOTAL, BATCH_SIZE,
)

log = logging.getLogger("contacts")

_pool: asyncpg.Pool | None = None


async def create_pool() -> asyncpg.Pool:
    global _pool
    _pool = await asyncpg.create_pool(
        host=PG_HOST, port=PG_PORT,
        database=PG_DB, user=PG_USER, password=PG_PASSWORD,
        min_size=2, max_size=8,      # 3 VMs × 8 = 24 total — safe for Cloud SQL
        command_timeout=120,
    )
    log.info("PostgreSQL pool created")
    return _pool


async def close_pool():
    if _pool:
        await _pool.close()


# ── Schema init ───────────────────────────────────────────────────────────────

# Each statement is isolated. A failure on one (e.g. table already exists with
# different schema) does NOT abort creation of subsequent indexes.
_DDL = [
    # Tables
    ("""
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
    )""", "domain_contacts table"),

    ("""
    CREATE TABLE IF NOT EXISTS domain_employees (
        id          SERIAL PRIMARY KEY,
        domain      TEXT,
        name        TEXT,
        title       TEXT,
        email       TEXT,
        phone       TEXT,
        linkedin    TEXT,
        fetched_at  TIMESTAMPTZ DEFAULT now()
    )""", "domain_employees table"),

    ("""
    CREATE TABLE IF NOT EXISTS domain_crawl_status (
        domain          TEXT PRIMARY KEY,
        category        TEXT,
        status_code     INTEGER,
        crawled_at      TIMESTAMPTZ DEFAULT now(),
        pages_crawled   INTEGER,
        contacts_found  INTEGER,
        error_msg       TEXT
    )""", "domain_crawl_status table"),

    # Optional column on domain_metadata
    ("ALTER TABLE domain_metadata ADD COLUMN IF NOT EXISTS company_address TEXT",
     "domain_metadata.company_address column"),

    # ── Indexes ───────────────────────────────────────────────────────────────
    # domain_contacts
    ("CREATE UNIQUE INDEX IF NOT EXISTS idx_contacts_dedup   ON domain_contacts(domain, entity_type, value)",
     "idx_contacts_dedup"),
    ("CREATE INDEX        IF NOT EXISTS idx_contacts_domain  ON domain_contacts(domain)",
     "idx_contacts_domain"),
    ("CREATE INDEX        IF NOT EXISTS idx_contacts_type    ON domain_contacts(entity_type)",
     "idx_contacts_type"),

    # domain_employees
    ("CREATE UNIQUE INDEX IF NOT EXISTS idx_employees_dedup  ON domain_employees(domain, name)",
     "idx_employees_dedup"),

    # domain_crawl_status — PK already indexes `domain` (used by LEFT JOIN)
    # No extra index needed; the TEXT PRIMARY KEY creates a B-tree automatically.

    # domain_metadata — covering index for fetch_batch query:
    #   WHERE status_code=200 AND category IN (...) AND domain > $x
    #   This lets PG satisfy the whole WHERE + ORDER BY from the index (index-only scan).
    ("CREATE INDEX IF NOT EXISTS idx_meta_status_cat_domain  ON domain_metadata(status_code, category, domain)",
     "idx_meta_status_cat_domain"),
]


async def init_db():
    """Create tables and indexes individually — each in its own try/except
    so a failure on one DDL statement never blocks the rest."""
    async with _pool.acquire() as conn:
        for stmt, label in _DDL:
            try:
                await conn.execute(stmt, timeout=300)
                log.info(f"  DDL OK: {label}")
            except Exception as e:
                log.warning(f"  DDL skipped [{label}]: {e}")
    log.info("init_db complete")


# ── Batch fetch ───────────────────────────────────────────────────────────────

async def count_pending() -> tuple[int, int]:
    """Return (total_200ok, already_crawled). Both are exact counts."""
    async with _pool.acquire() as conn:
        # Must filter to status_code=200 — reltuples counts ALL rows regardless
        # of WHERE clause and would inflate pending count with non-200 domains.
        total = await conn.fetchval(
            "SELECT COUNT(*) FROM domain_metadata WHERE status_code = 200",
            timeout=120,
        )
        done = await conn.fetchval("SELECT COUNT(*) FROM domain_crawl_status", timeout=60)
    return int(total or 0), int(done or 0)


async def fetch_batch(last_domain: str = ""):
    """Fetch the next batch using a LEFT JOIN (hash join, O(1) per row).

    The covering index on domain_metadata(status_code, category, domain)
    lets PostgreSQL do an index-only scan — no heap access, O(log n) seek.
    The LEFT JOIN against domain_crawl_status uses the TEXT PRIMARY KEY index.
    Together these make fetch_batch stay fast at 2M rows.
    """
    n = len(ALLOWED_CATEGORIES)
    cat_placeholders = ", ".join(f"${i+1}" for i in range(n))
    query = (
        f"SELECT dm.domain, dm.category "
        f"FROM domain_metadata dm "
        f"LEFT JOIN domain_crawl_status cs ON cs.domain = dm.domain "
        f"WHERE dm.status_code = 200 "
        f"  AND dm.category IN ({cat_placeholders}) "
        f"  AND (hashtext(dm.domain) & 2147483647) % ${n+1} = ${n+2} "
        f"  AND dm.domain > ${n+4} "
        f"  AND cs.domain IS NULL "
        f"ORDER BY dm.domain ASC "
        f"LIMIT ${n+3}"
    )
    params = tuple(ALLOWED_CATEGORIES) + (SHARD_TOTAL, SHARD_INDEX, BATCH_SIZE, last_domain)
    try:
        async with _pool.acquire() as conn:
            rows = await conn.fetch(query, *params, timeout=120)
        return [(r["domain"], r["category"]) for r in rows]
    except Exception as e:
        log.error(f"fetch_batch error: {e}")
        return None


# ── Batch write (executemany) ─────────────────────────────────────────────────

async def write_contacts(contact_rows, employee_rows, status_rows, address_updates=None):
    if not contact_rows and not employee_rows and not status_rows and not address_updates:
        return

    for attempt in range(3):
        try:
            async with _pool.acquire() as conn:
                async with conn.transaction():
                    if contact_rows:
                        await conn.executemany("""
                            INSERT INTO domain_contacts
                                (domain, category, page_url, entity_type, value, label, context, fetched_at)
                            VALUES ($1,$2,$3,$4,$5,$6,$7,$8)
                            ON CONFLICT (domain, entity_type, value) DO NOTHING
                        """, contact_rows)

                    if employee_rows:
                        await conn.executemany("""
                            INSERT INTO domain_employees
                                (domain, name, title, email, phone, linkedin, fetched_at)
                            VALUES ($1,$2,$3,$4,$5,$6,$7)
                            ON CONFLICT (domain, name) DO NOTHING
                        """, employee_rows)

                    if status_rows:
                        await conn.executemany("""
                            INSERT INTO domain_crawl_status
                                (domain, category, status_code, crawled_at,
                                 pages_crawled, contacts_found, error_msg)
                            VALUES ($1,$2,$3,$4,$5,$6,$7)
                            ON CONFLICT (domain) DO UPDATE SET
                                status_code    = EXCLUDED.status_code,
                                crawled_at     = EXCLUDED.crawled_at,
                                pages_crawled  = EXCLUDED.pages_crawled,
                                contacts_found = EXCLUDED.contacts_found,
                                error_msg      = EXCLUDED.error_msg
                        """, status_rows)

                    if address_updates:
                        await conn.executemany("""
                            UPDATE domain_metadata
                            SET company_address = COALESCE(company_address, $1)
                            WHERE domain = $2
                        """, address_updates)

            return  # success

        except Exception as e:
            backoff = 10 * (attempt + 1)
            err_type = type(e).__name__
            err_msg  = str(e) or "(no message)"
            if attempt < 2:
                log.warning(f"DB write error (attempt {attempt+1}/3) [{err_type}]: {err_msg} — retry in {backoff}s")
                await asyncio.sleep(backoff)
            else:
                log.error(f"DB write FAILED after 3 attempts [{err_type}]: {err_msg}")
                raise


# ── Final stats ───────────────────────────────────────────────────────────────

async def print_final_stats():
    async with _pool.acquire() as conn:
        log.info("\n" + "=" * 55)
        log.info("  CONTACT TYPE BREAKDOWN")
        log.info("=" * 55)
        rows = await conn.fetch("""
            SELECT entity_type, COUNT(*) as cnt
            FROM domain_contacts
            GROUP BY entity_type ORDER BY 2 DESC
        """)
        for r in rows:
            log.info(f"  {r['entity_type']:<25} {r['cnt']:>10,}")

        total     = await conn.fetchval("SELECT COUNT(*) FROM domain_crawl_status")
        with_data = await conn.fetchval(
            "SELECT COUNT(*) FROM domain_crawl_status WHERE contacts_found > 0"
        )
        pct = with_data * 100 // total if total else 0
        log.info("=" * 55)
        log.info(f"  Domains crawled       : {total:,}")
        log.info(f"  Domains with contacts : {with_data:,} ({pct}%)")
        log.info("=" * 55)
