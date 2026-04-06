"""
DB connection pool (asyncpg) and safe query execution for PostgreSQL.
Includes lead discovery with quality scoring and no-repeat logic.
"""
import asyncpg
import re
import logging
from typing import Optional
from config import settings

log = logging.getLogger("db")

_pool: asyncpg.Pool | None = None
_fts_ready: bool = False   # True once GIN index on search_vector is confirmed

FORBIDDEN_KEYWORDS = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|REPLACE|TRUNCATE|ATTACH|DETACH)\b",
    re.IGNORECASE,
)
SELECT_ONLY = re.compile(r"^\s*SELECT\b", re.IGNORECASE)


async def init_pool():
    global _pool, _fts_ready
    _pool = await asyncpg.create_pool(
        host=settings.PG_HOST, port=settings.PG_PORT,
        database=settings.PG_DB, user=settings.PG_USER,
        password=settings.PG_PASSWORD,
        min_size=2, max_size=10,
        command_timeout=60,   # raised from 30 — discovery query with subquery JOINs needs more time
    )
    # Check if the GIN full-text index is ready — determines which search strategy to use
    async with _pool.acquire() as conn:
        exists = await conn.fetchval("""
            SELECT EXISTS (
                SELECT 1 FROM pg_indexes
                WHERE tablename = 'domain_metadata'
                AND indexname   = 'idx_meta_search_vector'
            )
        """)
        _fts_ready = bool(exists)
    log.info(f"FTS index ready: {_fts_ready} (search_vector GIN index {'found' if _fts_ready else 'NOT found — using ILIKE fallback'})")


async def close_pool():
    global _pool
    if _pool:
        await _pool.close()


def get_pool() -> asyncpg.Pool:
    if not _pool:
        raise RuntimeError("DB pool not initialized")
    return _pool


# ── Safe AI query execution ───────────────────────────────────────────────

def validate_sql(sql: str) -> str:
    sql = sql.strip().rstrip(";")
    if not SELECT_ONLY.match(sql):
        raise ValueError("Only SELECT statements are allowed")
    if FORBIDDEN_KEYWORDS.search(sql):
        raise ValueError("Query contains forbidden SQL keywords")
    lower = sql.lower()
    if "pg_catalog" in lower or "information_schema" in lower:
        raise ValueError("Access to system tables is not allowed")
    return sql


async def run_query_safe(sql: str, max_rows: int | None = None):
    """Validate and execute AI-generated SQL. Returns (rows_as_dicts, column_names)."""
    max_rows = max_rows or settings.MAX_SQL_RESULTS
    sql = validate_sql(sql)

    # Enforce LIMIT
    limit_match = re.search(r"\bLIMIT\s+(\d+)", sql, re.IGNORECASE)
    if limit_match:
        if int(limit_match.group(1)) > max_rows:
            sql = re.sub(r"\bLIMIT\s+\d+", f"LIMIT {max_rows}", sql, flags=re.IGNORECASE)
    else:
        sql = f"{sql} LIMIT {max_rows}"

    log.info(f"Executing SQL: {sql[:200]}")
    async with get_pool().acquire() as conn:
        rows = await conn.fetch(sql)

    if not rows:
        return [], []
    columns = list(rows[0].keys())
    return [dict(r) for r in rows], columns


# ── Lead Discovery — core new function ───────────────────────────────────




