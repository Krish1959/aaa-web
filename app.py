# app.py
# Flask app that:
# 1) Accepts a submission (company + website URL) and appends it to a GitHub JSONL “DB”
# 2) Scrapes the website (main + internal URLs)
# 3) Builds a LiveAvatar Context payload using the *correct* API field names:
#       name, opening_text, prompt, interactive_style, links[]
#    where links[] is an ARRAY OF OBJECTS with required: url, faq, id (uuid)
# 4) Prevents name clashes by deleting any existing context with the same name before creating,
#    and also retries once if API returns “Context with this name already exists.”
#
# Env vars expected (Render / local):
#   LIVEAVATAR_API_KEY        (required)
#   LIVEAVATAR_BASE_URL       default: https://api.liveavatar.com
#
#   GITHUB_TOKEN              (optional but recommended for persistence)
#   GITHUB_REPO               e.g. "owner/repo"
#   GITHUB_BRANCH             default: "main"
#   GITHUB_DB_PATH            default: "data/submissions.jsonl"
#   GITHUB_ERRORS_PATH        default: "data/HeyGen_errors.txt"
#
# Optional tuning:
#   SCRAPE_MAX_PAGES          default: 25
#   SCRAPE_TIMEOUT_SEC        default: 20
#   SCRAPE_USER_AGENT         default: "aaa-web/1.0"
#
# Notes:
# - Context name is derived from URL domain rule: xxxx is word after "www" (or first subdomain if no www)
#   and we create name as f"{XXXX.upper()}1" (to match your BESCON1 pattern).
# - No word limit/truncation is applied to the prompt in this version.

from __future__ import annotations

import base64
import datetime as dt
import json
import os
import re
import time
import uuid
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from flask import Flask, jsonify, redirect, render_template, request, url_for

# -----------------------------
# Flask
# -----------------------------
app = Flask(__name__)

# -----------------------------
# Config
# -----------------------------
LIVEAVATAR_BASE_URL = os.getenv("LIVEAVATAR_BASE_URL", "https://api.liveavatar.com").rstrip("/")
LIVEAVATAR_API_KEY = os.getenv("LIVEAVATAR_API_KEY", "").strip()

GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "").strip()
GITHUB_REPO = os.getenv("GITHUB_REPO", "").strip()
GITHUB_BRANCH = os.getenv("GITHUB_BRANCH", "main").strip()
GITHUB_DB_PATH = os.getenv("GITHUB_DB_PATH", "data/submissions.jsonl").strip()
GITHUB_ERRORS_PATH = os.getenv("GITHUB_ERRORS_PATH", "data/HeyGen_errors.txt").strip()

SCRAPE_MAX_PAGES = int(os.getenv("SCRAPE_MAX_PAGES", "25"))
SCRAPE_TIMEOUT_SEC = int(os.getenv("SCRAPE_TIMEOUT_SEC", "20"))
SCRAPE_USER_AGENT = os.getenv("SCRAPE_USER_AGENT", "aaa-web/1.0")

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": SCRAPE_USER_AGENT})

# -----------------------------
# Helpers: time / logging
# -----------------------------
def utc_now_iso() -> str:
    return dt.datetime.utcnow().replace(tzinfo=dt.timezone.utc).isoformat()

def ensure_dir(path: str) -> None:
    folder = os.path.dirname(path)
    if folder:
        os.makedirs(folder, exist_ok=True)

def append_local_text(path: str, line: str) -> None:
    ensure_dir(path)
    with open(path, "a", encoding="utf-8") as f:
        f.write(line.rstrip("\n") + "\n")

def log_error(code: str, note: str = "", *, xxxx: str = "") -> None:
    # Keep your existing “HeyGen_errors.txt” style
    line = f"{utc_now_iso()} | xxxx={xxxx or '-'} | code={code}"
    if note:
        line += f" | note={note}"
    append_local_text(GITHUB_ERRORS_PATH, line)

