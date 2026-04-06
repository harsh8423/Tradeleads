# get_contacts.py — Contact scraper for domains with status_code=200
# Crawls homepage + contact/about/team pages, extracts emails, phones,
# social links, addresses, and employee details.

import asyncio
import aiohttp
import sqlite3
import time
import logging
import re
import socket
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse
import warnings
from bs4 import BeautifulSoup, MarkupResemblesLocatorWarning
import html as html_module
import phonenumbers
from phonenumbers import (
    PhoneNumberFormat, format_number, is_valid_number, NumberParseException
)
import spacy

warnings.filterwarnings("ignore", module="bs4")
warnings.filterwarnings("ignore", category=MarkupResemblesLocatorWarning)
# ── Config ──────────────────────────────────────────────────────────────────────
DB_PATH              = r"E:\domains.db"
ALLOWED_CATEGORIES   = ["exim", "logistics", "commodity"]

CONCURRENCY          = 300
CONNECT_TIMEOUT      = 8
READ_TIMEOUT         = 15
MAX_REDIRECTS        = 5
MAX_BODY_BYTES       = 2_000_000
MAX_PAGES_PER_DOMAIN = 5
BATCH_SIZE           = 2_000
WRITE_BATCH_SIZE     = 500

# ────────────────────────────────────────────────────────────────────────────────

# ── DROP COMMANDS — run once to reset, then restart ──────────────────────────────
#   sqlite3 "E:\domains.db" "DROP TABLE IF EXISTS domain_contacts; DROP TABLE IF EXISTS domain_employees; DROP TABLE IF EXISTS domain_crawl_status;"
# ────────────────────────────────────────────────────────────────────────────────

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

# URL path scoring — higher = more likely to have contacts
PAGE_PRIORITY = [
    (re.compile(r"contact[_\-]?us",         re.I), 12),
    (re.compile(r"contacts",                re.I), 11),
    (re.compile(r"contact",                 re.I), 10),
    (re.compile(r"reach[_\-]?us",           re.I), 10),
    (re.compile(r"get[_\-]?in[_\-]?touch",  re.I), 10),
    (re.compile(r"enquir",                  re.I), 9),
    (re.compile(r"inquir",                  re.I), 9),
    (re.compile(r"support",                 re.I), 7),
    (re.compile(r"our[_\-]?team",           re.I), 11),
    (re.compile(r"meet[_\-]?the[_\-]?team", re.I), 11),
    (re.compile(r"meet[_\-]?us",            re.I), 10),
    (re.compile(r"team[_\-]?member",        re.I), 10),
    (re.compile(r"\bteam\b",                re.I), 9),
    (re.compile(r"\bstaff\b",               re.I), 9),
    (re.compile(r"\bpeople\b",              re.I), 8),
    (re.compile(r"employee",                re.I), 8),
    (re.compile(r"personnel",               re.I), 8),
    (re.compile(r"expert",                  re.I), 7),
    (re.compile(r"about[_\-]?us",           re.I), 10),
    (re.compile(r"\babout\b",               re.I), 8),
    (re.compile(r"who[_\-]?we[_\-]?are",    re.I), 9),
    (re.compile(r"our[_\-]?story",          re.I), 8),
    (re.compile(r"our[_\-]?company",        re.I), 8),
    (re.compile(r"company[_\-]?profile",    re.I), 8),
    (re.compile(r"management",              re.I), 8),
    (re.compile(r"leadership",              re.I), 8),
    (re.compile(r"\bboard\b",               re.I), 7),
    (re.compile(r"director",                re.I), 7),
    (re.compile(r"executive",               re.I), 7),
    (re.compile(r"founder",                 re.I), 7),
    (re.compile(r"directory",               re.I), 7),
    (re.compile(r"\bprofile\b",             re.I), 6),
    (re.compile(r"\bbio\b",                 re.I), 6),
]

# ── Regex patterns ───────────────────────────────────────────────────────────────

EMAIL_RE = re.compile(
    r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}",
    re.I
)

EMPLOYEE_PAGE_RE = re.compile(
    r"/("
    # ── Team variations ──────────────────────────────────────────
    r"team|our[-_]?team|meet[-_]?the[-_]?team|meet[-_]?our[-_]?team|"
    r"team[-_]?members?|team[-_]?profile|team[-_]?page|"
    r"the[-_]?team|full[-_]?team|global[-_]?team|"

    # ── Staff variations ─────────────────────────────────────────
    r"staff|our[-_]?staff|staff[-_]?members?|staff[-_]?directory|"
    r"staff[-_]?profile|staff[-_]?page|staff[-_]?list|"

    # ── People variations ────────────────────────────────────────
    r"people|our[-_]?people|key[-_]?people|"

    # ── About variations ─────────────────────────────────────────
    r"about|about[-_]?us|about[-_]?the[-_]?company|about[-_]?our[-_]?company|"
    r"about[-_]?the[-_]?team|about[-_]?me|our[-_]?story|"
    r"who[-_]?we[-_]?are|what[-_]?we[-_]?do|our[-_]?mission|"
    r"company[-_]?profile|company[-_]?overview|company[-_]?info|"
    r"company[-_]?information|company[-_]?history|company[-_]?background|"
    r"our[-_]?company|the[-_]?company|"

    # ── Contact variations ───────────────────────────────────────
    r"contact|contacts|contact[-_]?us|contact[-_]?me|"
    r"contact[-_]?info|contact[-_]?information|contact[-_]?details|"
    r"contact[-_]?page|contact[-_]?form|contact[-_]?now|"
    r"get[-_]?in[-_]?touch|reach[-_]?us|reach[-_]?out|"
    r"write[-_]?to[-_]?us|write[-_]?us|email[-_]?us|call[-_]?us|"
    r"talk[-_]?to[-_]?us|speak[-_]?to[-_]?us|connect[-_]?with[-_]?us|"
    r"find[-_]?us|locate[-_]?us|our[-_]?location|our[-_]?locations|"
    r"enquir(?:y|ies|e)|enquir(?:y|ies|e)[-_]?form|"
    r"inquir(?:y|ies|e)|inquir(?:y|ies|e)[-_]?form|"
    r"support|customer[-_]?support|customer[-_]?service|"
    r"customer[-_]?care|help|helpdesk|help[-_]?desk|"
    r"assistance|query|queries|"

    # ── Leadership / Management ───────────────────────────────────
    r"leadership|our[-_]?leadership|leadership[-_]?team|"
    r"management|our[-_]?management|management[-_]?team|"
    r"senior[-_]?management|senior[-_]?team|senior[-_]?leadership|"
    r"executives?|executive[-_]?team|executive[-_]?leadership|"
    r"our[-_]?executives?|c[-_]?suite|"
    r"directors?|board[-_]?of[-_]?directors?|our[-_]?directors?|"
    r"board|our[-_]?board|board[-_]?members?|"
    r"founders?|our[-_]?founders?|founding[-_]?team|"
    r"partners?|our[-_]?partners?|partner[-_]?team|"
    r"principals?|our[-_]?principals?|"

    # ── Profile / Bio ─────────────────────────────────────────────
    r"profile|profiles|our[-_]?profiles?|"
    r"bio|bios|biography|biographies|our[-_]?bios?|"
    r"person|persons|personnel|"
    r"member|members|our[-_]?members?|"

    # ── Experts / Specialists ─────────────────────────────────────
    r"experts?|our[-_]?experts?|"
    r"specialists?|our[-_]?specialists?|"
    r"consultants?|our[-_]?consultants?|"
    r"advisors?|our[-_]?advisors?|advisory[-_]?board|"

    # ── Info / General ────────────────────────────────────────────
    r"info|information|"
    r"overview|introduction|"
    r"imprint|impressum|"          # European legal contact pages
    r"legal|legal[-_]?notice|"
    r"office|offices|our[-_]?offices?|"
    r"locations?|our[-_]?locations?|"
    r"hq|headquarters?|"
    
    # ── International ──────────────────────────────────────────────
    r"karein|sampark|humse[-_]?milein|hamare[-_]?bare[-_]?mein|"  # Hindi
    r"temas|iletisim|hakkimizda|"                                   # Turkish
    r"tentang[-_]?kami|hubungi[-_]?kami|"                          # Indonesian/Malay
    r"lien[-_]?he|guan[-_]?yu"
    r")(/|$|\?|-|\.|#)",
    re.I
)

