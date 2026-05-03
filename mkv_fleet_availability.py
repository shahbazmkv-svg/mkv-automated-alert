"""
MKV Luxury – Fleet Availability
Clean rebuild using only Appic Bookings API for rented status.
"""
import os, json, requests
from datetime import datetime, timezone, timedelta

SLACK_TOKEN   = os.environ["SLACK_BOT_TOKEN"]
APPIC_KEY     = os.environ.get("APPIC_KEY", "")
SLACK_CHANNEL = "C0B0TGBDCDU"   # #mkvtest — update to live channel when ready
DUBAI_TZ      = timezone(timedelta(hours=4))
BLOCK_LIMIT   = 2900

ALL_VEHICLES_URL = "https://www.appicfleet.com/appiccar-apis-mkv/get-all-vehicles.php"
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
    """
    Get plates of all vehicles on active contracts today.

    Fixes vs old version:
    1. Dates parsed properly (not string-compared) — handles any format Appic returns.
    2. endDate in API call set 1 year ahead so future-end contracts are included.
    3. Cancelled / voided statuses are excluded.
    """
    try:
        today      = now_dubai().date()
        start_str  = (now_dubai() - timedelta(days=365)).strftime("%Y-%m-%d")   # 1 yr back
        end_str    = (now_dubai() + timedelta(days=365)).strftime("%Y-%m-%d")   # 1 yr ahead
        r = requests.post(BOOKINGS_URL, data={
            "key": APPIC_KEY, "startDate": start_str, "endDate": end_str
        }, timeout=20)
        r.raise_for_status()
        bookings = r.json().get("bookings", [])
        print(f"  Bookings returned from API: {len(bookings)}")

        rented = set()
        for b in bookings:
            # Skip cancelled / voided contracts
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

            # Active contract = today falls within [startDate, endDate] inclusive
            if start_date <= today <= end_date:
                rented.add(normalize_plate(plate))   # store normalized so matching is consistent

        print(f"  Active rented plates today ({today}): {len(rented)}")
        return rented
    except Exception as ex:
        print(f"  Bookings API error: {ex}")
        return set()

def fetch_vehicles() -> list:
    """Get all active MKV vehicles (dailyrent > 0)."""
    try:
        r = requests.get(ALL_VEHICLES_URL,
                         headers={"User-Agent": "Mozilla/5.0"}, timeout=20)
        r.raise_for_status()
        all_v = r.json().get("data", [])
        # Print ALL keys from first vehicle to identify plate key
        if all_v:
            print(f"Vehicle keys: {list(all_v[0].keys())}")
            print(f"First vehicle sample: plate={all_v[0].get('plate')} | vehiclePlate={all_v[0].get('vehiclePlate')} | plateNo={all_v[0].get('plateNo')} | plate_no={all_v[0].get('plate_no')}")
        active = [v for v in all_v
                  if float(v.get("dailyrent", 0) or 0) > 0]
        print(f"Total: {len(all_v)} | Active (dailyrent>0): {len(active)}")
        return active
    except Exception as ex:
        print(f"Vehicles API error: {ex}")
        return []

def get_plate(v: dict) -> str:
    """Extract plate from vehicle dict — try all possible keys."""
    for key in ["plate", "vehiclePlate", "plateNo", "plate_no", "number_plate", "licensePlate"]:
        val = str(v.get(key, "") or "").strip()
        if val and val != "0":
            return val
    return ""

def normalize_plate(plate: str) -> str:
    """
    Normalize a plate for matching: uppercase, remove spaces and hyphens only.
    Preserves letters so 'BB 60137' -> 'BB60137' and 'B 60137' -> 'B60137'
    — these are different plates and must NOT collide.

    Examples:
      'I 47203'    -> 'I47203'
      'BB 60137'   -> 'BB60137'
      'X 33678'    -> 'X33678'
      'AA78042'    -> 'AA78042'
      'F83209'     -> 'F83209'
    """
    import re
    return re.sub(r"[\s\-]", "", plate).upper().strip()

