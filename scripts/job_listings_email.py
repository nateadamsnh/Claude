"""
Daily Remote Job Listings Email
================================
Pulls remote, full-time Data Engineering / Database Administrator postings
from Adzuna, RemoteOK, and USAJobs, and emails a digest.

Config files expected:
  C:\\Users\\Nathaniel\\Documents\\Trading\\config\\job_search_config.json
  C:\\Users\\Nathaniel\\Documents\\Trading\\config\\email_credentials.json

job_search_config.json format:
{
    "adzuna_app_id": "...",
    "adzuna_app_key": "...",
    "usajobs_api_key": "...",
    "usajobs_email": "you@example.com",
    "searches": [{"label": "Data Engineering", "keywords": "data engineer"}, ...],
    "remote_only": true,
    "full_time_only": true,
    "max_days_old": 1,
    "results_per_search": 15
}
"""

import json
import os
import re
import smtplib
import sys
import traceback
from datetime import date
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import requests

BASE_DIR = r"C:\Users\Nathaniel\Documents\Trading\config"
JOB_CONFIG = os.path.join(BASE_DIR, "job_search_config.json")
EMAIL_CREDS = os.path.join(BASE_DIR, "email_credentials.json")

ADZUNA_URL = "https://api.adzuna.com/v1/api/jobs/us/search/1"
REMOTEOK_URL = "https://remoteok.com/api"
USAJOBS_URL = "https://data.usajobs.gov/api/search"

REMOTE_HINT_RE = re.compile(r"remote", re.I)
NON_FULL_TIME_TAGS = {"contract", "part time", "parttime", "freelance", "internship", "temporary", "temp"}


def load_json(path):
    with open(path) as f:
        return json.load(f)


def fetch_adzuna(keywords, app_id, app_key, max_days_old, results_per_search, full_time_only=True):
    if not app_id or not app_key:
        return []
    # "where" is a geocoded location lookup, not a free-text filter — "remote" doesn't
    # resolve to a place and silently returns zero results. Instead, fetch a wider page
    # nationally and post-filter on title/location/description mentioning "remote".
    params = {
        "app_id": app_id,
        "app_key": app_key,
        "what_phrase": keywords,
        "max_days_old": max_days_old,
        "results_per_page": max(results_per_search * 4, 50),
        "sort_by": "date",
        "content-type": "application/json",
    }
    if full_time_only:
        params["full_time"] = 1
    try:
        r = requests.get(ADZUNA_URL, params=params, timeout=15)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        print(f"  Adzuna error for '{keywords}': {e}")
        return []

    jobs = []
    for item in data.get("results", []):
        title = item.get("title", "")
        location = (item.get("location") or {}).get("display_name", "")
        description = item.get("description", "")
        if not (REMOTE_HINT_RE.search(title) or REMOTE_HINT_RE.search(location) or REMOTE_HINT_RE.search(description)):
            continue
        jobs.append({
            "title": title,
            "company": (item.get("company") or {}).get("display_name", "Unknown"),
            "location": location or "Remote",
            "url": item.get("redirect_url", ""),
            "source": "Adzuna",
            "created": item.get("created", ""),
        })
        if len(jobs) >= results_per_search:
            break
    return jobs


def fetch_remoteok(keywords, results_per_search, alt_word=None, full_time_only=True):
    try:
        r = requests.get(
            REMOTEOK_URL,
            headers={"User-Agent": "Mozilla/5.0 (compatible; JobDigestBot/1.0)"},
            timeout=15,
        )
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        print(f"  RemoteOK error: {e}")
        return []

    terms = [t.lower() for t in keywords.split()]
    alt_re = re.compile(rf"\b{re.escape(alt_word)}\b", re.I) if alt_word else None
    jobs = []
    for item in data:
        if not isinstance(item, dict) or "position" not in item:
            continue  # first element is a legal-notice blob, not a job
        position = item.get("position", "")
        tag_list = [t.lower() for t in item.get("tags", [])]
        tags = " ".join(tag_list)
        haystack = f"{position} {tags}".lower()
        matches_phrase = all(term in haystack for term in terms)
        matches_alt = bool(alt_re and alt_re.search(haystack))
        if not (matches_phrase or matches_alt):
            continue
        if full_time_only and any(t in tag_list for t in NON_FULL_TIME_TAGS):
            continue
        jobs.append({
            "title": position,
            "company": item.get("company", "Unknown"),
            "location": item.get("location") or "Remote",
            "url": item.get("url", ""),
            "source": "RemoteOK",
            "created": item.get("date", ""),
        })
        if len(jobs) >= results_per_search:
            break
    return jobs


def fetch_usajobs(keywords, api_key, user_email, results_per_search, full_time_only=True, remote_only=True):
    if not api_key or not user_email:
        return []
    headers = {
        "Host": "data.usajobs.gov",
        "User-Agent": user_email,
        "Authorization-Key": api_key,
    }
    params = {
        "Keyword": keywords,
        "ResultsPerPage": results_per_search,
        "SortField": "opendate",
        "SortDirection": "desc",
    }
    if remote_only:
        params["RemoteIndicator"] = "true"
    if full_time_only:
        params["PositionScheduleTypeCode"] = "1"  # Full-Time

    try:
        r = requests.get(USAJOBS_URL, params=params, headers=headers, timeout=15)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        print(f"  USAJobs error for '{keywords}': {e}")
        return []

    jobs = []
    for item in data.get("SearchResult", {}).get("SearchResultItems", []):
        d = item.get("MatchedObjectDescriptor", {})
        locations = d.get("PositionLocation", [])
        location = locations[0].get("LocationName", "Remote") if locations else "Remote"
        jobs.append({
            "title": d.get("PositionTitle", ""),
            "company": d.get("OrganizationName", "Unknown"),
            "location": location,
            "url": d.get("PositionURI", ""),
            "source": "USAJobs",
            "created": d.get("PublicationStartDate", ""),
        })
    return jobs