async def discover_leads(
    keyword: Optional[str] = None,
    category: Optional[list] = None,
    country: Optional[str] = None,
    exclude_country: Optional[str] = None,
    contact_type: Optional[list] = None,
    has_employees: Optional[bool] = None,
    excluded_domains: Optional[list] = None,
    batch_size: int = 10,
) -> dict:
    """
    Returns enriched lead cards sorted by quality score,
    excluding already-seen domains.
    """
    pool = get_pool()
    params: list = []
    conditions = ["dm.status_code = 200", "dm.quality_score > 0"]

    def p():
        return f"${len(params)}"

    # ── Filters ──
    if category:
        params.append(category)
        conditions.append(f"dm.category = ANY({p()})")

    if country:
        params.append(f"%{country}%")
        conditions.append(f"dm.location ILIKE {p()}")

    # Negative location filter — "apart from India", "excluding US"
    if exclude_country:
        params.append(f"%{exclude_country}%")
        conditions.append(f"(dm.location IS NULL OR dm.location NOT ILIKE {p()})")

    if keyword:
        # Strategy depends on whether the GIN index is built yet.
        # With GIN index: use websearch_to_tsquery (fast, OR-aware ranked FTS)
        # Without GIN index: ILIKE on title+domain only for the FIRST keyword.
        #   - Scanning description (long text) on 2.75M rows without index = timeout
        #   - title is shorter; combined with domain gives enough signal
        kw_parts = [w.strip() for w in keyword.replace(" OR ", "|").split("|") if w.strip()]

        if _fts_ready:
            params.append(keyword)     # websearch will handle OR natively
            kp = p()
            ilike_clauses = []
            for kw in kw_parts[:3]:
                params.append(f"%{kw}%")
                lp = p()
                ilike_clauses.append(f"(dm.title ILIKE {lp} OR dm.domain ILIKE {lp})")
            ilike_str = " OR ".join(ilike_clauses) if ilike_clauses else "TRUE"
            conditions.append(
                f"""(
                    dm.search_vector @@ websearch_to_tsquery('english', {kp})
                    OR {ilike_str}
                )"""
            )
            log.debug(f"Keyword search: FTS path (index ready), kw={keyword!r}")
        else:
            # No GIN index yet — use ILIKE on title and domain only (fast columns).
            # Only use the FIRST keyword to avoid excessive seqscans; title is usually enough.
            primary_kw = kw_parts[0] if kw_parts else keyword
            params.append(f"%{primary_kw}%")
            lp = p()
            # Also check domain for exact domain-level hits
            ilike_clauses = [f"dm.title ILIKE {lp}", f"dm.domain ILIKE {lp}"]
            # Secondary keyword if present — also on title only
            if len(kw_parts) > 1:
                params.append(f"%{kw_parts[1]}%")
                lp2 = p()
                ilike_clauses.append(f"dm.title ILIKE {lp2}")
            conditions.append("(" + " OR ".join(ilike_clauses) + ")")
            log.info(f"Keyword search: ILIKE fallback (GIN index not ready), primary_kw={primary_kw!r}")

    if contact_type:
        params.append(contact_type)
        conditions.append(
            f"EXISTS (SELECT 1 FROM domain_contacts dc WHERE dc.domain = dm.domain AND dc.entity_type = ANY({p()}))"
        )

    if has_employees is True:
        conditions.append(
            "EXISTS (SELECT 1 FROM domain_employees de WHERE de.domain = dm.domain)"
        )

    # Exclude already-seen domains
    excluded = (excluded_domains or [])[:settings.MAX_EXCLUDED_DOMAINS]
    if excluded:
        params.append(excluded)
        conditions.append(f"dm.domain != ALL({p()})")

    where = "WHERE " + " AND ".join(conditions)

    # Count (without batch limit)
    # Count (with cap to prevent full seq-scans on millions of rows)
    count_sql = f"""
        SELECT count(*)::int FROM (
            SELECT 1
            FROM domain_metadata dm
            {where}
            LIMIT 5000
        ) sub
    """

    # Main discovery query leveraging denormalized columns
    # We no longer need CTEs or JOINs, bringing query time to <50ms
    data_sql = f"""
        SELECT
            dm.domain,
            dm.title,
            dm.description,
            dm.category,
            dm.location,
            dm.email_count,
            dm.phone_count,
            dm.linkedin_count,
            dm.social_count,
            dm.emp_count,
            dm.quality_score
        FROM domain_metadata dm
        {where}
        ORDER BY dm.quality_score DESC, dm.domain
        LIMIT ${len(params)+1}
    """

    async with pool.acquire() as conn:
        total = await conn.fetchval(count_sql, *params)
        rows = await conn.fetch(data_sql, *(params + [batch_size]))

    cards = []
    if rows:
        domains_in_batch = [r["domain"] for r in rows]
        # Fetch top 3 contacts per domain in one query
        contacts_sql = """
            SELECT domain, entity_type, value, label
            FROM domain_contacts
            WHERE domain = ANY($1) AND entity_type IN ('email','phone','linkedin','facebook','instagram','twitter','youtube','whatsapp')
            ORDER BY domain, entity_type, id
        """
        async with pool.acquire() as conn:
            all_contacts = await conn.fetch(contacts_sql, domains_in_batch)

        # Group contacts by domain
        contacts_by_domain: dict = {}
        for c in all_contacts:
            d = c["domain"]
            if d not in contacts_by_domain:
                contacts_by_domain[d] = []
            if len(contacts_by_domain[d]) < 30:
                contacts_by_domain[d].append({
                    "entity_type": c["entity_type"],
                    "value": c["value"],
                    "label": c["label"],
                })

        for r in rows:
            d = r["domain"]
            cards.append({
                "domain":         d,
                "title":          r["title"],
                "description":    r["description"],
                "category":       r["category"],
                "location":       r["location"],
                "quality_score":  r["quality_score"],
                "email_count":    r["email_count"],
                "phone_count":    r["phone_count"],
                "linkedin_count": r["linkedin_count"],
                "social_count":   r["social_count"],
                "has_employees":  r["emp_count"] > 0,
                "top_contacts":   contacts_by_domain.get(d, []),
            })

    return {
        "cards":          cards,
        "total_matched":  total or 0,
        "has_more":       (total or 0) > len(excluded) + batch_size,
        "batch_returned": len(cards),
    }


