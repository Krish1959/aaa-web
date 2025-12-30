# app.py  (root)

from __future__ import annotations

import base64
import json
import os
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import requests
from flask import Flask, jsonify, redirect, render_template_string, request, url_for

from services.liveavatar import LiveAvatarClient
from services.scraper import scrape_site
from services.text_cleaner import clean_text, chunk_text_with_provenance


app = Flask(__name__)

# -----------------------------
# Config
# -----------------------------
LIVEAVATAR_API_KEY = os.getenv("LIVEAVATAR_API_KEY", "").strip()
LIVEAVATAR_BASE_URL = os.getenv("LIVEAVATAR_BASE_URL", "https://api.liveavatar.com").strip()

GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "").strip()
GITHUB_REPO = os.getenv("GITHUB_REPO", "").strip()  # e.g. "kerish1959/aaa-web"
GITHUB_BRANCH = os.getenv("GITHUB_BRANCH", "main").strip()

# Data locations in repo
SUBMISSIONS_JSONL_PATH = "data/submissions.jsonl"
BY_ENTRY_DIR = "data/contexts/by-entry"
ERROR_LOG_PATH = "HeyGen_errors.txt"  # keep your existing filename convention

# Scrape limits (pages, not words). You asked for no word limit, so we do NOT truncate prompt.
DEFAULT_MAX_PAGES = int(os.getenv("SCRAPE_MAX_PAGES", "25"))


# -----------------------------
# HTML templates
# -----------------------------
INDEX_HTML = """
<!doctype html>
<html>
<head>
  <meta charset="utf-8"/>
  <title>AAA - Website to LiveAvatar Context</title>
  <style>
    body { font-family: Arial, sans-serif; margin: 28px; background:#0b1220; color:#e6eefc; }
    .card { background:#111b2e; padding:20px; border-radius:16px; max-width:720px; }
    label { display:block; margin-top:14px; font-weight:600; }
    input { width:100%; padding:12px; border-radius:10px; border:1px solid #2a3a5c; background:#0e1730; color:#e6eefc; font-size:16px; }
    button { margin-top:18px; padding:12px 16px; border-radius:12px; border:0; background:#2f6cff; color:white; font-weight:700; font-size:16px; cursor:pointer; }
    small { color:#b8c7e6; }
  </style>
</head>
<body>
  <h2>Step 1: Capture user details and store in GitHub with a timestamp.</h2>
  <div class="card">
    <form method="post" action="{{ url_for('submit') }}">
      <label>Name</label>
      <input name="name" required placeholder="Your name" />

      <label>Company</label>
      <input name="company" required placeholder="Company / Institution" />

      <label>Email</label>
      <input name="email" required type="email" placeholder="email@example.com" />

      <label>Phone (optional)</label>
      <input name="phone" placeholder="+65..." />

      <label>Web URL</label>
      <!-- IMPORTANT: keep name="web_url" but backend will also accept website/url to avoid mismatch -->
      <input name="web_url" required placeholder="https://example.com/" />

      <label>Max pages to scrape <small>(optional, default {{default_max_pages}})</small></label>
      <input name="max_pages" placeholder="{{default_max_pages}}" />

      <button type="submit">Submit</button>
    </form>
  </div>
</body>
</html>
"""

RESULT_HTML = """
<!doctype html>
<html>
<head>
  <meta charset="utf-8"/>
  <title>Submission Result</title>
  <style>
    body { font-family: Arial, sans-serif; margin: 28px; background:#0b1220; color:#e6eefc; }
    .row { display:flex; gap:14px; flex-wrap:wrap; margin-bottom:16px; }
    .pill { background:#111b2e; padding:10px 14px; border-radius:999px; }
    pre { background:#0e1730; border:1px solid #2a3a5c; padding:14px; border-radius:12px; overflow:auto; white-space:pre-wrap; word-break:break-word; }
    a { color:#9ac0ff; }
  </style>
</head>
<body>
  <h2>Submission Result</h2>

  <div class="row">
    <div class="pill">Context name: <b>{{context_name}}</b></div>
    <div class="pill">xxxx: <b>{{xxxx}}</b></div>
    <div class="pill">Pages scraped: <b>{{pages_count}}</b></div>
    <div class="pill">Links sent: <b>{{links_count}}</b></div>
  </div>

  {% if github_note %}
    <p><b>GitHub:</b> {{ github_note }}</p>
  {% endif %}

  <h3>Response (JSON)</h3>
  <pre>{{payload_json}}</pre>

  <p><a href="{{ url_for('index') }}">Back</a></p>
</body>
</html>
"""


