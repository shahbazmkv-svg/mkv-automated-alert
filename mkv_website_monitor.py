"""
MKV Luxury – Website Daily Snapshot Monitor
Checks the live site and posts a Slack report to #mkv-daily-lead-report (or a dedicated channel).
"""

import os
import re
import json
import requests
from datetime import datetime, timezone, timedelta
from bs4 import BeautifulSoup

# ── Config ──────────────────────────────────────────────────────────────────
SITE_BASE        = "https://www.mkvluxury.com"
SLACK_TOKEN      = os.environ["SLACK_BOT_TOKEN"]
SLACK_CHANNEL    = "C0AVCCCG0S0"   # #mkvtest  ← switch to live channel when ready
DUBAI_TZ         = timezone(timedelta(hours=4))
HEADERS          = {"User-Agent": "MKV-Monitor/1.0 (+https://mkvluxury.com)"}
TIMEOUT          = 15

# Pages to probe
PAGES_TO_CHECK = [
    {"name": "Homepage",        "url": f"{SITE_BASE}/"},
    {"name": "Fleet",           "url": f"{SITE_BASE}/cars"},
    {"name": "About Us",        "url": f"{SITE_BASE}/about-us"},
    {"name": "Contact",         "url": f"{SITE_BASE}/contact"},
    {"name": "FAQs",            "url": f"{SITE_BASE}/faqs"},
    {"name": "Terms",           "url": f"{SITE_BASE}/terms-and-conditions"},
    {"name": "Privacy Policy",  "url": f"{SITE_BASE}/privacy-policy"},
    {"name": "Blog",            "url": f"{SITE_BASE}/blog"},
]

# Spot-check a handful of car listing pages
CAR_PAGES = [
    {"name": "Ferrari Purosangue 2025",  "url": f"{SITE_BASE}/car/ferrari-purosangue-2025"},
    {"name": "Lamborghini Urus S",       "url": f"{SITE_BASE}/car/lamborghini-urus-s-2025"},
    {"name": "McLaren Artura Spider",    "url": f"{SITE_BASE}/car/mclaren-artura-spider-2025"},
    {"name": "Mercedes AMG G63 Retro",   "url": f"{SITE_BASE}/car/mercedes-amg-g63-retro"},
    {"name": "Bentley Bentayga Mansory", "url": f"{SITE_BASE}/car/bentley-bentayga-mansory"},
]

# ── Helpers ──────────────────────────────────────────────────────────────────

def fetch(url):
    try:
        r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
        return r
    except Exception as e:
        return None

def status_emoji(code):
    if code is None:    return "🔴"
    if code == 200:     return "✅"
    if code == 301:     return "↪️"
    if code == 302:     return "↪️"
    if code >= 400:     return "🔴"
    return "🟡"

def check_page_load(page):
    r = fetch(page["url"])
    code = r.status_code if r else None
    emoji = status_emoji(code)
    load_ms = int(r.elapsed.total_seconds() * 1000) if r else None
    speed_note = ""
    if load_ms:
        speed_note = f"  `{load_ms}ms`"
        if load_ms > 3000:
            speed_note += " ⚠️ slow"
    return {
        "name":     page["name"],
        "url":      page["url"],
        "code":     code,
        "emoji":    emoji,
        "load_ms":  load_ms,
        "speed_note": speed_note,
    }

def check_car_page(page):
    """Check car page: loads, has a price, no floating-point bug."""
    r = fetch(page["url"])
    if not r or r.status_code != 200:
        return {
            "name":   page["name"],
            "emoji":  "🔴",
            "issues": ["Page did not load"],
        }
    soup = BeautifulSoup(r.text, "lxml")
    text = soup.get_text(" ", strip=True)
    issues = []

    # Check for float bug (e.g. 49.989999...)
    float_bug = re.search(r'\d+\.\d{5,}', text)
    if float_bug:
        issues.append(f"Float display bug: `{float_bug.group()}`")

    # Check a price exists (AED followed by digits)
    if not re.search(r'AED\s*[\d,]+', text):
        issues.append("No AED price found on page")

    # Check images loaded (src not broken - approximate via img tags present)
    imgs = soup.find_all("img")
    if len(imgs) < 3:
        issues.append(f"Only {len(imgs)} image(s) detected — possible missing images")

    emoji = "✅" if not issues else "⚠️"
    return {
        "name":   page["name"],
        "emoji":  emoji,
        "issues": issues,
    }

def check_homepage_extras():
    """Additional homepage-specific checks."""
    r = fetch(f"{SITE_BASE}/")
    if not r:
        return ["Homepage fetch failed"]
    soup = BeautifulSoup(r.text, "lxml")
    issues = []

    # Note: "Chat Now" buttons use href="#" with a JS onClick handler for WhatsApp.
    # This is intentional — no check needed here.

    # Review badges: rendered as images — check by image src/alt, not page text
    all_imgs = soup.find_all("img")
    img_attrs = " ".join(
        f"{img.get('src','')} {img.get('alt','')}" for img in all_imgs
    ).lower()
    for badge, keyword in [
        ("Trustpilot", "trustpilot"),
        ("Google Reviews", "google"),
        ("Tripadvisor", "tripadvisor"),
    ]:
        if keyword not in img_attrs:
            issues.append(f"Review badge image missing: {badge}")

    # Check category filter links present
    text = soup.get_text(" ", strip=True)
    for cat in ["Supercars", "Luxury Cars", "Luxury SUV", "Convertible"]:
        if cat not in text:
            issues.append(f"Category filter missing: {cat}")

    return issues

