# services/text_cleaner.py
import re
from typing import Any, Dict, List, Optional

_WS_RE = re.compile(r"[ \t]+")
_MULTI_NL_RE = re.compile(r"\n{2,}")
_URL_RE = re.compile(r"https?://\S+")


def clean_text(text: str) -> str:
    """
    Lightweight cleanup for already-extracted visible text.
    - normalizes whitespace
    - removes excessive blank lines
    - trims noise-like very short lines
    """
    if not text:
        return ""

    # Normalize newlines
    t = text.replace("\r\n", "\n").replace("\r", "\n")

    # Remove very short "noise" lines (e.g., single menu items)
    lines = []
    for line in t.split("\n"):
        line = _WS_RE.sub(" ", line).strip()
        if not line:
            continue
        # keep short lines if they look meaningful (contain punctuation or numbers)
        if len(line) < 25 and not re.search(r"[0-9]|[.,;:()]", line):
            continue
        lines.append(line)

    t = "\n".join(lines)

    # Collapse multiple blank lines
    t = _MULTI_NL_RE.sub("\n\n", t)

    return t.strip()


def _chunk_by_chars(text: str, max_chars: int, overlap: int) -> List[str]:
    """
    Chunk a long text into segments of ~max_chars with optional overlap.
    This is char-based (simple + reliable).
    """
    if not text:
        return []
    if max_chars <= 0:
        return [text]

    t = text.strip()
    chunks: List[str] = []

    start = 0
    n = len(t)
    step = max_chars - max(overlap, 0)
    if step <= 0:
        step = max_chars  # avoid infinite loop

    while start < n:
        end = min(start + max_chars, n)
        chunk = t[start:end].strip()
        if chunk:
            chunks.append(chunk)
        if end >= n:
            break
        start += step

    return chunks


def chunk_text_with_provenance(*args: Any, **kwargs: Any) -> List[Dict]:
    """
    BACKWARD COMPATIBLE with your current version:

    Old style (current file):
        chunk_text_with_provenance(page_url: str, text: str, chunk_size: int = 900) -> list[dict]

    Newer style (used by some app.py versions):
        chunk_text_with_provenance(text: str, max_chars: int = 1800, overlap: int = 150) -> list[dict]
        (page_url omitted)

    Always returns list[dict] with:
      chunk_id, page_url, section_heading, chunk_index, text, char_count, source_url_refs_in_text
    """
    page_url: str = ""
    text: str = ""

    # Detect calling convention
    if len(args) >= 2 and isinstance(args[0], str) and isinstance(args[1], str) and (
        args[0].startswith("http://") or args[0].startswith("https://")
    ):
        # Old style: (page_url, text, chunk_size=?)
        page_url = args[0]
        text = args[1]
        chunk_size = int(kwargs.get("chunk_size", 900))
        max_chars = chunk_size
        overlap = 0
    else:
        # New style: (text, max_chars=?, overlap=?)
        if len(args) >= 1 and isinstance(args[0], str):
            text = args[0]
        else:
            text = kwargs.get("text", "") or ""
        page_url = kwargs.get("page_url", "") or ""
        max_chars = int(kwargs.get("max_chars", 1800))
        overlap = int(kwargs.get("overlap", 150))

    cleaned = clean_text(text)
    if not cleaned:
        return []

    # Prefer paragraph boundaries if possible, then char-chunk the joined text
    paras = [p.strip() for p in re.split(r"\n{2,}", cleaned) if len(p.strip()) > 0]
    joined = "\n\n".join(paras)

    raw_chunks = _chunk_by_chars(joined, max_chars=max_chars, overlap=overlap)

    out: List[Dict] = []
    for idx, ch in enumerate(raw_chunks):
        refs = _URL_RE.findall(ch) if ch else []
        out.append(
            {
                "chunk_id": f"{idx:03d}",
                "page_url": page_url,
                "section_heading": None,
                "chunk_index": idx,
                "text": ch,
                "char_count": len(ch),
                "source_url_refs_in_text": refs,
            }
        )
    return out


def chunk_text(lines, chunk_size=500):
    """
    Groups lines into context chunks (approx token-safe).
    Kept for compatibility with your older code.
    """
    chunks = []
    buffer = ""

    for line in lines:
        if len(buffer) + len(line) < chunk_size:
            buffer += " " + line
        else:
            chunks.append(buffer.strip())
            buffer = line

    if buffer:
        chunks.append(buffer.strip())

    return chunks
