# retry_failed_metadata.py — Multiprocess-Async Hybrid retry for failed domains
# Architecture: Main process loads failed domains → slices into N chunks →
# each worker process runs its own asyncio event loop + aiohttp session.
# Uses UPDATE (not INSERT) to overwrite failed rows in domain_metadata.

import asyncio, aiohttp, sqlite3, time, logging, re, socket, os, multiprocessing
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

# ── Config ────────────────────────────────────────────────────────────────────
DB_PATH            = r"E:\domains.db"

NUM_WORKERS        = min(10, os.cpu_count())   # OS processes to spawn
CONCURRENCY_PER    = 50                        # aiohttp connections per process
CONNECT_TIMEOUT    = 25
READ_TIMEOUT       = 25
MAX_REDIRECTS      = 5
MAX_HEAD_BYTES     = 32_768
WRITE_BATCH_SIZE   = 200
RETRY_STATUSES     = {429, 500, 502, 503, 504}
MAX_RETRIES        = 2
START_ROWID        = 60_000   # Skip the first N rows

# -1: Timeout  |  -2: DNS Fail  |  -3: Conn Error  |  -4: SSL Error 403: Forbidden  |  429: Rate Limited
# 403: Forbidden  |  429: Rate Limited
RETRY_TARGET_STATUSES = [-2]
# ─────────────────────────────────────────────────────────────────────────────

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:123.0) Gecko/20100101 Firefox/123.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_3) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.3 Safari/605.1.15",
]

REQUEST_HEADERS = {
    "Accept":          "text/html,application/xhtml+xml",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate",
}

HEAD_CLOSE_RE = re.compile(rb"</head>", re.IGNORECASE)


# ── Logging ───────────────────────────────────────────────────────────────────

def _setup_logging(worker_id=None):
    label  = f"worker-{worker_id}" if worker_id is not None else "main"
    logger = logging.getLogger(label)
    if not logger.handlers:
        fmt = logging.Formatter(f"%(asctime)s [{label}] %(levelname)s %(message)s")
        fh  = logging.FileHandler("retry_metadata.log", encoding="utf-8")
        ch  = logging.StreamHandler()
        fh.setFormatter(fmt); ch.setFormatter(fmt)
        logger.addHandler(fh); logger.addHandler(ch)
    logger.setLevel(logging.INFO)
    return logger


# ── DB utilities ──────────────────────────────────────────────────────────────

def load_all_pending() -> list[tuple[str, str]]:
    """Load ALL failed domains in one pass (we UPDATE, so no pagination loop needed)."""
    conn = sqlite3.connect(DB_PATH)
    ph   = ",".join("?" * len(RETRY_TARGET_STATUSES))
    params = RETRY_TARGET_STATUSES + [START_ROWID]
    rows = conn.execute(f"""
        SELECT domain, category
        FROM domain_metadata
        WHERE status_code IN ({ph})
        ORDER BY rowid ASC
        LIMIT -1 OFFSET ?
    """, params).fetchall()

    conn.close()
    return rows


def write_results_to_db(results: list):
    """UPDATE existing rows (keeps ROWIDs stable, crash-safe)."""
    if not results:
        return
    conn = sqlite3.connect(DB_PATH, timeout=60)
    conn.execute("PRAGMA synchronous=NORMAL")
    update_data = [
        (r[2], r[3], r[4], r[5], r[6], r[7], r[8], r[9], r[10], r[0])
        for r in results
    ]
    conn.executemany("""
        UPDATE domain_metadata
        SET status_code=?, title=?, description=?, language=?, location=?,
            redirected_url=?, fetch_ms=?, fetched_at=?, error_msg=?
        WHERE domain=?
    """, update_data)
    conn.commit()
    conn.close()


# ── HTML parsing ──────────────────────────────────────────────────────────────

def parse_head(raw_bytes: bytes) -> dict:
    import warnings
    from bs4 import BeautifulSoup, XMLParsedAsHTMLWarning
    warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)

    match = HEAD_CLOSE_RE.search(raw_bytes)
    head  = raw_bytes[:match.end()] if match else raw_bytes
    soup  = BeautifulSoup(head.decode("utf-8", errors="ignore"), "lxml")

    tag   = soup.find("title")
    title = tag.get_text(strip=True)[:500] if tag else None

    desc  = None
    for attrs in [{"name": "description"}, {"name": "Description"}, {"property": "og:description"}]:
        tag = soup.find("meta", attrs=attrs)
        if tag and tag.get("content"):
            desc = tag["content"].strip()[:1000]
            break

    lang  = None
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


# ── Threaded DNS resolver (Windows-safe) ──────────────────────────────────────

