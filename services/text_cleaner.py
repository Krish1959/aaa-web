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
