#!/bin/bash
# playwright_startup.sh — GCP VM startup script for Playwright second-pass scraper
# Runs after the main scraper completes to render JS-heavy empty domains.

set -e

# ── Read metadata ─────────────────────────────────────────────────────────────
get_meta() {
  local val=$(curl -sf "http://metadata.google.internal/computeMetadata/v1/instance/attributes/$1" -H "Metadata-Flavor: Google" || true)
  echo "${val:-$2}"
}

export PG_HOST=$(get_meta PG_HOST "34.93.217.19")
export PG_PORT=$(get_meta PG_PORT "5432")
export PG_DB=$(get_meta PG_DB "domains")
export PG_USER=$(get_meta PG_USER "scraper")
export PG_PASSWORD=$(get_meta PG_PASSWORD)
export SHARD_INDEX=$(get_meta SHARD_INDEX "0")
export SHARD_TOTAL=$(get_meta SHARD_TOTAL "1")
export PW_CONTEXTS=$(get_meta PW_CONTEXTS "20")

echo "[pw_startup] Shard: $SHARD_INDEX / $SHARD_TOTAL | PW_CONTEXTS: $PW_CONTEXTS"

# ── System packages ───────────────────────────────────────────────────────────
apt-get update -qq
apt-get install -y python3-pip python3-venv git \
  libpq-dev \
  libnss3 libatk1.0-0 libatk-bridge2.0-0 libxcomposite1 libxdamage1 \
  libxfixes3 libxrandr2 libgbm1 libxkbcommon0 libasound2 libpangocairo-1.0-0 \
  libcups2 libdrm2 libgtk-3-0 > /dev/null

# ── Pull latest code ──────────────────────────────────────────────────────────
WORKDIR="/opt/scraper"
mkdir -p "$WORKDIR"
gsutil -m rsync -r "gs://webcrawler-code-bucket/gcp_scraper/" "$WORKDIR/"
cd "$WORKDIR"

# ── Python env ────────────────────────────────────────────────────────────────
python3 -m venv venv
source venv/bin/activate
pip install -q -r requirements.txt
pip install -q playwright
python3 -m playwright install chromium
python3 -m spacy download en_core_web_sm -q 2>/dev/null || true

# ── Logging ───────────────────────────────────────────────────────────────────
exec > >(tee -a /var/log/pw_scraper.log) 2>&1

echo "[pw_startup] Starting Playwright scraper shard $SHARD_INDEX..."
python3 playwright_scraper.py
echo "[pw_startup] Done."