class ThreadedResolver:
    def __init__(self, pool_size):
        self._executor = ThreadPoolExecutor(max_workers=pool_size)

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


# ── Per-domain fetch ──────────────────────────────────────────────────────────

import random

async def fetch_one(session, domain, category):
    url    = f"https://{domain}"
    t0     = time.monotonic()
    now    = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    status = title = desc = lang = loc = redir = err = None
    headers = {**REQUEST_HEADERS, "User-Agent": random.choice(USER_AGENTS)}

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            async with session.get(url, headers=headers, allow_redirects=True,
                                   max_redirects=MAX_REDIRECTS, ssl=False) as resp:
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
            if any(x in msg for x in ("getaddrinfo", "name or service", "nodename")):
                status, err = -2, "dns_fail"
            else:
                status, err = -3, f"conn: {str(e)[:80]}"
            break
        except aiohttp.ClientSSLError:
            try:
                async with session.get(f"http://{domain}", headers=headers,
                                       allow_redirects=True, max_redirects=MAX_REDIRECTS, ssl=False) as resp:
                    status = resp.status;  redir = str(resp.url)
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
                    err = "ssl_fail_http_ok"
            except Exception:
                status, err = -4, "ssl_error"
            break
        except aiohttp.TooManyRedirects:
            status, err = -5, "too_many_redirects"; break
        except Exception as e:
            status, err = -6, f"err: {str(e)[:80]}"; break

    ms = int((time.monotonic() - t0) * 1000)
    return (domain, category, status, title, desc, lang, loc, redir, ms, now, err)


# ── Worker process entry point ────────────────────────────────────────────────

def worker_process(worker_id: int, domain_chunks: list):
    """Runs in a separate OS process with its own event loop and aiohttp session."""
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    log = _setup_logging(worker_id)

    async def _run():
        timeout   = aiohttp.ClientTimeout(connect=CONNECT_TIMEOUT, sock_read=READ_TIMEOUT,
                                          total=CONNECT_TIMEOUT + READ_TIMEOUT + 10)
        resolver  = ThreadedResolver(pool_size=CONCURRENCY_PER)
        connector = aiohttp.TCPConnector(
            limit=CONCURRENCY_PER, limit_per_host=2,
            ttl_dns_cache=300, ssl=False,
            enable_cleanup_closed=True,
            resolver=resolver,
        )
        sem     = asyncio.Semaphore(CONCURRENCY_PER)
        results = []
        done    = 0
        total   = len(domain_chunks)
        t0      = time.monotonic()

        async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
            async def bounded_fetch(domain, category):
                nonlocal done
                async with sem:
                    row = await fetch_one(session, domain, category)
                results.append(row)
                done += 1

                if len(results) >= WRITE_BATCH_SIZE:
                    batch = results[:]
                    results.clear()
                    write_results_to_db(batch)
                    rate = done / (time.monotonic() - t0) if time.monotonic() - t0 else 0
                    log.info(f"Saved {done:,}/{total:,} | {rate:.0f}/s")

            await asyncio.gather(*[bounded_fetch(d, c) for d, c in domain_chunks])

        if results:
            write_results_to_db(results)

        rate = done / (time.monotonic() - t0) if time.monotonic() - t0 else 0
        log.info(f"Worker done — {done:,}/{total:,} updated | avg {rate:.0f}/s")

    asyncio.run(_run())


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    log = _setup_logging()

    log.info("Loading failed domains from DB...")
    all_domains = load_all_pending()
    total = len(all_domains)

    if total == 0:
        log.info("No failed domains to retry.")
        return

    log.info(f"Total to retry    : {total:,}")
    log.info(f"Worker processes  : {NUM_WORKERS}")
    log.info(f"Connections/worker: {CONCURRENCY_PER}")
    log.info(f"Effective concurr.: {NUM_WORKERS * CONCURRENCY_PER:,}")
    log.info(f"Retry statuses    : {RETRY_TARGET_STATUSES}")

    # Slice evenly across workers
    chunk_size = (total + NUM_WORKERS - 1) // NUM_WORKERS
    chunks     = [all_domains[i:i + chunk_size] for i in range(0, total, chunk_size)]

    t_start  = time.monotonic()
    ctx      = multiprocessing.get_context("spawn")
    procs    = []

    for i, chunk in enumerate(chunks):
        p = ctx.Process(target=worker_process, args=(i, chunk), daemon=False)
        p.start()
        procs.append(p)
        log.info(f"Started worker-{i} with {len(chunk):,} domains (PID {p.pid})")

    for p in procs:
        p.join()

    elapsed = time.monotonic() - t_start
    log.info(f"All workers finished in {elapsed/60:.1f}m ({elapsed:.0f}s)")


if __name__ == "__main__":
    multiprocessing.freeze_support()
    main()
