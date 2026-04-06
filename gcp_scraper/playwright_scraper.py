# playwright_scraper.py — Second-pass Playwright scraper for JS-rendered domains
#
# Reads domains from domain_crawl_status where contacts_found=0 and no network error.
# Renders each page with a headless Chromium browser, then runs the same
# extract_contacts() ETL as the main scraper. Writes results to the same DB tables.
#
# Run separately from the main scraper (after main pass completes or concurrently).
# Controlled by SHARD_INDEX / SHARD_TOTAL / PW_CONTEXTS env vars.

import asyncio
import logging
import os
import time
from datetime import datetime, timezone
from collections import defaultdict

import asyncpg
from playwright.async_api import async_playwright, Browser

# ── Config from env ───────────────────────────────────────────────────────────
PG_HOST     = os.environ.get("PG_HOST") or "34.93.217.19"
PG_PORT     = int(os.environ.get("PG_PORT") or "5432")
PG_DB       = os.environ.get("PG_DB") or "domains"
PG_USER     = os.environ.get("PG_USER") or "scraper"
PG_PASSWORD = os.environ.get("PG_PASSWORD") or "Custarea@1"

SHARD_INDEX  = int(os.environ.get("SHARD_INDEX") or "0")
SHARD_TOTAL  = int(os.environ.get("SHARD_TOTAL") or "1")
PW_CONTEXTS  = int(os.environ.get("PW_CONTEXTS") or "20")   # concurrent Playwright pages
BATCH_SIZE   = int(os.environ.get("BATCH_SIZE") or "100")
PAGE_TIMEOUT = 20_000   # ms — Playwright timeout per page

ALLOWED_CATEGORIES = ["exim", "logistics", "commodity"]

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.StreamHandler(),
    ]
)
log = logging.getLogger("pw_scraper")

# ── Import ETL from main scraper (same codebase) ─────────────────────────────
import sys
sys.path.insert(0, os.path.dirname(__file__))
from extractor import extract_contacts

# ── DB helpers ────────────────────────────────────────────────────────────────
_pool = None

async def create_pool():
    global _pool
    _pool = await asyncpg.create_pool(
        host=PG_HOST, port=PG_PORT,
        database=PG_DB, user=PG_USER, password=PG_PASSWORD,
        min_size=2, max_size=6, command_timeout=120,
    )

async def fetch_batch(last_domain: str = "") -> list[tuple]:
    """Fetch empty domains (contacts_found=0, no network error) for this shard."""
    n = len(ALLOWED_CATEGORIES)
    cat_ph = ", ".join(f"${i+1}" for i in range(n))
    query = (
        f"SELECT cs.domain, dm.category "
        f"FROM domain_crawl_status cs "
        f"JOIN domain_metadata dm ON dm.domain = cs.domain "
        f"WHERE cs.contacts_found = 0 "
        f"  AND (cs.error_msg IS NULL OR cs.error_msg = '') "
        f"  AND dm.category IN ({cat_ph}) "
        f"  AND (hashtext(cs.domain) & 2147483647) % ${n+1} = ${n+2} "
        f"  AND cs.domain > ${n+3} "
        f"ORDER BY cs.domain ASC "
        f"LIMIT ${n+4}"
    )
    params = tuple(ALLOWED_CATEGORIES) + (SHARD_TOTAL, SHARD_INDEX, last_domain, BATCH_SIZE)
    async with _pool.acquire() as conn:
        rows = await conn.fetch(query, *params, timeout=60)
    return [(r["domain"], r["category"]) for r in rows]


async def write_results(contact_rows, status_rows, address_updates):
    for attempt in range(3):
        try:
            async with _pool.acquire() as conn:
                async with conn.transaction():
                    if contact_rows:
                        await conn.executemany("""
                            INSERT INTO domain_contacts
                                (domain, category, page_url, entity_type, value, label, context, fetched_at)
                            VALUES ($1,$2,$3,$4,$5,$6,$7,$8)
                            ON CONFLICT (domain, entity_type, value) DO NOTHING
                        """, contact_rows)
                    if status_rows:
                        await conn.executemany("""
                            UPDATE domain_crawl_status
                            SET contacts_found = $1,
                                crawled_at     = $2,
                                error_msg      = 'playwright'
                            WHERE domain = $3
                        """, status_rows)
                    if address_updates:
                        await conn.executemany("""
                            UPDATE domain_metadata
                            SET company_address = COALESCE(company_address, $1)
                            WHERE domain = $2
                        """, address_updates)
            return
        except Exception as e:
            if attempt < 2:
                log.warning(f"DB write error (attempt {attempt+1}/3): {e} — retrying in 10s")
                await asyncio.sleep(10)
            else:
                log.error(f"DB write FAILED after 3 attempts: {e}")


# ── Playwright page fetch ─────────────────────────────────────────────────────

