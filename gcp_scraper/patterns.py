# patterns.py — All regex patterns and small pure helper functions
# No I/O, no DB, no HTTP — pure text processing.

import re
import threading
import warnings
import spacy
from bs4 import MarkupResemblesLocatorWarning

warnings.filterwarnings("ignore", module="bs4")
warnings.filterwarnings("ignore", category=MarkupResemblesLocatorWarning)

# ── Email ─────────────────────────────────────────────────────────────────────
EMAIL_RE = re.compile(
    r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}",
    re.I
)

SPACED_EMAIL_RE = re.compile(
    r"[a-zA-Z0-9._%+\-]+\s*@\s*[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}",
    re.I
)

# TLD validation: EMAIL_RE already enforces [a-zA-Z]{2,}; this is a guard.
def _has_valid_tld(email: str) -> bool:
    tld = email.rsplit(".", 1)[-1]
    return len(tld) >= 2

ZERO_WIDTH_CHARS = re.compile(r"[\u200b\u200c\u200d\u200e\u200f\uFEFF\u00ad]")

def clean_zero_width(text: str) -> str:
    return ZERO_WIDTH_CHARS.sub("", text)

def extract_spaced_emails(text: str) -> list[str]:
    found = []
    for m in SPACED_EMAIL_RE.finditer(text):
        cleaned = re.sub(r"\s*@\s*", "@", m.group()).lower().strip()
        found.append(cleaned)
    return found

# ── Phone ─────────────────────────────────────────────────────────────────────
# PHONE_RE has been removed — use extract_phones_robust() in extractor.py
# which uses Google's phonenumbers (libphonenumber) for validated extraction.

def clean_phone(raw: str) -> str | None:
    """Used only for tel: href parsing where the string is already phone-like."""
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

# ── Social media ──────────────────────────────────────────────────────────────
LINKEDIN_RE  = re.compile(r"https?://(?:www\.)?linkedin\.com/(?:in|company|pub)/[^\s\"'<>]+", re.I)
TWITTER_RE   = re.compile(r"https?://(?:www\.)?(?:twitter|x)\.com/[a-zA-Z0-9_]+", re.I)
FACEBOOK_RE  = re.compile(r"https?://(?:www\.)?facebook\.com/[^\s\"'<>]+", re.I)
INSTAGRAM_RE = re.compile(r"https?://(?:www\.)?instagram\.com/[^\s\"'<>]+", re.I)
YOUTUBE_RE   = re.compile(r"https?://(?:www\.)?youtube\.com/(?:c/|channel/|user/)[^\s\"'<>]+", re.I)
WHATSAPP_RE  = re.compile(r"https?://(?:api\.)?whatsapp\.com/send[^\s\"'<>]*", re.I)

# ── Address ───────────────────────────────────────────────────────────────────
ADDRESS_KEYWORDS = re.compile(
    r"\b(?:address|location|headquarter|hq|office|registered.office|our.office|find.us|where.we.are)\b",
    re.I
)

# ── Page priority scoring ─────────────────────────────────────────────────────
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
    (re.compile(r"about[_\-]?us",           re.I), 10),
    (re.compile(r"\babout\b",               re.I), 8),
    (re.compile(r"who[_\-]?we[_\-]?are",    re.I), 9),
    (re.compile(r"management",              re.I), 8),
    (re.compile(r"leadership",              re.I), 8),
    (re.compile(r"\bboard\b",               re.I), 7),
    (re.compile(r"director",                re.I), 7),
    (re.compile(r"executive",               re.I), 7),
    (re.compile(r"founder",                 re.I), 7),
]

