# services/liveavatar.py

from __future__ import annotations

from typing import Any, Dict, Optional

import requests


class LiveAvatarClient:
    """
    LiveAvatar Context API client.

    Base:
      https://api.liveavatar.com

    Auth header:
      X-Api-Key: <key>

    Endpoints:
      GET    /v1/contexts
      GET    /v1/contexts/{id}
      POST   /v1/contexts
      PATCH  /v1/contexts/{id}
      DELETE /v1/contexts/{id}
    """

    def __init__(self, api_key: str, base_url: str = "https://api.liveavatar.com", timeout: int = 25) -> None:
        self.api_key = (api_key or "").strip()
        self.base_url = (base_url or "https://api.liveavatar.com").rstrip("/")
        self.timeout = timeout

    def _headers(self) -> Dict[str, str]:
        return {
            "accept": "application/json",
            "content-type": "application/json",
            "X-Api-Key": self.api_key,
        }

    def list_contexts(self) -> Dict[str, Any]:
        url = f"{self.base_url}/v1/contexts"
        r = requests.get(url, headers=self._headers(), timeout=self.timeout)
        try:
            return r.json()
        except Exception:
            return {"code": -1, "data": None, "message": r.text, "status_code": r.status_code}

    def get_context(self, context_id: str) -> Dict[str, Any]:
        url = f"{self.base_url}/v1/contexts/{context_id}"
        r = requests.get(url, headers=self._headers(), timeout=self.timeout)
        try:
            return r.json()
        except Exception:
            return {"code": -1, "data": None, "message": r.text, "status_code": r.status_code}

    def create_context(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        url = f"{self.base_url}/v1/contexts"
        r = requests.post(url, json=payload, headers=self._headers(), timeout=self.timeout)
        try:
            return r.json()
        except Exception:
            return {"code": -1, "data": None, "message": r.text, "status_code": r.status_code}

    def update_context(self, context_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        url = f"{self.base_url}/v1/contexts/{context_id}"
        r = requests.patch(url, json=payload, headers=self._headers(), timeout=self.timeout)
        try:
            return r.json()
        except Exception:
            return {"code": -1, "data": None, "message": r.text, "status_code": r.status_code}

    def delete_context(self, context_id: str) -> Dict[str, Any]:
        url = f"{self.base_url}/v1/contexts/{context_id}"
        r = requests.delete(url, headers=self._headers(), timeout=self.timeout)
        try:
            return r.json()
        except Exception:
            return {"code": -1, "data": None, "message": r.text, "status_code": r.status_code}
