# main.py — GCP metadata scraper
# Fetches <head> HTML for categorized domains from PostgreSQL,
# extracts title/description/lang/location and writes back to domain_metadata.
# Architecture: producer → CONCURRENCY async workers → db_writer task

import asyncio
import aiohttp
import socket
import time
import logging
import re
import collections
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import db
from config import (
    ALLOWED_CATEGORIES, CONCURRENCY, CONNECT_TIMEOUT, READ_TIMEOUT,
    MAX_REDIRECTS, MAX_HEAD_BYTES, WRITE_BATCH_SIZE, RETRY_STATUSES,
    MAX_RETRIES, REQUEST_HEADERS, SHARD_INDEX, SHARD_TOTAL,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler("fetch_metadata.log", encoding="utf-8"),
        logging.StreamHandler(),
    ]
)
log = logging.getLogger("metadata")

HEAD_CLOSE_RE = re.compile(rb"</head>", re.IGNORECASE)


# ── HTML parsing ──────────────────────────────────────────────────────────────

def parse_head(raw_bytes: bytes) -> dict:
    import warnings
    from bs4 import BeautifulSoup, XMLParsedAsHTMLWarning
    warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)

    match = HEAD_CLOSE_RE.search(raw_bytes)
    head  = raw_bytes[:match.end()] if match else raw_bytes
    html  = head.decode("utf-8", errors="ignore")
    soup  = BeautifulSoup(html, "lxml")

    tag   = soup.find("title")
    title = tag.get_text(strip=True)[:500] if tag else None

    desc  = None
    for attrs in [{"name": "description"}, {"name": "Description"},
                  {"property": "og:description"}]:
        tag = soup.find("meta", attrs=attrs)
        if tag and tag.get("content"):
            desc = tag["content"].strip()[:1000]
            break

    lang = None
    html_tag = soup.find("html")
    if html_tag and html_tag.get("lang"):
        lang = html_tag["lang"].strip()[:20]
    if not lang:
        tag = soup.find("meta", attrs={"http-equiv": re.compile("content-language", re.I)})
        if tag and tag.get("content"):
            lang = tag["content"].strip()[:20]

    location = None
    for name in ("geo.region", "geo.placename"):
        tag = soup.find("meta", attrs={"name": name})
        if tag and tag.get("content"):
            location = tag["content"].strip()[:200]
            break

    return {"title": title, "desc": desc, "lang": lang, "location": location}


# ── DNS resolver (thread-pool, avoids blocking event loop on Windows) ─────────

class ThreadedResolver:
    def __init__(self):
        self._executor = ThreadPoolExecutor(max_workers=CONCURRENCY)

    async def resolve(self, hostname, port=0, family=socket.AF_INET):
        loop  = asyncio.get_running_loop()
        infos = await loop.run_in_executor(
            self._executor,
            lambda: socket.getaddrinfo(hostname, port, family=family, type=socket.SOCK_STREAM)
        )
        return [
            {"hostname": hostname, "host": addr[0], "port": addr[1],
             "family": fam, "proto": proto, "flags": 0}
            for fam, _, proto, _, addr in infos
        ]

    async def close(self):
        self._executor.shutdown(wait=False)


# ── Single domain fetch ───────────────────────────────────────────────────────

async def fetch_one(session, domain: str, category: str) -> tuple:
    url  = f"https://{domain}"
    t0   = time.monotonic()
    now  = datetime.now(timezone.utc)
    status = title = desc = lang = loc = redir = err = None

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            async with session.get(
                url, allow_redirects=True,
                max_redirects=MAX_REDIRECTS, ssl=False
            ) as resp:
                status = resp.status
                redir  = str(resp.url) if str(resp.url) != url else None

                if status in RETRY_STATUSES and attempt < MAX_RETRIES:
                    await asyncio.sleep(1)
                    continue

                ct = resp.headers.get("Content-Type", "")
                if "html" in ct.lower() and status == 200:
                    raw = b""
                    async for chunk in resp.content.iter_chunked(4096):
                        raw += chunk
                        if len(raw) >= MAX_HEAD_BYTES or HEAD_CLOSE_RE.search(raw):
                            break
                    loop = asyncio.get_running_loop()
                    p    = await loop.run_in_executor(None, parse_head, raw)
                    title, desc, lang, loc = p["title"], p["desc"], p["lang"], p["location"]
                break

        except asyncio.TimeoutError:
            status, err = -1, "timeout"; break
        except aiohttp.ClientConnectorError as e:
            msg = str(e).lower()
            if any(k in msg for k in ("getaddrinfo", "name or service", "nodename")):
                status, err = -2, "dns_fail"
            else:
                status, err = -3, f"conn:{str(e)[:80]}"
            break
        except aiohttp.ClientSSLError:
            try:
                async with session.get(
                    f"http://{domain}", allow_redirects=True,
                    max_redirects=MAX_REDIRECTS, ssl=False
                ) as resp:
                    status = resp.status
                    redir  = str(resp.url)
                    ct     = resp.headers.get("Content-Type", "")
                    if "html" in ct.lower() and status == 200:
                        raw = b""
                        async for chunk in resp.content.iter_chunked(4096):
                            raw += chunk
                            if len(raw) >= MAX_HEAD_BYTES or HEAD_CLOSE_RE.search(raw):
                                break
                        loop = asyncio.get_running_loop()
                        p    = await loop.run_in_executor(None, parse_head, raw)
                        title, desc, lang, loc = p["title"], p["desc"], p["lang"], p["location"]
                    err = "ssl_fail_http_ok"
            except Exception:
                status, err = -4, "ssl_error"
            break
        except aiohttp.TooManyRedirects:
            status, err = -5, "too_many_redirects"; break
        except Exception as e:
            status, err = -6, f"err:{str(e)[:80]}"; break

    ms = int((time.monotonic() - t0) * 1000)
    return (domain, category, status, title, desc, lang, loc, redir, ms, now, err)