def log_http_error(status_code: int, err_obj: Any, *, xxxx: str = "") -> None:
    line = f"{utc_now_iso()} | xxxx={xxxx or '-'} | code=http_{status_code} | error={err_obj}"
    append_local_text(GITHUB_ERRORS_PATH, line)

# -----------------------------
# Helpers: domain rule (xxxx)
# -----------------------------
def derive_xxxx_from_url(url: str) -> str:
    """
    Domain rule:
    - xxxx is the word immediately after 'www' (or first subdomain if no 'www')
    Examples:
      https://www.bescon.com.sg  -> bescon
      https://bescon.com.sg      -> bescon
      https://abc.def.com        -> abc
    """
    try:
        host = (urlparse(url).hostname or "").lower()
    except Exception:
        host = ""
    host = host.split(":")[0].strip()
    if not host:
        return "site"

    parts = host.split(".")
    if len(parts) == 1:
        return parts[0]

    if parts[0] == "www" and len(parts) >= 2:
        return parts[1]
    return parts[0]

def build_context_name_from_url(url: str) -> str:
    xxxx = derive_xxxx_from_url(url)
    return f"{xxxx.upper()}1"

# -----------------------------
# Helpers: GitHub Content API
# -----------------------------
def gh_headers() -> Dict[str, str]:
    if not GITHUB_TOKEN:
        return {}
    return {
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
    }

def github_get_file(repo: str, path: str, branch: str) -> Tuple[str, Optional[str]]:
    """
    Returns (text, sha). If file doesn't exist, returns ("", None)
    """
    if not repo or not GITHUB_TOKEN:
        return ("", None)

    url = f"https://api.github.com/repos/{repo}/contents/{path}"
    params = {"ref": branch}
    r = requests.get(url, headers=gh_headers(), params=params, timeout=30)
    if r.status_code == 404:
        return ("", None)
    r.raise_for_status()
    data = r.json()
    content_b64 = data.get("content", "") or ""
    sha = data.get("sha")
    text = ""
    if content_b64:
        text = base64.b64decode(content_b64.encode("utf-8")).decode("utf-8", errors="replace")
    return (text, sha)

def github_put_file(repo: str, path: str, branch: str, new_text: str, sha: str | None) -> Dict[str, Any]:
    """
    Writes a file to GitHub at repo/contents/path on branch.
    """
    if not repo or not GITHUB_TOKEN:
        # best-effort local only
        ensure_dir(path)
        with open(path, "w", encoding="utf-8") as f:
            f.write(new_text)
        return {"local_only": True, "path": path}

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

def github_append_jsonl(repo: str, path: str, branch: str, obj: Dict[str, Any]) -> None:
    existing, sha = github_get_file(repo, path, branch)
    line = json.dumps(obj, ensure_ascii=False)
    new_text = (existing.rstrip("\n") + "\n" + line + "\n") if existing.strip() else (line + "\n")
    github_put_file(repo, path, branch, new_text, sha)

def github_upload_local_file(repo: str, local_path: str, remote_path: str, branch: str) -> None:
    if not os.path.exists(local_path):
        return
    with open(local_path, "r", encoding="utf-8") as f:
        text = f.read()
    _, sha = github_get_file(repo, remote_path, branch)
    github_put_file(repo, remote_path, branch, text, sha)

# -----------------------------
# Helpers: scraping
# -----------------------------
def normalize_url(url: str) -> str:
    url = url.strip()
    if not url:
        return url
    if not re.match(r"^https?://", url, re.I):
        url = "https://" + url
    return url

def same_site(a: str, b: str) -> bool:
    ha = (urlparse(a).hostname or "").lower()
    hb = (urlparse(b).hostname or "").lower()
    return ha == hb

def extract_internal_links(base_url: str, html: str) -> List[str]:
    soup = BeautifulSoup(html, "lxml")
    found: List[str] = []
    for a in soup.select("a[href]"):
        href = (a.get("href") or "").strip()
        if not href:
            continue
        if href.startswith("#") or href.lower().startswith("mailto:") or href.lower().startswith("javascript:"):
            continue
        u = urljoin(base_url, href)
        # strip fragments
        parsed = urlparse(u)
        u = parsed._replace(fragment="").geturl()
        if same_site(base_url, u):
            found.append(u)
    # de-dupe while preserving order
    seen = set()
    out = []
    for u in found:
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out

