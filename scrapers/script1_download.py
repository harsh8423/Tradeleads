# script1_download.py
import zlib, json, sqlite3, time, logging, requests
from urllib.parse import urlparse
from concurrent.futures import ProcessPoolExecutor, as_completed

# ── Config ────────────────────────────────────────────────
BASE_URL      = "https://data.commoncrawl.org"
OUTPUT_DB     = r"E:\domains.db"
TOTAL_SHARDS  = 300
WORKERS       = 20     # keep low to avoid saturating bandwidth
BATCH_SIZE    = 5000   # domains buffered before SQLite write
MAX_RETRIES   = 6      # retries per shard on transient errors
CHUNK_SIZE    = 5 * 1024 * 1024  # 5 MB download chunks
ALLOWED_TLDS  = {
    # Generic TLDs
    ".com", ".org", ".net", ".info", ".biz",
    # Tech / startup TLDs
    ".io", ".ai", ".tech", ".dev", ".co",
    # Country-code TLDs
    ".in", ".us", ".uk", ".ca", ".au", ".de", ".fr",
    ".jp", ".br", ".ru", ".cn", ".nz", ".eu"
}
BLOCKED_DOMAINS = {
    # Free hosting / PaaS subdomains
    ".vercel.app", ".netlify.app", ".netlify.com",
    ".onrender.com", ".render.com",
    ".herokuapp.com",
    ".github.io", ".gitlab.io", ".bitbucket.io",
    ".pages.dev",             # Cloudflare Pages
    ".web.app", ".firebaseapp.com",  # Firebase
    ".azurewebsites.net", ".azurestaticapps.net",
    ".amplifyapp.com",        # AWS Amplify
    ".surge.sh",
    ".fly.dev",
    ".railway.app",
    ".replit.app", ".repl.co",
    ".glitch.me",
    ".pythonanywhere.com",
    ".blogspot.com", ".blogspot.in",
    ".wordpress.com",
    ".wixsite.com", ".wix.com",
    ".squarespace.com",
    ".weebly.com",
    ".carrd.co",
    ".webflow.io",
    ".bubble.io",
    ".framer.app", ".framer.website",
    ".myshopify.com",
    ".substack.com",
}
# ─────────────────────────────────────────────────────────

log = logging.getLogger("crawler")

def _setup_logging():
    """Configure logging once per process (safe for multiprocessing on Windows)."""
    if not log.handlers:
        log.setLevel(logging.INFO)
        fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
        fh = logging.FileHandler("download.log")
        fh.setFormatter(fmt)
        sh = logging.StreamHandler()
        sh.setFormatter(fmt)
        log.addHandler(fh)
        log.addHandler(sh)

def get_latest_crawl_id():
    r = requests.get("https://index.commoncrawl.org/collinfo.json", timeout=15)
    crawl_id = r.json()[0]["id"]
    log.info(f"Latest crawl: {crawl_id}")
    return crawl_id

def init_db(db_path):
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA cache_size=-131072")  # 128MB page cache
    conn.execute("""
        CREATE TABLE IF NOT EXISTS domains (
            domain       TEXT PRIMARY KEY,
            last_crawled TEXT,
            shard        TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS progress (
            shard       TEXT PRIMARY KEY,
            status      TEXT,
            new_domains INTEGER,
            finished_at TEXT,
            lines_processed INTEGER DEFAULT 0
        )
    """)
    try:
        conn.execute("ALTER TABLE progress ADD COLUMN lines_processed INTEGER DEFAULT 0")
    except sqlite3.OperationalError:
        pass
    conn.commit()
    conn.close()
    log.info("Database initialized")

def get_completed_shards(db_path):
    conn = sqlite3.connect(db_path)
    rows = conn.execute(
        "SELECT shard FROM progress WHERE status='done'"
    ).fetchall()
    conn.close()
    done = {r[0] for r in rows}
    log.info(f"Shards already done: {len(done)}/{TOTAL_SHARDS}")
    return done

def parse_domain(url):
    """Extract clean root domain. Returns None if invalid, TLD not allowed, or blocked."""
    try:
        host = urlparse(url).netloc.lower()
        if host.startswith("www."):
            host = host[4:]
        if host and "." in host and len(host) > 3:
            # Check TLD against whitelist
            tld = "." + host.rsplit(".", 1)[-1]
            if tld not in ALLOWED_TLDS:
                return None
            # Reject free hosting subdomains
            if any(host.endswith(b) for b in BLOCKED_DOMAINS):
                return None
            return host
    except Exception:
        pass
    return None

