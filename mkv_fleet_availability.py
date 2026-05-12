"""
MKV Luxury – Fleet Availability
Uses get-mkv-vehicles.php (POST) for vehicle data.
"""
import os, json, re, requests
from datetime import datetime, timezone, timedelta

SLACK_TOKEN   = os.environ["SLACK_BOT_TOKEN"]
APPIC_KEY     = os.environ.get("APPIC_KEY", "")
SLACK_CHANNEL = "C0B0TGBDCDU"   # #mkvtest — update to live channel when ready
DUBAI_TZ      = timezone(timedelta(hours=4))
BLOCK_LIMIT   = 2900

MKV_VEHICLES_URL = "https://www.appicfleet.com/appiccar-apis-mkv/get-mkv-vehicles.php"
BOOKINGS_URL     = "https://www.appicfleet.com/appiccar-apis-mkv/get-mkv-bookings.php"

CONTACT_FOOTER = (
    "📱 +971 52 940 9280\n📱 +971 56 279 4545\n☎️  +971 4 238 8987\n"
    "🌐 https://www.mkvluxury.com/\n📸 https://www.instagram.com/mkvluxurydubai/\n"
    "✉️  contact@mkvluxury.com\n📍 Al Jreena Street 41, Al Qouz Industrial Third, Dubai, UAE"
)

def now_dubai():
    return datetime.now(DUBAI_TZ)

