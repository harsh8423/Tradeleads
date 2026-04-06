# script2_parse_zone.py - Parse .com zone file, categorize and insert domains
import sqlite3, time, logging, multiprocessing as mp
import threading, os, json
from typing import List, Tuple
from categorize import categorize  # Using the exact same tokenize/categorize logic

# ── Config ─────────────────────────────────────────────
ZONE_FILE  = r"E:\com.zone.51681"
DB_PATH    = r"E:\domains.db"
PROGRESS_FILE = r"E:\parse_zone_progress.json"
WORKERS    = 14            # Keep some threads for IO
BATCH_SIZE = 250_000       # How many lines to read before sending to a worker
TOTAL_SHARDS = 300
# ───────────────────────────────────────────────────────

log = logging.getLogger("parse_zone")

def _setup_logging():
    if not log.handlers:
        log.setLevel(logging.INFO)
        fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
        fh = logging.FileHandler("parse_zone.log")
        fh.setFormatter(fmt)
        sh = logging.StreamHandler()
        sh.setFormatter(fmt)
        log.addHandler(fh)
        log.addHandler(sh)


# ── Database Writer Thread ──────────────────────────────
def db_writer(queue: mp.Queue):
    """
    Runs in a background thread in the main process.
    Reads categorized domains from queue, assigns shards sequentially, and bulk writes.
    """
    _setup_logging()
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    
    # Ensure category column exists
    existing_cols = {r[1] for r in conn.execute("PRAGMA table_info(domains)")}
    if "category" not in existing_cols:
        conn.execute("ALTER TABLE domains ADD COLUMN category TEXT")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_category ON domains(category)")
    conn.commit()

    total_added = 0
    buffer = []
    shard_counter = 1
    today = time.strftime("%Y%m%d")

    while True:
        msg = queue.get()
        if msg == "DONE":
            if buffer:
                conn.executemany(
                    "INSERT INTO domains (domain, last_crawled, shard, category) VALUES (?, ?, ?, ?)",
                    buffer
                )
                conn.commit()
            break
            
        domains_batch = msg
        for domain, category in domains_batch:
            shard_name = str(shard_counter)
            buffer.append((domain, today, shard_name, category))
            
            shard_counter += 1
            if shard_counter > TOTAL_SHARDS:
                shard_counter = 1

        total_added += len(domains_batch)

        if len(buffer) >= 10_000:
            conn.executemany(
                "INSERT INTO domains (domain, last_crawled, shard, category) VALUES (?, ?, ?, ?)",
                buffer
            )
            # Write last 10 parsed domains to stats file
            try:
                with open(r"E:\parsed_domains_sample.txt", "w") as sf:
                    sf.write("LAST 10 CATEGORIZED DOMAINS:\n")
                    sf.write("-" * 40 + "\n")
                    last_10 = buffer[-10:] if len(buffer) >= 10 else buffer
                    for d, _, _, c in last_10:
                        sf.write(f"{d:<35} -> {c}\n")
            except Exception:
                pass
            
            buffer.clear()

    conn.close()
    log.info(f"DB Writer finished. Total domains added to DB: {total_added:,}")


# ── Worker Process ──────────────────────────────────────
def process_domain_chunk(domains: List[str]) -> List[Tuple[str, str]]:
    """
    Runs in worker process. Iterates domains and categorizes them.
    Filters out those with None category.
    """
    results = []
    for domain in domains:
        category = categorize(domain)
        if category:
            results.append((domain, category))
    return results


