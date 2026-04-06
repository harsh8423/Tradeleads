import asyncio
import asyncpg
import os
import ssl

PG_HOST     = os.environ.get("PG_HOST", "34.93.217.19")
PG_DB       = os.environ.get("PG_DB", "domains")
PG_USER     = os.environ.get("PG_USER", "scraper")
PG_PASSWORD = os.environ.get("PG_PASSWORD", "Custarea@1")
PG_PORT     = int(os.environ.get("PG_PORT", "5432"))

async def run_report():
    print(f"Connecting to Cloud SQL metadata at {PG_HOST}...")
    
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    
    conn = await asyncpg.connect(
        host=PG_HOST, port=PG_PORT, database=PG_DB,
        user=PG_USER, password=PG_PASSWORD,
        ssl=ctx
    )
    
    total = await conn.fetchval("SELECT COUNT(*) FROM domain_metadata")
    if not total:
        print("No metadata yet.")
        await conn.close()
        return

    ok = await conn.fetchval("SELECT COUNT(*) FROM domain_metadata WHERE status_code=200")
    fail = total - ok

    print(f"\n  Total fetched    : {total:,}")
    print(f"  Succeeded (200)  : {ok:,}  ({ok*100/total:.1f}%)")
    print(f"  Failed/Other     : {fail:,}  ({fail*100/total:.1f}%)")

    rows = await conn.fetch("""
        SELECT status_code, COUNT(*) as cnt
        FROM domain_metadata
        GROUP BY status_code
        ORDER BY cnt DESC
    """)

    labels = {
        200:"200 OK", 301:"301 Redirect", 302:"302 Redirect",
        400:"400 Bad Request", 401:"401 Unauthorized", 403:"403 Forbidden",
        404:"404 Not Found", 429:"429 Rate Limited", 500:"500 Server Error",
        502:"502 Bad Gateway", 503:"503 Unavailable",
        -1:"Timeout", -2:"DNS Fail", -3:"Conn Error",
        -4:"SSL Error", -5:"Too Many Redir", -6:"Other Error",
    }

    print(f"\n  {'Status':<22} {'Count':>10} {'Share':>8}")
    print(f"  {'-'*21} {'-'*10} {'-'*8}")
    for r in rows:
        c = r["cnt"]
        s = r["status_code"]
        pct = c * 100.0 / total
        print(f"  {labels.get(s, str(s)):<22} {c:>10,} {pct:>6.1f}%")

    # Sample titles
    print(f"\n  Sample titles (200 OK):")
    samples = await conn.fetch(
        "SELECT domain, title FROM domain_metadata WHERE status_code=200 "
        "AND title IS NOT NULL LIMIT 8"
    )
    for r in samples:
        t = str(r["title"]) or "-"
        print(f"    {r['domain']:<38} {t[:55]}")

    await conn.close()
    print()


if __name__ == "__main__":
    import platform
    if platform.system() == "Windows":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(run_report())
