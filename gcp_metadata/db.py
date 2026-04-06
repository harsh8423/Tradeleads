# db.py — PostgreSQL database layer for GCP metadata scraper
# Uses asyncpg connection pool.
# fetch_batch: LEFT JOIN against domain_metadata to skip already-fetched domains.
# write_metadata: UNNEST bulk-insert, idempotent (ON CONFLICT DO NOTHING).

import asyncio
import asyncpg
import logging
from config import (
    PG_HOST, PG_PORT, PG_DB, PG_USER, PG_PASSWORD,
    ALLOWED_CATEGORIES, SHARD_INDEX, SHARD_TOTAL, BATCH_SIZE,
)

log = logging.getLogger("metadata")

_pool: asyncpg.Pool | None = None


async def create_pool() -> asyncpg.Pool:
    global _pool
    _pool = await asyncpg.create_pool(
        host=PG_HOST, port=PG_PORT,
        database=PG_DB, user=PG_USER, password=PG_PASSWORD,
        min_size=2, max_size=8,
        command_timeout=120,
    )
    log.info("PostgreSQL pool created")
    return _pool


async def close_pool():
    if _pool:
        await _pool.close()


# ── Schema init ───────────────────────────────────────────────────────────────

_DDL = [
    ("""
    CREATE TABLE IF NOT EXISTS domain_metadata (
        domain         TEXT PRIMARY KEY,
        category       TEXT,
        status_code    INTEGER,
        title          TEXT,
        description    TEXT,
        language       TEXT,
        location       TEXT,
        redirected_url TEXT,
        fetch_ms       INTEGER,
        fetched_at     TIMESTAMPTZ,
        error_msg      TEXT,
        search_vector  TSVECTOR,
        quality_score  INT NOT NULL DEFAULT 0,
        email_count    INT NOT NULL DEFAULT 0,
        phone_count    INT NOT NULL DEFAULT 0,
        linkedin_count INT NOT NULL DEFAULT 0,
        social_count   INT NOT NULL DEFAULT 0,
        emp_count      INT NOT NULL DEFAULT 0,
        _quality_synced BOOLEAN DEFAULT FALSE
    )""", "domain_metadata table"),
    ("CREATE INDEX IF NOT EXISTS idx_meta_category ON domain_metadata(category)", "idx_meta_category"),
    ("CREATE INDEX IF NOT EXISTS idx_meta_status   ON domain_metadata(status_code)", "idx_meta_status"),
    ("CREATE INDEX IF NOT EXISTS idx_meta_quality  ON domain_metadata(quality_score DESC)", "idx_meta_quality"),
    ("CREATE INDEX IF NOT EXISTS idx_meta_status_cat_domain ON domain_metadata(status_code, category, domain)", "idx_meta_covering"),
]


async def init_db():
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
    """Return (exact_pending, already_fetched)."""
    async with _pool.acquire() as conn:
        n = len(ALLOWED_CATEGORIES)
        ph = ", ".join(f"${i+1}" for i in range(n))
        pending = await conn.fetchval(
            f"SELECT COUNT(*) FROM domains d "
            f"LEFT JOIN domain_metadata m ON d.domain = m.domain "
            f"WHERE d.category IN ({ph}) AND m.domain IS NULL",
            *ALLOWED_CATEGORIES, timeout=300
        )
        done = await conn.fetchval(
            "SELECT COUNT(*) FROM domain_metadata", timeout=60
        )
    return int(pending or 0), int(done or 0)