# ── Main File Parsing ────────────────────────────────────
def main():
    _setup_logging()
    log.info("Starting zone file parser...")

    # 1. Load existing domains into RAM for fast O(1) deduplication
    log.info(f"Loading existing domains from {DB_PATH} (takes a few seconds)...")
    t0 = time.time()
    conn = sqlite3.connect(DB_PATH)
    existing_domains = {r[0] for r in conn.execute("SELECT domain FROM domains")}
    conn.close()
    log.info(f"Loaded {len(existing_domains):,} existing domains in {time.time() - t0:.1f}s")

    # Setup multiprocessing pool and queue
    manager = mp.Manager()
    db_queue = manager.Queue()
    pool = mp.Pool(processes=WORKERS)
    
    # Start DB writer thread
    writer_thread = threading.Thread(target=db_writer, args=(db_queue,))
    writer_thread.start()

    log.info(f"Reading zone file: {ZONE_FILE}")
    t_start = time.time()
    
    start_line = 0
    if os.path.exists(PROGRESS_FILE):
        try:
            with open(PROGRESS_FILE, 'r') as pf:
                start_line = json.load(pf).get("lines_parsed", 0)
        except Exception:
            pass

    lines_parsed = 0
    domains_found = 0
    in_memory_seen = set()  # To deduplicate domains found inside the zone file itself
    
    chunk = []
    jobs = []

    def dispatch_chunk(c_list):
        # We append a callback to put results in db_queue directly
        jobs.append(
            pool.apply_async(
                process_domain_chunk, 
                args=(c_list,),
                callback=db_queue.put
            )
        )

    # Open the file and parse
    try:
        with open(ZONE_FILE, 'r', encoding='utf-8', errors='ignore') as f:
            if start_line > 0:
                log.info(f"Resuming from line {start_line:,}. Fast-forwarding (this takes a few seconds)...")
                from itertools import islice
                from collections import deque
                deque(islice(f, start_line), maxlen=0)
                lines_parsed = start_line
                log.info("Fast-forward complete. Resuming extraction!")

            for line in f:
                lines_parsed += 1
                
                # Split the zone file line: e.g. "example.com.  172800 IN NS ns1.example.com."
                # Take the first token
                parts = line.split(maxsplit=1)
                if not parts:
                    continue
                
                domain = parts[0].lower()
                
                # Remove trailing dot if present
                if domain.endswith('.'):
                    domain = domain[:-1]
                
                # Skip if already exists in DB or if already seen in file
                if domain in existing_domains or domain in in_memory_seen:
                    continue
                
                # Validate length briefly
                if '.' not in domain or len(domain) < 4:
                    continue
                    
                in_memory_seen.add(domain)
                domains_found += 1
                chunk.append(domain)
                
                if len(chunk) >= BATCH_SIZE:
                    dispatch_chunk(chunk)
                    chunk = []
                    
                # Live Stats printing
                if lines_parsed % 2_500_000 == 0:
                    elapsed = time.time() - t_start
                    rate = (lines_parsed - start_line) / elapsed if elapsed > 0 else 0
                    log.info(f"Parsed {lines_parsed:,} lines | Unique new domains found: {domains_found:,} | Rate: {rate:,.0f} lines/sec")
                    try:
                        with open(PROGRESS_FILE, 'w') as pf:
                            json.dump({"lines_parsed": lines_parsed}, pf)
                    except Exception as e:
                        log.error(f"Failed to save progress: {e}")

        # Dispatch anything left
        if chunk:
            dispatch_chunk(chunk)

    except FileNotFoundError:
        log.error(f"Could not find zone file at {ZONE_FILE}!")
        db_queue.put("DONE")
        pool.close()
        pool.join()
        writer_thread.join()
        return

    # Wait for all categorize workers to finish
    log.info("Finished reading file. Waiting for workers to finish categorizing...")
    for j in jobs:
        j.get()  # Block until worker is done
    
    pool.close()
    pool.join()
    
    # Notify DB writer to stop
    db_queue.put("DONE")
    writer_thread.join()
    
    elapsed = time.time() - t_start
    log.info("\n" + "="*50)
    log.info(f"COMPLETED in {elapsed/60:.1f} minutes")
    log.info(f"Lines parsed         : {lines_parsed:,}")
    log.info(f"Target Domains found : {domains_found:,}")
    log.info("="*50)
    
    # Show categorized stats
    conn = sqlite3.connect(DB_PATH)
    total = conn.execute("SELECT COUNT(*) FROM domains").fetchone()[0]
    cats  = conn.execute("SELECT category, COUNT(*) FROM domains WHERE category IS NOT NULL GROUP BY category ORDER BY COUNT(*) DESC").fetchall()
    conn.close()
    
    log.info(f"Total domains now in DB: {total:,}")
    for cat, count in cats:
        log.info(f"  {cat}: {count:,}")


if __name__ == "__main__":
    # Protect against multiple executions in windows
    mp.freeze_support()
    main()
