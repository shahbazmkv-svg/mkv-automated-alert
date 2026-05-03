"""
MKV Luxury – Fleet Availability
Uses all 3 Appic APIs:
  1. get-all-vehicles          — full fleet, filter dailyrent > 0
  2. get-mkv-bookings          — active contracts → rented / delivery / returning
  3. get-mkv-checkin-checkout  — physical check-outs (Out) and check-ins (In) today
  4. get-mkv-vehicle-assignments — assignments (fallback cross-check)

Classification (dailyrent > 0 only):
  DELIVERY TODAY  — checkout direction=Out today (physically leaving today)
  RETURNING TODAY — checkin direction=In today  (physically returning today)
  RENTED STR      — active contract today, not checking in/out today
  AVAILABLE       — no active contract today
  GARAGE/SERVICE  — per vehicle status field
"""
import os, json, re, requests
from datetime import datetime, timezone, timedelta

SLACK_TOKEN   = os.environ["SLACK_BOT_TOKEN"]
APPIC_KEY     = os.environ.get("APPIC_KEY", "96QQYxPRVRTiHjL0tEmgP0cr5FkLvED0")
SLACK_CHANNEL = "C0B0TGBDCDU"
DUBAI_TZ      = timezone(timedelta(hours=4))
BLOCK_LIMIT   = 2900

ALL_VEHICLES_URL  = "https://www.appicfleet.com/appiccar-apis-mkv/get-all-vehicles.php"
BOOKINGS_URL      = "https://www.appicfleet.com/appiccar-apis-mkv/get-mkv-bookings.php"
CHECKINOUT_URL    = "https://www.appicfleet.com/appiccar-apis-mkv/get-mkv-checkin-checkout.php"
ASSIGNMENTS_URL   = "https://www.appicfleet.com/appiccar-apis-mkv/get-mkv-vehicle-assignments.php"

CONTACT_FOOTER = (
    "📱 +971 52 940 9280\n📱 +971 56 279 4545\n☎️  +971 4 238 8987\n"
    "🌐 https://www.mkvluxury.com/\n📸 https://www.instagram.com/mkvluxurydubai/\n"
    "✉️  contact@mkvluxury.com\n📍 Al Jreena Street 41, Al Qouz Industrial Third, Dubai, UAE"
)

DEAD_STATUSES = {"cancelled", "canceled", "voided", "void", "deleted", "rejected"}

def now_dubai():
    return datetime.now(DUBAI_TZ)

