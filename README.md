# Job Notice Monitor

A free-to-deploy job and internship monitor for user-selected websites. It scrapes configured websites on a schedule, reads linked PDF notices, filters out expired/closed notices, ranks matches against your criteria, and serves the results as a static website.

## Why this architecture

- **Free hosting:** GitHub Pages serves the website from `docs/`.
- **Free scheduled scraping:** GitHub Actions runs the scraper every 6 hours in a public repository.
- **No paid database:** scraped results are written to `docs/data/jobs.json`.
- **PDF support:** linked PDF notices are downloaded and text is extracted before ranking.
- **Privacy-aware resume matching:** commit only keyword criteria; paste private resume keywords in the website UI for local-only re-ranking.

GitHub’s current documentation says GitHub Pages is available for public repositories on GitHub Free, and GitHub Actions usage is free for public repositories using standard GitHub-hosted runners.

## Files

- `config/sources.example.yaml` — example websites to monitor.
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
    include_patterns:
      - recruitment
      - vacancy
      - internship
      - pdf
```

Notes:

- Use the page where notices are listed, not the institution homepage when possible.
- Add words that commonly appear in useful links under `include_patterns`.
- For PDF-heavy university sites, include `notice`, `recruitment`, `vacancy`, and `pdf`.
- Some websites block automated requests or require login. LinkedIn and Indeed may limit scraping; official career pages and university notice pages are more reliable.

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

Do not commit a full private resume to a public repository. Use skill keywords instead.

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
- For a multi-customer SaaS product, GitHub Pages is not the right backend. This repo is designed as a free personal/job-monitor deployment.
