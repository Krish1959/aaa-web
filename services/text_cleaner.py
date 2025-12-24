import re

def chunk_text_with_provenance(page_url: str, text: str, chunk_size: int = 900) -> list[dict]:
    # simple paragraph split
    paras = [p.strip() for p in re.split(r"\n{2,}|\n", text) if len(p.strip()) > 40]
    chunks = []
    buf = ""
    idx = 0

    for p in paras:
        if len(buf) + len(p) < chunk_size:
            buf += (" " + p)
        else:
            chunks.append({
                "chunk_id": f"{idx:03d}",
                "page_url": page_url,
                "section_heading": None,
                "chunk_index": idx,
                "text": buf.strip(),
                "char_count": len(buf.strip()),
                "source_url_refs_in_text": []
            })
            idx += 1
            buf = p

    if buf.strip():
        chunks.append({
            "chunk_id": f"{idx:03d}",
            "page_url": page_url,
            "section_heading": None,
            "chunk_index": idx,
            "text": buf.strip(),
            "char_count": len(buf.strip()),
            "source_url_refs_in_text": []
        })

    return chunks

def chunk_text(lines, chunk_size=500):
    """
    Groups lines into context chunks (approx token-safe).
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

