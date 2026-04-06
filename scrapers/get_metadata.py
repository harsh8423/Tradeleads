# get_metadata.py — Fetch HTML <head> metadata for categorized domains
# Async I/O (not multiprocessing) — correct for network-bound work
import asyncio, aiohttp, sqlite3, time, logging, re, socket
import collections, threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

# ── Config ─────────────────────────────────────────────
DB_PATH            = r"E:\domains.db"
ALLOWED_CATEGORIES = ["logistics", "exim", "commodity"]

CONCURRENCY        = 1000        # stable — 150 proven, 1000 crashes event loop
CONNECT_TIMEOUT    = 12         # seconds to establish TCP connection
READ_TIMEOUT       = 15         # seconds to read response body
MAX_REDIRECTS      = 5
MAX_HEAD_BYTES     = 32_768     # 32KB
BATCH_SIZE         = 2_000      # keep low — 5000 tasks at once overloads event loop
WRITE_BATCH_SIZE   = 1000        # rows buffered before DB flush
RETRY_STATUSES     = {429, 500, 502, 503, 504}
MAX_RETRIES        = 2
# ───────────────────────────────────────────────────────

REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept":          "text/html,application/xhtml+xml",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate",
}

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


# ── DB setup (synchronous — runs once at start) ───────

