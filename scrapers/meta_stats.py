# meta_stats.py -- Stats for domain_metadata fetch results
import sqlite3

DB = r"E:\domains.db"
conn = sqlite3.connect(DB)

total = conn.execute("SELECT COUNT(*) FROM domain_metadata").fetchone()[0]
if total == 0:
    print("No data yet.")
    conn.close()
    exit()

ok    = conn.execute("SELECT COUNT(*) FROM domain_metadata WHERE status_code=200").fetchone()[0]
fail  = total - ok

print(f"\n  Total fetched    : {total:,}")
print(f"  Succeeded (200)  : {ok:,}  ({ok*100/total:.1f}%)")
print(f"  Failed/Other     : {fail:,}  ({fail*100/total:.1f}%)")

rows = conn.execute("""
    SELECT status_code, COUNT(*) as cnt
    FROM domain_metadata
    GROUP BY status_code
    ORDER BY cnt DESC
""").fetchall()

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
for s, c in rows:
    pct = c * 100.0 / total
    print(f"  {labels.get(s, str(s)):<22} {c:>10,} {pct:>6.1f}%")

# Sample titles
print(f"\n  Sample titles (200 OK):")
for d, t in conn.execute(
    "SELECT domain, title FROM domain_metadata WHERE status_code=200 "
    "AND title IS NOT NULL LIMIT 8"
).fetchall():
    print(f"    {d:<38} {(t or '-')[:55]}")

conn.close()
print()