def parse_date(date_str: str):
    """Parse a date string in any common format into a date object. Returns None on failure."""
    if not date_str:
        return None
    date_str = date_str.strip()
    for fmt in ("%Y-%m-%d", "%Y-%m-%d %H:%M:%S", "%d/%m/%Y", "%m/%d/%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(date_str, fmt).date()
        except ValueError:
            continue
    return None

def fetch_rented_plates() -> set:
    """Get plates of all vehicles on active contracts today."""
    try:
        today     = now_dubai().date()
        start_str = (now_dubai() - timedelta(days=365)).strftime("%Y-%m-%d")
        end_str   = (now_dubai() + timedelta(days=365)).strftime("%Y-%m-%d")

        r = requests.post(BOOKINGS_URL, data={
            "key": APPIC_KEY, "startDate": start_str, "endDate": end_str
        }, timeout=20)
        r.raise_for_status()
        bookings = r.json().get("bookings", [])
        print(f"  Bookings returned from API: {len(bookings)}")

        rented = set()
        for b in bookings:
            booking_status = str(b.get("status") or b.get("bookingStatus") or "").lower().strip()
            if booking_status in ("cancelled", "canceled", "voided", "void", "deleted"):
                continue

            plate = str(b.get("vehiclePlate", "") or "").strip()
            if not plate:
                continue

            start_date = parse_date(b.get("startDate") or "")
            end_date   = parse_date(b.get("endDate")   or "")

            if start_date is None or end_date is None:
                continue

            if start_date <= today <= end_date:
                rented.add(plate)

        print(f"  Active rented plates today ({today}): {len(rented)}")
        return rented
    except Exception as ex:
        print(f"  Bookings API error: {ex}")
        return set()

def fetch_vehicles() -> list:
    """Get all MKV vehicles via POST to get-mkv-vehicles.php (dailyrent > 0)."""
    try:
        r = requests.post(
            MKV_VEHICLES_URL,
            data={"key": APPIC_KEY},
            timeout=20
        )
        r.raise_for_status()
        response_json = r.json()

        # Handle both {"data": [...]} and direct list responses
        if isinstance(response_json, list):
            all_v = response_json
        else:
            all_v = response_json.get("data", response_json.get("vehicles", []))

        # Debug: print keys from first vehicle
        if all_v:
            print(f"  Vehicle keys: {list(all_v[0].keys())}")
            sample = all_v[0]
            print(f"  Sample — plate={sample.get('plate')} | vehiclePlate={sample.get('vehiclePlate')} | plateNo={sample.get('plateNo')} | dailyrent={sample.get('dailyrent')}")

        active = [v for v in all_v if float(v.get("dailyrent", 0) or 0) > 0]
        print(f"  Total vehicles: {len(all_v)} | Active (dailyrent>0): {len(active)}")
        return active
    except Exception as ex:
        print(f"  Vehicles API error: {ex}")
        return []

def get_plate(v: dict) -> str:
    """Extract plate from vehicle dict — try all possible keys."""
    for key in ["plate", "vehiclePlate", "plateNo", "plate_no", "number_plate", "licensePlate"]:
        val = str(v.get(key, "") or "").strip()
        if val and val != "0":
            return val
    return ""

def normalize_plate(plate: str) -> str:
    """Extract numeric part only for comparison.
    'I 47203' -> '47203', 'B15789' -> '15789', '77540' -> '77540'
    """
    digits = re.sub(r"[^0-9]", "", plate)
    return digits.lstrip("0") if digits else plate

def fmt_name(v: dict) -> str:
    make  = v.get("make",  "").strip().upper()
    model = v.get("model", "").strip().upper()
    year  = str(v.get("year", "")).strip()
    color = v.get("color", "").strip().upper()
    plate = get_plate(v)
    if model.startswith(make): model = model[len(make):].strip()
    parts = [p for p in [make, model] if p]
    if year and year not in ("0", "200", ""): parts.append(f"({year})")
    if color and color not in ("", "N/A"):    parts.append(color)
    if plate: parts.append(f"[{plate}]")
    return " ".join(parts) or "UNKNOWN"

def text_to_blocks(text):
    blocks, chunk = [], ""
    for line in text.split("\n"):
        cand = chunk + line + "\n"
        if len(cand) > BLOCK_LIMIT:
            if chunk:
                blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": f"```{chunk.rstrip()}```"}})
            chunk = line + "\n"
        else:
            chunk = cand
    if chunk.strip():
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": f"```{chunk.rstrip()}```"}})
    return blocks

def build_message(vehicles, rented_plates):
    now      = now_dubai()
    date_str = now.strftime("%B %d, %Y").upper()
    day_str  = now.strftime("%A").upper()

    rented_v    = []
    available_v = []
    garage_v    = []
    service_v   = []

    rented_norm = set(normalize_plate(p) for p in rented_plates)

    for v in vehicles:
        plate      = get_plate(v)
        plate_norm = normalize_plate(plate)
        avail      = (v.get("availability") or "").lower().strip()
        status_raw = (v.get("status") or "").lower().strip()

        if plate_norm and plate_norm in rented_norm:
            rented_v.append(v)
        elif avail in ("garage",) or status_raw in ("garage",):
            garage_v.append(v)
        elif avail in ("service", "maintenance") or status_raw in ("service", "maintenance"):
            service_v.append(v)
        else:
            available_v.append(v)

    total     = len(vehicles)
    rented    = len(rented_v)
    available = len(available_v)
    garage    = len(garage_v)
    service   = len(service_v)
    print(f"  Fleet — Total:{total} | Rented:{rented} | Available:{available} | Garage:{garage} | Service:{service}")

    summary = "\n".join([
        f"❝{date_str} {day_str}❞", "",
        f"✦ Total        : {total}", "",
        f"✦ Rented STR   : {rented}", "",
        f"✦ Garage       : {garage}", "",
        f"✦ Service      : {service}", "",
        f"✦ Available    : {available}", "",
        f"✦ Lease        : 0", "",
        f"✦ Longterm     : 0", "",
        f"✦ NRV          : 0",
    ])

    lines = []
    if available_v:
        lines.append("AVAILABLE")
        lines.append("-" * 30)
        for i, v in enumerate(available_v, 1):
            lines.append(f"{i}. {fmt_name(v)}")
    if garage_v:
        lines.append("")
        lines.append("GARAGE")
        lines.append("-" * 30)
        for i, v in enumerate(garage_v, 1):
            lines.append(f"{i}. {fmt_name(v)}")
    if service_v:
        lines.append("")
        lines.append("SERVICE")
        lines.append("-" * 30)
        for i, v in enumerate(service_v, 1):
            lines.append(f"{i}. {fmt_name(v)}")
    lines += ["", "For inquiries please contact this number", "", CONTACT_FOOTER]

    blocks = [
        {"type": "section", "text": {"type": "mrkdwn", "text": f"```{summary}```"}},
        {"type": "divider"},
        *text_to_blocks("\n".join(lines)),
        {"type": "context", "elements": [{"type": "mrkdwn",
          "text": "MKV Active Fleet • Auto-posted daily 10:00 AM Dubai time"}]},
    ]
    return {"blocks": blocks, "text": f"MKV Fleet Availability — {date_str} {day_str}"}

def post_slack(message):
    r = requests.post(
        "https://slack.com/api/chat.postMessage",
        headers={"Authorization": f"Bearer {SLACK_TOKEN}", "Content-Type": "application/json"},
        data=json.dumps({"channel": SLACK_CHANNEL, "username": "MKV Fleet Status",
                         "icon_emoji": ":car:", "unfurl_links": False, "unfurl_media": False,
                         **message}),
        timeout=15,
    )
    res = r.json()
    if not res.get("ok"):
        print(f"Slack error: {res.get('error')}")
        raise SystemExit(1)
    print("✅ Posted to Slack")

if __name__ == "__main__":
    print("=" * 50)
    print("MKV FLEET AVAILABILITY")
    print("=" * 50)
    rented_plates = fetch_rented_plates()
    vehicles      = fetch_vehicles()
    if not vehicles:
        print("No vehicles returned from API")
        raise SystemExit(1)
    msg = build_message(vehicles, rented_plates)
    post_slack(msg)
