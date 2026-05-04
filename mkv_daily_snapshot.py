"""
MKV Luxury — Daily Executive Snapshot
Runs AFTER daily-leads and website-snapshot jobs.
Reads mtd_store.json + calls Gallabox + Appic APIs
→ Sends unified morning briefing to Slack via Claude API.
"""

import os
import re
import json
import requests
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

# ── Credentials ───────────────────────────────────────────────────────────────
GALLABOX_ACCOUNT_ID  = "66e3f05033e71154d5fdd76c"
GALLABOX_API_KEY     = "69e7694e2da59f609317986b"
GALLABOX_API_SECRET  = "984394d316324482a8615eba6742b3ab"
APPIC_KEY            = os.environ.get("APPIC_KEY", "")
SLACK_TOKEN          = os.environ["SLACK_BOT_TOKEN"]
SLACK_CHANNEL        = "C0B0TGBDCDU"
CLAUDE_API_KEY       = os.environ.get("CLAUDE_API_KEY", "")
MTD_STORE            = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mtd_store.json")
SITE_BASE            = "https://www.mkvluxury.com"

GALLABOX_HEADERS = {
    "apiKey": GALLABOX_API_KEY, "apiSecret": GALLABOX_API_SECRET,
    "Content-Type": "application/json"
}
BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/123.0.0.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.5",
}

# ── Time ──────────────────────────────────────────────────────────────────────
dubai_tz        = timezone(timedelta(hours=4))
utc_tz          = timezone.utc
now_dubai       = datetime.now(dubai_tz)
yesterday_dubai = now_dubai - timedelta(days=1)
yesterday_key   = yesterday_dubai.strftime("%Y-%m-%d")
yesterday_str   = yesterday_dubai.strftime("%d %b %Y")
report_dt       = now_dubai.strftime("%d %b %Y | %I:%M %p Dubai Time")
cur_month       = now_dubai.strftime("%Y-%m")
days_in_mtd     = yesterday_dubai.day
month_name      = now_dubai.strftime("%B %Y")

yday_utc_start  = yesterday_dubai.replace(hour=0,  minute=0,  second=0,  microsecond=0).astimezone(utc_tz)
yday_utc_end    = yesterday_dubai.replace(hour=23, minute=59, second=59, microsecond=0).astimezone(utc_tz)

# ══════════════════════════════════════════════════════════════════════════════
# 1. LEADS DATA — read from mtd_store.json (already built by gallabox script)
# ══════════════════════════════════════════════════════════════════════════════

def read_leads_data():
    try:
        with open(MTD_STORE, "r", encoding="utf-8") as f:
            store = json.load(f)
    except:
        return None

    if store.get("month") != cur_month:
        return None

    days    = store.get("days", {})
    ftd_raw = days.get(yesterday_key, {})
    mtd_raw = {}
    for day_data in days.values():
        for k, v in day_data.items():
            mtd_raw[k] = mtd_raw.get(k, 0) + v

    def build_agents(raw, prefix_r, prefix_t):
        agents = {}
        for k, v in raw.items():
            if k.startswith(prefix_r):
                n = k[len(prefix_r):]
                agents.setdefault(n, {"recd": 0, "trig": 0})
                agents[n]["recd"] += v
            elif k.startswith(prefix_t):
                n = k[len(prefix_t):]
                agents.setdefault(n, {"recd": 0, "trig": 0})
                agents[n]["trig"] += v
        return agents

    ftd_agents  = build_agents(ftd_raw, "a_r_", "a_t_")
    mtd_agents  = build_agents(mtd_raw, "a_r_", "a_t_")
    ftd_sources = {k[2:]: v for k, v in ftd_raw.items() if k.startswith("s_")}
    mtd_sources = {k[2:]: v for k, v in mtd_raw.items() if k.startswith("s_")}
    ftd_stages  = {k[2:]: v for k, v in ftd_raw.items() if k.startswith("g_")}
    mtd_stages  = {k[2:]: v for k, v in mtd_raw.items() if k.startswith("g_")}

    return {
        "ftd_total":   sum(v["recd"] for v in ftd_agents.values()),
        "mtd_total":   sum(v["recd"] for v in mtd_agents.values()),
        "ftd_agents":  ftd_agents,
        "mtd_agents":  mtd_agents,
        "ftd_sources": ftd_sources,
        "mtd_sources": mtd_sources,
        "ftd_stages":  ftd_stages,
        "mtd_stages":  mtd_stages,
        "days_count":  len(days),
    }

