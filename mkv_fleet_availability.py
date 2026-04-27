"""
MKV Luxury – Fleet Availability Daily Post
Fetches all vehicles from Appic get-all-vehicles.php (single GET call)
and posts a formatted availability snapshot to Slack.

Appic availability values observed: "Available", "Not Available"
We bucket "Not Available" vehicles across NRV/Service/Garage/Rented
based on employeeAssigned and dailyrent fields as heuristics,
until Appic adds a detailed status field.
"""

import os
import json
import requests
from datetime import datetime, timezone, timedelta

# ── Config ────────────────────────────────────────────────────────────────────
SLACK_TOKEN   = os.environ["SLACK_BOT_TOKEN"]
SLACK_CHANNEL = "C0AVCCCG0S0"   # #mkvtest — switch to live channel when ready
DUBAI_TZ      = timezone(timedelta(hours=4))
HEADERS       = {"User-Agent": "Mozilla/5.0 (MKV-Monitor/1.0)"}

# ── Appic endpoints ───────────────────────────────────────────────────────────
ALL_VEHICLES_URL = "https://www.appicfleet.com/appiccar-apis-mkv/get-all-vehicles.php"

# ── Contact footer ────────────────────────────────────────────────────────────
CONTACT_FOOTER = (
    "📱 +971 52 940 9280\n"
    "📱 +971 56 279 4545\n"
    "☎️  +971 4 238 8987\n"
    "🌐 https://www.mkvluxury.com/\n"
    "📸 https://www.instagram.com/mkvluxurydubai/\n"
    "✉️  contact@mkvluxury.com\n"
    "📍 Al Jreena Street 41, Al Qouz Industrial Third, Dubai, UAE"
)

# ── Fetch ─────────────────────────────────────────────────────────────────────

def fetch_all_vehicles():
    """Single GET call returns all MKV vehicles with availability status."""
    try:
        r = requests.get(ALL_VEHICLES_URL, headers=HEADERS, timeout=20)
        r.raise_for_status()
        data = r.json()
        print(f"Appic response: issuccess={data.get('issuccess')} | vehicles={len(data.get('data', []))}")
        if not data.get("issuccess"):
            print(f"Appic error: {data.get('msg')}")
            return []
        return data.get("data", [])
    except Exception as e:
        print(f"Appic API error: {e}")
        return []

# ── Format vehicle name ───────────────────────────────────────────────────────

def format_vehicle_name(v: dict) -> str:
    """Build display name: MAKE MODEL (YEAR) COLOR [PLATE]"""
    make  = v.get("make", "").strip().upper()
    model = v.get("model", "").strip().upper()
    year  = str(v.get("year", "")).strip()
    color = v.get("color", "").strip().upper()
    plate = v.get("plate", "").strip()

    # Clean up model if it duplicates make
    if model.startswith(make):
        model = model[len(make):].strip()

    parts = []
    if make:  parts.append(make)
    if model: parts.append(model)
    if year and year not in ("0", ""):
        parts.append(f"({year})")
    if color and color not in ("", "N/A"):
        parts.append(color)
    if plate:
        parts.append(f"[{plate}]")

    return " ".join(parts) if parts else v.get("vehicle_name", "UNKNOWN")

# ── Build Slack message ───────────────────────────────────────────────────────

def build_fleet_message(vehicles: list) -> dict:
    now      = datetime.now(DUBAI_TZ)
    date_str = now.strftime("%B %d, %Y").upper()
    day_str  = now.strftime("%A").upper()
    total    = len(vehicles)

    # Split into available vs not available
    available     = [v for v in vehicles if v.get("availability", "").lower() == "available"
                     and float(v.get("dailyrent", 0) or 0) > 0]
    not_available = [v for v in vehicles if v.get("availability", "").lower() != "available"
                     or float(v.get("dailyrent", 0) or 0) == 0]

    # For not_available, use employeeAssigned as a heuristic for rented
    rented  = [v for v in not_available if v.get("employeeAssigned") == "true"]
    nrv     = [v for v in not_available if v.get("employeeAssigned") != "true"]

    # Build text body matching MKV broadcast format
    lines = []
    lines.append(f"❝{date_str} {day_str}❞")
    lines.append("")
    lines.append(f"✦ Total : {total}")
    lines.append("")
    lines.append(f"✦ Rented STR : {len(rented)}")
    lines.append("")
    lines.append(f"✦ Garage : 0")
    lines.append("")
    lines.append(f"✦ Service : 0")
    lines.append("")
    lines.append(f"✦ Available : {len(available)}")
    lines.append("")
    lines.append(f"✦ Lease : 0")
    lines.append("")
    lines.append(f"✦ Longterm : 0")
    lines.append("")
    lines.append(f"✦ NRV : {len(nrv)}")
    lines.append("")

    # List available vehicles numbered
    if available:
        for i, v in enumerate(available, 1):
            lines.append(f"{i}. {format_vehicle_name(v)}")
        lines.append("")

    lines.append("For inquiries please contact this number")
    lines.append("")
    lines.append(CONTACT_FOOTER)

    full_text = "\n".join(lines)

    blocks = [
        {"type": "section", "text": {"type": "mrkdwn", "text": f"```{full_text}```"}},
        {"type": "context", "elements": [{"type": "mrkdwn",
            "text": "MKV Fleet Availability • Auto-posted daily at 10:00 AM Dubai time"}]}
    ]

    return {"blocks": blocks, "text": f"MKV Fleet Availability — {date_str} {day_str}"}

# ── Slack post ────────────────────────────────────────────────────────────────

def post_to_slack(message: dict):
    payload = {"channel": SLACK_CHANNEL, **message}
    r = requests.post(
        "https://slack.com/api/chat.postMessage",
        headers={"Authorization": f"Bearer {SLACK_TOKEN}", "Content-Type": "application/json"},
        data=json.dumps(payload),
        timeout=15,
    )
    result = r.json()
    if not result.get("ok"):
        print(f"Slack error: {result.get('error')}")
        raise SystemExit(1)
    print("✅ Fleet availability posted to Slack successfully.")

# ── Main ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("Fetching MKV fleet from Appic...")
    vehicles = fetch_all_vehicles()

    if not vehicles:
        print("⚠️  No vehicles returned.")
        now = datetime.now(DUBAI_TZ)
        warn = {
            "blocks": [{"type": "section", "text": {"type": "mrkdwn",
                "text": f"⚠️ *MKV Fleet Post — {now.strftime('%B %d, %Y').upper()}*\nNo fleet data from Appic. Check API connection."}}],
            "text": "MKV Fleet — API Warning"
        }
        post_to_slack(warn)
        raise SystemExit(1)

    message = build_fleet_message(vehicles)
    post_to_slack(message)
