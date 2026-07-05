#!/usr/bin/env python3
"""
2027-swe-radar: daily scanner for new-grad SWE openings.

Sources:
  1. SimplifyJobs New-Grad-Positions (community-maintained JSON)
  2. Greenhouse public job boards for a configurable company watchlist
  3. Lever public job boards (same watchlist pattern)
  4. Ashby public job boards (same watchlist pattern)
  5. Adzuna aggregator (optional — needs ADZUNA_APP_ID/ADZUNA_APP_KEY env vars;
     free key at https://developer.adzuna.com)

Each run:
  - fetches + filters listings by keywords in config.yml
  - diffs against the previous snapshot (data/latest.json)
  - writes data/YYYY-MM-DD.json and regenerates README.md
  - prints a commit message suggestion to stdout (used by the CI workflow)
"""

import json
import os
import re
import sys
import urllib.parse
import urllib.request
from datetime import date, datetime, timezone
from pathlib import Path

import yaml

ROOT = Path(__file__).parent
DATA_DIR = ROOT / "data"
LATEST = DATA_DIR / "latest.json"

SIMPLIFY_URL = (
    "https://raw.githubusercontent.com/SimplifyJobs/"
    "New-Grad-Positions/dev/.github/scripts/listings.json"
)
GREENHOUSE_URL = "https://boards-api.greenhouse.io/v1/boards/{board}/jobs"
LEVER_URL = "https://api.lever.co/v0/postings/{board}?mode=json"
ASHBY_URL = "https://api.ashbyhq.com/posting-api/job-board/{board}"
ADZUNA_URL = (
    "https://api.adzuna.com/v1/api/jobs/us/search/1"
    "?app_id={app_id}&app_key={app_key}&results_per_page=50"
    "&max_days_old=30&what={what}&what_or={what_or}"
)


def fetch_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "2027-swe-radar/1.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode())


def load_config():
    with open(ROOT / "config.yml") as f:
        return yaml.safe_load(f)


US_STATE_CODES = {
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA", "HI", "ID",
    "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS",
    "MO", "MT", "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH", "OK",
    "OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV",
    "WI", "WY", "DC",
}
US_HINTS = ("united states", "usa", "u.s.", "nyc", "new york",
            "remote - us", "us remote", "remote, us")


def is_us(location):
    low = location.lower()
    if low.strip() == "remote":  # bare "Remote" on US-centric boards
        return True
    if any(h in low for h in US_HINTS):
        return True
    # Case-sensitive so state codes match "Chicago, IL" but not "Berlin"/"London"
    return any(code in US_STATE_CODES for code in re.findall(r"\b[A-Z]{2}\b", location))


def filter_us(jobs):
    kept = []
    for j in jobs:
        if not j["locations"]:  # unknown location — keep rather than miss a US role
            kept.append(j)
            continue
        us_locs = [loc for loc in j["locations"] if is_us(loc)]
        if us_locs:
            j["locations"] = us_locs
            kept.append(j)
    return kept


def matches(text, kw):
    t = text.lower()
    if any(x.lower() in t for x in kw.get("exclude", [])):
        return False
    require = kw.get("require", [])
    if require and not any(r.lower() in t for r in require):
        return False
    return any(k.lower() in t for k in kw["include"])


def normalize_simplify(item):
    return {
        "id": f"simplify-{item['id']}",
        "company": item.get("company_name", ""),
        "title": item.get("title", ""),
        "locations": item.get("locations", []),
        "url": item.get("url", ""),
        "sponsorship": item.get("sponsorship", ""),
        "posted": datetime.fromtimestamp(
            item.get("date_posted", 0), tz=timezone.utc
        ).strftime("%Y-%m-%d"),
        "source": "SimplifyJobs",
    }


def normalize_greenhouse(job, company):
    loc = job.get("location", {}).get("name", "")
    return {
        "id": f"gh-{company}-{job['id']}",
        "company": company.title(),
        "title": job.get("title", ""),
        "locations": [loc] if loc else [],
        "url": job.get("absolute_url", ""),
        "sponsorship": "",
        "posted": (job.get("updated_at") or "")[:10],
        "source": "Greenhouse",
    }


