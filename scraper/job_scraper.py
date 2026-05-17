# ============================================================
# job_scraper.py  —  FIXED VERSION
# Changes from original are marked with:  # ← CHANGED
#                                          # ← NEW
# ============================================================

# ─── CHANGE 1 ───────────────────────────────────────────────
# __future__ MUST be the very first import. Always. No exceptions.
# Your submitted file had ThreadPoolExecutor import BEFORE this — that causes SyntaxError.
from __future__ import annotations
# ────────────────────────────────────────────────────────────

import argparse
import datetime as dt
import hashlib
import io
import json
import logging
import re
import signal                                      # ← NEW (for per-source timeout)
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed  # ← MOVED here (was line 1, wrong)
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

import requests
import yaml
from bs4 import BeautifulSoup
from dateutil import parser as date_parser
from pypdf import PdfReader


logging.getLogger("pypdf").setLevel(logging.ERROR)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

NEGATIVE_STATUS_PATTERNS = re.compile(
    r"\b(expired|closed|cancelled|canceled|withdrawn|archived|not\s+active)\b",
    re.IGNORECASE,
)

DEADLINE_PATTERNS = [
    re.compile(
        r"(?:last\s+date|deadline|apply\s+by|valid\s+till|closing\s+date|"
        r"submission\s+date|walk[-\s]?in\s+date)[^\n\r:]{0,40}[:\-]?\s*"
        r"([0-3]?\d[./\-\s][A-Za-z]{3,9}[./\-\s]\d{2,4}|"
        r"[0-3]?\d[./\-][01]?\d[./\-]\d{2,4}|"
        r"[A-Za-z]{3,9}\s+[0-3]?\d,?\s+\d{4})",
        re.IGNORECASE,
    ),
    re.compile(
        r"([0-3]?\d[./\-][01]?\d[./\-]\d{4})",
        re.IGNORECASE,
    ),
]

LOCATION_KEYWORDS = [
    "remote", "work from home", "india", "kolkata", "kharagpur",
    "mumbai", "delhi", "new delhi", "bangalore", "bengaluru",
    "hyderabad", "chennai", "pune", "noida", "gurugram",
]

JOB_TYPE_RULES = [
    ("internship", ["internship", "intern"]),
    ("fellowship", ["fellowship", "fellow"]),
    ("research",   ["research", "jrf", "srf", "project associate", "research associate"]),
    ("project",    ["project assistant", "project position", "project staff"]),
    ("scholarship",["scholarship", "studentship"]),
    ("contract",   ["contract", "temporary", "consultant"]),
    ("faculty",    ["faculty", "professor"]),
]

CATEGORY_RULES = [
    ("government", ["isro", "drdo", "gov", "nic", "nielit", "icar", "dst", "csir", "cdac"]),
    ("research",   ["research", "fellowship", "jrf", "srf", "project associate"]),
    ("internship", ["internship", "intern"]),
    ("tech",       ["software", "developer", "engineer", "data", "machine learning", "ai", "cloud"]),
]

SALARY_PATTERN = re.compile(
    r"(?:₹|rs\.?|inr)\s?[0-9][0-9,]*(?:\s?[-–]\s?(?:₹|rs\.?|inr)?\s?[0-9][0-9,]*)?|"
    r"[0-9][0-9,]*\s?(?:per month|per annum|/month|/year|lpa|lakhs?)",
    re.IGNORECASE,
)


# ─── NO CHANGE: Source and Job dataclasses are identical ────
@dataclass
class Source:
    name: str
    url: str
    freshness_days: int
    include_patterns: list[str]
    category: str = "general"
    keywords: list[str] = field(default_factory=list)
    search_urls: list[str] = field(default_factory=list)


