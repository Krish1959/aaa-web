import os
import requests

LIVEAVATAR_BASE_URL = os.getenv("LIVEAVATAR_BASE_URL", "https://app.liveavatar.com").rstrip("/")
LIVEAVATAR_API_KEY = os.getenv("LIVEAVATAR_API_KEY", "").strip()

class LiveAvatarError(Exception):
    pass

def push_context_to_liveavatar(context: dict) -> dict:
    """
    Push context to LiveAvatar contexts endpoint.
    Returns LiveAvatar response JSON (should include an id / url).
    """
    if not LIVEAVATAR_API_KEY:
        return {"skipped": True, "reason": "LIVEAVATAR_API_KEY not set"}

    url = f"{LIVEAVATAR_BASE_URL}/contexts"

    headers = {
        "Authorization": f"Bearer {LIVEAVATAR_API_KEY}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    payload = {
        "title": context.get("title", "")[:200],
        "source_url": context.get("source_url", ""),
        "chunks": context.get("chunks", []),
        "created_at": context.get("created_at", ""),
        "metadata": {
            "project": "aaa-web",
            "stage": 3
        }
    }

    r = requests.post(url, headers=headers, json=payload, timeout=30)

    # Provide good diagnostics
    if r.status_code >= 400:
        raise LiveAvatarError(f"LiveAvatar error {r.status_code}: {r.text[:300]}")

    return r.json()