# ══════════════════════════════════════════════════════════════════════════════
# 2. FLEET DATA — from Appic API
# ══════════════════════════════════════════════════════════════════════════════

def read_fleet_data():
    try:
        today = now_dubai.strftime("%Y-%m-%d")
        start = (now_dubai - timedelta(days=90)).strftime("%Y-%m-%d")

        # Fetch all vehicles
        r_v = requests.get(
            "https://www.appicfleet.com/appiccar-apis-mkv/get-all-vehicles.php",
            headers=BROWSER_HEADERS, timeout=20
        )
        all_vehicles = r_v.json().get("data", [])
        active = [v for v in all_vehicles if float(v.get("dailyrent", 0) or 0) > 0]

        # Fetch active bookings
        r_b = requests.post(
            "https://www.appicfleet.com/appiccar-apis-mkv/get-mkv-bookings.php",
            data={"key": APPIC_KEY, "startDate": start, "endDate": today},
            timeout=20
        )
        bookings  = r_b.json().get("bookings", [])
        rented_plates = set()
        for b in bookings:
            plate = str(b.get("vehiclePlate", "") or "").strip()
            s = (b.get("startDate") or "").strip()
            e = (b.get("endDate")   or "").strip()
            if plate and s and e and s <= today <= e:
                rented_plates.add(plate)

        # Determine plate key
        plate_key = "plate"
        if active:
            sample = active[0]
            for k in ["plate", "vehiclePlate", "plateNo", "plate_no"]:
                if sample.get(k):
                    plate_key = k
                    break

        available = []
        rented    = []
        for v in active:
            plate = str(v.get(plate_key, "") or "").strip()
            name  = str(v.get("vehicleName") or v.get("name") or "").strip()
            rent  = float(v.get("dailyrent", 0) or 0)
            if plate in rented_plates:
                rented.append({"name": name, "plate": plate, "rate": rent})
            else:
                available.append({"name": name, "plate": plate, "rate": rent})

        return {
            "total":     len(active),
            "available": len(available),
            "rented":    len(rented),
            "util_pct":  round(len(rented) / max(1, len(active)) * 100),
            "available_list": sorted(available, key=lambda x: -x["rate"])[:5],
            "rented_list":    sorted(rented,    key=lambda x: -x["rate"])[:5],
        }
    except Exception as e:
        print(f"  Fleet data error: {e}")
        return None

# ══════════════════════════════════════════════════════════════════════════════
# 3. WEBSITE HEALTH — quick check on critical pages
# ══════════════════════════════════════════════════════════════════════════════

def read_website_data():
    pages = [
        {"name": "Homepage", "url": f"{SITE_BASE}/"},
        {"name": "Fleet",    "url": f"{SITE_BASE}/cars"},
        {"name": "Contact",  "url": f"{SITE_BASE}/contact"},
        {"name": "Blog",     "url": f"{SITE_BASE}/blog"},
    ]
    results = []

    def check(p):
        try:
            r  = requests.get(p["url"], headers=BROWSER_HEADERS, timeout=10, allow_redirects=True)
            ms = int(r.elapsed.total_seconds() * 1000)
            return {"name": p["name"], "code": r.status_code, "ms": ms}
        except:
            return {"name": p["name"], "code": None, "ms": None}

    with ThreadPoolExecutor(max_workers=4) as ex:
        results = list(ex.map(check, pages))

    ok_pages    = [r for r in results if r["code"] == 200]
    down_pages  = [r for r in results if r["code"] != 200]
    avg_ms      = int(sum(r["ms"] for r in ok_pages if r["ms"]) / max(1, len(ok_pages)))
    slow_pages  = [r for r in ok_pages if r["ms"] and r["ms"] > 3000]

    return {
        "total":      len(pages),
        "ok":         len(ok_pages),
        "down":       len(down_pages),
        "avg_ms":     avg_ms,
        "slow":       slow_pages,
        "down_pages": down_pages,
        "all_ok":     len(down_pages) == 0 and len(slow_pages) == 0,
    }

