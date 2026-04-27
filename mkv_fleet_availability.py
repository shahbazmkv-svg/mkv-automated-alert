"""
MKV Luxury – Fleet Availability Daily Post

Uses a hardcoded whitelist of MKV-owned vehicleIDs to filter out
borrowed vehicles. Counts come from get-mkv-vehicle-assignments.php.
Vehicle list comes from get-all-vehicles.php filtered by whitelist.

To add a new MKV vehicle: add its vehicleID to MKV_VEHICLE_IDS below.
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

# ── MKV-owned vehicle ID whitelist ────────────────────────────────────────────
# Source: get-all-vehicles.php where fuel_type is NOT null
# Add new vehicleIDs here when new vehicles are added to your fleet
MKV_VEHICLE_IDS = {
    "1456", "1457", "1458", "1459", "1460", "1461", "1462", "1463", "1464", "1465",
    "1466", "1467", "1468", "1469", "1470", "1471", "1472", "1473", "1474", "1475",
    "1476", "1477", "1478", "1479", "1480", "1481", "1482", "1483", "1485", "1486",
    "1487", "1488", "1489", "1490", "1491", "1492", "1493", "1494", "1495", "1496",
    "1497", "1498", "1499", "1500", "1501", "1502", "1503", "1504", "1505", "1506",
    "1507", "1508", "1509", "1510", "1532", "1545", "1546", "2175", "2340", "2343",
    "2346", "2347", "2352", "2356", "2357", "2359", "2453", "2455", "2484", "2485",
    "2486", "2506", "2542", "2545", "2546", "2562", "2591", "2596", "2616", "2624",
    "2627", "2662", "2663", "2664", "2665", "2667", "2668", "2933", "3010", "5298",
    "5468", "5675", "5715", "5748", "5749", "5769", "5816", "5824", "5853", "5871",
    "5874", "5886", "5889", "5893", "5925", "5983", "5984", "5988", "6076", "6078",
    "6094", "6095", "6097", "6098", "6274", "6275", "6367", "6372", "6619", "6723",
    "6999", "7015", "7053", "7056", "7065", "7066",
}

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
    try:
        r = requests.post(ASSIGNMENTS_URL, headers=HEADERS, json={}, timeout=20)
        r.raise_for_status()
        data = r.json()
        print(f"Assignments API: issuccess={data.get('issuccess')} counts={data.get('counts')}")
        if not data.get("issuccess"):
            return {}
        return data.get("counts", {})
    except Exception as e:
        print(f"Assignments API error: {e}")
        return {}

# ── Fetch MKV vehicle list ─────────────────────────────────────────────────────

def fetch_mkv_vehicles() -> list:
    try:
        r = requests.get(ALL_VEHICLES_URL, headers=HEADERS, timeout=20)
        r.raise_for_status()
        data = r.json()
        all_vehicles = data.get("data", [])
        print(f"All vehicles: {len(all_vehicles)} total")

        # Filter strictly by whitelist
        mkv = [v for v in all_vehicles if str(v.get("vehicleID", "")) in MKV_VEHICLE_IDS]
        excluded = len(all_vehicles) - len(mkv)
        print(f"MKV vehicles: {len(mkv)} | Excluded (borrowed): {excluded}")
        return mkv
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

    total     = counts.get("totalNonBorrowedVehicles", len(vehicles))
    rented    = counts.get("booked", 0)
    service   = counts.get("service", 0)
    available = counts.get("available", 0)
    nrv       = counts.get("unavailable", 0)

    avail_vehicles = [
        v for v in vehicles
        if v.get("availability", "").lower() == "available"
        and float(v.get("dailyrent", 0) or 0) > 0
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
        "text": "MKV Fleet (excl. borrowed) • Auto-posted daily 10:00 AM Dubai time"}]})

    return {"blocks": blocks, "text": f"MKV Fleet Availability — {date_str} {day_str}"}

# ── Slack post ────────────────────────────────────────────────────────────────

def post_to_slack(message: dict):
    r = requests.post(
        "https://slack.com/api/chat.postMessage",
        headers={"Authorization": f"Bearer {SLACK_TOKEN}", "Content-Type": "application/json"},
        data=json.dumps({"channel": SLACK_CHANNEL, **message}),
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
    vehicles = fetch_mkv_vehicles()

    if not vehicles:
        now = datetime.now(DUBAI_TZ)
        warn = {
            "blocks": [{"type": "section", "text": {"type": "mrkdwn",
                "text": f"⚠️ *MKV Fleet — {now.strftime('%B %d, %Y').upper()}*\nNo vehicles returned. Check API."}}],
            "text": "MKV Fleet — Warning"
        }
        post_to_slack(warn)
        raise SystemExit(1)

    message = build_fleet_message(counts, vehicles)
    print(f"Slack blocks: {len(message['blocks'])}")
    post_to_slack(message)