def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS domain_metadata (
            domain         TEXT PRIMARY KEY,
            category       TEXT,
            status_code    INTEGER,
            title          TEXT,
            description    TEXT,
            language       TEXT,
            location       TEXT,
            redirected_url TEXT,
            fetch_ms       INTEGER,
            fetched_at     TEXT,
            error_msg      TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_meta_category ON domain_metadata(category);
        CREATE INDEX IF NOT EXISTS idx_meta_status   ON domain_metadata(status_code);
    """)
    conn.commit()
    conn.close()
    log.info("domain_metadata table ready")


def count_pending():
    """Count how many categorized domains still need fetching."""
    conn = sqlite3.connect(DB_PATH)
    ph = ",".join("?" * len(ALLOWED_CATEGORIES))
    total = conn.execute(
        f"SELECT COUNT(*) FROM domains WHERE category IN ({ph})",
        ALLOWED_CATEGORIES
    ).fetchone()[0]
    done = conn.execute(
        f"SELECT COUNT(*) FROM domain_metadata WHERE category IN ({ph})", 
        ALLOWED_CATEGORIES
    ).fetchone()[0]
    conn.close()
    return total, done


def fetch_batch_from_db(last_rowid):
    """
    Fetch next batch of unprocessed domains. 
    Uses ROWID pagination so we never fetch the same domain twice in one session.
    """
    conn = sqlite3.connect(DB_PATH)
    ph = ",".join("?" * len(ALLOWED_CATEGORIES))
    rows = conn.execute(f"""
        SELECT rowid, d.domain, d.category
        FROM domains d
        WHERE d.category IN ({ph})
          AND rowid > ?
          AND NOT EXISTS (
              SELECT 1 FROM domain_metadata dm WHERE dm.domain = d.domain
          )
        ORDER BY rowid ASC
        LIMIT ?
    """, (*ALLOWED_CATEGORIES, last_rowid, BATCH_SIZE)).fetchall()
    conn.close()
    return rows


def write_results_to_db(results):
    """Write a batch of results. INSERT OR IGNORE = idempotent / crash-safe."""
    if not results:
        return
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.executemany("""
        INSERT OR IGNORE INTO domain_metadata
        (domain, category, status_code, title, description,
         language, location, redirected_url, fetch_ms, fetched_at, error_msg)
        VALUES (?,?,?,?,?,?,?,?,?,?,?)
    """, results)
    conn.commit()
    conn.close()


# ── HTML parsing ───────────────────────────────────────

def parse_head(raw_bytes: bytes) -> dict:
    """Parse <head> section using lxml (faster than html.parser)."""
    import warnings
    from bs4 import BeautifulSoup, XMLParsedAsHTMLWarning
    warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)

    match = HEAD_CLOSE_RE.search(raw_bytes)
    head = raw_bytes[:match.end()] if match else raw_bytes
    html = head.decode("utf-8", errors="ignore")
    soup = BeautifulSoup(html, "lxml")

    # Title
    tag = soup.find("title")
    title = tag.get_text(strip=True)[:500] if tag else None

    # Description
    desc = None
    for attrs in [{"name": "description"}, {"name": "Description"},
                  {"property": "og:description"}]:
        tag = soup.find("meta", attrs=attrs)
        if tag and tag.get("content"):
            desc = tag["content"].strip()[:1000]
            break

    # Language
    lang = None
    html_tag = soup.find("html")
    if html_tag and html_tag.get("lang"):
        lang = html_tag["lang"].strip()[:20]
    if not lang:
        tag = soup.find("meta", attrs={"http-equiv": re.compile("content-language", re.I)})
        if tag and tag.get("content"):
            lang = tag["content"].strip()[:20]

    # Location
    location = None
    for name in ("geo.region", "geo.placename"):
        tag = soup.find("meta", attrs={"name": name})
        if tag and tag.get("content"):
            location = tag["content"].strip()[:200]
            break

    return {"title": title, "desc": desc, "lang": lang, "location": location}


# ── HTTP fetch ─────────────────────────────────────────

async def fetch_one(session, domain, category):
    """
    Fetch <head> of a single domain. Returns a DB row tuple.
    Negative status codes = error types:
      -1=timeout  -2=dns  -3=connection  -4=ssl  -5=redirects  -6=other
    """
    url = f"https://{domain}"
    t0 = time.monotonic()
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    status = title = desc = lang = loc = redir = err = None

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            async with session.get(
                url, allow_redirects=True, max_redirects=MAX_REDIRECTS, ssl=False
            ) as resp:
                status = resp.status
                redir = str(resp.url) if str(resp.url) != url else None

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
                    p = await loop.run_in_executor(None, parse_head, raw)
                    title, desc, lang, loc = p["title"], p["desc"], p["lang"], p["location"]
                break  # success, exit retry loop

        except asyncio.TimeoutError:
            status, err = -1, "timeout"
            break
        except aiohttp.ClientConnectorError as e:
            msg = str(e).lower()
            if "getaddrinfo" in msg or "name or service" in msg or "nodename" in msg:
                status, err = -2, "dns_fail"
            else:
                status, err = -3, f"conn: {str(e)[:80]}"
            break
        except aiohttp.ClientSSLError:
            # Fallback to http://
            try:
                async with session.get(
                    f"http://{domain}", allow_redirects=True,
                    max_redirects=MAX_REDIRECTS, ssl=False
                ) as resp:
                    status = resp.status
                    redir = str(resp.url)
                    ct = resp.headers.get("Content-Type", "")
                    if "html" in ct.lower() and status == 200:
                        raw = b""
                        async for chunk in resp.content.iter_chunked(4096):
                            raw += chunk
                            if len(raw) >= MAX_HEAD_BYTES or HEAD_CLOSE_RE.search(raw):
                                break
                        loop = asyncio.get_running_loop()
                        p = await loop.run_in_executor(None, parse_head, raw)
                        title, desc, lang, loc = p["title"], p["desc"], p["lang"], p["location"]
                    err = "ssl_fail_http_ok"
            except Exception:
                status, err = -4, "ssl_error"
            break
        except aiohttp.TooManyRedirects:
            status, err = -5, "too_many_redirects"
            break
        except Exception as e:
            status, err = -6, f"err: {str(e)[:80]}"
            break

    ms = int((time.monotonic() - t0) * 1000)
    return (domain, category, status, title, desc, lang, loc, redir, ms, now, err)


# -- Threaded DNS resolver (Windows-safe) -------------------------------

class ThreadedResolver:
    """
    Offloads blocking socket.getaddrinfo to a thread pool so DNS
    never stalls the asyncio event loop. Works on Windows without aiodns.
    """
    def __init__(self):
        self._executor = ThreadPoolExecutor(max_workers=CONCURRENCY)

    async def resolve(self, hostname, port=0, family=socket.AF_INET):
        loop = asyncio.get_running_loop()
        infos = await loop.run_in_executor(
            self._executor,
            lambda: socket.getaddrinfo(
                hostname, port, family=family, type=socket.SOCK_STREAM
            )
        )
        return [
            {"hostname": hostname, "host": addr[0], "port": addr[1],
             "family": fam, "proto": proto, "flags": 0}
            for fam, _, proto, _, addr in infos
        ]

    async def close(self):
        self._executor.shutdown(wait=False)


# -- Threaded DNS resolver (Windows-safe) -------------------------------

class ThreadedResolver:
    """
    Offloads blocking socket.getaddrinfo to a thread pool so DNS
    never stalls the asyncio event loop. Works on Windows without aiodns.
    """
    def __init__(self):
        self._executor = ThreadPoolExecutor(max_workers=CONCURRENCY)

    async def resolve(self, hostname, port=0, family=socket.AF_INET):
        loop = asyncio.get_running_loop()
        infos = await loop.run_in_executor(
            self._executor,
            lambda: socket.getaddrinfo(
                hostname, port, family=family, type=socket.SOCK_STREAM
            )
        )
        return [
            {"hostname": hostname, "host": addr[0], "port": addr[1],
             "family": fam, "proto": proto, "flags": 0}
            for fam, _, proto, _, addr in infos
        ]

    async def close(self):
        self._executor.shutdown(wait=False)

# -- Main loop -----------------------------------------------------------────────

async def run():
    status_counts = collections.Counter()
    latency_buckets = collections.Counter()  # <1s, 1-5s, 5-12s, >12s
    
    init_db()
    total, done = count_pending()
    pending = total - done

    log.info(f"Total categorized : {total:,}")
    log.info(f"Already fetched   : {done:,}")
    log.info(f"Remaining         : {pending:,}")
    log.info(f"Concurrency       : {CONCURRENCY}")

    if pending == 0:
        log.info("Nothing to do.")
        return

    timeout = aiohttp.ClientTimeout(
        connect=CONNECT_TIMEOUT, sock_read=READ_TIMEOUT,
        total=CONNECT_TIMEOUT + READ_TIMEOUT + 10
    )
    
    resolver = ThreadedResolver()
    connector = aiohttp.TCPConnector(
        limit=CONCURRENCY, limit_per_host=4,
        ttl_dns_cache=600, ssl=False,
        enable_cleanup_closed=True,
        resolver=resolver,
    )
    queue = asyncio.Queue(maxsize=CONCURRENCY * 2)
    write_queue = asyncio.Queue()
    
    fetched_total = 0
    t_start = time.monotonic()
    
    # Dedicated DB Writer task
    async def db_writer():
        nonlocal fetched_total
        buf = []
        while True:
            item = await write_queue.get()
            if item is None:
                if buf:
                    buf_copy = buf[:]
                    buf.clear()
                    await asyncio.get_running_loop().run_in_executor(None, write_results_to_db, buf_copy)
                    fetched_total += len(buf_copy)
                write_queue.task_done()
                break
            
            buf.append(item)
            if len(buf) >= WRITE_BATCH_SIZE:
                to_write = buf[:]
                buf.clear()
                await asyncio.get_running_loop().run_in_executor(None, write_results_to_db, to_write)
                
                fetched_total += len(to_write)
                elapsed = time.monotonic() - t_start
                rate = fetched_total / elapsed if elapsed > 0 else 0
                remaining = pending - fetched_total
                eta = remaining / rate / 60 if rate > 0 else 0
                log.info(
                    f"Done: {fetched_total + done:,}/{total:,} | "
                    f"This session: {fetched_total:,} | "
                    f"Rate: {rate:.0f}/sec | ETA: {eta:.0f}m"
                )

                if fetched_total % 2000 == 0:
                    log.info(f"Status dist: {dict(status_counts.most_common(8))}")
                    log.info(f"Latency dist: {dict(latency_buckets)}")
                    log.info(f"Write queue depth: {write_queue.qsize()}")
                    log.info(f"Fetch queue depth: {queue.qsize()}")
                    log.info(f"Active OS threads: {threading.active_count()}")

            write_queue.task_done()
    
    # Producer: continuously feed queue from DB
    async def producer():
        last_rowid = 0
        while True:
            # Re-query DB moving forward by ROWID
            rows = fetch_batch_from_db(last_rowid)
            if not rows:
                break
            for r_id, d, c in rows:
                await queue.put((d, c))
                last_rowid = r_id
                
        # Send shutdown signals to workers
        for _ in range(CONCURRENCY):
            await queue.put(None)

    # Consumer worker: fetch and feed write_queue
    async def worker(session):
        while True:
            item = await queue.get()
            if item is None:
                queue.task_done()
                break
            d, c = item
            
            try:
                result = await fetch_one(session, d, c)
            except Exception as e:
                log.error(f"Worker error on {d}: {e}")
                queue.task_done()
                continue
                
            ms = result[8]  # fetch_ms column
            status_counts[result[2]] += 1  # status_code column
            
            if ms < 1000:    latency_buckets["<1s"] += 1
            elif ms < 5000:  latency_buckets["1-5s"] += 1
            elif ms < 12000: latency_buckets["5-12s"] += 1
            else:            latency_buckets[">12s"] += 1

            await write_queue.put(result)
            queue.task_done()

    async with aiohttp.ClientSession(
        connector=connector, headers=REQUEST_HEADERS, timeout=timeout
    ) as session:
        # Start producer
        prod_task = asyncio.create_task(producer())
        # Start exactly CONCURRENCY workers
        workers = [asyncio.create_task(worker(session)) for _ in range(CONCURRENCY)]
        # Start DB writer
        writer_task = asyncio.create_task(db_writer())
        
        # Wait for producer and workers
        await asyncio.gather(prod_task, *workers)

        # Signal writer to flush and exit
        await write_queue.put(None)
        await writer_task

    log.info("Fetch complete!")
    print_final_stats()


def print_final_stats():
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute("""
        SELECT status_code, COUNT(*) as cnt
        FROM domain_metadata
        GROUP BY status_code
        ORDER BY cnt DESC
    """).fetchall()
    total = conn.execute("SELECT COUNT(*) FROM domain_metadata").fetchone()[0]
    conn.close()

    log.info(f"\n{'='*50}")
    log.info(f"  {'Status':<25} {'Count':>10} {'%':>7}")
    log.info(f"  {'-'*50}")
    labels = {
        200: "200 OK", 301: "301 Redirect", 302: "302 Redirect",
        403: "403 Forbidden", 404: "404 Not Found",
        -1: "Timeout", -2: "DNS Fail", -3: "Conn Error",
        -4: "SSL Error", -5: "Too Many Redirects", -6: "Other Error",
    }
    for status, cnt in rows:
        pct = cnt * 100.0 / total if total else 0
        label = labels.get(status, str(status))
        log.info(f"  {label:<25} {cnt:>10,} {pct:>6.1f}%")
    log.info(f"{'='*50}")


if __name__ == "__main__":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(run())