# ── Main async pipeline ───────────────────────────────────────────────────────

async def run():
    status_counts  = collections.Counter()
    latency_counts = collections.Counter()

    await db.create_pool()
    await db.init_db()

    pending, done = await db.count_pending()
    log.info(f"Shard {SHARD_INDEX}/{SHARD_TOTAL} | "
             f"Global pending un-crawled: {pending:,} | Global already done: {done:,} | "
             f"This shard pending (approx): {pending // max(SHARD_TOTAL, 1):,}")

    if pending == 0:
        log.info("Nothing to do.")
        await db.close_pool()
        return

    timeout = aiohttp.ClientTimeout(
        connect=CONNECT_TIMEOUT, sock_read=READ_TIMEOUT,
        total=CONNECT_TIMEOUT + READ_TIMEOUT + 10
    )
    resolver  = ThreadedResolver()
    connector = aiohttp.TCPConnector(
        limit=CONCURRENCY, limit_per_host=3,
        ttl_dns_cache=600, ssl=False,
        enable_cleanup_closed=True,
        resolver=resolver,
    )

    fetch_queue = asyncio.Queue(maxsize=CONCURRENCY * 2)
    write_queue = asyncio.Queue()

    fetched_total = 0
    t_start = time.monotonic()

    # ── DB writer task ────────────────────────────────────────────────────────
    async def db_writer():
        nonlocal fetched_total
        buf = []
        while True:
            item = await write_queue.get()
            if item is None:
                if buf:
                    await asyncio.get_running_loop().run_in_executor(
                        None, lambda b=buf[:]: asyncio.run(db.write_metadata(b))
                    )
                    fetched_total += len(buf)
                write_queue.task_done()
                break

            buf.append(item)
            if len(buf) >= WRITE_BATCH_SIZE:
                to_write = buf[:]
                buf.clear()
                await db.write_metadata(to_write)
                fetched_total += len(to_write)

                elapsed   = time.monotonic() - t_start
                rate      = fetched_total / elapsed if elapsed > 0 else 0
                shard_est = pending // max(SHARD_TOTAL, 1)
                remaining = shard_est - fetched_total
                eta_m     = remaining / max(rate, 1) / 60
                log.info(
                    f"[Shard {SHARD_INDEX}] Done: {fetched_total:,} | "
                    f"Rate: {rate:.0f}/s | ETA: {eta_m:.0f}m | "
                    f"Status dist: {dict(status_counts.most_common(5))}"
                )
            write_queue.task_done()

    # ── Producer: streams DB batches into queue ───────────────────────────────
    async def producer():
        last_domain = ""
        while True:
            rows = await db.fetch_batch(last_domain)
            if rows is None or len(rows) == 0:
                break
            for domain, category in rows:
                await fetch_queue.put((domain, category))
                last_domain = domain
        for _ in range(CONCURRENCY):
            await fetch_queue.put(None)

    # ── Worker: fetches one domain, pushes result to write_queue ─────────────
    async def worker(session):
        while True:
            item = await fetch_queue.get()
            if item is None:
                fetch_queue.task_done()
                break
            domain, category = item
            try:
                result = await fetch_one(session, domain, category)
            except Exception as e:
                log.error(f"Worker error on {domain}: {e}")
                fetch_queue.task_done()
                continue

            ms, sc = result[8], result[2]
            status_counts[sc] += 1
            if ms < 1000:    latency_counts["<1s"] += 1
            elif ms < 5000:  latency_counts["1-5s"] += 1
            elif ms < 12000: latency_counts["5-12s"] += 1
            else:            latency_counts[">12s"] += 1

            await write_queue.put(result)
            fetch_queue.task_done()

    async with aiohttp.ClientSession(
        connector=connector, headers=REQUEST_HEADERS, timeout=timeout
    ) as session:
        prod_task   = asyncio.create_task(producer())
        workers     = [asyncio.create_task(worker(session)) for _ in range(CONCURRENCY)]
        writer_task = asyncio.create_task(db_writer())

        await asyncio.gather(prod_task, *workers)
        await write_queue.put(None)
        await writer_task

    await resolver.close()
    await db.close_pool()

    log.info("Fetch complete!")
    await db.print_final_stats()


if __name__ == "__main__":
    asyncio.run(run())