# _NAME_STRICT removed — replaced by spaCy PERSON NER in is_person_name().
# Supports non-Western names (Indian, Arabic, Chinese, etc.) which _NAME_STRICT rejected.

_NAME_REJECTS = re.compile(
    r"[0-9@#$%&*+=|<>{}/\\,;:!?]|"
    r"\b(our|the|and|for|with|meet|"
    r"new|old|north|south|east|west)\b",
    re.I
)

# Email TLD validation: just ensure the TLD is at least 2 chars (EMAIL_RE already enforces this)
# No hardcoded TLD whitelist — catches all ccTLDs and new gTLDs (.trade, .global, .store, etc.)
def _has_valid_tld(email: str) -> bool:
    tld = email.rsplit(".", 1)[-1]
    return len(tld) >= 2

# Phone extraction: fast regex pre-filter + phonenumbers.parse() per candidate.
# PhoneNumberMatcher is NOT used — it scans the full text O(n) × regions,
# which is prohibitively slow on 500KB page bodies with 300 concurrent workers.
#
# Strategy:
#   1. _PHONE_CAND_RE finds digit clusters that look like phones (single fast pass)
#   2. phonenumbers.parse() + is_valid_number() validates each candidate
#      → eliminates year strings, PINs, random numbers, etc.
#   3. Returns deduplicated E.164 strings.
_PHONE_CAND_RE = re.compile(
    r"(?<!\d)(\+?(?:1|44|91|61|81|86|49|33|39|34|55|971|65|60|62|63|66|82|886"
    r"|852|853|64|27|31|32|41|43|45|46|47|48|351|353|354|358|370|371|372|380"
    r"|420|421|36|40|359|385|386|387|30|90|972|966|20|234|254|212|213|216"
    r"|218|249|251|255|256)[\s\-.]?)?"
    r"(?:\(?\d{2,4}\)?[\s\-.]?)?"
    r"\d{3,5}[\s\-.]?\d{3,5}(?:[\s\-.]?\d{1,5})?"
    r"(?!\d)",
    re.I,
)
_REGION_ORDER = ["IN", "AE", "SG", "MY", "GB", "US"]

def extract_phones_robust(text: str) -> list[str]:
    """Extract validated phone numbers using libphonenumber.

    Uses a fast regex pre-filter to find candidates (single O(n) pass),
    then validates each with phonenumbers.parse() + is_valid_number().
    This eliminates false positives (years, PINs, etc.) while being
    orders of magnitude faster than PhoneNumberMatcher on large text.
    Returns deduplicated E.164 strings (e.g. '+919876543210').
    """
    found: dict[str, bool] = {}
    for m in _PHONE_CAND_RE.finditer(text):
        raw = m.group().strip()
        for region in _REGION_ORDER:
            try:
                num = phonenumbers.parse(raw, region)
                if is_valid_number(num):
                    found[format_number(num, PhoneNumberFormat.E164)] = True
                    break  # valid — no need to try more regions
            except NumberParseException:
                pass
    return list(found.keys())

# Load spaCy model once at module level — cheap to call, expensive to load.
try:
    _nlp = spacy.load("en_core_web_sm", disable=["parser", "tagger", "lemmatizer"])
except OSError:
    _nlp = None  # graceful fallback — is_person_name() uses heuristics

# Semaphore: cap concurrent spaCy calls to avoid GIL pile-up when
# many executor threads call nlp() simultaneously.
_NLP_SEM = threading.Semaphore(6)

LINKEDIN_RE  = re.compile(r"https?://(?:www\.)?linkedin\.com/(?:in|company|pub)/[^\s\"'<>]+", re.I)
TWITTER_RE   = re.compile(r"https?://(?:www\.)?(?:twitter|x)\.com/[a-zA-Z0-9_]+", re.I)
FACEBOOK_RE  = re.compile(r"https?://(?:www\.)?facebook\.com/[^\s\"'<>]+", re.I)
INSTAGRAM_RE = re.compile(r"https?://(?:www\.)?instagram\.com/[^\s\"'<>]+", re.I)
YOUTUBE_RE   = re.compile(r"https?://(?:www\.)?youtube\.com/(?:c/|channel/|user/)[^\s\"'<>]+", re.I)
WHATSAPP_RE  = re.compile(r"https?://(?:api\.)?whatsapp\.com/send[^\s\"'<>]*", re.I)

ADDRESS_KEYWORDS = re.compile(
    r"\b(?:address|location|headquarter|hq|office|registered.office|our.office|find.us|where.we.are)\b",
    re.I
)

ZERO_WIDTH_CHARS = re.compile(r"[\u200b\u200c\u200d\u200e\u200f\uFEFF\u00ad]")

def clean_zero_width(text: str) -> str:
    return ZERO_WIDTH_CHARS.sub("", text)

SPACED_EMAIL_RE = re.compile(
    r"[a-zA-Z0-9._%+\-]+\s*@\s*[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}",
    re.I
)

def extract_spaced_emails(text: str) -> list[str]:
    found = []
    for m in SPACED_EMAIL_RE.finditer(text):
        # SPACED_EMAIL_RE already enforces valid email structure; just normalise spaces
        cleaned = re.sub(r"\s*@\s*", "@", m.group()).lower().strip()
        found.append(cleaned)
    return found

