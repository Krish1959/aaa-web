from __future__ import annotations

from collections import deque
from typing import Any, Dict, List, Set, Tuple
from urllib.parse import urljoin, urlparse, urldefrag

import requests
from bs4 import BeautifulSoup


def _same_site(a: str, b: str) -> bool:
    pa = urlparse(a)
    pb = urlparse(b)
    return (pa.scheme, pa.netloc) == (pb.scheme, pb.netloc)


def _normalize(url: str) -> str:
    url = urldefrag(url)[0]
    return url.rstrip("/")


def _extract_text(html: str) -> str:
    soup = BeautifulSoup(html, "lxml")

    # remove junk
    for tag in soup(["script", "style", "noscript", "svg", "iframe"]):
        tag.decompose()

    text = soup.get_text(separator="\n")
    lines = [ln.strip() for ln in text.splitlines()]
    lines = [ln for ln in lines if ln]
    return "\n".join(lines)


def scrape_internal_pages(start_url: str, max_pages: int = 25, timeout: int = 25) -> List[Dict[str, Any]]:
    """
    Breadth-first crawl of INTERNAL pages only (same scheme+host as start_url).
    Returns list of dicts: {url, text}
    """
    start_url = start_url.strip()
    if not start_url:
        return []

    # Ensure scheme
    if not start_url.lower().startswith(("http://", "https://")):
        start_url = "https://" + start_url

    start_url = _normalize(start_url)

    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; aaa-web-scraper/1.0)",
        "Accept": "text/html,application/xhtml+xml",
    }

    q = deque([start_url])
    seen: Set[str] = set([start_url])
    results: List[Dict[str, Any]] = []

    while q and len(results) < max_pages:
        url = q.popleft()
        try:
            r = requests.get(url, headers=headers, timeout=timeout)
            if r.status_code >= 400:
                continue

            ct = r.headers.get("content-type", "")
            if "text/html" not in ct:
                continue

            html = r.text
            text = _extract_text(html)

            results.append({"url": url, "text": text})

            # discover links
            soup = BeautifulSoup(html, "lxml")
            for a in soup.find_all("a", href=True):
                href = a.get("href", "").strip()
                if not href:
                    continue
                # skip mailto/tel/javascript
                low = href.lower()
                if low.startswith(("mailto:", "tel:", "javascript:")):
                    continue

                nxt = urljoin(url + "/", href)
                nxt = _normalize(nxt)

                # internal only
                if not _same_site(start_url, nxt):
                    continue

                if nxt not in seen:
                    seen.add(nxt)
                    q.append(nxt)

        except Exception:
            continue

    return results