# -----------------------------
# GitHub helpers
# -----------------------------
def gh_headers() -> Dict[str, str]:
    if not GITHUB_TOKEN:
        return {}
    return {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "aaa-web",
    }


def github_get_file(repo: str, path: str, branch: str) -> Tuple[Optional[str], Optional[str]]:
    """
    Returns (text, sha) if file exists, else (None, None).
    """
    url = f"https://api.github.com/repos/{repo}/contents/{path}?ref={branch}"
    r = requests.get(url, headers=gh_headers(), timeout=30)
    if r.status_code == 404:
        return None, None
    r.raise_for_status()
    data = r.json()
    content_b64 = data.get("content", "")
    sha = data.get("sha")
    if not content_b64:
        return "", sha
    raw = base64.b64decode(content_b64).decode("utf-8", errors="replace")
    return raw, sha


def github_put_file(repo: str, path: str, branch: str, new_text: str, sha: str | None) -> dict:
    """
    Writes/updates a text file to GitHub.
    """
    url = f"https://api.github.com/repos/{repo}/contents/{path}"
    payload: Dict[str, Any] = {
        "message": f"Update {path}",
        "content": base64.b64encode(new_text.encode("utf-8")).decode("utf-8"),
        "branch": branch,
    }
    if sha:
        payload["sha"] = sha

    r = requests.put(url, headers=gh_headers(), data=json.dumps(payload), timeout=30)
    r.raise_for_status()
    return r.json()


def github_append_jsonl(repo: str, path: str, branch: str, entry: Dict[str, Any]) -> None:
    existing, sha = github_get_file(repo, path, branch)
    existing = existing or ""
    line = json.dumps(entry, ensure_ascii=False)
    new_text = (existing.rstrip("\n") + "\n" + line + "\n") if existing.strip() else (line + "\n")
    github_put_file(repo, path, branch, new_text, sha)


def github_write_json(repo: str, path: str, branch: str, obj: Dict[str, Any]) -> None:
    text = json.dumps(obj, ensure_ascii=False, indent=2)
    _, sha = github_get_file(repo, path, branch)
    github_put_file(repo, path, branch, text + "\n", sha)


def github_append_error_log(repo: str, path: str, branch: str, text_line: str) -> None:
    existing, sha = github_get_file(repo, path, branch)
    existing = existing or ""
    new_text = existing + text_line.rstrip("\n") + "\n"
    github_put_file(repo, path, branch, new_text, sha)


# -----------------------------
# URL → xxxx derivation (your domain rule)
# -----------------------------
def derive_xxxx_from_url(url: str) -> str:
    """
    Domain rule:
    - 'xxxx' is the word immediately after 'www' (or the first subdomain if no 'www').
    Examples:
      https://www.bescon.com.sg/ -> bescon
      https://bescon.com.sg/     -> bescon
      https://abc.example.com/   -> abc
    """
    try:
        u = (url or "").strip()
        if not u:
            return "site"
        # crude parse without extra deps
        u = u.replace("https://", "").replace("http://", "")
        host = u.split("/")[0].strip()
        if not host:
            return "site"
        parts = [p for p in host.split(".") if p]
        if not parts:
            return "site"
        if parts[0].lower() == "www" and len(parts) >= 2:
            return parts[1].lower()
        return parts[0].lower()
    except Exception:
        return "site"


# -----------------------------
# Prompt builder
# -----------------------------
PERSONA_BLOCK = """# PERSONA / ROLE
You are a passionate teacher acting as a mediator for learners.
Scope & safety:
Keep the conversation focused exclusively on bescon and related information only.
If asked about any other topic, reply with exactly:
"Let us focus only on bescon only"
Style & tone:
Use short replies: 1–3 concise sentences or a short paragraph.
Be clear, friendly, and encouraging.
"""


