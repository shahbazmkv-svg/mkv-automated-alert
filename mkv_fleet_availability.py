"""
MKV Luxury – Fleet Availability
Pulls from Appic Bookings + Vehicles APIs.

Classification logic (dailyrent > 0 vehicles only):
  DELIVERY TODAY  — contract starts today
  RETURNING TODAY — contract ends today
  RENTED STR      — active contract (start < today < end), not returning today
  AVAILABLE       — no active / draft contract valid for today
  GARAGE          — availability/status field = "garage"
  SERVICE         — availability/status field = "service" / "maintenance"
"""
import os, json, re, requests
from datetime import datetime, timezone, timedelta

SLACK_TOKEN   = os.environ["SLACK_BOT_TOKEN"]
APPIC_KEY     = os.environ.get("APPIC_KEY", "")
SLACK_CHANNEL = "C0B0TGBDCDU"
DUBAI_TZ      = timezone(timedelta(hours=4))
BLOCK_LIMIT   = 2900

ALL_VEHICLES_URL = "https://www.appicfleet.com/appiccar-apis-mkv/get-all-vehicles.php"
BOOKINGS_URL     = "https://www.appicfleet.com/appiccar-apis-mkv/get-mkv-bookings.php"

CONTACT_FOOTER = (
    "📱 +971 52 940 9280\n📱 +971 56 279 4545\n☎️  +971 4 238 8987\n"
    "🌐 https://www.mkvluxury.com/\n📸 https://www.instagram.com/mkvluxurydubai/\n"
    "✉️  contact@mkvluxury.com\n📍 Al Jreena Street 41, Al Qouz Industrial Third, Dubai, UAE"
)

# Statuses that mean the booking is NOT live
DEAD_STATUSES = {"cancelled", "canceled", "voided", "void", "deleted", "rejected"}

def now_dubai():
    return datetime.now(DUBAI_TZ)

