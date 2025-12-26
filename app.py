import base64
import hashlib
import json
import os
import re
import sqlite3
from datetime import datetime, timezone
from urllib.parse import urlparse

import requests
from flask import Flask, render_template, request

from services.scraper import scrape_site
from services.text_cleaner import chunk_text_with_provenance
from services.liveavatar import create_context


APP_TITLE = "AVATAR AGENTIC AI APPLICATION"

# ---- Config via environment variables (Render -> Environment) ----
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "").strip()
GITHUB_REPO = os.getenv("GITHUB_REPO", "").strip()        # e.g. "Krish1959/aaa-web"
GITHUB_BRANCH = os.getenv("GITHUB_BRANCH", "main").strip()
GITHUB_DB_PATH = os.getenv("GITHUB_DB_PATH", "data/submissions.jsonl").strip()

# Scraping controls
MAX_PAGES = int(os.getenv("MAX_PAGES", "5"))
MAX_CHARS_PER_PAGE = int(os.getenv("MAX_CHARS_PER_PAGE", "20000"))

# Local DB (optional fallback / audit)
SQLITE_PATH = os.getenv("SQLITE_PATH", "local_submissions.db")

# HeyGen / LiveAvatar error log file (local)
HEYGEN_ERROR_LOG = os.getenv("HEYGEN_ERROR_LOG", "HeyGen_errors.txt").strip()

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
PHONE_RE = re.compile(r"^[0-9+\-\s()]{6,20}$")


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def log_heygen_line(line: str):
    """
    Append a single line to HeyGen_errors.txt
    """
    try:
        with open(HEYGEN_ERROR_LOG, "a", encoding="utf-8") as f:
            f.write(line.rstrip() + "\n")
    except Exception:
        # Avoid breaking the app if logging fails
        pass


def is_valid_url(u: str) -> bool:
    try:
        p = urlparse(u.strip())
        return p.scheme in ("http", "https") and bool(p.netloc)
    except Exception:
        return False


def init_sqlite():
    conn = sqlite3.connect(SQLITE_PATH)
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS submissions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            name TEXT NOT NULL,
            company TEXT NOT NULL,
            email TEXT NOT NULL,
            phone TEXT,
            web_url TEXT NOT NULL,
            raw_json TEXT NOT NULL
        )
        """
    )
    conn.commit()
    conn.close()


def save_to_sqlite(record: dict):
    conn = sqlite3.connect(SQLITE_PATH)
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO submissions (created_at, name, company, email, phone, web_url, raw_json)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            record["created_at"],
            record["name"],
            record["company"],
            record["email"],
            record.get("phone"),
            record["web_url"],
            json.dumps(record, ensure_ascii=False),
        ),
    )
    conn.commit()
    conn.close()


def github_headers():
    return {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def github_get_file(repo: str, path: str, branch: str):
    """
    Returns: (text_content, sha) if exists
             ("", None) if not found (404)
    """
    url = f"https://api.github.com/repos/{repo}/contents/{path}"
    r = requests.get(url, headers=github_headers(), params={"ref": branch}, timeout=20)
    if r.status_code == 404:
        return "", None
    r.raise_for_status()
    data = r.json()
    content_b64 = data.get("content", "")
    sha = data.get("sha")
    if not content_b64:
        return "", sha
    text = base64.b64decode(content_b64).decode("utf-8", errors="replace")
    return text, sha


def github_put_file(repo: str, path: str, branch: str, new_text: str, sha: str | None):
    url = f"https://api.github.com/repos/{repo}/contents/{path}"
    payload = {
        "message": f"Update {path}",
        "content": base64.b64encode(new_text.encode("utf-8")).decode("utf-8"),
        "branch": branch,
    }
    if sha:
        payload["sha"] = sha

    r = requests.put(url, headers=github_headers(), json=payload, timeout=20)
    r.raise_for_status()
    return r.json()


def append_record_to_github_jsonl(record: dict):
    """
    GitHub 'database' = JSON Lines file:
    each line is one JSON object appended to data/submissions.jsonl
    """
    if not (GITHUB_TOKEN and GITHUB_REPO):
        return {"skipped": True, "reason": "GITHUB_TOKEN or GITHUB_REPO not set"}

    existing_text, sha = github_get_file(GITHUB_REPO, GITHUB_DB_PATH, GITHUB_BRANCH)
    line = json.dumps(record, ensure_ascii=False)

    new_text = existing_text + ("" if existing_text.endswith("\n") or existing_text == "" else "\n")
    new_text += line + "\n"

    result = github_put_file(GITHUB_REPO, GITHUB_DB_PATH, GITHUB_BRANCH, new_text, sha)
    return {"skipped": False, "commit": result.get("commit", {}).get("sha")}


def write_entry_context_json(entry_id: str, entry_obj: dict):
    """
    Writes/updates the per-entry JSON file:
      data/contexts/by-entry/<entry_id>.json
    """
    if not (GITHUB_TOKEN and GITHUB_REPO):
        return {"skipped": True, "reason": "GITHUB_TOKEN or GITHUB_REPO not set"}

    path = f"data/contexts/by-entry/{entry_id}.json"
    _, sha = github_get_file(GITHUB_REPO, path, GITHUB_BRANCH)

    result = github_put_file(
        GITHUB_REPO,
        path,
        GITHUB_BRANCH,
        json.dumps(entry_obj, indent=2, ensure_ascii=False),
        sha,
    )
    return {"skipped": False, "path": path, "commit": result.get("commit", {}).get("sha")}


def resolve_final_url_and_redirects(url: str):
    """
    Returns: final_url, final_host, redirect_chain, http_status
    """
    redirect_chain = []
    try:
        r = requests.get(url, timeout=25, allow_redirects=True, stream=True)
        http_status = r.status_code
        final_url = r.url
        final_host = urlparse(final_url).hostname or ""

        for resp in r.history:
            redirect_chain.append(
                {"from": resp.url, "to": resp.headers.get("Location", ""), "status": resp.status_code}
            )

        return final_url, final_host, redirect_chain, http_status
    except Exception:
        return url, "", [], 0


def derive_xxxx_from_host(host: str) -> str:
    """
    Your rule (v1):
    - if host starts with www. -> xxxx is the label immediately after www
    - else -> xxxx is the first label
    """
    host = (host or "").lower().strip(".")
    if hos
