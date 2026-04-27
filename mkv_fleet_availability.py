"""
MKV Luxury – Fleet Availability Daily Post

Active fleet filter:
  - Counts  → get-mkv-vehicle-assignments.php (MKV-only, borrowed excluded by Appic)
  - Vehicle list → get-all-vehicles.php filtered by dailyrent > 0
    (Borrowed vehicles have dailyrent = 0 set in Appic)
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
BLOCK_LIMIT   = 2900

ASSIGNMENTS_URL  = "https://www.appicfleet.com/appiccar-apis-mkv/get-mkv-vehicle-assignments.php"
ALL_VEHICLES_URL = "https://www.appicfleet.com/appiccar-apis-mkv/get-all-vehicles.php"

CONTACT_FOOTER = (
    "📱 +971 52 940 9280\n"
    "📱 +971 56 279 4545\n"
    "☎️  +971 4 238 8987\n"
    "🌐 https://www.mkvluxury.com/\n"
    "📸 https://www.instagram.com/mkvluxurydubai/\n"
    "✉️  contact@mkvluxury.com\n"
    "📍 Al Jreena Street 41, Al Qouz Industrial Third, Dubai, UAE"
)

# ── Fetch counts ──────────────────────────────────────────────────────────────

def fetch_mkv_counts() -> dict:
    """MKV-only counts from assignments API. Borrowed already excluded by Appic."""
    try:
        r = requests.post(ASSIGNMENTS_URL, headers=HEADERS, json={}, timeout=20)
        r.raise_for_status()
        data = r.json()
        print(f"Assignments API: issuccess={data.get('issuccess')} | counts={data.get('counts')}")
        if not data.get("issuccess"):
            return {}
        return data.get("counts", {})
    except Exception as e:
        print(f"Assignments API error: {e}")
        return {}

# ── Fetch active vehicle list ─────────────────────────────────────────────────

def fetch_active_vehicles() -> list:
    """
    GET all vehicles, then keep only active ones:
      - dailyrent > 0  (borrowed vehicles set to 0 in Appic)
      - has a plate number (excludes test/placeholder entries)
    """
    try:
        r = requests.get(ALL_VEHICLES_URL, headers=HEADERS, timeout=20)
        r.raise_for_status()
        data = r.json()
        all_vehicles = data.get("data", [])
        print(f"Total vehicles from API: {len(all_vehicles)}")

        active = [
            v for v in all_vehicles
            if float(v.get("dailyrent", 0) or 0) > 0
            and str(v.get("plate", "")).strip() not in ("", "0")
        ]

        excluded = len(all_vehicles) - len(active)
        print(f"Active vehicles: {len(active)} | Excluded (dailyrent=0 or no plate): {excluded}")
        return active
    except Exception as e:
        print(f"All vehicles API error: {e}")
        return []

# ── Format vehicle name ───────────────────────────────────────────────────────

def format_vehicle_name(v: dict) -> str:
    make  = v.get("make", "").strip().upper()
    model = v.get("model", "").strip().upper()
    year  = str(v.get("year", "")).strip()
    color = v.get("color", "").strip().upper()
    plate = v.get("plate", "").strip()

    if model.startswith(make):
        model = model[len(make):].strip()
    if make in ("AC", ""):
        make = ""

    parts = []
    if make:  parts.append(make)
    if model: parts.append(model)
    if year and year not in ("0", "200", ""):
        parts.append(f"({year})")
    if color and color not in ("", "N/A"):
        parts.append(color)
    if plate:
        parts.append(f"[{plate}]")

    return " ".join(parts) if parts else v.get("vehicle_name", "UNKNOWN").upper()

# ── Split into Slack-safe blocks ──────────────────────────────────────────────

def text_to_blocks(text: str) -> list:
    blocks, chunk = [], ""
    for line in text.split("\n"):
        candidate = chunk + line + "\n"
        if len(candidate) > BLOCK_LIMIT:
            if chunk:
                blocks.append({"type": "section",
                                "text": {"type": "mrkdwn", "text": f"```{chunk.rstrip()}```"}})
            chunk = line + "\n"
        else:
            chunk = candidate
    if chunk.strip():
        blocks.append({"type": "section",
                        "text": {"type": "mrkdwn", "text": f"```{chunk.rstrip()}```"}})
    return blocks

# ── Build message ─────────────────────────────────────────────────────────────

def build_fleet_message(counts: dict, vehicles: list) -> dict:
    now      = datetime.now(DUBAI_TZ)
    date_str = now.strftime("%B %d, %Y").upper()
    day_str  = now.strftime("%A").upper()

    # Counts from assignments API (MKV-only)
    total     = counts.get("totalNonBorrowedVehicles", len(vehicles))
    rented    = counts.get("booked", 0)
    service   = counts.get("service", 0)
    available = counts.get("available", 0)
    nrv       = counts.get("unavailable", 0)

    # Available vehicle list — active + available status
    avail_vehicles = [
        v for v in vehicles
        if v.get("availability", "").lower() == "available"
    ]

    summary = "\n".join([
        f"❝{date_str} {day_str}❞", "",
        f"✦ Total : {total}", "",
        f"✦ Rented STR : {rented}", "",
        f"✦ Garage : 0", "",
        f"✦ Service : {service}", "",
        f"✦ Available : {available}", "",
        f"✦ Lease : 0", "",
        f"✦ Longterm : 0", "",
        f"✦ NRV : {nrv}",
    ])

    vehicle_lines = [f"{i}. {format_vehicle_name(v)}" for i, v in enumerate(avail_vehicles, 1)]
    vehicle_lines += ["", "For inquiries please contact this number", "", CONTACT_FOOTER]
    vehicle_text = "\n".join(vehicle_lines)

    blocks = []
    blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": f"```{summary}```"}})
    blocks.append({"type": "divider"})
    blocks.extend(text_to_blocks(vehicle_text))
    blocks.append({"type": "context", "elements": [{"type": "mrkdwn",
        "text": "MKV Active Fleet • Auto-posted daily 10:00 AM Dubai time"}]})

    return {"blocks": blocks, "text": f"MKV Fleet Availability — {date_str} {day_str}"}

# ── Slack post ────────────────────────────────────────────────────────────────

def post_to_slack(message: dict):
    r = requests.post(
        "https://slack.com/api/chat.postMessage",
        headers={"Authorization": f"Bearer {SLACK_TOKEN}", "Content-Type": "application/json"},
        data=json.dumps({
            "channel":    SLACK_CHANNEL,
            "username":   "MKV Fleet Status",
            "icon_emoji": ":car:",
            **message
        }),
        timeout=15,
    )
    result = r.json()
    if not result.get("ok"):
        print(f"Slack error: {result.get('error')}")
        raise SystemExit(1)
    print("✅ Fleet availability posted to Slack successfully.")

# ── Main ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("Fetching MKV fleet data...")
    counts   = fetch_mkv_counts()
    vehicles = fetch_active_vehicles()

    if not vehicles:
        now = datetime.now(DUBAI_TZ)
        warn = {
            "blocks": [{"type": "section", "text": {"type": "mrkdwn",
                "text": f"⚠️ *MKV Fleet — {now.strftime('%B %d, %Y').upper()}*\nNo active vehicles returned. Check API."}}],
            "text": "MKV Fleet — Warning"
        }
        post_to_slack(warn)
        raise SystemExit(1)

    message = build_fleet_message(counts, vehicles)
    print(f"Slack blocks: {len(message['blocks'])}")
    post_to_slack(message)
