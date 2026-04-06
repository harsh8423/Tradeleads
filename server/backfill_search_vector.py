"""
Backfill search_vector for all domain_metadata rows.
Runs in batches of 20,000 to avoid timeouts.
Then creates the GIN index and trigger.

Usage:
    cd server
    python backfill_search_vector.py
"""
import psycopg2
import time

CONN = dict(
    host="34.93.217.19",
    port=5432,
    dbname="domains",
    user="scraper",
    password="Custarea@1",
)
BATCH = 20_000

def run():
    conn = psycopg2.connect(**CONN)
    conn.autocommit = True
    cur = conn.cursor()

    # Step 0: Ensure columns exist
    print("Adding columns if needed...")
    cur.execute("ALTER TABLE domain_metadata ADD COLUMN IF NOT EXISTS search_vector tsvector")
    cur.execute("ALTER TABLE domain_metadata ADD COLUMN IF NOT EXISTS quality_score INTEGER DEFAULT 0")
    print("Columns ready.")

    # Total rows needing backfill
    cur.execute("SELECT COUNT(*) FROM domain_metadata WHERE search_vector IS NULL")
    total = cur.fetchone()[0]
    print(f"Rows to backfill: {total:,}")

    done = 0
    t0 = time.time()
    while True:
        cur.execute("""
            UPDATE domain_metadata
            SET search_vector = to_tsvector(
                'english',
                coalesce(title, '')       || ' ' ||
                coalesce(description, '') || ' ' ||
                coalesce(domain, '')      || ' ' ||
                coalesce(location, '')    || ' ' ||
                coalesce(category, '')
            )
            WHERE domain IN (
                SELECT domain FROM domain_metadata
                WHERE search_vector IS NULL
                LIMIT %s
            )
        """, (BATCH,))
        updated = cur.rowcount
        if updated == 0:
            break
        done += updated
        elapsed = time.time() - t0
        pct = (done / total * 100) if total else 100
        rate = done / elapsed if elapsed else 0
        eta = (total - done) / rate if rate else 0
        print(f"  {done:>8,} / {total:,}  ({pct:.1f}%)  {rate:.0f} rows/s  ETA {eta/60:.1f}m")

    print("\nBackfill complete. Creating GIN index (this may take a few minutes)...")
    cur.execute("""
        CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_meta_search_vector
        ON domain_metadata USING GIN(search_vector)
    """)
    print("GIN index created.")

    print("Creating auto-update trigger...")
    cur.execute("""
        CREATE OR REPLACE FUNCTION update_search_vector()
        RETURNS trigger AS $$
        BEGIN
            NEW.search_vector := to_tsvector(
                'english',
                coalesce(NEW.title, '')       || ' ' ||
                coalesce(NEW.description, '') || ' ' ||
                coalesce(NEW.domain, '')      || ' ' ||
                coalesce(NEW.location, '')    || ' ' ||
                coalesce(NEW.category, '')
            );
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
    """)
    cur.execute("DROP TRIGGER IF EXISTS trig_search_vector ON domain_metadata")
    cur.execute("""
        CREATE TRIGGER trig_search_vector
            BEFORE INSERT OR UPDATE OF title, description, domain, location, category
            ON domain_metadata
            FOR EACH ROW EXECUTE FUNCTION update_search_vector()
    """)
    print("Trigger created.")

    cur.execute("""
        SELECT count(*) FILTER (WHERE search_vector IS NOT NULL), count(*)
        FROM domain_metadata
    """)
    with_vec, total_rows = cur.fetchone()
    print(f"\nDone! {with_vec:,} / {total_rows:,} rows have search_vector.")
    cur.close()
    conn.close()

if __name__ == "__main__":
    run()