def parse_date(date_str: str):
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
    """Uppercase, strip spaces/hyphens — preserve all letters.
    'BB 60137' → 'BB60137'  |  'I 47203' → 'I47203'
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
# API CALLS
# ══════════════════════════════════════════════════════════════════════════════

def fetch_vehicles() -> list:
    """All vehicles with dailyrent > 0."""
    try:
        r = requests.get(ALL_VEHICLES_URL,
                         headers={"User-Agent": "Mozilla/5.0"}, timeout=20)
        r.raise_for_status()
        raw   = r.json()
        all_v = raw.get("data", [])
        active = [v for v in all_v if float(v.get("dailyrent", 0) or 0) > 0]
        print(f"  Vehicles: total={len(all_v)} | dailyrent>0={len(active)}")
        if all_v:
            print(f"  Vehicle keys: {list(all_v[0].keys())}")
            sample = all_v[0]
            print(f"  Sample plate fields: plate={sample.get('plate')} "
                  f"vehiclePlate={sample.get('vehiclePlate')} "
                  f"plateNo={sample.get('plateNo')} "
                  f"availability={sample.get('availability')} "
                  f"status={sample.get('status')}")
        return active
    except Exception as ex:
        print(f"  [ERROR] Vehicles API: {ex}")
        return []


def fetch_bookings(today) -> set:
    """
    Return set of normalized plates that have an ACTIVE contract today.
    Active = start_date <= today <= end_date AND status not dead.
    This is the base 'rented' set — delivery/returning will be split out by checkin/checkout API.
    """
    start_str = (now_dubai() - timedelta(days=365)).strftime("%Y-%m-%d")
    end_str   = (now_dubai() + timedelta(days=365)).strftime("%Y-%m-%d")
    try:
        r = requests.post(BOOKINGS_URL,
                          data={"key": APPIC_KEY, "startDate": start_str, "endDate": end_str},
                          timeout=20)
        r.raise_for_status()
        raw      = r.json()
        bookings = raw.get("bookings", [])
        print(f"  Bookings API: {len(bookings)} records")

        # DEBUG — print all unique status values and first 2 full records
        statuses = set(str(b.get("status") or b.get("bookingStatus") or "").lower()
                       for b in bookings)
        print(f"  Booking statuses seen: {statuses}")
        if bookings:
            print(f"  Booking keys: {list(bookings[0].keys())}")
            for b in bookings[:2]:
                print(f"  SAMPLE BOOKING: {json.dumps(b)}")

        active_plates = set()
        for b in bookings:
            status = str(b.get("status") or b.get("bookingStatus") or "").lower().strip()
            if status in DEAD_STATUSES:
                continue
            plate = str(b.get("vehiclePlate", "") or "").strip()
            if not plate:
                continue
            s = parse_date(b.get("startDate") or "")
            e = parse_date(b.get("endDate")   or "")
            if s and e and s <= today <= e:
                active_plates.add(normalize_plate(plate))

        print(f"  Active contract plates today: {len(active_plates)} → {active_plates}")
        return active_plates

    except Exception as ex:
        print(f"  [ERROR] Bookings API: {ex}")
        import traceback; traceback.print_exc()
        return set()


def fetch_checkinout(today, direction: str) -> set:
    """
    Return normalized plates checked-out (direction='Out') or
    checked-in (direction='In') today.
    """
    today_str = today.strftime("%Y-%m-%d")
    try:
        r = requests.post(CHECKINOUT_URL,
                          data={"key": APPIC_KEY,
                                "startDate": today_str,
                                "endDate":   today_str,
                                "direction": direction},
                          timeout=20)
        r.raise_for_status()
        raw     = r.json()
        print(f"  CheckIn/Out [{direction}] raw keys: {list(raw.keys())}")
        print(f"  CheckIn/Out [{direction}] full response (first 1000): {json.dumps(raw)[:1000]}")

        # Try common root keys
        records = (raw.get("data") or raw.get("records") or
                   raw.get("checkouts") or raw.get("checkins") or
                   raw.get("results") or [])

        if not isinstance(records, list):
            print(f"  [WARN] Unexpected checkinout structure: {type(records)}")
            records = []

        plates = set()
        for rec in records:
            # Try all plate key variants
            plate = str(rec.get("vehiclePlate") or rec.get("plate") or
                        rec.get("plateNo") or rec.get("plate_no") or "").strip()
            if plate:
                plates.add(normalize_plate(plate))

        print(f"  {direction} plates today: {len(plates)} → {plates}")
        return plates

    except Exception as ex:
        print(f"  [ERROR] CheckIn/Out [{direction}] API: {ex}")
        import traceback; traceback.print_exc()
        return set()


def fetch_assignments() -> set:
    """
    Vehicle assignments — returns plates currently assigned (i.e. on contract).
    Used as a cross-check against bookings.
    """
    try:
        r = requests.post(ASSIGNMENTS_URL,
                          data={"key": APPIC_KEY},
                          timeout=20)
        r.raise_for_status()
        raw = r.json()
        print(f"  Assignments raw keys: {list(raw.keys())}")
        print(f"  Assignments full response (first 1000): {json.dumps(raw)[:1000]}")

        records = (raw.get("data") or raw.get("assignments") or
                   raw.get("records") or [])
        if not isinstance(records, list):
            print(f"  [WARN] Unexpected assignments structure")
            records = []

        plates = set()
        for rec in records:
            plate = str(rec.get("vehiclePlate") or rec.get("plate") or
                        rec.get("plateNo") or "").strip()
            if plate:
                plates.add(normalize_plate(plate))

        print(f"  Assignment plates: {len(plates)} → {plates}")
        return plates

    except Exception as ex:
        print(f"  [ERROR] Assignments API: {ex}")
        import traceback; traceback.print_exc()
        return set()


# ══════════════════════════════════════════════════════════════════════════════
# CLASSIFICATION
# ══════════════════════════════════════════════════════════════════════════════

def classify_vehicles(vehicles, active_plates, checkout_plates, checkin_plates):
    """
    active_plates   = plates with a live contract today (from bookings API)
    checkout_plates = plates physically leaving today (checkin/out direction=Out)
    checkin_plates  = plates physically returning today (checkin/out direction=In)

    Rules:
      1. plate in checkout_plates            → DELIVERY TODAY
      2. plate in checkin_plates             → RETURNING TODAY
      3. plate in active_plates (not above)  → RENTED STR
      4. vehicle availability/status=garage  → GARAGE
      5. vehicle availability/status=service → SERVICE
      6. none of the above                   → AVAILABLE
    """
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

        if plate_norm and plate_norm in checkout_plates:
            delivery_v.append(v)
        elif plate_norm and plate_norm in checkin_plates:
            returning_v.append(v)
        elif plate_norm and plate_norm in active_plates:
            rented_v.append(v)
        elif avail in ("garage",) or status_raw in ("garage",):
            garage_v.append(v)
        elif avail in ("service", "maintenance") or status_raw in ("service", "maintenance"):
            service_v.append(v)
        else:
            available_v.append(v)

    print(f"  Classification — Delivery:{len(delivery_v)} | Returning:{len(returning_v)} | "
          f"Rented:{len(rented_v)} | Available:{len(available_v)} | "
          f"Garage:{len(garage_v)} | Service:{len(service_v)}")

    return delivery_v, returning_v, rented_v, available_v, garage_v, service_v


# ══════════════════════════════════════════════════════════════════════════════
# SLACK MESSAGE
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


def build_message(vehicles, active_plates, checkout_plates, checkin_plates):
    now      = now_dubai()
    date_str = now.strftime("%B %d, %Y").upper()
    day_str  = now.strftime("%A").upper()

    (delivery_v, returning_v, rented_v,
     available_v, garage_v, service_v) = classify_vehicles(
        vehicles, active_plates, checkout_plates, checkin_plates)

    total     = len(vehicles)
    n_rented  = len(rented_v)
    n_deliver = len(delivery_v)
    n_return  = len(returning_v)
    n_avail   = len(available_v)
    n_garage  = len(garage_v)
    n_service = len(service_v)

    summary = "\n".join([
        f"❝{date_str} {day_str}❞", "",
        f"✦ Total           : {total}", "",
        f"✦ Rented STR      : {n_rented}", "",
        f"✦ Delivery Today  : {n_deliver}", "",
        f"✦ Returning Today : {n_return}", "",
        f"✦ Available       : {n_avail}", "",
        f"✦ Garage          : {n_garage}", "",
        f"✦ Service         : {n_service}", "",
        f"✦ Lease           : 0", "",
        f"✦ Longterm        : 0", "",
        f"✦ NRV             : 0",
    ])

    lines = []

    def section(emoji, title, lst):
        if not lst: return
        if lines: lines.append("")
        lines.append(f"{emoji} {title}")
        lines.append("-" * 30)
        for i, v in enumerate(lst, 1):
            lines.append(f"{i}. {fmt_name(v)}")

    section("🚗", "DELIVERY TODAY",  delivery_v)
    section("🔁", "RETURNING TODAY", returning_v)
    section("✅", "AVAILABLE",       available_v)
    section("🔧", "GARAGE",          garage_v)
    section("⚙️", "SERVICE",         service_v)

    lines += ["", "For inquiries please contact us", "", CONTACT_FOOTER]

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
    print("=" * 60)
    print("  MKV FLEET AVAILABILITY")
    print("  " + now_dubai().strftime("%d %b %Y | %I:%M %p Dubai Time"))
    print("=" * 60)

    today = now_dubai().date()

    print("\n[1/5] Fetching vehicles (dailyrent > 0)...")
    vehicles = fetch_vehicles()
    if not vehicles:
        print("  No vehicles — aborting")
        raise SystemExit(1)

    print("\n[2/5] Fetching active contracts (bookings)...")
    active_plates = fetch_bookings(today)

    print("\n[3/5] Fetching check-outs today (delivery)...")
    checkout_plates = fetch_checkinout(today, "Out")

    print("\n[4/5] Fetching check-ins today (returning)...")
    checkin_plates = fetch_checkinout(today, "In")

    print("\n[5/5] Building and posting to Slack...")
    msg = build_message(vehicles, active_plates, checkout_plates, checkin_plates)
    post_slack(msg)

    print("\n" + "=" * 60)
    print("  Done.")
    print("=" * 60)
