# app.py
# AAA-Web: Form submission -> GitHub JSONL -> Scrape + clean + chunk -> Build LiveAvatar Context -> Push logs/artifacts back to GitHub

from __future__ import annotations

import base64
import json
import os
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

import requests
from flask import Flask, render_template, request

# Your services (kept as-is; assumed present in your repo)
from services.scraper import scrape_site_map  # type: ignore
from services.text_cleaner import clean_text, chunk_text_with_provenance  # type: ignore

# --------------------------------------------------------------------------------------
# Flask
# --------------------------------------------------------------------------------------

app = Flask(__name__)

# --------------------------------------------------------------------------------------
# ENV / CONFIG
# --------------------------------------------------------------------------------------

GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "").strip()
GITHUB_REPO = os.getenv("GITHUB_REPO", "").strip()          # e.g. "Krish1959/aaa-web"
GITHUB_BRANCH = os.getenv("GITHUB_BRANCH", "main").strip()
GITHUB_DB_PATH = os.getenv("GITHUB_DB_PATH", "data/submissions.jsonl").strip()

# IMPORTANT: LiveAvatar API host (NOT app.liveavatar.com)
LIVEAVATAR_BASE_URL = os.getenv("LIVEAVATAR_BASE_URL", "https://api.liveavatar.com").strip().rstrip("/")
LIVEAVATAR_API_KEY = os.getenv("LIVEAVATAR_API_KEY", "").strip()

# Error log file (stored in repo so you can audit every run)
HEYGEN_ERRORS_PATH = os.getenv("HEYGEN_ERRORS_PATH", "data/HeyGen_errors.txt").strip()

# Scrape tuning (no prompt word-limit enforced)
SCRAPE_MAX_PAGES = int(os.getenv("SCRAPE_MAX_PAGES", "12").strip() or "12")
CHUNK_MAX_CHARS = int(os.getenv("CHUNK_MAX_CHARS", "1800").strip() or "1800")
CHUNK_OVERLAP = int(os.getenv("CHUNK_OVERLAP", "150").strip() or "150")

# Network timeouts
HTTP_TIMEOUT = int(os.getenv("HTTP_TIMEOUT", "45").strip() or "45")

# --------------------------------------------------------------------------------------
# Global run counter
# --------------------------------------------------------------------------------------

HeyGen_API_error = 0

# --------------------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------------------


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def safe_str(x: Any) -> str:
    return "" if x is None else str(x)


