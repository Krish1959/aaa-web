# services/liveavatar.py
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple, Union
from uuid import UUID, uuid4

import requests


def _is_uuid(s: str) -> bool:
    try:
        UUID(str(s))
        return True
    except Exception:
        return False


def _normalize_base_url(base_url: str) -> str:
    base = (base_url or "https://api.liveavatar.com").strip()
    if not base:
        base = "https://api.liveavatar.com"
    return base.rstrip("/")


def _safe_json(resp: requests.Response) -> Dict[str, Any]:
    try:
        data = resp.json()
        if isinstance(data, dict):
            data.setdefault("status_code", resp.status_code)
            return data
        return {"code": -1, "data": data, "message": None, "status_code": resp.status_code}
    except Exception:
        return {"code": -1, "data": None, "message": resp.text, "status_code": resp.status_code}


def _build_links(
    links: Optional[List[Union[str, Dict[str, Any]]]],
    default_faq: str = "Q&A",
) -> Optional[List[Dict[str, Any]]]:
    """
    LiveAvatar API expects:
      links: array of objects | null
      each object requires: url (string), faq (string), id (uuid)
    """
    if not links:
        return None

    out: List[Dict[str, Any]] = []
    for item in links:
        if isinstance(item, str):
            out.append(
                {
                    "url": item,
                    "faq": default_faq,
                    "id": str(uuid4()),
                }
            )
            continue

        if isinstance(item, dict):
            url = str(item.get("url", "")).strip()
            faq = str(item.get("faq", default_faq)).strip() or default_faq
            link_id = item.get("id", None)

            if not url:
                # Skip invalid link objects silently (caller can log upstream)
                continue

            # id is required by API; generate if missing/invalid
            if not link_id or not _is_uuid(str(link_id)):
                link_id = str(uuid4())

            out.append({"url": url, "faq": faq, "id": str(link_id)})
            continue

        # Unknown type -> ignore
        continue

    return out or None


@dataclass
class LiveAvatarError(Exception):
    status_code: int
    message: str
    payload: Optional[Dict[str, Any]] = None


