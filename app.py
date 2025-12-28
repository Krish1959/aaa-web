import base64
import hashlib
import json
import os
import re
import sqlite3
from datetime import datetime, timezone
from urllib.parse import urlparse, urlunparse

import requests
from flask import Flask, render_template, request

from services.scraper import scrape_site
from services.text_cleaner import chunk_text_with_provenance

APP_TITLE = os.getenv("APP_TITLE", "AVATAR AGENTIC AI APPLICATION").strip() or "AVATAR AGENTIC AI APPLICATION"

# GitHub storage
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "").strip()
GITHUB_REPO = os.getenv("GITHUB_REPO", "").strip()  # e.g. "Krish1959/aaa-web"
GITHUB_BRANCH = os.getenv("GITHUB_BRANCH", "main").strip()
GITHUB_DB_PATH = os.getenv("GITHUB_DB_PATH", "data/submissions.jsonl").strip()
GITHUB_CONTEXTS_DIR = os.getenv("GITHUB_CONTEXTS_DIR", "data/contexts/by-entry").strip()

# Error log (local + GitHub)
GITHUB_HEYGEN_LOG_PATH = os.getenv("GITHUB_HEYGEN_LOG_PATH", "data/HeyGen_errors.txt").strip()
HEYGEN_ERROR_LOG_LOCAL = os.getenv("HEYGEN_ERROR_LOG_LOCAL", "data/HeyGen_errors.txt").strip()

# LiveAvatar API (CONTEXT API)
LIVEAVATAR_API_KEY = os.getenv("LIVEAVATAR_API_KEY", "").strip()
LIVEAVATAR_BASE_URL = os.getenv("LIVEAVATAR_BASE_URL", "https://api.liveavatar.com").strip().rstrip("/")

# Scraping limits
MAX_PAGES = int(os.getenv("MAX_PAGES", "5"))
MAX_CHARS_PER_PAGE = int(os.getenv("MAX_CHARS_PER_PAGE", "120000"))

# Prompt length control
MAX_CONTEXT_WORDS = int(os.getenv("MAX_CONTEXT_WORDS", "600"))

# Local SQLite (optional audit)
SQLITE_PATH = os.getenv("SQLITE_PATH", "local_submissions.db").strip()

app = Flask(__name__)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def ensure_parent_dir(path: str) -> None:
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)


def normalize_url(url: str) -> str:
    url = (url or "").strip()
    if not url:
        return ""
    if not re.match(r"^https?://", url, flags=re.I):
        url = "https://" + url

    parsed = urlparse(url)
    parsed = parsed._replace(fragment="")

    # If netloc empty, repair
    if not parsed.netloc and parsed.path:
        reparsed = urlparse("https://" + url.lstrip("/"))
        parsed = reparsed._replace(fragment="")

    return urlunparse(parsed)


def derive_xxxx_from_url(url: str) -> str:
    """
    Rule:
    - If host starts with www., xxxx = label immediately after www
    - Otherwise prefer "registrable-ish" label:
        * for com.sg/org.sg/gov.sg/edu.sg => label before com/org/gov/edu
        * else => label before TLD

    NOTE: This fixes the earlier issue where "bescon.com.sg" was returning "com".
    """
    url = normalize_url(url)
    host = (urlparse(url).hostname or "").lower().strip(".")
    if not host:
        return "site"

    labels = host.split(".")
    if labels and labels[0] == "www" and len(labels) >= 2:
        return labels[1]

    # FIX: allow 3-label ccTLD domains like "bescon.com.sg" (len==3)
    if len(labels) >= 3 and labels[-1] in {"sg", "uk", "au", "in", "my", "id", "cn", "hk", "tw"} and labels[-2] in {
        "com", "net", "org", "gov", "edu"
    }:
        return labels[-3]

    if len(labels) >= 2:
        return labels[-2]

    return labels[0]


def make_entry_id(record: dict) -> str:
    raw = json.dumps(
        {
            "created_at": record.get("created_at"),
            "email": record.get("email"),
            "web_url": record.get("web_url"),
            "company": record.get("company"),
        },
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:12]


def word_count(text: str) -> int:
    return len(re.findall(r"\b\w+\b", text or ""))


def trim_to_words(text: str, max_words: int) -> str:
    if max_words <= 0:
        return ""
    words = re.findall(r"\S+", text or "")
    if len(words) <= max_words:
        return (text or "").strip()
    return " ".join(words[:max_words]).strip() + " ..."