def check_faq_duplicates():
    """Check for duplicate FAQ questions."""
    r = fetch(f"{SITE_BASE}/faqs")
    if not r:
        return ["FAQ page fetch failed"]
    soup = BeautifulSoup(r.text, "lxml")
    headings = [h.get_text(strip=True) for h in soup.find_all(["h2","h3","h4","button","summary"])]
    seen = set()
    dupes = []
    for h in headings:
        if h in seen and len(h) > 10:
            dupes.append(f"Duplicate: \"{h[:60]}\"")
        seen.add(h)
    return dupes

# ── Slack ────────────────────────────────────────────────────────────────────

def post_slack(blocks):
    payload = {
        "channel": SLACK_CHANNEL,
        "blocks":  blocks,
    }
    r = requests.post(
        "https://slack.com/api/chat.postMessage",
        headers={
            "Authorization": f"Bearer {SLACK_TOKEN}",
            "Content-Type":  "application/json",
        },
        data=json.dumps(payload),
        timeout=15,
    )
    result = r.json()
    if not result.get("ok"):
        print(f"Slack error: {result.get('error')}")
    else:
        print("Snapshot posted to Slack successfully.")

# ── Main ─────────────────────────────────────────────────────────────────────

def build_report():
    now_dubai = datetime.now(DUBAI_TZ).strftime("%d %b %Y, %I:%M %p")

    # 1. Page load checks
    page_results  = [check_page_load(p) for p in PAGES_TO_CHECK]
    pages_ok      = all(r["code"] == 200 for r in page_results)
    overall_emoji = "✅" if pages_ok else "🔴"

    # 2. Car page spot checks
    car_results = [check_car_page(c) for c in CAR_PAGES]
    cars_ok     = all(not r["issues"] for r in car_results)

    # 3. Homepage extras
    homepage_issues = check_homepage_extras()

    # 4. FAQ duplicates
    faq_issues = check_faq_duplicates()

    # ── Build Slack blocks ──
    blocks = []

    # Header
    blocks.append({
        "type": "header",
        "text": {"type": "plain_text", "text": f"🌐 MKV Website Daily Snapshot — {now_dubai}"}
    })
    blocks.append({"type": "divider"})

    # Overall status
    all_good = pages_ok and cars_ok and not homepage_issues and not faq_issues
    status_text = "All systems operational" if all_good else "Issues detected — review below"
    blocks.append({
        "type": "section",
        "text": {"type": "mrkdwn", "text": f"*Overall Status:* {'✅ ' + status_text if all_good else '⚠️ ' + status_text}"}
    })
    blocks.append({"type": "divider"})

    # Page load results
    page_lines = []
    for r in page_results:
        line = f"{r['emoji']} *{r['name']}* — `{r['code']}`{r['speed_note']}"
        page_lines.append(line)
    blocks.append({
        "type": "section",
        "text": {"type": "mrkdwn", "text": "*📄 Page Availability*\n" + "\n".join(page_lines)}
    })
    blocks.append({"type": "divider"})

    # Car page checks
    car_lines = []
    for r in car_results:
        if r["issues"]:
            car_lines.append(f"{r['emoji']} *{r['name']}*\n  " + "\n  ".join(f"• {i}" for i in r["issues"]))
        else:
            car_lines.append(f"{r['emoji']} *{r['name']}* — OK")
    blocks.append({
        "type": "section",
        "text": {"type": "mrkdwn", "text": "*🚗 Car Listing Spot Checks*\n" + "\n".join(car_lines)}
    })
    blocks.append({"type": "divider"})

    # Homepage extras
    if homepage_issues:
        hp_text = "*🏠 Homepage Issues*\n" + "\n".join(f"⚠️ {i}" for i in homepage_issues)
    else:
        hp_text = "*🏠 Homepage Checks* — ✅ All OK"
    blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": hp_text}})

    # FAQ issues
    if faq_issues:
        faq_text = "*❓ FAQ Issues*\n" + "\n".join(f"⚠️ {i}" for i in faq_issues)
    else:
        faq_text = "*❓ FAQ Page* — ✅ No duplicate questions detected"
    blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": faq_text}})

    blocks.append({"type": "divider"})
    blocks.append({
        "type": "context",
        "elements": [{"type": "mrkdwn", "text": f"MKV Website Monitor • Auto-run daily at 11:00 AM Dubai time • <{SITE_BASE}|mkvluxury.com>"}]
    })

    return blocks


if __name__ == "__main__":
    print("Running MKV Website Snapshot...")
    blocks = build_report()
    post_slack(blocks)