@dataclass
class Job:
    title: str
    organization: str
    url: str
    description: str = ""
    deadline: dt.date | None = None
    posted_date: dt.date | None = None
    location: str = ""
    job_type: str = "job"
    salary: str = ""
    source_id: str = ""
    is_pdf: bool = False
    pdf_url: str = ""
    tags: list[str] = field(default_factory=list)
    experience: str = ""
    category: str = "general"
    score: int = 0
    matched_keywords: list[str] = field(default_factory=list)
    checked_at: str = ""

    @property
    def unique_id(self) -> str:
        payload = f"{self.organization}|{self.url}|{self.title}".encode("utf-8", errors="ignore")
        return hashlib.sha256(payload).hexdigest()[:16]

    def to_dict(self) -> dict[str, Any]:
        return {
            "id":               self.unique_id,
            "source":           self.organization,
            "organization":     self.organization,
            "title":            self.title[:400],
            "url":              self.url,
            "score":            self.score,
            "matched_keywords": self.matched_keywords,
            "deadline":         self.deadline.isoformat() if self.deadline else None,
            "posted_date":      self.posted_date.isoformat() if self.posted_date else None,
            "content_type":     "pdf" if self.is_pdf else "html",
            "summary":          self.description[:1000],
            "description":      self.description[:1000],
            "location":         self.location,
            "job_type":         self.job_type,
            "salary":           self.salary,
            "source_id":        self.source_id,
            "is_pdf":           self.is_pdf,
            "pdf_url":          self.pdf_url,
            "tags":             self.tags,
            "experience":       self.experience,
            "category":         self.category,
            "checked_at":       self.checked_at or utc_now().isoformat(),
        }


# ─── NO CHANGE: utility functions ───────────────────────────
def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def stable_id(source: str, url: str, title: str) -> str:
    payload = f"{source}|{url}|{title}".encode("utf-8", errors="ignore")
    return hashlib.sha256(payload).hexdigest()[:16]


def load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"Missing config file: {path}")
    with path.open("r", encoding="utf-8") as handle:
        return yaml.safe_load(handle) or {}


def get_session() -> requests.Session:
    session = requests.Session()
    session.headers.update({
        "User-Agent":      USER_AGENT,
        "Accept-Language": "en-US,en;q=0.9",
        "Accept":          "text/html,application/xhtml+xml,application/xml;q=0.9,application/pdf,*/*;q=0.8",
    })
    return session


# ─── CHANGE 2 ───────────────────────────────────────────────
# Reduced timeout: 18 → 8 seconds
# Reduced attempts: 2 → 1
# Why: 18s × 2 attempts = 36s wasted per bad link. With 20 links per source
#      and 25 sources that's potentially 18,000 seconds of wasted waiting.
#      8s is enough for any good website. Bad websites are not worth retrying.
def fetch(
    session: requests.Session,
    url: str,
    timeout: int = 8,    # ← CHANGED: was 18
    attempts: int = 1,   # ← CHANGED: was 2
) -> requests.Response:
    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            response = session.get(url, timeout=timeout, allow_redirects=True)
            response.raise_for_status()
            return response
        except Exception as exc:
            last_error = exc
            if attempt < attempts - 1:
                time.sleep(1 + attempt)
    if last_error:
        raise last_error
    raise RuntimeError(f"Failed to fetch {url}")
# ────────────────────────────────────────────────────────────


# ─── NO CHANGE: parsing helpers ─────────────────────────────
def is_probably_pdf(url: str, content_type: str = "") -> bool:
    return urlparse(url).path.lower().endswith(".pdf") or "pdf" in content_type.lower()


def extract_pdf_text(content: bytes) -> str:
    reader = PdfReader(io.BytesIO(content))
    parts: list[str] = []
    for page in reader.pages[:8]:
        try:
            parts.append(page.extract_text() or "")
        except Exception:
            continue
    return normalize_text(" ".join(parts))


def extract_html_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for element in soup(["script", "style", "noscript"]):
        element.decompose()
    return normalize_text(soup.get_text(" "))


def candidate_links(source: Source, html: str, base_url: str) -> list[dict[str, str]]:
    soup = BeautifulSoup(html, "html.parser")
    candidates: list[dict[str, str]] = []
    patterns = [pattern.lower() for pattern in source.include_patterns]

    for anchor in soup.find_all("a", href=True):
        href        = anchor.get("href", "")
        title       = normalize_text(anchor.get_text(" "))
        absolute_url = urljoin(base_url, href)
        combined    = f"{title} {absolute_url}".lower()

        if not title and not absolute_url.lower().endswith(".pdf"):
            continue
        if patterns and not any(pattern in combined for pattern in patterns):
            continue

        candidates.append({
            "title": title or Path(urlparse(absolute_url).path).name or absolute_url,
            "url":   absolute_url,
        })

    if not candidates:
        page_title = normalize_text(soup.title.get_text(" ")) if soup.title else source.name
        candidates.append({"title": page_title, "url": base_url})

    seen:   set[str]         = set()
    unique: list[dict[str, str]] = []
    for candidate in candidates:
        if candidate["url"] in seen:
            continue
        seen.add(candidate["url"])
        unique.append(candidate)
    return unique[:80]


