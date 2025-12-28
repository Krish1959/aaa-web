"""
services/scraper.py

Lightweight site scraper used by app.py.

Goal:
- Given a start URL, crawl a limited number of internal pages (BFS),
  extract clean visible text per page, and return:
    {
      "base_url": <start_url>,
      "final_url": <final_url after redirects>,
      "pages": [ {page dict...}, ... ],
      "links": [<all discovered internal links>, ...]
    }

Notes:
- Keep it dependency-light: requests + beautifulsoup4 (+ lxml parser via bs4).
- Be resilient: timeouts, bad HTML, redirects, non-HTML responses.
- This module intentionally DOES NOT do aggressive crawling (no JS rendering).
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Set, Tuple
from urllib.parse import urljoin, urlparse, urlunparse, urldefrag

import requests
from bs4 import BeautifulSoup


DEFAULT_UA = (
    "Mozilla/5.0 (compatible; AAA-WebScraper/1.0; +https://example.invalid)"
)

_WHITESPACE_RE = re.compile(r"\s+")


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())


def _clean_whitespace(text: str) -> str:
    return _WHITESPACE_RE.sub(" ", (text or "").strip())


def _strip_fragment(url: str) -> str:
    return urldefrag(url)[0]


def _normalize_url(url: str) -> str:
    """
    Normalize URL for de-dupe:
    - remove fragment
    - strip trailing slash (except root)
    - lower-case scheme/host
    - remove default ports (:80 for http, :443 for https)
    """
    url = _strip_fragment((url or "").strip())
    p = urlparse(url)
    scheme = (p.scheme or "https").lower()
    netloc = (p.netloc or "").lower()

    path = p.path or "/"
    path = re.sub(r"/{2,}", "/", path)

    if netloc.endswith(":80") and scheme == "http":
        netloc = netloc[:-3]
    if netloc.endswith(":443") and scheme == "https":
        netloc = netloc[:-4]

    if path != "/" and path.endswith("/"):
        path = path[:-1]

    return urlunparse((scheme, netloc, path, "", p.query, ""))


def _same_site(a: str, b: str) -> bool:
    pa = urlparse(a)
    pb = urlparse(b)
    return (pa.netloc or "").lower() == (pb.netloc or "").lower()


def _is_http_url(url: str) -> bool:
    try:
        return urlparse(url).scheme in ("http", "https")
    except Exception:
        return False


def _extract_visible_text(soup: BeautifulSoup) -> str:
    """
    Extract user-facing text; remove scripts/styles/noscript/svg/canvas/iframes.
    Also removes common boilerplate containers (nav/footer/header/aside) to reduce noise.
    """
    for tag in soup(["script", "style", "noscript", "svg", "canvas", "iframe"]):
        tag.decompose()

    for tag in soup.find_all(["nav", "footer", "header", "aside"]):
        tag.decompose()

    text = soup.get_text(separator=" ")
    return _clean_whitespace(text)


def _extract_internal_links(
    base_url: str, html: str, final_url: Optional[str] = None
) -> List[str]:
    """
    Extract internal links (same host as final_url if provided, else base_url).
    """
    host_url = final_url or base_url
    soup = BeautifulSoup(html, "lxml")
    found: List[str] = []

    for a in soup.find_all("a", href=True):
        href = (a.get("href") or "").strip()
        if not href:
            continue

        if href.startswith(("mailto:", "tel:", "javascript:", "data:")):
            continue

        abs_url = urljoin(host_url, href)
        abs_url = _normalize_url(abs_url)
        if not _is_http_url(abs_url):
            continue

        if _same_site(host_url, abs_url):
            found.append(abs_url)

    seen: Set[str] = set()
    out: List[str] = []
    for u in found:
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


@dataclass
class PageResult:
    url: str
    final_url: str
    status_code: int
    content_type: str
    title: str
    clean_text: str
    fetched_at: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "url": self.url,
            "final_url": self.final_url,
            "status_code": self.status_code,
            "content_type": self.content_type,
            "title": self.title,
            "clean_text": self.clean_text,
            "text_len": len(self.clean_text or ""),
            "fetched_at": self.fetched_at,
        }


def scrape_page(
    url: str,
    timeout: int = 20,
    user_agent: str = DEFAULT_UA,
) -> Tuple[Optional[PageResult], List[str]]:
    """
    Fetch a single page and return (page_result, internal_links).
    If the response is not HTML or an error occurs, page_result may be None.
    """
    headers = {"User-Agent": user_agent, "Accept": "text/html,application/xhtml+xml"}
    fetched_at = _now_iso()

    try:
        r = requests.get(url, headers=headers, timeout=timeout, allow_redirects=True)
        final_url = _normalize_url(r.url or url)
        status = int(getattr(r, "status_code", 0) or 0)
        ctype = (r.headers.get("content-type") or "").split(";")[0].strip().lower()

        if "html" not in ctype:
            pr = PageResult(
                url=_normalize_url(url),
                final_url=final_url,
                status_code=status,
                content_type=ctype or "",
                title="",
                clean_text="",
                fetched_at=fetched_at,
            )
            return pr, []

        html = r.text or ""
        soup = BeautifulSoup(html, "lxml")

        title = ""
        if soup.title and soup.title.string:
            title = _clean_whitespace(str(soup.title.string))

        clean_text = _extract_visible_text(soup)
        internal_links = _extract_internal_links(url, html, final_url=final_url)

        pr = PageResult(
            url=_normalize_url(url),
            final_url=final_url,
            status_code=status,
            content_type=ctype or "text/html",
            title=title,
            clean_text=clean_text,
            fetched_at=fetched_at,
        )
        return pr, internal_links

    except Exception:
        return None, []


def scrape_site(
    start_url: str,
    max_pages: int = 10,
    timeout: int = 20,
    user_agent: str = DEFAULT_UA,
) -> Dict[str, Any]:
    """
    Crawl internal pages from start_url (BFS) up to max_pages.

    Returns dict with:
      - base_url
      - final_url (after fetching start_url)
      - pages: list of page dicts (includes clean_text)
      - links: all discovered internal links (deduped)
    """
    start_url = _normalize_url(start_url)
    pages: List[Dict[str, Any]] = []
    all_links: List[str] = []

    queue: List[str] = [start_url]
    seen: Set[str] = set()

    final_root_url = start_url

    while queue and len(pages) < max_pages:
        url = queue.pop(0)
        if url in seen:
            continue
        seen.add(url)

        pr, links = scrape_page(url, timeout=timeout, user_agent=user_agent)
        if pr is None:
            continue

        if len(pages) == 0 and pr.final_url:
            final_root_url = pr.final_url

        pages.append(pr.to_dict())

        for u in links:
            if u not in all_links:
                all_links.append(u)

            # Keep queue bounded to avoid exploding on link-heavy pages
            if u not in seen and u not in queue and len(queue) < max_pages * 5:
                queue.append(u)

    return {
        "base_url": start_url,
        "final_url": final_root_url,
        "pages": pages,
        "links": all_links,
        "max_pages": max_pages,
    }


# Backwards-compatible alias: older app.py versions may import scrape_site_map
def scrape_site_map(
    start_url: str,
    max_pages: int = 10,
    timeout: int = 20,
    user_agent: str = DEFAULT_UA,
) -> Dict[str, Any]:
    return scrape_site(
        start_url,
        max_pages=max_pages,
        timeout=timeout,
        user_agent=user_agent,
    )
