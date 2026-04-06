#!/bin/bash
# startup.sh — GCP VM startup script
# Copy this file into GCP VM metadata (startup-script key) or run manually.
# Each VM needs: SHARD_INDEX, SHARD_TOTAL, PG_HOST, PG_PASSWORD env vars.
#
# To set per-VM:  In GCP Console → VM → Edit → Metadata → add key:value
#   startup-script = <contents of this file>
#   SHARD_INDEX    = 0        ← change for each VM (0-9)
#   SHARD_TOTAL    = 10
#   PG_HOST        = 10.x.x.x  ← Cloud SQL private IP
#   PG_PASSWORD    = yourpass

set -e

# ── Read GCP metadata for per-VM env vars ─────────────────────────────────────
get_meta() {
    local val=$(curl -sf "http://metadata.google.internal/computeMetadata/v1/instance/attributes/$1" -H "Metadata-Flavor: Google" || true)
    echo "${val:-$2}"
}

export SHARD_INDEX=$(get_meta SHARD_INDEX "0")
export SHARD_TOTAL=$(get_meta SHARD_TOTAL "1")
export PG_HOST=$(get_meta PG_HOST "34.93.217.19")
export PG_PORT=$(get_meta PG_PORT "5432")
export PG_DB=$(get_meta PG_DB "domains")
export PG_USER=$(get_meta PG_USER "scraper")
export PG_PASSWORD=$(get_meta PG_PASSWORD)
export CONCURRENCY=$(get_meta CONCURRENCY "300")
export PARSE_WORKERS=$(get_meta PARSE_WORKERS)

echo "[startup] Shard: $SHARD_INDEX / $SHARD_TOTAL | PG_HOST: $PG_HOST"

# ── System packages ───────────────────────────────────────────────────────────
apt-get update -qq
apt-get install -y python3-pip python3-venv git libpq-dev > /dev/null

# ── Pull latest code (always sync on every boot) ─────────────────────────────
WORKDIR="/opt/scraper"
mkdir -p "$WORKDIR"
gsutil -m rsync -r "gs://webcrawler-code-bucket/gcp_scraper/" "$WORKDIR/"

cd "$WORKDIR"

# ── Python env ────────────────────────────────────────────────────────────────
python3 -m venv venv
source venv/bin/activate
pip install -q -r requirements.txt
python3 -m spacy download en_core_web_sm -q 2>/dev/null || true

# ── Run scraper ───────────────────────────────────────────────────────────────
echo "[startup] Starting self-healing scraper shard $SHARD_INDEX..."
cat << 'EOF' > run_loop.sh
#!/bin/bash
while true; do
    echo "[$(date)] Starting scraper..." >> /var/log/scraper.log
    python3 main.py >> /var/log/scraper.log 2>&1
    echo "[$(date)] Scraper exited with code $?. Restarting in 10s..." >> /var/log/scraper.log
    sleep 10
done
EOF
chmod +x run_loop.sh
nohup ./run_loop.sh >> /var/log/scraper.log 2>&1 &
echo "[startup] Scraper loop launched. PID=$!"
echo "Tail logs: sudo tail -f /var/log/scraper.log"