def html_to_text(html: str) -> str:
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    text = soup.get_text(separator="\n")
    return clean_text_basic(text)

def clean_text_basic(text: str) -> str:
    # keep it simple but effective
    text = text.replace("\r", "\n")
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    # trim each line
    lines = [ln.strip() for ln in text.split("\n")]
    lines = [ln for ln in lines if ln]  # drop empties
    return "\n".join(lines).strip()

def fetch_url(url: str) -> Tuple[Optional[str], Optional[str]]:
    try:
        r = SESSION.get(url, timeout=SCRAPE_TIMEOUT_SEC)
        r.raise_for_status()
        ctype = (r.headers.get("content-type") or "").lower()
        if "text/html" not in ctype and "application/xhtml" not in ctype and not url.lower().endswith((".htm", ".html", "/")):
            # skip obvious non-html
            return (None, None)
        return (r.text, ctype)
    except Exception:
        return (None, None)

def scrape_site(start_url: str, max_pages: int) -> List[Dict[str, Any]]:
    """
    Returns list of pages:
      [{"url": "...", "html": "...", "text": "..."}]
    """
    start_url = normalize_url(start_url)
    pages: List[Dict[str, Any]] = []
    if not start_url:
        return pages

    queue = [start_url]
    seen = set([start_url])

    while queue and len(pages) < max_pages:
        url = queue.pop(0)
        html, _ctype = fetch_url(url)
        if not html:
            continue

        text = html_to_text(html)
        pages.append({"url": url, "html": html, "text": text})

        # expand internal links from this page
        for u in extract_internal_links(url, html):
            if u not in seen and len(seen) < (max_pages * 20):  # prevent explosion
                seen.add(u)
                queue.append(u)

        time.sleep(0.1)  # be polite

    return pages

# -----------------------------
# Helpers: LiveAvatar API
# -----------------------------
def la_headers() -> Dict[str, str]:
    return {
        "accept": "application/json",
        "content-type": "application/json",
        "X-Api-Key": LIVEAVATAR_API_KEY,
    }

def liveavatar_list_contexts() -> List[Dict[str, Any]]:
    url = f"{LIVEAVATAR_BASE_URL}/v1/contexts"
    r = requests.get(url, headers=la_headers(), timeout=30)
    r.raise_for_status()
    data = r.json()
    results = (((data or {}).get("data") or {}).get("results") or [])
    return results if isinstance(results, list) else []

def liveavatar_delete_context(context_id: str) -> bool:
    url = f"{LIVEAVATAR_BASE_URL}/v1/contexts/{context_id}"
    r = requests.delete(url, headers=la_headers(), timeout=30)
    if r.status_code in (200, 204):
        return True
    # LiveAvatar sometimes returns JSON envelope; treat 1000 as success
    try:
        j = r.json()
        if (j or {}).get("code") == 1000:
            return True
    except Exception:
        pass
    return False

def liveavatar_delete_by_name(name: str) -> int:
    deleted = 0
    try:
        ctxs = liveavatar_list_contexts()
        for c in ctxs:
            if (c.get("name") or "").strip() == name:
                cid = c.get("id")
                if cid and liveavatar_delete_context(str(cid)):
                    deleted += 1
    except Exception:
        pass
    return deleted