def parse_date(value: str) -> dt.date | None:
    value = normalize_text(value)
    if not value:
        return None
    try:
        parsed = date_parser.parse(value, dayfirst=True, fuzzy=True)
    except (ValueError, OverflowError):
        return None
    return parsed.date()


def extract_deadline(text: str) -> dt.date | None:
    for pattern in DEADLINE_PATTERNS:
        for match in pattern.finditer(text[:8000]):
            parsed = parse_date(match.group(1))
            if parsed:
                return parsed
    return None


def extract_posted_date(text: str) -> dt.date | None:
    for pattern in [
        re.compile(r"(?:posted|published|dated|date)[^\n\r:]{0,30}[:\-]?\s*([0-3]?\d[./\-][01]?\d[./\-]\d{2,4})", re.IGNORECASE),
        re.compile(r"(?:posted|published|dated|date)[^\n\r:]{0,30}[:\-]?\s*([0-3]?\d\s+[A-Za-z]{3,9}\s+\d{4})", re.IGNORECASE),
    ]:
        match = pattern.search(text[:5000])
        if match:
            parsed = parse_date(match.group(1))
            if parsed:
                return parsed
    return None


def infer_location(text: str) -> str:
    lowered = text.lower()
    found   = []
    for location in LOCATION_KEYWORDS:
        if location in lowered:
            found.append("Bengaluru" if location == "bangalore" else location.title())
    return ", ".join(sorted(set(found))[:4])


def infer_job_type(text: str) -> str:
    lowered = text.lower()
    for job_type, keywords in JOB_TYPE_RULES:
        if any(keyword in lowered for keyword in keywords):
            return job_type
    return "job"


def infer_category(text: str, source: Source) -> str:
    lowered = f"{source.name} {source.url} {text}".lower()
    for category, keywords in CATEGORY_RULES:
        if any(keyword in lowered for keyword in keywords):
            return category
    return source.category or "general"


def extract_salary(text: str) -> str:
    match = SALARY_PATTERN.search(text[:8000])
    return normalize_text(match.group(0)) if match else ""


def score_match(text: str, profile: dict[str, Any]) -> tuple[int, list[str]]:
    candidate        = profile.get("candidate", {})
    include_keywords = candidate.get("include_keywords", []) or []
    location_keywords = candidate.get("location_keywords", []) or []
    resume_keywords  = candidate.get("resume_keywords", []) or []
    exclude_keywords = candidate.get("exclude_keywords", []) or []

    lowered = text.lower()
    matched: list[str] = []
    score = 0

    for keyword in include_keywords:
        if keyword.lower() in lowered:
            score += 2
            matched.append(keyword)
    for keyword in resume_keywords:
        if keyword.lower() in lowered:
            score += 1
            matched.append(keyword)
    for keyword in location_keywords:
        if keyword.lower() in lowered:
            score += 1
            matched.append(keyword)
    for keyword in exclude_keywords:
        if keyword.lower() in lowered:
            score -= 4

    return score, sorted(set(matched), key=str.lower)


def is_live_notice(text: str, deadline: dt.date | None, freshness_days: int) -> bool:
    today   = utc_now().date()
    lowered = text.lower()
    if NEGATIVE_STATUS_PATTERNS.search(lowered):
        return False
    if deadline:
        return deadline >= today
    found_dates  = [parse_date(match.group(0)) for match in re.finditer(r"\b[0-3]?\d[./\-][01]?\d[./\-]\d{4}\b", text[:6000])]
    recent_dates = [date for date in found_dates if date and date <= today]
    if recent_dates:
        newest = max(recent_dates)
        return (today - newest).days <= freshness_days
    return True


def summarize(text: str, max_chars: int = 320) -> str:
    cleaned = normalize_text(text)
    return cleaned[:max_chars].rsplit(" ", 1)[0] if len(cleaned) > max_chars else cleaned


# ─── CHANGE 3 ───────────────────────────────────────────────
# Added per-source hard timeout using signal.alarm (Linux/macOS only —
# GitHub Actions runners are Linux so this works perfectly in CI).
# If a source hangs for more than SOURCE_TIMEOUT_SECONDS, it is skipped
# automatically so it cannot block the rest of the scrape.
SOURCE_TIMEOUT_SECONDS = 90  # ← NEW: each source gets max 90 seconds total

class _SourceTimeout(Exception):
    """Raised when a single source exceeds its time budget."""

def _alarm_handler(signum: int, frame: object) -> None:
    raise _SourceTimeout()
