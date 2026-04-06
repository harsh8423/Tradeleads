# search.py — Fast domain lookup in domains.db
import sqlite3, sys

DB = r"E:\domains.db"

def search(query):
    conn = sqlite3.connect(DB)
    query = query.lower().strip()

    # Check if category column exists
    cols = {r[1] for r in conn.execute("PRAGMA table_info(domains)")}
    has_cat = "category" in cols
    select = "domain, last_crawled, shard, category" if has_cat else "domain, last_crawled, shard"

    # Exact match first
    row = conn.execute(f"SELECT {select} FROM domains WHERE domain = ?", (query,)).fetchone()

    if row:
        print(f"\n  EXACT MATCH")
        print(f"     Domain:       {row[0]}")
        print(f"     Last Crawled: {row[1]}")
        print(f"     Shard:        {row[2]}")
        if has_cat:
            print(f"     Category:     {row[3] or '-'}")

        # Show metadata if available
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
        if "domain_metadata" in tables:
            meta = conn.execute(
                "SELECT title, description, language, status_code, redirected_url "
                "FROM domain_metadata WHERE domain = ?", (query,)
            ).fetchone()
            if meta:
                print(f"     Status:       {meta[3]}")
                print(f"     Title:        {meta[0] or '-'}")
                print(f"     Description:  {(meta[1] or '-')[:120]}")
                print(f"     Language:     {meta[2] or '-'}")
                if meta[4]:
                    print(f"     Redirected:   {meta[4]}")

        print()
        conn.close()
        return

    # Fuzzy / partial match (LIKE %query%)
    rows = conn.execute(
        f"SELECT {select} FROM domains WHERE domain LIKE ? LIMIT 25",
        (f"%{query}%",)
    ).fetchall()
    conn.close()

    if rows:
        print(f"\n  🔍 No exact match. {len(rows)} partial matches for '{query}':\n")
        if has_cat:
            print(f"  {'Domain':<40} {'Category':<12} {'Last Crawled':<18} Shard")
            print(f"  {'-'*39} {'-'*11} {'-'*17} {'-'*16}")
            for r in rows:
                print(f"  {r[0]:<40} {(r[3] or '-'):<12} {r[1]:<18} {r[2]}")
        else:
            print(f"  {'Domain':<45} {'Last Crawled':<22} Shard")
            print(f"  {'-'*44} {'-'*21} {'-'*16}")
            for r in rows:
                print(f"  {r[0]:<45} {r[1]:<22} {r[2]}")
        print()
    else:
        print(f"\n  ❌ No domains found matching '{query}'\n")

if __name__ == "__main__":
    if len(sys.argv) > 1:
        search(" ".join(sys.argv[1:]))
    else:
        print("Usage: python search.py <domain or keyword>")
        print("  Examples:")
        print("    python search.py google.com")
        print("    python search.py shopify")
