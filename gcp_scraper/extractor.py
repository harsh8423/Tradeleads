# extractor.py — All HTML extraction logic
# Depends only on: patterns.py (no DB, no HTTP)

import re
import html as html_module
from urllib.parse import urljoin, urlparse
from bs4 import BeautifulSoup
import phonenumbers
from phonenumbers import (
    PhoneNumberFormat, format_number, is_valid_number, NumberParseException
)

from patterns import (
    EMAIL_RE, SPACED_EMAIL_RE, ZERO_WIDTH_CHARS,
    LINKEDIN_RE, TWITTER_RE, FACEBOOK_RE, INSTAGRAM_RE, YOUTUBE_RE, WHATSAPP_RE,
    ADDRESS_KEYWORDS, EMPLOYEE_PAGE_RE,
    TITLE_KEYWORDS, _LOCATION_SIGNALS, _SERVICE_SIGNALS,
    clean_zero_width, extract_spaced_emails, clean_phone,
    is_strict_person_name, is_strict_title, _decode_cf_email, score_url,
    _has_valid_tld, PAGE_PRIORITY,
    _nlp, _NLP_SEM,
)

PERSONAL_EMAIL_DOMAINS = {
    "gmail.com", "yahoo.com", "hotmail.com", "outlook.com", "icloud.com",
    "me.com", "mac.com", "protonmail.com", "proton.me", "aol.com", "zoho.com"
}

# Region hints ordered by relevance for exim/logistics targets.
# phonenumbers tries each as a fallback for numbers without a country code.
_PHONE_REGIONS = ["IN", "AE", "SG", "MY", "GB", "US"]
_REGION_ORDER  = _PHONE_REGIONS  # alias used by extract_phones_robust

# Candidate pre-filter: fast single-pass regex to find digit clusters.
# Each candidate is then validated with phonenumbers.parse() — much faster
# than running PhoneNumberMatcher over full page text × 6 regions.
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