def init_sqlite() -> None:
    ensure_parent_dir(SQLITE_PATH)
    conn = sqlite3.connect(SQLITE_PATH)
    try:
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
                stage INTEGER NOT NULL
            )
            """
        )
        conn.commit()
    finally:
        conn.close()


def sqlite_insert_submission(record: dict) -> None:
    conn = sqlite3.connect(SQLITE_PATH)
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO submissions (created_at, name, company, email, phone, web_url, stage)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record.get("created_at"),
                record.get("name"),
                record.get("company"),
                record.get("email"),
                record.get("phone"),
                record.get("web_url"),
                int(record.get("stage") or 0),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def gh_headers() -> dict:
    return {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def github_get_file(repo: str, path: str, branch: str) -> tuple[str, str | None]:
    url = f"https://api.github.com/repos/{repo}/contents/{path}"
    r = requests.get(url, headers=gh_headers(), params={"ref": branch}, timeout=30)
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


def github_put_file(repo: str, path: str, branch: str, new_text: str, sha: str | None) -> dict:
    url = f"https://api.github.com/repos/{repo}/contents/{path}"
    payload = {
        "message": f"Update {path}",
        "content": base64.b64encode(new_text.encode("utf-8")).decode("utf-8"),
        "branch": branch,
    }
    if sha:
        payload["sha"] = sha

    # CHANGE (minimal): use json=payload for consistency; functionality unchanged
    r = requests.put(url, headers=gh_headers(), json=payload, timeout=30)
    r.raise_for_status()
    return r.json()


def append_record_to_github_jsonl(record: dict) -> dict:
    if not (GITHUB_TOKEN and GITHUB_REPO):
        return {"ok": False, "error": "Missing GITHUB_TOKEN or GITHUB_REPO env vars."}

    existing_text, sha = github_get_file(GITHUB_REPO, GITHUB_DB_PATH, GITHUB_BRANCH)
    new_line = json.dumps(record, ensure_ascii=False)
    new_text = (existing_text.rstrip("\n") + "\n" + new_line + "\n").lstrip("\n")

    result = github_put_file(GITHUB_REPO, GITHUB_DB_PATH, GITHUB_BRANCH, new_text, sha)
    commit_sha = (result.get("commit") or {}).get("sha")
    return {"ok": True, "commit": commit_sha, "path": GITHUB_DB_PATH}


def write_entry_context_json(entry_id: str, entry_obj: dict) -> dict:
    rel_path = f"{GITHUB_CONTEXTS_DIR.rstrip('/')}/{entry_id}.json"
    local_path = os.path.join("data", "contexts", "by-entry", f"{entry_id}.json")

    ensure_parent_dir(local_path)
    with open(local_path, "w", encoding="utf-8") as f:
        json.dump(entry_obj, f, indent=2, ensure_ascii=False)

    if not (GITHUB_TOKEN and GITHUB_REPO):
        return {"ok": False, "error": "Missing GITHUB_TOKEN or GITHUB_REPO.", "path": rel_path}

    _, sha = github_get_file(GITHUB_REPO, rel_path, GITHUB_BRANCH)
    result = github_put_file(
        GITHUB_REPO,
        rel_path,
        GITHUB_BRANCH,
        json.dumps(entry_obj, indent=2, ensure_ascii=False) + "\n",
        sha,
    )
    commit_sha = (result.get("commit") or {}).get("sha")
    return {"ok": True, "commit": commit_sha, "path": rel_path}


def append_line_to_github_text(line: str, path: str) -> dict:
    if not (GITHUB_TOKEN and GITHUB_REPO):
        return {"ok": False, "error": "Missing GITHUB_TOKEN or GITHUB_REPO."}

    existing_text, sha = github_get_file(GITHUB_REPO, path, GITHUB_BRANCH)
    new_text = (existing_text.rstrip("\n") + "\n" + line + "\n").lstrip("\n")

    result = github_put_file(GITHUB_REPO, path, GITHUB_BRANCH, new_text, sha)
    commit_sha = (result.get("commit") or {}).get("sha")
    return {"ok": True, "commit": commit_sha, "path": path}


def log_heygen_line(line: str) -> dict:
    ensure_parent_dir(HEYGEN_ERROR_LOG_LOCAL)
    try:
        with open(HEYGEN_ERROR_LOG_LOCAL, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass
    return append_line_to_github_text(line, GITHUB_HEYGEN_LOG_PATH)


def build_full_prompt(xxxx: str, chunks: list[dict], max_words: int) -> tuple[str, dict]:
    base = f"""
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
""".strip()

    base_wc = word_count(base)
    budget_for_content = max(max_words - base_wc - 5, 0)

    selected = []
    used_words = 0
    dropped = 0

    for c in chunks:
        txt = (c.get("text") or "").strip()
        if not txt:
            continue

        prefix = f"[{c.get('page_url','')}] " if c.get("page_url") else ""
        blob = (prefix + txt).strip()
        wc = word_count(blob)

        if used_words + wc <= budget_for_content:
            selected.append(blob)
            used_words += wc
        else:
            if not selected and budget_for_content > 0:
                selected.append(trim_to_words(blob, budget_for_content))
                used_words = budget_for_content
            dropped += 1
            break

    content = "\n\n".join(selected).strip()
    prompt = f"{base}\n{content}".strip()

    final_wc = word_count(prompt)
    truncated = dropped > 0 or final_wc > max_words

    if final_wc > max_words:
        prompt = trim_to_words(prompt, max_words)
        final_wc = word_count(prompt)
        truncated = True

    meta = {
        "max_words": max_words,
        "base_words": base_wc,
        "content_words": used_words,
        "final_words": final_wc,
        "truncated": truncated,
        "chunks_included": len(selected),
        "chunks_total": len(chunks),
    }
    return prompt, meta


def create_liveavatar_context(payload: dict) -> tuple[bool, dict, str | None]:
    """
    POST {LIVEAVATAR_BASE_URL}/v1/contexts
    Auth header: X-Api-Key: <LIVEAVATAR_API_KEY>

    NOTE:
    - LiveAvatar requires: name, opening_text, prompt
    - links (if included) must be a list of objects, not a list of strings
    """
    if not LIVEAVATAR_API_KEY:
        return False, {"message": "Missing LIVEAVATAR_API_KEY environment variable."}, "missing_api_key"

    url = f"{LIVEAVATAR_BASE_URL}/v1/contexts"
    headers = {
        "X-Api-Key": LIVEAVATAR_API_KEY,
        "accept": "application/json",
        "content-type": "application/json",
    }

    try:
        r = requests.post(url, headers=headers, json=payload, timeout=45)
    except requests.RequestException as e:
        return False, {"message": f"Request error: {e}"}, "request_exception"

    if r.status_code >= 400:
        try:
            err = r.json()
        except Exception:
            err = {"message": r.text}
        return False, {"status_code": r.status_code, "error": err}, f"http_{r.status_code}"

    try:
        data = r.json()
    except Exception:
        data = {"raw": r.text}

    return True, data, None


init_sqlite()


@app.route("/", methods=["GET"])
def index():
    return render_template("index.html", title=APP_TITLE)


@app.route("/submit", methods=["POST"])
def submit():
    HeyGen_API_error = 0
    created_at = utc_now_iso()

    name = (request.form.get("name") or "").strip()
    company = (request.form.get("company") or "").strip()
    email = (request.form.get("email") or "").strip()
    phone = (request.form.get("phone") or "").strip()
    web_url = (request.form.get("web_url") or "").strip()

    errors = []
    if not name:
        errors.append("Name is required.")
    if not company:
        errors.append("Company is required.")
    if not email:
        errors.append("Email is required.")
    if not web_url:
        errors.append("Web URL is required.")

    if errors:
        return render_template("index.html", title=APP_TITLE, errors=errors, form=request.form)

    record = {
        "created_at": created_at,
        "name": name,
        "company": company,
        "email": email,
        "phone": phone,
        "web_url": normalize_url(web_url),
        "stage": 2,
    }

    try:
        sqlite_insert_submission(record)
    except Exception:
        pass

    gh = append_record_to_github_jsonl(record)

    xxxx = derive_xxxx_from_url(record["web_url"])
    entry_id = make_entry_id(record)

    entry_obj = {
        "schema_version": "1.0",
        "entry_id": entry_id,
        "identity": {"xxxx": xxxx, "input_url": record["web_url"], "final_url": None, "final_host": None},
        "fetch": {"fetched_at_utc": created_at, "http_status": None, "redirect_chain": [], "errors": []},
        "crawl_policy": {
            "scope_rule": "same_domain_only",
            "max_pages": MAX_PAGES,
            "max_chars_per_page": MAX_CHARS_PER_PAGE,
            "max_context_words": MAX_CONTEXT_WORDS,
        },
        "links": {"internal_links_discovered": [], "internal_links_selected": []},
        "pages": [],
        "chunks": [],
        "prompt_engineering": {"source_index": [], "word_budget": MAX_CONTEXT_WORDS, "prompt_word_stats": {}},
        "heygen_liveavatar": {
            "push_status": "not_started",
            "request_payload": {},
            "response": {"context_id": None, "context_url": None, "raw": {}},
            "pushed_at_utc": None,
            "error": None,
        },
        "submission": record,
    }

    try:
        scrape_result = scrape_site(record["web_url"], max_pages=MAX_PAGES, max_chars_per_page=MAX_CHARS_PER_PAGE)

        final_url = scrape_result.get("final_url") or record["web_url"]
        entry_obj["identity"]["final_url"] = final_url
        entry_obj["identity"]["final_host"] = (urlparse(final_url).hostname or "")

        entry_obj["fetch"]["http_status"] = scrape_result.get("http_status")
        entry_obj["fetch"]["redirect_chain"] = scrape_result.get("redirect_chain", [])
        entry_obj["fetch"]["errors"] = scrape_result.get("errors", [])

        links = [x["url"] for x in scrape_result.get("links", [])]
        entry_obj["links"]["internal_links_discovered"] = links
        entry_obj["links"]["internal_links_selected"] = links[:10]

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

        full_prompt, prompt_meta = build_full_prompt(xxxx, all_chunks, MAX_CONTEXT_WORDS)
        entry_obj["prompt_engineering"]["full_prompt"] = full_prompt
        entry_obj["prompt_engineering"]["prompt_word_stats"] = prompt_meta

        if prompt_meta.get("truncated"):
            log_heygen_line(
                f"{created_at} | xxxx={xxxx} | code=text_truncated | note=text truncated to {MAX_CONTEXT_WORDS} words"
            )

    except Exception as e:
        HeyGen_API_error += 1
        entry_obj["fetch"]["errors"].append(str(e))
        log_heygen_line(f"{created_at} | xxxx={xxxx} | code=scrape_failed | error={e}")

    # ============================================================
    # LIVEAVATAR CREATE CONTEXT PAYLOAD (aligned to your working curl):
    # Required fields: name, opening_text, prompt
    # Optional: links (must be list of dict objects), interactive_style
    # ============================================================
    payload_name = company.strip() or xxxx.upper()
    payload_opening_text = f"Welcome to the Q & A session on {xxxx.upper()}"
    payload_prompt = (entry_obj.get("prompt_engineering", {}) or {}).get("full_prompt") or ""

    # FIX: links must be objects, not strings
    payload_links = [{"url": u} for u in entry_obj["links"]["internal_links_selected"]]

    entry_obj["heygen_liveavatar"]["request_payload"] = {
        "name": payload_name,
        "opening_text": payload_opening_text,
        "prompt": payload_prompt,
        "interactive_style": "conversational",
        #"links": payload_links,
    }

    if payload_prompt.strip():
        ok, resp, err_code = create_liveavatar_context(entry_obj["heygen_liveavatar"]["request_payload"])
        entry_obj["heygen_liveavatar"]["pushed_at_utc"] = created_at

        if ok:
            entry_obj["heygen_liveavatar"]["push_status"] = "success"
            context_id = (
                (resp.get("data") or {}).get("id")
                or resp.get("id")
                or (resp.get("data") or {}).get("context_id")
                or resp.get("context_id")
            )
            context_url = f"https://app.liveavatar.com/contexts/{context_id}" if context_id else None
            entry_obj["heygen_liveavatar"]["response"] = {"context_id": context_id, "context_url": context_url, "raw": resp}
        else:
            HeyGen_API_error += 1
            entry_obj["heygen_liveavatar"]["push_status"] = "failed"
            entry_obj["heygen_liveavatar"]["response"] = {"context_id": None, "context_url": None, "raw": resp}
            entry_obj["heygen_liveavatar"]["error"] = f"{resp}"
            log_heygen_line(f"{created_at} | xxxx={xxxx} | code={err_code} | error={resp}")
    else:
        HeyGen_API_error += 1
        entry_obj["heygen_liveavatar"]["push_status"] = "failed"
        entry_obj["heygen_liveavatar"]["pushed_at_utc"] = created_at
        entry_obj["heygen_liveavatar"]["error"] = "Skipped push: prompt is empty (scrape likely failed)."
        log_heygen_line(f"{created_at} | xxxx={xxxx} | code=empty_prompt | error=Skipped push: empty prompt")

    ctx_write = write_entry_context_json(entry_id, entry_obj)

    if HeyGen_API_error == 0:
        log_heygen_line(f"{created_at} | xxxx={xxxx} | No error")
    else:
        log_heygen_line(f"{created_at} | xxxx={xxxx} | Total errors this run = {HeyGen_API_error}")

    return render_template("success.html", title=APP_TITLE, record=record, gh=gh, ctx=ctx_write, entry=entry_obj)


if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port)