# ────────────────────────────────────────────────────────────


# ─── CHANGE 4 ───────────────────────────────────────────────
# This is the core fix. The old version fetched every link one by one
# inside a plain for loop — completely sequential, very slow.
#
# The new version:
#   1. Defines _fetch_one_link() as a standalone function that handles
#      one link end-to-end (fetch → parse → score → build Job).
#   2. Uses ThreadPoolExecutor(max_workers=8) to run 8 links in parallel.
#   3. Uses as_completed() to collect results as they finish.
#
# Speed improvement: roughly 6-8× faster on the link-fetching step.
def _fetch_one_link(
    link: dict[str, str],
    session: requests.Session,
    source: Source,
    profile: dict[str, Any],
) -> tuple[dict[str, Any] | None, dict[str, str] | None]:
    """
    Fetch, parse, and score a single candidate link.
    Returns (job_dict, None) on success, (None, error_dict) on failure,
    or (None, None) if the notice is stale or below score threshold.
    """
    try:
        response     = fetch(session, link["url"])
        content_type = response.headers.get("content-type", "")

        if is_probably_pdf(link["url"], content_type):
            body_text          = extract_pdf_text(response.content)
            content_type_label = "pdf"
        else:
            body_text          = extract_html_text(response.text)
            content_type_label = "html"

        full_text = normalize_text(f"{link['title']} {body_text}")
        deadline  = extract_deadline(full_text)

        if not is_live_notice(full_text, deadline, source.freshness_days):
            return None, None   # stale — skip silently

        score, matched_keywords = score_match(full_text, profile)
        minimum_score = int(profile.get("candidate", {}).get("minimum_score", 1))
        if score < minimum_score:
            return None, None   # below threshold — skip silently

        job = Job(
            title           = link["title"],
            organization    = source.name,
            url             = link["url"],
            description     = summarize(full_text),
            deadline        = deadline,
            posted_date     = extract_posted_date(full_text),
            location        = infer_location(full_text),
            job_type        = infer_job_type(full_text),
            salary          = extract_salary(full_text),
            source_id       = stable_id(source.name, source.url, source.name),
            is_pdf          = content_type_label == "pdf",
            pdf_url         = link["url"] if content_type_label == "pdf" else "",
            tags            = matched_keywords[:16],
            category        = infer_category(full_text, source),
            score           = score,
            matched_keywords= matched_keywords,
            checked_at      = utc_now().isoformat(),
        )
        return job.to_dict(), None

    except Exception as exc:
        return None, {"source": source.name, "url": link["url"], "error": str(exc)}


def scrape_source(
    session:   requests.Session,
    source:    Source,
    profile:   dict[str, Any],
    max_links: int,
) -> tuple[list[dict[str, Any]], list[dict[str, str]], dict[str, Any]]:

    jobs:   list[dict[str, Any]]  = []
    errors: list[dict[str, str]]  = []
    stats:  dict[str, Any]        = {
        "source":     source.name,
        "url":        source.url,
        "candidates": 0,
        "matches":    0,
        "errors":     0,
    }

    # Step 1 — fetch listing pages and collect candidate links (unchanged)
    listing_urls = [source.url, *source.search_urls]
    links: list[dict[str, str]] = []
    for listing_url in dict.fromkeys(listing_urls):
        try:
            source_response = fetch(session, listing_url)
            links.extend(candidate_links(source, source_response.text, listing_url))
        except Exception as exc:
            errors.append({"source": source.name, "url": listing_url, "error": str(exc)})

    if not links:
        stats["errors"] = len(errors)
        return [], errors or [{"source": source.name, "url": source.url, "error": "No candidate links found"}], stats

    # Deduplicate and cap at max_links (unchanged)
    seen_link_urls: set[str] = set()
    links = [
        link for link in links
        if not (link["url"] in seen_link_urls or seen_link_urls.add(link["url"]))  # type: ignore[func-returns-value]
    ][:max_links]
    stats["candidates"] = len(links)

    # ── Step 2 — CHANGED: fetch all links concurrently ──────
    # Old code: for link in links: fetch(link) ... (sequential)
    # New code: ThreadPoolExecutor fetches 8 links at the same time.
    with ThreadPoolExecutor(max_workers=8) as executor:
        future_to_link = {
            executor.submit(_fetch_one_link, link, session, source, profile): link
            for link in links
        }
        for future in as_completed(future_to_link):
            job_dict, error = future.result()
            if job_dict:
                jobs.append(job_dict)
            if error:
                errors.append(error)
    # ─────────────────────────────────────────────────────────

    stats["matches"] = len(jobs)
    stats["errors"]  = len(errors)
    return jobs, errors, stats