GENERIC_WORDS = {
    "meet", "our", "team", "the", "and", "for", "with", "management",
    "storage", "solutions", "company", "profile", "services", "specialist",
    "support", "coordination", "welcome", "about", "contact",
    "delhi", "mumbai", "dubai", "london", "beijing", "shanghai",
    "singapore", "sydney", "melbourne", "toronto", "york", "angeles",
    "francisco", "chicago", "houston", "dallas", "miami", "boston",
    "kingdom", "states", "united", "america", "india", "china",
    "australia", "canada", "europe", "asia", "africa", "pacific",
    "north", "south", "east", "west", "new", "old", "central",
    "express", "logistics", "transport", "freight", "cargo", "shipping",
    "international", "global", "national", "regional", "local",
    "group", "holdings", "enterprises", "associates", "partners",
    "consulting", "technologies", "systems", "networks",
    "proposal", "marriage", "wedding", "diary", "report", "review",
    "guide", "tips", "news", "latest", "update", "announcement",
    "supervisor", "coordinator", "administrator", "representative",
}

def extract_all_attribute_text(soup) -> str:
    attrs_to_check = ["alt", "title", "placeholder", "data-email", "data-mail", "content", "aria-label"]
    parts = []
    for tag in soup.find_all(True):
        for attr in attrs_to_check:
            val = tag.get(attr, "")
            if val and isinstance(val, str):
                parts.append(val)
    return " ".join(parts)

def extract_all_emails(html: str, soup) -> list[tuple[str, str]]:
    found: dict[str, str] = {}

    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if href.lower().startswith("mailto:"):
            addr = href[7:].split("?")[0].strip().lower()
            addr = ZERO_WIDTH_CHARS.sub("", addr)
            if EMAIL_RE.match(addr):
                found[addr] = "mailto-link"

    for a in soup.find_all("a", href=True):
        if "email-protection" in a.get("href", ""):
            decoded = _decode_cf_email(a.get("data-cfemail", ""))
            if decoded and EMAIL_RE.match(decoded):
                found[decoded.lower()] = "cloudflare"

    def clean_html_for_scanning(h: str) -> str:
        h = re.sub(r"<script[^>]*>.*?</script>", " ", h, flags=re.S | re.I)
        h = re.sub(r"<style[^>]*>.*?</style>", " ", h, flags=re.S | re.I)
        return h

    decoded_html = html_module.unescape(clean_html_for_scanning(html))
    for m in EMAIL_RE.finditer(decoded_html):
        email = m.group().lower()
        if not re.search(r"\.(png|jpg|gif|svg|css|js|woff)$", email):
            found.setdefault(email, "html-entity-decoded")

    full_text = clean_zero_width(soup.get_text(separator=" ", strip=True))
    for m in EMAIL_RE.finditer(full_text):
        email = m.group().lower()
        if not re.search(r"\.(png|jpg|gif|svg|css|js|woff)$", email):
            found.setdefault(email, "text")

    # 5. Spelled-out obfuscation: "email at domain dot com"
    # Requirements: local ≥4 chars, domain ≥3 chars, TLD not a common English word.
    # Only matches spelled-out "at"/"dot" — NOT literal @ or . (those are handled by EMAIL_RE).
    _JUNK_TLDS = re.compile(
        r"^(if|or|the|an|in|it|is|be|as|at|by|do|go|of|on|up|us|we|so|"
        r"what|which|this|that|them|than|then|from|with|have|your|their|"
        r"train|track|route|book|search|plan|find|link|"
        r"want|need|help|call|ask|see|let|get|put|set|run|use|try|say)$",
        re.I
    )
    AT_DOT_RE = re.compile(
        r"(?<![\w@])([a-zA-Z0-9._%+\-]{4,})"   # local: min 4 chars, word-boundary start
        r"\s+[\[\(]?\s*(?:at|AT)\s*[\]\)]?\s+" # spelled-out "at" surrounded by spaces
        r"([a-zA-Z0-9][a-zA-Z0-9.\-]{2,})"      # domain: min 3 chars
        r"\s*[\[\(]?\s*(?:dot|DOT)\s*[\]\)]?\s*" # spelled-out "dot"
        r"([a-zA-Z]{2,6})(?![a-zA-Z])"           # TLD: 2-6 chars
    )
    for m in AT_DOT_RE.finditer(full_text):
        local_part, domain_part, tld = m.group(1), m.group(2), m.group(3)
        if _JUNK_TLDS.match(tld):
            continue
        email = f"{local_part}@{domain_part}.{tld}".lower()
        if EMAIL_RE.match(email):
            found.setdefault(email, "at-dot-obfuscation")

    for email in extract_spaced_emails(full_text):
        found.setdefault(email, "spaced-at")

    attr_text = clean_zero_width(extract_all_attribute_text(soup))
    for m in EMAIL_RE.finditer(attr_text):
        email = m.group().lower()
        if not re.search(r"\.(png|jpg|gif|svg|css|js|woff)$", email):
            found.setdefault(email, "html-attribute")

    result = []
    for email, ctx in found.items():
        if re.search(r"(example|test|dummy|sample|noreply|no-reply|sentry|wpcf7|yourname|username)@", email):
            continue
        if not _has_valid_tld(email):
            continue
        result.append((email, ctx))
    return result

# ── Employee patterns ─────────────────────────────────────────────────────────────

TITLE_KEYWORDS = re.compile(
    r"\b(?:CEO|CTO|CFO|COO|CMO|CXO|MD|Director|Manager|Head\s+of|VP|Vice[\s\-]President|"
    r"President|Founder|Co[\s\-]?Founder|Partner|Principal|Lead|Senior|Engineer|"
    r"Analyst|Consultant|Executive|Officer|Associate|Coordinator|Specialist|"
    r"Supervisor|Administrator|Controller|Advisor|Representative|Agent|"
    r"Account\s+Manager|Sales\s+Manager|Operations\s+Manager|Supply\s+Chain|"
    r"Logistics\s+Manager|Project\s+Manager|General\s+Manager|Country\s+Manager|"
    r"Regional\s+Manager|Business\s+Development)\b",
    re.I
)

PURE_TITLE_RE = re.compile(
    r"^(?:CEO|CTO|CFO|COO|CMO|MD|Director|Manager|Head\s+of\s+\w+|VP|"
    r"Vice[\s\-]President|President|Founder|Co[\s\-]?Founder|Partner|Principal|"
    r"Lead|Senior\s+\w+|Engineer|Analyst|Consultant|Executive|Officer|Associate|"
    r"Coordinator|Specialist|Supervisor|Administrator|Controller|Advisor|"
    r"Representative|Agent|"
    r"(?:Account|Sales|Operations|Logistics|Supply\s+Chain|Project|General|Country|"
    r"Regional|Business\s+Development|Expansion)\s+Manager|"
    r"Operations\s+Director|Supply\s+Chain\s+Coordinator)\s*$",
    re.I
)

# Person name: 2-4 capitalized words, alpha + hyphen/apostrophe only
NAME_RE = re.compile(
    r"^([A-Z][a-zA-Z'\-]{1,25})(\s+[A-Z][a-zA-Z'\-]{1,25}){1,3}$"
)

