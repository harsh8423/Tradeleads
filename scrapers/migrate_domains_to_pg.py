# migrate_domains_to_pg.py
# Reads new categorized domains from SQLite that don't yet have metadata in PG,
# and bulk-inserts them into PostgreSQL `domains` table.
# Safe to re-run — uses ON CONFLICT DO NOTHING.

import sqlite3
import asyncio
import asyncpg
import time
import logging

# ── Config ──────────────────────────────────────────────────────────────
SQLITE_PATH  = r"E:\domains.db"
PG_HOST      = "34.93.217.19"
PG_PORT      = 5432
PG_DB        = "domains"
PG_USER      = "scraper"
PG_PASSWORD  = "Custarea@1"

BATCH_SIZE   = 10_000   # rows per UNNEST insert
FETCH_SIZE   = 500_000  # rows to load from SQLite at a time
# ────────────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler("migrate_domains.log", encoding="utf-8"),
        logging.StreamHandler(),
    ]
)
log = logging.getLogger("migrate")


_DDL_STMTS = [
    ("DROP TABLE IF EXISTS domains", "drop old domains table"),
    ("""
    CREATE TABLE domains (
        domain       TEXT PRIMARY KEY,
        last_crawled TEXT,
        shard        TEXT,
        category     TEXT
    )""", "domains table"),
    ("CREATE INDEX IF NOT EXISTS idx_domains_category ON domains(category)", "idx_domains_category"),
]


async def create_table(pool):
    async with pool.acquire() as conn:
        for stmt, label in _DDL_STMTS:
            try:
                await conn.execute(stmt, timeout=300)
                log.info(f"  DDL OK: {label}")
            except Exception as e:
                log.warning(f"  DDL skipped [{label}]: {e}")
    log.info("domains table ready in PostgreSQL")



def load_sqlite_batch(offset: int) -> list:
    """
    Load a batch of categorized, unscraped domains from SQLite.
    We rely on ON CONFLICT DO NOTHING in PG for dedup — no need to pre-filter.
    """
    conn = sqlite3.connect(SQLITE_PATH)
    rows = conn.execute("""
        SELECT d.domain, d.last_crawled, d.shard, d.category
        FROM domains d
        LEFT JOIN domain_metadata m ON d.domain = m.domain
        WHERE d.category IS NOT NULL
          AND m.domain IS NULL
        LIMIT ? OFFSET ?
    """, (FETCH_SIZE, offset)).fetchall()
    conn.close()
    return rows


async def insert_batch(pool, batch: list) -> int:
    """Bulk insert using UNNEST — single query regardless of batch size."""
    if not batch:
        return 0

    domains      = [r[0] for r in batch]
    last_crawled = [r[1] for r in batch]
    shards       = [r[2] for r in batch]
    categories   = [r[3] for r in batch]

    query = """
        INSERT INTO domains (domain, last_crawled, shard, category)
        SELECT * FROM UNNEST($1::text[], $2::text[], $3::text[], $4::text[])
        AS t(domain, last_crawled, shard, category)
        ON CONFLICT (domain) DO NOTHING
    """
    for attempt in range(3):
        try:
            async with pool.acquire() as conn:
                result = await conn.execute(
                    query, domains, last_crawled, shards, categories,
                    timeout=120
                )
            # result is e.g. "INSERT 0 5000"
            inserted = int(result.split()[-1])
            return inserted
        except Exception as e:
            wait = 10 * (attempt + 1)
            log.warning(f"Insert attempt {attempt+1} failed: {e} — retry in {wait}s")
            await asyncio.sleep(wait)
    log.error("Insert FAILED after 3 attempts")
    return 0


async def main():
    log.info("Connecting to PostgreSQL...")
    pool = await asyncpg.create_pool(
        host=PG_HOST, port=PG_PORT, database=PG_DB,
        user=PG_USER, password=PG_PASSWORD,
        min_size=2, max_size=8, command_timeout=600
    )

    await create_table(pool)
    log.info("domains table ready — skipping PG pre-load (ON CONFLICT handles dedup)")

    # Count total pending from SQLite
    conn = sqlite3.connect(SQLITE_PATH)
    total_pending = conn.execute("""
        SELECT COUNT(*)
        FROM domains d
        LEFT JOIN domain_metadata m ON d.domain = m.domain
        WHERE d.category IS NOT NULL AND m.domain IS NULL
    """).fetchone()[0]
    conn.close()
    log.info(f"Total domains to migrate: {total_pending:,}")

    t_start = time.time()
    total_inserted = 0
    total_skipped  = 0
    offset = 0

    while True:
        log.info(f"Loading SQLite batch at offset {offset:,}...")
        batch_raw = load_sqlite_batch(offset)
        
        if not batch_raw and offset == 0:
            log.info("Nothing to migrate — all domains already in PG.")
            break
        if not batch_raw:
            log.info("All batches processed.")
            break

        # Sub-batch by BATCH_SIZE for insert
        for i in range(0, len(batch_raw), BATCH_SIZE):
            chunk = batch_raw[i:i + BATCH_SIZE]
            inserted = await insert_batch(pool, chunk)
            total_inserted += inserted
            total_skipped  += len(chunk) - inserted

        offset += FETCH_SIZE

        elapsed = time.time() - t_start
        rate    = total_inserted / elapsed if elapsed > 0 else 0
        remaining = total_pending - (offset)
        eta = remaining / max(rate, 1) / 60
        log.info(
            f"Progress: {offset:,}/{total_pending:,} fetched | "
            f"Inserted: {total_inserted:,} | Skipped (dupe): {total_skipped:,} | "
            f"Rate: {rate:,.0f}/sec | ETA: {eta:.0f}m"
        )

    await pool.close()
    elapsed = time.time() - t_start
    log.info("=" * 55)
    log.info(f"MIGRATION COMPLETE in {elapsed/60:.1f} minutes")
    log.info(f"Total inserted into PostgreSQL: {total_inserted:,}")
    log.info(f"Total skipped (already exists): {total_skipped:,}")
    log.info("=" * 55)


if __name__ == "__main__":
    asyncio.run(main())
