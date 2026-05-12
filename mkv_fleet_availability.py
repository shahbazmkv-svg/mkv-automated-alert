"""
MKV Luxury – Fleet Availability
=================================
API 1: get-mkv-vehicles.php          → MKV fleet list (any rate > 0 = 62 vehicles)
API 2: get-mkv-available-vehicle.php → per-vehicle status for all 62 vehicles
API 3: get-mkv-bookings.php          → rental type (STR / Lease / LTR) for booked vehicles

Flow:
  Step 1 → get 62 vehicles
  Step 2 → call availability API per vehicle → available / booked / service / nrv
  Step 3 → for booked vehicles → get rental type from bookings API
  Step 4 → build Slack message:
             Summary  = all counts (Total / STR / Lease / LTR / Available / Service / NRV)
             List     = AVAILABLE vehicles only
"""
import os, json, re, requests
from datetime import datetime, timezone, timedelta

SLACK_TOKEN   = os.environ["SLACK_BOT_TOKEN"]
APPIC_KEY     = os.environ.get("APPIC_KEY", "")
SLACK_CHANNEL = "C0B0TGBDCDU"   # #mkvtest
DUBAI_TZ      = timezone(timedelta(hours=4))
BLOCK_LIMIT   = 2900

BASE_URL         = "https://www.appicfleet.com/appiccar-apis-mkv"
MKV_VEHICLES_URL = f"{BASE_URL}/get-mkv-vehicles.php"
MKV_AVAIL_URL    = f"{BASE_URL}/get-mkv-available-vehicle.php"
BOOKINGS_URL     = f"{BASE_URL}/get-mkv-bookings.php"

CONTACT_FOOTER = (
    "📱 +971 56 279 4545\n☎️  +971 4 238 8987\n"
    "🌐 https://www.mkvluxury.com/\n📸 https://www.instagram.com/mkvluxurydubai/\n"
    "✉️  contact@mkvluxury.com\n📍 Al Jreena Street 41, Al Qouz Industrial Third, Dubai, UAE"
)

def now_dubai():
    return datetime.now(DUBAI_TZ)

