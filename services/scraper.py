import re
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

HEADERS = {"User-Agent": "Mozilla/5.0 (AgenticAvatarBot/1.0)"}


def _clean_text(html: str) -> tuple[str, str, str | None]:
    soup = BeautifulSoup(html, "lxml")

    # Remove noise
    for tag in soup(["script", "style", "noscript", "iframe", "svg"]):
        tag.decompose()

    title = soup.title.string.strip() if soup.title and soup.title.string else ""
    h1 = soup.find("h1")
    h1_text = h1.get_text(" ", strip=True) if h1 else None

    text = soup.get_text(separator="\n")
    lines = [ln.strip() for ln in text.splitlines() if len(ln.strip()) > 40]
    cleaned = "\n".join(lines)

    return cleaned, title, h1_text


def _base_host_key(host: str) -> str:
    """
    v1 heuristic for internal-link matching.
    Example: www.bescon.com.sg -> bescon.com.sg
    """
    host = (host or "").lower().strip(".")
    if host.startswith("www."):
        host = host[4:]
    parts = host.split(".")
    # keep last 3 labels for com.sg style
    return ".".join(parts[-3:]) if len(parts) >= 3 else host


def _is_internal(url: str, host_key: str) -> bool:
    try:
        h = (urlparse(url).hostname or "").lower()
        return h == host_key or h.endswith("." + host_key)
    except Exception:
        return False


def fetch_page(url: str) -> dict:
    r = requests.get(url, headers=HEADERS, timeout=25, allow_redirects=True)
    return {
        "status": r.status_code,
        "final_url": r.url,
        "html": r.text if r.status_code < 400 else "",
    }


def discover_internal_links(html: str, base_url: str, host_key: str) -> list[dict]:
    soup = BeautifulSoup(html, "lxml")
    found = []

    for a in soup.find_all("a", href=True):
        href = (a.get("href") or "").strip()
        if not href:
            continue

        abs_url = urljoin(base_url, href)
        if abs_url.startswith(("mailto:", "tel:", "javascript:")):
            continue

        abs_url = abs_url.split("#")[0]
        if not abs_url.startswith(("http://", "https://")):
            continue

        if _is_internal(abs_url, host_key):
            found.append(
                {
                    "url": abs_url,
                    "anchor_text": (a.get_text(" ", strip=True) or "")[:80] or None,
                    "discovered_on": base_url,
                    "type_hint": "unknown",
                }
            )

    # de-dupe
    seen = set()
    out = []
    for item in found:
        if item["url"] in seen:
            continue
        seen.add(item["url"])
        out.append(item)
    return out


def scrape_site(seed_url: str, max_pages: int = 5, max_chars_per_page: int = 20000) -> dict:
    """
    Scrape seed_url + up to max_pages internal pages.
    Returns pages + deduped internal links + base_host_key for scope.
    """
    seed_host = (urlparse(seed_url).hostname or "").lower()
    host_key = _base_host_key(seed_host)

    queue = [seed_url]
    visited = set()

    pages = []
    all_links = []

    while queue and len(pages) < max_pages:
        url = queue.pop(0)
        if url in visited:
            continue
        visited.add(url)

        fetched = fetch_page(url)
        status = fetched["status"]
        final_url = fetched["final_url"]
        html = fetched["html"]

        if status >= 400 or not html:
            pages.append(
                {
                    "page_id": "",
                    "url": url,
                    "final_url": final_url,
                    "title": "",
                    "h1": None,
                    "canonical_url": None,
                    "content_hash": "",
                    "char_count": 0,
                    "word_count": 0,
                    "scraped_at_utc": "",
                    "status": status,
                    "clean_text": "",
                }
            )
            continue

        cleaned, title, h1 = _clean_text(html)
        cleaned = cleaned[:max_chars_per_page]
        word_count = len(re.findall(r"\w+", cleaned))

        pages.append(
            {
                "page_id": "",
                "url": url,
                "final_url": final_url,
                "title": title,
                "h1": h1,
                "canonical_url": None,
                "content_hash": "",
                "char_count": len(cleaned),
                "word_count": word_count,
                "scraped_at_utc": "",
                "status": status,
                "clean_text": cleaned,
            }
        )

        found = discover_internal_links(html, final_url, host_key)
        all_links.extend(found)

        for item in found:
            u = item["url"]
            if u not in visited and u not in queue and len(queue) < 200:
                queue.append(u)

    # dedupe links
    seen = set()
    links = []
    for item in all_links:
        if item["url"] in seen:
            continue
        seen.add(item["url"])
        links.append(item)

    return {
        "base_host_key": host_key,
        "pages": pages,
        "links": links,
    }
