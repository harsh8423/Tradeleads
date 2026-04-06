# script1_certsh.py — Fetch domains from crt.sh via HTTPS JSON API
# Uses crt.sh/?q=KEYWORD&output=json — much more stable than direct PostgreSQL
import sqlite3, time, logging, re, requests
from urllib.parse import urlparse

# ── Config ─────────────────────────────────────────────
OUTPUT_DB   = r"E:\domains.db"
BATCH_SIZE  = 5_000
MAX_RETRIES = 5
REQUEST_TIMEOUT = 120       # crt.sh can be slow — large result sets take time

KEYWORDS = [
    # Logistics
    "freight", "logistics", "cargo", "shipping", "courier",
    "forwarder", "warehouse", "3pl", "haulage", "trucking",
    "clearance", "customs", "airfreight", "seafreight",
    # Exim
    "import", "export", "exim", "trading", "tradehouse",
    "broker", "overseas", "international",
    # Commodity / Agriculture
    "agri", "agro", "agriculture", "farm", "grain", "wheat",
    "rice", "cotton", "spice", "commodity", "pulses", "seeds",
    "fertilizer", "oilseed", "dairy", "poultry", "organic",
    "horticulture", "aquaculture", "mandi", "harvest",
]

ALLOWED_TLDS = {
    ".com", ".org", ".net", ".info", ".biz",
    ".io", ".ai", ".tech", ".dev", ".co",
    ".in", ".us", ".uk", ".ca", ".au",
    ".de", ".fr", ".sg", ".ae", ".cn",
    ".jp", ".br", ".mx", ".za", ".nz", ".eu",
}

BLOCKED_SUFFIXES = {
    ".vercel.app", ".netlify.app", ".netlify.com",
    ".onrender.com", ".render.com", ".herokuapp.com",
    ".github.io", ".gitlab.io", ".bitbucket.io",
    ".pages.dev", ".web.app", ".firebaseapp.com",
    ".azurewebsites.net", ".azurestaticapps.net",
    ".amplifyapp.com", ".surge.sh", ".fly.dev",
    ".railway.app", ".replit.app", ".repl.co",
    ".glitch.me", ".pythonanywhere.com",
    ".blogspot.com", ".wordpress.com",
    ".wixsite.com", ".squarespace.com", ".weebly.com",
    ".webflow.io", ".bubble.io", ".myshopify.com",
    ".substack.com", ".carrd.co",
}
# ───────────────────────────────────────────────────────

log = logging.getLogger("crtsh")

def setup_logging():
    if log.handlers:
        return
    log.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    fh = logging.FileHandler("crtsh_download.log", encoding="utf-8")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    log.addHandler(fh)
    log.addHandler(sh)


# ── Domain helpers ─────────────────────────────────────

WILDCARD_RE = re.compile(r"^\*\.")
IP_RE       = re.compile(r"^\d+\.\d+\.\d+\.\d+$")
MULTI_TLD   = {".co.in", ".com.au", ".co.uk", ".com.br", ".co.za", ".co.nz", ".co.jp"}

def extract_root_domain(name_value: str):
    name = WILDCARD_RE.sub("", name_value.lower().strip())
    if "://" in name:
        name = urlparse(name).netloc or name
    if name.startswith("www."):
        name = name[4:]
    if IP_RE.match(name) or "." not in name:
        return None
    if len(name) < 4 or len(name) > 253:
        return None

    parts = name.split(".")
    # Check 2-part TLD
    if len(parts) >= 3:
        two = f".{parts[-2]}.{parts[-1]}"
        if two in MULTI_TLD and len(parts) >= 3:
            root = f"{parts[-3]}{two}"
            if not any(root.endswith(b) for b in BLOCKED_SUFFIXES):
                return root

    tld = f".{parts[-1]}"
    if tld not in ALLOWED_TLDS:
        return None
    root = f"{parts[-2]}.{parts[-1]}"
    if len(root) < 4 or any(root.endswith(b) for b in BLOCKED_SUFFIXES):
        return None
    return root


# ── SQLite helpers ─────────────────────────────────────

