from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import io
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

import requests
import yaml
from bs4 import BeautifulSoup
from dateutil import parser as date_parser
from pypdf import PdfReader


USER_AGENT = (
    "Mozilla/5.0 (compatible; JobNoticeMonitor/1.0; "
    "+https://github.com/)"
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


@dataclass
class Source:
    name: str
    url: str
    freshness_days: int
    include_patterns: list[str]


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
    session.headers.update({"User-Agent": USER_AGENT})
    return session


def fetch(session: requests.Session, url: str, timeout: int = 25) -> requests.Response:
    response = session.get(url, timeout=timeout, allow_redirects=True)
    response.raise_for_status()
    return response


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


def candidate_links(source: Source, html: str) -> list[dict[str, str]]:
    soup = BeautifulSoup(html, "html.parser")
    candidates: list[dict[str, str]] = []
    patterns = [pattern.lower() for pattern in source.include_patterns]

    for anchor in soup.find_all("a", href=True):
        href = anchor.get("href", "")
        title = normalize_text(anchor.get_text(" "))
        absolute_url = urljoin(source.url, href)
        combined = f"{title} {absolute_url}".lower()

        if not title and not absolute_url.lower().endswith(".pdf"):
            continue
        if patterns and not any(pattern in combined for pattern in patterns):
            continue

        candidates.append(
            {
                "title": title or Path(urlparse(absolute_url).path).name or absolute_url,
                "url": absolute_url,
            }
        )

    if not candidates:
        page_title = normalize_text(soup.title.get_text(" ")) if soup.title else source.name
        candidates.append({"title": page_title, "url": source.url})

    seen: set[str] = set()
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


def score_match(text: str, profile: dict[str, Any]) -> tuple[int, list[str]]:
    candidate = profile.get("candidate", {})
    include_keywords = candidate.get("include_keywords", []) or []
    location_keywords = candidate.get("location_keywords", []) or []
    resume_keywords = candidate.get("resume_keywords", []) or []
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
    today = utc_now().date()
    lowered = text.lower()
    if NEGATIVE_STATUS_PATTERNS.search(lowered):
        return False
    if deadline:
        return deadline >= today

    found_dates = [parse_date(match.group(0)) for match in re.finditer(r"\b[0-3]?\d[./\-][01]?\d[./\-]\d{4}\b", text[:6000])]
    recent_dates = [date for date in found_dates if date and date <= today]
    if recent_dates:
        newest = max(recent_dates)
        return (today - newest).days <= freshness_days

    return True


def summarize(text: str, max_chars: int = 320) -> str:
    cleaned = normalize_text(text)
    return cleaned[:max_chars].rsplit(" ", 1)[0] if len(cleaned) > max_chars else cleaned


def scrape_source(session: requests.Session, source: Source, profile: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    jobs: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []

    try:
        source_response = fetch(session, source.url)
    except Exception as exc:
        return [], [{"source": source.name, "url": source.url, "error": str(exc)}]

    links = candidate_links(source, source_response.text)
    for link in links:
        try:
            response = fetch(session, link["url"])
            content_type = response.headers.get("content-type", "")
            if is_probably_pdf(link["url"], content_type):
                body_text = extract_pdf_text(response.content)
                content_type_label = "pdf"
            else:
                body_text = extract_html_text(response.text)
                content_type_label = "html"

            full_text = normalize_text(f"{link['title']} {body_text}")
            deadline = extract_deadline(full_text)
            if not is_live_notice(full_text, deadline, source.freshness_days):
                continue

            score, matched_keywords = score_match(full_text, profile)
            minimum_score = int(profile.get("candidate", {}).get("minimum_score", 1))
            if score < minimum_score:
                continue

            jobs.append(
                {
                    "id": stable_id(source.name, link["url"], link["title"]),
                    "source": source.name,
                    "title": link["title"],
                    "url": link["url"],
                    "score": score,
                    "matched_keywords": matched_keywords,
                    "deadline": deadline.isoformat() if deadline else None,
                    "content_type": content_type_label,
                    "summary": summarize(full_text),
                    "checked_at": utc_now().isoformat(),
                }
            )
        except Exception as exc:
            errors.append({"source": source.name, "url": link["url"], "error": str(exc)})

    return jobs, errors


def parse_sources(config: dict[str, Any]) -> list[Source]:
    sources: list[Source] = []
    for item in config.get("sources", []):
        if not item.get("name") or not item.get("url"):
            continue
        sources.append(
            Source(
                name=item["name"],
                url=item["url"],
                freshness_days=int(item.get("freshness_days", 45)),
                include_patterns=item.get("include_patterns", []) or [],
            )
        )
    return sources


def main() -> int:
    parser = argparse.ArgumentParser(description="Scrape configured job sources into a static JSON file.")
    parser.add_argument("--sources", default="config/sources.yaml")
    parser.add_argument("--profile", default="config/profile.yaml")
    parser.add_argument("--output", default="docs/data/jobs.json")
    args = parser.parse_args()

    source_path = Path(args.sources)
    profile_path = Path(args.profile)
    if not source_path.exists():
        source_path = Path("config/sources.example.yaml")
    if not profile_path.exists():
        profile_path = Path("config/profile.example.yaml")

    sources_config = load_yaml(source_path)
    profile = load_yaml(profile_path)
    sources = parse_sources(sources_config)
    session = get_session()

    all_jobs: list[dict[str, Any]] = []
    all_errors: list[dict[str, str]] = []
    for source in sources:
        jobs, errors = scrape_source(session, source, profile)
        all_jobs.extend(jobs)
        all_errors.extend(errors)

    deduped = {job["id"]: job for job in all_jobs}
    output = {
        "generated_at": utc_now().isoformat(),
        "jobs": sorted(deduped.values(), key=lambda item: item["score"], reverse=True),
        "errors": all_errors,
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
