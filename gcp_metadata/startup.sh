#!/bin/bash
# startup.sh — GCP VM startup script for metadata scraper
# Copy contents into GCP VM metadata key: startup-script
#
# Per-VM metadata keys to set in GCP Console → VM → Edit → Metadata:
#   SHARD_INDEX   = 0          ← unique per VM (0, 1, 2, ...)
#   SHARD_TOTAL   = 5          ← total number of VMs
#   PG_HOST       = 34.93.217.19
#   PG_PASSWORD   = Custarea@1
#   CONCURRENCY   = 400

set -e

get_meta() {
    local val=$(curl -sf "http://metadata.google.internal/computeMetadata/v1/instance/attributes/$1" \
        -H "Metadata-Flavor: Google" || true)
    echo "${val:-$2}"
}

export SHARD_INDEX=$(get_meta SHARD_INDEX "0")
export SHARD_TOTAL=$(get_meta SHARD_TOTAL "1")
export PG_HOST=$(get_meta PG_HOST "34.93.217.19")
export PG_PORT=$(get_meta PG_PORT "5432")
export PG_DB=$(get_meta PG_DB "domains")
export PG_USER=$(get_meta PG_USER "scraper")
export PG_PASSWORD=$(get_meta PG_PASSWORD "")
export CONCURRENCY=$(get_meta CONCURRENCY "400")

echo "[startup] Shard: $SHARD_INDEX / $SHARD_TOTAL | PG_HOST: $PG_HOST | CONCURRENCY: $CONCURRENCY"

# ── System packages ───────────────────────────────────────────────────────────
apt-get update -qq
apt-get install -y python3-pip python3-venv git libpq-dev > /dev/null

# ── Pull latest code from GCS ─────────────────────────────────────────────────
WORKDIR="/opt/metadata_scraper"
mkdir -p "$WORKDIR"
gsutil -m rsync -r "gs://webcrawler-code-bucket/gcp_metadata/" "$WORKDIR/"

cd "$WORKDIR"

# ── Python env ────────────────────────────────────────────────────────────────
python3 -m venv venv
source venv/bin/activate
pip install -q -r requirements.txt

# ── Self-healing loop ─────────────────────────────────────────────────────────
echo "[startup] Starting metadata scraper shard $SHARD_INDEX..."
cat << 'EOF' > run_loop.sh
#!/bin/bash
while true; do
    echo "[$(date)] Starting metadata scraper..." >> /var/log/metadata_scraper.log
    python3 main.py >> /var/log/metadata_scraper.log 2>&1
    echo "[$(date)] Exited with $?. Restarting in 15s..." >> /var/log/metadata_scraper.log
    sleep 15
done
EOF
chmod +x run_loop.sh
nohup ./run_loop.sh >> /var/log/metadata_scraper.log 2>&1 &
echo "[startup] Scraper loop launched. PID=$!"
echo "Tail logs: sudo tail -f /var/log/metadata_scraper.log"
