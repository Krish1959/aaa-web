# services/scraper.py

from __future__ import annotations

from collections import deque
from typing import Dict, List, Set, Tuple
from urllib.parse import urljoin, urlparse, urldefrag

import requests
from bs4 import BeautifulSoup


def _same_site(a: str, b: str) -> bool:
    try:
        pa = urlparse(a)
        pb = urlparse(b)
        return (pa.scheme in ("http", "https")) and (pb.scheme in ("http", "https")) and (pa.netloc == pb.netloc)
    except Exception:
        return False


def _normalize(url: str) -> str:
    u = (url or "").strip()
    u, _frag = urldefrag(u)
    return u.rstrip("/")


def _extract_links(base_url: str, html: str) -> List[str]:
    soup = BeautifulSoup(html or "", "lxml")
    out: List[str] = []
    for a in soup.select("a[href]"):
        href = (a.get("href") or "").strip()
        if not href or href.startswith("#"):
            continue
        if href.startswith("mailto:") or href.startswith("tel:") or href.startswith("javascript:"):
            continue
        abs_url = urljoin(base_url, href)
        abs_url = _normalize(abs_url)
        if abs_url:
            out.append(abs_url)
    return out


def scrape_site(start_url: str, max_pages: int = 25, timeout: int = 20) -> Tuple[List[Dict[str, str]], List[str]]:
    """
    Crawl internal pages starting from start_url. Returns:
      pages: [{url,title,text}]
      internal_urls: [url1,url2,...]  (unique, ordered by discovery)
    """
    start_url = _normalize(start_url)
    if not start_url.startswith("http"):
        start_url = "https://" + start_url

    q: deque[str] = deque([start_url])
    seen: Set[str] = set()
    internal_urls: List[str] = []
    pages: List[Dict[str, str]] = []

    while q and len(pages) < max_pages:
        url = _normalize(q.popleft())
        if not url or url in seen:
            continue
        seen.add(url)

        # only same site
        if not _same_site(start_url, url):
            continue

        try:
            r = requests.get(url, timeout=timeout, headers={"User-Agent": "aaa-web-scraper"})
            if r.status_code >= 400:
                continue

            html = r.text or ""
            soup = BeautifulSoup(html, "lxml")

            # Basic title
            title = (soup.title.get_text(" ", strip=True) if soup.title else "").strip()

            # Remove scripts/styles/nav/footer to reduce noise
            for tag in soup(["script", "style", "noscript", "header", "footer", "nav"]):
                try:
                    tag.decompose()
                except Exception:
                    pass

            text = soup.get_text("\n", strip=True)

            pages.append({"url": url, "title": title, "text": text})
            internal_urls.append(url)

            # enqueue more internal links
            for link in _extract_links(url, html):
                if link and link not in seen and _same_site(start_url, link):
                    q.append(link)

        except Exception:
            continue

    # Ensure unique internal_urls while preserving order
    dedup: List[str] = []
    s: Set[str] = set()
    for u in internal_urls:
        if u not in s:
            s.add(u)
            dedup.append(u)

    return pages, dedup
