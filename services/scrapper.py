import requests
from bs4 import BeautifulSoup

HEADERS = {
    "User-Agent": "Mozilla/5.0 (AgenticAvatarBot/1.0)"
}

def scrape_website(url: str) -> dict:
    """
    Fetches webpage and extracts clean visible text.
    """
    resp = requests.get(url, headers=HEADERS, timeout=20)
    resp.raise_for_status()

    soup = BeautifulSoup(resp.text, "lxml")

    # Remove noise
    for tag in soup(["script", "style", "noscript", "iframe", "svg"]):
        tag.decompose()

    text = soup.get_text(separator="\n")

    lines = [
        line.strip()
        for line in text.splitlines()
        if len(line.strip()) > 40
    ]

    return {
        "url": url,
        "title": soup.title.string.strip() if soup.title else "",
        "content": lines
    }