def init_db():
    conn = sqlite3.connect(OUTPUT_DB)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS crtsh_progress (
            keyword     TEXT PRIMARY KEY,
            status      TEXT,
            new_domains INTEGER DEFAULT 0,
            total_certs INTEGER DEFAULT 0,
            finished_at TEXT
        )
    """)
    conn.commit()
    conn.close()
    log.info("DB ready")


def get_completed():
    conn = sqlite3.connect(OUTPUT_DB)
    done = {r[0] for r in conn.execute(
        "SELECT keyword FROM crtsh_progress WHERE status='done'"
    ).fetchall()}
    conn.close()
    return done


def mark_done(keyword, new_domains, total_certs):
    conn = sqlite3.connect(OUTPUT_DB, timeout=60)
    conn.execute("""
        INSERT OR REPLACE INTO crtsh_progress
        (keyword, status, new_domains, total_certs, finished_at)
        VALUES (?, 'done', ?, ?, ?)
    """, (keyword, new_domains, total_certs, time.strftime("%Y-%m-%d %H:%M:%S")))
    conn.commit()
    conn.close()


def batch_write(rows):
    if not rows:
        return 0
    conn = sqlite3.connect(OUTPUT_DB, timeout=60)
    conn.executemany(
        "INSERT OR IGNORE INTO domains (domain, last_crawled, shard) VALUES (?,?,?)",
        rows
    )
    written = conn.total_changes
    conn.commit()
    conn.close()
    return written


def print_stats():
    conn = sqlite3.connect(OUTPUT_DB)
    total = conn.execute("SELECT COUNT(*) FROM domains").fetchone()[0]
    done  = conn.execute(
        "SELECT COUNT(*) FROM crtsh_progress WHERE status='done'"
    ).fetchone()[0]
    conn.close()
    log.info(f"Total unique domains : {total:,} | crt.sh keywords done: {done}/{len(KEYWORDS)}")


# ── Fetch from crt.sh API ──────────────────────────────

def fetch_crtsh(keyword):
    """
    Call crt.sh HTTPS JSON API.
    Returns list of name_value strings or raises on failure.
    """
    url = "https://crt.sh/"
    params = {"q": f"%{keyword}%", "output": "json"}
    headers = {"User-Agent": "domain-research-bot/1.0 (non-commercial)"}

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.get(url, params=params, headers=headers,
                                timeout=REQUEST_TIMEOUT)
            if resp.status_code == 200:
                return resp.json()
            elif resp.status_code == 429:
                wait = 60 * attempt
                log.warning(f"[{keyword}] Rate limited, waiting {wait}s")
                time.sleep(wait)
            else:
                log.warning(f"[{keyword}] HTTP {resp.status_code}, attempt {attempt}")
                time.sleep(10)
        except requests.Timeout:
            log.warning(f"[{keyword}] Timeout (attempt {attempt}/{MAX_RETRIES})")
            time.sleep(15 * attempt)
        except Exception as e:
            log.warning(f"[{keyword}] Error: {e} (attempt {attempt}/{MAX_RETRIES})")
            time.sleep(10)

    raise RuntimeError(f"[{keyword}] Failed after {MAX_RETRIES} attempts")


# ── Process one keyword ────────────────────────────────

def process_keyword(keyword):
    log.info(f"[{keyword}] Fetching from crt.sh API...")
    t0 = time.time()

    certs = fetch_crtsh(keyword)
    total_certs = len(certs)
    log.info(f"[{keyword}] {total_certs:,} cert records received — parsing...")

    seen  = set()
    write_buf = []
    today = time.strftime("%Y%m%d")

    for cert in certs:
        # name_value can contain multiple names separated by \n
        for name in cert.get("name_value", "").split("\n"):
            root = extract_root_domain(name.strip())
            if root and root not in seen:
                seen.add(root)
                write_buf.append((root, today, f"crtsh_{keyword}"))

        if len(write_buf) >= BATCH_SIZE:
            batch_write(write_buf)
            write_buf.clear()

    new_domains = batch_write(write_buf) if write_buf else 0
    # Total unique domains found (includes ones already in DB)
    total_unique = len(seen)

    mark_done(keyword, total_unique, total_certs)
    elapsed = time.time() - t0
    log.info(
        f"[{keyword}] Done | {total_certs:,} certs | "
        f"{total_unique:,} unique domains found | "
        f"{elapsed:.0f}s"
    )


# ── Main ───────────────────────────────────────────────

def main():
    setup_logging()
    init_db()

    completed = get_completed()
    pending = [k for k in KEYWORDS if k not in completed]

    log.info(f"Keywords total    : {len(KEYWORDS)}")
    log.info(f"Already completed : {len(completed)}")
    log.info(f"Remaining         : {len(pending)}")

    if not pending:
        log.info("All keywords done.")
        print_stats()
        return

    for i, keyword in enumerate(pending, 1):
        log.info(f"\n[{i}/{len(pending)}] Keyword: '{keyword}'")
        try:
            process_keyword(keyword)
        except Exception as e:
            log.error(f"[{keyword}] FAILED: {e}")
        print_stats()
        time.sleep(5)   # polite pause between keywords

    log.info("\n=== CRT.SH COMPLETE ===")
    print_stats()


if __name__ == "__main__":
    main()