def build_full_prompt(company: str, website: str, chunks: List[Dict[str, str]]) -> str:
    """
    No word-limit truncation here (as requested).
    """
    out: List[str] = []
    out.append(PERSONA_BLOCK.strip())
    out.append("")
    out.append("## COMPANY")
    out.append(f"Company: {company}")
    out.append(f"Website: {website}")
    out.append("")
    out.append("## SOURCE MATERIAL (SCRAPED)")
    out.append("Use the following scraped material as the knowledge base. Cite the source URL when helpful.")
    out.append("")
    if not chunks:
        out.append("(No scraped content was available.)")
        return "\n".join(out).strip()

    for c in chunks:
        # c: {chunk_id, url, text}
        out.append(f"[{c['chunk_id']}] {c['url']}")
        out.append(c["text"].strip())
        out.append("")  # spacer
    return "\n".join(out).strip()


# -----------------------------
# Routes
# -----------------------------
@app.get("/")
def index():
    return render_template_string(INDEX_HTML, default_max_pages=DEFAULT_MAX_PAGES)


@app.post("/submit")
def submit():
    created_at = datetime.now(timezone.utc).isoformat()

    # Accept multiple possible field names to avoid template/backend mismatch
    name = (request.form.get("name") or "").strip()
    company = (request.form.get("company") or "").strip()
    email = (request.form.get("email") or "").strip()
    phone = (request.form.get("phone") or "").strip()

    website = (
        (request.form.get("web_url") or "").strip()
        or (request.form.get("website") or "").strip()
        or (request.form.get("url") or "").strip()
    )

    max_pages_raw = (request.form.get("max_pages") or "").strip()
    try:
        max_pages = int(max_pages_raw) if max_pages_raw else DEFAULT_MAX_PAGES
    except Exception:
        max_pages = DEFAULT_MAX_PAGES

    # Basic validation
    if not (name and company and email and website):
        return (
            render_template_string(
                RESULT_HTML,
                context_name="(not created)",
                xxxx="(n/a)",
                pages_count=0,
                links_count=0,
                github_note="Missing required fields (name/company/email/web_url).",
                payload_json=json.dumps({"error": "Missing required fields"}, indent=2),
            ),
            400,
        )

    xxxx = derive_xxxx_from_url(website)
    # Context name must come from URL-derived xxxx (NOT from company input)
    context_name = f"{xxxx.upper()}1"

    entry_id = str(uuid.uuid4())

    entry_obj: Dict[str, Any] = {
        "id": entry_id,
        "created_at": created_at,
        "name": name,
        "company_input": company,
        "email": email,
        "phone": phone,
        "website": website,
        "xxxx": xxxx,
        "context_name": context_name,
        "scrape": {"pages": [], "pages_count": 0},
        "heygen_liveavatar": {},
    }

    # 1) Scrape site
    pages = []
    internal_urls: List[str] = []
    try:
        pages, internal_urls = scrape_site(website, max_pages=max_pages, timeout=20)
        entry_obj["scrape"]["pages"] = [
            {"url": p.get("url", ""), "title": p.get("title", ""), "text_len": len(p.get("text", "") or "")}
            for p in pages
        ]
        entry_obj["scrape"]["pages_count"] = len(pages)
    except Exception as e:
        entry_obj["scrape"]["pages"] = []
        entry_obj["scrape"]["pages_count"] = 0
        entry_obj["scrape"]["error"] = repr(e)

    # 2) Clean + chunk
    chunks: List[Dict[str, str]] = []
    try:
        # Build per-page chunking with provenance
        for i, p in enumerate(pages):
            url = (p.get("url") or "").strip()
            raw_text = p.get("text") or ""
            ct = clean_text(raw_text)
            # chunk_id prefix uses page index for stable mapping to links FAQ label
            page_prefix = f"P{i:03d}"
            for j, t in enumerate(chunk_text_with_provenance(ct, max_chars=2400)):
                chunks.append(
                    {
                        "chunk_id": f"{page_prefix}-C{j:02d}",
                        "url": url,
                        "text": t,
                    }
                )
    except Exception as e:
        entry_obj["chunking_error"] = repr(e)

    # 3) Build links objects (required: url, faq, id UUID)
    # Map page URL -> first page prefix Pxxx
    url_to_prefix: Dict[str, str] = {}
    for c in chunks:
        u = c["url"]
        if u and u not in url_to_prefix:
            url_to_prefix[u] = c["chunk_id"].split("-C")[0]  # "P000"

    payload_links: List[Dict[str, str]] = []
    for u in internal_urls:
        prefix = url_to_prefix.get(u, "P999")
        payload_links.append(
            {
                "url": u,
                "id": str(uuid.uuid4()),
                "faq": f"Scraped Content {prefix}",
            }
        )

    # 4) Build LiveAvatar payload (exact keys)
    opening_text = f"Welcome to the Q & A session on {context_name}"
    prompt = build_full_prompt(company=company, website=website, chunks=chunks)

    request_payload: Dict[str, Any] = {
        "name": context_name,
        "opening_text": opening_text,
        "prompt": prompt,
        "interactive_style": "conversational",
    }
    if payload_links:
        request_payload["links"] = payload_links

    entry_obj["heygen_liveavatar"]["request_payload"] = request_payload

    # 5) Create context with conflict handling
    live = LiveAvatarClient(api_key=LIVEAVATAR_API_KEY, base_url=LIVEAVATAR_BASE_URL)

    def _delete_by_name(nm: str) -> Dict[str, Any]:
        lst = live.list_contexts()
        data = lst.get("data") or {}
        results = data.get("results") or []
        target_id = None
        for it in results:
            if (it.get("name") or "") == nm:
                target_id = it.get("id")
                break
        if not target_id:
            return {"status": "not_found"}
        return live.delete_context(target_id)

    status = "not_attempted"
    resp: Dict[str, Any] = {}
    try:
        # A) Pre-delete same-name if exists
        _ = _delete_by_name(context_name)

        # B) Create
        resp = live.create_context(request_payload)
        status = "created" if resp.get("code") == 1000 else "error"

        # C) If “already exists”, delete+retry once
        msg = (resp.get("message") or "") if isinstance(resp, dict) else ""
        if status != "created" and "already exists" in msg.lower():
            _ = _delete_by_name(context_name)
            resp = live.create_context(request_payload)
            status = "created" if resp.get("code") == 1000 else "error"

    except Exception as e:
        status = "exception"
        resp = {"code": -1, "data": None, "message": repr(e)}

    entry_obj["heygen_liveavatar"]["status"] = status
    entry_obj["heygen_liveavatar"]["response"] = resp

    # 6) Persist to GitHub (submissions + by-entry + error log)
    github_note = ""
    if GITHUB_REPO and GITHUB_TOKEN:
        try:
            github_append_jsonl(GITHUB_REPO, SUBMISSIONS_JSONL_PATH, GITHUB_BRANCH, entry_obj)
            github_write_json(GITHUB_REPO, f"{BY_ENTRY_DIR}/{entry_id}.json", GITHUB_BRANCH, entry_obj)
            github_note = f"Wrote to {SUBMISSIONS_JSONL_PATH} and {BY_ENTRY_DIR}/{entry_id}.json"
        except Exception as e:
            github_note = f"GitHub write failed: {repr(e)}"
            # log error line
            try:
                line = f"{datetime.now(timezone.utc).isoformat()} | xxxx={xxxx} | code=github_write_failed | error={repr(e)}"
                github_append_error_log(GITHUB_REPO, ERROR_LOG_PATH, GITHUB_BRANCH, line)
            except Exception:
                pass
    else:
        github_note = "GitHub env vars not set (GITHUB_TOKEN / GITHUB_REPO). Skipped GitHub writes."

    # HTML response with JSON shown neatly
    return render_template_string(
        RESULT_HTML,
        context_name=context_name,
        xxxx=xxxx,
        pages_count=entry_obj["scrape"]["pages_count"],
        links_count=len(payload_links),
        github_note=github_note,
        payload_json=json.dumps(entry_obj, ensure_ascii=False, indent=2),
    )


@app.get("/health")
def health():
    return jsonify({"ok": True, "time": datetime.now(timezone.utc).isoformat()})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")), debug=True)
