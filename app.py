import base64
import json
import os
import re
import sqlite3
from datetime import datetime, timezone
from urllib.parse import urlparse

import requests
from flask import Flask, render_template, request

APP_TITLE = "AVATAR AGENTIC AI APPLICATION"

# ---- Config via environment variables (Render -> Environment) ----
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "").strip()
GITHUB_REPO = os.getenv("GITHUB_REPO", "").strip()        # e.g. "Krish1959/aaa-web"
GITHUB_BRANCH = os.getenv("GITHUB_BRANCH", "main").strip()
GITHUB_DB_PATH = os.getenv("GITHUB_DB_PATH", "data/submissions.jsonl").strip()

# Local DB (optional fallback / audit)
SQLITE_PATH = os.getenv("SQLITE_PATH", "local_submissions.db")

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
PHONE_RE = re.compile(r"^[0-9+\-\s()]{6,20}$")


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


app = Flask(__name__)
init_sqlite()


@app.route("/", methods=["GET"])
def index():
    return render_template("index.html", title=APP_TITLE)


@app.route("/submit", methods=["POST"])
def submit():
    name = (request.form.get("name") or "").strip()
    company = (request.form.get("company") or "").strip()
    email = (request.form.get("email") or "").strip()
    phone = (request.form.get("phone") or "").strip()
    web_url = (request.form.get("web_url") or "").strip()

    errors = []
    if len(name) < 2:
        errors.append("Name must be at least 2 characters.")
    if len(company) < 2:
        errors.append("Company must be at least 2 characters.")
    if not EMAIL_RE.match(email):
        errors.append("Please enter a valid email address.")
    if phone and not PHONE_RE.match(phone):
        errors.append("Phone looks invalid (use digits, +, spaces, - or brackets).")
    if not is_valid_url(web_url):
        errors.append("Please enter a valid web URL starting with http:// or https://")

    if errors:
        return render_template(
            "index.html",
            title=APP_TITLE,
            errors=errors,
            form={"name": name, "company": company, "email": email, "phone": phone, "web_url": web_url},
        ), 400

    record = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "name": name,
        "company": company,
        "email": email,
        "phone": phone if phone else None,
        "web_url": web_url,
        "stage": 1,
    }

    # Optional local audit
    save_to_sqlite(record)

    # GitHub "DB" append
    gh = append_record_to_github_jsonl(record)

    return render_template("success.html", title=APP_TITLE, record=record, gh=gh)


if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port)