def fmt_name(v: dict) -> str:
    make  = v.get("make",  "").strip().upper()
    model = v.get("model", "").strip().upper()
    year  = str(v.get("year", "")).strip()
    color = v.get("color", "").strip().upper()
    plate = get_plate(v)
    if model.startswith(make): model = model[len(make):].strip()
    parts = [p for p in [make, model] if p]
    if year and year not in ("0","200",""): parts.append(f"({year})")
    if color and color not in ("","N/A"):   parts.append(color)
    if plate: parts.append(f"[{plate}]")
    return " ".join(parts) or "UNKNOWN"

def text_to_blocks(text):
    blocks, chunk = [], ""
    for line in text.split("\n"):
        cand = chunk + line + "\n"
        if len(cand) > BLOCK_LIMIT:
            if chunk:
                blocks.append({"type":"section","text":{"type":"mrkdwn","text":f"```{chunk.rstrip()}```"}})
            chunk = line + "\n"
        else:
            chunk = cand
    if chunk.strip():
        blocks.append({"type":"section","text":{"type":"mrkdwn","text":f"```{chunk.rstrip()}```"}})
    return blocks

def build_message(vehicles, rented_plates):
    now      = now_dubai()
    date_str = now.strftime("%B %d, %Y").upper()
    day_str  = now.strftime("%A").upper()

    rented_v    = []
    available_v = []
    garage_v    = []
    service_v   = []

    # Normalize rented plates — preserve full plate (letters + digits, no spaces)
    # e.g. "BB 60137" -> "BB60137", "I 47203" -> "I47203"
    rented_norm = set(normalize_plate(p) for p in rented_plates if p)

    for v in vehicles:
        plate      = get_plate(v)
        plate_norm = normalize_plate(plate)
        avail      = (v.get("availability") or "").lower().strip()
        status_raw = (v.get("status") or "").lower().strip()
        if plate_norm and plate_norm in rented_norm:
            rented_v.append(v)
        elif avail in ("garage",) or status_raw in ("garage",):
            garage_v.append(v)
        elif avail in ("service","maintenance") or status_raw in ("service","maintenance"):
            service_v.append(v)
        else:
            available_v.append(v)

    total     = len(vehicles)
    rented    = len(rented_v)
    available = len(available_v)
    garage    = len(garage_v)
    service   = len(service_v)
    print(f"Fleet — Total:{total} | Rented:{rented} | Available:{available} | Garage:{garage} | Service:{service}")

    summary = "\n".join([
        f"❝{date_str} {day_str}❞","",
        f"✦ Total        : {total}","",
        f"✦ Rented STR   : {rented}","",
        f"✦ Garage       : {garage}","",
        f"✦ Service      : {service}","",
        f"✦ Available    : {available}","",
        f"✦ Lease        : 0","",
        f"✦ Longterm     : 0","",
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
    lines += ["","For inquiries please contact this number","", CONTACT_FOOTER]

    blocks = [
        {"type":"section","text":{"type":"mrkdwn","text":f"```{summary}```"}},
        {"type":"divider"},
        *text_to_blocks("\n".join(lines)),
        {"type":"context","elements":[{"type":"mrkdwn",
          "text":"MKV Active Fleet • Auto-posted daily 10:00 AM Dubai time"}]},
    ]
    return {"blocks": blocks, "text": f"MKV Fleet Availability — {date_str} {day_str}"}

def post_slack(message):
    r = requests.post(
        "https://slack.com/api/chat.postMessage",
        headers={"Authorization":f"Bearer {SLACK_TOKEN}","Content-Type":"application/json"},
        data=json.dumps({"channel":SLACK_CHANNEL,"username":"MKV Fleet Status",
                         "icon_emoji":":car:","unfurl_links":False,"unfurl_media":False,
                         **message}),
        timeout=15,
    )
    res = r.json()
    if not res.get("ok"):
        print(f"Slack error: {res.get('error')}")
        raise SystemExit(1)
    print("✅ Posted to Slack")

if __name__ == "__main__":
    print("="*50)
    print("MKV FLEET AVAILABILITY")
    print("="*50)
    rented_plates = fetch_rented_plates()
    vehicles      = fetch_vehicles()
    if not vehicles:
        print("No vehicles returned from API")
        raise SystemExit(1)
    msg = build_message(vehicles, rented_plates)
    post_slack(msg)