class LiveAvatarClient:
    """
    Compatible with the corrected LiveAvatar Context API fields:
      - name (required)
      - opening_text (required)
      - prompt (required)
      - interactive_style (optional)
      - links (optional) BUT must be array of objects (url, faq, id UUID)
    """

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.liveavatar.com",
        timeout: int = 25,
    ) -> None:
        self.api_key = (api_key or "").strip()
        self.base_url = _normalize_base_url(base_url)
        self.timeout = int(timeout)

    def _headers(self) -> Dict[str, str]:
        return {
            "accept": "application/json",
            "content-type": "application/json",
            "X-Api-Key": self.api_key,
        }

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        json_body: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        url = f"{self.base_url}{path}"
        r = requests.request(
            method=method.upper(),
            url=url,
            headers=self._headers(),
            params=params,
            json=json_body,
            timeout=self.timeout,
        )
        return _safe_json(r)

    # ---------- Context endpoints ----------

    def list_contexts(self) -> Dict[str, Any]:
        return self._request("GET", "/v1/contexts")

    def get_context(self, context_id: str) -> Dict[str, Any]:
        context_id = (context_id or "").strip()
        return self._request("GET", f"/v1/contexts/{context_id}")

    def delete_context(self, context_id: str) -> Dict[str, Any]:
        context_id = (context_id or "").strip()
        return self._request("DELETE", f"/v1/contexts/{context_id}")

    def update_context(self, context_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        context_id = (context_id or "").strip()
        clean_payload = dict(payload or {})
        # IMPORTANT: do not send undefined/unknown fields
        clean_payload = _filter_context_payload(clean_payload, allow_partial=True)
        return self._request("PATCH", f"/v1/contexts/{context_id}", json_body=clean_payload)

    def create_context(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        clean_payload = dict(payload or {})
        # IMPORTANT: do not send undefined/unknown fields
        clean_payload = _filter_context_payload(clean_payload, allow_partial=False)
        return self._request("POST", "/v1/contexts", json_body=clean_payload)

    # ---------- Helpers commonly needed by app.py ----------

    def find_context_id_by_name(self, name: str) -> Optional[str]:
        """
        Lists contexts and returns the first matching id by exact name.
        """
        name = (name or "").strip()
        if not name:
            return None
        resp = self.list_contexts()
        results = (resp.get("data") or {}).get("results") if isinstance(resp.get("data"), dict) else None
        if not isinstance(results, list):
            return None
        for row in results:
            if isinstance(row, dict) and row.get("name") == name:
                return row.get("id")
        return None

    def delete_context_by_name(self, name: str) -> Dict[str, Any]:
        cid = self.find_context_id_by_name(name)
        if not cid:
            return {"code": 1000, "data": None, "message": "No matching context to delete"}
        return self.delete_context(cid)

    def create_or_replace_by_name(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """
        If a context with the same name exists, delete it and retry once.
        """
        name = str((payload or {}).get("name", "")).strip()
        if name:
            existing_id = self.find_context_id_by_name(name)
            if existing_id:
                self.delete_context(existing_id)

        resp = self.create_context(payload)
        # If API still reports name exists, delete and retry once.
        msg = str(resp.get("message", "") or "")
        data = resp.get("data")
        # Some errors come as list in "data" with loc/name and message
        already_exists = False
        if "already exists" in msg.lower():
            already_exists = True
        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict) and "already exists" in str(item.get("message", "")).lower():
                    already_exists = True
                    break

        if already_exists and name:
            existing_id = self.find_context_id_by_name(name)
            if existing_id:
                self.delete_context(existing_id)
            return self.create_context(payload)

        return resp


def _filter_context_payload(payload: Dict[str, Any], allow_partial: bool) -> Dict[str, Any]:
    """
    Keep ONLY the allowed keys for the create/update context endpoint.
    This prevents the exact failure you hit earlier (undefined field like 'links' in wrong shape, etc.)
    """
    name = str(payload.get("name", "")).strip() if payload.get("name") is not None else ""
    opening_text = str(payload.get("opening_text", "")).strip() if payload.get("opening_text") is not None else ""
    prompt = str(payload.get("prompt", "")).strip() if payload.get("prompt") is not None else ""
    interactive_style = payload.get("interactive_style", None)

    # links may be list[str] or list[dict]; normalize to required object schema
    links_in = payload.get("links", None)
    links: Optional[List[Dict[str, Any]]] = None
    if isinstance(links_in, list):
        links = _build_links(links_in)

    out: Dict[str, Any] = {}

    # For PATCH allow_partial=True, we allow missing required fields.
    if name or (not allow_partial):
        out["name"] = name
    if opening_text or (not allow_partial):
        out["opening_text"] = opening_text
    if prompt or (not allow_partial):
        out["prompt"] = prompt

    if interactive_style is not None:
        out["interactive_style"] = str(interactive_style)

    if links is not None:
        out["links"] = links

    # For CREATE, enforce required fields
    if not allow_partial:
        missing = []
        if not out.get("name"):
            missing.append("name")
        if not out.get("opening_text"):
            missing.append("opening_text")
        if not out.get("prompt"):
            missing.append("prompt")
        if missing:
            raise LiveAvatarError(
                status_code=0,
                message=f"Missing required fields for create_context: {', '.join(missing)}",
                payload=out,
            )

    return out


# ---- Backward-compatible convenience function (if older app.py used it) ----

def liveavatar_create_context(
    base_url: str,
    api_key: str,
    payload: Dict[str, Any],
    timeout: int = 25,
    *,
    replace_if_exists: bool = False,
) -> Dict[str, Any]:
    """
    Drop-in helper for app.py:
      - uses X-Api-Key
      - strips unknown keys
      - optionally replace existing context by name
    """
    client = LiveAvatarClient(api_key=api_key, base_url=base_url, timeout=timeout)
    if replace_if_exists:
        return client.create_or_replace_by_name(payload)
    return client.create_context(payload)
