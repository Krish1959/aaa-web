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

# context setup
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


def write_entry_context_json(entry_id: str, entry_obj: dict):
    """
    Writes/updates the per-entry JSON file:
      data/contexts/by-entry/<entry_id>.json
    """
    if not (GITHUB_TOKEN and GITHUB_REPO):
        return {"skipped": True, "reason": "GITHUB_TOKEN or GITHUB_REPO not set"}

    path = f"data/contexts/by-entry/{entry_id}.json"
    existing_text, sha = github_get_file(GITHUB_REPO, path, GITHUB_BRANCH)

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
                {
                    "from": resp.url,
                    "to": resp.headers.get("Location", ""),
                    "status": resp.status_code,
                }
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
    if host.startswith("www."):
        parts = host.split(".")
        return parts[1] if len(parts) > 1 else "unknown"
    return host.split(".")[0] if host else "unknown"


def make_entry_id(record: dict) -> str:
    raw = f'{record["created_at"]}|{record["email"]}|{record["web_url"]}'
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]


def build_full_prompt(xxxx: str, chunks: list[dict]) -> str:
    content = "\n\n".join([c["text"] for c in chunks[:30]])
    return f"""
You are a passionate teacher acting as a mediator for learners.

Scope & safety:
Keep the conversation focused exclusively on {xxxx} and related information only.
Bounce all other topics with this fixed sentence:
"Let us focus only on {xxxx} only"
Categorically deny answering any topic outside the scope defined here.

Style & tone:
Use short replies: 1–3 concise sentences or a short paragraph.
Be clear, friendly, and encouraging.

CONTENT (scraped):
---------------------------------
{content}
""".strip()


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

    created_at = datetime.now(timezone.utc).isoformat()

    record = {
        "created_at": created_at,
        "name": name,
        "company": company,
        "email": email,
        "phone": phone if phone else None,
        "web_url": web_url,
        "stage": 2,
    }

    save_to_sqlite(record)
    gh = append_record_to_github_jsonl(record)

    final_url, final_host, redirect_chain, http_status = resolve_final_url_and_redirects(web_url)
    xxxx = derive_xxxx_from_host(final_host)
    entry_id = make_entry_id(record)

    entry_obj = {
        "schema_version": "1.0",
        "entry_id": entry_id,
        "identity": {
            "xxxx": xxxx,
            "input_url": web_url,
            "final_url": final_url,
            "final_host": final_host,
            "canonical_url": None,
        },
        "fetch": {
            "fetched_at_utc": created_at,
            "http_status": http_status,
            "user_agent": "Mozilla/5.0 (AgenticAvatarBot/1.0)",
            "redirect_chain": redirect_chain,
            "robots_respected": True,
            "paywall_detected": False,
            "errors": [],
        },
        "crawl_policy": {
            "scope_rule": "same_domain_only",
            "registrable_domain": None,
            "max_pages": MAX_PAGES,
            "max_depth": 1,
            "deny_url_patterns": [],
            "allow_url_patterns": [],
        },
        "pages": [],
        "links": {"internal_links_discovered": [], "internal_links_selected": []},
        "chunks": [],
        "prompt_engineering": {
            "name": xxxx.upper(),
            "opening_intro": f"Let us talk about {xxxx}",
            "scope_safety": {
                "topic_label": xxxx,
                "bounce_sentence": f"Let us focus only on {xxxx} only",
                "deny_out_of_scope": True,
            },
            "style_tone": {
                "persona": "passionate teacher",
                "reply_length": "short",
                "tone": ["clear", "friendly", "encouraging"],
            },
            "full_prompt": "",
            "source_index": [],
        },
        "heygen_liveavatar": {
            "push_status": "not_pushed",
            "request_payload": {"name": "", "opening_intro": "", "links": [], "full_prompt": ""},
            "response": {"context_id": None, "context_url": None, "raw": {}},
            "pushed_at_utc": None,
            "error": None,
        },
        "submission": {
            "name": record["name"],
            "company": record["company"],
            "email": record["email"],
            "phone": record.get("phone"),
        },
    }

    # --- Scrape site ---
    try:
        scrape_result = scrape_site(final_url, max_pages=MAX_PAGES, max_chars_per_page=MAX_CHARS_PER_PAGE)
        entry_obj["crawl_policy"]["registrable_domain"] = scrape_result.get("base_host_key")

        entry_obj["links"]["internal_links_discovered"] = scrape_result.get("links", [])
        entry_obj["links"]["internal_links_selected"] = [x["url"] for x in scrape_result.get("links", [])[:10]]

        all_chunks = []
        for p in scrape_result.get("pages", []):
            clean_text = p.pop("clean_text", "")
            entry_obj["pages"].append(p)
            if clean_text:
                all_chunks.extend(chunk_text_with_provenance(p.get("final_url") or p.get("url"), clean_text))

        entry_obj["chunks"] = all_chunks

        entry_obj["prompt_engineering"]["source_index"] = [
            {"label": f"Source {i+1}", "url": u} for i, u in enumerate(entry_obj["links"]["internal_links_selected"])
        ]

        entry_obj["prompt_engineering"]["full_prompt"] = build_full_prompt(xxxx, all_chunks)

    except Exception as e:
        entry_obj["fetch"]["errors"].append({"stage": "scrape", "url": final_url, "message": str(e)})

    # --- Push to LiveAvatar ONLY if prompt exists ---
    if entry_obj["prompt_engineering"]["full_prompt"].strip():
        try:
            payload_name = entry_obj["prompt_engineering"]["name"]
            payload_intro = entry_obj["prompt_engineering"]["opening_intro"]
            payload_links = entry_obj["links"]["internal_links_selected"]
            payload_prompt = entry_obj["prompt_engineering"]["full_prompt"]

            entry_obj["heygen_liveavatar"]["request_payload"] = {
                "name": payload_name,
                "opening_intro": payload_intro,
                "links": payload_links,
                "full_prompt": payload_prompt,
            }

            resp = create_context(payload_name, payload_intro, payload_links, payload_prompt)

            context_id = resp.get("id") or resp.get("context_id") or resp.get("data", {}).get("id")
            context_url = resp.get("url") or resp.get("context_url")
            if not context_url and context_id:
                context_url = f"https://app.liveavatar.com/contexts/{context_id}"

            entry_obj["heygen_liveavatar"]["push_status"] = "pushed"
            entry_obj["heygen_liveavatar"]["response"] = {
                "context_id": context_id,
                "context_url": context_url,
                "raw": resp,
            }
            entry_obj["heygen_liveavatar"]["pushed_at_utc"] = created_at
            entry_obj["heygen_liveavatar"]["error"] = None

        except Exception as e:
            entry_obj["heygen_liveavatar"]["push_status"] = "failed"
            entry_obj["heygen_liveavatar"]["response"] = {"context_id": None, "context_url": None, "raw": {}}
            entry_obj["heygen_liveavatar"]["pushed_at_utc"] = created_at
            entry_obj["heygen_liveavatar"]["error"] = str(e)
    else:
        entry_obj["heygen_liveavatar"]["push_status"] = "failed"
        entry_obj["heygen_liveavatar"]["response"] = {"context_id": None, "context_url": None, "raw": {}}
        entry_obj["heygen_liveavatar"]["pushed_at_utc"] = created_at
        entry_obj["heygen_liveavatar"]["error"] = "Skipped push: full_prompt is empty (scrape likely failed)."

    ctx_write = write_entry_context_json(entry_id, entry_obj)

    return render_template(
        "success.html",
        title=APP_TITLE,
        record=record,
        gh=gh,
        ctx=ctx_write,
        entry=entry_obj,
    )


if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port)
