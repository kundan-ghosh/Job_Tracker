# Job Notice Monitor

A free-to-deploy job, internship, fellowship, research, and vacancy monitor for user-selected websites. It scrapes configured websites on a schedule, reads linked PDF notices, filters out expired/closed notices where possible, stores broad live results, and lets each visitor filter the results for their own profile.

## Why this architecture

- **Free hosting:** GitHub Pages serves the website from `docs/`.
- **Free scheduled scraping:** GitHub Actions runs the scraper every 6 hours in a public repository.
- **No paid database:** scraped results are written to `docs/data/jobs.json`.
- **PDF support:** linked PDF notices are downloaded and text is extracted before ranking.
- **User-side matching:** visitors paste their own resume/skill keywords in the website UI; those keywords stay in their browser.
- **User-requested websites:** visitors can add a website request in the UI and get a ready-to-paste YAML block for `config/sources.yaml`.

GitHub’s current documentation says GitHub Pages is available for public repositories on GitHub Free, and GitHub Actions usage is free for public repositories using standard GitHub-hosted runners.

## Files

- `config/sources.example.yaml` — default websites to monitor.
- `config/profile.example.yaml` — example matching criteria.
- `scraper/job_scraper.py` — scraper, PDF reader, expiry filter, and ranker.
- `docs/index.html` — static website UI.
- `docs/data/jobs.json` — generated search data.
- `.github/workflows/scrape-and-deploy.yml` — scheduled scraper and GitHub Pages deployment.

## Local setup

```bash
cp config/sources.example.yaml config/sources.yaml
cp config/profile.example.yaml config/profile.yaml
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python scraper/job_scraper.py
python3 -m http.server 8000 --directory docs
```

Open `http://localhost:8000`.

## Configure sources

Edit `config/sources.yaml`:

```yaml
sources:
  - name: "Example University"
    url: "https://example.edu/careers"
    freshness_days: 90
    search_urls:
      - "https://example.edu/careers?query=internship"
      - "https://example.edu/careers?query=research"
    include_patterns:
      - recruitment
      - vacancy
      - internship
      - pdf
```

Notes:

- Use the page where notices are listed, not the institution homepage when possible.
- For job portals, prefer `search_urls` that already contain keywords/location filters instead of the generic homepage.
- Add words that commonly appear in useful links under `include_patterns`.
- For PDF-heavy university sites, include `notice`, `recruitment`, `vacancy`, and `pdf`.
- Some websites block automated requests or require login. LinkedIn and Indeed may limit scraping; official career pages and university notice pages are more reliable.
- A visitor can use the website’s **Add a website** form to generate a YAML block, but the block still must be committed to GitHub before the scraper can monitor it permanently.

## Configure matching criteria

Edit `config/profile.yaml`:

```yaml
candidate:
  include_keywords:
    - internship
    - python
    - data
  exclude_keywords:
    - senior
    - professor
  resume_keywords:
    - react
    - sql
  minimum_score: 2
```

Do not commit a full private resume to a public repository. Use broad public criteria here. Each user can paste their own private resume/skill keywords in the website UI for local-only filtering.

## Filtering in the website

The website supports:

- Search across title, source, summary, and matched keywords.
- Source filtering.
- Opportunity type filtering, including jobs, internships, research, fellowships, projects, and scholarships.
- Location keyword filtering.
- Minimum score filtering.
- Deadline filtering.
- PDF-only or HTML-only filtering.
- User-specific resume/skill keyword re-ranking.
- User-specific hide keywords.
- Source diagnostics showing configured sources, candidate links, matches, and scrape warnings.

Because GitHub Pages is static, these filters work on already-scraped data from `docs/data/jobs.json`.

## Deploy for free on GitHub

1. Create a public GitHub repository.
2. Push this project to the repository.
3. In GitHub, go to **Settings → Pages → Build and deployment**.
4. Set **Source** to **GitHub Actions**.
5. Go to **Actions → Scrape jobs and deploy site → Run workflow**.
6. After it completes, open the Pages URL shown by GitHub.

The workflow also runs every 6 hours. Scheduled GitHub Actions can be delayed during platform load, so do not treat timing as exact.

## Limits and hard cases

- Sites that require login, CAPTCHA, heavy JavaScript, or block bots may not scrape reliably.
- Expiry detection is heuristic: explicit deadlines are best; otherwise recent notices are kept based on `freshness_days`.
- Scanned image-only PDFs need OCR, which is intentionally not included in this free lightweight version.
- A static GitHub Pages site cannot let anonymous visitors permanently change the central scrape list. They can generate a source request, but the repository owner must commit it.
- The larger FastAPI/Supabase/AI design is possible, but this repository intentionally avoids paid or account-heavy backend dependencies.
- For a multi-customer SaaS product, GitHub Pages is not the right backend. This repo is designed as a free personal/job-monitor deployment.