def parse_date(s):
    if not s:
        return None
    s = str(s).strip()
    for fmt in ("%Y-%m-%d", "%Y-%m-%d %H:%M:%S", "%d/%m/%Y", "%m/%d/%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None

def normalize_plate(plate: str) -> str:
    digits = re.sub(r"[^0-9]", "", str(plate))
    return digits.lstrip("0") if digits else str(plate)

def get_plate(v: dict) -> str:
    for key in ["plate", "vehiclePlate", "plateNo", "plate_no"]:
        val = str(v.get(key, "") or "").strip()
        if val and val != "0":
            return val
    return ""

def get_vehicle_id(v: dict) -> str:
    for k in ["vehicleID", "id", "vehicleId", "vehicle_id"]:
        val = str(v.get(k, "") or "").strip()
        if val and val not in ("0", ""):
            return val
    return ""

# ─────────────────────────────────────────────────────────
# 1. Fetch MKV fleet (any rate > 0)
# ─────────────────────────────────────────────────────────
def fetch_vehicles() -> list:
    try:
        r     = requests.post(MKV_VEHICLES_URL, data={"key": APPIC_KEY}, timeout=20)
        r.raise_for_status()
        resp  = r.json()
        all_v = resp if isinstance(resp, list) else resp.get("data", resp.get("vehicles", []))
        active = [
            v for v in all_v
            if float(v.get("dailyrent",   0) or 0) > 0
            or float(v.get("weeklyrent",  0) or 0) > 0
            or float(v.get("monthlyrent", 0) or 0) > 0
        ]
        print(f"  Vehicles total : {len(all_v)} | MKV fleet: {len(active)}")
        return active
    except Exception as ex:
        print(f"  ❌ Vehicles API error: {ex}")
        return []

# ─────────────────────────────────────────────────────────
# 2. Get per-vehicle status from availability API
#    Returns: { vehicleID -> "available" | "booked" | "service" | "nrv" }
# ─────────────────────────────────────────────────────────
def fetch_vehicle_statuses(vehicles: list) -> dict:
    today_str    = now_dubai().strftime("%Y-%m-%d")
    tomorrow_str = (now_dubai() + timedelta(days=1)).strftime("%Y-%m-%d")
    statuses     = {}

    for v in vehicles:
        vid = get_vehicle_id(v)
        if not vid:
            statuses[vid] = "available"
            continue
        try:
            r = requests.post(MKV_AVAIL_URL, data={
                "key":       APPIC_KEY,
                "startDate": today_str,
                "endDate":   tomorrow_str,
                "vehicleID": vid
            }, timeout=15)
            r.raise_for_status()
            resp      = r.json()
            raw       = str(resp.get("status", "") or "").lower().strip()
            is_booked = resp.get("isBooked", False)

            if "accident" in raw or "damage" in raw:
                statuses[vid] = "nrv"
            elif "service" in raw or "garage" in raw:
                statuses[vid] = "service"
            elif is_booked or "booked" in raw or "rented" in raw:
                statuses[vid] = "booked"
            elif "available" in raw:
                statuses[vid] = "available"
            else:
                statuses[vid] = "available"
                print(f"  ⚠️  Unknown status vehicleID={vid} plate={get_plate(v)}: '{raw}'")

        except Exception as ex:
            print(f"  ⚠️  Error vehicleID={vid}: {ex}")
            statuses[vid] = "available"

    from collections import Counter
    counts = Counter(statuses.values())
    print(f"  Status counts  : {dict(counts)}")
    return statuses

# ─────────────────────────────────────────────────────────
# 3. Get rental type for booked vehicles from bookings API
#    Returns: { normalized_plate -> "str" | "lease" | "ltr" }
# ─────────────────────────────────────────────────────────
def fetch_rental_types(str_plates_norm: set) -> dict:
    """
    Uses rentalType field from bookings API if available.
    Falls back to duration:
      STR : 1–30  days
      Lease: 31–365 days
      LTR : 366+  days
    """
    PRIORITY = {"ltr": 3, "lease": 2, "str": 1}
    try:
        today     = now_dubai().date()
        start_str = (now_dubai() - timedelta(days=730)).strftime("%Y-%m-%d")
        end_str   = (now_dubai() + timedelta(days=730)).strftime("%Y-%m-%d")

        r = requests.post(BOOKINGS_URL, data={
            "key": APPIC_KEY, "startDate": start_str, "endDate": end_str
        }, timeout=20)
        r.raise_for_status()
        bookings = r.json().get("bookings", [])
        print(f"  Bookings total : {len(bookings)}")

        plate_type = {}

        for b in bookings:
            status = str(b.get("status") or b.get("bookingStatus") or "").lower().strip()
            if status in ("cancelled", "canceled", "voided", "void", "deleted"):
                continue

            plate = str(b.get("vehiclePlate", "") or "").strip()
            if not plate:
                continue

            norm = normalize_plate(plate)
            if norm not in str_plates_norm:
                continue

            sd = parse_date(b.get("startDate"))
            ed = parse_date(b.get("endDate"))
            if sd is None or ed is None:
                continue
            if not (sd <= today <= ed):
                continue

            # Try rentalType field first (exact Appic field)
            rental_type_raw = str(
                b.get("rentalType") or
                b.get("rental_type") or
                b.get("contractType") or
                b.get("type") or ""
            ).lower().strip()

            if "long" in rental_type_raw:
                ctype = "ltr"
            elif "lease" in rental_type_raw:
                ctype = "lease"
            elif "short" in rental_type_raw:
                ctype = "str"
            else:
                # Fallback: duration-based
                duration = (ed - sd).days
                ctype = "ltr" if duration >= 366 else "lease" if duration >= 31 else "str"

            if norm not in plate_type or PRIORITY[ctype] > PRIORITY[plate_type[norm]]:
                plate_type[norm] = ctype
                print(f"  RENTAL TYPE: plate={plate:<12} | rentalField='{rental_type_raw}' | type={ctype.upper()}")

        str_c   = sum(1 for t in plate_type.values() if t == "str")
        lease_c = sum(1 for t in plate_type.values() if t == "lease")
        ltr_c   = sum(1 for t in plate_type.values() if t == "ltr")
        print(f"  Rental types   : STR={str_c} | Lease={lease_c} | LTR={ltr_c}")
        return plate_type

    except Exception as ex:
        print(f"  ❌ Bookings API error: {ex}")
        return {}

# ─────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────
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

# ─────────────────────────────────────────────────────────
# 4. Build Slack message
# ─────────────────────────────────────────────────────────
def build_message(vehicles, vehicle_statuses, rental_types):
    now      = now_dubai()
    date_str = now.strftime("%B %d, %Y").upper()
    day_str  = now.strftime("%A").upper()

    str_v       = []
    lease_v     = []
    ltr_v       = []
    available_v = []
    service_v   = []
    nrv_v       = []

    for v in vehicles:
        vid        = get_vehicle_id(v)
        plate_norm = normalize_plate(get_plate(v))
        status     = vehicle_statuses.get(vid, "available")
        rtype      = rental_types.get(plate_norm, "str")

        if status == "service":
            service_v.append(v)
        elif status == "nrv":
            nrv_v.append(v)
        elif status == "booked":
            if rtype == "ltr":
                ltr_v.append(v)
            elif rtype == "lease":
                lease_v.append(v)
            else:
                str_v.append(v)
        else:
            available_v.append(v)

    total   = len(vehicles)
    str_c   = len(str_v)
    lease_c = len(lease_v)
    ltr_c   = len(ltr_v)
    avail_c = len(available_v)
    svc_c   = len(service_v)
    nrv_c   = len(nrv_v)

    print(f"\n  ┌─────────────────────────────────┐")
    print(f"  │ FLEET SUMMARY                   │")
    print(f"  │ Total    : {total:<22} │")
    print(f"  │ STR      : {str_c:<22} │")
    print(f"  │ Lease    : {lease_c:<22} │")
    print(f"  │ LTR      : {ltr_c:<22} │")
    print(f"  │ Available: {avail_c:<22} │")
    print(f"  │ Service  : {svc_c:<22} │")
    print(f"  │ NRV      : {nrv_c:<22} │")
    print(f"  └─────────────────────────────────┘")

    summary = "\n".join([
        f"❝{date_str} {day_str}❞", "",
        f"✦ Total        : {total}", "",
        f"✦ Rented STR   : {str_c}", "",
        f"✦ Service      : {svc_c}", "",
        f"✦ Available    : {avail_c}", "",
        f"✦ Lease        : {lease_c}", "",
        f"✦ Longterm     : {ltr_c}", "",
        f"✦ NRV          : {nrv_c}",
    ])

    lines = []
    if available_v:
        lines.append("AVAILABLE")
        lines.append("-" * 30)
        for i, v in enumerate(available_v, 1):
            lines.append(f"{i}. {fmt_name(v)}")
        lines.append("")

    lines += ["For inquiries please contact this number", "", CONTACT_FOOTER]

    blocks = [
        {"type": "section", "text": {"type": "mrkdwn", "text": f"```{summary}```"}},
        {"type": "divider"},
        *text_to_blocks("\n".join(lines)),
        {"type": "context", "elements": [{"type": "mrkdwn",
            "text": "MKV Active Fleet • Auto-posted daily 10:00 AM Dubai time"}]},
    ]
    return {"blocks": blocks, "text": f"MKV Fleet Availability — {date_str} {day_str}"}

# ─────────────────────────────────────────────────────────
# 5. Post to Slack
# ─────────────────────────────────────────────────────────
def post_slack(message):
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
    res = r.json()
    if not res.get("ok"):
        print(f"  ❌ Slack error: {res.get('error')}")
        raise SystemExit(1)
    print("  ✅ Posted to Slack successfully")

# ─────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("=" * 55)
    print("  MKV FLEET AVAILABILITY")
    print(f"  {now_dubai().strftime('%d %b %Y | %I:%M %p Dubai Time')}")
    print("=" * 55)

    print("\n[1] Fetching MKV fleet ...")
    vehicles = fetch_vehicles()
    if not vehicles:
        print("  No vehicles returned — exiting")
        raise SystemExit(1)

    plates_norm = set(normalize_plate(get_plate(v)) for v in vehicles)

    print("\n[2] Fetching per-vehicle availability status ...")
    vehicle_statuses = fetch_vehicle_statuses(vehicles)

    print("\n[3] Fetching rental types for booked vehicles ...")
    rental_types = fetch_rental_types(plates_norm)

    print("\n[4] Building Slack message ...")
    msg = build_message(vehicles, vehicle_statuses, rental_types)

    print("\n[5] Posting to Slack ...")
    post_slack(msg)
