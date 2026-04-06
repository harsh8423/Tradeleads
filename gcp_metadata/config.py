# config.py — GCP metadata scraper configuration
import os
import multiprocessing

# ── PostgreSQL ────────────────────────────────────────────────────────────────
PG_HOST      = os.environ.get("PG_HOST")      or "34.93.217.19"
PG_PORT      = int(os.environ.get("PG_PORT")  or "5432")
PG_DB        = os.environ.get("PG_DB")        or "domains"
PG_USER      = os.environ.get("PG_USER")      or "scraper"
PG_PASSWORD  = os.environ.get("PG_PASSWORD")

# ── Sharding ──────────────────────────────────────────────────────────────────
# Each VM is assigned a shard index via GCP instance metadata.
# VM 0 → 1/N of domains, VM 1 → 1/N, etc.
SHARD_INDEX  = int(os.environ.get("SHARD_INDEX") or "0")
SHARD_TOTAL  = int(os.environ.get("SHARD_TOTAL") or "1")

# ── Scraper tuning ────────────────────────────────────────────────────────────
ALLOWED_CATEGORIES = ["exim", "logistics", "commodity"]
CONCURRENCY        = int(os.environ.get("CONCURRENCY") or "400")
CONNECT_TIMEOUT    = 12     # seconds to establish TCP
READ_TIMEOUT       = 15     # seconds to read body
MAX_REDIRECTS      = 5
MAX_HEAD_BYTES     = 32_768  # 32KB — all we need for <head>
BATCH_SIZE         = 2_000
WRITE_BATCH_SIZE   = 500
MAX_RETRIES        = 2
RETRY_STATUSES     = {429, 500, 502, 503, 504}

# ── Request headers ───────────────────────────────────────────────────────────
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
