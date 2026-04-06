# migrate.py — SQLite → Cloud SQL PostgreSQL
# Simple, reliable: small batches + auto-reconnect on Cloud SQL timeouts.
# Speed: ~3,000-5,000 rows/s. For 31M domains: ~2-3 hours total.

import sqlite3, psycopg2, psycopg2.extras, os, time, json, logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("migrate")

SQLITE_PATH = os.environ.get("SQLITE_PATH", r"E:\domains.db")
CHECKPOINT  = "migrate_checkpoint.json"

PG_CONN_STR = (
    f"host={os.environ.get('PG_HOST', '34.93.217.19')} "
    f"port=5432 dbname=domains user=scraper "
    f"password={os.environ.get('PG_PASSWORD', 'Custarea@1')} "
    # Keepalives prevent Cloud SQL from silently dropping idle connections
    f"keepalives=1 keepalives_idle=30 keepalives_interval=10 keepalives_count=5 "
    f"connect_timeout=30"
)

BATCH = 50_000  # 50k rows per round trip → ~10x less network overhead

# ── Set start rowid per table (0 = beginning) ────────────────────────────────
START_ROWID = {
    "domains":             0,
    "domain_metadata":     0,
    "domain_contacts":     0,
    "domain_employees":    0,
    "domain_crawl_status": 0,
}

MIGRATIONS = [
    # 'domains' table SKIPPED — gcp_scraper never reads from it (saves 2+ hours).
    ("domain_metadata",     "domain_metadata",
     ["domain", "category", "status_code", "title", "description",
      "language", "location", "company_address", "redirected_url",
      "fetch_ms", "fetched_at", "error_msg"]),
    ("domain_contacts",     "domain_contacts",
     ["domain", "category", "page_url", "entity_type", "value",
      "label", "context", "fetched_at"]),
    ("domain_employees",    "domain_employees",
     ["domain", "name", "title", "email", "phone", "linkedin", "fetched_at"]),
    ("domain_crawl_status", "domain_crawl_status",
     ["domain", "category", "status_code", "crawled_at",
      "pages_crawled", "contacts_found", "error_msg"]),
]


# ── Checkpoint ────────────────────────────────────────────────────────────────

def load_checkpoint():
    if os.path.exists(CHECKPOINT):
        with open(CHECKPOINT) as f:
            data = json.load(f)
        log.info(f"Resuming: {data}")
        return data
    return {}

def save_checkpoint(state):
    with open(CHECKPOINT, "w") as f:
        json.dump(state, f)


# ── PG connection with auto-reconnect ─────────────────────────────────────────

def pg_connect():
    for attempt in range(10):
        try:
            conn = psycopg2.connect(PG_CONN_STR)
            conn.autocommit = False
            return conn
        except Exception as e:
            wait = 5 * (attempt + 1)
            log.warning(f"PG connect failed ({e}), retrying in {wait}s...")
            time.sleep(wait)
    raise RuntimeError("Could not connect to PostgreSQL after 10 attempts")


# ── Table migration ───────────────────────────────────────────────────────────

def migrate_table(src_table, dst_table, wanted_cols, checkpoint):
    sqlite_conn = sqlite3.connect(SQLITE_PATH)
    existing = {r[1] for r in sqlite_conn.execute(f"PRAGMA table_info({src_table})").fetchall()}
    cols = [c for c in wanted_cols if c in existing]
    if not cols:
        log.warning(f"No matching columns in {src_table}, skipping.")
        sqlite_conn.close()
        return

    total     = sqlite_conn.execute(f"SELECT COUNT(*) FROM {src_table}").fetchone()[0]
    max_rowid = sqlite_conn.execute(f"SELECT MAX(rowid) FROM {src_table}").fetchone()[0] or 0

    manual = START_ROWID.get(src_table, 0)
    last_rowid = manual if manual else checkpoint.get(src_table, 0)

    col_list = ", ".join(cols)
    sql = f"INSERT INTO {dst_table} ({col_list}) VALUES %s"

    if last_rowid:
        done = sqlite_conn.execute(
            f"SELECT COUNT(*) FROM {src_table} WHERE rowid <= ?", (last_rowid,)
        ).fetchone()[0]
        log.info(f"{src_table}: resuming from rowid {last_rowid:,} ({done:,}/{total:,} done)")
    else:
        log.info(f"{src_table}: starting fresh, {total:,} rows total")

    pg_conn = pg_connect()
    session_rows = 0
    t0 = time.monotonic()

    while last_rowid < max_rowid:
        # Fetch batch from SQLite
        rows = sqlite_conn.execute(
            f"SELECT rowid, {col_list} FROM {src_table} "
            f"WHERE rowid > ? ORDER BY rowid LIMIT ?",
            (last_rowid, BATCH)
        ).fetchall()
        if not rows:
            break
        data = [r[1:] for r in rows]
        batch_last = rows[-1][0]

        # Insert with retry on connection drop
        for attempt in range(5):
            try:
                with pg_conn.cursor() as cur:
                    psycopg2.extras.execute_values(cur, sql, data, page_size=BATCH)
                pg_conn.commit()
                break
            except (psycopg2.OperationalError, psycopg2.InterfaceError) as e:
                log.warning(f"  PG error ({e.__class__.__name__}), reconnecting...")
                try:
                    pg_conn.close()
                except Exception:
                    pass
                pg_conn = pg_connect()
        else:
            log.error(f"Failed batch at rowid {batch_last} after 5 attempts, skipping.")

        last_rowid = batch_last
        session_rows += len(rows)
        checkpoint[src_table] = last_rowid
        save_checkpoint(checkpoint)

        elapsed = time.monotonic() - t0
        rate = session_rows / elapsed if elapsed else 0
        remaining = max_rowid - last_rowid
        eta_min = (remaining / rate / 60) if rate else 0
        log.info(f"  {src_table}: {last_rowid:,}/{max_rowid:,} | "
                 f"{rate:,.0f} rows/s | ETA {eta_min:.0f}m")

    sqlite_conn.close()
    try:
        pg_conn.close()
    except Exception:
        pass
    log.info(f"✓ {src_table} done ({session_rows:,} rows this session)")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    checkpoint = load_checkpoint()
    sqlite_conn = sqlite3.connect(SQLITE_PATH)
    for src_table, dst_table, cols in MIGRATIONS:
        exists = sqlite_conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            (src_table,)
        ).fetchone()
        if not exists:
            log.info(f"Table {src_table} not found, skipping.")
            continue
        sqlite_conn.close()
        migrate_table(src_table, dst_table, cols, checkpoint)
        sqlite_conn = sqlite3.connect(SQLITE_PATH)
    sqlite_conn.close()

    if os.path.exists(CHECKPOINT):
        os.remove(CHECKPOINT)
    log.info("Migration complete!")

if __name__ == "__main__":
    main()
