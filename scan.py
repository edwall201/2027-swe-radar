#!/usr/bin/env python3
"""
2027-swe-radar: daily scanner for new-grad SWE openings.

Sources:
  1. SimplifyJobs New-Grad-Positions (community-maintained JSON)
  2. Greenhouse public job boards for a configurable company watchlist

Each run:
  - fetches + filters listings by keywords in config.yml
  - diffs against the previous snapshot (data/latest.json)
  - writes data/YYYY-MM-DD.json and regenerates README.md
  - prints a commit message suggestion to stdout (used by the CI workflow)
"""

import json
import re
import sys
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


def fetch_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "2027-swe-radar/1.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode())


def load_config():
    with open(ROOT / "config.yml") as f:
        return yaml.safe_load(f)


def matches(text, includes, excludes):
    t = text.lower()
    if excludes and any(x.lower() in t for x in excludes):
        return False
    return any(k.lower() in t for k in includes)


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
        if matches(text, kw["include"], kw.get("exclude", [])):
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
            if matches(job.get("title", ""), kw["include"], kw.get("exclude", [])):
                results.append(normalize_greenhouse(job, board))
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
        f"Only showing postings from **{min_posted}** onward, newest first.",
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
    lines += ["", "---", "_Sources: SimplifyJobs New-Grad-Positions, Greenhouse boards._",
              "_Built with a daily GitHub Actions workflow._"]
    (ROOT / "README.md").write_text("\n".join(lines))


def main():
    cfg = load_config()
    today = date.today().isoformat()
    DATA_DIR.mkdir(exist_ok=True)

    min_posted = str(cfg.get("min_posted", "2026-07-01"))
    jobs = scan_simplify(cfg) + scan_greenhouse(cfg)
    jobs = [j for j in jobs if j["posted"] >= min_posted]
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
