# categorize.py — Categorize 31M+ domains by keyword matching
import sqlite3, re, time, logging, multiprocessing as mp

# ── Config ─────────────────────────────────────────────
DB_PATH    = r"E:\domains.db"
BATCH_SIZE = 100_000       # domains read per batch (pure string work, go big)
WORKERS    = 14            # i7-11800H = 8C/16T, leave 2 threads for OS + DB writes
# ───────────────────────────────────────────────────────

log = logging.getLogger("categorize")

def _setup_logging():
    if not log.handlers:
        log.setLevel(logging.INFO)
        fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
        fh = logging.FileHandler("categorize.log")
        fh.setFormatter(fmt)
        sh = logging.StreamHandler()
        sh.setFormatter(fmt)
        log.addHandler(fh)
        log.addHandler(sh)

# ── Keyword definitions ────────────────────────────────

KEYWORDS = {
    "logistics": {
        # Core logistics & freight
        "freight", "cargo", "shipping", "courier", "logistics",
        "forwarder", "forwarding", "warehouse", "warehousing",
        "3pl", "4pl", "transport", "transportation",
        "haulage", "trucking", "dispatch", "fleet", "carrier",
        "delivery", "express", "parcel", "multimodal", "intermodal",
        "drayage", "fulfillment", "fulfilment", "airfreight",
        "seafreight", "breakbulk", "coldchain", "lastmile",
        "logistic", "transporter", "movers", "packers",
        "supplychain",
        # Shipping & Maritime
        "container", "vessel", "maritime", "marine", "port",
        "stevedore", "terminal", "dock", "berth",
        "chartering", "tanker", "bulk", "roro",
        # Road & Rail
        "rail", "railroad", "railway", "wagon", "trailer",
        "flatbed", "reefer", "ltl", "ftl", "truckload",
        "oversize", "overweight", "heavylift", "crane",
        # Storage & Distribution
        "depot", "distribution", "stockyard", "silo",
        "inventory", "storage", "pallets", "pallet",
        "crossdock", "sortation",
    },
    "exim": {
        # Import / Export / Trade
        "import", "export", "exim", "importer", "exporter",
        "importexport", "trade", "trader", "trading", "customs",
        "tariff", "clearance", "broker", "brokerage",
        "crossborder", "tranship", "transship", "shipment",
        "consignment", "consignee", "shipper", "incoterm",
        "tradehouse", "mercantile", "overseas",
        "importers", "exporters", "tradelink", "tradecorp",
        # Trade finance & compliance
        "fob", "cif", "bonded", "freeport", "freetrade",
        "ftz", "sez", "dutyfree", "quota", "embargo",
        "sanctions", "compliance", "hs code", "hscode",
        "billoflading", "lading", "manifest",
    },
    "commodity": {
        # ── Agriculture & Food ──
        "agri", "agro", "agriculture", "agricultural", "farm",
        "farmer", "farming", "crop", "crops", "grain", "grains",
        "wheat", "rice", "maize", "corn", "barley", "soybean",
        "soya", "pulse", "pulses", "lentil", "lentils", "chickpea",
        "sugar", "sugarcane", "cotton", "jute", "tobacco",
        "spice", "spices", "pepper", "turmeric", "cumin",
        "coriander", "cardamom", "ginger", "garlic", "onion",
        "seed", "seeds", "fertilizer", "pesticide",
        "commodity", "commodities", "produce", "harvest",
        "organic", "horticulture", "aquaculture", "dairy",
        "poultry", "livestock", "oilseed", "groundnut", "mustard",
        "sunflower", "saffron", "cashew", "almond", "walnut",
        "herbal", "herbs", "coffee", "cocoa", "tea", "rubber",
        "palm", "olive", "coconut", "banana", "mango", "fruit",
        "vegetable", "seafood", "fish", "shrimp", "meat", "beef",
        "pork", "chicken", "egg", "honey", "vanilla", "cinnamon",
        # ── Metals & Mining ──
        "steel", "iron", "copper", "aluminium", "aluminum",
        "zinc", "nickel", "lead", "tin", "titanium", "chrome",
        "manganese", "cobalt", "molybdenum", "tungsten",
        "gold", "silver", "platinum", "palladium",
        "metal", "metals", "alloy", "alloys", "ore", "mining",
        "mineral", "minerals", "foundry", "smelter", "slag",
        "scrap", "ferrous", "nonferrous",
        # ── Energy & Fuel ──
        "petroleum", "petrol", "diesel", "gasoline", "crude",
        "oil", "fuel", "gas", "coal", "coke", "lng", "lpg",
        "bitumen", "lubricant", "kerosene", "ethanol",
        "biodiesel", "biofuel", "solar", "energy",
        # ── Chemicals & Plastics ──
        "chemical", "chemicals", "petrochemical", "polymer",
        "plastic", "plastics", "resin", "resins", "solvent",
        "acid", "alkali", "dye", "dyes", "pigment", "pigments",
        "adhesive", "coating", "paint", "pharma", "pharmaceutical",
        # ── Textiles & Fibers ──
        "textile", "textiles", "fabric", "yarn", "fiber", "fibre",
        "garment", "apparel", "silk", "wool", "linen", "denim",
        "leather", "hide", "tanning",
        # ── Wood & Paper ──
        "timber", "lumber", "wood", "plywood", "veneer",
        "pulp", "paper", "cardboard", "packaging",
        # ── Construction Materials ──
        "cement", "concrete", "brick", "tile", "marble",
        "granite", "sand", "gravel", "glass", "ceramic",
    },
}

