"""
TradeLeads.io — B2B Lead Discovery API
FastAPI server backed by PostgreSQL (Cloud SQL)
"""
import time
import logging
import io
import csv
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, HTMLResponse
from pathlib import Path

from config import settings
from models import (
    NLQueryRequest, NLQueryResponse, SearchInsights,
    LeadSearchRequest, LeadSearchResponse, LeadResult,
    DiscoverRequest, DiscoverResponse, DiscoverCard, TopContact,
    FilterQueryRequest, FilterQueryResponse,
    CompanyProfile, ContactItem, EmployeeItem, StatsResponse,
)
from db import init_pool, close_pool, get_pool, run_query_safe, get_company_profile, get_stats, discover_leads
from ai_agent import extract_intent, generate_insights

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("server")


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_pool()
    pool = get_pool()
    async with pool.acquire() as conn:
        count = await conn.fetchval("SELECT COUNT(*) FROM domain_metadata")
        log.info(f"DB ready — {count:,} rows in domain_metadata")
    yield
    await close_pool()
    log.info("Server shut down")


app = FastAPI(
    title="TradeLeads.io API",
    description="B2B lead discovery for trade, logistics and commodity sectors",
    version="3.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Health ─────────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    try:
        async with get_pool().acquire() as conn:
            await conn.fetchval("SELECT 1")
        return {"status": "ok", "db": "connected"}
    except Exception as e:
        raise HTTPException(status_code=503, detail=str(e))


# ── Stats ──────────────────────────────────────────────────────────────────

@app.get("/stats", response_model=StatsResponse)
async def stats_endpoint():
    """Platform-level statistics."""
    data = await get_stats()
    return StatsResponse(**data)


# ── Company profile ────────────────────────────────────────────────────────

@app.get("/domain/{domain}", response_model=CompanyProfile)
async def company_profile(domain: str):
    """Full enriched profile: metadata + all contacts + employees."""
    profile = await get_company_profile(domain.lower().strip())
    if not profile:
        raise HTTPException(status_code=404, detail=f"Domain '{domain}' not found")

    return CompanyProfile(
        **{k: v for k, v in profile.items() if k not in ("contacts", "employees")},
        contacts=[ContactItem(**c) for c in profile["contacts"]],
        employees=[EmployeeItem(**e) for e in profile["employees"]],
    )


# ── Lead Discovery (new — swipable feed) ──────────────────────────────────

@app.post("/leads/discover", response_model=DiscoverResponse)
async def lead_discover(req: DiscoverRequest):
    """
    Core discovery endpoint: returns a batch of quality-scored lead cards,
    excluding any domains the user has already seen.
    Supports AI-powered keyword expansion and natural language filtering.
    """
    t0 = time.monotonic()

    result = await discover_leads(
        keyword=req.keyword,
        category=req.category,
        country=req.country,
        contact_type=req.contact_type,
        has_employees=req.has_employees,
        excluded_domains=req.excluded_domains,
        batch_size=req.batch_size,
    )

    cards = [
        DiscoverCard(
            **{k: v for k, v in c.items() if k != "top_contacts"},
            top_contacts=[TopContact(**tc) for tc in c.get("top_contacts", [])],
        )
        for c in result["cards"]
    ]

    elapsed_ms = int((time.monotonic() - t0) * 1000)
    log.info(f"Discover: {len(cards)} cards in {elapsed_ms}ms, {result['total_matched']} total")

    return DiscoverResponse(
        cards=cards,
        total_matched=result["total_matched"],
        has_more=result["has_more"],
        batch_returned=result["batch_returned"],
    )


@app.post("/leads/discover/ai", response_model=DiscoverResponse)
async def lead_discover_ai(req: NLQueryRequest):
    """
    AI-powered discovery: extracts intent from natural language,
    runs discovery, and returns results with AI-generated insights.
    """
    t0 = time.monotonic()

    # Retry loop: if 0 results, ask AI to broaden the search
    retry_query = req.query
    for attempt in range(3):
        # Step 1: Extract structured intent
        try:
            intent = await extract_intent(retry_query)
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Intent extraction failed: {e}")

        # Step 2: Run discovery with extracted parameters
        # NOTE: We do NOT pass category from AI intent — it's too strict and causes
        # zero results when the AI misclassifies. Keywords are broad enough.
        result = await discover_leads(
            keyword=intent.keyword_string or None,
            category=None,
            country=intent.country,
            exclude_country=intent.exclude_country,
            contact_type=intent.contact_type,
            has_employees=intent.has_employees,
            excluded_domains=[],
            batch_size=req.limit or settings.DEFAULT_BATCH_SIZE,
        )

        if result["total_matched"] > 0 or attempt == 2:
            break
            
        log.warning(f"0 results found on attempt {attempt+1}. Retrying with broader query...")
        retry_query = (
            f"The previous search for '{req.query}' returned 0 results. "
            f"Please make the search parameters MUCH broader and LESS STRICT. "
            f"Use fewer, more generic keywords. Do not use {intent.country} limit if it's too restrictive."
        )

    # Step 3: Populate insights from intent summary (removed generate_insights to optimize latency)
    insights = SearchInsights(
        summary=intent.intent_summary,
        total_matched=result["total_matched"],
    )

    cards = [
        DiscoverCard(
            **{k: v for k, v in c.items() if k != "top_contacts"},
            top_contacts=[TopContact(**tc) for tc in c.get("top_contacts", [])],
        )
        for c in result["cards"]
    ]

    elapsed_ms = int((time.monotonic() - t0) * 1000)
    log.info(f"AI Discover: {len(cards)} cards in {elapsed_ms}ms")

    return DiscoverResponse(
        cards=cards,
        insights=insights,
        total_matched=result["total_matched"],
        has_more=result["has_more"],
        batch_returned=result["batch_returned"],
    )


# ── Lead Search (legacy — kept for compatibility) ────────────────────────

@app.post("/leads/search", response_model=LeadSearchResponse)
async def lead_search(req: LeadSearchRequest):
    """Search for B2B leads filtered by category, contact type, country, keyword."""
    t0 = time.monotonic()
    pool = get_pool()

    conditions = ["dm.status_code = 200"]
    params: list = []
    p = lambda: f"${len(params)}"

    if req.category:
        params.append(req.category)
        conditions.append(f"dm.category = ANY({p()})")

    if req.country:
        params.append(f"%{req.country}%")
        idx = p()
        conditions.append(f"dm.location ILIKE {idx}")

    if req.keyword:
        params.append(f"%{req.keyword}%")
        idx = p()
        conditions.append(
            f"(dm.title ILIKE {idx} OR dm.description ILIKE {idx} OR dm.domain ILIKE {idx})"
        )

    contact_join = ""
    if req.contact_type:
        params.append(req.contact_type)
        contact_join = f"JOIN domain_contacts dc ON dc.domain = dm.domain AND dc.entity_type = ANY({p()})"
    else:
        contact_join = "LEFT JOIN domain_contacts dc ON dc.domain = dm.domain"

    if req.has_employees:
        conditions.append(
            "EXISTS (SELECT 1 FROM domain_employees de WHERE de.domain = dm.domain)"
        )

    where = "WHERE " + " AND ".join(conditions)
    limit = min(req.limit or 100, settings.MAX_RESULTS)
    offset = req.offset or 0

    count_sql = f"SELECT COUNT(DISTINCT dm.domain) FROM domain_metadata dm {contact_join} {where}"
    data_sql = f"""
        SELECT dm.domain, dm.category, dm.title, dm.location,
               dc.entity_type AS contact_type, dc.value AS contact_value, dc.label AS contact_label
        FROM domain_metadata dm
        {contact_join}
        {where}
        ORDER BY dm.domain
        LIMIT ${len(params)+1} OFFSET ${len(params)+2}
    """

    async with pool.acquire() as conn:
        total = await conn.fetchval(count_sql, *params)
        rows = await conn.fetch(data_sql, *(params + [limit, offset]))

    elapsed_ms = int((time.monotonic() - t0) * 1000)
    return LeadSearchResponse(
        results=[LeadResult(**dict(r)) for r in rows],
        total_matched=total or 0,
        total_returned=len(rows),
        offset=offset,
        elapsed_ms=elapsed_ms,
    )


# ── Export (CSV) ──────────────────────────────────────────────────────────

@app.post("/leads/export")
async def export_leads(req: LeadSearchRequest):
    """Export lead search results as CSV."""
    req.limit = 500
    result = await lead_search(req)

    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=[
        "domain", "category", "title", "location",
        "contact_type", "contact_value", "contact_label"
    ])
    writer.writeheader()
    for lead in result.results:
        writer.writerow(lead.model_dump())

    output.seek(0)
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=tradeleads_export.csv"},
    )