def extract_phones_robust(text: str) -> list[str]:
    """Extract validated phone numbers using libphonenumber.

    Uses a fast regex pre-filter to find candidates (single O(n) pass),
    then validates each with phonenumbers.parse() + is_valid_number().
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
                    break
            except NumberParseException:
                pass
    return list(found.keys())


# ── Root domain helper ────────────────────────────────────────────────────────

def get_root_domain(url_or_domain: str) -> str:
    val = url_or_domain.split("://")[-1].split("/")[0]
    parts = val.split(".")
    if len(parts) > 2 and parts[-2] in ("co", "com", "org", "net", "edu", "gov"):
        return ".".join(parts[-3:])
    return ".".join(parts[-2:]) if len(parts) >= 2 else val


def is_valid_employee_email(email: str, company_domain: str) -> bool:
    """Accept if email matches company domain OR is a known personal provider."""
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
    return root_email == root_company or root_email in PERSONAL_EMAIL_DOMAINS


# ── Internal link extraction ──────────────────────────────────────────────────

def extract_internal_links(html: str, base_url: str, soup=None) -> list[tuple[int, str]]:
    """Extract scored internal links. Accepts a pre-built soup to avoid double-parse."""
    if soup is None:
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


# ── Email extraction ──────────────────────────────────────────────────────────

def extract_all_attribute_text(soup) -> str:
    attrs_to_check = ["alt", "title", "placeholder", "data-email", "data-mail", "content", "aria-label"]
    parts = []
    for tag in soup.find_all(True):
        for attr in attrs_to_check:
            val = tag.get(attr, "")
            if val and isinstance(val, str):
                parts.append(val)
    return " ".join(parts)


def extract_all_emails(html: str, soup, full_text: str | None = None) -> list[tuple[str, str]]:
    found: dict[str, str] = {}

    # 1. mailto: links
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if href.lower().startswith("mailto:"):
            addr = href[7:].split("?")[0].strip().lower()
            addr = ZERO_WIDTH_CHARS.sub("", addr)
            if EMAIL_RE.match(addr):
                found[addr] = "mailto-link"

    # 2. Cloudflare protected emails
    for a in soup.find_all("a", href=True):
        if "email-protection" in a.get("href", ""):
            decoded = _decode_cf_email(a.get("data-cfemail", ""))
            if decoded and EMAIL_RE.match(decoded):
                found[decoded.lower()] = "cloudflare"

    # 3. HTML entity decoded raw scan
    def clean_html_for_scanning(h: str) -> str:
        h = re.sub(r"<script[^>]*>.*?</script>", " ", h, flags=re.S | re.I)
        h = re.sub(r"<style[^>]*>.*?</style>", " ", h, flags=re.S | re.I)
        return h

    decoded_html = html_module.unescape(clean_html_for_scanning(html))
    for m in EMAIL_RE.finditer(decoded_html):
        email = m.group().lower()
        if not re.search(r"\.(png|jpg|gif|svg|css|js|woff)$", email):
            found.setdefault(email, "html-entity-decoded")

    # 4. Visible text scan (use pre-computed full_text to avoid double get_text())
    _full_text = full_text if full_text is not None else clean_zero_width(soup.get_text(separator=" ", strip=True))
    for m in EMAIL_RE.finditer(_full_text):
        email = m.group().lower()
        if not re.search(r"\.(png|jpg|gif|svg|css|js|woff)$", email):
            found.setdefault(email, "text")

    # 5. [at] / [dot] obfuscation — spelled-out only (not literal @ or . which EMAIL_RE handles)
    # local ≥4 chars, domain ≥3 chars, TLD not a common English word.
    _JUNK_TLDS = re.compile(
        r"^(if|or|the|an|in|it|is|be|as|at|by|do|go|of|on|up|us|we|so|"
        r"what|which|this|that|them|than|then|from|with|have|your|their|"
        r"train|track|route|book|search|plan|find|link|"
        r"want|need|help|call|ask|see|let|get|put|set|run|use|try|say)$",
        re.I
    )
    AT_DOT_RE = re.compile(
        r"(?<![\w@])([a-zA-Z0-9._%+\-]{4,})"
        r"\s+[\[\(]?\s*(?:at|AT)\s*[\]\)]?\s+"
        r"([a-zA-Z0-9][a-zA-Z0-9.\-]{2,})"
        r"\s*[\[\(]?\s*(?:dot|DOT)\s*[\]\)]?\s*"
        r"([a-zA-Z]{2,6})(?![a-zA-Z])"
    )
    for m in AT_DOT_RE.finditer(_full_text):
        local_part, domain_part, tld = m.group(1), m.group(2), m.group(3)
        if _JUNK_TLDS.match(tld):
            continue
        email = f"{local_part}@{domain_part}.{tld}".lower()
        if EMAIL_RE.match(email):
            found.setdefault(email, "at-dot-obfuscation")

    # 6. Spaced-@ obfuscation
    for email in extract_spaced_emails(_full_text):
        found.setdefault(email, "spaced-at")

    # 7. HTML attribute values
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


# ── Email/phone from single soup element ──────────────────────────────────────

def _best_email_from_soup(tag) -> str | None:
    for a in tag.find_all("a", href=True):
        href = a["href"]
        if href.lower().startswith("mailto:"):
            addr = href[7:].split("?")[0].strip().lower()
            if EMAIL_RE.match(addr):
                return addr

    for a in tag.find_all("a", href=True):
        if "email-protection" in a.get("href", ""):
            decoded = _decode_cf_email(a.get("data-cfemail", ""))
            if decoded and EMAIL_RE.match(decoded):
                return decoded.lower()

    for t in tag.find_all(True):
        for attr in ("data-email", "data-mail", "data-contact"):
            val = t.get(attr, "").strip()
            if val and EMAIL_RE.match(val):
                return val.lower()

    text = tag.get_text(separator=" ")
    m = re.search(
        r"([a-zA-Z0-9._%+\-]+)\s*[\[\(]?\s*(?:at|@)\s*[\]\)]?\s*"
        r"([a-zA-Z0-9.\-]+)\s*[\[\(]?\s*(?:dot|\.)\s*[\]\)]?\s*([a-zA-Z]{2,})",
        text, re.I
    )
    if m:
        return f"{m.group(1)}@{m.group(2)}.{m.group(3)}".lower()

    em = EMAIL_RE.search(text)
    if em:
        val = em.group().lower()
        if not re.search(r"\.(png|jpg|gif|svg|css|js|woff)$", val):
            return val
    return None


def _best_phone_from_soup(tag) -> str | None:
    """Extract best phone as E.164 from a soup element.

    Uses extract_phones_robust for all paths so output is always E.164,
    matching the format used by extract_phones_robust in page-level scanning.
    This prevents the same number appearing twice with different formats.
    """
    # 1. tel: href (validate + E.164 via phonenumbers)
    for a in tag.find_all("a", href=True):
        if a["href"].lower().startswith("tel:"):
            phones = extract_phones_robust(a["href"][4:].strip())
            if phones:
                return phones[0]

    # 2. data-phone / data-tel attributes
    for t in tag.find_all(True):
        for attr in ("data-phone", "data-tel", "data-mobile"):
            val = t.get(attr, "").strip()
            if val:
                phones = extract_phones_robust(val)
                if phones:
                    return phones[0]

    # 3. Scan element text
    phones = extract_phones_robust(tag.get_text(separator=" "))
    return phones[0] if phones else None


# ── Footer targeted extraction ────────────────────────────────────────────────

def extract_footer_contacts(soup, add_fn):
    footer_selectors = [
        "footer", "[class*='footer']", "[id*='footer']",
        "[class*='contact']", "[id*='contact']",
        "[class*='widget']", "[class*='bottom']", "[id*='bottom']"
    ]
    seen_els = set()
    footer_elements = []
    for sel in footer_selectors:
        for el in soup.select(sel):
            el_id = id(el)
            if el_id not in seen_els:
                seen_els.add(el_id)
                footer_elements.append(el)

    for el in footer_elements:
        text = el.get_text(separator=" ", strip=True)
        for a in el.find_all("a", href=True):
            href = a["href"].strip()
            if href.lower().startswith("mailto:"):
                addr = href[7:].split("?")[0].strip().lower()
                if EMAIL_RE.match(addr):
                    add_fn("email", addr, context="footer-mailto")
            elif href.lower().startswith("tel:"):
                # E.164 via phonenumbers (consistent with page-level scan)
                phones = extract_phones_robust(href[4:].strip())
                for e164 in phones:
                    add_fn("phone", e164, context="footer-tel")
        for e164 in extract_phones_robust(text):
            add_fn("phone", e164, context="footer")


# ── Employee extraction ───────────────────────────────────────────────────────

def _extract_employees(soup, add_employee_fn, page_url: str, profile_links: list):
    base_domain = urlparse(page_url).netloc

    page_email_idx: dict[str, str] = {}
    page_phone_idx: dict[str, str] = {}
    seen_profile_urls: set[str] = set()

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

    # ── spaCy batch NER for all card headings ─────────────────────────────────
    # Collect every candidate heading text from pre-filtered cards upfront,
    # then run _nlp.pipe() in one batched call instead of 60 individual calls.
    # Cards not passing keyword/location/service filters are still excluded first.

    pre_filtered_cards = []
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
        headings = card.find_all(["h1", "h2", "h3", "h4", "h5", "strong", "b"])
        if not headings:
            continue
        pre_filtered_cards.append((card, headings))

    if not pre_filtered_cards:
        return

    # Gather all heading texts for batch NER
    all_heading_texts = [
        h.get_text(strip=True)
        for _, headings in pre_filtered_cards
        for h in headings
    ]

    if _nlp is not None:
        with _NLP_SEM:
            docs = list(_nlp.pipe(all_heading_texts, batch_size=50))
        person_flags = [any(e.label_ == "PERSON" for e in doc.ents) for doc in docs]
    else:
        person_flags = [all(w[0].isupper() for w in t.split() if w)
                        for t in all_heading_texts]

    # Map batch results back: track position across cards
    heading_offset = 0
    for card, headings in pre_filtered_cards:
        n_headings = len(headings)
        card_flags = person_flags[heading_offset: heading_offset + n_headings]
        heading_offset += n_headings

        name = None
        name_idx = 0
        for idx, (h, is_person) in enumerate(zip(headings, card_flags)):
            # Apply the same pre-filters inline
            text = h.get_text(strip=True)
            if not text:
                continue
            _words = text.split()
            if not 2 <= len(_words) <= 5 or len(text) > 60:
                continue
            if is_person:
                name = text
                name_idx = idx
                break
        if not name:
            continue

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

        email = _best_email_from_soup(card)
        phone = _best_phone_from_soup(card)
        lm = LINKEDIN_RE.search(str(card))
        linkedin = lm.group() if lm else None

        if not email or not phone:
            for k in [name.lower()] + name.lower().split():
                if not email and k in page_email_idx:
                    email = page_email_idx[k]
                if not phone and k in page_phone_idx:
                    phone = page_phone_idx[k]
                if email and phone:
                    break

        if not email or not phone:
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


# ── Full page extraction (main entry point) ───────────────────────────────────

def extract_contacts(html: str, page_url: str, domain: str):
    """
    Returns (contact_list, employee_list, address_str, profile_links).
    All four values always returned.
    """
    contacts: list[dict] = []
    employees: list[dict] = []
    seen_contacts: set[tuple] = set()
    seen_employee_names: set[str] = set()
    profile_links: list[tuple] = []

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

    # Parse once — reuse soup for all downstream calls to avoid double lxml parse
    soup = BeautifulSoup(html, "lxml")
    # Compute full_text once — passed into extract_all_emails to avoid 2nd get_text()
    full_text = clean_zero_width(soup.get_text(separator=" ", strip=True))

    for email_val, ctx in extract_all_emails(html, soup, full_text):
        add("email", email_val, context=ctx)

    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if href.lower().startswith("tel:"):
            # E.164 via phonenumbers so tel: links are consistent with page scan
            phones = extract_phones_robust(href[4:].strip())
            for e164 in phones:
                add("phone", e164, context="tel link")

    for e164 in extract_phones_robust(full_text):
        add("phone", e164, context="page-text")

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

    extract_footer_contacts(soup, add)

    if EMPLOYEE_PAGE_RE.search(page_url) or score_url(page_url) >= 8:
        _extract_employees(soup, add_employee, page_url, profile_links)

    return contacts, employees, address_str, profile_links, soup  # soup returned for reuse


def extract_profile_contacts(html: str):
    soup = BeautifulSoup(html, "lxml")
    return _best_email_from_soup(soup), _best_phone_from_soup(soup)


def strip_head(html: str) -> str:
    """Remove <head>...</head> to cut HTML size before parsing."""
    import re as _re
    body_start = _re.search(r"<body[\s>]", html, _re.I)
    if body_start:
        return html[body_start.start():]
    head_end = _re.search(r"</head\s*>", html, _re.I)
    if head_end:
        return html[head_end.end():]
    return html


# ── Subprocess-safe wrapper (for ProcessPoolExecutor) ─────────────────────────

def process_page(html: str, page_url: str, domain: str, is_homepage: bool = False):
    """Parse a page entirely inside a subprocess (no unpicklable objects cross
    the process boundary).

    Returns (contacts, employees, address, profile_links, internal_links).
    ``internal_links`` is populated only when ``is_homepage=True``.
    """
    contacts, employees, address, profile_links, soup = extract_contacts(
        html, page_url, domain
    )
    internal_links = []
    if is_homepage:
        internal_links = extract_internal_links(html, page_url, soup)
    return contacts, employees, address, profile_links, internal_links

