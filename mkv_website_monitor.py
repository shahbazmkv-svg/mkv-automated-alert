"""
MKV Luxury – Website Daily Snapshot Monitor
Checks the live site and posts a Slack report.
Also monitors social media pages for recent activity.
"""

import os
import re
import json
import requests
from datetime import datetime, timezone, timedelta
from bs4 import BeautifulSoup

# ── Config ───────────────────────────────────────────────────────────────────
SITE_BASE     = "https://www.mkvluxury.com"
SLACK_TOKEN   = os.environ["SLACK_BOT_TOKEN"]
SLACK_CHANNEL = "C0B0TGBDCDU"   # #mkvtest — switch to live channel when ready
DUBAI_TZ      = timezone(timedelta(hours=4))
HEADERS       = {"User-Agent": "Mozilla/5.0 (compatible; MKV-Monitor/1.0)"}
TIMEOUT       = 15

# Pages to probe
PAGES_TO_CHECK = [
    {"name": "Homepage",       "url": f"{SITE_BASE}/"},
    {"name": "Fleet",          "url": f"{SITE_BASE}/cars"},
    {"name": "About Us",       "url": f"{SITE_BASE}/about-us"},
    {"name": "Contact",        "url": f"{SITE_BASE}/contact"},
    {"name": "FAQs",           "url": f"{SITE_BASE}/faqs"},
    {"name": "Terms",          "url": f"{SITE_BASE}/terms-and-conditions"},
    {"name": "Privacy Policy", "url": f"{SITE_BASE}/privacy-policy"},
    {"name": "Blog",           "url": f"{SITE_BASE}/blog"},
]

# Car listing spot-checks
CAR_PAGES = [
    {"name": "Ferrari Purosangue 2025",  "url": f"{SITE_BASE}/car/ferrari-purosangue-2025"},
    {"name": "Lamborghini Urus S",       "url": f"{SITE_BASE}/car/lamborghini-urus-s-2025"},
    {"name": "McLaren Artura Spider",    "url": f"{SITE_BASE}/car/mclaren-artura-spider-2025"},
    {"name": "Mercedes AMG G63 Retro",   "url": f"{SITE_BASE}/car/mercedes-amg-g63-retro"},
    {"name": "Bentley Bentayga Mansory", "url": f"{SITE_BASE}/car/bentley-bentayga-mansory"},
]

# Social media profiles
SOCIAL_PROFILES = [
    {"name": "Instagram", "emoji": "📸", "url": "https://www.instagram.com/mkvluxurydubai/",          "check_fn": "check_instagram"},
    {"name": "Facebook",  "emoji": "📘", "url": "https://www.facebook.com/mkvluxury/",                "check_fn": "check_facebook"},
    {"name": "TikTok",    "emoji": "🎵", "url": "https://www.tiktok.com/@mkv.luxury",                 "check_fn": "check_tiktok"},
    {"name": "YouTube",   "emoji": "▶️", "url": "https://www.youtube.com/@MKVLuxuryCarRentalDubai",  "check_fn": "check_youtube"},
]

# ── Helpers ──────────────────────────────────────────────────────────────────

def fetch(url, extra_headers=None):
    h = {**HEADERS, **(extra_headers or {})}
    try:
        return requests.get(url, headers=h, timeout=TIMEOUT, allow_redirects=True)
    except Exception:
        return None

def status_emoji(code):
    if code is None:       return "🔴"
    if code == 200:        return "✅"
    if code in (301, 302): return "↪️"
    if code >= 400:        return "🔴"
    return "🟡"

# ── Page Load Checks ─────────────────────────────────────────────────────────

def check_page_load(page):
    r       = fetch(page["url"])
    code    = r.status_code if r else None
    load_ms = int(r.elapsed.total_seconds() * 1000) if r else None
    speed   = (f"`{load_ms}ms`" + (" ⚠️ slow" if load_ms > 3000 else "")) if load_ms else ""
    return {"name": page["name"], "code": code, "emoji": status_emoji(code), "speed": speed}

# ── Car Page Checks ───────────────────────────────────────────────────────────