# ────────────────────────────────────────────────────────────


# ─── NO CHANGE: parse_sources ───────────────────────────────
def parse_sources(config: dict[str, Any]) -> list[Source]:
    sources: list[Source] = []
    for item in config.get("sources", []):
        if not item.get("name") or not item.get("url"):
            continue
        sources.append(Source(
            name             = item["name"],
            url              = item["url"],
            freshness_days   = int(item.get("freshness_days", 45)),
            include_patterns = item.get("include_patterns", []) or [],
            category         = item.get("category", "general"),
            keywords         = item.get("keywords", []) or [],
            search_urls      = item.get("search_urls", []) or [],
        ))
    return sources


def main() -> int:
    parser = argparse.ArgumentParser(description="Scrape configured job sources into a static JSON file.")
    parser.add_argument("--sources",              default="config/sources.yaml")
    parser.add_argument("--profile",              default="config/profile.yaml")
    parser.add_argument("--output",               default="docs/data/jobs.json")
    parser.add_argument("--max-sources",          type=int, default=25)
    parser.add_argument("--max-links-per-source", type=int, default=20)
    parser.add_argument("--exclude-categories",   default="")
    args = parser.parse_args()

    source_path  = Path(args.sources)
    profile_path = Path(args.profile)
    if not source_path.exists():
        source_path = Path("config/sources.example.yaml")
    if not profile_path.exists():
        profile_path = Path("config/profile.example.yaml")

    sources_config = load_yaml(source_path)
    profile        = load_yaml(profile_path)
    sources        = parse_sources(sources_config)
    configured_source_count = len(sources)

    excluded_categories = {
        category.strip().lower()
        for category in args.exclude_categories.split(",")
        if category.strip()
    }
    if excluded_categories:
        sources = [s for s in sources if s.category.lower() not in excluded_categories]
    if args.max_sources > 0:
        sources = sources[:args.max_sources]

    session = get_session()

    all_jobs:  list[dict[str, Any]]  = []
    all_errors: list[dict[str, str]] = []
    all_stats: list[dict[str, Any]]  = []

    # ─── CHANGE 5 ─────────────────────────────────────────────
    # Old code: for source in sources: scrape_source(...)
    # New code: wraps each source in a 90-second alarm so one
    #           hanging government website cannot block everything.
    #
    # signal.alarm only works on Linux/macOS — works fine on
    # GitHub Actions (ubuntu-latest). On Windows it is skipped.
    use_alarm = hasattr(signal, "SIGALRM")   # False on Windows, True on Linux

    for source in sources:
        print(f"[{source.name}] Scraping...")
        if use_alarm:
            signal.signal(signal.SIGALRM, _alarm_handler)
            signal.alarm(SOURCE_TIMEOUT_SECONDS)
        try:
            jobs, errors, stats = scrape_source(
                session, source, profile, max_links=args.max_links_per_source
            )
        except _SourceTimeout:
            print(f"[{source.name}] SKIPPED — exceeded {SOURCE_TIMEOUT_SECONDS}s limit",
                  file=sys.stderr)
            jobs, errors, stats = [], [], {
                "source": source.name, "url": source.url,
                "candidates": 0, "matches": 0,
                "errors": 1, "note": "timed out"
            }
        finally:
            if use_alarm:
                signal.alarm(0)   # always cancel the alarm after each source

        all_jobs.extend(jobs)
        all_errors.extend(errors)
        all_stats.append(stats)
    # ──────────────────────────────────────────────────────────

    deduped = {job["id"]: job for job in all_jobs}
    output  = {
        "generated_at":           utc_now().isoformat(),
        "configured_source_count": configured_source_count,
        "scraped_source_count":   len(sources),
        "excluded_categories":    sorted(excluded_categories),
        "sources": [
            {
                "name":           s.name,
                "url":            s.url,
                "freshness_days": s.freshness_days,
                "category":       s.category,
                "search_urls":    s.search_urls,
            }
            for s in sources
        ],
        "jobs":   sorted(deduped.values(), key=lambda item: item["score"], reverse=True),
        "errors": all_errors,
        "stats":  all_stats,
    }

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(output, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Wrote {len(output['jobs'])} live matching jobs to {output_path}")
    if all_errors:
        print(f"Encountered {len(all_errors)} source/link errors", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
