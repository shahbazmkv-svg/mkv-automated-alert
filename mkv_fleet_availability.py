"""
MKV Luxury – Fleet Availability Daily Post

Logic:
  - Active fleet = vehicles with dailyrent > 0 and valid plate
  - Rented = vehicle has an active contract (from assignments API)
  - Available = active fleet - rented - service - NRV
"""

import os
import json
import requests
from datetime import datetime, timezone, timedelta

# ── Config ────────────────────────────────────────────────────────────────────
SLACK_TOKEN   = os.environ["SLACK_BOT_TOKEN"]
SLACK_CHANNEL = "C0B0TGBDCDU"   # #mkvtest — switch to live channel when ready
DUBAI_TZ      = timezone(timedelta(hours=4))
HEADERS       = {"User-Agent": "Mozilla/5.0 (MKV-Monitor/1.0)"}
BLOCK_LIMIT   = 2900

ASSIGNMENTS_URL  = "https://www.appicfleet.com/appiccar-apis-mkv/get-mkv-vehicle-assignments.php"
ALL_VEHICLES_URL = "https://www.appicfleet.com/appiccar-apis-mkv/get-all-vehicles.php"
BOOKINGS_URL     = "https://www.appicfleet.com/appiccar-apis-mkv/get-mkv-bookings.php"
APPIC_KEY        = os.environ.get("APPIC_KEY", "")

CONTACT_FOOTER = (
    "📱 +971 52 940 9280\n"
    "📱 +971 56 279 4545\n"
    "☎️  +971 4 238 8987\n"
    "🌐 https://www.mkvluxury.com/\n"
    "📸 https://www.instagram.com/mkvluxurydubai/\n"
    "✉️  contact@mkvluxury.com\n"
    "📍 Al Jreena Street 41, Al Qouz Industrial Third, Dubai, UAE"
)

# ── Fetch data ────────────────────────────────────────────────────────────────

def fetch_mkv_counts() -> dict:
    """MKV-only counts from assignments API."""
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

def fetch_active_contracts() -> set:
    """
    Get plates of vehicles currently on active contracts (rented).
    Uses bookings API — confirms bookings = active contracts.
    """
    try:
        now      = datetime.now(DUBAI_TZ)
        today    = now.strftime("%Y-%m-%d")
        # Search 90 days back to catch all active bookings spanning today
        from datetime import timedelta
        start_range = (now - timedelta(days=90)).strftime("%Y-%m-%d")
        r = requests.post(BOOKINGS_URL, data={
            "key":       APPIC_KEY,
            "startDate": start_range,
            "endDate":   today,
        }, timeout=20)
        r.raise_for_status()
        data = r.json()
        bookings = data.get("bookings", [])
        # Return set of plates with active contracts spanning today
        rented_plates = set()
        for b in bookings:
            plate  = str(b.get("vehiclePlate", "")).strip()
            start  = (b.get("startDate") or "").strip()
            end    = (b.get("endDate")   or "").strip()
            status = (b.get("status")    or "").lower()
            # Active = any booking spanning today (confirmed OR draft = vehicle is out)
            if plate and start and end and start <= today <= end:
                rented_plates.add(plate)
        print(f"Active contracts today: {len(rented_plates)} vehicles rented")
        print(f"  Date range searched: {start_range} to {today}")
        print(f"  Total bookings returned by API: {len(bookings)}")
        print(f"  Sample rented plates: {list(rented_plates)[:5]}")
        return rented_plates
    except Exception as e:
        print(f"Bookings API error: {e}")
        return set()

def fetch_active_vehicles() -> list:
    """
    GET all vehicles, filter by dailyrent > 0 and valid plate.
    These are MKV's active fleet (borrowed vehicles have dailyrent=0).
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

        print(f"Active fleet (dailyrent>0): {len(active)} | Excluded: {len(all_vehicles)-len(active)}")
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

def build_fleet_message(counts: dict, vehicles: list, rented_plates: set) -> dict:
    now      = datetime.now(DUBAI_TZ)
    date_str = now.strftime("%B %d, %Y").upper()
    day_str  = now.strftime("%A").upper()

    # Categorise each vehicle by contract status
    rented_vehicles    = []
    available_vehicles = []
    service_vehicles   = []
    nrv_vehicles       = []

    for v in vehicles:
        plate      = str(v.get("plate", "")).strip()
        avail      = (v.get("availability") or "").lower().strip()
        status_raw = (v.get("status") or "").lower().strip()

        # Primary rule: has active contract today → Rented
        if plate in rented_plates:
            rented_vehicles.append(v)
        # Check explicit status from API
        elif avail in ("service", "maintenance") or status_raw in ("service", "maintenance"):
            service_vehicles.append(v)
        elif avail in ("unavailable", "nrv") or status_raw in ("unavailable", "nrv"):
            nrv_vehicles.append(v)
        else:
            # No active contract and no special status → Available
            available_vehicles.append(v)

    total     = len(vehicles)
    rented    = len(rented_vehicles)
    service   = counts.get("service", len(service_vehicles))
    nrv       = counts.get("unavailable", len(nrv_vehicles))
    available = len(available_vehicles)

    print(f"Fleet summary — Total: {total} | Rented: {rented} | Available: {available} | Service: {service} | NRV: {nrv}")

    summary = "\n".join([
        f"❝{date_str} {day_str}❞", "",
        f"✦ Total        : {total}", "",
        f"✦ Rented STR   : {rented}", "",
        f"✦ Garage       : 0", "",
        f"✦ Service      : {service}", "",
        f"✦ Available    : {available}", "",
        f"✦ Lease        : 0", "",
        f"✦ Longterm     : 0", "",
        f"✦ NRV          : {nrv}",
    ])

    vehicle_lines = [f"{i}. {format_vehicle_name(v)}" for i, v in enumerate(available_vehicles, 1)]
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
    counts         = fetch_mkv_counts()
    vehicles       = fetch_active_vehicles()
    rented_plates  = fetch_active_contracts()

    if not vehicles:
        now = datetime.now(DUBAI_TZ)
        warn = {
            "blocks": [{"type": "section", "text": {"type": "mrkdwn",
                "text": f"⚠️ *MKV Fleet — {now.strftime('%B %d, %Y').upper()}*\nNo active vehicles returned. Check API."}}],
            "text": "MKV Fleet — Warning"
        }
        post_to_slack(warn)
        raise SystemExit(1)

    message = build_fleet_message(counts, vehicles, rented_plates)
    print(f"Slack blocks: {len(message['blocks'])}")
    post_to_slack(message)