def normalize_lever(job, company):
    loc = job.get("categories", {}).get("location", "")
    posted_ms = job.get("createdAt", 0)
    return {
        "id": f"lever-{company}-{job['id']}",
        "company": company.title(),
        "title": job.get("text", ""),
        "locations": [loc] if loc else [],
        "url": job.get("hostedUrl", ""),
        "sponsorship": "",
        "posted": datetime.fromtimestamp(
            posted_ms / 1000, tz=timezone.utc
        ).strftime("%Y-%m-%d"),
        "source": "Lever",
    }


def normalize_ashby(job, company):
    locs = [job.get("location", "")]
    locs += [s.get("location", "") for s in job.get("secondaryLocations", [])]
    return {
        "id": f"ashby-{company}-{job['id']}",
        "company": company.title(),
        "title": job.get("title", ""),
        "locations": [loc for loc in locs if loc],
        "url": job.get("jobUrl", ""),
        "sponsorship": "",
        "posted": (job.get("publishedAt") or "")[:10],
        "source": "Ashby",
    }


def normalize_adzuna(item):
    loc = item.get("location", {}).get("display_name", "")
    return {
        "id": f"adzuna-{item['id']}",
        "company": item.get("company", {}).get("display_name", ""),
        "title": item.get("title", ""),
        "locations": [loc] if loc else [],
        "url": item.get("redirect_url", ""),
        "sponsorship": "",
        "posted": (item.get("created") or "")[:10],
        "source": "Adzuna",
    }


def scan_simplify(cfg):
    kw = cfg["keywords"]
    results = []
    try:
        listings = fetch_json(SIMPLIFY_URL)
    except Exception as e:
        print(f"[warn] SimplifyJobs fetch failed: {e}", file=sys.stderr)
        return results
    for item in listings:
        if not item.get("active") or not item.get("is_visible"):
            continue
        text = item.get("title", "")
        if matches(text, kw):
            results.append(normalize_simplify(item))
    return results


def scan_greenhouse(cfg):
    kw = cfg["keywords"]
    results = []
    for board in cfg.get("greenhouse_boards", []):
        try:
            payload = fetch_json(GREENHOUSE_URL.format(board=board))
        except Exception as e:
            print(f"[warn] Greenhouse '{board}' failed: {e}", file=sys.stderr)
            continue
        for job in payload.get("jobs", []):
            if matches(job.get("title", ""), kw):
                results.append(normalize_greenhouse(job, board))
    return results


def scan_lever(cfg):
    kw = cfg["keywords"]
    results = []
    for board in cfg.get("lever_boards", []):
        try:
            postings = fetch_json(LEVER_URL.format(board=board))
        except Exception as e:
            print(f"[warn] Lever '{board}' failed: {e}", file=sys.stderr)
            continue
        for job in postings:
            if matches(job.get("text", ""), kw):
                results.append(normalize_lever(job, board))
    return results


def scan_ashby(cfg):
    kw = cfg["keywords"]
    results = []
    for board in cfg.get("ashby_boards", []):
        try:
            payload = fetch_json(ASHBY_URL.format(board=board))
        except Exception as e:
            print(f"[warn] Ashby '{board}' failed: {e}", file=sys.stderr)
            continue
        for job in payload.get("jobs", []):
            if not job.get("isListed", True):
                continue
            if matches(job.get("title", ""), kw):
                results.append(normalize_ashby(job, board))
    return results


def scan_adzuna(cfg):
    app_id = os.environ.get("ADZUNA_APP_ID")
    app_key = os.environ.get("ADZUNA_APP_KEY")
    if not app_id or not app_key:
        print("[info] Adzuna skipped — ADZUNA_APP_ID/ADZUNA_APP_KEY not set",
              file=sys.stderr)
        return []
    kw = cfg["keywords"]
    url = ADZUNA_URL.format(
        app_id=app_id,
        app_key=app_key,
        what=urllib.parse.quote("software engineer"),
        what_or=urllib.parse.quote("2027 grad graduate entry junior"),
    )
    results = []
    try:
        payload = fetch_json(url)
    except Exception as e:
        print(f"[warn] Adzuna fetch failed: {e}", file=sys.stderr)
        return results
    for item in payload.get("results", []):
        if matches(item.get("title", ""), kw):
            results.append(normalize_adzuna(item))
    return results


