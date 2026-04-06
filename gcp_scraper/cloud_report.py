import asyncio
import asyncpg
import os

PG_HOST     = os.environ.get("PG_HOST", "34.93.217.19")
PG_DB       = os.environ.get("PG_DB", "domains")
PG_USER     = os.environ.get("PG_USER", "scraper")
PG_PASSWORD = os.environ.get("PG_PASSWORD")
PG_PORT     = int(os.environ.get("PG_PORT", "5432"))

async def run_report():
    print(f"Connecting to Cloud SQL at {PG_HOST}...")
    conn = await asyncpg.connect(
        host=PG_HOST, port=PG_PORT, database=PG_DB,
        user=PG_USER, password=PG_PASSWORD
    )
    
    print("\n============================================================")
    print("  CRAWL STATUS & METRICS")
    print("============================================================")
    
    target_domains = await conn.fetchval("SELECT COUNT(*) FROM domain_metadata WHERE status_code = 200")
    crawled = await conn.fetchval("SELECT COUNT(*) FROM domain_crawl_status")
    crawled_pct = int(crawled * 100 / target_domains) if target_domains else 0
    remaining = target_domains - crawled
    
    success_contacts = await conn.fetchval("SELECT COUNT(*) FROM domain_crawl_status WHERE contacts_found > 0")
    success_pct = int(success_contacts * 100 / crawled) if crawled else 0
    
    empty_domains = await conn.fetchval("SELECT COUNT(*) FROM domain_crawl_status WHERE contacts_found = 0 AND (error_msg IS NULL OR error_msg = '')")
    empty_pct = int(empty_domains * 100 / crawled) if crawled else 0
    
    error_domains = await conn.fetchval("SELECT COUNT(*) FROM domain_crawl_status WHERE error_msg IS NOT NULL AND error_msg != ''")
    error_pct = int(error_domains * 100 / crawled) if crawled else 0
    
    print(f"  Target Domains (200 OK) : {target_domains:,}")
    print(f"  Domains Crawled         : {crawled:,} ({crawled_pct}%)")
    print(f"  Remaining to Crawl      : {remaining:,}")
    print("-" * 60)
    print(f"  SUCCESS: With Contacts  : {success_contacts:,} ({success_pct}%)")
    print(f"  EMPTY  : Needs Playwright: {empty_domains:,} ({empty_pct}%)")
    print(f"  ERRORS : Network/Timeout: {error_domains:,} ({error_pct}%)")
    print("-" * 60)
    print("  HTTP / ERROR BREAKDOWN")
    err_rows = await conn.fetch("""
        SELECT COALESCE(error_msg, 'HTTP 200') as msg, COUNT(*) as cnt
        FROM domain_crawl_status
        GROUP BY msg ORDER BY cnt DESC LIMIT 15
    """)
    for r in err_rows:
        print(f"    {str(r['msg'])[:40]:<40} : {r['cnt']:,}")

    # ── Contact totals ────────────────────────────────────────────────────────
    total_contacts = await conn.fetchval("SELECT COUNT(*) FROM domain_contacts")
    print("\n============================================================")
    print(f"  CONTACTS — TOTAL: {total_contacts:,}")
    print("============================================================")
    type_rows = await conn.fetch("""
        SELECT entity_type, COUNT(*) as cnt
        FROM domain_contacts
        GROUP BY entity_type
        ORDER BY cnt DESC
    """)
    for r in type_rows:
        pct = int(r['cnt'] * 100 / total_contacts) if total_contacts else 0
        print(f"  {str(r['entity_type']):<20} : {r['cnt']:>10,}  ({pct}%)")

    print("\n============================================================")
    print("  LAST 10 CONTACTS FOUND")
    print("============================================================")
    contacts = await conn.fetch("SELECT domain, entity_type, value FROM domain_contacts ORDER BY id DESC LIMIT 10")
    for c in contacts:
        print(f"  {c['domain'][:25]:<27} | {c['entity_type']:<15} | {c['value'][:60]}")
        
    await conn.close()


if __name__ == "__main__":
    import platform
    if platform.system() == "Windows":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(run_report())