async def fetch_rendered(browser: Browser, url: str) -> str | None:
    """Load a URL in a fresh Playwright page and return rendered HTML."""
    ctx = await browser.new_context(
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        java_script_enabled=True,
        ignore_https_errors=True,
    )
    try:
        page = await ctx.new_page()
        await page.route("**/*.{png,jpg,jpeg,gif,svg,woff,woff2,ttf,mp4,mp3}", lambda r: r.abort())
        await page.goto(url, timeout=PAGE_TIMEOUT, wait_until="domcontentloaded")
        # Brief wait for JS-rendered content to settle
        await page.wait_for_timeout(1500)
        return await page.content()
    except Exception as e:
        log.debug(f"Playwright fetch failed for {url}: {e}")
        return None
    finally:
        await ctx.close()


# ── Worker ────────────────────────────────────────────────────────────────────

import concurrent.futures

_parse_pool = None

async def worker(browser: Browser, sem: asyncio.Semaphore, queue: asyncio.Queue,
                 contact_buf: list, status_buf: list, addr_buf: list,
                 counters: dict):
    now = datetime.now(timezone.utc)
    loop = asyncio.get_running_loop()
    
    while True:
        item = await queue.get()
        if item is None:
            queue.task_done()
            break
        domain, category = item
        homepage_url = f"https://{domain}"

        async with sem:
            html = await fetch_rendered(browser, homepage_url)

        if not html:
            # Try http:// fallback
            async with sem:
                html = await fetch_rendered(browser, f"http://{domain}")

        if html:
            try:
                contacts, employees, address, _, _ = await asyncio.wait_for(
                    loop.run_in_executor(_parse_pool, extract_contacts, html, homepage_url, domain),
                    timeout=30.0
                )
            except asyncio.TimeoutError:
                log.warning(f"ETL timeout (regex freeze?) on {domain} — skipping")
                contacts, employees, address = [], [], None
            except Exception as e:
                log.warning(f"ETL error on {domain}: {e}")
                contacts, employees, address = [], [], None

            c_rows = [
                (domain, category, homepage_url,
                 c["entity_type"], c["value"][:500],
                 (c["label"] or "")[:200], (c["context"] or "")[:200], now)
                for c in contacts
            ]
            contact_buf.extend(c_rows)
            if address:
                addr_buf.append((address, domain))
            status_buf.append((len(c_rows), now, domain))
            counters["contacts"] += len(c_rows)
            counters["success"] += 1
        else:
            status_buf.append((0, now, domain))
            counters["failed"] += 1

        counters["crawled"] += 1
        queue.task_done()


# ── Main ──────────────────────────────────────────────────────────────────────

async def run():
    global _parse_pool
    _parse_pool = concurrent.futures.ProcessPoolExecutor(max_workers=int(os.environ.get("PARSE_WORKERS", "4")))
    
    await create_pool()

    # Count pending
    async with _pool.acquire() as conn:
        total = await conn.fetchval("""
            SELECT COUNT(*) FROM domain_crawl_status cs
            JOIN domain_metadata dm ON dm.domain = cs.domain
            WHERE cs.contacts_found = 0
              AND (cs.error_msg IS NULL OR cs.error_msg = '')
              AND dm.category = ANY($1::text[])
              AND (hashtext(cs.domain) & 2147483647) % $2 = $3
        """, ALLOWED_CATEGORIES, SHARD_TOTAL, SHARD_INDEX)

    log.info("=" * 60)
    log.info(f"  PLAYWRIGHT SCRAPER — Shard {SHARD_INDEX}/{SHARD_TOTAL}")
    log.info(f"  Empty domains (this shard) : {total:,}")
    log.info(f"  Concurrent Playwright pages: {PW_CONTEXTS}")
    log.info("=" * 60)

    if total == 0:
        log.info("Nothing to do.")
        await _pool.close()
        _parse_pool.shutdown()
        return

    t_start = time.monotonic()
    counters = defaultdict(int)
    last_domain = ""

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"],
        )
        sem = asyncio.Semaphore(PW_CONTEXTS)

        while True:
            rows = await fetch_batch(last_domain)
            if not rows:
                log.info("No more empty domains — done.")
                break

            log.info(f"[Shard {SHARD_INDEX}] Fetched {len(rows)} domains for Playwright pass...")
            queue = asyncio.Queue()
            contact_buf, status_buf, addr_buf = [], [], []

            for domain, category in rows:
                await queue.put((domain, category))
                last_domain = domain
            for _ in range(PW_CONTEXTS):
                queue.put_nowait(None)

            workers = [
                asyncio.create_task(worker(browser, sem, queue, contact_buf, status_buf, addr_buf, counters))
                for _ in range(PW_CONTEXTS)
            ]
            await asyncio.gather(*workers)

            # Write batch
            if contact_buf or status_buf:
                await write_results(contact_buf, status_buf, addr_buf)

            # Progress
            elapsed = time.monotonic() - t_start
            rate = counters["crawled"] / elapsed if elapsed else 0
            log.info(
                f"[Shard {SHARD_INDEX}] Crawled: {counters['crawled']:,}/{total:,} | "
                f"Rate: {rate:.2f}/s | Contacts: {counters['contacts']:,} | "
                f"Success: {counters['success']:,} | Failed: {counters['failed']:,}"
            )

        await browser.close()

    await _pool.close()
    _parse_pool.shutdown()
    log.info("Playwright scraper finished.")


if __name__ == "__main__":
    asyncio.run(run())