def dedupe(jobs):
    seen = set()
    out = []
    for j in jobs:
        key = j["url"] or (j["title"], j["company"])
        if key in seen:
            continue
        seen.add(key)
        out.append(j)
    return out


def build_html(sections, today_str):
    def job_row(j):
        return (
            "<tr>"
            f"<td style='padding:8px 10px;font-weight:600'>{j['title']}</td>"
            f"<td style='padding:8px 10px'>{j['company']}</td>"
            f"<td style='padding:8px 10px;color:#6b7280'>{j['location']}</td>"
            f"<td style='padding:8px 10px;color:#6b7280'>{j['source']}</td>"
            f"<td style='padding:8px 10px'><a href='{j['url']}' style='color:#2563eb;text-decoration:none'>View →</a></td>"
            "</tr>"
        )

    section_html = ""
    for label, jobs in sections:
        if jobs:
            rows = "".join(job_row(j) for j in jobs)
        else:
            rows = "<tr><td colspan='5' style='padding:12px;text-align:center;color:#6b7280'>No new listings today.</td></tr>"
        section_html += f"""
  <div style="padding:20px 24px;border-bottom:1px solid #e5e7eb">
    <h3 style="margin:0 0 12px;font-size:14px;color:#1e293b">{label} ({len(jobs)})</h3>
    <div style="overflow-x:auto">
      <table style="border-collapse:collapse;width:100%;font-size:13px">
        <tr style="background:#f9fafb;border-bottom:2px solid #e5e7eb">
          <th style="padding:8px 10px;text-align:left;color:#6b7280;font-weight:600">Title</th>
          <th style="padding:8px 10px;text-align:left;color:#6b7280;font-weight:600">Company</th>
          <th style="padding:8px 10px;text-align:left;color:#6b7280;font-weight:600">Location</th>
          <th style="padding:8px 10px;text-align:left;color:#6b7280;font-weight:600">Source</th>
          <th style="padding:8px 10px;text-align:left;color:#6b7280;font-weight:600">Link</th>
        </tr>
        {rows}
      </table>
    </div>
  </div>"""

    return f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"></head>
<body style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#f3f4f6;margin:0;padding:20px">
<div style="max-width:900px;margin:0 auto;background:#fff;border-radius:10px;overflow:hidden;box-shadow:0 1px 3px rgba(0,0,0,.1)">
  <div style="background:#1e293b;color:#fff;padding:20px 28px">
    <h1 style="margin:0;font-size:20px;font-weight:700">💼 Remote, Full-Time Job Listings</h1>
    <p style="margin:4px 0 0;color:#94a3b8;font-size:13px">{today_str}</p>
  </div>
  {section_html}
  <div style="padding:14px 24px;background:#f9fafb;border-top:1px solid #e5e7eb;font-size:11px;color:#9ca3af;text-align:center">
    Sources: Adzuna, RemoteOK, USAJobs · Generated by job_listings_email.py
  </div>
</div>
</body>
</html>"""


def main():
    today_str = date.today().strftime("%B %d, %Y")
    print(f"[{today_str}] Starting job listings digest...")

    cfg = load_json(JOB_CONFIG)
    email = load_json(EMAIL_CREDS)

    app_id = cfg.get("adzuna_app_id", "")
    app_key = cfg.get("adzuna_app_key", "")
    usajobs_key = cfg.get("usajobs_api_key", "")
    usajobs_email = cfg.get("usajobs_email", "")
    max_days_old = cfg.get("max_days_old", 1)
    results_per_search = cfg.get("results_per_search", 15)
    full_time_only = cfg.get("full_time_only", True)
    remote_only = cfg.get("remote_only", True)

    sections = []
    for search in cfg.get("searches", []):
        label = search["label"]
        keywords = search["keywords"]
        alt_word = search.get("alt_word")
        print(f"  Searching '{label}' ({keywords})...")

        jobs = []
        jobs += fetch_adzuna(keywords, app_id, app_key, max_days_old, results_per_search, full_time_only)
        jobs += fetch_remoteok(keywords, results_per_search, alt_word=alt_word, full_time_only=full_time_only)
        jobs += fetch_usajobs(keywords, usajobs_key, usajobs_email, results_per_search, full_time_only, remote_only)
        jobs = dedupe(jobs)[:results_per_search]

        print(f"    Found {len(jobs)} listings.")
        sections.append((label, jobs))

    html = build_html(sections, today_str)

    smtp_user = email["smtp_user"]
    smtp_pass = email["smtp_password"]
    to_addr = email["to_address"]
    subject = f"💼 Remote Job Listings — {today_str}"

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = smtp_user
    msg["To"] = to_addr
    msg.attach(MIMEText(html, "html"))

    print(f"  Sending email to {to_addr}...")
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(smtp_user, smtp_pass)
        server.sendmail(smtp_user, to_addr, msg.as_string())

    print("  Done! Email sent.")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
        sys.exit(1)
