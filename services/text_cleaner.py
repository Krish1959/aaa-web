# services/text_cleaner.py

from __future__ import annotations

import re
from typing import Dict, List, Sequence, Tuple


_WS_RE = re.compile(r"[ \t\r\f\v]+")
_MULTI_NL_RE = re.compile(r"\n{3,}")


def clean_text(text: str) -> str:
    """
    Conservative cleanup:
      - normalize whitespace
      - keep newlines meaningful
      - remove very short noise lines
    """
    if not text:
        return ""

    # normalize spaces per line
    lines = []
    for raw in text.splitlines():
        s = _WS_RE.sub(" ", raw).strip()
        # drop ultra-noisy tiny lines
        if len(s) <= 1:
            continue
        lines.append(s)

    out = "\n".join(lines)
    out = _MULTI_NL_RE.sub("\n\n", out).strip()
    return out


def chunk_text_with_provenance(text: str, max_chars: int = 2400) -> List[str]:
    """
    Split large text into chunks (by paragraphs) roughly <= max_chars.
    """
    if not text:
        return []

    paras = [p.strip() for p in text.split("\n\n") if p.strip()]
    chunks: List[str] = []
    buf: List[str] = []
    size = 0

    for p in paras:
        if not p:
            continue
        p_len = len(p)

        # If a single paragraph is huge, hard-split it
        if p_len > max_chars:
            if buf:
                chunks.append("\n\n".join(buf).strip())
                buf, size = [], 0
            # split long paragraph into slices
            for i in range(0, p_len, max_chars):
                chunks.append(p[i : i + max_chars].strip())
            continue

        if size + p_len + (2 if buf else 0) <= max_chars:
            buf.append(p)
            size += p_len + (2 if buf else 0)
        else:
            if buf:
                chunks.append("\n\n".join(buf).strip())
            buf = [p]
            size = p_len

    if buf:
        chunks.append("\n\n".join(buf).strip())

    return chunks