async def fetch_batch(last_domain: str = "") -> list[tuple[str, str]] | None:
    """
    Fetch next batch using LEFT JOIN (hash join, O(1) per row).
    Sharding: hashtext(domain) % SHARD_TOTAL = SHARD_INDEX
    Skips domains already in domain_metadata.
    """
    n = len(ALLOWED_CATEGORIES)
    cat_ph = ", ".join(f"${i+1}" for i in range(n))
    query = (
        f"SELECT d.domain, d.category "
        f"FROM domains d "
        f"LEFT JOIN domain_metadata m ON m.domain = d.domain "
        f"WHERE d.category IN ({cat_ph}) "
        f"  AND (hashtext(d.domain) & 2147483647) % ${n+1} = ${n+2} "
        f"  AND d.domain > ${n+3} "
        f"  AND m.domain IS NULL "
        f"ORDER BY d.domain ASC "
        f"LIMIT ${n+4}"
    )
    params = tuple(ALLOWED_CATEGORIES) + (SHARD_TOTAL, SHARD_INDEX, last_domain, BATCH_SIZE)
    try:
        async with _pool.acquire() as conn:
            rows = await conn.fetch(query, *params, timeout=120)
        return [(r["domain"], r["category"]) for r in rows]
    except Exception as e:
        log.error(f"fetch_batch error: {e}")
        return None


# ── Batch write ───────────────────────────────────────────────────────────────

async def write_metadata(rows: list[tuple]):
    """
    Bulk-insert metadata using UNNEST — one query for the whole batch.
    ON CONFLICT DO NOTHING = idempotent / crash-safe.
    rows: list of (domain, category, status_code, title, description,
                   language, location, redirected_url, fetch_ms, fetched_at, error_msg)
    """
    if not rows:
        return

    cols = {
        "domain":        [r[0]  for r in rows],
        "category":      [r[1]  for r in rows],
        "status_code":   [r[2]  for r in rows],
        "title":         [r[3]  for r in rows],
        "description":   [r[4]  for r in rows],
        "language":      [r[5]  for r in rows],
        "location":      [r[6]  for r in rows],
        "redirected_url":[r[7]  for r in rows],
        "fetch_ms":      [r[8]  for r in rows],
        "fetched_at":    [r[9]  for r in rows],
        "error_msg":     [r[10] for r in rows],
    }

    query = """
        INSERT INTO domain_metadata
            (domain, category, status_code, title, description,
             language, location, redirected_url, fetch_ms, fetched_at, error_msg)
        SELECT * FROM UNNEST(
            $1::text[], $2::text[], $3::int[], $4::text[], $5::text[],
            $6::text[], $7::text[], $8::text[], $9::int[], $10::timestamptz[], $11::text[]
        )
        ON CONFLICT (domain) DO NOTHING
    """

    for attempt in range(3):
        try:
            async with _pool.acquire() as conn:
                await conn.execute(
                    query,
                    cols["domain"], cols["category"], cols["status_code"],
                    cols["title"], cols["description"], cols["language"],
                    cols["location"], cols["redirected_url"], cols["fetch_ms"],
                    cols["fetched_at"], cols["error_msg"],
                    timeout=120
                )
            return
        except Exception as e:
            wait = 10 * (attempt + 1)
            log.warning(f"write_metadata attempt {attempt+1}/3 failed: {e} — retry in {wait}s")
            await asyncio.sleep(wait)
    log.error("write_metadata FAILED after 3 attempts — rows dropped")


# ── Final stats ───────────────────────────────────────────────────────────────

async def print_final_stats():
    async with _pool.acquire() as conn:
        total = await conn.fetchval("SELECT COUNT(*) FROM domain_metadata")
        rows  = await conn.fetch("""
            SELECT status_code, COUNT(*) as cnt
            FROM domain_metadata
            GROUP BY status_code
            ORDER BY cnt DESC
        """)

    labels = {
        200: "200 OK", 301: "301 Redirect", 302: "302 Redirect",
        403: "403 Forbidden", 404: "404 Not Found",
        -1: "Timeout", -2: "DNS Fail", -3: "Conn Error",
        -4: "SSL Error", -5: "Too Many Redirects", -6: "Other",
    }
    log.info("\n" + "=" * 55)
    log.info(f"  {'Status':<25} {'Count':>10}  {'%':>6}")
    log.info("  " + "-" * 45)
    for r in rows:
        pct   = r["cnt"] * 100.0 / total if total else 0
        label = labels.get(r["status_code"], str(r["status_code"]))
        log.info(f"  {label:<25} {r['cnt']:>10,}  {pct:>5.1f}%")
    log.info(f"  {'TOTAL':<25} {total:>10,}")
    log.info("=" * 55)
