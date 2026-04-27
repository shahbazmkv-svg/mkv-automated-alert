"""
MKV Luxury – Fleet Availability Daily Post

Two-API approach:
  1. POST get-mkv-vehicle-assignments.php  → MKV-only counts (booked/service/available etc)
  2. GET  get-all-vehicles.php             → full list, filter borrowed (fuel_type=null)

Borrowed vehicles are excluded from both counts and vehicle listing.
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

ASSIGNMENTS_URL = "https://www.appicfleet.com/appiccar-apis-mkv/get-mkv-vehicle-assignments.php"
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
    """
    POST get-mkv-vehicle-assignments.php
    Returns counts for MKV-owned vehicles only (borrowed excluded).
    Expected keys: booked, service, available, unavailable,
                   totalNonBorrowedVehicles, totalBorrowedVehicles
    """
    try:
        r = requests.post(ASSIGNMENTS_URL, headers=HEADERS, json={}, timeout=20)
        r.raise_for_status()
        data = r.json()
        print(f"Assignments API: issuccess={data.get('issuccess')} | counts={data.get('counts')}")
        if not data.get("issuccess"):
            print(f"Assignments API error: {data.get('msg')}")
            return {}
        return data.get("counts", {})
    except Exception as e:
        print(f"Assignments API error: {e}")
        return {}

# ── Fetch vehicle list ────────────────────────────────────────────────────────

def fetch_mkv_vehicles() -> list:
    """
    GET get-all-vehicles.php then filter out borrowed vehicles.
    MKV-owned vehicles have fuel_type != null.
    Borrowed vehicles have fuel_type = null.
    """
    try:
        r = requests.get(ALL_VEHICLES_URL, headers=HEADERS, timeout=20)
        r.raise_for_status()
        data = r.json()
        all_vehicles = data.get("data", [])
        print(f"All vehicles API: total={len(all_vehicles)}")

        # Filter to MKV-owned only (fuel_type is not None)
        mkv_vehicles = [v for v in all_vehicles if v.get("fuel_type") is not None]
        borrowed     = len(all_vehicles) - len(mkv_vehicles)
        print(f"After filter: MKV={len(mkv_vehicles)} | Borrowed={borrowed} (excluded)")

        return mkv_vehicles
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

    # Remove make prefix from model if duplicated
    if model.startswith(make):
        model = model[len(make):].strip()

    # Skip placeholder makes
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
    blocks = []
    lines  = text.split("\n")
    chunk  = ""
    for line in lines:
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

    # Use counts from assignments API (MKV-only, borrowed excluded)
    total     = counts.get("totalNonBorrowedVehicles", len(vehicles))
    rented    = counts.get("booked", 0)
    service   = counts.get("service", 0)
    available = counts.get("available", 0)
    unavail   = counts.get("unavailable", 0)

    # Derive Garage/Lease/Longterm/NRV from unavailable
    # Until Appic adds granular breakdown, show unavailable as NRV
    nrv       = unavail

    # Available vehicles list — from get-all-vehicles filtered to MKV + available
    avail_vehicles = [
        v for v in vehicles
        if v.get("availability", "").lower() == "available"
        and float(v.get("dailyrent", 0) or 0) > 0
    ]

    # ── Summary ──
    summary = "\n".join([
        f"❝{date_str} {day_str}❞",
        "",
        f"✦ Total : {total}",
        "",
        f"✦ Rented STR : {rented}",
        "",
        f"✦ Garage : 0",
        "",
        f"✦ Service : {service}",
        "",
        f"✦ Available : {available}",
        "",
        f"✦ Lease : 0",
        "",
        f"✦ Longterm : 0",
        "",
        f"✦ NRV : {nrv}",
    ])

    # ── Vehicle list ──
    vehicle_lines = []
    for i, v in enumerate(avail_vehicles, 1):
        vehicle_lines.append(f"{i}. {format_vehicle_name(v)}")
    vehicle_lines += ["", "For inquiries please contact this number", "", CONTACT_FOOTER]
    vehicle_text = "\n".join(vehicle_lines)

    # ── Assemble blocks ──
    blocks = []
    blocks.append({"type": "section",
                   "text": {"type": "mrkdwn", "text": f"```{summary}```"}})
    blocks.append({"type": "divider"})
    blocks.extend(text_to_blocks(vehicle_text))
    blocks.append({"type": "context", "elements": [{"type": "mrkdwn",
        "text": f"MKV Fleet (excl. borrowed) • Auto-posted daily 10:00 AM Dubai time"}]})

    return {"blocks": blocks, "text": f"MKV Fleet Availability — {date_str} {day_str}"}

# ── Slack post ────────────────────────────────────────────────────────────────

def post_to_slack(message: dict):
    r = requests.post(
        "https://slack.com/api/chat.postMessage",
        headers={"Authorization": f"Bearer {SLACK_TOKEN}",
                 "Content-Type": "application/json"},
        data=json.dumps({"channel": SLACK_CHANNEL, **message}),
        timeout=15,
    )
    result = r.json()
    if not result.get("ok"):
        print(f"Slack error: {result.get('error')}")
        for i, b in enumerate(message.get("blocks", [])):
            txt = b.get("text", {}).get("text", "")
            print(f"  Block {i}: {len(txt)} chars")
        raise SystemExit(1)
    print("✅ Fleet availability posted to Slack successfully.")

# ── Main ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("Fetching MKV fleet data...")

    counts   = fetch_mkv_counts()
    vehicles = fetch_mkv_vehicles()

    if not counts and not vehicles:
        now = datetime.now(DUBAI_TZ)
        warn = {
            "blocks": [{"type": "section", "text": {"type": "mrkdwn",
                "text": f"⚠️ *MKV Fleet Post — {now.strftime('%B %d, %Y').upper()}*\nBoth Appic APIs failed. Check connection."}}],
            "text": "MKV Fleet — API Warning"
        }
        post_to_slack(warn)
        raise SystemExit(1)

    print(f"Blocks will be generated from {len(vehicles)} MKV vehicles")
    message = build_fleet_message(counts, vehicles)
    print(f"Total Slack blocks: {len(message['blocks'])}")
    post_to_slack(message)
