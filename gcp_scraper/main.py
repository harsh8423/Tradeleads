# main.py — Async run loop: producer, workers, db_writer
# Entry point: python main.py

import asyncio
import aiohttp
import logging
import time
from collections import deque, defaultdict
from datetime import datetime, timezone

import config
from config import CONCURRENCY, CONNECT_TIMEOUT, READ_TIMEOUT, WRITE_BATCH_SIZE, REQUEST_HEADERS, PARSE_WORKERS
from db import create_pool, close_pool, init_db, fetch_batch, count_pending, write_contacts, print_final_stats
from crawler import crawl_domain, ThreadedResolver

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler("fetch_contacts.log", encoding="utf-8"),
        logging.StreamHandler(),
    ]
)
log = logging.getLogger("contacts")


async def run():
    await create_pool()
    await init_db()

    total, done = await count_pending()
    pending = total - done
    shard_pending = pending // max(config.SHARD_TOTAL, 1)

    log.info("=" * 60)
    log.info(f"  GCP Contact Scraper — Shard {config.SHARD_INDEX} / {config.SHARD_TOTAL}")
    log.info("=" * 60)
    log.info(f"  Total 200-OK domains  : {total:,}")
    log.info(f"  Already crawled       : {done:,}")
    log.info(f"  Pending (global)      : {pending:,}")
    log.info(f"  This shard (~)        : {shard_pending:,}")
    log.info(f"  Concurrency           : {CONCURRENCY}")
    log.info("=" * 60)

    if pending == 0:
        log.info("Nothing to do.")
        await close_pool()
        return

    from concurrent.futures import ThreadPoolExecutor as _TPE, ProcessPoolExecutor as _PPE
    # Small thread pool for misc blocking I/O (DNS, etc)
    asyncio.get_running_loop().set_default_executor(_TPE(max_workers=8))
    # Process pool for CPU-bound HTML parsing — bypasses GIL, safe on 4-vCPU VMs
    parse_pool = _PPE(max_workers=PARSE_WORKERS)
    log.info(f"  Parse workers (processes): {PARSE_WORKERS}")

    timeout = aiohttp.ClientTimeout(
        connect=CONNECT_TIMEOUT, sock_read=READ_TIMEOUT,
        total=CONNECT_TIMEOUT + READ_TIMEOUT + 10
    )
    resolver  = ThreadedResolver()
    connector = aiohttp.TCPConnector(
        limit=CONCURRENCY, limit_per_host=4,
        ttl_dns_cache=600, ssl=False,
        enable_cleanup_closed=True,
        resolver=resolver,
    )

    queue       = asyncio.Queue(maxsize=CONCURRENCY * 10)  # was 4x — bigger buffer reduces producer blocking
    write_queue = asyncio.Queue()

    # ── Shared metrics (written by db_writer, read by health logger) ──────────
    crawled_total  = 0
    contacts_total = 0
    employees_total = 0
    # Error type counters
    err_counts: dict[str, int] = defaultdict(int)   # "success"|"timeout"|"dns"|"conn"|"ssl"|"other"
    # Rolling window removed — conn error rate ≥60% is normal when scraping
    # millions of cold domains (dead, parked, DNS-failed). Pausing 60s every
    # ~100 domains cuts throughput by ~40%. Let it run.
    t_start = time.monotonic()

    def _classify_status(status_code: int, error_msg: str | None) -> str:
        if status_code == 200:          return "success"
        if status_code == -1:           return "timeout"
        if status_code == -2:           return "dns_fail"
        if status_code in (-3, -6):     return "conn_error"
        if status_code == -4:           return "ssl_error"
        if "connection_failed" in (error_msg or ""):
            return "conn_error"
        return "other"

    def _progress_line(batch_size: int) -> str:
        elapsed = time.monotonic() - t_start
        rate    = crawled_total / elapsed if elapsed else 0
        remaining = max(shard_pending - crawled_total, 0)
        eta_min = remaining / rate / 60 if rate else 0
        total_err = sum(v for k, v in err_counts.items() if k != "success")
        err_rate  = total_err * 100 / crawled_total if crawled_total else 0
        return (
            f"[Shard {config.SHARD_INDEX}] "
            f"Crawled: {crawled_total:,}/{shard_pending:,} | "
            f"Rate: {rate:.1f}/s | ETA: {eta_min:.0f}m | "
            f"Contacts: {contacts_total:,} | Employees: {employees_total:,} | "
            f"Errors: {total_err:,} ({err_rate:.1f}%) | "
            f"[ok={err_counts['success']:,} "
            f"dns={err_counts['dns_fail']:,} "
            f"tmout={err_counts['timeout']:,} "
            f"conn={err_counts['conn_error']:,} "
            f"ssl={err_counts['ssl_error']:,} "
            f"other={err_counts['other']:,}]"
        )

    # ── DB writer ─────────────────────────────────────────────────────────────
    async def db_writer():
        nonlocal crawled_total, contacts_total, employees_total
        contact_buf  = []
        employee_buf = []
        status_buf   = []
        addr_updates = []

        while True:
            item = await write_queue.get()
            if item is None:
                # Final flush
                if contact_buf or employee_buf or status_buf or addr_updates:
                    try:
                        await write_contacts(contact_buf[:], employee_buf[:], status_buf[:], addr_updates[:])
                        crawled_total   += len(status_buf)
                        contacts_total  += len(contact_buf)
                        employees_total += len(employee_buf)
                    except Exception as e:
                        log.error(f"Final flush DB WRITE FAILED (data may be lost): {e}")
                write_queue.task_done()
                break

            c_rows, e_rows, s_row, addr = item
            contact_buf.extend(c_rows)
            employee_buf.extend(e_rows)
            status_buf.append(s_row)
            if addr:
                addr_updates.append((addr, s_row[0]))

            # Track error class from status_row: (domain, category, status_code, ..., error_msg)
            err_counts[_classify_status(s_row[2], s_row[6])] += 1

            if len(status_buf) >= WRITE_BATCH_SIZE:
                to_c, to_e, to_s, to_a = (
                    contact_buf[:], employee_buf[:], status_buf[:], addr_updates[:]
                )
                contact_buf.clear(); employee_buf.clear()
                status_buf.clear();  addr_updates.clear()

                try:
                    await write_contacts(to_c, to_e, to_s, to_a)
                    crawled_total   += len(to_s)
                    contacts_total  += len(to_c)
                    employees_total += len(to_e)
                    log.info(_progress_line(len(to_s)))
                except Exception as e:
                    err_name = type(e).__name__
                    # Only re-buffer on transient errors (timeout, connection).
                    # Data errors (ProgramLimitExceeded, DataError) will always
                    # fail on retry — drop the batch to avoid infinite loop.
                    transient = err_name in ("TimeoutError", "ConnectionError",
                                             "InterfaceError", "ConnectionDoesNotExistError")
                    if transient:
                        log.warning(f"Transient DB error, re-buffering {len(to_s)} rows: {e}")
                        contact_buf[:0]  = to_c
                        employee_buf[:0] = to_e
                        status_buf[:0]   = to_s
                        addr_updates[:0] = to_a
                        await asyncio.sleep(30)
                    else:
                        # Non-transient (bad data) — count as crawled, drop contacts
                        crawled_total += len(to_s)
                        log.error(f"Non-transient DB error, dropping {len(to_c)} contacts from {len(to_s)} domains: {e}")
            write_queue.task_done()

    # ── Periodic health logger (every 60s) ────────────────────────────────────
    async def health_logger():
        while True:
            await asyncio.sleep(60)
            if crawled_total == 0:
                continue
            elapsed = time.monotonic() - t_start
            rate    = crawled_total / elapsed if elapsed else 0
            total_err = sum(v for k, v in err_counts.items() if k != "success")
            log.info(
                f"── HEALTH [{elapsed/60:.0f}m elapsed] "
                f"rate={rate:.1f}/s | "
                f"crawled={crawled_total:,} | contacts={contacts_total:,} | employees={employees_total:,} | "
                f"ok={err_counts['success']:,} | timeout={err_counts['timeout']:,} | "
                f"dns={err_counts['dns_fail']:,} | conn={err_counts['conn_error']:,} | "
                f"ssl={err_counts['ssl_error']:,} | other={err_counts['other']:,} | "
                f"err_rate={total_err*100//max(crawled_total,1)}%"
            )

    # ── Producer ──────────────────────────────────────────────────────────────
    async def producer():
        last_domain = ""
        while True:
            log.info(f"[Shard {config.SHARD_INDEX}] Fetching batch from DB...")
            rows = await fetch_batch(last_domain)
            
            if rows is None:
                log.warning("Producer DB fetch error, retrying in 5s...")
                await asyncio.sleep(5)
                continue
                
            log.info(f"[Shard {config.SHARD_INDEX}] Fetched {len(rows)} domains! Putting in queue...")
            if not rows:
                log.info(f"[Shard {config.SHARD_INDEX}] No more pending domains.")
                break
                
            for domain, category in rows:
                await queue.put((domain, category))
                last_domain = domain
        for _ in range(CONCURRENCY):
            await queue.put(None)

    pause_event = asyncio.Event()
    pause_event.set()

    # ── Workers ───────────────────────────────────────────────────────────────
    from collections import deque as _deque
    conn_error_window = _deque(maxlen=500)  # rolling window for observability

    async def worker(session):
        while True:
            await pause_event.wait()
            item = await queue.get()
            if item is None:
                queue.task_done()
                break
            domain, category = item
            try:
                result = await crawl_domain(session, domain, category, parse_pool=parse_pool)
                await write_queue.put(result)

                # Observability: warn if conn error rate spikes (no pause — expected at scale)
                status_code = result[2][2]
                is_err = status_code in (-1, -2, -3, -4, -5, -6)
                conn_error_window.append(1 if is_err else 0)
                if len(conn_error_window) == 500 and sum(conn_error_window) > 400:
                    log.warning(
                        f"High error rate: {sum(conn_error_window)}/500 recent domains failed "
                        f"(expected for cold/dead domains, not pausing)"
                    )
                    conn_error_window.clear()

            except asyncio.CancelledError:
                # Suppress Python 3.11 aiohttp DNS Shield CancelledError leak
                # Without this, the unhandled BaseException aborts asyncio.gather
                log.debug(f"Worker CancelledError on {domain} (timeout), continuing...")
                now = datetime.now(timezone.utc)
                await write_queue.put(
                    ([], [], (domain, category, -1, now, 0, 0, "timeout_dns_cancelled"), None)
                )
            except Exception as e:
                log.error(f"Worker exception on {domain}: {e}")
                now = datetime.now(timezone.utc)
                await write_queue.put(
                    ([], [], (domain, category, -1, now, 0, 0, str(e)[:100]), None)
                )
            queue.task_done()


    log.info("Creating tasks...")
    async with aiohttp.ClientSession(
        connector=connector, headers=REQUEST_HEADERS, timeout=timeout
    ) as session:
        log.info("ClientSession initialized.")
        prod_task    = asyncio.create_task(producer())
        workers      = [asyncio.create_task(worker(session)) for _ in range(CONCURRENCY)]
        writer_task  = asyncio.create_task(db_writer())
        health_task  = asyncio.create_task(health_logger())
        
        log.info("Awaiting gather...")
        await asyncio.gather(prod_task, *workers)
        log.info("Gather finished!")
        await write_queue.put(None)
        await writer_task
        health_task.cancel()

    await resolver.close()
    parse_pool.shutdown(wait=False)
    await close_pool()

    # Final summary
    elapsed = time.monotonic() - t_start
    log.info("=" * 60)
    log.info("  CRAWL COMPLETE")
    log.info("=" * 60)
    log.info(f"  Elapsed          : {elapsed/60:.1f} min")
    log.info(f"  Total crawled    : {crawled_total:,}")
    log.info(f"  Avg rate         : {crawled_total/elapsed:.1f} domains/s")
    log.info(f"  Contacts found   : {contacts_total:,}")
    log.info(f"  Employees found  : {employees_total:,}")
    log.info(f"  Success          : {err_counts['success']:,}")
    log.info(f"  DNS fail         : {err_counts['dns_fail']:,}")
    log.info(f"  Timeout          : {err_counts['timeout']:,}")
    log.info(f"  Conn error       : {err_counts['conn_error']:,}")
    log.info(f"  SSL error        : {err_counts['ssl_error']:,}")
    log.info(f"  Other            : {err_counts['other']:,}")
    log.info("=" * 60)
    await print_final_stats()


if __name__ == "__main__":
    import sys
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(run())