def batch_write(db_path, rows):
    """Write a batch of domain rows to SQLite."""
    if not rows:
        return
    conn = sqlite3.connect(db_path, timeout=60)
    conn.executemany(
        "INSERT OR IGNORE INTO domains (domain, last_crawled, shard) VALUES (?,?,?)",
        rows
    )
    conn.commit()
    conn.close()

def mark_shard_done(db_path, shard_name, new_count, lines_parsed):
    conn = sqlite3.connect(db_path, timeout=60)
    conn.execute(
        "INSERT OR REPLACE INTO progress (shard, status, new_domains, finished_at, lines_processed) VALUES (?,?,?,?,?)",
        (shard_name, "done", new_count, time.strftime("%Y-%m-%d %H:%M:%S"), lines_parsed)
    )
    conn.commit()
    conn.close()

def mark_shard_progress(db_path, shard_name, new_count, lines_parsed):
    conn = sqlite3.connect(db_path, timeout=60)
    conn.execute(
        "INSERT OR REPLACE INTO progress (shard, status, new_domains, finished_at, lines_processed) VALUES (?,?,?,?,?)",
        (shard_name, "in_progress", new_count, time.strftime("%Y-%m-%d %H:%M:%S"), lines_parsed)
    )
    conn.commit()
    conn.close()

def get_shard_resume_line(db_path, shard_name):
    conn = sqlite3.connect(db_path, timeout=60)
    row = conn.execute("SELECT lines_processed, new_domains FROM progress WHERE shard = ?", (shard_name,)).fetchone()
    conn.close()
    if row and row[0]:
        return max(0, row[0] - 500_000), (row[1] or 0)
    return 0, 0

def _process_line(line, seen, buffer, shard_name):
    """Parse one CDX line. Returns 1 if a new domain was added, else 0."""
    parts = line.split(" ", 2)
    if len(parts) < 3:
        return 0
    try:
        meta = json.loads(parts[2])
    except json.JSONDecodeError:
        return 0

    if meta.get("status") != "200":
        return 0
    mime = meta.get("mime", "")
    if mime and "html" not in mime.lower():
        return 0

    url_field = meta.get("url", "")
    domain = parse_domain(url_field)
    if not domain:
        return 0

    if domain not in seen:
        seen.add(domain)
        buffer.append((domain, parts[1], shard_name))
        return 1
    return 0

