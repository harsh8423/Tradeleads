# crawler.py — HTTP fetch + core domain crawl logic

import asyncio
import aiohttp
import socket
import random
import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from config import (
    REQUEST_HEADERS, USER_AGENTS, MAX_REDIRECTS, MAX_BODY_BYTES,
    MAX_PAGES_PER_DOMAIN, CONCURRENCY,
)
from extractor import process_page, strip_head

log = logging.getLogger("contacts")


# ── Windows-safe threaded DNS resolver ────────────────────────────────────────

class ThreadedResolver:
    def __init__(self):
        self._executor = ThreadPoolExecutor(max_workers=CONCURRENCY)

    async def resolve(self, hostname, port=0, family=socket.AF_UNSPEC):
        loop = asyncio.get_running_loop()
        infos = await loop.run_in_executor(
            self._executor,
            lambda: socket.getaddrinfo(
                hostname, port, family=family, type=socket.SOCK_STREAM
            )
        )
        return [
            {"hostname": hostname, "host": info[4][0], "port": info[4][1],
             "family": info[0], "proto": info[2], "flags": 0}
            for info in infos
        ]

    async def close(self):
        self._executor.shutdown(wait=False)


# ── HTTP fetch ────────────────────────────────────────────────────────────────

async def fetch_html(session: aiohttp.ClientSession, url: str) -> tuple[int, str | None]:
    try:
        headers = {**REQUEST_HEADERS, "User-Agent": random.choice(USER_AGENTS)}
        async with session.get(
            url, allow_redirects=True, headers=headers,
            max_redirects=MAX_REDIRECTS, ssl=False
        ) as resp:
            ct = resp.headers.get("Content-Type", "")
            if resp.status == 200 and "html" in ct.lower():
                raw = b""
                async for chunk in resp.content.iter_chunked(8192):
                    raw += chunk
                    if len(raw) >= MAX_BODY_BYTES:
                        break
                return resp.status, strip_head(raw.decode("utf-8", errors="ignore"))
            return resp.status, None
    except asyncio.TimeoutError:
        return -1, None
    except aiohttp.ClientSSLError:
        try:
            http_url = url.replace("https://", "http://", 1)
            async with session.get(
                http_url, allow_redirects=True,
                max_redirects=MAX_REDIRECTS, ssl=False
            ) as resp:
                ct = resp.headers.get("Content-Type", "")
                if resp.status == 200 and "html" in ct.lower():
                    raw = b""
                    async for chunk in resp.content.iter_chunked(8192):
                        raw += chunk
                        if len(raw) >= MAX_BODY_BYTES:
                            break
                    return resp.status, strip_head(raw.decode("utf-8", errors="ignore"))
                return resp.status, None
        except Exception:
            return -4, None
    except asyncio.TimeoutError:
        return -1, None
    except asyncio.CancelledError:
        # Python 3.11 + aiohttp: DNS shield can inject CancelledError on timeout
        # Must be caught here (BaseException subclass, not caught by except Exception)
        return -1, None
    except Exception:
        return -6, None


# ── Core domain crawl ─────────────────────────────────────────────────────────

async def crawl_domain(session: aiohttp.ClientSession, domain: str, category: str,
                       parse_pool=None):
    now = datetime.now(timezone.utc)
    homepage_url = f"https://{domain}"

    all_contacts: list[dict] = []
    seen_contacts: set[tuple] = set()
    pages_crawled = 0
    domain_address = None

    def dedup_contacts(new_contacts):
        fresh = []
        for c in new_contacts:
            key = (c["entity_type"], c["value"].lower())
            if key not in seen_contacts:
                seen_contacts.add(key)
                fresh.append(c)
        return fresh

    # Step 1: fetch homepage
    status, html = await fetch_html(session, homepage_url)
    if html is None:
        return [], [], (domain, category, status, now, 0, 0, f"connection_failed_{status}"), None

    pages_crawled += 1
    loop = asyncio.get_running_loop()
    # Homepage: extract contacts + internal links.
    # wait_for timeout guards against catastrophic regex backtracking on malformed HTML.
    try:
        c_res, _emp, addr, _p_links, candidate_links = await asyncio.wait_for(
            loop.run_in_executor(parse_pool, process_page, html, homepage_url, domain, True),
            timeout=60
        )
    except asyncio.TimeoutError:
        log.warning(f"parse timeout on {domain} (homepage) — skipping")
        c_res, addr, candidate_links = [], None, []
    if addr:
        domain_address = addr
    all_contacts.extend(dedup_contacts(c_res))

    # Step 2: find contact/about sub-pages, fetch them all IN PARALLEL
    sub_urls = [url for _, url in candidate_links[:MAX_PAGES_PER_DOMAIN - 1]]

    if sub_urls:
        # Semaphore limits to 2 concurrent sub-fetches per domain.
        # Prevents all 100 workers from simultaneously saturating the
        # TCPConnector's connection limit and deadlocking.
        sem = asyncio.Semaphore(2)

        async def fetch_sub(url):
            async with sem:
                return await fetch_html(session, url)

        # Fetch sub-pages in parallel (bounded by semaphore)
        sub_results = await asyncio.gather(
            *[fetch_sub(url) for url in sub_urls],
            return_exceptions=True
        )
        parse_tasks = []
        for url, result in zip(sub_urls, sub_results):
            if isinstance(result, Exception) or result[1] is None:
                continue
            _, sub_html = result
            pages_crawled += 1
            parse_tasks.append(asyncio.wait_for(
                loop.run_in_executor(parse_pool, process_page, sub_html, url, domain, False),
                timeout=60
            ))
        if parse_tasks:
            parsed = await asyncio.gather(*parse_tasks, return_exceptions=True)
            for res in parsed:
                if isinstance(res, Exception):
                    continue
                c_res, _emp, sub_addr, _p, _ = res
                if sub_addr and not domain_address:
                    domain_address = sub_addr
                all_contacts.extend(dedup_contacts(c_res))

    # Build DB rows — employees always empty (paused)
    # Truncate value/label/context to keep under B-tree index row limit (2704 bytes)
    contact_rows = [
        (domain, category, homepage_url,
         c["entity_type"], c["value"][:500], (c["label"] or "")[:200],
         (c["context"] or "")[:200], now)
        for c in all_contacts
    ]
    status_row = (
        domain, category, status, now,
        pages_crawled, len(contact_rows), None
    )

    return contact_rows, [], status_row, domain_address
