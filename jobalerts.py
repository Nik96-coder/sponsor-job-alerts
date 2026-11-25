"""Sponsored job alerts.

Every run:
  1. Downloads the latest GOV.UK register of licensed sponsors (Skilled Worker).
  2. Pulls jobs posted in the last few hours from LinkedIn, Indeed and Glassdoor (JobSpy).
  3. Keeps only jobs whose company is on the register, that pass the salary floor
     and title filters, and that haven't been emailed before.
  4. Emails the survivors.

Run locally:   python jobalerts.py            (sends email if SMTP settings exist)
Dry run:       python jobalerts.py --dry-run  (prints results, sends nothing, saves nothing)
"""

from __future__ import annotations

import argparse
import csv
import html
import io
import json
import os
import re
import smtplib
import sys
import time
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

import requests
import yaml
from rapidfuzz import fuzz, process

ROOT = Path(__file__).resolve().parent
STATE_FILE = ROOT / "state" / "seen.json"
REGISTER_CACHE = ROOT / "state" / "sponsor_register.csv"
REGISTER_PAGE = "https://www.gov.uk/government/publications/register-of-licensed-sponsors-workers"
UA = "Mozilla/5.0 (sponsor-job-alerts; personal job search)"

# Words that don't help tell companies apart.
NOISE = {
    "ltd", "limited", "plc", "llp", "llc", "inc", "co", "company", "corp", "corporation",
    "uk", "u.k", "gb", "group", "holdings", "the", "and", "of", "services", "international",
    "trading", "as", "t/a", "nhs", "foundation", "trust",
}


# First words too common to identify a company on their own.
GENERIC_FIRST = {
    "london", "royal", "british", "national", "global", "first", "united", "city", "north", "south",
    "east", "west", "new", "great", "capital", "central", "general", "smart", "digital", "data",
    "health", "care", "home", "homes", "tech", "technology", "technologies", "solutions", "systems",
    "university", "college", "school", "bank", "energy", "green", "blue", "star", "prime", "one",
    "and", "my", "your", "best", "top", "premier", "elite", "golden", "silver", "apex", "alpha",
}

# ---------------------------------------------------------------- helpers

def log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def normalise(name: str) -> str:
    """Lowercase, strip punctuation and legal suffixes: 'Bauer Radio Limited' -> 'bauer radio'."""
    name = (name or "").lower().replace("&", " and ")
    name = re.sub(r"['’`]", "", name)             # king's -> kings
    name = re.sub(r"\(.*?\)", " ", name)              # drop bracketed bits
    name = re.sub(r"[^a-z0-9 ]+", " ", name)
    words = [w for w in name.split() if w not in NOISE]
    return " ".join(words).strip()


