"""Query domains.db for progress and TLD breakdown."""
import sqlite3
import sys
import os
from collections import Counter

DB = r"E:\domains.db"
OUT = os.path.join(os.path.dirname(__file__), "stats_report.log")

try:
    conn = sqlite3.connect(DB, timeout=5)
    cur = conn.cursor()
except Exception as e:
    print(f"Cannot open {DB}: {e}")
    sys.exit(1)

lines = []

def p(s=""):
    lines.append(s)
    print(s)

# Shard progress
done = cur.execute("SELECT COUNT(*) FROM progress WHERE status='done'").fetchone()[0]
p("=" * 50)
p(f"  SHARD PROGRESS: {done}/300 ({done * 100 // 300}%)")
p("")
p(f"  {'Shard Name':<15} {'Status':<15} {'Lines':>12} {'New Doms':>12}  {'Finished At'}")
p(f"  {'-'*15} {'-'*15} {'-'*12} {'-'*12}  {'-'*19}")
try:
    rows = cur.execute("SELECT shard, status, lines_processed, new_domains, finished_at FROM progress ORDER BY shard").fetchall()
except sqlite3.OperationalError:
    rows = cur.execute("SELECT shard, status, 0 as lines_processed, new_domains, finished_at FROM progress ORDER BY shard").fetchall()

for row in rows:
    s, stat, lp, nd, fa = row
    lp_str = f"{lp:,}" if lp is not None else "0"
    nd_str = f"{nd:,}" if nd is not None else "0"
    fa_str = fa if fa else "-"
    p(f"  {s:<15} {stat:<15} {lp_str:>12} {nd_str:>12}  {fa_str}")
p("=" * 50)

# Total domains
total = cur.execute("SELECT COUNT(*) FROM domains").fetchone()[0]
p(f"  Total unique domains: {total:,}")
p()

# TLD breakdown
tld_counter = Counter()
cur.execute("SELECT domain FROM domains")
while True:
    batch = cur.fetchmany(50_000)
    if not batch:
        break
    for (d,) in batch:
        for ct in (".com.au", ".co.uk", ".co.in", ".co.nz", ".co.jp"):
            if d.endswith(ct):
                tld_counter[ct] += 1
                break
        else:
            dot = d.rfind(".")
            if dot != -1:
                tld_counter[d[dot:]] += 1

p(f"  {'TLD':<12} {'Count':>12} {'Share':>8}")
p(f"  {'---':<12} {'---':>12} {'---':>8}")
for tld, cnt in tld_counter.most_common():
    pct = cnt * 100.0 / total if total else 0
    bar = "#" * int(pct / 2)
    p(f"  {tld:<12} {cnt:>12,} {pct:>6.1f}%  {bar}")


# Category breakdown (if categorize.py has been run)
try:
    cats = cur.execute("""
        SELECT COALESCE(category, 'uncategorized'), COUNT(*)
        FROM domains
        GROUP BY category
        ORDER BY COUNT(*) DESC
    """).fetchall()

    categorized = sum(c for cat, c in cats if cat != 'uncategorized')
    if categorized > 0:
        p()
        p(f"  {'Category':<20} {'Count':>12} {'Share':>8}")
        p(f"  {'-'*19} {'-'*12} {'-'*8}")
        for cat, cnt in cats:
            pct = cnt * 100.0 / total if total else 0
            p(f"  {cat:<20} {cnt:>12,} {pct:>6.1f}%")
except sqlite3.OperationalError:
    pass  # category column doesn't exist yet

# Metadata fetch progress (if get_metadata.py has been run)
try:
    meta_total = cur.execute("SELECT COUNT(*) FROM domain_metadata").fetchone()[0]
    if meta_total > 0:
        p()
        p(f"  METADATA FETCH: {meta_total:,} domains fetched")
        p()
        rows = cur.execute("""
            SELECT status_code, COUNT(*) as cnt
            FROM domain_metadata
            GROUP BY status_code
            ORDER BY cnt DESC
        """).fetchall()
        labels = {
            200: "200 OK", 301: "301 Redirect", 302: "302 Redirect",
            403: "403 Forbidden", 404: "404 Not Found",
            -1: "Timeout", -2: "DNS Fail", -3: "Conn Error",
            -4: "SSL Error", -5: "Too Many Redir", -6: "Other Error",
        }
        p(f"  {'Status':<20} {'Count':>10} {'Share':>8}")
        p(f"  {'-'*19} {'-'*10} {'-'*8}")
        for status, cnt in rows:
            pct = cnt * 100.0 / meta_total if meta_total else 0
            label = labels.get(status, str(status))
            p(f"  {label:<20} {cnt:>10,} {pct:>6.1f}%")
except sqlite3.OperationalError:
    pass  # domain_metadata table doesn't exist yet


conn.close()
p("=" * 50)

# Save to file
with open(OUT, "w", encoding="utf-8") as f:
    f.write("\n".join(lines) + "\n")
print(f"\nReport saved to {OUT}")