# ══════════════════════════════════════════════════════════════════════════════
# 4. TRUSTPILOT — rating snapshot
# ══════════════════════════════════════════════════════════════════════════════

def read_reputation_data():
    try:
        r = requests.get(
            "https://www.trustpilot.com/review/mkvluxury.com",
            headers=BROWSER_HEADERS, timeout=15
        )
        if r.status_code != 200:
            return {"trustpilot": None}
        text          = r.text
        rating_match  = re.search(r'TrustScore\s+([\d.]+)', text) or \
                        re.search(r'"ratingValue"\s*:\s*"?([\d.]+)"?', text)
        count_match   = re.search(r'([\d,]+)\s+reviews?', text, re.IGNORECASE)
        return {
            "trustpilot": {
                "rating": rating_match.group(1) if rating_match else None,
                "count":  count_match.group(1)  if count_match  else None,
            }
        }
    except:
        return {"trustpilot": None}

# ══════════════════════════════════════════════════════════════════════════════
# 5. CLAUDE API — generate narrative snapshot
# ══════════════════════════════════════════════════════════════════════════════

def generate_snapshot(leads, fleet, website, reputation):
    if not CLAUDE_API_KEY:
        return None

    # ── Build data summary for Claude ─────────────────────────────────────────
    # Leads section
    leads_text = "NO DATA"
    if leads:
        agent_lines = []
        for name, v in sorted(leads["ftd_agents"].items(), key=lambda x: -x[1]["recd"]):
            trig_rate = round(v["trig"] / max(1, v["recd"]) * 100)
            agent_lines.append(f"  {name}: {v['recd']} received, {v['trig']} triggered ({trig_rate}%)")
        top_src = max(leads["ftd_sources"].items(), key=lambda x: x[1])[0] if leads["ftd_sources"] else "?"
        top_src_pct = round(leads["ftd_sources"].get(top_src, 0) / max(1, leads["ftd_total"]) * 100)
        mtd_avg = round(leads["mtd_total"] / max(1, leads["days_count"]), 1)
        converted = leads["ftd_stages"].get("Converted lead", 0)
        qualified = leads["ftd_stages"].get("Qualified lead", 0)
        leads_text = f"""
  FTD leads: {leads["ftd_total"]} | MTD leads: {leads["mtd_total"]} ({days_in_mtd} days, avg {mtd_avg}/day)
  Top source: {top_src} ({top_src_pct}% of FTD)
  Conversions today: {converted} converted, {qualified} qualified
  Agent breakdown:
{chr(10).join(agent_lines)}"""

    # Fleet section
    fleet_text = "NO DATA"
    if fleet:
        avail_names = ", ".join(v["name"] for v in fleet["available_list"][:3]) or "none"
        fleet_text = f"""
  Total fleet: {fleet["total"]} | Rented: {fleet["rented"]} | Available: {fleet["available"]}
  Utilization: {fleet["util_pct"]}%
  Top available: {avail_names}"""

    # Website section
    web_text = "NO DATA"
    if website:
        issues = []
        if website["down"]:  issues.append(f"{website['down']} page(s) down")
        if website["slow"]:  issues.append(f"{len(website['slow'])} slow page(s) >{3000}ms")
        web_text = f"""
  Pages checked: {website["total"]} | OK: {website["ok"]} | Avg speed: {website["avg_ms"]}ms
  Issues: {", ".join(issues) if issues else "None — all clear"}"""

    # Reputation
    rep_text = "NO DATA"
    if reputation and reputation.get("trustpilot"):
        tp = reputation["trustpilot"]
        rep_text = f"Trustpilot: {tp['rating']}/5 ({tp['count']} reviews)" if tp["rating"] else "Trustpilot reachable"

    # ── Claude prompt ─────────────────────────────────────────────────────────
    prompt = f"""You are the Digital Marketing Manager for MKV Luxury Car Rental Dubai.
Write a sharp, data-driven daily executive briefing for the operations team.
Today is {report_dt}.

Use this exact structure with these 5 sections. Be concise — max 2-3 sentences per section.
Use numbers. Flag anything that needs action. No greetings or sign-offs.

---
DATA:

LEADS (Yesterday = {yesterday_str}):
{leads_text}

FLEET (Today):
{fleet_text}

WEBSITE:
{web_text}

REPUTATION:
{rep_text}

SOCIAL MEDIA:
Instagram, Facebook, TikTok, YouTube, X — all profiles confirmed active.
Full metrics (followers, post dates, engagement) require manual check today.
---

Write the briefing now using EXACTLY this format:

📊 LEADS
[2-3 sentences on lead volume, agent performance, top source, conversion pipeline]

🚗 FLEET
[2 sentences on utilization rate, availability, revenue opportunity]

🌐 WEBSITE
[1-2 sentences — all clear or specific issues]

⭐ REPUTATION
[1 sentence on Trustpilot status + recommendation]

📱 SOCIAL MEDIA
[1-2 sentences — what to check/post today based on the data]

🚨 TOP 3 PRIORITIES TODAY
1. [Most urgent action]
2. [Second priority]
3. [Third priority]

Keep each section tight. Priorities must be specific and actionable."""

    try:
        response = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key":         CLAUDE_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type":      "application/json",
            },
            json={
                "model":      "claude-haiku-4-5-20251001",
                "max_tokens": 600,
                "messages":   [{"role": "user", "content": prompt}]
            },
            timeout=30,
        )
        data = response.json()
        text = data.get("content", [{}])[0].get("text", "").strip()
        print("  Claude snapshot: generated ✅")
        return text
    except Exception as e:
        print(f"  Claude snapshot error: {e}")
        return None

