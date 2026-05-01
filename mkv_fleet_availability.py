"""
MKV Luxury – Fleet Availability Daily Post
"""

import os, json, requests
from datetime import datetime, timezone, timedelta

SLACK_TOKEN      = os.environ["SLACK_BOT_TOKEN"]
APPIC_KEY        = os.environ.get("APPIC_KEY", "")
SLACK_CHANNEL    = "C0B0TGBDCDU"   # #mkvtest — switch to live channel when ready
DUBAI_TZ         = timezone(timedelta(hours=4))
HEADERS          = {"User-Agent": "Mozilla/5.0 (MKV-Monitor/1.0)"}
BLOCK_LIMIT      = 2900

ASSIGNMENTS_URL  = "https://www.appicfleet.com/appiccar-apis-mkv/get-mkv-vehicle-assignments.php"
ALL_VEHICLES_URL = "https://www.appicfleet.com/appiccar-apis-mkv/get-all-vehicles.php"
BOOKINGS_URL     = "https://www.appicfleet.com/appiccar-apis-mkv/get-mkv-bookings.php"

CONTACT_FOOTER = (
    "📱 +971 52 940 9280\n"
    "📱 +971 56 279 4545\n"
    "☎️  +971 4 238 8987\n"
    "🌐 https://www.mkvluxury.com/\n"
    "📸 https://www.instagram.com/mkvluxurydubai/\n"
    "✉️  contact@mkvluxury.com\n"
    "📍 Al Jreena Street 41, Al Qouz Industrial Third, Dubai, UAE"
)

def fetch_active_vehicles() -> list:
    try:
        r = requests.get(ALL_VEHICLES_URL, headers=HEADERS, timeout=20)
        r.raise_for_status()
        data = r.json()
        all_v = data.get("data", [])
        active = [v for v in all_v
                  if float(v.get("dailyrent", 0) or 0) > 0
                  and str(v.get("plate", "")).strip() not in ("", "0")]
        print(f"Total vehicles: {len(all_v)} | Active (dailyrent>0): {len(active)}")
        # Print sample plates from vehicles API
        sample = [str(v.get("plate","")).strip() for v in active[:5]]
        print(f"Sample vehicle plates: {sample}")
        return active
    except Exception as e:
        print(f"All vehicles API error: {e}")
        return []

def fetch_rented_plates() -> set:
    try:
        now         = datetime.now(DUBAI_TZ)
        today       = now.strftime("%Y-%m-%d")
        start_range = (now - timedelta(days=90)).strftime("%Y-%m-%d")
        r = requests.post(BOOKINGS_URL, data={
            "key":       APPIC_KEY,
            "startDate": start_range,
            "endDate":   today,
        }, timeout=20)
        r.raise_for_status()
        data     = r.json()
        bookings = data.get("bookings", [])
        rented   = set()
        for b in bookings:
            plate = str(b.get("vehiclePlate", "")).strip()
            start = (b.get("startDate") or "").strip()
            end   = (b.get("endDate")   or "").strip()
            if plate and start and end and start <= today <= end:
                rented.add(plate)
        print(f"Rented plates from bookings API: {len(rented)}")
        print(f"Sample rented plates: {list(rented)[:5]}")
        return rented
    except Exception as e:
        print(f"Bookings API error: {e}")
        return set()

def format_vehicle_name(v: dict) -> str:
    make  = v.get("make",  "").strip().upper()
    model = v.get("model", "").strip().upper()
    year  = str(v.get("year", "")).strip()
    color = v.get("color", "").strip().upper()
    plate = v.get("plate", "").strip()
    if model.startswith(make): model = model[len(make):].strip()
    if make in ("AC", ""): make = ""
    parts = []
    if make:  parts.append(make)
    if model: parts.append(model)
    if year and year not in ("0", "200", ""): parts.append(f"({year})")
    if color and color not in ("", "N/A"): parts.append(color)
    if plate: parts.append(f"[{plate}]")
    return " ".join(parts) if parts else v.get("vehicle_name", "UNKNOWN").upper()

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

def build_fleet_message(vehicles: list, rented_plates: set) -> dict:
    now      = datetime.now(DUBAI_TZ)
    date_str = now.strftime("%B %d, %Y").upper()
    day_str  = now.strftime("%A").upper()

    rented_v    = []
    available_v = []

    for v in vehicles:
        plate = str(v.get("plate", "")).strip()
        if plate in rented_plates:
            rented_v.append(v)
        else:
            available_v.append(v)

    total     = len(vehicles)
    rented    = len(rented_v)
    available = len(available_v)

    print(f"Fleet — Total: {total} | Rented: {rented} | Available: {available}")

    summary = "\n".join([
        f"❝{date_str} {day_str}❞", "",
        f"✦ Total        : {total}", "",
        f"✦ Rented STR   : {rented}", "",
        f"✦ Garage       : 0", "",
        f"✦ Service      : 0", "",
        f"✦ Available    : {available}", "",
        f"✦ Lease        : 0", "",
        f"✦ Longterm     : 0", "",
        f"✦ NRV          : 0",
    ])

    vehicle_lines = [f"{i}. {format_vehicle_name(v)}" for i, v in enumerate(available_v, 1)]
    vehicle_lines += ["", "For inquiries please contact this number", "", CONTACT_FOOTER]
    vehicle_text  = "\n".join(vehicle_lines)

    blocks = []
    blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": f"```{summary}```"}})
    blocks.append({"type": "divider"})
    blocks.extend(text_to_blocks(vehicle_text))
    blocks.append({"type": "context", "elements": [{"type": "mrkdwn",
        "text": "MKV Active Fleet • Auto-posted daily 10:00 AM Dubai time"}]})

    return {"blocks": blocks, "text": f"MKV Fleet Availability — {date_str} {day_str}"}

def post_to_slack(message: dict):
    r = requests.post(
        "https://slack.com/api/chat.postMessage",
        headers={"Authorization": f"Bearer {SLACK_TOKEN}", "Content-Type": "application/json"},
        data=json.dumps({
            "channel":      SLACK_CHANNEL,
            "username":     "MKV Fleet Status",
            "icon_emoji":   ":car:",
            "unfurl_links": False,
            "unfurl_media": False,
            **message
        }),
        timeout=15,
    )
    result = r.json()
    if not result.get("ok"):
        print(f"Slack error: {result.get('error')}")
        raise SystemExit(1)
    print("✅ Fleet availability posted to Slack successfully.")

if __name__ == "__main__":
    print("Fetching MKV fleet data...")
    vehicles      = fetch_active_vehicles()
    rented_plates = fetch_rented_plates()

    if not vehicles:
        now  = datetime.now(DUBAI_TZ)
        warn = {"blocks": [{"type": "section", "text": {"type": "mrkdwn",
                "text": f"⚠️ *MKV Fleet — {now.strftime('%B %d, %Y').upper()}*\nNo active vehicles. Check API."}}],
                "text": "MKV Fleet — Warning"}
        post_to_slack(warn)
        raise SystemExit(1)

    message = build_fleet_message(vehicles, rented_plates)
    post_to_slack(message)
