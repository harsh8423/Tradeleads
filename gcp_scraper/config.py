# config.py — Central configuration for GCP scraper
# All secrets/DSNs are read from environment variables so the same
# code runs identically on every VM; only the env differs.

import os
import multiprocessing

# ── PostgreSQL (Cloud SQL) ────────────────────────────────────────────────────
# Set these in your GCP VM startup script or .env file.
#   PG_HOST: Cloud SQL private IP or unix socket path
#   PG_DB, PG_USER, PG_PASSWORD: credentials
PG_HOST     = os.environ.get("PG_HOST") or "34.93.217.19"
PG_PORT     = int(os.environ.get("PG_PORT") or "5432")
PG_DB       = os.environ.get("PG_DB") or "domains"
PG_USER     = os.environ.get("PG_USER") or "scraper"
PG_PASSWORD = os.environ.get("PG_PASSWORD")

# ── Workload sharding ─────────────────────────────────────────────────────────
# Each VM is assigned a shard index (0-9) via env var.
# VM 0 → rowid 0..200_000, VM 1 → 200_001..400_000, etc.
# Set SHARD_INDEX (0-9) and SHARD_TOTAL (e.g. 10) on each VM.
SHARD_INDEX = int(os.environ.get("SHARD_INDEX") or "0")
SHARD_TOTAL = int(os.environ.get("SHARD_TOTAL") or "1")

# ── Scraper tuning ────────────────────────────────────────────────────────────
ALLOWED_CATEGORIES   = ["exim", "logistics", "commodity"]
CONCURRENCY          = int(os.environ.get("CONCURRENCY") or "100")
PARSE_WORKERS        = int(os.environ.get("PARSE_WORKERS") or str(multiprocessing.cpu_count()))
CONNECT_TIMEOUT      = 15
READ_TIMEOUT         = 30
MAX_REDIRECTS        = 5
MAX_BODY_BYTES       = 300_000  # was 2MB — contact data is in first 50KB, no need to parse more
MAX_PAGES_PER_DOMAIN = 3    # was 5 — homepage + 2 sub-pages (contact/about)
BATCH_SIZE           = 2_000
WRITE_BATCH_SIZE     = 100

# ── Request headers ───────────────────────────────────────────────────────────
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:123.0) Gecko/20100101 Firefox/123.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_3) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.3 Safari/605.1.15",
]

REQUEST_HEADERS = {
    "Accept":          "text/html,application/xhtml+xml",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate",
}