def process_shard(shard_index, crawl_id, db_path):
    """
    Stream one CDX shard via HTTPS, decompress on the fly,
    extract unique domains, write to SQLite in batches.
    Handles multi-member gzip (ZipNum format).
    """
    _setup_logging()  # ensure logging is configured in this child process

    shard_name = f"cdx-{str(shard_index).zfill(5)}.gz"
    url = f"{BASE_URL}/cc-index/collections/{crawl_id}/indexes/{shard_name}"

    buffer = []
    seen_local = set()
    resume_line, new_count = get_shard_resume_line(db_path, shard_name)
    
    # Use a session with keep-alives to reduce IncompleteRead drops
    session = requests.Session()
    adapter = requests.adapters.HTTPAdapter(max_retries=0, pool_connections=1, pool_maxsize=1)
    session.mount("https://", adapter)

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            if resume_line > 0:
                log.info(f"[{shard_name}] Starting download (attempt {attempt}/{MAX_RETRIES}) | Fast-forwarding to line {resume_line:,}")
            else:
                log.info(f"[{shard_name}] Starting download (attempt {attempt}/{MAX_RETRIES})")
            t_start = time.time()

            resp = session.get(url, stream=True, timeout=(15, 300))
            resp.raise_for_status()

            decompressor = zlib.decompressobj(wbits=31)
            leftover = ""
            lines_parsed = 0
            bytes_downloaded = 0

            for chunk in resp.iter_content(chunk_size=CHUNK_SIZE):
                if not chunk:
                    continue
                bytes_downloaded += len(chunk)

                # Handle multi-member gzip (ZipNum format):
                # Each chunk may span multiple gzip members.
                # Decompress, check for unused data, and recycle
                # the decompressor when a member boundary is hit.
                raw = chunk
                decompressed = b""
                while raw:
                    try:
                        decompressed += decompressor.decompress(raw)
                    except zlib.error:
                        # Corrupted block — skip remaining bytes in this chunk
                        break
                    raw = decompressor.unused_data
                    if raw:
                        # End of one gzip member, start next
                        decompressor = zlib.decompressobj(wbits=31)

                text = leftover + decompressed.decode("utf-8", errors="ignore")
                lines = text.split("\n")
                leftover = lines.pop()  # incomplete last line

                for line in lines:
                    line = line.strip()
                    if not line:
                        continue
                    lines_parsed += 1

                    # Fast-forward skip
                    if lines_parsed <= resume_line:
                        continue

                    new_count += _process_line(line, seen_local, buffer, shard_name)

                    # Flush buffer to DB in batches
                    if len(buffer) >= BATCH_SIZE:
                        batch_write(db_path, buffer)
                        buffer.clear()

                    # Progress log and save state every 500K lines
                    if lines_parsed % 500_000 == 0:
                        batch_write(db_path, buffer)
                        buffer.clear()
                        mark_shard_progress(db_path, shard_name, new_count, lines_parsed)
                        
                        elapsed = time.time() - t_start
                        mb = bytes_downloaded / (1024 * 1024)
                        log.info(
                            f"[{shard_name}] {lines_parsed:,} lines | "
                            f"{new_count:,} new domains | "
                            f"{mb:.0f} MB downloaded | {elapsed:.0f}s elapsed"
                        )

            # Process any remaining leftover
            if leftover.strip():
                lines_parsed += 1
                new_count += _process_line(leftover.strip(), seen_local, buffer, shard_name)

            # Final flush for this shard
            batch_write(db_path, buffer)
            mark_shard_done(db_path, shard_name, new_count, lines_parsed)
            elapsed = time.time() - t_start
            mb = bytes_downloaded / (1024 * 1024)
            log.info(
                f"[{shard_name}] DONE in {elapsed:.0f}s | "
                f"{lines_parsed:,} lines parsed | {mb:.0f} MB downloaded | "
                f"{new_count:,} new domains saved"
            )
            session.close()
            return shard_name, new_count, None

        except Exception as e:
            if attempt < MAX_RETRIES:
                # Longer backoff for DNS / network failures
                is_dns = "NameResolution" in str(e) or "getaddrinfo" in str(e)
                wait = 30 if is_dns else min(2 ** attempt, 60)
                log.warning(
                    f"[{shard_name}] attempt {attempt} failed, "
                    f"retrying in {wait}s{' (DNS recovery)' if is_dns else ''} — {e}"
                )
                time.sleep(wait)
                buffer.clear()
                # Reload resume state in case it updated before the crash
                resume_line, new_count = get_shard_resume_line(db_path, shard_name)
            else:
                session.close()
                return shard_name, 0, str(e)

def print_stats(db_path):
    conn = sqlite3.connect(db_path)
    total = conn.execute("SELECT COUNT(*) FROM domains").fetchone()[0]
    done  = conn.execute("SELECT COUNT(*) FROM progress WHERE status='done'").fetchone()[0]
    conn.close()
    log.info(f"Progress: {done}/{TOTAL_SHARDS} shards | {total:,} unique domains")
    return total

def main():
    _setup_logging()  # configure logging in the parent process
    crawl_id  = get_latest_crawl_id()
    init_db(OUTPUT_DB)
    completed = get_completed_shards(OUTPUT_DB)

    pending = [
        i for i in range(TOTAL_SHARDS)
        if f"cdx-{str(i).zfill(5)}.gz" not in completed
    ]

    if not pending:
        log.info("All shards already processed.")
        print_stats(OUTPUT_DB)
        return

    log.info(f"Shards to process: {len(pending)} | Workers: {WORKERS}")

    with ProcessPoolExecutor(max_workers=WORKERS) as executor:
        futures = {
            executor.submit(process_shard, i, crawl_id, OUTPUT_DB): i
            for i in pending
        }
        for future in as_completed(futures):
            shard_name, count, error = future.result()
            if error:
                log.error(f"[{shard_name}] FAILED — {error}")
            else:
                total = print_stats(OUTPUT_DB)
                log.info(f"[{shard_name}] +{count:,} new domains | Running total: {total:,}")

    log.info("=== DOWNLOAD COMPLETE ===")
    print_stats(OUTPUT_DB)

if __name__ == "__main__":
    main()