def check_car_page(page):
    r = fetch(page["url"])
    if not r or r.status_code != 200:
        return {"name": page["name"], "emoji": "🔴", "issues": ["Page did not load"]}
    soup   = BeautifulSoup(r.text, "lxml")
    text   = soup.get_text(" ", strip=True)
    issues = []
    if re.search(r'\d+\.\d{5,}', text):
        issues.append("Float display bug in pricing")
    if not re.search(r'AED\s*[\d,]+', text):
        issues.append("No AED price found")
    if len(soup.find_all("img")) < 3:
        issues.append("Fewer than 3 images detected")
    return {"name": page["name"], "emoji": "✅" if not issues else "⚠️", "issues": issues}

# ── Homepage Checks ───────────────────────────────────────────────────────────

def check_homepage_extras():
    r = fetch(f"{SITE_BASE}/")
    if not r or r.status_code != 200:
        return ["Homepage fetch failed"]
    soup      = BeautifulSoup(r.text, "lxml")
    img_attrs = " ".join(f"{i.get('src','')} {i.get('alt','')}" for i in soup.find_all("img")).lower()
    text      = soup.get_text(" ", strip=True)
    issues    = []
    for badge, kw in [("Trustpilot", "trustpilot"), ("Google", "google"), ("Tripadvisor", "tripadvisor")]:
        if kw not in img_attrs:
            issues.append(f"Review badge missing: {badge}")
    for cat in ["Supercars", "Luxury Cars", "Luxury SUV", "Convertible"]:
        if cat not in text:
            issues.append(f"Category filter missing: {cat}")
    return issues

# ── FAQ Duplicate Check ───────────────────────────────────────────────────────

def check_faq_duplicates():
    """
    Collect all <h3> question texts and flag only those appearing MORE THAN ONCE.
    Previously the script was incorrectly flagging every question because it marked
    the first occurrence as 'seen' and then treated all others as duplicates of it.
    Fix: count occurrences first, then report only count > 1.
    """
    r = fetch(f"{SITE_BASE}/faqs")
    if not r or r.status_code != 200:
        return ["FAQ page fetch failed"]
    soup      = BeautifulSoup(r.text, "lxml")
    questions = [h.get_text(strip=True) for h in soup.find_all("h3") if len(h.get_text(strip=True)) > 10]
    counts    = {}
    for q in questions:
        counts[q] = counts.get(q, 0) + 1
    return [f'"{q[:70]}"' for q, n in counts.items() if n > 1]

# ── Social Media Checks ───────────────────────────────────────────────────────

def check_instagram():
    r = fetch("https://www.instagram.com/mkvluxurydubai/",
              extra_headers={"Accept-Language": "en-US,en;q=0.9"})
    if not r or r.status_code != 200:
        return {"reachable": False, "note": "Profile page blocked or unavailable"}
    if "mkvluxurydubai" in r.text.lower():
        return {"reachable": True, "note": "Profile loads ✅ — verify latest post manually"}
    return {"reachable": False, "note": "Profile not found in response"}

def check_facebook():
    r = fetch("https://www.facebook.com/mkvluxury/",
              extra_headers={"Accept-Language": "en-US,en;q=0.9"})
    if not r or r.status_code != 200:
        return {"reachable": False, "note": "Profile page blocked or unavailable"}
    if "mkvluxury" in r.text.lower():
        return {"reachable": True, "note": "Profile loads ✅ — verify latest post manually"}
    return {"reachable": False, "note": "Profile not found in response"}

def check_tiktok():
    r = fetch("https://www.tiktok.com/@mkv.luxury",
              extra_headers={"Accept-Language": "en-US,en;q=0.9"})
    if not r or r.status_code != 200:
        return {"reachable": False, "note": "Profile unavailable"}
    if "mkv" in r.text.lower():
        return {"reachable": True, "note": "Profile loads ✅ — verify latest video manually"}
    return {"reachable": False, "note": "Profile not found in response"}

def check_youtube():
    r = fetch("https://www.youtube.com/@MKVLuxuryCarRentalDubai",
              extra_headers={"Accept-Language": "en-US,en;q=0.9"})
    if not r or r.status_code != 200:
        return {"reachable": False, "note": "Channel unavailable"}
    text   = r.text
    recent = re.search(r'"(\d+ (hour|day|minute)s? ago|yesterday)"', text)
    if recent:
        return {"reachable": True, "note": f"Recent upload detected: {recent.group(1)} ✅"}
    if "mkv" in text.lower():
        return {"reachable": True, "note": "Channel loads ✅ — verify latest video manually"}
    return {"reachable": False, "note": "Channel not found in response"}