def load_config() -> dict:
    with open(ROOT / "config.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f)


# ---------------------------------------------------------------- sponsor register

def register_csv_url() -> str:
    # Set REGISTER_CSV_URL to override, if GOV.UK ever changes the page layout.
    if os.environ.get("REGISTER_CSV_URL"):
        return os.environ["REGISTER_CSV_URL"]
    page = requests.get(REGISTER_PAGE, headers={"User-Agent": UA}, timeout=60)
    page.raise_for_status()
    links = [html.unescape(u) for u in re.findall(r'href="([^"]+?\.csv[^"]*)"', page.text)]
    links = [u if u.startswith("http") else "https://www.gov.uk" + u for u in links]
    preferred = [u for u in links if "assets.publishing.service.gov.uk" in u] or links
    if not preferred:
        raise RuntimeError("Couldn't find the sponsor register CSV link on GOV.UK.")
    return preferred[0]


def load_register() -> dict[str, str]:
    """Return {normalised name: official name} for Skilled Worker sponsors."""
    text = None
    try:
        url = register_csv_url()
        log(f"Downloading sponsor register: {url.rsplit('/', 1)[-1]}")
        r = requests.get(url, headers={"User-Agent": UA}, timeout=120)
        r.raise_for_status()
        text = r.content.decode("utf-8-sig", errors="replace")
        REGISTER_CACHE.parent.mkdir(exist_ok=True)
        REGISTER_CACHE.write_text(text, encoding="utf-8")
    except Exception as e:  # fall back to the last good copy
        if REGISTER_CACHE.exists():
            log(f"Register download failed ({e}); using cached copy.")
            text = REGISTER_CACHE.read_text(encoding="utf-8")
        else:
            raise

    reader = csv.DictReader(io.StringIO(text))
    cols = {c.lower().strip(): c for c in reader.fieldnames or []}
    name_col = next((cols[c] for c in cols if "organisation" in c), None)
    route_col = next((cols[c] for c in cols if c == "route"), None)
    if not name_col:
        raise RuntimeError(f"Unexpected register columns: {reader.fieldnames}")

    sponsors: dict[str, str] = {}
    for row in reader:
        route = (row.get(route_col) or "") if route_col else "Skilled Worker"
        if "skilled worker" not in route.lower():
            continue
        official = (row.get(name_col) or "").strip()
        key = normalise(official)
        if key:
            sponsors.setdefault(key, official)
    log(f"Skilled Worker sponsors on register: {len(sponsors):,}")
    return sponsors


def build_first_word_index(sponsors: dict[str, str]) -> dict[str, list[str]]:
    index: dict[str, list[str]] = {}
    for key, official in sponsors.items():
        first = key.split()[0]
        if len(first) >= 4 and first not in GENERIC_FIRST:
            index.setdefault(first, []).append(official)
    return index


def match_sponsor(company: str, sponsors: dict[str, str], keys: list[str], threshold: int,
                  first_index: dict[str, list[str]]):
    """Return (official name, score, kind). kind is 'exact', 'fuzzy', 'brand' or None."""
    key = normalise(company)
    if not key:
        return None, 0, None
    if key in sponsors:
        return sponsors[key], 100, "exact"
    best = process.extractOne(key, keys, scorer=fuzz.token_sort_ratio, score_cutoff=threshold)
    if best:
        return sponsors[best[0]], int(best[1]), "fuzzy"
    # Brand names often differ from the legal name ("Bauer Media" vs "Bauer Radio Limited").
    # Only trust it when the rest of the job's company name also appears in the register name,
    # so "Nuffield Health" doesn't match "Nuffield Foundation".
    tokens = key.split()
    candidates = first_index.get(tokens[0], [])
    if 0 < len(candidates) <= 5:
        rest = set(tokens[1:])
        for official in candidates:
            if rest <= set(normalise(official).split()):
                return official, 0, "brand"
    return None, 0, None


# ---------------------------------------------------------------- jobs

def fetch_jobs(cfg: dict):
    import pandas as pd
    from jobspy import scrape_jobs

    frames = []
    for term in cfg["search_terms"]:
        try:
            df = scrape_jobs(
                site_name=cfg.get("sites", ["indeed", "linkedin"]),
                search_term=term,
                location=cfg.get("location", "London, United Kingdom"),
                distance=cfg.get("distance_miles", 25),
                results_wanted=cfg.get("results_per_search", 25),
                hours_old=cfg.get("hours_old", 3),
                country_indeed="UK",
                enforce_annual_salary=True,
                verbose=0,
            )
            log(f"'{term}': {len(df)} jobs")
            if len(df):
                frames.append(df)
        except Exception as e:  # one failing site/term shouldn't stop the run
            log(f"'{term}' failed: {e}")
        time.sleep(3)  # be polite to the job sites
    if not frames:
        return pd.DataFrame()
    jobs = pd.concat(frames, ignore_index=True)
    jobs["job_url"] = jobs["job_url"].astype(str)
    return jobs.drop_duplicates(subset=["job_url"])


def annual_max(row) -> float | None:
    """Best annual salary figure available (enforce_annual_salary already converts most)."""
    for col in ("max_amount", "min_amount"):
        val = row.get(col)
        try:
            if val is not None and val == val and float(val) > 0:  # val == val filters NaN
                return float(val)
        except (TypeError, ValueError):
            continue
    return None


def filter_jobs(jobs, sponsors: dict[str, str], seen: dict, cfg: dict) -> list[dict]:
    keys = list(sponsors.keys())
    first_index = build_first_word_index(sponsors)
    excl_titles = [w.lower() for w in cfg.get("exclude_title_words", [])]
    incl_titles = [w.lower() for w in cfg.get("include_title_words", [])]
    title_exceptions = [w.lower() for w in cfg.get("title_exceptions", [])]
    excl_companies = {normalise(c) for c in cfg.get("exclude_companies", [])}
    agency_words = [w.lower() for w in cfg.get("agency_words", [])]
    floor = cfg.get("min_salary", 33400)
    keep_no_salary = cfg.get("keep_jobs_without_salary", True)
    threshold = cfg.get("match_threshold", 92)

    kept, stats = [], {"seen": 0, "title": 0, "company": 0, "not_sponsor": 0, "salary": 0}
    for _, row in jobs.iterrows():
        url = str(row.get("job_url") or "")
        title = str(row.get("title") or "").strip()
        company = str(row.get("company") or "").strip()
        t = title.lower()
        dup_key = f"ct:{normalise(company)}|{normalise(title)}"
        if url in seen or dup_key in seen:
            stats["seen"] += 1
            continue
        if incl_titles and not any(re.search(rf"\b{re.escape(w)}", t) for w in incl_titles):
            stats["title"] += 1
            continue
        if not any(p in t for p in title_exceptions) and \
                any(re.search(rf"\b{re.escape(w)}\b", t) for w in excl_titles):
            stats["title"] += 1
            continue
        if not company or normalise(company) in excl_companies:
            stats["company"] += 1
            continue
        official, score, kind = match_sponsor(company, sponsors, keys, threshold, first_index)
        if not official:
            stats["not_sponsor"] += 1
            continue
        salary = annual_max(row)
        if salary is not None and salary < floor:
            stats["salary"] += 1
            continue
        if salary is None and not keep_no_salary:
            stats["salary"] += 1
            continue

        flags = []
        if salary is None:
            flags.append("salary not listed")
        if kind == "fuzzy":
            flags.append(f"name match {score}%: check it's '{official}'")
        elif kind == "brand":
            flags.append(f"possible sponsor: register lists '{official}'. Check it's the same organisation")
        if any(re.search(rf"\b{re.escape(w)}\b", company.lower()) for w in agency_words):
            flags.append("agency: check who the employer is")

        min_amt = row.get("min_amount")
        salary_text = ""
        if salary is not None:
            lo = float(min_amt) if min_amt == min_amt and min_amt not in (None, "") else None
            salary_text = f"£{lo:,.0f} to £{salary:,.0f}" if lo and lo != salary else f"£{salary:,.0f}"

        kept.append({
            "title": title,
            "company": company,
            "sponsor_name": official,
            "location": str(row.get("location") or ""),
            "site": str(row.get("site") or ""),
            "posted": str(row.get("date_posted") or "")[:10],
            "salary": salary_text,
            "url": url,
            "flags": flags,
            "dup_key": dup_key,
        })
    # Same job on several sites: keep one copy, preferring the one that shows a salary.
    best = {}
    for j in kept:
        k = j["dup_key"]
        if k not in best or (not best[k]["salary"] and j["salary"]):
            best[k] = j
    stats["duplicate"] = len(kept) - len(best)
    kept = list(best.values())
    log(f"Filtered out: {stats}")
    kept.sort(key=lambda j: (bool(j["flags"]), j["company"].lower()))
    return kept


# ---------------------------------------------------------------- state

def load_seen() -> dict:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_seen(seen: dict, new_jobs: list[dict], remember_days: int) -> None:
    now = datetime.now(timezone.utc)
    for j in new_jobs:
        seen[j["url"]] = now.isoformat()
        seen[j["dup_key"]] = now.isoformat()
    cutoff = now - timedelta(days=remember_days)
    seen = {u: t for u, t in seen.items() if datetime.fromisoformat(t) >= cutoff}
    STATE_FILE.parent.mkdir(exist_ok=True)
    STATE_FILE.write_text(json.dumps(seen, indent=0), encoding="utf-8")


# ---------------------------------------------------------------- email

def build_email(jobs: list[dict]) -> tuple[str, str]:
    subject = f"{len(jobs)} new sponsored job{'s' if len(jobs) != 1 else ''}: " + ", ".join(
        sorted({j['company'] for j in jobs})[:3]) + ("…" if len({j['company'] for j in jobs}) > 3 else "")
    rows = []
    for j in jobs:
        flags = "".join(f'<div style="color:#b45309;font-size:12px">⚠ {html.escape(f)}</div>' for f in j["flags"])
        rows.append(
            f'<tr><td style="padding:10px;border-bottom:1px solid #e5e7eb">'
            f'<a href="{html.escape(j["url"])}" style="font-weight:600;color:#1d4ed8;text-decoration:none">{html.escape(j["title"])}</a>'
            f'<div>{html.escape(j["company"])} · {html.escape(j["location"])}</div>'
            f'<div style="color:#4b5563;font-size:13px">{html.escape(j["salary"] or "Salary not listed")} · '
            f'{html.escape(j["site"])} · posted {html.escape(j["posted"])}</div>{flags}</td></tr>'
        )
    body = (
        '<div style="font-family:Arial,sans-serif;max-width:640px">'
        f'<p>New roles from <b>licensed Skilled Worker sponsors</b>, posted in the last few hours.</p>'
        f'<table style="border-collapse:collapse;width:100%">{"".join(rows)}</table>'
        '<p style="color:#6b7280;font-size:12px">Always check the advert says it can sponsor, '
        'and that the salary meets your threshold, before applying.</p></div>'
    )
    return subject, body


def send_email(subject: str, body: str) -> None:
    host = os.environ.get("SMTP_HOST", "smtp.gmail.com")
    port = int(os.environ.get("SMTP_PORT", "465"))
    user = os.environ["SMTP_USER"]
    password = os.environ["SMTP_PASSWORD"]
    to = os.environ.get("ALERT_TO", user)

    msg = MIMEMultipart("alternative")
    msg["Subject"], msg["From"], msg["To"] = subject, user, to
    msg.attach(MIMEText(body, "html", "utf-8"))
    with smtplib.SMTP_SSL(host, port, timeout=60) as s:
        s.login(user, password)
        s.sendmail(user, [a.strip() for a in to.split(",")], msg.as_string())
    log(f"Email sent to {to}")


# ---------------------------------------------------------------- main

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="print results; don't email or save state")
    args = ap.parse_args()

    cfg = load_config()
    sponsors = load_register()
    jobs = fetch_jobs(cfg)
    log(f"Jobs fetched (unique): {len(jobs)}")
    if jobs.empty:
        return 0

    seen = load_seen()
    new_jobs = filter_jobs(jobs, sponsors, seen, cfg)
    log(f"New sponsored jobs: {len(new_jobs)}")
    for j in new_jobs:
        print(f" - {j['title']} | {j['company']} | {j['salary'] or 'no salary'} | {j['url']}"
              + (f"  [{'; '.join(j['flags'])}]" if j["flags"] else ""))

    if args.dry_run or not new_jobs:
        return 0
    subject, body = build_email(new_jobs)
    if os.environ.get("SMTP_USER") and os.environ.get("SMTP_PASSWORD"):
        send_email(subject, body)
    else:
        log("No SMTP settings found, so no email sent.")
    save_seen(seen, new_jobs, cfg.get("remember_days", 21))
    return 0


if __name__ == "__main__":
    sys.exit(main())