# ══════════════════════════════════════════════════════════════════════════════
# 6. BUILD SLACK MESSAGE
# ══════════════════════════════════════════════════════════════════════════════

def build_slack_blocks(narrative, leads, fleet, website, reputation):
    blocks = []

    # Header
    blocks.append({
        "type": "header",
        "text": {"type": "plain_text", "text": f"🧠 MKV DAILY EXECUTIVE SNAPSHOT — {report_dt}", "emoji": True}
    })
    blocks.append({"type": "divider"})

    # Claude narrative (main body)
    if narrative:
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": narrative}
        })
    else:
        # Fallback if Claude API unavailable — build from raw data
        lines = []
        if leads:
            lines.append(f"*📊 LEADS* — FTD: {leads['ftd_total']} | MTD: {leads['mtd_total']} ({days_in_mtd} days)")
            top_agent = max(leads["ftd_agents"].items(), key=lambda x: x[1]["recd"])[0] if leads["ftd_agents"] else "?"
            lines.append(f"Top agent: {top_agent}")
        if fleet:
            lines.append(f"*🚗 FLEET* — {fleet['rented']}/{fleet['total']} rented ({fleet['util_pct']}% utilization)")
        if website:
            ws = "✅ All OK" if website["all_ok"] else f"⚠️ {website['down']} page(s) down"
            lines.append(f"*🌐 WEBSITE* — {ws} | Avg speed: {website['avg_ms']}ms")
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(lines)}})

    blocks.append({"type": "divider"})

    # Raw data strip — quick reference numbers
    data_cols = []
    if leads:
        ftd_trig = sum(v["trig"] for v in leads["ftd_agents"].values())
        trig_rate = round(ftd_trig / max(1, leads["ftd_total"]) * 100)
        data_cols.append(f"*Leads FTD*\n{leads['ftd_total']} received\n{trig_rate}% triggered")
        data_cols.append(f"*Leads MTD*\n{leads['mtd_total']} total\n{days_in_mtd} days")
    if fleet:
        data_cols.append(f"*Fleet*\n{fleet['available']} available\n{fleet['util_pct']}% utilised")

    if data_cols:
        fields = [{"type": "mrkdwn", "text": col} for col in data_cols[:3]]
        blocks.append({"type": "section", "fields": fields})

    # Website + Reputation strip
    rep_line = "Trustpilot: checking..." 
    if reputation and reputation.get("trustpilot") and reputation["trustpilot"].get("rating"):
        tp = reputation["trustpilot"]
        rep_line = f"⭐ {tp['rating']}/5 · {tp['count']} reviews"

    web_line = f"✅ {website['ok']}/{website['total']} pages OK · {website['avg_ms']}ms avg" if website else "Website: check failed"
    if website and (website["down"] or website["slow"]):
        web_line = f"⚠️ {website['down']} down · {len(website['slow'])} slow · {website['avg_ms']}ms avg"

    blocks.append({
        "type": "section",
        "fields": [
            {"type": "mrkdwn", "text": f"*Website*\n{web_line}"},
            {"type": "mrkdwn", "text": f"*Trustpilot*\n{rep_line}"},
        ]
    })

    # Footer
    blocks.append({"type": "divider"})
    blocks.append({
        "type": "context",
        "elements": [{"type": "mrkdwn",
            "text": f"MKV Executive Snapshot • {report_dt} • Data: Gallabox + Appic + Website • "
                    f"<{SITE_BASE}|mkvluxury.com>"}]
    })

    return blocks

