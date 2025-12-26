import os
import requests


class LiveAvatarError(Exception):
    pass


def _normalize_base_url(raw: str) -> str:
    """
    Users often paste https://app.liveavatar.com in env vars (UI host).
    The API host is typically https://api.liveavatar.com.
    We'll auto-normalize to reduce configuration mistakes.
    """
    base = (raw or "").strip().rstrip("/")
    if not base:
        base = "https://api.liveavatar.com"

    # If user provided the app host, switch to api host automatically
    if "app.liveavatar.com" in base:
        base = base.replace("app.liveavatar.com", "api.liveavatar.com")

    return base


LIVEAVATAR_BASE_URL = _normalize_base_url(os.getenv("LIVEAVATAR_BASE_URL", "https://api.liveavatar.com"))
LIVEAVATAR_API_KEY = os.getenv("LIVEAVATAR_API_KEY", "").strip()


def create_context(name: str, opening_intro: str, links: list[str], full_prompt: str) -> dict:
    """
    Create a LiveAvatar/HeyGen context.

    Endpoint:
      POST {LIVEAVATAR_BASE_URL}/v1/contexts

    Payload:
      {
        "name": "...",
        "opening_intro": "...",
        "links": [...],
        "full_prompt": "..."
      }
    """
    if not LIVEAVATAR_API_KEY:
        raise LiveAvatarError("LIVEAVATAR_API_KEY is not set")

    url = f"{LIVEAVATAR_BASE_URL}/v1/contexts"

    headers = {
        "Authorization": f"Bearer {LIVEAVATAR_API_KEY}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    payload = {
        "name": name,
        "opening_intro": opening_intro,
        "links": links or [],
        "full_prompt": full_prompt,
    }

    r = requests.post(url, headers=headers, json=payload, timeout=40)

    if r.status_code >= 400:
        # return enough text to diagnose, but not too huge
        msg = (r.text or "").strip()
        raise LiveAvatarError(f"Create context failed ({r.status_code}): {msg[:800]}")

    return r.json()