@app.post("/leads/discover/export")
async def export_discover(req: DiscoverRequest):
    """Export discovered cards as CSV."""
    req.batch_size = 500
    result = await lead_discover(req)

    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=[
        "domain", "title", "category", "location",
        "quality_score", "email_count", "phone_count", "linkedin_count"
    ])
    writer.writeheader()
    for card in result.cards:
        writer.writerow({
            "domain": card.domain,
            "title": card.title or "",
            "category": card.category or "",
            "location": card.location or "",
            "quality_score": card.quality_score,
            "email_count": card.email_count,
            "phone_count": card.phone_count,
            "linkedin_count": card.linkedin_count,
        })

    output.seek(0)
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=tradeleads_discover.csv"},
    )


# ── Filter Query ───────────────────────────────────────────────────────────

@app.post("/query/filter", response_model=FilterQueryResponse)
async def filter_query(req: FilterQueryRequest):
    """Structured filter-based company search."""
    t0 = time.monotonic()
    pool = get_pool()

    conditions = []
    params: list = []
    p = lambda: f"${len(params)}"

    if req.category:
        params.append(req.category)
        conditions.append(f"dm.category = ANY({p()})")
    if req.status_code:
        params.append(req.status_code)
        conditions.append(f"dm.status_code = ANY({p()})")
    if req.language:
        params.append(req.language)
        conditions.append(f"dm.language = ANY({p()})")
    if req.location_contains:
        params.append(f"%{req.location_contains}%")
        conditions.append(f"dm.location ILIKE {p()}")
    if req.title_contains:
        params.append(f"%{req.title_contains}%")
        conditions.append(f"dm.title ILIKE {p()}")
    if req.description_contains:
        params.append(f"%{req.description_contains}%")
        conditions.append(f"dm.description ILIKE {p()}")
    if req.domain_contains:
        params.append(f"%{req.domain_contains}%")
        conditions.append(f"dm.domain ILIKE {p()}")
    if req.has_contacts is True:
        conditions.append("EXISTS (SELECT 1 FROM domain_contacts dc WHERE dc.domain = dm.domain)")
    elif req.has_contacts is False:
        conditions.append("NOT EXISTS (SELECT 1 FROM domain_contacts dc WHERE dc.domain = dm.domain)")

    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    limit = min(req.limit or 100, settings.MAX_RESULTS)
    offset = req.offset or 0

    count_sql = f"SELECT COUNT(*) FROM domain_metadata dm {where}"
    data_sql = f"""
        SELECT dm.domain, dm.category, dm.status_code, dm.title,
               dm.description, dm.language, dm.location,
               dm.redirected_url, dm.fetch_ms, cs.contacts_found, cs.crawled_at
        FROM domain_metadata dm
        LEFT JOIN domain_crawl_status cs ON cs.domain = dm.domain
        {where}
        ORDER BY dm.domain
        LIMIT ${len(params)+1} OFFSET ${len(params)+2}
    """

    async with pool.acquire() as conn:
        total = await conn.fetchval(count_sql, *params)
        rows = await conn.fetch(data_sql, *(params + [limit, offset]))

    elapsed_ms = int((time.monotonic() - t0) * 1000)
    return FilterQueryResponse(
        results=[dict(r) for r in rows],
        total_matched=total or 0,
        total_returned=len(rows),
        offset=offset,
        elapsed_ms=elapsed_ms,
    )


# ── Filter options for UI dropdowns ───────────────────────────────────────

@app.get("/filters/options")
async def filter_options():
    pool = get_pool()
    async with pool.acquire() as conn:
        categories = [r[0] for r in await conn.fetch(
            "SELECT DISTINCT category FROM domain_metadata WHERE category IS NOT NULL ORDER BY category"
        )]
        languages = [r[0] for r in await conn.fetch(
            "SELECT DISTINCT language FROM domain_metadata WHERE language IS NOT NULL AND status_code=200 ORDER BY language LIMIT 50"
        )]
        contact_types = [r[0] for r in await conn.fetch(
            "SELECT entity_type, COUNT(*) cnt FROM domain_contacts GROUP BY entity_type ORDER BY cnt DESC"
        )]
    return {"categories": categories, "languages": languages, "contact_types": contact_types}