# Build a flat set of ALL keywords for the substring scanner
_ALL_KEYWORDS = set()
for _kws in KEYWORDS.values():
    _ALL_KEYWORDS.update(_kws)

# Precompile TLD stripper — handles .co.in, .com.au, .co.uk BEFORE .com, .co etc.
TLD_RE = re.compile(
    r'\.(co\.in|co\.uk|com\.au|com\.br|co\.za|co\.nz|co\.jp'
    r'|com|net|org|biz|info|io|ai|tech|dev|co'
    r'|in|us|uk|au|ca|de|fr|jp|br|ru|cn|nz|eu)$',
    re.IGNORECASE
)

TOKEN_RE = re.compile(r'[^a-z0-9]+')


def tokenize(domain: str) -> set:
    """
    Convert domain to lowercase tokens + substring keyword extraction.
    agri-exports.co.in  → {"agri", "exports", "export"}
    freightlogistics.com → {"freightlogistics", "freight", "logistic", "logistics"}
    """
    d = domain.lower().strip()
    d = TLD_RE.sub("", d)
    if d.startswith("www."):
        d = d[4:]

    # Split on separators (dots, hyphens, underscores, digits-letters boundary)
    raw_tokens = set(TOKEN_RE.split(d))

    # Substring scan: find keywords embedded inside concatenated tokens
    expanded = set(raw_tokens)
    for token in raw_tokens:
        if len(token) > 5:
            for kw in _ALL_KEYWORDS:
                if len(kw) >= 3 and kw in token and kw != token:
                    expanded.add(kw)

    return {t for t in expanded if len(t) >= 3}


def categorize(domain: str):
    """Returns the best matching category or None."""
    tokens = tokenize(domain)
    if not tokens:
        return None

    scores = {}
    for cat, kw_set in KEYWORDS.items():
        scores[cat] = len(tokens & kw_set)

    best = max(scores.values())
    if best == 0:
        return None

    # Priority order for tie-breaking: commodity > exim > logistics
    for cat in ("commodity", "exim", "logistics"):
        if scores[cat] == best:
            return cat

    return None


# ── Worker (runs in child process) ─────────────────────

def categorize_batch(domains):
    """Process a list of domain strings. Returns list of (category, domain)."""
    return [(categorize(d), d) for d in domains]


# ── DB helpers ─────────────────────────────────────────