# ── Domain helpers ────────────────────────────────────────────────────────

async def get_company_profile(domain: str) -> dict | None:
    pool = get_pool()
    async with pool.acquire() as conn:
        meta = await conn.fetchrow("""
            SELECT dm.domain, dm.category, dm.title, dm.description,
                   dm.language, dm.location, dm.redirected_url,
                   dm.status_code, cs.contacts_found, cs.crawled_at
            FROM domain_metadata dm
            LEFT JOIN domain_crawl_status cs ON cs.domain = dm.domain
            WHERE dm.domain = $1
        """, domain)
        if not meta:
            return None

        contacts = await conn.fetch("""
            SELECT entity_type, value, label, context, page_url
            FROM domain_contacts WHERE domain = $1
            ORDER BY entity_type, id
        """, domain)

        employees = await conn.fetch("""
            SELECT name, title, email, phone, linkedin
            FROM domain_employees WHERE domain = $1
            ORDER BY id
        """, domain)

    return {
        **dict(meta),
        "quality_score": None,   # computed on demand if needed
        "crawled_at": str(meta["crawled_at"]) if meta["crawled_at"] else None,
        "contacts": [dict(c) for c in contacts],
        "employees": [dict(e) for e in employees],
    }


async def get_stats() -> dict:
    pool = get_pool()
    async with pool.acquire() as conn:
        total      = await conn.fetchval("SELECT COUNT(*) FROM domain_metadata")
        active     = await conn.fetchval("SELECT COUNT(*) FROM domain_metadata WHERE status_code = 200")
        with_addr  = await conn.fetchval("SELECT COUNT(*) FROM domain_metadata WHERE location IS NOT NULL")
        with_cont  = await conn.fetchval("SELECT COUNT(DISTINCT domain) FROM domain_contacts")
        total_cont = await conn.fetchval("SELECT COUNT(*) FROM domain_contacts")

        by_cat  = {r["category"]: r["cnt"] for r in await conn.fetch(
            "SELECT category, COUNT(*) cnt FROM domain_metadata GROUP BY category ORDER BY cnt DESC"
        )}
        by_type = {r["entity_type"]: r["cnt"] for r in await conn.fetch(
            "SELECT entity_type, COUNT(*) cnt FROM domain_contacts GROUP BY entity_type ORDER BY cnt DESC"
        )}
        by_lang = {r["language"]: r["cnt"] for r in await conn.fetch(
            """SELECT language, COUNT(*) cnt FROM domain_metadata
               WHERE status_code=200 AND language IS NOT NULL
               GROUP BY language ORDER BY cnt DESC LIMIT 20"""
        )}

    return dict(
        total_domains=total, active_domains=active,
        domains_with_contacts=with_cont, domains_with_address=with_addr,
        total_contacts=total_cont,
        by_category=by_cat, by_contact_type=by_type, by_language=by_lang,
    )
