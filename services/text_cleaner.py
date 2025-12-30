from __future__ import annotations

import re
from typing import Dict, List, Sequence, Tuple


def clean_text(text: str) -> str:
    """
    Basic cleanup: collapse whitespace, keep newlines meaningful.
    """
    if not text:
        return ""
    t = text.replace("\r\n", "\n").replace("\r", "\n")
    # collapse repeated spaces/tabs
    t = re.sub(r"[ \t]+", " ", t)
    # collapse excessive blank lines
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t.strip()


def chunk_text_with_provenance(items: Sequence[Tuple[str, str]], chunk_size_words: int = 220) -> List[Dict[str, str]]:
    """
    items: [(url, cleaned_text)]
    returns: [{chunk_id, url, text}]
    chunk_id: 000, 001, ...
    """
    chunks: List[Dict[str, str]] = []
    idx = 0

    for url, text in items:
        words = text.split()
        if not words:
            continue

        start = 0
        while start < len(words):
            end = min(start + chunk_size_words, len(words))
            piece = " ".join(words[start:end]).strip()
            if piece:
                chunks.append(
                    {
                        "chunk_id": f"{idx:03d}",
                        "url": url,
                        "text": piece,
                    }
                )
                idx += 1
            start = end

    return chunks