def init_columns(db_path):
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    existing = {r[1] for r in conn.execute("PRAGMA table_info(domains)")}
    if "category" not in existing:
        conn.execute("ALTER TABLE domains ADD COLUMN category TEXT")
        log.info("Added column: category")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_category ON domains(category)")
    conn.commit()
    conn.close()


def get_resume_rowid(db_path):
    """Find the first ROWID where category IS NULL (resume point)."""
    conn = sqlite3.connect(db_path)
    row = conn.execute("SELECT MIN(rowid) FROM domains WHERE category IS NULL").fetchone()
    conn.close()
    return row[0] if row and row[0] else None


def fetch_batch(db_path, start_rowid, limit):
    """Fetch domains using ROWID range — O(1) seek, no OFFSET scan."""
    conn = sqlite3.connect(db_path)
    rows = conn.execute(
        "SELECT rowid, domain FROM domains WHERE rowid >= ? AND category IS NULL LIMIT ?",
        (start_rowid, limit)
    ).fetchall()
    conn.close()
    return rows


def write_results(db_path, results):
    conn = sqlite3.connect(db_path, timeout=60)
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.executemany(
        "UPDATE domains SET category=? WHERE domain=?",
        results
    )
    conn.commit()
    conn.close()


def print_stats(db_path):
    conn = sqlite3.connect(db_path)
    total = conn.execute("SELECT COUNT(*) FROM domains").fetchone()[0]
    cats  = conn.execute("""
        SELECT COALESCE(category, 'uncategorized'), COUNT(*)
        FROM domains
        GROUP BY category
        ORDER BY COUNT(*) DESC
    """).fetchall()
    conn.close()

    log.info(f"\n{'='*45}")
    log.info(f"  Total domains : {total:>12,}")
    log.info(f"{'-'*45}")
    for cat, count in cats:
        pct = count * 100.0 / total if total else 0
        log.info(f"    {cat:<20} {count:>10,}  ({pct:.1f}%)")
    log.info(f"{'='*45}\n")


# ── Main ───────────────────────────────────────────────

def main():
    _setup_logging()
    log.info("Starting domain categorization")
    init_columns(DB_PATH)

    start_rowid = get_resume_rowid(DB_PATH)
    if start_rowid is None:
        log.info("All domains already categorized.")
        print_stats(DB_PATH)
        return

    total_pending = sqlite3.connect(DB_PATH).execute(
        "SELECT COUNT(*) FROM domains WHERE category IS NULL"
    ).fetchone()[0]
    log.info(f"Domains to categorize: {total_pending:,} | Workers: {WORKERS}")

    pool = mp.Pool(processes=WORKERS)
    total_done = 0
    total_matched = 0
    t_start = time.time()
    current_rowid = start_rowid

    while True:
        rows = fetch_batch(DB_PATH, current_rowid, BATCH_SIZE)
        if not rows:
            break

        domains = [d for _, d in rows]
        max_rowid = rows[-1][0]

        # Split across workers
        chunk_size = max(1, len(domains) // WORKERS)
        chunks = [domains[i:i+chunk_size] for i in range(0, len(domains), chunk_size)]

        # Parallel categorization (pure CPU, no IO)
        all_results = []
        for result in pool.map(categorize_batch, chunks):
            all_results.extend(result)

        # Write back
        write_results(DB_PATH, all_results)

        matched = sum(1 for r in all_results if r[0] is not None)
        total_done += len(rows)
        total_matched += matched
        current_rowid = max_rowid + 1

        # Progress
        elapsed = time.time() - t_start
        rate = total_done / elapsed if elapsed > 0 else 0
        remaining = total_pending - total_done
        eta = remaining / rate / 60 if rate > 0 else 0

        log.info(
            f"Done: {total_done:,}/{total_pending:,} | "
            f"Matched: {matched:,} (total {total_matched:,}) | "
            f"Rate: {rate:,.0f}/sec | ETA: {eta:.1f}m"
        )

    pool.close()
    pool.join()

    log.info("Categorization complete!")
    print_stats(DB_PATH)


if __name__ == "__main__":
    main()