# ── Employee page URL match ───────────────────────────────────────────────────
EMPLOYEE_PAGE_RE = re.compile(
    r"/("
    r"team|our[-_]?team|meet[-_]?the[-_]?team|meet[-_]?our[-_]?team|"
    r"team[-_]?members?|staff|our[-_]?staff|people|our[-_]?people|"
    r"about|about[-_]?us|about[-_]?the[-_]?company|"
    r"who[-_]?we[-_]?are|company[-_]?profile|"
    r"contact|contacts|contact[-_]?us|"
    r"leadership|our[-_]?leadership|leadership[-_]?team|"
    r"management|our[-_]?management|management[-_]?team|"
    r"executives?|executive[-_]?team|founders?|"
    r"directors?|board[-_]?of[-_]?directors?|board|"
    r"partners?|profile|profiles|bio|bios|biography|"
    r"member|members|experts?|specialists?|consultants?|advisors?|"
    r"imprint|impressum|office|offices|locations?"
    r")(/|$|\?|-|\.|#)",
    re.I
)

# ── Name / title validation ───────────────────────────────────────────────────
# _NAME_STRICT has been removed — replaced by spaCy PERSON NER in is_person_name().
# Supports non-Western names (Indian, Arabic, Chinese, etc.) which _NAME_STRICT rejected.

_NAME_REJECTS = re.compile(
    r"[0-9@#$%&*+=|<>{}/\\,;:!?]|"
    r"\b(our|the|and|for|with|meet|"
    r"new|old|north|south|east|west)\b",
    re.I
)

# Load spaCy model once at module level — cheap to call, expensive to load.
# Disable unneeded components for speed (we only need NER).
try:
    _nlp = spacy.load("en_core_web_sm", disable=["parser", "tagger", "lemmatizer"])
except OSError:
    _nlp = None  # graceful degradation — is_person_name() falls back to heuristics

# Semaphore: cap concurrent spaCy calls. Raised to 16 to match 32-thread executor
# without leaving 26 threads always blocked (was 6 before, too conservative).
_NLP_SEM = threading.Semaphore(16)

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

NOT_A_NAME_WORDS = re.compile(
    r"\b(?:FAQ|Question|Answer|Service|Solution|Option|Feature|Fleet|"
    r"Luxury|Premium|Package|Plan|Pricing|About|Contact|"
    r"Mission|Vision|Policy|Term|Condition|Privacy|Legal|"
    r"Overview|Introduction|Summary|Detail|Information|"
    r"Product|Category|Section|Chapter|Frequently|Asked)\b",
    re.I
)

# Card-level filter signals — module-level to avoid recompiling in hot loop
_LOCATION_SIGNALS = re.compile(
    r"\b(?:office|branch|location|headquarter|address|"
    r"warehouse|facility|depot|terminal|port|city|"
    r"country|region|zone|area|district)\b", re.I
)
_SERVICE_SIGNALS = re.compile(
    r"\b(?:solution|service|product|feature|package|"
    r"option|plan|pricing|benefit|advantage)\b", re.I
)

# ── Helpers ───────────────────────────────────────────────────────────────────
def is_person_name(text: str) -> bool:
    """NER-backed person name validator.

    Uses spaCy en_core_web_sm PERSON entity recognition, which handles
    Western, Indian, Arabic, Chinese and other non-Western name patterns.
    Falls back to conservative heuristics if the model is unavailable.
    """
    text = text.strip()
    if text.isupper():
        text = text.title()

    # Fast structural pre-filter before hitting NLP
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
        # Fallback: basic capitalisation heuristic
        return all(w[0].isupper() for w in words if w)

    with _NLP_SEM:
        doc = _nlp(text)
    return any(ent.label_ == "PERSON" for ent in doc.ents)


# Backward-compat alias — callers using the old name still work
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

def _decode_cf_email(encoded: str) -> str | None:
    """Decode Cloudflare XOR-obfuscated email hex string."""
    try:
        b = bytes.fromhex(encoded)
        key = b[0]
        return "".join(chr(c ^ key) for c in b[1:])
    except Exception:
        return None

def score_url(url: str) -> int:
    from urllib.parse import urlparse
    path = urlparse(url).path
    for pattern, score in PAGE_PRIORITY:
        if pattern.search(path):
            return score
    return 0
