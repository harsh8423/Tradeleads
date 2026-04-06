"""
AI Agent — Intent extractor and insight generator for TradeLeads.
Converts natural language to structured search intent, then generates
human-readable insights from search results.
"""
import logging
import json
import re
from dataclasses import dataclass, field
from typing import Optional, List, Dict
from openai import AsyncOpenAI
from config import settings

log = logging.getLogger("ai_agent")
client = AsyncOpenAI(api_key=settings.OPENAI_API_KEY)


# ── Intent Extraction ─────────────────────────────────────────────────────

INTENT_SYSTEM_PROMPT = """
You are a B2B lead search assistant for a trade & logistics platform.
Convert the user's natural language query into structured search intent.

Respond ONLY with valid JSON (no markdown, no code fences):
{
  "keywords": ["word1", "word2"],
  "category": null or one of ["logistics", "exim", "commodity"],
  "country": null or country name to INCLUDE (positive filter),
  "exclude_country": null or country name to EXCLUDE (for queries like "apart from X", "excluding X", "not in X"),
  "contact_type": null or array of: ["email", "phone", "linkedin", "facebook", "instagram", "whatsapp"],
  "has_employees": null or boolean,
  "intent_summary": "one sentence describing what the user is looking for"
}

CRITICAL RULES:
- keywords: 2-3 SHORT SINGLE WORDS only (e.g. ["rice", "importer"] NOT ["rice importers", "grain importers"])
- category: ONLY set if user explicitly says "logistics", "exim", "commodity". For food/agriculture queries, leave null.
- country: only for POSITIVE location filter ("in India", "from UAE")
- exclude_country: for NEGATIVE location filter ("apart from India", "excluding US", "not in China")
- contact_type: only set if user explicitly asks for it
- Be LOOSE — prefer fewer filters over many strict ones
"""


@dataclass
class SearchIntent:
    keywords: List[str] = field(default_factory=list)
    category: Optional[str] = None
    country: Optional[str] = None
    exclude_country: Optional[str] = None
    contact_type: Optional[List[str]] = None
    has_employees: Optional[bool] = None
    intent_summary: str = ""
    keyword_string: str = ""  # OR-joined for websearch_to_tsquery


async def extract_intent(query: str) -> SearchIntent:
    """Convert natural language query to structured search intent."""
    log.info(f"Extracting intent from: {query!r}")
    response = await client.chat.completions.create(
        model=settings.OPENAI_MODEL,
        messages=[
            {"role": "system", "content": INTENT_SYSTEM_PROMPT},
            {"role": "user", "content": query},
        ],
        response_format={"type": "json_object"},
        max_completion_tokens=500,
    )
    raw = response.choices[0].message.content.strip()
    log.info(f"Intent raw: {raw}")

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        parsed = json.loads(m.group()) if m else {}

    keywords = [k.strip() for k in (parsed.get("keywords") or []) if k.strip()]

    # Build OR-based websearch query: "rice OR importer OR grain"
    # This means ANY keyword match is enough — much looser than AND
    keyword_string = " OR ".join(keywords) if keywords else ""

    log.info(f"Resolved intent → keywords={keywords}, kw_string={keyword_string!r}, "
             f"category={parsed.get('category')}, country={parsed.get('country')}, "
             f"exclude_country={parsed.get('exclude_country')}")

    return SearchIntent(
        keywords=keywords,
        keyword_string=keyword_string,
        category=parsed.get("category"),
        country=parsed.get("country"),
        exclude_country=parsed.get("exclude_country"),
        contact_type=parsed.get("contact_type"),
        has_employees=parsed.get("has_employees"),
        intent_summary=parsed.get("intent_summary", query),
    )


# ── Insight Generation ────────────────────────────────────────────────────

INSIGHT_SYSTEM_PROMPT = """
You are a B2B data analyst. Given a list of company search results, generate a concise, 
useful insight summary for a lead researcher.

Respond ONLY with valid JSON (no markdown, no code fences):
{
  "summary": "2-3 sentence insight about the results",
  "top_categories": {"category_name": count, ...},
  "top_locations": {"location_name": count, ...},
  "contact_breakdown": {"email": count, "phone": count, "linkedin": count}
}

Focus on what's actionable for a salesperson. Be specific about numbers and geographies.
"""


async def generate_insights(query: str, cards: list, total_matched: int) -> dict:
    """Generate AI-powered search insights from result cards."""
    if not cards:
        return {
            "summary": f"No companies found matching '{query}'. Try broader keywords or different filters.",
            "top_categories": {},
            "top_locations": {},
            "contact_breakdown": {},
        }

    # Build a compact summary of results for the LLM
    sample = cards[:20]  # use up to 20 cards for insight generation
    categories: Dict[str, int] = {}
    locations: Dict[str, int] = {}
    emails = phones = linkedins = 0

    for c in sample:
        cat = c.get("category") or "unknown"
        categories[cat] = categories.get(cat, 0) + 1
        loc = (c.get("location") or "").strip()
        if loc:
            # Extract first meaningful location token
            loc_key = loc.split(",")[0].strip()[:30]
            if loc_key:
                locations[loc_key] = locations.get(loc_key, 0) + 1
        emails += c.get("email_count", 0)
        phones += c.get("phone_count", 0)
        linkedins += c.get("linkedin_count", 0)

    results_summary = {
        "query": query,
        "total_matched": total_matched,
        "sample_size": len(sample),
        "categories": categories,
        "locations": locations,
        "contact_counts": {"email": emails, "phone": phones, "linkedin": linkedins},
    }

    try:
        response = await client.chat.completions.create(
            model=settings.OPENAI_MODEL,
            messages=[
                {"role": "system", "content": INSIGHT_SYSTEM_PROMPT},
                {"role": "user", "content": json.dumps(results_summary)},
            ],
            response_format={"type": "json_object"},
            max_completion_tokens=400,
        )
        raw = response.choices[0].message.content.strip()
        parsed = json.loads(raw)
        return {
            "summary": parsed.get("summary", ""),
            "top_categories": parsed.get("top_categories", categories),
            "top_locations": parsed.get("top_locations", locations),
            "contact_breakdown": parsed.get("contact_breakdown", {}),
        }
    except Exception as e:
        log.warning(f"Insight generation failed: {e}")
        return {
            "summary": f"Found {total_matched:,} companies matching '{query}'.",
            "top_categories": dict(sorted(categories.items(), key=lambda x: -x[1])[:5]),
            "top_locations": dict(sorted(locations.items(), key=lambda x: -x[1])[:5]),
            "contact_breakdown": {"email": emails, "phone": phones, "linkedin": linkedins},
        }