def newest_first(jobs):
    return sorted(jobs, key=lambda j: j["posted"], reverse=True)


def table(jobs, bold_company=False):
    lines = ["| Company | Role | Location | Posted | Apply |", "|---|---|---|---|---|"]
    for j in jobs:
        loc = "; ".join(j["locations"][:2]) or "—"
        company = f"**{j['company']}**" if bold_company else j["company"]
        lines.append(
            f"| {company} | {j['title']} | {loc} | {j['posted']} | [Link]({j['url']}) |"
        )
    return lines


def build_readme(all_jobs, new_jobs, today, min_posted):
    jobs_2027 = [j for j in all_jobs if "2027" in j["title"]]
    lines = [
        "# 📡 2027 SWE Radar",
        "",
        "Automated daily scanner for **2027 new-grad software engineering** openings.",
        f"Last scan: **{today}** · Tracking **{len(all_jobs)}** matching postings"
        f" · 🆕 **{len(new_jobs)}** new today",
        f"Only showing **US** postings from **{min_posted}** onward, newest first.",
        "",
    ]
    if new_jobs:
        lines += ["## 🆕 New today", ""]
        lines += table(newest_first(new_jobs), bold_company=True)
        lines.append("")
    lines += ["## 🎯 Explicit 2027 openings", ""]
    if jobs_2027:
        lines += table(newest_first(jobs_2027))
    else:
        lines.append("_No postings explicitly mentioning 2027 yet._")
    lines.append("")
    lines += ["## All tracked postings (most recent 50)", ""]
    lines += table(newest_first(all_jobs)[:50])
    lines += ["", "---",
              "_Sources: SimplifyJobs New-Grad-Positions, Greenhouse/Lever/Ashby"
              " boards, Adzuna aggregator._",
              "_Scanned ~10× daily by a GitHub Actions workflow._"]
    (ROOT / "README.md").write_text("\n".join(lines))


def main():
    cfg = load_config()
    today = date.today().isoformat()
    DATA_DIR.mkdir(exist_ok=True)

    min_posted = str(cfg.get("min_posted", "2026-07-01"))
    jobs = (scan_simplify(cfg) + scan_greenhouse(cfg) + scan_lever(cfg)
            + scan_ashby(cfg) + scan_adzuna(cfg))
    jobs = [j for j in jobs if j["posted"] >= min_posted]
    if cfg.get("us_only", True):
        jobs = filter_us(jobs)
    jobs_by_id = {j["id"]: j for j in jobs}

    previous_ids = set()
    if LATEST.exists():
        previous_ids = {j["id"] for j in json.loads(LATEST.read_text())}

    if not jobs_by_id and previous_ids:
        # Every source came back empty while we previously had data — almost
        # certainly a fetch outage, not a real market wipe. Keep old data.
        print("chore: scan aborted — all sources returned no data")
        return

    new_jobs = [j for jid, j in jobs_by_id.items() if jid not in previous_ids]

    snapshot = newest_first(jobs_by_id.values())
    (DATA_DIR / f"{today}.json").write_text(json.dumps(snapshot, indent=2))
    LATEST.write_text(json.dumps(snapshot, indent=2))

    build_readme(snapshot, new_jobs, today, min_posted)

    if new_jobs:
        companies = ", ".join(sorted({j["company"] for j in new_jobs})[:4])
        more = "…" if len({j["company"] for j in new_jobs}) > 4 else ""
        msg = f"feat: {len(new_jobs)} new 2027 SWE postings ({companies}{more})"
    else:
        msg = f"chore: daily scan {today} — no new postings"
    print(msg)


if __name__ == "__main__":
    main()