def parse_date(date_str: str):
    """Parse any common date format → date object. Returns None on failure."""
    if not date_str:
        return None
    date_str = date_str.strip()
    for fmt in ("%Y-%m-%d", "%Y-%m-%d %H:%M:%S", "%d/%m/%Y", "%m/%d/%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(date_str, fmt).date()
        except ValueError:
            continue
    return None

def normalize_plate(plate: str) -> str:
    """
    Uppercase, strip spaces/hyphens — preserve letters.
    'BB 60137' → 'BB60137'   'I 47203' → 'I47203'
    Different letter prefixes = different plates, must NOT collide.
    """
    return re.sub(r"[\s\-]", "", plate).upper().strip()

def get_plate(v: dict) -> str:
    for key in ["plate", "vehiclePlate", "plateNo", "plate_no", "number_plate", "licensePlate"]:
        val = str(v.get(key, "") or "").strip()
        if val and val != "0":
            return val
    return ""

def fmt_name(v: dict) -> str:
    make  = v.get("make",  "").strip().upper()
    model = v.get("model", "").strip().upper()
    year  = str(v.get("year", "")).strip()
    color = v.get("color", "").strip().upper()
    plate = get_plate(v)
    if model.startswith(make):
        model = model[len(make):].strip()
    parts = [p for p in [make, model] if p]
    if year and year not in ("0", "200", ""):
        parts.append(f"({year})")
    if color and color not in ("", "N/A"):
        parts.append(color)
    if plate:
        parts.append(f"[{plate}]")
    return " ".join(parts) or "UNKNOWN"

# ══════════════════════════════════════════════════════════════════════════════
# FETCH
# ══════════════════════════════════════════════════════════════════════════════

def fetch_vehicles() -> list:
    """Return all vehicles with dailyrent > 0."""
    try:
        r = requests.get(ALL_VEHICLES_URL,
                         headers={"User-Agent": "Mozilla/5.0"}, timeout=20)
        r.raise_for_status()
        all_v  = r.json().get("data", [])
        active = [v for v in all_v if float(v.get("dailyrent", 0) or 0) > 0]
        print(f"  Vehicles — total: {len(all_v)} | dailyrent>0: {len(active)}")
        if active:
            print(f"  Sample keys: {list(active[0].keys())}")
        return active
    except Exception as ex:
        print(f"  Vehicles API error: {ex}")
        return []

def fetch_bookings_today() -> dict:
    """
    Fetch all bookings from Appic and classify each plate into one of:
      'delivery'  — start date == today  (contract begins today)
      'returning' — end date   == today  (contract ends today)
      'rented'    — start < today < end  (mid-contract, not returning today)

    Returns:
      {
        "delivery":  set of normalized plates,
        "returning": set of normalized plates,
        "rented":    set of normalized plates,
      }

    Priority: delivery > returning > rented
    (A car delivering and returning same day is an edge case; treat as delivery.)
    """
    today     = now_dubai().date()
    start_str = (now_dubai() - timedelta(days=365)).strftime("%Y-%m-%d")
    end_str   = (now_dubai() + timedelta(days=365)).strftime("%Y-%m-%d")

    try:
        r = requests.post(BOOKINGS_URL, data={
            "key": APPIC_KEY, "startDate": start_str, "endDate": end_str
        }, timeout=20)
        r.raise_for_status()
        bookings = r.json().get("bookings", [])
        print(f"  Bookings from API: {len(bookings)}")

        # Debug: print unique status values seen
        statuses = set(str(b.get("status") or b.get("bookingStatus") or "").lower().strip()
                       for b in bookings)
        print(f"  Booking statuses seen: {statuses}")

    except Exception as ex:
        print(f"  Bookings API error: {ex}")
        return {"delivery": set(), "returning": set(), "rented": set()}

    delivery  = set()
    returning = set()
    rented    = set()

    for b in bookings:
        # Skip dead statuses
        status = str(b.get("status") or b.get("bookingStatus") or "").lower().strip()
        if status in DEAD_STATUSES:
            continue

        plate = str(b.get("vehiclePlate", "") or "").strip()
        if not plate:
            continue
        plate_norm = normalize_plate(plate)

        start_date = parse_date(b.get("startDate") or "")
        end_date   = parse_date(b.get("endDate")   or "")

        if start_date is None or end_date is None:
            continue

        # Only care about contracts that are active today or touching today
        if not (start_date <= today <= end_date):
            continue

        # Classify by date position
        if start_date == today:
            delivery.add(plate_norm)
        elif end_date == today:
            returning.add(plate_norm)
        else:
            # start < today < end — mid-contract
            rented.add(plate_norm)

    # A plate in multiple buckets: delivery takes priority, then returning
    # (e.g. same car returned & redelivered same day — show as delivery)
    returning -= delivery
    rented    -= delivery
    rented    -= returning

    print(f"  Delivery today:  {len(delivery)}")
    print(f"  Returning today: {len(returning)}")
    print(f"  Rented (mid):    {len(rented)}")

    return {"delivery": delivery, "returning": returning, "rented": rented}

# ══════════════════════════════════════════════════════════════════════════════
# BUILD MESSAGE
# ══════════════════════════════════════════════════════════════════════════════

def text_to_blocks(text):
    blocks, chunk = [], ""
    for line in text.split("\n"):
        cand = chunk + line + "\n"
        if len(cand) > BLOCK_LIMIT:
            if chunk:
                blocks.append({"type": "section", "text": {"type": "mrkdwn",
                                "text": f"```{chunk.rstrip()}```"}})
            chunk = line + "\n"
        else:
            chunk = cand
    if chunk.strip():
        blocks.append({"type": "section", "text": {"type": "mrkdwn",
                        "text": f"```{chunk.rstrip()}```"}})
    return blocks

def build_message(vehicles, bookings):
    now      = now_dubai()
    date_str = now.strftime("%B %d, %Y").upper()
    day_str  = now.strftime("%A").upper()

    delivery_plates  = bookings["delivery"]
    returning_plates = bookings["returning"]
    rented_plates    = bookings["rented"]

    delivery_v  = []
    returning_v = []
    rented_v    = []
    available_v = []
    garage_v    = []
    service_v   = []

    for v in vehicles:
        plate      = get_plate(v)
        plate_norm = normalize_plate(plate)
        avail      = (v.get("availability") or "").lower().strip()
        status_raw = (v.get("status")       or "").lower().strip()

        if plate_norm and plate_norm in delivery_plates:
            delivery_v.append(v)
        elif plate_norm and plate_norm in returning_plates:
            returning_v.append(v)
        elif plate_norm and plate_norm in rented_plates:
            rented_v.append(v)
        elif avail in ("garage",) or status_raw in ("garage",):
            garage_v.append(v)
        elif avail in ("service", "maintenance") or status_raw in ("service", "maintenance"):
            service_v.append(v)
        else:
            available_v.append(v)

    total     = len(vehicles)
    n_rented  = len(rented_v)
    n_deliver = len(delivery_v)
    n_return  = len(returning_v)
    n_avail   = len(available_v)
    n_garage  = len(garage_v)
    n_service = len(service_v)

    print(f"  Fleet — Total:{total} | Rented:{n_rented} | Delivery:{n_deliver} | "
          f"Returning:{n_return} | Available:{n_avail} | Garage:{n_garage} | Service:{n_service}")

    summary = "\n".join([
        f"❝{date_str} {day_str}❞", "",
        f"✦ Total          : {total}", "",
        f"✦ Rented STR     : {n_rented}", "",
        f"✦ Delivery Today : {n_deliver}", "",
        f"✦ Returning Today: {n_return}", "",
        f"✦ Available      : {n_avail}", "",
        f"✦ Garage         : {n_garage}", "",
        f"✦ Service        : {n_service}", "",
        f"✦ Lease          : 0", "",
        f"✦ Longterm       : 0", "",
        f"✦ NRV            : 0",
    ])

    lines = []

    if delivery_v:
        lines.append("🚗 DELIVERY TODAY")
        lines.append("-" * 30)
        for i, v in enumerate(delivery_v, 1):
            lines.append(f"{i}. {fmt_name(v)}")

    if returning_v:
        if lines: lines.append("")
        lines.append("🔁 RETURNING TODAY")
        lines.append("-" * 30)
        for i, v in enumerate(returning_v, 1):
            lines.append(f"{i}. {fmt_name(v)}")

    if available_v:
        if lines: lines.append("")
        lines.append("✅ AVAILABLE")
        lines.append("-" * 30)
        for i, v in enumerate(available_v, 1):
            lines.append(f"{i}. {fmt_name(v)}")

    if garage_v:
        if lines: lines.append("")
        lines.append("🔧 GARAGE")
        lines.append("-" * 30)
        for i, v in enumerate(garage_v, 1):
            lines.append(f"{i}. {fmt_name(v)}")

    if service_v:
        if lines: lines.append("")
        lines.append("⚙️ SERVICE")
        lines.append("-" * 30)
        for i, v in enumerate(service_v, 1):
            lines.append(f"{i}. {fmt_name(v)}")

    lines += ["", "For inquiries please contact us", "", CONTACT_FOOTER]

    blocks = [
        {"type": "section", "text": {"type": "mrkdwn", "text": f"```{summary}```"}},
        {"type": "divider"},
        *text_to_blocks("\n".join(lines)),
        {"type": "context", "elements": [{"type": "mrkdwn",
          "text": "MKV Active Fleet • Auto-posted daily 10:00 AM Dubai time"}]},
    ]
    return {"blocks": blocks, "text": f"MKV Fleet Availability — {date_str} {day_str}"}

# ══════════════════════════════════════════════════════════════════════════════
# POST TO SLACK
# ══════════════════════════════════════════════════════════════════════════════

def post_slack(message):
    r = requests.post(
        "https://slack.com/api/chat.postMessage",
        headers={"Authorization": f"Bearer {SLACK_TOKEN}", "Content-Type": "application/json"},
        data=json.dumps({"channel": SLACK_CHANNEL, "username": "MKV Fleet Status",
                         "icon_emoji": ":car:", "unfurl_links": False,
                         "unfurl_media": False, **message}),
        timeout=15,
    )
    res = r.json()
    if not res.get("ok"):
        print(f"  Slack error: {res.get('error')}")
        raise SystemExit(1)
    print("  ✅ Posted to Slack")

# ══════════════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("=" * 50)
    print("  MKV FLEET AVAILABILITY")
    print("  " + now_dubai().strftime("%d %b %Y | %I:%M %p Dubai Time"))
    print("=" * 50)

    print("\n[1/3] Fetching vehicles...")
    vehicles = fetch_vehicles()
    if not vehicles:
        print("  No vehicles returned — aborting")
        raise SystemExit(1)

    print("\n[2/3] Fetching bookings...")
    bookings = fetch_bookings_today()

    print("\n[3/3] Building and posting to Slack...")
    msg = build_message(vehicles, bookings)
    post_slack(msg)

    print("\n" + "=" * 50)
    print("  Done.")
    print("=" * 50)
