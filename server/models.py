"""
Request / Response models for the TradeLeads Data Provider Platform API.
"""
from pydantic import BaseModel, Field
from typing import Optional, List, Dict, Any


# ── Shared ─────────────────────────────────────────────────────────────────

class ContactItem(BaseModel):
    entity_type: Optional[str] = None   # email | phone | linkedin | youtube | facebook | instagram | twitter
    value:       Optional[str] = None
    label:       Optional[str] = None
    context:     Optional[str] = None
    page_url:    Optional[str] = None


class EmployeeItem(BaseModel):
    name:     Optional[str] = None
    title:    Optional[str] = None
    email:    Optional[str] = None
    phone:    Optional[str] = None
    linkedin: Optional[str] = None


class CompanyProfile(BaseModel):
    """Full enriched profile for a single domain."""
    domain:          Optional[str] = None
    category:        Optional[str] = None
    title:           Optional[str] = None
    description:     Optional[str] = None
    language:        Optional[str] = None
    location:        Optional[str] = None
    redirected_url:  Optional[str] = None
    status_code:     Optional[int] = None
    quality_score:   Optional[int] = None
    contacts_found:  Optional[int] = None
    crawled_at:      Optional[str] = None
    contacts:        List[ContactItem] = []
    employees:       List[EmployeeItem] = []


# ── Lead Discovery (new swipable feed) ────────────────────────────────────

class DiscoverRequest(BaseModel):
    keyword:          Optional[str]       = Field(None, description="Full-text keyword search across title/description/domain")
    category:         Optional[List[str]] = Field(None, description="logistics | exim | commodity")
    country:          Optional[str]       = Field(None, description="Country name e.g. 'India', 'UAE'")
    contact_type:     Optional[List[str]] = Field(None, description="email | phone | linkedin | facebook | instagram | twitter | whatsapp")
    has_employees:    Optional[bool]      = Field(None, description="Only companies with named employee data")
    excluded_domains: List[str]           = Field(default_factory=list, description="Domains already seen — will not be returned")
    batch_size:       int                 = Field(10, ge=1, le=50, description="Cards per batch")


class TopContact(BaseModel):
    entity_type: str
    value:       str
    label:       Optional[str] = None


class DiscoverCard(BaseModel):
    """Enriched company card for the discovery feed."""
    domain:        str
    title:         Optional[str] = None
    description:   Optional[str] = None
    category:      Optional[str] = None
    location:      Optional[str] = None
    quality_score: int            = 0
    email_count:   int            = 0
    phone_count:   int            = 0
    linkedin_count: int           = 0
    social_count:  int            = 0
    has_employees: bool           = False
    top_contacts:  List[TopContact] = []


class SearchInsights(BaseModel):
    summary:           str
    total_matched:     int
    top_categories:    Dict[str, int] = {}
    top_locations:     Dict[str, int] = {}
    contact_breakdown: Dict[str, int] = {}


class DiscoverResponse(BaseModel):
    cards:         List[DiscoverCard]
    insights:      Optional[SearchInsights] = None
    total_matched: int
    has_more:      bool
    batch_returned: int


# ── Natural Language Query ─────────────────────────────────────────────────

class NLQueryRequest(BaseModel):
    query: str = Field(..., min_length=3, max_length=500,
                       description="Natural language query",
                       examples=["show me active logistics companies in India with email contacts"])
    limit: Optional[int] = Field(200, ge=1, le=500)


class NLQueryResponse(BaseModel):
    query:          str
    insights:       Optional[SearchInsights] = None
    results:        List[Dict[str, Any]]
    total_returned: int
    elapsed_ms:     int


# ── Lead Search ────────────────────────────────────────────────────────────

class LeadSearchRequest(BaseModel):
    category:      Optional[List[str]] = Field(None, description="logistics | exim | commodity")
    contact_type:  Optional[List[str]] = Field(None, description="email | phone | linkedin | youtube | instagram | facebook")
    country:       Optional[str]       = Field(None, description="Country name or code, e.g. 'India' or 'IN'")
    has_address:   Optional[bool]      = Field(None, description="Only companies with a physical address")
    has_employees: Optional[bool]      = Field(None, description="Only companies with employee data")
    keyword:       Optional[str]       = Field(None, description="Search in title/description/domain")
    limit:         Optional[int]       = Field(100, ge=1, le=500)
    offset:        Optional[int]       = Field(0, ge=0)


class LeadResult(BaseModel):
    domain:        Optional[str] = None
    category:      Optional[str] = None
    title:         Optional[str] = None
    location:      Optional[str] = None
    contact_type:  Optional[str] = None
    contact_value: Optional[str] = None
    contact_label: Optional[str] = None


class LeadSearchResponse(BaseModel):
    results:        List[LeadResult]
    total_matched:  int
    total_returned: int
    offset:         int
    elapsed_ms:     int


# ── Filter Query ───────────────────────────────────────────────────────────

class FilterQueryRequest(BaseModel):
    category:             Optional[List[str]] = None
    status_code:          Optional[List[int]] = None
    language:             Optional[List[str]] = None
    location_contains:    Optional[str]       = None
    title_contains:       Optional[str]       = None
    description_contains: Optional[str]       = None
    domain_contains:      Optional[str]       = None
    has_contacts:         Optional[bool]      = None
    limit:                Optional[int]       = Field(100, ge=1, le=500)
    offset:               Optional[int]       = Field(0, ge=0)


class FilterQueryResponse(BaseModel):
    results:        List[Dict[str, Any]]
    total_matched:  int
    total_returned: int
    offset:         int
    elapsed_ms:     int


# ── Stats ──────────────────────────────────────────────────────────────────

class StatsResponse(BaseModel):
    total_domains:         int
    active_domains:        int
    domains_with_contacts: int
    domains_with_address:  int
    total_contacts:        int
    by_category:           Dict[str, int]
    by_contact_type:       Dict[str, int]
    by_language:           Dict[str, int]