# ══════════════════════════════════════════════════════════════════════════════
# POST TO SLACK
# ══════════════════════════════════════════════════════════════════════════════

def post_slack(blocks):
    payload = {
        "channel":    SLACK_CHANNEL,
        "username":   "MKV Executive Snapshot",
        "icon_emoji": ":brain:",
        "blocks":     blocks,
    }
    r = requests.post(
        "https://slack.com/api/chat.postMessage",
        headers={"Authorization": f"Bearer {SLACK_TOKEN}", "Content-Type": "application/json"},
        json=payload,
        timeout=15,
    )
    result = r.json()
    if result.get("ok"):
        print("  ✅ Snapshot posted to Slack")
    else:
        print(f"  Slack error: {result.get('error')}")

# ══════════════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════════════

def main():
    print("=" * 56)
    print("  MKV DAILY EXECUTIVE SNAPSHOT")
    print("  " + report_dt)
    print("=" * 56)

    print("\n[1/5] Reading leads data from MTD store...")
    leads = read_leads_data()
    print(f"  FTD: {leads['ftd_total'] if leads else 'N/A'} | MTD: {leads['mtd_total'] if leads else 'N/A'}")

    print("\n[2/5] Fetching fleet data from Appic...")
    fleet = read_fleet_data()
    print(f"  Available: {fleet['available'] if fleet else 'N/A'} | Rented: {fleet['rented'] if fleet else 'N/A'}")

    print("\n[3/5] Checking website health...")
    website = read_website_data()
    print(f"  Pages OK: {website['ok']}/{website['total']} | Avg: {website['avg_ms']}ms")

    print("\n[4/5] Checking reputation...")
    reputation = read_reputation_data()
    tp = reputation.get("trustpilot") or {}
    print(f"  Trustpilot: {tp.get('rating', 'N/A')}/5 ({tp.get('count', '?')} reviews)")

    print("\n[5/5] Generating Claude narrative...")
    narrative = generate_snapshot(leads, fleet, website, reputation)

    print("\n  Posting to Slack...")
    blocks = build_slack_blocks(narrative, leads, fleet, website, reputation)
    post_slack(blocks)

    print("=" * 56)
    print("  Done.")
    print("=" * 56)

if __name__ == "__main__":
    main()