def liveavatar_create_context(payload: Dict[str, Any], *, xxxx: str = "") -> Dict[str, Any]:
    """
    Create context with de-dup strategy:
      A) delete any existing contexts that already have the same name
      B) POST create
      C) if still fails with “Context with this name already exists.”, delete + retry once
    """
    name = (payload.get("name") or "").strip()
    if not name:
        raise ValueError("payload.name is required")

    # A) delete first
    try:
        liveavatar_delete_by_name(name)
    except Exception as e:
        log_error("delete_existing_failed", f"{e}", xxxx=xxxx)

    url = f"{LIVEAVATAR_BASE_URL}/v1/contexts"
    r = requests.post(url, headers=la_headers(), json=payload, timeout=60)

    if r.status_code >= 400:
        # Inspect error
        try:
            err = r.json()
        except Exception:
            err = {"raw": r.text}

        # B) if name exists, delete + retry once
        msg = ""
        try:
            msg = json.dumps(err, ensure_ascii=False)
        except Exception:
            msg = str(err)

        if "Context with this name already exists" in msg:
            try:
                liveavatar_delete_by_name(name)
                r2 = requests.post(url, headers=la_headers(), json=payload, timeout=60)
                if r2.status_code >= 400:
                    try:
                        err2 = r2.json()
                    except Exception:
                        err2 = {"raw": r2.text}
                    log_http_error(r2.status_code, err2, xxxx=xxxx)
                    r2.raise_for_status()
                return r2.json()
            except Exception as e:
                log_error("retry_after_delete_failed", f"{e}", xxxx=xxxx)
                log_http_error(r.status_code, err, xxxx=xxxx)
                r.raise_for_status()

        log_http_error(r.status_code, err, xxxx=xxxx)
        r.raise_for_status()

    return r.json()

# -----------------------------
# Prompt builder (NO WORD LIMIT)
# -----------------------------
def build_full_prompt(company_name: str, website: str, pages: List[Dict[str, Any]]) -> str:
    persona = (
        "# PERSONA / ROLE\n"
        "You are a passionate teacher acting as a mediator for learners.\n"
        "Scope & safety:\n"
        "Keep the conversation focused exclusively on bescon and related information only.\n"
        'If asked about any other topic, reply with exactly:\n'
        '"Let us focus only on bescon only"\n'
        "Style & tone:\n"
        "Use short replies: 1–3 concise sentences or a short paragraph.\n"
        "Be clear, friendly, and encouraging.\n"
    )

    header = (
        "\n\n## COMPANY\n"
        f"Company: {company_name}\n"
        f"Website: {website}\n"
        "\n## SOURCE MATERIAL (SCRAPED)\n"
        "Use the following scraped material as the knowledge base. Cite the source URL when helpful.\n"
    )

    if not pages:
        return persona + header + "\n\n(No scraped content was available.)\n"

    blocks: List[str] = []
    for i, p in enumerate(pages):
        url = p.get("url") or ""
        text = (p.get("text") or "").strip()
        if not text:
            continue
        blocks.append(f"\n\n### SOURCE {i+1}: {url}\n{text}")

    if not blocks:
        return persona + header + "\n\n(No scraped content was available.)\n"

    return persona + header + "".join(blocks)

# -----------------------------
# Core pipeline
# -----------------------------
def process_submission(company_input: str, website_input: str) -> Dict[str, Any]:
    website = normalize_url(website_input)
    xxxx = derive_xxxx_from_url(website)
    context_name = build_context_name_from_url(website)

    entry_id = str(uuid.uuid4())
    entry_obj: Dict[str, Any] = {
        "id": entry_id,
        "created_at": utc_now_iso(),
        "company_input": company_input,
        "website": website,
        "xxxx": xxxx,
        "context_name": context_name,
        "scrape": {},
        "heygen_liveavatar": {},
    }

    # 1) Scrape
    pages = scrape_site(website, max_pages=SCRAPE_MAX_PAGES)
    entry_obj["scrape"]["pages_count"] = len(pages)
    entry_obj["scrape"]["pages"] = [{"url": p.get("url"), "text_len": len((p.get("text") or ""))} for p in pages]

    # 2) Build links[] objects (required: url, faq, id uuid)
    payload_links: List[Dict[str, Any]] = []
    for i, p in enumerate(pages):
        u = (p.get("url") or "").strip()
        if not u:
            continue
        payload_links.append(
            {
                "url": u,
                "id": str(uuid.uuid4()),
                "faq": f"Scraped content chunk_{i:03d}",
            }
        )

    # 3) Build full prompt (NO LIMIT)
    full_prompt = build_full_prompt(company_input.strip() or context_name, website, pages)

    # 4) Create Context payload using correct keys
    payload: Dict[str, Any] = {
        "name": context_name,
        "opening_text": f"Welcome to the Q & A session on {context_name}",
        "prompt": full_prompt,
        "interactive_style": "conversational",
    }
    if payload_links:
        payload["links"] = payload_links

    entry_obj["heygen_liveavatar"]["request_payload"] = payload

    # 5) Create context (with de-dup delete strategy)
    if not LIVEAVATAR_API_KEY:
        log_error("missing_liveavatar_api_key", "LIVEAVATAR_API_KEY is empty", xxxx=xxxx)
        entry_obj["heygen_liveavatar"]["error"] = "LIVEAVATAR_API_KEY is empty"
        return entry_obj

    try:
        resp = liveavatar_create_context(payload, xxxx=xxxx)
        entry_obj["heygen_liveavatar"]["response"] = resp
        entry_obj["heygen_liveavatar"]["status"] = "created"
    except Exception as e:
        entry_obj["heygen_liveavatar"]["status"] = "error"
        entry_obj["heygen_liveavatar"]["exception"] = str(e)
        log_error("create_context_failed", str(e), xxxx=xxxx)

    return entry_obj

