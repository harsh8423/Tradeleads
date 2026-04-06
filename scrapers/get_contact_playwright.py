import asyncio
import sqlite3
import logging
import time
from datetime import datetime, timezone
from get_contact import DB_PATH, REQUEST_HEADERS, extract_contacts

PLAYWRIGHT_WORKERS = 5
PLAYWRIGHT_TIMEOUT = 25_000

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("pw_contacts")

def fetch_batch_from_db():
    conn = sqlite3.connect(DB_PATH)
    # We update used_playwright=1, so domains naturally fall out of this query
    rows = conn.execute("""
        SELECT domain, category
        FROM domain_crawl_status
        WHERE contacts_found = 0
          AND used_playwright = 0
          AND error_msg IS NULL
        LIMIT 50
    """).fetchall()
    conn.close()
    return rows

async def fetch_with_playwright(domain: str) -> dict[str, str]:
    from playwright.async_api import async_playwright
    results = {}
    homepage_url = f"https://{domain}"
    urls = [homepage_url, f"{homepage_url}/contact", f"{homepage_url}/about"]
    
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent=REQUEST_HEADERS["User-Agent"],
            ignore_https_errors=True,
        )
        for url in urls:
            try:
                page = await context.new_page()
                await page.goto(url, timeout=PLAYWRIGHT_TIMEOUT, wait_until="domcontentloaded")
                html = await page.content()
                results[url] = html
                await page.close()
            except Exception as e:
                log.debug(f"Playwright failed {url}: {e}")
        await context.close()
        await browser.close()
    return results

async def worker(queue):
    while True:
        item = await queue.get()
        if item is None:
            queue.task_done()
            break
        domain, category = item
        
        try:
            pw_result = await fetch_with_playwright(domain)
            now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            seen_contacts = set()
            seen_employees = set()
            contact_rows = []
            employee_rows = []
            domain_address = None
            
            if pw_result:
                for url, html in pw_result.items():
                    c_res, e_res, addr = await asyncio.get_running_loop().run_in_executor(
                        None, extract_contacts, html, url, domain
                    )
                    if addr and not domain_address: domain_address = addr
                    for c in c_res:
                        key = (c["entity_type"], c["value"].lower())
                        if key not in seen_contacts:
                            seen_contacts.add(key)
                            contact_rows.append(
                                (domain, category, url, c["entity_type"], c["value"], c["label"], c["context"], now)
                            )
                    for e in e_res:
                        key = e["name"].lower()
                        if key not in seen_employees:
                            seen_employees.add(key)
                            employee_rows.append(
                                (domain, e["name"], e["title"], e["email"], e["phone"], e["linkedin"], now)
                            )
            
            # Sync writes are sufficiently fast for 5 workers
            conn = sqlite3.connect(DB_PATH, timeout=30)
            if contact_rows:
                conn.executemany("""
                    INSERT OR IGNORE INTO domain_contacts
                    (domain, category, page_url, entity_type, value, label, context, fetched_at)
                    VALUES (?,?,?,?,?,?,?,?)
                """, contact_rows)
            if employee_rows:
                conn.executemany("""
                    INSERT OR IGNORE INTO domain_employees
                    (domain, name, title, email, phone, linkedin, fetched_at)
                    VALUES (?,?,?,?,?,?,?)
                """, employee_rows)
            
            if domain_address:
                conn.execute("""
                    UPDATE domain_metadata
                    SET company_address = COALESCE(company_address, ?)
                    WHERE domain = ?
                """, (domain_address, domain))

            conn.execute("""
                UPDATE domain_crawl_status 
                SET contacts_found = ?, used_playwright = 1
                WHERE domain = ?
            """, (len(contact_rows) + len(employee_rows), domain))
            conn.commit()
            conn.close()
            
            log.info(f"[{domain}] Playwright found {len(contact_rows) + len(employee_rows)} contacts")
            
        except Exception as e:
            log.error(f"Worker error on {domain}: {e}")
            
        queue.task_done()

async def run():
    queue = asyncio.Queue(maxsize=PLAYWRIGHT_WORKERS * 2)
    
    async def producer():
        while True:
            rows = fetch_batch_from_db()
            if not rows:
                break
            for row in rows:
                await queue.put(row)
            # Wait for queue to drain before fetching next batch
            await queue.join()
            
        for _ in range(PLAYWRIGHT_WORKERS):
            await queue.put(None)

    prod_task = asyncio.create_task(producer())
    workers = [asyncio.create_task(worker(queue)) for _ in range(PLAYWRIGHT_WORKERS)]
    
    await asyncio.gather(prod_task, *workers)
    log.info("Playwright crawl complete!")

if __name__ == "__main__":
    # Playwright officially requires ProactorEventLoop on Windows
    import sys
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    asyncio.run(run())
