import psycopg2
import time

CONN = dict(
    host="34.93.217.19",
    port=5432,
    dbname="domains",
    user="scraper",
    password="Custarea@1",
)
BATCH_SIZE = 5_000

def run():
    conn = psycopg2.connect(**CONN)
    conn.autocommit = True
    cur = conn.cursor()

    print("Running DDL migration...")
    cur.execute("""
        ALTER TABLE domain_metadata ADD COLUMN IF NOT EXISTS email_count INT NOT NULL DEFAULT 0;
        ALTER TABLE domain_metadata ADD COLUMN IF NOT EXISTS phone_count INT NOT NULL DEFAULT 0;
        ALTER TABLE domain_metadata ADD COLUMN IF NOT EXISTS linkedin_count INT NOT NULL DEFAULT 0;
        ALTER TABLE domain_metadata ADD COLUMN IF NOT EXISTS social_count INT NOT NULL DEFAULT 0;
        ALTER TABLE domain_metadata ADD COLUMN IF NOT EXISTS emp_count INT NOT NULL DEFAULT 0;
        ALTER TABLE domain_metadata ADD COLUMN IF NOT EXISTS _quality_synced BOOLEAN DEFAULT FALSE;
    """)
    print("DDL complete.")

    cur.execute("SELECT COUNT(*) FROM domain_metadata WHERE _quality_synced = FALSE")
    total = cur.fetchone()[0]
    print(f"Rows to backfill: {total:,}")

    done = 0
    t0 = time.time()
    
    # We do updates chunked by CTID or domain. 
    # To be safe and fast, we'll fetch domains that need update, then update them.
    while True:
        cur.execute("SELECT domain FROM domain_metadata WHERE _quality_synced = FALSE LIMIT %s", (BATCH_SIZE,))
        domains = [r[0] for r in cur.fetchall()]
        if not domains:
            break
            
        cur.execute("""
            UPDATE domain_metadata dm
            SET 
                email_count = COALESCE(ec.cnt, 0),
                phone_count = COALESCE(pc.cnt, 0),
                linkedin_count = COALESCE(lc.cnt, 0),
                social_count = COALESCE(sc.cnt, 0),
                emp_count = COALESCE(emp.cnt, 0),
                quality_score = 
                    (CASE WHEN COALESCE(ec.cnt, 0) > 0   THEN 20 ELSE 0 END) +
                    (CASE WHEN COALESCE(pc.cnt, 0) > 0   THEN 15 ELSE 0 END) +
                    (CASE WHEN COALESCE(lc.cnt, 0) > 0   THEN 10 ELSE 0 END) +
                    (CASE WHEN COALESCE(sc.cnt, 0) > 0   THEN  5 ELSE 0 END) +
                    (CASE WHEN COALESCE(emp.cnt, 0) > 0  THEN 15 ELSE 0 END) +
                    (CASE WHEN dm.description IS NOT NULL THEN 5 ELSE 0 END) +
                    (CASE WHEN dm.location IS NOT NULL    THEN 5 ELSE 0 END) +
                    (CASE WHEN dm.status_code = 200       THEN 10 ELSE 0 END) +
                    (CASE WHEN (COALESCE(ec.cnt, 0) + COALESCE(pc.cnt, 0)) > 2 THEN 10 ELSE 0 END),
                _quality_synced = TRUE
            FROM UNNEST(%s::text[]) AS target(domain)
            LEFT JOIN (SELECT domain, COUNT(*)::int cnt FROM domain_contacts WHERE domain = ANY(%s) AND entity_type = 'email' GROUP BY domain) ec ON ec.domain = target.domain
            LEFT JOIN (SELECT domain, COUNT(*)::int cnt FROM domain_contacts WHERE domain = ANY(%s) AND entity_type = 'phone' GROUP BY domain) pc ON pc.domain = target.domain
            LEFT JOIN (SELECT domain, COUNT(*)::int cnt FROM domain_contacts WHERE domain = ANY(%s) AND entity_type = 'linkedin' GROUP BY domain) lc ON lc.domain = target.domain
            LEFT JOIN (SELECT domain, COUNT(*)::int cnt FROM domain_contacts WHERE domain = ANY(%s) AND entity_type IN ('facebook','instagram','twitter','youtube','whatsapp') GROUP BY domain) sc ON sc.domain = target.domain
            LEFT JOIN (SELECT domain, COUNT(*)::int cnt FROM domain_employees WHERE domain = ANY(%s) GROUP BY domain) emp ON emp.domain = target.domain
            WHERE dm.domain = target.domain
        """, (domains, domains, domains, domains, domains, domains))
        
        updated = cur.rowcount
        done += len(domains)  # some might be legit 0 quality so rowcount might vary, better to use len(domains)
        
        elapsed = time.time() - t0
        pct = (done / total * 100) if total else 100
        rate = done / elapsed if elapsed else 0
        eta = (total - done) / rate if rate else 0
        print(f"  {done:>8,} / {total:,}  ({pct:.1f}%)  {rate:.0f} rows/s  ETA {eta/60:.1f}m")

    print("\nBackfill complete. Creating B-Tree index on quality_score (this may take a minute)...")
    cur.execute("""
        CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_meta_quality 
        ON domain_metadata (quality_score DESC)
    """)
    print("Quality index created.")

    cur.close()
    conn.close()

if __name__ == "__main__":
    run()
