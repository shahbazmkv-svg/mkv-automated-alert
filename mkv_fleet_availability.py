"""
MKV Luxury – Fleet Availability Daily Post
Pulls fleet data from Appic API and posts a formatted availability
snapshot to Slack, matching the daily broadcast format.

Status categories (mapped from Appic statuses):
  available  → Available
  rented     → Rented STR
  garage     → Garage
  service    → Service
  lease      → Lease
  longterm   → Long Term
  nrv        → NRV (Not Road Worthy / Not Ready for Viewing)

The available vehicles are listed individually by name, year, color.
"""

import os
import json
import requests
from datetime import datetime, timezone, timedelta

# ── Config ────────────────────────────────────────────────────────────────────
SLACK_TOKEN  = os.environ["SLACK_BOT_TOKEN"]
APPIC_URL    = os.environ.get("APPIC_API_URL", "")
APPIC_KEY    = os.environ.get("APPIC_API_KEY", "")
SLACK_CHANNEL = "C0AVCCCG0S0"   # #mkvtest — switch to live channel when ready

DUBAI_TZ = timezone(timedelta(hours=4))

# ── Status mapping from Appic → display labels ────────────────────────────────
# Adjust these keys to match your exact Appic status field values
STATUS_MAP = {
    "available":  "Available",
    "rented":     "Rented STR",
    "rent":       "Rented STR",
    "str":        "Rented STR",
    "garage":     "Garage",
    "service":    "Service",
    "lease":      "Lease",
    "long_term":  "Longterm",
    "longterm":   "Longterm",
    "nrv":        "NRV",
    "not_ready":  "NRV",
}

# Display order and emoji for each status bucket
STATUS_ORDER = [
    ("Rented STR", "✦"),
    ("Garage",     "✦"),
    ("Service",    "✦"),
    ("Available",  "✦"),
    ("Lease",      "✦"),
    ("Longterm",   "✦"),
    ("NRV",        "✦"),
]

# Contact footer (fixed)
CONTACT_FOOTER = (
    "📱 +971 52 940 9280\n"
    "📱 +971 56 279 4545\n"
    "☎️  +971 4 238 8987\n"
    "🌐 https://www.mkvluxury.com/\n"
    "📸 https://www.instagram.com/mkvluxurydubai/\n"
    "✉️  contact@mkvluxury.com\n"
    "📍 Al Jreena Street 41, Al Qouz Industrial Third, Dubai, UAE"
)

# ── Appic API ─────────────────────────────────────────────────────────────────

def fetch_fleet_from_appic():
    """
    Fetch all vehicles from Appic.
    Returns a list of vehicle dicts.
    Appic is POST-only and requires startDate + endDate.
    We use today's date for both to get current fleet snapshot.
    """
    today = datetime.now(DUBAI_TZ).strftime("%Y-%m-%d")

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {APPIC_KEY}",
    }
    payload = {
        "startDate": today,
        "endDate":   today,
    }

    try:
        r = requests.post(APPIC_URL, headers=headers, json=payload, timeout=20)
        r.raise_for_status()
        data = r.json()
        # Appic typically returns {"data": [...]} or a list directly
        if isinstance(data, list):
            return data
        return data.get("data", data.get("vehicles", data.get("fleet", [])))
    except Exception as e:
        print(f"Appic API error: {e}")
        return []


def normalise_status(raw_status: str) -> str:
    """Map Appic raw status string to our display category."""
    key = raw_status.lower().strip().replace(" ", "_").replace("-", "_")
    return STATUS_MAP.get(key, raw_status.title())


def format_vehicle_name(v: dict) -> str:
    """
    Build a clean display name from Appic vehicle fields.
    Adjust field names below to match your actual Appic response keys.
    Common Appic fields: make, model, year, color, plate_number
    """
    parts = []

    make  = v.get("make", v.get("brand", "")).upper()
    model = v.get("model", "").upper()
    year  = v.get("year", v.get("model_year", ""))
    color = v.get("color", v.get("colour", "")).upper()
    plate = v.get("plate_number", v.get("plate", ""))

    if make:  parts.append(make)
    if model: parts.append(model)
    if year:  parts.append(f"({year})")
    if color: parts.append(color)
    if plate: parts.append(f"[{plate}]")

    return " ".join(parts) if parts else v.get("name", "UNKNOWN VEHICLE")


# ── Build Message ─────────────────────────────────────────────────────────────

def build_fleet_message(vehicles: list) -> dict:
    """
    Build the Slack message blocks matching the daily broadcast format.
    """
    now       = datetime.now(DUBAI_TZ)
    date_str  = now.strftime("%B %d, %Y").upper()
    day_str   = now.strftime("%A").upper()
    total     = len(vehicles)

    # Bucket vehicles by status
    buckets = {}
    for v in vehicles:
        raw    = v.get("status", v.get("vehicle_status", "unknown"))
        status = normalise_status(raw)
        buckets.setdefault(status, []).append(v)

    # Available vehicles list (numbered)
    available_vehicles = buckets.get("Available", [])

    # ── Build plain-text body (matching broadcast style) ──
    lines = []
    lines.append(f"❝{date_str} {day_str}❞")
    lines.append("")
    lines.append(f"✦ Total : {total}")
    lines.append("")

    for label, emoji in STATUS_ORDER:
        count = len(buckets.get(label, []))
        lines.append(f"{emoji} {label} : {count}")
        lines.append("")

    # List available cars
    if available_vehicles:
        for i, v in enumerate(available_vehicles, 1):
            lines.append(f"{i}. {format_vehicle_name(v)}")
        lines.append("")

    lines.append("For inquiries please contact this number")
    lines.append("")
    lines.append(CONTACT_FOOTER)

    full_text = "\n".join(lines)

    # ── Slack blocks ──
    blocks = [
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"```{full_text}```"}
        },
        {
            "type": "context",
            "elements": [{"type": "mrkdwn",
                          "text": f"MKV Fleet Availability • Auto-posted daily at 10:00 AM Dubai time"}]
        }
    ]

    return {"blocks": blocks, "text": f"MKV Fleet Availability — {date_str}"}


# ── Slack Post ────────────────────────────────────────────────────────────────

def post_to_slack(message: dict):
    payload = {
        "channel": SLACK_CHANNEL,
        **message,
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
        raise SystemExit(1)
    print("✅ Fleet availability posted to Slack successfully.")


# ── Main ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("Fetching fleet data from Appic...")
    vehicles = fetch_fleet_from_appic()

    if not vehicles:
        print("⚠️  No vehicles returned from Appic. Check API credentials or endpoint.")
        # Post a warning to Slack rather than silently failing
        now      = datetime.now(DUBAI_TZ)
        date_str = now.strftime("%B %d, %Y").upper()
        day_str  = now.strftime("%A").upper()
        warn_msg = {
            "blocks": [{
                "type": "section",
                "text": {"type": "mrkdwn",
                         "text": f"⚠️ *MKV Fleet Post — {date_str} {day_str}*\nNo fleet data returned from Appic API. Please check the API connection."}
            }],
            "text": "MKV Fleet — API Warning"
        }
        post_to_slack(warn_msg)
        raise SystemExit(1)

    print(f"✅ {len(vehicles)} vehicles fetched.")
    message = build_fleet_message(vehicles)
    post_to_slack(message)