# -----------------------------
# Routes
# -----------------------------
@app.get("/healthz")
def healthz():
    return jsonify({"ok": True})

@app.get("/")
def index():
    # If you already have templates, keep them.
    # Fallback simple HTML if template missing.
    try:
        return render_template("index.html")
    except Exception:
        return """
        <html><body>
        <h2>aaa-web</h2>
        <form method="post" action="/submit">
          <label>Company / Institution</label><br/>
          <input name="company" style="width:420px"/><br/><br/>
          <label>Website URL</label><br/>
          <input name="website" style="width:420px" placeholder="https://www.bescon.com.sg"/><br/><br/>
          <button type="submit">Submit</button>
        </form>
        </body></html>
        """

@app.post("/submit")
def submit():
    company = (request.form.get("company") or "").strip()
    website = (request.form.get("website") or "").strip()

    # Always derive xxxx/name from URL (NOT from company field)
    website_norm = normalize_url(website)
    xxxx = derive_xxxx_from_url(website_norm)

    submission_obj = {
        "submitted_at": utc_now_iso(),
        "company": company,
        "website": website_norm,
        "xxxx": xxxx,
    }

    # Append to GitHub DB (best effort)
    try:
        if GITHUB_REPO and GITHUB_TOKEN:
            github_append_jsonl(GITHUB_REPO, GITHUB_DB_PATH, GITHUB_BRANCH, submission_obj)
    except Exception as e:
        log_error("github_db_append_failed", str(e), xxxx=xxxx)

    # Also keep local DB (best effort)
    try:
        ensure_dir(GITHUB_DB_PATH)
        append_local_text(GITHUB_DB_PATH, json.dumps(submission_obj, ensure_ascii=False))
    except Exception:
        pass

    # Process immediately (sync)
    entry_obj = process_submission(company, website_norm)

    # Persist entry artifact to GitHub (best effort)
    try:
        if GITHUB_REPO and GITHUB_TOKEN:
            remote_path = f"data/entries/{entry_obj['id']}.json"
            github_put_file(GITHUB_REPO, remote_path, GITHUB_BRANCH, json.dumps(entry_obj, ensure_ascii=False, indent=2), None)
            # Upload errors log too
            github_upload_local_file(GITHUB_REPO, GITHUB_ERRORS_PATH, GITHUB_ERRORS_PATH, GITHUB_BRANCH)
    except Exception as e:
        log_error("github_entry_write_failed", str(e), xxxx=xxxx)

    # Show result
    try:
        return render_template("success.html", result=entry_obj)
    except Exception:
        # minimal JSON response
        return jsonify(entry_obj)

# -----------------------------
# Main
# -----------------------------
if __name__ == "__main__":
    # Local dev
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")), debug=True)