SOCIAL_CHECK_FNS = {
    "check_instagram": check_instagram,
    "check_facebook":  check_facebook,
    "check_tiktok":    check_tiktok,
    "check_youtube":   check_youtube,
}

# ── Slack ─────────────────────────────────────────────────────────────────────

def post_slack(blocks):
    payload = {
        "channel":    SLACK_CHANNEL,
        "username":   "MKV Website Monitor",
        "icon_emoji": ":globe_with_meridians:",
        "blocks":     blocks,
    }
    r = requests.post(
        "https://slack.com/api/chat.postMessage",
        headers={"Authorization": f"Bearer {SLACK_TOKEN}", "Content-Type": "application/json"},
        data=json.dumps(payload),
        timeout=15,
    )
    result = r.json()
    if not result.get("ok"):
        print(f"Slack error: {result.get('error')}")
    else:
        print("✅ Snapshot posted to Slack successfully.")

# ── Build Report ──────────────────────────────────────────────────────────────

def build_report():
    now_dubai = datetime.now(DUBAI_TZ).strftime("%d %b %Y, %I:%M %p")

    page_results    = [check_page_load(p) for p in PAGES_TO_CHECK]
    car_results     = [check_car_page(c)  for c in CAR_PAGES]
    homepage_issues = check_homepage_extras()
    faq_dupes       = check_faq_duplicates()
    social_results  = [{**p, "result": SOCIAL_CHECK_FNS[p["check_fn"]]()} for p in SOCIAL_PROFILES]

    pages_ok  = all(r["code"] == 200 for r in page_results)
    cars_ok   = all(not r["issues"] for r in car_results)
    all_clear = pages_ok and cars_ok and not homepage_issues and not faq_dupes

    blocks = []

    # Header
    blocks.append({"type": "header", "text": {"type": "plain_text", "text": f"🌐 MKV Website Daily Snapshot — {now_dubai}"}})
    blocks.append({"type": "divider"})

    # Overall
    status = "✅ All systems operational" if all_clear else "⚠️ Issues detected — review below"
    blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": f"*Overall Status:* {status}"}})
    blocks.append({"type": "divider"})

    # Page availability
    lines = [f"{r['emoji']} *{r['name']}* — `{r['code']}` {r['speed']}" for r in page_results]
    blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": "*📄 Page Availability*\n" + "\n".join(lines)}})
    blocks.append({"type": "divider"})

    # Car checks
    car_lines = []
    for r in car_results:
        if r["issues"]:
            car_lines.append(f"⚠️ *{r['name']}*\n  " + "\n  ".join(f"• {i}" for i in r["issues"]))
        else:
            car_lines.append(f"✅ *{r['name']}* — OK")
    blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": "*🚗 Car Listing Spot Checks*\n" + "\n".join(car_lines)}})
    blocks.append({"type": "divider"})

    # Homepage
    hp = "*🏠 Homepage Checks* — ✅ All OK" if not homepage_issues else \
         "*🏠 Homepage Issues*\n" + "\n".join(f"⚠️ {i}" for i in homepage_issues)
    blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": hp}})

    # FAQ
    faq_text = "*❓ FAQ Page* — ✅ No duplicate questions" if not faq_dupes else \
               "*❓ FAQ — Duplicate Questions Found*\n" + "\n".join(f"⚠️ Duplicate: {d}" for d in faq_dupes)
    blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": faq_text}})
    blocks.append({"type": "divider"})

    # Social media
    social_lines = []
    for s in social_results:
        res   = s["result"]
        reach = "✅" if res["reachable"] else "🔴"
        social_lines.append(f"{reach} {s['emoji']} *{s['name']}* — {res['note']}\n  ↳ <{s['url']}|View Profile>")
    blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": "*📱 Social Media Profiles*\n" + "\n".join(social_lines)}})
    blocks.append({"type": "context", "elements": [{"type": "mrkdwn",
        "text": "ℹ️ Social platforms block automated post-date reads. Profile reachability is auto-checked; latest post dates require manual verification."}]})
    blocks.append({"type": "divider"})

    # Footer
    blocks.append({"type": "context", "elements": [{"type": "mrkdwn",
        "text": f"MKV Website Monitor • Daily 11:00 AM Dubai time • <{SITE_BASE}|mkvluxury.com>"}]})

    return blocks


if __name__ == "__main__":
    print("Running MKV Website Snapshot...")
    blocks = build_report()
    post_slack(blocks)
