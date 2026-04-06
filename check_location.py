import asyncio
import asyncpg
import os
from dotenv import load_dotenv

load_dotenv("server/.env")

async def q():
    conn = await asyncpg.connect(
        host=os.getenv("PG_HOST"),
        port=int(os.getenv("PG_PORT", 5432)),
        database=os.getenv("PG_DB"),
        user=os.getenv("PG_USER"),
        password=os.getenv("PG_PASSWORD"),
    )
    total     = await conn.fetchval("SELECT COUNT(*) FROM domain_metadata")
    with_loc  = await conn.fetchval("SELECT COUNT(*) FROM domain_metadata WHERE location IS NOT NULL AND location != ''")
    top_locs  = await conn.fetch("""
        SELECT location, COUNT(*) cnt
        FROM domain_metadata
        WHERE location IS NOT NULL AND location != ''
        GROUP BY location
        ORDER BY cnt DESC
        LIMIT 15
    """)
    await conn.close()
    print(f"Total domains : {total:,}")
    print(f"With location : {with_loc:,}")
    print(f"Coverage      : {with_loc/total*100:.1f}%")
    print("\nTop locations:")
    for r in top_locs:
        print(f"  {r['cnt']:>8,}  {r['location']}")

asyncio.run(q())
