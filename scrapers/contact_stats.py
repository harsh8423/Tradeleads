import sqlite3

DB_PATH = r"E:\domains.db"

def print_stats():
    conn = sqlite3.connect(DB_PATH)
    
    # 1. Contact type breakdown
    print("\n" + "="*60)
    print("  CONTACT TYPE BREAKDOWN")
    print("="*60)
    try:
        rows = conn.execute("""
            SELECT entity_type, COUNT(*) as cnt
            FROM domain_contacts
            GROUP BY entity_type
            ORDER BY cnt DESC
        """).fetchall()
        if not rows:
            print("  <No contacts found yet>")
        else:
            for etype, cnt in rows:
                print(f"  {etype:<25} {cnt:>10,}")
        
        # Employees
        emp_cnt = conn.execute("SELECT COUNT(*) FROM domain_employees").fetchone()[0]
        if emp_cnt > 0:
            print(f"  {'employees':<25} {emp_cnt:>10,}")
            
    except sqlite3.OperationalError:
        print("  <Table domain_contacts does not exist yet>")

    # 2. Crawl Status
    print("\n" + "="*60)
    print("  CRAWL STATUS & PLAYWRIGHT SIGNALS")
    print("="*60)
    try:
        # Get total target domains
        target_total = 0
        try:
            target_total = conn.execute("SELECT COUNT(*) FROM domain_metadata WHERE status_code = 200").fetchone()[0]
        except sqlite3.OperationalError:
            pass

        total_crawled = conn.execute("SELECT COUNT(*) FROM domain_crawl_status").fetchone()[0]
        
        if total_crawled > 0:
            with_data = conn.execute("SELECT COUNT(*) FROM domain_crawl_status WHERE contacts_found > 0").fetchone()[0]
            with_emps = conn.execute("SELECT COUNT(DISTINCT domain) FROM domain_employees").fetchone()[0]
            
            # Playwright signal: 0 contacts AND no error msg
            needs_pw = conn.execute("SELECT COUNT(*) FROM domain_crawl_status WHERE contacts_found = 0 AND (error_msg IS NULL OR error_msg = '')").fetchone()[0]
            pw_used = conn.execute("SELECT COUNT(*) FROM domain_crawl_status WHERE used_playwright = 1").fetchone()[0]
            
            # Errors
            errors = conn.execute("SELECT COUNT(*) FROM domain_crawl_status WHERE error_msg IS NOT NULL AND error_msg != ''").fetchone()[0]

            print(f"  Target Domains (200 OK) : {target_total:,}")
            print(f"  Domains Crawled         : {total_crawled:,} " + 
                  (f"({total_crawled*100//target_total}%)" if target_total else ""))
            print(f"  Remaining to Crawl      : {max(0, target_total - total_crawled):,}")
            print("-" * 60)
            print(f"  SUCCESS: With Contacts  : {with_data:,} ({with_data*100//total_crawled}%)")
            print(f"  SUCCESS: With Employees : {with_emps:,}")
            print(f"  EMPTY  : Needs Playwright: {needs_pw:,} ({needs_pw*100//total_crawled}%)")
            print(f"  ERRORS : Network/Timeout: {errors:,} ({errors*100//total_crawled}%)")
            print("-" * 60)
            
            print("  HTTP STATUS BREAKDOWN")
            statuses = conn.execute("SELECT status_code, COUNT(*) FROM domain_crawl_status GROUP BY status_code ORDER BY COUNT(*) DESC LIMIT 8").fetchall()
            for st, count in statuses:
                st_label = "Conn Error" if st == -1 else (f"HTTP {st}" if st else "None")
                print(f"    {st_label:<15} : {count:,}")
            
            print("-" * 60)
            print(f"  Playwright Executed     : {pw_used:,}")
        else:
            print("  <No crawl status records yet>")
    except sqlite3.OperationalError:
        print("  <Table domain_crawl_status does not exist yet>")

    # 3. Tail 10 Data
    print("\n" + "="*60)
    print("  LAST 10 GENERIC CONTACTS FOUND")
    print("="*60)
    try:
        tail_rows = conn.execute("""
            SELECT id, domain, category, page_url, entity_type, value, label, context, fetched_at
            FROM domain_contacts
            ORDER BY id DESC
            LIMIT 10
        """).fetchall()
        
        if not tail_rows:
            print("  <No generic contacts to display>")
        else:
            for r in tail_rows:
                print(f"\n[ID: {r[0]}] Domain: {r[1]} | Category: {r[2]}")
                print(f"  Type      : {r[4]}")
                print(f"  Value     : {r[5]}")
                print(f"  Label     : {r[6]}")
                print(f"  Context   : {r[7].strip() if r[7] else 'None'}")
                print(f"  Page URL  : {r[3]}")
                print(f"  Fetched At: {r[8]}")
                
        print("\n" + "="*60)
        print("  LAST 10 EMPLOYEES FOUND")
        print("="*60)
        
        emp_rows = conn.execute("""
            SELECT id, domain, name, title, email, phone, linkedin, fetched_at
            FROM domain_employees
            ORDER BY id DESC
            LIMIT 10
        """).fetchall()
        
        if not emp_rows:
            print("  <No employees to display>")
        else:
            for r in emp_rows:
                print(f"\n[ID: {r[0]}] Domain: {r[1]}")
                print(f"  Name      : {r[2]}")
                print(f"  Title     : {r[3]}")
                print(f"  Email     : {r[4]}")
                print(f"  Phone     : {r[5]}")
                print(f"  LinkedIn  : {r[6]}")
                print(f"  Fetched At: {r[7]}")
                
    except sqlite3.OperationalError:
        print("  <Tables do not exist yet>")

    print("\n" + "="*60 + "\n")
    conn.close()

if __name__ == "__main__":
    print_stats()