def normalize_spaces(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def domain_token(url: str) -> str:
    """
    Domain rule:
    - 'xxxx' is the word immediately after 'www' (or the first subdomain if no 'www') for naming/paths.
    """
    try:
        host = (urlparse(url).hostname or "").lower()
        host = host.split(":")[0]
        if not host:
            return "unknown"

        parts = [p for p in host.split(".") if p]
        if not parts:
            return "unknown"

        if parts[0] == "www" and len(parts) >= 2:
            return parts[1]
        return parts[0]
    except Exception:
        return "unknown"


def require_env(value: str, name: str) -> None:
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")


# --------------------------------------------------------------------------------------
# GitHub helpers (Contents API)
# --------------------------------------------------------------------------------------


def gh_headers() -> Dict[str, str]:
    require_env(GITHUB_TOKEN, "GITHUB_TOKEN")
    return {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "aaa-web",
    }


def github_get_file(repo: str, path: str, branch: str) -> Tuple[str, Optional[str]]:
    """
    Returns (text_content, sha) if exists, else ("", None) if not found.
    """
    url = f"https://api.github.com/repos/{repo}/contents/{path}"
    r = requests.get(url, headers=gh_headers(), params={"ref": branch}, timeout=HTTP_TIMEOUT)
    if r.status_code == 404:
        return "", None
    r.raise_for_status()
    data = r.json()
    content_b64 = data.get("content", "") or ""
    sha = data.get("sha")
    try:
        text = base64.b64decode(content_b64).decode("utf-8", errors="replace")
    except Exception:
        text = ""
    return text, sha


def github_put_file(repo: str, path: str, branch: str, new_text: str, sha: str | None) -> Dict[str, Any]:
    """
    Create or update a file in GitHub via Contents API.
    """
    url = f"https://api.github.com/repos/{repo}/contents/{path}"
    payload: Dict[str, Any] = {
        "message": f"Update {path}",
        "content": base64.b64encode(new_text.encode("utf-8")).decode("utf-8"),
        "branch": branch,
    }
    if sha:
        payload["sha"] = sha

    r = requests.put(url, headers=gh_headers(), data=json.dumps(payload), timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    return r.json()


def github_append_line(repo: str, path: str, branch: str, line: str) -> Dict[str, Any]:
    """
    Append a single line to a text file in GitHub (creates if missing).
    """
    existing, sha = github_get_file(repo, path, branch)
    if existing and not existing.endswith("\n"):
        existing += "\n"
    new_text = existing + line + "\n"
    return github_put_file(repo, path, branch, new_text, sha)


# --------------------------------------------------------------------------------------
# HeyGen/LiveAvatar error log helpers
# --------------------------------------------------------------------------------------


def log_heygen_line(xxxx: str, code: str, note: Optional[str] = None, error_obj: Optional[dict] = None) -> None:
    """
    Writes a single line to data/HeyGen_errors.txt and pushes to GitHub immediately.
    """
    global HeyGen_API_error

    ts = utc_now_iso()
    if error_obj is not None:
        HeyGen_API_error += 1
        line = f"{ts} | xxxx={xxxx} | code={code} | error={error_obj}"
    else:
        line = f"{ts} | xxxx={xxxx} | code={code} | note={note or ''}".rstrip()

    # Always push log line to GitHub
    if GITHUB_REPO and GITHUB_TOKEN:
        github_append_line(GITHUB_REPO, HEYGEN_ERRORS_PATH, GITHUB_BRANCH, line)


def finalize_run_log(xxxx: str, ok: bool) -> None:
    """
    Always record end-of-run summary and push.
    """
    global HeyGen_API_error
    ts = utc_now_iso()

    if ok and HeyGen_API_error == 0:
        line = f"{ts} | xxxx={xxxx} | No error"
        if GITHUB_REPO and GITHUB_TOKEN:
            github_append_line(GITHUB_REPO, HEYGEN_ERRORS_PATH, GITHUB_BRANCH, line)
    else:
        line = f"{ts} | xxxx={xxxx} | Total errors this run = {HeyGen_API_error}"
        if GITHUB_REPO and GITHUB_TOKEN:
            github_append_line(GITHUB_REPO, HEYGEN_ERRORS_PATH, GITHUB_BRANCH, line)


# --------------------------------------------------------------------------------------
# LiveAvatar API
# --------------------------------------------------------------------------------------


def liveavatar_headers() -> Dict[str, str]:
    require_env(LIVEAVATAR_API_KEY, "LIVEAVATAR_API_KEY")
    return {
        "accept": "application/json",
        "content-type": "application/json",
        # Header spelling that you proved works in PowerShell curl tests
        "X-Api-Key": LIVEAVATAR_API_KEY,
    }


def liveavatar_create_context(payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    POST /v1/contexts
    Required fields (per docs / your working curl tests):
      - name
      - prompt
      - opening_text
    Optional:
      - interactive_style
      - links (array of objects with required url+faq, optional id but if provided must be valid UUID)
    """
    url = f"{LIVEAVATAR_BASE_URL}/v1/contexts"
    r = requests.post(url, headers=liveavatar_headers(), json=payload, timeout=HTTP_TIMEOUT)
    if not r.ok:
        raise requests.HTTPError(f"LiveAvatar HTTP {r.status_code}: {r.text}", response=r)
    return r.json()


# --------------------------------------------------------------------------------------
# Prompt + Links builders (NO word limit enforced)
# --------------------------------------------------------------------------------------

@dataclass
class Chunk:
    chunk_id: str
    url: str
    text: str


def build_chunks(scrape_result: Dict[str, Any]) -> List[Chunk]:
    """
    Build chunks from every scraped page (no artificial cap here).
    Assumes scrape_result["pages"] is a list of dicts with at least: url, raw_text (or text).
    """
    pages = scrape_result.get("pages") or []
    all_chunks: List[Chunk] = []
    i = 0

    for page in pages:
        page_url = safe_str(page.get("url") or page.get("page_url") or "")
        raw = safe_str(page.get("raw_text") or page.get("text") or page.get("content") or "")
        if not page_url or not raw:
            continue

        cleaned = clean_text(raw)
        if not cleaned:
            continue

        # chunk_text_with_provenance in your repo may return list[str] (as earlier),
        # or list[dict]. We'll support both safely.
        pieces = chunk_text_with_provenance(
            cleaned,
            max_chars=CHUNK_MAX_CHARS,
            overlap=CHUNK_OVERLAP,
        )

        if isinstance(pieces, list):
            for p in pieces:
                if isinstance(p, dict):
                    chunk_text = safe_str(p.get("text") or "")
                else:
                    chunk_text = safe_str(p)

                chunk_text = chunk_text.strip()
                if not chunk_text:
                    continue

                chunk_id = f"{i:03d}"
                all_chunks.append(Chunk(chunk_id=chunk_id, url=page_url, text=chunk_text))
                i += 1

    return all_chunks


def build_prompt(persona_text: str, company: str, base_url: str, chunks: List[Chunk]) -> str:
    """
    Builds one big prompt containing all chunks (no truncation).
    """
    lines: List[str] = []

    lines.append("## PERSONA / ROLE")
    lines.append(persona_text.strip())
    lines.append("")

    lines.append("## COMPANY")
    if company:
        lines.append(f"Company: {company}")
    if base_url:
        lines.append(f"Website: {base_url}")
    lines.append("")

    lines.append("## SOURCE MATERIAL (SCRAPED)")
    lines.append("Use the following scraped material as the knowledge base. Cite the source URL when helpful.")
    lines.append("")

    if not chunks:
        lines.append("(No scraped content was available.)")
        return "\n".join(lines).strip()

    # Group by page URL for readability
    by_url: Dict[str, List[Chunk]] = {}
    for c in chunks:
        by_url.setdefault(c.url, []).append(c)

    for page_url, page_chunks in by_url.items():
        lines.append(f"### {page_url}")
        for c in page_chunks:
            lines.append(f"[{c.chunk_id}] {c.text}")
            lines.append("")
        lines.append("")

    return "\n".join(lines).strip()


def build_links_from_chunks(chunks: List[Chunk]) -> List[Dict[str, Any]]:
    """
    LiveAvatar 'links' must be: array of objects, each with required url + faq, and id if included must be UUID.

    We'll create one link object per URL:
      {
        "url": "<page_url>",
        "id": "<uuid4>",
        "faq": "Scraped content included in prompt. Chunk IDs: 000,001,..."
      }
    """
    by_url: Dict[str, List[str]] = {}
    for c in chunks:
        by_url.setdefault(c.url, []).append(c.chunk_id)

    link_objs: List[Dict[str, Any]] = []
    for page_url, ids in by_url.items():
        ids_sorted = sorted(ids)
        # Keep faq human-readable (not too long)
        if len(ids_sorted) <= 12:
            chunk_part = ", ".join(ids_sorted)
        else:
            chunk_part = ", ".join(ids_sorted[:6]) + " ... " + ", ".join(ids_sorted[-3:])

        link_objs.append({
            "url": page_url,
            "id": str(uuid.uuid4()),  # MUST be valid UUID if present
            "faq": f"Scraped content included in prompt. Chunk IDs: {chunk_part}",
        })

    return link_objs


# --------------------------------------------------------------------------------------
# Core submit pipeline
# --------------------------------------------------------------------------------------


def append_submission_jsonl(record: Dict[str, Any]) -> None:
    """
    Append record to GitHub JSONL file.
    """
    require_env(GITHUB_REPO, "GITHUB_REPO")
    line = json.dumps(record, ensure_ascii=False)
    existing, sha = github_get_file(GITHUB_REPO, GITHUB_DB_PATH, GITHUB_BRANCH)
    if existing and not existing.endswith("\n"):
        existing += "\n"
    new_text = existing + line + "\n"
    github_put_file(GITHUB_REPO, GITHUB_DB_PATH, GITHUB_BRANCH, new_text, sha)


def save_entry_artifacts(entry_id: str, xxxx: str, entry_obj: Dict[str, Any]) -> None:
    """
    Save detailed per-entry JSON plus a "latest by domain token" JSON.
    """
    require_env(GITHUB_REPO, "GITHUB_REPO")

    per_entry_path = f"data/contexts/by-entry/{entry_id}.json"
    latest_path = f"data/contexts/{xxxx}.json"

    per_entry_text = json.dumps(entry_obj, indent=2, ensure_ascii=False)
    latest_text = json.dumps(entry_obj, indent=2, ensure_ascii=False)

    # Put per-entry
    existing, sha = github_get_file(GITHUB_REPO, per_entry_path, GITHUB_BRANCH)
    github_put_file(GITHUB_REPO, per_entry_path, GITHUB_BRANCH, per_entry_text, sha)

    # Put latest
    existing2, sha2 = github_get_file(GITHUB_REPO, latest_path, GITHUB_BRANCH)
    github_put_file(GITHUB_REPO, latest_path, GITHUB_BRANCH, latest_text, sha2)


# --------------------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------------------


@app.get("/")
def index():
    return render_template("index.html")


@app.post("/submit")
def submit():
    global HeyGen_API_error
    HeyGen_API_error = 0  # reset per run

    created_at = utc_now_iso()

    name = normalize_spaces(request.form.get("name", ""))
    company = normalize_spaces(request.form.get("company", ""))
    email = normalize_spaces(request.form.get("email", ""))
    phone = normalize_spaces(request.form.get("phone", ""))
    web_url = normalize_spaces(request.form.get("web_url", ""))

    xxxx = domain_token(web_url)

    # Basic validation
    if not company or not web_url:
        log_heygen_line(xxxx, "input_error", note="Missing required company or web_url")
        finalize_run_log(xxxx, ok=False)
        return render_template("success.html", record={"error": "Missing company or web_url"}, context_result=None)

    # Entry ID for traceability
    entry_id = uuid.uuid4().hex[:12]

    # -----------------------------
    # Stage 1: Store submission
    # -----------------------------
    record = {
        "created_at": created_at,
        "entry_id": entry_id,
        "name": name,
        "company": company,
        "email": email,
        "phone": phone,
        "web_url": web_url,
        "stage": 1,
    }

    try:
        append_submission_jsonl(record)
    except Exception as e:
        log_heygen_line(xxxx, "github_write_failed", error_obj={"error": str(e)})
        finalize_run_log(xxxx, ok=False)
        return render_template("success.html", record=record, context_result=None)

    # -----------------------------
    # Stage 2: Scrape + clean + chunk
    # -----------------------------
    entry_obj: Dict[str, Any] = {
        "created_at": created_at,
        "entry_id": entry_id,
        "form": record,
        "links": {},
        "scrape": {},
        "chunks": [],
        "heygen_liveavatar": {},
    }

    try:
        scrape_result = scrape_site_map(web_url, max_pages=SCRAPE_MAX_PAGES)  # keep your service behavior
        entry_obj["scrape"] = scrape_result
    except Exception as e:
        log_heygen_line(xxxx, "scrape_failed", error_obj={"error": str(e), "web_url": web_url})
        finalize_run_log(xxxx, ok=False)
        try:
            save_entry_artifacts(entry_id, xxxx, entry_obj)
        except Exception:
            pass
        return render_template("success.html", record=record, context_result=None)

    try:
        chunks = build_chunks(entry_obj["scrape"])
        entry_obj["chunks"] = [
            {"chunk_id": c.chunk_id, "url": c.url, "text": c.text} for c in chunks
        ]
    except Exception as e:
        log_heygen_line(xxxx, "chunk_failed", error_obj={"error": str(e)})
        finalize_run_log(xxxx, ok=False)
        try:
            save_entry_artifacts(entry_id, xxxx, entry_obj)
        except Exception:
            pass
        return render_template("success.html", record=record, context_result=None)

    # -----------------------------
    # Stage 3: Build Context payload (NO word limit; include all internal URLs + scraped texts)
    # -----------------------------

    persona_text = (
        "You are a passionate teacher acting as a mediator for learners.\n"
        "Scope & safety:\n"
        "Keep the conversation focused exclusively on bescon and related information only.\n"
        "If asked about any other topic, reply with exactly:\n"
        "\"Let us focus only on bescon only\"\n"
        "Style & tone:\n"
        "Use short replies: 1–3 concise sentences or a short paragraph.\n"
        "Be clear, friendly, and encouraging."
    )

    payload_name = f"{company}".strip() or f"{xxxx}".strip()
    payload_opening_text = f"Welcome to the Q & A session on {company}".strip()

    payload_prompt = build_prompt(
        persona_text=persona_text,
        company=company,
        base_url=web_url,
        chunks=[Chunk(**c) if isinstance(c, dict) else c for c in entry_obj["chunks"]]  # type: ignore
    )

    # Build correct link objects (url + faq required; id must be UUID if present)
    payload_links = build_links_from_chunks([Chunk(**c) for c in entry_obj["chunks"]])  # type: ignore

    # IMPORTANT: Use the exact API keys (NO undefined fields)
    request_payload: Dict[str, Any] = {
        "name": payload_name,
        "opening_text": payload_opening_text,
        "prompt": payload_prompt,
        "interactive_style": "conversational",
    }
    if payload_links:
        request_payload["links"] = payload_links

    entry_obj["heygen_liveavatar"]["request_payload"] = request_payload

    # -----------------------------
    # Stage 4: Create context via LiveAvatar API
    # -----------------------------
    context_result: Optional[Dict[str, Any]] = None
    ok = False

    try:
        require_env(LIVEAVATAR_API_KEY, "LIVEAVATAR_API_KEY")
        result = liveavatar_create_context(request_payload)
        entry_obj["heygen_liveavatar"]["response"] = result
        context_result = result
        ok = True
    except requests.HTTPError as e:
        resp = getattr(e, "response", None)
        status = getattr(resp, "status_code", None)
        text = getattr(resp, "text", "")
        err_obj = {
            "status_code": status,
            "error": safe_str(text)[:2000],  # keep log bounded
        }
        log_heygen_line(xxxx, "http_error", error_obj=err_obj)
        entry_obj["heygen_liveavatar"]["error"] = err_obj
    except Exception as e:
        err_obj = {"error": str(e)}
        log_heygen_line(xxxx, "unexpected_error", error_obj=err_obj)
        entry_obj["heygen_liveavatar"]["error"] = err_obj

    # -----------------------------
    # Stage 5: Save artifacts + finalize logs
    # -----------------------------
    try:
        save_entry_artifacts(entry_id, xxxx, entry_obj)
    except Exception as e:
        log_heygen_line(xxxx, "github_artifact_save_failed", error_obj={"error": str(e)})

    finalize_run_log(xxxx, ok=ok)

    return render_template("success.html", record=record, context_result=context_result)


# --------------------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------------------

if __name__ == "__main__":
    # Local dev: python app.py
    # Render/Gunicorn: uses "gunicorn app:app"
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "10000")), debug=False)