# Non-name heading words (deliberately conservative — fewer exclusions = higher recall)
NOT_A_NAME_WORDS = re.compile(
    r"\b(?:FAQ|Question|Answer|Service|Solution|Option|Feature|Fleet|"
    r"Luxury|Premium|Package|Plan|Pricing|About|Contact|"
    r"Mission|Vision|Policy|Term|Condition|Privacy|Legal|"
    r"Overview|Introduction|Summary|Detail|Information|"
    r"Product|Category|Section|Chapter|Frequently|Asked)\b",
    re.I
)

# Card-level filter signals (defined here to avoid recompiling on every loop iteration)
_LOCATION_SIGNALS = re.compile(
    r"\b(?:office|branch|location|headquarter|address|"
    r"warehouse|facility|depot|terminal|port|city|"
    r"country|region|zone|area|district)\b", re.I
)
_SERVICE_SIGNALS = re.compile(
    r"\b(?:solution|service|product|feature|package|"
    r"option|plan|pricing|benefit|advantage)\b", re.I
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler("fetch_contacts.log", encoding="utf-8"),
        logging.StreamHandler(),
    ]
)
log = logging.getLogger("contacts")


# ── DB setup ──────────────────────────────────────────────────────────────────────

def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    try:
        conn.execute("ALTER TABLE domain_metadata ADD COLUMN company_address TEXT;")
    except sqlite3.OperationalError:
        pass
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS domain_contacts (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            domain       TEXT NOT NULL,
            category     TEXT,
            page_url     TEXT,
            entity_type  TEXT NOT NULL,
            value        TEXT NOT NULL,
            label        TEXT,
            context      TEXT,
            fetched_at   TEXT
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_contacts_dedup
            ON domain_contacts(domain, entity_type, value);
        CREATE INDEX IF NOT EXISTS idx_contacts_domain  ON domain_contacts(domain);
        CREATE INDEX IF NOT EXISTS idx_contacts_type    ON domain_contacts(entity_type);

        CREATE TABLE IF NOT EXISTS domain_employees (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            domain      TEXT,
            name        TEXT,
            title       TEXT,
            email       TEXT,
            phone       TEXT,
            linkedin    TEXT,
            fetched_at  TEXT
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_employees_dedup
            ON domain_employees(domain, name);

        CREATE TABLE IF NOT EXISTS domain_crawl_status (
            domain        TEXT PRIMARY KEY,
            category      TEXT,
            status_code   INTEGER,
            crawled_at    TEXT,
            pages_crawled INTEGER,
            contacts_found INTEGER,
            used_playwright INTEGER DEFAULT 0,
            error_msg     TEXT
        );
    """)
    conn.commit()
    conn.close()
    log.info("Tables ready")


def count_pending():
    conn = sqlite3.connect(DB_PATH)
    total = conn.execute(
        "SELECT COUNT(*) FROM domain_metadata WHERE status_code = 200"
    ).fetchone()[0]
    done = conn.execute("SELECT COUNT(*) FROM domain_crawl_status").fetchone()[0]
    conn.close()
    return total, done


def is_metadata_fetching_done():
    conn = sqlite3.connect(DB_PATH)
    pending = conn.execute("""
        SELECT COUNT(*) FROM domains d
        LEFT JOIN domain_metadata m ON d.domain = m.domain
        WHERE m.domain IS NULL
    """).fetchone()[0]
    conn.close()
    return pending == 0


def fetch_batch_from_db(last_rowid):
    conn = sqlite3.connect(DB_PATH)
    placeholders = ",".join("?" * len(ALLOWED_CATEGORIES))
    query = f"""
        SELECT dm.rowid, dm.domain, dm.category
        FROM domain_metadata dm
        WHERE dm.status_code = 200
          AND dm.category IN ({placeholders})
          AND dm.rowid > ?
          AND NOT EXISTS (
              SELECT 1 FROM domain_crawl_status cs WHERE cs.domain = dm.domain
          )
        ORDER BY dm.rowid ASC
        LIMIT ?
    """
    params = tuple(ALLOWED_CATEGORIES) + (last_rowid, BATCH_SIZE)
    rows = conn.execute(query, params).fetchall()
    conn.close()
    return rows


def write_contacts_to_db(contact_rows, employee_rows, status_rows, address_updates=None):
    if not contact_rows and not employee_rows and not status_rows and not address_updates:
        return
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.execute("PRAGMA synchronous=NORMAL")
    if contact_rows:
        conn.executemany("""
            INSERT OR IGNORE INTO domain_contacts
            (domain, category, page_url, entity_type, value, label, context, fetched_at)
            VALUES (?,?,?,?,?,?,?,?)
        """, contact_rows)
    if employee_rows:
        conn.executemany("""
            INSERT OR IGNORE INTO domain_employees
            (domain, name, title, email, phone, linkedin, fetched_at)
            VALUES (?,?,?,?,?,?,?)
        """, employee_rows)
    if status_rows:
        conn.executemany("""
            INSERT OR REPLACE INTO domain_crawl_status
            (domain, category, status_code, crawled_at, pages_crawled, contacts_found, used_playwright, error_msg)
            VALUES (?,?,?,?,?,?,?,?)
        """, status_rows)
    if address_updates:
        conn.executemany("""
            UPDATE domain_metadata SET company_address = COALESCE(company_address, ?)
            WHERE domain = ?
        """, address_updates)
    conn.commit()
    conn.close()


# ── URL utilities ─────────────────────────────────────────────────────────────────

def score_url(url: str) -> int:
    path = urlparse(url).path
    for pattern, score in PAGE_PRIORITY:
        if pattern.search(path):
            return score
    return 0


def extract_internal_links(html: str, base_url: str) -> list[tuple[int, str]]:
    soup = BeautifulSoup(html, "lxml")
    base_domain = urlparse(base_url).netloc
    seen = set()
    scored = []
    for tag in soup.find_all("a", href=True):
        full = urljoin(base_url, tag["href"].strip())
        parsed = urlparse(full)
        if (parsed.scheme in ("http", "https")
                and parsed.netloc == base_domain
                and full not in seen):
            s = score_url(full)
            if s > 0:
                seen.add(full)
                scored.append((s, full))
    scored.sort(reverse=True)
    return scored


# ── Contact/phone helpers ─────────────────────────────────────────────────────────

def strip_head(html: str) -> str:
    """Remove <head>...</head> to cut HTML size by 30-60% before parsing."""
    body_start = re.search(r"<body[\s>]", html, re.I)
    if body_start:
        return html[body_start.start():]
    head_end = re.search(r"</head\s*>", html, re.I)
    if head_end:
        return html[head_end.end():]
    return html

def clean_phone(raw: str) -> str | None:
    digits = re.sub(r"\D", "", raw)
    if len(digits) < 7 or len(digits) > 15:
        return None
    if re.match(r"^(19|20)\d{2}$", digits):
        return None
    if re.match(r"^(\d)\1{6,}$", digits):
        return None
    if len(set(digits)) < 3:
        return None
    if len(digits) < 8 and not raw.strip().startswith("+"):
        return None
    return raw.strip()


def _decode_cf_email(encoded: str) -> str | None:
    """Decode Cloudflare XOR-obfuscated email hex string."""
    try:
        b = bytes.fromhex(encoded)
        key = b[0]
        return "".join(chr(c ^ key) for c in b[1:])
    except Exception:
        return None


def _best_email_from_soup(tag) -> str | None:
    """
    Extract best email from a BeautifulSoup element.
    Priority: mailto href > CF decode > data-* attrs > [at] obfuscation > text regex.
    """
    # 1. mailto: href
    for a in tag.find_all("a", href=True):
        href = a["href"]
        if href.lower().startswith("mailto:"):
            addr = href[7:].split("?")[0].strip().lower()
            if EMAIL_RE.match(addr):
                return addr

    # 2. CloudFlare email protection
    for a in tag.find_all("a", href=True):
        if "email-protection" in a.get("href", ""):
            decoded = _decode_cf_email(a.get("data-cfemail", ""))
            if decoded and EMAIL_RE.match(decoded):
                return decoded.lower()

    # 3. data-email / data-mail attributes
    for t in tag.find_all(True):
        for attr in ("data-email", "data-mail", "data-contact"):
            val = t.get(attr, "").strip()
            if val and EMAIL_RE.match(val):
                return val.lower()

    text = tag.get_text(separator=" ")

    # 4. [at] / [dot] obfuscation
    m = re.search(
        r"([a-zA-Z0-9._%+\-]+)\s*[\[\(]?\s*(?:at|@)\s*[\]\)]?\s*"
        r"([a-zA-Z0-9.\-]+)\s*[\[\(]?\s*(?:dot|\.)\s*[\]\)]?\s*([a-zA-Z]{2,})",
        text, re.I
    )
    if m:
        return f"{m.group(1)}@{m.group(2)}.{m.group(3)}".lower()

    # 5. Plain text regex
    em = EMAIL_RE.search(text)
    if em:
        val = em.group().lower()
        if not re.search(r"\.(png|jpg|gif|svg|css|js|woff)$", val):
            return val

    return None


def _best_phone_from_soup(tag) -> str | None:
    """
    Extract best phone from a BeautifulSoup element.
    Priority: tel: href > data-* attrs > phonenumbers scan.
    """
    # 1. tel: href
    for a in tag.find_all("a", href=True):
        if a["href"].lower().startswith("tel:"):
            ph = clean_phone(a["href"][4:].strip())
            if ph:
                return ph

    # 2. data-phone / data-tel attributes
    for t in tag.find_all(True):
        for attr in ("data-phone", "data-tel", "data-mobile"):
            val = t.get(attr, "").strip()
            if val:
                ph = clean_phone(val)
                if ph:
                    return ph

    # 3. phonenumbers scan on card text
    phones = extract_phones_robust(tag.get_text(separator=" "))
    return phones[0] if phones else None


# ── Name / title helpers ──────────────────────────────────────────────────────────

def is_person_name(text: str) -> bool:
    """NER-backed person name validator.

    Uses spaCy en_core_web_sm PERSON NER, which handles Western, Indian,
    Arabic, Chinese and other non-Western names that regex rejects.
    Falls back to capitalisation heuristics if the model is unavailable.
    """
    text = text.strip()
    if text.isupper():
        text = text.title()

    # Fast pre-filter before NLP
    if not text:
        return False
    words = text.split()
    if not 2 <= len(words) <= 5:
        return False
    if len(text) > 60:
        return False
    if _NAME_REJECTS.search(text):
        return False
    if NOT_A_NAME_WORDS.search(text):
        return False

    if _nlp is None:
        return all(w[0].isupper() for w in words if w)

    # Semaphore: only 6 threads may call nlp() concurrently.
    # Prevents GIL saturation when 300 async workers all hit spaCy at once.
    with _NLP_SEM:
        doc = _nlp(text)
    return any(ent.label_ == "PERSON" for ent in doc.ents)


# Backward-compat alias
is_strict_person_name = is_person_name


def is_strict_title(text: str) -> bool:
    text = text.strip()
    
    if not 2 <= len(text) <= 60:
        return False
    
    if len(text.split()) > 6:
        return False
    
    if not TITLE_KEYWORDS.search(text):
        return False
    
    if re.search(
        r"\b(she|he|her|his|they|who|with|oversees|manages|"
        r"leads|coordinates|responsible|understanding|"
        r"experience)\b", text, re.I
    ):
        return False
    
    if ":" in text or text.count(",") > 1:
        return False
    
    if text[0].islower():
        return False
    
    return True


def extract_footer_contacts(soup, add_fn):
    """Specifically target footer elements which contain 90% of contact data."""
    footer_selectors = [
        "footer", "[class*='footer']", "[id*='footer']", 
        "[class*='contact']", "[id*='contact']",
        "[class*='widget']", "[class*='bottom']", "[id*='bottom']"
    ]
    
    footer_elements = []
    seen_els = set()
    
    for sel in footer_selectors:
        for el in soup.select(sel):
            el_id = id(el)
            if el_id not in seen_els:
                seen_els.add(el_id)
                footer_elements.append(el)
    
    for el in footer_elements:
        text = el.get_text(separator=" ", strip=True)
        
        # Tel links
        for a in el.find_all("a", href=True):
            href = a["href"].strip()
            if href.lower().startswith("mailto:"):
                addr = href[7:].split("?")[0].strip().lower()
                if EMAIL_RE.match(addr):
                    add_fn("email", addr, context="footer-mailto")
            elif href.lower().startswith("tel:"):
                ph = clean_phone(href[4:].strip())
                if ph:
                    add_fn("phone", ph, context="footer-tel")
        
        # Phones from text (libphonenumber validated)
        for e164 in extract_phones_robust(text):
            add_fn("phone", e164, context="footer")


# ── Main page extraction ──────────────────────────────────────────────────────────

def extract_contacts(html: str, page_url: str, domain: str):
    """
    Returns (contact_list, employee_list, address_str, profile_links).
    ALL four values always returned — no more 3-vs-4 tuple bugs.
    """
    contacts: list[dict] = []
    employees: list[dict] = []
    seen_contacts: set[tuple] = set()
    seen_employee_names: set[str] = set()
    profile_links: list[tuple] = []   # (name, title, linkedin, url)

    def add(entity_type: str, value: str, label=None, context=None):
        value = value.strip()
        key = (entity_type, value.lower())
        if not value or key in seen_contacts:
            return
        seen_contacts.add(key)
        contacts.append({
            "entity_type": entity_type, "value": value,
            "label": label,
            "context": context[:200] if context else None,
        })

    def add_employee(name, title, email, phone, linkedin):
        name = name.strip()
        key = name.lower()
        if not name or key in seen_employee_names:
            return
        seen_employee_names.add(key)
        employees.append({
            "name": name, "title": title,
            "email": email, "phone": phone, "linkedin": linkedin
        })

    soup = BeautifulSoup(html, "lxml")
    full_text = soup.get_text(separator=" ", strip=True)

    # ── Master Email Extractor (handles obfuscation) ──────────────────────────────
    for email_val, ctx in extract_all_emails(html, soup):
        add("email", email_val, context=ctx)

    # ── Phones from links (tel tags) ──────────────────────────────────────────────
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if href.lower().startswith("tel:"):
            ph = clean_phone(href[4:].strip())
            if ph:
                add("phone", ph, context="tel link")

    # ── Phones from visible text (libphonenumber validated, E.164) ───────────────
    for e164 in extract_phones_robust(full_text):
        add("phone", e164, context="page-text")

    # ── Social links ──────────────────────────────────────────────────────────────
    for tag in soup.find_all("a", href=True):
        href = tag["href"].strip()
        label = tag.get_text(strip=True)[:100] or tag.get("aria-label", "")[:100]
        if LINKEDIN_RE.match(href):
            add("linkedin_company" if "/company/" in href else "linkedin_profile", href, label=label)
        elif TWITTER_RE.match(href):
            add("twitter", href, label=label)
        elif FACEBOOK_RE.match(href):
            add("facebook", href, label=label)
        elif INSTAGRAM_RE.match(href):
            add("instagram", href, label=label)
        elif YOUTUBE_RE.match(href):
            add("youtube", href, label=label)
        elif WHATSAPP_RE.match(href):
            add("whatsapp", href, label=label)

    # ── Address ───────────────────────────────────────────────────────────────────
    address_str = None
    for tag in soup.find_all(string=ADDRESS_KEYWORDS):
        parent = tag.parent
        if parent:
            block = parent.get_text(separator=" ", strip=True)
            if 20 < len(block) < 400 and not address_str:
                address_str = block
    for tag in soup.find_all("address"):
        block = tag.get_text(separator=" ", strip=True)
        if block and not address_str:
            address_str = block

    # ── Footer Targeted Extraction ────────────────────────────────────────────────
    extract_footer_contacts(soup, add)

    # ── Employees ─────────────────────────────────────────────────────────────────
    if EMPLOYEE_PAGE_RE.search(page_url) or page_url.rstrip("/") == f"https://{domain}" or score_url(page_url) >= 8:
        _extract_employees(soup, add_employee, page_url, profile_links)

    return contacts, employees, address_str, profile_links

PERSONAL_EMAIL_DOMAINS = {
    "gmail.com", "yahoo.com", "hotmail.com", "outlook.com", "icloud.com", 
    "me.com", "mac.com", "protonmail.com", "proton.me", "aol.com", "zoho.com"
}

def get_root_domain(url_or_domain: str) -> str:
    val = url_or_domain.split("://")[-1].split("/")[0]
    parts = val.split(".")
    if len(parts) > 2 and parts[-2] in ("co", "com", "org", "net", "edu", "gov"):
        return ".".join(parts[-3:])
    return ".".join(parts[-2:]) if len(parts) >= 2 else val

def is_valid_employee_email(email: str, company_domain: str) -> bool:
    """Accept employee email if it matches the company's domain OR is from a known personal provider."""
    if not email or "@" not in email:
        return False
    local, _, domain = email.partition("@")
    if not _has_valid_tld(email):
        return False
    generic = {"info", "sales", "contact", "support", "admin", "hello", "hi",
               "enquiries", "webmaster", "careers", "jobs", "media", "team"}
    if local.lower() in generic:
        return False
    root_email = get_root_domain(domain)
    root_company = get_root_domain(company_domain)
    # Valid if: belongs to the company's own domain OR is a known personal provider
    if root_email == root_company or root_email in PERSONAL_EMAIL_DOMAINS:
        return True
    return False

def _extract_employees(soup, add_employee_fn, page_url: str, profile_links: list):
    """
    Scan page for employee cards. For each valid card:
      1. Extract email/phone from card-level links and attributes
      2. Fall back to page-level mailto/tel link index matched by employee name
      3. Collect profile page URL for follow-up crawl if still missing contacts
    """
    base_domain = urlparse(page_url).netloc

    # Build page-level lookup: anchor-text/word → email or phone
    # Used when a card has no mailto/tel but there's a "Contact John" link elsewhere
    page_email_idx: dict[str, str] = {}
    page_phone_idx: dict[str, str] = {}

    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        anchor = a.get_text(strip=True).lower()

        if href.lower().startswith("mailto:"):
            addr = href[7:].split("?")[0].strip().lower()
            if EMAIL_RE.match(addr):
                page_email_idx[anchor] = addr
                for w in anchor.split():
                    if len(w) > 2:
                        page_email_idx.setdefault(w, addr)

        elif href.lower().startswith("tel:"):
            ph = clean_phone(href[4:].strip())
            if ph:
                page_phone_idx[anchor] = ph
                for w in anchor.split():
                    if len(w) > 2:
                        page_phone_idx.setdefault(w, ph)

        if "email-protection" in href:
            decoded = _decode_cf_email(a.get("data-cfemail", ""))
            if decoded and EMAIL_RE.match(decoded):
                page_email_idx[anchor] = decoded.lower()

    card_pattern = re.compile(
        r"team|member|person|staff|employee|people|card|profile|bio", re.I
    )
    cards = soup.find_all(
        lambda tag: tag.name in ("div", "article", "li", "section")
        and any(card_pattern.search(cls) for cls in (tag.get("class") or []))
    )

    seen_profile_urls: set[str] = set()

    for card in cards[:50]:
        card_text = card.get_text(separator="\n", strip=True)
        if not TITLE_KEYWORDS.search(card_text):
            continue

        if _LOCATION_SIGNALS.search(card_text) and not re.search(
            r"\b(?:Manager|Director|Officer|Head)\b", card_text, re.I
        ):
            continue
        if _SERVICE_SIGNALS.search(card_text[:100]):
            continue

        # Name
        headings = card.find_all(["h1", "h2", "h3", "h4", "h5", "strong", "b"])
        if not headings:
            continue
            
        name = None
        name_idx = 0
        for idx, h in enumerate(headings):
            text = h.get_text(strip=True)
            if is_strict_person_name(text):
                name = text
                name_idx = idx
                break
        if not name:
            continue

        # Title
        title = None
        if name_idx + 1 < len(headings):
            candidate = headings[name_idx + 1].get_text(strip=True)
            if is_strict_title(candidate):
                title = candidate
                
        if not title:
            for sibling in headings[name_idx].find_next_siblings(["p", "span", "div"], limit=3):
                text = sibling.get_text(strip=True)
                if is_strict_title(text):
                    title = text
                    break

        # Email — card-level first
        email = _best_email_from_soup(card)

        # Phone — card-level first
        phone = _best_phone_from_soup(card)

        # LinkedIn
        lm = LINKEDIN_RE.search(str(card))
        linkedin = lm.group() if lm else None

        # Page-level fallback by name
        if not email or not phone:
            for k in [name.lower()] + name.lower().split():
                if not email and k in page_email_idx:
                    email = page_email_idx[k]
                if not phone and k in page_phone_idx:
                    phone = page_phone_idx[k]
                if email and phone:
                    break

        # Collect profile page link if still missing
        if (not email or not phone):
            for a in card.find_all("a", href=True):
                full = urljoin(page_url, a["href"].strip())
                parsed = urlparse(full)
                if (parsed.scheme in ("http", "https")
                        and parsed.netloc == base_domain
                        and full not in seen_profile_urls
                        and re.search(
                            r"(?:team|staff|people|member|profile|about|bio|person)/",
                            parsed.path, re.I
                        )):
                    seen_profile_urls.add(full)
                    profile_links.append((name, title, linkedin, full))
                    break

        if email and not is_valid_employee_email(email, base_domain):
            email = None

        add_employee_fn(name=name, title=title, email=email, phone=phone, linkedin=linkedin)


# ── HTTP / Playwright fetch ───────────────────────────────────────────────────────

import random
async def fetch_html(session, url: str) -> tuple[int, str | None]:
    try:
        headers = {**REQUEST_HEADERS, "User-Agent": random.choice(USER_AGENTS)}
        async with session.get(url, allow_redirects=True, headers=headers,
                                max_redirects=MAX_REDIRECTS, ssl=False) as resp:
            ct = resp.headers.get("Content-Type", "")
            if resp.status == 200 and "html" in ct.lower():
                raw = b""
                async for chunk in resp.content.iter_chunked(8192):
                    raw += chunk
                    if len(raw) >= MAX_BODY_BYTES:
                        break
                html_text = raw.decode("utf-8", errors="ignore")
                return resp.status, strip_head(html_text)
            return resp.status, None
    except asyncio.TimeoutError:
        return -1, None
    except aiohttp.ClientSSLError:
        try:
            http_url = url.replace("https://", "http://", 1)
            async with session.get(http_url, allow_redirects=True,
                                    max_redirects=MAX_REDIRECTS, ssl=False) as resp:
                ct = resp.headers.get("Content-Type", "")
                if resp.status == 200 and "html" in ct.lower():
                    raw = b""
                    async for chunk in resp.content.iter_chunked(8192):
                        raw += chunk
                        if len(raw) >= MAX_BODY_BYTES:
                            break
                    html_text = raw.decode("utf-8", errors="ignore")
                    return resp.status, strip_head(html_text)
                return resp.status, None
        except Exception:
            return -4, None
    except Exception:
        return -6, None


def extract_profile_contacts(html: str):
    soup = BeautifulSoup(html, "lxml")
    return _best_email_from_soup(soup), _best_phone_from_soup(soup)

# ── Core domain crawl ─────────────────────────────────────────────────────────────

async def crawl_domain(session, domain: str, category: str):
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    homepage_url = f"https://{domain}"

    # Store employees in a dict keyed by name.lower() for easy backfill
    employee_map: dict[str, dict] = {}
    all_contacts: list[dict] = []
    seen_contacts: set[tuple] = set()
    pages_crawled = 0
    error_msg = None
    domain_address = None

    # Profile links: (name, title, linkedin, url)
    all_profile_links: list[tuple] = []
    seen_profile_urls: set[str] = set()

    def dedup_contacts(new_contacts):
        fresh = []
        for c in new_contacts:
            key = (c["entity_type"], c["value"].lower())
            if key not in seen_contacts:
                seen_contacts.add(key)
                fresh.append(c)
        return fresh

    def merge_employees(new_employees):
        for e in new_employees:
            key = e["name"].lower()
            if key not in employee_map:
                employee_map[key] = dict(e)
            else:
                ex = employee_map[key]
                if not ex["email"]    and e["email"]:    ex["email"]    = e["email"]
                if not ex["phone"]    and e["phone"]:    ex["phone"]    = e["phone"]
                if not ex["linkedin"] and e["linkedin"]: ex["linkedin"] = e["linkedin"]
                if not ex["title"]    and e["title"]:    ex["title"]    = e["title"]

    def collect_profiles(links):
        for item in links:
            url = item[3]
            if url not in seen_profile_urls:
                seen_profile_urls.add(url)
                all_profile_links.append(item)

    # Step 1: homepage
    status, html = await fetch_html(session, homepage_url)
    used_pw = 0

    if html is None:
        return [], [], (domain, category, status, now, 0, 0, 0, f"connection_failed_{status}"), None

    pages_crawled += 1
    c_res, e_res, addr, p_links = await asyncio.get_running_loop().run_in_executor(
        None, extract_contacts, html, homepage_url, domain
    )
    if addr:
        domain_address = addr
    all_contacts.extend(dedup_contacts(c_res))
    merge_employees(e_res)
    collect_profiles(p_links)

    # Step 2: scored sub-pages
    candidate_links = await asyncio.get_running_loop().run_in_executor(
        None, extract_internal_links, html, homepage_url
    )
    for url in [u for _, u in candidate_links[:MAX_PAGES_PER_DOMAIN - 1]]:
        _, sub_html = await fetch_html(session, url)
        if sub_html:
            pages_crawled += 1
            c_res, e_res, addr, p_links = await asyncio.get_running_loop().run_in_executor(
                None, extract_contacts, sub_html, url, domain
            )
            if addr and not domain_address:
                domain_address = addr
            all_contacts.extend(dedup_contacts(c_res))
            merge_employees(e_res)
            collect_profiles(p_links)

    # Step 3: individual profile pages for employees still missing email/phone
    for (emp_name, _title, _linkedin, profile_url) in all_profile_links[:10]:
        emp = employee_map.get(emp_name.lower())
        if emp and emp["email"] and emp["phone"]:
            continue

        _, prof_html = await fetch_html(session, profile_url)
        if not prof_html:
            continue

        pages_crawled += 1
        found_email, found_phone = await asyncio.get_running_loop().run_in_executor(
            None, extract_profile_contacts, prof_html
        )

        if emp:
            if found_email and not is_valid_employee_email(found_email, domain):
                found_email = None
            if not emp["email"] and found_email:
                emp["email"] = found_email
                key_c = ("email", found_email)
                if key_c not in seen_contacts:
                    seen_contacts.add(key_c)
                    all_contacts.append({
                        "entity_type": "email", "value": found_email,
                        "label": emp_name, "context": "profile page"
                    })
            if not emp["phone"] and found_phone:
                emp["phone"] = found_phone

    # Build DB rows
    contact_rows = [
        (domain, category, homepage_url,
         c["entity_type"], c["value"], c["label"], c["context"], now)
        for c in all_contacts
    ]
    employee_rows = [
        (domain, e["name"], e["title"], e["email"], e["phone"], e["linkedin"], now)
        for e in employee_map.values()
    ]
    status_row = (
        domain, category, status, now,
        pages_crawled, len(contact_rows) + len(employee_rows), used_pw, error_msg
    )

    return contact_rows, employee_rows, status_row, domain_address


# ── Threaded resolver (Windows-safe) ─────────────────────────────────────────────

class ThreadedResolver:
    def __init__(self):
        self._executor = ThreadPoolExecutor(max_workers=CONCURRENCY)

    async def resolve(self, hostname, port=0, family=socket.AF_INET):
        loop = asyncio.get_running_loop()
        infos = await loop.run_in_executor(
            self._executor,
            lambda: socket.getaddrinfo(
                hostname, port, family=family, type=socket.SOCK_STREAM
            )
        )
        return [
            {"hostname": hostname, "host": addr[0], "port": addr[1],
             "family": fam, "proto": proto, "flags": 0}
            for fam, _, proto, _, addr in infos
        ]

    async def close(self):
        self._executor.shutdown(wait=False)


# ── Main ──────────────────────────────────────────────────────────────────────────

async def run():
    init_db()
    total, done = count_pending()
    pending = total - done

    log.info(f"Total 200-OK domains : {total:,}")
    log.info(f"Already crawled      : {done:,}")
    log.info(f"Remaining            : {pending:,}")
    log.info(f"Concurrency          : {CONCURRENCY}")

    if pending == 0:
        log.info("Nothing to do.")
        return

    # Use a fixed-size thread pool for run_in_executor(None, ...) calls.
    # Default is cpu_count+4 which is too small for 300 concurrent workers.
    loop = asyncio.get_running_loop()
    loop.set_default_executor(ThreadPoolExecutor(max_workers=32))

    timeout = aiohttp.ClientTimeout(
        connect=CONNECT_TIMEOUT, sock_read=READ_TIMEOUT,
        total=CONNECT_TIMEOUT + READ_TIMEOUT + 10
    )
    resolver  = ThreadedResolver()
    connector = aiohttp.TCPConnector(
        limit=CONCURRENCY, limit_per_host=4,
        ttl_dns_cache=600, ssl=False,
        enable_cleanup_closed=True,
        resolver=resolver,
    )

    queue       = asyncio.Queue(maxsize=CONCURRENCY * 4)
    write_queue = asyncio.Queue()

    crawled_total  = 0
    contacts_total = 0
    t_start = time.monotonic()

    async def db_writer():
        nonlocal crawled_total, contacts_total
        contact_buf  = []
        employee_buf = []
        status_buf   = []
        addr_updates = []

        while True:
            item = await write_queue.get()
            if item is None:
                if contact_buf or employee_buf or status_buf or addr_updates:
                    await asyncio.get_running_loop().run_in_executor(
                        None, write_contacts_to_db,
                        contact_buf[:], employee_buf[:], status_buf[:], addr_updates[:]
                    )
                    crawled_total  += len(status_buf)
                    contacts_total += len(contact_buf) + len(employee_buf)
                write_queue.task_done()
                break

            c_rows, e_rows, s_row, addr = item
            contact_buf.extend(c_rows)
            employee_buf.extend(e_rows)
            status_buf.append(s_row)
            if addr:
                addr_updates.append((addr, s_row[0]))

            if len(status_buf) >= WRITE_BATCH_SIZE:
                to_c, to_e, to_s, to_a = (
                    contact_buf[:], employee_buf[:], status_buf[:], addr_updates[:]
                )
                contact_buf.clear(); employee_buf.clear()
                status_buf.clear();  addr_updates.clear()

                await asyncio.get_running_loop().run_in_executor(
                    None, write_contacts_to_db, to_c, to_e, to_s, to_a
                )
                crawled_total  += len(to_s)
                contacts_total += len(to_c)

                elapsed = time.monotonic() - t_start
                rate    = crawled_total / elapsed if elapsed else 0
                eta     = (pending - crawled_total) / rate / 60 if rate else 0
                log.info(
                    f"Crawled: {crawled_total + done:,}/{total:,} | "
                    f"Session: {crawled_total:,} | Contacts: {contacts_total:,} | "
                    f"Rate: {rate:.0f}/s | ETA: {eta:.0f}m"
                )
            write_queue.task_done()

    async def producer():
        last_rowid = 0
        while True:
            rows = fetch_batch_from_db(last_rowid)
            if not rows:
                if not await asyncio.get_running_loop().run_in_executor(
                        None, is_metadata_fetching_done):
                    log.info("Waiting for get_metadata.py...")
                    await asyncio.sleep(10)
                    continue
                break
            for r_id, d, c in rows:
                await queue.put((d, c))
                last_rowid = r_id
        for _ in range(CONCURRENCY):
            await queue.put(None)

    from collections import deque
    conn_error_window = deque(maxlen=500)
    pause_event = asyncio.Event()
    pause_event.set()

    async def worker(session):
        while True:
            await pause_event.wait()
            item = await queue.get()
            if item is None:
                queue.task_done()
                break
            domain, category = item
            try:
                result = await crawl_domain(session, domain, category)
                await write_queue.put(result)

            except Exception as e:
                log.error(f"Worker error on {domain}: {e}")
                now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
                await write_queue.put(
                    ([], [], (domain, category, -1, now, 0, 0, 0, str(e)[:100]), None)
                )
            queue.task_done()

    async with aiohttp.ClientSession(
        connector=connector, headers=REQUEST_HEADERS, timeout=timeout
    ) as session:
        prod_task   = asyncio.create_task(producer())
        workers     = [asyncio.create_task(worker(session)) for _ in range(CONCURRENCY)]
        writer_task = asyncio.create_task(db_writer())

        await asyncio.gather(prod_task, *workers)
        await write_queue.put(None)
        await writer_task

    # (pw_executor removed — Playwright is not used in this version)
    log.info("Crawl complete!")
    print_final_stats()


def print_final_stats():
    conn = sqlite3.connect(DB_PATH)
    log.info("\n" + "=" * 55)
    log.info("  CONTACT TYPE BREAKDOWN")
    log.info("=" * 55)
    for etype, cnt in conn.execute("""
        SELECT entity_type, COUNT(*) FROM domain_contacts
        GROUP BY entity_type ORDER BY 2 DESC
    """).fetchall():
        log.info(f"  {etype:<25} {cnt:>10,}")

    log.info("\n" + "=" * 55)
    log.info("  CRAWL STATUS")
    log.info("=" * 55)
    total     = conn.execute("SELECT COUNT(*) FROM domain_crawl_status").fetchone()[0]
    with_data = conn.execute(
        "SELECT COUNT(*) FROM domain_crawl_status WHERE contacts_found > 0"
    ).fetchone()[0]
    pw_count = conn.execute(
        "SELECT COUNT(*) FROM domain_crawl_status WHERE used_playwright = 1"
    ).fetchone()[0]
    pct = with_data * 100 // total if total else 0
    log.info(f"  Domains crawled       : {total:,}")
    log.info(f"  Domains with contacts : {with_data:,} ({pct}%)")
    log.info(f"  Used Playwright       : {pw_count:,}")
    log.info("=" * 55)

    log.info("  Error breakdown:")
    for msg, cnt in conn.execute("""
        SELECT COALESCE(error_msg, 'success'), COUNT(*)
        FROM domain_crawl_status
        GROUP BY 1 ORDER BY 2 DESC LIMIT 10
    """).fetchall():
        log.info(f"    {msg:<40} {cnt:>8,}")
    conn.close()


if __name__ == "__main__":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(run())